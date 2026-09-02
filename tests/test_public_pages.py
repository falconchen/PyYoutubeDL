import unittest
from pathlib import Path
from unittest.mock import patch

from app import app


class TestPublicInformationPages(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True

    def test_public_information_pages_are_available(self):
        expected_content = {
            '/about': '一个轻量、可自托管的媒体下载工具',
            '/terms': '合法与授权使用',
            '/privacy': '本实例处理的数据',
        }

        for path, text in expected_content.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                html = response.get_data(as_text=True)

                self.assertEqual(response.status_code, 200)
                self.assertIn(text, html)
                self.assertIn('href="/about"', html)
                self.assertIn('href="/terms"', html)
                self.assertIn('href="/privacy"', html)

    def test_download_page_links_to_public_information_pages(self):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-label="站点信息"', html)
        self.assertIn('href="/about"', html)
        self.assertIn('href="/terms"', html)
        self.assertIn('href="/privacy"', html)

    def test_site_footer_has_top_padding(self):
        css = Path(app.static_folder, 'style.css').read_text(encoding='utf-8')
        footer_rule = css.split('.site-footer {', 1)[1].split('}', 1)[0]

        self.assertIn('padding-top: 1rem;', footer_rule)


class TestIndexOAuthStatus(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True

    @patch('youtube_auth.load_user_profile', return_value={
        'channel_id': 'UCtest',
        'name': '测试频道',
        'avatar_url': 'https://example.com/avatar.png',
    })
    @patch('youtube_auth.load_token', return_value={'token': 'x'})
    def test_index_hides_avatar_and_name_when_authorized(
        self, load_token, load_profile,
    ):
        # 公开站点：即使已授权且有本地用户资料，也不渲染头像/用户名
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('测试频道', html)
        self.assertNotIn('https://example.com/avatar.png', html)
        self.assertIn('重新授权', html)

    @patch('youtube_auth.load_user_profile', return_value=None)
    @patch('youtube_auth.load_token', return_value=None)
    def test_index_shows_login_link_when_not_authorized(
        self, load_token, load_profile,
    ):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('登录', html)
        self.assertNotIn('使用 Google 账号登录', html)

    @patch('youtube_auth.load_user_profile', return_value=None)
    @patch('youtube_auth.load_token', return_value={'token': 'x'})
    def test_index_shows_relogin_link_when_authorized_without_profile(
        self, load_token, load_profile,
    ):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('重新授权', html)
        self.assertNotIn('已授权', html)


if __name__ == '__main__':
    unittest.main()
