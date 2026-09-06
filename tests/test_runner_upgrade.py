import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


class TestRunnerUpgrade(unittest.TestCase):
    def test_upgrade_command_updates_pip_and_requirements_for_both_runners(self):
        for script_name in ('runner.sh', 'supervisor-runner.sh'):
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                calls_path = root_path / 'python-calls.log'
                python_bin = root_path / 'python'
                python_bin.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "$*" >> "$PYTHON_CALLS"\n',
                    encoding='utf-8',
                )
                python_bin.chmod(0o755)
                env = os.environ.copy()
                env['PYTHON_CALLS'] = str(calls_path)

                result = subprocess.run(
                    [
                        'bash',
                        '-c',
                        'source "$1"; PYTHON_BIN="$2"; upgrade_dependencies',
                        'upgrade-test',
                        str(PROJECT_DIR / script_name),
                        str(python_bin),
                    ],
                    cwd=PROJECT_DIR,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    calls_path.read_text(encoding='utf-8').splitlines(),
                    [
                        '-m pip install --upgrade pip',
                        f'-m pip install --upgrade -r {PROJECT_DIR / "requirements.txt"}',
                    ],
                )

    def test_runner_upgrade_options_restart_all_services(self):
        for option in ('-u', '--upgrade'):
            with self.subTest(option=option):
                result = subprocess.run(
                    [
                        'bash',
                        '-c',
                        '''
source ./runner.sh
upgrade_dependencies() { echo UPGRADE; }
stop_services() { echo "STOP:$1"; }
start_services() { echo "START:$1"; }
main "$1"
''',
                        'upgrade-test',
                        option,
                    ],
                    cwd=PROJECT_DIR,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('UPGRADE', result.stdout)
                self.assertIn('STOP:all', result.stdout)
                self.assertIn('START:all', result.stdout)

    def test_supervisor_upgrade_options_restart_all_services(self):
        for option in ('-u', '--upgrade'):
            with self.subTest(option=option):
                result = subprocess.run(
                    [
                        'bash',
                        '-c',
                        '''
source ./supervisor-runner.sh
SUPERVISORCTL_BIN=/usr/bin/true
upgrade_dependencies() { echo UPGRADE; }
run_supervisor_action() { echo "ACTION:$1:$2"; }
main "$1"
''',
                        'upgrade-test',
                        option,
                    ],
                    cwd=PROJECT_DIR,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('UPGRADE', result.stdout)
                self.assertIn('ACTION:restart:all', result.stdout)

    def test_upgrade_failure_does_not_restart_services(self):
        cases = (
            (
                'runner.sh',
                'stop_services() { echo STOP; }\n'
                'start_services() { echo START; }',
            ),
            (
                'supervisor-runner.sh',
                'SUPERVISORCTL_BIN=/usr/bin/true\n'
                'run_supervisor_action() { echo ACTION; }',
            ),
        )
        for script_name, action_stub in cases:
            with self.subTest(script=script_name):
                shell_code = f'''
source ./{script_name}
upgrade_dependencies() {{ echo UPGRADE_FAILED; return 1; }}
{action_stub}
main --upgrade
'''
                result = subprocess.run(
                    ['bash', '-c', shell_code],
                    cwd=PROJECT_DIR,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn('UPGRADE_FAILED', result.stdout)
                self.assertNotIn('STOP', result.stdout)
                self.assertNotIn('START', result.stdout)
                self.assertNotIn('ACTION', result.stdout)


if __name__ == '__main__':
    unittest.main()
