#!venv/bin/python
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, jsonify, abort, Response, stream_with_context, session
import os
import glob
import html
import time
import json
import re
import subprocess
import sys
from functools import lru_cache, wraps
from urllib.parse import parse_qs, unquote, urlparse, urlsplit, urlunsplit
import hashlib
import hmac
from werkzeug.utils import safe_join
from werkzeug.middleware.proxy_fix import ProxyFix
from config_util import get_playlist_max_items, get_ytdlp_config_path, load_config
import pytz
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth
from log_util import setup_logger
import ai_summary_store
import anonymous_cleanup
import task_queue
import user_store
import youtube_auth
import click
from flask.cli import with_appcontext

app = Flask(__name__, static_url_path='/static', static_folder='static')
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def get_release_commit():
    """返回当前部署版本；Git 不可用时返回 unknown。"""
    release = os.environ.get('DROPLOAD_RELEASE', '').strip()
    if release:
        return release
    try:
        return subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=PROJECT_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'

# 加载配置
config = load_config()

trusted_proxy_count = config.get('TRUSTED_PROXY_COUNT', 0)
if (
    isinstance(trusted_proxy_count, int)
    and not isinstance(trusted_proxy_count, bool)
    and trusted_proxy_count > 0
):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_count)

# 配置日志
logger = setup_logger(
    name='app',
    log_dir=config["LOG_DIR"],
    log_file='app.log',
    max_bytes=config["MAX_LOG_SIZE"],
    backup_count=config["BACKUP_COUNT"],
    timezone=config.get("TIMEZONE", "UTC")
)

# 将logger赋值给app.logger
app.logger = logger

# OAuth state 会话需要稳定密钥；未配置时生成临时密钥（多 worker 部署需在 config.json 配置）
app.secret_key = config.get("FLASK_SECRET_KEY") or os.urandom(24)
if not config.get("FLASK_SECRET_KEY"):
    app.logger.warning("FLASK_SECRET_KEY 未配置，OAuth state、匿名会话和按 IP 额度在重启或多 worker 部署下可能失效，请在 config.json 中设置稳定密钥。")

USER_DB_PATH = config["USER_DB_PATH"]
user_store.init_db(USER_DB_PATH)

# 会话 cookie 的基本加固；HTTPS 部署时由 SESSION_COOKIE_SECURE 打开 Secure。
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(config.get('SESSION_COOKIE_SECURE', False)),
)

URLS_DIR = config["URLS_DIR"]
FILES_DIR = config["FILES_DIR"]
# 兼容历史任务中曾使用过的数字随机后缀，同时限制为安全文件名字符。
TASK_ID_PATTERN = re.compile(r'^[va][A-Za-z0-9_-]{1,127}$')
# 状态文件表与下载器共用，含暂停状态 .paused
TASK_STATE_EXTENSIONS = task_queue.TASK_STATE_FILES
DOWNLOADER_LOG_INITIAL_BYTES = 64 * 1024
DOWNLOADER_LOG_MAX_BYTES = 128 * 1024
PROGRESS_MARKER = 'PYDL_PROGRESS|'
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
DEFAULT_PROGRESS_PATTERN = re.compile(
    r'\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%'
    r'(?:\s+of(?:\s+~)?\s+(?P<total>.+?))?'
    r'(?:\s+at\s+(?P<speed>.+?))?'
    r'(?:\s+ETA\s+(?P<eta>\S+))?$'
)
SUBTITLE_EXTENSIONS = {'ass', 'lrc', 'srt', 'ssa', 'ttml', 'vtt'}
VIDEO_SIDECAR_SUBTITLE_FORMATS = ('vtt', 'srt', 'ass', 'ssa', 'ttml')
VIDEO_SIDECAR_LANGUAGE_PREFERENCES = ('zh-hans', 'zh-hant', 'zh', 'en')
AUDIO_EXTENSIONS = {'aac', 'flac', 'm4a', 'mp3', 'ogg', 'opus', 'wav'}
VIDEO_EXTENSIONS = {'avi', 'flv', 'mkv', 'mov', 'mp4', 'webm'}
AUDIO_MIME_TYPES = {
    'aac': 'audio/aac',
    'flac': 'audio/flac',
    'm4a': 'audio/mp4',
    'mp3': 'audio/mpeg',
    'ogg': 'audio/ogg',
    'opus': 'audio/ogg',
    'wav': 'audio/wav',
}
LYRICS_EXTENSIONS = {'lrc', 'srt', 'vtt'}
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')
SUBTITLE_TIMESTAMP_PATTERN = re.compile(
    r'^(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+'
)
AI_SUMMARY_MAX_SUBTITLE_CHARS = 120000

# 保证文件夹存在
os.makedirs(URLS_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)
ai_summary_store.init_db(config["AI_SUMMARY_DB_PATH"])


# --- 认证与授权 ---------------------------------------------------------

SESSION_USER_KEY = 'user_id'
SESSION_ANONYMOUS_KEY = 'anonymous_id'


def current_user():
    """返回当前登录用户；未登录、已删除或被停用时返回 None。"""
    user_id = session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = user_store.get_user(USER_DB_PATH, user_id)
    if not user or user['status'] != user_store.STATUS_ACTIVE:
        # 账号被停用或删除后，旧 cookie 不应继续有效。
        session.pop(SESSION_USER_KEY, None)
        return None
    return user


def current_anonymous_id(create=True):
    """返回匿名会话标识；它只用于资源隔离，不用于每日额度。"""
    anonymous_id = session.get(SESSION_ANONYMOUS_KEY)
    if anonymous_id or not create:
        return anonymous_id
    anonymous_id = user_store.new_id()
    session[SESSION_ANONYMOUS_KEY] = anonymous_id
    session.permanent = True
    return anonymous_id


def current_owner(create_anonymous=True):
    """返回 ``(user|anonymous, id)`` 形式的当前资源主体。"""
    user = current_user()
    if user:
        return 'user', user['id']
    anonymous_id = current_anonymous_id(create=create_anonymous)
    return ('anonymous', anonymous_id) if anonymous_id else (None, None)


def anonymous_ip_hash():
    """对可信客户端 IP 做 HMAC，数据库不保存原始 IP。"""
    address = request.remote_addr or 'unknown'
    secret = app.secret_key
    if isinstance(secret, str):
        secret = secret.encode('utf-8')
    return hmac.new(secret, address.encode('utf-8'), hashlib.sha256).hexdigest()


def anonymous_day_key():
    timezone = pytz.timezone(config.get('TIMEZONE', 'UTC'))
    return datetime.now(timezone).strftime('%Y-%m-%d')


def anonymous_task_limit():
    value = config.get('ANONYMOUS_DAILY_TASK_LIMIT', 3)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 3
    return value


PENDING_TASK_KEY = 'pending_task'


def remember_pending_task(url, types):
    """记住匿名访客想创建的任务，登录或注册成功后立刻补建。"""
    session[PENDING_TASK_KEY] = {'url': url, 'types': list(types)}


def consume_pending_task(user):
    """取出并执行登录前暂存的任务；没有则返回 None。

    Returns:
        tuple | None: (任务 ID 列表, 原始 URL)；解析失败或无暂存时为 None。
    """
    pending = session.pop(PENDING_TASK_KEY, None)
    if not isinstance(pending, dict):
        return None
    url = extract_url(pending.get('url'))
    types = [t for t in (pending.get('types') or []) if t in {'video', 'audio'}]
    if not url or not types:
        return None

    urls, error = expand_task_urls(url)
    if error:
        app.logger.warning('登录后补建任务失败: %s', error)
        return None

    task_ids = create_tasks(urls, types)
    user_store.record_tasks(USER_DB_PATH, task_ids, user['id'], url=url)
    return task_ids, url


def login_user(user):
    # session.clear() 会一并清掉待建任务，所以先取出来再放回去
    pending = session.get(PENDING_TASK_KEY)
    anonymous_id = current_anonymous_id(create=False)
    if anonymous_id:
        user_store.transfer_anonymous_ownership(
            USER_DB_PATH, anonymous_id, user['id']
        )
    session.clear()
    session[SESSION_USER_KEY] = user['id']
    if pending is not None:
        session[PENDING_TASK_KEY] = pending
    session.permanent = True


