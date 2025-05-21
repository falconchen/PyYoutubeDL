from flask import Flask, request, render_template, redirect, url_for, send_from_directory, jsonify
import os
import time
import json
from urllib.parse import unquote
import hashlib
from config_util import load_config

app = Flask(__name__, static_url_path='/static', static_folder='static')

# 加载配置
config = load_config()

VIDEO_DIR = config["VIDEO_DIR"]
AUDIO_DIR = config["AUDIO_DIR"]
FILES_DIR = config["FILES_DIR"]

# 保证文件夹存在
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

def get_file_hash(filepath):
    """获取文件的MD5哈希值"""
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()

@app.template_filter('versioned')
def versioned_static(filename):
    """生成带版本号的静态文件URL"""
    filepath = os.path.join(app.static_folder, filename)
    file_hash = get_file_hash(filepath)
    if file_hash:
        return f"{url_for('static', filename=filename)}?v={file_hash[:8]}"
    return url_for('static', filename=filename)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        types = request.form.getlist('type')

        
        for t in types:
            timestamp = time.strftime('%Y%m%d%H%M%S') + f'{int(time.time() * 1000) % 1000:03d}'
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

@app.route('/api/add_task', methods=['POST'])
def api_add_task():
    data = request.get_json() if request.is_json else request.form
    url = data.get('url')
    types = data.get('types')
    if not url or not types:
        return jsonify({"success": False, "msg": "Missing required parameters: url and types"}), 400
    if not isinstance(types, list):
        # 支持表单传递的字符串类型
        types = [types]

    
    tasks = []
    for t in types:
        timestamp = time.strftime('%Y%m%d%H%M%S') + f'{int(time.time() * 1000) % 1000:03d}'
        folder = VIDEO_DIR if t == 'video' else AUDIO_DIR
        filename = os.path.join(folder, f"{timestamp}.txt")
        with open(filename, 'w') as f:
            f.write(url)
        # 只返回相对路径
        tasks.append(f"{t}/{timestamp}")
    return jsonify({"success": True, "msg": "Task(s) added successfully", "tasks": tasks})

if __name__ == "__main__":
    app.run()
