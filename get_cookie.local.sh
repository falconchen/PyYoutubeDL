#!/bin/bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd $SCRIPT_DIR
# 加载 .env 文件中的环境变量
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "错误: .env 文件不存在"
    exit 1
fi

# 检查必要的环境变量
if [ -z "$AUTH_USERNAME" ] || [ -z "$AUTH_PASSWORD" ]; then
    echo "错误: 未找到认证凭据 (AUTH_USERNAME 或 AUTH_PASSWORD)"
    exit 1
fi

# 设置API地址
API_URL="http://localhost:5001/cookies/mozilla?format=text"

# 使用curl获取cookie并保存到文件
echo "正在获取YouTube cookie..."
curl -s -u "$AUTH_USERNAME:$AUTH_PASSWORD" "$API_URL" > /etc/youtube-cookie.txt

# 检查是否成功
if [ $? -eq 0 ]; then
    echo "Cookie已成功保存到 /etc/youtube-cookie.txt"
    # 显示文件内容的前几行
    echo "文件内容预览:"
    head -n 10 /etc/youtube-cookie.txt
else
    echo "错误: 获取cookie失败" 
    exit 1
fi 