def wants_json():
    """接口请求返回 401 JSON，页面请求重定向到登录页。"""
    return (
        request.path.startswith('/api/')
        or request.accept_mimetypes.best == 'application/json'
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            if wants_json():
                return jsonify({"success": False, "msg": "需要登录"}), 401
            return redirect(url_for('login', next=request.full_path))
        request.user = user
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            if wants_json():
                return jsonify({"success": False, "msg": "需要登录"}), 401
            return redirect(url_for('login', next=request.full_path))
        if user['role'] != user_store.ROLE_ADMIN:
            if wants_json():
                return jsonify({"success": False, "msg": "需要管理员权限"}), 403
            abort(403)
        request.user = user
        return view(*args, **kwargs)

    return wrapped


def safe_next_url(candidate):
    """只接受本站相对路径，避免 open redirect。"""
    if not candidate:
        return url_for('index')
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith('/'):
        return url_for('index')
    return candidate


@app.before_request
def redirect_localhost_to_loopback():
    """本机开发时把 localhost:端口 统一跳到 127.0.0.1:端口。

    浏览器把 localhost 与 127.0.0.1 当作两个站点，cookie 不共享；OAuth
    回调配置为 127.0.0.1 时，在 localhost 上登录会丢失会话。只在
    REDIRECT_LOCALHOST_TO_LOOPBACK 开启时生效：反向代理若以 localhost
    作为 Host 转发，默认开启会把线上访问者跳到他们自己电脑的 127.0.0.1。
    """
    if not config.get('REDIRECT_LOCALHOST_TO_LOOPBACK', False):
        return None
    hostname, _, port = request.host.partition(':')
    if hostname.lower() != 'localhost':
        return None
    netloc = '127.0.0.1' + (f':{port}' if port else '')
    target = urlunsplit(urlsplit(request.url)._replace(netloc=netloc))
    # 301 会让浏览器把 POST 改成 GET 并丢掉请求体，非 GET/HEAD 用 308 保留方法。
    status = 301 if request.method in ('GET', 'HEAD') else 308
    return redirect(target, code=status)


@app.route('/healthz', methods=['GET'])
def healthz():
    """供部署和反向代理使用的轻量应用健康检查。"""
    return jsonify({'status': 'ok', 'commit': get_release_commit()})

def get_file_hash(filepath):
    """获取文件的MD5哈希值"""
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()

@app.template_filter('versioned')
def versioned_static(filename):
    """生成带版本号的静态文件URL"""
    filepath = os.path.join(app.static_folder, filename)
    file_hash = get_file_hash(filepath)
    if file_hash:
        return f"{url_for('static', filename=filename)}?v={file_hash[:8]}"
    return url_for('static', filename=filename)

def extract_url(text):
    """从分享文本中提取URL

    支持的格式：
    - 直接的URL：https://example.com/video
    - 小红书分享：... http://xhslink.com/o/AxoI91g6MgD  ...
    - Bilibili分享：【视频标题】 https://b23.tv/Uxjn5Wc
    - 带口令的分享文本等

    Args:
        text (str): 包含URL的原始文本

    Returns:
        str: 提取出的URL，如果未找到返回原始文本
    """
    if not text:
        return text

    # URL正则表达式，匹配 http/https 开头的URL
    url_pattern = r'https?://[^\s\u4e00-\u9fa5\u3000-\u303f\uff00-\uffef]+'
    matches = re.findall(url_pattern, text)

    if matches:
        # 返回第一个匹配的URL，并去除末尾可能的标点符号
        return matches[0].rstrip('.,;:)]\'"。，；：）、）')

    return text

def get_current_time():
    timezone = pytz.timezone(config["TIMEZONE"])
    return datetime.now(timezone)


# 播放列表解析超时（秒）。解析依赖网络与 cookies，超时后明确报错而不是静默降级。
PLAYLIST_RESOLVE_TIMEOUT_SECONDS = 60

# 频道主页下被视为内容列表的标签页；community/about/search 等不算
CHANNEL_CONTENT_TABS = {'videos', 'shorts', 'streams', 'podcasts', 'releases'}


def _is_channel_content_path(path):
    """判断 YouTube 路径是否为频道主页或内容标签页（可逐集解析的列表）。"""
    path = (path or '').lower().rstrip('/')
    segments = [segment for segment in path.split('/') if segment]
    if not segments:
        return False
    if segments[0].startswith('@'):
        # /@handle 频道主页，或 /@handle/<内容标签页>
        if len(segments) == 1:
            return True
        return segments[1] in CHANNEL_CONTENT_TABS
    # /channel/<id>、/c/<name>、/user/<name> 及其任意子路径
    return segments[0] in {'channel', 'c', 'user'}


def _pick_ytdlp_conf(mode='video'):
    """选择 yt-dlp 配置文件，优先使用 .local.conf 本机覆盖版本。"""
    return get_ytdlp_config_path(mode)


def looks_like_playlist(url):
    """判断 URL 是否疑似播放列表，命中才执行 yt-dlp 解析，避免单视频提交变慢。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if not parsed.scheme or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip('.')
    if host in {
        'youtube.com', 'www.youtube.com', 'm.youtube.com',
        'music.youtube.com', 'youtube-nocookie.com',
    }:
        query = parse_qs(parsed.query)
        if query.get('list') and any(query['list']):
            return True
        path = parsed.path.lower()
        if (
            path.startswith('/playlist')
            or path.startswith('/playlists')
            or path.startswith('/mix')
        ):
            return True
        # 频道主页或内容标签页（/@handle/videos、/channel/UCxxx/videos 等）
        if _is_channel_content_path(path):
            return True
    return False


def resolve_playlist_urls(url, conf_path, max_items=None):
    """使用 yt-dlp flat-playlist 模式提取播放列表各条目 URL。

    Args:
        url (str): 播放列表 URL。
        conf_path (str): yt-dlp 配置文件路径（含 cookies 等）。
        max_items (int | None): 只解析前 N 个条目；None 表示不额外限制。

    Returns:
        tuple: (urls, error)。成功时 urls 为条目 URL 列表、error 为 None；
        失败时 urls 为 None、error 为错误说明。
    """
    cmd = [
        sys.executable, '-m', 'yt_dlp',
        '--config-location', conf_path,
        '--flat-playlist',
    ]
    if isinstance(max_items, int) and not isinstance(max_items, bool) and max_items > 0:
        cmd.extend(['--playlist-end', str(max_items)])
    cmd.extend([
        '--print', '%(id)s|%(webpage_url)s',
        '--no-warnings',
        '--ignore-errors',
        url,
    ])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PLAYLIST_RESOLVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"解析播放列表失败: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or '').strip()
        detail = stderr.splitlines()[-1] if stderr else ''
        return None, f"解析播放列表失败 (yt-dlp 退出码 {result.returncode}): {detail}"

    urls = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split('|', 1)
        if len(parts) != 2:
            continue
        entry_url = parts[1].strip()
        if entry_url and entry_url.lower() != 'na':
            urls.append(entry_url)
    return urls, None


def expand_task_urls(url):
    """将提交的 URL 展开为待下载的 URL 列表。

    疑似播放列表的 URL 会被解析成逐集 URL，并只返回配置数量的前几个
    条目；普通视频 URL 原样返回。解析失败时返回错误，由调用方提示用户。

    Returns:
        tuple: (urls, error)。成功时 urls 为 URL 列表、error 为 None。
    """
    if not looks_like_playlist(url):
        return [url], None

    max_items = get_playlist_max_items(config)

    urls, error = resolve_playlist_urls(
        url,
        _pick_ytdlp_conf('video'),
        max_items=max_items,
    )
    if error:
        return None, error

    if not urls:
        return None, "播放列表解析结果为空，请检查链接是否为公开播放列表"
    return urls[:max_items], None


def create_tasks(urls, types):
    """创建下载任务并返回任务ID列表（委托 task_queue 共享实现）。

    Args:
        urls (list): 要下载的 URL 列表（播放列表已展开为逐集 URL）。
        types (list): 下载类型列表，可以是 ['video'] 或 ['audio'] 或两者都有

    Returns:
        list: 创建的任务ID列表
    """
    return task_queue.create_tasks(urls, types, URLS_DIR, config["TIMEZONE"])


def classify_download_stage(extension, vcodec, acodec):
    """根据 yt-dlp 当前产物信息识别正在下载的媒体阶段。"""
    extension = (extension or '').strip().lower()
    vcodec = (vcodec or '').strip().lower()
    acodec = (acodec or '').strip().lower()
    empty_codecs = {'', 'na', 'none', 'null', 'unknown'}
    has_video = vcodec not in empty_codecs
    has_audio = acodec not in empty_codecs

    if extension in SUBTITLE_EXTENSIONS:
        return 'download_subtitles'
    if has_video and not has_audio:
        return 'download_video'
    if has_audio and not has_video:
        return 'download_audio'
    if has_video and has_audio:
        return 'download_media'
    if extension in AUDIO_EXTENSIONS:
        return 'download_audio'
    if extension in VIDEO_EXTENSIONS:
        return 'download_video'
    return 'downloading'


def detect_processing_stage(line):
    """从 yt-dlp 后处理日志识别合并、嵌入字幕等阶段。"""
    if '[EmbedSubtitle]' in line:
        return 'embed_subtitles'
    if '[Merger]' in line or 'Merging formats into' in line:
        return 'merge_media'
    if '[ExtractAudio]' in line:
        return 'extract_audio'
    if '[Metadata]' in line:
        return 'write_metadata'
    if any(marker in line for marker in (
        '[VideoConvertor]',
        '[VideoRemuxer]',
        '[Fixup',
        '[ThumbnailsConvertor]',
        '[MoveFiles]',
    )):
        return 'postprocessing'
    return None


def normalize_progress_value(value):
    """将 yt-dlp 的不可用占位值统一转换为空字符串。"""
    normalized = (value or '').strip()
    if normalized.upper() in {'NA', 'N/A', 'NONE', 'NULL', 'UNKNOWN'}:
        return ''
    return normalized


def valid_nonnegative_number(value):
    """返回有效的非负数；布尔值和非法数据返回 None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or not (number < float('inf')):
        return None
    return number


def select_primary_result_file(filenames, task_type):
    """从本地可用产物中选择最大的主媒体文件。"""
    extensions = AUDIO_EXTENSIONS if task_type == 'audio' else VIDEO_EXTENSIONS
    candidates = []
    for filename in filenames:
        extension = os.path.splitext(filename)[1].lower().lstrip('.')
        filepath = safe_join(FILES_DIR, filename)
        if extension not in extensions or not filepath or not os.path.isfile(filepath):
            continue
        candidates.append((os.path.getsize(filepath), filename))
    if not candidates:
        return None
    return max(candidates)[1]


def parse_task_progress(log_path):
    """从任务日志末尾提取 yt-dlp 最近一次下载进度。"""
    if not os.path.isfile(log_path):
        return {}

    try:
        with open(log_path, 'rb') as log_file:
            log_file.seek(0, os.SEEK_END)
            file_size = log_file.tell()
            log_file.seek(max(0, file_size - 128 * 1024))
            content = log_file.read().decode('utf-8', errors='replace')
    except OSError as exc:
        app.logger.warning("读取任务进度日志失败: %s (%s)", log_path, exc)
        return {}

    lines = [
        ANSI_ESCAPE_PATTERN.sub('', line).strip()
        for line in content.splitlines()
    ]
    processing_stage = None
    for line in reversed(lines):
        if processing_stage is None:
            processing_stage = detect_processing_stage(line)

        if line.startswith(PROGRESS_MARKER):
            fields = line[len(PROGRESS_MARKER):].split('|')
            if len(fields) < 6:
                continue
            status, percent_text, downloaded, total, speed, eta = fields[:6]
            percent_match = re.search(r'\d+(?:\.\d+)?', percent_text)
            progress = {
                "phase": status.strip(),
                "downloaded": normalize_progress_value(downloaded),
                "total": normalize_progress_value(total),
                "speed": normalize_progress_value(speed),
                "eta": normalize_progress_value(eta),
                "stage": processing_stage or 'downloading',
            }
            if len(fields) >= 10 and processing_stage is None:
                extension, _format_id, vcodec, acodec = fields[6:10]
                progress["stage"] = classify_download_stage(
                    extension,
                    vcodec,
                    acodec,
                )
            if percent_match:
                progress["percent"] = min(100.0, float(percent_match.group()))
            return progress

        match = DEFAULT_PROGRESS_PATTERN.search(line)
        if match:
            progress = {
                "phase": "downloading",
                "percent": min(100.0, float(match.group('percent'))),
                "stage": processing_stage or "downloading",
            }
            for key in ('total', 'speed', 'eta'):
                value = match.group(key)
                if value:
                    progress[key] = value.strip()
            return progress
    if processing_stage:
        return {
            "phase": "processing",
            "percent": 100.0,
            "stage": processing_stage,
        }
    return {}


def recover_task_files_from_logs(task):
    """从 downloader 移动日志恢复旧任务的最终产物文件名。"""
    log_pattern = os.path.join(config["LOG_DIR"], 'downloader.log*')
    log_paths = sorted(
        glob.glob(log_pattern),
        key=lambda path: os.path.getmtime(path),
        reverse=True,
    )
    recovered_files = []
    files_root = os.path.realpath(FILES_DIR)

    for log_path in log_paths:
        try:
            with open(log_path, 'rb') as log_file:
                log_file.seek(0, os.SEEK_END)
                file_size = log_file.tell()
                log_file.seek(max(0, file_size - 2 * 1024 * 1024))
                content = log_file.read().decode('utf-8', errors='replace')
        except OSError:
            continue

        for line in reversed(content.splitlines()):
            if task not in line or '已移动文件:' not in line or ' -> ' not in line:
                continue
            destination = line.rsplit(' -> ', 1)[1].strip()
            destination_realpath = os.path.realpath(destination)
            try:
                inside_files_dir = (
                    os.path.commonpath([files_root, destination_realpath])
                    == files_root
                )
            except ValueError:
                inside_files_dir = False
            if not inside_files_dir or not os.path.isfile(destination_realpath):
                continue
            filename = os.path.basename(destination_realpath)
            if filename not in recovered_files:
                recovered_files.append(filename)
    return recovered_files


def get_task_info(task):
    """读取单个任务的生命周期状态与最近下载进度。"""
    if not isinstance(task, str) or not TASK_ID_PATTERN.fullmatch(task):
        return {"task": task, "exists": False, "msg": "Invalid task id"}

    task_path = None
    state = None
    for extension, candidate_state in TASK_STATE_EXTENSIONS:
        candidate_path = os.path.join(URLS_DIR, f"{task}{extension}")
        if os.path.isfile(candidate_path):
            task_path = candidate_path
            state = candidate_state
            break

    if not task_path:
        return {
            "task": task,
            "exists": False,
            "state": "missing",
            "msg": "Task file not found",
        }

    try:
        with open(task_path, 'r') as task_file:
            url = task_file.read().strip()
    except OSError as exc:
        return {
            "task": task,
            "exists": False,
            "state": "missing",
            "msg": f"Read error: {exc}",
        }

    timestamp = task[1:15]
    try:
        task_time = time.strptime(timestamp, '%Y%m%d%H%M%S')
        time_fmt = time.strftime('%Y-%m-%d %H:%M:%S', task_time)
    except ValueError:
        time_fmt = timestamp

    task_type = 'video' if task[0] == 'v' else 'audio'
    if state == 'completed':
        progress = {
            "percent": 100.0,
            "phase": "finished",
            "stage": "completed",
        }
    else:
        progress = parse_task_progress(
            os.path.join(config["LOG_DIR"], f"{task}.log")
        )
    if state == 'queued':
        progress = {
            "percent": 0.0,
            "phase": "queued",
            "stage": "queued",
        }
    elif state == 'failed':
        progress["stage"] = "failed"
    elif state == 'paused':
        # 保留暂停前解析到的进度，便于用户看到已经下到哪里
        progress = dict(progress or {"percent": 0.0})
        progress["phase"] = "paused"
        progress["stage"] = "paused"
    elif not progress:
        progress = {
            "percent": 0.0,
            "phase": "starting" if state == 'downloading' else state,
            "stage": "starting" if state == 'downloading' else state,
        }

    task_info = {
        "task": task,
        "exists": True,
        "type": task_type,
        "timestamp": task[1:],
        "time": time_fmt,
        "url": url,
        "state": state,
        "progress": progress,
    }

    # 预览信息必须保持轻量，不能为了封面或标题再次调用 yt-dlp。
    # YouTube 单视频的缩略图地址可以从 URL 直接推导，标题等元数据由前端
    # 另行异步获取；这样任务状态不会被慢速元数据请求阻塞。
    youtube_video_id = extract_youtube_video_id(url)
    task_info["source_url"] = url
    task_info["thumbnail"] = (
        f"https://i.ytimg.com/vi/{youtube_video_id}/hqdefault.jpg"
        if youtube_video_id
        else None
    )
    task_info["metadata_state"] = "preview" if youtube_video_id else "pending"

    if state == 'completed':
        result_path = os.path.join(URLS_DIR, f"{task}.result.json")
        try:
            with open(result_path, 'r') as result_file:
                result_data = json.load(result_file)
        except (OSError, json.JSONDecodeError):
            result_data = {}

        result_files = result_data.get("files", [])
        if not result_files:
            result_files = recover_task_files_from_logs(task)

        available_files = []
        for filename in result_files:
            if not isinstance(filename, str):
                continue
            filepath = safe_join(FILES_DIR, filename)
            if filepath and os.path.isfile(filepath):
                available_files.append(filename)

        task_info["files"] = available_files
        summary = result_data.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}

        final_size_bytes = valid_nonnegative_number(
            summary.get("final_size_bytes")
        )
        elapsed_seconds = valid_nonnegative_number(
            summary.get("elapsed_seconds")
        )
        average_speed = valid_nonnegative_number(
            summary.get("average_speed_bytes_per_second")
        )

        primary_filename = summary.get("primary_file")
        if not isinstance(primary_filename, str):
            primary_filename = None
        if final_size_bytes is None:
            primary_filename = select_primary_result_file(
                available_files,
                task_type,
            )
            if primary_filename:
                primary_path = safe_join(FILES_DIR, primary_filename)
                final_size_bytes = os.path.getsize(primary_path)

        if final_size_bytes is not None:
            progress["final_size_bytes"] = final_size_bytes
        if elapsed_seconds is not None:
            progress["elapsed_seconds"] = elapsed_seconds
        if average_speed is not None:
            progress["average_speed_bytes_per_second"] = average_speed

        playable_extensions = AUDIO_EXTENSIONS if task_type == 'audio' else {'mp4'}
        player_filename = next(
            (
                filename for filename in available_files
                if os.path.splitext(filename)[1].lower().lstrip('.')
                in playable_extensions
            ),
            None,
        )
        if player_filename:
            task_info["player_url"] = url_for(
                'audio_player' if task_type == 'audio' else 'player',
                file=player_filename,
            )
            task_info["download_url"] = url_for(
                'download_file',
                filename=player_filename,
            )

    return task_info


SUBTITLE_LANGUAGE_ALIASES = {
    "chi": "zh",
    "zho": "zh",
    "eng": "en",
    "jpn": "ja",
    "kor": "ko",
}

