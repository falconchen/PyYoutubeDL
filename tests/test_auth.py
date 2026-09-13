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

        # 停用后旧 cookie 立即失效；首页对匿名开放，故改看受保护接口
        self.assertEqual(self.client.get('/api/media_list').status_code, 401)
        self.assertNotIn('退出登录', self.client.get('/').get_data(as_text=True))

    def test_logout_clears_the_session(self):
        self.register('first@example.com')

        self.client.post('/logout')

        self.assertEqual(self.client.get('/api/media_list').status_code, 401)

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

    def test_anonymous_task_panel_lists_supported_sources(self):
        html = self.client.get('/').get_data(as_text=True)

        # 未登录时用示例条目说明支持哪些站点，而不是空状态
        for label in [
            'YouTube 视频', 'YouTube 音频', 'Bilibili 视频', '小红书视频', 'Vimeo 视频',
        ]:
            self.assertIn(label, html)
        self.assertIn('支持的站点', html)
        self.assertNotIn('还没有任务', html)

    def test_signed_in_task_panel_has_no_demo_rows(self):
        self.register('first@example.com')

        html = self.client.get('/').get_data(as_text=True)

        self.assertNotIn('dl-task-demo', html)
        self.assertIn('还没有任务', html)

    def test_adding_a_task_prompts_login_and_remembers_the_submission(self):
        response = self.client.post(
            '/api/add_task',
            json={'url': 'https://example.com/v', 'types': ['video']},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 401)
        self.assertTrue(payload['login_required'])
        self.assertEqual(payload['login_url'], '/login')
        with self.client.session_transaction() as session:
            self.assertEqual(
                session[app_module.PENDING_TASK_KEY],
                {'url': 'https://example.com/v', 'types': ['video']},
            )

    def test_pending_task_is_created_right_after_registering(self):
        self.client.post(
            '/api/add_task',
            json={'url': 'https://example.com/v', 'types': ['video']},
        )

        with patch(
            'app.expand_task_urls', return_value=(['https://example.com/v'], None)
        ):
            response = self.register('first@example.com')

        owner = user_store.get_user_by_email(self.db_path, 'first@example.com')
        created = user_store.user_task_ids(self.db_path, owner['id'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(created), 1)
        # 注册后直接跳到带任务的首页，用户不必重新粘贴链接
        self.assertIn(created[0], response.headers['Location'])
        with self.client.session_transaction() as session:
            self.assertNotIn(app_module.PENDING_TASK_KEY, session)

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

    def test_form_submission_without_session_redirects_to_login(self):
        response = self.client.post(
            '/', data={'url': 'https://example.com/v', 'type': ['video']}
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])
        with self.client.session_transaction() as session:
            self.assertIn(app_module.PENDING_TASK_KEY, session)
