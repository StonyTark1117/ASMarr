import io
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
import production_upgrade as upgrade


class ProductionUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'app'; self.app.mkdir(); (self.app / 'old-code').write_bytes(b'old')
        self.config = self.root / 'config'; self.config.mkdir(); (self.config / 'auth.json').write_text('{}')
        self.acceptance = self.root / 'acceptance'; self.acceptance.mkdir()
        self.old = {'sourceCommit':'a'*40, 'artifactSha256':'d'*64}
        (self.acceptance / 'deployed-release.json').write_text(json.dumps(self.old))
        (self.acceptance / 'production-accepted.json').write_text(json.dumps(dict(self.old, testedCommit='a'*40, ready=True)))
        self.runtime = self.root / 'deployed-release-runtime.json'; self.runtime.write_text(json.dumps(self.old))
        self.state = self.root / 'state.db'
        with sqlite3.connect(self.state) as db:
            db.executescript("""CREATE TABLE tasks(name TEXT,enabled INTEGER,next_run TEXT);
                              INSERT INTO tasks VALUES('queue',1,'later'),('discovery',0,'tomorrow');
                              CREATE TABLE commands(state TEXT);
                              CREATE TABLE preserved(value TEXT);
                              INSERT INTO preserved VALUES('original');""")
        self.archive = self.root / 'payload.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as tar:
            for name in ('ASMarr','ASMarr.dll','wwwroot/index.html'):
                entry = tarfile.TarInfo(name); entry.size=3; entry.mode=0o755
                tar.addfile(entry, io.BytesIO(b'new'))
        self.changed = False
        def snapshot():
            with sqlite3.connect(self.state) as db:
                tasks = db.execute('SELECT name,enabled FROM tasks ORDER BY name').fetchall()
                active = db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running')").fetchone()[0]
            return {'mode':'production','integrity':'ok','activeCommands':active,
                    'videoOptIns':0,'videoAssets':0,'videoCandidates':0,'videoBackfills':0,
                    'taskSettingsHash':json.dumps(tasks),'assetRowHash':'changed' if self.changed else 'original'}
        def system(args, **kwargs):
            stdout = 'disabled' if 'is-enabled' in args else 'inactive' if 'is-active' in args else b'unit'
            return Mock(stdout=stdout, returncode=0)
        self.ci = Mock()
        self.ci.json.return_value = {'head_sha':'b'*40,'status':'completed','conclusion':'success','html_url':'https://example.invalid/ci'}
        patches = [patch.multiple(upgrade, DB=self.state,APPLICATION=self.app,CONFIG=self.config,
                                 ACCEPTANCE=self.acceptance,BACKUPS=self.root/'backups',RUNTIME=self.runtime),
                   patch.object(upgrade, 'invariants', side_effect=snapshot),
                   patch.object(upgrade, 'artifact_hash', side_effect=lambda path:'d'*64 if (path/'old-code').exists() else 'c'*64),
                   patch.object(upgrade, 'verify_fixture_files'), patch.object(upgrade.os,'chown'),
                   patch.object(upgrade.requests, 'get', return_value=self.ci),
                   patch.object(upgrade.subprocess, 'run', side_effect=system),
                   patch.object(upgrade, 'healthy')]
        for context in patches:
            context.start(); self.addCleanup(context.stop)
        self.service = patch.object(upgrade, 'system').start(); self.addCleanup(patch.stopall)

    def deploy(self, failed=False, changed=False):
        def evidence(command, *arguments):
            flag = '--baseline' if 'capture' in arguments else '--output'
            destination = Path(arguments[arguments.index(flag)+1])
            destination.write_text(json.dumps({'passed':not failed}))
            if command == 'runtime_fixtures.py' and failed:
                self.changed = changed
                raise RuntimeError('fixture_failure')
        with patch.object(upgrade, 'evidence', side_effect=evidence):
            return upgrade.deploy(self.archive, self.root/'fixtures','b'*40,123,'c'*64)

    def tasks(self):
        with sqlite3.connect(self.state) as db:
            return db.execute('SELECT name,enabled,next_run FROM tasks ORDER BY name').fetchall()

    def test_upgrade_preserves_backup_evidence_and_exact_schedule(self):
        original = self.tasks()
        report = self.deploy()
        self.assertTrue(report['ready']); self.assertTrue(report['schedulerRestored'])
        self.assertEqual(self.tasks(), original)
        self.assertTrue((Path(report['backup'])/'application/old-code').is_file())
        with sqlite3.connect(Path(report['backup'])/'state.db') as db:
            self.assertEqual(db.execute('SELECT value FROM preserved').fetchone()[0], 'original')
        self.assertTrue((self.app/'ASMarr.dll').is_file())
        self.assertEqual(json.loads(self.runtime.read_text())['sourceCommit'], 'b'*40)
        self.assertEqual(json.loads((self.acceptance/'production-accepted.json').read_text())['testedCommit'], 'a'*40)
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o640)

    def test_failed_installed_test_restores_application_and_schedule_without_rewriting_database(self):
        original = self.tasks()
        with self.assertRaisesRegex(RuntimeError, 'fixture_failure'):
            self.deploy(failed=True)
        self.assertTrue((self.app/'old-code').is_file())
        self.assertEqual(json.loads(self.runtime.read_text()), self.old)
        self.assertEqual(json.loads((self.acceptance/'deployed-release.json').read_text()), self.old)
        self.assertEqual(self.tasks(), original)
        self.assertTrue(list((self.root/'backups').glob('*/failed-application/ASMarr.dll')))

    def test_concurrent_state_change_is_not_overwritten_and_leaves_scheduler_held(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture_failure'):
            self.deploy(failed=True, changed=True)
        self.assertTrue(self.changed)
        self.assertTrue((self.app/'old-code').is_file())
        self.assertTrue(all(not row[1] for row in self.tasks()))

    def test_failed_payload_activation_restores_old_application(self):
        original = self.tasks()
        rename = Path.rename
        def activate(path, destination):
            if path.name.startswith('asmarr-production-release-'):
                raise OSError('activation_failed')
            return rename(path, destination)
        with patch.object(Path, 'rename', activate), self.assertRaisesRegex(OSError, 'activation_failed'):
            self.deploy()
        self.assertTrue((self.app/'old-code').is_file())
        self.assertEqual(self.tasks(), original)

    def test_wrong_ci_never_stops_service_or_holds_tasks(self):
        self.ci.json.return_value['head_sha'] = 'f'*40
        original = self.tasks()
        with self.assertRaisesRegex(ValueError, 'exact_release_ci'):
            self.deploy()
        self.service.assert_not_called()
        self.assertEqual(self.tasks(), original)

    def test_active_work_prevents_holding_schedule(self):
        original = self.tasks()
        with sqlite3.connect(self.state) as db: db.execute("INSERT INTO commands VALUES('running')")
        with self.assertRaisesRegex(ValueError, 'active_commands'):
            upgrade.hold_tasks()
        self.assertEqual(self.tasks(), original)

    def test_new_task_inventory_prevents_partial_schedule_restore(self):
        original = upgrade.hold_tasks()
        with sqlite3.connect(self.state) as db: db.execute("INSERT INTO tasks VALUES('new',0,'later')")
        with self.assertRaisesRegex(ValueError, 'inventory_changed'):
            upgrade.restore_tasks(original)
        self.assertTrue(all(not row[1] for row in self.tasks()))

    def test_opted_in_or_unaccepted_instance_is_rejected(self):
        snapshot = upgrade.invariants()
        accepted = dict(self.old, testedCommit='a'*40, ready=True)
        for changes in ({'mode':'shadow'},{'videoOptIns':1},{'videoAssets':1},{'activeCommands':1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                upgrade.validate_baseline(dict(snapshot,**changes),self.old,accepted,'d'*64)
        with self.assertRaisesRegex(ValueError, 'acceptance'):
            upgrade.validate_baseline(snapshot,self.old,dict(accepted,testedCommit='f'*40),'d'*64)

    def test_revision_requires_matching_runtime_and_plex_evidence_not_a_ready_flag(self):
        runtime = dict(testedCommit='a'*40,artifactSha256='d'*64,passed=True,
                       artifactUnchanged=True,networkPolicy='connections blocked',
                       tests=list(upgrade.TESTS)+['test_video.VideoTests.test_fixture'],
                       testsRun=len(upgrade.TESTS)+1,fixtureManifest={'sourceCommit':'a'*40},
                       errors=[],failures=[],skipped=[])
        (self.acceptance/'runtime.json').write_text(json.dumps(runtime))
        (self.acceptance/'plex.json').write_text(json.dumps({'passed':True,'changes':[]}))
        prior = dict(self.old,ready=True,schedulerRestored=True,runtimeEvidence='runtime.json',plexPreservation='plex.json')
        (self.acceptance/'prior.json').write_text(json.dumps(prior))
        self.assertTrue(upgrade.prior_certification('prior.json',self.old)['ready'])
        runtime['testedCommit'] = 'f'*40
        (self.acceptance/'runtime.json').write_text(json.dumps(runtime))
        with self.assertRaisesRegex(ValueError, 'does_not_certify'):
            upgrade.prior_certification('prior.json',self.old)
        with self.assertRaisesRegex(ValueError, 'outside_protected'):
            upgrade.read_protected(self.root/'untrusted.json')

    def test_existing_video_requires_full_boundary_and_zero_resumable_work(self):
        snapshot=upgrade.invariants()
        snapshot.update(videoOptIns=1,videoAssets=374,videoCandidates=374,videoBackfills=1,
                        videoPreservationHash='e'*64,videoFilesHash='e'*64,videoInventoryHash='e'*64,
                        protectedConfigHash='e'*64,resumableBackfills=0,activeDownloadJobs=0)
        accepted=dict(self.old,testedCommit='a'*40,ready=True)
        upgrade.validate_baseline(snapshot,self.old,accepted,'d'*64,True)
        for change in ({'resumableBackfills':1},{'activeDownloadJobs':1},{'videoFilesHash':''}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                upgrade.validate_baseline(dict(snapshot,**change),self.old,accepted,'d'*64,True)
        with self.assertRaises(ValueError):upgrade.validate_baseline(snapshot,self.old,accepted,'d'*64)

    def test_existing_video_cannot_bypass_exact_prior_certification(self):
        with self.assertRaisesRegex(ValueError,'exact_prior_upgrade'):
            upgrade.deploy(self.archive,self.root/'fixtures','b'*40,123,'c'*64,preserve_existing_video=True)
        self.service.assert_not_called()


class ExistingVideoPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.video=self.root/'video';self.video.mkdir()
        self.file=self.video/'recording.mp4';self.file.write_bytes(b'preserved video')
        self.config=self.root/'config';self.config.mkdir();(self.config/'auth.json').write_text('{}')
        self.state=self.root/'state.db'
        with sqlite3.connect(self.state) as db:
            db.executescript('''CREATE TABLE creators(id INTEGER,name TEXT,monitor_video INTEGER);
                INSERT INTO creators VALUES(1,'Video Creator',0);
                CREATE TABLE identities(id INTEGER,handle TEXT);INSERT INTO identities VALUES(1,'linked');
                CREATE TABLE settings(key TEXT,value TEXT);
                CREATE TABLE video_candidates(id INTEGER,details TEXT);INSERT INTO video_candidates VALUES(1,'original');
                CREATE TABLE backfill_jobs(id TEXT,creator_id INTEGER,state TEXT,cursor TEXT);
                INSERT INTO backfill_jobs VALUES('b',1,'completed','original');
                CREATE TABLE queue(id TEXT,state TEXT);INSERT INTO queue VALUES('q','imported');
                CREATE TABLE downloads(id TEXT);CREATE TABLE aliases(id TEXT);
                CREATE TABLE media_assets(recording_key TEXT,media_kind TEXT,saved_path TEXT,state TEXT,details TEXT);''')
            db.execute("INSERT INTO settings VALUES('video.root',?)",(str(self.video),))
            db.execute("INSERT INTO media_assets VALUES('a','Video',?,'imported','{}')",(str(self.file),))
        context=patch.multiple(upgrade,DB=self.state,CONFIG=self.config);context.start();self.addCleanup(context.stop)

    def update(self,sql,*args):
        with sqlite3.connect(self.state) as db:db.execute(sql,args)

    def test_video_rows_candidates_backfills_identities_and_settings_are_fingerprinted(self):
        for statement in ("UPDATE video_candidates SET details='changed'", "UPDATE backfill_jobs SET cursor='changed'",
                          "UPDATE identities SET handle='changed'", "INSERT INTO settings VALUES('custom','preserved')",
                          "UPDATE creators SET name='changed'"):
            before=upgrade.existing_video_state()['videoPreservationHash'];self.update(statement)
            self.assertNotEqual(before,upgrade.existing_video_state()['videoPreservationHash'])

    def test_video_content_and_artwork_inventory_changes_are_detected(self):
        before=upgrade.existing_video_state()
        self.file.write_bytes(b'changed video')
        after=upgrade.existing_video_state()
        self.assertNotEqual(before['videoFilesHash'],after['videoFilesHash'])
        (self.video/'artwork.jpg').write_bytes(b'artwork')
        self.assertNotEqual(after['videoInventoryHash'],upgrade.existing_video_state()['videoInventoryHash'])

    def test_missing_or_changed_verified_import_cannot_pass_upgrade_audit(self):
        sha=hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.update('UPDATE media_assets SET details=?',json.dumps({'asmarr_file':{'sha256':sha}}))
        upgrade.existing_video_state()
        self.file.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'digest_mismatch'):upgrade.existing_video_state()
        self.file.unlink()
        with self.assertRaisesRegex(ValueError,'imported_video_missing'):upgrade.existing_video_state()

    def test_resumable_backfill_and_active_downloads_are_reported(self):
        self.update("UPDATE creators SET monitor_video=1")
        self.update("UPDATE backfill_jobs SET state='failed'")
        self.update("UPDATE queue SET state='downloading'")
        state=upgrade.existing_video_state()
        self.assertEqual(1,state['resumableBackfills']);self.assertEqual(1,state['activeDownloadJobs'])

    def test_password_config_and_empty_removal_migration_are_preserved(self):
        before=upgrade.existing_video_state()
        self.update("CREATE TABLE creator_exclusions(name TEXT)")
        self.assertEqual(before,upgrade.existing_video_state())
        (self.config/'auth.json').write_text('{"changed":true}')
        self.assertNotEqual(before['protectedConfigHash'],upgrade.existing_video_state()['protectedConfigHash'])


if __name__ == '__main__': unittest.main()
