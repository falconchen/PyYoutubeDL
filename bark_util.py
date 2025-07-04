from BarkNotificator import BarkNotificator
import threading
from config_util import load_config

_bark_instance = None
_bark_lock = threading.Lock()
config = load_config()

def get_bark_instance(device_token):
    global _bark_instance
    with _bark_lock:
        if _bark_instance is None:
            _bark_instance = BarkNotificator(device_token=device_token)
    return _bark_instance

def bark_notify(device_token, title, content, icon_url=None):
    if icon_url is None:
        icon_url = config.get('BARK_ICON_URL', 'https://photo.cellmean.com/i/2025/05/22/jyxhwo-0.webp')
    bark = get_bark_instance(device_token)
    try:
        bark.send(title=title, content=content, icon_url=icon_url)
    except Exception as e:
        # 这里可以加日志
        print(f"Bark通知发送失败: {e}")
