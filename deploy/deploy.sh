#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
PROJECT_DIR=$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)
CONFIG_FILE="$SCRIPT_DIR/targets.conf"
LOCK_DIR="${TMPDIR:-/tmp}/pyyoutubedl-deploy.lock"

die() { echo "错误: $*" >&2; exit 1; }
[ -f "$CONFIG_FILE" ] || die "未找到 $CONFIG_FILE，请复制 targets.conf.example 并填写。"
# shellcheck disable=SC1090
source "$CONFIG_FILE"
[ "${#DEPLOY_TARGETS[@]}" -gt 0 ] || die "DEPLOY_TARGETS 不能为空。"

cd "$PROJECT_DIR"
[ "$(git branch --show-current)" = master ] || die "发布必须从 master 分支执行。"
[ -z "$(git status --porcelain)" ] || die "本机工作区不干净，请先提交或清理变更。"
git fetch --quiet origin master
git diff --quiet HEAD origin/master || die "本机 master 与 origin/master 不一致，请先同步。"
git push origin master
TARGET_COMMIT=$(git rev-parse HEAD)

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    die "已有另一个发布任务运行，或遗留锁目录: $LOCK_DIR"
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "开始发布 commit: $TARGET_COMMIT"
for target in "${DEPLOY_TARGETS[@]}"; do
    IFS='|' read -r name ssh_host project_dir service_manager health_url <<< "$target"
    [ -n "$name" ] && [ -n "$ssh_host" ] && [ -n "$project_dir" ] && [ -n "$service_manager" ] && [ -n "$health_url" ] \
        || die "目标配置格式错误: $target"
    echo "[$name] 发布到 $ssh_host:$project_dir ($service_manager)"
    ssh -- "$ssh_host" "cd '$project_dir' && bash -s -- '$TARGET_COMMIT' '$service_manager' '$health_url' '$project_dir'" \
        < "$SCRIPT_DIR/remote-deploy.sh" \
        || die "[$name] 发布失败；按串行失败即停策略，不继续后续目标。"
    echo "[$name] 发布完成"
done
echo "全部目标发布完成: $TARGET_COMMIT"
