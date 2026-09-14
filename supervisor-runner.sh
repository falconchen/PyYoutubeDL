#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN:-/usr/bin/supervisorctl}"
SUPERVISOR_PROGRAMS=()

show_usage() {
    echo "用法: $0 [start|stop|restart|status] [app|downloader|ai|webdav|playlist]"
    echo "       $0 -l|--list"
    echo "       $0 -u|--upgrade"
    echo "不提供操作时默认执行 restart；不提供服务时默认操作全部服务。"
}

show_services() {
    echo "可用服务:"
    echo "  app         dropload-app"
    echo "  downloader  dropload-downloader"
    echo "  ai          dropload-ai-summary"
    echo "  webdav      dropload-webdav"
    echo "  playlist    dropload-playlist-monitor"
}

select_programs() {
    local service="$1"

    case "$service" in
        all)
            SUPERVISOR_PROGRAMS=(
                dropload-app
                dropload-downloader
                dropload-ai-summary
                dropload-webdav
                dropload-playlist-monitor
            )
            ;;
        app)
            SUPERVISOR_PROGRAMS=(dropload-app)
            ;;
        downloader)
            SUPERVISOR_PROGRAMS=(dropload-downloader)
            ;;
        ai)
            SUPERVISOR_PROGRAMS=(dropload-ai-summary)
            ;;
        webdav)
            SUPERVISOR_PROGRAMS=(dropload-webdav)
            ;;
        playlist)
            SUPERVISOR_PROGRAMS=(dropload-playlist-monitor)
            ;;
        *)
            return 1
            ;;
    esac
}

upgrade_dependencies() {
    echo "正在升级 pip..."
    "$PYTHON_BIN" -m pip install --upgrade pip
    echo "正在升级 requirements.txt 中的依赖..."
    "$PYTHON_BIN" -m pip install --upgrade -r "$SCRIPT_DIR/requirements.txt"
}

update_dependencies() {
    if [ "${DROPLOAD_UPDATE_DEPS:-0}" != "1" ]; then
        return 0
    fi

    upgrade_dependencies
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
    local upgrade_requested=false

    if [ "$#" -gt 2 ]; then
        show_usage
        return 2
    fi

    if [ "$#" -eq 1 ] && { [ "$1" = "-l" ] || [ "$1" = "--list" ]; }; then
        show_services
        return 0
    fi

    if [ "$#" -eq 1 ] && { [ "$1" = "-u" ] || [ "$1" = "--upgrade" ]; }; then
        upgrade_requested=true
        action=restart
        service=all
    else
        action="${1:-restart}"
        service="${2:-all}"
    fi

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
            if [ "$upgrade_requested" = "true" ]; then
                upgrade_dependencies
            else
                update_dependencies
            fi
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
