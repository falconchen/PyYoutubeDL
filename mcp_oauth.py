#!/usr/bin/env python3
"""PyYoutubeDL MCP 内置 OAuth 2.1 授权服务器（零新增第三方依赖）。

支持同时接入 Claude 网页版（默认 CIMD）、ChatGPT 网页版（CIMD / DCR）及支持
OAuth 的桌面/CLI 客户端：
- RFC 9728 受保护资源元数据：`/.well-known/oauth-protected-resource`（及带资源路径后缀）
- RFC 8414 授权服务器元数据：`/.well-known/oauth-authorization-server` / `/.well-known/openid-configuration`
- RFC 7591 动态客户端注册（DCR）：`POST /mcp/oauth/register`
- Client ID Metadata Document（CIMD）：支持 `https://` URL 形式 client_id（拉取强制过 SSRF 校验）
- OAuth 2.1 授权码 + 强制 PKCE（S256）+ RFC 8707 resource 绑定 + Refresh Token 轮换
- 所有授权码、Access Token、Refresh Token 与 client_secret 仅保存 SHA-256 哈希
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests
from flask import Blueprint, Response, jsonify, redirect, render_template, request, session

from config_util import get_mcp_path, is_mcp_oauth_enabled
from url_security import validate_download_url


SCHEMA_VERSION = 1
MCP_SCOPE = 'mcp'
ACCEPTED_SCOPES = {MCP_SCOPE, 'offline_access'}

OAUTH_PREFIX = '/mcp/oauth'
AUTHORIZE_PATH = f'{OAUTH_PREFIX}/authorize'
TOKEN_PATH = f'{OAUTH_PREFIX}/token'
REGISTER_PATH = f'{OAUTH_PREFIX}/register'
REVOKE_PATH = f'{OAUTH_PREFIX}/revoke'

AUTH_CODE_TTL_SECONDS = 120
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 3600
DEFAULT_REFRESH_TOKEN_IDLE_TTL_SECONDS = 90 * 24 * 60 * 60
LAST_USED_TOUCH_INTERVAL_SECONDS = 60

CIMD_FETCH_TIMEOUT_SECONDS = 5
CIMD_MAX_RESPONSE_BYTES = 32 * 1024
MAX_REDIRECT_URIS = 20
PKCE_CHALLENGE_RE = re.compile(r'^[A-Za-z0-9_-]{43,128}$')
PKCE_VERIFIER_RE = re.compile(r'^[A-Za-z0-9._~-]{43,128}$')
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
SUPPORTED_AUTH_METHODS = {'none', 'client_secret_post', 'client_secret_basic'}
CSRF_SESSION_KEY = 'mcp_oauth_csrf_token'


class OAuthProtocolError(Exception):
    """标准 OAuth 2.1 错误。"""

    def __init__(self, error, description, status=400, redirect_to=None):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.redirect_to = redirect_to


class CimdFetchError(Exception):
    """拉取或校验客户端元数据文档（CIMD）失败。"""


def _now_ts():
    return int(time.time())


def _sha256_hex(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _positive_int(value, default):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    return conn


def init_db(db_path):
    """初始化 MCP OAuth SQLite 数据库；重复调用安全。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _connect(db_path) as db:
        db.execute('PRAGMA journal_mode = WAL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f'MCP OAuth 数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}'
            )
        if version == 0:
            db.executescript(
                '''
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris_json TEXT NOT NULL,
                    token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
                    client_secret_hash TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'dcr',
                    verified_domain TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_grants (
                    id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    redirect_host TEXT NOT NULL DEFAULT '',
                    verified_domain TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'mcp',
                    resource TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_oauth_grants_created
                    ON oauth_grants(created_at DESC);

                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    code_challenge_method TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL DEFAULT '',
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    token_type TEXT NOT NULL,
                    grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
                    client_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL DEFAULT '',
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_oauth_tokens_grant
                    ON oauth_tokens(grant_id, token_type);
                '''
            )
            db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')


def resolve_public_origin(runtime_config, req=None):
    """解析对外公开的 Origin（scheme://host[:port]），优先使用 MCP_PUBLIC_BASE_URL。"""
    configured = str(runtime_config.get('MCP_PUBLIC_BASE_URL') or '').strip()
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme.lower() in {'http', 'https'} and parsed.netloc:
            return f'{parsed.scheme.lower()}://{parsed.netloc}'
    if req is not None:
        forwarded_proto = (req.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        forwarded_host = (req.headers.get('X-Forwarded-Host') or '').split(',')[0].strip()
        scheme = forwarded_proto if forwarded_proto in {'http', 'https'} else req.scheme
        host = forwarded_host or req.host
        if scheme and host:
            return f'{scheme}://{host}'
    return 'http://127.0.0.1:5100'


def build_resource_url(runtime_config, req=None):
    """构造 RFC 8707 资源标识 URL（如 https://example.com/mcp）。"""
    origin = resolve_public_origin(runtime_config, req=req)
    return f'{origin}{get_mcp_path(runtime_config)}'


def normalize_resource_url(value):
    """规范化用于比较的 resource URL（去掉多余尾斜杠）。"""
    if not isinstance(value, str):
        return ''
    stripped = value.strip()
    if not stripped:
        return ''
    parsed = urlsplit(stripped)
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.netloc:
        return ''
    path = parsed.path.rstrip('/') or '/'
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, '', ''))


def is_loopback_host(hostname):
    return (hostname or '').strip().lower().strip('[]') in LOOPBACK_HOSTS


