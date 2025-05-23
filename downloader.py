#!venv/bin/python
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
from bark_util import bark_notify
from config_util import load_config
from log_util import setup_logger

# 加载配置
config = load_config()

# 创建必要的目录
for folder in [config["LOG_DIR"], config["URLS_DIR"], config["TMP_DIR"], config["FILES_DIR"]]:
    os.makedirs(folder, exist_ok=True)

# 配置日志
logger = setup_logger(
    name='downloader',
    log_dir=config["LOG_DIR"],
    log_file='downloader.log',
    max_bytes=config["MAX_LOG_SIZE"],
    backup_count=config["BACKUP_COUNT"],
    timezone=config.get("TIMEZONE", "UTC")
)

class DownloadHandler(FileSystemEventHandler):
    def __init__(self, executor):
        super().__init__()
        self.executor = executor

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.txt') and os.path.exists(event.src_path):
            logger.info(f"检测到新文件: {event.src_path}")
            self.executor.submit(self.process_file, event.src_path)

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.endswith('.txt') and os.path.exists(event.dest_path):
            logger.info(f"检测到文件重命名为txt: {event.src_path} -> {event.dest_path}")
            self.executor.submit(self.process_file, event.dest_path)

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
            # 下载前先重命名为.downloading
            downloading_path = filepath.rsplit('.', 1)[0] + '.downloading'
            try:
                os.rename(filepath, downloading_path)
                logger.info(f"任务开始，文件重命名为: {downloading_path}")
            except Exception as e:
                logger.error(f"重命名为.downloading失败: {e}")
                return
            # 根据首字母判断模式
            mode = 'audio' if base_name[0] == 'a' else 'video'
            result = self.download(url, base_name, mode)
            new_extension = '.ok' if result else '.fail'
            new_filepath = downloading_path.rsplit('.', 1)[0] + new_extension
            os.rename(downloading_path, new_filepath)
            logger.info(f"任务完成，文件重命名为: {new_filepath}")
            bark_notify(config['BARK_DEVICE_TOKEN'],
                        title="下载完成" if result else "下载失败",
                        content=f"{url} 下载{'完成' if result else '失败'}，文件: {os.path.basename(new_filepath)}")
        except Exception as e:
            logger.error(f"处理文件失败: {filepath}, 错误信息: {e}")
            bark_notify(config['BARK_DEVICE_TOKEN'],
                        title="下载失败",
                        content=f"{url} 下载失败，错误信息: {e}")

    def download(self, url, base_name, mode):
        logger.info(f"开始下载: {url} ({mode})")
        conf_file = 'yt-dlp.conf' if mode == 'video' else 'yta-dlp.conf'
        conf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), conf_file)

        #prefix = 'v' if mode == 'video' else 'a'
        task_tmp_dir = os.path.join(config["TMP_DIR"], f"{base_name}")
        log_basename = os.path.basename(task_tmp_dir)
        log_path = os.path.join(config["LOG_DIR"], f"{log_basename}.log")
        cmd = [
            'yt-dlp',
            '--config-location', conf_path,            
            '-o', os.path.join(task_tmp_dir, config["YT_DLP_OUTPUT_TEMPLATE"]),
            url
        ]

        try:
            os.makedirs(task_tmp_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"创建临时目录失败: {e}")
            return False

        try:
            with open(log_path, 'w', encoding='utf-8') as log_file:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                for line in process.stdout:
                    print(line, end='')
                    log_file.write(line)
                process.wait()
                if process.returncode != 0:
                    raise subprocess.CalledProcessError(process.returncode, cmd)
            self.move_files(task_tmp_dir)
            logger.info(f"下载完成: {url}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"下载失败: {url}，错误信息: {e}")
            # 下载失败时删除临时目录
            if os.path.exists(task_tmp_dir):
                try:
                    shutil.rmtree(task_tmp_dir)
                    logger.info(f"下载失败，已删除临时目录: {task_tmp_dir}")
                except Exception as e:
                    logger.error(f"下载失败，删除临时目录失败: {task_tmp_dir}, 错误信息: {e}")
            bark_notify(config['BARK_DEVICE_TOKEN'],
                        title="下载失败",
                        content=f"{url} 下载失败，错误信息: {e}")
            return False

    def move_files(self, tmp_dir):
        for filename in os.listdir(tmp_dir):
            src = os.path.join(tmp_dir, filename)
            dst = os.path.join(config["FILES_DIR"], filename)
            if not os.path.exists(src):
                logger.warning(f"源文件不存在，跳过处理: {src}")
                continue
            try:
                shutil.move(src, dst)
                logger.info(f"已移动文件: {src} -> {dst}")
            except Exception as e:
                logger.error(f"移动文件失败: {src}, 错误信息: {e}")
        if os.path.exists(tmp_dir):
            try:
                shutil.rmtree(tmp_dir)
                logger.info(f"已删除临时目录: {tmp_dir}")
            except Exception as e:
                logger.error(f"删除临时目录失败: {tmp_dir}, 错误信息: {e}")

def start_monitor(folder):
    executor = ThreadPoolExecutor(max_workers=config["MAX_WORKERS"])
    event_handler = DownloadHandler(executor)
    observer = Observer()
    observer.schedule(event_handler, folder, recursive=False)
    observer.start()
    logger.info(f"开始监控目录: {folder}")
    return observer

def main():
    observer = start_monitor(config["URLS_DIR"])
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == '__main__':
    main()
