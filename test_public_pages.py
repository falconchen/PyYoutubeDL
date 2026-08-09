import unittest

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


if __name__ == '__main__':
    unittest.main()
