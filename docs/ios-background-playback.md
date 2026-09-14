# iOS 锁屏播放

音频播放页在 iPhone / iPad 上不建立 Web Audio 频谱管线，保留媒体元素的原生声音输出，避免 AudioContext 后台挂起造成无声。桌面频谱保持原有行为。支持 Audio Session API 时申请 `playback`，支持 Media Session API 时提供标题、作者、封面、播放/暂停、快进/快退与进度控制。两个播放器共享会话，只允许当前播放器发声。

视频和音频使用各自的原生媒体元素；`?listen=1` 不会切换视频播放器模式。系统媒体控制由 Media Session API 提供，不会把视频转换成音频模式。媒体文件通过现有 `/files/` 接口和 Range 响应提供。没有静音保活、后台轮询或强制自动恢复播放。

## 验证

自动化检查：

```sh
python -m pytest tests/test_player.py tests/test_audio_player.py tests/test_media_library.py tests/test_background_playback.py -q
node --test tests/test_background_playback.js
git diff --check
```

实机验收（自动测试不能证明 iOS 锁屏行为）：

1. iPhone Safari 打开音频，点击播放，锁屏并保持至少两分钟；检查持续发声、控制中心标题、暂停/继续、快进和拖动。
2. 视频播放到中途，锁屏至少两分钟，确认系统媒体控制对应当前视频。
3. 解锁后切换视频，检查标题和进度；切换到音频标签后确认旧播放器停止、锁屏控制只控制当前播放器。
4. 检查播放结束后的自动下一首、耳机暂停和来电打断后的手动恢复。

网页无法申请原生 App 的后台运行权限。不同 iOS 版本、低电量模式、内嵌浏览器和系统回收策略可能中断播放或阻止后台自动下一首；Media Session 提供系统控制，不保证进程常驻。请优先在 Safari 实机验收。

参考：[WebKit AudioContext 后台问题](https://bugs.webkit.org/show_bug.cgi?id=261554)、[Safari 16.4 Audio Session 支持](https://webkit.org/blog/13966/webkit-features-in-safari-16-4/)、[Apple 原生媒体后台能力](https://developer.apple.com/documentation/avfoundation/configuring-your-app-for-media-playback)。
