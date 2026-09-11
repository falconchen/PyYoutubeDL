#!/bin/bash
# Supervisor 可选服务守卫。
#
# Supervisor 没有条件启动能力，而 ai / webdav / playlist 三个 worker 受 config.json
# 开关控制（与 runner.sh 中的判断一致）。本脚本先判定服务是否启用：
#   启用   -> exec 对应 worker，supervisor 托管的 PID 就是 worker 本身
#   未配置 -> 退出码 10
#   已关闭 -> 退出码 11
#   读配置失败 -> 退出码 1（交由 supervisor 当作异常重启）
#
# 退出前的 sleep 是必需的：supervisor 的 exitcodes/autorestart=unexpected 只在进程
# RUNNING 满 startsecs 之后才生效。若守卫瞬间退出，supervisor 会判定为启动失败并
# 重试至 FATAL。睡过 startsecs 再退出，状态才会干净地停在 EXITED。

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/venv/bin/python}"
GUARD_SETTLE_SECS="${GUARD_SETTLE_SECS:-6}"

show_usage() {
    echo "用法: $0 [--check-only] <ai|webdav|playlist>"
    echo "  --check-only  只判定是否启用并返回退出码，不启动 worker"
}

service_script() {
    case "$1" in
        ai) echo 'ai_summary_worker.py' ;;
        webdav) echo 'webdav_uploader.py' ;;
        playlist) echo 'playlist_monitor.py' ;;
        *) return 1 ;;
    esac
}

# 返回 0=启用 10=未配置 11=开关关闭 1=读取失败
check_enabled() {
    local service="$1"

    "$PYTHON_BIN" - "$service" <<'PY'
import sys

from config_util import (
    is_ai_summary_enabled,
    is_playlist_monitor_enabled,
    is_playlist_monitor_switch_on,
    is_webdav_upload_enabled,
    load_config,
)

service = sys.argv[1]
config = load_config()

if service == 'ai':
    sys.exit(0 if is_ai_summary_enabled(config) else 10)
if service == 'webdav':
    sys.exit(0 if is_webdav_upload_enabled(config) else 10)
if service == 'playlist':
    if not is_playlist_monitor_switch_on(config):
        sys.exit(11)
    sys.exit(0 if is_playlist_monitor_enabled(config) else 10)

print(f'未知服务: {service}', file=sys.stderr)
sys.exit(1)
PY
}

disabled_reason() {
    case "$1:$2" in
        ai:10) echo 'AI总结尚未配置，已跳过启动AI总结Worker。' ;;
        webdav:10) echo 'WebDAV上传已关闭，已跳过启动上传器。' ;;
        playlist:11) echo '播放列表监控已关闭，已跳过启动播放列表监控。' ;;
        playlist:10) echo 'OAuth 或播放列表尚未配置，已跳过启动播放列表监控。' ;;
        *) echo "服务 $1 未启用（判定码 $2）。" ;;
    esac
}

main() {
    local check_only=false
    local service
    local script
    local status

    if [ "${1:-}" = '--check-only' ]; then
        check_only=true
        shift
    fi

    if [ "$#" -ne 1 ] || [ "${1:-}" = '-h' ] || [ "${1:-}" = '--help' ]; then
        show_usage
        return 2
    fi

    service="$1"
    if ! script=$(service_script "$service"); then
        echo "无效服务: $service" >&2
        show_usage
        return 2
    fi

    if [ ! -x "$PYTHON_BIN" ]; then
        echo "错误: 找不到项目虚拟环境中的 Python: $PYTHON_BIN" >&2
        return 1
    fi

    cd "$PROJECT_DIR" || return 1

    check_enabled "$service"
    status=$?

    case "$status" in
        0)
            if [ "$check_only" = 'true' ]; then
                echo "服务 $service 已启用。"
                return 0
            fi
            exec "$PYTHON_BIN" "$PROJECT_DIR/$script"
            ;;
        10|11)
            disabled_reason "$service" "$status"
            [ "$check_only" = 'true' ] || sleep "$GUARD_SETTLE_SECS"
            return "$status"
            ;;
        *)
            echo "无法读取 $service 配置，已跳过启动。" >&2
            return 1
            ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
