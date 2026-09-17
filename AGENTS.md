# AGENTS.md

本文件用于指导 Codex 和其他 AI Agent 在本仓库中工作。所有回复、计划、说明和总结默认使用简体中文。


## 回复风格

- 使用简体中文。
- 风格务实、客观、谨慎。
- 直言不讳，不拐弯抹角。
- 不夸大结果，不隐瞒风险。
- 不确定的地方要明确说明，并给出可验证的下一步。

## 工作原则

- 每次执行任务前，先列出简短计划。
- 执行过程中优先阅读现有代码、配置和文档，再动手修改。
- 修改范围要尽量小，避免无关重构。
- 不要覆盖用户已有改动；如果发现工作区有无关变更，保留它们。
- 涉及命令、配置、部署、定时任务、路径、凭据或生产环境操作时，要谨慎确认影响范围。
- 完成后说明做了什么、生成或修改了哪些文件、是否运行了验证命令。

## 项目概览

本项目是基于 Flask 和 yt-dlp 的 YouTube 下载工具，主要由以下部分组成：

- `app.py`：Flask Web 应用，提供页面和 API。
- `downloader.py`：监听任务目录并调用 `yt-dlp` 下载视频或音频。
- `webdav_uploader.py`：监听下载产物并上传到 WebDAV。
- `playlist_monitor.py`：播放列表监控 worker（可选），轮询 YouTube Data API 的收件箱播放列表并消费式下发下载任务。
- `youtube_auth.py`：YouTube OAuth 授权与 Data API 封装（令牌加载/刷新、service 构建）。
- `task_queue.py`：下载任务写入（Web 与 worker 共用），负责 `<v|a><时间戳><随机>.txt` 任务文件。
- `user_store.py`：多用户 SQLite 存储（账号、Google 身份绑定、按用户的 OAuth 令牌、任务与媒体归属）。下载器不感知用户，归属只在数据库里维护。
- 匿名下载按脱敏客户端 IP 每个自然日限额，资源访问则按签名会话中的随机匿名 ID 隔离；两者不能混用。匿名产物由 `anonymous_cleanup.py` 按独立小时数清理。
- `config_util.py`：读取 `config.json`，并提供默认配置。
- `start.py` / `stop.py` / `runner.sh`：启动、停止和运行相关服务。
- `yt-dlp.conf` / `yta-dlp.conf`：视频和音频下载配置。
- `*.local.conf`：本机覆盖配置，通常不应提交。

`config.json` 和 `config.sample.json` 虽保留 `.json` 后缀，但运行配置使用 JSON5 解析器，可写 `//`、`/* ... */` 注释和尾逗号；其他 API、日志和结果 JSON 仍使用严格 JSON。

任务文件生命周期：

```text
.txt -> .downloading -> .ok / .fail
.txt / .downloading -> .paused -> .txt（继续，沿用临时目录断点续传）
```

暂停、继续、重启、删除由 `/api/task_action` 发起。非下载中的任务由 Web 端直接改名或清理；`.downloading` 任务只能由下载器处理：Web 端原子写入 `<task_id>.control`（内容为 `pause`/`restart`/`delete`），下载器用 psutil 终止 yt-dlp 进程树后再改写状态。yt-dlp 不另开进程组（Supervisor 依赖 `stopasgroup`），且必须由 `Popen` 自己等待，否则会拿到返回码 0 被误判为下载成功。

`RESUME_INTERRUPTED_DOWNLOADS` 为 `true` 时，下载器启动后会恢复遗留的 `.downloading` 任务；默认关闭，避免历史任务在重启后被自动执行。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动所有服务
python start.py

# 使用 shell runner 启动
./runner.sh

# 停止服务
python stop.py

# 启动 Flask 开发服务器
export FLASK_APP=app.py && export FLASK_DEBUG=1 && flask run --host=0.0.0.0 --port=5100

# 运行测试
python -m pytest tests/ -q

