import os
import json

# 默认配置
DEFAULT_CONFIG = {
    # 下载器配置
    "URLS_DIR": "./urls",           # 存放待下载URL文件的目录
    "TMP_DIR": "./tmp",             # 下载时的临时目录
    "FILES_DIR": "./files",         # 下载完成后的文件存放目录
    "LOG_DIR": "../logs",           # 日志存放目录
    "MAX_WORKERS": 4,               # 最大并行下载数
    "MAX_LOG_SIZE": 10 * 1024 * 1024, # 单个日志文件最大字节数
    "BACKUP_COUNT": 5,              # 日志备份保留数量
    "YT_DLP_OUTPUT_TEMPLATE": "%(title.0:20)s-%(id)s.%(ext)s", # yt-dlp 文件名输出模板
    "BARK_DEVICE_TOKEN": "",        # Bark 通知推送 Token
    
    # WebDAV上传器配置
    "WEBDAV_OPTIONS": {},           # WebDAV 连接选项 (hostname, login, password 等)
    "UPLOAD_MAX_RETRIES": 3,        # 上传失败最大重试次数
    "UPLOAD_RETRY_DELAY": 60,       # 上传失败重试间隔（秒）
    "DELETE_AFTER_UPLOAD": True,    # 上传成功后是否删除本地文件
    "FILES_EXPIRE_DAYS": 1,         # 本地文件过期时间（天），超过此时间将被清理，0表示不清理
    
    # 通用配置
    "TIMEZONE": "Asia/Shanghai"     # 系统使用的时区
}

# 需要转换为绝对路径的配置项
PATH_CONFIG_KEYS = [
    "URLS_DIR",  "TMP_DIR", 
    "FILES_DIR", "LOG_DIR"
]

def load_config(default_config=None, config_keys=None):
    """
    加载配置文件
    
    Args:
        default_config: 默认配置，如果为None则使用DEFAULT_CONFIG
        config_keys: 需要转换为绝对路径的配置项，如果为None则使用PATH_CONFIG_KEYS
    
    Returns:
        配置字典
    """
    if default_config is None:
        default_config = DEFAULT_CONFIG.copy()
    if config_keys is None:
        config_keys = PATH_CONFIG_KEYS

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(BASE_DIR, 'config.json')

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                user_config = json.load(f)
            default_config.update(user_config)
        except Exception as e:
            print(f"加载配置文件失败，使用默认配置: {e}")

    for key in config_keys:
        if key in default_config:
            default_config[key] = os.path.abspath(os.path.join(BASE_DIR, default_config[key]))

    return default_config
