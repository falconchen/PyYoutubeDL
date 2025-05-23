#!/bin/bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# 检查并激活虚拟环境
if [ -d "$SCRIPT_DIR/venv" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
    echo "已激活虚拟环境"
fi

cd ${SCRIPT_DIR}
echo "正在更新yt-dlp..."
pip install --upgrade yt-dlp

echo "正在停止已有进程..."
python ./stop.py

echo "正在启动下载器..."
nohup ./downloader.py >/dev/null 2>&1 &

echo "正在启动上传器..."
nohup ./webdav_uploader.py >/dev/null 2>&1 &

# 检查 devil 命令是否存在
if ! command -v devil >/dev/null 2>&1; then
    echo "未检测到devil命令，使用python方式启动Web应用..."
    nohup ./app.py >/dev/null 2>&1 &
fi

echo "所有服务已启动完成！"

