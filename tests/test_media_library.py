"""媒体库：入口路由保留深链，列表由 /api/media_list 提供。"""
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module
import user_store
from auth_helper import logged_in_client, other_user


def bootstrap_payload(html):
    """取出模板写入的引导 JSON，前端据此决定初始视图。"""
    match = re.search(
        r'<script id="dl-bootstrap" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    assert match, '缺少 dl-bootstrap'
    return json.loads(match.group(1))


def test_library_routes_carry_view_tab_and_file():
    with logged_in_client() as (client, _user, _db):
        _assert_routes(client)


def _assert_routes(client):
    cases = [
        ('/', 'download', 'video', ''),
        ('/?view=library', 'library', 'video', ''),
        ('/?view=library&tab=audio', 'library', 'audio', ''),
        ('/player', 'library', 'video', ''),
        ('/player?tab=audio', 'library', 'audio', ''),
        ('/audio-player', 'library', 'audio', ''),
        ('/audio-player?file=audio.mp3', 'library', 'audio', 'audio.mp3'),
        ('/player?file=video.mp4', 'library', 'video', 'video.mp4'),
    ]
    for path, view, tab, requested in cases:
        response = client.get(path)
        assert response.status_code == 200, path
        payload = bootstrap_payload(response.get_data(as_text=True))
        assert payload['view'] == view, path
        assert payload['tab'] == tab, path
        assert payload['file'] == requested, path


def test_requested_file_wins_over_tab():
    """已完成任务的直达链接要能覆盖 tab，逻辑在 dropload.js 的 findMedia 中。"""
    script = Path(app_module.app.static_folder, 'dropload.js').read_text(
        encoding='utf-8',
    )
    assert 'function findMedia(filename)' in script
    assert 'if (found) libraryTab = type;' in script
    assert 'var target = findMedia(preferredFile);' in script


def test_media_list_splits_video_and_audio():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in ['video.mp4', 'audio.mp3', "quote's video.mp4"]:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir):
            response = client.get('/api/media_list')
            payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert {item['filename'] for item in payload['video']} == {
        'video.mp4',
        "quote's video.mp4",
    }
    assert [item['filename'] for item in payload['audio']] == ['audio.mp3']
    for item in payload['video'] + payload['audio']:
        assert item['url'].startswith('/files/')
        assert item['download_url'].startswith('/downloads/')
        assert item['title']
        assert item['thumbnail_candidates']
        assert item['thumbnail_candidates'][-1].endswith(
            '/static/images/media-cover-default.svg'
        )


def test_media_list_applies_exclude_keywords():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in ['keep.mp4', 'skip-me.mp4']:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir), patch.dict(
            app_module.config,
            {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['skip']},
        ):
            payload = client.get('/api/media_list').get_json()

    assert [item['filename'] for item in payload['video']] == ['keep.mp4']


def test_empty_library_returns_empty_lists():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        with patch('app.FILES_DIR', files_dir):
            payload = client.get('/api/media_list').get_json()

    assert payload['success'] is True
    assert payload['video'] == []
    assert payload['audio'] == []


def test_library_panel_has_accessible_tabs():
    with logged_in_client() as (client, _user, _db):
        html = client.get('/player').get_data(as_text=True)

    assert 'role="tablist"' in html
    assert 'data-tab="video"' in html
    assert 'data-tab="audio"' in html


def test_media_list_only_returns_own_files():
    """媒体私有：他人下载的文件不应出现在自己的媒体库里。"""
    with logged_in_client() as (client, user, db_path), TemporaryDirectory() as files_dir:
        Path(files_dir, 'mine.mp4').touch()
        Path(files_dir, 'theirs.mp4').touch()
        stranger = other_user(db_path)
        user_store.record_media(db_path, 'mine.mp4', user['id'])
        user_store.record_media(db_path, 'theirs.mp4', stranger['id'])

        with patch('app.FILES_DIR', files_dir):
            payload = client.get('/api/media_list').get_json()

    assert [item['filename'] for item in payload['video']] == ['mine.mp4']


def test_serving_another_users_file_is_not_found():
    with logged_in_client() as (client, _user, db_path), TemporaryDirectory() as files_dir:
        Path(files_dir, 'theirs.mp4').write_bytes(b'x')
        stranger = other_user(db_path)
        user_store.record_media(db_path, 'theirs.mp4', stranger['id'])

        with patch('app.FILES_DIR', files_dir):
            played = client.get('/files/theirs.mp4')
            downloaded = client.get('/downloads/theirs.mp4')

    # 用 404 而不是 403，避免泄露「该文件存在」
    assert played.status_code == 404
    assert downloaded.status_code == 404


def test_anonymous_can_open_pages_and_an_empty_private_library():
    """匿名页面和媒体接口开放，但只返回当前匿名会话的资源。"""
    app_module.app.testing = True
    client = app_module.app.test_client()

    assert client.get('/').status_code == 200
    assert client.get('/player').status_code == 200
    payload = client.get('/api/media_list').get_json()
    assert payload['success'] is True
    assert payload['video'] == []
    assert payload['audio'] == []