def _allowed_redirect_hosts(runtime_config):
    raw = runtime_config.get('MCP_OAUTH_ALLOWED_REDIRECT_HOSTS', [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [
        item.strip().lower().rstrip('.')
        for item in raw
        if isinstance(item, str) and item.strip()
    ]


def validate_redirect_uri(uri, runtime_config=None, enforce_allowlist=True):
    """校验 OAuth 2.1 redirect_uri 合法性，返回规范信息或抛出 ValueError。"""
    if not isinstance(uri, str) or not uri.strip() or len(uri) > 2048:
        raise ValueError('redirect_uri 必须是非空字符串且不超过 2048 字符')
    uri = uri.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in uri):
        raise ValueError('redirect_uri 包含非法控制字符')

    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or '').lower().rstrip('.')
    if parsed.fragment:
        raise ValueError('redirect_uri 不允许包含 fragment (#)')
    if not hostname:
        raise ValueError('redirect_uri 缺少主机名')

    loopback = is_loopback_host(hostname)
    if scheme == 'https':
        pass
    elif scheme == 'http' and loopback:
        pass
    else:
        raise ValueError('redirect_uri 必须使用 https（或本机回环 http）')

    if enforce_allowlist and not loopback and runtime_config:
        allowed = _allowed_redirect_hosts(runtime_config)
        if allowed and not any(
            hostname == item or hostname.endswith('.' + item)
            for item in allowed
        ):
            raise ValueError(f'回调域名 {hostname} 不在允许列表内')

    return {
        'uri': uri,
        'host': parsed.netloc,
        'hostname': hostname,
        'is_loopback': loopback,
    }


def _append_query_params(uri, params):
    parsed = urlsplit(uri)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is not None:
            existing.append((key, str(value)))
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(existing),
        '',
    ))


def _verify_pkce_s256(code_verifier, expected_challenge):
    if not isinstance(code_verifier, str) or not PKCE_VERIFIER_RE.fullmatch(code_verifier):
        return False
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return hmac.compare_digest(computed, expected_challenge)


def _is_cimd_client_id(client_id):
    if not isinstance(client_id, str):
        return False
    stripped = client_id.strip()
    if not stripped.startswith('https://') or len(stripped) > 2048:
        return False
    parsed = urlsplit(stripped)
    return (
        parsed.scheme.lower() == 'https'
        and bool(parsed.hostname)
        and bool(parsed.path and parsed.path != '/')
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def fetch_cimd_client(client_id, runtime_config, testing=False):
    """拉取并严格校验 Client ID Metadata Document（CIMD）。"""
    if not _is_cimd_client_id(client_id):
        raise CimdFetchError('client_id 不是合法的 HTTPS 元数据文档 URL')

    validation_config = dict(runtime_config)
    if testing:
        validation_config['DOWNLOAD_URL_RESOLVE_HOSTS'] = False
    reason = validate_download_url(client_id, validation_config)
    if reason:
        raise CimdFetchError(f'元数据地址未通过安全策略校验：{reason}')

    try:
        response = requests.get(
            client_id,
            timeout=CIMD_FETCH_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'PyYoutubeDL-MCP/1.0',
            },
        )
    except requests.RequestException as exc:
        raise CimdFetchError(f'无法拉取客户端元数据文档：{exc}') from exc

    try:
        if response.status_code != 200:
            raise CimdFetchError(
                f'客户端元数据文档返回 HTTP {response.status_code}'
            )
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            total += len(chunk)
            if total > CIMD_MAX_RESPONSE_BYTES:
                raise CimdFetchError('客户端元数据文档体积超过限制')
            chunks.append(chunk)
        raw_bytes = b''.join(chunks)
    finally:
        response.close()

    try:
        doc = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CimdFetchError('客户端元数据文档不是有效 JSON') from exc

    if not isinstance(doc, dict):
        raise CimdFetchError('客户端元数据文档必须是 JSON 对象')
    if doc.get('client_id') != client_id:
        raise CimdFetchError('元数据文档中的 client_id 与请求地址不一致')

    raw_redirects = doc.get('redirect_uris')
    if (
        not isinstance(raw_redirects, list)
        or not raw_redirects
        or len(raw_redirects) > MAX_REDIRECT_URIS
    ):
        raise CimdFetchError('元数据文档缺少有效的 redirect_uris')

    normalized_redirects = []
    for uri in raw_redirects:
        try:
            # CIMD 由域名身份核实，不再套用 DCR 回调白名单
            info = validate_redirect_uri(uri, runtime_config, enforce_allowlist=False)
        except ValueError as exc:
            raise CimdFetchError(f'元数据文档包含非法 redirect_uri：{exc}') from exc
        normalized_redirects.append(info['uri'])

    parsed_id = urlsplit(client_id)
    verified_domain = (parsed_id.hostname or '').lower()
    raw_name = doc.get('client_name')
    client_name = (
        raw_name.strip()[:120]
        if isinstance(raw_name, str) and raw_name.strip()
        else verified_domain
    )
    auth_method = doc.get('token_endpoint_auth_method') or 'none'
    if auth_method != 'none':
        auth_method = 'none'

    return {
        'client_id': client_id,
        'client_name': client_name,
        'redirect_uris': normalized_redirects,
        'token_endpoint_auth_method': auth_method,
        'source': 'cimd',
        'verified_domain': verified_domain,
    }


