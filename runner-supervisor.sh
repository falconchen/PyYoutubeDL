#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
SUPERVISORCTL_BIN="/usr/bin/supervisorctl"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "错误: 找不到项目虚拟环境中的 Python: $PYTHON_BIN" >&2
    exit 1
fi

if [ ! -x "$SUPERVISORCTL_BIN" ]; then
    echo "错误: 找不到 supervisorctl: $SUPERVISORCTL_BIN" >&2
    exit 1
fi

echo "正在更新 pip..."
"$PYTHON_BIN" -m pip install --upgrade pip

echo "正在更新 yt-dlp..."
"$PYTHON_BIN" -m pip install --upgrade yt-dlp

echo "正在通过 Supervisor 重启 PyYoutubeDL 服务..."
"$SUPERVISORCTL_BIN" restart \
    pyyoutubedl-app \
    pyyoutubedl-downloader \
    pyyoutubedl-ai-summary \
    pyyoutubedl-webdav

echo "PyYoutubeDL 依赖更新及服务重启完成。"
