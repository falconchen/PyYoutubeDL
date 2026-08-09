import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import app
from config_util import DEFAULT_CONFIG


class TestAudioPlayerPage(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True
        app_module._probe_audio_metadata.cache_clear()

    def test_extracts_supported_youtube_video_urls(self):
        cases = {
            'https://www.youtube.com/watch?v=Hh3AmV46epI': 'Hh3AmV46epI',
            'https://youtu.be/Hh3AmV46epI?t=20': 'Hh3AmV46epI',
            'https://m.youtube.com/shorts/Hh3AmV46epI': 'Hh3AmV46epI',
            'https://youtube.com/live/Hh3AmV46epI?feature=share': 'Hh3AmV46epI',
            'https://www.youtube-nocookie.com/embed/Hh3AmV46epI': 'Hh3AmV46epI',
        }

        for source_url, expected in cases.items():
            with self.subTest(source_url=source_url):
                self.assertEqual(
                    app_module.extract_youtube_video_id(source_url),
                    expected,
                )

        self.assertIsNone(app_module.extract_youtube_video_id('https://example.com/video'))
        self.assertIsNone(app_module.extract_youtube_video_id('https://youtu.be/too-short'))
        self.assertIsNone(app_module.extract_youtube_video_id(None))
        self.assertEqual(
            app_module.extract_youtube_video_id_from_text(
                '参考 https://example.com/post，来源 '
                'https://youtube.com/watch?v=Hh3AmV46epI。'
            ),
            'Hh3AmV46epI',
        )

    def test_cover_candidates_use_youtube_then_configured_fallback(self):
        candidates = app_module.build_audio_cover_candidates(
            'Hh3AmV46epI',
            '/static/images/audio-cover-default.svg',
        )

        self.assertEqual(candidates, [
            'https://i.ytimg.com/vi/Hh3AmV46epI/maxresdefault.jpg',
            'https://i.ytimg.com/vi/Hh3AmV46epI/hqdefault.jpg',
            '/static/images/audio-cover-default.svg',
        ])

    def test_audio_metadata_uses_ffprobe_tags_and_mime_type(self):
        probe_result = subprocess.CompletedProcess(
            args=['ffprobe'],
            returncode=0,
            stdout=json.dumps({
                'format': {
                    'tags': {
                        'title': '测试标题',
                        'artist': '测试作者',
                        'purl': 'https://www.youtube.com/watch?v=Hh3AmV46epI',
                    },
                },
            }),
            stderr='',
        )

        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'test.mp3'
            Path(files_dir, filename).touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.subprocess.run', return_value=probe_result) as run,
            ):
                metadata = app_module.get_audio_metadata(
                    filename,
                    '/fallback.svg',
                )

        self.assertEqual(metadata['title'], '测试标题')
        self.assertEqual(metadata['artist'], '测试作者')
        self.assertEqual(metadata['mime_type'], 'audio/mpeg')
        self.assertEqual(
            metadata['cover_candidates'][0],
            'https://i.ytimg.com/vi/Hh3AmV46epI/maxresdefault.jpg',
        )
        self.assertEqual(run.call_count, 1)

    def test_audio_metadata_failure_falls_back_without_blocking_page(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'fallback-title.flac'
            Path(files_dir, filename).touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.subprocess.run',
                    side_effect=subprocess.TimeoutExpired('ffprobe', 15),
                ),
            ):
                metadata = app_module.get_audio_metadata(
                    filename,
                    '/fallback.svg',
                )

        self.assertEqual(metadata['title'], 'fallback-title')
        self.assertEqual(metadata['artist'], '')
        self.assertEqual(metadata['mime_type'], 'audio/flac')
        self.assertEqual(metadata['cover_candidates'], ['/fallback.svg'])

    def test_audio_page_filters_sorts_and_selects_requested_file(self):
        with tempfile.TemporaryDirectory() as files_dir:
            older = Path(files_dir, 'older.mp3')
            requested = Path(files_dir, 'requested song.m4a')
            ignored_video = Path(files_dir, 'video.mp4')
            older.touch()
            requested.touch()
            ignored_video.touch()
            older.touch()

            def metadata(filename, fallback_url):
                extension = Path(filename).suffix.lower()
                return {
                    'title': Path(filename).stem,
                    'artist': 'Artist',
                    'mime_type': 'audio/mp4' if extension == '.m4a' else 'audio/mpeg',
                    'cover_candidates': [fallback_url],
                }

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_audio_metadata', side_effect=metadata),
                patch.dict(
                    app_module.config,
                    {'AUDIO_PLAYER_FALLBACK_COVER_URL': '/fallback.svg'},
                ),
            ):
                response = self.client.get(
                    '/audio-player',
                    query_string={'file': requested.name},
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<source src="/files/requested%20song.m4a" type="audio/mp4">', html)
        self.assertLess(html.index('requested song'), html.index('older.mp3'))
        self.assertNotIn('video.mp4', html)

    def test_audio_page_matches_preferred_sidecar_lyrics(self):
        with tempfile.TemporaryDirectory() as files_dir:
            audio = Path(files_dir, 'song.mp3')
            Path(files_dir, 'song.en.srt').write_text(
                '1\n00:00:00,000 --> 00:00:01,000\nEnglish\n',
                encoding='utf-8',
            )
            preferred = Path(files_dir, 'song.zh-Hans.srt')
            preferred.write_text(
                '1\n00:00:00,000 --> 00:00:01,000\n中文\n',
                encoding='utf-8',
            )
            audio.touch()

            with app.test_request_context(), patch('app.FILES_DIR', files_dir):
                lyrics = app_module.find_audio_lyrics(
                    audio.name,
                    ['zh-Hans', 'en-US'],
                )

        self.assertEqual(lyrics['filename'], preferred.name)
        self.assertEqual(lyrics['format'], 'srt')
        self.assertIn('/files/song.zh-Hans.srt', lyrics['url'])

    def test_audio_page_prefers_neutral_lrc_lyrics(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'song.mp3').touch()
            Path(files_dir, 'song.zh-Hans.srt').touch()
            neutral = Path(files_dir, 'song.lrc')
            neutral.touch()

            with app.test_request_context(), patch('app.FILES_DIR', files_dir):
                lyrics = app_module.find_audio_lyrics('song.mp3', ['zh-CN'])

        self.assertEqual(lyrics['filename'], neutral.name)
        self.assertEqual(lyrics['format'], 'lrc')

    def test_audio_page_matches_language_family(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'song.mp3').touch()
            english = Path(files_dir, 'song.en.srt')
            english.touch()
            Path(files_dir, 'song.zh-Hans.srt').touch()

            with app.test_request_context(), patch('app.FILES_DIR', files_dir):
                lyrics = app_module.find_audio_lyrics('song.mp3', ['en-US'])

        self.assertEqual(lyrics['filename'], english.name)

    def test_audio_player_uses_poster_mode_and_race_safe_fallback(self):
        template = Path(app.template_folder, 'audio_player.html').read_text(
            encoding='utf-8',
        )

        self.assertIn('audioPosterMode: true', template)
        self.assertIn('pictureInPictureToggle: false', template)
        self.assertIn('fullscreenToggle: false', template)
        self.assertIn('var posterRequestSerial = 0;', template)
        self.assertIn('var requestSerial = ++posterRequestSerial;', template)
        self.assertIn('image.onerror = function () {', template)
        self.assertIn('tryCandidate(index + 1);', template)
        self.assertIn("player.poster(fallbackCoverUrl);", template)

    def test_audio_player_renders_real_audio_spectrum_visualizer(self):
        template = Path('templates/audio_player.html').read_text(encoding='utf-8')
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('id="audio-visualizer"', template)
        self.assertIn('id="audio-cover-gradient"', template)
        self.assertIn('player.el().appendChild(coverGradient);', template)
        self.assertIn("aria-hidden=\"true\"", template)
        self.assertIn('window.AudioContext || window.webkitAudioContext', template)
        self.assertIn('createMediaElementSource(', template)
        self.assertEqual(template.count('createMediaElementSource('), 1)
        self.assertIn('visualizerAnalyser.getByteFrequencyData(', template)
        self.assertIn('window.requestAnimationFrame(drawAudioVisualizer)', template)
        self.assertIn('window.cancelAnimationFrame(visualizerAnimationFrame)', template)
        self.assertIn("player.on('play', startAudioVisualizer);", template)
        self.assertIn("player.on('pause', stopAudioVisualizer);", template)
        self.assertIn("document.addEventListener('visibilitychange'", template)
        self.assertIn("window.matchMedia('(prefers-reduced-motion: reduce)')", template)
        self.assertIn('pointer-events: none;', css)
        self.assertIn('.audio-cover-gradient {', css)
        self.assertIn('rgba(10, 15, 28, 0.78) 100%', css)
        self.assertIn('@media (prefers-reduced-motion: reduce)', css)

    def test_audio_player_reuses_playback_download_and_auto_next_behaviors(self):
        template = Path(app.template_folder, 'audio_player.html').read_text(
            encoding='utf-8',
        )

        self.assertIn("controlBar.addChild('DownloadButton'", template)
        self.assertIn('link.download = currentFilename;', template)
        self.assertIn("player.on('ended', function () {", template)
        self.assertIn("player.on('loadedmetadata', restoreCurrentAudioProgress);", template)
        self.assertIn("player.on('pause', saveCurrentAudioProgress);", template)
        self.assertIn("window.addEventListener('beforeunload', saveCurrentAudioProgress);", template)
        self.assertLess(
            template.index('playlist-section'),
            template.index('audio-comment-container'),
        )

    def test_audio_player_loads_and_synchronizes_sidecar_lyrics(self):
        template = Path(app.template_folder, 'audio_player.html').read_text(
            encoding='utf-8',
        )
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('id="lyrics-content"', template)
        self.assertIn('function parseLrcLyrics(text)', template)
        self.assertIn('function parseTimedTextLyrics(text)', template)
        self.assertIn('fetch(audioItem.lyrics.url', template)
        self.assertIn('function syncLyrics()', template)
        self.assertIn('player.currentTime(cue.time);', template)
        self.assertIn('loadLyrics(audioItem);', template)
        self.assertIn('.lyrics-line.active {', css)

    def test_default_cover_config_and_asset_exist(self):
        self.assertEqual(
            DEFAULT_CONFIG['AUDIO_PLAYER_FALLBACK_COVER_URL'],
            '/static/images/audio-cover-default.svg',
        )
        cover_path = Path(app.static_folder, 'images', 'audio-cover-default.svg')
        self.assertTrue(cover_path.is_file())
        self.assertIn('viewBox="0 0 1600 900"', cover_path.read_text(encoding='utf-8'))

    def test_home_page_links_both_players(self):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/player"', html)
        self.assertIn('href="/audio-player"', html)


if __name__ == '__main__':
    unittest.main()
