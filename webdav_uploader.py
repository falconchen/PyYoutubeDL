#!/usr/bin/env python
import os
import time
import re
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
    """
    获取 WebDAV 服务器支持的所有 HTTP 方法。

    Args:
        url (str): 服务器 URL。
        username (str): 登录用户名。
        password (str): 登录密码。

    Returns:
        str: 服务器响应的 'Allow' 头信息，包含支持的方法；请求失败则返回 None。
    """
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
    if not video_webdav.list("/"):
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
    if not audio_webdav.list("/"):
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

    def sanitize_filename(self, filename):
        """
        清理文件名中的特殊字符，避免WebDAV上传问题
        """
        
        # 移除或替换可能导致问题的字符
        # 替换 % 为 emoji百分号
        filename = filename.replace('%', '﹪')
        
        # 替换其他可能导致问题的字符
        filename = filename.replace('\\', '_')
        filename = filename.replace('/', '_')
        filename = filename.replace(':', '_')
        filename = filename.replace('*', '_')
        filename = filename.replace('?', '_')
        filename = filename.replace('"', '_')
        filename = filename.replace('<', '_')
        filename = filename.replace('>', '_')
        filename = filename.replace('|', '_')
        
        # 移除控制字符
        filename = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', filename)
        
        # 确保文件名不为空
        if not filename.strip():
            filename = "unnamed_file"
            
        return filename

    def on_created(self, event):
        """
        当目录中创建了新文件时触发的处理函数。

        Args:
            event: 文件系统事件对象。
        """
        if not event.is_directory and os.path.exists(event.src_path):
            logger.info(f"检测到新文件: {event.src_path}")
            self.process_file(event.src_path)

    def on_modified(self, event):
        """
        当监控的文件被修改时触发的处理函数。

        Args:
            event: 文件系统事件对象。
        """
        if not event.is_directory and os.path.exists(event.src_path):
            logger.info(f"检测到文件修改: {event.src_path}")
            self.process_file(event.src_path)

    def on_moved(self, event):
        """
        当监控的文件被移动或重命名时触发的处理函数。

        Args:
            event: 文件系统加密事件对象。
        """
        if not event.is_directory and os.path.exists(event.dest_path):
            logger.info(f"检测到文件重命名: {event.src_path} -> {event.dest_path}")
            self.process_file(event.dest_path)

    def process_file(self, file_path):
        """
        处理文件上传流程：判断分类、连接WebDAV、执行上传、清理本地文件
        
        Args:
            file_path: 本地文件路径
        """
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

        remote_dir = f"/{today_str}"
        
        # 处理文件名中的特殊字符，避免WebDAV上传问题
        original_filename = os.path.basename(file_path)
        safe_filename = self.sanitize_filename(original_filename)
        remote_path = f"{remote_dir}/{safe_filename}"
        
        # 如果文件名被修改，记录日志
        if original_filename != safe_filename:
            logger.info(f"文件名已清理: '{original_filename}' -> '{safe_filename}'")

        try:
            if not webdav_client.check(remote_dir):
                webdav_client.mkdir(remote_dir)

            if webdav_client.check(remote_path):
                logger.info(f"WebDAV已存在相同文件，跳过上传: {remote_path} | 类型: {category} | 服务器: {webdav_host}")
                if config.get("DELETE_AFTER_UPLOAD", True):
                    os.remove(file_path)
                    logger.info(f"文件已在WebDAV端存在，已删除本地文件: {file_path}")
                else:
                    logger.info(f"文件已在WebDAV端存在，保留本地文件 (根据配置): {file_path}")
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

            if config.get("DELETE_AFTER_UPLOAD", True):
                os.remove(file_path)
                logger.info(f"上传成功，已删除本地文件: {file_path}")
            else:
                logger.info(f"上传成功，保留本地文件 (根据配置): {file_path}")

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

def cleanup_expired_files(directory, days):
    """
    扫描并清理超过指定天数的文件
    """
    if days <= 0:
        return
        
    logger.info(f"开始扫描并清理 {directory} 中创建时间超过 {days} 天的文件")
    now = time.time()
    cutoff_time = now - (days * 86400)
    
    try:
        count = 0
        for filename in os.listdir(directory):
            filepath = os.path.join(directory, filename)
            if os.path.isfile(filepath):
                stat_info = os.stat(filepath)
                # 为了兼容 macOS 和 Linux，优先使用 st_birthtime（创建时间），否则使用 st_mtime（修改时间）
                file_time = getattr(stat_info, 'st_birthtime', stat_info.st_mtime)
                
                if file_time < cutoff_time:
                    try:
                        os.remove(filepath)
                        logger.info(f"已清理过期文件: {filepath}")
                        count += 1
                    except Exception as e:
                        logger.error(f"清理文件失败 {filepath}: {e}")
        if count > 0:
            logger.info(f"清理完成，共删除了 {count} 个过期文件")
        else:
            logger.info("未发现需要清理的过期文件")
    except Exception as e:
        logger.error(f"扫描目录失败 {directory}: {e}")

def main():
    """
    程序主入口，初始化文件监控器并启动观察者。
    """
    # 启动前清理过期文件
    expire_days = config.get("FILES_EXPIRE_DAYS", 1)
    if expire_days > 0:
        cleanup_expired_files(config["FILES_DIR"], expire_days)

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
