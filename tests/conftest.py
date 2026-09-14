"""全局测试夹具。

app.py 在导入时读取本机 config.json。Flask 测试客户端默认主机名是
localhost，若本机开启了 REDIRECT_LOCALHOST_TO_LOOPBACK，所有请求都会被
重定向，测试结果随开发机配置变化。这里统一关闭，需要验证重定向的用例在
测试内显式打开。
"""
from unittest.mock import patch

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def disable_localhost_redirect():
    with patch.dict(app_module.config, {'REDIRECT_LOCALHOST_TO_LOOPBACK': False}):
        yield
