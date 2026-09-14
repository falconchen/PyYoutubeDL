"""localhost:端口 → 127.0.0.1:端口 的重定向（仅在配置开启时）。"""
import unittest
from unittest.mock import patch
from urllib.parse import quote

import app as app_module
from app import app


class TestLocalhostRedirect(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.client = app.test_client()

    def enabled(self, value=True):
        return patch.dict(
            app_module.config, {'REDIRECT_LOCALHOST_TO_LOOPBACK': value}
        )

    def test_get_keeps_port_path_and_query_with_301(self):
        filename = '09141125-中文 文件.mp3'
        with self.enabled():
            response = self.client.get(
                f'/player?file={quote(filename)}',
                base_url='http://localhost:5200',
            )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response.headers['Location'],
            f'http://127.0.0.1:5200/player?file={quote(filename)}',
        )

    def test_post_uses_308_to_preserve_method(self):
        with self.enabled():
            response = self.client.post(
                '/api/add_task',
                json={'url': 'https://example.com/v', 'types': ['video']},
                base_url='http://localhost:5200',
            )

        self.assertEqual(response.status_code, 308)
        self.assertEqual(
            response.headers['Location'], 'http://127.0.0.1:5200/api/add_task'
        )

    def test_localhost_without_port(self):
        with self.enabled():
            response = self.client.get('/about', base_url='http://localhost')

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers['Location'], 'http://127.0.0.1/about')

    def test_loopback_address_is_served_directly(self):
        with self.enabled():
            response = self.client.get('/about', base_url='http://127.0.0.1:5200')

        self.assertEqual(response.status_code, 200)

    def test_disabled_by_default_so_proxies_forwarding_localhost_are_safe(self):
        with self.enabled(False):
            response = self.client.get('/about', base_url='http://localhost:5100')

        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
