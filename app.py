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
import yt_dlp
import pytz
from datetime import datetime

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

def create_tasks(url, types):
    """创建下载任务并返回任务ID列表
    
    Args:
        url (str): 要下载的URL
        types (list): 下载类型列表，可以是 ['video'] 或 ['audio'] 或两者都有
        
    Returns:
        list: 创建的任务ID列表
    """
    task_ids = []
    # 获取配置的时区
    timezone = pytz.timezone(config["TIMEZONE"])
    # 获取当前时间并转换为指定时区
    current_time = datetime.now(timezone)
    
    for t in types:
        # 使用时区时间格式化时间戳
        timestamp = current_time.strftime('%Y%m%d%H%M%S') + random_str(3)
        prefix = 'v' if t == 'video' else 'a'
        task_id = f"{prefix}{timestamp}"
        task_ids.append(task_id)
        
        filename = os.path.join(URLS_DIR, f"{task_id}.txt")
        with open(filename, 'w') as f:
            f.write(url)
    
    return task_ids

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        types = request.form.getlist('type')
        
        # 使用新的辅助函数创建任务
        task_ids = create_tasks(url, types)
        
        # 构建重定向URL，包含所有参数
        redirect_url = url_for('index', 
                             url=url,
                             types=','.join(types),
                             tasks=','.join(task_ids))
        
        return redirect(redirect_url)

    # GET请求处理
    url = request.args.get('url', '')
    types = request.args.get('types', '').split(',') if request.args.get('types') else []
    tasks = request.args.get('tasks', '').split(',') if request.args.get('tasks') else []
    
    return render_template('index.html', 
                         url=url,
                         types=types,
                         tasks=tasks)

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

    # 使用新的辅助函数创建任务
    tasks = create_tasks(url, types)
    
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

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.static_folder, 'images'),
                             'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/api/video_info', methods=['POST'])
def api_video_info():
    data = request.get_json() if request.is_json else request.form
    url = data.get('url')
    
    if not url:
        return jsonify({"success": False, "msg": "Missing required parameter: url"}), 400
    
    try:
        # 配置yt-dlp选项
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,  # 不下载视频，只获取信息
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 获取视频信息
            info = ydl.extract_info(url, download=False)
            
            # 提取需要的信息
            video_info = {
                "success": True,
                "title": info.get('title'),
                "description": info.get('description'),
                "duration": info.get('duration'),
                "uploader": info.get('uploader'),
                "upload_date": info.get('upload_date'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count'),
                "thumbnail": info.get('thumbnail'),
                "formats": [{
                    "format_id": f.get('format_id'),
                    "ext": f.get('ext'),
                    "resolution": f.get('resolution'),
                    "filesize": f.get('filesize'),
                    "format_note": f.get('format_note'),
                    "vcodec": f.get('vcodec'),
                    "acodec": f.get('acodec'),
                } for f in info.get('formats', []) if f.get('vcodec') != 'none'],
                "audio_formats": [{
                    "format_id": f.get('format_id'),
                    "ext": f.get('ext'),
                    "filesize": f.get('filesize'),
                    "acodec": f.get('acodec'),
                } for f in info.get('formats', []) if f.get('vcodec') == 'none'],
            }
            
            return jsonify(video_info)
            
    except Exception as e:
        return jsonify({
            "success": False,
            "msg": f"Failed to get video info: {str(e)}"
        }), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0')
