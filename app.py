#!venv/bin/python
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, jsonify
import os
import time
import json
from urllib.parse import unquote
import hashlib
from config_util import load_config
import random
import string

app = Flask(__name__, static_url_path='/static', static_folder='static')

# 加载配置
config = load_config()

URLS_DIR = config["URLS_DIR"]
FILES_DIR = config["FILES_DIR"]

# 保证文件夹存在
os.makedirs(URLS_DIR, exist_ok=True)
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

def random_str(length=3):
    return ''.join(random.choices(string.ascii_letters, k=length))

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        types = request.form.getlist('type')

        for t in types:
            timestamp = time.strftime('%Y%m%d%H%M%S') + random_str(3)
            prefix = 'v' if t == 'video' else 'a'
            filename = os.path.join(URLS_DIR, f"{prefix}{timestamp}.txt")
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
        timestamp = time.strftime('%Y%m%d%H%M%S') + random_str(3)
        prefix = 'v' if t == 'video' else 'a'
        filename = os.path.join(URLS_DIR, f"{prefix}{timestamp}.txt")
        with open(filename, 'w') as f:
            f.write(url)
        # 只返回不带后缀的文件名
        tasks.append(f"{prefix}{timestamp}")
    msg = "Task added successfully" if len(tasks) == 1 else "Tasks added successfully"
    return jsonify({"success": True, "msg": msg, "tasks": tasks})

@app.route('/api/task_info', methods=['POST'])
def api_task_info():
    data = request.get_json() if request.is_json else request.form
    tasks = data.get('tasks')
    if not tasks:
        return jsonify({"success": False, "msg": "Missing required parameter: tasks"}), 400
    if not isinstance(tasks, list):
        tasks = [tasks]
    result = []
    for task in tasks:
        # 直接用task作为文件名（不带后缀）
        try:
            if not task or len(task) < 2:
                result.append({"task": task, "exists": False, "msg": "Invalid task id"})
                continue
            filename = os.path.join(URLS_DIR, f"{task}.txt")
            if not os.path.exists(filename):
                result.append({"task": task, "exists": False, "msg": "Task file not found"})
                continue
            with open(filename, 'r') as f:
                url = f.read().strip()
            # 解析类型和时间
            t = 'video' if task[0] == 'v' else 'audio'
            ts = task[1:]
            time_str = ts[:14]  # 20240601123456
            try:
                task_time = time.strptime(time_str, '%Y%m%d%H%M%S')
                time_fmt = time.strftime('%Y-%m-%d %H:%M:%S', task_time)
            except Exception:
                time_fmt = time_str
            result.append({
                "task": task,
                "exists": True,
                "type": t,
                "timestamp": ts,
                "time": time_fmt,
                "url": url
            })
        except Exception as e:
            result.append({"task": task, "exists": False, "msg": f"Parse error: {e}"})
    return jsonify({"success": True, "tasks": result})


if __name__ == "__main__":
    app.run(host='0.0.0.0')
