import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytz
from webdav3.client import Client
from webdav3.exceptions import RemoteResourceNotFound

import webdav_cleanup
import webdav_uploader
from config_util import DEFAULT_CONFIG
from webdav_cleanup import WebDAVUploadHistory, webdav_target_id


class TestWebDAVDateCleanup(unittest.TestCase):
    def cleanup(self, client, days, dry_run=False, now=None):
        webdav_cleanup.cleanup_date_directories(
            client, days, 'Asia/Shanghai', 'Video', dry_run=dry_run,
            now=now or pytz.UTC.localize(datetime(2026, 10, 11, 16)),
        )

    def test_zero_and_invalid_days_skip_remote_scan(self):
        for days in (0, -1, '3', 1.5, True, False, None):
            with self.subTest(days=days):
                client = MagicMock()
                self.cleanup(client, days)
                client.list.assert_not_called()
                client.clean.assert_not_called()

    def test_calendar_days_include_today_and_use_configured_timezone(self):
        client = MagicMock()
        client.list.return_value = [
            '20261012/', '20261011/', '20261010/', '20261009/',
            '20261008/', '20261008/', '20261013/', '20260101/',
            '20260229/', '20261301/', '00000101/', '２０２６０１０１/',
            'nested/20260101/', '20260102.mp4',
        ]
        client.is_dir.side_effect = lambda name: name != '20260101'
        self.cleanup(client, 3)
        self.assertEqual(client.clean.call_args_list, [call('20261008'), call('20261009')])
        self.assertEqual({c.args[0] for c in client.is_dir.call_args_list}, {
            '20260101', '20261008', '20261009',
        })

    def test_sparse_directory_count_does_not_protect_expired_directory(self):
        client = MagicMock()
        client.list.return_value = ['20260101/']
        client.is_dir.return_value = True
        self.cleanup(client, 30)
        client.clean.assert_called_once_with('20260101')

    def test_leap_day_and_year_boundary(self):
        client = MagicMock()
        client.list.return_value = ['20240228/', '20240229/', '20240301/', '20230229/']
        client.is_dir.return_value = True
        self.cleanup(client, 2, now=pytz.UTC.localize(datetime(2024, 3, 1)))
        client.clean.assert_called_once_with('20240228')
        client.reset_mock()
        client.list.return_value = ['20251230/', '20251231/', '20260101/']
        self.cleanup(client, 2, now=pytz.UTC.localize(datetime(2026, 1, 1)))
        client.clean.assert_called_once_with('20251230')

    def test_dry_run_lists_candidates_without_deleting(self):
        client = MagicMock()
        client.list.return_value = ['20260101/']
        client.is_dir.return_value = True
        with patch.object(webdav_cleanup.logger, 'info') as info:
            self.cleanup(client, 7, dry_run=True)
        self.assertIn('20260101', info.call_args.args[0])
        client.clean.assert_not_called()

    def test_unknown_directory_type_and_query_failure_are_skipped(self):
        client = MagicMock()
        client.list.return_value = ['20261001/', '20261002/', '20261003/', '20261004/']
        client.is_dir.side_effect = [RuntimeError('lookup failed'), None, False, True]
        self.cleanup(client, 1)
        client.clean.assert_called_once_with('20261004')

    def test_listing_failure_never_deletes(self):
        client = MagicMock()
        client.list.side_effect = RuntimeError('listing failed')
        self.cleanup(client, 1)
        client.clean.assert_not_called()

    def test_one_deletion_failure_does_not_stop_other_candidates(self):
        client = MagicMock()
        client.list.return_value = ['20261001/', '20261002/']
        client.is_dir.return_value = True
        client.clean.side_effect = [RuntimeError('delete failed'), None]
        self.cleanup(client, 1)
        self.assertEqual(client.clean.call_args_list, [call('20261001'), call('20261002')])