SUBTITLE_LANGUAGE_LABELS = {
    "zh": "中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
}

CHINESE_SUBTITLE_VARIANTS = (
    ("zh-Hans", "简体中文"),
    ("zh-Hant", "繁体中文"),
)


def normalize_subtitle_language(language):
    """将 ffprobe 返回的语言代码转换为浏览器常用的 BCP 47 代码。"""
    normalized = (language or "und").strip().lower()
    return SUBTITLE_LANGUAGE_ALIASES.get(normalized, normalized)


def _canonical_subtitle_language(language):
    normalized = normalize_subtitle_language(language).replace('_', '-')
    return {
        'zh-hans': 'zh-Hans',
        'zh-hant': 'zh-Hant',
    }.get(normalized, normalized)


def _subtitle_language_label(language):
    if not language or language == 'und':
        return '字幕'
    if language == 'zh-Hans':
        return '简体中文'
    if language == 'zh-Hant':
        return '繁体中文'
    return SUBTITLE_LANGUAGE_LABELS.get(language, language)


def get_sidecar_subtitles(filename):
    """查找与视频严格同 stem 的外挂字幕，并按语言和格式去重排序。"""
    video_stem, video_extension = os.path.splitext(filename)
    if video_extension.lower() != '.mp4':
        return []
    try:
        directory_entries = os.listdir(FILES_DIR)
    except OSError as exc:
        app.logger.warning("读取外挂字幕目录失败: %s (%s)", FILES_DIR, exc)
        return []

    candidates = []
    for candidate in directory_entries:
        candidate_stem, extension = os.path.splitext(candidate)
        extension = extension.lower().lstrip('.')
        if extension not in VIDEO_SIDECAR_SUBTITLE_FORMATS:
            continue
        if candidate_stem == video_stem:
            language_suffix = ''
        elif candidate_stem.startswith(f'{video_stem}.'):
            language_suffix = candidate_stem[len(video_stem) + 1:]
            if not language_suffix:
                continue
        else:
            continue
        candidate_path = safe_join(FILES_DIR, candidate)
        if not candidate_path or not os.path.isfile(candidate_path):
            continue

        language = _canonical_subtitle_language(language_suffix)
        normalized_language = language.lower()
        try:
            language_rank = VIDEO_SIDECAR_LANGUAGE_PREFERENCES.index(
                normalized_language
            ) + 1
        except ValueError:
            language_rank = len(VIDEO_SIDECAR_LANGUAGE_PREFERENCES) + 1
        if not language_suffix:
            language_rank = 0
        candidates.append((
            language_rank,
            normalized_language,
            VIDEO_SIDECAR_SUBTITLE_FORMATS.index(extension),
            candidate.lower(),
            candidate,
            language,
        ))

    tracks = []
    seen_languages = set()
    for _, normalized_language, _, _, subtitle_filename, language in sorted(candidates):
        if normalized_language in seen_languages:
            continue
        seen_languages.add(normalized_language)
        tracks.append({
            'id': f'sidecar:{subtitle_filename}',
            'source_kind': 'sidecar',
            'subtitle_filename': subtitle_filename,
            'stream_index': None,
            'language': language or 'und',
            'label': _subtitle_language_label(language),
        })
    return tracks


@lru_cache(maxsize=256)
def _probe_embedded_subtitles(filepath, file_mtime_ns, file_size):
    """读取 MP4 的内嵌字幕流；文件属性参数用于自动失效缓存。"""
    del file_mtime_ns, file_size
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "s",
                "-show_entries", "stream=index:stream_tags=language,title",
                "-of", "json", filepath,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        app.logger.warning("读取视频字幕流失败，已跳过字幕: %s (%s)", filepath, exc)
        return ()

    subtitles = []
    for stream in streams:
        if not isinstance(stream.get("index"), int):
            continue
        tags = stream.get("tags") or {}
        language = normalize_subtitle_language(tags.get("language"))
        base_label = tags.get("title") or SUBTITLE_LANGUAGE_LABELS.get(language, language if language != "und" else "字幕")
        subtitles.append({
            "stream_index": stream["index"],
            "language": language,
            "base_label": base_label,
        })

    # MP4 的 mov_text 通常会把 zh-Hans 和 zh-Hant 都保存成 zho。
    # yt-dlp.conf 按简体、繁体的顺序请求字幕，因此对前两个无标题的中文轨道恢复语言变体。
    generic_chinese_subtitles = [
        subtitle for subtitle in subtitles
        if subtitle["language"] == "zh" and subtitle["base_label"] == "中文"
    ]
    if len(generic_chinese_subtitles) >= 2:
        for subtitle, (language, label) in zip(
            generic_chinese_subtitles,
            CHINESE_SUBTITLE_VARIANTS,
        ):
            subtitle["language"] = language
            subtitle["base_label"] = label

    totals = {}
    for subtitle in subtitles:
        totals[subtitle["base_label"]] = totals.get(subtitle["base_label"], 0) + 1

    seen = {}
    for subtitle in subtitles:
        base_label = subtitle.pop("base_label")
        seen[base_label] = seen.get(base_label, 0) + 1
        subtitle["label"] = (
            f"{base_label} {seen[base_label]}"
            if totals[base_label] > 1
            else base_label
        )
    return tuple(subtitles)


def get_embedded_subtitles(filename):
    filepath = safe_join(FILES_DIR, filename)
    if not filepath or not os.path.isfile(filepath):
        return []
    stat = os.stat(filepath)
    return [dict(subtitle) for subtitle in _probe_embedded_subtitles(
        filepath,
        stat.st_mtime_ns,
        stat.st_size,
    )]


def get_video_subtitle_tracks(filename):
    """外挂字幕优先；没有外挂字幕时返回规范化的内嵌字幕轨道。"""
    sidecar_tracks = get_sidecar_subtitles(filename)
    if sidecar_tracks:
        return sidecar_tracks
    tracks = get_embedded_subtitles(filename)
    for track in tracks:
        track.setdefault('id', f"embedded:{track.get('stream_index')}")
        track.setdefault('source_kind', 'embedded')
        track.setdefault('subtitle_filename', '')
    return tracks


def extract_subtitle_text(filepath, stream_index):
    """把指定内嵌字幕流转换为适合发送给 AI 的纯文本。"""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", filepath,
                "-map", f"0:{stream_index}", "-f", "webvtt", "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg，无法读取视频字幕") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError("读取视频字幕失败") from exc

    return subtitle_webvtt_to_text(result.stdout)


def subtitle_webvtt_to_text(subtitle):
    """清理 WebVTT 字节或文本，返回适合发送给 AI 的纯文本。"""
    if isinstance(subtitle, bytes):
        subtitle = subtitle.decode("utf-8", errors="replace")
    text_lines = []
    previous_line = None
    skip_note = False
    for raw_line in subtitle.splitlines():
        line = raw_line.strip()
        if not line:
            skip_note = False
            continue
        if line == "WEBVTT" or line.startswith(("STYLE", "REGION")):
            continue
        if line.startswith("NOTE"):
            skip_note = True
            continue
        if skip_note or SUBTITLE_TIMESTAMP_PATTERN.match(line) or line.isdigit():
            continue
        line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if line and line != previous_line:
            text_lines.append(line)
            previous_line = line

    subtitle_text = "\n".join(text_lines)
    if len(subtitle_text) > AI_SUMMARY_MAX_SUBTITLE_CHARS:
        subtitle_text = subtitle_text[:AI_SUMMARY_MAX_SUBTITLE_CHARS]
        subtitle_text += "\n（字幕内容过长，已截断）"
    return subtitle_text


