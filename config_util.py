import os
import json

# 默认配置
DEFAULT_CONFIG = {
    # 下载器配置
    "URLS_DIR": "./urls",
    "TMP_DIR": "./tmp",
    "FILES_DIR": "./files",
    "LOG_DIR": "../logs",
    "MAX_WORKERS": 4,
    "MAX_LOG_SIZE": 10 * 1024 * 1024,
    "BACKUP_COUNT": 5,
    "YT_DLP_OUTPUT_TEMPLATE": "%(title.0:20)s-%(id)s.%(ext)s",
    "BARK_DEVICE_TOKEN": "",
    
    # WebDAV上传器配置
    "WEBDAV_OPTIONS": {},
    "UPLOAD_MAX_RETRIES": 3,
    "UPLOAD_RETRY_DELAY": 60,
    "DELETE_AFTER_UPLOAD": True,
    
    # 通用配置
    "TIMEZONE": "Asia/Shanghai"
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
