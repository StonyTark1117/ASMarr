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
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

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
MAINTENANCE_DROPIN = Path('/run/systemd/system/asmarr.service.d/90-asmarr-upgrade-maintenance.conf')
EMERGENCY_DROPINS = Path('/etc/systemd/system/asmarr.service.d')
MAINTENANCE_CONTENT = '[Service]\nEnvironment=ASMARR_MAINTENANCE_HOLD=1\n'
_video_digest_cache = None


def clear_video_digest_cache():
    if _video_digest_cache is not None:
        _video_digest_cache.clear()


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
            identity=(before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_ino)
            cached=_video_digest_cache.get(str(path)) if _video_digest_cache is not None else None
            if cached is not None and cached[0]==identity:
                sha=cached[1]
            else:
                with path.open('rb') as stream:sha = hashlib.file_digest(stream,'sha256').hexdigest()
            after = path.stat()
            if identity!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_ino):
                raise ValueError('preserved_video_changed_during_audit')
            if _video_digest_cache is not None:
                _video_digest_cache[str(path)]=(identity,sha)
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
        commands_exist = db.execute("SELECT 1 FROM sqlite_master WHERE name='commands'").fetchone()
        queued = sorted(db.execute("SELECT * FROM commands WHERE state='queued'").fetchall(), key=repr) if commands_exist else []
        running = db.execute("SELECT count(*) FROM commands WHERE state='running'").fetchone()[0] if commands_exist else 0
        tasks_exist = db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone()
        schedule = db.execute('SELECT name,enabled,next_run FROM tasks ORDER BY name').fetchall() if tasks_exist else []
        running_backfills = db.execute("SELECT count(*) FROM backfill_jobs WHERE state='running'").fetchone()[0]
        media_transfers = db.execute("SELECT count(*) FROM media_assets WHERE state IN ('downloading','importing')").fetchone()[0]
        return {'videoPreservationHash':digest(rows),'videoFilesHash':digest(sorted(media,key=repr)),
                'videoInventoryHash':digest(sorted(inventory,key=repr)),'protectedConfigHash':digest(configuration),
                'resumableBackfills':resumable,'activeDownloadJobs':transfers,
                'queuedCommandsHash':digest(queued),'queuedCommandsCount':len(queued),
                'runningCommands':running,'runningBackfills':running_backfills,
                'activeMediaTransfers':media_transfers,'taskScheduleHash':digest(schedule)}


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


def validate_baseline(snapshot, release, accepted, installed_hash, preserve_existing_video=False, preserve_queued_work=False):
    if preserve_queued_work and not preserve_existing_video:
        raise ValueError('queued_work_requires_existing_video_preservation')
    busy = snapshot['activeCommands']
    if preserve_queued_work:
        required=('queuedCommandsHash','taskScheduleHash')
        if any(not re.fullmatch('[a-f0-9]{64}',snapshot.get(key,'')) for key in required):
            raise ValueError('queued_work_requires_complete_fingerprints')
        if (type(snapshot.get('queuedCommandsCount')) is not int or snapshot['queuedCommandsCount'] < 0
                or snapshot.get('runningCommands') != 0 or snapshot.get('runningBackfills') != 0
                or snapshot.get('activeMediaTransfers') != 0):
            raise ValueError('queued_upgrade_requires_no_running_work')
        busy = snapshot['activeCommands'] != snapshot['queuedCommandsCount']
    if snapshot['mode'] != 'production' or snapshot['integrity'] != 'ok' or busy:
        raise ValueError('upgrade_requires_healthy_idle_production')
    if preserve_existing_video:
        required=('videoPreservationHash','videoFilesHash','videoInventoryHash','protectedConfigHash')
        if any(not re.fullmatch('[a-f0-9]{64}',snapshot.get(key,'')) for key in required):
            raise ValueError('full_video_preservation_boundary_required')
        if (not preserve_queued_work and snapshot.get('resumableBackfills') != 0) or snapshot.get('activeDownloadJobs') != 0:
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


def require_service_state(expected):
    result = subprocess.run(['systemctl','show','asmarr.service','--property=LoadState,ActiveState'],
                            capture_output=True,text=True,timeout=30)
    fields = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode != 0 or fields.get('LoadState') != 'loaded' or fields.get('ActiveState') != expected:
        raise ValueError('service_state_unknown_or_not_' + expected)


