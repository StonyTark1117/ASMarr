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
    def test_creator_audio_release_requires_expanded_installed_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            app=Path(name)
            upgrade.require_creator_audio_evidence(app,{'passed':True})
            (app/'providers').mkdir();(app/'providers/creator_audio_sources.py').write_text('# fixture')
            for proof in ({'passed':True}, {'creatorAudioFixtures':True,'tests':[]}):
                with self.assertRaisesRegex(ValueError,'creator_audio_fixtures_not_certified'):
                    upgrade.require_creator_audio_evidence(app,proof)
            upgrade.require_creator_audio_evidence(app,{'creatorAudioFixtures':True,'tests':[
                'test_creator_audio_sources.CreatorAudioSourceTests.test_fixture',
                'test_providers.ProviderTests.test_fixture']})

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

    def test_complete_queued_rows_and_exact_schedule_changes_are_detected(self):
        self.update('CREATE TABLE commands(id TEXT,state TEXT,arguments TEXT,created TEXT)')
        self.update("INSERT INTO commands VALUES('queued','queued','original','original-time')")
        self.update('CREATE TABLE tasks(name TEXT,enabled INTEGER,next_run TEXT)')
        self.update("INSERT INTO tasks VALUES('discovery',1,'original-next')")
        before=upgrade.existing_video_state()
        self.assertEqual(1,before['queuedCommandsCount'])
        self.update("UPDATE commands SET arguments='changed',created='changed-time'")
        after=upgrade.existing_video_state()
        self.assertEqual(1,after['queuedCommandsCount'])
        self.assertNotEqual(before['queuedCommandsHash'],after['queuedCommandsHash'])
        self.update("UPDATE tasks SET next_run='different-next'")
        self.assertNotEqual(before['taskScheduleHash'],upgrade.existing_video_state()['taskScheduleHash'])


