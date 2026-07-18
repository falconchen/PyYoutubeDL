#!/bin/bash
# PyYoutubeDL systemd 服务安装脚本
# 运行此脚本前请先 cd 到 PyYoutubeDL 项目目录

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SERVICE_NAME="pyyoutubedl"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "项目目录: ${SCRIPT_DIR}"

# 检查必要文件是否存在
if [ ! -f "${SCRIPT_DIR}/runner.sh" ]; then
    echo "错误: 未找到 runner.sh，请确保在 PyYoutubeDL 目录下运行此脚本"
    exit 1
fi

if [ ! -f "${SCRIPT_DIR}/stop.py" ]; then
    echo "错误: 未找到 stop.py"
    exit 1
fi

# 生成 systemd 服务文件
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=PyYoutubeDL Service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/bin/bash ${SCRIPT_DIR}/runner.sh
ExecStop=${SCRIPT_DIR}/venv/bin/python ${SCRIPT_DIR}/stop.py
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "已生成服务文件: ${SERVICE_FILE}"

# 重载 systemd 并启用开机自启
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "服务 ${SERVICE_NAME} 已设置为开机自启"
echo ""
echo "常用命令:"
echo "  systemctl start  ${SERVICE_NAME}    # 启动服务"
echo "  systemctl stop   ${SERVICE_NAME}    # 停止服务"
echo "  systemctl status ${SERVICE_NAME}    # 查看状态"
echo "  journalctl -u    ${SERVICE_NAME} -f # 查看日志"
