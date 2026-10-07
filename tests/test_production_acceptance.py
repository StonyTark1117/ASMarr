import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
import production_acceptance as acceptance
from runtime_fixtures import TESTS


class ProductionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=sqlite3.connect(self.root/'state.db');self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE settings(key TEXT,value TEXT);
            CREATE TABLE assets(key TEXT PRIMARY KEY,saved_path TEXT,acquired INTEGER,state TEXT);
            CREATE TABLE commands(id TEXT,name TEXT,state TEXT,started TEXT,finished TEXT,result TEXT);
            CREATE TABLE history(at TEXT,event TEXT,recording_key TEXT,details TEXT);
            CREATE TABLE queue(id TEXT,download_id TEXT,details TEXT,recording_key TEXT,provider TEXT,media_kind TEXT,state TEXT);
            CREATE TABLE downloads(id TEXT);CREATE TABLE meta(key TEXT,value TEXT);
            CREATE TABLE tasks(name TEXT,enabled INTEGER,interval_seconds INTEGER,next_run TEXT);''')
        self.db.executemany('INSERT INTO settings VALUES(?,?)',[('mode','production'),('root',str(self.root))])
        self.db.executemany('INSERT INTO tasks VALUES(?,0,600,?)',[('discovery','later'),('backup','later')])
        self.plex={'capturedAt':'2026-10-10T03:00:00Z','tracks':{},'playlists':{},'section':'4'}
        for index in range(687):
            path=self.root/(str(index)+'.m4a');path.write_bytes(b'baseline')
            self.db.execute('INSERT INTO assets VALUES(?,?,?,?)',(str(index),str(path),1,'complete'))
            self.plex['tracks'][str(index)]={'paths':[str(path)]}
        original=acceptance.media_snapshot(self.db)
        self.db.execute('INSERT INTO meta VALUES(?,?)',('migration_media_manifest',json.dumps({'files':original})))
        for key,provider in [('direct','direct'),('fallback','qbittorrent')]:
            path=self.root/(key+'.m4a');path.write_bytes(key.encode())
            self.db.execute('INSERT INTO assets VALUES(?,?,?,?)',(key,str(path),acceptance.stamp('2026-10-10T01:00:00Z'),'complete'))
            details={'path':str(path),'provider':provider,'sha256':acceptance.file_digest(path)}
            self.db.execute('INSERT INTO history VALUES(?,?,?,?)',('2026-10-10T01:00:00Z','import',key,json.dumps(details)))
            self.plex['tracks'][key]={'paths':[str(path)]}
        self.db.execute('INSERT INTO queue VALUES(?,?,?,?,?,?,?)',('job','b'*40,'{"guid":"release","indexerId":7}','fallback','qbittorrent','Audio','imported'))
        self.command('direct-command','queue',{'saved':[{'status':'complete','path':str(self.root/'direct.m4a')}]})
        self.command('fallback-command','queue',{'downloads':{'jobs':[{'id':'job','status':'complete','path':str(self.root/'fallback.m4a')}]}})
        self.command('index','plex-verify',{'indexedImported':689,'unindexed':[]})
        self.command('playlist-verify','playlists-verify',{'status':'ok','managedCount':15})
        self.command('repeat-queue','queue',{'saved':[],'failed':[],'fallback':[],'downloads':{'jobs':[]}})
        self.command('repeat-plex','plex',{'status':'not_needed'})
        self.command('repeat-playlists','playlists',{'status':'healthy','created':0,'updated':0,'tagged':0})
        self.deployment={'sourceCommit':'a'*40,'artifactSha256':'b'*64}
        self.binding={'testedCommit':'a'*40,'artifactSha256':'b'*64}
        self.proof=dict(self.binding,directKey='direct',fallbackKey='fallback',directCommandId='direct-command',fallbackCommandId='fallback-command',indexCommandId='index',playlistCommandId='playlist-verify')
        self.cutover=dict(self.binding,ready=True,plexPreservation={'passed':True})
        self.transition={'startedAt':'2026-10-10T00:00:00Z','taskEnabled':{'discovery':1,'backup':0}}
        self.outages=dict(self.binding,passed=True,artifactUnchanged=True,networkPolicy='connections blocked',tests=list(TESTS),testsRun=6,errors=[],failures=[],skipped=[])
        self.outages['fixtureManifest']={'sourceCommit':'a'*40,'files':{name:'a'*40 for name in ('tests/test_acquisition.py','tests/test_legacy_parsers.py','tests/test_playlists.py')}}
        state=acceptance.snapshot(self.db,self.plex)
        self.repeat=dict(self.binding,startedAt='2026-10-10T02:00:00Z',finishedAt='2026-10-10T03:00:00Z',before=state,after=state,commands={'queue':'repeat-queue','plex':'repeat-plex','playlists':'repeat-playlists'})
        self.probe=patch.object(acceptance.subprocess,'run',return_value=type('Probe',(),{'returncode':0,'stdout':'{"format":{"duration":"30"},"streams":[{"codec_type":"audio"}]}'})())
        self.probe.start();self.db.commit()

    def command(self,id,name,result):
        self.db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)',(id,name,'completed','2026-10-10T02:01:00Z','2026-10-10T02:02:00Z',json.dumps(result)))

    def report(self):
        return acceptance.inspect(self.db,self.proof,self.deployment,self.cutover,self.transition,self.outages,self.repeat,self.plex,{'passed':True},'b'*64)

    def tearDown(self):
        self.probe.stop();self.db.close();self.temp.cleanup()

    def test_complete_bound_live_evidence_passes(self):
        report=self.report();self.assertTrue(report['ready'],report['checks'])

    def test_shadow_mode_cannot_enable_scheduler(self):
        self.db.execute("UPDATE settings SET value='shadow' WHERE key='mode'")
        self.assertFalse(self.report()['ready'])

    def test_stale_release_evidence_is_rejected(self):
        self.proof['testedCommit']='c'*40
        self.assertFalse(self.report()['checks']['releaseMatches'])

    def test_direct_boolean_claim_without_real_import_cannot_pass(self):
        self.db.execute("DELETE FROM history WHERE recording_key='direct'")
        self.assertFalse(self.report()['checks']['directAcquisitionAndIndex'])

    def test_unfinished_command_is_not_acquisition_evidence(self):
        self.db.execute("UPDATE commands SET state='running' WHERE id='direct-command'")
        self.assertFalse(self.report()['ready'])

    def test_canary_missing_from_plex_is_not_accepted(self):
        del self.plex['tracks']['direct']
        self.assertFalse(self.report()['checks']['directAcquisitionAndIndex'])

    def test_fallback_must_have_immutable_audio_job(self):
        self.db.execute("UPDATE queue SET media_kind='Video'")
        self.assertFalse(self.report()['checks']['controlledFallbackAndIndex'])

    def test_changed_fallback_destination_is_not_verified_copy(self):
        (self.root/'fallback.m4a').write_bytes(b'changed')
        self.assertFalse(self.report()['checks']['controlledFallbackAndIndex'])

    def test_modified_migrated_media_is_rejected(self):
        (self.root/'0.m4a').write_bytes(b'changed old media')
        self.assertFalse(self.report()['checks']['migratedMediaPreserved'])

    def test_repeat_with_new_download_is_not_idempotent(self):
        self.db.execute("UPDATE commands SET result=? WHERE id='repeat-queue'",(json.dumps({'saved':[{'status':'complete'}]}),))
        self.assertFalse(self.report()['checks']['repeatCycleIdempotent'])

    def test_repeat_with_playlist_mutation_is_rejected(self):
        self.db.execute("UPDATE commands SET result=? WHERE id='repeat-playlists'",(json.dumps({'status':'healthy','created':0,'updated':1,'tagged':0}),))
        self.assertFalse(self.report()['checks']['repeatCycleIdempotent'])

    def test_repeat_with_plex_outage_is_not_accepted(self):
        self.db.execute("UPDATE commands SET result=? WHERE id='repeat-plex'",(json.dumps({'status':'http_503'}),))
        self.assertFalse(self.report()['checks']['repeatCycleIdempotent'])

    def test_missing_queue_result_fields_are_not_an_empty_success(self):
        self.db.execute("UPDATE commands SET result='{}' WHERE id='repeat-queue'")
        self.assertFalse(self.report()['checks']['repeatCycleIdempotent'])

    def test_repeat_digest_must_match_current_state(self):
        self.db.execute("INSERT INTO downloads VALUES('new')")
        self.assertFalse(self.report()['checks']['repeatCycleIdempotent'])

    def test_repeat_end_snapshot_must_follow_completed_commands(self):
        self.repeat['finishedAt']='2026-10-10T02:01:00Z'
        self.assertFalse(self.report()['checks']['repeatCycleIdempotent'])

    def test_partial_or_skipped_outage_evidence_is_rejected(self):
        self.outages['skipped']=[TESTS[0]]
        self.assertFalse(self.report()['checks']['runtimeOutagesPassed'])

    def test_outage_boolean_without_fixture_provenance_is_rejected(self):
        del self.outages['fixtureManifest']
        self.assertFalse(self.report()['checks']['runtimeOutagesPassed'])

    def test_existing_approval_cannot_repeat_scheduler_changes(self):
        (self.root/'production-accepted.json').write_text('{}')
        with patch.object(acceptance,'ACCEPTANCE',self.root),patch.object(acceptance,'audit') as audit,patch.object(acceptance.subprocess,'run') as run:
            self.assertFalse(acceptance.enable()['ready']);audit.assert_not_called();run.assert_not_called()

    def test_valid_enable_restores_only_saved_task_states(self):
        (self.root/'transition.json').write_text(json.dumps(self.transition))
        with patch.object(acceptance,'ACCEPTANCE',self.root),patch.object(acceptance,'DB',self.root/'state.db'),patch.object(acceptance,'audit',return_value={'ready':True}),patch('plex_invariants.write_protected') as evidence,patch.object(acceptance.subprocess,'run') as run:
            self.assertTrue(acceptance.enable()['ready'])
            self.assertEqual([['systemctl','stop','asmarr'],['systemctl','start','asmarr']],[call.args[0] for call in run.call_args_list])
            evidence.assert_called_once()
        self.assertEqual(dict(self.db.execute('SELECT name,enabled FROM tasks')),self.transition['taskEnabled'])
        self.assertTrue(all(row[0]!='later' for row in self.db.execute('SELECT next_run FROM tasks')))

    def test_unknown_scheduler_configuration_requires_review(self):
        self.transition['taskEnabled']['unknown']=1
        self.assertFalse(self.report()['checks']['savedSchedulerMatches'])

    def test_failed_gate_never_stops_service_or_changes_scheduler(self):
        with patch.object(acceptance,'audit',return_value={'ready':False}),patch.object(acceptance.subprocess,'run') as run:
            self.assertFalse(acceptance.enable()['ready']);run.assert_not_called()

    def test_recheck_failure_restarts_service_without_enabling_tasks(self):
        with patch.object(acceptance,'ACCEPTANCE',self.root),patch.object(acceptance,'audit',side_effect=[{'ready':True},ValueError('changed')]),patch.object(acceptance.subprocess,'run') as run:
            with self.assertRaises(ValueError):acceptance.enable()
            self.assertEqual([['systemctl','stop','asmarr'],['systemctl','start','asmarr']],[call.args[0] for call in run.call_args_list])
        self.assertEqual(self.db.execute('SELECT sum(enabled) FROM tasks').fetchone()[0],0)

    def test_disabled_but_running_legacy_timer_is_rejected(self):
        proof=dict(self.proof,outageFile='outages.json',repeatFile='repeat.json')
        evidence={'deployed-release.json':self.deployment,'production-acceptance.json':proof,
                  'cutover.json':dict(self.cutover,plexBaseline='baseline.json'),
                  'transition.json':self.transition,'outages.json':self.outages,
                  'repeat.json':self.repeat,'baseline.json':self.plex}
        def status(args,**kwargs):
            if args[0]=='ffprobe':text='{"format":{"duration":"30"},"streams":[{"codec_type":"audio"}]}'
            elif args[1]=='is-enabled':text='disabled'
            else:text='active' if args[2]=='asmr-scraper.timer' else 'inactive'
            return type('Result',(),{'returncode':0,'stdout':text})()
        with patch.object(acceptance,'DB',self.root/'state.db'),patch.object(acceptance,'read_evidence',side_effect=lambda name:evidence[name]),patch.object(acceptance,'artifact_hash',return_value='b'*64),patch('plex_invariants.runtime_snapshot',return_value=self.plex),patch('plex_invariants.compare',return_value={'passed':True}),patch.object(acceptance.subprocess,'run',side_effect=status):
            report=acceptance.audit()
        self.assertTrue(report['checks']['legacyTimerDisabled'])
        self.assertFalse(report['checks']['legacyTimerStopped'])
        self.assertFalse(report['ready'])