# 获取 YouTube cookies
flask get-cookie
```

## 开发注意事项

- Shell 命令应尽量兼容 Linux、FreeBSD 和 macOS。
- Python 代码应遵循现有文件风格，不引入不必要的新框架。
- 与下载、上传、删除、cookie、WebDAV、日志清理相关的改动要特别谨慎。
- `tests/test_video_info.py` 中的部分测试可能访问 YouTube，需要网络环境支持。
- 本地验证服务时注意执行环境的进程生命周期：当前 Codex 命令执行器在命令结束后可能回收由命令启动的后台子进程，即使 `runner.sh` 使用了 `nohup` 也可能导致服务随后不可访问。需要保持服务运行时，应在用户自己的终端启动 `./runner.sh start`，或让验证会话保持前台；不要仅根据 `runner.sh` 的“启动成功”提示判断服务已持续运行，需另行检查 `http://127.0.0.1:5100/` 和相关进程。
- 不要把真实 cookie、密钥、Token、服务器密码等敏感信息写入文档、日志或测试数据。
- 修改配置时优先更新 `config.sample.json` 或文档说明，不要把本地私有配置当作默认值。
- 带注释的配置不能使用 `python -m json.tool` 或 `jq` 验证；应运行 `tests/test_config_util.py`。项目只约定 JSONC 风格注释和尾逗号，不推广其他 JSON5 扩展语法。
- 首个注册账号成为管理员并接管历史任务、文件与旧的全局 Google 令牌；其余注册进入待审批，管理员在 `/admin` 放行。改动认证相关代码时，注意 `current_user()` 会在账号被停用后立即让旧 cookie 失效。
- 任务与媒体按用户隔离。新增任何读取 `FILES_DIR` 或任务的路由时，必须走 `require_media_access()` 或 `owned_tasks()`，否则会造成越权。跨用户访问统一返回 404 而不是 403，避免泄露资源是否存在。
- 匿名会话也属于独立资源主体。修改归属同步、首个管理员历史接管或登录流程时，必须防止接管其他匿名会话的数据；登录只转移当前匿名会话的数据。
- 单页 `templates/index.html`（下载器 + 媒体库）已接入 Waline 评论，服务地址是 `https://waline.v2ai.eu.cc`；旧的 `templates/player.html` 已随播放页重做删除。
- 评论区默认不显示，由 `config.json` 中的 `SHOW_WALINE_ON_INDEX` 和 `SHOW_WALINE_ON_PLAYER` 分别控制。
- Waline 客户端 `path` 使用 `window.location.hostname + window.location.pathname`，按域名和路径隔离评论，避免多个站点的 `/` 共享评论。
- 如果修改页面评论相关代码，确认 Waline 的 `/srv/docker/waline/.env` 中 `SECURE_DOMAINS` 包含 `yter.cellmean.com`。
- Chrome 扩展实时日志通过 `/api/downloader_log` 增量读取 `LOG_DIR/downloader.log`；必须使用 `EXTENSION_LOG_TOKEN` 和 `X-Yter-Log-Token` 请求头，禁止在 URL、文档或测试中写入真实令牌。
- URL 级 AI 总结持久化在 `AI_SUMMARY_DB_PATH` 的 SQLite 中，由 `ai_summary_worker.py` 异步、流式处理；生成中的 Markdown 增量可写入任务记录，原始字幕不得写入数据库。扩展接口 `/api/ai_summaries` 及其流接口必须使用独立的 `AI_SUMMARY_ACCESS_TOKEN` 和 `X-Yter-AI-Token`。
- YouTube OAuth 令牌文件（默认 `data/youtube_token.json`）是凭据，禁止写入文档、日志或测试数据；`data/` 已在 `.gitignore` 中。OAuth 客户端密钥只应出现在 gitignored 的 `config.json`，示例配置 `config.sample.json` 用占位值。
- 播放列表监控是消费式的：`playlist_monitor.py` 会先从播放列表删除条目、删除成功才写下载任务；涉及该 worker 的改动要注意不要破坏“先删除、后下发、失败跳过”的顺序，避免重复下载或丢失条目。

## 文档要求

- 重要变更应同步更新相关文档。
- 新增脚本、配置项、部署步骤或定时任务时，要说明用途、运行方式和风险点。
- 面向后续 AI Agent 的说明要具体，避免只写结论不写上下文。

## 任务完成后的汇报格式

完成任务后，至少说明：

- 已完成的事项。
- 新增或修改的文件。
- 已运行的验证命令及结果。
- 未验证或需要用户确认的事项。
