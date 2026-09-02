import base64
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

import app as app_module
from app import app


class TestOAuthRoutes(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True
        # /oauth/start 的 Basic Auth 依赖 config.json 实际配置；测试里禁用，
        # 使路由行为不随本机 config.json 的凭据变化。Basic Auth 行为由
        # TestOAuthStartBasicAuth 单独覆盖。
        patch.dict(
            app_module.config,
            {'OAUTH_AUTH_USERNAME': '', 'OAUTH_AUTH_PASSWORD_SHA256': ''},
        ).start()
        self.addCleanup(patch.stopall)

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_redirects_to_google(self, build_flow):
        flow = MagicMock()
        flow.code_verifier = 'test-verifier'
        flow.authorization_url.return_value = (
            'https://accounts.google.com/o/oauth2/auth?client_id=x',
            'fixed_state',
        )
        build_flow.return_value = flow

        response = self.client.get('/oauth/start')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers['Location'].startswith('https://accounts.google.com')
        )
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['oauth_code_verifier'], 'test-verifier')

    @patch('youtube_auth.build_oauth_flow')
    @patch('youtube_auth.save_token')
    @patch('youtube_auth.clear_fail_lock')
    @patch('youtube_auth.fetch_user_profile', return_value={
        'channel_id': 'UCtest',
        'name': '测试频道',
        'avatar_url': 'https://example.com/avatar.png',
    })
    @patch('youtube_auth.save_user_profile')
    @patch('bark_util.bark_notify')
    def test_oauth_callback_saves_token_and_clears_lock(
        self, bark_notify, save_user_profile, fetch_user_profile,
        clear_lock, save_token, build_flow,
    ):
        flow = MagicMock()
        flow.credentials = MagicMock()
        flow.credentials.refresh_token = 'refresh-token'
        flow.credentials.to_json.return_value = json.dumps({
            'token': 'access-token',
            'refresh_token': 'refresh-token',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': 'cid',
            'client_secret': 'cs',
            'scopes': ['https://www.googleapis.com/auth/youtube'],
        })
        build_flow.return_value = flow

        with self.client.session_transaction() as sess:
            sess['oauth_state'] = 'fixed_state'
            sess['oauth_code_verifier'] = 'test-verifier'

        response = self.client.get('/oauth/callback?code=CODE&state=fixed_state')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(flow.code_verifier, 'test-verifier')
        flow.fetch_token.assert_called_once_with(code='CODE')
        save_token.assert_called_once()
        clear_lock.assert_called_once()
        fetch_user_profile.assert_called_once()
        save_user_profile.assert_called_once()

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_callback_rejects_state_mismatch(self, build_flow):
        with self.client.session_transaction() as sess:
            sess['oauth_state'] = 'expected'

        response = self.client.get('/oauth/callback?code=CODE&state=wrong')

        self.assertEqual(response.status_code, 400)
        build_flow.assert_not_called()

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_callback_requires_refresh_token(self, build_flow):
        flow = MagicMock()
        flow.credentials = MagicMock()
        flow.credentials.refresh_token = None
        build_flow.return_value = flow

        with self.client.session_transaction() as sess:
            sess['oauth_state'] = 's'
            sess['oauth_code_verifier'] = 'test-verifier'

        response = self.client.get('/oauth/callback?code=CODE&state=s')

        self.assertEqual(response.status_code, 400)
        flow.fetch_token.assert_called_once_with(code='CODE')

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_callback_requires_code_verifier(self, build_flow):
        flow = MagicMock()
        build_flow.return_value = flow

        with self.client.session_transaction() as sess:
            sess['oauth_state'] = 's'

        response = self.client.get('/oauth/callback?code=CODE&state=s')

        self.assertEqual(response.status_code, 400)
        flow.fetch_token.assert_not_called()


class TestOAuthStartBasicAuth(unittest.TestCase):
    """/oauth/start 的 Python 原生 Basic Auth 校验（配置启用时）。"""

    USERNAME = 'test-user'
    PASSWORD = 'test-pass'
    PASSWORD_SHA256 = hashlib.sha256(PASSWORD.encode('utf-8')).hexdigest()

    def setUp(self):
        self.client = app.test_client()
        app.testing = True
        patch.dict(
            app_module.config,
            {
                'OAUTH_AUTH_USERNAME': self.USERNAME,
                'OAUTH_AUTH_PASSWORD_SHA256': self.PASSWORD_SHA256,
            },
        ).start()
        self.addCleanup(patch.stopall)

    def _auth_header(self, username, password):
        token = base64.b64encode(
            f'{username}:{password}'.encode('utf-8')
        ).decode('ascii')
        return {'Authorization': f'Basic {token}'}

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_requires_credentials(self, build_flow):
        response = self.client.get('/oauth/start')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.headers['WWW-Authenticate'],
            'Basic realm="PyYoutubeDL OAuth"',
        )
        build_flow.assert_not_called()

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_rejects_wrong_password(self, build_flow):
        response = self.client.get(
            '/oauth/start',
            headers=self._auth_header(self.USERNAME, 'wrong-pass'),
        )

        self.assertEqual(response.status_code, 401)
        build_flow.assert_not_called()

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_accepts_valid_credentials(self, build_flow):
        flow = MagicMock()
        flow.code_verifier = 'test-verifier'
        flow.authorization_url.return_value = (
            'https://accounts.google.com/o/oauth2/auth?client_id=x',
            'fixed_state',
        )
        build_flow.return_value = flow

        response = self.client.get(
            '/oauth/start',
            headers=self._auth_header(self.USERNAME, self.PASSWORD),
        )

        self.assertEqual(response.status_code, 302)
        build_flow.assert_called_once()
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['oauth_code_verifier'], 'test-verifier')

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_passes_through_when_unconfigured(self, build_flow):
        """OAUTH_AUTH_* 未配置时不应要求认证（向后兼容）。"""
        flow = MagicMock()
        flow.code_verifier = 'test-verifier'
        flow.authorization_url.return_value = (
            'https://accounts.google.com/o/oauth2/auth?client_id=x',
            'fixed_state',
        )
        build_flow.return_value = flow

        with patch.dict(
            app_module.config,
            {'OAUTH_AUTH_USERNAME': '', 'OAUTH_AUTH_PASSWORD_SHA256': ''},
        ):
            response = self.client.get('/oauth/start')

        self.assertEqual(response.status_code, 302)


if __name__ == '__main__':
    unittest.main()
