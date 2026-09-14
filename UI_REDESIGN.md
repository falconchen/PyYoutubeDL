# DropLoad 单页 UI

首页是单页双模式：顶栏（小屏为内容区顶部）的分段控件在「下载器」和「媒体库」之间切换，两块面板共用同一个页面与同一套设计语言。下载器部分见下文「首页：DropLoad 下载页」，媒体库部分见「媒体库」。

设计稿在 `DropLoadWithMediaLibrary/`（v0 生成的 Next.js 工程），只作参考，不接入运行时；上一版不含媒体库的稿子保留在 `DropLoad/`。

## 媒体库

媒体库按设计稿重做，替换了原来的 `/player` 整页。左栏是播放器和「正在播放」信息卡，右栏是播放列表，顶部有视频／音频标签；桌面 `minmax(0, 1fr) 390px` 双栏，小屏单栏。

### 播放内核

采用第三方 [zwplayer](https://github.com/chenfanyu/zwplayer-release) 3.3.2，静态资源在 `static/zwplayer/`（约 4.1MB，含 `css/`、`plugins/`、`widgets/`）。用原生 UMD 构建而不是设计稿里的 `zwplayer-react`：

```js
new ZWPlayer({ playerElm: mount, url, poster, fluid: true, autoplay, speedButton: true, music: { mediaSession: false } })
```

- CSS 由 `zwplayer.js` 依据自身脚本路径自动加载 `css/zwplayer.css`，模板里不用单独引入。
- `fluid` 模式会用 `padding-top` 自撑 16:9，而外框 `.dl-stage-frame` 已定好比例，所以 CSS 里强制它 `height:100%; padding-top:0` 填满外框。
- 用户点选或自动播放下一条时，若类型相同则复用同一个实例调用 `play(url)` 换源，`videoEl` 保持不变；视频与音频之间切换、首次载入、深链定位时重建。`play(url)` 不会在标准模式和音乐模式之间切换（zwplayer 只在播放本地文件时切），所以跨类型必须重建；跨类型只可能来自用户点击，重建时仍有手势授权。iOS 上新建的媒体元素没有用户手势授权，重建会让自动下一个和锁屏续播被拦截。离开媒体库模式时销毁实例，避免在下载器页面继续出声。
- `.dl-layout > *` 和 `.dl-library > *` 必须保留 `min-width: 0`。grid item 默认 `min-width:auto`，播放器内部的 min-content 会把列撑破，小屏出现横向溢出。

### 路由与数据

- `/`、`/player`、`/audio-player` 渲染同一个 `templates/index.html`，由模板写入的 `#dl-bootstrap` JSON 决定初始视图（`view`／`tab`／`file`）。
- `/?view=library` 直接打开媒体库；`/player?tab=audio` 和 `/audio-player` 默认选中音频。
- `file` 参数匹配的媒体类型优先于 `tab`：前端 `findMedia()` 找到文件后会把标签切到它所在的那一类，任务完成后的直达播放链接因此保持可用。
- 列表由 `GET /api/media_list` 提供，进入媒体库模式时按需拉取并在前端缓存；两类列表各自按修改时间倒序，沿用 `PLAYER_FILENAME_EXCLUDE_KEYWORDS` 过滤。
- 标题优先用文件内嵌标签，回退到文件名时才隐藏下载器写入的数字时间戳前缀（`08221544-` 这类）。

`/api/media_list` 对每个文件调用一次 `ffprobe` 取时长和高度，结果按文件属性走 `lru_cache`。首次请求在几十个文件的目录上需要数秒，之后命中缓存。

### 随旧播放页移除的功能

按需求采用设计稿的简化播放器，以下原有能力已从界面移除：内嵌／外挂字幕轨道与字幕语言偏好、音频歌词同步滚动、播放器内的 AI 总结、音频频谱与封面模糊背景、播放页的 Waline 评论。倍速、播放进度记忆、自动播放下一个和锁屏控制已在 zwplayer 上补回，见下节。

### 倍速、进度、自动下一个与锁屏控制

均在 `static/dropload.js` 的媒体库段实现，没有使用 zwplayer 自带的播放列表模块（它按 ZWMAP JSON 驱动，没有「切换条目」回调，和右侧列表对不上）。

- **倍速**：标准模式下 zwplayer 默认不显示倍速按钮，需显式 `speedButton: true`，档位为 0.25x–2.0x（旧版的 3x 不提供）。选择写入 `localStorage['dropload:playback-rate']`；换源时 zwplayer 会把菜单复位成 1x、浏览器也会重置 `playbackRate`，所以在 `loadedmetadata` 重新应用，并在 `canplay`／`play` 时调用 `_syncSpeedBtnUI` 对齐菜单文字（该方法属 zwplayer 内部，升级时需复查）。
- **进度**：`localStorage['dropload:playback-progress:<filename>']`，`timeupdate` 每 5 秒、`pause`、换源前、`pagehide`、页面隐藏时保存，`loadedmetadata` 时恢复；距结尾不足 3 秒视为播完并清除。换源途中（新条目元数据未载入）不写入，避免把上一条的位置记到新条目上。
- **自动下一个**：`ended` 后按正在播放条目所属类型的列表顺序播下一条，末尾停止不循环；用户切到另一个标签不影响。
- **地址栏**：用户切换、自动下一个、锁屏切换时用 `replaceState` 改成 `/player?file=<filename>`，与任务完成后的直达链接同一格式；首次载入和深链定位不改地址。
- **锁屏控制**：音频会触发 zwplayer 音乐模式，它自带的 Media Session 指向内部播放列表，因此用 `music: { mediaSession: false }` 关掉，由我们统一注册 `play`／`pause`／快退快进／`seekto`／上一条／下一条，并设置标题、作者、封面和进度；播放时尝试 `navigator.audioSession.type = 'playback'`。
- **iOS 限制**：zwplayer 对音频也使用 `<video>` 元素，iOS 切后台或锁屏时系统可能暂停播放，需以真机测试为准。

对应的后端未删除，仍可用且仍有测试覆盖：`/subtitles/...` 字幕转换路由、`/api/ai_summary*` 与 `/api/ai_summaries*`、`ai_summary_worker.py` 及其 SQLite 存储（Chrome 扩展仍在用）。`find_audio_lyrics()`、`get_local_summary_tracks()` 等只被旧播放页调用的辅助函数现在没有调用方。

删除的文件：`templates/player.html`、`templates/audio_player.html`、`templates/_audio_controller.html`、`static/player.css`、`static/refresh.css`、`static/media-library.js`、`static/background-playback.js`。`static/style.css` 保留，`templates/content_base.html`（关于／条款／隐私）和 chrome 扩展仍在用。

### 验证

```bash
venv/bin/python -m pytest tests/test_media_library.py tests/test_player.py tests/test_audio_player.py tests/test_task_info.py -q
node --check static/dropload.js
```

浏览器检查：桌面双栏与小屏单栏、视频／音频标签切换、`/player?file=` 直达链接选中正确条目并切到对应标签、模式切换时播放器销毁、375px 下无横向溢出。

## 首页：DropLoad 下载页

下载器面板仍是 Flask + Jinja 页面，不引入 Node 构建。

### 结构

- `templates/index.html`：整页结构，图标以内联 SVG sprite（`<symbol>` + `<use>`）提供，替代原来的 Font Awesome CDN。
- `static/dropload.css`：纯 CSS 实现设计稿的间距、配色与断点（640px / 1024px），下载器与媒体库共用，不影响 `style.css` 和关于／条款／隐私页。
- `static/dropload.js`：模式切换、任务入队、进度轮询、任务详情抽屉和媒体库。

左栏为链接输入与格式勾选，右栏为任务列表；桌面端 `minmax(0, 1fr) 390px` 双栏，小屏单栏且任务列表限高 420px。

### 数据流

下载器面板复用既有接口，未新增后端路由（媒体库新增的 `/api/media_list` 见上文）：

- 提交走 `POST /api/add_task`，返回的任务 ID 直接插入右栏列表，不再整页跳转。
- 列表状态轮询 `POST /api/task_info`，2 秒一次，全部任务进入 `completed` / `failed` 后停止。
- 抽屉日志轮询 `POST /api/task_log`，只请求当前任务，1.5 秒一次，任务结束后停止。
- 抽屉的标题、封面、作者和时长来自 `POST /api/video_info_basic`，按源 URL 缓存，保留 30 秒超时与一次重试；`task_info` 给出的 YouTube 封面先行占位。

任务 ID 存在 `localStorage` 的 `dropload.tasks`（最多 20 条，与 `/api/task_log` 的上限一致），刷新后列表仍在。轮询发现任务文件已被清理（`state` 为 `missing`）时从本地列表移除。`<form method="post">` 保留，未启用 JS 时仍走原来的 `POST /` 重定向流程，重定向带回的任务 ID 会并入本地列表。

### 与设计稿的差异

- 设计稿的邮箱／密码注册登录（better-auth + Postgres）未实现，顶栏按钮仍指向既有的 YouTube OAuth（`/oauth/start`），未授权显示「登录」，已授权显示「重新授权」。
- 任务行增加了细进度条、失败状态和「下载文件／播放」链接，这些是原首页已有的能力，设计稿中没有。
- 顶栏的「下载器／媒体库」用链接而不是按钮，未启用 JS 时仍能跳到 `/` 和 `/player`。
- 原首页的复制链接、视频元数据卡片和下载日志侧栏合并进了任务详情抽屉，点击任务行打开，Esc 或点击遮罩关闭。

### 验证

```bash
venv/bin/python -m pytest tests/test_task_info.py tests/test_public_pages.py -q
node --check static/dropload.js
```

浏览器检查：桌面双栏与小屏单栏、任务入队后列表即时更新、抽屉的元数据与日志、375px 下顶栏和格式行无横向溢出。
