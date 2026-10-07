"""Audited CT130 transition. Default is a read-only preflight.

Run --apply only after preflight and the separate acceptance evidence pass.
Rollback never deletes or rewrites library media.
"""
import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import yaml

DB=Path('/var/lib/asmarr/asmarr.db')
ACCEPTANCE=Path('/var/lib/asmarr/acceptance')
PRODUCTION_OVERRIDE=Path('/etc/systemd/system/asmarr.service.d/production.conf')
APPLICATION=Path('/opt/asmarr')
PARITY_FIELDS=('discoveryParity','eligibilityAndSourceParity','checkpointCalculationParity',
               'productionCheckpointsUnchanged','applicationCheckpointsUnchanged','mediaUnchanged',
               'playlistPreviewHealthy','playlistCalculationParity')

def run_command(name, deadline):
    from smoke import command,wait
    result=wait(command(name),deadline)
    if not isinstance(result,dict) or result.get('status') in {'running','failed','degraded'}:
        # An observation timeout is not completion. Keep the durable command ID
        # available to the operator; never enqueue another run automatically.
        raise RuntimeError(f'{name} did not finish successfully: {result}')
    return result

def preflight(now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as db:
        daily={}
        for started,comparison in db.execute('SELECT started,comparison FROM shadow_cycles WHERE clean=1 ORDER BY started'):
            comparison=json.loads(comparison)
            if comparison.get('legacyImplementationSha256') and all(comparison.get(k) is True for k in PARITY_FIELDS):
                daily[started[:10]]=started
        stamps=[dt.datetime.fromisoformat(s.replace('Z','+00:00')) for s in daily.values()]
        three_days=len(stamps)>=3 and (stamps[-1]-stamps[-3]).total_seconds()>=46*3600 and 0<=(now-stamps[-1]).total_seconds()<30*3600
        audit=db.execute('SELECT result FROM migration_audits ORDER BY id DESC LIMIT 1').fetchone()
        evidence_path=ACCEPTANCE/'pre-cutover.json'
        evidence=json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
        tests_passed=all(evidence.get(k) is True for k in ['providerFixtures','sqliteIntegration','browserAcceptance','playlistRecovery'])
        manifest_path=ACCEPTANCE/'deployed-release.json'
        deployment=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        source=deployment.get('sourceCommit','')
        expected=deployment.get('artifactSha256','')
        release_matches=False
        if re.fullmatch(r'[a-f0-9]{40}',source) and re.fullmatch(r'[a-f0-9]{64}',expected):
            from release_integrity import artifact_hash
            release_matches=evidence.get('testedCommit')==source and evidence.get('artifactSha256')==expected and artifact_hash(APPLICATION)==expected
        mode=db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
        result={'mode':mode[0] if mode else None,'threeDailyShadowCycles':three_days,'qualifiedDays':list(daily),'migrationPassed':bool(audit and json.loads(audit[0]).get('passed')),'preCutoverTestsPassed':tests_passed,'testedDeploymentMatches':release_matches,'integrity':db.execute('PRAGMA integrity_check').fetchone()[0]}
        result['ready']=result['mode']=='shadow' and three_days and result['migrationPassed'] and tests_passed and release_matches and result['integrity']=='ok'
        return result

def apply():
    report=preflight()
    if not report['ready']:print(json.dumps(report));raise SystemExit('Pre-cutover gates are incomplete')
    if (ACCEPTANCE/'transition.json').exists():raise RuntimeError('Existing transition journal requires review before another cutover')
    subprocess.run(['systemctl','stop','asmr-scraper.timer'],check=True)
    subprocess.run(['systemctl','disable','asmr-scraper.timer'],check=True)
    # Permit the last old-scraper run to finish naturally, preserving its work.
    deadline=time.monotonic()+1900
    while subprocess.run(['systemctl','is-active','--quiet','asmr-scraper.service']).returncode==0:
        if time.monotonic()>deadline:raise RuntimeError('Old scraper has not finished; cutover remains pending')
        time.sleep(5)
    result=run_command('migration',300)
    if not result.get('passed'):raise RuntimeError('Final delta migration failed')
    scan=run_command('disk-scan',300)
    if scan.get('missing') or scan.get('outside') or not scan.get('unchanged'):raise RuntimeError('Final media reconciliation failed')
    from plex_invariants import runtime_snapshot,write_protected,compare
    plex_before=runtime_snapshot()
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    baseline_path=ACCEPTANCE/('plex-before-cutover-'+stamp+'.json')
    write_protected(baseline_path,plex_before)
    report['plexBaseline']=str(baseline_path)
    # Scheduler work is paused while permissions and execution mode change.
    subprocess.run(['systemctl','stop','asmarr'],check=True)
    with sqlite3.connect(DB) as db:
        ACCEPTANCE.mkdir(mode=0o700,parents=True,exist_ok=True)
        transition=ACCEPTANCE/'transition.json'
        if transition.exists():raise RuntimeError('Existing transition journal requires review before another cutover')
        saved={'taskEnabled':dict(db.execute('SELECT name,enabled FROM tasks')),
               'startedAt':dt.datetime.now(dt.timezone.utc).isoformat()}
        with transition.open('w') as output:
            transition.chmod(0o600);json.dump(saved,output)
        db.execute("UPDATE settings SET value='production' WHERE key='mode'")
        db.execute('UPDATE tasks SET enabled=0')
    subprocess.run(['setfacl','-R','-m','u:asmarr:rwX','/mnt/cephfs/media/asmr','/mnt/cephfs/media/asmr-video','/mnt/downloads/asmarr'],check=True)
    dirs=subprocess.run(['find','/mnt/cephfs/media/asmr','/mnt/cephfs/media/asmr-video','-type','d','-print0'],capture_output=True,check=True).stdout
    for folder in dirs.split(b'\0'):
        if folder:subprocess.run(['setfacl','-m','d:u:asmarr:rwx',os.fsdecode(folder)],check=True)
    PRODUCTION_OVERRIDE.parent.mkdir(exist_ok=True)
    PRODUCTION_OVERRIDE.write_text('[Service]\nReadOnlyPaths=\nReadWritePaths=/mnt/cephfs/media/asmr /mnt/cephfs/media/asmr-video /mnt/downloads/asmarr\n')
    config=Path('/etc/asmarr/sources.yaml');cfg=yaml.safe_load(config.read_text());original_limit=cfg.get('max_downloads_per_run',25)
    cfg['max_downloads_per_run']=1;config.write_text(yaml.safe_dump(cfg))
    try:
        subprocess.run(['systemctl','daemon-reload'],check=True);subprocess.run(['systemctl','start','asmarr'],check=True)
        time.sleep(5)
        discovery=run_command('discovery',1800)
        cycle=run_command('queue',1800)
        if cycle.get('failed'):raise RuntimeError('Bounded acquisition cycle contains failed transfers')
        plex=run_command('plex',300)
        playlists=run_command('playlists',600)
        indexing=run_command('plex-verify',300)
        if indexing.get('unindexed'):raise RuntimeError('Imported media is not fully indexed by Plex')
        playlist_verification=run_command('playlists-verify',600)
        if playlist_verification.get('status')!='ok' or playlist_verification.get('managedCount')!=15:
            raise RuntimeError('The 15 managed playlists failed verification')
        preservation=compare(plex_before,runtime_snapshot())
        write_protected(ACCEPTANCE/('plex-preservation-'+stamp+'.json'),preservation)
        if not preservation['passed']:raise RuntimeError('Plex preservation audit requires review before accepting cutover')
        report.update(finalMigration=result,finalScan=scan,discovery=discovery,boundedCycle=cycle,plex=plex,playlists=playlists,indexing=indexing,playlistVerification=playlist_verification,plexPreservation=preservation)
        target=Path('/var/lib/asmarr/acceptance/cutover.json');target.write_text(json.dumps(report));target.chmod(0o600)
    finally:
        cfg['max_downloads_per_run']=original_limit;config.write_text(yaml.safe_dump(cfg))
    # Keep scheduling disabled until direct-source, controlled torrent fallback,
    # outage/repeat and playlist acceptance are recorded. This is deliberately
    # a bounded transition, not a claim that production acceptance has passed.
    print(json.dumps({'status':'bounded_cycle_complete','evidence':'/var/lib/asmarr/acceptance/cutover.json','scheduler':'awaiting_production_acceptance'}))

def rollback():
    subprocess.run(['systemctl','stop','asmarr'],check=True)
    with sqlite3.connect(DB) as db:
        db.execute("UPDATE settings SET value='shadow' WHERE key='mode'")
        transition=ACCEPTANCE/'transition.json'
        if transition.exists():
            saved=json.loads(transition.read_text())
            for name,enabled in saved['taskEnabled'].items():db.execute('UPDATE tasks SET enabled=? WHERE name=?',(enabled,name))
    if PRODUCTION_OVERRIDE.exists():
        # Preserve the override as a recoverable audit artifact, but restore the
        # unit's read-only media sandbox for a later shadow-mode restart.
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        ACCEPTANCE.mkdir(mode=0o700,parents=True,exist_ok=True)
        PRODUCTION_OVERRIDE.rename(ACCEPTANCE/('production-override-rolled-back-'+stamp+'.conf'))
    if transition.exists():
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        transition.rename(ACCEPTANCE/('transition-rolled-back-'+stamp+'.json'))
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','asmr-scraper.timer'],check=True)
    print(json.dumps({'status':'rolled_back','media':'preserved','oldTimer':'enabled'}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();g=parser.add_mutually_exclusive_group();g.add_argument('--apply',action='store_true');g.add_argument('--rollback',action='store_true');args=parser.parse_args()
    if args.rollback:rollback()
    elif args.apply:apply()
    else:print(json.dumps(preflight()))
