"""Fingerprint the deployed application, including DLLs, providers, and UI.

Hashing only the .NET apphost executable does not identify application code.
Python bytecode caches are generated at runtime and deliberately excluded.
"""
import hashlib
from pathlib import Path


def artifact_hash(root):
    root=Path(root)
    if not root.is_dir():raise ValueError('application_artifact_missing')
    fingerprint=hashlib.sha256();count=0
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if '__pycache__' in relative.parts or path.suffix=='.pyc':continue
        if path.is_symlink():raise ValueError('application_artifact_symlink_requires_review')
        if not path.is_file():continue
        before=path.stat();content=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):content.update(chunk)
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
            raise ValueError('application_artifact_changed_during_audit')
        fingerprint.update(relative.as_posix().encode()+b'\0'+content.hexdigest().encode()+b'\n');count+=1
    if not count:raise ValueError('application_artifact_empty')
    return fingerprint.hexdigest()