class TestWebDAVUploadHistory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = str(Path(self.temp_dir.name) / 'nested' / 'uploads.sqlite3')
        self.history = WebDAVUploadHistory(self.path)
        self.client = MagicMock()
        self.client.check.return_value = True
        self.client.is_dir.return_value = False
        self.type_patch = patch.object(webdav_cleanup, 'remote_is_dir', return_value=False)
        self.remote_is_dir = self.type_patch.start()
        self.addCleanup(self.type_patch.stop)

    def cleanup(self, days=1, dry_run=False):
        webdav_cleanup.cleanup_uploaded_files(
            self.client, self.history, 'target', 'Video', days,
            dry_run=dry_run, now=200000,
        )

    def records(self):
        return self.history.expired('target', 'Video', 1000000)

    def test_persistence_scope_and_exact_elapsed_time_boundary(self):
        self.history.record('target', 'Video', '/expired.mp4', 113600)
        self.history.record('target', 'Video', '/recent.mp4', 113601)
        self.history.record('target', 'Audio', '/audio.mp3', 1)
        self.history.record('other-target', 'Video', '/other.mp4', 1)
        self.history = WebDAVUploadHistory(self.path)
        self.cleanup()
        self.client.clean.assert_called_once_with('/expired.mp4')
        self.assertEqual(self.records(), [('/recent.mp4', 113601)])
        self.assertEqual(self.history.expired('target', 'Audio', 200000), [('/audio.mp3', 1)])
        self.assertEqual(self.history.expired('other-target', 'Video', 200000), [('/other.mp4', 1)])
        self.client.list.assert_not_called()

    def test_reupload_refreshes_time_and_old_record_cannot_remove_new_record(self):
        self.history.record('target', 'Video', '/episode.mp4', 1)
        self.history.record('target', 'Video', '/episode.mp4', 200000)
        self.history.forget('target', 'Video', '/episode.mp4', 1)
        self.cleanup()
        self.client.clean.assert_not_called()
        self.assertEqual(self.records(), [('/episode.mp4', 200000)])

    def test_dry_run_preserves_files_and_all_records(self):
        self.history.record('target', 'Video', '/expired.mp4', 1)
        self.history.record('target', 'Video', '/missing.mp4', 2)
        self.remote_is_dir.side_effect = [False, RemoteResourceNotFound('/missing.mp4')]
        self.cleanup(dry_run=True)
        self.client.clean.assert_not_called()
        self.assertEqual(len(self.records()), 2)

    def test_missing_remote_file_forgets_record_without_deletion(self):
        self.history.record('target', 'Video', '/missing.mp4', 1)
        self.remote_is_dir.side_effect = RemoteResourceNotFound('/missing.mp4')
        self.cleanup()
        self.client.clean.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_directory_or_unknown_file_type_is_preserved(self):
        for is_dir in (True, None):
            with self.subTest(is_dir=is_dir):
                self.history.record('target', 'Video', '/episode.mp4', 1)
                self.remote_is_dir.return_value = is_dir
                self.cleanup()
                self.client.clean.assert_not_called()
                self.assertEqual(self.records(), [('/episode.mp4', 1)])

    def test_unknown_file_type_or_network_error_preserves_record(self):
        for result in (None, RuntimeError('network failed')):
            with self.subTest(result=result):
                self.history.record('target', 'Video', '/episode.mp4', 1)
                self.remote_is_dir.side_effect = [result]
                self.cleanup()
                self.client.clean.assert_not_called()
                self.assertEqual(self.records(), [('/episode.mp4', 1)])

    def test_failed_delete_keeps_record_for_retry(self):
        self.history.record('target', 'Video', '/episode.mp4', 1)
        self.client.clean.side_effect = RuntimeError('delete failed')
        self.cleanup()
        self.assertEqual(self.records(), [('/episode.mp4', 1)])
        self.client.clean.side_effect = None
        self.cleanup()
        self.assertEqual(self.records(), [])

    def test_invalid_paths_cannot_be_registered(self):
        for path in ('/', '/.', '/..', '/nested/file.mp4', 'file.mp4', '//file.mp4'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.history.record('target', 'Video', path)

    def test_corrupt_nested_record_is_never_deleted(self):
        with self.history.connect() as connection:
            connection.execute('INSERT INTO webdav_uploads VALUES (?, ?, ?, ?)',
                               ('target', 'Video', '/nested/file.mp4', 1))
        self.cleanup()
        self.client.check.assert_not_called()
        self.client.clean.assert_not_called()

    def test_zero_and_invalid_days_do_not_open_database_or_access_remote(self):
        for days in (0, -1, '3', 1.5, True, False, None):
            with self.subTest(days=days):
                self.cleanup(days=days)
        self.assertFalse(Path(self.path).exists())
        self.client.check.assert_not_called()
        self.client.clean.assert_not_called()

    def test_database_error_never_deletes_remote_files(self):
        Path(self.temp_dir.name, 'invalid.sqlite3').write_bytes(b'not a database')
        self.history = WebDAVUploadHistory(str(Path(self.temp_dir.name) / 'invalid.sqlite3'))
        self.cleanup()
        self.client.check.assert_not_called()
        self.client.clean.assert_not_called()

    def test_target_identity_changes_with_server_root_and_account_only(self):
        options = {'webdav_hostname': 'https://dav.example.test', 'webdav_root': '/video', 'webdav_login': 'test'}
        identity = webdav_target_id(options)
        for key, value in (('webdav_hostname', 'https://other.example.test'), ('webdav_root', '/audio'), ('webdav_login', 'other')):
            with self.subTest(key=key):
                self.assertNotEqual(webdav_target_id(dict(options, **{key: value})), identity)
        self.assertEqual(webdav_target_id(dict(options, webdav_password='placeholder')), identity)


class TestWebDAVCleanupIntegration(unittest.TestCase):
    def test_real_client_metadata_and_http_errors_preserve_correct_history(self):
        # 使用真实客户端解析模拟 PROPFIND 响应，避免 HEAD 将 403/500 当作不存在。
        options = {'webdav_hostname': 'https://dav.example.test', 'webdav_root': '/video', 'webdav_login': 'test'}
        with tempfile.TemporaryDirectory() as root:
            history = WebDAVUploadHistory(str(Path(root) / 'uploads.sqlite3'))
            client = Client(options)
            for status_code, resource_type, property_status, href, should_delete, should_forget in (
                (207, '', '200 OK', '/video/episode.mp4', True, True),
                (207, '<d:collection/>', '200 OK', '/video/episode.mp4', False, False),
                (207, '', '403 Forbidden', '/video/episode.mp4', False, False),
                (207, '', '200 OK', '/video/other.mp4', False, False),
                (403, '', '200 OK', '/video/episode.mp4', False, False),
                (500, '', '200 OK', '/video/episode.mp4', False, False),
                (404, '', '200 OK', '/video/episode.mp4', False, True),
            ):
                with self.subTest(status=status_code, resource_type=resource_type, property_status=property_status, href=href):
                    history.record('target', 'Video', '/episode.mp4', 1)
                    response = MagicMock()
                    response.status_code = status_code
                    response.content = f'''<d:multistatus xmlns:d="DAV:"><d:response>
                        <d:href>{href}</d:href><d:propstat><d:prop>
                        <d:resourcetype>{resource_type}</d:resourcetype>
                        </d:prop><d:status>HTTP/1.1 {property_status}</d:status>
                        </d:propstat></d:response></d:multistatus>'''.encode()
                    with (
                        patch.object(client.session, 'request', return_value=response) as request,
                        patch.object(client, 'clean') as clean,
                    ):
                        webdav_cleanup.cleanup_uploaded_files(
                            client, history, 'target', 'Video', 1, dry_run=False, now=200000,
                        )
                    self.assertEqual(clean.call_count, int(should_delete))
                    self.assertEqual(history.expired('target', 'Video', 200000) == [], should_forget)
                    self.assertEqual(request.call_args.kwargs['method'], 'PROPFIND')
                    self.assertEqual(request.call_args.kwargs['url'], 'https://dav.example.test/video/episode.mp4')

    def test_successful_upload_is_persisted_even_when_cleanup_disabled(self):
        options = {'webdav_hostname': 'https://dav.example.test', 'webdav_root': '/video', 'webdav_login': 'test'}
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / 'episode.mp4'
            media.write_bytes(b'media')
            db_path = str(Path(root) / 'uploads.sqlite3')
            client = MagicMock()
            client.check.return_value = False
            with (
                patch.dict(webdav_uploader.config, {
                    'ENABLE_WEBDAV_UPLOAD': True, 'ENABLE_WEBDAV_CLEANUP': False,
                    'WEBDAV_CREATE_DATE_DIR': False, 'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [],
                    'DELETE_AFTER_UPLOAD': False, 'WEBDAV_UPLOAD_DB_PATH': db_path,
                    'VIDEO_WEBDAV_OPTIONS': options,
                }),
                patch.object(webdav_uploader, 'video_webdav', client),
                patch.object(webdav_uploader, 'bark_notify'),
            ):
                handler = webdav_uploader.WebDAVUploadHandler()
                handler.process_file(str(media))
                history = WebDAVUploadHistory(db_path)
                records = history.expired(webdav_target_id(options), 'Video', float('inf'))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0][0], '/episode.mp4')
                # 重复文件不会刷新上传时间，也不会让历史同名文件进入清理记录。
                client.check.return_value = True
                with patch.object(webdav_uploader, 'WebDAVUploadHistory') as history_factory:
                    handler.process_file(str(media))
                history_factory.assert_not_called()
                self.assertEqual(history.expired(webdav_target_id(options), 'Video', float('inf')), records)
            self.assertTrue(media.exists())
            client.upload_sync.assert_called_once()

    def test_failed_upload_does_not_create_history(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / 'episode.mp4'
            media.write_bytes(b'media')
            client = MagicMock()
            client.check.return_value = False
            client.upload_sync.side_effect = RuntimeError('upload failed')
            with (
                patch.dict(webdav_uploader.config, {
                    'ENABLE_WEBDAV_UPLOAD': True, 'WEBDAV_CREATE_DATE_DIR': False,
                    'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [],
                }),
                patch.object(webdav_uploader, 'video_webdav', client),
                patch.object(webdav_uploader, 'UPLOAD_MAX_RETRIES', 0),
                patch.object(webdav_uploader, 'bark_notify'),
                patch.object(webdav_uploader, 'WebDAVUploadHistory') as history_factory,
            ):
                webdav_uploader.WebDAVUploadHandler().process_file(str(media))
            history_factory.assert_not_called()
            self.assertTrue(media.exists())

    def test_history_failure_does_not_turn_successful_upload_into_retry(self):
        with tempfile.TemporaryDirectory() as root:
            media = Path(root) / 'episode.mp4'
            media.write_bytes(b'media')
            client = MagicMock()
            client.check.return_value = False
            with (
                patch.dict(webdav_uploader.config, {
                    'ENABLE_WEBDAV_UPLOAD': True, 'WEBDAV_CREATE_DATE_DIR': False,
                    'WEBDAV_UPLOAD_EXCLUDE_KEYWORDS': [], 'DELETE_AFTER_UPLOAD': False,
                    'VIDEO_WEBDAV_OPTIONS': {},
                }),
                patch.object(webdav_uploader, 'video_webdav', client),
                patch.object(webdav_uploader, 'webdav_target_id', return_value='target'),
                patch.object(webdav_uploader, 'WebDAVUploadHistory') as history_factory,
                patch.object(webdav_uploader.threading, 'Timer') as timer,
                patch.object(webdav_uploader, 'bark_notify'),
                patch.object(webdav_uploader.logger, 'error') as error,
            ):
                history_factory.return_value.record.side_effect = RuntimeError('database unavailable')
                webdav_uploader.WebDAVUploadHandler().process_file(str(media))
            client.upload_sync.assert_called_once()
            error.assert_called_once()
            self.assertIn('记录保存失败', error.call_args.args[0])
            timer.assert_not_called()
            self.assertTrue(media.exists())

    def test_cleanup_defaults_are_safe(self):
        self.assertIs(DEFAULT_CONFIG['ENABLE_WEBDAV_CLEANUP'], False)
        self.assertIs(DEFAULT_CONFIG['WEBDAV_CLEANUP_DRY_RUN'], True)
        self.assertEqual(DEFAULT_CONFIG['VIDEO_WEBDAV_RETENTION_DAYS'], 7)
        self.assertEqual(DEFAULT_CONFIG['AUDIO_WEBDAV_RETENTION_DAYS'], 30)

    def test_separate_retention_days_and_mode_routing(self):
        for dated in (True, False):
            with (
                self.subTest(dated=dated),
                patch.dict(webdav_uploader.config, {
                    'ENABLE_WEBDAV_UPLOAD': True, 'ENABLE_WEBDAV_CLEANUP': True,
                    'WEBDAV_CREATE_DATE_DIR': dated,
                    'VIDEO_WEBDAV_RETENTION_DAYS': 2, 'AUDIO_WEBDAV_RETENTION_DAYS': 10,
                    'VIDEO_WEBDAV_OPTIONS': {}, 'AUDIO_WEBDAV_OPTIONS': {},
                    'WEBDAV_CLEANUP_DRY_RUN': True,
                }),
                patch.object(webdav_uploader, 'WebDAVUploadHistory'),
                patch.object(webdav_uploader, 'webdav_target_id', return_value='target'),
                patch.object(webdav_uploader, 'cleanup_date_directories') as directories,
                patch.object(webdav_uploader, 'cleanup_uploaded_files') as files,
            ):
                client = MagicMock()
                webdav_uploader.cleanup_webdav(client, 'test', 'Video')
                webdav_uploader.cleanup_webdav(client, 'test', 'Audio')
                if dated:
                    self.assertEqual([c.args[1] for c in directories.call_args_list], [2, 10])
                    files.assert_not_called()
                else:
                    self.assertEqual([c.args[4] for c in files.call_args_list], [2, 10])
                    directories.assert_not_called()

    def test_disabled_cleanup_or_upload_never_scans_remote(self):
        for upload, cleanup in ((True, False), (False, True)):
            with (
                self.subTest(upload=upload),
                patch.dict(webdav_uploader.config, {'ENABLE_WEBDAV_UPLOAD': upload, 'ENABLE_WEBDAV_CLEANUP': cleanup}),
                patch.object(webdav_uploader, 'cleanup_date_directories') as directories,
                patch.object(webdav_uploader, 'cleanup_uploaded_files') as files,
            ):
                webdav_uploader.cleanup_webdav(MagicMock(), 'test', 'Video')
                directories.assert_not_called()
                files.assert_not_called()

    def test_periodic_cleanup_waits_interval_and_handles_both_categories(self):
        stop_event = MagicMock()
        stop_event.wait.side_effect = [False, True]
        with (
            patch.dict(webdav_uploader.config, {'ENABLE_WEBDAV_CLEANUP': True, 'WEBDAV_CLEANUP_INTERVAL_HOURS': 24}),
            patch.object(webdav_uploader, 'cleanup_webdav') as cleanup,
        ):
            webdav_uploader.periodic_webdav_cleanup(stop_event)
        self.assertEqual(stop_event.wait.call_args_list, [call(86400), call(86400)])
        self.assertEqual([c.args[2] for c in cleanup.call_args_list], ['Video', 'Audio'])

    def test_invalid_interval_does_not_start_periodic_cleanup(self):
        for hours in (0, -1, True, '24', 1.5, 8761):
            with (
                self.subTest(hours=hours),
                patch.dict(webdav_uploader.config, {'ENABLE_WEBDAV_CLEANUP': True, 'WEBDAV_CLEANUP_INTERVAL_HOURS': hours}),
                patch.object(webdav_uploader, 'cleanup_webdav') as cleanup,
            ):
                event = MagicMock()
                webdav_uploader.periodic_webdav_cleanup(event)
                event.wait.assert_not_called()
                cleanup.assert_not_called()

    def test_disabled_periodic_cleanup_does_not_wait_or_scan(self):
        event = MagicMock()
        with (
            patch.dict(webdav_uploader.config, {'ENABLE_WEBDAV_CLEANUP': False}),
            patch.object(webdav_uploader, 'cleanup_webdav') as cleanup,
        ):
            webdav_uploader.periodic_webdav_cleanup(event)
        event.wait.assert_not_called()
        cleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
