"""Root-only, terminal-only initial login retrieval and random password recovery.

Never accepts a password in argv, exports it, or prints it to redirected output.
Run after the service's first start; ordinary upgrades never call this tool.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile


def read_protected(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Credential files must be regular files with mode 0600.')
        return stream.read(), info


def initial_login(config):
    auth_text, _ = read_protected(config / 'auth.json')
    bootstrap, _ = read_protected(config / 'initial-admin.txt')
    auth = json.loads(auth_text)
    lines = dict(line.split(': ', 1) for line in bootstrap.splitlines())
    password = lines['Password']
    actual = base64.b64encode(hashlib.pbkdf2_hmac(
        'sha256', password.encode(), base64.b64decode(auth['salt']), 600000, 32)).decode()
    if lines['Username'] != auth['username'] or not hmac.compare_digest(actual, auth['hash']):
        raise ValueError('Initial credentials are no longer valid; use Settings or --reset.')
    return auth['username'], password


def reset_login(config):
    text, info = read_protected(config / 'auth.json')
    auth = json.loads(text)
    # Fail closed on incomplete configuration rather than inventing a new API key.
    if not all(isinstance(auth.get(k), str) and auth[k] for k in ('username', 'salt', 'hash', 'apiKey')):
        raise ValueError('Authentication configuration is incomplete.')
    password = secrets.token_hex(18).upper()
    salt = secrets.token_bytes(16)
    auth.update(username='admin', salt=base64.b64encode(salt).decode(),
                hash=base64.b64encode(hashlib.pbkdf2_hmac(
                    'sha256', password.encode(), salt, 600000, 32)).decode())
    fd, temporary = tempfile.mkstemp(prefix='.auth-reset-', dir=config)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            json.dump(auth, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config / 'auth.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # A reset is displayed once, not stored as a new plaintext bootstrap file.
    (config / 'initial-admin.txt').unlink(missing_ok=True)
    return 'admin', password


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/asmarr'))
    parser.add_argument('--reset', action='store_true', help='Generate a NEW random administrator password; preserve the API key.')
    args = parser.parse_args(argv)
    # Check before reading or mutating anything: CI, pipes and logs get no secrets.
    if os.geteuid() != 0:
        print('Run locally as root.', file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('A local interactive terminal is required; credentials cannot be redirected.', file=sys.stderr)
        return 1
    try:
        if args.reset and input('Replace the administrator password? Type RESET: ') != 'RESET':
            print('Cancelled; credentials unchanged.')
            return 1
        username, password = reset_login(args.config) if args.reset else initial_login(args.config)
    except (OSError, ValueError, KeyError):
        print('Credentials unavailable or unsafe. Start the service first; if already changed, use Settings or --reset.', file=sys.stderr)
        return 1
    print('ASMarr administrator login (keep private; do not record this terminal):')
    print('Username: ' + username)
    print('Password: ' + password)
    print('Use HTTPS. Change the password in Settings after login (at least 12 characters).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
