import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_summary_store as store
import ai_summary_worker as worker
import app as app_module


class TestAiSummaryWorker(unittest.TestCase):
    def test_prefers_configured_requested_subtitle(self):
        selected = worker.select_summary_subtitle({
            'requested_subtitles': {
                'en': {'name': 'English'},
                'zh': {'name': 'Chinese'},
            },
            'subtitles': {'zh': [{'ext': 'vtt'}]},
        })
        self.assertEqual(selected, ('zh', 'manual', 'Chinese'))

    def test_falls_back_to_manual_original_language(self):
        selected = worker.select_summary_subtitle({
            'requested_subtitles': None,
            'language': 'ja',
            'subtitles': {'ja': [{'name': 'Japanese', 'ext': 'vtt'}]},
        })
        self.assertEqual(selected, ('ja', 'manual', 'Japanese'))

    def test_subtitle_file_is_cleaned_to_plain_text(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'test.srt'
            path.write_text(
                '1\n00:00:00,000 --> 00:00:01,000\n<b>第一句</b>\n\n'
                '2\n00:00:01,000 --> 00:00:02,000\n第二句\n',
                encoding='utf-8',
            )
            self.assertEqual(worker.subtitle_file_to_text(str(path)), '第一句\n第二句')

    def test_local_job_generates_persistent_summary_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            files_dir = root_path / 'files'
            tmp_dir = root_path / 'tmp'
            files_dir.mkdir()
            tmp_dir.mkdir()
            (files_dir / 'video.mp4').touch()
            db_path = str(root_path / 'summary.sqlite3')
            runtime = {
                'AI_SUMMARY_DB_PATH': db_path,
                'FILES_DIR': str(files_dir),
                'TMP_DIR': str(tmp_dir),
                'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                'AI_API_MODEL': 'test-model',
                'AI_API_TOKEN': 'provider-token',
            }
            store.init_db(db_path)
            profile = store.summary_profile_key(runtime)
            created = store.create_local_job(
                db_path,
                'video.mp4',
                2,
                'local:test-video',
                profile,
            )

            with (
                patch.dict(worker.config, runtime),
                patch.dict(app_module.config, runtime),
                patch('app.get_media_source_url', return_value=''),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[
                        {'stream_index': 2, 'language': 'zh', 'label': '中文'},
                    ],
                ),
                patch('app.extract_subtitle_text', return_value='第一句\n第二句'),
                patch('app.request_ai_summary', return_value='# 这是总结') as request_ai,
            ):
                with self.assertLogs(worker.logger, level='INFO') as captured_logs:
                    self.assertTrue(worker.run_once())

            job = store.get_job(db_path, created['job']['id'])
            self.assertEqual(job['status'], 'completed')
            self.assertEqual(job['summary']['markdown'], '# 这是总结')
            request_ai.assert_called_once()
            log_output = '\n'.join(captured_logs.output)
            self.assertIn('已领取 AI 总结任务:', log_output)
            self.assertIn('开始读取本地媒体:', log_output)
            self.assertIn('已选择字幕:', log_output)
            self.assertIn('开始提取内嵌字幕:', log_output)
            self.assertIn('开始调用 AI:', log_output)
            self.assertIn('AI 总结完成:', log_output)
            self.assertNotIn('provider-token', log_output)
            self.assertNotIn('第一句', log_output)
            self.assertNotIn('# 这是总结', log_output)
            summary_tmp = tmp_dir / 'ai-summary'
            self.assertEqual(list(summary_tmp.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