def upsert_client(db_path, client_record, client_secret_hash=''):
    now = _now_ts()
    with _connect(db_path) as db:
        db.execute(
            '''
            INSERT INTO oauth_clients (
                client_id, client_name, redirect_uris_json,
                token_endpoint_auth_method, client_secret_hash,
                source, verified_domain, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                client_name = excluded.client_name,
                redirect_uris_json = excluded.redirect_uris_json,
                token_endpoint_auth_method = excluded.token_endpoint_auth_method,
                source = excluded.source,
                verified_domain = excluded.verified_domain,
                updated_at = excluded.updated_at
            ''',
            (
                client_record['client_id'],
                client_record['client_name'],
                json.dumps(client_record['redirect_uris'], ensure_ascii=False),
                client_record.get('token_endpoint_auth_method', 'none'),
                client_secret_hash,
                client_record.get('source', 'dcr'),
                client_record.get('verified_domain', ''),
                now,
                now,
            ),
        )


def get_client(db_path, client_id):
    if not isinstance(client_id, str) or not client_id.strip():
        return None
    with _connect(db_path) as db:
        row = db.execute(
            'SELECT * FROM oauth_clients WHERE client_id = ?',
            (client_id.strip(),),
        ).fetchone()
    if row is None:
        return None
    try:
        redirect_uris = json.loads(row['redirect_uris_json'])
    except ValueError:
        redirect_uris = []
    return {
        'client_id': row['client_id'],
        'client_name': row['client_name'],
        'redirect_uris': redirect_uris if isinstance(redirect_uris, list) else [],
        'token_endpoint_auth_method': row['token_endpoint_auth_method'],
        'client_secret_hash': row['client_secret_hash'],
        'source': row['source'],
        'verified_domain': row['verified_domain'],
        'created_at': row['created_at'],
    }


def register_dynamic_client(db_path, payload, runtime_config):
    """执行 RFC 7591 动态客户端注册（DCR）。"""
    if not isinstance(payload, dict):
        raise OAuthProtocolError(
            'invalid_client_metadata',
            '请求体必须是 JSON 对象',
        )

    raw_redirects = payload.get('redirect_uris')
    if (
        not isinstance(raw_redirects, list)
        or not raw_redirects
        or len(raw_redirects) > MAX_REDIRECT_URIS
    ):
        raise OAuthProtocolError(
            'invalid_redirect_uri',
            'redirect_uris 必须是非空数组（最多 20 项）',
        )

    redirect_uris = []
    for uri in raw_redirects:
        try:
            info = validate_redirect_uri(uri, runtime_config, enforce_allowlist=True)
        except ValueError as exc:
            raise OAuthProtocolError('invalid_redirect_uri', str(exc)) from exc
        if info['uri'] not in redirect_uris:
            redirect_uris.append(info['uri'])

    raw_name = payload.get('client_name')
    client_name = (
        raw_name.strip()[:120]
        if isinstance(raw_name, str) and raw_name.strip()
        else '未命名 MCP 应用'
    )

    auth_method = payload.get('token_endpoint_auth_method', 'none')
    if not isinstance(auth_method, str) or auth_method not in SUPPORTED_AUTH_METHODS:
        auth_method = 'none'

    client_id = f'pydl_cid_{secrets.token_urlsafe(18)}'
    client_secret = ''
    client_secret_hash = ''
    if auth_method in {'client_secret_post', 'client_secret_basic'}:
        client_secret = f'pydl_csec_{secrets.token_urlsafe(24)}'
        client_secret_hash = _sha256_hex(client_secret)

    now = _now_ts()
    upsert_client(
        db_path,
        {
            'client_id': client_id,
            'client_name': client_name,
            'redirect_uris': redirect_uris,
            'token_endpoint_auth_method': auth_method,
            'source': 'dcr',
            'verified_domain': '',
        },
        client_secret_hash=client_secret_hash,
    )

    response_payload = {
        'client_id': client_id,
        'client_id_issued_at': now,
        'client_name': client_name,
        'redirect_uris': redirect_uris,
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': auth_method,
        'scope': MCP_SCOPE,
    }
    if client_secret:
        response_payload['client_secret'] = client_secret
        response_payload['client_secret_expires_at'] = 0
    return response_payload


def _normalize_requested_scope(raw_scope):
    if raw_scope is None or not str(raw_scope).strip():
        return MCP_SCOPE
    parts = [part for part in str(raw_scope).strip().split() if part]
    if any(part not in ACCEPTED_SCOPES for part in parts):
        raise OAuthProtocolError(
            'invalid_scope',
            f'不支持的授权范围，仅支持 {MCP_SCOPE}',
        )
    return MCP_SCOPE


