import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import webdav_uploader
import start
from config_util import DEFAULT_CONFIG, is_webdav_upload_enabled


class TestWebDAVUploadToggle(unittest.TestCase):
    def setUp(self):
        webdav_uploader.retry_count.clear()
        webdav_uploader.pending_files.clear()

    def tearDown(self):
        webdav_uploader.retry_count.clear()
        webdav_uploader.pending_files.clear()

    def test_upload_is_enabled_by_default(self):
        self.assertIs(DEFAULT_CONFIG['ENABLE_WEBDAV_UPLOAD'], True)
        self.assertEqual(DEFAULT_CONFIG['WEBDAV_UPLOAD_EXCLUDE_KEYWORDS'], [])
        self.assertEqual(DEFAULT_CONFIG['WEBDAV_RECONNECT_INTERVAL'], 30)
        self.assertTrue(is_webdav_upload_enabled({}))

    def test_excluded_keyword_matching_ignores_empty_and_invalid_values(self):
        self.assertEqual(
            webdav_uploader.find_upload_exclude_keyword(
                'demo-preview.mp4',
                ['', None, 'preview', 'demo'],
            ),
            'preview',
        )
        self.assertIsNone(
            webdav_uploader.find_upload_exclude_keyword(
                'demo-PREVIEW.mp4',
                ['preview'],
            )
        )
        self.assertIsNone(
            webdav_uploader.find_upload_exclude_keyword(
                'demo-preview.mp4',
                'preview',
            )
        )

    def test_start_services_skips_uploader_when_disabled(self):
        scripts = start.get_service_scripts({'ENABLE_WEBDAV_UPLOAD': False})

        self.assertEqual(scripts, [('downloader.py', '下载器')])

    def test_start_services_includes_uploader_when_enabled(self):
        scripts = start.get_service_scripts({'ENABLE_WEBDAV_UPLOAD': True})

        self.assertIn(('webdav_uploader.py', '上传器'), scripts)

    def test_start_services_include_ai_worker_only_when_configured(self):
        disabled = start.get_service_scripts({})
        enabled = start.get_service_scripts({
            'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
            'AI_API_MODEL': 'test-model',
            'AI_API_TOKEN': 'test-token',
        })

        self.assertNotIn(('ai_summary_worker.py', 'AI总结Worker'), disabled)
        self.assertIn(('ai_summary_worker.py', 'AI总结Worker'), enabled)

    def test_start_services_include_ai_worker_only_when_configured(self):
        disabled = start.get_service_scripts({})
        enabled = start.get_service_scripts({
            'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
            'AI_API_MODEL': 'test-model',
            'AI_API_TOKEN': 'test-token',
        })

        self.assertNotIn(('ai_summary_worker.py', 'AI总结Worker'), disabled)
        self.assertIn(('ai_summary_worker.py', 'AI总结Worker'), enabled)

    def test_runner_reports_disabled_upload_and_skips_uploader(self):
        result = subprocess.run(
            [
                'bash',
                '-c',
                '''
source ./runner.sh
command() { return 0; }
start_service() { echo "START:$1"; }
ai_summary_status() { return 10; }
webdav_upload_status() { return 10; }
start_services
''',
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn(
            'WebDAV上传已关闭，已跳过启动上传器。',
            result.stdout,
        )
        self.assertNotIn('START:上传器', result.stdout)

    def test_disabled_main_does_not_initialize_webdav_clients(self):
        with (
            patch.dict(
                webdav_uploader.config,
                {'ENABLE_WEBDAV_UPLOAD': False},
            ),
            patch.object(webdav_uploader, 'initialize_webdav_clients') as init,
            patch('webdav_uploader.time.sleep', side_effect=KeyboardInterrupt),
        ):
            webdav_uploader.main()

        init.assert_not_called()

    def test_disabled_upload_keeps_local_file_and_skips_webdav(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        with tempfile.TemporaryDirectory() as root:
            media_file = Path(root) / 'video.mp4'
            media_file.write_bytes(b'video')

            with (
                patch.dict(
                    webdav_uploader.config,
                    {'ENABLE_WEBDAV_UPLOAD': False},
                ),
                patch.object(webdav_uploader, 'video_webdav') as client,
            ):
                handler.process_file(str(media_file))

            self.assertTrue(media_file.exists())
            client.check.assert_not_called()
            client.upload_sync.assert_not_called()

    def test_excluded_filename_keeps_local_file_and_logs_keyword(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        with tempfile.TemporaryDirectory() as root:
            media_file = Path(root) / 'demo-preview.mp4'
            media_file.write_bytes(b'video')
            webdav_uploader.retry_count[str(media_file)] = 1

            with (
                patch.dict(
                    webdav_uploader.config,
                    {
                        'ENABLE_WEBDAV_UPLOAD': True,
                        'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [
                            '',
                            'preview',
                            'demo',
                        ],
                    },
                ),
                patch.object(webdav_uploader, 'video_webdav') as client,
                patch.object(webdav_uploader.logger, 'info') as log_info,
            ):
                handler.process_file(str(media_file))

            self.assertTrue(media_file.exists())
            self.assertNotIn(str(media_file), webdav_uploader.retry_count)
            client.check.assert_not_called()
            client.upload_sync.assert_not_called()
            log_info.assert_called_once()
            log_message = log_info.call_args.args[0]
            self.assertIn("'preview'", log_message)
            self.assertIn(str(media_file), log_message)

    def test_subtitles_are_kept_locally_without_webdav_upload(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        for extension in sorted(webdav_uploader.SUBTITLE_EXTENSIONS):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as root:
                subtitle_file = Path(root) / f'episode.zh-Hans{extension}'
                subtitle_file.write_text('subtitle', encoding='utf-8')
                webdav_uploader.retry_count[str(subtitle_file)] = 1

                with (
                    patch.dict(
                        webdav_uploader.config,
                        {
                            'ENABLE_WEBDAV_UPLOAD': True,
                            'DELETE_AFTER_UPLOAD': True,
                            'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [],
                        },
                    ),
                    patch.object(webdav_uploader, 'video_webdav') as video_client,
                    patch.object(webdav_uploader, 'audio_webdav') as audio_client,
                    patch.object(webdav_uploader.logger, 'info') as log_info,
                ):
                    handler.process_file(str(subtitle_file))

                self.assertTrue(subtitle_file.exists())
                self.assertNotIn(
                    str(subtitle_file),
                    webdav_uploader.retry_count,
                )
                video_client.check.assert_not_called()
                video_client.upload_sync.assert_not_called()
                audio_client.check.assert_not_called()
                audio_client.upload_sync.assert_not_called()
                log_message = log_info.call_args.args[0]
                self.assertIn('字幕文件', log_message)
                self.assertIn('保留本地文件', log_message)
                self.assertIn(str(subtitle_file), log_message)

    def test_disconnected_webdav_queues_file_instead_of_dropping_it(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        with tempfile.TemporaryDirectory() as root:
            media_file = Path(root) / 'video.mp4'
            media_file.write_bytes(b'video')
            file_path = str(media_file)

            with (
                patch.dict(
                    webdav_uploader.config,
                    {
                        'ENABLE_WEBDAV_UPLOAD': True,
                        'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [],
                    },
                ),
                patch.object(webdav_uploader, 'video_webdav', None),
                patch.object(webdav_uploader.logger, 'warning') as log_warning,
            ):
                handler.process_file(file_path)

            self.assertIn(file_path, webdav_uploader.pending_files)
            self.assertIn('等待队列', log_warning.call_args.args[0])

    def test_reconnect_processes_queued_files_after_connection_recovers(self):
        handler = webdav_uploader.WebDAVUploadHandler()
        stop_event = MagicMock()
        stop_event.wait.side_effect = [False, True]

        def restore_clients():
            webdav_uploader.video_webdav = MagicMock()
            webdav_uploader.audio_webdav = MagicMock()

        with (
            patch.object(webdav_uploader, 'video_webdav', None),
            patch.object(webdav_uploader, 'audio_webdav', None),
            patch.object(
                webdav_uploader,
                'initialize_webdav_clients',
                side_effect=restore_clients,
            ) as initialize,
            patch.object(handler, 'process_pending_files') as process_pending,
            patch.object(webdav_uploader.logger, 'info'),
        ):
            webdav_uploader.reconnect_webdav_clients(handler, stop_event)

        initialize.assert_called_once()
        process_pending.assert_called_once()

    def test_upload_failure_notifies_immediately_and_retries_once(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        with tempfile.TemporaryDirectory() as root:
            media_file = Path(root) / 'video.mp4'
            media_file.write_bytes(b'video')
            file_path = str(media_file)
            client = MagicMock()
            client.check.side_effect = [True, False, True, False]
            client.upload_sync.side_effect = RuntimeError('upload failed')

            with (
                patch.dict(
                    webdav_uploader.config,
                    {
                        'ENABLE_WEBDAV_UPLOAD': True,
                        'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [],
                    },
                ),
                patch.object(webdav_uploader, 'video_webdav', client),
                patch.object(
                    webdav_uploader,
                    'video_webdav_host',
                    'dav.example.test',
                ),
                patch.object(webdav_uploader, 'UPLOAD_MAX_RETRIES', 1),
                patch.object(webdav_uploader, 'UPLOAD_RETRY_DELAY', 60),
                patch.object(webdav_uploader, 'bark_notify') as notify,
                patch.object(webdav_uploader.threading, 'Timer') as timer,
                patch.object(webdav_uploader.logger, 'error'),
                patch.object(webdav_uploader.logger, 'info'),
            ):
                handler.process_file(file_path)

                self.assertEqual(notify.call_count, 1)
                self.assertIn('第1/1次重试', notify.call_args.kwargs['content'])
                timer.assert_called_once()
                timer.return_value.start.assert_called_once()
                self.assertEqual(webdav_uploader.retry_count[file_path], 1)

                handler.process_file(file_path)

            self.assertEqual(client.upload_sync.call_count, 2)
            client.put.assert_not_called()
            self.assertEqual(notify.call_count, 2)
            self.assertIn('不再重试', notify.call_args.kwargs['content'])
            timer.assert_called_once()
            self.assertNotIn(file_path, webdav_uploader.retry_count)


if __name__ == '__main__':
    unittest.main()
