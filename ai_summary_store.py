#!/usr/bin/env python3
"""AI 总结 SQLite 存储、任务队列和 URL 规范化。"""

import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


SCHEMA_VERSION = 1
PROMPT_VERSION = 1
YOUTUBE_HOSTS = {
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'music.youtube.com',
    'youtu.be',
}


def now_ts():
    return int(time.time())


def summary_profile_key(runtime_config):
    payload = {
        'api_base_url': str(runtime_config.get('AI_API_BASE_URL') or '').strip(),
        'model': str(runtime_config.get('AI_API_MODEL') or '').strip(),
        'prompt_version': PROMPT_VERSION,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def extract_youtube_id(source_url):
    try:
        parsed = urlparse(source_url)
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or '').lower()
    candidate = None
    if host == 'youtu.be':
        candidate = parsed.path.strip('/').split('/', 1)[0]
    elif host in YOUTUBE_HOSTS:
        if parsed.path == '/watch':
            candidate = parse_qs(parsed.query).get('v', [None])[0]
        else:
            parts = parsed.path.strip('/').split('/')
            if len(parts) >= 2 and parts[0] in {'shorts', 'embed', 'live'}:
                candidate = parts[1]
    if (
        isinstance(candidate, str)
        and len(candidate) == 11
        and all(char.isalnum() or char in '_-' for char in candidate)
    ):
        return candidate
    return None


def normalize_source_url(source_url):
    """规范化 URL；YouTube 的多种链接统一为 watch URL。"""
    if not isinstance(source_url, str):
        raise ValueError('URL 必须是字符串')
    source_url = source_url.strip()
    if not source_url or len(source_url) > 2048:
        raise ValueError('URL 为空或过长')
    try:
        parsed = urlparse(source_url)
    except ValueError as exc:
        raise ValueError('URL 格式无效') from exc
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('URL 必须使用 http 或 https')
    if parsed.username or parsed.password:
        raise ValueError('URL 不允许包含用户名或密码')

    youtube_id = extract_youtube_id(source_url)
    if youtube_id:
        return f'https://www.youtube.com/watch?v={youtube_id}'

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip('.')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('URL 端口无效') from exc
    netloc = host
    if port and not ((scheme == 'http' and port == 80) or (scheme == 'https' and port == 443)):
        netloc = f'{host}:{port}'
    query_items = parse_qs(parsed.query, keep_blank_values=True)
    query = urlencode(
        sorted((key, value) for key, values in query_items.items() for value in values),
        doseq=True,
    )
    path = parsed.path or '/'
    return urlunparse((scheme, netloc, path, '', query, ''))


def validate_public_url(source_url):
    """拒绝明显的本机、私网、链路本地及保留地址。"""
    normalized = normalize_source_url(source_url)
    host = urlparse(normalized).hostname
    if not host:
        raise ValueError('URL 缺少主机名')
    if host.lower() == 'localhost':
        raise ValueError('不允许访问本机或私网地址')
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise ValueError('URL 主机名无法解析') from exc
    if not addresses:
        raise ValueError('URL 主机名无法解析')
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError('不允许访问本机、私网或保留地址')
    return normalized


def local_source_key(filepath, stat_result=None):
    if stat_result is None:
        stat_result = os.stat(filepath)
    payload = (
        f'{os.path.realpath(filepath)}\0{stat_result.st_mtime_ns}\0'
        f'{stat_result.st_size}'
    )
    return f'local:{hashlib.sha256(payload.encode()).hexdigest()}'


def source_key(extractor, extractor_id):
    return f'{str(extractor).strip().lower()}:{str(extractor_id).strip()}'


@contextmanager
def connect(db_path):
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA busy_timeout = 10000')
    try:
        yield connection
    finally:
        connection.close()


