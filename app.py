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
import requests
from requests.auth import HTTPBasicAuth
from log_util import setup_logger
import click
from flask.cli import with_appcontext

app = Flask(__name__, static_url_path='/static', static_folder='static')

# 加载配置
config = load_config()

# 配置日志
logger = setup_logger(
    name='app',
    log_dir=config["LOG_DIR"],
    log_file='app.log',
    max_bytes=config["MAX_LOG_SIZE"],
    backup_count=config["BACKUP_COUNT"],
    timezone=config.get("TIMEZONE", "UTC")
)

# 将logger赋值给app.logger
app.logger = logger

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

def get_current_time():
    timezone = pytz.timezone(config["TIMEZONE"])
    return datetime.now(timezone)

def create_tasks(url, types):
    """创建下载任务并返回任务ID列表
    
    Args:
        url (str): 要下载的URL
        types (list): 下载类型列表，可以是 ['video'] 或 ['audio'] 或两者都有
        
    Returns:
        list: 创建的任务ID列表
    """
    task_ids = []
    current_time = get_current_time()
    for t in types:
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
        # 检查是否存在.local.conf文件
        script_dir = os.path.dirname(os.path.abspath(__file__))
        default_conf_file = 'yt-dlp.conf'
        local_conf_file = default_conf_file.replace('.conf', '.local.conf')
        local_conf_path = os.path.join(script_dir, local_conf_file)
        default_conf_path = os.path.join(script_dir, default_conf_file)
        
        # 优先使用.local.conf文件，如果不存在则使用默认配置文件
        conf_path = local_conf_path if os.path.exists(local_conf_path) else default_conf_path
        
        # 配置yt-dlp选项
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,  # 不下载视频，只获取信息
            'config_locations': [conf_path],  # 使用检测到的配置文件路径            
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

def get_youtube_cookie():
    """从API获取YouTube cookie并保存到文件"""
    try:
        ytc_config = config.get('YTC', {})
        api_url = ytc_config.get('API_URL')
        auth_username = ytc_config.get('AUTH_USERNAME')
        auth_password = ytc_config.get('AUTH_PASSWORD')
        cookie_file = ytc_config.get('COOKIE_FILE')

        if not all([api_url, auth_username, auth_password, cookie_file]):
            app.logger.error("YTC配置不完整，请检查config.json")
            return False

        # 确保cookie文件所在目录存在
        cookie_dir = os.path.dirname(cookie_file)
        if cookie_dir:
            os.makedirs(cookie_dir, exist_ok=True)

        # 使用Basic认证获取cookie
        response = requests.get(
            api_url,
            auth=HTTPBasicAuth(auth_username, auth_password),
            timeout=10
        )
        
        if response.status_code == 200:
            # 写入cookie文件
            with open(cookie_file, 'w') as f:
                f.write(response.text)
            app.logger.info(f"成功更新YouTube cookie文件: {cookie_file}")
            return True
        else:
            app.logger.error(f"获取cookie失败，状态码: {response.status_code}")
            return False
            
    except Exception as e:
        app.logger.error(f"获取YouTube cookie时发生错误: {str(e)}")
        return False

@app.cli.command("get-cookie")
@with_appcontext
def get_cookie_command():
    """获取 YouTube cookie 的命令行工具"""
    try:
        if get_youtube_cookie():
            click.echo("成功获取并更新 YouTube cookie")
        else:
            click.echo("获取 YouTube cookie 失败，请检查日志获取详细信息", err=True)
    except Exception as e:
        click.echo(f"执行过程中发生错误: {str(e)}", err=True)

@app.route('/api/get-cookie', methods=['GET', 'POST'])
def api_get_cookie():
    current_time = get_current_time()
    time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
    success = get_youtube_cookie()
    if success:
        return app.response_class(
            response=json.dumps({"success": True, "msg": "成功获取并更新 YouTube cookie", "time": time_str}, ensure_ascii=False),
            status=200,
            mimetype='application/json'
        )
    else:
        return app.response_class(
            response=json.dumps({"success": False, "msg": "获取 YouTube cookie 失败，请检查日志获取详细信息", "time": time_str}, ensure_ascii=False),
            status=500,
            mimetype='application/json'
        )

if __name__ == "__main__":
    # 启动时获取cookie
    get_youtube_cookie()
    app.run(host='0.0.0.0', debug=True)

