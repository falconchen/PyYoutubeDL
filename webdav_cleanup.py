"""WebDAV 按天清理及直接上传文件的持久化记录。"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytz
from webdav3.client import Urn, WebDavXmlUtils
from webdav3.exceptions import RemoteResourceNotFound


logger = logging.getLogger(__name__)


def webdav_target_id(options):
    """隔离服务器、根目录和账户；数据库不保存连接凭据。"""
    identity = [
        options['webdav_hostname'].rstrip('/'),
        '/' + options.get('webdav_root', '').strip('/'),
        options.get('webdav_login', ''),
    ]
    return hashlib.sha256(json.dumps(identity).encode('utf-8')).hexdigest()


def is_root_file_path(path):
    """记录只允许配置根目录的直接子文件，避免递归删除目录。"""
    return (
        isinstance(path, str) and path.startswith('/')
        and path[1:] not in ('', '.', '..') and '/' not in path[1:]
    )


class WebDAVUploadHistory:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            with connection:
                connection.execute('''
                    CREATE TABLE IF NOT EXISTS webdav_uploads (
                        target_id TEXT NOT NULL,
                        category TEXT NOT NULL,
                        remote_path TEXT NOT NULL,
                        uploaded_at REAL NOT NULL,
                        PRIMARY KEY (target_id, category, remote_path)
                    )
                ''')
                yield connection
        finally:
            connection.close()

    def record(self, target_id, category, remote_path, uploaded_at=None):
        if not is_root_file_path(remote_path):
            raise ValueError('只能记录根目录的直接子文件')
        timestamp = time.time() if uploaded_at is None else uploaded_at
        with self.connect() as connection:
            connection.execute('''
                INSERT INTO webdav_uploads VALUES (?, ?, ?, ?)
                ON CONFLICT (target_id, category, remote_path)
                DO UPDATE SET uploaded_at = excluded.uploaded_at
            ''', (target_id, category, remote_path, timestamp))

    def expired(self, target_id, category, cutoff):
        with self.connect() as connection:
            return connection.execute('''
                SELECT remote_path, uploaded_at FROM webdav_uploads
                WHERE target_id = ? AND category = ? AND uploaded_at <= ?
                ORDER BY uploaded_at, remote_path
            ''', (target_id, category, cutoff)).fetchall()

    def forget(self, target_id, category, remote_path, uploaded_at):
        with self.connect() as connection:
            connection.execute('''
                DELETE FROM webdav_uploads
                WHERE target_id = ? AND category = ? AND remote_path = ? AND uploaded_at = ?
            ''', (target_id, category, remote_path, uploaded_at))


def valid_retention_days(days, category, log=logger):
    if type(days) is not int or days < 0:
        log.warning(f'[{category}] WebDAV保留天数必须是非负整数，跳过清理')
        return False
    return days > 0


def remote_is_dir(client, path):
    """直接 PROPFIND；client.check/is_dir 会将部分 HTTP 错误误判为不存在。"""
    urn = Urn(path)
    response = client.execute_request(action='info', path=urn.quote(), headers_ext=['Depth: 0'])
    try:
        resource = WebDavXmlUtils.extract_response_for_path(
            content=response.content, path=client.get_full_path(urn), hostname=client.webdav.hostname,
        )
    except RemoteResourceNotFound as e:
        # 只有 HTTP 404 能确认不存在，响应未包含资源也可能是服务器异常。
        raise ValueError('WebDAV响应未包含请求资源，不能确认文件不存在') from e
    for propstat in resource.findall('{DAV:}propstat'):
        status = propstat.findtext('{DAV:}status', '').split()
        resource_type = propstat.find('{DAV:}prop/{DAV:}resourcetype')
        if len(status) >= 2 and status[1] == '200' and resource_type is not None:
            return resource_type.find('{DAV:}collection') is not None
    raise ValueError('WebDAV响应未提供成功的资源类型属性')


def cleanup_date_directories(client, days, timezone_name, category, dry_run=True, now=None, log=logger):
    """保留含今天在内的 N 个自然日；只删除更早的有效日期目录。"""
    if not client or not valid_retention_days(days, category, log):
        return
    try:
        timezone = pytz.timezone(timezone_name)
        today = (now or datetime.now(timezone)).astimezone(timezone).date()
        cutoff = today - timedelta(days=days - 1)
        items = client.list('/')
        for name in sorted({item.strip('/') for item in items}):
            if not re.fullmatch(r'[0-9]{8}', name):
                continue
            try:
                directory_date = datetime.strptime(name, '%Y%m%d').date()
            except ValueError:
                continue
            if directory_date >= cutoff:
                continue
            try:
                if client.is_dir(name) is not True:
                    continue
                if dry_run:
                    log.info(f'[{category}] WebDAV清理预演，待删除日期目录: {name}')
                else:
                    client.clean(name)
                    log.info(f'[{category}] 已删除WebDAV过期日期目录: {name}')
            except Exception as e:
                log.error(f'[{category}] WebDAV目录清理失败，保留待处理项 {name}: {e}')
    except Exception as e:
        log.error(f'[{category}] WebDAV日期目录扫描失败: {e}')


def cleanup_uploaded_files(client, history, target_id, category, days, dry_run=True, now=None, log=logger):
    """按上传成功后的 N * 24 小时清理，历史无记录文件不参与。"""
    if not client or not valid_retention_days(days, category, log):
        return
    try:
        cutoff = (time.time() if now is None else now) - days * 86400
        for path, uploaded_at in history.expired(target_id, category, cutoff):
            if not is_root_file_path(path):
                log.warning(f'[{category}] 上传记录路径不属于根目录直接子文件，跳过清理')
                continue
            try:
                if remote_is_dir(client, path) is not False:
                    continue
                if dry_run:
                    log.info(f'[{category}] WebDAV清理预演，待删除文件: {path}')
                else:
                    client.clean(path)
                    history.forget(target_id, category, path, uploaded_at)
                    log.info(f'[{category}] 已删除WebDAV过期文件: {path}')
            except RemoteResourceNotFound:
                if not dry_run:
                    history.forget(target_id, category, path, uploaded_at)
            except Exception as e:
                log.error(f'[{category}] WebDAV文件清理失败，保留上传记录 {path}: {e}')
    except Exception as e:
        log.error(f'[{category}] WebDAV上传记录清理失败: {e}')
