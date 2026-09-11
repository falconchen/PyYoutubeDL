#!/bin/bash
# 把 PyYoutubeDL 从 systemd 切换到 Supervisor。幂等，可重复执行。
#
#   ./deploy/supervisor/install.sh --dry-run          只打印将要执行的命令
#   ./deploy/supervisor/install.sh --render-only PATH 只渲染 conf 到指定路径
#   ./deploy/supervisor/install.sh --force            忽略历史遗留的任务标记
#   ./deploy/supervisor/install.sh                    实际执行切换
#
# 切换细节见 docs/supervisor-migration.md。

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)
TEMPLATE="$SCRIPT_DIR/pyyoutubedl.conf"
TARGET_CONF="${TARGET_CONF:-/etc/supervisor/conf.d/pyyoutubedl.conf}"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN:-/usr/bin/supervisorctl}"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
SYSTEMD_UNIT="${SYSTEMD_UNIT:-pyyoutubedl}"
PROGRAMS=(
    pyyoutubedl-app
    pyyoutubedl-downloader
    pyyoutubedl-ai-summary
    pyyoutubedl-webdav
    pyyoutubedl-playlist-monitor
)

DRY_RUN=false
FORCE=false

die() { echo "错误: $*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" = 'true' ]; then
        echo "  [dry-run] $*"
    else
        echo "  + $*"
        "$@"
    fi
}

render_conf() {
    local destination="$1"
    [ -f "$TEMPLATE" ] || die "找不到配置模板: $TEMPLATE"
    # PROJECT_DIR 是绝对路径且不含 | ，可安全用作 sed 分隔符
    sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$TEMPLATE" > "$destination"
}

check_prerequisites() {
    [ -x "$PYTHON_BIN" ] || die "找不到项目虚拟环境 Python: $PYTHON_BIN"
    [ -d "$PROJECT_DIR/.git" ] || die "项目目录不是 Git checkout: $PROJECT_DIR"
    [ -x "$SCRIPT_DIR/service-guard.sh" ] || die "service-guard.sh 不可执行: $SCRIPT_DIR/service-guard.sh"
}

# 有进行中的下载/上传时拒绝切换，避免任务被打断后留下 .downloading / .uploading 残骸
# （RESUME_INTERRUPTED_DOWNLOADS 默认 false，不会自动续传）。
# 判定逻辑与 deploy/remote-deploy.sh 的 active_tasks() 一致。
#
# 注意标记文件也可能是历史遗留：进程早已退出但文件没清掉。这种情况下用 --force 跳过。
check_no_active_tasks() {
    local urls_dir files_dir markers=()

    urls_dir=$("$PYTHON_BIN" -c 'from config_util import load_config; print(load_config()["URLS_DIR"])')
    files_dir=$("$PYTHON_BIN" -c 'from config_util import load_config; print(load_config()["FILES_DIR"])')

    while IFS= read -r marker; do
        [ -n "$marker" ] && markers+=("$marker")
    done < <(
        find "$urls_dir" -maxdepth 1 -type f -name '*.downloading' 2>/dev/null
        find "$files_dir" -maxdepth 1 -type f -name '*.uploading' 2>/dev/null
    )

    if [ "${#markers[@]}" -eq 0 ]; then
        echo "  无进行中的下载或上传任务。"
        return 0
    fi

    echo "  检测到 ${#markers[@]} 个进行中的任务标记:"
    printf '    %s\n' "${markers[@]}"

    if [ "$FORCE" = 'true' ]; then
        echo "  --force 已指定，继续切换。"
        return 0
    fi
    if [ "$DRY_RUN" = 'true' ]; then
        echo "  （dry-run：实际执行时此处会中止，确认是历史残留后可用 --force 跳过）"
        return 0
    fi

    die "存在进行中的任务，请等待完成后再切换；确认是历史残留可加 --force。"
}

install_supervisor() {
    if [ -x "$SUPERVISORCTL_BIN" ]; then
        echo "  supervisor 已安装: $SUPERVISORCTL_BIN"
        return 0
    fi
    # 必须用 apt 安装：supervisor-runner.sh 默认 supervisorctl 位于 /usr/bin，
    # pip 安装会落在 /usr/local/bin 从而不匹配。
    run apt-get update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y supervisor
}

stop_systemd() {
    if ! systemctl list-unit-files "${SYSTEMD_UNIT}.service" --no-legend 2>/dev/null | grep -q .; then
        echo "  未找到 ${SYSTEMD_UNIT}.service，跳过。"
        return 0
    fi
    # 必须 disable，否则下次开机 systemd 与 supervisor 会各拉起一套进程。
    run systemctl stop "$SYSTEMD_UNIT"
    run systemctl disable "$SYSTEMD_UNIT"
    # 兜底清理 nohup 遗留进程及其 yt-dlp 子进程
    run "$PYTHON_BIN" "$PROJECT_DIR/stop.py"
}

deploy_conf() {
    local target_dir
    target_dir=$(dirname "$TARGET_CONF")
    run mkdir -p "$target_dir"
    run mkdir -p "$PROJECT_DIR/logs/supervisor"

    if [ "$DRY_RUN" = 'true' ]; then
        echo "  [dry-run] 渲染 $TEMPLATE -> $TARGET_CONF（PROJECT_DIR=$PROJECT_DIR）"
        return 0
    fi
    echo "  + 渲染 $TEMPLATE -> $TARGET_CONF"
    render_conf "$TARGET_CONF"
    chmod 644 "$TARGET_CONF"
}

start_supervisor() {
    run "$SUPERVISORCTL_BIN" reread
    run "$SUPERVISORCTL_BIN" update
    run "$SUPERVISORCTL_BIN" start "${PROGRAMS[@]}"
    run "$SUPERVISORCTL_BIN" status "${PROGRAMS[@]}"
}

main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dry-run)
                DRY_RUN=true
                ;;
            --force)
                FORCE=true
                ;;
            --render-only)
                [ "$#" -eq 2 ] || die "--render-only 需要一个输出路径"
                render_conf "$2"
                echo "已渲染到: $2"
                return 0
                ;;
            -h|--help)
                sed -n '2,10p' "${BASH_SOURCE[0]}"
                return 0
                ;;
            *)
                die "未知参数: $1"
                ;;
        esac
        shift
    done

    cd "$PROJECT_DIR" || die "无法进入项目目录: $PROJECT_DIR"

    echo "项目目录: $PROJECT_DIR"
    if [ "$DRY_RUN" = 'true' ]; then
        echo "（dry-run：不会做任何变更）"
    fi

    echo "[1/6] 检查前置条件"
    check_prerequisites
    echo "[2/6] 检查进行中的任务"
    check_no_active_tasks
    echo "[3/6] 安装 supervisor"
    install_supervisor
    echo "[4/6] 停止并禁用 systemd 单元"
    stop_systemd
    echo "[5/6] 部署 program 配置"
    deploy_conf
    echo "[6/6] 启动 supervisor program"
    start_supervisor

    echo
    if [ "$DRY_RUN" = 'true' ]; then
        echo "dry-run 结束，未做任何变更。"
        return 0
    fi
    echo "切换完成。日常操作请改用 ./supervisor-runner.sh，不要再用 runner.sh / stop.py。"
    echo "回滚步骤见 docs/supervisor-migration.md。"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
