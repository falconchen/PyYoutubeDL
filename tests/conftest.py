"""全局测试夹具。

app.py 与 downloader.py 在导入时读取本机 config.json，测试结果不应随开发机
配置变化：localhost 重定向和视频片尾二维码都在这里统一关闭，需要验证它们的
用例在测试内显式打开。片尾尤其要关——它会对测试用的假媒体文件真的调用
ffprobe，而且有些用例 mock 了 subprocess，连带影响到它。
"""
from unittest.mock import patch

import pytest

import app as app_module
import downloader as downloader_module


@pytest.fixture(autouse=True)
def disable_localhost_redirect():
    with patch.dict(app_module.config, {'REDIRECT_LOCALHOST_TO_LOOPBACK': False}):
        yield


@pytest.fixture(autouse=True)
def disable_video_qr_tail():
    with patch.dict(
        downloader_module.config, {'VIDEO_QR_TAIL': {'ENABLED': False}}
    ):
        yield
