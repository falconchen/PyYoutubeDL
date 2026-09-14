"""多用户：注册、审批、登录、会话失效与任务隔离。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
import user_store
from app import app


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name, 'users.sqlite3'))
        user_store.init_db(self.db_path)

        patcher = patch.object(app_module, 'USER_DB_PATH', self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 注册流程会扫描这两个目录接管历史数据，指到空目录避免读到开发机数据
        for name in ('URLS_DIR', 'FILES_DIR'):
            empty = Path(self.directory.name, name.lower())
            empty.mkdir(exist_ok=True)
            dir_patcher = patch.object(app_module, name, str(empty))
            dir_patcher.start()
            self.addCleanup(dir_patcher.stop)

        app.testing = True
        self.client = app.test_client()

    def register(self, email, password='password123', client=None):
        return (client or self.client).post(
            '/register',
            data={'email': email, 'password': password},
            follow_redirects=False,
        )

    def login(self, email, password='password123', client=None):
        return (client or self.client).post(
            '/login',
            data={'email': email, 'password': password},
            follow_redirects=False,
        )


class TestRegistrationAndApproval(AuthTestCase):
    def test_first_registration_becomes_active_admin_and_signs_in(self):
        response = self.register('first@example.com')
        user = user_store.get_user_by_email(self.db_path, 'first@example.com')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(user['role'], user_store.ROLE_ADMIN)
        self.assertEqual(user['status'], user_store.STATUS_ACTIVE)
        with self.client.session_transaction() as session:
            self.assertEqual(session[app_module.SESSION_USER_KEY], user['id'])

    def test_second_registration_waits_for_approval(self):
        self.register('first@example.com')
        second_client = app.test_client()

        response = self.register('second@example.com', client=second_client)
        user = user_store.get_user_by_email(self.db_path, 'second@example.com')

        self.assertEqual(response.status_code, 200)
        self.assertIn('管理员审批', response.get_data(as_text=True))
        self.assertEqual(user['status'], user_store.STATUS_PENDING)
        with second_client.session_transaction() as session:
            self.assertIsNone(session.get(app_module.SESSION_USER_KEY))

    def test_pending_user_cannot_log_in_until_approved(self):
        self.register('first@example.com')
        second = user_store.create_user(
            self.db_path, 'second@example.com', password='password123'
        )
        second_client = app.test_client()

        blocked = self.login('second@example.com', client=second_client)
        self.assertEqual(blocked.status_code, 400)
        self.assertIn('等待管理员审批', blocked.get_data(as_text=True))

        user_store.set_status(self.db_path, second['id'], user_store.STATUS_ACTIVE)
        allowed = self.login('second@example.com', client=second_client)
        self.assertEqual(allowed.status_code, 302)

    def test_short_password_is_rejected(self):
        response = self.register('first@example.com', password='short')

        self.assertEqual(response.status_code, 400)
        self.assertIn('密码至少 8 位', response.get_data(as_text=True))
        self.assertIsNone(user_store.get_user_by_email(self.db_path, 'first@example.com'))

    def test_duplicate_email_is_rejected(self):
        self.register('first@example.com')

        response = self.register('first@example.com', client=app.test_client())

        self.assertEqual(response.status_code, 400)
        self.assertIn('已被注册', response.get_data(as_text=True))

    def test_registration_can_be_closed_after_first_admin(self):
        self.register('first@example.com')

        with patch.dict(app_module.config, {'REGISTRATION_OPEN': False}):
            response = self.register('second@example.com', client=app.test_client())

        self.assertEqual(response.status_code, 403)


class TestLoginBehaviour(AuthTestCase):
    def test_wrong_password_does_not_reveal_whether_email_exists(self):
        self.register('first@example.com')
        client = app.test_client()

        unknown = self.login('nobody@example.com', client=client)
        wrong = self.login('first@example.com', password='bad-password', client=client)

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(wrong.status_code, 400)
        self.assertIn('邮箱或密码不正确', unknown.get_data(as_text=True))
        self.assertIn('邮箱或密码不正确', wrong.get_data(as_text=True))

    def test_disabling_a_user_invalidates_the_existing_session(self):
        self.register('first@example.com')
        user = user_store.get_user_by_email(self.db_path, 'first@example.com')
        self.assertEqual(self.client.get('/api/media_list').status_code, 200)

        user_store.set_status(self.db_path, user['id'], user_store.STATUS_DISABLED)

        # 停用后旧 cookie 立即失效，并退回匿名媒体库身份。
        self.assertEqual(self.client.get('/api/media_list').status_code, 200)
        self.assertNotIn('退出登录', self.client.get('/').get_data(as_text=True))

    def test_logout_clears_the_session(self):
        self.register('first@example.com')

        self.client.post('/logout')

        self.assertEqual(self.client.get('/api/media_list').status_code, 200)
        self.assertNotIn('退出登录', self.client.get('/').get_data(as_text=True))

    def test_login_only_redirects_to_local_paths(self):
        self.register('first@example.com')
        user_store.set_status(
            self.db_path,
            user_store.get_user_by_email(self.db_path, 'first@example.com')['id'],
            user_store.STATUS_ACTIVE,
        )
        client = app.test_client()

        response = client.post(
            '/login',
            data={
                'email': 'first@example.com',
                'password': 'password123',
                'next': 'https://evil.example.com/steal',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn('evil.example.com', response.headers['Location'])


class TestAdminPage(AuthTestCase):
    def _admin_and_member(self):
        self.register('admin@example.com')
        member = user_store.create_user(
            self.db_path, 'member@example.com', password='password123'
        )
        return user_store.get_user_by_email(self.db_path, 'admin@example.com'), member

    def test_admin_can_list_and_approve_pending_users(self):
        _admin, member = self._admin_and_member()

        page = self.client.get('/admin')
        self.assertEqual(page.status_code, 200)
        self.assertIn('member@example.com', page.get_data(as_text=True))
        self.assertIn('待审批', page.get_data(as_text=True))

        self.client.post(
            f'/admin/users/{member["id"]}/status', data={'status': 'active'}
        )

        refreshed = user_store.get_user(self.db_path, member['id'])
        self.assertEqual(refreshed['status'], user_store.STATUS_ACTIVE)

    def test_non_admin_cannot_open_admin_page(self):
        _admin, member = self._admin_and_member()
        user_store.set_status(self.db_path, member['id'], user_store.STATUS_ACTIVE)
        member_client = app.test_client()
        with member_client.session_transaction() as session:
            session[app_module.SESSION_USER_KEY] = member['id']

        self.assertEqual(member_client.get('/admin').status_code, 403)

    def test_admin_cannot_disable_their_own_account(self):
        admin, _member = self._admin_and_member()

        response = self.client.post(
            f'/admin/users/{admin["id"]}/status', data={'status': 'disabled'}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            user_store.get_user(self.db_path, admin['id'])['status'],
            user_store.STATUS_ACTIVE,
        )

    def test_anonymous_admin_page_redirects_to_login(self):
        response = app.test_client().get('/admin')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])


class TestTaskIsolation(AuthTestCase):
    def test_task_info_hides_other_users_tasks(self):
        self.register('first@example.com')
        owner = user_store.get_user_by_email(self.db_path, 'first@example.com')
        stranger = user_store.create_user(
            self.db_path, 'other@example.com', password='password123'
        )
        user_store.record_tasks(self.db_path, ['v20260101000000AAA'], stranger['id'])

        response = self.client.post(
            '/api/task_info', json={'tasks': ['v20260101000000AAA']}
        )
        task = response.get_json()['tasks'][0]

        # 与「任务不存在」返回一致，不暴露该任务属于别人
        self.assertEqual(response.status_code, 200)
        self.assertFalse(task['exists'])
        self.assertEqual(task['state'], 'missing')
        self.assertIsNotNone(owner)

    def test_task_log_rejects_other_users_tasks(self):
        self.register('first@example.com')
        stranger = user_store.create_user(
            self.db_path, 'other@example.com', password='password123'
        )
        user_store.record_tasks(self.db_path, ['v20260101000000BBB'], stranger['id'])

        response = self.client.post(
            '/api/task_log', json={'tasks': ['v20260101000000BBB']}
        )

        self.assertEqual(response.status_code, 400)

    def test_created_tasks_are_recorded_for_the_current_user(self):
        self.register('first@example.com')
        owner = user_store.get_user_by_email(self.db_path, 'first@example.com')

        with patch('app.expand_task_urls', return_value=(['https://x/v'], None)):
            response = self.client.post(
                '/api/add_task',
                json={'url': 'https://x/v', 'types': ['video']},
            )

        task_ids = response.get_json()['tasks']
        self.assertEqual(len(task_ids), 1)
        self.assertEqual(
            user_store.task_owner(self.db_path, task_ids[0]), owner['id']
        )


class TestLegacyAdoption(AuthTestCase):
    def test_first_admin_adopts_existing_tasks_files_and_token(self):
        urls_dir = Path(app_module.URLS_DIR)
        files_dir = Path(app_module.FILES_DIR)
        (urls_dir / 'v20250101000000OLD.ok').write_text('https://old/video', encoding='utf-8')
        (files_dir / 'legacy.mp4').write_text('x', encoding='utf-8')

        with patch.object(
            app_module.youtube_auth,
            'load_token',
            return_value={'refresh_token': 'legacy-refresh'},
        ):
            self.register('first@example.com')

        owner = user_store.get_user_by_email(self.db_path, 'first@example.com')
        self.assertEqual(
            user_store.task_owner(self.db_path, 'v20250101000000OLD'), owner['id']
        )
        self.assertEqual(
            user_store.media_owner(self.db_path, 'legacy.mp4'), owner['id']
        )
        self.assertIn(
            'legacy-refresh',
            user_store.load_google_token(self.db_path, owner['id'])['token_json'],
        )


if __name__ == '__main__':
    unittest.main()


class TestAnonymousHomepage(AuthTestCase):
    def test_homepage_is_visible_without_signing_in(self):
        response = self.client.get('/')
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('把链接变成文件', html)
        self.assertIn('登录 / 注册', html)
        self.assertNotIn('dl-account-menu', html)

    def test_anonymous_task_panel_explains_quota_and_retention(self):
        html = self.client.get('/').get_data(as_text=True)

        self.assertIn('匿名用户每天可下载 3 条', html)
        self.assertIn('文件保留 24 小时', html)
        self.assertIn('YouTube 视频', html)
        self.assertIn('Bilibili 视频', html)

    def test_signed_in_empty_task_panel_keeps_demo_rows(self):
        self.register('first@example.com')

        html = self.client.get('/').get_data(as_text=True)

        self.assertIn('dl-task-demo', html)
        self.assertIn('支持的站点', html)

    def test_anonymous_ip_gets_three_tasks_then_must_log_in(self):
        task_ids = []
        for index in range(3):
            response = self.client.post(
                '/api/add_task',
                json={
                    'url': f'https://example.com/v{index}',
                    'types': ['video'],
                },
            )
            self.assertEqual(response.status_code, 200)
            task_ids.extend(response.get_json()['tasks'])

        blocked = self.client.post(
            '/api/add_task',
            json={'url': 'https://example.com/v4', 'types': ['video']},
        )
        payload = blocked.get_json()

        self.assertEqual(blocked.status_code, 401)
        self.assertTrue(payload['login_required'])
        self.assertEqual(payload['anonymous_used'], 3)
        self.assertEqual(payload['login_url'], '/login')
        self.assertEqual(len(task_ids), 3)

    def test_pending_task_is_created_right_after_registering(self):
        for index in range(3):
            self.client.post(
                '/api/add_task',
                json={'url': f'https://example.com/v{index}', 'types': ['video']},
            )
        self.client.post(
            '/api/add_task',
            json={'url': 'https://example.com/pending', 'types': ['video']},
        )

        with patch(
            'app.expand_task_urls', return_value=(['https://example.com/v'], None)
        ):
            response = self.register('first@example.com')

        owner = user_store.get_user_by_email(self.db_path, 'first@example.com')
        created = user_store.user_task_ids(self.db_path, owner['id'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(created), 4)
        # 注册后直接跳到带任务的首页，用户不必重新粘贴链接
        self.assertIn('tasks=', response.headers['Location'])
        with self.client.session_transaction() as session:
            self.assertNotIn(app_module.PENDING_TASK_KEY, session)

        for task_id in created:
            self.assertIsNone(
                user_store.anonymous_task_owner(self.db_path, task_id)
            )

    def test_pending_task_is_created_right_after_logging_in(self):
        self.register('first@example.com')
        self.client.post('/logout')

        self.client.post(
            '/api/add_task',
            json={'url': 'https://example.com/later', 'types': ['audio']},
        )
        with patch(
            'app.expand_task_urls', return_value=(['https://example.com/later'], None)
        ):
            response = self.login('first@example.com')

        owner = user_store.get_user_by_email(self.db_path, 'first@example.com')
        created = user_store.user_task_ids(self.db_path, owner['id'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].startswith('a'))

    def test_form_submission_without_session_creates_anonymous_task(self):
        response = self.client.post(
            '/', data={'url': 'https://example.com/v', 'type': ['video']}
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('tasks=', response.headers['Location'])
        with self.client.session_transaction() as session:
            self.assertIn(app_module.SESSION_ANONYMOUS_KEY, session)


class TestAnonymousIsolationAndRetention(AuthTestCase):
    def create_anonymous_task(self, client=None):
        response = (client or self.client).post(
            '/api/add_task',
            json={'url': 'https://example.com/anonymous', 'types': ['video']},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()['tasks'][0]

    def test_anonymous_task_is_visible_only_to_the_same_session(self):
        task_id = self.create_anonymous_task()

        own = self.client.post('/api/task_info', json={'tasks': [task_id]})
        other = app.test_client().post(
            '/api/task_info', json={'tasks': [task_id]}
        )

        self.assertTrue(own.get_json()['tasks'][0]['exists'])
        self.assertFalse(other.get_json()['tasks'][0]['exists'])

    def test_first_admin_does_not_adopt_another_anonymous_session(self):
        anonymous_client = app.test_client()
        task_id = self.create_anonymous_task(anonymous_client)
        anonymous_id = user_store.anonymous_task_owner(self.db_path, task_id)

        self.register('first@example.com')

        self.assertIsNone(user_store.task_owner(self.db_path, task_id))
        self.assertEqual(
            user_store.anonymous_task_owner(self.db_path, task_id),
            anonymous_id,
        )

    def test_quota_is_shared_by_sessions_with_the_same_ip(self):
        first = app.test_client()
        second = app.test_client()
        for index in range(3):
            response = first.post(
                '/api/add_task',
                json={'url': f'https://example.com/{index}', 'types': ['video']},
                environ_base={'REMOTE_ADDR': '198.51.100.8'},
            )
            self.assertEqual(response.status_code, 200)

        blocked = second.post(
            '/api/add_task',
            json={'url': 'https://example.com/blocked', 'types': ['video']},
            environ_base={'REMOTE_ADDR': '198.51.100.8'},
        )
        allowed = second.post(
            '/api/add_task',
            json={'url': 'https://example.com/allowed', 'types': ['video']},
            environ_base={'REMOTE_ADDR': '198.51.100.9'},
        )

        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_anonymous_media_can_be_listed_played_and_not_cross_accessed(self):
        task_id = self.create_anonymous_task()
        urls_dir = Path(app_module.URLS_DIR)
        files_dir = Path(app_module.FILES_DIR)
        filename = 'anonymous.mp4'
        (urls_dir / f'{task_id}.result.json').write_text(
            '{"files":["anonymous.mp4"]}', encoding='utf-8'
        )
        (files_dir / filename).write_bytes(b'media')

        with (
            patch('app.get_media_dimensions', return_value=(0, 0)),
            patch('app.get_video_metadata', return_value={}),
        ):
            listing = self.client.get('/api/media_list').get_json()

        self.assertEqual([item['filename'] for item in listing['video']], [filename])
        self.assertEqual(self.client.get(f'/files/{filename}').status_code, 200)
        self.assertEqual(app.test_client().get(f'/files/{filename}').status_code, 404)

    def test_expired_anonymous_media_is_deleted(self):
        task_id = self.create_anonymous_task()
        filename = 'expired.mp4'
        filepath = Path(app_module.FILES_DIR, filename)
        filepath.write_bytes(b'old')
        with self.client.session_transaction() as session:
            anonymous_id = session[app_module.SESSION_ANONYMOUS_KEY]
        user_store.record_anonymous_media(
            self.db_path, filename, anonymous_id, task_id
        )
        with user_store.connect(self.db_path) as db:
            db.execute(
                '''UPDATE anonymous_media_owners SET created_at = ?
                   WHERE filename = ?''',
                (user_store.now_ts() - 25 * 3600, filename),
            )

        response = self.client.get('/api/media_list')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(filepath.exists())
        self.assertIsNone(
            user_store.anonymous_media_owner(self.db_path, filename)
        )
