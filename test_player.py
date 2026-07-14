import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app


class TestPlayerPage(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.testing = True

    def test_download_control_is_rendered_for_current_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'test video.mp4'
            Path(files_dir, filename).touch()

            with patch('app.FILES_DIR', files_dir):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("videojs.registerComponent('DownloadButton'", html)
        self.assertIn("controlBar.addChild('DownloadButton'", html)
        self.assertIn("this.addClass('vjs-download-control')", html)
        self.assertIn('var currentFilename = "test video.mp4";', html)
        self.assertIn('link.download = currentFilename;', html)
        self.assertLess(html.index('video-js.min.css'), html.index('player.css'))

    def test_download_icon_uses_centered_control_box(self):
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('cursor: pointer;', css)
        self.assertIn('align-items: center;', css)
        self.assertIn('justify-content: center;', css)
        self.assertIn('font-size: 1.3em;', css)
        self.assertIn('line-height: 1;', css)


if __name__ == '__main__':
    unittest.main()
