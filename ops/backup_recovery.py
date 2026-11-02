"""Offline recovery-key creation and safe restore into a NEW private directory.

Never overwrites a live installation, restores media, or enables scheduling.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile

MAX_BYTES = 4 * 1024 ** 3


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError('Choose a new absolute directory; existing targets are never overwritten.')
    path.mkdir(mode=0o700, parents=False)
    return path


def init_key(path):
    path = Path(path)
    if any((p / '.git').exists() for p in [path, *path.parents]):
        raise ValueError('Recovery keys must be kept outside source checkouts.')
    path = private_directory(path)
    key, cert = path / 'recovery-key.pem', path / 'recovery-cert.pem'
    try:
        # Key never visits the application server. Protect/export it separately.
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '3650',
                        '-subj', '/CN=ASMarr backup recovery', '-addext',
                        'keyUsage=critical,keyEncipherment'], check=True,
                       capture_output=True, timeout=120)
        key.chmod(0o600); cert.chmod(0o600)
    except Exception:
        for file in (key, cert):
            file.unlink(missing_ok=True)
        path.rmdir()
        raise
    return {'recoveryKey': str(key), 'recipientCertificate': str(cert),
            'warning': 'Keep the key offline/separate from backups; losing it loses recovery. Copy ONLY the certificate to the server.'}


def safe_member(member):
    path = PurePosixPath(member.name)
    if (not member.isfile() or path.is_absolute() or '..' in path.parts
            or str(path) != member.name or member.size < 0
            or not (member.name == 'manifest.json' or path.parts[0] in
                    {'config', 'application', 'state', 'service'})):
        raise ValueError('Unsafe recovery archive member.')


def restore(bundle, key, output):
    bundle, key, output = Path(bundle), Path(key), Path(output)
    for file in (bundle, key):
        if not stat.S_ISREG(file.lstat().st_mode) or file.is_symlink():
            raise ValueError('Recovery input must be a regular file, not a link.')
    if key.stat().st_mode & 0o077:
        raise ValueError('Recovery key must have mode 0600.')
    if bundle.stat().st_size > MAX_BYTES:
        raise ValueError('Recovery bundle exceeds size limit.')
    # Output must not exist, including dangling symlinks. Parent must be private;
    # decrypted data is created only inside a private temporary directory.
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('Restore target must be a new absolute directory.')
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError('Restore parent must be an existing real directory.')
    with tempfile.TemporaryDirectory(prefix='.asmarr-restore-', dir=output.parent) as scratch_name:
        scratch = Path(scratch_name)
        plain = scratch / 'bundle.tar.gz'
        subprocess.run(['openssl', 'cms', '-decrypt', '-binary', '-inform', 'DER',
                        '-in', str(bundle), '-inkey', str(key), '-out', str(plain)],
                       check=True, capture_output=True, timeout=300)
        destination = scratch / 'restored'; destination.mkdir(mode=0o700)
        with tarfile.open(plain, 'r:gz') as archive:
            members = []; total = 0; names = set()
            for member in archive:
                safe_member(member)
                if member.name in names:
                    raise ValueError('Duplicate recovery archive member.')
                names.add(member.name); total += member.size; members.append(member)
                if total > MAX_BYTES or len(members) > 10000:
                    raise ValueError('Recovery archive exceeds size limits.')
            if 'manifest.json' not in names:
                raise ValueError('Recovery manifest missing.')
            manifest_member = next(m for m in members if m.name == 'manifest.json')
            if manifest_member.size > 4 * 1024 ** 2:
                raise ValueError('Recovery manifest too large.')
            manifest = json.load(archive.extractfile(manifest_member))
            if manifest.get('format') != 1 or set(manifest.get('files', {})) != names - {'manifest.json'}:
                raise ValueError('Recovery manifest scope mismatch.')
            for member in members:
                target = destination / member.name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                expected = manifest['files'].get(member.name)
                if expected and (type(expected['size']) is not int or expected['size'] != member.size):
                    raise ValueError('Recovery file size mismatch.')
                with archive.extractfile(member) as source, target.open('xb') as dest:
                    shutil.copyfileobj(source, dest)
                target.chmod(0o700 if expected and expected.get('executable') else 0o600)
                if expected:
                    with target.open('rb') as stream:
                        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if actual != expected['sha256']:
                        raise ValueError('Recovery file checksum mismatch.')
        required = ['config/auth.json', 'state/asmarr.db', 'application/ASMarr']
        if not all((destination / name).is_file() for name in required):
            raise ValueError('Incomplete recovery bundle.')
        db_path = destination / 'state/asmarr.db'
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Restored database failed integrity check.')
            tasks = [dict(zip(('name', 'enabled', 'next_run'), row)) for row in
                     db.execute('SELECT name,enabled,next_run FROM tasks ORDER BY name')]
            commands = db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running')").fetchone()[0]
            mode = db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
            # Restore must not replay old imports/removals/downloads. Preserve
            # their rows for review, not automatic execution.
            with db:
                db.execute('UPDATE tasks SET enabled=0')
                db.execute("UPDATE commands SET state='interrupted',error='Recovery hold: review before retry' WHERE state IN ('queued','running')")
                db.execute("UPDATE backfill_jobs SET state='interrupted',error='Recovery hold: review before retry' WHERE state IN ('queued','running')")
                # Worker resumes interrupted backfills in production. Hold mode
                # in addition to task disablement until the operator reviews it.
                db.execute("UPDATE settings SET value='shadow' WHERE key='mode'")
                db.execute("INSERT INTO settings(key,value) VALUES('recovery.hold','true') ON CONFLICT(key) DO UPDATE SET value='true'")
                db.execute("INSERT INTO settings(key,value) VALUES('recovery.restored','true') ON CONFLICT(key) DO UPDATE SET value='true'")
                db.execute('DELETE FROM task_locks')
        report = {'verified': True, 'sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
                  'restoredFiles': len(manifest['files']), 'savedTasks': tasks,
                  'interruptedCommands': commands, 'schedulerHeld': True,
                  'savedMode': mode[0] if mode else 'shadow',
                  'mode': 'shadow', 'mediaRestored': False,
                  'next': 'Keep the old instance stopped. Install OS dependencies, verify mounts and credentials, then follow docs/recovery.md. Do not run both instances.'}
        # Preserve historical data-protection keys for forensics, but never let
        # restoring a backup resurrect pre-restore browser sessions.
        cookie_keys = destination / 'config/keys'
        if cookie_keys.exists():
            archive_keys = destination / 'recovery/original-cookie-keys'
            archive_keys.parent.mkdir(mode=0o700)
            cookie_keys.rename(archive_keys)
            cookie_keys.mkdir(mode=0o700)
        report['oldBrowserSessionsRevoked'] = True
        report_path = destination / 'recovery-report.json'
        report_path.write_text(json.dumps(report, indent=2)); report_path.chmod(0o600)
        # Rename publishes a verified directory, never an incomplete live tree.
        os.rename(destination, output)
    return report


def release_hold(state, report_path, confirmation):
    if os.geteuid() != 0 or confirmation != 'OLD_INSTANCE_STOPPED':
        raise ValueError('Root and explicit OLD_INSTANCE_STOPPED confirmation required.')
    # Do not race the worker when changing the recovery hold/saved schedules.
    query = subprocess.run(['systemctl', 'show', 'asmarr', '--property=LoadState,ActiveState'],
                           check=False, capture_output=True, text=True)
    properties = dict(line.split('=',1) for line in query.stdout.splitlines() if '=' in line)
    if (query.returncode != 0 or properties.get('LoadState') != 'loaded'
            or properties.get('ActiveState') not in ('inactive','failed')):
        raise ValueError('Stop the recovered service before releasing its hold.')
    state, report_path = Path(state), Path(report_path)
    if not state.is_absolute() or state.is_symlink() or report_path.is_symlink():
        raise ValueError('Use explicit real recovery paths.')
    report = json.loads(report_path.read_text())
    if report.get('verified') is not True or report.get('savedMode') not in ('production', 'shadow'):
        raise ValueError('Verified recovery report required.')
    with contextlib.closing(sqlite3.connect('file:' + str(state / 'asmarr.db') + '?mode=rw', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity failed.')
        hold = db.execute("SELECT value FROM settings WHERE key='recovery.hold'").fetchone()
        if not hold or hold[0] != 'true': raise ValueError('Not a held recovery database.')
        saved = report['savedTasks']
        names = [r[0] for r in db.execute('SELECT name FROM tasks ORDER BY name')]
        if names != sorted(r['name'] for r in saved) or len(set(names)) != len(saved):
            raise ValueError('Recovery task inventory mismatch.')
        for task in saved:
            if task['enabled'] not in (0,1): raise ValueError('Invalid saved task state.')
        with db:
            # Retain next_run from the backup: overdue work runs normally after
            # startup rather than silently postponing the next daily cycle.
            for task in saved:
                db.execute('UPDATE tasks SET enabled=? WHERE name=?',(task['enabled'],task['name']))
            db.execute("UPDATE settings SET value=? WHERE key='mode'",(report['savedMode'],))
            db.execute("DELETE FROM settings WHERE key='recovery.hold'")
    return {'released': True, 'mode': report['savedMode'],
            'savedTasksRestored': True, 'interruptedCommandsReplayed': False,
            'next': 'Start ONLY this instance. Review interrupted commands; verify downloads, discovery, Plex and the next backup.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init-key'); init.add_argument('--directory', type=Path, required=True)
    recover = commands.add_parser('restore')
    recover.add_argument('--bundle', type=Path, required=True)
    recover.add_argument('--key', type=Path, required=True)
    recover.add_argument('--output', type=Path, required=True)
    release = commands.add_parser('release-hold')
    release.add_argument('--state', type=Path, default=Path('/var/lib/asmarr'))
    release.add_argument('--report', type=Path, required=True)
    release.add_argument('--confirm', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init-key': result = init_key(args.directory)
        elif args.command == 'release-hold': result = release_hold(args.state, args.report, args.confirm)
        else: result = restore(args.bundle, args.key, args.output)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError):
        print('Recovery operation failed safely. Check private paths, recipient key, bundle integrity and available space; no existing installation was replaced.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
