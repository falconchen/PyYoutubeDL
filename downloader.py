import os
import sys
import time
import shutil
import subprocess
import json
import logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime
from webdav3.client import Client

# 加载配置
def load_config():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(BASE_DIR, 'config.json')

    default_config = {
        "URLS_DIR": "./urls",
        "VIDEO_DIR": "./urls/video",
        "AUDIO_DIR": "./urls/audio",
        "TMP_DIR": "./tmp",
        "FILES_DIR": "./files",
        "LOG_DIR": "../logs",
        "MAX_WORKERS": 4,
        "MAX_LOG_SIZE": 10 * 1024 * 1024,
        "BACKUP_COUNT": 5,
        "YT_DLP_OUTPUT_TEMPLATE": "%(title.0:20)s-%(id)s.%(ext)s",
        "WEBDAV_OPTIONS": {}
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                user_config = json.load(f)
            default_config.update(user_config)
        except Exception as e:
            print(f"加载配置文件失败，使用默认配置: {e}")

    for key in ["URLS_DIR", "VIDEO_DIR", "AUDIO_DIR", "TMP_DIR", "FILES_DIR", "LOG_DIR"]:
        default_config[key] = os.path.abspath(os.path.join(BASE_DIR, default_config[key]))

    return default_config

config = load_config()

for folder in [config["LOG_DIR"], config["URLS_DIR"], config["VIDEO_DIR"], config["AUDIO_DIR"], config["TMP_DIR"], config["FILES_DIR"]]:
    os.makedirs(folder, exist_ok=True)

logger = logging.getLogger('downloader')
logger.setLevel(logging.INFO)
log_file = os.path.join(config["LOG_DIR"], 'downloader.log')
handler = RotatingFileHandler(log_file, maxBytes=config["MAX_LOG_SIZE"], backupCount=config["BACKUP_COUNT"])
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

webdav = Client(config["WEBDAV_OPTIONS"])
try:
    if not webdav.check():
        logger.error("WebDAV连接失败")
        webdav = None
    else:
        logger.info("WebDAV连接成功")
except Exception as e:
    logger.error(f"WebDAV连接失败: {e}")
    webdav = None

class DownloadHandler(FileSystemEventHandler):
    def __init__(self, mode, executor):
        super().__init__()
        self.mode = mode
        self.executor = executor

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.txt'):
            logger.info(f"检测到新文件: {event.src_path}")
            self.executor.submit(self.process_file, event.src_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.txt'):
            logger.info(f"检测到文件修改(touch): {event.src_path}")
            self.executor.submit(self.process_file, event.src_path)

    def process_file(self, filepath):
        time.sleep(0.5)
        if not os.path.exists(filepath):
            logger.warning(f"文件已不存在: {filepath}")
            return
        try:
            with open(filepath, 'r') as f:
                url = f.read().strip()
                if not url:
                    logger.warning(f"文件内容为空: {filepath}")
                    return

            base_name = os.path.splitext(os.path.basename(filepath))[0]
            result = self.download(url, base_name)
            new_extension = '.ok' if result else '.fail'
            new_filepath = filepath.rsplit('.', 1)[0] + new_extension
            os.rename(filepath, new_filepath)
            logger.info(f"任务完成，文件重命名为: {new_filepath}")
        except Exception as e:
            logger.error(f"处理文件失败: {filepath}, 错误信息: {e}")

    def download(self, url, base_name):
        logger.info(f"开始下载: {url} ({self.mode})")
        conf_file = 'yt-dlp.conf' if self.mode == 'video' else 'yta-dlp.conf'
        conf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), conf_file)

        prefix = 'v' if self.mode == 'video' else 'a'
        task_tmp_dir = os.path.join(config["TMP_DIR"], f"{prefix}{base_name}")
        os.makedirs(task_tmp_dir, exist_ok=True)

        cmd = [
            'yt-dlp',
            '--config-location', conf_path,
            '-o', os.path.join(task_tmp_dir, config["YT_DLP_OUTPUT_TEMPLATE"]),
            url
        ]

        try:
            subprocess.run(cmd, check=True)
            self.move_and_upload(task_tmp_dir)
            logger.info(f"下载完成: {url}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"下载失败: {url}，错误信息: {e}")
            return False

    def move_and_upload(self, tmp_dir):
        for filename in os.listdir(tmp_dir):
            src = os.path.join(tmp_dir, filename)
            dst = os.path.join(config["FILES_DIR"], filename)
            try:
                shutil.move(src, dst)
                logger.info(f"已移动文件: {src} -> {dst}")
                self.upload_to_webdav(dst)
            except Exception as e:
                logger.error(f"移动文件失败: {src}, 错误信息: {e}")

    def upload_to_webdav(self, file_path):
        if not webdav:
            logger.warning("WebDAV未连接，跳过上传")
            return
        ext = os.path.splitext(file_path)[1].lower()
        category = 'Video' if ext in ['.mp4', '.mkv', '.webm', '.mov'] else 'Audio'
        today_str = datetime.now().strftime('%Y%m%d')
        remote_dir = f"/{category}/{today_str}"
        remote_path = f"{remote_dir}/{os.path.basename(file_path)}"

        try:
            if not webdav.check(remote_dir):
                webdav.mkdir(remote_dir)

            if webdav.check(remote_path):
                logger.info(f"WebDAV已存在相同文件，跳过上传: {remote_path}")
                return

            logger.info(f"开始上传: {file_path} -> {remote_path}")
            start_time = time.time()
            webdav.upload_sync(remote_path=remote_path, local_path=file_path)
            elapsed = time.time() - start_time
            logger.info(f"上传完成: {remote_path}，耗时: {elapsed:.2f} 秒")

            os.remove(file_path)
            logger.info(f"本地文件已删除: {file_path}")
        except Exception as e:
            logger.error(f"上传到WebDAV失败: {file_path}，错误: {e}")

def start_monitor(folder, mode):
    executor = ThreadPoolExecutor(max_workers=config["MAX_WORKERS"])
    event_handler = DownloadHandler(mode, executor)
    observer = Observer()
    observer.schedule(event_handler, folder, recursive=False)
    observer.start()
    logger.info(f"开始监控目录: {folder} (模式: {mode})")
    return observer

def main():
    video_observer = start_monitor(config["VIDEO_DIR"], 'video')
    audio_observer = start_monitor(config["AUDIO_DIR"], 'audio')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        video_observer.stop()
        audio_observer.stop()

    video_observer.join()
    audio_observer.join()

if __name__ == '__main__':
    main()
