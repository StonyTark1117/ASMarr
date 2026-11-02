"""Boot a real restored application in isolation; never use production state."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import ssl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'ASMarr/providers'))
import backup_snapshot
spec=importlib.util.spec_from_file_location('runtime_recovery',ROOT/'ops/backup_recovery.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)


@unittest.skipUnless((ROOT/'publish/ASMarr').is_file(),'Publish the self-contained app before the restore boot test')
class RecoveryRuntimeTests(unittest.TestCase):
    def test_complete_backup_restores_and_boots_with_no_work_replayed(self):
        with tempfile.TemporaryDirectory(prefix='asmarr-recovery-runtime-') as name:
            root=Path(name);state=root/'state';config=root/'config'
            state.mkdir(mode=0o700);config.mkdir(mode=0o700)
            (config/'fresh-install.json').write_text(json.dumps({'kind':'fresh','audioRoot':str(root/'audio'),'videoRoot':str(root/'video')}))
            (config/'sources.yaml').write_text('{}')
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                            '-keyout',str(config/'tls.key'),'-out',str(config/'tls.pem'),
                            '-days','1','-subj','/CN=localhost'],check=True,capture_output=True)
            def launch(app,state,config):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                env=dict(os.environ,ASMARR_STATE=str(state),ASMARR_CONFIG=str(config),
                         ASPNETCORE_URLS='https://127.0.0.1:'+str(port),
                         ASPNETCORE_Kestrel__Certificates__Default__Path=str(config/'tls.pem'),
                         ASPNETCORE_Kestrel__Certificates__Default__KeyPath=str(config/'tls.key'))
                process=subprocess.Popen([str(app/'ASMarr')],cwd=app,env=env,
                                         preexec_fn=lambda:os.umask(0o077),
                                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    for _ in range(150):
                        if process.poll() is not None:raise RuntimeError('Isolated recovery app exited')
                        try:
                            with urllib.request.urlopen('https://127.0.0.1:'+str(port)+'/healthz',
                                                        context=ssl._create_unverified_context(),timeout=1) as response:
                                if response.status==200:return process
                        except OSError:time.sleep(.1)
                    raise RuntimeError('Isolated recovery app did not start')
                except Exception:
                    process.terminate();process.wait(timeout=20);raise
            process=launch(ROOT/'publish',state,config)
            with contextlib.closing(sqlite3.connect(state/'asmarr.db')) as db:
                self.assertEqual('fresh',db.execute("SELECT value FROM settings WHERE key='installation.kind'").fetchone()[0])
                self.assertEqual(0,db.execute('SELECT count(*) FROM commands').fetchone()[0])
            process.terminate();process.wait(timeout=20)
            with contextlib.closing(sqlite3.connect(state/'asmarr.db')) as db:
                with db:
                    db.execute('UPDATE tasks SET enabled=0')
                    db.execute("UPDATE settings SET value='production' WHERE key='mode'")
                    db.execute("INSERT INTO creators(id,name,path,monitor_video) VALUES(1,'Generated fixture','/not-mounted-fixture',1)")
                    db.execute("INSERT INTO commands(id,name,arguments,state,created) VALUES('fixture-do-not-replay','creator-remove','{}','queued','2026-11-02T00:00:00Z')")
                    db.execute("INSERT INTO backfill_jobs(id,creator_id,media_kind,state,started,updated) VALUES('fixture-history',1,'Video','running','2026-11-02','2026-11-02')")
            keys=recovery.init_key(root/'keys')
            (config/'backup-recipient.pem').write_bytes(Path(keys['recipientCertificate']).read_bytes())
            (config/'backup-policy.json').write_text('{"keepLocal":2}');(config/'backup-policy.json').chmod(0o600)
            bundle=backup_snapshot.create(state,config,ROOT/'publish',root/'no-units')
            original_auth=(config/'auth.json').read_bytes()
            result=recovery.restore(Path(bundle['path']),Path(keys['recoveryKey']),root/'restored')
            self.assertTrue(result['oldBrowserSessionsRevoked'])
            restored=root/'restored'
            self.assertEqual(original_auth,(restored/'config/auth.json').read_bytes())
            self.assertTrue((restored/'recovery/original-cookie-keys').is_dir())
            process=launch(restored/'application',restored/'state',restored/'config')
            try:
                time.sleep(3)
                with contextlib.closing(sqlite3.connect(restored/'state/asmarr.db')) as db:
                    self.assertEqual('true',db.execute("SELECT value FROM settings WHERE key='recovery.hold'").fetchone()[0])
                    self.assertEqual(0,db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running','completed')").fetchone()[0])
                    self.assertEqual('interrupted',db.execute("SELECT state FROM commands WHERE id='fixture-do-not-replay'").fetchone()[0])
                    self.assertEqual('interrupted',db.execute("SELECT state FROM backfill_jobs WHERE id='fixture-history'").fetchone()[0])
                    self.assertEqual('Generated fixture',db.execute('SELECT name FROM creators').fetchone()[0])
                    self.assertEqual('ok',db.execute('PRAGMA integrity_check').fetchone()[0])
            finally:
                process.terminate();process.wait(timeout=20)


if __name__=='__main__':unittest.main()
