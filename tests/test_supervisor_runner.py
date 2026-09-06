import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_DIR / 'supervisor-runner.sh'
SERVICES = ('app', 'downloader', 'ai', 'webdav', 'playlist')
PROGRAMS = (
    'pyyoutubedl-app',
    'pyyoutubedl-downloader',
    'pyyoutubedl-ai-summary',
    'pyyoutubedl-webdav',
    'pyyoutubedl-playlist-monitor',
)


class TestSupervisorRunner(unittest.TestCase):
    def run_runner(self, *args):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            log_path = temp_path / 'supervisorctl.log'
            supervisorctl = temp_path / 'supervisorctl'
            supervisorctl.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SUPERVISOR_LOG"\n',
                encoding='utf-8',
            )
            supervisorctl.chmod(
                supervisorctl.stat().st_mode | stat.S_IXUSR,
            )
            env = os.environ.copy()
            env.update({
                'SUPERVISORCTL_BIN': str(supervisorctl),
                'SUPERVISOR_LOG': str(log_path),
            })
            result = subprocess.run(
                [str(RUNNER), *args],
                cwd=PROJECT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = log_path.read_text(encoding='utf-8').splitlines() \
                if log_path.exists() else []
            return result, calls

    def test_no_arguments_restart_all_programs(self):
        result, calls = self.run_runner()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [f'restart {" ".join(PROGRAMS)}'])

    def test_actions_can_select_one_service(self):
        cases = (
            ('start', 'downloader', 'start pyyoutubedl-downloader'),
            ('stop', 'webdav', 'stop pyyoutubedl-webdav'),
            ('restart', 'app', 'restart pyyoutubedl-app'),
        )
        for action, service, expected_call in cases:
            with self.subTest(action=action, service=service):
                result, calls = self.run_runner(action, service)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [expected_call])

    def test_status_lists_all_programs(self):
        result, calls = self.run_runner('status')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [f'status {" ".join(PROGRAMS)}'])

    def test_status_can_select_one_service(self):
        result, calls = self.run_runner('status', 'app')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ['status pyyoutubedl-app'])

    def test_list_prints_all_services_without_calling_supervisor(self):
        for option in ('-l', '--list'):
            with self.subTest(option=option):
                result, calls = self.run_runner(option)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [])
                for service in SERVICES:
                    self.assertRegex(result.stdout, rf'(?m)^  {service}\s')

    def test_rejects_unknown_service_without_calling_supervisor(self):
        result, calls = self.run_runner('restart', 'unknown')

        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])
        self.assertIn('无效服务: unknown', result.stdout)


if __name__ == '__main__':
    unittest.main()
