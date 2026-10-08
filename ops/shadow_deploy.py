"""Backed-up shadow-only release replacement. Never changes source settings/media.

Validate the exact successful GitHub run and payload before stopping ASMarr.
Retain both releases and online DB/config/unit backups for recovery.
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
import tarfile
import tempfile
import time
import requests
import urllib3

from release_integrity import artifact_hash

DB=Path('/var/lib/asmarr/asmarr.db')
APPLICATION=Path('/opt/asmarr')
CONFIG=Path('/etc/asmarr')
ACCEPTANCE=Path('/var/lib/asmarr/acceptance')
BACKUPS=Path('/root/asmarr-rollout-backups')
RUNTIME_RELEASE=DB.parent/'deployed-release-runtime.json'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def invariants():
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('SELECT * FROM assets ORDER BY key').fetchall()
        paths=db.execute("SELECT saved_path FROM assets WHERE state='complete' ORDER BY saved_path").fetchall()
        media=[]
        for (saved,) in paths:
            path=Path(saved or '')
            if not path.is_file():raise ValueError('existing_media_missing')
            stat=path.stat();media.append((saved,stat.st_size,stat.st_mtime_ns))
        return {'mode':db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0],
                'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],
                'assets':len(rows),'assetRowHash':digest(rows),'savedPaths':len(paths),'mediaStatHash':digest(media),
                'audioRowsHash':digest(db.execute("SELECT * FROM media_assets WHERE media_kind='Audio' ORDER BY recording_key").fetchall()),
                'checkpointHash':digest(db.execute('SELECT * FROM meta ORDER BY key').fetchall()),
                'sourceSettingsHash':digest({name:hashlib.sha256((CONFIG/name).read_bytes()).hexdigest() for name in ('sources.yaml','source-secrets.json','integrations.json','legacy-sources.yaml') if (CONFIG/name).is_file()}),
                'taskSettingsHash':digest(db.execute('SELECT name,enabled,interval_seconds FROM tasks ORDER BY name').fetchall()),
                'creators':db.execute('SELECT count(*) FROM creators').fetchone()[0],
                'videoOptIns':db.execute('SELECT count(*) FROM creators WHERE monitor_video!=0').fetchone()[0],
                'videoAssets':db.execute("SELECT count(*) FROM media_assets WHERE media_kind='Video'").fetchone()[0],
                'videoCandidates':db.execute('SELECT count(*) FROM video_candidates').fetchone()[0],
                'videoBackfills':db.execute('SELECT count(*) FROM backfill_jobs').fetchone()[0],
                'queueRows':db.execute('SELECT count(*) FROM queue').fetchone()[0],
                'activeCommands':db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running')").fetchone()[0]}


def validate_idle(snapshot):
    if snapshot['mode']!='shadow' or snapshot['integrity']!='ok' or snapshot['activeCommands']:
        raise ValueError('shadow_deploy_requires_healthy_idle_shadow_instance')
    if any(snapshot[key] for key in ('videoOptIns','videoAssets','videoCandidates','videoBackfills','queueRows')):
        raise ValueError('shadow_baseline_has_video_or_download_work_requiring_review')


def extract_payload(archive,stage):
    stage=Path(stage)
    with tarfile.open(archive) as tar:
        for entry in tar.getmembers():
            path=stage/entry.name
            if Path(entry.name).is_absolute() or not path.resolve().is_relative_to(stage.resolve()) or not (entry.isfile() or entry.isdir()) or entry.mode&0o7000:
                raise ValueError('unsafe_application_archive_entry')
        # Every entry has been validated; no links, devices or special modes.
        tar.extractall(stage)
    if not (stage/'ASMarr').is_file() or not (stage/'ASMarr.dll').is_file() or not (stage/'wwwroot/index.html').is_file():
        raise ValueError('application_payload_incomplete')
    stage.chmod(0o755)


def deploy(payload,commit,ci_run,expected_hash,ops_commit):
    if not re.fullmatch(r'[a-f0-9]{40}',commit) or not re.fullmatch(r'[a-f0-9]{40}',ops_commit) or not re.fullmatch(r'[a-f0-9]{64}',expected_hash):
        raise ValueError('full_release_identity_required')
    ci=requests.get(f'https://api.github.com/repos/StonyTark1117/ASMarr/actions/runs/{ci_run}',timeout=30)
    ci.raise_for_status();ci=ci.json()
    if ci.get('head_sha')!=commit or ci.get('status')!='completed' or ci.get('conclusion')!='success':
        raise ValueError('exact_release_ci_not_successful')
    before=invariants();validate_idle(before)
    sandbox=subprocess.run(['systemctl','show','asmarr.service','-p','ReadOnlyPaths','--value'],check=True,capture_output=True,text=True).stdout.split()
    if not {'/mnt/cephfs/media/asmr','/mnt/cephfs/media/asmr-video','/mnt/downloads/asmarr'}.issubset(sandbox):
        raise ValueError('shadow_media_sandbox_not_present')
    stage=Path(tempfile.mkdtemp(prefix='asmarr-release-',dir=APPLICATION.parent))
    extract_payload(payload,stage)
    for path in [stage,*stage.rglob('*')]:
        os.chown(path,0,0)
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode&0o111 else 0o644)
    if artifact_hash(stage)!=expected_hash:raise ValueError('staged_release_hash_mismatch')
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=BACKUPS/('shadow-'+stamp);backup.mkdir(mode=0o700,parents=True)
    shutil.copytree(CONFIG,backup/'config')
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(backup/'state.db') as target:
        source.backup(target)
    (backup/'state.db').chmod(0o600)
    for unit in ('asmarr.service','asmr-scraper.service','asmr-scraper.timer'):
        result=subprocess.run(['systemctl','cat',unit],check=True,capture_output=True)
        path=backup/unit;path.write_bytes(result.stdout);path.chmod(0o600)
    for name in ('deployed-release.json','pre-cutover.json'):
        if (ACCEPTANCE/name).exists():shutil.copy2(ACCEPTANCE/name,backup/name)
    if RUNTIME_RELEASE.exists():shutil.copy2(RUNTIME_RELEASE,backup/RUNTIME_RELEASE.name)
    # Check again after staging/backup; an active command must finish naturally.
    validate_idle(invariants())
    subprocess.run(['systemctl','stop','asmarr'],check=True)
    previous=backup/'application'
    old_moved=False;runtime_replaced=False
    try:
        APPLICATION.rename(previous);old_moved=True
        stage.rename(APPLICATION)
        deployed_at=dt.datetime.now(dt.timezone.utc).isoformat()
        runtime={'sourceCommit':commit,'artifactSha256':expected_hash,'deployedAt':deployed_at}
        runtime_temporary=RUNTIME_RELEASE.with_name('deployed-release-runtime-'+stamp+'.tmp')
        with runtime_temporary.open('x') as output:
            runtime_temporary.chmod(0o640);json.dump(runtime,output,indent=2)
        owner=DB.stat();os.chown(runtime_temporary,owner.st_uid,owner.st_gid)
        runtime_temporary.replace(RUNTIME_RELEASE);runtime_replaced=True
        subprocess.run(['systemctl','start','asmarr'],check=True)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        healthy=False
        for _ in range(60):
            try:
                response=requests.get('https://127.0.0.1:8787/healthz',verify=False,timeout=2)
                healthy=response.status_code==200 and response.json().get('application')=='ASMarr'
            except requests.RequestException:pass
            if healthy:break
            time.sleep(1)
        if not healthy:raise RuntimeError('new_release_did_not_become_healthy')
        after=invariants()
        for _ in range(60):
            if not after['activeCommands']:break
            time.sleep(1);after=invariants()
        if after!=before or artifact_hash(APPLICATION)!=expected_hash:
            raise RuntimeError('shadow_release_invariants_changed')
        auth=json.loads((CONFIG/'auth.json').read_text())
        response=requests.get('https://127.0.0.1:8787/api/v1/creators',headers={'X-Api-Key':auth['apiKey']},verify=False,timeout=30)
        response.raise_for_status()
        if sum(row['audio_completed'] for row in response.json())!=before['savedPaths']:
            raise RuntimeError('creator_audio_totals_do_not_match_imported_library')
        manifest={'sourceCommit':commit,'opsCommit':ops_commit,'artifactSha256':expected_hash,
                  'deployedAt':deployed_at,'mode':'shadow',
                  'ciRun':f'https://github.com/StonyTark1117/ASMarr/actions/runs/{ci_run}',
                  'before':before,'after':after,'backup':str(backup),'liveSmokePassed':True}
        ACCEPTANCE.mkdir(mode=0o700,parents=True,exist_ok=True)
        temporary=ACCEPTANCE/('deployed-release-'+stamp+'.tmp')
        with temporary.open('x') as output:
            temporary.chmod(0o600);json.dump(manifest,output,indent=2)
        temporary.replace(ACCEPTANCE/'deployed-release.json')
        return manifest
    except Exception:
        # Keep the failed payload for inspection; never rewrite or restore media.
        subprocess.run(['systemctl','stop','asmarr'],check=True)
        if old_moved:
            if APPLICATION.exists():APPLICATION.rename(backup/'failed-application')
            previous.rename(APPLICATION)
        if runtime_replaced:
            saved_runtime=backup/RUNTIME_RELEASE.name
            if saved_runtime.exists():shutil.copy2(saved_runtime,RUNTIME_RELEASE)
            else:RUNTIME_RELEASE.unlink(missing_ok=True)
        subprocess.run(['systemctl','start','asmarr'],check=True)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--payload',type=Path,required=True);parser.add_argument('--commit',required=True)
    parser.add_argument('--ci-run',type=int,required=True);parser.add_argument('--artifact-sha256',required=True)
    parser.add_argument('--ops-commit',required=True)
    args=parser.parse_args()
    result=deploy(args.payload,args.commit,args.ci_run,args.artifact_sha256,args.ops_commit)
    print(json.dumps({'sourceCommit':result['sourceCommit'],'artifactSha256':result['artifactSha256'],'backup':result['backup'],'invariantsPreserved':result['before']==result['after']}))
