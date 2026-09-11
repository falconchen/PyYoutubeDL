# 从 systemd 切换到 Supervisor

## 为什么要换

现有的 `pyyoutubedl.service` 是 `Type=oneshot` + `RemainAfterExit=yes`，只执行一次
`runner.sh`，而 `runner.sh` 用 `nohup` 把 5 个进程扔到后台后自己就退出了。结果是
**systemd 并没有真正监管任何进程**：

- 单个 worker 崩溃不会被拉起，`systemctl status` 永远显示 `active (exited)`；
- 没有按服务粒度的 start/stop/restart，只能整组操作；
- `runner.sh` 建的启动日志在 1 秒后就被删除，启动失败原因一闪而过；
- `journalctl -u pyyoutubedl` 基本是空的，因为进程的 stdout 早已脱离该 unit。

Supervisor 按 program 粒度管理、自带 autorestart 与日志落盘，更贴合本项目 5 个常驻进程的形态。

## 相关文件

| 文件 | 作用 |
|---|---|
| `deploy/supervisor/pyyoutubedl.conf` | 5 个 program 定义模板，路径用 `__PROJECT_DIR__` 占位 |
| `deploy/supervisor/service-guard.sh` | 可选服务（ai / webdav / playlist）的启用判定守卫 |
| `deploy/supervisor/install.sh` | 安装并切换，幂等，支持 `--dry-run` |
| `supervisor-runner.sh` | 切换后的日常运维入口，参数与 `runner.sh` 一致 |

## 切换步骤

先看一遍将要执行的操作，不做任何变更：

```bash
./deploy/supervisor/install.sh --dry-run
```

确认无误后执行切换（需 root）：

```bash
./deploy/supervisor/install.sh
```

脚本依次完成：检查前置条件 → 确认无进行中的下载/上传 → `apt-get install supervisor` →
**停止并 disable** `pyyoutubedl.service` 并用 `stop.py` 兜底清理 → 渲染 program 配置到
`/etc/supervisor/conf.d/pyyoutubedl.conf` → `supervisorctl reread && update && start`。

切换后把 `deploy/targets.conf` 中本机那一行的服务管理器字段由 `systemd` 改为 `supervisor`。

## 必须注意的点

### 1. systemd 必须 disable，不能只 stop

只 `stop` 不 `disable`，下次开机 systemd 与 supervisor 会各拉起一套：两个 `app.py` 抢
5100 端口，两个 `downloader.py` 用 watchdog 盯同一个 `urls/` 目录抢同一个 `.txt` 任务文件
（`.downloading` 重命名竞争），会出现任务重复下载或直接失败。这是整个迁移最危险的一点。
`install.sh` 已经包含 `disable`，手工操作时不要漏。

### 2. 切换后不要再用 `runner.sh` 和 `stop.py`

- `stop.py` 按 cmdline 扫描杀进程，会杀掉 supervisor 托管的进程，supervisor 立刻拉起来——
  表现为「怎么都杀不掉」。
- `runner.sh restart` 更糟：`nohup` 再起一套脱离 supervisor 的进程，与托管进程抢端口和任务文件。

统一改用 `./supervisor-runner.sh`，动作和服务名与 `runner.sh` 完全一致：

```bash
./supervisor-runner.sh status
./supervisor-runner.sh restart downloader
./supervisor-runner.sh -u          # 升级 pip 与依赖后重启全部
```

### 3. 挑停机时机

downloader 下载中被 SIGTERM，任务文件会停在 `.downloading`；`RESUME_INTERRUPTED_DOWNLOADS`
默认为 `false`，不会自动续传，需要手工改回 `.txt`。`install.sh` 会在切换前检查
`urls/*.downloading` 和 `files/*.uploading`，非空即列出文件并中止。

标记文件也可能只是历史遗留（进程早已退出但文件没清掉）。确认无对应进程后，用 `--force`
跳过这项检查：

```bash
./deploy/supervisor/install.sh --force
```

### 4. 用 apt 装，不要用 pip 装

`supervisor-runner.sh` 与 `deploy/remote-deploy.sh` 都默认 `supervisorctl` 位于
`/usr/bin/supervisorctl`（Debian apt 包的路径）。pip 安装会落在 `/usr/local/bin`，届时需要
额外设置 `SUPERVISORCTL_BIN` 环境变量。

### 5. PATH 必须显式配置

`app.py` 的字幕功能直接以 `ffmpeg` 调用外部程序（依赖 PATH），而 supervisord 继承的环境
未必含完整 PATH。program 配置里已显式写入 `environment=PATH=...`，删掉会导致字幕接口静默
返回 503。yt-dlp 不受影响——它走 `sys.executable -m yt_dlp`，不依赖 PATH。

### 6. 子进程必须整组收敛

downloader 会 fork yt-dlp，app 会 fork ffmpeg。program 配置里的 `stopasgroup=true` 与
`killasgroup=true` 是必需的，否则停止 program 后子进程会变成孤儿继续写 `tmp/`。这相当于
`stop.py` 中显式递归收子进程的等价替代。

### 7. 可选服务的状态是 EXITED 而非 FATAL

ai / webdav / playlist 受 `config.json` 开关控制。`service-guard.sh` 先判定是否启用：
启用则 `exec` 对应 worker；未配置退出 10，开关关闭退出 11。program 配置用
`autorestart=unexpected` + `exitcodes=0,10,11` 把这两种情况当作预期退出，状态停在
`EXITED`，日志里留一行中文原因。

守卫在禁用路径上会先 `sleep`（`GUARD_SETTLE_SECS`，默认 6 秒）再退出。这不是凑数：
supervisor 的 `exitcodes` / `autorestart=unexpected` 只在进程 RUNNING 满 `startsecs`
之后才生效，若守卫瞬间退出，supervisor 会判定为启动失败并重试至 **FATAL**。睡过
`startsecs`（配置为 5 秒）再退出，状态才会干净地停在 `EXITED`。

### 8. 日志位置变了

| 内容 | 切换前 | 切换后 |
|---|---|---|
| 进程 stdout/stderr | 基本丢失 | `logs/supervisor/<program>.log`（10MB × 5 轮转） |
| 任务级日志 | `logs/<taskid>.log` | 不变 |

```bash
supervisorctl tail -f pyyoutubedl-downloader
tail -f logs/supervisor/downloader.log
```

supervisor 日志放在 `logs/supervisor/` 子目录，不能直接放 `logs/`——`log_util` 已经在那里写
`downloader.log` 等同名文件，且 `app.py` 会 glob `downloader.log*` 供任务日志接口读取，撞名
会让接口读到 supervisor 的输出。

### 9. FLASK_DEBUG 已关闭

`FLASK_DEBUG` 为 `true` 时 Werkzeug reloader 会 fork 出子进程（切换前 `app.py` 是两个进程），
supervisor 只认父 PID，且 git 部署改动文件会触发 reloader 自重启，与 supervisor 的重启语义
打架。已在 `config.json` 与 `config.sample.json` 中改为 `false`。

## 回滚

```bash
supervisorctl stop pyyoutubedl-app pyyoutubedl-downloader pyyoutubedl-ai-summary pyyoutubedl-webdav pyyoutubedl-playlist-monitor
rm /etc/supervisor/conf.d/pyyoutubedl.conf
supervisorctl update
systemctl enable --now pyyoutubedl
```

`setup_pyyoutubedl_service.sh` 和 systemd unit 都保留着，随时可以切回。
