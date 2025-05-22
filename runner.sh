#!/bin/bash
./stop.py
nohup ./downloader.py >/dev/null 2>&1 &
nohup ./webdav_uploader.py >/dev/null 2>&1 &

# 检查 devil 命令是否存在
if ! command -v devil >/dev/null 2>&1; then
    nohup ./app.py >/dev/null 2>&1 &
fi

