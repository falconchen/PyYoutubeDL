#!venv/bin/python
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, jsonify, abort, Response
import os
import time
import json
import re
import subprocess
from functools import lru_cache
from urllib.parse import unquote
import hashlib
from werkzeug.utils import safe_join
from config_util import load_config
import random
import string
import pytz
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth
from log_util import setup_logger
import click
from flask.cli import with_appcontext

app = Flask(__name__, static_url_path='/static', static_folder='static')

# 加载配置
config = load_config()

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

URLS_DIR = config["URLS_DIR"]
FILES_DIR = config["FILES_DIR"]
# 兼容历史任务中曾使用过的数字随机后缀，同时限制为安全文件名字符。
TASK_ID_PATTERN = re.compile(r'^[va][A-Za-z0-9_-]{1,127}$')
TASK_STATE_EXTENSIONS = (
    ('.ok', 'completed'),
    ('.fail', 'failed'),
    ('.downloading', 'downloading'),
    ('.txt', 'queued'),
)
PROGRESS_MARKER = 'PYDL_PROGRESS|'
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
DEFAULT_PROGRESS_PATTERN = re.compile(
    r'\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%'
    r'(?:\s+of(?:\s+~)?\s+(?P<total>.+?))?'
    r'(?:\s+at\s+(?P<speed>.+?))?'
    r'(?:\s+ETA\s+(?P<eta>\S+))?$'
)

