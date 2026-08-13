#!venv/bin/python
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, jsonify, abort, Response
import os
import glob
import html
import time
import json
import re
import subprocess
from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlparse
import hashlib
import hmac
from werkzeug.utils import safe_join
from config_util import load_config
import random
import string
import pytz
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth
from log_util import setup_logger
import ai_summary_store
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

    subtitle = result.stdout.decode("utf-8", errors="replace")
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


def request_ai_summary(filename, subtitle_label, subtitle_text):
    """调用 chat/completions 兼容接口并返回总结文本。"""
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
        timeout=120,
    )
    response.raise_for_status()
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI 接口返回了无法识别的数据") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("AI 接口未返回总结内容")
    return content.strip()


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
    }, 202


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


@lru_cache(maxsize=256)
def _probe_audio_metadata(filepath, file_mtime_ns, file_size):
    """读取音频 metadata；文件属性参数用于在文件变化时自动失效缓存。"""
    del file_mtime_ns, file_size
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format_tags=title,artist,purl,comment',
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
        app.logger.warning("读取音频 metadata 失败，使用文件名和默认封面: %s (%s)", filepath, exc)
        return '', '', None, ''

    if not isinstance(raw_tags, dict):
        raw_tags = {}

    tags = {
        str(key).lower(): value
        for key, value in raw_tags.items()
        if isinstance(value, str)
    }
    source_url = extract_media_source_url(tags)
    video_id = None
    for tag_name in ('purl', 'comment'):
        video_id = extract_youtube_video_id_from_text(tags.get(tag_name, ''))
        if video_id:
            break
    return tags.get('title', ''), tags.get('artist', ''), video_id, source_url


@lru_cache(maxsize=256)
def _probe_media_source_url(filepath, file_mtime_ns, file_size):
    """读取视频等媒体文件的来源 URL；文件属性用于缓存自动失效。"""
    del file_mtime_ns, file_size
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format_tags=purl,comment',
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
        app.logger.warning("读取媒体来源 metadata 失败，已忽略来源链接: %s (%s)", filepath, exc)
        return ''
    return extract_media_source_url(raw_tags)


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
    except OSError as exc:
        app.logger.warning("读取音频文件属性失败，使用默认信息: %s (%s)", filepath, exc)
        title, artist, video_id, source_url = '', '', None, ''

    return {
        'title': title or fallback_title,
        'artist': artist,
        'source_url': source_url,
        'mime_type': AUDIO_MIME_TYPES.get(extension, 'audio/mpeg'),
        'cover_candidates': build_audio_cover_candidates(
            video_id,
            fallback_cover_url,
        ),
    }


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


def get_player_exclude_keywords():
    exclude_keywords = config.get("PLAYER_FILENAME_EXCLUDE_KEYWORDS", [])
    if not isinstance(exclude_keywords, list):
        app.logger.warning("PLAYER_FILENAME_EXCLUDE_KEYWORDS 必须是字符串数组，已忽略无效配置")
        return []
    return [
        keyword for keyword in exclude_keywords
        if isinstance(keyword, str) and keyword
    ]

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


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/player')
def player():
    exclude_keywords = get_player_exclude_keywords()

    # 获取 files 目录下的所有 mp4 文件
    video_files = [
        filename for filename in os.listdir(FILES_DIR)
        if filename.endswith('.mp4')
        and not any(keyword in filename for keyword in exclude_keywords)
    ]
    
    # 根据文件的最后修改时间进行降序排序（从晚到早）
    video_files.sort(key=lambda f: os.path.getmtime(os.path.join(FILES_DIR, f)), reverse=True)

    requested_file = request.args.get('file', '')
    if requested_file in video_files:
        video_files.remove(requested_file)
        video_files.insert(0, requested_file)

    subtitle_tracks = {}
    video_source_urls = {}
    for filename in video_files:
        video_source_urls[filename] = get_media_source_url(filename)
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
        video_source_urls=video_source_urls,
        subtitle_tracks=subtitle_tracks,
        browser_subtitle_languages=[
            language
            for language, quality in request.accept_languages
            if quality > 0
        ],
        ai_summary_configured=ai_summary_is_configured(),
        show_waline=config.get("SHOW_WALINE_ON_PLAYER", False),
    )


@app.route('/audio-player')
def audio_player():
    exclude_keywords = get_player_exclude_keywords()
    audio_files = [
        filename for filename in os.listdir(FILES_DIR)
        if os.path.splitext(filename)[1].lower().lstrip('.') in AUDIO_EXTENSIONS
        and not any(keyword in filename for keyword in exclude_keywords)
    ]
    audio_files.sort(
        key=lambda filename: os.path.getmtime(os.path.join(FILES_DIR, filename)),
        reverse=True,
    )

    requested_file = request.args.get('file', '')
    if requested_file in audio_files:
        audio_files.remove(requested_file)
        audio_files.insert(0, requested_file)

    fallback_cover_url = config.get(
        'AUDIO_PLAYER_FALLBACK_COVER_URL',
        url_for('static', filename='images/audio-cover-default.svg'),
    )
    if not isinstance(fallback_cover_url, str) or not fallback_cover_url.strip():
        fallback_cover_url = url_for(
            'static',
            filename='images/audio-cover-default.svg',
        )

    audio_items = []
    preferred_languages = [
        language
        for language, quality in request.accept_languages
        if quality > 0
    ]
    for filename in audio_files:
        metadata = get_audio_metadata(filename, fallback_cover_url)
        metadata.update({
            'filename': filename,
            'url': url_for('serve_file', filename=filename),
            'lyrics': find_audio_lyrics(filename, preferred_languages),
        })
        audio_items.append(metadata)

    return render_template(
        'audio_player.html',
        audio_items=audio_items,
        fallback_cover_url=fallback_cover_url,
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
    if not isinstance(filename, str) or not filename:
        return ai_summary_api_response({"success": False, "message": "缺少视频文件名"}, 400)
    if stream_index is not None and (
        isinstance(stream_index, bool) or not isinstance(stream_index, int)
    ):
        return ai_summary_api_response({"success": False, "message": "字幕流编号无效"}, 400)

    filepath = safe_join(FILES_DIR, filename)
    if (
        not filepath
        or not filepath.lower().endswith('.mp4')
        or not os.path.isfile(filepath)
    ):
        return ai_summary_api_response({"success": False, "message": "视频文件不存在"}, 404)

    tracks = get_embedded_subtitles(filename)
    if not tracks:
        return ai_summary_api_response({"success": False, "message": "当前视频没有可用字幕"}, 400)
    if stream_index is None:
        selected_track = tracks[0]
    else:
        selected_track = next(
            (track for track in tracks if track["stream_index"] == stream_index),
            None,
        )
        if selected_track is None:
            return ai_summary_api_response({"success": False, "message": "所选字幕流不存在"}, 400)

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
