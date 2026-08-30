#!/bin/bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

start_service() {
    local service_name="$1"
    local script_name="$2"
    local startup_log
    local python_bin
    local pid
    local exit_code
    local reason

    startup_log=$(mktemp "${TMPDIR:-/tmp}/pyyoutubedl-${script_name%.py}.XXXXXX") || {
        echo "正在启动${service_name}...（启动失败，原因: 无法创建临时日志文件）"
        return 1
    }

    python_bin="$PYTHON_BIN"
    [ -x "$python_bin" ] || python_bin=$(command -v python) || {
        echo "正在启动${service_name}...（启动失败，原因: 找不到Python解释器）"
        rm -f "$startup_log"
        return 1
    }

    printf "正在启动%s..." "$service_name"
    nohup "$python_bin" "$SCRIPT_DIR/$script_name" >"$startup_log" 2>&1 &
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

show_usage() {
    echo "用法: $0 [start|stop|restart]"
    echo "不提供参数时默认执行 restart。"
}

update_dependencies() {
    if [ "${PYTUBEDL_UPDATE_DEPS:-0}" != "1" ]; then
        return 0
    fi

    echo "正在按 requirements.txt 安装依赖..."
    if ! "$PYTHON_BIN" -m pip install -r "$SCRIPT_DIR/requirements.txt"; then
        echo "安装依赖失败，已中止启动。"
        return 1
    fi
}

stop_services() {
    local restart_devil="${1:-false}"
    local stop_args=()

    if [ "$restart_devil" = "true" ]; then
        stop_args+=(--restart-devil)
    fi

    echo "正在停止已有进程..."
    if ! "$PYTHON_BIN" ./stop.py "${stop_args[@]}"; then
        echo "停止已有进程失败。"
        return 1
    fi
}

webdav_upload_status() {
    "$PYTHON_BIN" -c '
import sys
from config_util import is_webdav_upload_enabled, load_config

sys.exit(0 if is_webdav_upload_enabled(load_config()) else 10)
'
}

ai_summary_status() {
    "$PYTHON_BIN" -c '
import sys
from config_util import is_ai_summary_enabled, load_config

sys.exit(0 if is_ai_summary_enabled(load_config()) else 10)
'
}

start_services() {
    local services_failed=0
    local webdav_status

    if ! command -v devil >/dev/null 2>&1; then
        echo "未检测到devil命令，使用python方式启动Web应用..."
        start_service "Web应用" "app.py" || services_failed=1
    else
        echo "检测到devil命令，Web应用由Devil管理。"
    fi

    start_service "下载器" "downloader.py" || services_failed=1
    if ai_summary_status; then
        start_service "AI总结Worker" "ai_summary_worker.py" || services_failed=1
    else
        ai_summary_status_code=$?
        if [ "$ai_summary_status_code" -eq 10 ]; then
            echo "AI总结尚未配置，已跳过启动AI总结Worker。"
        else
            echo "无法读取AI总结配置，已跳过启动AI总结Worker。"
            services_failed=1
        fi
    fi
    if webdav_upload_status; then
        start_service "上传器" "webdav_uploader.py" || services_failed=1
    else
        webdav_status=$?
        if [ "$webdav_status" -eq 10 ]; then
            echo "WebDAV上传已关闭，已跳过启动上传器。"
        else
            echo "无法读取WebDAV上传配置，已跳过启动上传器。"
            services_failed=1
        fi
    fi

    if [ "$services_failed" -ne 0 ]; then
        echo "部分服务启动失败，请根据上述原因检查配置或日志。"
        return 1
    fi

    echo "所有已启用的服务启动完成！"
}

main() {
    local action

    if [ "$#" -gt 1 ]; then
        show_usage
        return 2
    fi

    action="${1:-restart}"
    case "$action" in
        start|stop|restart)
            ;;
        -h|--help)
            show_usage
            return 0
            ;;
        *)
            echo "无效操作: $action"
            show_usage
            return 2
            ;;
    esac

    # 优先固定使用项目虚拟环境，避免依赖远程机器 PATH。
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
    if [ -d "$SCRIPT_DIR/venv" ]; then
        echo "使用项目虚拟环境: $PYTHON_BIN"
    else
        PYTHON_BIN=$(command -v python) || {
            echo "错误: 未找到项目虚拟环境或 Python 解释器。" >&2
            return 1
        }
    fi

    cd "$SCRIPT_DIR" || return 1

    case "$action" in
        start)
            update_dependencies && start_services
            ;;
        stop)
            stop_services
            ;;
        restart)
            update_dependencies && stop_services true && start_services
            ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
