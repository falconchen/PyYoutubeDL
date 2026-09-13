"""媒体库：入口路由保留深链，列表由 /api/media_list 提供。"""
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module


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
    client = app_module.app.test_client()
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
        assert response.status_code == 200
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
    with TemporaryDirectory() as files_dir:
        for name in ['video.mp4', 'audio.mp3', "quote's video.mp4"]:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir):
            response = app_module.app.test_client().get('/api/media_list')

    assert response.status_code == 200
    payload = response.get_json()
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


def test_media_list_applies_exclude_keywords():
    with TemporaryDirectory() as files_dir:
        for name in ['keep.mp4', 'skip-me.mp4']:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir), patch.dict(
            app_module.config,
            {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['skip']},
        ):
            response = app_module.app.test_client().get('/api/media_list')

    assert [item['filename'] for item in response.get_json()['video']] == ['keep.mp4']


def test_empty_library_returns_empty_lists():
    with TemporaryDirectory() as files_dir, patch('app.FILES_DIR', files_dir):
        payload = app_module.app.test_client().get('/api/media_list').get_json()

    assert payload['success'] is True
    assert payload['video'] == []
    assert payload['audio'] == []


def test_library_panel_has_accessible_tabs():
    html = app_module.app.test_client().get('/player').get_data(as_text=True)

    assert 'role="tablist"' in html
    assert 'data-tab="video"' in html
    assert 'data-tab="audio"' in html