def acquire_provider_hold():
    """The worker uses this same transactional lease before starting a command."""
    owner = 'production-upgrade-' + uuid.uuid4().hex
    now = dt.datetime.now(dt.timezone.utc)
    with sqlite3.connect(DB,timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        checks = (("commands", "state='running'"), ("backfill_jobs", "state='running'"),
                  ("queue", "state IN ('submitting','downloading','importing')"),
                  ("media_assets", "state IN ('downloading','importing')"))
        for table, predicate in checks:
            if db.execute('SELECT count(*) FROM '+table+' WHERE '+predicate).fetchone()[0]:
                raise ValueError('running_work_must_drain_before_upgrade')
        previous = db.execute("SELECT expires FROM task_locks WHERE name='providers'").fetchone()
        if previous:
            try: expired = dt.datetime.fromisoformat(previous[0]) < now
            except (ValueError,TypeError): expired = False
            if not expired: raise ValueError('provider_lease_is_busy_or_unknown')
        db.execute("INSERT INTO task_locks(name,owner,expires) VALUES('providers',?,?) "
                   "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires",
                   (owner,(now+dt.timedelta(minutes=10)).isoformat()))
    return owner


def release_provider_hold(owner):
    with sqlite3.connect(DB,timeout=30) as db:
        if db.execute("DELETE FROM task_locks WHERE name='providers' AND owner=?",(owner,)).rowcount != 1:
            raise ValueError('upgrade_provider_lease_was_lost')


def download_client_idle():
    """Read-only scope: ASMarr torrents only. Seeding completed files is allowed."""
    options = json.loads((CONFIG/'integrations.json').read_text()).get('qbittorrent',{})
    if not options.get('url'):
        raise ValueError('cannot_verify_download_client_without_configuration')
    base = options['url'].rstrip('/')
    with requests.Session() as session:
        login = session.post(base+'/api/v2/auth/login',data={
            'username':options.get('username',''),'password':options.get('password','')},timeout=(10,30))
        if login.status_code != 200 or login.text.strip() != 'Ok.':
            raise ValueError('cannot_verify_download_client_authentication')
        response = session.get(base+'/api/v2/torrents/info',params={'category':'asmarr'},timeout=(10,30))
        response.raise_for_status(); torrents=response.json()
    if not isinstance(torrents,list):raise ValueError('download_client_inventory_invalid')
    stable = {'uploading','stalledUP','queuedUP','pausedUP','stoppedUP','forcedUP'}
    for item in torrents:
        if (item.get('category') != 'asmarr' or item.get('state') not in stable
                or not isinstance(item.get('progress'),(int,float)) or item['progress'] != 1):
            raise ValueError('asmarr_download_client_work_must_drain')


def write_owned_dropin(path, content):
    if os.geteuid() != 0:raise ValueError('maintenance_upgrade_requires_root')
    path = Path(path)
    for parent in path.parents:
        if parent.exists() and (parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022):
            raise ValueError('maintenance_dropin_parent_not_root_protected')
    path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
    with path.open('x') as output:
        path.chmod(0o644); output.write(content); output.flush();os.fsync(output.fileno())
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
    subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)


def remove_owned_dropin(path, content):
    path=Path(path)
    if (path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022
            or path.read_text()!=content):
        raise ValueError('maintenance_dropin_changed_requires_operator_review')
    path.unlink()
    subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)


def require_maintenance_status():
    auth=json.loads((CONFIG/'auth.json').read_text())
    response=requests.get('https://127.0.0.1:8787/api/v1/system/status',
                          headers={'X-Api-Key':auth['apiKey']},verify=False,timeout=10)
    response.raise_for_status()
    if response.json().get('maintenanceHeld') is not True:
        raise ValueError('replacement_is_not_maintenance_held')


def require_hold_configuration():
    result=subprocess.run(['systemctl','show','asmarr.service',
                           '--property=Environment,EnvironmentFiles,UnsetEnvironment'],
                          check=True,capture_output=True,text=True,timeout=30)
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    settings=[item for item in shlex.split(fields.get('Environment',''))
              if item.startswith('ASMARR_MAINTENANCE_HOLD=')]
    unset=shlex.split(fields.get('UnsetEnvironment',''))
    # EnvironmentFile and UnsetEnvironment can override an Environment assignment.
    # Refuse these uncertain configurations instead of starting a live worker.
    if (settings!=['ASMARR_MAINTENANCE_HOLD=1'] or fields.get('EnvironmentFiles')
            or any(item.split('=',1)[0]=='ASMARR_MAINTENANCE_HOLD' for item in unset)):
        raise ValueError('effective_maintenance_environment_not_guaranteed')


