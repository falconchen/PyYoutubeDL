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

使用 Supervisor 部署时，可运行专用维护脚本更新 pip 和 yt-dlp，并由 Supervisor 重启 Web 应用、下载器、AI 总结及 WebDAV 服务：

```bash
./runner-supervisor.sh
```

该脚本要求项目虚拟环境位于 `venv/`，并要求 `/usr/bin/supervisorctl` 可用。依赖更新失败时脚本会立即退出，不会继续重启服务。定时执行时建议使用 `flock` 防止任务重叠，并将输出重定向到日志文件。

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

启动后访问 `http://<host>:5100`，通过网页提交 YouTube/小红书/Bilibili 等链接，选择视频或音频模式即可下载。

站点提供以下公开信息页面，并在下载页、视频播放器和音频播放器页脚提供入口：

- `/about`：项目介绍与工作方式
- `/terms`：服务条款与使用边界
- `/privacy`：默认数据处理、第三方请求和保存说明

这些页面按自托管实例编写。公开部署前，运营者应根据实际的反向代理日志、数据保留期限、Waline、WebDAV 和联系方式更新条款及隐私说明，并按适用司法辖区进行必要的法律审查。

提交任务后，页面会每 2 秒查询一次任务状态，显示排队、下载、完成或失败状态，并在下载阶段显示百分比、已下载大小、总大小、速度和预计剩余时间。进度条会标明当前处于下载字幕、下载视频、下载音频、合并音视频、嵌入字幕或后处理等阶段，避免多个阶段分别达到 100% 时产生误解。任务完成后显示最终主媒体文件大小、从进入下载状态到全部处理及移动完成的总耗时，以及“最终文件大小 ÷ 总耗时”得到的平均处理速率；字幕等辅助文件不计入最终大小。页面中的二进制容量单位会简化显示为 `G`、`M`、`K`。视频或音频下载完成后会出现“播放”链接，直接打开对应播放器。页面关闭或刷新不会影响后台下载；带有 `tasks` 查询参数的任务结果页可继续查看这些任务。

提交播放列表链接（如 YouTube `playlist` 页面、带 `list` 参数的链接、`mix` 混合列表，以及频道主页或内容标签页如 `https://www.youtube.com/@频道名/videos`、`/channel/UCxxx/videos`）时，Web 应用会用 yt-dlp 的 `--flat-playlist` 模式把列表解析为逐集 URL，并为每集创建独立任务（视频或音频按所选模式）。每集任务独立下载、独立显示进度，下载完成后立即移入 `FILES_DIR` 并触发 WebDAV 上传，单个视频失败不影响其他视频。解析需要网络访问且可能耗时（默认超时 60 秒），解析失败或列表超出 `PLAYLIST_MAX_ITEMS` 上限时会明确报错，而不会静默只下载第一个视频。普通单视频链接不经过解析，提交行为与之前完全一致。

批量提交（尤其是大播放列表展开出的数百个任务）时，下载器默认每 10 秒最多启动一个新下载（`DOWNLOAD_MIN_INTERVAL_SECONDS`），避免短时间连续请求 YouTube 触发风控；同时运行的下载数由 `MAX_WORKERS` 线程池控制。节流等待期间任务显示为“准备下载”。该节流全局生效，若希望关闭可把 `DOWNLOAD_MIN_INTERVAL_SECONDS` 设为 `0`。

播放器会使用 `ffprobe` 识别 MP4 内嵌字幕，并在浏览器请求字幕时通过 `ffmpeg` 转换为 WebVTT，Video.js 控制栏会显示可用的字幕选项。该功能不修改原视频，但运行环境必须能够直接执行 `ffprobe` 和 `ffmpeg`；无法识别或转换字幕时，视频仍可正常播放，只是不显示字幕选项。

视频播放器支持按需生成 AI 总结。只有当前视频存在内嵌字幕且 AI 接口已配置时，“生成总结”按钮才可用；后端会读取当前选择的字幕流，通过 `chat/completions` 兼容接口生成简体中文总结，`AI_API_TOKEN` 不会发送给浏览器。请在不提交到 Git 的 `config.json` 中配置：

```json
{
  "AI_API_BASE_URL": "https://ccx.v2ai.eu.cc/v1/chat/completions",
  "AI_API_MODEL": "gpt-5.5",
  "AI_API_TOKEN": "sk-xxxxx"
}
```

`AI_API_BASE_URL` 应填写完整的 `chat/completions` 地址，而不是只填写 API 根路径。配置修改后需要重启 Web 应用。字幕文本单次最多发送 120000 个字符，超出部分会截断；同一服务进程内，相同视频版本、字幕流、接口和模型的结果会缓存，以减少重复请求和费用，服务重启后缓存失效。上游接口超时为 120 秒。

