import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import downloader


class TestDownloadRateGateInterval(unittest.TestCase):
    def test_first_acquire_does_not_wait(self):
        gate = downloader.DownloadRateGate(min_interval_seconds=10)
        with (
            patch('downloader.time.monotonic', return_value=100.0),
            patch('downloader.time.sleep') as sleeper,
        ):
            gate.acquire()

        sleeper.assert_not_called()
        self.assertEqual(gate._next_start, 110.0)

    def test_second_acquire_waits_until_next_start(self):
        gate = downloader.DownloadRateGate(min_interval_seconds=10)
        clock = iter([100.0, 101.0, 111.0])
        with (
            patch('downloader.time.monotonic', side_effect=lambda: next(clock)),
            patch('downloader.time.sleep') as sleeper,
        ):
            gate.acquire()  # now=100, wait=0, next_start=110
            gate.acquire()  # now=101, wait=9, sleep(9), next_start=121

        sleeper.assert_called_once_with(9.0)
        self.assertEqual(gate._next_start, 121.0)

    def test_zero_interval_never_waits(self):
        gate = downloader.DownloadRateGate(min_interval_seconds=0)
        with (
            patch('downloader.time.monotonic', side_effect=[0.0, 1.0, 2.0, 3.0]),
            patch('downloader.time.sleep') as sleeper,
        ):
            gate.acquire()
            gate.acquire()

        sleeper.assert_not_called()


class TestDownloadGateIntegration(unittest.TestCase):
    def setUp(self):
        self.handler = downloader.DownloadHandler(executor=None)

    def test_download_acquires_gate_on_success(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            log_dir = root_path / 'logs'
            tmp_dir = root_path / 'tmp'
            log_dir.mkdir()
            tmp_dir.mkdir()
            process = MagicMock(stdout=[], returncode=0)
            gate = MagicMock()

            with (
                patch.dict(
                    downloader.config,
                    {'LOG_DIR': str(log_dir), 'TMP_DIR': str(tmp_dir)},
                ),
                patch('downloader.subprocess.Popen', return_value=process),
                patch('downloader.probe_subtitle_fallback', return_value=None),
                patch.object(self.handler, 'move_files', return_value=True),
                patch('downloader.download_gate', gate),
            ):
                result = self.handler.download(
                    'https://example.com/video',
                    'video-gate-success',
                    'video',
                )

            self.assertTrue(result)
            gate.acquire.assert_called_once()

    def test_download_acquires_gate_on_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            log_dir = root_path / 'logs'
            tmp_dir = root_path / 'tmp'
            log_dir.mkdir()
            tmp_dir.mkdir()
            process = MagicMock(stdout=[], returncode=1)
            gate = MagicMock()

            with (
                patch.dict(
                    downloader.config,
                    {'LOG_DIR': str(log_dir), 'TMP_DIR': str(tmp_dir)},
                ),
                patch('downloader.subprocess.Popen', return_value=process),
                patch('downloader.probe_subtitle_fallback', return_value=None),
                patch.object(self.handler, 'move_files', return_value=True),
                patch('downloader.download_gate', gate),
            ):
                result = self.handler.download(
                    'https://example.com/video',
                    'video-gate-failure',
                    'video',
                )

            self.assertFalse(result)
            gate.acquire.assert_called_once()


if __name__ == '__main__':
    unittest.main()
