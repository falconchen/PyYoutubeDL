import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_summary_store as store
import app as app_module
from app import app


class TestAiSummaryApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / 'summary.sqlite3')
        self.config = {
            'AI_SUMMARY_DB_PATH': self.db_path,
            'AI_SUMMARY_ACCESS_TOKEN': 'extension-ai-token',
            'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
            'AI_API_MODEL': 'test-model',
            'AI_API_TOKEN': 'provider-token',
        }
        self.config_patcher = patch.dict(app_module.config, self.config)
        self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)
        store.init_db(self.db_path)
        app.testing = True
        self.client = app.test_client()
        self.url = 'https://www.youtube.com/watch?v=l38ceFOWOAE'
        self.headers = {'X-Yter-AI-Token': 'extension-ai-token'}

    def submit(self):
        with patch('app.ai_summary_store.validate_public_url', return_value=self.url):
            return self.client.post(
                '/api/ai_summaries',
                json={'url': self.url},
                headers=self.headers,
            )

    def test_requires_independent_access_token(self):
        response = self.client.post('/api/ai_summaries', json={'url': self.url})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['Cache-Control'], 'no-store, private')

    def test_returns_503_when_access_api_is_disabled(self):
        with patch.dict(app_module.config, {'AI_SUMMARY_ACCESS_TOKEN': ''}):
            response = self.client.post(
                '/api/ai_summaries',
                json={'url': self.url},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 503)

    def test_creates_and_deduplicates_async_url_job(self):
        first = self.submit()
        second = self.submit()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.headers['Retry-After'], '2')
        self.assertEqual(first.get_json()['job_id'], second.get_json()['job_id'])

    def test_returns_persisted_summary_immediately(self):
        job = self.submit().get_json()['job_id']
        profile = store.summary_profile_key(app_module.config)
        media_id = store.upsert_media(
            self.db_path,
            'youtube',
            'l38ceFOWOAE',
            self.url,
            '测试视频',
            aliases=(self.url,),
        )
        store.save_summary_and_complete(
            self.db_path,
            job,
            media_id,
            profile,
            'test-model',
            'zh',
            'manual',
            'hash',
            '# 已保存总结',
        )

        response = self.submit()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['cached'])
        self.assertEqual(response.get_json()['summary']['markdown'], '# 已保存总结')

    def test_job_endpoint_returns_422_for_failure(self):
        job_id = self.submit().get_json()['job_id']
        store.fail_or_retry_job(
            self.db_path,
            job_id,
            1,
            'no_subtitles',
            '当前页面没有可用字幕',
            False,
        )

        response = self.client.get(
            f'/api/ai_summaries/jobs/{job_id}',
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()['error']['code'], 'no_subtitles')
        self.assertFalse(response.get_json()['error']['retryable'])

    def test_job_stream_returns_partial_and_completed_ndjson(self):
        job_id = self.submit().get_json()['job_id']
        claimed = store.claim_next_job(self.db_path, 'worker-one')
        store.update_job(self.db_path, job_id, 'generating')
        store.update_job_stream(self.db_path, job_id, '## 部分')

        response = self.client.get(
            f'/api/ai_summaries/jobs/{job_id}/stream',
            headers=self.headers,
            buffered=False,
        )
        iterator = iter(response.response)
        partial = next(iterator).decode('utf-8')
        self.assertIn('"partial_markdown": "## 部分"', partial)

        media_id = store.upsert_media(
            self.db_path, 'youtube', 'l38ceFOWOAE', self.url, '测试视频', aliases=(self.url,),
        )
        store.save_summary_and_complete(
            self.db_path, job_id, media_id, store.summary_profile_key(app_module.config),
            'test-model', 'zh', 'manual', 'hash', '## 完整总结',
        )
        completed = next(iterator).decode('utf-8')
        self.assertIn('"status": "completed"', completed)
        self.assertIn('## 完整总结', completed)


if __name__ == '__main__':
    unittest.main()
