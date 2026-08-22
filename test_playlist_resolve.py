import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import app


class TestLooksLikePlaylist(unittest.TestCase):
    def test_youtube_watch_url_is_not_playlist(self):
        self.assertFalse(app.looks_like_playlist(
            'https://www.youtube.com/watch?v=abcDEF12345'))

    def test_youtube_short_url_is_not_playlist(self):
        self.assertFalse(app.looks_like_playlist('https://youtu.be/abcDEF12345'))

    def test_youtube_list_parameter_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/watch?v=abcDEF12345&list=PL123456789'))

    def test_youtube_playlist_path_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/playlist?list=PL123456789'))

    def test_youtube_mix_path_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/mix/PL123456789'))

    def test_youtube_playlists_path_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/playlists'))

    def test_channel_handle_videos_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/@TED/videos'))

    def test_channel_handle_homepage_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/@TED'))

    def test_channel_id_videos_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/channel/UCsooa4yRKGN_zEE8iknghZA/videos'))

    def test_channel_id_homepage_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/channel/UCsooa4yRKGN_zEE8iknghZA'))

    def test_channel_custom_url_videos_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/c/SomeChannel/videos'))

    def test_channel_user_url_is_playlist(self):
        self.assertTrue(app.looks_like_playlist(
            'https://www.youtube.com/user/SomeUser'))

    def test_channel_content_tabs_are_playlist(self):
        for tab in ('shorts', 'streams', 'podcasts', 'releases'):
            with self.subTest(tab=tab):
                self.assertTrue(app.looks_like_playlist(
                    f'https://www.youtube.com/@TED/{tab}'))

    def test_channel_non_content_tabs_are_not_playlist(self):
        for tab in ('about', 'community', 'search'):
            with self.subTest(tab=tab):
                self.assertFalse(app.looks_like_playlist(
                    f'https://www.youtube.com/@TED/{tab}'))

    def test_non_youtube_url_is_not_playlist(self):
        self.assertFalse(app.looks_like_playlist(
            'https://example.com/video/123'))

    def test_empty_or_invalid_input(self):
        self.assertFalse(app.looks_like_playlist(''))
        self.assertFalse(app.looks_like_playlist('not a url'))
        self.assertFalse(app.looks_like_playlist('  '))


class TestResolvePlaylistUrls(unittest.TestCase):
    def test_parses_entry_urls_from_stdout(self):
        completed = MagicMock(returncode=0, stdout=(
            'dQw4w9WgXcQ|https://www.youtube.com/watch?v=dQw4w9WgXcQ\n'
            'abcDEF12345|https://www.youtube.com/watch?v=abcDEF12345\n'
        ), stderr='')
        with patch('app.subprocess.run', return_value=completed) as run:
            urls, error = app.resolve_playlist_urls(
                'https://www.youtube.com/playlist?list=PLtest',
                '/x/yt-dlp.conf',
            )

        self.assertIsNone(error)
        self.assertEqual(urls, [
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'https://www.youtube.com/watch?v=abcDEF12345',
        ])
        cmd = run.call_args.args[0]
        self.assertIn('--flat-playlist', cmd)
        self.assertIn('%(id)s|%(webpage_url)s', cmd)
        self.assertEqual(cmd[cmd.index('--config-location') + 1], '/x/yt-dlp.conf')

    def test_skips_na_urls_and_blank_lines(self):
        completed = MagicMock(returncode=0, stdout=(
            'vid1|NA\n'
            '\n'
            'vid2|https://example.com/v2\n'
        ), stderr='')
        with patch('app.subprocess.run', return_value=completed):
            urls, error = app.resolve_playlist_urls('url', 'conf')

        self.assertIsNone(error)
        self.assertEqual(urls, ['https://example.com/v2'])

    def test_nonzero_exit_returns_error(self):
        completed = MagicMock(returncode=1, stdout='', stderr='ERROR: boom')
        with patch('app.subprocess.run', return_value=completed):
            urls, error = app.resolve_playlist_urls('url', 'conf')

        self.assertIsNone(urls)
        self.assertIn('boom', error)

    def test_timeout_returns_error(self):
        with patch(
            'app.subprocess.run',
            side_effect=subprocess.TimeoutExpired('cmd', 60),
        ):
            urls, error = app.resolve_playlist_urls('url', 'conf')

        self.assertIsNone(urls)
        self.assertIn('timed out', error)


