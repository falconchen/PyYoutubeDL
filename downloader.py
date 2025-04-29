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

# 加载配置
def load_config():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(BASE_DIR, 'config.json')
    
    # 默认配置
    default_config = {
        "URLS_DIR": "./urls",
        "VIDEO_DIR": "./urls/video",
        "AUDIO_DIR": "./urls/audio",
        "TMP_DIR": "./tmp",
        "FILES_DIR": "./files",
        "LOG_DIR": "../logs",
        "MAX_WORKERS": 4,
        "MAX_LOG_SIZE": 10 * 1024 * 1024,  # 10MB
        "BACKUP_COUNT": 5
    }
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                user_config = json.load(f)
            default_config.update(user_config)
        except Exception as e:
            print(f"加载配置文件失败，使用默认配置: {e}")

    # 统一为绝对路径
    for key in ["URLS_DIR", "VIDEO_DIR", "AUDIO_DIR", "TMP_DIR", "FILES_DIR", "LOG_DIR"]:
        default_config[key] = os.path.abspath(os.path.join(BASE_DIR, default_config[key]))
    
    return default_config

# 加载配置
config = load_config()

# 确保目录存在
for folder in [config["LOG_DIR"], config["URLS_DIR"], config["VIDEO_DIR"], config["AUDIO_DIR"], config["TMP_DIR"], config["FILES_DIR"]]:
    os.makedirs(folder, exist_ok=True)

# 日志设置
logger = logging.getLogger('downloader')
logger.setLevel(logging.INFO)
log_file = os.path.join(config["LOG_DIR"], 'downloader.log')
handler = RotatingFileHandler(log_file, maxBytes=config["MAX_LOG_SIZE"], backupCount=config["BACKUP_COUNT"])
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

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
        time.sleep(0.5)  # 等待写入完成
        if not os.path.exists(filepath):
            logger.warning(f"文件已不存在: {filepath}")
            return
        try:
            with open(filepath, 'r') as f:
                url = f.read().strip()
                if not url:
                    logger.warning(f"文件内容为空: {filepath}")
                    return

            result = self.download(url)
            new_extension = '.ok' if result else '.fail'
            new_filepath = filepath.rsplit('.', 1)[0] + new_extension
            os.rename(filepath, new_filepath)
            logger.info(f"任务完成，文件重命名为: {new_filepath}")
        except Exception as e:
            logger.error(f"处理文件失败: {filepath}, 错误信息: {e}")

    def download(self, url):
        logger.info(f"开始下载: {url} ({self.mode})")
        conf_file = 'yt-dlp.conf' if self.mode == 'video' else 'yta-dlp.conf'
        conf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), conf_file)
        cmd = [
            'yt-dlp',
            '--config-location', conf_path,
            '-o', os.path.join(config["TMP_DIR"],'%(title.0:20)s-%(id)s.%(ext)s'),
            url
        ]

        try:
            subprocess.run(cmd, check=True)
            self.move_downloaded_files()
            logger.info(f"下载完成: {url}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"下载失败: {url}，错误信息: {e}")
            return False

    def move_downloaded_files(self):
        for filename in os.listdir(config["TMP_DIR"]):
            src = os.path.join(config["TMP_DIR"], filename)
            dst = os.path.join(config["FILES_DIR"], filename)
            try:
                shutil.move(src, dst)
                logger.info(f"已移动文件: {src} -> {dst}")
            except Exception as e:
                logger.error(f"移动文件失败: {src}, 错误信息: {e}")

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
