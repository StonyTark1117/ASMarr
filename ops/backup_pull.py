"""Pull encrypted bundles over a pinned, restricted SSH connection; no passwords.

The recovery private key is NOT needed for transport. Restore uses it offline.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

NAME = re.compile(r'asmarr-full-\d{8}T\d{12}Z-[a-f0-9]{8}\.cms\Z')


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def ssh(policy, *command, **kwargs):
    return subprocess.run(['ssh', '-i', policy['identityFile'], '-o', 'BatchMode=yes',
                           '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                           '-o', 'UserKnownHostsFile=' + policy['knownHostsFile'],
                           '-o', 'ConnectTimeout=15', policy['host'], *command],
                          check=True, stderr=subprocess.PIPE, timeout=600, **kwargs)


def pull(policy):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+', policy['host']):
        raise ValueError('invalid_ssh_host')
    keep = policy.get('keepOffHost', 30)
    if type(keep) is not int or not 2 <= keep <= 365: raise ValueError('invalid_retention')
    for field in ('identityFile', 'knownHostsFile'):
        path = Path(policy[field])
        if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_mode & 0o077:
            raise ValueError('private_ssh_files_required')
    destination = Path(policy['destination'])
    if not destination.is_absolute() or destination.is_symlink(): raise ValueError('invalid_destination')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.stat().st_mode & 0o077: raise ValueError('private_destination_required')
    rows = json.loads(ssh(policy, 'list', stdout=subprocess.PIPE).stdout)
    if not isinstance(rows, list) or not rows or len(rows) > 365:
        raise ValueError('no_valid_remote_backups')
    names = set(); copied = 0
    for row in rows:
        name = row['name']
        if (not NAME.fullmatch(name) or name in names or type(row['size']) is not int
                or not 0 < row['size'] <= 4 * 1024 ** 3 or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise ValueError('invalid_remote_inventory')
        names.add(name)
    for row in sorted(rows, key=lambda r: r['name'])[-keep:]:
        path = destination / row['name']
        if path.exists() or path.is_symlink():
            if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_nlink != 1 or path.stat().st_mode & 0o077 or sha(path) != row['sha256']:
                raise ValueError('existing_offhost_copy_does_not_match')
            continue
        fd, temporary = tempfile.mkstemp(prefix='.pull-', dir=destination)
        try:
            with os.fdopen(fd, 'wb') as stream:
                ssh(policy, 'get', row['name'], stdout=stream)
                stream.flush(); os.fsync(stream.fileno())
            temp = Path(temporary)
            if temp.stat().st_size != row['size'] or sha(temp) != row['sha256']:
                raise ValueError('offhost_transfer_verification_failed')
            os.replace(temp, path); copied += 1
        finally:
            Path(temporary).unlink(missing_ok=True)
    files = sorted((p for p in destination.iterdir() if NAME.fullmatch(p.name)), reverse=True)
    pruned = 0
    for path in files[keep:]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError('unsafe_retention_target')
        path.unlink(); pruned += 1
    latest = max(rows, key=lambda r: r['name'])
    created = datetime.strptime(latest['name'][len('asmarr-full-'):].split('-',1)[0],
                                '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc)-created).total_seconds()
    fresh = -300 <= age <= 36*3600
    report = {'verifiedAt': datetime.now(timezone.utc).isoformat(), 'copied': copied,
              'pruned': pruned, 'retention': keep, 'latest': latest,
              'sourceBackupCreatedAt':created.isoformat(),'sourceBackupAgeSeconds':int(age),
              'sourceBackupFresh':fresh,'status':'verified' if fresh else 'copied-source-backup-overdue-or-clock-invalid',
              'encryptedOnly': True, 'restoreStillRequiresRecoveryKey': True}
    fd, temporary = tempfile.mkstemp(prefix='.receipt-', dir=destination)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, destination / 'last-pull.json')
    finally: Path(temporary).unlink(missing_ok=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        path = args.policy
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_mode & 0o077:
            raise ValueError('private_policy_required')
        report=pull(json.loads(path.read_text()))
        print(json.dumps(report, indent=2))
        return 0 if report['sourceBackupFresh'] else 2
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('Off-host copy failed. Existing backups retained; check SSH trust, destination space and private policy.')
        return 1


if __name__ == '__main__': raise SystemExit(main())
