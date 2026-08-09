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

`start` 和 `restart` 会自动激活虚拟环境、更新 pip 与 yt-dlp，然后显式使用该虚拟环境的 Python 启动 Web 应用、下载器和上传器，不依赖各脚本的 shebang。`stop` 不更新依赖；在 Devil 环境中，单独执行 `stop` 不会重启 Devil 管理的 Web 应用，`restart` 则保持原有的 Devil 重启行为。

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

站点提供以下公开信息页面，并在下载页、视频播放器和音频播放器页脚提供入口：

- `/about`：项目介绍与工作方式
- `/terms`：服务条款与使用边界
- `/privacy`：默认数据处理、第三方请求和保存说明

这些页面按自托管实例编写。公开部署前，运营者应根据实际的反向代理日志、数据保留期限、Waline、WebDAV 和联系方式更新条款及隐私说明，并按适用司法辖区进行必要的法律审查。

提交任务后，页面会每 2 秒查询一次任务状态，显示排队、下载、完成或失败状态，并在下载阶段显示百分比、已下载大小、总大小、速度和预计剩余时间。进度条会标明当前处于下载字幕、下载视频、下载音频、合并音视频、嵌入字幕或后处理等阶段，避免多个阶段分别达到 100% 时产生误解。任务完成后显示最终主媒体文件大小、从进入下载状态到全部处理及移动完成的总耗时，以及“最终文件大小 ÷ 总耗时”得到的平均处理速率；字幕等辅助文件不计入最终大小。页面中的二进制容量单位会简化显示为 `G`、`M`、`K`。视频或音频下载完成后会出现“播放”链接，直接打开对应播放器。页面关闭或刷新不会影响后台下载；带有 `tasks` 查询参数的任务结果页可继续查看这些任务。

播放器会使用 `ffprobe` 识别 MP4 内嵌字幕，并在浏览器请求字幕时通过 `ffmpeg` 转换为 WebVTT，Video.js 控制栏会显示可用的字幕选项。该功能不修改原视频，但运行环境必须能够直接执行 `ffprobe` 和 `ffmpeg`；无法识别或转换字幕时，视频仍可正常播放，只是不显示字幕选项。

播放器支持通过 `file` 查询参数选择指定视频，例如：

```text
/player?file=视频文件名.mp4
```

该参数只会匹配当前播放器列表中实际存在的 MP4 文件，不匹配时仍按原有顺序播放列表中的第一条。

在播放列表中手动切换视频，或当前视频结束后自动播放下一条时，播放器会同步更新地址栏中的 `file` 参数，便于复制当前视频的播放链接。

音频播放器使用 Video.js `audioPosterMode`，保留 16:9 封面并隐藏视频专用画面。页面会从音频 metadata 的 `purl` 或 `comment` 读取 YouTube 视频 ID，依次尝试 `maxresdefault.jpg`、`hqdefault.jpg`，失败时使用 `AUDIO_PLAYER_FALLBACK_COVER_URL`。封面由浏览器直接访问 `i.ytimg.com`，服务器不会额外保存图片文件。

音频封面会从上方透明逐渐过渡到下方的半透明深色遮罩，确保底部频谱和控制栏在明暗不同的封面上都有足够对比度。播放音频时，封面底部会通过浏览器原生 Web Audio API 显示随声音频率变化的实时频谱柱。频谱会在暂停、播放结束或页面进入后台时停止并清空；系统启用“减少动态效果”时不会显示。该效果不依赖第三方库，初始化失败时会自动退化为带渐变封面的普通播放器，不影响播放、切歌或下载。

频谱分析要求音频与播放器同源，当前 `/files/...` 路径满足此条件。如果以后改为从其他域名直接加载音频，需要远端提供允许当前站点访问的 CORS 响应头，否则浏览器只能播放音频，不能读取频率数据。

音频播放器也支持 `file` 查询参数、自动播放下一首、播放进度恢复和当前音频下载：

```text
/audio-player?file=音频文件名.mp3
```

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

# 首次读取 downloader.log 末尾；后续请求传回响应中的 cursor 和 file_id
curl "http://localhost:5001/api/downloader_log" \
  -H "X-Yter-Log-Token: <EXTENSION_LOG_TOKEN>"
