import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


class TestDownloaderLogAPI(unittest.TestCase):
    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def request_log(
        self,
        token='test-log-token',
        cursor=None,
        file_id=None,
    ):
        query = {} if cursor is None else {'cursor': str(cursor)}
        if file_id is not None:
            query['file_id'] = file_id
        return self.client.get(
            '/api/downloader_log',
            query_string=query,
            headers={'X-Yter-Log-Token': token},
        )

    def test_log_api_is_disabled_without_server_token(self):
        with patch.dict(app_module.config, {'EXTENSION_LOG_TOKEN': ''}):
            response = self.request_log()

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()['success'])

    def test_log_api_rejects_invalid_token(self):
        with patch.dict(
            app_module.config,
            {'EXTENSION_LOG_TOKEN': 'correct-token'},
        ):
            response = self.request_log(token='wrong-token')

        self.assertEqual(response.status_code, 401)

    def test_log_api_returns_initial_tail_and_incremental_content(self):
        with tempfile.TemporaryDirectory() as log_dir:
            log_path = Path(log_dir, 'downloader.log')
            log_path.write_text('first line\nsecond line\n', encoding='utf-8')

            with patch.dict(
                app_module.config,
                {
                    'EXTENSION_LOG_TOKEN': 'test-log-token',
                    'LOG_DIR': log_dir,
                },
            ):
                initial_response = self.request_log()
                initial = initial_response.get_json()

                with log_path.open('a', encoding='utf-8') as log_file:
                    log_file.write('third line 中文\n')

                incremental_response = self.request_log(
                    cursor=initial['cursor'],
                    file_id=initial['file_id'],
                )
                incremental = incremental_response.get_json()

        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(
            initial_response.headers['Cache-Control'],
            'no-store, private',
        )
        self.assertEqual(initial['text'], 'first line\nsecond line\n')
        self.assertFalse(initial['reset'])
        self.assertEqual(incremental_response.status_code, 200)
        self.assertEqual(incremental['text'], 'third line 中文\n')
        self.assertGreater(incremental['cursor'], initial['cursor'])

    def test_log_api_resets_cursor_after_log_rotation(self):
        with tempfile.TemporaryDirectory() as log_dir:
            log_path = Path(log_dir, 'downloader.log')
            log_path.write_text('old log\n', encoding='utf-8')
            with patch.dict(
                app_module.config,
                {
                    'EXTENSION_LOG_TOKEN': 'test-log-token',
                    'LOG_DIR': log_dir,
                },
            ):
                initial = self.request_log().get_json()
                log_path.rename(Path(log_dir, 'downloader.log.1'))
                log_path.write_text(
                    'new log after rotation and already larger\n',
                    encoding='utf-8',
                )
                response = self.request_log(
                    cursor=initial['cursor'],
                    file_id=initial['file_id'],
                )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['reset'])
        self.assertEqual(
            data['text'],
            'new log after rotation and already larger\n',
        )
        self.assertNotEqual(data['file_id'], initial['file_id'])

    def test_log_api_rejects_invalid_cursor(self):
        with patch.dict(
            app_module.config,
            {'EXTENSION_LOG_TOKEN': 'test-log-token'},
        ):
            response = self.request_log(cursor='not-a-number')

        self.assertEqual(response.status_code, 400)

    def test_task_log_returns_only_matching_task_logs(self):
        with tempfile.TemporaryDirectory() as root_dir:
            urls_dir = Path(root_dir, 'urls')
            log_dir = Path(root_dir, 'logs')
            urls_dir.mkdir()
            log_dir.mkdir()
            task_id = 'v20260831172600AbC'
            Path(urls_dir, f'{task_id}.txt').write_text(
                'https://example.com/video', encoding='utf-8'
            )
            project_root = Path(app_module.__file__).resolve().parent
            Path(log_dir, f'{task_id}.log').write_text(
                f'task line\n[download] Destination: '
                f'{project_root}/tmp/{task_id}/file.m4a\n',
                encoding='utf-8',
            )
            Path(log_dir, 'downloader.log').write_text(
                'matching https://example.com/video\nother task secret\n',
                encoding='utf-8',
            )
            with (
                patch.object(app_module, 'URLS_DIR', str(urls_dir)),
                patch.dict(app_module.config, {'LOG_DIR': str(log_dir)}),
            ):
                response = self.client.post(
                    '/api/task_log', json={'tasks': [task_id]}
                )

        self.assertEqual(response.status_code, 200)
        text = response.get_json()['text']
        self.assertIn('task line', text)
        self.assertIn('matching https://example.com/video', text)
        self.assertIn('📁/tmp/v20260831172600AbC/file.m4a', text)
        self.assertNotIn(str(project_root), text)
        self.assertNotIn('other task secret', text)


if __name__ == '__main__':
    unittest.main()
