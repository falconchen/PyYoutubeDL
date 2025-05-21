#!/usr/bin/env python3
import os
import sys
import subprocess
import signal
import time
from datetime import datetime


def get_venv_python():
    """获取虚拟环境中的Python解释器路径"""
    if sys.prefix != sys.base_prefix:
        # 已经在虚拟环境中
        return sys.executable
    else:
        # 尝试在当前目录下查找虚拟环境
        venv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'venv')
        if os.path.exists(venv_path):
            if os.name == 'nt':  # Windows
                return os.path.join(venv_path, 'Scripts', 'python.exe')
            else:  # Linux/Mac
                return os.path.join(venv_path, 'bin', 'python')
    return None

def restart_in_venv():
    """如果不在虚拟环境中，使用虚拟环境的Python重新启动自己"""
    if sys.prefix == sys.base_prefix:
        venv_python = get_venv_python()
        if venv_python:
            print("正在切换到虚拟环境...")
            os.execv(venv_python, [venv_python, __file__] + sys.argv[1:])
        else:
            print("错误：未找到虚拟环境，请先创建虚拟环境")
            sys.exit(1)

def has_devil():
    """判断devil命令是否存在于PATH中"""
    from shutil import which
    return which('devil') is not None

def get_domain_from_path():
    """从当前脚本路径中提取domains/后的目录名作为域名"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    parts = base_dir.split(os.sep)
    if 'domains' in parts:
        idx = parts.index('domains')
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None

def start_processes():
    """启动下载器、上传器和Web应用进程"""
    # 确保在虚拟环境中运行
    restart_in_venv()

    # 获取脚本所在目录
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 设置进程列表
    processes = []
    scripts = [
        ('downloader.py', '下载器'),
        ('webdav_uploader.py', '上传器')
    ]

    try:
        # 启动所有进程
        for script, name in scripts:
            script_path = os.path.join(base_dir, script)
            print(f"正在启动{name}...")
            process = subprocess.Popen(
                [sys.executable, script_path],  # 使用当前Python解释器
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            processes.append((process, name))
            print(f"{name}已启动，PID: {process.pid}")

        # 启动app.py
        if has_devil():
            domain = get_domain_from_path()
            if not domain:
                print("未能自动识别域名目录，devil命令启动失败！")
                sys.exit(1)
            print(f"检测到devil命令，使用devil方式启动Web应用({domain})...")
            app_process = subprocess.Popen(
                ['devil', 'www', 'restart', domain],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            processes.append((app_process, f'Web应用(devil:{domain})'))
            print(f"Web应用(devil:{domain})已启动，PID: {app_process.pid}")
        else:
            script_path = os.path.join(base_dir, 'app.py')
            print("未检测到devil命令，使用python方式启动Web应用...")
            app_process = subprocess.Popen(
                [sys.executable, script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            processes.append((app_process, 'Web应用'))
            print(f"Web应用已启动，PID: {app_process.pid}")

        print("\n所有服务已启动，按 Ctrl+C 停止所有服务\n")

        # 监控进程输出
        while True:
            for process, name in processes:
                # devil方式启动的Web应用不监控输出
                if 'devil:' in name:
                    continue
                # 读取输出
                output = process.stdout.readline()
                if output:
                    print(f"[{name}] {output.strip()}")
                
                # 检查进程是否还在运行
                if process.poll() is not None:
                    print(f"\n{name}已停止运行，退出码: {process.returncode}")
                    # 停止所有其他进程
                    for p, n in processes:
                        if p.poll() is None:
                            p.terminate()
                    return

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n正在停止所有服务...")
        # 停止所有进程
        for process, name in processes:
            if process.poll() is None:
                process.terminate()
                print(f"已停止{name}")
        
        # 等待进程结束
        for process, name in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                print(f"强制终止{name}")

if __name__ == '__main__':
    start_processes() 