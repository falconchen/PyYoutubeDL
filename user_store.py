#!venv/bin/python
"""用户、身份绑定、Google 令牌与资源归属的 SQLite 存储。

多用户模型：
- `users`：账号本体。首个注册者成为 admin 且直接可用，其余为 pending，
  需管理员审批后才能登录。
- `user_identities`：外部身份（目前只有 google），用于 Google 登录，
  同时也是「普通账号绑定 Google」的落点，一个用户至多一条 google 身份。
- `google_tokens`：按用户存放 OAuth 令牌与已授权 scope，替代原先的全局
  单份令牌文件。播放列表监控与后续的 Drive 上传都从这里取凭据。
- `task_owners` / `media_owners`：任务与下载产物的归属。任务文件仍留在
  URLS_DIR，本表只记录 task_id -> user_id，下载器无需改动。

约定与 ai_summary_store.py 保持一致：WAL、外键、busy_timeout，
以及用 PRAGMA user_version 做迁移。
"""

import os
import secrets
import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA_VERSION = 1

ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
STATUS_ACTIVE = 'active'
STATUS_PENDING = 'pending'
STATUS_DISABLED = 'disabled'

PROVIDER_GOOGLE = 'google'


def now_ts():
    return int(time.time())


def new_id():
    """用随机 id 而不是自增，避免用户数量和注册顺序被外部推断。"""
    return secrets.token_hex(16)


def normalize_email(email):
    return (email or '').strip().lower()


def connect(db_path):
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA busy_timeout = 10000')
    return connection