# 保证文件夹存在
os.makedirs(URLS_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

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

def random_str(length=3):
    return ''.join(random.choices(string.ascii_letters, k=length))

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

def create_tasks(url, types):
    """创建下载任务并返回任务ID列表
    
    Args:
        url (str): 要下载的URL
        types (list): 下载类型列表，可以是 ['video'] 或 ['audio'] 或两者都有
        
    Returns:
        list: 创建的任务ID列表
    """
    task_ids = []
    current_time = get_current_time()
    for t in types:
        timestamp = current_time.strftime('%Y%m%d%H%M%S') + random_str(3)
        prefix = 'v' if t == 'video' else 'a'
        task_id = f"{prefix}{timestamp}"
        task_ids.append(task_id)
        filename = os.path.join(URLS_DIR, f"{task_id}.txt")
        with open(filename, 'w') as f:
            f.write(url)
    return task_ids


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
    for line in reversed(lines):
        marker_index = line.find(PROGRESS_MARKER)
        if marker_index >= 0:
            fields = line[marker_index + len(PROGRESS_MARKER):].split('|')
            if len(fields) < 6:
                continue
            status, percent_text, downloaded, total, speed, eta = fields[:6]
            percent_match = re.search(r'\d+(?:\.\d+)?', percent_text)
            progress = {
                "phase": status.strip(),
                "downloaded": downloaded.strip(),
                "total": total.strip(),
                "speed": speed.strip(),
                "eta": eta.strip(),
            }
            if percent_match:
                progress["percent"] = min(100.0, float(percent_match.group()))
            return progress

        match = DEFAULT_PROGRESS_PATTERN.search(line)
        if match:
            progress = {
                "phase": "downloading",
                "percent": min(100.0, float(match.group('percent'))),
            }
            for key in ('total', 'speed', 'eta'):
                value = match.group(key)
                if value:
                    progress[key] = value.strip()
            return progress
    return {}


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

    progress = parse_task_progress(
        os.path.join(config["LOG_DIR"], f"{task}.log")
    )
    if state == 'completed':
        progress["percent"] = 100.0
        progress["phase"] = "finished"
    elif state == 'queued':
        progress = {"percent": 0.0, "phase": "queued"}
    elif not progress:
        progress = {
            "percent": 0.0,
            "phase": "starting" if state == 'downloading' else state,
        }

    return {
        "task": task,
        "exists": True,
        "type": 'video' if task[0] == 'v' else 'audio',
        "timestamp": task[1:],
        "time": time_fmt,
        "url": url,
        "state": state,
        "progress": progress,
    }


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

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        types = request.form.getlist('type')

        # 从分享文本中提取URL
        url = extract_url(url)

        # 使用新的辅助函数创建任务
        task_ids = create_tasks(url, types)
        
        # 构建重定向URL，包含所有参数
        redirect_url = url_for('index', 
                             url=url,
                             types=','.join(types),
                             tasks=','.join(task_ids))
        
        return redirect(redirect_url)

    # GET请求处理
    url = request.args.get('url', '')
    types = request.args.get('types', '').split(',') if request.args.get('types') else []
    tasks = request.args.get('tasks', '').split(',') if request.args.get('tasks') else []
    
    return render_template('index.html', 
                         url=url,
                         types=types,
                         tasks=tasks,
                         show_waline=config.get("SHOW_WALINE_ON_INDEX", False))

@app.route('/player')
def player():
    exclude_keywords = config.get("PLAYER_FILENAME_EXCLUDE_KEYWORDS", [])
    if not isinstance(exclude_keywords, list):
        app.logger.warning("PLAYER_FILENAME_EXCLUDE_KEYWORDS 必须是字符串数组，已忽略无效配置")
        exclude_keywords = []
    exclude_keywords = [
        keyword for keyword in exclude_keywords
        if isinstance(keyword, str) and keyword
    ]

    # 获取 files 目录下的所有 mp4 文件
    video_files = [
        filename for filename in os.listdir(FILES_DIR)
        if filename.endswith('.mp4')
        and not any(keyword in filename for keyword in exclude_keywords)
    ]
    
    # 根据文件的最后修改时间进行降序排序（从晚到早）
    video_files.sort(key=lambda f: os.path.getmtime(os.path.join(FILES_DIR, f)), reverse=True)

    subtitle_tracks = {}
    for filename in video_files:
        tracks = get_embedded_subtitles(filename)
        for track in tracks:
            track["url"] = url_for(
                "serve_subtitle",
                filename=filename,
                stream_index=track["stream_index"],
            )
        subtitle_tracks[filename] = tracks

    return render_template(
        'player.html',
        video_files=video_files,
        subtitle_tracks=subtitle_tracks,
        show_waline=config.get("SHOW_WALINE_ON_PLAYER", False),
    )
    
@app.route('/files/<path:filename>')
def serve_file(filename):
    decoded_filename = unquote(filename)  
    return send_from_directory(FILES_DIR, decoded_filename)


@app.route('/subtitles/<path:filename>/<int:stream_index>.vtt')
def serve_subtitle(filename, stream_index):
    """将 MP4 内嵌字幕流转换为浏览器可读取的 WebVTT。"""
    decoded_filename = unquote(filename)
    filepath = safe_join(FILES_DIR, decoded_filename)
    if (
        not filepath
        or not filepath.lower().endswith('.mp4')
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

    # 使用新的辅助函数创建任务
    tasks = create_tasks(url, types)
    
    msg = "Task added successfully" if len(tasks) == 1 else "Tasks added successfully"
    return jsonify({"success": True, "msg": msg, "tasks": tasks})

@app.route('/api/task_info', methods=['POST'])
def api_task_info():
    data = request.get_json() if request.is_json else request.form
    tasks = data.get('tasks')
    if not tasks:
        return jsonify({"success": False, "msg": "Missing required parameter: tasks"}), 400
    if not isinstance(tasks, list):
        tasks = [tasks]
    result = [get_task_info(task) for task in tasks]
    return jsonify({"success": True, "tasks": result})

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
        # 检查是否存在.local.conf文件
        script_dir = os.path.dirname(os.path.abspath(__file__))
        default_conf_file = 'yt-dlp.conf'
        local_conf_file = default_conf_file.replace('.conf', '.local.conf')
        local_conf_path = os.path.join(script_dir, local_conf_file)
        default_conf_path = os.path.join(script_dir, default_conf_file)
        
        # 优先使用.local.conf文件，如果不存在则使用默认配置文件
        conf_path = local_conf_path if os.path.exists(local_conf_path) else default_conf_path
        cmd = [
            'yt-dlp',
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
        debug=config.get("FLASK_DEBUG", True)
    )
