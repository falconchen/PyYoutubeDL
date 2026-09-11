import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
GUARD = PROJECT_DIR / 'deploy' / 'supervisor' / 'service-guard.sh'
SERVICE_SCRIPTS = {
    'ai': 'ai_summary_worker.py',
    'webdav': 'webdav_uploader.py',
    'playlist': 'playlist_monitor.py',
}
# 与 supervisor program 的 exitcodes=0,10,11 对应
EXPECTED_EXIT_CODES = (0, 10, 11)


class TestServiceGuard(unittest.TestCase):
    """守卫脚本用假 Python 隔离测试，只验证其自身的判定与分发逻辑。"""

    def run_guard(self, *args, fake_status=0, settle_secs=0):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_python = Path(temp_dir) / 'python'
            # 判定调用形如 `python - <service>`（脚本走 stdin）；启用后的
            # exec 调用形如 `python <worker>.py`，此时打印路径代替真正启动。
            fake_python.write_text(
                '#!/bin/sh\n'
                'if [ "$1" = "-" ]; then\n'
                '    cat >/dev/null\n'
                '    exit "$FAKE_STATUS"\n'
                'fi\n'
                'echo "EXEC $1"\n',
                encoding='utf-8',
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env.update({
                'PYTHON_BIN': str(fake_python),
                'FAKE_STATUS': str(fake_status),
                'GUARD_SETTLE_SECS': str(settle_secs),
            })
            return subprocess.run(
                [str(GUARD), *args],
                cwd=PROJECT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_enabled_service_execs_its_worker(self):
        for service, script in SERVICE_SCRIPTS.items():
            with self.subTest(service=service):
                result = self.run_guard(service, fake_status=0)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'EXEC {PROJECT_DIR / script}', result.stdout)

    def test_unconfigured_service_exits_ten_with_reason(self):
        result = self.run_guard('ai', fake_status=10)

        self.assertEqual(result.returncode, 10)
        self.assertIn('AI总结尚未配置', result.stdout)
        self.assertNotIn('EXEC', result.stdout)

    def test_switched_off_service_exits_eleven_with_reason(self):
        result = self.run_guard('playlist', fake_status=11)

        self.assertEqual(result.returncode, 11)
        self.assertIn('播放列表监控已关闭', result.stdout)
        self.assertNotIn('EXEC', result.stdout)

    def test_config_read_failure_is_unexpected_exit(self):
        # 非 0/10/11 的判定失败必须映射为 1，落在 exitcodes 之外好让 supervisor 重启
        result = self.run_guard('webdav', fake_status=3)

        self.assertEqual(result.returncode, 1)
        self.assertIn('无法读取 webdav 配置', result.stderr)

    def test_check_only_does_not_exec_worker(self):
        result = self.run_guard('--check-only', 'ai', fake_status=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('EXEC', result.stdout)
        self.assertIn('服务 ai 已启用', result.stdout)

    def test_rejects_unknown_service(self):
        result = self.run_guard('bogus')

        self.assertEqual(result.returncode, 2)
        self.assertIn('无效服务: bogus', result.stderr)

    def test_disabled_exit_codes_match_supervisor_config(self):
        """守卫的禁用退出码必须全部列在 program 的 exitcodes 中。"""
        template = (PROJECT_DIR / 'deploy' / 'supervisor'
                    / 'pyyoutubedl.conf').read_text(encoding='utf-8')
        declared = [
            line.split('=', 1)[1].strip()
            for line in template.splitlines()
            if line.startswith('exitcodes=')
        ]

        self.assertTrue(declared, '模板中未找到 exitcodes 声明')
        for value in declared:
            codes = {int(code) for code in value.split(',')}
            self.assertEqual(codes, set(EXPECTED_EXIT_CODES))


if __name__ == '__main__':
    unittest.main()
