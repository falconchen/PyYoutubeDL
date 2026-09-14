"""任务操作接口：暂停、继续、重启、删除。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
import task_queue
import user_store
from auth_helper import install_auth, other_user


class TaskActionTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.urls_dir = root / 'urls'
        self.files_dir = root / 'files'
        self.tmp_dir = root / 'tmp'
        self.log_dir = root / 'logs'
        for folder in (self.urls_dir, self.files_dir, self.tmp_dir, self.log_dir):
            folder.mkdir()

        for patcher in (
            patch.object(app_module, 'URLS_DIR', str(self.urls_dir)),
            patch.object(app_module, 'FILES_DIR', str(self.files_dir)),
            patch.dict(app_module.config, {
                'TMP_DIR': str(self.tmp_dir),
                'LOG_DIR': str(self.log_dir),
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        app_module.app.testing = True
        self.client = app_module.app.test_client()
        install_auth(self)

    def make_task(self, task_id, extension, owner_id=None):
        (self.urls_dir / f'{task_id}{extension}').write_text(
            'https://example.com/video', encoding='utf-8'
        )
        user_store.record_tasks(
            self.user_db_path, [task_id], owner_id or self.logged_in_user['id']
        )

    def act(self, task_id, action, **extra):
        return self.client.post(
            '/api/task_action', json={'task': task_id, 'action': action, **extra}
        )


class TestPauseResumeRestart(TaskActionTestCase):
    def test_pause_queued_task_renames_to_paused(self):
        self.make_task('v20260914000000Pau', '.txt')

        response = self.act('v20260914000000Pau', 'pause')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['state'], 'paused')
        self.assertTrue((self.urls_dir / 'v20260914000000Pau.paused').exists())
        self.assertFalse((self.urls_dir / 'v20260914000000Pau.txt').exists())

    def test_resume_requeues_same_task_and_keeps_partial_download(self):
        self.make_task('v20260914000000Res', '.paused')
        partial = self.tmp_dir / 'v20260914000000Res' / 'video.mp4.part'
        partial.parent.mkdir()
        partial.write_bytes(b'partial')

        response = self.act('v20260914000000Res', 'resume')

        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.urls_dir / 'v20260914000000Res.txt').exists())
        # 续传依赖临时目录里的 .part 文件
        self.assertTrue(partial.exists())

    def test_restart_failed_task_discards_temp_and_requeues(self):
        self.make_task('v20260914000000Rst', '.fail')
        (self.tmp_dir / 'v20260914000000Rst').mkdir()
        (self.urls_dir / 'v20260914000000Rst.result.json').write_text('{}')

        response = self.act('v20260914000000Rst', 'restart')

        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.urls_dir / 'v20260914000000Rst.txt').exists())
        self.assertFalse((self.tmp_dir / 'v20260914000000Rst').exists())
        self.assertFalse((self.urls_dir / 'v20260914000000Rst.result.json').exists())

    def test_restart_is_not_offered_for_completed_tasks(self):
        self.make_task('v20260914000000Don', '.ok')

        response = self.act('v20260914000000Don', 'restart')

        self.assertEqual(response.status_code, 409)
        self.assertTrue((self.urls_dir / 'v20260914000000Don.ok').exists())

    def test_downloading_task_is_handed_to_downloader(self):
        self.make_task('v20260914000000Run', '.downloading')

        response = self.act('v20260914000000Run', 'pause')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()['pending'], 'pause')
        control = self.urls_dir / 'v20260914000000Run.control'
        self.assertEqual(control.read_text(encoding='utf-8'), 'pause')
        # 状态文件由下载器在终止 yt-dlp 后改写，Web 端不动
        self.assertTrue((self.urls_dir / 'v20260914000000Run.downloading').exists())

    def test_paused_task_reports_paused_state_with_last_progress(self):
        self.make_task('v20260914000000Inf', '.paused')

        info = self.client.post(
            '/api/task_info', json={'tasks': ['v20260914000000Inf']}
        ).get_json()['tasks'][0]

        self.assertEqual(info['state'], 'paused')
        self.assertEqual(info['progress']['stage'], 'paused')


class TestDelete(TaskActionTestCase):
    def complete_task(self, task_id, filename, owner=None):
        self.make_task(task_id, '.ok', owner_id=owner)
        (self.files_dir / filename).write_bytes(b'media')
        (self.urls_dir / f'{task_id}.result.json').write_text(
            json.dumps({'files': [filename]}), encoding='utf-8'
        )
        user_store.record_media(
            self.user_db_path, filename, owner or self.logged_in_user['id'], task_id
        )

    def test_delete_completed_task_keeps_files_by_default(self):
        self.complete_task('v20260914000000Kep', 'keep.mp4')

        response = self.act('v20260914000000Kep', 'delete')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['removed_files'], 0)
        self.assertFalse((self.urls_dir / 'v20260914000000Kep.ok').exists())
        self.assertTrue((self.files_dir / 'keep.mp4').exists())
        self.assertEqual(
            user_store.media_owner(self.user_db_path, 'keep.mp4'),
            self.logged_in_user['id'],
        )
        self.assertIsNone(
            user_store.task_owner(self.user_db_path, 'v20260914000000Kep')
        )

    def test_delete_completed_task_with_files_removes_owned_files(self):
        self.complete_task('v20260914000000Del', 'gone.mp4')

        response = self.act('v20260914000000Del', 'delete', delete_files=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['removed_files'], 1)
        self.assertFalse((self.files_dir / 'gone.mp4').exists())
        self.assertIsNone(user_store.media_owner(self.user_db_path, 'gone.mp4'))

    def test_delete_files_never_touches_another_owners_file(self):
        stranger = other_user(self.user_db_path)
        self.complete_task('v20260914000000Oth', 'shared.mp4')
        # 结果清单登记了同名文件，但它归属另一个用户
        user_store.delete_media(self.user_db_path, 'shared.mp4')
        user_store.record_media(self.user_db_path, 'shared.mp4', stranger['id'])

        response = self.act('v20260914000000Oth', 'delete', delete_files=True)

        self.assertEqual(response.get_json()['removed_files'], 0)
        self.assertTrue((self.files_dir / 'shared.mp4').exists())

    def test_delete_paused_task_removes_temp_and_log(self):
        self.make_task('v20260914000000Tmp', '.paused')
        (self.tmp_dir / 'v20260914000000Tmp').mkdir()
        (self.log_dir / 'v20260914000000Tmp.log').write_text('log')

        response = self.act('v20260914000000Tmp', 'delete')

        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.urls_dir / 'v20260914000000Tmp.paused').exists())
        self.assertFalse((self.tmp_dir / 'v20260914000000Tmp').exists())
        self.assertFalse((self.log_dir / 'v20260914000000Tmp.log').exists())

    def test_delete_downloading_task_hides_it_immediately(self):
        self.make_task('v20260914000000Can', '.downloading')

        response = self.act('v20260914000000Can', 'delete')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            (self.urls_dir / 'v20260914000000Can.control').read_text(encoding='utf-8'),
            task_queue.ACTION_DELETE,
        )
        # 归属立即解除，任务从列表消失；文件由下载器异步清理
        self.assertIsNone(
            user_store.task_owner(self.user_db_path, 'v20260914000000Can')
        )


class TestAccessControl(TaskActionTestCase):
    def test_other_users_task_is_not_found(self):
        stranger = other_user(self.user_db_path)
        self.make_task('v20260914000000Sec', '.txt', owner_id=stranger['id'])

        response = self.act('v20260914000000Sec', 'delete')

        self.assertEqual(response.status_code, 404)
        self.assertTrue((self.urls_dir / 'v20260914000000Sec.txt').exists())

    def test_rejects_unknown_action_and_invalid_task_id(self):
        self.make_task('v20260914000000Bad', '.txt')

        self.assertEqual(self.act('v20260914000000Bad', 'explode').status_code, 400)
        self.assertEqual(self.act('../../config', 'delete').status_code, 400)


if __name__ == '__main__':
    unittest.main()