def init_db(db_path):
    with connect(db_path) as db:
        db.execute('PRAGMA journal_mode = WAL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f'AI 总结数据库版本过新: {version}')
        if version == 0:
            db.executescript(
                """
                CREATE TABLE media_sources (
                    id INTEGER PRIMARY KEY,
                    source_key TEXT NOT NULL UNIQUE,
                    extractor TEXT NOT NULL,
                    extractor_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(extractor, extractor_id)
                );
                CREATE TABLE media_url_aliases (
                    normalized_url TEXT PRIMARY KEY,
                    media_source_id INTEGER NOT NULL REFERENCES media_sources(id) ON DELETE CASCADE,
                    original_url TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );
                CREATE TABLE ai_summaries (
                    id TEXT PRIMARY KEY,
                    media_source_id INTEGER NOT NULL REFERENCES media_sources(id) ON DELETE CASCADE,
                    profile_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version INTEGER NOT NULL,
                    subtitle_language TEXT NOT NULL DEFAULT '',
                    subtitle_kind TEXT NOT NULL DEFAULT '',
                    subtitle_hash TEXT NOT NULL,
                    summary_markdown TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(media_source_id, profile_key)
                );
                CREATE TABLE ai_summary_jobs (
                    id TEXT PRIMARY KEY,
                    input_kind TEXT NOT NULL CHECK(input_kind IN ('url', 'local_file')),
                    request_url TEXT NOT NULL DEFAULT '',
                    normalized_url TEXT NOT NULL,
                    filename TEXT NOT NULL DEFAULT '',
                    stream_index INTEGER,
                    media_source_id INTEGER REFERENCES media_sources(id) ON DELETE SET NULL,
                    profile_key TEXT NOT NULL,
                    summary_id TEXT REFERENCES ai_summaries(id) ON DELETE SET NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'resolving', 'downloading_subtitle', 'generating', 'completed', 'failed')),
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_until INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    error_retryable INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER
                );
                CREATE INDEX ai_summary_jobs_queue_idx
                    ON ai_summary_jobs(status, next_attempt_at, created_at);
                CREATE UNIQUE INDEX ai_summary_jobs_active_url_idx
                    ON ai_summary_jobs(normalized_url, profile_key)
                    WHERE status IN ('queued', 'resolving', 'downloading_subtitle', 'generating');
                """
            )
            db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        db.commit()


def _upsert_media(db, extractor, extractor_id, canonical_url, title=''):
    timestamp = now_ts()
    key = source_key(extractor, extractor_id)
    db.execute(
        """
        INSERT INTO media_sources (
            source_key, extractor, extractor_id, canonical_url, title,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            canonical_url = excluded.canonical_url,
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE media_sources.title END,
            updated_at = excluded.updated_at
        """,
        (key, str(extractor).lower(), str(extractor_id), canonical_url, title or '', timestamp, timestamp),
    )
    return db.execute(
        'SELECT id FROM media_sources WHERE source_key = ?',
        (key,),
    ).fetchone()['id']


def upsert_media(db_path, extractor, extractor_id, canonical_url, title='', aliases=()):
    with connect(db_path) as db:
        media_id = _upsert_media(db, extractor, extractor_id, canonical_url, title)
        for original_url in aliases:
            _upsert_alias(db, original_url, media_id)
        db.commit()
        return media_id


def _upsert_alias(db, original_url, media_source_id):
    normalized = normalize_source_url(original_url)
    timestamp = now_ts()
    db.execute(
        """
        INSERT INTO media_url_aliases (
            normalized_url, media_source_id, original_url, created_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(normalized_url) DO UPDATE SET
            media_source_id = excluded.media_source_id,
            last_seen_at = excluded.last_seen_at
        """,
        (normalized, media_source_id, original_url, timestamp, timestamp),
    )
    return normalized


def _summary_query():
    return """
        SELECT s.*, m.canonical_url, m.title, m.source_key
        FROM ai_summaries s
        JOIN media_sources m ON m.id = s.media_source_id
    """


def summary_payload(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'source_url': row['canonical_url'],
        'source_key': row['source_key'],
        'title': row['title'],
        'markdown': row['summary_markdown'],
        'subtitle_language': row['subtitle_language'],
        'subtitle_kind': row['subtitle_kind'],
        'model': row['model'],
        'prompt_version': row['prompt_version'],
        'created_at': row['created_at'],
    }


def find_summary_for_url(db_path, normalized_url, profile_key):
    with connect(db_path) as db:
        row = db.execute(
            _summary_query() + """
                JOIN media_url_aliases a ON a.media_source_id = m.id
                WHERE a.normalized_url = ? AND s.profile_key = ?
            """,
            (normalized_url, profile_key),
        ).fetchone()
        if row:
            db.execute(
                'UPDATE media_url_aliases SET last_seen_at = ? WHERE normalized_url = ?',
                (now_ts(), normalized_url),
            )
            db.commit()
        return summary_payload(row)


def find_summary_for_media(db_path, media_source_id, profile_key):
    with connect(db_path) as db:
        row = db.execute(
            _summary_query() + ' WHERE s.media_source_id = ? AND s.profile_key = ?',
            (media_source_id, profile_key),
        ).fetchone()
        return summary_payload(row)


def create_url_job(db_path, request_url, normalized_url, profile_key):
    timestamp = now_ts()
    with connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        summary = db.execute(
            _summary_query() + """
                JOIN media_url_aliases a ON a.media_source_id = m.id
                WHERE a.normalized_url = ? AND s.profile_key = ?
            """,
            (normalized_url, profile_key),
        ).fetchone()
        if summary:
            db.commit()
            return {'summary': summary_payload(summary), 'job': None}
        active = db.execute(
            """
            SELECT * FROM ai_summary_jobs
            WHERE normalized_url = ? AND profile_key = ?
              AND status IN ('queued', 'resolving', 'downloading_subtitle', 'generating')
            ORDER BY created_at LIMIT 1
            """,
            (normalized_url, profile_key),
        ).fetchone()
        if active:
            db.commit()
            return {'summary': None, 'job': dict(active)}
        job_id = str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO ai_summary_jobs (
                id, input_kind, request_url, normalized_url, profile_key,
                status, next_attempt_at, created_at, updated_at
            ) VALUES (?, 'url', ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, request_url, normalized_url, profile_key, timestamp, timestamp, timestamp),
        )
        job = db.execute('SELECT * FROM ai_summary_jobs WHERE id = ?', (job_id,)).fetchone()
        db.commit()
        return {'summary': None, 'job': dict(job)}


