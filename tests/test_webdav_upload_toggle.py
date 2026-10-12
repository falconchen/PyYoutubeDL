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
        self.assertIs(DEFAULT_CONFIG['WEBDAV_CREATE_DATE_DIR'], True)
        self.assertEqual(DEFAULT_CONFIG['WEBDAV_UPLOAD_EXCLUDE_KEYWORDS'], [])
        self.assertEqual(DEFAULT_CONFIG['WEBDAV_RECONNECT_INTERVAL'], 30)
        self.assertTrue(is_webdav_upload_enabled({}))

    def test_upload_destination_with_date_directory_enabled_disabled_or_missing(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        for create_date_dir in (True, False, None):
            for extension, client_name in (('.mp4', 'video_webdav'), ('.mp3', 'audio_webdav')):
                with self.subTest(setting=create_date_dir, extension=extension), tempfile.TemporaryDirectory() as root:
                    media_file = Path(root) / f'episode:demo{extension}'
                    media_file.write_bytes(b'media')
                    runtime_config = DEFAULT_CONFIG.copy()
                    runtime_config['DELETE_AFTER_UPLOAD'] = False
                    runtime_config['VIDEO_WEBDAV_OPTIONS'] = {}
                    runtime_config['AUDIO_WEBDAV_OPTIONS'] = {}
                    if create_date_dir is None:
                        runtime_config.pop('WEBDAV_CREATE_DATE_DIR')
                    else:
                        runtime_config['WEBDAV_CREATE_DATE_DIR'] = create_date_dir
                    client = MagicMock()
                    client.check.return_value = False
                    prefix = '' if create_date_dir is False else '/20261012'
                    remote_path = f'{prefix}/episode_demo{extension}'

                    with (
                        patch.dict(webdav_uploader.config, runtime_config, clear=True),
                        patch.object(webdav_uploader, client_name, client),
                        patch.object(webdav_uploader, 'datetime') as mock_datetime,
                        patch.object(webdav_uploader, 'bark_notify'),
                        patch.object(webdav_uploader, 'WebDAVUploadHistory') as history_factory,
                        patch.object(webdav_uploader, 'webdav_target_id', return_value='target'),
                    ):
                        mock_datetime.now.return_value.strftime.return_value = '20261012'
                        handler.process_file(str(media_file))

                    client.upload_sync.assert_called_once_with(
                        remote_path=remote_path, local_path=str(media_file),
                    )
                    if create_date_dir is False:
                        client.check.assert_called_once_with(remote_path)
                        client.mkdir.assert_not_called()
                        mock_datetime.now.assert_not_called()
                        history_factory.return_value.record.assert_called_once_with(
                            'target', 'Video' if extension == '.mp4' else 'Audio', remote_path,
                        )
                    else:
                        self.assertEqual(client.check.call_args_list[0].args, (prefix,))
                        client.mkdir.assert_called_once_with(prefix)
                        history_factory.assert_not_called()
                    self.assertTrue(media_file.exists())
                    self.assertFalse(Path(f'{media_file}.uploading').exists())

    def test_root_upload_skips_existing_file_and_respects_local_delete_setting(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        for delete_after_upload in (True, False):
            with self.subTest(delete=delete_after_upload), tempfile.TemporaryDirectory() as root:
                media_file = Path(root) / 'episode.mp4'
                media_file.write_bytes(b'media')
                client = MagicMock()
                client.check.return_value = True
                with (
                    patch.dict(webdav_uploader.config, {
                        'ENABLE_WEBDAV_UPLOAD': True,
                        'WEBDAV_CREATE_DATE_DIR': False,
                        'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [],
                        'DELETE_AFTER_UPLOAD': delete_after_upload,
                    }),
                    patch.object(webdav_uploader, 'video_webdav', client),
                ):
                    handler.process_file(str(media_file))

                client.check.assert_called_once_with('/episode.mp4')
                client.mkdir.assert_not_called()
                client.upload_sync.assert_not_called()
                self.assertEqual(media_file.exists(), not delete_after_upload)

    def test_connection_success_dispatches_cleanup_for_both_categories(self):
        for create_date_dir in (True, False, None):
            with self.subTest(setting=create_date_dir):
                runtime_config = DEFAULT_CONFIG.copy()
                runtime_config.update({
                    'VIDEO_WEBDAV_OPTIONS': {
                        'webdav_hostname': 'https://video.example.test',
                        'webdav_login': 'test', 'webdav_password': 'test',
                    },
                    'AUDIO_WEBDAV_OPTIONS': {
                        'webdav_hostname': 'https://audio.example.test',
                        'webdav_login': 'test', 'webdav_password': 'test',
                    },
                })
                if create_date_dir is None:
                    runtime_config.pop('WEBDAV_CREATE_DATE_DIR')
                else:
                    runtime_config['WEBDAV_CREATE_DATE_DIR'] = create_date_dir
                with (
                    patch.dict(webdav_uploader.config, runtime_config, clear=True),
                    patch.object(webdav_uploader, 'video_webdav', None),
                    patch.object(webdav_uploader, 'audio_webdav', None),
                    patch.object(webdav_uploader, 'video_webdav_host', None),
                    patch.object(webdav_uploader, 'audio_webdav_host', None),
                    patch.object(webdav_uploader, 'Client') as client_factory,
                    patch.object(webdav_uploader, 'get_webdav_methods', return_value='PROPFIND'),
                    patch.object(webdav_uploader, 'cleanup_webdav') as cleanup,
                ):
                    webdav_uploader.initialize_webdav_clients()

                self.assertEqual(client_factory.call_count, 2)
                self.assertEqual(cleanup.call_count, 2)

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

    def test_start_services_include_playlist_monitor_only_when_configured(self):
        disabled = start.get_service_scripts({})
        enabled = start.get_service_scripts({
            'GOOGLE_OAUTH_CLIENT_ID': 'cid',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'secret',
            'MONITOR_PLAYLISTS': {'PL1': ['video']},
        })

        self.assertNotIn(('playlist_monitor.py', '播放列表监控'), disabled)
        self.assertIn(('playlist_monitor.py', '播放列表监控'), enabled)

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
playlist_monitor_status() { return 10; }
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

    def test_runner_starts_only_selected_service(self):
        result = subprocess.run(
            [
                'bash',
                '-c',
                '''
source ./runner.sh
start_service() { echo "START:$2"; }
start_services downloader
''',
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn('START:downloader.py', result.stdout)
        self.assertNotIn('START:app.py', result.stdout)
        self.assertNotIn('START:webdav_uploader.py', result.stdout)

    def test_runner_rejects_unknown_service(self):
        result = subprocess.run(
            ['./runner.sh', 'start', 'unknown'],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('无效服务: unknown', result.stdout)

    def test_runner_lists_all_service_names(self):
        for option in ('-l', '--list'):
            with self.subTest(option=option):
                result = subprocess.run(
                    ['./runner.sh', option],
                    capture_output=True,
                    text=True,
                    check=True,
                )

                for service in ('app', 'downloader', 'ai', 'webdav', 'playlist'):
                    self.assertRegex(result.stdout, rf'(?m)^  {service}\s', msg=option)

    def test_runner_status_lists_all_services(self):
        result = subprocess.run(
            ['./runner.sh', 'status'],
            capture_output=True,
            text=True,
            check=True,
        )

        for service in ('app', 'downloader', 'ai', 'webdav', 'playlist'):
            self.assertRegex(result.stdout, rf'(?m)^{service}\s', msg=service)

    def test_runner_status_can_select_one_service(self):
        result = subprocess.run(
            ['./runner.sh', 'status', 'app'],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertRegex(result.stdout, r'(?m)^app\s')
        self.assertNotRegex(result.stdout, r'(?m)^downloader\s')

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
                        'WEBDAV_CREATE_DATE_DIR': True,
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
