"""Read-only SSH export of encrypted ASMarr backups, optionally through Proxmox.

Install root-owned and force this command on a dedicated restricted SSH key.
Only list/get are accepted. No shell, path arguments, config, or plaintext export.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

NAME = re.compile(r'asmarr-full-\d{8}T\d{12}Z-[a-f0-9]{8}\.cms\Z')
FOLDER = Path('/var/lib/asmarr/backups')


def read_file(folder, name):
    if not NAME.fullmatch(name): raise ValueError('invalid_backup_name')
    fd = os.open(Path(folder) / name, os.O_RDONLY | os.O_NOFOLLOW)
    stream = os.fdopen(fd, 'rb')
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4 * 1024 ** 3:
        stream.close(); raise ValueError('invalid_backup_file')
    return stream, info


def export(command, folder=FOLDER):
    if command == ['list']:
        rows = []
        for path in sorted(Path(folder).iterdir()):
            if NAME.fullmatch(path.name):
                with read_file(folder, path.name)[0] as stream:
                    info = os.fstat(stream.fileno())
                    rows.append({'name': path.name, 'size': info.st_size,
                                 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()})
        print(json.dumps(rows))
    elif len(command) == 2 and command[0] == 'get' and NAME.fullmatch(command[1]):
        with read_file(folder, command[1])[0] as stream:
            while block := stream.read(1024 * 1024): sys.stdout.buffer.write(block)
    else:
        raise ValueError('export_supports_only_list_and_get')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', type=int)
    parser.add_argument('--direct', action='store_true')
    parser.add_argument('command', nargs='*')
    args = parser.parse_args()
    try:
        command = args.command if args.direct else shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        if not (command == ['list'] or len(command) == 2 and command[0] == 'get' and NAME.fullmatch(command[1])):
            raise ValueError('invalid_export_command')
        if args.container:
            # stdin is our root-owned script, never caller-provided shell/code.
            result = subprocess.run(['pct', 'exec', str(args.container), '--', 'python3', '-', '--direct', *command],
                                    input=Path(__file__).read_bytes(), timeout=600)
            return result.returncode
        export(command)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        print('Encrypted backup export rejected or unavailable.', file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
