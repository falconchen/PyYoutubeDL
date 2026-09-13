#!/usr/bin/env python3
"""容器内多进程入口：启动服务并将停止信号转发给所有子进程。"""

import signal
import subprocess
import sys
import time

from config_util import load_config
from start import get_service_scripts


STOP_TIMEOUT_SECONDS = 10


def stop_processes(processes):
    """先优雅终止全部子进程，超时后再强制结束。"""
    for process, _name in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    for process, _name in processes:
        remaining = max(0, deadline - time.monotonic())
        if process.poll() is None:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()

    for process, _name in processes:
        if process.poll() is None:
            process.wait()


def main():
    runtime_config = load_config()
    services = get_service_scripts(runtime_config)
    services.append(("app.py", "Web应用"))
    processes = []
    stopping = False

    def request_stop(signum, _frame):
        nonlocal stopping
        print(f"收到信号 {signum}，正在停止全部服务……", flush=True)
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        for script, name in services:
            process = subprocess.Popen([sys.executable, script])
            processes.append((process, name))
            print(f"{name}已启动，PID: {process.pid}", flush=True)

        while not stopping:
            for process, name in processes:
                returncode = process.poll()
                if returncode is not None:
                    print(
                        f"{name}意外退出，退出码: {returncode}；正在停止其他服务。",
                        file=sys.stderr,
                        flush=True,
                    )
                    return returncode if returncode != 0 else 1
            time.sleep(0.5)
        return 0
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
