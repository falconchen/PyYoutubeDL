# 统一媒体库 UI

播放页采用暖灰背景、深绿操作按钮和卡片布局。视频／音频标签位于播放列表顶部，替代原列表标题，以紧凑样式切换播放器及对应播放列表；桌面双栏，小屏单栏。首页已改为 DropLoad 设计稿（见下文「首页：DropLoad 下载页」），媒体库入口移到首页顶栏。

## 路由与兼容

- `/player` 默认打开视频，`/player?tab=audio` 打开音频。
- `file` 参数匹配的媒体类型优先于 `tab`，继续支持下载任务的直接播放链接。
- `/audio-player` 保留为统一播放页的兼容入口，默认选中音频；切换后 URL 更新为 `/player`。
- 两类列表继续独立按修改时间倒序排列，应用原有关键词过滤。
- 标签切换暂停另一个播放器，不自动播放新标签；保留各自进度与选中项。左右方向键、Home、End 可切换标签。

## 文件结构

- `templates/player.html`：共同页面、视频面板、视频控制逻辑和共享评论区。
- `templates/audio_player.html`：音频面板片段，不能单独作为完整页面渲染。
- `templates/_audio_controller.html`：音频控制脚本，以 IIFE 隔离变量；独立下载组件和 AI DOM ID，防止与视频冲突。
- `static/media-library.js`：标签状态、互斥暂停、键盘操作及 URL 更新。
- `static/refresh.css`：首页和媒体库的样式覆盖，基础播放器样式仍在 `player.css`。

字幕、歌词、倍速、下载、播放进度、连续播放及 AI 总结沿用原有逻辑。Waline 仍由原开关控制，共享一处容器，位于播放器列 AI 总结下方，背景、圆角、宽度与信息卡片一致，域名和路径隔离规则不变；旧音频入口路径的历史评论不会自动迁移到 `/player`。部署时继续确认 Waline 的 `SECURE_DOMAINS` 包含 `yter.cellmean.com`。

## 验证

```bash
venv/bin/python -m pytest tests/test_player.py tests/test_audio_player.py tests/test_media_library.py tests/test_public_pages.py -q
git diff --check
```

浏览器检查首页、视频和音频切换、播放后跨标签暂停、键盘切换、移动端横向溢出。测试使用临时目录覆盖空列表、旧入口、文件定位优先级、唯一 DOM ID 和文件名引号安全。未变更下载器、上传器或生产配置。

## 音频字幕自动滚动

`syncLyrics()` 仅使用 `lyricsContent.scrollTo()` 将当前语句居中到字幕容器；不要对语句调用 `scrollIntoView()`，它会连带滚动页面，让正在查看评论的用户跳回播放器。字幕高亮和减少动态效果设置保持原行为。

## 首页：DropLoad 下载页

首页按 `DropLoad/`（v0 生成的 Next.js 设计稿）重做，但仍是 Flask + Jinja 页面，不引入 Node 构建。设计稿只作参考，未接入运行时。

### 结构

- `templates/index.html`：整页结构，图标以内联 SVG sprite（`<symbol>` + `<use>`）提供，替代原来的 Font Awesome CDN。
- `static/dropload.css`：纯 CSS 实现设计稿的间距、配色与断点（640px / 1024px），仅在首页使用，不影响 `style.css`、`refresh.css` 和播放页。
- `static/dropload.js`：任务入队、进度轮询和任务详情抽屉。

左栏为链接输入与格式勾选，右栏为任务列表；桌面端 `minmax(0, 1fr) 390px` 双栏，小屏单栏且任务列表限高 420px。

### 数据流

页面复用既有接口，未新增后端路由：

- 提交走 `POST /api/add_task`，返回的任务 ID 直接插入右栏列表，不再整页跳转。
- 列表状态轮询 `POST /api/task_info`，2 秒一次，全部任务进入 `completed` / `failed` 后停止。
- 抽屉日志轮询 `POST /api/task_log`，只请求当前任务，1.5 秒一次，任务结束后停止。
- 抽屉的标题、封面、作者和时长来自 `POST /api/video_info_basic`，按源 URL 缓存，保留 30 秒超时与一次重试；`task_info` 给出的 YouTube 封面先行占位。

任务 ID 存在 `localStorage` 的 `dropload.tasks`（最多 20 条，与 `/api/task_log` 的上限一致），刷新后列表仍在。轮询发现任务文件已被清理（`state` 为 `missing`）时从本地列表移除。`<form method="post">` 保留，未启用 JS 时仍走原来的 `POST /` 重定向流程，重定向带回的任务 ID 会并入本地列表。

### 与设计稿的差异

- 设计稿的邮箱／密码注册登录（better-auth + Postgres）未实现，顶栏按钮仍指向既有的 YouTube OAuth（`/oauth/start`），未授权显示「登录」，已授权显示「重新授权」。
- 播放器部分按要求未改动，`/player`、`/audio-player` 及其模板保持原样。
- 任务行增加了细进度条、失败状态和「下载文件／播放」链接，这些是原首页已有的能力，设计稿中没有。
- 原首页的复制链接、视频元数据卡片和下载日志侧栏合并进了任务详情抽屉，点击任务行打开，Esc 或点击遮罩关闭。

### 验证

```bash
venv/bin/python -m pytest tests/test_task_info.py tests/test_public_pages.py tests/test_audio_player.py -q
node --check static/dropload.js
```

浏览器检查：桌面双栏与小屏单栏、任务入队后列表即时更新、抽屉的元数据与日志、375px 下顶栏和格式行无横向溢出。
