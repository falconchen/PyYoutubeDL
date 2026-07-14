import errno
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import downloader


class TestDownloaderMove(unittest.TestCase):
    def setUp(self):
        self.handler = downloader.DownloadHandler(executor=None)

    def test_existing_file_is_renamed_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tmp_dir = root_path / 'tmp' / 'task'
            files_dir = root_path / 'files'
            tmp_dir.mkdir(parents=True)
            files_dir.mkdir()
            (tmp_dir / 'video.mp4').write_text('new', encoding='utf-8')
            (files_dir / 'video.mp4').write_text('old', encoding='utf-8')

            with patch.dict(downloader.config, {'FILES_DIR': str(files_dir)}):
                result = self.handler.move_files(str(tmp_dir))

            self.assertTrue(result)
            self.assertEqual(
                (files_dir / 'video.mp4').read_text(encoding='utf-8'),
                'old',
            )
            self.assertEqual(
                (files_dir / 'video (1).mp4').read_text(encoding='utf-8'),
                'new',
            )
            self.assertFalse(tmp_dir.exists())

    def test_counter_advances_and_preserves_compound_filename(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tmp_dir = root_path / 'tmp' / 'task'
            files_dir = root_path / 'files'
            tmp_dir.mkdir(parents=True)
            files_dir.mkdir()
            filename = 'video.zh-Hans.srt'
            (tmp_dir / filename).write_text('new subtitle', encoding='utf-8')
            (files_dir / filename).write_text('old subtitle', encoding='utf-8')
            (files_dir / 'video.zh-Hans (1).srt').write_text(
                'older subtitle',
                encoding='utf-8',
            )

            with patch.dict(downloader.config, {'FILES_DIR': str(files_dir)}):
                result = self.handler.move_files(str(tmp_dir))

            self.assertTrue(result)
            self.assertEqual(
                (files_dir / 'video.zh-Hans (2).srt').read_text(encoding='utf-8'),
                'new subtitle',
            )

    def test_move_failure_preserves_source_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tmp_dir = root_path / 'tmp' / 'task'
            files_dir = root_path / 'files'
            tmp_dir.mkdir(parents=True)
            files_dir.mkdir()
            source = tmp_dir / 'video.mp4'
            source.write_text('new', encoding='utf-8')

            with (
                patch.dict(downloader.config, {'FILES_DIR': str(files_dir)}),
                patch(
                    'downloader.os.link',
                    side_effect=OSError(errno.EACCES, 'permission denied'),
                ),
            ):
                result = self.handler.move_files(str(tmp_dir))

            self.assertFalse(result)
            self.assertTrue(source.exists())
            self.assertTrue(tmp_dir.exists())
            self.assertFalse((files_dir / 'video.mp4').exists())

    def test_cross_filesystem_fallback_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / 'source.mp4'
            destination = root_path / 'video.mp4'
            source.write_text('new', encoding='utf-8')
            destination.write_text('old', encoding='utf-8')
            real_link = downloader.os.link
            link_calls = 0

            def simulate_cross_filesystem_link(src, dst):
                nonlocal link_calls
                link_calls += 1
                if link_calls == 1:
                    raise OSError(errno.EXDEV, 'cross-device link')
                return real_link(src, dst)

            with patch(
                'downloader.os.link',
                side_effect=simulate_cross_filesystem_link,
            ):
                final_destination = downloader.move_without_overwrite(
                    str(source),
                    str(destination),
                )

            self.assertEqual(destination.read_text(encoding='utf-8'), 'old')
            self.assertEqual(
                Path(final_destination).read_text(encoding='utf-8'),
                'new',
            )
            self.assertEqual(Path(final_destination).name, 'video (1).mp4')
            self.assertFalse(source.exists())
            self.assertEqual(
                list(root_path.glob(f'{downloader.MOVE_STAGING_PREFIX}*')),
                [],
            )

    def test_concurrent_moves_allocate_distinct_names(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            destination = root_path / 'video.mp4'
            destination.write_text('old', encoding='utf-8')
            sources = [root_path / 'source-a.mp4', root_path / 'source-b.mp4']
            sources[0].write_text('new-a', encoding='utf-8')
            sources[1].write_text('new-b', encoding='utf-8')

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda source: downloader.move_without_overwrite(
                        str(source),
                        str(destination),
                    ),
                    sources,
                ))

            self.assertEqual(destination.read_text(encoding='utf-8'), 'old')
            self.assertEqual(
                {Path(result).name for result in results},
                {'video (1).mp4', 'video (2).mp4'},
            )
            self.assertEqual(
                {Path(result).read_text(encoding='utf-8') for result in results},
                {'new-a', 'new-b'},
            )


if __name__ == '__main__':
    unittest.main()
