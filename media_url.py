"""清理视频链接里的跟踪参数。

分享出来的链接常带一长串来源统计参数（B 站的 `spm_id_from`、`vd_source`，
YouTube 的 `pp`、`si` 等）。它们对打开视频没有作用，却会让播放页的「原始链接」
和片尾二维码下面的文字长到看不清，也让同一个视频出现多个不同的 URL。

已知站点按白名单只保留有意义的参数；其他站点用通用黑名单，避免误删站内必需的
参数。
"""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 已知视频站：只保留这些参数，其余一律去掉
HOST_KEPT_PARAMS = {
    'youtube.com': {'v', 'list', 'index', 't', 'start'},
    'youtu.be': {'list', 'index', 't', 'start'},
    'bilibili.com': {'p', 't', 'start_progress'},
}

# 其他站点：只去掉这些常见的跟踪参数，其余保留
TRACKING_PARAMS = {
    'fbclid', 'gclid', 'igshid', 'mc_cid', 'mc_eid', 'msclkid',
    'ref', 'ref_src', 'ref_url', 'referrer', 'scene', 'share_medium',
    'share_plat', 'share_session_id', 'share_source', 'share_tag',
    'share_times', 'spm', 'spm_id_from', 'trackid', 'track_id',
    'unique_k', 'utm_campaign', 'utm_content', 'utm_medium', 'utm_name',
    'utm_source', 'utm_term', 'vd_source', 'yclid',
}
TRACKING_PREFIXES = ('utm_',)


def _host_rule(hostname):
    """按域名后缀匹配白名单，覆盖 m./www. 等各种子域。"""
    host = (hostname or '').lower().rstrip('.')
    for domain, kept in HOST_KEPT_PARAMS.items():
        if host == domain or host.endswith('.' + domain):
            return kept
    return None


def _is_tracking(name):
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def strip_tracking_params(url):
    """去掉跟踪参数后的链接；解析失败或不是 http(s) 时原样返回。"""
    if not isinstance(url, str) or not url.strip():
        return url
    candidate = url.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return candidate
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        return candidate
    if not parsed.query:
        return candidate

    kept_params = _host_rule(parsed.hostname)

    def keep(name):
        if kept_params is not None:
            return name.lower() in kept_params
        return not _is_tracking(name)

    # 保持原有顺序，只筛掉不需要的，避免改写出一个看起来完全不同的链接
    pairs = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if keep(name)
    ]
    query = urlencode(pairs)
    if query == parsed.query:
        return candidate
    return urlunparse(parsed._replace(query=query))
