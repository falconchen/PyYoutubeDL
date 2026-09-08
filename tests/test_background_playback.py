"""Render native audio mode and preserve byte-range delivery for seeking."""
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module


class MediaElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.by_id = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.by_id[attrs['id']] = (tag, attrs)


def test_video_listen_mode_uses_audio_and_preserves_deep_link():
    with TemporaryDirectory() as files_dir:
        for name in ['first.mp4', 'second.mp4']:
            Path(files_dir, name).touch()
        with (
            patch('app.FILES_DIR', files_dir),
            patch('app.get_video_subtitle_tracks', return_value=[]),
            patch('app.get_video_metadata', return_value={'title': '测试视频'}),
        ):
            client = app_module.app.test_client()
            for query, expected_tag in [('', 'video'), ('&listen=1', 'audio')]:
                response = client.get('/player?tab=video&file=first.mp4' + query)
                assert response.status_code == 200
                html = response.get_data(as_text=True)
                elements = MediaElements(html).by_id
                assert elements['video-player'][0] == expected_tag
                assert elements['audio-player'][0] == 'audio'
                assert elements['background-listen-toggle'][1]['aria-pressed'] == str(expected_tag == 'audio').lower()
                assert 'var currentFilename = "first.mp4";' in html
                assert 'background-playback.js' in html


def test_media_range_request_keeps_seek_support():
    with TemporaryDirectory() as files_dir, patch('app.FILES_DIR', files_dir):
        Path(files_dir, 'sample.mp4').write_bytes(b'0123456789')
        response = app_module.app.test_client().get('/files/sample.mp4', headers={'Range': 'bytes=3-6'})
        try:
            assert response.status_code == 206
            assert response.data == b'3456'
            assert response.headers['Content-Range'] == 'bytes 3-6/10'
        finally:
            response.close()