def convert_sidecar_subtitle_to_webvtt(filepath):
    """使用 ffmpeg 将外挂字幕统一转换为 WebVTT。"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", filepath, "-f", "webvtt", "pipe:1"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg，无法读取外挂字幕") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError("读取外挂字幕失败") from exc
    return result.stdout


def extract_sidecar_subtitle_text(filepath):
    return subtitle_webvtt_to_text(convert_sidecar_subtitle_to_webvtt(filepath))


def request_ai_summary(filename, subtitle_label, subtitle_text, on_delta=None):
    """流式调用 chat/completions 兼容接口并返回完整总结文本。"""
    api_base_url = str(config.get("AI_API_BASE_URL") or "").strip()
    api_model = str(config.get("AI_API_MODEL") or "").strip()
    api_token = str(config.get("AI_API_TOKEN") or "").strip()
    if not api_base_url or not api_model or not api_token:
        raise RuntimeError("AI 总结尚未完成配置")

    response = requests.post(
        api_base_url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        json={
            "model": api_model,
            "stream": True,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一名严谨的视频内容总结助手。仅根据提供的字幕总结，"
                        "不要补充字幕中没有的信息。使用简体中文输出，先给出简短概述，"
                        "再列出关键要点；字幕信息不足或含糊时明确说明。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"视频文件名：{filename}\n"
                        f"字幕：{subtitle_label}\n\n"
                        f"字幕内容：\n{subtitle_text}"
                    ),
                },
            ],
        },
        timeout=(15, 120),
        stream=True,
    )
    response.raise_for_status()
    content_type = response.headers.get('Content-Type', '')
    chunks = []
    if 'text/event-stream' not in content_type.lower():
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI 接口返回了无法识别的数据") from exc
        if isinstance(content, str):
            chunks.append(content)
            if on_delta:
                on_delta(content)
    else:
        for raw_line in response.iter_lines(decode_unicode=False):
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode('utf-8', errors='replace')
            line = (raw_line or '').strip()
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            try:
                event = json.loads(data)
                delta = event['choices'][0].get('delta', {}).get('content')
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if isinstance(delta, str) and delta:
                chunks.append(delta)
                if on_delta:
                    on_delta(''.join(chunks))
    content = ai_summary_store.repair_utf8_mojibake(''.join(chunks)).strip()
    if not content:
        raise RuntimeError("AI 接口未返回总结内容")
    return content


def ai_summary_is_configured():
    return all(
        isinstance(config.get(key), str) and config.get(key).strip()
        for key in ("AI_API_BASE_URL", "AI_API_MODEL", "AI_API_TOKEN")
    )


def ai_summary_access_is_configured():
    return bool(str(config.get("AI_SUMMARY_ACCESS_TOKEN") or "").strip())


def ai_summary_api_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store, private'
    if status == 202:
        response.headers['Retry-After'] = '2'
    return response


def ai_summary_job_payload(job, legacy=False):
    if job['status'] == 'completed' and job.get('summary'):
        summary = job['summary']
        if legacy:
            return {
                'success': True,
                'status': 'completed',
                'job_id': job['id'],
                'summary': summary['markdown'],
                'subtitle': summary['subtitle_language'] or summary['subtitle_kind'],
                'cached': bool(job.get('cache_hit')),
            }, 200
        return {
            'success': True,
            'status': 'completed',
            'job_id': job['id'],
            'cached': bool(job.get('cache_hit')),
            'summary': summary,
        }, 200
    if job['status'] == 'failed':
        error = {
            'code': job.get('error_code') or 'summary_failed',
            'message': job.get('error_message') or '生成总结失败',
            'retryable': bool(job.get('error_retryable')),
        }
        payload = {
            'success': False,
            'status': 'failed',
            'job_id': job['id'],
            'error': error,
        }
        if legacy:
            payload['message'] = error['message']
        return payload, 422
    return {
        'success': True,
        'status': job['status'],
        'job_id': job['id'],
        'cached': False,
        'partial_markdown': job.get('partial_markdown') or '',
        'stream_revision': job.get('stream_revision') or 0,
    }, 202


def ai_summary_job_stream(job_id, legacy=False):
    """将 SQLite 中的任务增量以 NDJSON 持续发送给浏览器。"""
    initial = ai_summary_store.get_job(config['AI_SUMMARY_DB_PATH'], job_id)
    if not initial:
        return ai_summary_api_response({'success': False, 'message': 'AI 总结任务不存在'}, 404)

    @stream_with_context
    def generate():
        last_marker = None
        last_keepalive = time.monotonic()
        while True:
            job = ai_summary_store.get_job(config['AI_SUMMARY_DB_PATH'], job_id)
            if not job:
                payload = {'success': False, 'status': 'missing', 'message': 'AI 总结任务不存在'}
                yield json.dumps(payload, ensure_ascii=False) + '\n'
                return
            marker = (job['status'], job.get('stream_revision') or 0, job.get('updated_at'))
            if marker != last_marker:
                payload, _ = ai_summary_job_payload(job, legacy=legacy)
                yield json.dumps(payload, ensure_ascii=False) + '\n'
                last_marker = marker
                last_keepalive = time.monotonic()
            if job['status'] in {'completed', 'failed'}:
                return
            if time.monotonic() - last_keepalive >= 15:
                yield json.dumps({'type': 'keepalive'}) + '\n'
                last_keepalive = time.monotonic()
            time.sleep(0.2)

    response = Response(generate(), content_type='application/x-ndjson; charset=utf-8')
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


def require_ai_summary_access():
    configured_token = str(config.get("AI_SUMMARY_ACCESS_TOKEN") or "").strip()
    if not configured_token:
        return ai_summary_api_response({
            'success': False,
            'message': 'AI 总结扩展接口尚未配置访问令牌',
        }, 503)
    request_token = request.headers.get('X-Yter-AI-Token', '')
    if not hmac.compare_digest(request_token, configured_token):
        return ai_summary_api_response({
            'success': False,
            'message': 'AI 总结访问令牌无效',
        }, 401)
    return None


def extract_youtube_video_id(source_url):
    """从常见 YouTube 地址中提取 11 位视频 ID。"""
    if not isinstance(source_url, str) or not source_url.strip():
        return None

    try:
        parsed = urlparse(source_url.strip())
    except ValueError:
        return None

    hostname = (parsed.hostname or '').lower()
    if hostname.startswith('www.'):
        hostname = hostname[4:]
    if hostname.startswith('m.'):
        hostname = hostname[2:]

    candidate = None
    if hostname == 'youtu.be':
        candidate = parsed.path.strip('/').split('/', 1)[0]
    elif hostname in {'youtube.com', 'youtube-nocookie.com'}:
        path_parts = [part for part in parsed.path.split('/') if part]
        if parsed.path.rstrip('/') == '/watch':
            candidate = parse_qs(parsed.query).get('v', [None])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {
            'embed', 'live', 'shorts',
        }:
            candidate = path_parts[1]

    if candidate and YOUTUBE_VIDEO_ID_PATTERN.fullmatch(candidate):
        return candidate
    return None


def extract_youtube_video_id_from_text(text):
    """从可能包含多个链接的 metadata 文本中找到首个 YouTube 视频 ID。"""
    if not isinstance(text, str):
        return None
    urls = re.findall(
        r'https?://[^\s\u4e00-\u9fa5\u3000-\u303f\uff00-\uffef]+',
        text,
    )
    for source_url in urls:
        video_id = extract_youtube_video_id(
            source_url.rstrip('.,;:)]\'"。，；：）、）'),
        )
        if video_id:
            return video_id
    return None


def extract_media_source_url(tags):
    """从媒体 metadata 的 purl/comment 标签中提取安全的来源页面 URL。"""
    if not isinstance(tags, dict):
        return ''

    normalized_tags = {
        str(key).lower(): value
        for key, value in tags.items()
        if isinstance(value, str)
    }
    for tag_name in ('purl', 'comment'):
        text = normalized_tags.get(tag_name, '')
        urls = re.findall(
            r'https?://[^\s\u4e00-\u9fa5\u3000-\u303f\uff00-\uffef]+',
            text,
            flags=re.IGNORECASE,
        )
        for raw_url in urls:
            source_url = raw_url.rstrip('.,;:)]\'"。，；：）、）')
            try:
                parsed = urlparse(source_url)
            except ValueError:
                continue
            if parsed.scheme.lower() in {'http', 'https'} and parsed.hostname:
                return source_url
    return ''


def build_audio_cover_candidates(video_id, fallback_url):
    """生成按清晰度和可靠性排序的音频封面候选地址。"""
    candidates = []
    if video_id and YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
        base_url = f"https://i.ytimg.com/vi/{video_id}"
        candidates.extend([
            f"{base_url}/maxresdefault.jpg",
            f"{base_url}/hqdefault.jpg",
        ])
    if isinstance(fallback_url, str) and fallback_url.strip():
        candidates.append(fallback_url.strip())
    return list(dict.fromkeys(candidates))


def format_media_metadata_date(value):
    """将 yt-dlp 常见的 YYYYMMDD 日期转换为更易读的格式。"""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if re.fullmatch(r'\d{8}', value):
        return f'{value[:4]}-{value[4:6]}-{value[6:]}'
    return value


def build_media_display_metadata(tags):
    """从 ffprobe 标签中筛选适合直接展示给用户的媒体信息。"""
    if not isinstance(tags, dict):
        tags = {}
    normalized_tags = {
        str(key).lower(): value.strip()
        for key, value in tags.items()
        if isinstance(value, str) and value.strip()
    }
    description = (
        normalized_tags.get('description')
        or normalized_tags.get('synopsis')
        or ''
    )
    return {
        'title': normalized_tags.get('title', ''),
        'artist': normalized_tags.get('artist', ''),
        'album': normalized_tags.get('album', ''),
        'date': format_media_metadata_date(normalized_tags.get('date', '')),
        'genre': normalized_tags.get('genre', ''),
        'description': description,
        'source_url': extract_media_source_url(normalized_tags),
    }


@lru_cache(maxsize=256)
def _probe_media_metadata(filepath, file_mtime_ns, file_size):
    """一次读取播放器需要的媒体标签；文件属性用于缓存自动失效。"""
    del file_mtime_ns, file_size
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries',
                'format_tags=title,artist,album,date,genre,description,synopsis,purl,comment',
                '-of', 'json', filepath,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        payload = json.loads(result.stdout)
        raw_tags = payload.get('format', {}).get('tags', {})
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        app.logger.warning("读取媒体 metadata 失败，使用默认信息: %s (%s)", filepath, exc)
        raw_tags = {}
    return build_media_display_metadata(raw_tags)


@lru_cache(maxsize=256)
def _probe_media_dimensions(filepath, file_mtime_ns, file_size):
    """读取媒体时长与视频高度，供媒体库列表展示；文件属性用于缓存失效。"""
    del file_mtime_ns, file_size
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'format=duration:stream=height',
                '-of', 'json', filepath,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        payload = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        app.logger.warning("读取媒体时长失败，已忽略: %s (%s)", filepath, exc)
        return None, None

    duration = valid_nonnegative_number(payload.get('format', {}).get('duration'))
    streams = payload.get('streams') or []
    height = None
    if streams:
        height = valid_nonnegative_number(streams[0].get('height'))
    return duration, int(height) if height else None


def get_media_dimensions(filename):
    """安全读取 FILES_DIR 中媒体文件的时长与高度。"""
    filepath = safe_join(FILES_DIR, filename)
    if not filepath or not os.path.isfile(filepath):
        return None, None
    try:
        stat = os.stat(filepath)
    except OSError:
        return None, None
    return _probe_media_dimensions(filepath, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=256)
def _probe_audio_metadata(filepath, file_mtime_ns, file_size):
    """读取音频 metadata；文件属性参数用于在文件变化时自动失效缓存。"""
    metadata = _probe_media_metadata(filepath, file_mtime_ns, file_size)
    source_url = metadata['source_url']
    video_id = extract_youtube_video_id(source_url)
    return metadata['title'], metadata['artist'], video_id, source_url


@lru_cache(maxsize=256)
def _probe_media_source_url(filepath, file_mtime_ns, file_size):
    """读取视频等媒体文件的来源 URL；文件属性用于缓存自动失效。"""
    return _probe_media_metadata(
        filepath,
        file_mtime_ns,
        file_size,
    )['source_url']


def get_media_source_url(filename):
    """安全读取 FILES_DIR 中媒体文件保存的来源页面 URL。"""
    filepath = safe_join(FILES_DIR, filename)
    if not filepath or not os.path.isfile(filepath):
        return ''
    try:
        stat = os.stat(filepath)
        return _probe_media_source_url(
            filepath,
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError as exc:
        app.logger.warning("读取媒体文件属性失败，已忽略来源链接: %s (%s)", filepath, exc)
        return ''


def get_audio_metadata(filename, fallback_cover_url):
    """返回音频页面所需的标题、作者、MIME 和封面候选地址。"""
    filepath = safe_join(FILES_DIR, filename)
    extension = os.path.splitext(filename)[1].lower().lstrip('.')
    fallback_title = os.path.splitext(filename)[0]
    if (
        not filepath
        or extension not in AUDIO_EXTENSIONS
        or not os.path.isfile(filepath)
    ):
        return {
            'title': fallback_title,
            'artist': '',
            'album': '',
            'date': '',
            'genre': '',
            'description': '',
            'source_url': '',
            'mime_type': AUDIO_MIME_TYPES.get(extension, 'audio/mpeg'),
            'cover_candidates': build_audio_cover_candidates(
                None,
                fallback_cover_url,
            ),
        }

    try:
        stat = os.stat(filepath)
        title, artist, video_id, source_url = _probe_audio_metadata(
            filepath,
            stat.st_mtime_ns,
            stat.st_size,
        )
        display_metadata = _probe_media_metadata(
            filepath,
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError as exc:
        app.logger.warning("读取音频文件属性失败，使用默认信息: %s (%s)", filepath, exc)
        title, artist, video_id, source_url = '', '', None, ''
        display_metadata = build_media_display_metadata({})

    return {
        'title': title or fallback_title,
        'artist': artist,
        'album': display_metadata['album'],
        'date': display_metadata['date'],
        'genre': display_metadata['genre'],
        'description': display_metadata['description'],
        'source_url': source_url,
        'mime_type': AUDIO_MIME_TYPES.get(extension, 'audio/mpeg'),
        'cover_candidates': build_audio_cover_candidates(
            video_id,
            fallback_cover_url,
        ),
    }


def get_video_metadata(filename):
    """返回视频播放器需要的标题、展示信息、来源链接和封面候选。"""
    filepath = safe_join(FILES_DIR, filename)
    fallback_title = filename
    metadata = build_media_display_metadata({})
    if filepath and os.path.isfile(filepath):
        try:
            stat = os.stat(filepath)
            metadata = dict(_probe_media_metadata(
                filepath,
                stat.st_mtime_ns,
                stat.st_size,
            ))
        except OSError as exc:
            app.logger.warning("读取视频文件属性失败，使用默认信息: %s (%s)", filepath, exc)

    # 保留既有入口，方便调用方覆写或单独读取来源链接。
    source_url = get_media_source_url(filename)
    metadata['source_url'] = source_url
    metadata['title'] = metadata['title'] or fallback_title
    metadata['cover_candidates'] = build_audio_cover_candidates(
        extract_youtube_video_id(source_url),
        '',
    )
    return metadata


def find_audio_lyrics(filename, preferred_languages=None):
    """查找与音频同名的旁挂歌词，并返回浏览器可读取的信息。"""
    audio_stem = os.path.splitext(filename)[0]
    preferred_languages = preferred_languages or []
    normalized_languages = []
    for language in preferred_languages:
        if not isinstance(language, str):
            continue
        normalized = language.strip().lower().replace('_', '-')
        if not normalized:
            continue
        language_variants = [normalized]
        if normalized in {'zh-cn', 'zh-sg'}:
            language_variants.append('zh-hans')
        elif normalized in {'zh-hk', 'zh-mo', 'zh-tw'}:
            language_variants.append('zh-hant')
        language_variants.append(normalized.split('-', 1)[0])
        for variant in language_variants:
            if variant not in normalized_languages:
                normalized_languages.append(variant)

    language_order = normalized_languages + [
        'zh-hans', 'zh-cn', 'zh', 'zh-hant', 'zh-tw', 'en',
    ]
    language_order = list(dict.fromkeys(language_order))
    extension_order = {'lrc': 0, 'vtt': 1, 'srt': 2}
    candidates = []

    try:
        directory_entries = os.listdir(FILES_DIR)
    except OSError as exc:
        app.logger.warning("读取歌词目录失败: %s (%s)", FILES_DIR, exc)
        return None

    for candidate in directory_entries:
        candidate_stem, extension = os.path.splitext(candidate)
        extension = extension.lower().lstrip('.')
        if extension not in LYRICS_EXTENSIONS:
            continue
        if candidate_stem == audio_stem:
            language = ''
        elif candidate_stem.startswith(f'{audio_stem}.'):
            language = candidate_stem[len(audio_stem) + 1:]
        else:
            continue

        candidate_path = safe_join(FILES_DIR, candidate)
        if not candidate_path or not os.path.isfile(candidate_path):
            continue

        normalized_language = language.lower().replace('_', '-')
        try:
            language_rank = language_order.index(normalized_language)
        except ValueError:
            language_rank = len(language_order)
        candidates.append((
            0 if not normalized_language else 1,
            language_rank,
            extension_order[extension],
            candidate.lower(),
            candidate,
            language,
        ))

    if not candidates:
        return None

    _, _, _, _, lyrics_filename, language = min(candidates)
    extension = os.path.splitext(lyrics_filename)[1].lower().lstrip('.')
    return {
        'filename': lyrics_filename,
        'url': url_for('serve_file', filename=lyrics_filename),
        'format': extension,
        'language': language,
    }


def get_audio_sidecar_subtitles(filename):
    """返回音频所有可用于 AI 总结的同名旁挂歌词。"""
    audio_stem, extension = os.path.splitext(filename)
    if extension.lower().lstrip('.') not in AUDIO_EXTENSIONS:
        return []
    candidates = []
    try:
        directory_entries = os.listdir(FILES_DIR)
    except OSError:
        return []
    format_order = {'lrc': 0, 'vtt': 1, 'srt': 2}
    language_order = ('zh-hans', 'zh-cn', 'zh', 'zh-hant', 'zh-tw', 'en')
    for candidate in directory_entries:
        candidate_stem, candidate_extension = os.path.splitext(candidate)
        candidate_extension = candidate_extension.lower().lstrip('.')
        if candidate_extension not in LYRICS_EXTENSIONS:
            continue
        if candidate_stem == audio_stem:
            language_suffix = ''
        elif candidate_stem.startswith(f'{audio_stem}.'):
            language_suffix = candidate_stem[len(audio_stem) + 1:]
        else:
            continue
        candidate_path = safe_join(FILES_DIR, candidate)
        if not candidate_path or not os.path.isfile(candidate_path):
            continue
        language = _canonical_subtitle_language(language_suffix)
        normalized = language.lower()
        try:
            language_rank = language_order.index(normalized) + 1
        except ValueError:
            language_rank = len(language_order) + 1
        if not language_suffix:
            language_rank = 0
        candidates.append((language_rank, normalized, format_order[candidate_extension], candidate.lower(), candidate, language))
    tracks = []
    seen = set()
    for _, normalized, _, _, subtitle_filename, language in sorted(candidates):
        if normalized in seen:
            continue
        seen.add(normalized)
        tracks.append({
            'id': f'sidecar:{subtitle_filename}',
            'source_kind': 'sidecar',
            'subtitle_filename': subtitle_filename,
            'stream_index': None,
            'language': language or 'und',
            'label': _subtitle_language_label(language),
        })
    return tracks


def get_local_summary_tracks(filename):
    """返回本地视频或音频可用于 AI 总结的字幕轨道。"""
    extension = os.path.splitext(filename)[1].lower().lstrip('.')
    if extension in AUDIO_EXTENSIONS:
        sidecars = get_audio_sidecar_subtitles(filename)
        if sidecars:
            return sidecars
        tracks = get_embedded_subtitles(filename)
        for track in tracks:
            track.setdefault('id', f"embedded:{track.get('stream_index')}")
            track.setdefault('source_kind', 'embedded')
            track.setdefault('subtitle_filename', '')
        return tracks
    return get_video_subtitle_tracks(filename)


def get_player_exclude_keywords():
    exclude_keywords = config.get("PLAYER_FILENAME_EXCLUDE_KEYWORDS", [])
    if not isinstance(exclude_keywords, list):
        app.logger.warning("PLAYER_FILENAME_EXCLUDE_KEYWORDS 必须是字符串数组，已忽略无效配置")
        return []
    return [
        keyword for keyword in exclude_keywords
        if isinstance(keyword, str) and keyword
    ]

def render_index_page(
    url='',
    types=None,
    tasks=None,
    error='',
    view='download',
    tab='video',
    status=200,
):
    """渲染单页（下载器 + 媒体库），三个入口共用同一个模板。"""
    user = current_user()
    html = render_template(
        'index.html',
        current_user=user,
        google_identity=(
            user_store.get_identity_for_user(
                USER_DB_PATH, user_store.PROVIDER_GOOGLE, user['id']
            )
            if user
            else None
        ),
        url=url,
        types=types or [],
        tasks=tasks or [],
        error=error,
        view=view,
        tab=tab,
        requested_file=request.args.get('file', ''),
        show_waline=config.get("SHOW_WALINE_ON_INDEX", False),
        anonymous_task_limit=anonymous_task_limit(),
        anonymous_files_expire_hours=config.get(
            'ANONYMOUS_FILES_EXPIRE_HOURS', 24
        ),
    )
    return (html, status) if status != 200 else html


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        types = list(dict.fromkeys(
            task_type for task_type in request.form.getlist('type')
            if task_type in {'video', 'audio'}
        ))
        # 从分享文本中提取URL
        url = extract_url(request.form.get('url'))

        if not url or not types:
            return render_index_page(
                types=types,
                error='请输入有效链接并至少选择一种下载格式',
                status=400,
            )

        # 播放列表展开为逐集 URL，再逐个创建任务
        urls, error = expand_task_urls(url)
        if error:
            return render_index_page(
                url=url,
                types=types,
                error=error,
                status=400,
            )

        submitter = current_user()
        if submitter is None:
            task_count = len(urls) * len(types)
            ip_hash = anonymous_ip_hash()
            day_key = anonymous_day_key()
            allowed, _used = user_store.reserve_anonymous_downloads(
                USER_DB_PATH,
                ip_hash,
                day_key,
                task_count,
                anonymous_task_limit(),
            )
            if not allowed:
                remember_pending_task(url, types)
                return redirect(url_for('login', next=url_for('index')))
            try:
                task_ids = create_tasks(urls, types)
            except Exception:
                user_store.release_anonymous_downloads(
                    USER_DB_PATH, ip_hash, day_key, task_count
                )
                raise
            user_store.record_anonymous_tasks(
                USER_DB_PATH,
                task_ids,
                current_anonymous_id(),
                ip_hash,
                url=url,
            )
        else:
            task_ids = create_tasks(urls, types)
            user_store.record_tasks(
                USER_DB_PATH, task_ids, submitter['id'], url=url
            )

        # 构建重定向URL，包含所有参数
        redirect_url = url_for(
            'index',
            url=url,
            types=','.join(types),
            tasks=','.join(task_ids),
        )
        return redirect(redirect_url)

    # GET请求处理
    requested_view = 'library' if request.args.get('view') == 'library' else 'download'
    return render_index_page(
        url=request.args.get('url', ''),
        types=request.args.get('types', '').split(',') if request.args.get('types') else [],
        tasks=request.args.get('tasks', '').split(',') if request.args.get('tasks') else [],
        view=requested_view,
        tab='audio' if request.args.get('tab') == 'audio' else 'video',
    )


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user() is not None:
        return redirect(safe_next_url(request.args.get('next')))

    error = ''
    email = ''
    if request.method == 'POST':
        email = request.form.get('email', '')
        password = request.form.get('password', '')
        user = user_store.get_user_by_email(USER_DB_PATH, email)
        if not user or not user_store.verify_password(user, password):
            # 不区分「邮箱不存在」和「密码错误」，避免探测已注册邮箱。
            error = '邮箱或密码不正确'
        elif user['status'] == user_store.STATUS_PENDING:
            error = '账号正在等待管理员审批'
        elif user['status'] != user_store.STATUS_ACTIVE:
            error = '账号已被停用'
        else:
            login_user(user)
            created = consume_pending_task(user)
            if created:
                task_ids, pending_url = created
                return redirect(
                    url_for('index', tasks=','.join(task_ids), url=pending_url)
                )
            return redirect(safe_next_url(request.form.get('next')))

    return render_template(
        'login.html',
        mode='login',
        error=error,
        email=email,
        next_url=request.values.get('next', ''),
        registration_open=bool(config.get('REGISTRATION_OPEN', True)),
        google_enabled=google_oauth_configured(),
    ), (400 if error else 200)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user() is not None:
        return redirect(url_for('index'))

    first_user = user_store.user_count(USER_DB_PATH) == 0
    if not first_user and not config.get('REGISTRATION_OPEN', True):
        abort(403)

    error = ''
    email = request.form.get('email', '')
    display_name = request.form.get('display_name', '')
    if request.method == 'POST':
        password = request.form.get('password', '')
        if len(password) < 8:
            error = '密码至少 8 位'
        else:
            try:
                user = user_store.create_user(
                    USER_DB_PATH,
                    email,
                    password=password,
                    display_name=display_name,
                )
            except ValueError as exc:
                error = str(exc)
            else:
                if user['status'] == user_store.STATUS_ACTIVE:
                    adopt_legacy_data(user['id'])
                    login_user(user)
                    created = consume_pending_task(user)
                    if created:
                        task_ids, pending_url = created
                        return redirect(
                            url_for('index', tasks=','.join(task_ids), url=pending_url)
                        )
                    return redirect(url_for('index'))
                return render_template('login.html', mode='pending'), 200

    return render_template(
        'login.html',
        mode='register',
        error=error,
        email=email,
        display_name=display_name,
        first_user=first_user,
        google_enabled=google_oauth_configured(),
    ), (400 if error else 200)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/admin', methods=['GET'])
@admin_required
def admin():
    return render_template(
        'admin.html',
        users=user_store.list_users(USER_DB_PATH),
        current_user_id=request.user['id'],
    )


@app.route('/admin/users/<user_id>/status', methods=['POST'])
@admin_required
def admin_set_status(user_id):
    status = request.form.get('status', '')
    if user_id == request.user['id']:
        # 防止管理员把自己锁在外面
        abort(400)
    target = user_store.get_user(USER_DB_PATH, user_id)
    if not target:
        abort(404)
    try:
        user_store.set_status(USER_DB_PATH, user_id, status)
    except ValueError:
        abort(400)
    app.logger.info('管理员 %s 将用户 %s 状态改为 %s', request.user['email'], target['email'], status)
    return redirect(url_for('admin'))


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


def adopt_legacy_data(user_id):
    """把单用户时代的遗留数据归到首个管理员名下。

    包括：URLS_DIR 中已有的任务文件、FILES_DIR 中已有的媒体文件，以及原
    先保存在 GOOGLE_OAUTH_TOKEN_FILE 的全局 Google 令牌。只在首个账号
    创建时调用一次；重复执行是安全的（归属表按主键忽略冲突）。
    """
    adopted_tasks = 0
    try:
        for entry in os.listdir(URLS_DIR):
            task_id, extension = os.path.splitext(entry)
            if extension not in {'.txt', '.downloading', '.ok', '.fail'}:
                continue
            if not TASK_ID_PATTERN.fullmatch(task_id):
                continue
            if (
                user_store.task_owner(USER_DB_PATH, task_id)
                or user_store.anonymous_task_owner(USER_DB_PATH, task_id)
            ):
                continue
            url = ''
            try:
                with open(os.path.join(URLS_DIR, entry), 'r', encoding='utf-8') as f:
                    url = f.read().strip()
            except OSError:
                pass
            user_store.record_tasks(
                USER_DB_PATH,
                [task_id],
                user_id,
                url=url,
                media_type='video' if task_id[0] == 'v' else 'audio',
            )
            adopted_tasks += 1
    except OSError as exc:
        app.logger.warning('接管历史任务失败: %s', exc)

    adopted_files = 0
    try:
        for filename in os.listdir(FILES_DIR):
            if (
                os.path.isfile(os.path.join(FILES_DIR, filename))
                and user_store.media_owner(USER_DB_PATH, filename) is None
                and user_store.anonymous_media_owner(
                    USER_DB_PATH, filename
                ) is None
            ):
                user_store.record_media(USER_DB_PATH, filename, user_id)
                adopted_files += 1
    except OSError as exc:
        app.logger.warning('接管历史文件失败: %s', exc)

    legacy_token = youtube_auth.load_token(config)
    if legacy_token and not user_store.load_google_token(USER_DB_PATH, user_id):
        user_store.UserTokenStore(USER_DB_PATH, user_id).save(legacy_token)
        app.logger.info('已接管历史 Google 令牌')

    app.logger.info(
        '首个管理员接管历史数据：任务 %s 个，文件 %s 个',
        adopted_tasks,
        adopted_files,
    )


def sync_media_ownership():
    """依据任务结果文件补齐媒体归属。

    下载器不感知用户，它只写 `<task>.result.json`。这里把其中的文件名
    与任务归属对应起来补进 media_owners，未知来源的文件（历史遗留、
    播放列表监控产物）归到首个管理员，避免出现任何人都看不到的孤儿文件。
    """
    known = {
        filename: ('user', owner)
        for filename, owner in user_store.media_owner_map(USER_DB_PATH).items()
    }
    known.update({
        filename: ('anonymous', owner)
        for filename, owner in user_store.anonymous_media_owner_map(
            USER_DB_PATH
        ).items()
    })
    fallback = user_store.first_admin_id(USER_DB_PATH)

    for result_path in glob.glob(os.path.join(URLS_DIR, '*.result.json')):
        task_id = os.path.basename(result_path)[: -len('.result.json')]
        user_owner = user_store.task_owner(USER_DB_PATH, task_id)
        anonymous_owner = user_store.anonymous_task_owner(USER_DB_PATH, task_id)
        if not user_owner and not anonymous_owner:
            continue
        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                result = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for filename in result.get('files', []):
            if isinstance(filename, str) and filename not in known:
                if user_owner:
                    user_store.record_media(
                        USER_DB_PATH, filename, user_owner, task_id
                    )
                    known[filename] = ('user', user_owner)
                else:
                    user_store.record_anonymous_media(
                        USER_DB_PATH,
                        filename,
                        anonymous_owner,
                        task_id,
                        created_at=(
                            os.path.getmtime(os.path.join(FILES_DIR, filename))
                            if os.path.isfile(os.path.join(FILES_DIR, filename))
                            else None
                        ),
                    )
                    known[filename] = ('anonymous', anonymous_owner)

    if not fallback:
        return known
    try:
        entries = os.listdir(FILES_DIR)
    except OSError:
        return known
    for filename in entries:
        if filename not in known and os.path.isfile(
            os.path.join(FILES_DIR, filename)
        ):
            user_store.record_media(USER_DB_PATH, filename, fallback)
            known[filename] = ('user', fallback)
    return known


def owned_tasks(task_ids, owner):
    """过滤出属于当前主体的任务，防止用任务 ID 越权读取。"""
    owner_type, owner_id = owner
    if owner_type == 'user':
        owns = lambda task_id: (
            user_store.task_owner(USER_DB_PATH, task_id) == owner_id
        )
    else:
        owns = lambda task_id: (
            user_store.anonymous_task_owner(USER_DB_PATH, task_id) == owner_id
        )
    return [task_id for task_id in task_ids if owns(task_id)]


def google_oauth_configured():
    return bool(
        config.get('GOOGLE_OAUTH_CLIENT_ID')
        and config.get('GOOGLE_OAUTH_CLIENT_SECRET')
        and config.get('GOOGLE_OAUTH_REDIRECT_URI')
    )


def fetch_google_userinfo(credentials):
    """用已授权凭据读取 Google 账号的 sub / email / 名称 / 头像。"""
    response = requests.get(
        'https://www.googleapis.com/oauth2/v3/userinfo',
        headers={'Authorization': f'Bearer {credentials.token}'},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def oauth_result(ok, message, status=200):
    return render_template('oauth_result.html', ok=ok, message=message), status


@app.route('/oauth/start')
def oauth_start():
    """Google 授权入口。

    intent=login  仅申请身份 scope，用于 Google 登录/注册。
    intent=bind   申请 YouTube 与 Drive scope，需要先登录，用于把 Google
                  账号绑定到当前用户，供播放列表监控和后续的 Drive 上传使用。
    """
    if not google_oauth_configured():
        return oauth_result(False, 'Google OAuth 未配置，请联系管理员。', 503)

    intent = 'bind' if request.args.get('intent') == 'bind' else 'login'
    if intent == 'bind' and current_user() is None:
        return redirect(url_for('login', next=request.full_path))

    scopes = (
        youtube_auth.FULL_SCOPES if intent == 'bind' else youtube_auth.LOGIN_SCOPES
    )
    flow = youtube_auth.build_oauth_flow(config, scopes=scopes)
    state = hashlib.sha256(os.urandom(32)).hexdigest()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        state=state,
        include_granted_scopes='true',
    )
    session['oauth_state'] = state
    session['oauth_intent'] = intent
    session['oauth_scopes'] = scopes
    # PKCE：authorization_url 会生成 code_verifier 并放入 code_challenge，
    # 回调换取令牌时必须带上同一个 code_verifier，故一并存入会话
    session['oauth_code_verifier'] = flow.code_verifier
    return redirect(authorization_url)


@app.route('/oauth/callback')
def oauth_callback():
    error = request.args.get('error')
    if error:
        return oauth_result(False, f'授权被取消或失败：{error}', 400)

    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        return oauth_result(False, 'OAuth state 校验失败，请重新发起授权。', 400)

    code = request.args.get('code')
    if not code:
        return oauth_result(False, '缺少授权码，请重新发起授权。', 400)

    intent = session.pop('oauth_intent', 'login')
    scopes = session.pop('oauth_scopes', None)

    # 恢复 /oauth/start 时生成的 PKCE code_verifier，否则换取令牌会报
    # invalid_grant: Missing code verifier
    flow = youtube_auth.build_oauth_flow(config, scopes=scopes)
    flow.code_verifier = session.pop('oauth_code_verifier', None)
    if not flow.code_verifier:
        return oauth_result(
            False, '缺少 PKCE code_verifier（会话可能已丢失），请重新发起授权。', 400
        )

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Exception as exc:
        return oauth_result(False, f'换取令牌失败：{exc}', 400)

    try:
        userinfo = fetch_google_userinfo(creds)
    except Exception as exc:
        app.logger.warning('读取 Google 用户信息失败: %s', exc)
        return oauth_result(False, f'读取 Google 账号信息失败：{exc}', 400)

    google_sub = userinfo.get('sub', '')
    if not google_sub:
        return oauth_result(False, 'Google 未返回账号标识，请重试。', 400)

    google_email = userinfo.get('email', '')
    google_name = userinfo.get('name', '') or google_email
    google_avatar = userinfo.get('picture', '')

    if intent == 'bind':
        return _complete_google_bind(
            creds, google_sub, google_email, google_name, google_avatar
        )
    return _complete_google_login(
        creds, google_sub, google_email, google_name, google_avatar
    )


def _store_google_credentials(user_id, creds):
    """保存令牌。Google 只在首次同意时返回 refresh_token，重新授权时若
    缺失则保留已有的那一份，避免把可用的离线凭据覆盖掉。"""
    token = json.loads(creds.to_json())
    if not token.get('refresh_token'):
        existing = user_store.UserTokenStore(USER_DB_PATH, user_id).load() or {}
        if existing.get('refresh_token'):
            token['refresh_token'] = existing['refresh_token']
    user_store.UserTokenStore(USER_DB_PATH, user_id).save(token)
    return token


def _complete_google_login(creds, sub, email, name, avatar):
    identity = user_store.get_identity(USER_DB_PATH, user_store.PROVIDER_GOOGLE, sub)
    if identity:
        user = user_store.get_user(USER_DB_PATH, identity['user_id'])
    else:
        # 同邮箱的已有账号直接绑定，避免一个人产生两个账号。
        user = user_store.get_user_by_email(USER_DB_PATH, email) if email else None
        if user is None:
            if not email:
                return oauth_result(False, 'Google 账号没有可用邮箱，无法注册。', 400)
            first_user = user_store.user_count(USER_DB_PATH) == 0
            if not first_user and not config.get('REGISTRATION_OPEN', True):
                return oauth_result(False, '本站已关闭注册，请联系管理员。', 403)
            try:
                user = user_store.create_user(
                    USER_DB_PATH,
                    email,
                    display_name=name,
                    provider=user_store.PROVIDER_GOOGLE,
                )
            except ValueError as exc:
                return oauth_result(False, str(exc), 400)
            if user['status'] == user_store.STATUS_ACTIVE:
                adopt_legacy_data(user['id'])
        user_store.link_identity(
            USER_DB_PATH,
            user_store.PROVIDER_GOOGLE,
            sub,
            user['id'],
            email=email,
            display_name=name,
            avatar_url=avatar,
        )

    if user['status'] == user_store.STATUS_PENDING:
        return oauth_result(False, '账号已创建，正在等待管理员审批。', 200)
    if user['status'] != user_store.STATUS_ACTIVE:
        return oauth_result(False, '账号已被停用，请联系管理员。', 403)

    _store_google_credentials(user['id'], creds)
    login_user(user)
    created = consume_pending_task(user)
    if created:
        task_ids, pending_url = created
        return redirect(
            url_for('index', tasks=','.join(task_ids), url=pending_url)
        )
    return redirect(url_for('index'))


def _complete_google_bind(creds, sub, email, name, avatar):
    user = current_user()
    if user is None:
        return redirect(url_for('login'))

    try:
        user_store.link_identity(
            USER_DB_PATH,
            user_store.PROVIDER_GOOGLE,
            sub,
            user['id'],
            email=email,
            display_name=name,
            avatar_url=avatar,
        )
    except ValueError as exc:
        return oauth_result(False, str(exc), 409)

    token = _store_google_credentials(user['id'], creds)
    if not token.get('refresh_token'):
        return oauth_result(
            False,
            '未返回 refresh_token，请在 Google 账号页面移除本应用的授权后重新绑定。',
            400,
        )

    try:
        import bark_util

        device_token = config.get('BARK_DEVICE_TOKEN')
        if device_token:
            bark_util.bark_notify(
                device_token, 'Google 绑定成功', f"{user['email']} 已授权"
            )
    except Exception:
        pass

    return oauth_result(True, 'Google 绑定成功，播放列表监控与 Drive 授权已就绪。')


@app.route('/account/google/unbind', methods=['POST'])
@login_required
def google_unbind():
    user_store.unlink_identity(
        USER_DB_PATH, user_store.PROVIDER_GOOGLE, request.user['id']
    )
    user_store.delete_google_token(USER_DB_PATH, request.user['id'])
    return redirect(url_for('index'))


NUMERIC_FILENAME_PREFIX_PATTERN = re.compile(r'^\d+-')


def strip_numeric_filename_prefix(name):
    """隐藏下载器写入的数字时间戳前缀，如 08221544-example.mp4。"""
    stripped = NUMERIC_FILENAME_PREFIX_PATTERN.sub('', name, count=1)
    return stripped or name


def media_display_title(probed_title, fallback_name):
    """媒体标题优先用文件内嵌标签；回退到文件名时才隐藏数字前缀。"""
    if probed_title and probed_title != fallback_name:
        return probed_title
    return strip_numeric_filename_prefix(fallback_name)


def build_media_library_items(owner=None):
    """列出媒体库需要的视频与音频条目，按修改时间倒序。

    传入 owner 时只返回该用户或匿名会话拥有的文件；归属表由 sync_media_ownership()
    依据任务结果维护。
    """
    exclude_keywords = get_player_exclude_keywords()
    owners = sync_media_ownership() if owner else {}

    def listed(predicate):
        names = [
            filename for filename in os.listdir(FILES_DIR)
            if predicate(filename)
            and not any(keyword in filename for keyword in exclude_keywords)
            and (owner is None or owners.get(filename) == owner)
        ]
        names.sort(
            key=lambda name: os.path.getmtime(os.path.join(FILES_DIR, name)),
            reverse=True,
        )
        return names

    video_files = listed(lambda name: name.endswith('.mp4'))
    audio_files = listed(
        lambda name: os.path.splitext(name)[1].lower().lstrip('.')
        in AUDIO_EXTENSIONS
    )

    fallback_cover_url = config.get(
        'AUDIO_PLAYER_FALLBACK_COVER_URL',
        url_for('static', filename='images/audio-cover-default.svg'),
    )
    if not isinstance(fallback_cover_url, str) or not fallback_cover_url.strip():
        fallback_cover_url = url_for(
            'static',
            filename='images/audio-cover-default.svg',
        )
    default_library_cover_url = url_for(
        'static',
        filename='images/media-cover-default.svg',
    )

    def library_cover_candidates(metadata):
        candidates = list(metadata.get('cover_candidates') or [])
        candidates.append(default_library_cover_url)
        return list(dict.fromkeys(filter(None, candidates)))

    def base_item(filename, media_type):
        duration, height = get_media_dimensions(filename)
        extension = os.path.splitext(filename)[1].lower().lstrip('.')
        try:
            size_bytes = os.path.getsize(os.path.join(FILES_DIR, filename))
        except OSError:
            size_bytes = None
        return {
            'filename': filename,
            'type': media_type,
            'extension': extension,
            'height': height,
            'duration': duration,
            'size_bytes': size_bytes,
            'url': url_for('serve_file', filename=filename),
            'download_url': url_for('download_file', filename=filename),
        }

    videos = []
    for filename in video_files:
        metadata = get_video_metadata(filename)
        cover_candidates = library_cover_candidates(metadata)
        item = base_item(filename, 'video')
        item.update({
            'title': media_display_title(metadata.get('title'), filename),
            'artist': metadata.get('artist', ''),
            'source_url': metadata.get('source_url', ''),
            'description': metadata.get('description', ''),
            'poster': next(iter(metadata.get('cover_candidates') or []), ''),
            'thumbnail_candidates': cover_candidates,
        })
        videos.append(item)

    audios = []
    for filename in audio_files:
        metadata = get_audio_metadata(filename, fallback_cover_url)
        cover_candidates = library_cover_candidates(metadata)
        item = base_item(filename, 'audio')
        item.update({
            'title': media_display_title(
                metadata.get('title'),
                os.path.splitext(filename)[0],
            ),
            'artist': metadata.get('artist', ''),
            'source_url': metadata.get('source_url', ''),
            'description': metadata.get('description', ''),
            'poster': next(iter(metadata.get('cover_candidates') or []), ''),
            'thumbnail_candidates': cover_candidates,
        })
        audios.append(item)

    return videos, audios


def cleanup_expired_anonymous_media():
    """删除超过匿名保留期的本地媒体及其归属记录。"""
    cleanup_config = dict(config)
    cleanup_config['USER_DB_PATH'] = USER_DB_PATH
    cleanup_config['FILES_DIR'] = FILES_DIR
    cleanup_config['URLS_DIR'] = URLS_DIR
    anonymous_cleanup.cleanup_expired_media(cleanup_config, app.logger)


@app.route('/api/media_list', methods=['GET'])
def api_media_list():
    """媒体库列表接口，供首页的媒体库模式异步加载。"""
    try:
        cleanup_expired_anonymous_media()
        videos, audios = build_media_library_items(current_owner())
    except OSError as exc:
        app.logger.error("读取媒体库目录失败: %s", exc)
        return jsonify({"success": False, "msg": "读取媒体目录失败"}), 500
    return jsonify({"success": True, "video": videos, "audio": audios})


@app.route('/player')
def player():
    """媒体库入口，与首页同页；保留原路径以兼容既有播放链接。"""
    return render_index_page(
        view='library',
        tab='audio' if request.args.get('tab') == 'audio' else 'video',
    )


@app.route('/audio-player')
def audio_player():
    """兼容已有音频链接，在媒体库中打开音频标签。"""
    return render_index_page(view='library', tab='audio')
def require_media_access(filename):
    """媒体私有：只有登录用户本人或原匿名会话可以访问。"""
    cleanup_expired_anonymous_media()
    request_owner = current_owner()
    user_owner = user_store.media_owner(USER_DB_PATH, filename)
    anonymous_owner = user_store.anonymous_media_owner(USER_DB_PATH, filename)
    if user_owner is None and anonymous_owner is None:
        # 归属未登记的文件先补齐，再判断，避免刚下载完就 404。
        resource_owner = sync_media_ownership().get(filename)
    elif user_owner:
        resource_owner = ('user', user_owner)
    else:
        resource_owner = ('anonymous', anonymous_owner)
    if resource_owner != request_owner:
        abort(404)


@app.route('/files/<path:filename>')
def serve_file(filename):
    decoded_filename = unquote(filename)
    require_media_access(decoded_filename)
    return send_from_directory(FILES_DIR, decoded_filename)


@app.route('/downloads/<path:filename>')
def download_file(filename):
    """以附件方式下载 FILES_DIR 中的媒体文件。"""
    decoded_filename = unquote(filename)
    require_media_access(decoded_filename)
    return send_from_directory(
        FILES_DIR,
        decoded_filename,
        as_attachment=True,
    )


@app.route('/subtitles/<path:filename>/<int:stream_index>.vtt')
def serve_subtitle(filename, stream_index):
    """将 MP4 内嵌字幕流转换为浏览器可读取的 WebVTT。"""
    decoded_filename = unquote(filename)
    require_media_access(decoded_filename)
    filepath = safe_join(FILES_DIR, decoded_filename)
    if (
        not filepath
        or os.path.splitext(filepath)[1].lower().lstrip('.') not in ({'mp4'} | AUDIO_EXTENSIONS)
        or not os.path.isfile(filepath)
    ):
        abort(404)

    valid_stream_indexes = {
        track["stream_index"] for track in get_embedded_subtitles(decoded_filename)
    }
    if stream_index not in valid_stream_indexes:
        abort(404)

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", filepath,
                "-map", f"0:{stream_index}", "-f", "webvtt", "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        app.logger.error("找不到 ffmpeg，无法转换视频字幕")
        return Response("ffmpeg is required", status=503, content_type="text/plain; charset=utf-8")
    except (subprocess.SubprocessError, OSError) as exc:
        app.logger.error("转换视频字幕失败: %s (stream=%s, error=%s)", filepath, stream_index, exc)
        return Response("subtitle conversion failed", status=500, content_type="text/plain; charset=utf-8")

    return Response(result.stdout, content_type="text/vtt; charset=utf-8")


@app.route('/subtitles/sidecar/<path:subtitle_filename>.vtt')
def serve_sidecar_subtitle(subtitle_filename):
    """将有效的同名外挂字幕转换为浏览器可读取的 WebVTT。"""
    decoded_filename = unquote(subtitle_filename)
    if decoded_filename != os.path.basename(decoded_filename):
        abort(404)
    owning_video = next((
        video_filename
        for video_filename in os.listdir(FILES_DIR)
        if video_filename.lower().endswith('.mp4')
        if any(
            track['subtitle_filename'] == decoded_filename
            for track in get_sidecar_subtitles(video_filename)
        )
    ), None)
    filepath = safe_join(FILES_DIR, decoded_filename)
    if not owning_video or not filepath or not os.path.isfile(filepath):
        abort(404)
    require_media_access(owning_video)
    try:
        subtitle = convert_sidecar_subtitle_to_webvtt(filepath)
    except RuntimeError as exc:
        app.logger.error("转换外挂字幕失败: %s (%s)", filepath, exc)
        return Response(
            "subtitle conversion failed",
            status=500,
            content_type="text/plain; charset=utf-8",
        )
    return Response(subtitle, content_type="text/vtt; charset=utf-8")


@app.route('/api/ai_summary', methods=['POST'])
def api_ai_summary():
    """播放器兼容接口：命中持久化总结或创建本地字幕异步任务。"""
    if not ai_summary_is_configured():
        return ai_summary_api_response({"success": False, "message": "AI 总结尚未完成配置"}, 503)

    data = request.get_json(silent=True) or {}
    job_id = data.get('job_id')
    if isinstance(job_id, str) and job_id:
        job = ai_summary_store.get_job(config['AI_SUMMARY_DB_PATH'], job_id)
        if not job:
            return ai_summary_api_response({
                'success': False,
                'message': 'AI 总结任务不存在',
            }, 404)
        payload, status = ai_summary_job_payload(job, legacy=True)
        return ai_summary_api_response(payload, status)

    filename = data.get("filename")
    stream_index = data.get("stream_index")
    subtitle_source = data.get('subtitle_source')
    subtitle_filename = data.get('subtitle_filename')
    if not isinstance(filename, str) or not filename:
        return ai_summary_api_response({"success": False, "message": "缺少视频文件名"}, 400)
    if stream_index is not None and (
        isinstance(stream_index, bool) or not isinstance(stream_index, int)
    ):
        return ai_summary_api_response({"success": False, "message": "字幕流编号无效"}, 400)

    require_media_access(filename)

    filepath = safe_join(FILES_DIR, filename)
    if (
        not filepath
        or os.path.splitext(filepath)[1].lower().lstrip('.') not in ({'mp4'} | AUDIO_EXTENSIONS)
        or not os.path.isfile(filepath)
    ):
        return ai_summary_api_response({"success": False, "message": "媒体文件不存在"}, 404)

    tracks = get_local_summary_tracks(filename)
    sidecar_tracks = [
        track for track in tracks if track['source_kind'] == 'sidecar'
    ]
    if not tracks:
        return ai_summary_api_response({"success": False, "message": "当前媒体没有可用字幕或歌词"}, 400)
    if subtitle_source is None:
        # 兼容旧播放器：存在外挂字幕时忽略旧的内嵌流编号并使用首条外挂字幕。
        if sidecar_tracks:
            selected_track = sidecar_tracks[0]
        elif stream_index is None:
            selected_track = tracks[0]
        else:
            selected_track = next(
                (track for track in tracks if track["stream_index"] == stream_index),
                None,
            )
    elif subtitle_source == 'sidecar' and isinstance(subtitle_filename, str):
        selected_track = next(
            (
                track for track in sidecar_tracks
                if track['subtitle_filename'] == subtitle_filename
            ),
            None,
        )
    elif subtitle_source == 'embedded' and not sidecar_tracks:
        selected_track = next(
            (track for track in tracks if track["stream_index"] == stream_index),
            None,
        )
    else:
        selected_track = None
    if selected_track is None:
        return ai_summary_api_response({"success": False, "message": "所选字幕不存在"}, 400)

    profile_key = ai_summary_store.summary_profile_key(config)
    source_url = get_media_source_url(filename)
    normalized_key = None
    if source_url:
        try:
            normalized_key = ai_summary_store.normalize_source_url(source_url)
        except ValueError:
            normalized_key = None
    if normalized_key:
        summary = ai_summary_store.find_summary_for_url(
            config['AI_SUMMARY_DB_PATH'],
            normalized_key,
            profile_key,
        )
        if summary:
            return ai_summary_api_response({
                'success': True,
                'status': 'completed',
                'summary': summary['markdown'],
                'subtitle': summary['subtitle_language'] or summary['subtitle_kind'],
                'cached': True,
            })
    else:
        normalized_key = ai_summary_store.local_source_key(filepath)

    created = ai_summary_store.create_local_job(
        config['AI_SUMMARY_DB_PATH'],
        filename,
        selected_track['stream_index'],
        normalized_key,
        profile_key,
        subtitle_source=selected_track['source_kind'],
        subtitle_filename=selected_track['subtitle_filename'],
    )
    if created['summary']:
        summary = created['summary']
        return ai_summary_api_response({
            'success': True,
            'status': 'completed',
            'summary': summary['markdown'],
            'subtitle': summary['subtitle_language'] or summary['subtitle_kind'],
            'cached': True,
        })
    payload, status = ai_summary_job_payload(created['job'], legacy=True)
    return ai_summary_api_response(payload, status)


@app.route('/api/ai_summary/jobs/<job_id>/stream', methods=['GET'])
def api_ai_summary_stream(job_id):
    """播放器使用的同源流式总结接口。"""
    if not ai_summary_is_configured():
        return ai_summary_api_response({'success': False, 'message': 'AI 总结尚未完成配置'}, 503)
    return ai_summary_job_stream(job_id, legacy=True)


@app.route('/api/ai_summaries', methods=['POST'])
def api_ai_summaries():
    """Chrome 扩展按 URL 查询或创建 AI 总结任务。"""
    denied = require_ai_summary_access()
    if denied:
        return denied
    if not ai_summary_is_configured():
        return ai_summary_api_response({
            'success': False,
            'message': 'AI 总结尚未完成配置',
        }, 503)
    data = request.get_json(silent=True) or {}
    source_url = data.get('url')
    try:
        normalized_url = ai_summary_store.validate_public_url(source_url)
    except ValueError as exc:
        return ai_summary_api_response({
            'success': False,
            'message': str(exc),
        }, 400)
    created = ai_summary_store.create_url_job(
        config['AI_SUMMARY_DB_PATH'],
        source_url.strip(),
        normalized_url,
        ai_summary_store.summary_profile_key(config),
    )
    if created['summary']:
        return ai_summary_api_response({
            'success': True,
            'status': 'completed',
            'cached': True,
            'job_id': None,
            'summary': created['summary'],
        })
    payload, status = ai_summary_job_payload(created['job'])
    return ai_summary_api_response(payload, status)


@app.route('/api/ai_summaries/jobs/<job_id>', methods=['GET'])
def api_ai_summary_job(job_id):
    denied = require_ai_summary_access()
    if denied:
        return denied
    job = ai_summary_store.get_job(config['AI_SUMMARY_DB_PATH'], job_id)
    if not job:
        return ai_summary_api_response({
            'success': False,
            'message': 'AI 总结任务不存在',
        }, 404)
    payload, status = ai_summary_job_payload(job)
    return ai_summary_api_response(payload, status)


@app.route('/api/ai_summaries/jobs/<job_id>/stream', methods=['GET'])
def api_ai_summary_job_stream(job_id):
    """Chrome 扩展使用的鉴权流式总结接口。"""
    denied = require_ai_summary_access()
    if denied:
        return denied
    return ai_summary_job_stream(job_id)

@app.route('/api/add_task', methods=['POST'])
def api_add_task():
    data = request.get_json() if request.is_json else request.form
    url = data.get('url')
    types = data.get('types')

    # 从分享文本中提取URL
    url = extract_url(url)

    if not url or not types:
        return jsonify({"success": False, "msg": "Missing required parameters: url and types"}), 400
    if not isinstance(types, list):
        # 支持表单传递的字符串类型
        types = [types]
    types = list(dict.fromkeys(
        task_type for task_type in types
        if task_type in {'video', 'audio'}
    ))
    if not types:
        return jsonify({"success": False, "msg": "Invalid download types"}), 400

    user = current_user()
    # 播放列表展开为逐集 URL，再逐个创建任务
    urls, error = expand_task_urls(url)
    if error:
        return jsonify({"success": False, "msg": error}), 400

    if user is None:
        task_count = len(urls) * len(types)
        ip_hash = anonymous_ip_hash()
        day_key = anonymous_day_key()
        limit = anonymous_task_limit()
        allowed, used = user_store.reserve_anonymous_downloads(
            USER_DB_PATH, ip_hash, day_key, task_count, limit
        )
        if not allowed:
            remember_pending_task(url, types)
            return jsonify({
                "success": False,
                "msg": f"匿名用户每天最多下载 {limit} 条，请登录后继续",
                "login_required": True,
                "login_url": url_for('login'),
                "anonymous_limit": limit,
                "anonymous_used": used,
            }), 401
        try:
            tasks = create_tasks(urls, types)
        except Exception:
            user_store.release_anonymous_downloads(
                USER_DB_PATH, ip_hash, day_key, task_count
            )
            raise
        user_store.record_anonymous_tasks(
            USER_DB_PATH,
            tasks,
            current_anonymous_id(),
            ip_hash,
            url=url,
        )
        remaining = max(0, limit - used)
    else:
        tasks = create_tasks(urls, types)
        user_store.record_tasks(USER_DB_PATH, tasks, user['id'], url=url)
        remaining = None

    if len(urls) > 1:
        msg = f"播放列表已解析为 {len(urls)} 个视频，共创建 {len(tasks)} 个任务"
    else:
        msg = "Task added successfully" if len(tasks) == 1 else "Tasks added successfully"
    payload = {"success": True, "msg": msg, "tasks": tasks}
    if remaining is not None:
        payload['anonymous_remaining'] = remaining
    return jsonify(payload)

@app.route('/api/task_info', methods=['POST'])
def api_task_info():
    data = request.get_json() if request.is_json else request.form
    tasks = data.get('tasks')
    if not tasks:
        return jsonify({"success": False, "msg": "Missing required parameter: tasks"}), 400
    if not isinstance(tasks, list):
        tasks = [tasks]
    # 逐个返回，保持「请求几个就回几个」的契约：前端据此结束轮询。
    # 非本人任务与不存在的任务返回同样的 missing，不泄露是否存在。
    owned = set(owned_tasks(tasks, current_owner()))
    result = [
        get_task_info(task)
        if task in owned
        else {"task": task, "exists": False, "state": "missing"}
        for task in tasks
    ]
    return jsonify({"success": True, "tasks": result})


# 各动作允许的起始状态；与前端按钮显示规则保持一致
TASK_ACTION_STATES = {
    'pause': {'queued', 'downloading'},
    'resume': {'paused'},
    'restart': {'downloading', 'paused', 'failed'},
    'delete': {'queued', 'downloading', 'paused', 'failed', 'completed'},
}


def delete_task_media(task_id, owner):
    """删除已完成任务下载的文件；只删当前主体名下的，避免误删他人同名文件。"""
    filenames = task_queue.task_result_files(URLS_DIR, task_id)
    if not filenames:
        filenames = recover_task_files_from_logs(task_id)
    owner_type, owner_id = owner
    removed = 0
    for filename in filenames:
        if not isinstance(filename, str) or filename != os.path.basename(filename):
            continue
        if owner_type == 'user':
            owned = user_store.media_owner(USER_DB_PATH, filename) == owner_id
        else:
            owned = user_store.anonymous_media_owner(USER_DB_PATH, filename) == owner_id
        if not owned:
            continue
        filepath = safe_join(FILES_DIR, filename)
        if filepath and os.path.isfile(filepath):
            os.remove(filepath)
            removed += 1
        user_store.delete_media(USER_DB_PATH, filename)
        user_store.delete_anonymous_media(USER_DB_PATH, filename)
    return removed


def perform_task_action(task_id, action, owner, delete_files=False):
    """执行任务动作，返回 ``(HTTP 状态码, 结果字典)``。

    未在下载的任务直接改写状态文件；正在下载的交给下载器（写控制指令）。
    状态文件可能在两次检查之间被下载器改名（例如刚从排队转为下载），
    改名失败时重新判定状态再试。
    """
    for _ in range(3):
        task_path, state = task_queue.find_task_file(URLS_DIR, task_id)
        if task_path is None:
            return 404, {"success": False, "msg": "任务不存在"}
        if state not in TASK_ACTION_STATES[action]:
            return 409, {"success": False, "msg": "当前状态不支持该操作", "state": state}

        base = task_path[:-len(os.path.splitext(task_path)[1])]
        try:
            if state == 'downloading':
                control = 'restart' if action == 'restart' else action
                task_queue.write_task_control(URLS_DIR, task_id, control)
                if action == 'delete':
                    user_store.forget_task(USER_DB_PATH, task_id)
                return 202, {"success": True, "state": state, "pending": action}

            if action == 'pause':
                os.rename(task_path, base + task_queue.PAUSED_EXTENSION)
                return 200, {"success": True, "state": "paused"}

            if action == 'resume':
                # 同一任务 ID 重新入队，下载器会复用临时目录断点续传
                os.rename(task_path, base + '.txt')
                return 200, {"success": True, "state": "queued"}

            if action == 'restart':
                task_queue.discard_task_temp(config["TMP_DIR"], task_id)
                try:
                    os.remove(os.path.join(URLS_DIR, f"{task_id}.result.json"))
                except FileNotFoundError:
                    pass
                os.rename(task_path, base + '.txt')
                return 200, {"success": True, "state": "queued"}

            # delete：先删状态文件，确保排队中的任务不会再被下载器拾取
            os.remove(task_path)
        except FileNotFoundError:
            continue

        removed = 0
        if state == 'completed' and delete_files:
            removed = delete_task_media(task_id, owner)
        task_queue.discard_task_temp(config["TMP_DIR"], task_id)
        task_queue.remove_task_records(URLS_DIR, config["LOG_DIR"], task_id)
        user_store.forget_task(USER_DB_PATH, task_id)
        return 200, {"success": True, "state": "deleted", "removed_files": removed}

    return 409, {"success": False, "msg": "任务状态正在变化，请稍后重试"}


@app.route('/api/task_action', methods=['POST'])
def api_task_action():
    """暂停、继续、重启或删除当前主体自己的任务。"""
    data = request.get_json(silent=True) or {}
    task_id = data.get('task')
    action = data.get('action')
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        return jsonify({"success": False, "msg": "Invalid task id"}), 400
    if action not in TASK_ACTION_STATES:
        return jsonify({"success": False, "msg": "Invalid action"}), 400

    owner = current_owner(create_anonymous=False)
    # 非本人任务与不存在的任务同样返回 404，不泄露任务是否存在
    if owner[0] is None or not owned_tasks([task_id], owner):
        return jsonify({"success": False, "msg": "任务不存在"}), 404

    status, payload = perform_task_action(
        task_id,
        action,
        owner,
        delete_files=bool(data.get('delete_files')),
    )
    payload["task"] = task_id
    return jsonify(payload), status


def read_downloader_log_chunk(filepath, cursor=None, expected_file_id=None):
    """按字节游标读取 downloader.log，兼容日志截断与轮转。"""
    file_stat = os.stat(filepath)
    file_size = file_stat.st_size
    file_identity = f"{file_stat.st_dev}:{file_stat.st_ino}"
    file_id = hashlib.sha256(file_identity.encode('ascii')).hexdigest()[:16]
    reset = cursor is not None and (
        cursor > file_size
        or (expected_file_id and expected_file_id != file_id)
    )

    if cursor is None or reset:
        start = max(0, file_size - DOWNLOADER_LOG_INITIAL_BYTES)
    else:
        start = cursor

    with open(filepath, 'rb') as log_file:
        log_file.seek(start)
        if start > 0 and (cursor is None or reset):
            log_file.readline()

        data = log_file.read(DOWNLOADER_LOG_MAX_BYTES)
        if data and not data.endswith(b'\n') and log_file.tell() < file_size:
            data += log_file.readline(DOWNLOADER_LOG_MAX_BYTES)
        next_cursor = log_file.tell()

    return {
        "text": data.decode('utf-8', errors='replace'),
        "cursor": next_cursor,
        "reset": reset,
        "has_more": next_cursor < file_size,
        "file_id": file_id,
    }


def read_task_log_tail(filepath):
    """读取单个任务日志的末尾，避免首页日志接口暴露完整全局日志。"""
    try:
        with open(filepath, 'rb') as log_file:
            log_file.seek(0, os.SEEK_END)
            file_size = log_file.tell()
            log_file.seek(max(0, file_size - DOWNLOADER_LOG_MAX_BYTES))
            data = log_file.read()
    except OSError:
        return ''
    return data.decode('utf-8', errors='replace')


def redact_task_log_text(text):
    """隐藏日志中的项目绝对路径，保留相对目录和文件名便于排查。"""
    project_root = os.path.abspath(os.path.dirname(__file__))
    return text.replace(project_root, '📁')


@app.route('/api/task_log', methods=['POST'])
def api_task_log():
    """返回指定任务相关的日志，供首页侧栏使用。"""
    data = request.get_json() if request.is_json else request.form
    tasks = data.get('tasks')
    if not tasks:
        return jsonify({"success": False, "msg": "Missing required parameter: tasks"}), 400
    if not isinstance(tasks, list):
        tasks = [tasks]
    tasks = [task for task in tasks if isinstance(task, str) and TASK_ID_PATTERN.fullmatch(task)]
    tasks = owned_tasks(tasks, current_owner())
    if not tasks or len(tasks) > 20:
        return jsonify({"success": False, "msg": "Invalid task list"}), 400

    task_urls = {}
    for task in tasks:
        task_info = get_task_info(task)
        if task_info.get('exists'):
            task_urls[task] = task_info.get('url', '')

    relevant_lines = []
    seen_lines = set()

    def append_lines(text):
        text = redact_task_log_text(text)
        for line in text.splitlines():
            if line and line not in seen_lines:
                seen_lines.add(line)
                relevant_lines.append(line)

    for task, task_url in task_urls.items():
        task_log_path = os.path.join(config['LOG_DIR'], f'{task}.log')
        append_lines(read_task_log_tail(task_log_path))

    global_log_path = os.path.join(config['LOG_DIR'], 'downloader.log')
    global_log = read_task_log_tail(global_log_path)
    for line in global_log.splitlines():
        if any(
            marker and marker in line
            for marker in [*task_urls.keys(), *task_urls.values()]
        ):
            append_lines(line)

    return jsonify({
        'success': True,
        'text': '\n'.join(relevant_lines),
        'tasks': tasks,
    })


@app.route('/api/downloader_log', methods=['GET'])
def api_downloader_log():
    def log_response(payload, status=200):
        response = jsonify(payload)
        response.status_code = status
        response.headers['Cache-Control'] = 'no-store, private'
        return response

    configured_token = str(config.get("EXTENSION_LOG_TOKEN", "")).strip()
    if not configured_token:
        return log_response({
            "success": False,
            "msg": "Downloader log API is disabled",
        }, 503)

    request_token = request.headers.get('X-Yter-Log-Token', '')
    if not hmac.compare_digest(request_token, configured_token):
        return log_response({
            "success": False,
            "msg": "Invalid log access token",
        }, 401)

    cursor_value = request.args.get('cursor')
    expected_file_id = request.args.get('file_id')
    cursor = None
    if cursor_value not in (None, ''):
        try:
            cursor = int(cursor_value)
        except ValueError:
            return log_response({
                "success": False,
                "msg": "Invalid cursor",
            }, 400)
        if cursor < 0:
            return log_response({
                "success": False,
                "msg": "Invalid cursor",
            }, 400)

    log_path = os.path.join(config["LOG_DIR"], 'downloader.log')
    if not os.path.isfile(log_path):
        return log_response({
            "success": True,
            "text": "",
            "cursor": 0,
            "reset": cursor not in (None, 0),
            "has_more": False,
            "file_id": None,
        })

    try:
        result = read_downloader_log_chunk(
            log_path,
            cursor,
            expected_file_id=expected_file_id,
        )
    except OSError as exc:
        app.logger.error("读取 downloader.log 失败: %s", exc)
        return log_response({
            "success": False,
            "msg": "Failed to read downloader log",
        }, 500)

    return log_response({"success": True, **result})

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.static_folder, 'images'),
                             'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/api/video_info', methods=['POST'])
def api_video_info():
    data = request.get_json() if request.is_json else request.form
    url = data.get('url')

    # 从分享文本中提取URL
    url = extract_url(url)

    if not url:
        return jsonify({"success": False, "msg": "Missing required parameter: url"}), 400
    
    try:
        conf_path = _pick_ytdlp_conf('video')
        cmd = [
            sys.executable, '-m', 'yt_dlp',
            '--config-location', conf_path,
            # 视频信息查询不继承下载配置中的 -t sleep 等限速等待设置。
            '--sleep-requests', '0',
            '--sleep-interval', '0',
            '--max-sleep-interval', '0',
            '--sleep-subtitles', '0',
            '--dump-single-json',
            '--no-playlist',
            '--no-warnings',
            url,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        info = json.loads(result.stdout)
        platform = info.get('extractor_key') or info.get('extractor') or '未知平台'
        uploader = (
            info.get('uploader')
            or info.get('channel')
            or info.get('creator')
            or info.get('artist')
            or platform
        )

        video_info = {
            "success": True,
            "title": info.get('title'),
            "description": info.get('description'),
            "duration": info.get('duration'),
            "uploader": uploader,
            "platform": platform,
            "upload_date": info.get('upload_date'),
            "view_count": info.get('view_count'),
            "like_count": info.get('like_count'),
            "thumbnail": info.get('thumbnail'),
            "formats": [{
                "format_id": f.get('format_id'),
                "ext": f.get('ext'),
                "resolution": f.get('resolution'),
                "filesize": f.get('filesize'),
                "format_note": f.get('format_note'),
                "vcodec": f.get('vcodec'),
                "acodec": f.get('acodec'),
            } for f in info.get('formats', []) if f.get('vcodec') != 'none'],
            "audio_formats": [{
                "format_id": f.get('format_id'),
                "ext": f.get('ext'),
                "filesize": f.get('filesize'),
                "acodec": f.get('acodec'),
            } for f in info.get('formats', []) if f.get('vcodec') == 'none'],
        }

        return jsonify(video_info)
            
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            stderr = (e.stderr or '').strip()
            stdout = (e.stdout or '').strip()
            detail = stderr or stdout or str(e)
        else:
            detail = str(e)
        return jsonify({
            "success": False,
            "msg": f"Failed to get video info: {detail}"
        }), 500


@app.route('/api/video_info_basic', methods=['POST'])
def api_video_info_basic():
    """只提取首页预览所需的标题、作者、时长和缩略图。"""
    data = request.get_json() if request.is_json else request.form
    url = extract_url(data.get('url'))

    if not url:
        return jsonify({"success": False, "msg": "Missing required parameter: url"}), 400

    try:
        conf_path = _pick_ytdlp_conf('video')
        cmd = [
            sys.executable, '-m', 'yt_dlp',
            '--config-location', conf_path,
            '--sleep-requests', '0',
            '--sleep-interval', '0',
            '--max-sleep-interval', '0',
            '--sleep-subtitles', '0',
            '--no-progress',
            '--no-write-subs',
            '--no-write-auto-subs',
            '--no-playlist',
            '--no-warnings',
            '--print', '%(title)j\t%(uploader)j\t%(duration)j\t%(thumbnail)j',
            url,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        output_line = next(
            (line.strip() for line in reversed(result.stdout.splitlines()) if line.strip()),
            '',
        )
        values = output_line.split('\t', 3)
        if len(values) != 4:
            raise ValueError('yt-dlp returned incomplete basic video info')

        def parse_printed_value(value):
            if value in {'NA', 'N/A', 'null', 'None', ''}:
                return None
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value

        title, uploader, duration, thumbnail = (
            parse_printed_value(value) for value in values
        )
        return jsonify({
            'success': True,
            'title': title,
            'uploader': uploader,
            'duration': duration,
            'thumbnail': thumbnail,
        })
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            stderr = (e.stderr or '').strip()
            stdout = (e.stdout or '').strip()
            detail = stderr or stdout or str(e)
        else:
            detail = str(e)
        return jsonify({
            'success': False,
            'msg': f'Failed to get basic video info: {detail}',
        }), 500

def get_youtube_cookie():
    """从API获取YouTube cookie并保存到文件"""
    try:
        ytc_config = config.get('YTC', {})
        api_url = ytc_config.get('API_URL')
        auth_username = ytc_config.get('AUTH_USERNAME')
        auth_password = ytc_config.get('AUTH_PASSWORD')
        cookie_file = ytc_config.get('COOKIE_FILE')

        if not all([api_url, auth_username, auth_password, cookie_file]):
            app.logger.error("YTC配置不完整，请检查config.json")
            return False

        # 确保cookie文件所在目录存在
        cookie_dir = os.path.dirname(cookie_file)
        if cookie_dir:
            os.makedirs(cookie_dir, exist_ok=True)

        # 使用Basic认证获取cookie
        response = requests.get(
            api_url,
            auth=HTTPBasicAuth(auth_username, auth_password),
            timeout=10
        )
        
        if response.status_code == 200:
            # 写入cookie文件
            with open(cookie_file, 'w') as f:
                f.write(response.text)
            app.logger.info(f"成功更新YouTube cookie文件: {cookie_file}")
            return True
        else:
            app.logger.error(f"获取cookie失败，状态码: {response.status_code}")
            return False
            
    except Exception as e:
        app.logger.error(f"获取YouTube cookie时发生错误: {str(e)}")
        return False

@app.cli.command("get-cookie")
@with_appcontext
def get_cookie_command():
    """获取 YouTube cookie 的命令行工具"""
    try:
        if get_youtube_cookie():
            click.echo("成功获取并更新 YouTube cookie")
        else:
            click.echo("获取 YouTube cookie 失败，请检查日志获取详细信息", err=True)
    except Exception as e:
        click.echo(f"执行过程中发生错误: {str(e)}", err=True)

@app.route('/api/get-cookie', methods=['GET', 'POST'])
def api_get_cookie():
    current_time = get_current_time()
    time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
    success = get_youtube_cookie()
    if success:
        return app.response_class(
            response=json.dumps({"success": True, "msg": "成功获取并更新 YouTube cookie", "time": time_str}, ensure_ascii=False),
            status=200,
            mimetype='application/json'
        )
    else:
        return app.response_class(
            response=json.dumps({"success": False, "msg": "获取 YouTube cookie 失败，请检查日志获取详细信息", "time": time_str}, ensure_ascii=False),
            status=500,
            mimetype='application/json'
        )

if __name__ == "__main__":
    # 启动时获取cookie
    # 仅当配置了 YTC 时才在启动时获取 cookie
    if config.get("YTC"):
        get_youtube_cookie()
    app.run(
        host=config.get("FLASK_HOST", "0.0.0.0"),
        port=config.get("FLASK_PORT", 5100),
        debug=config.get("FLASK_DEBUG", True)
    )
