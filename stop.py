#!venv/bin/python
import os
import sys
import psutil

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

if __name__ == '__main__':
    kill_existing_processes()
    print("All related processes have been terminated.") 