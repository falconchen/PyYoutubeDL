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
        app_module._probe_media_metadata.cache_clear()
        app_module._probe_audio_metadata.cache_clear()
        app_module._probe_media_source_url.cache_clear()

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

    def test_extracts_safe_media_source_url_from_metadata(self):
        self.assertEqual(
            app_module.extract_media_source_url({
                'COMMENT': '来源 https://example.com/watch/123。',
            }),
            'https://example.com/watch/123',
        )
        self.assertEqual(
            app_module.extract_media_source_url({
                'purl': 'https://youtu.be/Hh3AmV46epI?t=20',
                'comment': 'https://example.com/fallback',
            }),
            'https://youtu.be/Hh3AmV46epI?t=20',
        )
        self.assertEqual(
            app_module.extract_media_source_url({
                'comment': 'javascript:alert(1)',
            }),
            '',
        )

    def test_audio_metadata_uses_ffprobe_tags_and_mime_type(self):
        probe_result = subprocess.CompletedProcess(
            args=['ffprobe'],
            returncode=0,
            stdout=json.dumps({
                'format': {
                    'tags': {
                        'title': '测试标题',
                        'artist': '测试作者',
                        'album': '测试专辑',
                        'date': '20260817',
                        'genre': '科技',
                        'description': '测试简介',
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
        self.assertEqual(metadata['album'], '测试专辑')
        self.assertEqual(metadata['date'], '2026-08-17')
        self.assertEqual(metadata['genre'], '科技')
        self.assertEqual(metadata['description'], '测试简介')
        self.assertEqual(
            metadata['source_url'],
            'https://www.youtube.com/watch?v=Hh3AmV46epI',
        )
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
        self.assertEqual(metadata['source_url'], '')
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
                    'album': 'Album',
                    'date': '2026-08-17',
                    'genre': 'Music',
                    'description': 'Description',
                    'source_url': 'https://example.com/original-audio',
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
        self.assertIn('href="https://example.com/original-audio"', html)
        self.assertIn('>原始链接 <i', html)
        self.assertIn('id="current-audio-source"', html)
        self.assertIn('sourceLink.href = sourceUrl;', html)
        self.assertIn('id="current-audio-metadata"', html)
        self.assertIn('<dd>Album</dd>', html)
        self.assertIn('href="/player"', html)
        self.assertLess(html.index('class="player-nav"'), html.index('class="player-content"'))
        self.assertNotIn('class="footer-actions', html)
        self.assertIn('aria-labelledby="lyrics-heading"\n                        hidden', html)

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
        self.assertIn('filter: blur(4px)', css)
        self.assertIn('transform: scale(1.08);', css)
        self.assertIn('bottom: 2.5rem;', css)
        self.assertIn("aria-hidden=\"true\"", template)
        self.assertIn('window.AudioContext || window.webkitAudioContext', template)
        self.assertIn('createMediaElementSource(', template)
        self.assertEqual(template.count('createMediaElementSource('), 1)
        self.assertIn('visualizerAnalyser.getByteFrequencyData(', template)
        self.assertIn('Math.min(54, height * 0.1)', template)
        self.assertNotIn('height * 0.14', template)
        self.assertIn('window.requestAnimationFrame(drawAudioVisualizer)', template)
        self.assertIn('window.cancelAnimationFrame(visualizerAnimationFrame)', template)
        self.assertIn('function resetAudioVisualizer()', template)
        self.assertIn('resetAudioVisualizer();', template)
        stop_body = template.split('function stopAudioVisualizer()', 1)[1].split(
            'function resetAudioVisualizer()',
            1,
        )[0]
        self.assertNotIn('clearAudioVisualizer();', stop_body)
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
        self.assertIn('player.el().appendChild(lyricsPanel);', template)
        self.assertIn('function parseLrcLyrics(text)', template)
        self.assertIn('function parseTimedTextLyrics(text)', template)
        self.assertIn('fetch(audioItem.lyrics.url', template)
        self.assertIn('function syncLyrics()', template)
        self.assertIn('function hideLyricsPanel()', template)
        self.assertIn('lyricsPanel.hidden = true;', template)
        self.assertIn('lyricsPanel.hidden = false;', template)
        self.assertIn('if (!audioItem || !audioItem.lyrics) {', template)
        self.assertIn('.audio-player-page .lyrics-panel[hidden] {', css)
        self.assertIn('player.currentTime(cue.time);', template)
        self.assertIn('loadLyrics(audioItem);', template)
        self.assertIn('.lyrics-line.active {', css)
        self.assertIn('.audio-player-page .lyrics-panel {', css)
        self.assertIn('z-index: 3;', css)
        lyrics_panel_rule = css.split(
            '.audio-player-page .lyrics-panel {',
            1,
        )[1].split('}', 1)[0]
        self.assertIn('top: 1%;', lyrics_panel_rule)
        self.assertIn('bottom: 42%;', lyrics_panel_rule)
        self.assertIn('border: none;', lyrics_panel_rule)
        self.assertNotIn('right:', lyrics_panel_rule)
        self.assertNotIn('left:', lyrics_panel_rule)
        self.assertNotIn('border-radius:', lyrics_panel_rule)
        self.assertNotIn('background:', lyrics_panel_rule)
        self.assertNotIn('box-shadow:', lyrics_panel_rule)
        self.assertNotIn('backdrop-filter:', lyrics_panel_rule)
        self.assertLess(
            template.index('id="audio-visualizer"'),
            template.index('class="lyrics-panel"'),
        )

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
