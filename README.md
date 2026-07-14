# PyYoutubeDL

基于 yt-dlp 的 YouTube 视频/音频下载服务，提供 Web 管理界面、自动下载、WebDAV 远程上传等功能。

## 架构

项目由三个独立进程组成：

- **app.py** — Flask Web 应用，提供管理界面和 API，用户通过网页提交下载链接
- **downloader.py** — 下载器，基于 watchdog 监控 `urls/` 目录，自动处理新任务并调用 yt-dlp 下载
- **webdav_uploader.py** — 上传器，基于 watchdog 监控 `files/` 目录，自动将完成文件上传至 WebDAV 远程存储

任务通过文件系统通信：Web 端写入 `.txt` 任务文件到 `urls/` 目录，下载器处理后重命名为 `.ok`/`.fail`，完成文件移动到 `files/` 目录后由上传器处理。

## 快速开始

### 1. 安装依赖

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

复制并编辑配置文件：

```bash
cp config.sample.json config.json
vim config.json
```

主要配置项参见下方[配置说明](#配置说明)。

### 3. 启动

```bash
./runner.sh
```

脚本会自动激活虚拟环境、更新依赖、停止旧进程，然后启动 Web 应用、下载器和上传器。

停止本项目通过 Python 启动的 Web 应用、下载器、上传器及其子进程：

```bash
python stop.py
```

停止脚本只匹配当前项目目录中的目标脚本，先正常终止进程，5 秒后仍未退出时再强制终止。`runner.sh` 会使用 `python stop.py --restart-devil`，在 Devil 环境中显式重启由 Devil 管理的 Web 应用；直接运行 `python stop.py` 不会重启 Devil。

### 4. 设为开机自启

项目提供了 systemd 服务安装脚本，动态获取当前目录路径：

```bash
sudo bash setup_pyyoutubedl_service.sh
```

服务安装后可使用的常用命令：

```bash
systemctl start  pyyoutubedl    # 启动
systemctl stop   pyyoutubedl    # 停止
systemctl status pyyoutubedl    # 查看状态
journalctl -u pyyoutubedl -f    # 实时日志
```

## 使用方式

### Web 界面

启动后访问 `http://<host>:5001`，通过网页提交 YouTube/小红书/Bilibili 等链接，选择视频或音频模式即可下载。

### API

```bash
# 添加下载任务
curl -X POST http://localhost:5001/api/add_task \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=xxx", "types": ["video"]}'

# 查询任务状态
curl -X POST http://localhost:5001/api/task_info \
  -H "Content-Type: application/json" \
  -d '{"tasks": ["v20250601120000abc"]}'
```

## 配置说明

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `URLS_DIR` | string | 任务文件存放目录，默认 `./urls` |
| `TMP_DIR` | string | 下载临时目录，默认 `./tmp` |
| `FILES_DIR` | string | 下载完成文件存放目录，默认 `./files` |
| `LOG_DIR` | string | 日志目录，默认 `./logs` |
| `MAX_WORKERS` | int | 下载线程池大小，默认 4 |
| `MAX_LOG_SIZE` | int | 单个日志文件最大字节数，默认 10MB |
| `BACKUP_COUNT` | int | 日志文件保留数量，默认 5 |
| `YT_DLP_OUTPUT_TEMPLATE` | string | 视频文件名模板 |
| `YTA_DLP_OUTPUT_TEMPLATE` | string | 音频文件名模板 |
| `DELETE_AFTER_UPLOAD` | bool | WebDAV 上传后是否删除本地文件 |
| `FILES_EXPIRE_DAYS` | int | 启动时清理超过 N 天的旧文件，0 表示不清理 |
| `VIDEO_WEBDAV_OPTIONS` | object | 视频 WebDAV 远程存储配置 |
| `AUDIO_WEBDAV_OPTIONS` | object | 音频 WebDAV 远程存储配置 |
| `BARK_DEVICE_TOKEN` | string | Bark 推送通知 Token |
| `TIMEZONE` | string | 时区，如 `Asia/Shanghai` |
| `FLASK_HOST` | string | Flask 监听地址，默认 `0.0.0.0` |
| `FLASK_DEBUG` | bool | Flask 调试模式 |
| `SCHEDULED_PLAYLISTS` | array | 定时下载的播放列表配置 |

## Cookies 配置

YouTube 部分视频需要登录才能下载。支持通过 yt-dlp 浏览器 cookies 提取，或配合 [ytc](https://github.com/falconchen/ytc) 服务自动刷新 cookies。

在 `config.json` 中配置 `YTC` 段：

```json
"YTC": {
  "API_URL": "http://localhost:5001/cookies/mozilla?format=text",
  "AUTH_USERNAME": "admin",
  "AUTH_PASSWORD": "your_password",
  "COOKIE_FILE": "/etc/youtube-cookie.txt"
}
```

手动更新 cookies：

```bash
./update_cookie.sh
```

## WebDAV 上传

支持将下载完成的文件自动上传到 WebDAV 远程存储，区分视频和音频不同目标路径，支持自动重试和保留最新 N 个文件。

## 文件结构

```
PyYoutubeDL/
├── app.py                # Flask Web 应用
├── downloader.py         # 下载器（watchdog + yt-dlp）
├── webdav_uploader.py    # WebDAV 上传器
├── runner.sh             # 启动脚本
├── stop.py               # 停止脚本
├── setup_pyyoutubedl_service.sh  # systemd 服务安装脚本
├── config.json           # 配置文件
├── config.sample.json    # 配置示例
├── config_util.py        # 配置加载工具
├── log_util.py           # 日志工具
├── bark_util.py          # Bark 通知工具
├── requirements.txt      # Python 依赖
├── urls/                 # 任务文件目录
├── tmp/                  # 临时下载目录
├── files/                # 完成文件目录
├── logs/                 # 日志目录
├── static/               # Web 静态资源
├── templates/            # Jinja2 模板
├── yt-dlp.conf           # yt-dlp 视频配置文件
├── yta-dlp.conf          # yt-dlp 音频配置文件
└── venv/                 # Python 虚拟环境
```
