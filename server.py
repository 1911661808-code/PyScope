"""Linux 服务器入口；配置与 Docker 检查失败时拒绝启动。"""
import argparse
import atexit
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys

from auth import validate_origins
from werkzeug.security import generate_password_hash


def validate_config(config):
    if not isinstance(config, dict) or config.get('version') != 1:
        raise ValueError('配置 version 必须为 1。')
    secret = config.get('secret_key')
    if not isinstance(secret, str) or len(secret) < 43 or secret.startswith('<'):
        raise ValueError('secret_key 必须由 init 生成，不能使用示例占位内容。')
    users = config.get('users')
    if not isinstance(users, dict) or not users or len(users) > 100:
        raise ValueError('需要 1–100 个已配置账号。')
    for name, password_hash in users.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name):
            raise ValueError('用户名只允许 1–64 位字母、数字、下划线、点和横线。')
        if not isinstance(password_hash, str) or not re.fullmatch(r'scrypt:32768:8:1\$[A-Za-z0-9]+\$[0-9a-f]{128}', password_hash):
            raise ValueError('账号密码必须使用 init/add-user 生成的 scrypt 哈希。')
    if type(config.get('secure_cookie')) is not bool:
        raise ValueError('secure_cookie 必须为布尔值。')
    validate_origins(config.get('allowed_origins'), config['secure_cookie'])
    if not isinstance(config.get('data_dir'), str) or not Path(config['data_dir']).is_absolute():
        raise ValueError('data_dir 必须是独立数据目录的绝对路径。')
    if type(config.get('port', 9000)) is not int or not 1024 <= config.get('port', 9000) <= 65535:
        raise ValueError('port 必须在 1024–65535 之间。')
    if type(config.get('session_hours', 8)) is not int or not 1 <= config.get('session_hours', 8) <= 24:
        raise ValueError('session_hours 必须在 1–24 之间。')
    if config.get('require_rootless', True) is not True:
        raise ValueError('服务器入口要求 rootless Docker。')
    return config


def load_config(path):
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('配置权限过宽，请 chmod 600，并由服务账号持有。')
    return validate_config(json.loads(path.read_text(encoding='utf-8')))


def new_password():
    password = getpass.getpass('新密码（至少 12 位，不会显示）：')
    if len(password) < 12 or len(password) > 1024:
        raise ValueError('密码长度必须为 12–1024 位。')
    if password != getpass.getpass('再次输入密码：'):
        raise ValueError('两次密码不一致。')
    return generate_password_hash(password, method='scrypt:32768:8:1')


def save_new(path, config):
    validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description='PyScope 服务器管理（Linux + rootless Docker）')
    parser.add_argument('--config', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--username', required=True)
    init.add_argument('--origin', required=True)
    init.add_argument('--data-dir', type=Path)
    add = commands.add_parser('add-user', help='创建账号或更新该账号密码；随后重启服务')
    add.add_argument('--username', required=True)
    commands.add_parser('doctor')
    commands.add_parser('serve')
    args = parser.parse_args()
    path = args.config.resolve()
    try:
        if args.command == 'init':
            if path.exists():
                raise ValueError('配置已存在；不会覆盖。可用 add-user 更新账号。')
            config = dict(version=1, secret_key=secrets.token_urlsafe(32), users={args.username: new_password()},
                          allowed_origins=[args.origin], secure_cookie=args.origin.startswith('https://'),
                          data_dir=str((args.data_dir or path.parent / 'data').resolve()), port=9000,
                          docker_image='pyscope-worker:1.1.0', docker_runtime=None, require_rootless=True, session_hours=8)
            save_new(path, config)
            print(f'已创建配置：{path}。不要提交此文件到 GitHub。')
            return
        config = load_config(path)
        if args.command == 'add-user':
            config['users'][args.username] = new_password()
            validate_config(config)
            temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
            save_new(temporary, config)
            os.replace(temporary, path)
            print('账号已更新。重启服务后生效；该账号原有会话将失效。')
            return
        if sys.platform != 'linux' or os.geteuid() == 0:
            raise ValueError('服务器入口要求 Linux 非 root 服务账号；Windows 请使用 app.py 本地模式。')
        os.umask(0o077)
        directory = Path(config['data_dir'])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for protected in [directory, *(directory / name for name in ('history.sqlite3', 'auth.sqlite3') if (directory / name).exists())]:
            info = protected.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('数据目录及数据库必须由服务账号持有，目录权限 700、文件权限 600。')
        import fcntl
        instance_lock = (directory / 'server.lock').open('a')
        try:
            fcntl.flock(instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('该数据目录已有服务器实例正在运行。')
        from execution_backend import DockerBackend
        backend = DockerBackend(image=config.get('docker_image', 'pyscope-worker:1.1.0'), runtime=config.get('docker_runtime'))
        atexit.register(backend.close)
        result = backend.verify(require_rootless=True)
        print('隔离环境检查通过：' + json.dumps(result, ensure_ascii=False))
        if args.command == 'doctor':
            return
        from app import create_app
        from session_manager import SessionManager
        from waitress import serve
        app = create_app(data_dir=config['data_dir'], manager=SessionManager(backend=backend, max_sessions=4,
                         max_saved_sessions=4, max_trace_chars=8_000_000), server_config=config)
        atexit.register(app.extensions['shutdown'])
        def stop_signal(signum, frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop_signal)
        print(f"PyScope 服务器监听 127.0.0.1:{config.get('port', 9000)}", flush=True)
        try:
            serve(app, host='127.0.0.1', port=config.get('port', 9000), threads=8,
                  max_request_body_size=150_000, max_request_header_size=16_384,
                  connection_limit=64, channel_timeout=30, expose_tracebacks=False,
                  clear_untrusted_proxy_headers=True)
        finally:
            app.extensions['shutdown']()
            backend.close()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f'无法启动：{exc}\n')


if __name__ == '__main__':
    main()