def init_db(db_path):
    """建库并执行迁移；重复调用是安全的。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    with connect(db_path) as db:
        db.execute('PRAGMA journal_mode = WAL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f'用户数据库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}'
            )
        if version == 0:
            db.executescript(
                '''
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT,
                    display_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE user_identities (
                    provider TEXT NOT NULL,
                    provider_user_id TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    email TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (provider, provider_user_id)
                );

                CREATE UNIQUE INDEX idx_identity_user
                    ON user_identities(provider, user_id);

                CREATE TABLE google_tokens (
                    user_id TEXT PRIMARY KEY
                        REFERENCES users(id) ON DELETE CASCADE,
                    token_json TEXT NOT NULL,
                    scopes TEXT NOT NULL DEFAULT '',
                    fail_reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE task_owners (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    url TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX idx_task_owner_user
                    ON task_owners(user_id, created_at DESC);

                CREATE TABLE media_owners (
                    filename TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX idx_media_owner_user ON media_owners(user_id);
                '''
            )
            db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')


# --- 用户 ---------------------------------------------------------------


def user_count(db_path):
    with connect(db_path) as db:
        return db.execute('SELECT COUNT(*) FROM users').fetchone()[0]


def _row_to_user(row):
    return dict(row) if row is not None else None


def get_user(db_path, user_id):
    with connect(db_path) as db:
        return _row_to_user(
            db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        )


def get_user_by_email(db_path, email):
    with connect(db_path) as db:
        return _row_to_user(
            db.execute(
                'SELECT * FROM users WHERE email = ?', (normalize_email(email),)
            ).fetchone()
        )


def list_users(db_path, status=None):
    query = 'SELECT * FROM users'
    params = ()
    if status:
        query += ' WHERE status = ?'
        params = (status,)
    query += ' ORDER BY created_at ASC'
    with connect(db_path) as db:
        return [dict(row) for row in db.execute(query, params)]


def create_user(db_path, email, password=None, display_name='', provider=None):
    """创建账号。首个账号自动成为已启用的管理员，其余进入待审批。

    Returns:
        dict: 新建的用户记录。

    Raises:
        ValueError: 邮箱为空或已被占用。
    """
    email = normalize_email(email)
    if not email:
        raise ValueError('邮箱不能为空')
    if not password and not provider:
        raise ValueError('必须提供密码或外部身份')

    timestamp = now_ts()
    user_id = new_id()
    password_hash = generate_password_hash(password) if password else None

    with connect(db_path) as db:
        first_user = db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
        role = ROLE_ADMIN if first_user else ROLE_USER
        status = STATUS_ACTIVE if first_user else STATUS_PENDING
        try:
            db.execute(
                '''INSERT INTO users
                   (id, email, password_hash, display_name, role, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    user_id,
                    email,
                    password_hash,
                    display_name or email.split('@')[0],
                    role,
                    status,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError('该邮箱已被注册') from exc

    return get_user(db_path, user_id)


def verify_password(user, password):
    """校验密码；仅用 Google 注册的账号没有密码，一律返回 False。"""
    if not user or not user.get('password_hash'):
        return False
    return check_password_hash(user['password_hash'], password or '')


def set_password(db_path, user_id, password):
    with connect(db_path) as db:
        db.execute(
            'UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?',
            (generate_password_hash(password), now_ts(), user_id),
        )


def set_status(db_path, user_id, status):
    if status not in {STATUS_ACTIVE, STATUS_PENDING, STATUS_DISABLED}:
        raise ValueError(f'未知状态: {status}')
    with connect(db_path) as db:
        db.execute(
            'UPDATE users SET status = ?, updated_at = ? WHERE id = ?',
            (status, now_ts(), user_id),
        )


def set_role(db_path, user_id, role):
    if role not in {ROLE_ADMIN, ROLE_USER}:
        raise ValueError(f'未知角色: {role}')
    with connect(db_path) as db:
        db.execute(
            'UPDATE users SET role = ?, updated_at = ? WHERE id = ?',
            (role, now_ts(), user_id),
        )


def first_admin_id(db_path):
    """返回最早创建的管理员 id，用于给历史数据兜底归属。"""
    with connect(db_path) as db:
        row = db.execute(
            '''SELECT id FROM users WHERE role = ?
               ORDER BY created_at ASC LIMIT 1''',
            (ROLE_ADMIN,),
        ).fetchone()
    return row['id'] if row else None


# --- 外部身份 -----------------------------------------------------------


def get_identity(db_path, provider, provider_user_id):
    with connect(db_path) as db:
        row = db.execute(
            '''SELECT * FROM user_identities
               WHERE provider = ? AND provider_user_id = ?''',
            (provider, provider_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_identity_for_user(db_path, provider, user_id):
    with connect(db_path) as db:
        row = db.execute(
            'SELECT * FROM user_identities WHERE provider = ? AND user_id = ?',
            (provider, user_id),
        ).fetchone()
    return dict(row) if row else None


def link_identity(
    db_path,
    provider,
    provider_user_id,
    user_id,
    email='',
    display_name='',
    avatar_url='',
):
    """绑定外部身份。同一外部账号不能绑定到两个本地用户。

    Raises:
        ValueError: 该外部账号已绑定到别的用户。
    """
    existing = get_identity(db_path, provider, provider_user_id)
    if existing and existing['user_id'] != user_id:
        raise ValueError('该 Google 账号已绑定到其他用户')

    with connect(db_path) as db:
        db.execute(
            '''INSERT INTO user_identities
               (provider, provider_user_id, user_id, email, display_name,
                avatar_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, provider_user_id) DO UPDATE SET
                 email = excluded.email,
                 display_name = excluded.display_name,
                 avatar_url = excluded.avatar_url''',
            (
                provider,
                provider_user_id,
                user_id,
                email,
                display_name,
                avatar_url,
                now_ts(),
            ),
        )


def unlink_identity(db_path, provider, user_id):
    with connect(db_path) as db:
        db.execute(
            'DELETE FROM user_identities WHERE provider = ? AND user_id = ?',
            (provider, user_id),
        )


# --- Google 令牌 --------------------------------------------------------


def save_google_token(db_path, user_id, token_json, scopes=''):
    with connect(db_path) as db:
        db.execute(
            '''INSERT INTO google_tokens
               (user_id, token_json, scopes, fail_reason, updated_at)
               VALUES (?, ?, ?, '', ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 token_json = excluded.token_json,
                 scopes = excluded.scopes,
                 fail_reason = '',
                 updated_at = excluded.updated_at''',
            (user_id, token_json, scopes, now_ts()),
        )


def load_google_token(db_path, user_id):
    with connect(db_path) as db:
        row = db.execute(
            'SELECT * FROM google_tokens WHERE user_id = ?', (user_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_google_token(db_path, user_id):
    with connect(db_path) as db:
        db.execute('DELETE FROM google_tokens WHERE user_id = ?', (user_id,))


def set_google_fail_reason(db_path, user_id, reason):
    """记录刷新失败原因，替代原来的全局 fail-lock 文件。"""
    with connect(db_path) as db:
        db.execute(
            'UPDATE google_tokens SET fail_reason = ?, updated_at = ? WHERE user_id = ?',
            (reason or '', now_ts(), user_id),
        )


def users_with_google_token(db_path):
    """返回所有已绑定且处于启用状态的用户，供后台 worker 逐个处理。"""
    with connect(db_path) as db:
        rows = db.execute(
            '''SELECT u.*, t.token_json, t.scopes, t.fail_reason
               FROM users u
               JOIN google_tokens t ON t.user_id = u.id
               WHERE u.status = ?
               ORDER BY u.created_at ASC''',
            (STATUS_ACTIVE,),
        ).fetchall()
    return [dict(row) for row in rows]


# --- 任务与媒体归属 -----------------------------------------------------


def record_tasks(db_path, task_ids, user_id, url='', media_type=''):
    timestamp = now_ts()
    with connect(db_path) as db:
        db.executemany(
            '''INSERT INTO task_owners (task_id, user_id, url, media_type, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO NOTHING''',
            [
                (task_id, user_id, url, media_type, timestamp)
                for task_id in task_ids
            ],
        )


def task_owner(db_path, task_id):
    with connect(db_path) as db:
        row = db.execute(
            'SELECT user_id FROM task_owners WHERE task_id = ?', (task_id,)
        ).fetchone()
    return row['user_id'] if row else None


def user_task_ids(db_path, user_id, limit=200):
    with connect(db_path) as db:
        rows = db.execute(
            '''SELECT task_id FROM task_owners WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?''',
            (user_id, limit),
        ).fetchall()
    return [row['task_id'] for row in rows]


def record_media(db_path, filename, user_id, task_id=''):
    with connect(db_path) as db:
        db.execute(
            '''INSERT INTO media_owners (filename, user_id, task_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(filename) DO NOTHING''',
            (filename, user_id, task_id, now_ts()),
        )


def media_owner(db_path, filename):
    with connect(db_path) as db:
        row = db.execute(
            'SELECT user_id FROM media_owners WHERE filename = ?', (filename,)
        ).fetchone()
    return row['user_id'] if row else None


def media_owner_map(db_path):
    with connect(db_path) as db:
        rows = db.execute('SELECT filename, user_id FROM media_owners').fetchall()
    return {row['filename']: row['user_id'] for row in rows}


def delete_media(db_path, filename):
    with connect(db_path) as db:
        db.execute('DELETE FROM media_owners WHERE filename = ?', (filename,))


class UserTokenStore:
    """youtube_auth 的令牌存储实现，按用户读写 google_tokens 表。

    与 youtube_auth.FileTokenStore 接口一致，供 Web 授权流程和后台
    worker 共用；失败原因写回记录，替代原来的全局 fail-lock 文件。
    """

    def __init__(self, db_path, user_id):
        self.db_path = db_path
        self.user_id = user_id

    def load(self):
        import json

        record = load_google_token(self.db_path, self.user_id)
        if not record:
            return None
        try:
            token = json.loads(record['token_json'])
        except ValueError:
            return None
        return token if isinstance(token, dict) else None

    def save(self, token):
        import json

        scopes = ' '.join(token.get('scopes') or []) if isinstance(token, dict) else ''
        save_google_token(
            self.db_path,
            self.user_id,
            json.dumps(token, ensure_ascii=False),
            scopes=scopes,
        )

    def failed(self):
        record = load_google_token(self.db_path, self.user_id)
        return bool(record and record['fail_reason'])

    def mark_failed(self, reason=''):
        set_google_fail_reason(self.db_path, self.user_id, reason)

    def clear_failure(self):
        set_google_fail_reason(self.db_path, self.user_id, '')