def retain_emergency_lease(owner):
    # A failure to prove the service stopped must not let our lease time out
    # and silently resume an old, maintenance-unaware worker.
    if owner is None: owner=acquire_provider_hold()
    with sqlite3.connect(DB,timeout=30) as db:
        if db.execute("UPDATE task_locks SET expires='9999-12-31T23:59:59+00:00' "
                      "WHERE name='providers' AND owner=?",(owner,)).rowcount!=1:
            raise ValueError('emergency_provider_lease_not_owned')
    return owner


def emergency_guard(guard, marker, problems):
    try:
        if marker.exists() or marker.is_symlink():raise ValueError('review_marker_already_exists')
        write_owned_dropin(guard,'[Unit]\nConditionPathExists='+str(marker)+'\n')
    except Exception as error:
        problems.append('startup-guard:'+type(error).__name__)


def record_upgrade_failure(backup, report):
    try:
        backup.mkdir(mode=0o700,parents=True,exist_ok=True)
        protected_json(backup/'upgrade-failed.json',report)
    except Exception as error:
        report['failureEvidenceError']=type(error).__name__
    # Always make recovery targets visible, even on a full or read-only disk.
    print(json.dumps(report),file=sys.stderr)


def evidence(command, *arguments):
    subprocess.run([sys.executable, str(OPS / command), *map(str, arguments)], check=True)


def require_creator_audio_evidence(application, proof):
    if not (Path(application)/'providers/creator_audio_sources.py').is_file():
        return
    tests=proof.get('tests', [])
    if (proof.get('creatorAudioFixtures') is not True
            or not any(name.startswith('test_creator_audio_sources.CreatorAudioSourceTests.') for name in tests)
            or not any(name.startswith('test_providers.ProviderTests.') for name in tests)):
        raise ValueError('installed_creator_audio_fixtures_not_certified')


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


