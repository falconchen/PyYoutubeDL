"""个人访问令牌：存储、个人页管理、API 认证与日志范围。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
import user_store
from app import app


class AccessTokenStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name, 'users.sqlite3'))
        user_store.init_db(self.db_path)
        self.user = user_store.create_user(
            self.db_path, 'owner@example.com', password='password123'
        )

    def test_only_hash_is_stored(self):
        token, record = user_store.create_access_token(
            self.db_path, self.user['id'], name='ext'
        )
        self.assertTrue(token.startswith('dlpat_'))
        self.assertNotIn('token_hash', record)
        self.assertTrue(token.startswith(record['prefix']))
        with sqlite3.connect(self.db_path) as db:
            stored = db.execute('SELECT token_hash FROM access_tokens').fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(stored, user_store.hash_access_token(token))

    def test_authenticate_records_last_use(self):
        token, _ = user_store.create_access_token(self.db_path, self.user['id'])
        user = user_store.authenticate_access_token(self.db_path, token)
        self.assertEqual(user['id'], self.user['id'])
        self.assertIsNotNone(
            user_store.list_access_tokens(self.db_path, self.user['id'])[0]['last_used_at']
        )

    def test_rejects_unknown_expired_revoked_and_disabled(self):
        self.assertIsNone(user_store.authenticate_access_token(self.db_path, 'dlpat_nope'))
        self.assertIsNone(user_store.authenticate_access_token(self.db_path, ''))

        token, record = user_store.create_access_token(
            self.db_path, self.user['id'], expires_in_days=1
        )
        with patch.object(user_store, 'now_ts', return_value=record['expires_at']):
            self.assertIsNone(user_store.authenticate_access_token(self.db_path, token))

        self.assertTrue(
            user_store.revoke_access_token(self.db_path, self.user['id'], record['id'])
        )
        self.assertIsNone(user_store.authenticate_access_token(self.db_path, token))

        token, _ = user_store.create_access_token(self.db_path, self.user['id'])
        user_store.set_status(self.db_path, self.user['id'], user_store.STATUS_DISABLED)
        self.assertIsNone(user_store.authenticate_access_token(self.db_path, token))

    def test_cannot_revoke_other_users_token(self):
        other = user_store.create_user(self.db_path, 'b@example.com', password='x' * 8)
        _, record = user_store.create_access_token(self.db_path, self.user['id'])
        self.assertFalse(
            user_store.revoke_access_token(self.db_path, other['id'], record['id'])
        )

    def test_migrates_version_two_database(self):
        db_path = str(Path(self.directory.name, 'old.sqlite3'))
        user_store.init_db(db_path)
        with sqlite3.connect(db_path) as db:
            db.execute('DROP TABLE access_tokens')
            db.execute('PRAGMA user_version = 2')
        user_store.init_db(db_path)
        with sqlite3.connect(db_path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            db.execute('SELECT * FROM access_tokens')


class AccessTokenAppTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name, 'users.sqlite3'))
        user_store.init_db(self.db_path)

        patches = {'USER_DB_PATH': self.db_path}
        for name in ('URLS_DIR', 'FILES_DIR'):
            path = Path(self.directory.name, name.lower())
            path.mkdir()
            patches[name] = str(path)
        for name, value in patches.items():
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.log_dir = Path(self.directory.name, 'logs')
        self.log_dir.mkdir()
        config_patcher = patch.dict(app_module.config, {
            'LOG_DIR': str(self.log_dir),
            'EXTENSION_LOG_TOKEN': 'legacy-token',
        })
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        self.admin = user_store.create_user(
            self.db_path, 'admin@example.com', password='password123'
        )
        self.member = user_store.create_user(
            self.db_path, 'member@example.com', password='password123'
        )
        user_store.set_status(self.db_path, self.member['id'], user_store.STATUS_ACTIVE)

        app.testing = True
        self.client = app.test_client()

    def login(self, user):
        with self.client.session_transaction() as session:
            session[app_module.SESSION_USER_KEY] = user['id']

    def bearer(self, token):
        return {'Authorization': f'Bearer {token}'}

    def test_profile_requires_login(self):
        response = self.client.get('/profile')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_create_token_shows_plaintext_once(self):
        self.login(self.member)
        response = self.client.post(
            '/profile/tokens', data={'name': 'laptop', 'expires_in': '30'}
        )
        self.assertEqual(response.status_code, 302)

        page = self.client.get('/profile').get_data(as_text=True)
        tokens = user_store.list_access_tokens(self.db_path, self.member['id'])
        self.assertEqual(len(tokens), 1)
        self.assertIsNotNone(tokens[0]['expires_at'])
        self.assertIn('laptop', page)
        self.assertEqual(page.count('value="dlpat_'), 1)

        again = self.client.get('/profile').get_data(as_text=True)
        self.assertNotIn('value="dlpat_', again)

    def test_invalid_expiry_is_rejected(self):
        self.login(self.member)
        response = self.client.post('/profile/tokens', data={'expires_in': '7'})
        self.assertEqual(response.status_code, 400)

    def test_revoke_only_own_tokens(self):
        _, record = user_store.create_access_token(self.db_path, self.admin['id'])
        self.login(self.member)
        response = self.client.post(f'/profile/tokens/{record["id"]}/revoke')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(user_store.list_access_tokens(self.db_path, self.admin['id'])), 1)

    def test_token_cannot_manage_tokens(self):
        token, _ = user_store.create_access_token(self.db_path, self.member['id'])
        response = self.client.post(
            '/profile/tokens', data={'expires_in': 'never'}, headers=self.bearer(token)
        )
        self.assertNotEqual(response.status_code, 302)
        self.assertEqual(len(user_store.list_access_tokens(self.db_path, self.member['id'])), 1)

    def test_add_task_with_token_belongs_to_token_user(self):
        token, _ = user_store.create_access_token(self.db_path, self.member['id'])
        with patch('app.expand_task_urls', return_value=(['https://example.com/v'], None)), \
                patch.object(user_store, 'reserve_anonymous_downloads') as reserve:
            response = self.client.post(
                '/api/add_task',
                json={'url': 'https://example.com/v', 'types': ['video', 'audio']},
                headers=self.bearer(token),
            )
        self.assertEqual(response.status_code, 200)
        tasks = response.get_json()['tasks']
        self.assertEqual(len(tasks), 2)
        reserve.assert_not_called()
        for task in tasks:
            self.assertEqual(user_store.task_owner(self.db_path, task), self.member['id'])

        info = self.client.post(
            '/api/task_info', json={'tasks': tasks}, headers=self.bearer(token)
        ).get_json()
        self.assertTrue(all(item['exists'] for item in info['tasks']))

        admin_token, _ = user_store.create_access_token(self.db_path, self.admin['id'])
        info = self.client.post(
            '/api/task_info', json={'tasks': tasks}, headers=self.bearer(admin_token)
        ).get_json()
        self.assertTrue(all(item['state'] == 'missing' for item in info['tasks']))

    def test_invalid_token_is_rejected_instead_of_anonymous(self):
        with patch.object(user_store, 'reserve_anonymous_downloads') as reserve:
            response = self.client.post(
                '/api/add_task',
                json={'url': 'https://example.com/v', 'types': ['video']},
                headers=self.bearer('dlpat_invalid'),
            )
        self.assertEqual(response.status_code, 401)
        reserve.assert_not_called()

    def test_token_ignores_session_of_another_user(self):
        token, _ = user_store.create_access_token(self.db_path, self.member['id'])
        self.login(self.admin)
        response = self.client.get('/api/downloader_log', headers=self.bearer(token))
        self.assertEqual(response.get_json()['scope'], 'own')

    def write_log(self, member_task):
        (self.log_dir / 'downloader.log').write_text(
            f'开始下载: https://example.com/mine (video) {member_task}\n'
            'other user line https://example.com/secret\n'
            f'任务完成 {member_task}\n',
            encoding='utf-8',
        )

    def test_admin_token_reads_full_log(self):
        self.write_log('v20260917000000abc')
        token, _ = user_store.create_access_token(self.db_path, self.admin['id'])
        payload = self.client.get(
            '/api/downloader_log', headers=self.bearer(token)
        ).get_json()
        self.assertEqual(payload['scope'], 'all')
        self.assertIn('secret', payload['text'])

    def test_member_token_reads_only_own_lines(self):
        with patch('app.expand_task_urls', return_value=(['https://example.com/mine'], None)):
            token, _ = user_store.create_access_token(self.db_path, self.member['id'])
            task = self.client.post(
                '/api/add_task',
                json={'url': 'https://example.com/mine', 'types': ['video']},
                headers=self.bearer(token),
            ).get_json()['tasks'][0]
        self.write_log(task)

        response = self.client.get('/api/downloader_log', headers=self.bearer(token))
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['scope'], 'own')
        self.assertNotIn('secret', payload['text'])
        self.assertEqual(len(payload['text'].splitlines()), 2)
        self.assertGreater(payload['cursor'], 0)

    def test_log_requires_auth_and_legacy_token_still_works(self):
        self.write_log('v20260917000000abc')
        self.assertEqual(self.client.get('/api/downloader_log').status_code, 401)
        payload = self.client.get(
            '/api/downloader_log', headers={'X-Yter-Log-Token': 'legacy-token'}
        ).get_json()
        self.assertEqual(payload['scope'], 'all')
        self.assertIn('secret', payload['text'])


if __name__ == '__main__':
    unittest.main()
