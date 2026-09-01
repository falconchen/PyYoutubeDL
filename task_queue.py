#!venv/bin/python
"""下载任务队列写入，供 Web 与后台 worker 共用。

任务文件命名契约（与 downloader.py 对齐）：
- 文件名 `<v|a><YYYYmmddHHMMSS><3 随机字母>.txt`
- 首字母 `v` = 视频，`a` = 音频（downloader.py 据此判断下载模式）
- 文件内容 = 待下载 URL
"""

import os
import random
import string
from datetime import datetime

import pytz

_ALLOWED_TYPES = {"video", "audio"}


def _random_str(length=3):
    return "".join(random.choices(string.ascii_letters, k=length))


def create_tasks(urls, types, urls_dir, timezone_name):
    """为每个 URL 按每种类型创建下载任务，返回任务 ID 列表。

    Args:
        urls (list): 待下载 URL 列表。
        types (list): 下载类型，元素为 'video' 或 'audio'。
        urls_dir (str): 任务目录（绝对路径）。
        timezone_name (str): 生成时间戳用的时区名称。

    Returns:
        list: 创建的任务 ID 列表。
    """
    task_ids = []
    os.makedirs(urls_dir, exist_ok=True)

    timezone = pytz.timezone(timezone_name)
    current_time = datetime.now(timezone)

    for url in urls:
        for t in types:
            if t not in _ALLOWED_TYPES:
                continue
            prefix = "v" if t == "video" else "a"
            # 同一批任务保证 task_id 唯一，避免覆盖已有任务文件
            while True:
                timestamp = current_time.strftime("%Y%m%d%H%M%S") + _random_str()
                task_id = f"{prefix}{timestamp}"
                filename = os.path.join(urls_dir, f"{task_id}.txt")
                if not os.path.exists(filename):
                    break
            task_ids.append(task_id)
            # 原子写入，避免下载器 watchdog 读到半截文件
            tmp_filename = filename + ".tmp"
            with open(tmp_filename, "w", encoding="utf-8") as f:
                f.write(url)
            os.replace(tmp_filename, filename)

    return task_ids
