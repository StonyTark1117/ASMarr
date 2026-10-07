"""Cutover/rollback safety tests; no live services or production paths."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cutover_ops', Path(__file__).resolve().parents[1] / 'ops/cutover.py')
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ops'))
from release_integrity import artifact_hash


class CutoverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(cutover, DB=self.root/'state.db', ACCEPTANCE=self.root/'acceptance',
                                    PRODUCTION_OVERRIDE=self.root/'units'/'production.conf',APPLICATION=self.root/'app')
        self.paths.start()
        cutover.ACCEPTANCE.mkdir()
        with sqlite3.connect(cutover.DB) as db:
            db.executescript('CREATE TABLE shadow_cycles(started TEXT,comparison TEXT,clean INTEGER);'
                             'CREATE TABLE migration_audits(id INTEGER PRIMARY KEY,result TEXT);'
                             'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);'
                             'CREATE TABLE tasks(name TEXT PRIMARY KEY,enabled INTEGER);')
            db.execute('INSERT INTO migration_audits VALUES(1,?)', (json.dumps({'passed': True}),))
            db.execute("INSERT INTO settings VALUES('mode','shadow')")
            db.execute("INSERT INTO tasks VALUES('discovery',0)")
        self.now = dt.datetime(2026,10,10,12,tzinfo=dt.timezone.utc)
        evidence = dict.fromkeys(['providerFixtures','sqliteIntegration','browserAcceptance','playlistRecovery'], True)
        cutover.APPLICATION.mkdir();(cutover.APPLICATION/'ASMarr.dll').write_bytes(b'tested application')
        digest=artifact_hash(cutover.APPLICATION)
        evidence.update(testedCommit='a'*40,artifactSha256=digest)
        (cutover.ACCEPTANCE/'deployed-release.json').write_text(json.dumps({'sourceCommit':'a'*40,'artifactSha256':digest}))
        (cutover.ACCEPTANCE/'pre-cutover.json').write_text(json.dumps(evidence))

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def cycle(self, stamp, **overrides):
        comparison = dict.fromkeys(cutover.PARITY_FIELDS, True)
        comparison['legacyImplementationSha256'] = 'a'*64
        comparison.update(overrides)
        with sqlite3.connect(cutover.DB) as db:
            db.execute('INSERT INTO shadow_cycles VALUES(?,?,1)', (stamp,json.dumps(comparison)))

    def test_three_distinct_recent_daily_cycles_pass(self):
        for day in (8,9,10): self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        self.assertTrue(cutover.preflight(self.now)['ready'])

    def test_three_same_day_runs_do_not_satisfy_daily_gate(self):
        for hour in (8,9,10): self.cycle(f'2026-10-10T{hour:02d}:00:00Z')
        self.assertFalse(cutover.preflight(self.now)['ready'])

    def test_clean_flag_cannot_override_failed_parity(self):
        for day in (8,9,10): self.cycle(f'2026-10-{day:02d}T11:00:00Z',mediaUnchanged=False)
        self.assertEqual([],cutover.preflight(self.now)['qualifiedDays'])

    def test_future_observations_are_not_accepted(self):
        for day in (10,11,12): self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        self.assertFalse(cutover.preflight(self.now)['ready'])

    def test_stale_observations_are_not_accepted(self):
        for day in (6,7,8): self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        self.assertFalse(cutover.preflight(self.now)['ready'])

    def test_missing_approval_does_not_use_historical_audio_baseline(self):
        for day in (8,9,10): self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        (cutover.ACCEPTANCE/'pre-cutover.json').rename(cutover.ACCEPTANCE/'audio-baseline.json')
        self.assertFalse(cutover.preflight(self.now)['ready'])

    def test_existing_production_mode_cannot_reenter_migration_cutover(self):
        for day in (8,9,10): self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        with sqlite3.connect(cutover.DB) as db:db.execute("UPDATE settings SET value='production'")
        self.assertFalse(cutover.preflight(self.now)['ready'])

    def test_changed_application_dll_invalidates_otherwise_green_approval(self):
        for day in (8,9,10):self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        (cutover.APPLICATION/'ASMarr.dll').write_bytes(b'unverified replacement')
        report=cutover.preflight(self.now)
        self.assertFalse(report['testedDeploymentMatches']);self.assertFalse(report['ready'])

    def test_stale_test_commit_does_not_approve_another_deployment(self):
        for day in (8,9,10):self.cycle(f'2026-10-{day:02d}T11:00:00Z')
        manifest=cutover.ACCEPTANCE/'deployed-release.json'
        value=json.loads(manifest.read_text());value['sourceCommit']='b'*40;manifest.write_text(json.dumps(value))
        self.assertFalse(cutover.preflight(self.now)['ready'])

    def test_bytecode_cache_does_not_invalidate_immutable_release(self):
        before=artifact_hash(cutover.APPLICATION)
        cache=cutover.APPLICATION/'providers'/'__pycache__';cache.mkdir(parents=True)
        (cache/'scraper.pyc').write_bytes(b'runtime bytecode')
        self.assertEqual(before,artifact_hash(cutover.APPLICATION))

    def test_symlink_outside_application_is_never_hashed(self):
        (cutover.APPLICATION/'external').symlink_to(self.root/'state.db')
        with self.assertRaises(ValueError):artifact_hash(cutover.APPLICATION)

    def test_readonly_preflight_does_not_create_missing_database(self):
        cutover.DB=self.root/'missing.db'
        with self.assertRaises(sqlite3.OperationalError):cutover.preflight(self.now)
        self.assertFalse(cutover.DB.exists())

    def test_timeout_retains_handle_and_never_restarts_command(self):
        submitted=[]
        smoke=types.SimpleNamespace(command=lambda name: submitted.append(name) or 123,
                                    wait=lambda handle,deadline: {'status':'running','commandId':handle})
        with patch.dict(sys.modules,{'smoke':smoke}):
            with self.assertRaisesRegex(RuntimeError,'123'): cutover.run_command('discovery',1)
        self.assertEqual(['discovery'],submitted)

    def test_degraded_cycle_is_not_success(self):
        smoke=types.SimpleNamespace(command=lambda name: 1,wait=lambda *args: {'status':'degraded'})
        with patch.dict(sys.modules,{'smoke':smoke}):
            with self.assertRaises(RuntimeError):cutover.run_command('discovery',1)

    def test_rollback_restores_readonly_unit_and_saved_schedule_without_touching_media(self):
        with sqlite3.connect(cutover.DB) as db:db.execute("UPDATE settings SET value='production'")
        cutover.PRODUCTION_OVERRIDE.parent.mkdir()
        cutover.PRODUCTION_OVERRIDE.write_text('[Service]\nReadOnlyPaths=\n')
        (cutover.ACCEPTANCE/'transition.json').write_text(json.dumps({'taskEnabled':{'discovery':1}}))
        with patch.object(cutover.subprocess,'run') as run:
            cutover.rollback()
        self.assertFalse(cutover.PRODUCTION_OVERRIDE.exists())
        self.assertFalse((cutover.ACCEPTANCE/'transition.json').exists())
        self.assertEqual(1,len(list(cutover.ACCEPTANCE.glob('production-override-rolled-back-*.conf'))))
        with sqlite3.connect(cutover.DB) as db:
            self.assertEqual('shadow',db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0])
            self.assertEqual(1,db.execute("SELECT enabled FROM tasks WHERE name='discovery'").fetchone()[0])
        self.assertEqual([
            ['systemctl','stop','asmarr'], ['systemctl','daemon-reload'],
            ['systemctl','enable','--now','asmr-scraper.timer']], [call.args[0] for call in run.call_args_list])


if __name__ == '__main__': unittest.main()
