#!/usr/bin/env python3
"""SQLite 驱动的 AI 总结异步 worker。"""

import hashlib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import requests
from werkzeug.utils import safe_join

import ai_summary_store as store
from config_util import load_config
from downloader import (
    SUBTITLE_LANGUAGE_PREFERENCES,
    select_subtitle_fallback,
)
from log_util import setup_logger


config = load_config()
logger = setup_logger(
    name='ai_summary_worker',
    log_dir=config['LOG_DIR'],
    log_file='ai-summary-worker.log',
    max_bytes=config['MAX_LOG_SIZE'],
    backup_count=config['BACKUP_COUNT'],
    timezone=config.get('TIMEZONE', 'UTC'),
)
WORKER_ID = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}'
SUBTITLE_TIMESTAMP_PATTERN = re.compile(
    r'^(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+'
)
SUBTITLE_MAX_CHARS = 120000
JOB_LEASE_SECONDS = 600
SUBTITLE_EXTENSIONS = {'.ass', '.srt', '.ssa', '.ttml', '.vtt'}


class JobFailure(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def video_config_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_path = os.path.join(script_dir, 'yt-dlp.local.conf')
    default_path = os.path.join(script_dir, 'yt-dlp.conf')
    return local_path if os.path.exists(local_path) else default_path


def run_yt_dlp_metadata(source_url, conf_path=None):
    conf_path = conf_path or video_config_path()
    command = [
        'yt-dlp',
        '--config-location', conf_path,
        '--simulate',
        '--skip-download',
        '--no-playlist',
        '--sleep-requests', '0',
        '--sleep-interval', '0',
        '--max-sleep-interval', '0',
        '--sleep-subtitles', '0',
        '--dump-single-json',
        source_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JobFailure('extractor_unavailable', '暂时无法读取页面信息', True) from exc
    if result.returncode != 0:
        raise JobFailure('unsupported_url', 'yt-dlp 无法解析当前页面')
    try:
        info = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise JobFailure('invalid_metadata', '页面返回了无法识别的媒体信息') from exc
    if not isinstance(info, dict) or info.get('_type') in {'playlist', 'multi_video'}:
        raise JobFailure('playlist_not_supported', '暂不支持对播放列表生成总结')
    if not info.get('id'):
        raise JobFailure('unsupported_url', '页面中没有可识别的单个媒体')
    return info


def _requested_languages(info):
    requested = info.get('requested_subtitles')
    if not isinstance(requested, dict):
        return []
    return [language for language, value in requested.items() if language and value]


def select_summary_subtitle(info):
    """优先沿用配置选择，否则使用下载器的准确性优先回退。"""
    requested = _requested_languages(info)
    if requested:
        lowered = {language.lower(): language for language in requested}
        language = next(
            (
                lowered[candidate.lower()]
                for candidate in SUBTITLE_LANGUAGE_PREFERENCES
                if candidate.lower() in lowered
            ),
            requested[0],
        )
        manual = info.get('subtitles') or {}
        kind = 'manual' if language in manual else 'automatic'
        value = info['requested_subtitles'].get(language) or {}
        return language, kind, value.get('name') or language

    fallback = select_subtitle_fallback(info)
    if not fallback:
        raise JobFailure('no_subtitles', '当前页面没有可用字幕')
    language, fallback_kind = fallback
    kind_map = {
        '人工字幕': 'manual',
        '自动原文字幕': 'automatic',
        '自动翻译字幕': 'translated',
        '自动字幕': 'automatic',
    }
    caption_map = (
        info.get('subtitles') if fallback_kind == '人工字幕'
        else info.get('automatic_captions')
    ) or {}
    formats = caption_map.get(language) or []
    name = next(
        (
            item.get('name') for item in formats
            if isinstance(item, dict) and item.get('name')
        ),
        language,
    )
    return language, kind_map.get(fallback_kind, 'automatic'), name


def download_subtitles(source_url, language, destination_dir, conf_path=None):
    conf_path = conf_path or video_config_path()
    output_template = os.path.join(destination_dir, '%(id)s.%(ext)s')
    command = [
        'yt-dlp',
        '--config-location', conf_path,
        '--skip-download',
        '--no-playlist',
        '--no-embed-subs',
        '--write-subs',
        '--write-auto-subs',
        '--sub-langs', language,
        '--sub-format', 'vtt/srt/best',
        '--convert-subs', 'srt',
        '--sleep-requests', '0',
        '--sleep-interval', '0',
        '--max-sleep-interval', '0',
        '--sleep-subtitles', '0',
        '-o', output_template,
        source_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JobFailure('subtitle_download_failed', '字幕下载暂时失败', True) from exc
    if result.returncode != 0:
        raise JobFailure('subtitle_download_failed', '字幕下载失败', True)
    candidates = [
        os.path.join(destination_dir, filename)
        for filename in os.listdir(destination_dir)
        if os.path.splitext(filename)[1].lower() in SUBTITLE_EXTENSIONS
    ]
    exact_marker = f'.{language}.'
    selected = next(
        (path for path in candidates if exact_marker in os.path.basename(path)),
        None,
    )
    if not selected and candidates:
        selected = sorted(candidates)[0]
    if not selected:
        raise JobFailure('subtitle_download_failed', 'yt-dlp 未生成字幕文件')
    return selected


def subtitle_file_to_text(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as subtitle_file:
            raw_text = subtitle_file.read()
    except OSError as exc:
        raise JobFailure('subtitle_read_failed', '无法读取下载的字幕') from exc
    text_lines = []
    previous = None
    in_style_block = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        upper = line.upper()
        if upper in {'STYLE', 'REGION'}:
            in_style_block = True
            continue
        if in_style_block:
            if not line:
                in_style_block = False
            continue
        if (
            not line
            or upper == 'WEBVTT'
            or line.isdigit()
            or SUBTITLE_TIMESTAMP_PATTERN.match(line)
            or line.startswith(('NOTE ', 'Kind:', 'Language:'))
        ):
            continue
        line = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
        line = re.sub(r'^\{\\[^}]+\}', '', line).strip()
        if line and line != previous:
            text_lines.append(line)
            previous = line
    text = '\n'.join(text_lines)
    if len(text) > SUBTITLE_MAX_CHARS:
        text = text[:SUBTITLE_MAX_CHARS] + '\n（字幕内容过长，已截断）'
    if not text:
        raise JobFailure('empty_subtitles', '字幕中没有可总结的文本')
    return text


def media_from_info(info, request_url):
    extractor = str(info.get('extractor_key') or info.get('extractor') or 'unknown').lower()
    extractor_id = str(info['id'])
    canonical_url = str(info.get('webpage_url') or info.get('original_url') or request_url)
    title = str(info.get('title') or '')
    return extractor, extractor_id, canonical_url, title


def resolve_local_media(job, filepath):
    import app as app_module

    source_url = app_module.get_media_source_url(job['filename'])
    if source_url:
        try:
            info = run_yt_dlp_metadata(source_url)
            extractor, extractor_id, canonical_url, title = media_from_info(info, source_url)
            media_id = store.upsert_media(
                config['AI_SUMMARY_DB_PATH'],
                extractor,
                extractor_id,
                canonical_url,
                title,
                aliases=(source_url, canonical_url),
            )
            return media_id, title or job['filename']
        except JobFailure as exc:
            if exc.retryable:
                raise
            logger.warning('本地视频来源解析失败，使用文件指纹: %s', exc.message)
    stat = os.stat(filepath)
    key = store.local_source_key(filepath, stat)
    media_id = store.upsert_media(
        config['AI_SUMMARY_DB_PATH'],
        'local',
        key.split(':', 1)[1],
        f'local://{key.split(":", 1)[1]}',
        job['filename'],
    )
    return media_id, job['filename']


def process_url_job(job, temp_dir):
    info = run_yt_dlp_metadata(job['request_url'])
    extractor, extractor_id, canonical_url, title = media_from_info(
        info,
        job['request_url'],
    )
    media_id = store.upsert_media(
        config['AI_SUMMARY_DB_PATH'],
        extractor,
        extractor_id,
        canonical_url,
        title,
        aliases=(job['request_url'], canonical_url),
    )
    cached = store.find_summary_for_media(
        config['AI_SUMMARY_DB_PATH'],
        media_id,
        job['profile_key'],
    )
    if cached:
        store.complete_job_from_cache(
            config['AI_SUMMARY_DB_PATH'],
            job['id'],
            media_id,
            cached['id'],
        )
        return

    language, subtitle_kind, subtitle_label = select_summary_subtitle(info)
    store.update_job(
        config['AI_SUMMARY_DB_PATH'],
        job['id'],
        'downloading_subtitle',
        media_source_id=media_id,
        lease_until=store.now_ts() + JOB_LEASE_SECONDS,
    )
    subtitle_path = download_subtitles(
        job['request_url'],
        language,
        temp_dir,
    )
    subtitle_text = subtitle_file_to_text(subtitle_path)
    return media_id, title, language, subtitle_kind, subtitle_label, subtitle_text


def process_local_job(job):
    import app as app_module

    filepath = safe_join(config['FILES_DIR'], job['filename'])
    if not filepath or not os.path.isfile(filepath):
        raise JobFailure('video_not_found', '视频文件不存在')
    tracks = app_module.get_embedded_subtitles(job['filename'])
    selected = next(
        (
            track for track in tracks
            if job['stream_index'] is None or track['stream_index'] == job['stream_index']
        ),
        None,
    )
    if not selected:
        raise JobFailure('no_subtitles', '当前视频没有可用字幕')
    media_id, title = resolve_local_media(job, filepath)
    cached = store.find_summary_for_media(
        config['AI_SUMMARY_DB_PATH'],
        media_id,
        job['profile_key'],
    )
    if cached:
        store.complete_job_from_cache(
            config['AI_SUMMARY_DB_PATH'],
            job['id'],
            media_id,
            cached['id'],
        )
        return
    subtitle_text = app_module.extract_subtitle_text(
        filepath,
        selected['stream_index'],
    )
    if not subtitle_text:
        raise JobFailure('empty_subtitles', '字幕中没有可总结的文本')
    return (
        media_id,
        title,
        selected.get('language') or '',
        'embedded',
        selected.get('label') or selected.get('language') or '字幕',
        subtitle_text,
    )


def generate_and_save(job, prepared):
    import app as app_module

    media_id, title, language, kind, label, subtitle_text = prepared
    store.update_job(
        config['AI_SUMMARY_DB_PATH'],
        job['id'],
        'generating',
        media_source_id=media_id,
        lease_until=store.now_ts() + JOB_LEASE_SECONDS,
    )
    try:
        summary = app_module.request_ai_summary(title, label, subtitle_text)
    except RuntimeError as exc:
        raise JobFailure('ai_invalid_response', str(exc)) from exc
    store.save_summary_and_complete(
        config['AI_SUMMARY_DB_PATH'],
        job['id'],
        media_id,
        job['profile_key'],
        str(config.get('AI_API_MODEL') or '').strip(),
        language,
        kind,
        hashlib.sha256(subtitle_text.encode('utf-8')).hexdigest(),
        summary,
    )


def process_job(job):
    if job['profile_key'] != store.summary_profile_key(config):
        raise JobFailure('profile_changed', 'AI 配置已经变更，请重新提交任务')
    temp_root = os.path.join(config['TMP_DIR'], 'ai-summary')
    os.makedirs(temp_root, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=f"{job['id']}-", dir=temp_root)
    try:
        if job['input_kind'] == 'url':
            prepared = process_url_job(job, temp_dir)
        else:
            prepared = process_local_job(job)
        if prepared:
            generate_and_save(job, prepared)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_once():
    job = store.claim_next_job(
        config['AI_SUMMARY_DB_PATH'],
        WORKER_ID,
        lease_seconds=JOB_LEASE_SECONDS,
    )
    if not job:
        return False
    try:
        process_job(job)
    except JobFailure as exc:
        retry = store.fail_or_retry_job(
            config['AI_SUMMARY_DB_PATH'],
            job['id'],
            job['attempts'],
            exc.code,
            exc.message,
            exc.retryable,
        )
        logger.warning(
            'AI 总结任务%s: %s (%s)',
            '将在稍后重试' if retry else '失败',
            job['id'],
            exc.code,
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        retryable = status_code == 429 or (status_code is not None and status_code >= 500)
        retry = store.fail_or_retry_job(
            config['AI_SUMMARY_DB_PATH'],
            job['id'],
            job['attempts'],
            'ai_request_failed' if retryable else 'ai_request_rejected',
            'AI 接口请求失败' if retryable else 'AI 接口拒绝了总结请求',
            retryable,
        )
        logger.warning(
            'AI 总结接口 HTTP 失败，任务%s%s (status=%s)',
            job['id'],
            '等待重试' if retry else '已失败',
            status_code,
        )
    except requests.RequestException as exc:
        retry = store.fail_or_retry_job(
            config['AI_SUMMARY_DB_PATH'],
            job['id'],
            job['attempts'],
            'ai_request_failed',
            'AI 接口请求失败',
            True,
        )
        logger.warning(
            'AI 总结接口失败，任务%s%s (%s)',
            job['id'],
            '等待重试' if retry else '已失败',
            type(exc).__name__,
        )
    except Exception as exc:
        store.fail_or_retry_job(
            config['AI_SUMMARY_DB_PATH'],
            job['id'],
            job['attempts'],
            'internal_error',
            '生成总结时发生内部错误',
            False,
        )
        logger.exception('AI 总结任务发生内部错误: %s', job['id'])
    return True


def main():
    store.init_db(config['AI_SUMMARY_DB_PATH'])
    logger.info('AI 总结 worker 已启动: %s', WORKER_ID)
    last_cleanup = 0
    while True:
        timestamp = store.now_ts()
        if timestamp - last_cleanup >= 86400:
            removed = store.cleanup_jobs(
                config['AI_SUMMARY_DB_PATH'],
                config.get('AI_SUMMARY_JOB_RETENTION_DAYS', 30),
            )
            if removed:
                logger.info('已清理 %s 条过期 AI 总结任务', removed)
            last_cleanup = timestamp
        if not run_once():
            time.sleep(1)


if __name__ == '__main__':
    main()
