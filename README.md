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
./runner.sh start    # 只启动服务
./runner.sh stop     # 只停止本项目的 Python 服务
./runner.sh restart  # 先停止再启动
./runner.sh          # 默认执行 restart
```

`start` 和 `restart` 会自动激活虚拟环境、更新 pip 与 yt-dlp，然后启动 Web 应用、下载器和上传器。`stop` 不更新依赖；在 Devil 环境中，单独执行 `stop` 不会重启 Devil 管理的 Web 应用，`restart` 则保持原有的 Devil 重启行为。

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

提交任务后，页面会每 2 秒查询一次任务状态，显示排队、下载、完成或失败状态，并在下载阶段显示百分比、已下载大小、总大小、速度和预计剩余时间。页面中的二进制容量单位会简化显示为 `G`、`M`、`K`。页面关闭或刷新不会影响后台下载；带有 `tasks` 查询参数的任务结果页可继续查看这些任务。

播放器会使用 `ffprobe` 识别 MP4 内嵌字幕，并在浏览器请求字幕时通过 `ffmpeg` 转换为 WebVTT，Video.js 控制栏会显示可用的字幕选项。该功能不修改原视频，但运行环境必须能够直接执行 `ffprobe` 和 `ffmpeg`；无法识别或转换字幕时，视频仍可正常播放，只是不显示字幕选项。

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

`/api/task_info` 会返回任务的 `state`（`queued`、`downloading`、`completed`、`failed` 或 `missing`）和 `progress`。下载中任务的 `progress` 包含可用的 `percent`、`downloaded`、`total`、`speed`、`eta` 等字段。

### 同名文件处理

下载产物移入 `FILES_DIR` 时不会覆盖已有文件。同名文件会自动追加编号：

```text
video.mp4
video (1).mp4
video (2).mp4
```

名称分配采用原子操作，多个下载任务并发完成时也不会互相覆盖。移动失败时任务会标记为失败，尚未移动的文件会保留在任务临时目录中，避免静默删除。

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
| `PLAYER_FILENAME_EXCLUDE_KEYWORDS` | array | 播放器列表排除的文件名关键词，任一非空关键词命中即隐藏，默认 `[]` |
| `DELETE_AFTER_UPLOAD` | bool | WebDAV 上传后是否删除本地文件 |
| `FILES_EXPIRE_DAYS` | int | 启动时清理超过 N 天的旧文件，0 表示不清理 |
| `VIDEO_WEBDAV_OPTIONS` | object | 视频 WebDAV 远程存储配置 |
| `AUDIO_WEBDAV_OPTIONS` | object | 音频 WebDAV 远程存储配置 |
| `BARK_DEVICE_TOKEN` | string | Bark 推送通知 Token |
| `TIMEZONE` | string | 时区，如 `Asia/Shanghai` |
| `FLASK_HOST` | string | Flask 监听地址，默认 `0.0.0.0` |
| `FLASK_DEBUG` | bool | Flask 调试模式 |
| `SCHEDULED_PLAYLISTS` | array | 定时下载的播放列表配置 |

播放器列表可按文件名关键词隐藏视频，例如：

```json
"PLAYER_FILENAME_EXCLUDE_KEYWORDS": ["预告", "preview", "sample"]
```

匹配区分大小写，文件名包含任一非空关键词时不会出现在 `/player` 页面；原文件不会被删除，也不影响下载和 WebDAV 上传。修改后需要重启 Web 应用以重新加载配置。

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

## 页面评论

首页 `templates/index.html` 和播放页 `templates/player.html` 已接入 Waline 评论，评论服务地址：

```txt
https://waline.v2ai.eu.cc
```

公开页面地址：

```txt
https://yter.cellmean.com/
https://yter.cellmean.com/player
```

评论区默认不显示。通过 `config.json` 分别控制首页和播放页：

```json
"SHOW_WALINE_ON_INDEX": false,
"SHOW_WALINE_ON_PLAYER": false
```

需要显示时把对应项改成 `true`，然后重启服务：

```bash
systemctl restart pyyoutubedl.service
```

评论数据按“域名 + 路径”隔离。客户端传给 Waline 的 `path` 形如：

```txt
yter.cellmean.com/
yter.cellmean.com/player
```

这样可以避免不同站点都使用 `/` 时共享同一组评论。

Waline 侧需要允许当前站点域名：

```dotenv
SECURE_DOMAINS=...,yter.cellmean.com
```

如果评论区返回 `403 Forbidden`，优先检查 Waline 部署目录 `/srv/docker/waline/.env` 中的 `SECURE_DOMAINS` 是否包含 `yter.cellmean.com`，修改后执行：

```bash
cd /srv/docker/waline
docker compose up -d --force-recreate
```

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
