import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import shadow_deploy as deploy


class Response:
    def __init__(self,value):self.value=value;self.status_code=200
    def json(self):return self.value
    def raise_for_status(self):pass


class ShadowDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.stage=self.root/'stage';self.stage.mkdir()
        self.archive=self.root/'release.tar.gz'

    def tearDown(self):self.temp.cleanup()

    def archive_files(self,entries):
        with tarfile.open(self.archive,'w:gz') as tar:
            for name,data,kind,mode in entries:
                entry=tarfile.TarInfo(name);entry.type=kind;entry.mode=mode
                if kind==tarfile.SYMTYPE:entry.linkname='/outside'
                else:entry.size=len(data)
                tar.addfile(entry,io.BytesIO(data) if kind==tarfile.REGTYPE else None)

    def valid_archive(self):
        self.archive_files([(name,b'new',tarfile.REGTYPE,0o755 if name=='ASMarr' else 0o644) for name in ('ASMarr','ASMarr.dll','wwwroot/index.html')])

    def test_valid_regular_payload_extracts(self):
        self.valid_archive();deploy.extract_payload(self.archive,self.stage)
        self.assertTrue((self.stage/'ASMarr.dll').is_file())

    def test_traversal_symlink_and_special_modes_are_rejected(self):
        for entry in [('../escape',b'x',tarfile.REGTYPE,0o644),('link',b'',tarfile.SYMTYPE,0o777),('tool',b'x',tarfile.REGTYPE,0o4755)]:
            with self.subTest(entry=entry):
                self.archive_files([entry])
                with self.assertRaises(ValueError):deploy.extract_payload(self.archive,self.stage)
        self.assertFalse((self.root/'escape').exists())

    def test_incomplete_payload_cannot_replace_application(self):
        self.archive_files([('ASMarr',b'x',tarfile.REGTYPE,0o755)])
        with self.assertRaisesRegex(ValueError,'incomplete'):deploy.extract_payload(self.archive,self.stage)

    def test_production_active_work_and_video_optins_require_review(self):
        baseline={'mode':'shadow','integrity':'ok','activeCommands':0,'videoOptIns':0,'videoAssets':0,'videoCandidates':0,'videoBackfills':0,'queueRows':0}
        deploy.validate_idle(baseline)
        for changed in ({'mode':'production'},{'activeCommands':1},{'videoOptIns':1},{'queueRows':1}):
            with self.assertRaises(ValueError):deploy.validate_idle(dict(baseline,**changed))

    def fixture_deploy(self,healthy=True,ci_success=True):
        self.valid_archive()
        application=self.root/'app';application.mkdir();(application/'old-code').write_bytes(b'old')
        config=self.root/'config';config.mkdir();(config/'auth.json').write_text('{"apiKey":"fixture-only"}')
        state=self.root/'state.db'
        with sqlite3.connect(state) as db:db.execute('CREATE TABLE original(value TEXT)');db.execute("INSERT INTO original VALUES('preserve')")
        runtime=self.root/'deployed-release-runtime.json';runtime.write_text('{"sourceCommit":"old"}')
        baseline={'mode':'shadow','integrity':'ok','activeCommands':0,'videoOptIns':0,'videoAssets':0,'videoCandidates':0,'videoBackfills':0,'queueRows':0,'savedPaths':1}
        def request(url,**kwargs):
            if url.startswith('https://api.github.com'):
                return Response({'head_sha':'a'*40,'status':'completed','conclusion':'success' if ci_success else 'failure'})
            if url.endswith('/healthz'):return Response({'application':'ASMarr' if healthy else 'wrong'})
            return Response([{'audio_completed':1}])
        def system(args,**kwargs):
            return type('Result',(),{'returncode':0,'stdout':'/mnt/cephfs/media/asmr /mnt/cephfs/media/asmr-video /mnt/downloads/asmarr' if 'show' in args else b'unit'})()
        patches=[patch.multiple(deploy,DB=state,APPLICATION=application,CONFIG=config,ACCEPTANCE=self.root/'acceptance',BACKUPS=self.root/'backups',RUNTIME_RELEASE=runtime),patch.object(deploy,'invariants',return_value=baseline),patch.object(deploy,'artifact_hash',return_value='b'*64),patch.object(deploy.os,'chown'),patch.object(deploy.requests,'get',side_effect=request),patch.object(deploy.subprocess,'run',side_effect=system),patch.object(deploy.time,'sleep')]
        for value in patches:value.start();self.addCleanup(value.stop)
        return application,state

    def test_exact_green_release_preserves_online_backup_and_records_manifest(self):
        application,state=self.fixture_deploy()
        report=deploy.deploy(self.archive,'a'*40,123,'b'*64,'c'*40)
        self.assertEqual(report['before'],report['after'])
        self.assertTrue((Path(report['backup'])/'application/old-code').is_file())
        with sqlite3.connect(Path(report['backup'])/'state.db') as db:self.assertEqual(db.execute('SELECT value FROM original').fetchone()[0],'preserve')
        self.assertEqual(json.loads((self.root/'acceptance/deployed-release.json').read_text())['sourceCommit'],'a'*40)
        runtime=json.loads((self.root/'deployed-release-runtime.json').read_text())
        self.assertEqual((runtime['sourceCommit'],runtime['artifactSha256']),('a'*40,'b'*64))
        self.assertEqual((self.root/'deployed-release-runtime.json').stat().st_mode&0o777,0o640)
        self.assertTrue((application/'ASMarr.dll').is_file())

    def test_unhealthy_release_restores_previous_application_without_db_rewrite(self):
        application,state=self.fixture_deploy(healthy=False)
        with self.assertRaisesRegex(RuntimeError,'healthy'):deploy.deploy(self.archive,'a'*40,123,'b'*64,'c'*40)
        self.assertTrue((application/'old-code').is_file())
        self.assertEqual(json.loads((self.root/'deployed-release-runtime.json').read_text()),{'sourceCommit':'old'})
        self.assertTrue(list((self.root/'backups').glob('*/failed-application/ASMarr.dll')))
        with sqlite3.connect(state) as db:self.assertEqual(db.execute('SELECT value FROM original').fetchone()[0],'preserve')

    def test_failed_ci_never_stops_existing_service(self):
        application,state=self.fixture_deploy(ci_success=False)
        with patch.object(deploy.subprocess,'run') as system:
            with self.assertRaisesRegex(ValueError,'ci'):deploy.deploy(self.archive,'a'*40,123,'b'*64,'c'*40)
            system.assert_not_called()
        self.assertTrue((application/'old-code').is_file())
