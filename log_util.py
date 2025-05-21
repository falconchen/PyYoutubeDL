import os
import sys
import logging
from logging.handlers import RotatingFileHandler
import pytz
from datetime import datetime

class TimezoneFormatter(logging.Formatter):
    """自定义格式化器，支持时区"""
    def __init__(self, fmt=None, datefmt=None, timezone=None):
        super().__init__(fmt, datefmt)
        self.timezone = pytz.timezone(timezone) if timezone else pytz.UTC

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        dt = self.timezone.localize(dt)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

def setup_logger(name, log_dir, log_file, max_bytes, backup_count, timezone="UTC"):
    """
    设置日志配置
    
    Args:
        name: 日志记录器名称
        log_dir: 日志目录
        log_file: 日志文件名
        max_bytes: 单个日志文件最大大小
        backup_count: 备份文件数量
        timezone: 时区设置，默认为UTC
    """
    # 确保日志目录存在
    os.makedirs(log_dir, exist_ok=True)
    
    # 创建日志记录器
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # 创建格式化器（使用时区）
    formatter = TimezoneFormatter(
        fmt='%(asctime)s [%(levelname)s] %(message)s',
        timezone=timezone
    )
    
    # 文件处理器
    log_path = os.path.join(log_dir, log_file)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger 