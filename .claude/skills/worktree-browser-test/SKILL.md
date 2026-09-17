---
name: worktree-browser-test
description: 在 DropLoad（PyYoutubeDL）项目的 git worktree 中启动一份独立的 Web 服务并用内置浏览器测试改动。端口从 5201 起，被占用或已被其他 worktree 登记时依次递增；自动生成 worktree 的 config.json 和 .claude/launch.json，默认共享主工作区的数据、登录状态、下载器和 AI worker。只要用户在 worktree / 新工作区里想"用浏览器测试""打开页面看看""预览改动""跑起来验证 UI"，或提到 5201、5202 这类测试端口，就使用本 skill，即使没有明确说 worktree。
---

# 在 worktree 中用浏览器测试 DropLoad

主工作区（`git rev-parse --git-common-dir` 的上级目录）的服务固定跑在 **5200**，同时还在跑 `downloader.py` 和 `ai_summary_worker.py`。worktree 是代码副本，但没有 `config.json`、`venv/` 和数据目录，所以不能直接 `python app.py`。本 skill 让 worktree 的 Web 服务跑在另一个端口上，并复用主工作区的其余部分。

## 步骤

1. **准备配置**（在 worktree 根目录执行，脚本会自己定位主工作区）：

   ```bash
   <主工作区>/venv/bin/python .claude/skills/worktree-browser-test/scripts/prepare_worktree_app.py
   ```

   脚本输出一段 JSON，记下 `port`、`launch_name`、`url`、`already_running`。它做了这些事：
   - 从 5201 开始找端口：已被监听、或已写在其他 worktree 的 `config.json` 里的都跳过，依次 +1。本 worktree 之前用过的端口若仍空闲、或正是本 worktree 的服务在用，就沿用，避免每次换端口。
   - 以主工作区 `config.json` 为底生成本 worktree 的 `config.json`（已 gitignore）：改 `FLASK_PORT`，并把 `config_util.PATH_CONFIG_KEYS` 里的路径（任务、文件、日志、用户库、AI 总结库、OAuth 文件）改成主工作区的绝对路径。
   - 把主工作区的 `*.local.conf`、`*-cookies.txt` 软链过来（均已 gitignore）。
   - 写入 `.claude/launch.json`（已 gitignore），配置名为 `dropload-app-<port>`，带 `FLASK_DEBUG=1`，前端和模板改动会自动重载。

2. **启动并打开**：`already_running` 为 `true` 时服务已在跑，直接导航到 `url`；否则调用内置浏览器的 `preview_start`，`name` 用脚本给出的 `launch_name`。启动后用 `preview_logs` 看一眼有没有报错。

3. **访问地址用 `127.0.0.1`**，不要用 `localhost`：配置了 `REDIRECT_LOCALHOST_TO_LOOPBACK`，`localhost` 会被重定向。常用入口：`/?view=library`（媒体库）、`/player?file=<文件名>`。

4. **登录**：Google 登录的回调地址绑定 5200，不能在测试端口上登录。Cookie 按主机共享、不区分端口，且共享模式下 `FLASK_SECRET_KEY` 和用户库相同，所以让用户**在同一个浏览器里**先打开 `http://127.0.0.1:5200/login` 自己完成登录，再回到测试端口即为登录状态。内置浏览器与用户的 Chrome 不共享 Cookie，两个浏览器要分别登录。登录需要用户本人操作，不要代填账号密码。不登录也可以匿名测试（匿名下载、匿名媒体库）。

5. **测试后**：停止服务用 `preview_list` 取 serverId 再 `preview_stop`。生成的 `config.json`、`launch.json` 和软链都被 gitignore，可以留着下次复用，不会进入提交。

## 共享模式的注意事项

默认共享主工作区数据，这是能直接看到真实媒体库和登录状态的原因，但也意味着：

- 在测试端口上删除文件、提交下载、生成 AI 总结，会真实作用于主工作区数据，AI 调用也会计费。做破坏性操作前先跟用户确认。
- 下载任务和 AI 总结任务由**主工作区**的 downloader / AI worker 处理，运行的是主工作区的代码。如果本次改动涉及 `downloader.py`、`ai_summary_worker.py`、`webdav_uploader.py` 等 worker，共享模式测不到这些改动，需要改用隔离模式。
- 主工作区的 worker 没在运行时，下载和总结会一直排队。可以用 `ps` 检查主工作区路径下的这些进程。

## 隔离模式

改动涉及 worker，或不想碰真实数据时：

```bash
<主工作区>/venv/bin/python .claude/skills/worktree-browser-test/scripts/prepare_worktree_app.py --isolated
```

隔离模式只改端口，路径保持相对，数据落在 worktree 自己的 `urls/`、`files/`、`data/`（都已 gitignore）。需要的 worker 另外在 worktree 根目录用 `<主工作区>/venv/bin/python downloader.py`、`ai_summary_worker.py` 以后台方式启动，测试后记得停掉。隔离模式的用户库是空的，又无法在测试端口登录，只能匿名测试。

## 汇报给用户

告诉用户：实际使用的端口（以及为什么不是 5201，如被哪个服务占用）、共享还是隔离模式、打开的地址、是否需要先去 5200 登录，以及共享模式下会影响真实数据这一点。
