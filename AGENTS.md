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
- `config_util.py`：读取 `config.json`，并提供默认配置。
- `start.py` / `stop.py` / `runner.sh`：启动、停止和运行相关服务。
- `yt-dlp.conf` / `yta-dlp.conf`：视频和音频下载配置。
- `*.local.conf`：本机覆盖配置，通常不应提交。

任务文件生命周期：

```text
.txt -> .downloading -> .ok / .fail
```

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
export FLASK_APP=app.py && export FLASK_DEBUG=1 && flask run --host=0.0.0.0

# 运行测试
python -m pytest test_timezone.py test_video_info.py -v

# 获取 YouTube cookies
flask get-cookie
```

## 开发注意事项

- Shell 命令应尽量兼容 Linux、FreeBSD 和 macOS。
- Python 代码应遵循现有文件风格，不引入不必要的新框架。
- 与下载、上传、删除、cookie、WebDAV、日志清理相关的改动要特别谨慎。
- `test_video_info.py` 中的部分测试可能访问 YouTube，需要网络环境支持。
- 不要把真实 cookie、密钥、Token、服务器密码等敏感信息写入文档、日志或测试数据。
- 修改配置时优先更新 `config.sample.json` 或文档说明，不要把本地私有配置当作默认值。
- 首页 `templates/index.html` 已接入 Waline 评论，服务地址是 `https://waline.v2ai.eu.cc`。
- 如果修改首页评论相关代码，确认 Waline 的 `/srv/docker/waline/.env` 中 `SECURE_DOMAINS` 包含 `yter.cellmean.com`。

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
