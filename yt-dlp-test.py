import yt_dlp
import os

url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

def progress_hook(d):
    if d['status'] == 'downloading':
        print(f"下载进度: {d['_percent_str']} of {d['_total_bytes_str']} at {d['_speed_str']} ETA {d['_eta_str']}")
    elif d['status'] == 'finished':
        print('下载完成，正在处理...')

# 获取当前目录下的yt-dlp.conf路径
current_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(current_dir, 'yt-dlp.conf')

ydl_opts = {
    'progress_hooks': [progress_hook],
    'config_location': config_path,  # 指定配置文件路径
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([url])