"""Offline encrypted backup/restore: generated fixture secrets only, no network."""
import importlib.util
import contextlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ASMarr/providers'))
import backup_snapshot as backup
spec = importlib.util.spec_from_file_location('backup_recovery', ROOT / 'ops/backup_recovery.py')
recovery = importlib.util.module_from_spec(spec); spec.loader.exec_module(recovery)


class BackupRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_temp = tempfile.TemporaryDirectory()
        cls.keys = Path(cls.keys_temp.name)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cls.keys / 'key.pem'), '-out', str(cls.keys / 'cert.pem'),
                        '-days', '1', '-subj', '/CN=Generated fixture'],
                       check=True, capture_output=True)
        (cls.keys / 'key.pem').chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.keys_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.config = self.root / 'config'; self.config.mkdir(mode=0o700)
        self.app = self.root / 'application'; self.app.mkdir()
        (self.app / 'ASMarr').write_text('fixture app'); (self.app / 'ASMarr').chmod(0o700)
        (self.app / 'build-identity.json').write_text(json.dumps({'version':'0.3.0','sourceCommit':'a'*40}))
        (self.config / 'auth.json').write_text(json.dumps({'apiKey': secrets.token_hex(32)}))
        (self.config / 'backup-policy.json').write_text('{"keepLocal":2}')
        (self.config / 'backup-policy.json').chmod(0o600)
        (self.config / 'backup-recipient.pem').write_bytes((self.keys / 'cert.pem').read_bytes())
        self.secret = secrets.token_hex(32)
        (self.config / 'source-secrets.json').write_text(self.secret)
        self.units = self.root / 'units'; self.units.mkdir()
        (self.units / 'asmarr.service').write_text('[Service]\nUser=asmarr\n')
        with contextlib.closing(sqlite3.connect(self.state / 'asmarr.db')) as db, db:
            db.executescript("CREATE TABLE tasks(name TEXT,enabled INTEGER,next_run TEXT);"
                             "INSERT INTO tasks VALUES('discovery',1,'future'),('queue',0,'future');"
                             "CREATE TABLE commands(id TEXT,state TEXT,error TEXT);"
                             "INSERT INTO commands VALUES('fixture','running',NULL);"
                             "CREATE TABLE backfill_jobs(state TEXT,error TEXT);"
                             "INSERT INTO backfill_jobs VALUES('running',NULL);"
                             "CREATE TABLE task_locks(name TEXT);INSERT INTO task_locks VALUES('providers');"
                             "CREATE TABLE settings(key TEXT UNIQUE,value TEXT);"
                             "INSERT INTO settings VALUES('mode','production');")

    def create(self):
        return backup.create(self.state, self.config, self.app, self.units)

    def test_encrypted_full_restore_with_scheduler_and_replay_hold(self):
        report = self.create(); path = Path(report['path'])
        self.assertTrue(backup.NAME.fullmatch(path.name)); self.assertTrue(report['encrypted'])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertNotIn(self.secret.encode(), path.read_bytes())
        output = self.root / 'restore'
        result = recovery.restore(path, self.keys / 'key.pem', output)
        self.assertTrue(result['verified']); self.assertFalse(result['mediaRestored'])
        self.assertEqual(self.secret, (output / 'config/source-secrets.json').read_text())
        self.assertEqual((self.app / 'ASMarr').read_bytes(), (output / 'application/ASMarr').read_bytes())
        self.assertEqual('[Service]\nUser=asmarr\n', (output / 'service/asmarr.service').read_text())
        with contextlib.closing(sqlite3.connect(output / 'state/asmarr.db')) as db:
            self.assertEqual(0, db.execute('SELECT sum(enabled) FROM tasks').fetchone()[0])
            self.assertEqual('interrupted', db.execute('SELECT state FROM commands').fetchone()[0])
            self.assertEqual('true', db.execute("SELECT value FROM settings WHERE key='recovery.hold'").fetchone()[0])
            self.assertEqual('shadow', db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0])
            self.assertEqual(0, db.execute('SELECT count(*) FROM task_locks').fetchone()[0])
        with contextlib.closing(sqlite3.connect(self.state / 'asmarr.db')) as db:
            self.assertEqual(1, db.execute("SELECT enabled FROM tasks WHERE name='discovery'").fetchone()[0])
        self.assertEqual([], list((self.state / 'backups').glob('.snapshot-*')))

    def test_retention_deletes_only_own_encrypted_completed_files(self):
        first = Path(self.create()['path'])
        folder = first.parent
        legacy = folder / 'legacy.db'; legacy.write_bytes(b'keep')
        unrelated = folder / 'notes.cms'; unrelated.write_bytes(b'keep')
        self.create(); third = self.create()
        self.assertFalse(first.exists()); self.assertTrue(legacy.exists()); self.assertTrue(unrelated.exists())
        self.assertEqual(2, len(backup.completed(folder))); self.assertEqual(1, third['pruned'])

    def test_encryption_failure_preserves_existing_backups(self):
        first = Path(self.create()['path'])
        with patch.object(backup.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'fixture')):
            with self.assertRaises(subprocess.CalledProcessError): self.create()
        self.assertTrue(first.exists()); self.assertEqual([first], backup.completed(first.parent))
        self.assertEqual([], list(first.parent.glob('.snapshot-*')))

    def test_invalid_retention_and_unprotected_policy_fail_closed(self):
        for value in (0, 1, 366, '14', True):
            (self.config / 'backup-policy.json').write_text(json.dumps({'keepLocal':value}))
            with self.assertRaises(ValueError): self.create()
        (self.config / 'backup-policy.json').chmod(0o644)
        with self.assertRaises(ValueError): self.create()

    def test_links_and_shared_files_not_followed(self):
        (self.config / 'outside').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError): self.create()
        (self.config / 'outside').unlink()
        os.link(self.config / 'auth.json', self.config / 'shared')
        with self.assertRaises(ValueError): self.create()

    def test_corruption_cannot_publish_partial_restore(self):
        path = Path(self.create()['path']); data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
        target = self.root / 'rejected'
        with self.assertRaises(subprocess.CalledProcessError): recovery.restore(path, self.keys / 'key.pem', target)
        self.assertFalse(target.exists()); self.assertEqual([], list(self.root.glob('.asmarr-restore-*')))

    def test_existing_restore_target_is_never_overwritten(self):
        path = Path(self.create()['path'])
        with self.assertRaises(ValueError): recovery.restore(path, self.keys / 'key.pem', self.config)
        self.assertEqual(self.secret, (self.config / 'source-secrets.json').read_text())

    def test_malicious_archive_paths_and_links_rejected(self):
        for name in ('/etc/passwd', '../outside', 'config/../../outside', 'unreviewed/file', './config/file'):
            with self.assertRaises(ValueError): recovery.safe_member(tarfile.TarInfo(name))
        info = tarfile.TarInfo('config/link'); info.type = tarfile.SYMTYPE
        with self.assertRaises(ValueError): recovery.safe_member(info)

    def test_missing_recipient_never_silently_falls_back_to_plaintext(self):
        (self.config / 'backup-recipient.pem').unlink()
        with self.assertRaises(FileNotFoundError): self.create()
        self.assertFalse((self.state / 'backups').exists())

    def test_old_binary_cannot_be_packaged_as_safely_held_full_recovery(self):
        (self.app / 'build-identity.json').write_text(json.dumps({'version':'0.2.0','sourceCommit':'a'*40}))
        with self.assertRaises(ValueError):self.create()
        self.assertFalse((self.state / 'backups').exists())

    def test_configuration_change_during_snapshot_fails_and_keeps_prior_backup(self):
        first = Path(self.create()['path']); original = backup.inventory; calls = 0
        def inventory(*args, **kwargs):
            nonlocal calls
            if Path(args[0]) == self.config:
                calls += 1
                if calls == 2: (self.config / 'auth.json').write_text('{}')
            return original(*args, **kwargs)
        with patch.object(backup, 'inventory', side_effect=inventory):
            with self.assertRaises(ValueError): self.create()
        self.assertEqual([first], backup.completed(first.parent))

    def test_release_hold_requires_stopped_service_and_restores_saved_states_only(self):
        path = Path(self.create()['path']); output = self.root / 'restored'
        recovery.restore(path, self.keys / 'key.pem', output)
        with patch.object(recovery.os,'geteuid',return_value=0), \
                patch.object(recovery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='LoadState=loaded\nActiveState=active\n')):
            with self.assertRaises(ValueError):
                recovery.release_hold(output/'state',output/'recovery-report.json','OLD_INSTANCE_STOPPED')
        with patch.object(recovery.os,'geteuid',return_value=0), \
                patch.object(recovery.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='LoadState=loaded\nActiveState=inactive\n')):
            result = recovery.release_hold(output/'state',output/'recovery-report.json','OLD_INSTANCE_STOPPED')
        self.assertTrue(result['savedTasksRestored']); self.assertFalse(result['interruptedCommandsReplayed'])
        with contextlib.closing(sqlite3.connect(output/'state/asmarr.db')) as db:
            self.assertEqual([('discovery',1),('queue',0)],db.execute('SELECT name,enabled FROM tasks ORDER BY name').fetchall())
            self.assertEqual('production',db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0])
            self.assertEqual('interrupted',db.execute('SELECT state FROM commands').fetchone()[0])
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='recovery.hold'").fetchone())

    def test_release_hold_fails_closed_when_service_state_cannot_be_verified(self):
        path = Path(self.create()['path']); output = self.root / 'restored'
        recovery.restore(path,self.keys/'key.pem',output)
        for response in (subprocess.CompletedProcess([],1,stdout=''),subprocess.CompletedProcess([],0,stdout='LoadState=not-found\nActiveState=inactive\n')):
            with patch.object(recovery.os,'geteuid',return_value=0),patch.object(recovery.subprocess,'run',return_value=response):
                with self.assertRaises(ValueError):recovery.release_hold(output/'state',output/'recovery-report.json','OLD_INSTANCE_STOPPED')


if __name__ == '__main__': unittest.main()