AI 返回的 Markdown 会在浏览器中转换为经过清洗的 HTML；为避免模型输出触发脚本或外部图片请求，页面会移除脚本能力、图片、SVG、MathML 和内联样式。总结默认收起长内容，可通过“展开”或“收起”切换；“复制”按钮复制原始 Markdown 文本。

播放器支持通过 `file` 查询参数选择指定视频，例如：

```text
/player?file=视频文件名.mp4
```

该参数只会匹配当前播放器列表中实际存在的 MP4 文件，不匹配时仍按原有顺序播放列表中的第一条。

在播放列表中手动切换视频，或当前视频结束后自动播放下一条时，播放器会同步更新地址栏中的 `file` 参数，便于复制当前视频的播放链接。

视频和音频播放器均声明 iOS Safari 内联播放（`playsinline`），点击播放时保持在当前页面窗口内；视频仍保留 Video.js 全屏控件，只有用户主动点击全屏按钮时才进入全屏。

音频播放器使用 Video.js `audioPosterMode`，保留 16:9 封面并隐藏视频专用画面。页面会从音频 metadata 的 `purl` 或 `comment` 读取 YouTube 视频 ID，依次尝试 `maxresdefault.jpg`、`hqdefault.jpg`，失败时使用 `AUDIO_PLAYER_FALLBACK_COVER_URL`。视频播放器也会使用相同来源字段，在开始播放前显示可用的 YouTube 封面，并在切换视频时同步更新；没有可识别的 YouTube 来源时仍可正常播放，只是不显示封面。封面由浏览器直接访问 `i.ytimg.com`，服务器不会额外保存图片文件。

下载器会在调用 yt-dlp 时为视频和音频统一追加 `--add-metadata`，不依赖 `yt-dlp.conf`、`yta-dlp.conf` 或对应的本机覆盖配置。新下载的媒体会尽可能写入标题、作者、来源页面 URL 等平台可提供的 metadata；具体字段仍取决于来源平台和输出容器支持。

视频和音频播放器会读取媒体 metadata 中的 `title`、`artist`、`album`、`date`、`genre`、`description`、`synopsis`、`purl` 和 `comment`。标题下方按实际存在的字段显示作者、专辑、日期、类型和可展开的简介；`YYYYMMDD` 日期会格式化为 `YYYY-MM-DD`。来源地址只以“原始链接”短文本显示并在新标签页打开，不直接展示长 URL。切换播放列表条目时，标题、metadata、来源链接和封面会同步更新；字段缺失时对应内容自动隐藏。

视频和音频播放器的“返回下载页”及另一种播放器入口统一放在页面右上方，窄屏下保持可点击的紧凑导航，不再重复显示底部导航按钮。

音频封面会从上方透明逐渐过渡到下方的半透明深色遮罩，确保底部频谱和控制栏在明暗不同的封面上都有足够对比度。播放音频时，封面底部会通过浏览器原生 Web Audio API 显示紧贴播放进度条上方的实时频谱柱。暂停或页面进入后台时停止采样和动画，但保留暂停当时的频谱形状；切换音频时才清空并重新绘制。系统启用“减少动态效果”时不会显示。该效果不依赖第三方库，初始化失败时会自动退化为带渐变封面的普通播放器，不影响播放、切歌或下载。

频谱分析要求音频与播放器同源，当前 `/files/...` 路径满足此条件。如果以后改为从其他域名直接加载音频，需要远端提供允许当前站点访问的 CORS 响应头，否则浏览器只能播放音频，不能读取频率数据。

音频播放器也支持 `file` 查询参数、自动播放下一首、播放进度恢复和当前音频下载：

```text
/audio-player?file=音频文件名.mp3
```

音频下载默认同时请求简体中文、繁体中文和英文字幕，并转换为旁挂 SRT 歌词。音频播放器会自动关联与音频同名的 `.lrc`、`.vtt` 或 `.srt` 文件，在高斯模糊的封面和实时频谱上方按播放时间高亮并滚动当前歌词，点击某行可跳转到对应时间；没有匹配歌词、时间轴无法识别或歌词加载失败时不显示歌词面板，音频播放不受影响。本机使用 `yta-dlp.local.conf` 时，需要在该覆盖文件中同步加入歌词下载参数。

### API

