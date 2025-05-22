import config_util
import datetime
import pytz

config = config_util.load_config()
timezone = config.get('TIMEZONE', 'Asia/Shanghai')

# 获取当前UTC时间
now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
# 转换为配置时区时间
try:
    tz = pytz.timezone(timezone)
    now_local = now_utc.astimezone(tz)
except Exception as e:
    print(f"时区转换失败: {e}")
    now_local = now_utc

print(f"配置时区: {timezone}")
print(f"当前UTC时间: {now_utc}")
print(f"当前本地时间: {now_local}") 