def resolve_authorize_request(
    db_path,
    params,
    runtime_config,
    expected_resource,
    testing=False,
):
    """解析并严格校验 OAuth 2.1 授权请求参数。

    只有在 client_id 与 redirect_uri 均校验通过后，才会附带 redirect_to，
    防止成为开放重定向器。
    """
    client_id = (params.get('client_id') or '').strip()
    redirect_uri = (params.get('redirect_uri') or '').strip()
    response_type = (params.get('response_type') or '').strip()
    code_challenge = (params.get('code_challenge') or '').strip()
    code_challenge_method = (params.get('code_challenge_method') or '').strip()
    state = params.get('state')
    raw_scope = params.get('scope')
    raw_resource = (params.get('resource') or '').strip()

    if not client_id:
        raise OAuthProtocolError('invalid_request', '缺少 client_id 参数')
    if not redirect_uri:
        raise OAuthProtocolError('invalid_request', '缺少 redirect_uri 参数')

    client = None
    if _is_cimd_client_id(client_id):
        if not bool(runtime_config.get('MCP_OAUTH_ENABLE_CIMD', True)):
            raise OAuthProtocolError(
                'unauthorized_client',
                '当前服务端已禁用 CIMD，请改用动态客户端注册（DCR）',
            )
        client = fetch_cimd_client(client_id, runtime_config, testing=testing)
        upsert_client(db_path, client)
    else:
        client = get_client(db_path, client_id)
        if client is None:
            raise OAuthProtocolError(
                'unauthorized_client',
                '未找到对应的客户端登记记录，请重新发起连接',
            )

    if redirect_uri not in client['redirect_uris']:
        raise OAuthProtocolError(
            'invalid_request',
            'redirect_uri 与客户端登记的回调地址不匹配',
        )

    redirect_info = validate_redirect_uri(
        redirect_uri,
        runtime_config,
        enforce_allowlist=False,
    )

    def make_redirect_error(code, description):
        target = _append_query_params(
            redirect_uri,
            {'error': code, 'error_description': description, 'state': state},
        )
        return OAuthProtocolError(code, description, redirect_to=target)

    if response_type != 'code':
        raise make_redirect_error(
            'unsupported_response_type',
            '仅支持 authorization_code (response_type=code)',
        )
    if not code_challenge or not PKCE_CHALLENGE_RE.fullmatch(code_challenge):
        raise make_redirect_error(
            'invalid_request',
            '缺少或无效的 PKCE code_challenge',
        )
    if code_challenge_method != 'S256':
        raise make_redirect_error(
            'invalid_request',
            'OAuth 2.1 强制要求 code_challenge_method=S256',
        )

    try:
        scope = _normalize_requested_scope(raw_scope)
    except OAuthProtocolError as exc:
        raise make_redirect_error(exc.error, exc.description) from exc

    normalized_expected = normalize_resource_url(expected_resource)
    bound_resource = expected_resource
    if raw_resource:
        if normalize_resource_url(raw_resource) != normalized_expected:
            raise make_redirect_error(
                'invalid_target',
                '请求的 resource 与当前 MCP 服务地址不匹配',
            )
        bound_resource = expected_resource

    return {
        'client': client,
        'client_id': client['client_id'],
        'client_name': client['client_name'],
        'verified_domain': client.get('verified_domain', ''),
        'source': client.get('source', 'dcr'),
        'redirect_uri': redirect_uri,
        'redirect_host': redirect_info['host'],
        'redirect_is_loopback': redirect_info['is_loopback'],
        'code_challenge': code_challenge,
        'code_challenge_method': code_challenge_method,
        'state': state,
        'scope': scope,
        'resource': bound_resource,
    }


def issue_authorization_code(db_path, auth_request):
    """用户确认同意后，创建 grant 记录与一次性短效授权码。"""
    now = _now_ts()
    grant_id = f'pydl_gr_{secrets.token_hex(12)}'
    raw_code = f'pydl_ac_{secrets.token_urlsafe(32)}'
    code_hash = _sha256_hex(raw_code)
    expires_at = now + AUTH_CODE_TTL_SECONDS

    with _connect(db_path) as db:
        db.execute(
            '''
            INSERT INTO oauth_grants (
                id, client_id, client_name, redirect_host, verified_domain,
                scope, resource, created_at, last_used_at, revoked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ''',
            (
                grant_id,
                auth_request['client_id'],
                auth_request['client_name'],
                auth_request['redirect_host'],
                auth_request.get('verified_domain', ''),
                auth_request['scope'],
                auth_request['resource'],
                now,
                now,
            ),
        )
        db.execute(
            '''
            INSERT INTO oauth_codes (
                code_hash, grant_id, client_id, redirect_uri,
                code_challenge, code_challenge_method, scope, resource,
                expires_at, used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ''',
            (
                code_hash,
                grant_id,
                auth_request['client_id'],
                auth_request['redirect_uri'],
                auth_request['code_challenge'],
                auth_request['code_challenge_method'],
                auth_request['scope'],
                auth_request['resource'],
                expires_at,
            ),
        )
    return raw_code, grant_id


def _issue_token_pair(db, grant_id, client_id, scope, resource, runtime_config):
    now = _now_ts()
    access_ttl = _positive_int(
        runtime_config.get('MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS'),
        DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
    )
    refresh_ttl = _positive_int(
        runtime_config.get('MCP_OAUTH_REFRESH_TOKEN_IDLE_TTL_SECONDS'),
        DEFAULT_REFRESH_TOKEN_IDLE_TTL_SECONDS,
    )

    access_token = f'pydl_at_{secrets.token_urlsafe(32)}'
    refresh_token = f'pydl_rt_{secrets.token_urlsafe(32)}'

    db.execute(
        '''
        INSERT INTO oauth_tokens (
            token_hash, token_type, grant_id, client_id, scope,
            resource, expires_at, revoked_at, created_at
        )
        VALUES (?, 'access', ?, ?, ?, ?, ?, NULL, ?)
        ''',
        (
            _sha256_hex(access_token),
            grant_id,
            client_id,
            scope,
            resource,
            now + access_ttl,
            now,
        ),
    )
    db.execute(
        '''
        INSERT INTO oauth_tokens (
            token_hash, token_type, grant_id, client_id, scope,
            resource, expires_at, revoked_at, created_at
        )
        VALUES (?, 'refresh', ?, ?, ?, ?, ?, NULL, ?)
        ''',
        (
            _sha256_hex(refresh_token),
            grant_id,
            client_id,
            scope,
            resource,
            now + refresh_ttl,
            now,
        ),
    )
    db.execute(
        'UPDATE oauth_grants SET last_used_at = ? WHERE id = ?',
        (now, grant_id),
    )
    return {
        'access_token': access_token,
        'token_type': 'Bearer',
        'expires_in': access_ttl,
        'refresh_token': refresh_token,
        'scope': scope,
    }


