import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import app


class TestPlayerPage(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True

    def test_download_control_is_rendered_for_current_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'test video.mp4'
            Path(files_dir, filename).touch()

            with patch('app.FILES_DIR', files_dir):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("videojs.registerComponent('DownloadButton'", html)
        self.assertIn("controlBar.addChild('DownloadButton'", html)
        self.assertIn("this.addClass('vjs-download-control')", html)
        self.assertIn('var currentFilename = "test video.mp4";', html)
        self.assertIn('link.download = currentFilename;', html)
        self.assertLess(html.index('video-js.min.css'), html.index('player.css'))

    def test_download_icon_uses_centered_control_box(self):
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('cursor: pointer;', css)
        self.assertIn('align-items: center;', css)
        self.assertIn('justify-content: center;', css)
        self.assertIn('font-size: 1.3em;', css)
        self.assertIn('line-height: 1;', css)

    def test_file_parameter_selects_requested_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            first = Path(files_dir, 'first.mp4')
            requested = Path(files_dir, 'requested video.mp4')
            first.touch()
            requested.touch()
            first.touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get(
                    '/player',
                    query_string={'file': requested.name},
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'var currentFilename = "requested video.mp4";',
            html,
        )
        self.assertIn(
            '正在播放: requested video.mp4',
            html,
        )

    def test_file_parameter_ignores_unknown_and_non_mp4_paths(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'available.mp4'
            Path(files_dir, filename).touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get(
                    '/player',
                    query_string={'file': '../config.json'},
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('var currentFilename = "available.mp4";', html)

    def test_switch_video_updates_file_parameter_in_url(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'first.mp4').touch()
            Path(files_dir, 'second video.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("url.searchParams.set('file', filename);", html)
        self.assertIn(
            "window.history.replaceState({ filename: filename }, '', url);",
            html,
        )
        self.assertIn('updatePlayerUrl(filename);', html)
        self.assertLess(
            html.index('currentFilename = filename;'),
            html.index('updatePlayerUrl(filename);'),
        )

    def test_player_saves_and_restores_progress_per_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'first.mp4').touch()
            Path(files_dir, 'second.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "var playbackProgressPrefix = 'pyyoutubedl:playback-progress:';",
            html,
        )
        self.assertIn(
            'localStorage.setItem(\n'
            '                    playbackProgressKey(currentFilename),',
            html,
        )
        self.assertIn(
            "player.on('loadedmetadata', restoreCurrentVideoProgress);",
            html,
        )
        self.assertIn("player.on('timeupdate', function () {", html)
        self.assertIn(
            "player.on('pause', saveCurrentVideoProgress);",
            html,
        )
        self.assertIn(
            "window.addEventListener('beforeunload', saveCurrentVideoProgress);",
            html,
        )
        self.assertIn('clearVideoProgress(currentFilename);', html)
        self.assertIn(
            'switchVideo(nextSrc, nextFile, items[currentIndex], false);',
            html,
        )
        switch_start = html.index(
            'function switchVideo(src, filename, element, savePrevious)'
        )
        source_update = html.index(
            'player.src({ type: "video/mp4", src: src });',
            switch_start,
        )
        self.assertLess(
            html.index('saveCurrentVideoProgress();', switch_start),
            source_update,
        )

    def test_player_filters_filenames_by_configured_keywords(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '保留的视频.mp4').touch()
            Path(files_dir, '包含预告的预告片.mp4').touch()
            Path(files_dir, 'sample-preview.mp4').touch()
            Path(files_dir, '不是视频.mp3').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['预告', 'preview', '']},
                ),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('保留的视频.mp4', html)
        self.assertNotIn('包含预告的预告片.mp4', html)
        self.assertNotIn('sample-preview.mp4', html)

    def test_player_ignores_invalid_keyword_config(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '正常视频.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': '不是数组'},
                ),
            ):
                response = self.client.get('/player')

        self.assertEqual(response.status_code, 200)
        self.assertIn('正常视频.mp4', response.get_data(as_text=True))

    def test_player_loads_embedded_subtitles_as_webvtt_tracks(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()
            subtitles = [
                {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                {'stream_index': 3, 'language': 'zh-Hant', 'label': '繁体中文'},
            ]

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=subtitles),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('kind="subtitles"', html)
        self.assertIn('srclang="zh-Hans" label="简体中文"', html)
        self.assertIn('srclang="zh-Hant" label="繁体中文"', html)
        self.assertIn('/subtitles/', html)
        self.assertIn('player.addRemoteTextTrack({', html)
        self.assertIn('player.removeRemoteTextTrack(currentTracks[index]);', html)

    def test_probe_distinguishes_simplified_and_traditional_chinese_tracks(self):
        probe_result = subprocess.CompletedProcess(
            args=['ffprobe'],
            returncode=0,
            stdout='''{
                "streams": [
                    {"index": 2, "tags": {"language": "zho"}},
                    {"index": 3, "tags": {"language": "zho"}}
                ]
            }''',
            stderr='',
        )

        app_module._probe_embedded_subtitles.cache_clear()
        with patch('app.subprocess.run', return_value=probe_result):
            subtitles = app_module._probe_embedded_subtitles(
                '/tmp/video-with-chinese-subs.mp4',
                1,
                1,
            )

        self.assertEqual(
            subtitles,
            (
                {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                {'stream_index': 3, 'language': 'zh-Hant', 'label': '繁体中文'},
            ),
        )

    def test_subtitle_route_converts_valid_stream_to_webvtt(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()
            completed = subprocess.CompletedProcess(
                args=['ffmpeg'],
                returncode=0,
                stdout='WEBVTT\n\n00:00.000 --> 00:01.000\n测试\n'.encode(),
                stderr=b'',
            )

            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[{'stream_index': 2, 'language': 'zh', 'label': '中文'}],
                ),
                patch('app.subprocess.run', return_value=completed) as run,
            ):
                response = self.client.get('/subtitles/%E5%B8%A6%E5%AD%97%E5%B9%95.mp4/2.vtt')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/vtt')
        self.assertTrue(response.data.startswith(b'WEBVTT'))
        self.assertIn('0:2', run.call_args.args[0])

    def test_subtitle_route_rejects_unknown_stream(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/subtitles/%E5%B8%A6%E5%AD%97%E5%B9%95.mp4/99.vtt')

        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
