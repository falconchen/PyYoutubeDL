#!/usr/bin/env python
import os
import sys
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime
from webdav3.client import Client
from bark_util import bark_notify
import threading
from config_util import load_config
from log_util import setup_logger
import requests
import pytz

# 加载配置
config = load_config()

# 创建必要的目录
for folder in [config["LOG_DIR"], config["FILES_DIR"]]:
    os.makedirs(folder, exist_ok=True)

# 配置日志
logger = setup_logger(
    name='webdav_uploader',
    log_dir=config["LOG_DIR"],
    log_file='downloader.log',
    max_bytes=config["MAX_LOG_SIZE"],
    backup_count=config["BACKUP_COUNT"],
    timezone=config.get("TIMEZONE", "UTC")
)

# 初始化WebDAV客户端（分别为视频和音频）
video_webdav = None
audio_webdav = None
video_webdav_host = None
audio_webdav_host = None

def get_webdav_methods(url, username, password):
    try:
        resp = requests.options(url, auth=(username, password), timeout=10)
        allow = resp.headers.get('Allow', '')
        return allow
    except Exception as e:
        logger.warning(f"OPTIONS 请求失败: {e}")
        return None

try:
    video_webdav = Client(config["VIDEO_WEBDAV_OPTIONS"])
    video_webdav_host = config["VIDEO_WEBDAV_OPTIONS"]["webdav_hostname"].split("//")[-1]
    if not video_webdav.check():
        logger.error("视频WebDAV连接失败")
        video_webdav = None
    else:
        logger.info(f"视频WebDAV连接成功，主机: {video_webdav_host}")
        allow_methods = get_webdav_methods(
            config["VIDEO_WEBDAV_OPTIONS"]["webdav_hostname"],
            config["VIDEO_WEBDAV_OPTIONS"]["webdav_login"],
            config["VIDEO_WEBDAV_OPTIONS"]["webdav_password"]
        )
        if allow_methods:
            logger.info(f"视频WebDAV服务器支持的方法: {allow_methods}")
        else:
            logger.warning("无法获取视频服务器支持的方法")
except Exception as e:
    logger.error(f"视频WebDAV连接失败: {e}")
    video_webdav = None

try:
    audio_webdav = Client(config["AUDIO_WEBDAV_OPTIONS"])
    audio_webdav_host = config["AUDIO_WEBDAV_OPTIONS"]["webdav_hostname"].split("//")[-1]
    if not audio_webdav.check():
        logger.error("音频WebDAV连接失败")
        audio_webdav = None
    else:
        logger.info(f"音频WebDAV连接成功，主机: {audio_webdav_host}")
        allow_methods = get_webdav_methods(
            config["AUDIO_WEBDAV_OPTIONS"]["webdav_hostname"],
            config["AUDIO_WEBDAV_OPTIONS"]["webdav_login"],
            config["AUDIO_WEBDAV_OPTIONS"]["webdav_password"]
        )
        if allow_methods:
            logger.info(f"音频WebDAV服务器支持的方法: {allow_methods}")
        else:
            logger.warning("无法获取音频服务器支持的方法")
except Exception as e:
    logger.error(f"音频WebDAV连接失败: {e}")
    audio_webdav = None

# 读取最大重试次数
UPLOAD_MAX_RETRIES = config.get("UPLOAD_MAX_RETRIES", 3)
UPLOAD_RETRY_DELAY = config.get("UPLOAD_RETRY_DELAY", 60)

# 记录每个文件的重试次数
retry_count = {}
retry_lock = threading.Lock()

class WebDAVUploadHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()

    def on_created(self, event):
        if not event.is_directory and os.path.exists(event.src_path):
            logger.info(f"检测到新文件: {event.src_path}")
            self.process_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and os.path.exists(event.src_path):
            logger.info(f"检测到文件修改: {event.src_path}")
            self.process_file(event.src_path)

    def on_moved(self, event):
        if not event.is_directory and os.path.exists(event.dest_path):
            logger.info(f"检测到文件重命名: {event.src_path} -> {event.dest_path}")
            self.process_file(event.dest_path)

    def process_file(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.mp4', '.mkv', '.webm', '.mov']:
            category = 'Video'
            webdav_client = video_webdav
            webdav_host = video_webdav_host
        elif ext == '.mp3':
            category = 'Audio'
            webdav_client = audio_webdav
            webdav_host = audio_webdav_host
        else:
            logger.info(f"文件类型不支持，跳过上传,并删除文件: {file_path}")
            os.remove(file_path)
            return

        if not webdav_client:
            logger.warning(f"{category} WebDAV未连接，跳过上传 (host: {webdav_host})")
            return

        timezone_str = config.get('TIMEZONE', 'UTC')
        tz = pytz.timezone(timezone_str)
        today_str = datetime.now(tz).strftime('%Y%m%d')
        remote_dir = f"/{category}/{today_str}"
        remote_path = f"{remote_dir}/{os.path.basename(file_path)}"

        try:
            if not webdav_client.check(remote_dir):
                webdav_client.mkdir(remote_dir)

            if webdav_client.check(remote_path):
                logger.info(f"WebDAV已存在相同文件，跳过上传，并删除文件: {remote_path} | 类型: {category} | 服务器: {webdav_host}")
                os.remove(file_path)
                return

            # 上传前检查本地文件是否存在
            if not os.path.exists(file_path):
                logger.warning(f"文件不存在，跳过上传: {file_path} | 类型: {category} | 服务器: {webdav_host}")
                with retry_lock:
                    retry_count.pop(file_path, None)
                return

            file_size = os.path.getsize(file_path)
            file_size_mb = file_size / (1024 * 1024)
            logger.info(f"开始上传: {file_path} -> {remote_path}，文件大小: {file_size_mb:.2f} MB | 类型: {category} | 服务器: {webdav_host}")

            start_time = time.time()
            try:
                # 尝试使用不同的上传方法
                webdav_client.upload_sync(remote_path=remote_path, local_path=file_path)
            except Exception as upload_error:
                logger.error(f"标准上传方法失败，尝试备用方法: {upload_error} | 类型: {category} | 服务器: {webdav_host}")
                try:
                    # 尝试使用PUT方法直接上传
                    with open(file_path, 'rb') as f:
                        webdav_client.put(remote_path, f.read())
                except Exception as put_error:
                    raise Exception(f"所有上传方法都失败: {put_error}")

            elapsed = time.time() - start_time
            speed = file_size_mb / elapsed if elapsed > 0 else 0

            logger.info(f"上传完成: {remote_path}，耗时: {elapsed:.2f} 秒，平均速度: {speed:.2f} MB/s | 类型: {category} | 服务器: {webdav_host}")
            bark_notify(
                config['BARK_DEVICE_TOKEN'],
                title=f"上传完成{file_size_mb:.2f} MB [{category}] [{webdav_host}]",
                content=f"{remote_path}，耗时: {elapsed:.2f} 秒，平均速度: {speed:.2f} MB/s"
            )

            os.remove(file_path)
            with retry_lock:
                retry_count.pop(file_path, None)

        except Exception as e:
            logger.error(f"上传到WebDAV失败: {file_path}，错误: {e} | 类型: {category} | 服务器: {webdav_host}")
            with retry_lock:
                count = retry_count.get(file_path, 0) + 1
                if count < UPLOAD_MAX_RETRIES:
                    retry_count[file_path] = count
                    logger.info(f"将在{UPLOAD_RETRY_DELAY}秒后重试（第{count}次）: {file_path} | 类型: {category} | 服务器: {webdav_host}")
                    threading.Timer(UPLOAD_RETRY_DELAY, self.process_file, args=[file_path]).start()
                else:
                    logger.error(f"文件已达到最大重试次数({UPLOAD_MAX_RETRIES})，放弃上传: {file_path} | 类型: {category} | 服务器: {webdav_host}")
                    bark_notify(
                        config['BARK_DEVICE_TOKEN'],
                        title=f"上传失败 [{category}] [{webdav_host}]",
                        content=f"文件 {os.path.basename(file_path)} 上传失败，已达到最大重试次数"
                    )
                    retry_count.pop(file_path, None)

def main():
    event_handler = WebDAVUploadHandler()
    observer = Observer()
    observer.schedule(event_handler, config["FILES_DIR"], recursive=False)
    observer.start()
    logger.info(f"开始监控目录: {config['FILES_DIR']}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logger.info("监控已停止")

    observer.join()

if __name__ == '__main__':
    main() 