def _extract_client_credentials(req, form_data):
    client_id = (form_data.get('client_id') or '').strip()
    client_secret = (form_data.get('client_secret') or '').strip()

    auth_header = req.headers.get('Authorization', '')
    scheme, _, encoded = auth_header.partition(' ')
    if scheme.lower() == 'basic' and encoded.strip():
        try:
            decoded = base64.b64decode(encoded.strip()).decode('utf-8')
            basic_id, sep, basic_secret = decoded.partition(':')
            if sep:
                client_id = client_id or basic_id
                client_secret = client_secret or basic_secret
        except Exception:
            pass
    return client_id, client_secret


def _verify_client_secret_if_required(client, client_secret):
    method = client.get('token_endpoint_auth_method', 'none')
    if method == 'none':
        return
    expected_hash = client.get('client_secret_hash') or ''
    if not expected_hash or not client_secret:
        raise OAuthProtocolError(
            'invalid_client',
            '客户端凭据无效',
            status=401,
        )
    if not hmac.compare_digest(_sha256_hex(client_secret), expected_hash):
        raise OAuthProtocolError(
            'invalid_client',
            '客户端凭据无效',
            status=401,
        )


def exchange_authorization_code(
    db_path,
    code,
    client_id,
    client_secret,
    redirect_uri,
    code_verifier,
    resource,
    runtime_config,
):
    """使用授权码 + PKCE code_verifier 换取 Access Token 和 Refresh Token。"""
    if not code or not client_id or not redirect_uri or not code_verifier:
        raise OAuthProtocolError(
            'invalid_request',
            '缺少 code、client_id、redirect_uri 或 code_verifier 参数',
        )

    client = get_client(db_path, client_id)
    if client is None:
        raise OAuthProtocolError('invalid_client', '未知客户端', status=401)
    _verify_client_secret_if_required(client, client_secret)

    now = _now_ts()
    code_hash = _sha256_hex(code)
    with _connect(db_path) as db:
        row = db.execute(
            'SELECT * FROM oauth_codes WHERE code_hash = ?',
            (code_hash,),
        ).fetchone()
        if row is None:
            raise OAuthProtocolError('invalid_grant', '授权码无效')

        # 授权码被重放时，按 OAuth 2.1 撤销关联 grant 下所有令牌
        if row['used_at'] is not None:
            db.execute(
                'UPDATE oauth_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL',
                (now, row['grant_id']),
            )
            db.execute(
                'UPDATE oauth_tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL',
                (now, row['grant_id']),
            )
            raise OAuthProtocolError('invalid_grant', '授权码已使用，已吊销关联授权')

        if row['expires_at'] < now:
            raise OAuthProtocolError('invalid_grant', '授权码已过期')
        if row['client_id'] != client_id or row['redirect_uri'] != redirect_uri:
            raise OAuthProtocolError('invalid_grant', '授权码与客户端或回调地址不匹配')
        if not _verify_pkce_s256(code_verifier, row['code_challenge']):
            raise OAuthProtocolError('invalid_grant', 'PKCE 校验失败')

        if resource:
            if normalize_resource_url(resource) != normalize_resource_url(row['resource']):
                raise OAuthProtocolError(
                    'invalid_target',
                    'resource 参数与授权时不一致',
                )

        cursor = db.execute(
            'UPDATE oauth_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL',
            (now, code_hash),
        )
        if cursor.rowcount != 1:
            raise OAuthProtocolError('invalid_grant', '授权码已使用')

        return _issue_token_pair(
            db,
            row['grant_id'],
            client_id,
            row['scope'],
            row['resource'],
            runtime_config,
        )


def exchange_refresh_token(
    db_path,
    refresh_token,
    client_id,
    client_secret,
    resource,
    runtime_config,
):
    """使用 Refresh Token 轮换签发新的 Access Token 与 Refresh Token。"""
    if not refresh_token:
        raise OAuthProtocolError('invalid_request', '缺少 refresh_token 参数')

    now = _now_ts()
    token_hash = _sha256_hex(refresh_token)
    with _connect(db_path) as db:
        row = db.execute(
            '''
            SELECT t.*, g.revoked_at AS grant_revoked_at
            FROM oauth_tokens t
            JOIN oauth_grants g ON g.id = t.grant_id
            WHERE t.token_hash = ? AND t.token_type = 'refresh'
            ''',
            (token_hash,),
        ).fetchone()
        if row is None:
            raise OAuthProtocolError('invalid_grant', 'refresh_token 无效')

        effective_client_id = client_id or row['client_id']
        if effective_client_id != row['client_id']:
            raise OAuthProtocolError('invalid_grant', 'refresh_token 与客户端不匹配')

        client = get_client(db_path, effective_client_id)
        if client is None:
            raise OAuthProtocolError('invalid_client', '未知客户端', status=401)
        _verify_client_secret_if_required(client, client_secret)

        if row['revoked_at'] is not None or row['grant_revoked_at'] is not None:
            raise OAuthProtocolError('invalid_grant', 'refresh_token 已被吊销')
        if row['expires_at'] < now:
            raise OAuthProtocolError('invalid_grant', 'refresh_token 已过期，请重新授权')

        if resource:
            if normalize_resource_url(resource) != normalize_resource_url(row['resource']):
                raise OAuthProtocolError(
                    'invalid_target',
                    'resource 参数与原授权不一致',
                )

        # 轮换：撤销旧 refresh_token，签发全新一对令牌
        cursor = db.execute(
            '''
            UPDATE oauth_tokens
            SET revoked_at = ?
            WHERE token_hash = ? AND revoked_at IS NULL
            ''',
            (now, token_hash),
        )
        if cursor.rowcount != 1:
            raise OAuthProtocolError('invalid_grant', 'refresh_token 已被使用或吊销')

        return _issue_token_pair(
            db,
            row['grant_id'],
            row['client_id'],
            row['scope'],
            row['resource'],
            runtime_config,
        )