```

`/api/task_info` 会返回任务的 `state`（`queued`、`downloading`、`completed`、`failed` 或 `missing`）和 `progress`。下载中任务的 `progress` 包含可用的 `percent`、`downloaded`、`total`、`speed`、`eta` 等字段；新任务完成后包含 `final_size_bytes`、`elapsed_seconds`、`average_speed_bytes_per_second`。视频或音频任务完成并且主媒体产物仍在本地时，还会返回对应的 `player_url`。

对于没有完成摘要的旧任务，任务 API 只从仍存在的主媒体文件读取最终大小，不使用最后一个下载阶段的耗时和速率；无法可靠恢复的总耗时及平均速率会省略。未生成 `result.json` 的旧任务还会尝试从 downloader 的文件移动日志中恢复最终文件名；只有日志记录和本地文件都仍然存在时才会返回播放链接。

### Chrome 右键下载扩展

仓库中的 `chrome-extension/` 是 Manifest V3 扩展。加载后，右键点击网页空白处或网页链接，可在“使用yter下载”二级菜单中选择“下载视频”或“下载音频”。空白处使用当前页面 URL，链接处优先使用链接 URL，扩展会调用 `/api/add_task` 创建对应任务。

任务提交后，扩展每 30 秒调用 `/api/task_info` 查询状态，并在下载完成或失败时发送 Chrome 通知。

左键点击浏览器工具栏中的扩展图标会直接显示服务地址设置和保存按钮，不再跳转后才配置。

弹窗中的“实时日志”会打开独立日志页，每秒增量读取当前服务器的 `downloader.log`，效果类似 `tail -f`。该接口必须配置 `EXTENSION_LOG_TOKEN`，令牌通过请求头传递，并仅保存在扩展本机存储中。

开发者模式安装、服务地址配置和验证方法见 [`chrome-extension/README.md`](chrome-extension/README.md)。

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
| `YT_DLP_OUTPUT_TEMPLATE` | string | 视频文件名主体模板；下载时自动添加 `MMDDHHmm-` 前缀 |
| `YTA_DLP_OUTPUT_TEMPLATE` | string | 音频文件名主体模板；下载时自动添加 `MMDDHHmm-` 前缀 |
| `PLAYER_FILENAME_EXCLUDE_KEYWORDS` | array | 播放器列表排除的文件名关键词，任一非空关键词命中即隐藏，默认 `[]` |
| `AUDIO_PLAYER_FALLBACK_COVER_URL` | string | YouTube 音频封面不可用时的图片 URL，默认 `/static/images/audio-cover-default.svg` |
| `ENABLE_WEBDAV_UPLOAD` | bool | 是否将下载完成的文件上传到 WebDAV，默认 `true`；关闭时文件保留在本地 |
| `WEBDAV_UPLOAD_EXCLUDE_KEYWORDS` | array | WebDAV 上传排除的文件名关键词，命中任一非空关键词即跳过上传，默认 `[]` |
| `DELETE_AFTER_UPLOAD` | bool | WebDAV 上传后是否删除本地文件 |
| `FILES_EXPIRE_DAYS` | int | 启动时清理超过 N 天的旧文件，0 表示不清理 |
| `VIDEO_WEBDAV_OPTIONS` | object | 视频 WebDAV 远程存储配置 |
| `AUDIO_WEBDAV_OPTIONS` | object | 音频 WebDAV 远程存储配置 |
| `BARK_DEVICE_TOKEN` | string | Bark 推送通知 Token |
| `EXTENSION_LOG_TOKEN` | string | Chrome 扩展读取 `downloader.log` 的访问令牌；为空时禁用日志接口 |
| `TIMEZONE` | string | 时区，如 `Asia/Shanghai` |
| `FLASK_HOST` | string | Flask 监听地址，默认 `0.0.0.0` |
| `FLASK_DEBUG` | bool | Flask 调试模式 |
| `SCHEDULED_PLAYLISTS` | array | 定时下载的播放列表配置 |

将 `ENABLE_WEBDAV_UPLOAD` 设为 `false` 后，`runner.sh` 和 `start.py` 会输出“WebDAV上传已关闭，已跳过启动上传器。”，不再启动上传器进程。此时不会连接 WebDAV、上传或删除下载文件，也不会执行本地过期文件和 WebDAV 远端日期目录清理；下载文件会保留在 `FILES_DIR`。直接运行 `webdav_uploader.py` 时，进程会保持空闲并执行相同的禁用行为。

WebDAV 上传可按文件名关键词排除，例如：

```json
"WEBDAV_UPLOAD_EXCLUDE_KEYWORDS": ["预告", "preview", "sample"]
```

匹配区分大小写，并使用未清理特殊字符前的原始文件名。文件名包含任一非空关键词时，上传器会跳过该文件、保留本地文件，并在 `downloader.log` 记录命中的关键词和文件路径。修改后需要重启上传器以重新加载配置。

下载文件名使用 `TIMEZONE` 指定时区的任务开始时间作为前缀，格式为“月份、日期、小时、分钟”，各字段不足两位时前补 `0`。例如 8 月 4 日 01:01 开始下载时，文件名为 `08040101-当前命名模板.mp4`。字幕等由 yt-dlp 生成的关联文件使用相同前缀。

播放器列表可按文件名关键词隐藏视频，例如：

```json
"PLAYER_FILENAME_EXCLUDE_KEYWORDS": ["预告", "preview", "sample"]
```

匹配区分大小写，文件名包含任一非空关键词时不会出现在 `/player` 或 `/audio-player` 页面；原文件不会被删除，也不影响下载和 WebDAV 上传。修改后需要重启 Web 应用以重新加载配置。

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

## 播放进度

视频和音频播放页会按文件名把当前播放时间分别保存在浏览器 `localStorage` 中。切换媒体、暂停或离开页面时会保存进度，再次播放同一文件时自动回到上次位置。

视频正常播放结束或距离结尾不足 3 秒时会清除该视频的记录，下次从头播放。进度仅保存在当前浏览器和当前站点下，不会同步到其他浏览器或设备；清除站点数据也会删除记录。

## 视频字幕偏好

视频播放器首次打开时会根据浏览器的 `Accept-Language` 和语言设置选择对应字幕。只要请求头接受简体中文（`zh-CN`、`zh-SG` 或 `zh-Hans`，且权重大于 0），存在简体中文字幕时会优先显示简体中文；否则按照浏览器语言顺序匹配可用字幕。

用户在 Video.js 字幕菜单中手动选择其他语言或关闭字幕后，播放器会把该选择保存到浏览器 `localStorage` 的 `pyyoutubedl:subtitle-preference`，以后打开或切换视频时优先采用该偏好。字幕偏好仅保存在当前浏览器和当前站点；清除站点数据后会恢复浏览器语言自动选择。

## 页面评论

首页、视频播放页和音频播放页已接入 Waline 评论，评论服务地址：

```txt
https://waline.v2ai.eu.cc
```

公开页面地址：

```txt
https://yter.cellmean.com/
https://yter.cellmean.com/player
https://yter.cellmean.com/audio-player
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
yter.cellmean.com/audio-player
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
