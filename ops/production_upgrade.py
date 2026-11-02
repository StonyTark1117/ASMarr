"""Backed-up production upgrade with explicit existing-video preservation.

This is not cutover approval or live video acceptance. It preserves the original
audio evidence, verifies the replacement in isolation, and restores scheduling.
No media, source settings, client configuration, or creator opt-ins are changed.
Default first-video upgrades still require no video state. Follow-up upgrades
require explicit opt-in, exact prior certification, and expanded preservation.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import requests
import urllib3
from release_integrity import artifact_hash
from runtime_fixtures import TESTS, verify_fixture_files
from shadow_deploy import extract_payload, invariants as audio_invariants

DB = Path('/var/lib/asmarr/asmarr.db')
APPLICATION = Path('/opt/asmarr')
CONFIG = Path('/etc/asmarr')
ACCEPTANCE = Path('/var/lib/asmarr/acceptance')
BACKUPS = Path('/root/asmarr-rollout-backups')
RUNTIME = DB.parent / 'deployed-release-runtime.json'
OPS = Path(__file__).resolve().parent


def existing_video_state():
    """Full preservation boundary for upgrades after video enrollment exists."""
    digest = lambda value: hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as db:
        tables = ('creators','identities','settings','video_candidates','backfill_jobs','queue','downloads','aliases','creator_exclusions')
        rows = {}
        for table in tables:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            # The removal migration adds an empty exclusions table; absent and
            # empty are equivalent, but preexisting exclusions must be retained.
            rows[table] = sorted(db.execute('SELECT * FROM '+table).fetchall(),key=repr) if exists else []
        rows['video'] = db.execute("SELECT * FROM media_assets WHERE media_kind='Video' ORDER BY recording_key").fetchall()
        resumable = db.execute("SELECT count(*) FROM backfill_jobs b JOIN creators c ON c.id=b.creator_id WHERE c.monitor_video=1 AND b.state IN ('queued','running','failed','interrupted')").fetchone()[0]
        transfers = db.execute("SELECT count(*) FROM queue WHERE state IN ('submitting','downloading','importing')").fetchone()[0]
        root_row = db.execute("SELECT value FROM settings WHERE key='video.root'").fetchone()
        root = Path(root_row[0]) if root_row else None
        media = []
        for path, state, details in db.execute("SELECT saved_path,state,details FROM media_assets WHERE media_kind='Video' AND saved_path IS NOT NULL"):
            path = Path(path)
            if root is None or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('preserved_video_outside_library')
            if not path.is_file():
                if state == 'imported':raise ValueError('preserved_imported_video_missing')
                media.append((str(path),'missing'));continue
            before = path.stat()
            with path.open('rb') as stream:sha = hashlib.file_digest(stream,'sha256').hexdigest()
            after = path.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                raise ValueError('preserved_video_changed_during_audit')
            expected = json.loads(details or '{}').get('asmarr_file',{}).get('sha256')
            if expected and expected != sha:raise ValueError('preserved_video_digest_mismatch')
            media.append((str(path),before.st_size,before.st_mtime_ns,before.st_ino,sha))
        # Retain artwork/untracked files as well, without following symlinks.
        inventory = []
        if root and root.exists():
            def fail(error):raise error
            for folder, directories, files in os.walk(root,followlinks=False,onerror=fail):
                for name in sorted(directories+files):
                    path=Path(folder)/name;info=path.lstat()
                    inventory.append((str(path),info.st_mode,info.st_size,info.st_mtime_ns,info.st_ino))
        configuration = []
        for path in sorted(CONFIG.rglob('*')):
            if path.is_symlink():raise ValueError('protected_config_symlink_requires_review')
            if path.is_file():
                info=path.stat();configuration.append((str(path.relative_to(CONFIG)),hashlib.sha256(path.read_bytes()).hexdigest(),info.st_mode,info.st_uid,info.st_gid))
        return {'videoPreservationHash':digest(rows),'videoFilesHash':digest(sorted(media,key=repr)),
                'videoInventoryHash':digest(sorted(inventory,key=repr)),'protectedConfigHash':digest(configuration),
                'resumableBackfills':resumable,'activeDownloadJobs':transfers}


def invariants():
    return dict(audio_invariants(), **existing_video_state())


def protected_json(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.upgrade-tmp')
    with temporary.open('x') as output:
        temporary.chmod(mode)
        json.dump(value, output, indent=2)
        output.flush(); os.fsync(output.fileno())
    if mode == 0o640:
        owner = DB.stat(); os.chown(temporary, owner.st_uid, owner.st_gid)
    temporary.replace(path)


def validate_baseline(snapshot, release, accepted, installed_hash, preserve_existing_video=False):
    if snapshot['mode'] != 'production' or snapshot['integrity'] != 'ok' or snapshot['activeCommands']:
        raise ValueError('upgrade_requires_healthy_idle_production')
    if preserve_existing_video:
        required=('videoPreservationHash','videoFilesHash','videoInventoryHash','protectedConfigHash')
        if any(not re.fullmatch('[a-f0-9]{64}',snapshot.get(key,'')) for key in required):
            raise ValueError('full_video_preservation_boundary_required')
        if snapshot.get('resumableBackfills') != 0 or snapshot.get('activeDownloadJobs') != 0:
            raise ValueError('existing_video_must_be_idle_and_not_resumable')
    elif any(snapshot[key] for key in ('videoOptIns','videoAssets','videoCandidates','videoBackfills')):
        raise ValueError('first_video_upgrade_requires_untouched_video_baseline')
    if (accepted.get('ready') is not True or accepted.get('testedCommit') != release.get('sourceCommit')
            or accepted.get('artifactSha256') != installed_hash
            or release.get('artifactSha256') != installed_hash):
        raise ValueError('existing_audio_acceptance_does_not_match_installed_release')


def hold_tasks():
    with sqlite3.connect(DB, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running')").fetchone()[0]:
            raise ValueError('active_commands_appeared_before_scheduler_hold')
        tasks = db.execute('SELECT name,enabled,next_run FROM tasks ORDER BY name').fetchall()
        db.execute('UPDATE tasks SET enabled=0')
    return tasks


def restore_tasks(tasks):
    with sqlite3.connect(DB, timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        if {r[0] for r in db.execute('SELECT name FROM tasks')} != {r[0] for r in tasks}:
            raise ValueError('task_inventory_changed_during_upgrade')
        db.executemany('UPDATE tasks SET enabled=?,next_run=? WHERE name=?',
                       [(enabled, next_run, name) for name, enabled, next_run in tasks])


def healthy():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    for _ in range(30):
        try:
            result = requests.get('https://127.0.0.1:8787/healthz', verify=False, timeout=2)
            if result.status_code == 200 and result.json().get('application') == 'ASMarr':
                return
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('replacement_application_unhealthy')


def system(action):
    subprocess.run(['systemctl', action, 'asmarr'], check=True)


def evidence(command, *arguments):
    subprocess.run([sys.executable, str(OPS / command), *map(str, arguments)], check=True)


def read_protected(path):
    path = Path(path)
    if not path.is_absolute():
        path = ACCEPTANCE / path
    if path.is_symlink() or not path.resolve().is_relative_to(ACCEPTANCE.resolve()):
        raise ValueError('upgrade_evidence_outside_protected_directory')
    return json.loads(path.read_text())


def prior_certification(path, release):
    prior = read_protected(path)
    runtime = read_protected(prior['runtimeEvidence'])
    plex = read_protected(prior['plexPreservation'])
    if (prior.get('ready') is not True or prior.get('schedulerRestored') is not True
            or prior.get('sourceCommit') != release.get('sourceCommit')
            or prior.get('artifactSha256') != release.get('artifactSha256')
            or runtime.get('testedCommit') != release.get('sourceCommit')
            or runtime.get('artifactSha256') != release.get('artifactSha256')
            or runtime.get('passed') is not True or runtime.get('artifactUnchanged') is not True
            or runtime.get('networkPolicy') != 'connections blocked'
            or any(runtime.get(key) for key in ('errors','failures','skipped'))
            or not set(TESTS).issubset(runtime.get('tests', []))
            or runtime.get('testsRun') != len(runtime.get('tests', []))
            or runtime.get('fixtureManifest', {}).get('sourceCommit') != release.get('sourceCommit')
            or not any(name.startswith('test_video.VideoTests.') for name in runtime.get('tests', []))
            or plex.get('passed') is not True or plex.get('changes') != []):
        raise ValueError('prior_upgrade_does_not_certify_current_release')
    return {'ready':True,'testedCommit':prior['sourceCommit'],'artifactSha256':prior['artifactSha256']}


def deploy(payload, fixtures, commit, ci_run, expected_hash, prior_upgrade=None, preserve_existing_video=False, include_removal_tests=False):
    if not re.fullmatch('[a-f0-9]{40}', commit) or not re.fullmatch('[a-f0-9]{64}', expected_hash):
        raise ValueError('full_release_identity_required')
    ci = requests.get(f'https://api.github.com/repos/StonyTark1117/ASMarr/actions/runs/{ci_run}', timeout=30)
    ci.raise_for_status(); ci = ci.json()
    if ci.get('head_sha') != commit or ci.get('status') != 'completed' or ci.get('conclusion') != 'success':
        raise ValueError('exact_release_ci_not_successful')
    release = json.loads((ACCEPTANCE / 'deployed-release.json').read_text())
    accepted = json.loads((ACCEPTANCE / 'production-accepted.json').read_text())
    if prior_upgrade:
        accepted = prior_certification(prior_upgrade, release)
    if preserve_existing_video and not prior_upgrade:
        raise ValueError('existing_video_requires_exact_prior_upgrade_certification')
    before = invariants()
    validate_baseline(before, release, accepted, artifact_hash(APPLICATION), preserve_existing_video)
    for action, unit, expected in (('is-enabled','asmr-scraper.timer','disabled'),
                                   ('is-active','asmr-scraper.timer','inactive'),
                                   ('is-active','asmr-scraper.service','inactive')):
        if subprocess.run(['systemctl',action,unit], capture_output=True, text=True).stdout.strip() != expected:
            raise ValueError('legacy_scraper_is_not_disabled_and_idle')
    proposed = {'sourceCommit': commit, 'artifactSha256': expected_hash}
    verify_fixture_files(fixtures, proposed)
    stage = Path(tempfile.mkdtemp(prefix='asmarr-production-release-', dir=APPLICATION.parent))
    extract_payload(payload, stage)
    for path in [stage, *stage.rglob('*')]:
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    if artifact_hash(stage) != expected_hash:
        raise ValueError('staged_release_hash_mismatch')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = BACKUPS / ('production-video-' + stamp)
    backup.mkdir(mode=0o700, parents=True)
    shutil.copytree(CONFIG, backup / 'config')
    shutil.copytree(ACCEPTANCE, backup / 'acceptance')
    shutil.copy2(RUNTIME, backup / RUNTIME.name)
    with sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(backup / 'state.db') as target:
        source.backup(target)
    (backup / 'state.db').chmod(0o600)
    for unit in ('asmarr.service','asmr-scraper.service','asmr-scraper.timer'):
        result = subprocess.run(['systemctl','cat',unit], check=True, capture_output=True)
        path = backup / unit; path.write_bytes(result.stdout); path.chmod(0o600)
    tasks = hold_tasks()
    stopped = moved = False
    held = None
    try:
        protected_json(backup / 'task-schedule.json', tasks)
        held = invariants()
        if held['activeCommands']:
            raise ValueError('active_command_appeared_after_scheduler_hold')
        system('stop'); stopped = True
        if invariants() != held:
            raise ValueError('instance_changed_while_stopping')
        baseline = ACCEPTANCE / ('plex-before-video-upgrade-' + stamp + '.json')
        plex_extra=('--include-video',) if preserve_existing_video else ()
        evidence('plex_invariants.py', 'capture', '--baseline', baseline, *plex_extra)
        APPLICATION.rename(backup / 'application'); moved = True
        stage.rename(APPLICATION)
        proposed.update(deployedAt=dt.datetime.now(dt.timezone.utc).isoformat(), mode='production',
                        ciRun=ci['html_url'], backup=str(backup), priorRelease=release['sourceCommit'],
                        acceptanceScope='upgrade pending; original audio acceptance is historical')
        if prior_upgrade:
            proposed['priorUpgradeEvidence'] = str(prior_upgrade)
        protected_json(ACCEPTANCE / 'deployed-release.json', proposed)
        protected_json(RUNTIME, {k: proposed[k] for k in ('sourceCommit','artifactSha256','deployedAt')}, 0o640)
        system('start'); healthy()
        if invariants() != held or artifact_hash(APPLICATION) != expected_hash:
            raise ValueError('replacement_changed_preserved_state')
        runtime = ACCEPTANCE / ('runtime-video-upgrade-' + stamp + '.json')
        extra=('--include-removal',) if include_removal_tests else ()
        evidence('runtime_fixtures.py', 'verify', '--fixtures', fixtures, '--include-video', *extra, '--output', runtime)
        if json.loads(runtime.read_text()).get('passed') is not True:
            raise ValueError('installed_runtime_fixtures_failed')
        preservation = ACCEPTANCE / ('plex-video-upgrade-' + stamp + '.json')
        evidence('plex_invariants.py', 'compare', '--baseline', baseline, '--output', preservation, *plex_extra)
        if json.loads(preservation.read_text()).get('passed') is not True or invariants() != held:
            raise ValueError('upgrade_preservation_failed')
        # Stop before restoring the saved schedule so overdue tasks cannot race
        # the final preservation check. Existing next-run times are preserved.
        system('stop')
        if invariants() != held:
            raise ValueError('instance_changed_before_scheduler_restore')
        restore_tasks(tasks)
        if invariants() != before:
            raise ValueError('saved_scheduler_or_state_not_restored')
        report = dict(proposed, ready=True, scope='production upgrade; live video acceptance still required',
                      runtimeEvidence=str(runtime), plexPreservation=str(preservation),
                      originalAudioAcceptancePreserved=True, schedulerRestored=True)
        report.update(existingVideoPreserved=preserve_existing_video,removalRuntimeTests=include_removal_tests,
                      preservedBefore=before,preservedAfter=invariants())
        system('start'); healthy()
        protected_json(ACCEPTANCE / ('production-video-upgrade-' + stamp + '.json'), report)
        return report
    except Exception:
        if stopped:
            system('stop')
        if moved:
            if APPLICATION.exists():
                APPLICATION.rename(backup / 'failed-application')
            (backup / 'application').rename(APPLICATION)
            shutil.copy2(backup / 'acceptance/deployed-release.json', ACCEPTANCE / 'deployed-release.json')
            shutil.copy2(backup / RUNTIME.name, RUNTIME)
        # Never roll back the DB or media over a possible concurrent user edit.
        # Restore scheduling only when all other preserved state is unchanged.
        current = invariants()
        comparable = lambda value: {k:v for k,v in value.items() if k != 'taskSettingsHash'}
        if comparable(current) == comparable(before):
            restore_tasks(tasks)
        else:
            with sqlite3.connect(DB) as db: db.execute('UPDATE tasks SET enabled=0')
        if stopped:
            system('start')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--ci-run', type=int, required=True)
    parser.add_argument('--artifact-sha256', required=True)
    parser.add_argument('--prior-upgrade-proof', type=Path)
    parser.add_argument('--preserve-existing-video', action='store_true')
    parser.add_argument('--include-removal-tests', action='store_true')
    args = parser.parse_args()
    result = deploy(args.payload, args.fixtures, args.commit, args.ci_run, args.artifact_sha256, args.prior_upgrade_proof, args.preserve_existing_video, args.include_removal_tests)
    print(json.dumps({k:result[k] for k in ('sourceCommit','artifactSha256','backup','scope','schedulerRestored')}))
