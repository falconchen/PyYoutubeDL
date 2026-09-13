import base64
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

import tempfile
from pathlib import Path

import app as app_module
import user_store
import youtube_auth
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
            {
                'OAUTH_AUTH_USERNAME': '',
                'OAUTH_AUTH_PASSWORD_SHA256': '',
                'ENABLE_OAUTH_BASIC_AUTH': False,
            },
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

    def _flow_with_credentials(self, refresh_token='refresh-token'):
        flow = MagicMock()
        flow.credentials = MagicMock()
        flow.credentials.refresh_token = refresh_token
        flow.credentials.token = 'access-token'
        flow.credentials.to_json.return_value = json.dumps({
            'token': 'access-token',
            'refresh_token': refresh_token,
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': 'cid',
            'client_secret': 'cs',
            'scopes': list(youtube_auth.FULL_SCOPES),
        })
        return flow

    @patch('app.fetch_google_userinfo')
    @patch('youtube_auth.build_oauth_flow')
    def test_google_login_creates_first_admin_and_signs_in(
        self, build_flow, userinfo,
    ):
        flow = self._flow_with_credentials()
        build_flow.return_value = flow
        userinfo.return_value = {
            'sub': 'google-sub-1',
            'email': 'first@example.com',
            'name': '第一个用户',
        }

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory, 'users.sqlite3'))
            user_store.init_db(db_path)
            with patch.object(app_module, 'USER_DB_PATH', db_path):
                with self.client.session_transaction() as sess:
                    sess['oauth_state'] = 'fixed_state'
                    sess['oauth_code_verifier'] = 'test-verifier'
                    sess['oauth_intent'] = 'login'

                response = self.client.get(
                    '/oauth/callback?code=CODE&state=fixed_state'
                )

                user = user_store.get_user_by_email(db_path, 'first@example.com')
                identity = user_store.get_identity(
                    db_path, user_store.PROVIDER_GOOGLE, 'google-sub-1'
                )
                token = user_store.load_google_token(db_path, user['id'])
                with self.client.session_transaction() as sess:
                    signed_in = sess.get(app_module.SESSION_USER_KEY)

        # 首个 Google 登录者同样成为管理员并直接可用
        self.assertEqual(response.status_code, 302)
        self.assertEqual(flow.code_verifier, 'test-verifier')
        self.assertEqual(user['role'], user_store.ROLE_ADMIN)
        self.assertEqual(user['status'], user_store.STATUS_ACTIVE)
        self.assertEqual(identity['user_id'], user['id'])
        self.assertIn('refresh-token', token['token_json'])
        self.assertEqual(signed_in, user['id'])

    @patch('app.fetch_google_userinfo')
    @patch('youtube_auth.build_oauth_flow')
    def test_google_login_second_user_waits_for_approval(
        self, build_flow, userinfo,
    ):
        build_flow.return_value = self._flow_with_credentials()
        userinfo.return_value = {
            'sub': 'google-sub-2',
            'email': 'second@example.com',
            'name': '第二个用户',
        }

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory, 'users.sqlite3'))
            user_store.init_db(db_path)
            user_store.create_user(db_path, 'admin@example.com', password='pw123456')
            with patch.object(app_module, 'USER_DB_PATH', db_path):
                with self.client.session_transaction() as sess:
                    sess['oauth_state'] = 's'
                    sess['oauth_code_verifier'] = 'v'
                    sess['oauth_intent'] = 'login'

                response = self.client.get('/oauth/callback?code=CODE&state=s')
                created = user_store.get_user_by_email(db_path, 'second@example.com')
                with self.client.session_transaction() as sess:
                    signed_in = sess.get(app_module.SESSION_USER_KEY)

        self.assertEqual(response.status_code, 200)
        self.assertIn('等待管理员审批', response.get_data(as_text=True))
        self.assertEqual(created['status'], user_store.STATUS_PENDING)
        self.assertIsNone(signed_in)

    @patch('app.fetch_google_userinfo')
    @patch('youtube_auth.build_oauth_flow')
    def test_google_bind_attaches_token_to_current_user(
        self, build_flow, userinfo,
    ):
        build_flow.return_value = self._flow_with_credentials()
        userinfo.return_value = {
            'sub': 'google-sub-3',
            'email': 'bound@gmail.com',
            'name': '绑定账号',
        }

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory, 'users.sqlite3'))
            user_store.init_db(db_path)
            owner = user_store.create_user(
                db_path, 'owner@example.com', password='pw123456'
            )
            with patch.object(app_module, 'USER_DB_PATH', db_path):
                with self.client.session_transaction() as sess:
                    sess[app_module.SESSION_USER_KEY] = owner['id']
                    sess['oauth_state'] = 's'
                    sess['oauth_code_verifier'] = 'v'
                    sess['oauth_intent'] = 'bind'

                response = self.client.get('/oauth/callback?code=CODE&state=s')
                identity = user_store.get_identity_for_user(
                    db_path, user_store.PROVIDER_GOOGLE, owner['id']
                )
                token = user_store.load_google_token(db_path, owner['id'])

        self.assertEqual(response.status_code, 200)
        self.assertIn('绑定成功', response.get_data(as_text=True))
        self.assertEqual(identity['provider_user_id'], 'google-sub-3')
        # 绑定申请的是含 YouTube 与 Drive 的完整 scope
        self.assertIn('drive.file', token['scopes'])
        self.assertIn('youtube', token['scopes'])

    @patch('app.fetch_google_userinfo')
    @patch('youtube_auth.build_oauth_flow')
    def test_google_bind_rejects_account_bound_elsewhere(
        self, build_flow, userinfo,
    ):
        build_flow.return_value = self._flow_with_credentials()
        userinfo.return_value = {'sub': 'shared-sub', 'email': 'x@gmail.com'}

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory, 'users.sqlite3'))
            user_store.init_db(db_path)
            first = user_store.create_user(db_path, 'a@example.com', password='pw123456')
            second = user_store.create_user(db_path, 'b@example.com', password='pw123456')
            user_store.set_status(db_path, second['id'], user_store.STATUS_ACTIVE)
            user_store.link_identity(
                db_path, user_store.PROVIDER_GOOGLE, 'shared-sub', first['id']
            )
            with patch.object(app_module, 'USER_DB_PATH', db_path):
                with self.client.session_transaction() as sess:
                    sess[app_module.SESSION_USER_KEY] = second['id']
                    sess['oauth_state'] = 's'
                    sess['oauth_code_verifier'] = 'v'
                    sess['oauth_intent'] = 'bind'

                response = self.client.get('/oauth/callback?code=CODE&state=s')

        self.assertEqual(response.status_code, 409)
        self.assertIn('已绑定到其他用户', response.get_data(as_text=True))

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_callback_rejects_state_mismatch(self, build_flow):
        with self.client.session_transaction() as sess:
            sess['oauth_state'] = 'expected'

        response = self.client.get('/oauth/callback?code=CODE&state=wrong')

        self.assertEqual(response.status_code, 400)
        build_flow.assert_not_called()

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_callback_requires_refresh_token(self, build_flow):
        flow = self._flow_with_credentials(refresh_token=None)
        build_flow.return_value = flow

        with tempfile.TemporaryDirectory() as directory, patch(
            'app.fetch_google_userinfo',
            return_value={'sub': 'sub-x', 'email': 'x@gmail.com'},
        ):
            db_path = str(Path(directory, 'users.sqlite3'))
            user_store.init_db(db_path)
            owner = user_store.create_user(
                db_path, 'owner@example.com', password='pw123456'
            )
            with patch.object(app_module, 'USER_DB_PATH', db_path):
                with self.client.session_transaction() as sess:
                    sess[app_module.SESSION_USER_KEY] = owner['id']
                    sess['oauth_state'] = 's'
                    sess['oauth_code_verifier'] = 'test-verifier'
                    sess['oauth_intent'] = 'bind'

                response = self.client.get('/oauth/callback?code=CODE&state=s')

        # 绑定必须拿到离线凭据，否则后台 worker 无法续期
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
                'ENABLE_OAUTH_BASIC_AUTH': True,
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
            {
                'OAUTH_AUTH_USERNAME': '',
                'OAUTH_AUTH_PASSWORD_SHA256': '',
                'ENABLE_OAUTH_BASIC_AUTH': True,
            },
        ):
            response = self.client.get('/oauth/start')

        self.assertEqual(response.status_code, 302)

    @patch('youtube_auth.build_oauth_flow')
    def test_oauth_start_disabled_when_flag_false(self, build_flow):
        """开关关闭时（如反代层已有整站 Basic Auth），即使配了凭据也不要求认证。"""
        flow = MagicMock()
        flow.code_verifier = 'test-verifier'
        flow.authorization_url.return_value = (
            'https://accounts.google.com/o/oauth2/auth?client_id=x',
            'fixed_state',
        )
        build_flow.return_value = flow

        with patch.dict(
            app_module.config,
            {'ENABLE_OAUTH_BASIC_AUTH': False},
        ):
            response = self.client.get('/oauth/start')

        self.assertEqual(response.status_code, 302)
        build_flow.assert_called_once()


if __name__ == '__main__':
    unittest.main()
