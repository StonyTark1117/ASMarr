"""Audited CT130 transition. Default is a read-only preflight.

Run --apply only after preflight and the separate acceptance evidence pass.
Rollback never deletes or rewrites library media.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import yaml
from smoke import command,wait

DB=Path('/var/lib/asmarr/asmarr.db')
def preflight():
    with sqlite3.connect(DB) as db:
        daily={}
        for started,comparison in db.execute('SELECT started,comparison FROM shadow_cycles WHERE clean=1 ORDER BY started'):
            comparison=json.loads(comparison)
            if comparison.get('legacyImplementationSha256'):daily.setdefault(started[:10],started)
        stamps=[dt.datetime.fromisoformat(s.replace('Z','+00:00')) for s in daily.values()]
        three_days=len(stamps)>=3 and (stamps[-1]-stamps[-3]).total_seconds()>=46*3600 and (dt.datetime.now(dt.timezone.utc)-stamps[-1]).total_seconds()<30*3600
        audit=db.execute('SELECT result FROM migration_audits ORDER BY id DESC LIMIT 1').fetchone()
        evidence_path=Path('/var/lib/asmarr/acceptance/pre-cutover.json')
        evidence=json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
        tests_passed=all(evidence.get(k) is True for k in ['providerFixtures','sqliteIntegration','browserAcceptance','playlistRecovery'])
        result={'threeDailyShadowCycles':three_days,'qualifiedDays':list(daily),'migrationPassed':bool(audit and json.loads(audit[0]).get('passed')),'preCutoverTestsPassed':tests_passed,'integrity':db.execute('PRAGMA integrity_check').fetchone()[0]}
        result['ready']=three_days and result['migrationPassed'] and tests_passed and result['integrity']=='ok'
        return result

def apply():
    report=preflight()
    if not report['ready']:print(json.dumps(report));raise SystemExit('Pre-cutover gates are incomplete')
    subprocess.run(['systemctl','stop','asmr-scraper.timer'],check=True)
    subprocess.run(['systemctl','disable','asmr-scraper.timer'],check=True)
    # Permit the last old-scraper run to finish naturally, preserving its work.
    deadline=time.monotonic()+1900
    while subprocess.run(['systemctl','is-active','--quiet','asmr-scraper.service']).returncode==0:
        if time.monotonic()>deadline:raise RuntimeError('Old scraper has not finished; cutover remains pending')
        time.sleep(5)
    result=wait(command('migration'),300)
    if not result.get('passed'):raise RuntimeError('Final delta migration failed')
    scan=wait(command('disk-scan'),300)
    if scan.get('missing') or scan.get('outside') or not scan.get('unchanged'):raise RuntimeError('Final media reconciliation failed')
    # Scheduler work is paused while permissions and execution mode change.
    subprocess.run(['systemctl','stop','asmarr'],check=True)
    with sqlite3.connect(DB) as db:
        db.execute("UPDATE settings SET value='production' WHERE key='mode'")
        db.execute('UPDATE tasks SET enabled=0')
    subprocess.run(['setfacl','-R','-m','u:asmarr:rwX','/mnt/cephfs/media/asmr','/mnt/downloads/asmarr'],check=True)
    dirs=subprocess.run(['find','/mnt/cephfs/media/asmr','-type','d','-print0'],capture_output=True,check=True).stdout
    for folder in dirs.split(b'\0'):
        if folder:subprocess.run(['setfacl','-m','d:u:asmarr:rwx',os.fsdecode(folder)],check=True)
    override=Path('/etc/systemd/system/asmarr.service.d');override.mkdir(exist_ok=True)
    (override/'production.conf').write_text('[Service]\nReadOnlyPaths=\nReadWritePaths=/mnt/cephfs/media/asmr /mnt/downloads/asmarr\n')
    config=Path('/etc/asmarr/sources.yaml');cfg=yaml.safe_load(config.read_text());original_limit=cfg.get('max_downloads_per_run',25)
    cfg['max_downloads_per_run']=1;config.write_text(yaml.safe_dump(cfg))
    subprocess.run(['systemctl','daemon-reload'],check=True);subprocess.run(['systemctl','start','asmarr'],check=True)
    time.sleep(5)
    try:
        discovery=wait(command('discovery'),1800)
        cycle=wait(command('queue'),1800)
        plex=wait(command('plex'),300)
        playlists=wait(command('playlists'),600)
        indexing=wait(command('plex-verify'),300)
        report.update(finalMigration=result,finalScan=scan,discovery=discovery,boundedCycle=cycle,plex=plex,playlists=playlists,indexing=indexing)
        target=Path('/var/lib/asmarr/acceptance/cutover.json');target.write_text(json.dumps(report));target.chmod(0o600)
    finally:
        cfg['max_downloads_per_run']=original_limit;config.write_text(yaml.safe_dump(cfg))
    # Keep scheduling disabled until direct-source, controlled torrent fallback,
    # outage/repeat and playlist acceptance are recorded. This is deliberately
    # a bounded transition, not a claim that production acceptance has passed.
    print(json.dumps({'status':'bounded_cycle_complete','evidence':'/var/lib/asmarr/acceptance/cutover.json','scheduler':'awaiting_production_acceptance'}))

def rollback():
    subprocess.run(['systemctl','stop','asmarr'],check=True)
    with sqlite3.connect(DB) as db:db.execute("UPDATE settings SET value='shadow' WHERE key='mode'")
    subprocess.run(['systemctl','enable','--now','asmr-scraper.timer'],check=True)
    print(json.dumps({'status':'rolled_back','media':'preserved','oldTimer':'enabled'}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();g=parser.add_mutually_exclusive_group();g.add_argument('--apply',action='store_true');g.add_argument('--rollback',action='store_true');args=parser.parse_args()
    if args.rollback:rollback()
    elif args.apply:apply()
    else:print(json.dumps(preflight()))