```bash
# 添加下载任务
curl -X POST http://localhost:5100/api/add_task \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=xxx", "types": ["video"]}'

# 查询任务状态
curl -X POST http://localhost:5100/api/task_info \
  -H "Content-Type: application/json" \
  -d '{"tasks": ["v20250601120000abc"]}'

# 首次读取 downloader.log 末尾；后续请求传回响应中的 cursor 和 file_id
curl "http://localhost:5100/api/downloader_log" \
  -H "X-Yter-Log-Token: <EXTENSION_LOG_TOKEN>"

# 按 URL 查询或创建 AI 总结任务
curl -X POST http://localhost:5100/api/ai_summaries \
  -H "Content-Type: application/json" \
  -H "X-Yter-AI-Token: <AI_SUMMARY_ACCESS_TOKEN>" \
  -d '{"url":"https://www.youtube.com/watch?v=l38ceFOWOAE"}'

# 查询异步 AI 总结任务
curl http://localhost:5100/api/ai_summaries/jobs/<job_id> \
  -H "X-Yter-AI-Token: <AI_SUMMARY_ACCESS_TOKEN>"
```

`/api/task_info` 会返回任务的 `state`（`queued`、`downloading`、`completed`、`failed` 或 `missing`）和 `progress`。下载中任务的 `progress` 包含可用的 `percent`、`downloaded`、`total`、`speed`、`eta` 等字段；新任务完成后包含 `final_size_bytes`、`elapsed_seconds`、`average_speed_bytes_per_second`。视频或音频任务完成并且主媒体产物仍在本地时，还会返回对应的 `player_url`。

对于没有完成摘要的旧任务，任务 API 只从仍存在的主媒体文件读取最终大小，不使用最后一个下载阶段的耗时和速率；无法可靠恢复的总耗时及平均速率会省略。未生成 `result.json` 的旧任务还会尝试从 downloader 的文件移动日志中恢复最终文件名；只有日志记录和本地文件都仍然存在时才会返回播放链接。

AI 总结接口命中 SQLite 中当前接口、模型和提示词版本的记录时返回 HTTP 200；未命中时返回 HTTP 202、`job_id` 和 `Retry-After: 2`。播放器和 Chrome 扩展随后通过 NDJSON 流实时接收 AI 生成的 Markdown，增量会写入 SQLite，断线后可恢复；任务完成后返回原始 Markdown，无字幕等确定性失败返回 HTTP 422。扩展接口必须使用独立的 `AI_SUMMARY_ACCESS_TOKEN`，令牌只通过 `X-Yter-AI-Token` 请求头传递。

总结永久保存在 `AI_SUMMARY_DB_PATH` 指定的 SQLite 数据库中。数据库使用 WAL、外键和任务租约；模型、接口地址或内部提示词版本变化时生成新版本。字幕只在 `TMP_DIR/ai-summary/` 临时存在并在任务结束后删除，完成和失败的任务记录默认保留 30 天。`ai_summary_worker.py` 独立处理 URL 字幕下载、本地视频内嵌字幕和 AI 请求；预检使用实际生效的 yt-dlp 视频配置，但只下载字幕，不下载视频。

数据库迁移版本 3 会修复早期流式接口在缺少 charset 时将 UTF-8 中文误按 ISO-8859-1 解码而产生的典型乱码；新请求始终按 UTF-8 解码上游 SSE 字节。迁移只处理具有明确 C1 控制字符特征且可无损还原的文本。

AI Worker 的运行日志写入 `LOG_DIR/ai-summary-worker.log`，正常任务会记录领取、媒体解析、字幕选择与获取、AI 调用、缓存命中和完成阶段。日志只记录任务标识及必要的阶段元数据，不记录访问令牌、字幕正文或总结正文。

反向代理需要允许流接口保持长连接并禁用响应缓冲。应用已返回 `X-Accel-Buffering: no` 和 `Cache-Control: no-store, private`；如果代理未遵循该响应头，需要在 `/api/ai_summary/` 和 `/api/ai_summaries/` 对应位置显式设置 `proxy_buffering off`。

### Chrome 右键下载扩展

仓库中的 `chrome-extension/` 是 Manifest V3 扩展。加载后，右键点击网页空白处或网页链接，可在“使用yter下载”二级菜单中选择“下载视频”、“下载音频”或“AI总结”。空白处使用当前页面 URL，链接处优先使用链接 URL；下载操作调用 `/api/add_task`，AI 总结调用 `/api/ai_summaries`。

任务提交后，扩展每 30 秒调用 `/api/task_info` 查询状态，并在下载完成或失败时发送 Chrome 通知。

