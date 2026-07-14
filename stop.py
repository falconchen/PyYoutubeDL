#!/usr/bin/env python
import argparse
import os
import subprocess
import sys

import psutil


BASE_DIR = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
TARGET_SCRIPT_NAMES = {
    'app.py',
    'downloader.py',
    'start.py',
    'webdav_uploader.py',
}
PROCESS_TIMEOUT = 5


def get_executed_script(cmdline):
    """从命令行中提取实际执行的 Python 脚本参数。"""
    if not cmdline:
        return None

    executable = cmdline[0]
    if os.path.basename(executable) in TARGET_SCRIPT_NAMES:
        return executable

    executable_name = os.path.basename(executable).lower()
    if not (executable_name.startswith('python') or executable_name.startswith('pypy')):
        return None

    index = 1
    while index < len(cmdline):
        argument = cmdline[index]
        if argument in ('-c', '-m'):
            return None
        if argument == '--':
            return cmdline[index + 1] if index + 1 < len(cmdline) else None
        if argument in ('-W', '-X'):
            index += 2
            continue
        if argument.startswith('-'):
            index += 1
            continue
        return argument

    return None


def command_targets_project(cmdline, cwd, base_dir=BASE_DIR):
    """判断命令行是否明确执行本项目中的目标脚本。"""
    script_argument = get_executed_script(cmdline)
    if not script_argument:
        return False

    target_paths = {
        os.path.realpath(os.path.join(base_dir, name))
        for name in TARGET_SCRIPT_NAMES
    }
    if os.path.basename(script_argument) not in TARGET_SCRIPT_NAMES:
        return False
    if os.path.isabs(script_argument):
        script_path = os.path.realpath(script_argument)
    elif cwd:
        script_path = os.path.realpath(os.path.join(cwd, script_argument))
    else:
        return False
    return script_path in target_paths


def describe_process(proc):
    """返回用于日志的进程命令行，不因进程消失而抛出异常。"""
    try:
        cmdline = proc.cmdline()
        if cmdline:
            return ' '.join(cmdline)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return f"PID {proc.pid}"


def find_target_processes():
    """查找本项目服务进程，并包含它们派生的全部子进程。"""
    current_pid = os.getpid()
    roots = []

    try:
        for proc in psutil.process_iter(['pid', 'cmdline', 'cwd'], ad_value=None):
            if proc.pid == current_pid:
                continue
            if command_targets_project(
                proc.info.get('cmdline'),
                proc.info.get('cwd'),
            ):
                roots.append(proc)
    except (psutil.Error, OSError) as exc:
        return [], [f"无法读取系统进程列表: {exc}"]

    processes = {}
    errors = []
    for root in roots:
        processes[root.pid] = root
        try:
            for child in root.children(recursive=True):
                if child.pid != current_pid:
                    processes[child.pid] = child
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as exc:
            errors.append(f"无法读取 PID {root.pid} 的子进程: {exc}")

    # 子进程优先，避免父进程退出后留下 yt-dlp 等孤儿进程。
    root_pids = {proc.pid for proc in roots}
    ordered = [proc for pid, proc in processes.items() if pid not in root_pids]
    ordered.extend(proc for proc in roots if proc.pid in processes)
    return ordered, errors


def terminate_processes(processes, timeout=PROCESS_TIMEOUT):
    """先发送 SIGTERM，超时后发送 SIGKILL，并确认进程已经退出。"""
    waiting = []
    errors = []

    for proc in processes:
        try:
            print(f"正在终止 PID {proc.pid}: {describe_process(proc)}")
            proc.terminate()
            waiting.append(proc)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as exc:
            errors.append(f"无法终止 PID {proc.pid}: {exc}")

    if waiting:
        _, alive = psutil.wait_procs(waiting, timeout=timeout)
    else:
        alive = []

    force_killed = []
    for proc in alive:
        try:
            print(f"PID {proc.pid} 未在 {timeout} 秒内退出，正在强制终止")
            proc.kill()
            force_killed.append(proc)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as exc:
            errors.append(f"无法强制终止 PID {proc.pid}: {exc}")

    if force_killed:
        _, still_alive = psutil.wait_procs(force_killed, timeout=timeout)
        for proc in still_alive:
            errors.append(f"PID {proc.pid} 在强制终止后仍未退出")

    return errors


def kill_existing_processes():
    """可靠终止本项目的服务进程及其全部子进程。"""
    processes, errors = find_target_processes()
    if not processes:
        print("未发现正在运行的本项目服务进程。")
    else:
        errors.extend(terminate_processes(processes))

    for error in errors:
        print(f"错误: {error}", file=sys.stderr)
    return not errors


def has_devil():
    from shutil import which
    return which('devil') is not None


def get_domain_from_path():
    parts = BASE_DIR.split(os.sep)
    if 'domains' in parts:
        idx = parts.index('domains')
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def restart_devil():
    """按 runner.sh 的显式要求重启 Devil 管理的 Web 应用。"""
    if not has_devil():
        return True

    domain = get_domain_from_path()
    if not domain:
        print("错误: 未能从项目路径识别 Devil 域名目录。", file=sys.stderr)
        return False

    print(f"检测到 devil 命令，重启 Web 应用 ({domain})...")
    try:
        result = subprocess.run(['devil', 'www', 'restart', domain], check=False)
    except OSError as exc:
        print(f"错误: 无法执行 devil 命令: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            f"错误: Devil Web 应用重启失败，退出码: {result.returncode}",
            file=sys.stderr,
        )
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="停止本项目的 Python 服务进程")
    parser.add_argument(
        '--restart-devil',
        action='store_true',
        help="停止 Python 服务后重启 Devil 管理的 Web 应用",
    )
    args = parser.parse_args()

    success = kill_existing_processes()
    if args.restart_devil and success:
        success = restart_devil()

    if success:
        print("本项目相关进程已全部终止。")
        return 0

    print("部分进程未能终止，请检查上述错误。", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