def create_local_job(
    db_path,
    filename,
    stream_index,
    normalized_key,
    profile_key,
    media_source_id=None,
):
    timestamp = now_ts()
    with connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        if media_source_id:
            summary = db.execute(
                _summary_query() + ' WHERE s.media_source_id = ? AND s.profile_key = ?',
                (media_source_id, profile_key),
            ).fetchone()
            if summary:
                db.commit()
                return {'summary': summary_payload(summary), 'job': None}
        active = db.execute(
            """
            SELECT * FROM ai_summary_jobs
            WHERE normalized_url = ? AND profile_key = ?
              AND status IN ('queued', 'resolving', 'downloading_subtitle', 'generating')
            ORDER BY created_at LIMIT 1
            """,
            (normalized_key, profile_key),
        ).fetchone()
        if active:
            db.commit()
            return {'summary': None, 'job': dict(active)}
        job_id = str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO ai_summary_jobs (
                id, input_kind, normalized_url, filename, stream_index,
                media_source_id, profile_key, status, next_attempt_at,
                created_at, updated_at
            ) VALUES (?, 'local_file', ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                job_id, normalized_key, filename, stream_index,
                media_source_id, profile_key, timestamp, timestamp, timestamp,
            ),
        )
        job = db.execute('SELECT * FROM ai_summary_jobs WHERE id = ?', (job_id,)).fetchone()
        db.commit()
        return {'summary': None, 'job': dict(job)}


