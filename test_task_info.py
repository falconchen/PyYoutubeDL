import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class TestTaskInfoAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.urls_dir = root / 'urls'
        self.logs_dir = root / 'logs'
        self.urls_dir.mkdir()
        self.logs_dir.mkdir()
        self.client = app.app.test_client()
        app.app.testing = True

        self.patches = [
            patch.object(app, 'URLS_DIR', str(self.urls_dir)),
            patch.dict(app.config, {'LOG_DIR': str(self.logs_dir)}),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def write_task(self, task_id, extension):
        (self.urls_dir / f'{task_id}{extension}').write_text(
            'https://example.com/video',
            encoding='utf-8',
        )

    def test_reports_queued_task(self):
        task_id = 'v20260723120000AbC'
        self.write_task(task_id, '.txt')

        response = self.client.post('/api/task_info', json={'tasks': [task_id]})
        task = response.get_json()['tasks'][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(task['state'], 'queued')
        self.assertEqual(task['progress']['percent'], 0)

    def test_reports_structured_download_progress(self):
        task_id = 'a20260723120000XyZ'
        self.write_task(task_id, '.downloading')
        (self.logs_dir / f'{task_id}.log').write_text(
            (
                'PYDL_PROGRESS|downloading| 42.5%|4.25MiB|10.00MiB|'
                '1.50MiB/s|00:04|m4a|140|none|mp4a.40.2\n'
            ),
            encoding='utf-8',
        )

        response = self.client.post('/api/task_info', json={'tasks': [task_id]})
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['state'], 'downloading')
        self.assertEqual(task['progress']['percent'], 42.5)
        self.assertEqual(task['progress']['stage'], 'download_audio')
        self.assertEqual(task['progress']['speed'], '1.50MiB/s')
        self.assertEqual(task['progress']['eta'], '00:04')

    def test_structured_progress_distinguishes_video_and_subtitles(self):
        cases = (
            (
                'PYDL_PROGRESS|downloading|100.0%|10M|10M|1M/s|00:00|'
                'mp4|137|avc1.640028|none\n',
                'download_video',
            ),
            (
                'PYDL_PROGRESS|downloading|100.0%|20K|20K|100K/s|00:00|'
                'vtt|en|none|none\n',
                'download_subtitles',
            ),
        )

        for index, (line, expected_stage) in enumerate(cases):
            with self.subTest(stage=expected_stage):
                log_path = self.logs_dir / f'stage-{index}.log'
                log_path.write_text(line, encoding='utf-8')

                progress = app.parse_task_progress(str(log_path))

                self.assertEqual(progress['percent'], 100)
                self.assertEqual(progress['stage'], expected_stage)

    def test_postprocessing_stage_overrides_previous_100_percent(self):
        log_path = self.logs_dir / 'merge.log'
        log_path.write_text(
            (
                'PYDL_PROGRESS|finished|100.0%|NA|35M|1M/s|NA|'
                'm4a|140|none|mp4a.40.2\n'
                '[Merger] Merging formats into \"/tmp/video.mp4\"\n'
            ),
            encoding='utf-8',
        )

        progress = app.parse_task_progress(str(log_path))

        self.assertEqual(progress['percent'], 100)
        self.assertEqual(progress['stage'], 'merge_media')

    def test_completed_task_is_always_100_percent(self):
        task_id = 'v20260723120000QwE'
        self.write_task(task_id, '.ok')

        response = self.client.post('/api/task_info', json={'tasks': task_id})
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['state'], 'completed')
        self.assertEqual(task['progress']['percent'], 100)

    def test_completed_video_task_returns_player_url(self):
        task_id = 'v20260723120000PlY'
        filename = '视频 file.mp4'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        (files_dir / filename).touch()
        (self.urls_dir / f'{task_id}.result.json').write_text(
            json.dumps({'files': [filename, '视频 file.zh-Hans.srt']}),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['files'], [filename])
        self.assertEqual(
            task['player_url'],
            '/player?file=%E8%A7%86%E9%A2%91+file.mp4',
        )

    def test_completed_legacy_task_recovers_player_url_from_move_log(self):
        task_id = 'v20260723161431ohK'
        filename = '恢复的视频 (1).mp4'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        final_path = files_dir / filename
        final_path.touch()
        (self.logs_dir / 'downloader.log').write_text(
            (
                '2026-07-23 16:15:49 [INFO] 已移动文件: '
                f'/tmp/{task_id}/恢复的视频.mp4 -> {final_path}\n'
            ),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['files'], [filename])
        self.assertEqual(
            task['player_url'],
            '/player?file=%E6%81%A2%E5%A4%8D%E7%9A%84%E8%A7%86%E9%A2%91+(1).mp4',
        )

    def test_rejects_invalid_task_id_without_path_lookup(self):
        response = self.client.post(
            '/api/task_info',
            json={'tasks': ['../../config']},
        )
        task = response.get_json()['tasks'][0]

        self.assertFalse(task['exists'])
        self.assertEqual(task['msg'], 'Invalid task id')

    def test_accepts_legacy_numeric_task_suffix(self):
        task_id = 'a202505161122552581'
        self.write_task(task_id, '.txt')

        response = self.client.post('/api/task_info', json={'tasks': task_id})
        task = response.get_json()['tasks'][0]

        self.assertTrue(task['exists'])
        self.assertEqual(task['state'], 'queued')

    def test_parses_legacy_default_progress_line(self):
        log_path = self.logs_dir / 'legacy.log'
        log_path.write_text(
            '[download]  18.7% of ~  12.00MiB at  2.00MiB/s ETA 00:05\n',
            encoding='utf-8',
        )

        progress = app.parse_task_progress(str(log_path))

        self.assertEqual(progress['percent'], 18.7)
        self.assertEqual(progress['total'], '12.00MiB')
        self.assertEqual(progress['speed'], '2.00MiB/s')
        self.assertEqual(progress['eta'], '00:05')


if __name__ == '__main__':
    unittest.main()