def revoke_token(db_path, raw_token):
    """按 RFC 7009 吊销令牌；若传入的是 refresh_token 则连同整个 grant 一起撤销。"""
    if not isinstance(raw_token, str) or not raw_token.strip():
        return False
    now = _now_ts()
    token_hash = _sha256_hex(raw_token.strip())
    with _connect(db_path) as db:
        row = db.execute(
            'SELECT token_type, grant_id FROM oauth_tokens WHERE token_hash = ?',
            (token_hash,),
        ).fetchone()
        if row is None:
            return False
        if row['token_type'] == 'refresh':
            db.execute(
                'UPDATE oauth_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL',
                (now, row['grant_id']),
            )
            db.execute(
                'UPDATE oauth_tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL',
                (now, row['grant_id']),
            )
        else:
            db.execute(
                'UPDATE oauth_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL',
                (now, token_hash),
            )
    return True


def verify_access_token(db_path, raw_token, expected_resource=None):
    """校验 OAuth Access Token，有效时返回授权上下文，否则返回 None。"""
    if not isinstance(raw_token, str) or not raw_token.strip():
        return None
    if not os.path.isfile(db_path):
        return None

    now = _now_ts()
    token_hash = _sha256_hex(raw_token.strip())
    with _connect(db_path) as db:
        row = db.execute(
            '''
            SELECT t.grant_id, t.client_id, t.scope, t.resource, t.expires_at,
                   t.revoked_at, g.client_name, g.redirect_host, g.last_used_at,
                   g.revoked_at AS grant_revoked_at
            FROM oauth_tokens t
            JOIN oauth_grants g ON g.id = t.grant_id
            WHERE t.token_hash = ? AND t.token_type = 'access'
            ''',
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if row['revoked_at'] is not None or row['grant_revoked_at'] is not None:
            return None
        if row['expires_at'] < now:
            return None
        if expected_resource and row['resource']:
            if normalize_resource_url(row['resource']) != normalize_resource_url(expected_resource):
                return None

        if now - (row['last_used_at'] or 0) >= LAST_USED_TOUCH_INTERVAL_SECONDS:
            db.execute(
                'UPDATE oauth_grants SET last_used_at = ? WHERE id = ?',
                (now, row['grant_id']),
            )

        return {
            'via': 'oauth',
            'subject': f"grant:{row['grant_id']}",
            'grant_id': row['grant_id'],
            'client_id': row['client_id'],
            'client_name': row['client_name'],
            'redirect_host': row['redirect_host'],
            'scope': row['scope'],
        }


def list_grants(db_path, include_revoked=False):
    """列出已授权的 MCP OAuth 应用（供 CLI 运维查看）。"""
    if not os.path.isfile(db_path):
        return []
    query = 'SELECT * FROM oauth_grants'
    if not include_revoked:
        query += ' WHERE revoked_at IS NULL'
    query += ' ORDER BY created_at DESC'
    with _connect(db_path) as db:
        rows = db.execute(query).fetchall()
    return [dict(row) for row in rows]


def revoke_grant(db_path, grant_id):
    """撤销指定 grant 及关联的所有访问/刷新令牌。"""
    if not os.path.isfile(db_path) or not isinstance(grant_id, str) or not grant_id.strip():
        return False
    now = _now_ts()
    with _connect(db_path) as db:
        cursor = db.execute(
            'UPDATE oauth_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL',
            (now, grant_id.strip()),
        )
        db.execute(
            'UPDATE oauth_tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL',
            (now, grant_id.strip()),
        )
        return cursor.rowcount > 0


def revoke_all_grants(db_path):
    """撤销全部未撤销的 OAuth 授权，返回撤销数量。"""
    if not os.path.isfile(db_path):
        return 0
    now = _now_ts()
    with _connect(db_path) as db:
        cursor = db.execute(
            'UPDATE oauth_grants SET revoked_at = ? WHERE revoked_at IS NULL',
            (now,),
        )
        db.execute(
            'UPDATE oauth_tokens SET revoked_at = ? WHERE revoked_at IS NULL',
            (now,),
        )
        return cursor.rowcount


def _apply_cors_headers(response):
    origin = request.headers.get('Origin')
    response.headers['Access-Control-Allow-Origin'] = origin or '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = (
        'Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name'
    )
    response.headers['Access-Control-Max-Age'] = '600'
    if origin:
        response.headers['Vary'] = 'Origin'
    return response


def _apply_authorize_security_headers(response):
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    response.headers['Cache-Control'] = 'no-store'
    return response


def _oauth_json(payload, status=200, cors=True):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    if cors:
        _apply_cors_headers(response)
    return response


