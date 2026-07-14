import os
import unittest
from unittest.mock import Mock, patch

import psutil

import stop


class TestCommandTargetsProject(unittest.TestCase):
    def setUp(self):
        self.base_dir = os.path.realpath('/srv/PyYoutubeDL')

    def test_matches_absolute_project_script(self):
        cmdline = ['python', '/srv/PyYoutubeDL/downloader.py']
        self.assertTrue(stop.command_targets_project(cmdline, '/', self.base_dir))

    def test_matches_relative_project_script(self):
        cmdline = ['python', './webdav_uploader.py']
        self.assertTrue(
            stop.command_targets_project(cmdline, self.base_dir, self.base_dir)
        )

    def test_rejects_same_script_name_in_another_project(self):
        cmdline = ['python', '/srv/another-project/downloader.py']
        self.assertFalse(stop.command_targets_project(cmdline, '/', self.base_dir))

    def test_rejects_filename_mentioned_inside_an_argument(self):
        cmdline = ['python', '-c', 'print("downloader.py")']
        self.assertFalse(
            stop.command_targets_project(cmdline, self.base_dir, self.base_dir)
        )

    def test_rejects_project_path_passed_to_python_c(self):
        cmdline = [
            'python',
            '-c',
            'print("test")',
            '/srv/PyYoutubeDL/downloader.py',
        ]
        self.assertFalse(
            stop.command_targets_project(cmdline, self.base_dir, self.base_dir)
        )


class TestTerminateProcesses(unittest.TestCase):
    @patch('stop.psutil.process_iter')
    def test_reports_process_list_read_failure(self, process_iter):
        process_iter.side_effect = PermissionError('not permitted')

        processes, errors = stop.find_target_processes()

        self.assertEqual(processes, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('无法读取系统进程列表', errors[0])

    @patch('stop.psutil.process_iter')
    def test_finds_target_process_and_its_child_only(self, process_iter):
        child = Mock(pid=124)
        root = Mock(
            pid=123,
            info={
                'pid': 123,
                'cmdline': ['python', os.path.join(stop.BASE_DIR, 'downloader.py')],
                'cwd': stop.BASE_DIR,
            },
        )
        root.children.return_value = [child]
        unrelated = Mock(
            pid=999,
            info={
                'pid': 999,
                'cmdline': ['python', '/srv/other/downloader.py'],
                'cwd': '/srv/other',
            },
        )
        process_iter.return_value = [root, unrelated]

        processes, errors = stop.find_target_processes()

        self.assertEqual([proc.pid for proc in processes], [124, 123])
        self.assertEqual(errors, [])

    @patch('stop.psutil.wait_procs')
    def test_force_kills_process_that_ignores_terminate(self, wait_procs):
        proc = Mock(pid=123)
        proc.cmdline.return_value = ['python', '/srv/PyYoutubeDL/downloader.py']
        wait_procs.side_effect = [([], [proc]), ([proc], [])]

        errors = stop.terminate_processes([proc], timeout=0)

        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        self.assertEqual(errors, [])

    def test_reports_access_denied(self):
        proc = Mock(pid=456)
        proc.cmdline.return_value = ['python', '/srv/PyYoutubeDL/app.py']
        proc.terminate.side_effect = psutil.AccessDenied(pid=proc.pid)

        errors = stop.terminate_processes([proc], timeout=0)

        self.assertEqual(len(errors), 1)
        self.assertIn('无法终止 PID 456', errors[0])


if __name__ == '__main__':
    unittest.main()
