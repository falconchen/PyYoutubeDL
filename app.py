from flask import Flask, request, render_template, redirect, url_for, send_from_directory
import os
import time
import json
from urllib.parse import unquote


app = Flask(__name__, static_url_path='/static', static_folder='static')

# 加载配置
def load_config():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(BASE_DIR, 'config.json')
    default_config = {
        "VIDEO_DIR": "./urls/video",
        "AUDIO_DIR": "./urls/audio",
        "FILES_DIR": "./files"
    }
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                user_config = json.load(f)
            default_config.update(user_config)
        except Exception as e:
            print(f"加载配置失败，使用默认配置: {e}")

    for key in ["VIDEO_DIR", "AUDIO_DIR", "FILES_DIR"]:
        default_config[key] = os.path.abspath(os.path.join(BASE_DIR, default_config[key]))

    return default_config

config = load_config()

VIDEO_DIR = config["VIDEO_DIR"]
AUDIO_DIR = config["AUDIO_DIR"]
FILES_DIR = config["FILES_DIR"]

# 保证文件夹存在
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        types = request.form.getlist('type')

        timestamp = time.strftime('%Y%m%d%H%M%S') + f'{int(time.time() * 1000) % 1000:03d}'
        for t in types:
            folder = VIDEO_DIR if t == 'video' else AUDIO_DIR
            filename = os.path.join(folder, f"{timestamp}.txt")
            with open(filename, 'w') as f:
                f.write(url)
        return redirect(url_for('index'))

    return render_template('index.html')

@app.route('/player')
def player():
    # 获取 files 目录下的所有 mp4 文件
    video_files = [f for f in os.listdir(FILES_DIR) if f.endswith('.mp4')]

    # 确保 video_files 是一个有效的列表
    if video_files is None:
        video_files = []
    
    # 根据文件的最后修改时间进行降序排序（从晚到早）
    video_files.sort(key=lambda f: os.path.getmtime(os.path.join(FILES_DIR, f)), reverse=True)

    return render_template('player.html', video_files=video_files)
    
@app.route('/files/<path:filename>')
def serve_file(filename):
    decoded_filename = unquote(filename)  
    return send_from_directory(FILES_DIR, decoded_filename)

if __name__ == "__main__":
    app.run(debug=True)
