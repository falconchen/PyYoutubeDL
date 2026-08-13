import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

import ai_summary_store as store


class TestAiSummaryStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / 'summary.sqlite3')
        store.init_db(self.db_path)
        self.profile = 'profile-one'

    def test_initializes_wal_schema(self):
        with store.connect(self.db_path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(
                db.execute('PRAGMA journal_mode').fetchone()[0].lower(),
                'wal',
            )

    def test_migrates_version_one_jobs_for_streaming(self):
        migration_path = str(Path(self.temp_dir.name) / 'version-one.sqlite3')
        with sqlite3.connect(migration_path) as db:
            db.execute('CREATE TABLE ai_summary_jobs (id TEXT PRIMARY KEY)')
            db.execute('PRAGMA user_version = 1')
        store.init_db(migration_path)

        with sqlite3.connect(migration_path) as db:
            columns = {
                row[1] for row in db.execute('PRAGMA table_info(ai_summary_jobs)')
            }
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertIn('partial_markdown', columns)
        self.assertIn('stream_revision', columns)

    def test_normalizes_youtube_aliases(self):
        expected = 'https://www.youtube.com/watch?v=l38ceFOWOAE'
        self.assertEqual(
            store.normalize_source_url('https://youtu.be/l38ceFOWOAE?t=2'),
            expected,
        )
        self.assertEqual(
            store.normalize_source_url(
                'https://www.youtube.com/watch?v=l38ceFOWOAE&list=abc'
            ),
            expected,
        )

    def test_rejects_private_url(self):
        with self.assertRaisesRegex(ValueError, '私网'):
            store.validate_public_url('http://127.0.0.1/video')

    def test_duplicate_active_job_returns_same_job(self):
        url = 'https://www.youtube.com/watch?v=l38ceFOWOAE'
        first = store.create_url_job(self.db_path, url, url, self.profile)
        second = store.create_url_job(self.db_path, url, url, self.profile)

        self.assertEqual(first['job']['id'], second['job']['id'])

    def test_claim_recovers_expired_lease(self):
        url = 'https://video.example/watch/1'
        created = store.create_url_job(self.db_path, url, url, self.profile)
        first = store.claim_next_job(self.db_path, 'worker-one', lease_seconds=-1)
        second = store.claim_next_job(self.db_path, 'worker-two')

        self.assertEqual(first['id'], created['job']['id'])
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['attempts'], 2)

    def test_saved_summary_persists_and_alias_hits(self):
        url = 'https://www.youtube.com/watch?v=l38ceFOWOAE'
        job = store.create_url_job(self.db_path, url, url, self.profile)['job']
        media_id = store.upsert_media(
            self.db_path,
            'youtube',
            'l38ceFOWOAE',
            url,
            '标题',
            aliases=(url,),
        )
        summary, cache_hit = store.save_summary_and_complete(
            self.db_path,
            job['id'],
            media_id,
            self.profile,
            'test-model',
            'zh',
            'manual',
            'subtitle-hash',
            '# 总结',
        )

        self.assertFalse(cache_hit)
        self.assertEqual(summary['markdown'], '# 总结')
        self.assertEqual(
            store.find_summary_for_url(self.db_path, url, self.profile)['title'],
            '标题',
        )
        other_profile = store.find_summary_for_url(
            self.db_path,
            url,
            'new-profile',
        )
        self.assertIsNone(other_profile)

    def test_persists_streaming_markdown_progress(self):
        url = 'https://video.example/stream'
        job = store.create_url_job(self.db_path, url, url, self.profile)['job']
        claimed = store.claim_next_job(self.db_path, 'worker-one')
        store.update_job(self.db_path, claimed['id'], 'generating')
        store.update_job_stream(self.db_path, claimed['id'], '## 部分总结')

        updated = store.get_job(self.db_path, job['id'])
        self.assertEqual(updated['partial_markdown'], '## 部分总结')
        self.assertGreater(updated['stream_revision'], 0)

    def test_cleanup_only_removes_old_terminal_jobs(self):
        url = 'https://video.example/old'
        job = store.create_url_job(self.db_path, url, url, self.profile)['job']
        with store.connect(self.db_path) as db:
            db.execute(
                "UPDATE ai_summary_jobs SET status='failed', completed_at=1 WHERE id=?",
                (job['id'],),
            )
            db.commit()

        with patch('ai_summary_store.now_ts', return_value=40 * 86400):
            removed = store.cleanup_jobs(self.db_path, 30)

        self.assertEqual(removed, 1)
        self.assertIsNone(store.get_job(self.db_path, job['id']))


if __name__ == '__main__':
    unittest.main()
