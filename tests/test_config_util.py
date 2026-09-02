import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import config_util
from config_util import DEFAULT_CONFIG


class TestConfigDefaults(unittest.TestCase):
    def test_flask_port_defaults_to_5100(self):
        self.assertEqual(DEFAULT_CONFIG['FLASK_PORT'], 5100)

    def test_interrupted_download_resume_defaults_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG['RESUME_INTERRUPTED_DOWNLOADS'], False)


class TestConfigLoading(unittest.TestCase):
    def load_config_text(self, content, default_config, config_keys=None):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / 'config.json').write_text(content, encoding='utf-8')
            fake_module_path = root_path / 'config_util.py'
            with patch.object(config_util, '__file__', str(fake_module_path)):
                result = config_util.load_config(
                    default_config=default_config,
                    config_keys=[] if config_keys is None else config_keys,
                )
            return result, root_path

    def test_strict_json_remains_supported(self):
        result, _ = self.load_config_text(
            '{"FLASK_PORT": 5200}',
            {'FLASK_PORT': 5100},
        )

        self.assertEqual(result['FLASK_PORT'], 5200)

    def test_javascript_comments_and_trailing_commas_are_supported(self):
        result, _ = self.load_config_text(
            '''{
                // 单行注释
                "FLASK_PORT": 5300,
                /* 块注释 */
                "RESUME_INTERRUPTED_DOWNLOADS": true,
            }''',
            {
                'FLASK_PORT': 5100,
                'RESUME_INTERRUPTED_DOWNLOADS': False,
            },
        )

        self.assertEqual(result['FLASK_PORT'], 5300)
        self.assertIs(result['RESUME_INTERRUPTED_DOWNLOADS'], True)

    def test_comment_markers_inside_strings_are_preserved(self):
        result, _ = self.load_config_text(
            '''{
                "API_URL": "https://example.com/path//segment",
                "TOKEN": "prefix//middle/*suffix*/",
            }''',
            {'API_URL': '', 'TOKEN': ''},
        )

        self.assertEqual(
            result['API_URL'],
            'https://example.com/path//segment',
        )
        self.assertEqual(result['TOKEN'], 'prefix//middle/*suffix*/')

    def test_commented_path_override_is_resolved_from_project_root(self):
        result, root_path = self.load_config_text(
            '''{
                // 使用相对路径
                "URLS_DIR": "./commented-urls",
            }''',
            {'URLS_DIR': './urls'},
            config_keys=['URLS_DIR'],
        )

        self.assertEqual(
            result['URLS_DIR'],
            str(root_path / 'commented-urls'),
        )

    def test_invalid_jsonc_falls_back_to_defaults(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result, _ = self.load_config_text(
                '{"FLASK_PORT": }',
                {'FLASK_PORT': 5100},
            )

        self.assertEqual(result['FLASK_PORT'], 5100)
        self.assertIn('加载配置文件失败，使用默认配置', output.getvalue())

    def test_hash_comments_are_not_supported(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result, _ = self.load_config_text(
                '{# 不支持这种注释\n"FLASK_PORT": 5400}',
                {'FLASK_PORT': 5100},
            )

        self.assertEqual(result['FLASK_PORT'], 5100)
        self.assertIn('加载配置文件失败，使用默认配置', output.getvalue())


class TestPlaylistMonitorConfig(unittest.TestCase):
    def test_oauth_redirect_uri_default(self):
        self.assertEqual(
            DEFAULT_CONFIG['GOOGLE_OAUTH_REDIRECT_URI'],
            'https://yter.cellmean.com/oauth/callback',
        )

    def test_poll_interval_default(self):
        self.assertEqual(DEFAULT_CONFIG['PLAYLIST_POLL_INTERVAL_SECONDS'], 300)

    def test_monitor_playlists_default_empty(self):
        self.assertEqual(DEFAULT_CONFIG['MONITOR_PLAYLISTS'], {})

    def test_oauth_user_file_default(self):
        self.assertEqual(
            DEFAULT_CONFIG['GOOGLE_OAUTH_USER_FILE'],
            './data/youtube_user.json',
        )

    def test_is_playlist_monitor_enabled(self):
        cfg = {
            'GOOGLE_OAUTH_CLIENT_ID': 'cid',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'secret',
            'MONITOR_PLAYLISTS': {'PL1': ['video']},
        }
        self.assertTrue(config_util.is_playlist_monitor_enabled(cfg))

    def test_is_playlist_monitor_disabled_without_secret(self):
        cfg = {
            'GOOGLE_OAUTH_CLIENT_ID': 'cid',
            'GOOGLE_OAUTH_CLIENT_SECRET': '',
            'MONITOR_PLAYLISTS': {'PL1': ['video']},
        }
        self.assertFalse(config_util.is_playlist_monitor_enabled(cfg))

    def test_is_playlist_monitor_disabled_without_playlists(self):
        cfg = {
            'GOOGLE_OAUTH_CLIENT_ID': 'cid',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'secret',
            'MONITOR_PLAYLISTS': {},
        }
        self.assertFalse(config_util.is_playlist_monitor_enabled(cfg))


if __name__ == '__main__':
    unittest.main()
