"""下载器对暂停、重启、删除指令的处理。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import downloader
import task_queue


class ControlTestCase(unittest.TestCase):
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
        patcher = patch.dict(downloader.config, {
            'URLS_DIR': str(self.urls_dir),
            'FILES_DIR': str(self.files_dir),
            'TMP_DIR': str(self.tmp_dir),
            'LOG_DIR': str(self.log_dir),
            'BARK_DEVICE_TOKEN': 'test-token',
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.handler = downloader.DownloadHandler(executor=MagicMock())

    def write(self, name, content='https://example.com/video'):
        path = self.urls_dir / name
        path.write_text(content, encoding='utf-8')
        return path

    def send(self, task_id, action):
        return self.handler.handle_control_file(
            task_queue.write_task_control(str(self.urls_dir), task_id, action)
        )


class TestStaleDownloadingTask(ControlTestCase):
    """下载器曾异常退出留下的 .downloading 不在本进程手上，指令直接生效。"""

    def test_pause_keeps_partial_download(self):
        self.write('v1Stale.downloading')
        (self.tmp_dir / 'v1Stale').mkdir()

        self.send('v1Stale', 'pause')

        self.assertTrue((self.urls_dir / 'v1Stale.paused').exists())
        self.assertTrue((self.tmp_dir / 'v1Stale').exists())
        self.assertFalse((self.urls_dir / 'v1Stale.control').exists())

    def test_restart_discards_temp_and_requeues(self):
        self.write('v1Again.downloading')
        (self.tmp_dir / 'v1Again').mkdir()

        self.send('v1Again', 'restart')

        self.assertTrue((self.urls_dir / 'v1Again.txt').exists())
        self.assertFalse((self.tmp_dir / 'v1Again').exists())

    def test_delete_removes_every_trace(self):
        self.write('v1Gone.downloading')
        (self.tmp_dir / 'v1Gone').mkdir()
        (self.log_dir / 'v1Gone.log').write_text('log')

        self.send('v1Gone', 'delete')

        self.assertEqual(list(self.urls_dir.iterdir()), [])
        self.assertFalse((self.tmp_dir / 'v1Gone').exists())
        self.assertFalse((self.log_dir / 'v1Gone.log').exists())

    def test_pending_controls_are_applied_on_startup(self):
        self.write('v1Boot.downloading')
        task_queue.write_task_control(str(self.urls_dir), 'v1Boot', 'pause')

        applied = self.handler.apply_pending_controls(str(self.urls_dir))

        self.assertEqual(applied, 1)
        self.assertTrue((self.urls_dir / 'v1Boot.paused').exists())

    def test_invalid_control_content_is_ignored(self):
        self.write('v1Junk.downloading')
        (self.urls_dir / 'v1Junk.control').write_text('rm -rf /')

        self.handler.handle_control_file(str(self.urls_dir / 'v1Junk.control'))

        self.assertTrue((self.urls_dir / 'v1Junk.downloading').exists())


class TestActiveTask(ControlTestCase):
    def test_control_for_running_task_terminates_ytdlp(self):
        process = MagicMock()
        self.handler._active_tasks.add('v1Live')
        self.handler._processes['v1Live'] = process

        with patch.object(self.handler, '_terminate_process_tree') as terminate:
            self.send('v1Live', 'pause')

        terminate.assert_called_once_with(process)
        self.assertEqual(self.handler._stop_requests['v1Live'], 'pause')

    def test_paused_download_is_neither_failed_nor_reported(self):
        task_path = self.write('v1Stop.txt')
        partial = self.tmp_dir / 'v1Stop'

        def fake_download(url, base_name, mode, started_at=None):
            partial.mkdir()
            self.handler._stop_requests[base_name] = 'pause'
            return False

        with (
            patch('downloader.time.sleep'),
            patch.object(self.handler, 'download', side_effect=fake_download),
            patch('downloader.bark_notify') as notify,
        ):
            self.handler.process_file(str(task_path))

        self.assertTrue((self.urls_dir / 'v1Stop.paused').exists())
        self.assertFalse((self.urls_dir / 'v1Stop.fail').exists())
        self.assertTrue(partial.exists())
        notify.assert_not_called()
        self.assertNotIn('v1Stop', self.handler._active_tasks)

    def test_pause_arriving_after_success_completes_normally(self):
        task_path = self.write('v1Late.txt')

        def fake_download(url, base_name, mode, started_at=None):
            self.handler._stop_requests[base_name] = 'pause'
            return True

        with (
            patch('downloader.time.sleep'),
            patch.object(self.handler, 'download', side_effect=fake_download),
        ):
            self.handler.process_file(str(task_path))

        self.assertTrue((self.urls_dir / 'v1Late.ok').exists())

    def test_delete_of_running_task_removes_files_moved_during_run(self):
        task_path = self.write('v1Cancel.txt')
        moved = self.files_dir / 'item-1.mp4'

        def fake_download(url, base_name, mode, started_at=None):
            moved.write_bytes(b'item')
            self.handler._moved_files[base_name] = [str(moved)]
            self.handler._stop_requests[base_name] = 'delete'
            return False

        with (
            patch('downloader.time.sleep'),
            patch.object(self.handler, 'download', side_effect=fake_download),
        ):
            self.handler.process_file(str(task_path))

        self.assertFalse(moved.exists())
        self.assertEqual(list(self.urls_dir.iterdir()), [])

    def test_terminated_ytdlp_keeps_temp_and_skips_failure_alert(self):
        process = MagicMock(stdout=['[download]  12.0% of 10MiB\n'], returncode=-15)
        self.handler._stop_requests['v1Kill'] = 'pause'

        with (
            patch('downloader.subprocess.Popen', return_value=process),
            patch('downloader.probe_subtitle_fallback', return_value=None),
            patch('downloader.download_gate', MagicMock()),
            patch('downloader.bark_notify') as notify,
            patch.object(self.handler, '_terminate_process_tree'),
        ):
            result = self.handler.download('https://example.com/v', 'v1Kill', 'video')

        self.assertFalse(result)
        # 断点续传需要保留临时目录
        self.assertTrue((self.tmp_dir / 'v1Kill').exists())
        notify.assert_not_called()

    def test_terminating_real_child_keeps_signal_exit_code(self):
        # 回归：psutil 若抢先回收直接子进程，Popen.wait() 会得到返回码 0，
        # 被暂停的下载会被误判为成功，.part 被当成成品移走
        process = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)']
        )
        self.addCleanup(process.kill)

        self.handler._terminate_process_tree(process)

        self.assertNotEqual(process.wait(timeout=5), 0)

    def test_stop_requested_during_wait_skips_starting_ytdlp(self):
        self.handler._stop_requests['v1Wait'] = 'delete'

        with (
            patch('downloader.subprocess.Popen') as popen,
            patch('downloader.probe_subtitle_fallback', return_value=None),
            patch('downloader.download_gate', MagicMock()),
        ):
            result = self.handler.download('https://example.com/v', 'v1Wait', 'video')

        self.assertFalse(result)
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
