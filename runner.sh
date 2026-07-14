#!/bin/bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

start_service() {
    local service_name="$1"
    local script_name="$2"
    local startup_log
    local pid
    local exit_code
    local reason

    startup_log=$(mktemp "${TMPDIR:-/tmp}/pyyoutubedl-${script_name%.py}.XXXXXX") || {
        echo "正在启动${service_name}...（启动失败，原因: 无法创建临时日志文件）"
        return 1
    }

    printf "正在启动%s..." "$service_name"
    nohup "$SCRIPT_DIR/$script_name" >"$startup_log" 2>&1 &
    pid=$!

    # 捕获启动阶段立即退出的情况，同时避免长期阻塞启动脚本。
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "（启动成功）"
        rm -f "$startup_log"
        return 0
    fi

    wait "$pid" 2>/dev/null
    exit_code=$?
    reason=$(tail -n 1 "$startup_log" | tr -d '\r')
    if [ -z "$reason" ]; then
        reason="进程已退出，退出码: $exit_code"
    fi

    echo "（启动失败，原因: $reason）"
    rm -f "$startup_log"
    return 1
}

# 检查并激活虚拟环境
if [ -d "$SCRIPT_DIR/venv" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
    echo "已激活虚拟环境"
fi

cd "$SCRIPT_DIR" || exit 1
echo "正在更新pip..."
pip install --upgrade pip

echo "正在更新yt-dlp..."
pip install --upgrade yt-dlp

echo "正在停止已有进程..."
if ! python ./stop.py --restart-devil; then
    echo "停止已有进程失败，已中止启动。"
    exit 1
fi

# 检查 devil 命令是否存在
if ! command -v devil >/dev/null 2>&1; then
    echo "未检测到devil命令，使用python方式启动Web应用..."
    nohup ./app.py >/dev/null 2>&1 &
fi

services_failed=0
start_service "下载器" "downloader.py" || services_failed=1
start_service "上传器" "webdav_uploader.py" || services_failed=1

if [ "$services_failed" -eq 0 ]; then
    echo "所有服务已启动完成！"
else
    echo "部分服务启动失败，请根据上述原因检查配置或日志。"
    exit 1
fi
