"""链接清理：已知视频站按白名单保留参数，其他站点只去掉常见跟踪参数。"""
import media_url

strip = media_url.strip_tracking_params


def test_bilibili_share_link_drops_tracking_but_keeps_page_and_time():
    assert strip(
        'https://www.bilibili.com/video/BV1pdet6MEBD'
        '?trackid=web_pegasus_0.router-web-pegasus-2479516&spm_id_from=333.1007'
    ) == 'https://www.bilibili.com/video/BV1pdet6MEBD'

    assert strip(
        'https://www.bilibili.com/video/BV1xx411c7mD?p=3&t=120&vd_source=abc'
    ) == 'https://www.bilibili.com/video/BV1xx411c7mD?p=3&t=120'


def test_youtube_keeps_video_playlist_and_start_time():
    assert strip(
        'https://www.youtube.com/watch?v=abc&list=PL123&index=2&t=30s&si=xyz'
    ) == 'https://www.youtube.com/watch?v=abc&list=PL123&index=2&t=30s'

    assert strip(
        'https://www.youtube.com/watch?v=PgYvR3iD_ls&pp=0gcJCR4MAYcqIYzv'
    ) == 'https://www.youtube.com/watch?v=PgYvR3iD_ls'

    assert strip('https://youtu.be/abcdefg?si=xyz&t=45') == 'https://youtu.be/abcdefg?t=45'


def test_subdomains_follow_the_same_rule():
    assert strip(
        'https://m.bilibili.com/video/BV1xx411c7mD?spm_id_from=333.1007'
    ) == 'https://m.bilibili.com/video/BV1xx411c7mD'


def test_unknown_host_only_loses_known_trackers():
    """不认识的站点不能按白名单删，站内参数可能是打开视频必需的。"""
    assert strip(
        'https://example.com/video?id=7&utm_source=weibo&page=2'
    ) == 'https://example.com/video?id=7&page=2'


def test_links_without_query_or_tracking_are_returned_unchanged():
    for url in (
        'https://www.youtube.com/shorts/pJWK-ZExAbc',
        'https://example.com/video?id=7',
        'not a url',
        '',
    ):
        assert strip(url) == url