class QueuedUpgradeTests(unittest.TestCase):
    def setUp(self):
        ProductionUpgradeTests.setUp(self)
        with sqlite3.connect(self.state) as db:
            db.executescript('''ALTER TABLE commands ADD COLUMN id TEXT;
                ALTER TABLE commands ADD COLUMN arguments TEXT;
                INSERT INTO commands VALUES('queued','keep-me','{"creatorId":40}');
                CREATE TABLE backfill_jobs(id TEXT,state TEXT,cursor TEXT);
                INSERT INTO backfill_jobs VALUES('history','interrupted','{"page":3}');
                CREATE TABLE queue(id TEXT,state TEXT);
                CREATE TABLE media_assets(id TEXT,state TEXT);
                CREATE TABLE creators(id INTEGER,monitored INTEGER,monitor_video INTEGER);
                INSERT INTO creators VALUES(40,1,1);
                CREATE TABLE task_locks(name TEXT PRIMARY KEY,owner TEXT,expires TEXT);''')
        self.maintenance=self.root/'run/maintenance.conf'
        self.emergency=self.root/'etc/dropins'
        self.stage=self.root/'stage';self.stage.mkdir();(self.stage/'ASMarr.dll').write_bytes(b'new')
        self.accepted=dict(self.old,testedCommit='a'*40,ready=True)
        self.proposed={'sourceCommit':'b'*40,'artifactSha256':'c'*64}
        self.mutate=False;self.bad_probe=False;self.failed_fixtures=False
        self.order=[]
        original_snapshot=upgrade.invariants.side_effect
        def snapshot():
            result=original_snapshot()
            with sqlite3.connect(self.state) as db:
                queued=db.execute("SELECT * FROM commands WHERE state='queued' ORDER BY id").fetchall()
                checkpoints=db.execute('SELECT * FROM backfill_jobs').fetchall()
                creators=db.execute('SELECT * FROM creators').fetchall()
                schedules=db.execute('SELECT * FROM tasks ORDER BY name').fetchall()
                running=db.execute("SELECT count(*) FROM commands WHERE state='running'").fetchone()[0]
                running_backfills=db.execute("SELECT count(*) FROM backfill_jobs WHERE state='running'").fetchone()[0]
            sha=lambda rows:hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            result.update(videoPreservationHash=sha([checkpoints,creators]),videoFilesHash='e'*64,videoOptIns=1,
                          videoInventoryHash='e'*64,protectedConfigHash='e'*64,
                          resumableBackfills=1,activeDownloadJobs=0,activeMediaTransfers=0,
                          queuedCommandsCount=len(queued),queuedCommandsHash=sha(queued),
                          runningCommands=running,runningBackfills=running_backfills,
                          taskScheduleHash=sha(schedules))
            return result
        upgrade.invariants.side_effect=snapshot
        def evidence(command,*args):
            self.order.append(command)
            destination=Path(args[args.index('--baseline' if 'capture' in args else '--output')+1])
            if command=='maintenance_probe.py':
                result=dict(self.proposed,passed=not self.bad_probe,providerExecutionDisabled=True,
                            queuedWorkUnchanged=True,syntheticConfigurationOnly=True,productionDatabaseOpened=False)
            else:result={'passed':not self.failed_fixtures}
            destination.write_text(json.dumps(result))
            if command=='runtime_fixtures.py' and self.mutate:
                with sqlite3.connect(self.state) as db:db.execute("UPDATE commands SET arguments='changed' WHERE id='keep-me'")
        def write_dropin(path,content):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        patches=[patch.multiple(upgrade,MAINTENANCE_DROPIN=self.maintenance,EMERGENCY_DROPINS=self.emergency),
                 patch.object(upgrade.os,'geteuid',return_value=0),
                 patch.object(upgrade,'require_service_state'),patch.object(upgrade,'download_client_idle'),
                 patch.object(upgrade,'require_maintenance_status'),
                 patch.object(upgrade,'require_hold_configuration'),
                 patch.object(upgrade,'evidence',side_effect=evidence),
                 patch.object(upgrade,'write_owned_dropin',side_effect=write_dropin),
                 patch.object(upgrade,'remove_owned_dropin',side_effect=lambda path,content:path.unlink())]
        for context in patches:context.start();self.addCleanup(context.stop)
        self.service.side_effect=lambda action:self.order.append(action)

    def run_upgrade(self):
        return upgrade.deploy_preserving_queued(self.stage,self.root/'fixtures',self.proposed,
                    self.ci.json(),self.old,self.accepted,'prior.json','fixture',False)

    def test_queued_upgrade_preserves_exact_commands_checkpoints_schedules_and_monitoring(self):
        before=upgrade.invariants()
        result=self.run_upgrade()
        self.assertTrue(result['queuedWorkPreserved']);self.assertEqual(before,upgrade.invariants())
        self.assertEqual(before,result['preservedBefore']);self.assertEqual(before,result['preservedAfter'])
        self.assertEqual(self.order[0],'maintenance_probe.py')
        self.assertFalse(self.maintenance.exists());self.assertFalse(self.emergency.exists())
        with sqlite3.connect(self.state) as db:
            self.assertEqual(('queued','keep-me','{"creatorId":40}'),db.execute('SELECT * FROM commands').fetchone())
            self.assertEqual('{"page":3}',db.execute('SELECT cursor FROM backfill_jobs').fetchone()[0])
            self.assertEqual((1,1),db.execute('SELECT monitored,monitor_video FROM creators WHERE id=40').fetchone())
            self.assertEqual(0,db.execute('SELECT count(*) FROM task_locks').fetchone()[0])
        self.assertEqual(self.service.call_args_list[-1].args,('start',))

    def test_failed_prelaunch_probe_never_acquires_live_lease_or_stops_service(self):
        self.bad_probe=True
        with self.assertRaisesRegex(ValueError,'probe_not_certified'):self.run_upgrade()
        self.service.assert_not_called();self.assertTrue((self.app/'old-code').exists())
        with sqlite3.connect(self.state) as db:self.assertEqual(0,db.execute('SELECT count(*) FROM task_locks').fetchone()[0])

    def test_running_work_or_busy_lease_is_refused_without_interrupting_worker(self):
        for statement in ("UPDATE commands SET state='running'", "UPDATE backfill_jobs SET state='running'",
                          "INSERT INTO queue VALUES('q','downloading')", "INSERT INTO media_assets VALUES('a','downloading')",
                          "INSERT INTO task_locks VALUES('providers','worker','2999-01-01T00:00:00+00:00')"):
            with self.subTest(statement=statement),sqlite3.connect(self.state) as db:
                db.execute('SAVEPOINT fixture');db.execute(statement);db.commit()
                with self.assertRaises(ValueError):upgrade.acquire_provider_hold()
                db.execute("UPDATE commands SET state='queued'");db.execute("UPDATE backfill_jobs SET state='interrupted'")
                db.execute('DELETE FROM queue');db.execute('DELETE FROM media_assets');db.execute('DELETE FROM task_locks')
        self.service.assert_not_called()

    def test_lease_closes_check_to_stop_race_without_rewriting_queued_rows(self):
        owner=upgrade.acquire_provider_hold()
        with self.assertRaisesRegex(ValueError,'lease_is_busy'):upgrade.acquire_provider_hold()
        upgrade.release_provider_hold(owner)
        with self.assertRaisesRegex(ValueError,'lease_was_lost'):upgrade.release_provider_hold(owner)

    def test_changed_queued_arguments_are_not_overwritten_and_rollback_stays_persistently_stopped(self):
        self.mutate=True
        with self.assertRaisesRegex(ValueError,'queued_work_or_checkpoint_changed'):self.run_upgrade()
        self.assertTrue((self.app/'old-code').exists())
        guards=list(self.emergency.glob('*.conf'));self.assertEqual(1,len(guards))
        marker=Path(guards[0].read_text().split('ConditionPathExists=')[1].strip())
        self.assertFalse(marker.exists());self.assertTrue(self.maintenance.exists())
        self.assertEqual(self.service.call_args_list[-1].args,('stop',))
        with sqlite3.connect(self.state) as db:self.assertEqual('changed',db.execute('SELECT arguments FROM commands').fetchone()[0])
        report=json.loads((marker.parent/'upgrade-failed.json').read_text())
        self.assertTrue(report['requiresOperatorReview']);self.assertTrue(report['serviceStopped'])

    def test_failed_runtime_tests_restore_old_app_but_never_restart_hold_unaware_binary(self):
        self.failed_fixtures=True
        with self.assertRaisesRegex(ValueError,'fixtures_failed'):self.run_upgrade()
        self.assertTrue((self.app/'old-code').exists())
        self.assertEqual(self.service.call_args_list[-1].args,('stop',))
        self.assertTrue(list(self.emergency.glob('*.conf')))

    def test_unproven_runtime_hold_rolls_back_stopped_before_fixture_execution(self):
        with patch.object(upgrade,'require_maintenance_status',side_effect=ValueError('not_held')):
            with self.assertRaisesRegex(ValueError,'not_held'):self.run_upgrade()
        self.assertNotIn('runtime_fixtures.py',self.order)
        self.assertTrue((self.app/'old-code').exists())
        self.assertEqual(self.service.call_args_list[-1].args,('stop',))

    def test_unknown_service_state_after_stop_does_not_replace_application(self):
        with patch.object(upgrade,'require_service_state',side_effect=[None,ValueError('unknown'),None]):
            with self.assertRaisesRegex(ValueError,'unknown'):self.run_upgrade()
        self.assertTrue((self.app/'old-code').exists())
        self.assertTrue(list(self.emergency.glob('*.conf')))

    def test_existing_hold_is_never_overwritten(self):
        self.maintenance.parent.mkdir();self.maintenance.write_text('unrelated')
        with self.assertRaisesRegex(ValueError,'already_exists'):self.run_upgrade()
        self.assertEqual('unrelated',self.maintenance.read_text());self.service.assert_not_called()

    def test_post_release_health_failure_never_stops_or_rolls_back_new_legitimate_work(self):
        checks=0
        def health():
            nonlocal checks
            checks+=1
            if checks==2:
                with sqlite3.connect(self.state) as db:db.execute("UPDATE commands SET state='running'")
                raise RuntimeError('health_response_lost_after_resume')
        with patch.object(upgrade,'healthy',side_effect=health):
            with self.assertRaisesRegex(RuntimeError,'after_resume'):self.run_upgrade()
        self.assertTrue((self.app/'ASMarr.dll').exists());self.assertFalse((self.app/'old-code').exists())
        self.assertEqual(('start',),self.service.call_args_list[-1].args)
        with sqlite3.connect(self.state) as db:
            self.assertEqual('running',db.execute('SELECT state FROM commands').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM task_locks').fetchone()[0])
        report=json.loads((self.root/'backups/production-queued-fixture/upgrade-failed.json').read_text())
        self.assertTrue(report['resumeUnverified']);self.assertTrue(report['runningWorkNotInterrupted'])
        self.assertNotIn('serviceStopped',report);self.assertFalse(report['resumeVerified'])
        self.assertEqual('b'*40,json.loads(self.runtime.read_text())['sourceCommit'])
        pending=json.loads((self.acceptance/'production-video-upgrade-fixture.json').read_text())
        self.assertTrue(pending['heldValidationPassed']);self.assertFalse(pending['ready'])

    def test_emergency_daemon_reload_failure_cannot_skip_stop_lease_or_evidence(self):
        self.failed_fixtures=True
        write=upgrade.write_owned_dropin.side_effect
        def reload_failure(path,content):
            write(path,content)
            if path.parent==self.emergency:raise RuntimeError('reload_failed')
        with patch.object(upgrade,'write_owned_dropin',side_effect=reload_failure):
            with self.assertRaisesRegex(ValueError,'fixtures_failed'):self.run_upgrade()
        self.assertEqual(('stop',),self.service.call_args_list[-1].args)
        self.assertTrue((self.app/'old-code').exists())
        report=json.loads((self.root/'backups/production-queued-fixture/upgrade-failed.json').read_text())
        self.assertIn('startup-guard:RuntimeError',report['recoveryErrors'])
        self.assertTrue(report['serviceStopped']);self.assertTrue(report['providerLeaseRetained'])

    def test_cannot_prove_stop_extends_own_lease_instead_of_expiring_into_live_work(self):
        self.service.side_effect=RuntimeError('stop_unavailable')
        with patch.object(upgrade,'require_service_state',side_effect=[None,ValueError('unknown')]):
            with self.assertRaisesRegex(RuntimeError,'stop_unavailable'):self.run_upgrade()
        self.assertTrue((self.app/'old-code').exists())
        with sqlite3.connect(self.state) as db:
            owner,expires=db.execute('SELECT owner,expires FROM task_locks').fetchone()
        self.assertTrue(owner.startswith('production-upgrade-'));self.assertTrue(expires.startswith('9999-'))
        report=json.loads((self.root/'backups/production-queued-fixture/upgrade-failed.json').read_text())
        self.assertFalse(report['serviceStopped']);self.assertEqual(owner,report['providerLeaseOwner'])

    def test_new_mode_is_opt_in_and_requires_full_prior_video_proof(self):
        snapshot=upgrade.invariants()
        with self.assertRaises(ValueError):upgrade.validate_baseline(snapshot,self.old,self.accepted,'d'*64,True)
        upgrade.validate_baseline(snapshot,self.old,self.accepted,'d'*64,True,True)
        for changes in ({'runningCommands':1},{'runningBackfills':1},{'activeDownloadJobs':1},
                        {'activeMediaTransfers':1},{'queuedCommandsHash':''},{'taskScheduleHash':''}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                upgrade.validate_baseline(dict(snapshot,**changes),self.old,self.accepted,'d'*64,True,True)
        with self.assertRaisesRegex(ValueError,'prior_proof'):
            upgrade.deploy(self.archive,self.root/'fixtures','b'*40,123,'c'*64,preserve_queued_work=True)
        self.service.assert_not_called()


class UpgradeControlTests(unittest.TestCase):
    def test_effective_hold_must_not_be_overridden_by_later_unit_settings(self):
        valid='Environment=OTHER=value ASMARR_MAINTENANCE_HOLD=1\nEnvironmentFiles=\nUnsetEnvironment=\n'
        with patch.object(upgrade.subprocess,'run',return_value=Mock(stdout=valid)):
            upgrade.require_hold_configuration()
        for changed in (valid.replace('HOLD=1','HOLD=0'),valid.replace('EnvironmentFiles=','EnvironmentFiles=/etc/fixture.env'),
                        valid.replace('UnsetEnvironment=','UnsetEnvironment=ASMARR_MAINTENANCE_HOLD'), ''):
            with patch.object(upgrade.subprocess,'run',return_value=Mock(stdout=changed)),self.assertRaises(ValueError):
                upgrade.require_hold_configuration()

    def test_unknown_service_state_fails_closed(self):
        for code,output in ((1,''),(0,'LoadState=not-found\nActiveState=inactive'),
                            (0,'LoadState=loaded\nActiveState=deactivating')):
            with patch.object(upgrade.subprocess,'run',return_value=Mock(returncode=code,stdout=output)),self.assertRaises(ValueError):
                upgrade.require_service_state('inactive')
        with patch.object(upgrade.subprocess,'run',return_value=Mock(returncode=0,stdout='LoadState=loaded\nActiveState=inactive')):
            upgrade.require_service_state('inactive')

    def test_qbit_inventory_is_scoped_read_only_and_rejects_mutating_or_incomplete_jobs(self):
        session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        session.post.return_value=Mock(status_code=200,text='Ok.')
        with tempfile.TemporaryDirectory() as temp:
            config=Path(temp);(config/'integrations.json').write_text(json.dumps({'qbittorrent':{'url':'https://fixture.invalid'}}))
            with patch.object(upgrade,'CONFIG',config),patch.object(upgrade.requests,'Session',return_value=session):
                for state in ('uploading','stoppedUP','stalledUP'):
                    session.get.return_value.json.return_value=[{'category':'asmarr','state':state,'progress':1}]
                    upgrade.download_client_idle()
                for row in ({'category':'asmarr','state':'checkingUP','progress':1},
                            {'category':'asmarr','state':'moving','progress':1},
                            {'category':'asmarr','state':'pausedDL','progress':.5},
                            {'category':'other','state':'uploading','progress':1}):
                    session.get.return_value.json.return_value=[row]
                    with self.assertRaises(ValueError):upgrade.download_client_idle()
        self.assertEqual({'category':'asmarr'},session.get.call_args.kwargs['params'])
        self.assertTrue(all(call.args[0].endswith('/auth/login') for call in session.post.call_args_list))


if __name__ == '__main__': unittest.main()
