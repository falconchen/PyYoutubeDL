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

`/api/media_list` 需要的时长、画面高度、展示标签和内嵌字幕流都来自 `_probe_media_info()` 的一次 `ffprobe`，结果按文件路径、修改时间和大小走 `lru_cache`（1024 条）；`_probe_media_dimensions`／`_probe_media_metadata`／`_probe_embedded_subtitles` 从它派生。列表请求先用 4 个线程并发预取未命中的文件，服务启动时还会在后台线程预热全部媒体（调试模式只在重载后的子进程里执行）。44 个文件的冷启动从约 7 秒（124 次 ffprobe 串行）降到约 0.9 秒，命中缓存时约 20 毫秒。`cleanup_expired_anonymous_media()` 在列表和每个 `/files/` 请求中都会调用，现按同一组目录 60 秒内最多执行一次（downloader 另有每小时的清理循环）；未登记归属的新文件仍由 `sync_media_ownership()` 即时补齐。

### 随旧播放页移除的功能

按需求采用设计稿的简化播放器，以下原有能力已从界面移除：字幕语言偏好记忆、音频频谱与封面模糊背景、播放页的 Waline 评论。倍速、播放进度记忆、自动播放下一个和锁屏控制已在 zwplayer 上补回，见下节；AI 总结已补回到「正在播放」卡片下方，见「AI 总结」。

「正在播放」卡片补回了旧播放页的「原始链接」和可展开的「简介」：`/api/media_list` 的每个条目带 `source_url` 和 `description`（来自媒体标签 `purl`／`comment` 与 `description`／`synopsis`，与旧页相同），字段为空时对应元素隐藏。原始链接与作者、格式信息同一行；简介默认展开，保留原文换行，超过 240px 在框内滚动，切换条目时重新展开。旧页的专辑、日期、类型未补回。

### 倍速、进度、自动下一个与锁屏控制

均在 `static/dropload.js` 的媒体库段实现，没有使用 zwplayer 自带的播放列表模块（它按 ZWMAP JSON 驱动，没有「切换条目」回调，和右侧列表对不上）。

- **倍速**：标准模式下 zwplayer 默认不显示倍速按钮，需显式 `speedButton: true`，档位为 0.25x–2.0x（旧版的 3x 不提供）。选择写入 `localStorage['dropload:playback-rate']`；换源时 zwplayer 会把菜单复位成 1x、浏览器也会重置 `playbackRate`，所以在 `loadedmetadata` 重新应用，并在 `canplay`／`play` 时调用 `_syncSpeedBtnUI` 对齐菜单文字（该方法属 zwplayer 内部，升级时需复查）。
- **进度**：`localStorage['dropload:playback-progress:<filename>']`，`timeupdate` 每 5 秒、`pause`、换源前、`pagehide`、页面隐藏时保存，`loadedmetadata` 时恢复；距结尾不足 3 秒视为播完并清除。换源途中（新条目元数据未载入）不写入，避免把上一条的位置记到新条目上。
- **自动下一个**：`ended` 后按正在播放条目所属类型的列表顺序播下一条，末尾停止不循环；用户切到另一个标签不影响。
- **链接自动播放**：通过 `file` 参数定位到条目（任务完成后的「播放」链接、分享的地址）时自动开播；不带参数或文件不存在时只选中第一条、不播放。浏览器通常会拦截没有点击的有声自动播放，此时 zwplayer 改为静音播放，所以配置为 `disableMutedConfirm: false`，保留「因浏览器限制，已静音播放，请点击打开声音」提示（手机上为「点击开启声音」）；设为 `true` 会静音播放且毫无提示。
- **地址栏**：用户切换、自动下一个、锁屏切换时用 `replaceState` 改成 `/player?file=<filename>`，与任务完成后的直达链接同一格式；首次载入和深链定位不改地址。
- **锁屏控制**：音频会触发 zwplayer 音乐模式，它自带的 Media Session 指向内部播放列表，因此用 `music: { mediaSession: false }` 关掉，由我们统一注册 `play`／`pause`／快退快进／`seekto`／上一条／下一条，并设置标题、作者、封面和进度；播放时尝试 `navigator.audioSession.type = 'playback'`。
- **字幕**：`/api/media_list` 的视频条目带 `subtitles`（`url`、`label`、`language`），沿用旧播放页的来源：同名外挂字幕优先，没有时用 MP4 内嵌字幕流，分别经 `/subtitles/sidecar/...vtt` 与 `/subtitles/<file>/<stream>.vtt` 转成 WebVTT。`zh-CN`／`zh-TW` 等地区代码归并为简体／繁体。前端先 `fetch` 字幕文本，确认仍是当前条目后才调用 `zwplayer.addSubtitle(text, pos, label)`：zwplayer 按 URL 加载时不核对片源，快速切换会把上一条的字幕挂到新视频上。第一条可用轨道（简体、繁体、中文、英文的顺序）作为主字幕默认显示，其余进 CC 菜单，可切换或开双语；`play(url)` 内部的 `stop()` 会清空上一条字幕。用户在菜单里的选择不跨条目记忆。字幕样式默认「字幕大小：较小」（`fontSize: '0.14'`）、背景不透明度 50%（`bgOpacity: 0.5`）：zwplayer 没有对应的构造参数，建好实例后直接改 `_subtitleSettings` 并调用 `_applyAllSubtitleSettings()`（均为内部字段，升级时需复查）；用户在字幕设置面板里的修改（主字幕与第二字幕的大小、颜色、描边，以及位置、背景不透明度、等比缩放、淡入淡出）写入 `localStorage['dropload:subtitle-settings']`，建实例时在本站默认值之上合并恢复，跨条目、跨页面生效；zwplayer 没有设置变更事件，面板的每项修改都会调用 `_applyAllSubtitleSettings()`，因此替换实例上的这个方法，调用原实现后保存。读取时逐项校验取值（颜色会被拼进面板 HTML，只接受十六进制）。「恢复默认设置」改为回到本站默认值（较小、50%）并清除记忆：在 document 捕获阶段识别该按钮的点击，接管随后那次 `_applyAllSubtitleSettings()`。字号换算也由我们接管：zwplayer 的 `updateSubtitlePosition` 以播放器宽度的 5% 为「适中」基准并夹在 14–32px，宽于 640px 后全屏和页面内一样大；现在替换实例上的这个方法，基准取宽度 4.2% 与高度 7.5% 的较小者（下限仍为 14px），各档位按 `fontSize / 0.2` 缩放，实际字号最大 36px。页面内 760×428 时「较小」为 22.34px（与原先相同），全屏时随画面放大到 36px 为止；「等比缩放」关闭时不改字号，与原行为一致。字幕轨道与其他媒体信息共用同一次 ffprobe 结果。
- **歌词**：音频条目带 `lyrics_url`（没有旁挂歌词时为空串），指向 `/lyrics/<音频文件名>.lrc`。该路由按音频文件鉴权（旁挂歌词不一定单独登记了归属），用 `find_audio_lyrics()` 选出同名歌词（无语言后缀、简体、繁体、英文的顺序，LRC 优先于 VTT、SRT），LRC 原样返回，其余经 `ffmpeg -f lrc` 转换；多行字幕会变成同一时间戳的几行，zwplayer 显示为主歌词加翻译。前端同样先取回文本再 `setLyrics(text)`，换源时先 `setLyrics('')`，因为 `play(url)` 不会清掉上一首的歌词。zwplayer 的 LRC 解析只认两位分钟数，超过 100 分钟的部分不显示。zwplayer 每换一句就对当前行调用 `scrollIntoView({block: 'center'})`，它会连带滚动整个页面，用户下拉页面时会被拽回播放器；`confineLyricsScrolling()` 在 `Element.prototype` 上接管该方法，只对 `.zwp-music-lyrics` 里的元素改为滚动歌词容器本身（歌词行随切歌和面板切换重建，逐个元素替换会漏），其他元素仍调用原生实现。
- **iOS 限制**：zwplayer 对音频也使用 `<video>` 元素，iOS 切后台或锁屏时系统可能暂停播放，需以真机测试为准。

