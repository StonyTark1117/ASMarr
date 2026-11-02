"""Authenticated, recipient-encrypted recovery bundles. No recovery key on server.

Only this module's exact completed filenames are eligible for retention/export.
Media, transfer staging, legacy files, and existing SQLite backups are untouched.
"""
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import tarfile
import tempfile

NAME = re.compile(r'asmarr-full-\d{8}T\d{12}Z-[a-f0-9]{8}\.cms\Z')
MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 4 * MAX_FILE


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def protected_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError('backup_directory_must_be_private')
    return path


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('backup_requires_regular_unshared_files')
    if info.st_size > MAX_FILE:
        raise ValueError('backup_file_size_limit')
    return info


def inventory(root, prefix, excluded=()):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('backup_source_directory_invalid')
    result = {}
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [n for n in sorted(directories) if n not in excluded]
        for name in directories:
            if (Path(folder) / name).is_symlink():
                raise ValueError('backup_linked_directory')
        for name in sorted(files):
            path = Path(folder) / name
            if name in excluded:
                continue
            info = regular(path)
            result[str(Path(prefix) / path.relative_to(root))] = {
                'path': path, 'size': info.st_size, 'sha256': digest(path),
                'executable': bool(info.st_mode & 0o111)}
    return result


def completed(folder):
    return sorted((p for p in Path(folder).iterdir() if NAME.fullmatch(p.name)
                   and p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1),
                  key=lambda p: p.name, reverse=True)


def prune(folder, keep):
    if type(keep) is not int or not 2 <= keep <= 365:
        raise ValueError('backup_retention_must_be_2_to_365')
    removed = []
    for path in completed(folder)[keep:]:
        # Recheck before unlink; never follow links or recurse.
        regular(path)
        path.unlink()
        removed.append(path.name)
    return removed


def create(state, config, application=None, service_root=Path('/etc/systemd/system')):
    state, config = Path(state), Path(config)
    application = Path(application or Path(__file__).resolve().parents[1])
    identity_path = application / 'build-identity.json'
    regular(identity_path)
    identity = json.loads(identity_path.read_text())
    version = identity.get('version', '')
    if (not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or tuple(map(int, version.split('.'))) < (0, 3, 0)
            or not re.fullmatch('[a-f0-9]{40}', identity.get('sourceCommit',''))):
        raise ValueError('full_recovery_requires_hold_aware_release_0_3_or_later')
    policy_path = config / 'backup-policy.json'
    regular(policy_path)
    if policy_path.stat().st_mode & 0o077:
        raise ValueError('backup_policy_must_be_private')
    policy = json.loads(policy_path.read_text())
    keep = policy.get('keepLocal', 14)
    if type(keep) is not int or not 2 <= keep <= 365:
        raise ValueError('backup_retention_must_be_2_to_365')
    certificate = config / 'backup-recipient.pem'
    regular(certificate)
    folder = protected_directory(state / 'backups')
    lock = os.open(folder / '.full-backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = 'asmarr-full-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + secrets.token_hex(4) + '.cms'
        target = folder / name
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=folder) as scratch_name:
            scratch = Path(scratch_name)
            snapshot = scratch / 'asmarr.db'
            with contextlib.closing(sqlite3.connect('file:' + str(state / 'asmarr.db') + '?mode=ro', uri=True)) as source:
                with contextlib.closing(sqlite3.connect(snapshot)) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('backup_database_integrity_failed')
            before = inventory(config, 'config')
            files = dict(before)
            files.update(inventory(application, 'application', ('__pycache__',)))
            files['state/asmarr.db'] = {'path': snapshot, 'size': snapshot.stat().st_size,
                                       'sha256': digest(snapshot), 'executable': False}
            release = state / 'deployed-release-runtime.json'
            if release.exists():
                regular(release)
                files['state/deployed-release-runtime.json'] = {
                    'path': release, 'size': release.stat().st_size,
                    'sha256': digest(release), 'executable': False}
            unit = Path(service_root) / 'asmarr.service'
            if unit.exists():
                regular(unit)
                files['service/asmarr.service'] = {'path': unit, 'size': unit.stat().st_size,
                                                  'sha256': digest(unit), 'executable': False}
            overrides = Path(service_root) / 'asmarr.service.d'
            if overrides.exists():
                files.update(inventory(overrides, 'service/asmarr.service.d'))
            if sum(f['size'] for f in files.values()) > MAX_TOTAL:
                raise ValueError('backup_total_size_limit')
            manifest = {'format': 1, 'restoreHoldAware': True, 'applicationIdentity': identity,
                        'createdAt': datetime.now(timezone.utc).isoformat(),
                        'scope': 'application, database, protected configuration, service; no media or OS dependencies',
                        'files': {k: {a: v for a, v in f.items() if a != 'path'} for k, f in files.items()}}
            plain = scratch / 'bundle.tar.gz'
            with tarfile.open(plain, 'w:gz') as archive:
                for name, item in sorted(files.items()):
                    # No symlinks, hardlinks, file owner names, or arbitrary tar metadata.
                    fd = os.open(item['path'], os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise ValueError('backup_source_changed')
                        info = tarfile.TarInfo(name)
                        info.size = item['size']; info.mode = 0o700 if item['executable'] else 0o600
                        archive.addfile(info, stream)
                    if digest(item['path']) != item['sha256']:
                        raise ValueError('backup_source_changed')
                content = json.dumps(manifest, sort_keys=True).encode()
                info = tarfile.TarInfo('manifest.json'); info.size = len(content); info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
            if inventory(config, 'config') != before:
                raise ValueError('backup_configuration_changed_retry')
            encrypted = scratch / 'encrypted.cms'
            subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes-256-gcm',
                            '-in', str(plain), '-outform', 'DER', '-out', str(encrypted),
                            str(certificate)], check=True, capture_output=True, timeout=300)
            encrypted.chmod(0o600)
            with encrypted.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(encrypted, target)
        removed = prune(folder, keep)
        return {'path': str(target), 'sha256': digest(target), 'encrypted': True,
                'fileCount': len(files), 'retention': keep, 'pruned': len(removed),
                'offHost': 'pending independent pull; check off-host timer evidence'}
    finally:
        os.close(lock)