def get_job(db_path, job_id):
    with connect(db_path) as db:
        row = db.execute('SELECT * FROM ai_summary_jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        if job['summary_id']:
            summary = db.execute(
                _summary_query() + ' WHERE s.id = ?',
                (job['summary_id'],),
            ).fetchone()
            job['summary'] = summary_payload(summary)
        return job


def claim_next_job(db_path, worker_id, lease_seconds=180):
    timestamp = now_ts()
    with connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(
            """
            UPDATE ai_summary_jobs
            SET status = 'queued', lease_owner = NULL, lease_until = NULL,
                updated_at = ?, next_attempt_at = ?
            WHERE status IN ('resolving', 'downloading_subtitle', 'generating')
              AND lease_until IS NOT NULL AND lease_until < ?
            """,
            (timestamp, timestamp, timestamp),
        )
        row = db.execute(
            """
            SELECT * FROM ai_summary_jobs
            WHERE status = 'queued' AND next_attempt_at <= ?
            ORDER BY created_at LIMIT 1
            """,
            (timestamp,),
        ).fetchone()
        if not row:
            db.commit()
            return None
        db.execute(
            """
            UPDATE ai_summary_jobs
            SET status = 'resolving', attempts = attempts + 1,
                lease_owner = ?, lease_until = ?, updated_at = ?,
                started_at = COALESCE(started_at, ?), error_code = NULL,
                error_message = NULL, error_retryable = 0
            WHERE id = ?
            """,
            (worker_id, timestamp + lease_seconds, timestamp, timestamp, row['id']),
        )
        claimed = db.execute('SELECT * FROM ai_summary_jobs WHERE id = ?', (row['id'],)).fetchone()
        db.commit()
        return dict(claimed)


def update_job(db_path, job_id, status, **fields):
    allowed = {
        'media_source_id', 'summary_id', 'lease_until', 'error_code',
        'error_message', 'error_retryable', 'next_attempt_at', 'cache_hit',
    }
    updates = {'status': status, 'updated_at': now_ts()}
    updates.update({key: value for key, value in fields.items() if key in allowed})
    assignments = ', '.join(f'{key} = ?' for key in updates)
    values = list(updates.values()) + [job_id]
    with connect(db_path) as db:
        db.execute(f'UPDATE ai_summary_jobs SET {assignments} WHERE id = ?', values)
        db.commit()


def save_summary_and_complete(
    db_path,
    job_id,
    media_source_id,
    profile_key,
    model,
    subtitle_language,
    subtitle_kind,
    subtitle_hash,
    summary_markdown,
):
    timestamp = now_ts()
    with connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute(
            'SELECT id FROM ai_summaries WHERE media_source_id = ? AND profile_key = ?',
            (media_source_id, profile_key),
        ).fetchone()
        cache_hit = existing is not None
        if existing:
            summary_id = existing['id']
        else:
            summary_id = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO ai_summaries (
                    id, media_source_id, profile_key, model, prompt_version,
                    subtitle_language, subtitle_kind, subtitle_hash,
                    summary_markdown, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id, media_source_id, profile_key, model,
                    PROMPT_VERSION, subtitle_language or '', subtitle_kind or '',
                    subtitle_hash, summary_markdown, timestamp, timestamp,
                ),
            )
        db.execute(
            """
            UPDATE ai_summary_jobs
            SET status = 'completed', media_source_id = ?, summary_id = ?,
                cache_hit = ?, lease_owner = NULL, lease_until = NULL,
                updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (media_source_id, summary_id, int(cache_hit), timestamp, timestamp, job_id),
        )
        row = db.execute(
            _summary_query() + ' WHERE s.id = ?',
            (summary_id,),
        ).fetchone()
        db.commit()
        return summary_payload(row), cache_hit


def complete_job_from_cache(db_path, job_id, media_source_id, summary_id):
    timestamp = now_ts()
    with connect(db_path) as db:
        db.execute(
            """
            UPDATE ai_summary_jobs
            SET status = 'completed', media_source_id = ?, summary_id = ?,
                cache_hit = 1, lease_owner = NULL, lease_until = NULL,
                updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (media_source_id, summary_id, timestamp, timestamp, job_id),
        )
        db.commit()


def fail_or_retry_job(
    db_path,
    job_id,
    attempts,
    error_code,
    error_message,
    retryable,
    max_attempts=3,
):
    timestamp = now_ts()
    retry = retryable and attempts < max_attempts
    if retry:
        delay = 5 * (4 ** max(0, attempts - 1))
        status = 'queued'
        completed_at = None
        next_attempt_at = timestamp + delay
    else:
        status = 'failed'
        completed_at = timestamp
        next_attempt_at = timestamp
    with connect(db_path) as db:
        db.execute(
            """
            UPDATE ai_summary_jobs
            SET status = ?, error_code = ?, error_message = ?,
                error_retryable = ?, next_attempt_at = ?, lease_owner = NULL,
                lease_until = NULL, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                status, error_code, error_message, int(retryable),
                next_attempt_at, timestamp, completed_at, job_id,
            ),
        )
        db.commit()
    return retry


def cleanup_jobs(db_path, retention_days):
    cutoff = now_ts() - max(1, int(retention_days)) * 86400
    with connect(db_path) as db:
        cursor = db.execute(
            """
            DELETE FROM ai_summary_jobs
            WHERE status IN ('completed', 'failed')
              AND completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoff,),
        )
        db.commit()
        return cursor.rowcount
