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

### 自动重载
 Flask 提供了开发模式下的自动重载功能。有两种方式可以实现：
使用 Flask 的开发服务器：

```bash
export FLASK_APP=app.py && export FLASK_DEBUG=1 && flask run --host=0.0.0.0
```

## 配置项说明

在 `config.json` 中可以配置以下选项：

- `DELETE_AFTER_UPLOAD`: 布尔值，WebDAV 上传完成后是否删除原视频，默认为 `true`。

