import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import user_store
from auth_helper import install_auth


class TestTaskInfoAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.urls_dir = root / 'urls'
        self.logs_dir = root / 'logs'
        self.urls_dir.mkdir()
        self.logs_dir.mkdir()
        self.client = app.app.test_client()
        app.app.testing = True
        install_auth(self)

        self.patches = [
            patch.object(app, 'URLS_DIR', str(self.urls_dir)),
            patch.dict(app.config, {'LOG_DIR': str(self.logs_dir)}),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def write_task(self, task_id, extension):
        (self.urls_dir / f'{task_id}{extension}').write_text(
            'https://example.com/video',
            encoding='utf-8',
        )
        self.own_task(task_id)

    def own_task(self, task_id):
        """任务接口只返回当前用户的任务，测试里的任务需登记归属。"""
        user_store.record_tasks(
            self.user_db_path, [task_id], self.logged_in_user['id']
        )

    def test_reports_queued_task(self):
        task_id = 'v20260723120000AbC'
        self.write_task(task_id, '.txt')

        response = self.client.post('/api/task_info', json={'tasks': [task_id]})
        task = response.get_json()['tasks'][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(task['state'], 'queued')
        self.assertEqual(task['progress']['percent'], 0)

    def test_reports_lightweight_youtube_preview_for_queued_task(self):
        task_id = 'v20260723120000YtP'
        (self.urls_dir / f'{task_id}.txt').write_text(
            'https://youtu.be/dQw4w9WgXcQ?t=12',
            encoding='utf-8',
        )
        self.own_task(task_id)

        response = self.client.post('/api/task_info', json={'tasks': [task_id]})
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['source_url'], 'https://youtu.be/dQw4w9WgXcQ?t=12')
        self.assertEqual(
            task['thumbnail'],
            'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg',
        )
        self.assertEqual(task['metadata_state'], 'preview')

    def test_non_youtube_task_has_no_derived_preview(self):
        task_id = 'a20260723120000NoP'
        self.write_task(task_id, '.txt')

        response = self.client.post('/api/task_info', json={'tasks': [task_id]})
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['source_url'], 'https://example.com/video')
        self.assertIsNone(task['thumbnail'])
        self.assertEqual(task['metadata_state'], 'pending')

    def test_reports_structured_download_progress(self):
        task_id = 'a20260723120000XyZ'
        self.write_task(task_id, '.downloading')
        (self.logs_dir / f'{task_id}.log').write_text(
            (
                'PYDL_PROGRESS|downloading| 42.5%|4.25MiB|10.00MiB|'
                '1.50MiB/s|00:04|m4a|140|none|mp4a.40.2\n'
            ),
            encoding='utf-8',
        )

        response = self.client.post('/api/task_info', json={'tasks': [task_id]})
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['state'], 'downloading')
        self.assertEqual(task['progress']['percent'], 42.5)
        self.assertEqual(task['progress']['stage'], 'download_audio')
        self.assertEqual(task['progress']['speed'], '1.50MiB/s')
        self.assertEqual(task['progress']['eta'], '00:04')

    def test_verbose_command_line_is_not_parsed_as_progress(self):
        log_path = self.logs_dir / 'verbose.log'
        debug_line = (
            "[debug] Command-line config: ['--progress-template', "
            "'download:PYDL_PROGRESS|%(progress.status)s|"
            "%(progress._percent_str)s|%(progress._downloaded_bytes_str)s|"
            "%(progress._total_bytes_str)s|%(progress._speed_str)s|"
            "%(progress._eta_str)s|%(info.ext)s']\n"
        )
        log_path.write_text(debug_line, encoding='utf-8')

        self.assertEqual(app.parse_task_progress(str(log_path)), {})

        log_path.write_text(
            debug_line
            + 'PYDL_PROGRESS|downloading|12.5%|1.00MiB|8.00MiB|'
            '2.00MiB/s|00:03|mp4|135|avc1|none\n',
            encoding='utf-8',
        )

        progress = app.parse_task_progress(str(log_path))

        self.assertEqual(progress['percent'], 12.5)
        self.assertEqual(progress['downloaded'], '1.00MiB')
        self.assertEqual(progress['stage'], 'download_video')

    def test_structured_progress_distinguishes_video_and_subtitles(self):
        cases = (
            (
                'PYDL_PROGRESS|downloading|100.0%|10M|10M|1M/s|00:00|'
                'mp4|137|avc1.640028|none\n',
                'download_video',
            ),
            (
                'PYDL_PROGRESS|downloading|100.0%|20K|20K|100K/s|00:00|'
                'vtt|en|none|none\n',
                'download_subtitles',
            ),
        )

        for index, (line, expected_stage) in enumerate(cases):
            with self.subTest(stage=expected_stage):
                log_path = self.logs_dir / f'stage-{index}.log'
                log_path.write_text(line, encoding='utf-8')

                progress = app.parse_task_progress(str(log_path))

                self.assertEqual(progress['percent'], 100)
                self.assertEqual(progress['stage'], expected_stage)

    def test_postprocessing_stage_overrides_previous_100_percent(self):
        log_path = self.logs_dir / 'merge.log'
        log_path.write_text(
            (
                'PYDL_PROGRESS|finished|100.0%|NA|35M|1M/s|NA|'
                'm4a|140|none|mp4a.40.2\n'
                '[Merger] Merging formats into \"/tmp/video.mp4\"\n'
            ),
            encoding='utf-8',
        )

        progress = app.parse_task_progress(str(log_path))

        self.assertEqual(progress['percent'], 100)
        self.assertEqual(progress['stage'], 'merge_media')
        self.assertEqual(progress['downloaded'], '')
        self.assertEqual(progress['total'], '35M')
        self.assertEqual(progress['speed'], '1M/s')
        self.assertEqual(progress['eta'], '')

    def test_completed_task_does_not_return_na_downloaded_size(self):
        task_id = 'v20260723120000NaX'
        self.write_task(task_id, '.ok')
        (self.logs_dir / f'{task_id}.log').write_text(
            (
                'PYDL_PROGRESS|finished|100.0%|NA|5.44MiB|'
                '645.56KiB/s|NA|mp4|18|avc1|mp4a\n'
            ),
            encoding='utf-8',
        )

        response = self.client.post('/api/task_info', json={'tasks': task_id})
        progress = response.get_json()['tasks'][0]['progress']

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('downloaded', progress)
        self.assertNotIn('total', progress)
        self.assertNotIn('speed', progress)
        self.assertNotIn('eta', progress)

    def test_completed_task_returns_persisted_final_summary(self):
        task_id = 'v20260804120000Sum'
        filename = 'final.mp4'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        (files_dir / filename).write_bytes(b'v' * 10)
        (self.urls_dir / f'{task_id}.result.json').write_text(
            json.dumps({
                'files': [filename, 'final.zh-Hans.srt'],
                'summary': {
                    'primary_file': filename,
                    'final_size_bytes': 101384507,
                    'elapsed_seconds': 132.4,
                    'average_speed_bytes_per_second': 765744.0,
                },
            }),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        progress = response.get_json()['tasks'][0]['progress']

        self.assertEqual(progress['final_size_bytes'], 101384507)
        self.assertEqual(progress['elapsed_seconds'], 132.4)
        self.assertEqual(
            progress['average_speed_bytes_per_second'],
            765744.0,
        )
        self.assertNotIn('downloaded', progress)
        self.assertNotIn('total', progress)
        self.assertNotIn('speed', progress)
        self.assertNotIn('eta', progress)

    def test_legacy_completed_task_returns_only_final_file_size(self):
        task_id = 'a20260804120000Old'
        filename = 'final.mp3'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        (files_dir / filename).write_bytes(b'a' * 2048)
        (files_dir / 'final.zh-Hans.srt').write_bytes(b's' * 8192)
        (self.urls_dir / f'{task_id}.result.json').write_text(
            json.dumps({'files': [filename, 'final.zh-Hans.srt']}),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        progress = response.get_json()['tasks'][0]['progress']

        self.assertEqual(progress['final_size_bytes'], 2048)
        self.assertNotIn('elapsed_seconds', progress)
        self.assertNotIn('average_speed_bytes_per_second', progress)

    def read_asset(self, *parts):
        if parts[0] == 'templates':
            return Path(app.app.template_folder, *parts[1:]).read_text(
                encoding='utf-8',
            )
        return Path(app.app.static_folder, *parts[1:]).read_text(
            encoding='utf-8',
        )

    def test_completed_summary_uses_final_summary_fields(self):
        script = self.read_asset('static', 'dropload.js')

        self.assertIn("state === 'completed'", script)
        self.assertIn('progress.final_size_bytes', script)
        self.assertIn('progress.elapsed_seconds', script)
        self.assertIn('progress.average_speed_bytes_per_second', script)
        self.assertIn("parts.push(finalSize + ' in ' + elapsed);", script)
        self.assertIn("state === 'downloading' ? 'dl-spin' : ''", script)

    def test_video_duration_rounds_fractional_seconds(self):
        script = self.read_asset('static', 'dropload.js')

        self.assertIn(
            'var totalSeconds = Math.round(Number(data.duration) || 0);',
            script,
        )
        self.assertIn('var minutes = Math.floor(totalSeconds / 60);', script)
        self.assertIn('var seconds = totalSeconds % 60;', script)

    def test_video_metadata_retries_slow_requests(self):
        script = self.read_asset('static', 'dropload.js')

        self.assertIn('var METADATA_TIMEOUT_MS = 30000;', script)
        self.assertIn('var METADATA_MAX_ATTEMPTS = 2;', script)
        self.assertIn("fetch('/api/video_info_basic'", script)
        self.assertNotIn("fetch('/api/video_info',", script)
        self.assertIn(
            "setThumbnail(data.thumbnail, data.title || '视频缩略图');",
            script,
        )
        self.assertIn('drawerThumb.hidden = false;', script)
        self.assertIn("error.name === 'AbortError'", script)
        self.assertIn('正在重试获取标题…', script)
        self.assertIn('updateMetadata(url, attempt + 1);', script)

    def test_video_metadata_is_requested_once_per_source_url(self):
        # 元数据接口较慢，抽屉反复打开同一任务时必须命中缓存。
        script = self.read_asset('static', 'dropload.js')

        self.assertIn("cached.state === 'ready'", script)
        self.assertIn("cached.state === 'pending'", script)
        self.assertIn("metadata[url] = { state: 'pending' };", script)

    def test_task_detail_drawer_opens_from_task_row(self):
        template = self.read_asset('templates', 'index.html')
        script = self.read_asset('static', 'dropload.js')

        self.assertIn('<div class="dl-drawer-overlay" hidden>', template)
        self.assertIn('aria-label="关闭任务详情"', template)
        self.assertIn('openDrawer(item.dataset.task);', script)
        self.assertIn("event.key === 'Escape'", script)
        self.assertNotIn('task-log-toggle', template)

    def test_task_detail_drawer_shows_only_current_task_log(self):
        template = self.read_asset('templates', 'index.html')
        script = self.read_asset('static', 'dropload.js')
        css = self.read_asset('static', 'dropload.css')

        self.assertIn('class="dl-drawer-log"', template)
        self.assertIn('tasks: [taskId]', script)
        self.assertIn('max-width: 460px;', css)
        self.assertIn('background: #101827;', css)

    def test_task_log_autoscroll_matches_extension_controls(self):
        template = self.read_asset('templates', 'index.html')
        script = self.read_asset('static', 'dropload.js')

        self.assertIn('class="dl-drawer-autoscroll is-active"', template)
        self.assertIn('aria-pressed="true"', template)
        self.assertIn('自动滚动：开', template)
        self.assertIn('taskLogAutoScroll = !taskLogAutoScroll;', script)
        self.assertIn("if (taskLogAutoScroll && !isTaskLogNearBottom())", script)
        self.assertIn('drawerLog.scrollTop = drawerLog.scrollHeight;', script)

    def test_drawer_orders_preview_url_status_then_log(self):
        template = self.read_asset('templates', 'index.html')
        css = self.read_asset('static', 'dropload.css')

        self.assertLess(
            template.index('class="dl-drawer-info"'),
            template.index('class="dl-drawer-url"'),
        )
        self.assertLess(
            template.index('class="dl-drawer-url"'),
            template.index('class="dl-drawer-meta"'),
        )
        self.assertLess(
            template.index('class="dl-drawer-meta"'),
            template.index('class="dl-drawer-log"'),
        )
        self.assertIn('aria-label="复制链接" title="复制链接"', template)
        # 链接始终单行显示，超出部分省略，不撑开抽屉。
        self.assertIn('.dl-drawer-url-text {', css)
        self.assertIn('text-overflow: ellipsis;', css)

    def test_completed_task_row_links_to_download_and_player(self):
        script = self.read_asset('static', 'dropload.js')

        self.assertIn('info.download_url', script)
        self.assertIn('info.player_url', script)
        self.assertIn("' 下载文件'", script)
        self.assertIn("' 播放'", script)

    def test_task_list_uses_responsive_two_column_layout(self):
        template = self.read_asset('templates', 'index.html')
        css = self.read_asset('static', 'dropload.css')

        self.assertIn('{{ tasks|length }} 个任务', template)
        self.assertIn('支持的站点', template)
        self.assertIn('dl-task-demo', template)
        self.assertIn('.dl-task-list {', css)
        # 小屏单栏、任务列表限高；桌面右侧固定宽度栏。
        self.assertIn('max-height: 420px;', css)
        self.assertIn('grid-template-columns: minmax(0, 1fr) 390px;', css)

    def test_task_page_does_not_show_submitted_url_in_input(self):
        template = self.read_asset('templates', 'index.html')

        self.assertIn('value="{{ \'\' if tasks else url }}"', template)
        # 服务端渲染的任务 ID 通过 JSON 交给前端，避免再次内联 URL 文本。
        self.assertIn("'tasks': tasks, 'url': url", template)
        self.assertIn('| tojson }}</script>', template)

    def test_completed_task_is_always_100_percent(self):
        task_id = 'v20260723120000QwE'
        self.write_task(task_id, '.ok')

        response = self.client.post('/api/task_info', json={'tasks': task_id})
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['state'], 'completed')
        self.assertEqual(task['progress']['percent'], 100)

    def test_completed_video_task_returns_player_url(self):
        task_id = 'v20260723120000PlY'
        filename = '视频 file.mp4'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        (files_dir / filename).touch()
        (self.urls_dir / f'{task_id}.result.json').write_text(
            json.dumps({'files': [filename, '视频 file.zh-Hans.srt']}),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['files'], [filename])
        self.assertEqual(
            task['player_url'],
            '/player?file=%E8%A7%86%E9%A2%91+file.mp4',
        )
        self.assertEqual(
            task['download_url'],
            '/downloads/%E8%A7%86%E9%A2%91%20file.mp4',
        )

    def test_completed_audio_task_returns_audio_player_url(self):
        task_id = 'a20260804120000AuD'
        filename = '音频 file.mp3'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        (files_dir / filename).touch()
        (self.urls_dir / f'{task_id}.result.json').write_text(
            json.dumps({'files': [filename]}),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['files'], [filename])
        self.assertEqual(
            task['player_url'],
            '/audio-player?file=%E9%9F%B3%E9%A2%91+file.mp3',
        )
        self.assertEqual(
            task['download_url'],
            '/downloads/%E9%9F%B3%E9%A2%91%20file.mp3',
        )

    def test_completed_legacy_task_recovers_player_url_from_move_log(self):
        task_id = 'v20260723161431ohK'
        filename = '恢复的视频 (1).mp4'
        self.write_task(task_id, '.ok')
        files_dir = Path(self.temp_dir.name) / 'files'
        files_dir.mkdir()
        final_path = files_dir / filename
        final_path.touch()
        (self.logs_dir / 'downloader.log').write_text(
            (
                '2026-07-23 16:15:49 [INFO] 已移动文件: '
                f'/tmp/{task_id}/恢复的视频.mp4 -> {final_path}\n'
            ),
            encoding='utf-8',
        )

        with patch.object(app, 'FILES_DIR', str(files_dir)):
            response = self.client.post(
                '/api/task_info',
                json={'tasks': task_id},
            )
        task = response.get_json()['tasks'][0]

        self.assertEqual(task['files'], [filename])
        self.assertEqual(
            task['player_url'],
            '/player?file=%E6%81%A2%E5%A4%8D%E7%9A%84%E8%A7%86%E9%A2%91+(1).mp4',
        )

    def test_rejects_invalid_task_id_without_path_lookup(self):
        response = self.client.post(
            '/api/task_info',
            json={'tasks': ['../../config']},
        )
        task = response.get_json()['tasks'][0]

        self.assertFalse(task['exists'])
        # 非法 ID 不属于任何人，与「任务不存在」返回同样的 missing
        self.assertEqual(task['state'], 'missing')

    def test_accepts_legacy_numeric_task_suffix(self):
        task_id = 'a202505161122552581'
        self.write_task(task_id, '.txt')

        response = self.client.post('/api/task_info', json={'tasks': task_id})
        task = response.get_json()['tasks'][0]

        self.assertTrue(task['exists'])
        self.assertEqual(task['state'], 'queued')

    def test_parses_legacy_default_progress_line(self):
        log_path = self.logs_dir / 'legacy.log'
        log_path.write_text(
            '[download]  18.7% of ~  12.00MiB at  2.00MiB/s ETA 00:05\n',
            encoding='utf-8',
        )

        progress = app.parse_task_progress(str(log_path))

        self.assertEqual(progress['percent'], 18.7)
        self.assertEqual(progress['total'], '12.00MiB')
        self.assertEqual(progress['speed'], '2.00MiB/s')
        self.assertEqual(progress['eta'], '00:05')


if __name__ == '__main__':
    unittest.main()
