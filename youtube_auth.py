#!venv/bin/python
"""YouTube OAuth 授权与 Data API 客户端封装。

职责：
- 加载/保存 OAuth 令牌文件（0600 权限）
- 用 refresh_token 刷新 access token
- 刷新失败时写入 fail-lock 并通过回调通知（Bark）
- 构建 YouTube Data API v3 service（支持可选代理）
- 构建 OAuth Web Flow（供 /oauth/start 与 /oauth/callback 使用）

本模块不依赖 Flask，可被 app.py（Web 授权）与 playlist_monitor.py
（后台轮询）共同使用。
"""

import json
import os
from urllib.parse import urlparse

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/youtube"]


def build_client_config(config):
    """用配置中的 client id/secret/redirect 生成 google-auth 客户端配置。"""
    return {
        "web": {
            "client_id": config.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            "client_secret": config.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.get("GOOGLE_OAUTH_REDIRECT_URI", "")],
        }
    }


def build_oauth_flow(config):
    """构建 OAuth Web Flow；调用方需在 authorization_url 时传入
    access_type='offline' 与 prompt='consent' 以取得 refresh_token。"""
    return Flow.from_client_config(
        build_client_config(config),
        scopes=SCOPES,
        redirect_uri=config.get("GOOGLE_OAUTH_REDIRECT_URI", ""),
    )


def get_oauth_start_url(config):
    """由回调地址推导 /oauth/start 重新授权入口 URL。"""
    redirect_uri = (config.get("GOOGLE_OAUTH_REDIRECT_URI") or "").rstrip("/")
    if redirect_uri.endswith("/callback"):
        return redirect_uri[: -len("/callback")] + "/start"
    return redirect_uri.rsplit("/", 1)[0] + "/start"


def load_token(config):
    """读取令牌文件；不存在或解析失败返回 None。"""
    token_file = config.get("GOOGLE_OAUTH_TOKEN_FILE", "")
    if not token_file or not os.path.exists(token_file):
        return None
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_token(config, token):
    """保存令牌字典，文件权限 0600。"""
    token_file = config.get("GOOGLE_OAUTH_TOKEN_FILE", "")
    if not token_file:
        return
    token_dir = os.path.dirname(token_file)
    if token_dir:
        os.makedirs(token_dir, exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass


def fail_lock_exists(config):
    lock_file = config.get("GOOGLE_OAUTH_FAIL_LOCK_FILE", "")
    return bool(lock_file and os.path.exists(lock_file))


def set_fail_lock(config):
    lock_file = config.get("GOOGLE_OAUTH_FAIL_LOCK_FILE", "")
    if not lock_file:
        return
    lock_dir = os.path.dirname(lock_file)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as f:
        f.write("1")


def clear_fail_lock(config):
    lock_file = config.get("GOOGLE_OAUTH_FAIL_LOCK_FILE", "")
    if lock_file and os.path.exists(lock_file):
        try:
            os.unlink(lock_file)
        except OSError:
            pass


def _proxied_request(config):
    """构造带可选代理的 google-auth 刷新请求会话。"""
    proxy = (config.get("YOUTUBE_API_PROXY") or "").strip()
    session = None
    if proxy:
        import requests

        session = requests.Session()
        session.proxies = {"http": proxy, "https": proxy}
    return GoogleAuthRequest(session=session)


def _credentials_to_token(credentials):
    """把 Credentials 转成 from_authorized_user_info 可读的令牌字典。"""
    return json.loads(credentials.to_json())


def get_credentials(config, notify=None):
    """加载并确保有效的 Credentials。

    Args:
        config: 运行配置字典。
        notify: 可选回调 notify(title, content)，用于失败时的 Bark 通知。

    Returns:
        Credentials 或 None（尚无令牌，需先授权）。

    Raises:
        RuntimeError: 令牌刷新失败（已写 fail-lock）。
    """
    token = load_token(config)
    if not token:
        return None

    creds = Credentials.from_authorized_user_info(token)
    if creds.valid:
        return creds

    if not creds.refresh_token:
        _handle_refresh_failure(config, notify, reason="缺少 refresh_token")
        raise RuntimeError("缺少 refresh_token")

    try:
        creds.refresh(_proxied_request(config))
    except Exception as exc:
        _handle_refresh_failure(config, notify, reason=str(exc))
        raise RuntimeError(f"刷新令牌失败: {exc}") from exc

    save_token(config, _credentials_to_token(creds))
    return creds


def _handle_refresh_failure(config, notify, reason=""):
    set_fail_lock(config)
    if notify:
        notify(
            "YouTube 授权已失效，请重新授权",
            f"{get_oauth_start_url(config)}\n{reason}",
        )


def _build_proxy_info(proxy):
    """把 http/https/socks5/socks5h://host:port 转成 httplib2 ProxyInfo。"""
    from httplib2 import ProxyInfo, socks

    parsed = urlparse(proxy)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    port = parsed.port
    if scheme in ("socks5", "socks5h"):
        if port is None:
            port = 1080
        return ProxyInfo(
            proxy_type=socks.PROXY_TYPE_SOCKS5,
            proxy_host=host,
            proxy_port=port,
            proxy_rdns=(scheme == "socks5h"),
        )
    if port is None:
        port = 8080
    return ProxyInfo(
        proxy_type=socks.PROXY_TYPE_HTTP,
        proxy_host=host,
        proxy_port=port,
    )


def build_youtube_service(config, credentials):
    """构建 YouTube Data API v3 service，支持可选 YOUTUBE_API_PROXY 代理。"""
    from googleapiclient.discovery import build

    proxy = (config.get("YOUTUBE_API_PROXY") or "").strip()
    if proxy:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp

        http = httplib2.Http(proxy_info=_build_proxy_info(proxy), timeout=30)
        authorized_http = AuthorizedHttp(credentials, http=http)
        return build("youtube", "v3", http=authorized_http, cache_discovery=False)
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)