### AI 总结

「正在播放」卡片下方的 `.dl-summary` 面板，只对 `/api/media_list` 中 `ai_summary` 为 `true` 的条目显示：视频有字幕轨道，音频由 `get_local_summary_tracks()` 找到旁挂歌词／字幕或内嵌字幕。`AI_API_*` 未配置时（引导 JSON 的 `aiSummary` 为 `false`）面板仍显示，按钮禁用并提示未配置。

- 点击「生成总结」后 `POST /api/ai_summary`，只传 `filename`，字幕由后端选择（外挂优先，其次首条内嵌轨道）；命中已保存的总结时直接返回 200。
- 返回 202 时读取 `/api/ai_summary/jobs/<job_id>/stream` 的 NDJSON，把 `partial_markdown` 实时渲染出来；断线最多重连 3 次。
- 结果按文件名缓存在页面内存里，切换条目再切回直接显示；生成中切走不会中断，切回后继续显示进度。
- Markdown 用 jsDelivr 上的 `marked` 渲染、`DOMPurify` 清洗（去掉 `img`／`svg`／`math`／`style` 和内联样式，链接新窗口打开）；两个库没载入时按纯文本显示。支持复制原始 Markdown 和展开／收起。
- 权限与媒体访问一致：登录用户和匿名访客都能对自己拥有的文件生成总结（`require_media_access`），对别人的文件返回 404。
- 与旧播放页的区别：不再随播放器当前显示的字幕轨道切换总结语言。

`/subtitles/...` 字幕转换路由、`/api/ai_summaries*` 扩展接口和 `ai_summary_worker.py` 保持不变（Chrome 扩展仍在用）。

删除的文件：`templates/player.html`、`templates/audio_player.html`、`templates/_audio_controller.html`、`static/player.css`、`static/refresh.css`、`static/media-library.js`、`static/background-playback.js`。`static/style.css` 也已删除，关于／条款／隐私页改用 `dropload.css`。

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
- `static/dropload.css`：纯 CSS 实现设计稿的间距、配色与断点（640px / 1024px），下载器与媒体库共用，关于／条款／隐私页也共用。
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
