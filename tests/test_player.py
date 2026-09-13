import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module
import ai_summary_store
from app import app


class TestPlayerPage(unittest.TestCase):
    def setUp(self):
        self.summary_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.summary_temp_dir.cleanup)
        self.summary_db_path = str(
            Path(self.summary_temp_dir.name) / 'ai-summaries.sqlite3'
        )
        self.config_patcher = patch.dict(
            app_module.config,
            {'AI_SUMMARY_DB_PATH': self.summary_db_path},
        )
        self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)
        ai_summary_store.init_db(self.summary_db_path)
        self.client = app.test_client()
        app.testing = True
        app_module._probe_media_metadata.cache_clear()
        app_module._probe_media_source_url.cache_clear()
        self.source_url_patcher = patch('app.get_media_source_url', return_value='')
        self.source_url_patcher.start()
        self.addCleanup(self.source_url_patcher.stop)

    def test_video_source_url_probe_reads_purl_or_comment_tags(self):
        source_url = 'https://example.com/original-video'
        probe_result = subprocess.CompletedProcess(
            args=['ffprobe'],
            returncode=0,
            stdout=(
                '{"format":{"tags":{"comment":"'
                + source_url
                + '"}}}'
            ),
            stderr='',
        )
        app_module._probe_media_source_url.cache_clear()

        with patch('app.subprocess.run', return_value=probe_result) as run:
            result = app_module._probe_media_source_url('/tmp/video.mp4', 1, 2)

        self.assertEqual(result, source_url)
        command = run.call_args.args[0]
        self.assertTrue(any(
            'title,artist,album,date,genre,description,synopsis,purl,comment'
            in argument
            for argument in command
        ))

    def test_sidecar_subtitles_are_strictly_matched_sorted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as files_dir:
            for filename in (
                'video.mp4',
                'video.srt',
                'video.zh-Hans.srt',
                'video.zh-Hans.vtt',
                'video.zh-Hant.ass',
                'video.en.ttml',
                'video.ja.ssa',
                'video.lrc',
                'video-extra.zh-Hans.srt',
            ):
                Path(files_dir, filename).touch()

            with patch('app.FILES_DIR', files_dir):
                tracks = app_module.get_sidecar_subtitles('video.mp4')

        self.assertEqual(
            [track['subtitle_filename'] for track in tracks],
            [
                'video.srt',
                'video.zh-Hans.vtt',
                'video.zh-Hant.ass',
                'video.en.ttml',
                'video.ja.ssa',
            ],
        )
        self.assertEqual(
            [track['language'] for track in tracks],
            ['und', 'zh-Hans', 'zh-Hant', 'en', 'ja'],
        )

    def test_ai_summary_requires_server_configuration(self):
        with patch.dict(
            app_module.config,
            {'AI_API_BASE_URL': '', 'AI_API_MODEL': '', 'AI_API_TOKEN': ''},
        ):
            response = self.client.post(
                '/api/ai_summary',
                json={'filename': 'video.mp4', 'stream_index': 2},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['message'], 'AI 总结尚未完成配置')

    def test_ai_summary_rejects_video_without_subtitles(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '无字幕.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                response = self.client.post(
                    '/api/ai_summary',
                    json={'filename': '无字幕.mp4'},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['message'], '当前媒体没有可用字幕或歌词')

    def test_ai_summary_creates_and_reuses_async_local_job(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '带字幕.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[
                        {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                    ],
                ),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                first = self.client.post(
                    '/api/ai_summary',
                    json={'filename': '带字幕.mp4', 'stream_index': 2},
                )
                second = self.client.post(
                    '/api/ai_summary',
                    json={'filename': '带字幕.mp4', 'stream_index': 2},
                )

        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.get_json()['cached'])
        self.assertEqual(first.get_json()['status'], 'queued')
        self.assertEqual(first.get_json()['job_id'], second.get_json()['job_id'])

    def test_ai_summary_creates_sidecar_job_for_selected_track(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'video.mp4').touch()
            Path(files_dir, 'video.en.srt').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                response = self.client.post(
                    '/api/ai_summary',
                    json={
                        'filename': 'video.mp4',
                        'subtitle_source': 'sidecar',
                        'subtitle_filename': 'video.en.srt',
                        'stream_index': None,
                    },
                )

        self.assertEqual(response.status_code, 202)
        job = ai_summary_store.get_job(
            self.summary_db_path,
            response.get_json()['job_id'],
        )
        self.assertEqual(job['subtitle_source'], 'sidecar')
        self.assertEqual(job['subtitle_filename'], 'video.en.srt')
        self.assertIsNone(job['stream_index'])

    def test_ai_summary_rejects_unrelated_sidecar_filename(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'video.mp4').touch()
            Path(files_dir, 'video.en.srt').touch()
            Path(files_dir, 'other.en.srt').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                response = self.client.post(
                    '/api/ai_summary',
                    json={
                        'filename': 'video.mp4',
                        'subtitle_source': 'sidecar',
                        'subtitle_filename': 'other.en.srt',
                    },
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['message'], '所选字幕不存在')

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

    def test_sidecar_subtitle_route_converts_valid_file_to_webvtt(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'video.mp4').touch()
            Path(files_dir, 'video.zh-Hans.srt').touch()
            completed = subprocess.CompletedProcess(
                args=['ffmpeg'],
                returncode=0,
                stdout=b'WEBVTT\n\n00:00.000 --> 00:01.000\ntext\n',
                stderr=b'',
            )
            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.subprocess.run', return_value=completed) as run,
            ):
                response = self.client.get(
                    '/subtitles/sidecar/video.zh-Hans.srt.vtt'
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/vtt')
        self.assertTrue(any(
            argument.endswith('video.zh-Hans.srt')
            for argument in run.call_args.args[0]
        ))

    def test_sidecar_subtitle_route_rejects_unrelated_file(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'video.mp4').touch()
            Path(files_dir, 'other.srt').touch()
            with patch('app.FILES_DIR', files_dir):
                response = self.client.get('/subtitles/sidecar/other.srt.vtt')

        self.assertEqual(response.status_code, 404)

    def test_sidecar_subtitle_route_reports_conversion_failure(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'video.mp4').touch()
            Path(files_dir, 'video.srt').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.subprocess.run',
                    side_effect=subprocess.CalledProcessError(1, ['ffmpeg']),
                ),
            ):
                response = self.client.get('/subtitles/sidecar/video.srt.vtt')

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_data(as_text=True), 'subtitle conversion failed')


    def test_media_list_title_hides_numeric_datetime_prefix(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '08221544-example-720.mp4').touch()
            Path(files_dir, 'abcdefgh-example-720.mp4').touch()

            with patch('app.FILES_DIR', files_dir):
                response = self.client.get('/api/media_list')

        titles = {
            item['filename']: item['title']
            for item in response.get_json()['video']
        }
        self.assertEqual(response.status_code, 200)
        # 只有纯数字前缀会被隐藏，文件名本身不变。
        self.assertEqual(titles['08221544-example-720.mp4'], 'example-720.mp4')
        self.assertEqual(
            titles['abcdefgh-example-720.mp4'],
            'abcdefgh-example-720.mp4',
        )

    def test_media_list_filters_filenames_by_configured_keywords(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'keep-me.mp4').touch()
            Path(files_dir, 'sample-trailer.mp4').touch()
            Path(files_dir, 'sample-trailer.mp3').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['trailer']},
                ),
            ):
                payload = self.client.get('/api/media_list').get_json()

        self.assertEqual(
            [item['filename'] for item in payload['video']],
            ['keep-me.mp4'],
        )
        self.assertEqual(payload['audio'], [])

    def test_media_list_ignores_invalid_keyword_config(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'keep-me.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': 'trailer'},
                ),
            ):
                payload = self.client.get('/api/media_list').get_json()

        # 配置非法时退回不过滤，而不是整页失败。
        self.assertEqual(
            [item['filename'] for item in payload['video']],
            ['keep-me.mp4'],
        )


if __name__ == '__main__':
    unittest.main()