class TestExpandTaskUrls(unittest.TestCase):
    def test_single_video_url_returned_as_is(self):
        url = 'https://www.youtube.com/watch?v=abcDEF12345'
        urls, error = app.expand_task_urls(url)

        self.assertIsNone(error)
        self.assertEqual(urls, [url])

    def test_playlist_url_expanded(self):
        entries = [
            'https://www.youtube.com/watch?v=a1',
            'https://www.youtube.com/watch?v=b2',
        ]
        with (
            patch('app.looks_like_playlist', return_value=True),
            patch('app.resolve_playlist_urls', return_value=(entries, None)),
        ):
            urls, error = app.expand_task_urls('https://www.youtube.com/playlist?list=PLx')

        self.assertIsNone(error)
        self.assertEqual(urls, entries)

    def test_playlist_exceeds_max_items(self):
        many = [f'https://www.youtube.com/watch?v=vid{i}' for i in range(501)]
        with (
            patch('app.looks_like_playlist', return_value=True),
            patch('app.resolve_playlist_urls', return_value=(many, None)),
            patch.dict(app.config, {'PLAYLIST_MAX_ITEMS': 500}),
        ):
            urls, error = app.expand_task_urls('playlist')

        self.assertIsNone(urls)
        self.assertIn('上限', error)

    def test_playlist_resolve_failure_returns_error(self):
        with (
            patch('app.looks_like_playlist', return_value=True),
            patch(
                'app.resolve_playlist_urls',
                return_value=(None, '解析播放列表失败: boom'),
            ),
        ):
            urls, error = app.expand_task_urls('playlist')

        self.assertIsNone(urls)
        self.assertEqual(error, '解析播放列表失败: boom')

    def test_empty_playlist_returns_error(self):
        with (
            patch('app.looks_like_playlist', return_value=True),
            patch('app.resolve_playlist_urls', return_value=([], None)),
        ):
            urls, error = app.expand_task_urls('playlist')

        self.assertIsNone(urls)
        self.assertIn('为空', error)


class TestCreateTasks(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.urls_dir = Path(self.temp_dir.name)
        self.patches = [
            patch.object(app, 'URLS_DIR', str(self.urls_dir)),
            patch(
                'app.get_current_time',
                return_value=datetime(2026, 8, 22, 12, 0, 0),
            ),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def test_creates_one_task_per_url_and_type(self):
        urls = [
            'https://www.youtube.com/watch?v=aaa',
            'https://www.youtube.com/watch?v=bbb',
        ]
        task_ids = app.create_tasks(urls, ['video', 'audio'])

        self.assertEqual(len(task_ids), 4)
        files = list(self.urls_dir.glob('*.txt'))
        self.assertEqual(len(files), 4)
        prefixes = sorted(name[0] for name in (f.name for f in files))
        self.assertEqual(prefixes, ['a', 'a', 'v', 'v'])
        contents = sorted(f.read_text(encoding='utf-8') for f in files)
        self.assertEqual(contents, sorted(urls * 2))

    def test_create_skips_existing_task_id(self):
        # 预置一个将要生成的 task_id 任务文件，验证 while 循环避免覆盖
        (self.urls_dir / 'v20260822120000xyz.txt').write_text(
            'existing',
            encoding='utf-8',
        )
        calls = iter(['xyz', 'abc'])
        with patch('app.random_str', side_effect=lambda _length: next(calls)):
            task_ids = app.create_tasks(['u1'], ['video'])

        self.assertEqual(task_ids, ['v20260822120000abc'])
        self.assertEqual(
            (self.urls_dir / 'v20260822120000xyz.txt').read_text(encoding='utf-8'),
            'existing',
        )


class PlaylistSubmitTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.urls_dir = root / 'urls'
        self.urls_dir.mkdir()
        self.client = app.app.test_client()
        app.app.testing = True
        self.patch_urls = patch.object(app, 'URLS_DIR', str(self.urls_dir))
        self.patch_urls.start()

    def tearDown(self):
        self.patch_urls.stop()
        self.temp_dir.cleanup()


class TestAddTaskAPI(PlaylistSubmitTestCase):
    def test_single_video_adds_one_task(self):
        url = 'https://www.youtube.com/watch?v=aaa'
        with patch('app.expand_task_urls', return_value=([url], None)):
            response = self.client.post(
                '/api/add_task',
                json={'url': url, 'types': ['video']},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(len(data['tasks']), 1)

    def test_playlist_expands_into_multiple_tasks(self):
        urls = [
            'https://www.youtube.com/watch?v=a1',
            'https://www.youtube.com/watch?v=b2',
            'https://www.youtube.com/watch?v=c3',
        ]
        with patch('app.expand_task_urls', return_value=(urls, None)):
            response = self.client.post(
                '/api/add_task',
                json={'url': 'playlist', 'types': ['video']},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data['tasks']), 3)
        self.assertIn('3 个视频', data['msg'])

    def test_playlist_failure_returns_400(self):
        with patch(
            'app.expand_task_urls',
            return_value=(None, '解析播放列表失败: boom'),
        ):
            response = self.client.post(
                '/api/add_task',
                json={'url': 'playlist', 'types': ['video']},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])


class TestIndexPlaylistSubmit(PlaylistSubmitTestCase):
    def test_playlist_post_redirects_with_all_tasks(self):
        urls = [
            'https://www.youtube.com/watch?v=a1',
            'https://www.youtube.com/watch?v=b2',
        ]
        with patch('app.expand_task_urls', return_value=(urls, None)):
            response = self.client.post(
                '/',
                data={'url': 'playlist', 'type': ['video']},
            )

        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers['Location']).query)
        tasks = query['tasks'][0].split(',')
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            len(list(self.urls_dir.glob('*.txt'))),
            2,
        )

    def test_playlist_error_renders_error_message(self):
        with patch(
            'app.expand_task_urls',
            return_value=(None, '解析播放列表失败: boom'),
        ):
            response = self.client.post(
                '/',
                data={'url': 'playlist', 'type': ['video']},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn('解析播放列表失败: boom', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
