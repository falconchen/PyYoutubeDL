#!/bin/bash

# 自动获取脚本所在目录作为工作目录
WORK_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# 激活虚拟环境
source "$WORK_DIR/venv/bin/activate"

# 切换到工作目录
cd "$WORK_DIR"

# 设置环境变量
export FLASK_APP=app.py
export FLASK_ENV=production

# 执行 Flask 命令
flask get-cookie 2>> "$WORK_DIR/cookie_update_error.log"

# 记录执行时间到日志
echo "$(date '+%Y-%m-%d %H:%M:%S') - Cookie update completed" >> "$WORK_DIR/cookie_update.log" 