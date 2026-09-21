import unittest
from unittest.mock import patch

from url_security import validate_download_url


class TestDownloadUrlSecurity(unittest.TestCase):
    def test_rejects_loopback_and_metadata_ip_literals(self):
        for url in (
            'http://127.0.0.1:22/',
            'http://localhost/',
            'http://169.254.169.254/latest/meta-data/',
            'http://[::1]/',
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(validate_download_url(url))

    def test_rejects_obfuscated_ipv4_literal(self):
        self.assertIsNotNone(validate_download_url('http://2130706433/'))

    def test_rejects_non_http_schemes_and_credentials(self):
        self.assertIsNotNone(validate_download_url('file:///etc/passwd'))
        self.assertIsNotNone(validate_download_url('http://user:pass@example.com/'))

    def test_rejects_domain_resolving_to_private_address(self):
        with patch(
            'url_security.socket.getaddrinfo',
            return_value=[(2, 1, 6, '', ('10.0.0.8', 80))],
        ):
            self.assertIsNotNone(validate_download_url('https://video.example/'))

    def test_allows_public_domain_and_custom_block(self):
        with patch(
            'url_security.socket.getaddrinfo',
            return_value=[(2, 1, 6, '', ('93.184.216.34', 443))],
        ):
            self.assertIsNone(validate_download_url('https://video.example/'))
            self.assertIsNotNone(validate_download_url(
                'https://video.example/',
                {'DOWNLOAD_URL_BLOCKED_HOSTS': ['video.example']},
            ))

    def test_custom_cidr_and_wildcard_blocks_are_supported(self):
        with patch(
            'url_security.socket.getaddrinfo',
            return_value=[(2, 1, 6, '', ('203.0.113.8', 443))],
        ):
            self.assertIsNotNone(validate_download_url(
                'https://cdn.example/',
                {'DOWNLOAD_URL_BLOCKED_HOSTS': ['203.0.113.0/24']},
            ))
            self.assertIsNotNone(validate_download_url(
                'https://cdn.example/',
                {'DOWNLOAD_URL_BLOCKED_HOSTS': ['*.example']},
            ))


if __name__ == '__main__':
    unittest.main()