def _verify_admin_password(password, runtime_config):
    expected = str(runtime_config.get('MCP_OAUTH_ADMIN_PASSWORD_SHA256') or '').strip().lower()
    if not expected or not isinstance(password, str) or not password:
        return False
    actual = hashlib.sha256(password.encode('utf-8')).hexdigest().lower()
    return hmac.compare_digest(actual, expected)


def create_mcp_oauth_blueprint(get_config, is_testing=None):
    """创建 MCP OAuth 2.1 与 .well-known 发现端点 Blueprint。"""
    bp = Blueprint('mcp_oauth', __name__)
    testing_checker = is_testing or (lambda: False)

    def _disabled_response():
        return _oauth_json(
            {
                'error': 'oauth_disabled',
                'error_description': 'MCP OAuth 2.1 服务未启用或未完成配置',
            },
            status=503,
        )

    def _protected_resource_metadata():
        cfg = get_config()
        if not is_mcp_oauth_enabled(cfg):
            return _disabled_response()
        origin = resolve_public_origin(cfg, req=request)
        resource = build_resource_url(cfg, req=request)
        return _oauth_json({
            'resource': resource,
            'resource_name': 'PyYoutubeDL',
            'authorization_servers': [origin],
            'bearer_methods_supported': ['header'],
            'scopes_supported': [MCP_SCOPE],
        })

    def _authorization_server_metadata():
        cfg = get_config()
        if not is_mcp_oauth_enabled(cfg):
            return _disabled_response()
        origin = resolve_public_origin(cfg, req=request)
        return _oauth_json({
            'issuer': origin,
            'authorization_endpoint': f'{origin}{AUTHORIZE_PATH}',
            'token_endpoint': f'{origin}{TOKEN_PATH}',
            'registration_endpoint': f'{origin}{REGISTER_PATH}',
            'revocation_endpoint': f'{origin}{REVOKE_PATH}',
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'token_endpoint_auth_methods_supported': [
                'none',
                'client_secret_post',
                'client_secret_basic',
            ],
            'code_challenge_methods_supported': ['S256'],
            'scopes_supported': [MCP_SCOPE],
            'client_id_metadata_document_supported': bool(
                cfg.get('MCP_OAUTH_ENABLE_CIMD', True)
            ),
        })

    @bp.route('/.well-known/oauth-protected-resource', methods=['GET', 'OPTIONS'])
    @bp.route('/.well-known/oauth-protected-resource/<path:subpath>', methods=['GET', 'OPTIONS'])
    def oauth_protected_resource(subpath=None):
        del subpath
        if request.method == 'OPTIONS':
            return _apply_cors_headers(Response('', status=204))
        return _protected_resource_metadata()

    @bp.route('/.well-known/oauth-authorization-server', methods=['GET', 'OPTIONS'])
    @bp.route('/.well-known/openid-configuration', methods=['GET', 'OPTIONS'])
    def oauth_authorization_server():
        if request.method == 'OPTIONS':
            return _apply_cors_headers(Response('', status=204))
        return _authorization_server_metadata()

    @bp.route(REGISTER_PATH, methods=['POST', 'OPTIONS'])
    def oauth_register():
        if request.method == 'OPTIONS':
            return _apply_cors_headers(Response('', status=204))
        cfg = get_config()
        if not is_mcp_oauth_enabled(cfg):
            return _disabled_response()

        db_path = cfg['MCP_OAUTH_DB_PATH']
        init_db(db_path)
        payload = request.get_json(silent=True)
        try:
            registered = register_dynamic_client(db_path, payload, cfg)
        except OAuthProtocolError as exc:
            return _oauth_json(
                {'error': exc.error, 'error_description': exc.description},
                status=exc.status,
            )
        return _oauth_json(registered, status=201)

    @bp.route(AUTHORIZE_PATH, methods=['GET', 'POST'])
    def oauth_authorize():
        cfg = get_config()
        if not is_mcp_oauth_enabled(cfg):
            resp = Response(
                render_template(
                    'mcp_oauth_authorize.html',
                    auth_request=None,
                    error='MCP OAuth 2.1 尚未启用，请在 config.json 中配置 ENABLE_MCP_SERVER、ENABLE_MCP_OAUTH 与 MCP_OAUTH_ADMIN_PASSWORD_SHA256。',
                    csrf_token='',
                    form_error='',
                    raw_params={},
                ),
                status=503,
            )
            return _apply_authorize_security_headers(resp)

        db_path = cfg['MCP_OAUTH_DB_PATH']
        init_db(db_path)
        expected_resource = build_resource_url(cfg, req=request)
        params = request.form if request.method == 'POST' else request.args

        try:
            auth_request = resolve_authorize_request(
                db_path,
                params,
                cfg,
                expected_resource,
                testing=bool(testing_checker()),
            )
        except CimdFetchError as exc:
            resp = Response(
                render_template(
                    'mcp_oauth_authorize.html',
                    auth_request=None,
                    error=(
                        f'没能核实这个应用的身份（{exc}）。'
                        '如果在 ChatGPT 中连接，请展开高级 OAuth 设置并将注册方法改为「动态客户端注册（DCR）」。'
                    ),
                    csrf_token='',
                    form_error='',
                    raw_params={},
                ),
                status=400,
            )
            return _apply_authorize_security_headers(resp)
        except OAuthProtocolError as exc:
            if exc.redirect_to:
                return _apply_authorize_security_headers(redirect(exc.redirect_to))
            resp = Response(
                render_template(
                    'mcp_oauth_authorize.html',
                    auth_request=None,
                    error=exc.description,
                    csrf_token='',
                    form_error='',
                    raw_params={},
                ),
                status=400,
            )
            return _apply_authorize_security_headers(resp)

        raw_params = {
            'client_id': auth_request['client_id'],
            'redirect_uri': auth_request['redirect_uri'],
            'response_type': 'code',
            'code_challenge': auth_request['code_challenge'],
            'code_challenge_method': auth_request['code_challenge_method'],
            'scope': auth_request['scope'],
            'state': auth_request['state'] or '',
            'resource': params.get('resource') or '',
        }

        if request.method == 'GET':
            csrf_token = secrets.token_urlsafe(24)
            session[CSRF_SESSION_KEY] = csrf_token
            resp = Response(
                render_template(
                    'mcp_oauth_authorize.html',
                    auth_request=auth_request,
                    error='',
                    csrf_token=csrf_token,
                    form_error='',
                    raw_params=raw_params,
                ),
                status=200,
            )
            return _apply_authorize_security_headers(resp)

        # POST：先校验 CSRF
        submitted_csrf = (request.form.get('csrf_token') or '').strip()
        expected_csrf = session.get(CSRF_SESSION_KEY) or ''
        if not submitted_csrf or not expected_csrf or not hmac.compare_digest(submitted_csrf, expected_csrf):
            new_csrf = secrets.token_urlsafe(24)
            session[CSRF_SESSION_KEY] = new_csrf
            resp = Response(
                render_template(
                    'mcp_oauth_authorize.html',
                    auth_request=auth_request,
                    error='',
                    csrf_token=new_csrf,
                    form_error='页面会话已失效，请核对信息后重新提交。',
                    raw_params=raw_params,
                ),
                status=400,
            )
            return _apply_authorize_security_headers(resp)

        decision = (request.form.get('decision') or '').strip().lower()
        if decision == 'deny':
            session.pop(CSRF_SESSION_KEY, None)
            deny_url = _append_query_params(
                auth_request['redirect_uri'],
                {
                    'error': 'access_denied',
                    'error_description': '用户拒绝了授权请求',
                    'state': auth_request['state'],
                },
            )
            return _apply_authorize_security_headers(redirect(deny_url))

        password = request.form.get('admin_password') or ''
        if not _verify_admin_password(password, cfg):
            resp = Response(
                render_template(
                    'mcp_oauth_authorize.html',
                    auth_request=auth_request,
                    error='',
                    csrf_token=expected_csrf,
                    form_error='管理员授权口令不正确，请重试。',
                    raw_params=raw_params,
                ),
                status=403,
            )
            return _apply_authorize_security_headers(resp)

        session.pop(CSRF_SESSION_KEY, None)
        code, _ = issue_authorization_code(db_path, auth_request)
        origin = resolve_public_origin(cfg, req=request)
        callback_url = _append_query_params(
            auth_request['redirect_uri'],
            {
                'code': code,
                'state': auth_request['state'],
                'iss': origin,
            },
        )
        return _apply_authorize_security_headers(redirect(callback_url))

    @bp.route(TOKEN_PATH, methods=['POST', 'OPTIONS'])
    def oauth_token():
        if request.method == 'OPTIONS':
            return _apply_cors_headers(Response('', status=204))
        cfg = get_config()
        if not is_mcp_oauth_enabled(cfg):
            return _disabled_response()

        db_path = cfg['MCP_OAUTH_DB_PATH']
        init_db(db_path)
        data = request.get_json(silent=True) if request.is_json else request.form
        if not isinstance(data, dict) and hasattr(data, 'to_dict'):
            data = data.to_dict()
        data = data or {}

        grant_type = (data.get('grant_type') or '').strip()
        client_id, client_secret = _extract_client_credentials(request, data)
        resource = (data.get('resource') or '').strip()

        try:
            if grant_type == 'authorization_code':
                result = exchange_authorization_code(
                    db_path=db_path,
                    code=(data.get('code') or '').strip(),
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=(data.get('redirect_uri') or '').strip(),
                    code_verifier=(data.get('code_verifier') or '').strip(),
                    resource=resource,
                    runtime_config=cfg,
                )
            elif grant_type == 'refresh_token':
                result = exchange_refresh_token(
                    db_path=db_path,
                    refresh_token=(data.get('refresh_token') or '').strip(),
                    client_id=client_id,
                    client_secret=client_secret,
                    resource=resource,
                    runtime_config=cfg,
                )
            else:
                raise OAuthProtocolError(
                    'unsupported_grant_type',
                    '仅支持 authorization_code 与 refresh_token',
                )
        except OAuthProtocolError as exc:
            return _oauth_json(
                {'error': exc.error, 'error_description': exc.description},
                status=exc.status,
            )
        return _oauth_json(result, status=200)

    @bp.route(REVOKE_PATH, methods=['POST', 'OPTIONS'])
    def oauth_revoke():
        if request.method == 'OPTIONS':
            return _apply_cors_headers(Response('', status=204))
        cfg = get_config()
        if not is_mcp_oauth_enabled(cfg):
            return _disabled_response()

        db_path = cfg['MCP_OAUTH_DB_PATH']
        init_db(db_path)
        data = request.get_json(silent=True) if request.is_json else request.form
        token = (data.get('token') or '').strip() if data else ''
        revoke_token(db_path, token)
        # RFC 7009：即使令牌不存在也返回 200
        return _oauth_json({'revoked': True}, status=200)

    return bp
