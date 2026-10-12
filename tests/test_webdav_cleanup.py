import unittest
from unittest.mock import MagicMock, call, patch

import webdav_uploader


class TestWebDAVDirectoryCleanup(unittest.TestCase):
    def cleanup(self, client, keep_count):
        webdav_uploader.cleanup_webdav_directories(
            client, keep_count, 'dav.example.test', 'Video',
        )

    def test_zero_disables_cleanup_without_scanning_remote(self):
        client = MagicMock()
        self.cleanup(client, 0)
        client.list.assert_not_called()
        client.clean.assert_not_called()

    def test_invalid_keep_count_skips_cleanup_with_warning(self):
        for keep_count in (-1, -3, '3', 1.5, True, False, None):
            with self.subTest(keep_count=keep_count):
                client = MagicMock()
                with patch.object(webdav_uploader.logger, 'warning') as warning:
                    self.cleanup(client, keep_count)
                warning.assert_called_once()
                client.list.assert_not_called()
                client.clean.assert_not_called()

    def test_keeps_latest_unique_valid_date_directories_only(self):
        client = MagicMock()
        client.list.return_value = [
            '20261012/', '/20261010/', '20261009/', '20261010/',
            '20240229/', '20230229/', '20261301/', '00000101/',
            '20261008', '20261007/', 'notes/', '20261001.mp4',
            'nested/20261001/', '２０２６１００１/',
        ]
        client.is_dir.side_effect = lambda name: name not in ('20261008', '20261007')

        self.cleanup(client, 2)

        client.list.assert_called_once_with('/')
        self.assertEqual(client.clean.call_args_list, [call('20240229'), call('20261009')])
        checked_names = {args.args[0] for args in client.is_dir.call_args_list}
        self.assertEqual(checked_names, {
            '20261012', '20261010', '20261009', '20240229', '20261008', '20261007',
        })

    def test_does_not_delete_when_directory_count_is_within_limit(self):
        for keep_count in (2, 3):
            with self.subTest(keep_count=keep_count):
                client = MagicMock()
                client.list.return_value = ['20261011/', '20261012/']
                client.is_dir.return_value = True
                self.cleanup(client, keep_count)
                client.clean.assert_not_called()

    def test_failed_or_unknown_directory_type_is_skipped(self):
        client = MagicMock()
        client.list.return_value = ['20261008/', '20261009/', '20261010/', '20261011/']
        client.is_dir.side_effect = [RuntimeError('lookup failed'), None, True, True]

        with patch.object(webdav_uploader.logger, 'warning') as warning:
            self.cleanup(client, 1)

        warning.assert_called_once()
        client.clean.assert_called_once_with('20261010')

    def test_remote_listing_failure_does_not_delete_anything(self):
        client = MagicMock()
        client.list.side_effect = RuntimeError('listing failed')
        with patch.object(webdav_uploader.logger, 'error') as error:
            self.cleanup(client, 1)
        error.assert_called_once()
        client.clean.assert_not_called()

    def test_one_deletion_failure_does_not_stop_other_deletions(self):
        client = MagicMock()
        client.list.return_value = ['20261009/', '20261010/', '20261011/']
        client.is_dir.return_value = True
        client.clean.side_effect = [RuntimeError('delete failed'), None]
        with patch.object(webdav_uploader.logger, 'error') as error:
            self.cleanup(client, 1)
        error.assert_called_once()
        self.assertEqual(client.clean.call_args_list, [call('20261009'), call('20261010')])


if __name__ == '__main__':
    unittest.main()
