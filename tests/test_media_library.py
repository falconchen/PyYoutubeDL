"""Unified routes must preserve deep links and isolate both players' DOM state."""
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append(dict(attrs))


def test_unified_routes_select_media_and_keep_unique_ids():
    with TemporaryDirectory() as files_dir:
        for name in ['video.mp4', 'audio.mp3', "quote's video.mp4"]:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir), patch('app.get_video_subtitle_tracks', return_value=[]):
            client = app_module.app.test_client()
            for path, selected in [
                ('/player', 'video'),
                ('/player?tab=audio', 'audio'),
                ('/audio-player?file=audio.mp3', 'audio'),
                ('/player?file=audio.mp3', 'audio'),
                ('/player?tab=audio&file=video.mp4', 'video'),
            ]:
                response = client.get(path)
                assert response.status_code == 200
                elements = Elements(response.get_data(as_text=True)).elements
                ids = [e['id'] for e in elements if 'id' in e]
                assert len(ids) == len(set(ids))
                by_id = {e['id']: e for e in elements if 'id' in e}
                assert by_id[selected + '-tab']['aria-selected'] == 'true'
                assert 'hidden' not in by_id[selected + '-panel']
                other = 'video' if selected == 'audio' else 'audio'
                assert 'hidden' in by_id[other + '-panel']
                # Names are data, never interpolated into executable JavaScript.
                quote_item = next(e for e in elements if e.get('data-filename') == "quote's video.mp4")
                assert "quote's" not in quote_item['onclick']


def test_empty_library_keeps_both_accessible_tabs():
    with TemporaryDirectory() as files_dir, patch('app.FILES_DIR', files_dir):
        html = app_module.app.test_client().get('/player?tab=audio').get_data(as_text=True)
        assert '暂无下载视频' in html
        assert '暂无下载音频' in html
        assert 'role="tablist"' in html


def test_small_screens_hide_library_intro():
    css = Path(app_module.app.static_folder, 'refresh.css').read_text(
        encoding='utf-8',
    )
    mobile_css = css.split('@media (max-width: 480px)', 1)[1]

    assert '.player-page .library-intro { display: none; }' in mobile_css
