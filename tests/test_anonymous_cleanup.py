import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import anonymous_cleanup
import user_store


class TestAnonymousCleanup(unittest.TestCase):
    def test_user_store_migrates_version_one_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / 'users.sqlite3')
            user_store.init_db(db_path)
            with user_store.connect(db_path) as db:
                db.executescript(
                    '''
                    DROP TABLE anonymous_daily_usage;
                    DROP TABLE anonymous_media_owners;
                    DROP TABLE anonymous_task_owners;
                    PRAGMA user_version = 1;
                    '''
                )

            user_store.init_db(db_path)

            with user_store.connect(db_path) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                tables = {
                    row['name'] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(version, user_store.SCHEMA_VERSION)
            self.assertIn('access_tokens', tables)
            self.assertIn('anonymous_daily_usage', tables)
            self.assertIn('anonymous_task_owners', tables)
            self.assertIn('anonymous_media_owners', tables)

    def test_worker_syncs_result_and_deletes_media_after_24_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            urls_dir = root / 'urls'
            files_dir = root / 'files'
            urls_dir.mkdir()
            files_dir.mkdir()
            db_path = str(root / 'users.sqlite3')
            user_store.init_db(db_path)
            task_id = 'v20260914000000AbC'
            user_store.record_anonymous_tasks(
                db_path,
                [task_id],
                'anonymous-session',
                'hashed-ip',
            )
            (urls_dir / f'{task_id}.result.json').write_text(
                '{"files":["video.mp4"]}', encoding='utf-8'
            )
            media_path = files_dir / 'video.mp4'
            media_path.write_bytes(b'media')
            runtime_config = {
                'URLS_DIR': str(urls_dir),
                'FILES_DIR': str(files_dir),
                'USER_DB_PATH': db_path,
                'ANONYMOUS_FILES_EXPIRE_HOURS': 24,
            }

            anonymous_cleanup.sync_media_ownership(runtime_config, Mock())
            with patch(
                'anonymous_cleanup.time.time',
                return_value=user_store.now_ts() + 25 * 3600,
            ):
                anonymous_cleanup.cleanup_expired_media(
                    runtime_config, Mock()
                )

            self.assertFalse(media_path.exists())
            self.assertIsNone(
                user_store.anonymous_media_owner(db_path, 'video.mp4')
            )


if __name__ == '__main__':
    unittest.main()
