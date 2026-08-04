import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webdav_uploader
import start
from config_util import DEFAULT_CONFIG, is_webdav_upload_enabled


class TestWebDAVUploadToggle(unittest.TestCase):
    def test_upload_is_enabled_by_default(self):
        self.assertIs(DEFAULT_CONFIG['ENABLE_WEBDAV_UPLOAD'], True)
        self.assertTrue(is_webdav_upload_enabled({}))

    def test_start_services_skips_uploader_when_disabled(self):
        scripts = start.get_service_scripts({'ENABLE_WEBDAV_UPLOAD': False})

        self.assertEqual(scripts, [('downloader.py', '下载器')])

    def test_start_services_includes_uploader_when_enabled(self):
        scripts = start.get_service_scripts({'ENABLE_WEBDAV_UPLOAD': True})

        self.assertIn(('webdav_uploader.py', '上传器'), scripts)

    def test_runner_reports_disabled_upload_and_skips_uploader(self):
        result = subprocess.run(
            [
                'bash',
                '-c',
                '''
source ./runner.sh
command() { return 0; }
start_service() { echo "START:$1"; }
webdav_upload_status() { return 10; }
start_services
''',
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn(
            'WebDAV上传已关闭，已跳过启动上传器。',
            result.stdout,
        )
        self.assertNotIn('START:上传器', result.stdout)

    def test_disabled_main_does_not_initialize_webdav_clients(self):
        with (
            patch.dict(
                webdav_uploader.config,
                {'ENABLE_WEBDAV_UPLOAD': False},
            ),
            patch.object(webdav_uploader, 'initialize_webdav_clients') as init,
            patch('webdav_uploader.time.sleep', side_effect=KeyboardInterrupt),
        ):
            webdav_uploader.main()

        init.assert_not_called()

    def test_disabled_upload_keeps_local_file_and_skips_webdav(self):
        handler = webdav_uploader.WebDAVUploadHandler()

        with tempfile.TemporaryDirectory() as root:
            media_file = Path(root) / 'video.mp4'
            media_file.write_bytes(b'video')

            with (
                patch.dict(
                    webdav_uploader.config,
                    {'ENABLE_WEBDAV_UPLOAD': False},
                ),
                patch.object(webdav_uploader, 'video_webdav') as client,
            ):
                handler.process_file(str(media_file))

            self.assertTrue(media_file.exists())
            client.check.assert_not_called()
            client.upload_sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
