"""下载 URL 的 SSRF 安全校验。

下载器会让 yt-dlp 主动访问用户提交的地址，因此不能只在 Web 层判断
URL 格式。这个模块同时供 Web 入口和 downloader worker 使用，worker 的
校验是最后一道防线，避免手工写入 urls/ 目录的任务绕过 Web 层。
"""

import ipaddress
import socket
from urllib.parse import urlsplit


DEFAULT_MAX_URL_LENGTH = 4096
_ALLOWED_SCHEMES = {"http", "https"}
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}


def _normalized_hostname(hostname):
    """返回用于比较的 ASCII 小写主机名。"""
    hostname = (hostname or "").strip().rstrip(".").lower()
    if not hostname:
        return ""
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _parse_ip_literal(hostname):
    """解析标准及常见混淆形式的 IP 字面量。"""
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass

    # inet_aton 接受 127.1、十进制整数及 0x7f000001 等形式；这些形式
    # 也可能被底层 HTTP 客户端当作 127.0.0.1，不能只依赖 ipaddress。
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        return None
    return ipaddress.ip_address(packed)


def _is_non_public_ip(address):
    """判断地址是否不应被服务器端下载器访问。"""
    return not address.is_global


def _configured_blocked_hosts(runtime_config):
    blocked = runtime_config.get("DOWNLOAD_URL_BLOCKED_HOSTS", [])
    if isinstance(blocked, str):
        blocked = [blocked]
    if not isinstance(blocked, (list, tuple, set)):
        return []
    return [
        item.strip().lower()
        for item in blocked
        if isinstance(item, str) and item.strip()
    ]


def _matches_configured_block(hostname, address, blocked_hosts):
    for item in blocked_hosts:
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            network = None
        if network is not None:
            if address is not None and address in network:
                return True
            continue

        if item.startswith("*."):
            wildcard_host = _normalized_hostname(item[2:])
            if not wildcard_host:
                continue
            suffix = "." + wildcard_host
            if hostname.endswith(suffix):
                return True
            continue

        normalized_item = _normalized_hostname(item)
        if normalized_item and (
            hostname == normalized_item
            or hostname.endswith("." + normalized_item)
        ):
            return True
    return False


def validate_download_url(url, runtime_config=None):
    """校验下载地址，返回错误说明；通过时返回 ``None``。

    ``DOWNLOAD_URL_BLOCK_PRIVATE_NETWORKS`` 默认开启，并对域名做一次 DNS
    解析检查。DNS 解析失败不在这里阻断，让 yt-dlp 返回正常的网络错误；
    但只要解析结果中出现一个非公网地址，就拒绝整个 URL。
    """
    runtime_config = runtime_config or {}
    if not isinstance(url, str) or not url.strip():
        return "URL 为空"
    try:
        max_length = int(runtime_config.get(
            "DOWNLOAD_URL_MAX_LENGTH",
            DEFAULT_MAX_URL_LENGTH,
        ))
    except (TypeError, ValueError):
        max_length = DEFAULT_MAX_URL_LENGTH
    if len(url) > max_length:
        return "URL 过长"
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return "URL 包含控制字符"

    try:
        parsed = urlsplit(url.strip())
        hostname = _normalized_hostname(parsed.hostname)
        port = parsed.port
    except (TypeError, ValueError):
        return "URL 格式无效"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return "只允许 http 和 https URL"
    if not hostname:
        return "URL 缺少主机名"
    if parsed.username is not None or parsed.password is not None:
        return "不允许 URL 携带用户名或密码"

    blocked_hosts = _configured_blocked_hosts(runtime_config)
    address = _parse_ip_literal(hostname)
    block_private = runtime_config.get("DOWNLOAD_URL_BLOCK_PRIVATE_NETWORKS", True)
    if _matches_configured_block(hostname, address, blocked_hosts):
        return "URL 命中了禁止的主机或网段"
    if (
        hostname in _LOCAL_HOSTNAMES
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        return "URL 指向本地主机名"
    if address is not None:
        if block_private and _is_non_public_ip(address):
            return "URL 指向非公网 IP 地址"
        return None

    if not runtime_config.get("DOWNLOAD_URL_RESOLVE_HOSTS", True):
        return None

    try:
        resolved = socket.getaddrinfo(
            hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None

    addresses = set()
    for result in resolved:
        sockaddr = result[4]
        if not sockaddr:
            continue
        try:
            addresses.add(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue

    for resolved_address in addresses:
        if _matches_configured_block(hostname, resolved_address, blocked_hosts):
            return "URL 解析到了禁止的主机或网段"
        if block_private and _is_non_public_ip(resolved_address):
            return "URL 解析到了非公网 IP 地址"
    return None
