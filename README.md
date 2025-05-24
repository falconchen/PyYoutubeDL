# PyYoutubeDL

## 简介

PyYoutubeDL 是一个基于 yt-dlp 的 Python 库，用于下载 YouTube 视频。

## 安装

```bash
pip install -r requirements.txt
./runner.sh
```

## 导出浏览器 cookies

PS：`-vU` 用于调试

```bash
yt-dlp -vU --cookies-from-browser firefox --cookies firefox-cookie.txt

yt-dlp -vU --cookies-from-browser chrome --cookies chrome-cookie.txt

yt-dlp -vU --cookies-from-browser edge --cookies edge-cookie.txt
```

## 或直接使用cookies文件

```bash
yt-dlp --cookies-from-browser firefox https://www.youtube.com/shorts/gD2iMAzW918
yt-dlp --cookies firefox-cookie.txt https://www.youtube.com/shorts/gD2iMAzW918
```

### 使用 yt-dlp lib

``` python
import yt_dlp

url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
ydl_opts = {}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([url])
```
