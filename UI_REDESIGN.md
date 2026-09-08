# 统一媒体库 UI

首页与播放页采用暖灰背景、深绿操作按钮和卡片布局。首页的「打开媒体库」进入 `/player`，视频／音频标签位于播放列表顶部，替代原列表标题，以紧凑样式切换播放器及对应播放列表；桌面双栏，小屏单栏。

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