def deploy_preserving_queued(stage, fixtures, proposed, ci, release, accepted, prior_upgrade,
                             stamp, include_removal_tests):
    """Explicit maintenance path; every failed attempt remains stopped for review.

    The candidate must pass the synthetic boot probe BEFORE acquiring the live
    lease. Queued commands and task rows are never rewritten or discarded.
    """
    if os.geteuid()!=0:raise ValueError('maintenance_upgrade_requires_root')
    if MAINTENANCE_DROPIN.exists() or MAINTENANCE_DROPIN.is_symlink():
        raise ValueError('maintenance_dropin_already_exists')
    probe = ACCEPTANCE / ('maintenance-probe-' + stamp + '.json')
    evidence('maintenance_probe.py','--application',stage,'--expected-hash',proposed['artifactSha256'],
             '--commit',proposed['sourceCommit'],'--output',probe)
    proof=json.loads(probe.read_text())
    if (any(proof.get(k) is not True for k in ('passed','providerExecutionDisabled','queuedWorkUnchanged','syntheticConfigurationOnly'))
            or proof.get('productionDatabaseOpened') is not False
            or proof.get('sourceCommit')!=proposed['sourceCommit']
            or proof.get('artifactSha256')!=proposed['artifactSha256']
            or artifact_hash(stage)!=proposed['artifactSha256']):
        raise ValueError('candidate_maintenance_probe_not_certified')
    require_service_state('active')
    download_client_idle()
    owner=acquire_provider_hold()
    backup=BACKUPS/('production-queued-'+stamp)
    guard=EMERGENCY_DROPINS/('99-asmarr-upgrade-review-'+stamp+'.conf')
    marker=backup/'operator-reviewed'
    moved=False; stopped=False;resume_attempted=False
    try:
        # Once the lease is held no provider command can start during stop.
        system('stop');require_service_state('inactive');stopped=True
        release_provider_hold(owner);owner=None
        download_client_idle()
        before=invariants()
        validate_baseline(before,release,accepted,artifact_hash(APPLICATION),True,True)
        backup.mkdir(mode=0o700,parents=True)
        shutil.copytree(CONFIG,backup/'config')
        shutil.copytree(ACCEPTANCE,backup/'acceptance')
        shutil.copy2(RUNTIME,backup/RUNTIME.name)
        with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(backup/'state.db') as target:
            source.backup(target)
        (backup/'state.db').chmod(0o600)
        units={}
        for unit in ('asmarr.service','asmr-scraper.service','asmr-scraper.timer'):
            units[unit]=subprocess.run(['systemctl','cat',unit],check=True,capture_output=True).stdout
            path=backup/unit;path.write_bytes(units[unit]);path.chmod(0o600)
        with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as db:
            tasks=db.execute('SELECT name,enabled,next_run FROM tasks ORDER BY name').fetchall()
        protected_json(backup/'task-schedule.json',tasks)
        protected_json(backup/'preserved-before.json',before)
        if invariants()!=before:raise ValueError('stopped_baseline_changed_during_backup')
        baseline=ACCEPTANCE/('plex-before-queued-upgrade-'+stamp+'.json')
        evidence('plex_invariants.py','capture','--baseline',baseline,'--include-video')
        write_owned_dropin(MAINTENANCE_DROPIN,MAINTENANCE_CONTENT)
        require_hold_configuration()
        APPLICATION.rename(backup/'application');moved=True
        stage.rename(APPLICATION)
        proposed.update(deployedAt=dt.datetime.now(dt.timezone.utc).isoformat(),mode='production',
                        ciRun=ci['html_url'],backup=str(backup),priorRelease=release['sourceCommit'],
                        priorUpgradeEvidence=str(prior_upgrade),maintenanceProbe=str(probe),
                        acceptanceScope='upgrade pending; original audio acceptance is historical')
        protected_json(ACCEPTANCE/'deployed-release.json',proposed)
        protected_json(RUNTIME,{k:proposed[k] for k in ('sourceCommit','artifactSha256','deployedAt')},0o640)
        system('start');healthy();require_service_state('active');require_maintenance_status()
        if invariants()!=before or artifact_hash(APPLICATION)!=proposed['artifactSha256']:
            raise ValueError('held_replacement_changed_preserved_state')
        runtime=ACCEPTANCE/('runtime-queued-upgrade-'+stamp+'.json')
        extra=('--include-removal',) if include_removal_tests else ()
        evidence('runtime_fixtures.py','verify','--fixtures',fixtures,'--include-video',*extra,'--output',runtime)
        runtime_proof=json.loads(runtime.read_text())
        require_creator_audio_evidence(APPLICATION,runtime_proof)
        if runtime_proof.get('passed') is not True:
            raise ValueError('installed_runtime_fixtures_failed')
        preservation=ACCEPTANCE/('plex-queued-upgrade-'+stamp+'.json')
        evidence('plex_invariants.py','compare','--baseline',baseline,'--output',preservation,'--include-video')
        if json.loads(preservation.read_text()).get('passed') is not True:
            raise ValueError('plex_preservation_failed')
        require_maintenance_status();download_client_idle()
        system('stop');require_service_state('inactive')
        if invariants()!=before:raise ValueError('queued_work_or_checkpoint_changed_during_upgrade')
        # Idempotent and guarded: no changed task schedule is silently overwritten.
        restore_tasks(tasks)
        if invariants()!=before:raise ValueError('exact_schedule_not_preserved')
        remove_owned_dropin(MAINTENANCE_DROPIN,MAINTENANCE_CONTENT)
        for unit,original in units.items():
            current=subprocess.run(['systemctl','cat',unit],check=True,capture_output=True).stdout
            if current!=original:raise ValueError('service_configuration_changed')
        # Re-read every byte at the final commit point. Earlier intermediate
        # checks reuse digests only while size, mtime, ctime and inode match.
        clear_video_digest_cache()
        if invariants()!=before or artifact_hash(APPLICATION)!=proposed['artifactSha256']:
            raise ValueError('final_preservation_failed')
        report=dict(proposed,ready=False,heldValidationPassed=True,resumeVerified=False,
                    activationStatus='held-verified-ready-to-resume',
                    scope='production upgrade preserving queued work; live video acceptance still required',
                    schedulerRestored=True,existingVideoPreserved=True,queuedWorkPreserved=True,
                    removalRuntimeTests=include_removal_tests,originalAudioAcceptancePreserved=True,
                    runtimeEvidence=str(runtime),plexPreservation=str(preservation),
                    preservedBefore=before,preservedAfter=invariants())
        report_path=ACCEPTANCE/('production-video-upgrade-'+stamp+'.json')
        protected_json(report_path,report)
        # Commit point: after this attempt legitimate queued work may execute.
        # A failed health request must not interrupt it or roll back its binary.
        resume_attempted=True
        system('start');healthy()
        report.update(ready=True,resumeVerified=True,activationStatus='resumed',
                      resumedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        protected_json(report_path,report)
        return report
    except Exception as failure:
        # A persistent systemd condition also protects old hold-unaware binaries
        # after reboot. Never create this review marker or remove unrelated units.
        problems=[]
        emergency_guard(guard,marker,problems)
        if resume_attempted:
            # The persistent guard prevents a later restart, not current work.
            record_upgrade_failure(backup,dict(errorClass=type(failure).__name__,ready=False,
                heldValidationPassed=True,resumeVerified=False,resumeUnverified=True,
                activationStatus='resume-unverified',requiresOperatorReview=True,
                runningWorkNotInterrupted=True,installedNewApplicationRetained=True,
                startupGuard=str(guard),absentReviewMarker=str(marker),recoveryErrors=problems))
            raise
        try:system('stop')
        except Exception as error:problems.append('service-stop:'+type(error).__name__)
        try:require_service_state('inactive');stopped=True
        except Exception as error:stopped=False;problems.append('service-state:'+type(error).__name__)
        # Independent attempts: a failed daemon-reload cannot skip stopping,
        # lease retention, old-application restoration or recovery evidence.
        lease_retained=False
        try:owner=retain_emergency_lease(owner);lease_retained=True
        except Exception as error:problems.append('provider-lease:'+type(error).__name__)
        if moved and stopped:
            try:
                if APPLICATION.exists():APPLICATION.rename(backup/'failed-application')
                (backup/'application').rename(APPLICATION)
                shutil.copy2(backup/'acceptance/deployed-release.json',ACCEPTANCE/'deployed-release.json')
                shutil.copy2(backup/RUNTIME.name,RUNTIME)
            except Exception as error:problems.append('application-rollback:'+type(error).__name__)
        # No DB restore, schedule rewrite, media operation, or automatic restart.
        record_upgrade_failure(backup,dict(errorClass=type(failure).__name__,
                       serviceStopped=stopped,requiresOperatorReview=True,providerLeaseOwner=owner,
                       providerLeaseRetained=lease_retained,
                       startupGuard=str(guard),absentReviewMarker=str(marker),recoveryErrors=problems,
                       temporaryMaintenanceDropin=str(MAINTENANCE_DROPIN)))
        raise


def _deploy(payload, fixtures, commit, ci_run, expected_hash, prior_upgrade=None, preserve_existing_video=False, include_removal_tests=False, preserve_queued_work=False):
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
    if preserve_queued_work and (not preserve_existing_video or not prior_upgrade):
        raise ValueError('queued_work_requires_existing_video_and_prior_proof')
    before = invariants()
    validate_baseline(before, release, accepted, artifact_hash(APPLICATION), preserve_existing_video, preserve_queued_work)
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
    if preserve_queued_work:
        return deploy_preserving_queued(stage,fixtures,proposed,ci,release,accepted,prior_upgrade,stamp,include_removal_tests)
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
        runtime_proof=json.loads(runtime.read_text())
        require_creator_audio_evidence(APPLICATION,runtime_proof)
        if runtime_proof.get('passed') is not True:
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
        clear_video_digest_cache()
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
        comparable = lambda value: {k:v for k,v in value.items() if k not in ('taskSettingsHash','taskScheduleHash')}
        if comparable(current) == comparable(before):
            restore_tasks(tasks)
        else:
            with sqlite3.connect(DB) as db: db.execute('UPDATE tasks SET enabled=0')
        if stopped:
            system('start')
        raise


def deploy(payload, fixtures, commit, ci_run, expected_hash, prior_upgrade=None, preserve_existing_video=False, include_removal_tests=False, preserve_queued_work=False):
    global _video_digest_cache
    if _video_digest_cache is not None:
        raise RuntimeError('production_upgrade_already_running')
    _video_digest_cache={}
    try:
        return _deploy(payload, fixtures, commit, ci_run, expected_hash,
                       prior_upgrade, preserve_existing_video,
                       include_removal_tests, preserve_queued_work)
    finally:
        _video_digest_cache=None


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
    parser.add_argument('--preserve-queued-work', action='store_true',
                        help='Require prior video proof; probe and hold replacement while preserving queued commands/checkpoints. Failures remain stopped for operator review.')
    args = parser.parse_args()
    result = deploy(args.payload, args.fixtures, args.commit, args.ci_run, args.artifact_sha256, args.prior_upgrade_proof, args.preserve_existing_video, args.include_removal_tests, args.preserve_queued_work)
    print(json.dumps({k:result[k] for k in ('sourceCommit','artifactSha256','backup','scope','schedulerRestored')}))
