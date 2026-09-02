import glob
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import playlist_monitor
import youtube_auth


class PlaylistMonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.config = {
            'URLS_DIR': os.path.join(root, 'urls'),
            'LOG_DIR': os.path.join(root, 'logs'),
            'MAX_LOG_SIZE': 10 * 1024 * 1024,
            'BACKUP_COUNT': 2,
            'TIMEZONE': 'Asia/Shanghai',
            'MONITOR_PLAYLISTS': {},
            'PLAYLIST_POLL_INTERVAL_SECONDS': 300,
            'PLAYLIST_MAX_ITEMS_PER_RUN': 10,
            'BARK_DEVICE_TOKEN': '',
        }
        self.monitor = playlist_monitor.PlaylistMonitor(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _item(video_id='dQw4w9WgXcQ', title='Test video', item_id='ITEM1',
              kind='youtube#video'):
        return {
            'id': item_id,
            'snippet': {
                'title': title,
                'resourceId': {'kind': kind, 'videoId': video_id},
            },
        }

    def _service(self, items=None):
        service = MagicMock()
        service.playlistItems.return_value.list.return_value.execute.return_value = {
            'items': items or [],
        }
        return service

    def _task_files(self):
        return sorted(glob.glob(os.path.join(self.config['URLS_DIR'], '*.txt')))

    def test_consume_item_creates_video_and_audio_tasks_and_deletes(self):
        service = self._service()
        self.monitor._consume_item(service, self._item(), ['video', 'audio'])

        service.playlistItems.return_value.delete.assert_called_once_with(
            id='ITEM1'
        )
        files = self._task_files()
        self.assertEqual(len(files), 2)
        names = [os.path.basename(f) for f in files]
        self.assertTrue(any(n.startswith('v') for n in names))
        self.assertTrue(any(n.startswith('a') for n in names))
        for path in files:
            with open(path) as fh:
                self.assertEqual(
                    fh.read(),
                    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                )

    def test_consume_item_skips_non_video(self):
        service = self._service()
        self.monitor._consume_item(
            service, self._item(kind='youtube#channel'), ['video'],
        )
        service.playlistItems.return_value.delete.assert_not_called()
        self.assertEqual(self._task_files(), [])

    def test_consume_item_notify_marks_download_type(self):
        service = self._service()
        with patch.object(self.monitor, 'notify') as notify:
            self.monitor._consume_item(
                service, self._item(), ['video', 'audio']
            )
        notify.assert_called_once_with(
            '已加入下载【视频+音频】: Test video',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        )

    def test_consume_item_notify_video_only(self):
        service = self._service()
        with patch.object(self.monitor, 'notify') as notify:
            self.monitor._consume_item(
                service, self._item(title='Only video'), ['video']
            )
        notify.assert_called_once_with(
            '已加入下载【视频】: Only video',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        )

    def test_consume_item_notify_audio_only(self):
        service = self._service()
        with patch.object(self.monitor, 'notify') as notify:
            self.monitor._consume_item(
                service, self._item(title='Only audio'), ['audio']
            )
        notify.assert_called_once_with(
            '已加入下载【音频】: Only audio',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        )

    def test_process_playlist_skips_enqueue_on_delete_failure_and_continues(self):
        items = [
            self._item(video_id='VIDEO1', item_id='ITEM1'),
            self._item(video_id='VIDEO2', item_id='ITEM2', title='Second'),
        ]
        service = self._service(items=items)
        service.playlistItems.return_value.delete.return_value.execute.side_effect = [
            RuntimeError('boom'),
            None,
        ]

        self.monitor._process_playlist(service, 'PL123', ['video'])

        files = self._task_files()
        self.assertEqual(len(files), 1)
        with open(files[0]) as fh:
            self.assertEqual(
                fh.read(), 'https://www.youtube.com/watch?v=VIDEO2'
            )


class TestDescribeTypes(unittest.TestCase):
    def test_video(self):
        self.assertEqual(playlist_monitor.describe_types(['video']), '视频')

    def test_audio(self):
        self.assertEqual(playlist_monitor.describe_types(['audio']), '音频')

    def test_video_and_audio_order_insensitive(self):
        self.assertEqual(
            playlist_monitor.describe_types(['video', 'audio']), '视频+音频'
        )
        self.assertEqual(
            playlist_monitor.describe_types(['audio', 'video']), '视频+音频'
        )

    def test_unknown_and_empty(self):
        self.assertEqual(playlist_monitor.describe_types([]), '')
        self.assertEqual(playlist_monitor.describe_types(['unknown']), '')
        self.assertEqual(playlist_monitor.describe_types(None), '')


class TestRunOnceGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.config = {
            'URLS_DIR': os.path.join(root, 'urls'),
            'LOG_DIR': os.path.join(root, 'logs'),
            'MAX_LOG_SIZE': 1024,
            'BACKUP_COUNT': 1,
            'TIMEZONE': 'Asia/Shanghai',
            'GOOGLE_OAUTH_CLIENT_ID': 'cid',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'secret',
            'GOOGLE_OAUTH_REDIRECT_URI': 'https://yter.cellmean.com/oauth/callback',
            'GOOGLE_OAUTH_TOKEN_FILE': os.path.join(root, 'token.json'),
            'GOOGLE_OAUTH_FAIL_LOCK_FILE': os.path.join(root, 'fail.lock'),
            'MONITOR_PLAYLISTS': {'PL123': ['video']},
            'PLAYLIST_POLL_INTERVAL_SECONDS': 300,
            'PLAYLIST_MAX_ITEMS_PER_RUN': 10,
        }
        self.monitor = playlist_monitor.PlaylistMonitor(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    @patch.object(playlist_monitor, 'load_config')
    def test_run_once_skips_api_when_fail_lock_exists(self, load_config):
        load_config.return_value = self.config
        with open(self.config['GOOGLE_OAUTH_FAIL_LOCK_FILE'], 'w') as fh:
            fh.write('1')

        with patch.object(
            playlist_monitor.youtube_auth, 'get_credentials'
        ) as get_credentials:
            self.monitor._run_once()
            get_credentials.assert_not_called()

    @patch.object(playlist_monitor, 'load_config')
    def test_run_once_skips_when_not_enabled(self, load_config):
        cfg = dict(self.config)
        cfg['GOOGLE_OAUTH_CLIENT_ID'] = ''
        load_config.return_value = cfg

        with patch.object(
            playlist_monitor.youtube_auth, 'get_credentials'
        ) as get_credentials:
            self.monitor._run_once()
            get_credentials.assert_not_called()


class TestYoutubeAuthRefreshFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {
            'GOOGLE_OAUTH_TOKEN_FILE': os.path.join(self.tmp.name, 'token.json'),
            'GOOGLE_OAUTH_FAIL_LOCK_FILE': os.path.join(self.tmp.name, 'fail.lock'),
            'GOOGLE_OAUTH_REDIRECT_URI': 'https://yter.cellmean.com/oauth/callback',
            'YOUTUBE_API_PROXY': '',
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_refresh_failure_writes_fail_lock(self):
        fake = MagicMock()
        fake.valid = False
        fake.refresh_token = 'refresh-token'
        fake.refresh.side_effect = RuntimeError('boom')

        with patch.object(
            youtube_auth, 'load_token',
            return_value={'token': 'x', 'refresh_token': 'refresh-token'},
        ), patch.object(
            youtube_auth.Credentials, 'from_authorized_user_info',
            return_value=fake,
        ):
            with self.assertRaises(RuntimeError):
                youtube_auth.get_credentials(self.config)

        self.assertTrue(
            os.path.exists(self.config['GOOGLE_OAUTH_FAIL_LOCK_FILE'])
        )


if __name__ == '__main__':
    unittest.main()
