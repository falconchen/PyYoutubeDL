import unittest
from pathlib import Path
from unittest.mock import patch

import user_store
from app import app
from auth_helper import logged_in_client


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
        with logged_in_client() as (client, _user, _db):
            response = client.get('/')
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


class TestAuthEntryPoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True

    def test_anonymous_index_redirects_to_login(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_login_page_renders_without_session(self):
        response = self.client.get('/login')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('登录', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="password"', html)

    def test_signed_in_header_shows_account_menu_not_oauth_status(self):
        with logged_in_client() as (client, user, _db):
            html = client.get('/').get_data(as_text=True)

        # 多用户后顶栏展示账号菜单与绑定入口，不再显示全局授权状态
        self.assertIn(user['display_name'], html)
        self.assertIn('绑定 Google 账号', html)
        self.assertIn('退出登录', html)
        self.assertNotIn('重新授权 Google', html)

    def test_bound_account_shows_rebind_and_unbind(self):
        with logged_in_client() as (client, user, db_path):
            user_store.link_identity(
                db_path,
                user_store.PROVIDER_GOOGLE,
                'google-sub-1',
                user['id'],
                email='bound@gmail.com',
            )
            html = client.get('/').get_data(as_text=True)

        self.assertIn('bound@gmail.com', html)
        self.assertIn('重新授权 Google', html)
        self.assertIn('解除绑定', html)


if __name__ == '__main__':
    unittest.main()