点击“AI总结”后，扩展会在当前页面注入隔离样式的浮层并显示任务状态。完成后安全渲染 Markdown，提供复制、展开/收起和关闭按钮。AI 令牌仅保存在 `chrome.storage.local` 并由扩展 Service Worker 使用，不会传入当前网页；页面不可用或已经离开时改用 Chrome 通知提示结果。

左键点击浏览器工具栏中的扩展图标，会显示针对当前 HTTP/HTTPS 页面的“下载视频”、“下载音频”和“AI总结”快捷按钮；“服务设置”默认折叠，点击后才展开服务地址、访问令牌和保存按钮。

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
| `PLAYLIST_MAX_ITEMS` | int | 单个播放列表最多展开的任务数，超出拒绝，默认 500 |
| `DOWNLOAD_MIN_INTERVAL_SECONDS` | int | 两次下载启动的最小间隔（秒），0 表示不限速，默认 10 |
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
| `UPLOAD_MAX_RETRIES` | int | 首次上传失败后的最大重试次数，默认 1；每次失败都会立即发送 Bark 通知 |
| `UPLOAD_RETRY_DELAY` | int | WebDAV 上传重试间隔（秒），默认 60 |
| `BARK_DEVICE_TOKEN` | string | Bark 推送通知 Token |
| `EXTENSION_LOG_TOKEN` | string | Chrome 扩展读取 `downloader.log` 的访问令牌；为空时禁用日志接口 |
| `AI_SUMMARY_DB_PATH` | string | AI 总结 SQLite 数据库路径，默认 `./data/ai_summaries.sqlite3` |
| `AI_SUMMARY_ACCESS_TOKEN` | string | Chrome 扩展调用 AI 总结接口的独立访问令牌；为空时禁用扩展接口 |
| `AI_SUMMARY_JOB_RETENTION_DAYS` | int | 已完成和失败的 AI 总结任务记录保留天数，默认 30；总结正文不随任务清理 |
| `TIMEZONE` | string | 时区，如 `Asia/Shanghai` |
| `FLASK_HOST` | string | Flask 监听地址，默认 `0.0.0.0` |
| `FLASK_PORT` | int | Flask Web 应用监听端口，默认 `5100`；应避免与 YTC 的 `5001` 冲突 |
| `FLASK_DEBUG` | bool | Flask 调试模式 |
| `SCHEDULED_PLAYLISTS` | array | 定时下载的播放列表配置 |

将 `ENABLE_WEBDAV_UPLOAD` 设为 `false` 后，`runner.sh` 和 `start.py` 会输出“WebDAV上传已关闭，已跳过启动上传器。”，不再启动上传器进程。此时不会连接 WebDAV、上传或删除下载文件，也不会执行本地过期文件和 WebDAV 远端日期目录清理；下载文件会保留在 `FILES_DIR`。直接运行 `webdav_uploader.py` 时，进程会保持空闲并执行相同的禁用行为。

WebDAV 上传可按文件名关键词排除，例如：

```json
"WEBDAV_UPLOAD_EXCLUDE_KEYWORDS": ["预告", "preview", "sample"]
```

匹配区分大小写，并使用未清理特殊字符前的原始文件名。文件名包含任一非空关键词时，上传器会跳过该文件、保留本地文件，并在 `downloader.log` 记录命中的关键词和文件路径。修改后需要重启上传器以重新加载配置。

上传器检测到 `.ass`、`.lrc`、`.srt`、`.ssa`、`.ttml` 或 `.vtt` 字幕文件时会跳过 WebDAV 上传并保留本地文件，即使 `DELETE_AFTER_UPLOAD` 为 `true` 也不会在上传处理阶段删除字幕。字幕仍受 `FILES_EXPIRE_DAYS` 本地过期清理规则约束。

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

下载视频前，下载器会使用实际生效的 `yt-dlp.conf` 或 `yt-dlp.local.conf` 预检首个视频的字幕。如果配置中的 `--sub-langs` 已经匹配字幕，下载器完全保留配置结果；如果没有匹配，则只追加一条回退字幕，依次优先选择人工中文字幕、人工英文字幕、其他人工原文字幕、自动原文字幕以及中英文自动翻译字幕。这样可以为 AI 总结尽量保留一条可用字幕，同时避免使用 `--sub-langs all` 下载大量自动翻译字幕。

字幕预检失败或超时时不会阻止视频下载，下载器会记录警告并继续使用原配置。预检会额外发起一次 yt-dlp 信息提取请求；播放列表只根据首个视频确定回退语言，同一播放列表中其他视频仍可能没有该语言字幕。

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
├── runner-supervisor.sh  # Supervisor 部署维护脚本
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
