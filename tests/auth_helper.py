"""多用户测试的公共夹具：临时用户库 + 已登录的测试客户端。

app.py 在导入时就绑定了 USER_DB_PATH，测试里用 patch 覆盖该常量并指向
临时数据库，避免污染开发机上的 data/users.sqlite3。
"""
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import app as app_module
import user_store


@contextmanager
def logged_in_client(role=user_store.ROLE_ADMIN, extra_users=0):
    """提供一个已登录的测试客户端。

    Yields:
        (client, user, db_path)：Flask 测试客户端、当前登录用户、用户库路径。
    """
    with tempfile.TemporaryDirectory() as directory:
        db_path = str(Path(directory, 'users.sqlite3'))
        user_store.init_db(db_path)

        with ExitStack() as stack:
            stack.enter_context(patch.object(app_module, 'USER_DB_PATH', db_path))
            # 首个账号即管理员；需要普通用户时再建一个并放行。
            user = user_store.create_user(
                db_path, 'owner@example.com', password='password123'
            )
            if role != user_store.ROLE_ADMIN:
                user = user_store.create_user(
                    db_path, 'member@example.com', password='password123'
                )
                user_store.set_status(db_path, user['id'], user_store.STATUS_ACTIVE)
                user = user_store.get_user(db_path, user['id'])

            for index in range(extra_users):
                other = user_store.create_user(
                    db_path, f'other{index}@example.com', password='password123'
                )
                user_store.set_status(
                    db_path, other['id'], user_store.STATUS_ACTIVE
                )

            app_module.app.testing = True
            client = app_module.app.test_client()
            with client.session_transaction() as session:
                session[app_module.SESSION_USER_KEY] = user['id']

            yield client, user, db_path


def other_user(db_path, email='second@example.com'):
    """创建另一个可用账号，用于验证跨用户访问被拒绝。"""
    user = user_store.create_user(db_path, email, password='password123')
    user_store.set_status(db_path, user['id'], user_store.STATUS_ACTIVE)
    return user_store.get_user(db_path, user['id'])


def install_auth(testcase, client=None, email='owner@example.com'):
    """给 unittest 用例装上临时用户库与已登录会话。

    必须在 setUp 创建好 self.client 之后调用。会自动注册 cleanup，
    测试结束时恢复 app.USER_DB_PATH。

    Returns:
        (user, db_path)
    """
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    db_path = str(Path(directory.name, 'users.sqlite3'))
    user_store.init_db(db_path)

    patcher = patch.object(app_module, 'USER_DB_PATH', db_path)
    patcher.start()
    testcase.addCleanup(patcher.stop)

    user = user_store.create_user(db_path, email, password='password123')
    client = client or testcase.client
    with client.session_transaction() as session:
        session[app_module.SESSION_USER_KEY] = user['id']

    testcase.user_db_path = db_path
    testcase.logged_in_user = user
    return user, db_path
