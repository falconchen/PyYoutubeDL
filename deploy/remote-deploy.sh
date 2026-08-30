#!/bin/bash
set -Eeuo pipefail

PROJECT_DIR=${4:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)}
TARGET_COMMIT=${1:-}
SERVICE_MANAGER=${2:-}
HEALTH_URL=${3:-}
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
OLD_COMMIT=""
CHANGED=0

die() { echo "错误: $*" >&2; exit 1; }

[ -n "$TARGET_COMMIT" ] || die "缺少目标 commit。"
[ "$SERVICE_MANAGER" = systemd ] || [ "$SERVICE_MANAGER" = supervisor ] || die "不支持的服务管理器: $SERVICE_MANAGER"
[ -n "$HEALTH_URL" ] || die "缺少健康检查 URL。"
[ -d "$PROJECT_DIR/.git" ] || die "项目目录不是 Git checkout: $PROJECT_DIR"
[ -x "$PYTHON_BIN" ] || die "找不到虚拟环境 Python: $PYTHON_BIN"

cd "$PROJECT_DIR"
[ "$(git branch --show-current)" = master ] || die "远端必须位于 master 分支。"
[ -z "$(git status --porcelain)" ] || die "远端工作区不干净，拒绝发布。"

active_tasks() {
    local urls_dir files_dir
    urls_dir=$($PYTHON_BIN -c 'from config_util import load_config; print(load_config()["URLS_DIR"])')
    files_dir=$($PYTHON_BIN -c 'from config_util import load_config; print(load_config()["FILES_DIR"])')
    find "$urls_dir" -maxdepth 1 -type f -name '*.downloading' -print -quit | grep -q . && return 0
    find "$files_dir" -maxdepth 1 -type f -name '*.uploading' -print -quit | grep -q . && return 0
    return 1
}

active_tasks && die "检测到活动下载或上传任务，请稍后再发布。" || true

git fetch --quiet origin master
REMOTE_COMMIT=$(git rev-parse origin/master)
[ "$REMOTE_COMMIT" = "$TARGET_COMMIT" ] || die "origin/master=$REMOTE_COMMIT，与目标 commit=$TARGET_COMMIT 不一致。"
OLD_COMMIT=$(git rev-parse HEAD)
[ "$OLD_COMMIT" != "$TARGET_COMMIT" ] || { echo "已是目标版本: $TARGET_COMMIT"; exit 0; }

restart_services() {
    if [ "$SERVICE_MANAGER" = systemd ]; then
        systemctl restart pyyoutubedl
    else
        /usr/bin/supervisorctl restart \
            pyyoutubedl-app \
            pyyoutubedl-downloader \
            pyyoutubedl-ai-summary \
            pyyoutubedl-webdav
    fi
}

service_status() {
    local ai_enabled webdav_enabled
    ai_enabled=$($PYTHON_BIN -c 'from config_util import is_ai_summary_enabled, load_config; print(int(is_ai_summary_enabled(load_config())))')
    webdav_enabled=$($PYTHON_BIN -c 'from config_util import is_webdav_upload_enabled, load_config; print(int(is_webdav_upload_enabled(load_config())))')
    if [ "$SERVICE_MANAGER" = systemd ]; then
        systemctl is-active --quiet pyyoutubedl || return 1
        pgrep -f "$PROJECT_DIR/downloader.py" >/dev/null || return 1
        [ "$ai_enabled" -eq 0 ] || pgrep -f "$PROJECT_DIR/ai_summary_worker.py" >/dev/null || return 1
        [ "$webdav_enabled" -eq 0 ] || pgrep -f "$PROJECT_DIR/webdav_uploader.py" >/dev/null || return 1
    else
        /usr/bin/supervisorctl status \
            pyyoutubedl-app \
            pyyoutubedl-downloader | grep -qE 'RUNNING' || return 1
        [ "$ai_enabled" -eq 0 ] || /usr/bin/supervisorctl status pyyoutubedl-ai-summary | grep -qE 'RUNNING' || return 1
        [ "$webdav_enabled" -eq 0 ] || /usr/bin/supervisorctl status pyyoutubedl-webdav | grep -qE 'RUNNING' || return 1
    fi
}

wait_for_health() {
    local attempt=1
    while [ "$attempt" -le 30 ]; do
        if curl --fail --silent --show-error --max-time 3 "$HEALTH_URL" | "$PYTHON_BIN" -c \
            'import json, sys; data=json.load(sys.stdin); sys.exit(0 if data.get("status") == "ok" else 1)' \
            && service_status; then
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    return 1
}

rollback() {
    local status=$?
    if [ "$CHANGED" -eq 1 ]; then
        echo "发布失败，正在回滚到 $OLD_COMMIT..." >&2
        git merge --abort 2>/dev/null || true
        git reset --hard "$OLD_COMMIT" >/dev/null
        restart_services || true
        wait_for_health || true
    fi
    exit "$status"
}

trap rollback ERR
git merge --ff-only "$TARGET_COMMIT"
CHANGED=1
"$PYTHON_BIN" -m pip install -r requirements.txt
restart_services
wait_for_health
trap - ERR
echo "发布成功: $OLD_COMMIT -> $TARGET_COMMIT"
