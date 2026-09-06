#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN:-/usr/bin/supervisorctl}"
SUPERVISOR_PROGRAMS=()

show_usage() {
    echo "用法: $0 [start|stop|restart|status] [app|downloader|ai|webdav|playlist]"
    echo "       $0 -l|--list"
    echo "不提供操作时默认执行 restart；不提供服务时默认操作全部服务。"
}

show_services() {
    echo "可用服务:"
    echo "  app         pyyoutubedl-app"
    echo "  downloader  pyyoutubedl-downloader"
    echo "  ai          pyyoutubedl-ai-summary"
    echo "  webdav      pyyoutubedl-webdav"
    echo "  playlist    pyyoutubedl-playlist-monitor"
}

select_programs() {
    local service="$1"

    case "$service" in
        all)
            SUPERVISOR_PROGRAMS=(
                pyyoutubedl-app
                pyyoutubedl-downloader
                pyyoutubedl-ai-summary
                pyyoutubedl-webdav
                pyyoutubedl-playlist-monitor
            )
            ;;
        app)
            SUPERVISOR_PROGRAMS=(pyyoutubedl-app)
            ;;
        downloader)
            SUPERVISOR_PROGRAMS=(pyyoutubedl-downloader)
            ;;
        ai)
            SUPERVISOR_PROGRAMS=(pyyoutubedl-ai-summary)
            ;;
        webdav)
            SUPERVISOR_PROGRAMS=(pyyoutubedl-webdav)
            ;;
        playlist)
            SUPERVISOR_PROGRAMS=(pyyoutubedl-playlist-monitor)
            ;;
        *)
            return 1
            ;;
    esac
}

update_dependencies() {
    if [ "${PYTUBEDL_UPDATE_DEPS:-0}" != "1" ]; then
        return 0
    fi

    echo "正在按 requirements.txt 安装依赖..."
    "$PYTHON_BIN" -m pip install -r "$SCRIPT_DIR/requirements.txt"
}

run_supervisor_action() {
    local action="$1"
    local service="$2"

    select_programs "$service"
    echo "正在通过 Supervisor 执行 $action: $service"
    "$SUPERVISORCTL_BIN" "$action" "${SUPERVISOR_PROGRAMS[@]}"
}

main() {
    local action
    local service

    if [ "$#" -gt 2 ]; then
        show_usage
        return 2
    fi

    if [ "$#" -eq 1 ] && { [ "$1" = "-l" ] || [ "$1" = "--list" ]; }; then
        show_services
        return 0
    fi

    action="${1:-restart}"
    case "$action" in
        start|stop|restart|status)
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

    service="${2:-all}"
    if ! select_programs "$service"; then
        echo "无效服务: $service"
        show_usage
        return 2
    fi

    if [ ! -x "$PYTHON_BIN" ]; then
        echo "错误: 找不到项目虚拟环境中的 Python: $PYTHON_BIN" >&2
        return 1
    fi

    if [ ! -x "$SUPERVISORCTL_BIN" ]; then
        echo "错误: 找不到 supervisorctl: $SUPERVISORCTL_BIN" >&2
        return 1
    fi

    cd "$SCRIPT_DIR" || return 1

    case "$action" in
        start|restart)
            update_dependencies
            run_supervisor_action "$action" "$service"
            ;;
        stop|status)
            run_supervisor_action "$action" "$service"
            ;;
    esac

    echo "Supervisor 操作完成: $action $service"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
