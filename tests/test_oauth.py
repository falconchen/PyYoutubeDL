import json
import unittest
from unittest.mock import MagicMock, patch

from app import app


class TestOAuthRoutes(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_redirects_to_google(self, build_flow):
        flow = MagicMock()
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

    @patch('youtube_auth.build_oauth_flow')
    @patch('youtube_auth.save_token')
    @patch('youtube_auth.clear_fail_lock')
    @patch('bark_util.bark_notify')
    def test_oauth_callback_saves_token_and_clears_lock(
        self, bark_notify, clear_lock, save_token, build_flow,
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

        response = self.client.get('/oauth/callback?code=CODE&state=fixed_state')

        self.assertEqual(response.status_code, 200)
        flow.fetch_token.assert_called_once_with(code='CODE')
        save_token.assert_called_once()
        clear_lock.assert_called_once()

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

        response = self.client.get('/oauth/callback?code=CODE&state=s')

        self.assertEqual(response.status_code, 400)
        flow.fetch_token.assert_called_once_with(code='CODE')


if __name__ == '__main__':
    unittest.main()
