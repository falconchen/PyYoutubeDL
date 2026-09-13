import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from auth_helper import install_auth
import ai_summary_store
from app import app
from config_util import DEFAULT_CONFIG


class TestAudioPlayerPage(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True
        install_auth(self)
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

    def test_media_list_filters_and_sorts_audio_by_mtime(self):
        with tempfile.TemporaryDirectory() as files_dir:
            older = Path(files_dir, 'older.mp3')
            newer = Path(files_dir, 'requested song.m4a')
            ignored_video = Path(files_dir, 'video.mp4')
            older.touch()
            ignored_video.touch()
            newer.touch()

            def metadata(filename, fallback_url):
                return {
                    'title': Path(filename).stem,
                    'artist': 'Artist',
                    'source_url': 'https://example.com/original-audio',
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
                response = self.client.get('/api/media_list')

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        # 最近修改的排在前面，视频不会混进音频列表。
        self.assertEqual(
            [item['filename'] for item in payload['audio']],
            ['requested song.m4a', 'older.mp3'],
        )
        self.assertEqual(
            [item['filename'] for item in payload['video']],
            ['video.mp4'],
        )
        first = payload['audio'][0]
        self.assertEqual(first['url'], '/files/requested%20song.m4a')
        self.assertEqual(first['source_url'], 'https://example.com/original-audio')
        self.assertEqual(first['artist'], 'Artist')
        self.assertEqual(first['poster'], '/fallback.svg')

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

    def test_audio_ai_summary_creates_local_sidecar_job(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            files_dir = root_path / 'files'
            files_dir.mkdir()
            (files_dir / 'song.mp3').touch()
            (files_dir / 'song.en.srt').touch()
            db_path = str(root_path / 'summary.sqlite3')
            ai_summary_store.init_db(db_path)
            with (
                patch('app.FILES_DIR', str(files_dir)),
                patch('app.get_media_source_url', return_value=''),
                patch.dict(app_module.config, {
                    'AI_SUMMARY_DB_PATH': db_path,
                    'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                    'AI_API_MODEL': 'test-model',
                    'AI_API_TOKEN': 'test-token',
                }),
            ):
                response = self.client.post('/api/ai_summary', json={
                    'filename': 'song.mp3',
                    'subtitle_source': 'sidecar',
                    'subtitle_filename': 'song.en.srt',
                    'stream_index': None,
                })

            job = ai_summary_store.get_job(db_path, response.get_json()['job_id'])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(job['filename'], 'song.mp3')
        self.assertEqual(job['subtitle_source'], 'sidecar')
        self.assertEqual(job['subtitle_filename'], 'song.en.srt')

    def test_default_cover_config_and_asset_exist(self):
        self.assertEqual(
            DEFAULT_CONFIG['AUDIO_PLAYER_FALLBACK_COVER_URL'],
            '/static/images/audio-cover-default.svg',
        )
        cover_path = Path(app.static_folder, 'images', 'audio-cover-default.svg')
        self.assertTrue(cover_path.is_file())
        self.assertIn('viewBox="0 0 1600 900"', cover_path.read_text(encoding='utf-8'))

    def test_home_page_switches_between_downloader_and_library(self):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/player"', html)
        self.assertIn('>媒体库<', html)
        self.assertIn('>下载器<', html)
        self.assertIn('data-view="library"', html)


if __name__ == '__main__':
    unittest.main()
