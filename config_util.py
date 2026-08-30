import os
import json
from datetime import datetime

import pytz

MOVE_STAGING_PREFIX = '.pyyoutubedl-moving-'
DEFAULT_PLAYLIST_MAX_ITEMS = 20

# 默认配置
DEFAULT_CONFIG = {
    # 下载器配置
    "URLS_DIR": "./urls",           # 存放待下载URL文件的目录
    "TMP_DIR": "./tmp",             # 下载时的临时目录
    "FILES_DIR": "./files",         # 下载完成后的文件存放目录
    "LOG_DIR": "../logs",           # 日志存放目录
    "MAX_WORKERS": 4,               # 最大并行下载数
    "PLAYLIST_MAX_ITEMS": DEFAULT_PLAYLIST_MAX_ITEMS, # 单个播放列表只下载前 N 个条目
    "DOWNLOAD_MIN_INTERVAL_SECONDS": 10, # 两次下载启动的最小间隔（秒），0 表示不限速
    "RESUME_INTERRUPTED_DOWNLOADS": False, # 下载器启动时是否恢复遗留的 .downloading 任务
    "MAX_LOG_SIZE": 10 * 1024 * 1024, # 单个日志文件最大字节数
    "BACKUP_COUNT": 5,              # 日志备份保留数量
    "YT_DLP_OUTPUT_TEMPLATE": "%(title.0:20)s-%(id)s.%(ext)s", # yt-dlp 文件名输出模板
    "PLAYER_FILENAME_EXCLUDE_KEYWORDS": [], # 播放器列表排除的文件名关键词
    "AUDIO_PLAYER_FALLBACK_COVER_URL": "/static/images/audio-cover-default.svg", # 音频封面加载失败时的默认图
    "SHOW_WALINE_ON_INDEX": False,  # 是否在首页显示 Waline 评论
    "SHOW_WALINE_ON_PLAYER": False, # 是否在播放页显示 Waline 评论
    "AI_API_BASE_URL": "",          # AI 总结使用的 chat/completions 兼容接口
    "AI_API_MODEL": "",             # AI 总结模型名称
    "AI_API_TOKEN": "",             # AI 总结接口 Token
    "AI_SUMMARY_DB_PATH": "./data/ai_summaries.sqlite3", # AI 总结持久化数据库
    "AI_SUMMARY_ACCESS_TOKEN": "",  # Chrome 扩展调用 AI 总结接口的独立令牌
    "AI_SUMMARY_JOB_RETENTION_DAYS": 30, # 已完成/失败 AI 任务记录保留天数
    "BARK_DEVICE_TOKEN": "",        # Bark 通知推送 Token
    "EXTENSION_LOG_TOKEN": "",      # Chrome 扩展读取 downloader.log 的访问令牌；为空时禁用接口
    
    # WebDAV上传器配置
    "ENABLE_WEBDAV_UPLOAD": True,  # 是否将下载完成的文件上传到 WebDAV
    "WEBDAV_UPLOAD_EXCLUDE_KEYWORDS": [], # WebDAV 上传排除的文件名关键词
    "WEBDAV_OPTIONS": {},           # WebDAV 连接选项 (hostname, login, password 等)
    "UPLOAD_MAX_RETRIES": 1,        # 首次上传失败后的最大重试次数
    "UPLOAD_RETRY_DELAY": 60,       # 上传失败重试间隔（秒）
    "WEBDAV_RECONNECT_INTERVAL": 30, # WebDAV 启动连接失败后的重连间隔（秒）
    "DELETE_AFTER_UPLOAD": True,    # 上传成功后是否删除本地文件
    "FILES_EXPIRE_DAYS": 1,         # 本地文件过期时间（天），超过此时间将被清理，0表示不清理
    "VIDEO_WEBDAV_KEEP_COUNT": 3,   # 视频 WebDAV 保留的日期目录数量
    "AUDIO_WEBDAV_KEEP_COUNT": 5,   # 音频 WebDAV 保留的日期目录数量
    
    # 通用配置
    "TIMEZONE": "Asia/Shanghai",    # 系统使用的时区
    "FLASK_PORT": 5100,              # Flask Web 应用监听端口
}

# 需要转换为绝对路径的配置项
PATH_CONFIG_KEYS = [
    "URLS_DIR",  "TMP_DIR", 
    "FILES_DIR", "LOG_DIR", "AI_SUMMARY_DB_PATH"
]


def build_dated_output_template(output_template, timezone_name):
    """在 yt-dlp 原命名模板前添加下载开始时间（MMDDHHmm-）。"""
    timezone = pytz.timezone(timezone_name)
    prefix = datetime.now(timezone).strftime('%m%d%H%M')
    return f"{prefix}-{output_template}"


def is_webdav_upload_enabled(runtime_config):
    """返回是否启用 WebDAV 上传；未配置时默认启用。"""
    return bool(runtime_config.get("ENABLE_WEBDAV_UPLOAD", True))


def is_ai_summary_enabled(runtime_config):
    """AI 接口三项配置完整时启用持久化总结 worker。"""
    return all(
        isinstance(runtime_config.get(key), str)
        and runtime_config.get(key).strip()
        for key in ('AI_API_BASE_URL', 'AI_API_MODEL', 'AI_API_TOKEN')
    )


def get_playlist_max_items(runtime_config):
    """返回有效的播放列表下载条目数，无效配置回退为默认值。"""
    max_items = runtime_config.get(
        "PLAYLIST_MAX_ITEMS",
        DEFAULT_PLAYLIST_MAX_ITEMS,
    )
    if (
        not isinstance(max_items, int)
        or isinstance(max_items, bool)
        or max_items <= 0
    ):
        return DEFAULT_PLAYLIST_MAX_ITEMS
    return max_items


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
