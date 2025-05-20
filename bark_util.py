from BarkNotificator import BarkNotificator
import threading

_bark_instance = None
_bark_lock = threading.Lock()

def get_bark_instance(device_token):
    global _bark_instance
    with _bark_lock:
        if _bark_instance is None:
            _bark_instance = BarkNotificator(device_token=device_token)
    return _bark_instance

def bark_notify(device_token, title, content):
    bark = get_bark_instance(device_token)
    try:
        bark.send(title=title, content=content)
    except Exception as e:
        # 这里可以加日志
        print(f"Bark通知发送失败: {e}")
