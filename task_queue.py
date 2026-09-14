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


# --- 任务控制：暂停、继续、重启、删除 -------------------------------------
#
# Web 与下载器是两个进程，唯一的共享通道是 URLS_DIR。未在下载的任务由
# Web 直接改名或删除状态文件；正在下载的任务（.downloading）由下载器负责，
# Web 写入 `<task_id>.control`（内容为动作名），下载器终止 yt-dlp 后执行。

PAUSED_EXTENSION = ".paused"
CONTROL_EXTENSION = ".control"

ACTION_PAUSE = "pause"
ACTION_RESTART = "restart"
ACTION_DELETE = "delete"
CONTROL_ACTIONS = {ACTION_PAUSE, ACTION_RESTART, ACTION_DELETE}

# 与 app.py 的状态识别顺序一致：同一时刻只应存在其中一个。
TASK_STATE_FILES = (
    (".ok", "completed"),
    (".fail", "failed"),
    (".downloading", "downloading"),
    (PAUSED_EXTENSION, "paused"),
    (".txt", "queued"),
)


def find_task_file(urls_dir, task_id):
    """返回 ``(状态文件路径, 状态)``；任务不存在时返回 ``(None, None)``。"""
    for extension, state in TASK_STATE_FILES:
        path = os.path.join(urls_dir, f"{task_id}{extension}")
        if os.path.isfile(path):
            return path, state
    return None, None


def write_task_control(urls_dir, task_id, action):
    """原子写入控制指令，避免下载器读到半截内容。"""
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"未知任务动作: {action}")
    path = os.path.join(urls_dir, f"{task_id}{CONTROL_EXTENSION}")
    # 临时文件后缀不能是 .txt 或 .control，否则会被下载器当作任务或指令拾取
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(action)
    os.replace(tmp_path, path)
    return path


def consume_task_control(control_path):
    """读取并删除控制指令文件，返回动作名；无效或已被处理时返回 None。"""
    try:
        with open(control_path, "r", encoding="utf-8") as f:
            action = f.read().strip()
    except OSError:
        return None
    try:
        os.remove(control_path)
    except FileNotFoundError:
        # 另一线程已处理过同一指令
        return None
    except OSError:
        pass
    return action if action in CONTROL_ACTIONS else None


def task_result_files(urls_dir, task_id):
    """读取 `<task_id>.result.json` 中登记的产物文件名（只保留纯文件名）。"""
    import json

    path = os.path.join(urls_dir, f"{task_id}.result.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    files = data.get("files", []) if isinstance(data, dict) else []
    return [
        name for name in files
        if isinstance(name, str) and name and name == os.path.basename(name)
    ]


def discard_task_temp(tmp_root, task_id):
    """删除任务临时目录（含断点续传用的 .part 文件）。"""
    import shutil

    path = os.path.join(tmp_root, task_id)
    # 任务 ID 已由调用方校验，这里仍防御性地确认目录在 tmp_root 之内
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(tmp_root):
        return
    shutil.rmtree(path, ignore_errors=True)


def remove_task_records(urls_dir, log_dir, task_id):
    """删除任务的状态文件、结果清单、未处理的控制指令与任务日志。"""
    names = [f"{task_id}{extension}" for extension, _ in TASK_STATE_FILES]
    names += [f"{task_id}.result.json", f"{task_id}{CONTROL_EXTENSION}"]
    for name in names:
        try:
            os.remove(os.path.join(urls_dir, name))
        except FileNotFoundError:
            pass
    try:
        os.remove(os.path.join(log_dir, f"{task_id}.log"))
    except FileNotFoundError:
        pass
