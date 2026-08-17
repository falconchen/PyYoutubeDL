import errno
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_move_records_final_renamed_filenames_for_task(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tmp_dir = root_path / 'tmp' / 'v20260723120000AbC'
            files_dir = root_path / 'files'
            urls_dir = root_path / 'urls'
            tmp_dir.mkdir(parents=True)
            files_dir.mkdir()
            urls_dir.mkdir()
            (tmp_dir / 'video.mp4').write_text('new video', encoding='utf-8')
            (tmp_dir / 'video.zh-Hans.srt').write_text('subtitle', encoding='utf-8')
            (files_dir / 'video.mp4').write_text('old video', encoding='utf-8')

            with patch.dict(
                downloader.config,
                {
                    'FILES_DIR': str(files_dir),
                    'URLS_DIR': str(urls_dir),
                },
            ):
                result = self.handler.move_files(
                    str(tmp_dir),
                    task_id='v20260723120000AbC',
                )

            result_data = json.loads(
                (urls_dir / 'v20260723120000AbC.result.json').read_text(
                    encoding='utf-8',
                )
            )
            self.assertTrue(result)
            self.assertEqual(
                set(result_data['files']),
                {'video (1).mp4', 'video.zh-Hans.srt'},
            )

    def test_video_summary_uses_largest_final_video_and_full_elapsed_time(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            tmp_dir = root_path / 'tmp' / 'v20260804120000Vid'
            files_dir = root_path / 'files'
            urls_dir = root_path / 'urls'
            tmp_dir.mkdir(parents=True)
            files_dir.mkdir()
            urls_dir.mkdir()
            (tmp_dir / 'small.mp4').write_bytes(b'v' * 8)
            (tmp_dir / 'final.mkv').write_bytes(b'v' * 24)
            (tmp_dir / 'final.zh-Hans.srt').write_bytes(b's' * 100)

            with (
                patch.dict(
                    downloader.config,
                    {
                        'FILES_DIR': str(files_dir),
                        'URLS_DIR': str(urls_dir),
                    },
                ),
                patch('downloader.time.monotonic', return_value=132.4),
            ):
                result = self.handler.move_files(
                    str(tmp_dir),
                    task_id='v20260804120000Vid',
                    mode='video',
                    started_at=0,
                )

            result_data = json.loads(
                (urls_dir / 'v20260804120000Vid.result.json').read_text(
                    encoding='utf-8',
                )
            )
            summary = result_data['summary']
            self.assertTrue(result)
            self.assertEqual(summary['primary_file'], 'final.mkv')
            self.assertEqual(summary['final_size_bytes'], 24)
            self.assertEqual(summary['elapsed_seconds'], 132.4)
            self.assertAlmostEqual(
                summary['average_speed_bytes_per_second'],
                24 / 132.4,
            )

    def test_audio_summary_uses_audio_file_and_ignores_subtitle(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            audio = root_path / 'final.mp3'
            subtitle = root_path / 'final.zh-Hans.srt'
            audio.write_bytes(b'a' * 20)
            subtitle.write_bytes(b's' * 200)

            summary = downloader.build_task_summary(
                [str(audio), str(subtitle)],
                'audio',
                10,
            )

            self.assertEqual(summary['primary_file'], 'final.mp3')
            self.assertEqual(summary['final_size_bytes'], 20)
            self.assertEqual(summary['average_speed_bytes_per_second'], 2)

    def test_single_video_output_still_generates_summary(self):
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / 'only-video.mp4'
            video.write_bytes(b'v' * 50)

            summary = downloader.build_task_summary(
                [str(video)],
                'video',
                5,
            )

            self.assertEqual(summary['primary_file'], 'only-video.mp4')
            self.assertEqual(summary['final_size_bytes'], 50)
            self.assertEqual(summary['elapsed_seconds'], 5)
            self.assertEqual(summary['average_speed_bytes_per_second'], 10)

    def test_process_timer_starts_after_entering_downloading_state(self):
        with tempfile.TemporaryDirectory() as root:
            task_path = Path(root) / 'v20260804120000Tim.txt'
            task_path.write_text('https://example.com/video', encoding='utf-8')

            with (
                patch('downloader.time.sleep'),
                patch('downloader.time.monotonic', return_value=123.5),
                patch.object(self.handler, 'download', return_value=True) as download,
            ):
                self.handler.process_file(str(task_path))

            download.assert_called_once_with(
                'https://example.com/video',
                'v20260804120000Tim',
                'video',
                started_at=123.5,
            )
            self.assertTrue((Path(root) / 'v20260804120000Tim.ok').exists())

    def test_runtime_command_adds_metadata_for_video_and_audio(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            log_dir = root_path / 'logs'
            tmp_dir = root_path / 'tmp'
            log_dir.mkdir()
            tmp_dir.mkdir()

            for mode in ('video', 'audio'):
                with self.subTest(mode=mode):
                    process = MagicMock()
                    process.stdout = []
                    process.returncode = 0

                    with (
                        patch.dict(
                            downloader.config,
                            {
                                'LOG_DIR': str(log_dir),
                                'TMP_DIR': str(tmp_dir),
                            },
                        ),
                        patch(
                            'downloader.subprocess.Popen',
                            return_value=process,
                        ) as popen,
                        patch(
                            'downloader.probe_subtitle_fallback',
                            return_value=None,
                        ),
                        patch.object(self.handler, 'move_files', return_value=True),
                    ):
                        result = self.handler.download(
                            'https://example.com/media',
                            f'{mode}-metadata',
                            mode,
                        )

                    self.assertTrue(result)
                    cmd = popen.call_args.args[0]
                    self.assertEqual(cmd.count('--add-metadata'), 1)
                    self.assertLess(
                        cmd.index('--config-location'),
                        cmd.index('--add-metadata'),
                    )
                    self.assertLess(
                        cmd.index('--add-metadata'),
                        cmd.index('https://example.com/media'),
                    )

    def test_subtitle_fallback_is_not_used_when_config_matched(self):
        result = downloader.select_subtitle_fallback({
            'requested_subtitles': {'zh': {'ext': 'vtt'}},
            'subtitles': {'zh': [{'ext': 'vtt'}]},
        })

        self.assertIsNone(result)

    def test_subtitle_fallback_prefers_requested_order_for_manual_subtitles(self):
        result = downloader.select_subtitle_fallback({
            'requested_subtitles': None,
            'language': 'ja',
            'subtitles': {
                'en': [{'ext': 'vtt'}],
                'zh': [{'ext': 'vtt'}],
                'ja': [{'ext': 'vtt'}],
            },
        })

        self.assertEqual(result, ('zh', '人工字幕'))

    def test_subtitle_fallback_ignores_danmaku_and_live_chat(self):
        result = downloader.select_subtitle_fallback({
            'requested_subtitles': None,
            'subtitles': {
                'danmaku': [{'ext': 'xml'}],
                'live_chat': [{'ext': 'json'}],
            },
        })

        self.assertIsNone(result)

    def test_subtitle_fallback_prefers_any_manual_track_over_translation(self):
        result = downloader.select_subtitle_fallback({
            'requested_subtitles': None,
            'language': 'ja',
            'subtitles': {'ja': [{'ext': 'vtt'}]},
            'automatic_captions': {
                'zh-Hans-ja': [{'ext': 'vtt', 'name': 'Chinese from Japanese'}],
            },
        })

        self.assertEqual(result, ('ja', '人工字幕'))

    def test_subtitle_fallback_prefers_automatic_original_track(self):
        result = downloader.select_subtitle_fallback({
            'requested_subtitles': None,
            'language': 'ja-JP',
            'automatic_captions': {
                'zh-Hans-ja': [{'ext': 'vtt', 'name': 'Chinese from Japanese'}],
                'ja': [{'ext': 'vtt', 'name': 'Japanese'}],
            },
        })

        self.assertEqual(result, ('ja', '自动原文字幕'))

    def test_subtitle_fallback_uses_preferred_automatic_translation_last(self):
        result = downloader.select_subtitle_fallback({
            'requested_subtitles': None,
            'automatic_captions': {
                'fr-ja': [{'ext': 'vtt', 'name': 'French from Japanese'}],
                'zh-Hant-ja': [
                    {'ext': 'vtt', 'name': 'Chinese from Japanese'},
                ],
            },
        })

        self.assertEqual(result, ('zh-Hant-ja', '自动翻译字幕'))

    def test_video_download_adds_dynamic_subtitle_language(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            log_dir = root_path / 'logs'
            tmp_dir = root_path / 'tmp'
            log_dir.mkdir()
            tmp_dir.mkdir()
            process = MagicMock(stdout=[], returncode=0)

            with (
                patch.dict(
                    downloader.config,
                    {'LOG_DIR': str(log_dir), 'TMP_DIR': str(tmp_dir)},
                ),
                patch(
                    'downloader.probe_subtitle_fallback',
                    return_value=('ja', '人工字幕'),
                ),
                patch(
                    'downloader.subprocess.Popen',
                    return_value=process,
                ) as popen,
                patch.object(self.handler, 'move_files', return_value=True),
            ):
                result = self.handler.download(
                    'https://example.com/video',
                    'video-subtitle-fallback',
                    'video',
                )

            self.assertTrue(result)
            cmd = popen.call_args.args[0]
            self.assertEqual(
                cmd[cmd.index('--sub-langs') + 1],
                'ja',
            )

    def test_probe_uses_real_config_and_disables_configured_sleep(self):
        metadata = {
            'requested_subtitles': None,
            'subtitles': {'ja': [{'ext': 'vtt'}]},
        }
        completed = MagicMock(
            returncode=0,
            stdout=json.dumps(metadata),
        )

        with patch('downloader.subprocess.run', return_value=completed) as run:
            result = downloader.probe_subtitle_fallback(
                'https://example.com/video',
                '/project/yt-dlp.local.conf',
            )

        self.assertEqual(result, ('ja', '人工字幕'))
        cmd = run.call_args.args[0]
        self.assertEqual(
            cmd[cmd.index('--config-location') + 1],
            '/project/yt-dlp.local.conf',
        )
        self.assertEqual(cmd[cmd.index('--sleep-subtitles') + 1], '0')
        self.assertIn('--simulate', cmd)
        self.assertIn('--dump-single-json', cmd)


if __name__ == '__main__':
    unittest.main()
