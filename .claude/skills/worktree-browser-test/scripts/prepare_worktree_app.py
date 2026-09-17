#!/usr/bin/env python3
"""为当前 git worktree 准备一个可在浏览器中测试的 DropLoad Web 服务。

- 从 5201 起找第一个空闲且未被其他 worktree 占用的端口（5200 留给主工作区）；
- 以主工作区 config.json 为底生成本 worktree 的 config.json（已被 gitignore）；
- 共享模式下，数据路径指向主工作区，复用其登录用户、媒体库、下载器与 AI worker；
- 生成 .claude/launch.json，供内置浏览器的 preview_start 启动。

只用标准库和项目 venv 中已有的 json5，兼容 Linux、FreeBSD 和 macOS。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys

PORT_START = 5201
PORT_LIMIT = 5299
LAUNCH_NAME_PREFIX = 'dropload-app-'


def git(root, *args):
    return subprocess.check_output(['git', '-C', root, *args], text=True).strip()


def locate(start):
    worktree = git(start, 'rev-parse', '--show-toplevel')
    common_dir = git(worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    main = os.path.dirname(common_dir)
    return os.path.realpath(worktree), os.path.realpath(main)


def load_json5(path):
    import json5
    with open(path, encoding='utf-8') as handle:
        return json5.load(handle)


def port_is_free(port):
    """0.0.0.0 与 127.0.0.1 都能绑定才算空闲，app 默认监听 0.0.0.0。"""
    for host in ('0.0.0.0', '127.0.0.1'):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                return False
    return True


def listener_cwds(port):
    """返回监听该端口的进程工作目录；没有 lsof 时返回空列表。"""
    if not shutil.which('lsof'):
        return []
    try:
        pids = subprocess.run(
            ['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'],
            capture_output=True, text=True, check=False,
        ).stdout.split()
    except OSError:
        return []
    cwds = []
    for pid in set(pids):
        output = subprocess.run(
            ['lsof', '-a', '-p', pid, '-d', 'cwd', '-Fn'],
            capture_output=True, text=True, check=False,
        ).stdout
        cwds += [os.path.realpath(line[1:]) for line in output.splitlines() if line.startswith('n')]
    return cwds


def ports_claimed_by_other_worktrees(worktree, main):
    """其他 worktree 的 config.json 已登记的端口，即使服务暂时没启动也跳过。"""
    claimed = set()
    try:
        listing = git(main, 'worktree', 'list', '--porcelain')
    except subprocess.CalledProcessError:
        return claimed
    for line in listing.splitlines():
        if not line.startswith('worktree '):
            continue
        path = os.path.realpath(line[len('worktree '):])
        if path in (worktree, main):
            continue
        config_path = os.path.join(path, 'config.json')
        if not os.path.isfile(config_path):
            continue
        try:
            port = load_json5(config_path).get('FLASK_PORT')
        except Exception:
            continue
        if isinstance(port, int):
            claimed.add(port)
    return claimed


def choose_port(worktree, main, current_port):
    claimed = ports_claimed_by_other_worktrees(worktree, main)
    # 本 worktree 已配置过端口：空闲或正由本 worktree 的服务占用时沿用。
    if isinstance(current_port, int) and current_port >= PORT_START and current_port not in claimed:
        if port_is_free(current_port):
            return current_port, False
        if worktree in listener_cwds(current_port):
            return current_port, True
    for port in range(PORT_START, PORT_LIMIT + 1):
        if port in claimed:
            continue
        if port_is_free(port):
            return port, False
    sys.exit(f'{PORT_START}-{PORT_LIMIT} 之间没有可用端口')


def build_config(worktree, main, port, isolated):
    main_config_path = os.path.join(main, 'config.json')
    if not os.path.isfile(main_config_path):
        sys.exit(f'主工作区缺少 {main_config_path}，无法生成配置')
    config = load_json5(main_config_path)
    config['FLASK_PORT'] = port
    if isolated:
        return config

    sys.path.insert(0, worktree)
    from config_util import DEFAULT_CONFIG, PATH_CONFIG_KEYS

    for key in PATH_CONFIG_KEYS:
        value = config.get(key, DEFAULT_CONFIG.get(key))
        if isinstance(value, str) and value:
            config[key] = os.path.normpath(os.path.join(main, value))
    return config


def link_local_files(worktree, main):
    """yt-dlp 本地覆盖配置与 cookie 文件不入库，共享模式下软链过来。"""
    linked = []
    for name in sorted(os.listdir(main)):
        if not (name.endswith('.local.conf') or name.endswith('-cookies.txt')):
            continue
        target = os.path.join(worktree, name)
        if os.path.lexists(target):
            continue
        os.symlink(os.path.join(main, name), target)
        linked.append(name)
    return linked


def write_launch(worktree, main, port):
    python = os.path.join(main, 'venv', 'bin', 'python')
    launch_path = os.path.join(worktree, '.claude', 'launch.json')
    os.makedirs(os.path.dirname(launch_path), exist_ok=True)
    launch = {'version': '0.0.1', 'configurations': []}
    if os.path.isfile(launch_path):
        try:
            with open(launch_path, encoding='utf-8') as handle:
                launch = json.load(handle)
        except (OSError, ValueError):
            pass
    name = f'{LAUNCH_NAME_PREFIX}{port}'
    configurations = [
        item for item in launch.get('configurations', [])
        if not str(item.get('name', '')).startswith(LAUNCH_NAME_PREFIX)
    ]
    configurations.append({
        'name': name,
        'runtimeExecutable': '/usr/bin/env',
        'runtimeArgs': ['FLASK_DEBUG=1', python, 'app.py'],
        'port': port,
        'url': f'http://127.0.0.1:{port}',
    })
    launch['configurations'] = configurations
    with open(launch_path, 'w', encoding='utf-8') as handle:
        json.dump(launch, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return name


def main_entry():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated', action='store_true',
                        help='不共享主工作区数据，使用本 worktree 自己的 urls/files/data')
    args = parser.parse_args()

    worktree, main = locate(os.getcwd())
    if worktree == main:
        sys.exit('当前就是主工作区，直接使用 5200 端口的服务即可')

    config_path = os.path.join(worktree, 'config.json')
    current_port = None
    if os.path.isfile(config_path):
        try:
            current_port = load_json5(config_path).get('FLASK_PORT')
        except Exception:
            current_port = None

    port, running = choose_port(worktree, main, current_port)
    config = build_config(worktree, main, port, args.isolated)
    with open(config_path, 'w', encoding='utf-8') as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write('\n')

    linked = [] if args.isolated else link_local_files(worktree, main)
    launch_name = write_launch(worktree, main, port)
    print(json.dumps({
        'worktree': worktree,
        'main': main,
        'port': port,
        'already_running': running,
        'mode': 'isolated' if args.isolated else 'shared',
        'launch_name': launch_name,
        'url': f'http://127.0.0.1:{port}/',
        'linked_local_files': linked,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main_entry()
