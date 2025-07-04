#!/usr/bin/env python
import os
import sys
import psutil
import subprocess

def kill_existing_processes():
    """终止已存在的相关进程，包括start.py自身"""
    current_pid = os.getpid()
    killed = set()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.pid == current_pid:
                continue
            cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
            # 只杀死python相关的目标脚本进程
            if (
                ('downloader.py' in cmdline) or
                ('webdav_uploader.py' in cmdline) or
                ('start.py' in cmdline) or
                ('app.py' in cmdline)
            ):
                print(f"Terminating PID {proc.pid}: {cmdline}")
                proc.terminate()
                killed.add(proc.pid)
        except Exception:
            continue
    # 等待进程结束
    gone, alive = psutil.wait_procs([psutil.Process(pid) for pid in killed if psutil.pid_exists(pid)], timeout=5)
    for p in alive:
        try:
            print(f"Killing PID {p.pid}")
            p.kill()
        except Exception:
            pass

def has_devil():
    from shutil import which
    return which('devil') is not None

def get_domain_from_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    parts = base_dir.split(os.sep)
    if 'domains' in parts:
        idx = parts.index('domains')
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None

if __name__ == '__main__':
    kill_existing_processes()
    print("All related processes have been terminated.")

    # 检查是否需要重启 devil
    if has_devil():
        domain = get_domain_from_path()
        if domain:
            print(f"检测到devil命令，重启Web应用({domain})...")
            subprocess.run(['devil', 'www', 'restart', domain])
        else:
            print("未能自动识别域名目录，devil命令重启失败！") 