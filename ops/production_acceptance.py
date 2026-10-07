"""Final production gate. Default inspection never enables scheduling.

Protected acceptance input names real command IDs and canary recording keys.
Records, files, current Plex state and release identity are independently checked.
No boolean declaration alone can approve a direct download or torrent import.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from release_integrity import artifact_hash
from runtime_fixtures import TESTS

DB = Path('/var/lib/asmarr/asmarr.db')
APPLICATION = Path('/opt/asmarr')
ACCEPTANCE = Path('/var/lib/asmarr/acceptance')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def stamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def read_evidence(name):
    path = Path(name)
    if not path.is_absolute(): path = ACCEPTANCE/path
    if path.is_symlink() or not path.resolve().is_relative_to(ACCEPTANCE.resolve()):
        raise ValueError('evidence_outside_protected_acceptance_directory')
    return json.loads(path.read_text())


def completed(db, command_id, name, since):
    row = db.execute('SELECT name,state,started,finished,result FROM commands WHERE id=?', (command_id,)).fetchone()
    if not row or row['name'] != name or row['state'] != 'completed': return None
    if not row['started'] or not row['finished'] or stamp(row['started']) < since: return None
    result = json.loads(row['result'])
    if not isinstance(result, dict) or result.get('status') in {'failed', 'degraded', 'running'}: return None
    return result


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''): value.update(chunk)
    return value.hexdigest()


def media_snapshot(db):
    root = Path(db.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]).resolve()
    files = {}
    for row in db.execute("SELECT key,saved_path FROM assets WHERE state='complete' ORDER BY key"):
        path = Path(row['saved_path'] or '')
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('completed_media_missing_or_outside_library')
        stat = path.stat()
        files[row['key']] = {'path':str(path), 'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns}
    return files


def snapshot(db, plex):
    current = dict(plex);current.pop('capturedAt', None)
    downloads = [tuple(row) for row in db.execute('SELECT * FROM downloads ORDER BY 1')]
    return {'media':digest(media_snapshot(db)), 'downloads':digest(downloads), 'plex':digest(current)}


def canary(db, key, provider, command_id, since, plex):
    row = db.execute('SELECT saved_path,acquired,state FROM assets WHERE key=?', (key,)).fetchone()
    if not row or row['state'] != 'complete' or not row['acquired'] or row['acquired'] < since: return False
    root = Path(db.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]).resolve()
    path = Path(row['saved_path'] or '')
    if not path.resolve().is_relative_to(root) or not path.is_file(): return False
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(path)], capture_output=True, text=True, timeout=30)
    if probe.returncode: return False
    streams = json.loads(probe.stdout)
    if float(streams.get('format', {}).get('duration', 0)) <= 0 or not any(s.get('codec_type') == 'audio' for s in streams.get('streams', [])) or any(s.get('codec_type') == 'video' for s in streams.get('streams', [])): return False
    if not any(str(path) in track['paths'] for track in plex['tracks'].values()): return False
    events = db.execute("SELECT at,details FROM history WHERE recording_key=? AND event='import'", (key,)).fetchall()
    imported = [json.loads(event['details']) for event in events if stamp(event['at']) >= since]
    imported = [item for item in imported if item.get('provider') == provider and item.get('path') == str(path)]
    if not imported: return False
    command = completed(db, command_id, 'queue', since)
    if not command: return False
    if provider == 'direct':
        return any(item.get('status') == 'complete' and item.get('path') == str(path) for item in command.get('saved', []))
    if not any(item.get('sha256') == file_digest(path) for item in imported): return False
    for job in db.execute("SELECT id,download_id,details FROM queue WHERE recording_key=? AND provider='qbittorrent' AND media_kind='Audio' AND state IN ('imported','removed')", (key,)):
        detail = json.loads(job['details'])
        if not re.fullmatch(r'[a-fA-F0-9]{40}', job['download_id']) or not detail.get('guid') or not detail.get('indexerId'): continue
        if any(item.get('id') == job['id'] and item.get('status') == 'complete' and item.get('path') == str(path) for item in command.get('downloads', {}).get('jobs', [])):
            return True
    return False


def inspect(db, proof, deployment, cutover, transition, outages, repeat, plex, preservation, artifact):
    since = stamp(transition['startedAt'])
    same_release = lambda item:item.get('testedCommit') == deployment.get('sourceCommit') and item.get('artifactSha256') == deployment.get('artifactSha256')
    checks = {
        'productionMode': db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0] == 'production',
        'releaseMatches': bool(re.fullmatch(r'[a-f0-9]{40}',deployment.get('sourceCommit',''))) and same_release(proof) and same_release(cutover) and artifact == deployment.get('artifactSha256'),
        'boundedCutoverPassed': cutover.get('ready') is True and cutover.get('plexPreservation', {}).get('passed') is True,
        'databaseIntegrity': db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
        'noActiveCommands': db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running')").fetchone()[0] == 0,
        'schedulerHeld': db.execute('SELECT count(*) FROM tasks WHERE enabled!=0').fetchone()[0] == 0,
        'noFailedDownloads': db.execute("SELECT count(*) FROM queue WHERE state='failed'").fetchone()[0] == 0,
        'runtimeOutagesPassed': same_release(outages) and outages.get('passed') is True and outages.get('artifactUnchanged') is True and outages.get('networkPolicy') == 'connections blocked' and tuple(outages.get('tests', [])) == TESTS and outages.get('testsRun') == len(TESTS) and not any(outages.get(key) for key in ('errors','failures','skipped')) and outages.get('fixtureManifest',{}).get('sourceCommit') == deployment.get('sourceCommit') and all(re.fullmatch(r'[a-f0-9]{40}',outages.get('fixtureManifest',{}).get('files',{}).get(name,'')) for name in ('tests/test_acquisition.py','tests/test_legacy_parsers.py','tests/test_playlists.py')),
        'plexPreserved': preservation.get('passed') is True,
    }
    baseline = db.execute("SELECT value FROM meta WHERE key='migration_media_manifest'").fetchone()
    original = json.loads(baseline[0]).get('files', {}) if baseline else {}
    current = media_snapshot(db)
    checks['migratedMediaPreserved'] = len(original) >= 687 and all(current.get(key) == value for key, value in original.items())
    checks['distinctCanaries'] = bool(proof.get('directKey') and proof.get('fallbackKey') and proof['directKey'] != proof['fallbackKey'] and proof['directKey'] not in original and proof['fallbackKey'] not in original)
    checks['directAcquisitionAndIndex'] = canary(db, proof.get('directKey'), 'direct', proof.get('directCommandId'), since, plex)
    checks['controlledFallbackAndIndex'] = canary(db, proof.get('fallbackKey'), 'qbittorrent', proof.get('fallbackCommandId'), since, plex)
    index = completed(db, proof.get('indexCommandId'), 'plex-verify', since)
    playlists = completed(db, proof.get('playlistCommandId'), 'playlists-verify', since)
    checks['allMediaIndexed'] = bool(index and not index.get('unindexed') and index.get('indexedImported') == len(current))
    checks['fifteenPlaylistsVerified'] = bool(playlists and playlists.get('status') == 'ok' and playlists.get('managedCount') == 15)
    valid_repeat = same_release(repeat) and repeat.get('before') == repeat.get('after') == snapshot(db, plex)
    valid_repeat = valid_repeat and set(repeat.get('before', {})) == {'media','downloads','plex'} and stamp(repeat.get('startedAt', '1970-01-01T00:00:00Z')) >= since
    for name in ('queue','plex','playlists'):
        result = completed(db, repeat.get('commands', {}).get(name), name, stamp(repeat.get('startedAt', '1970-01-01T00:00:00Z')))
        valid_repeat = valid_repeat and result is not None
        command_finished = db.execute('SELECT finished FROM commands WHERE id=?',(repeat.get('commands',{}).get(name),)).fetchone()
        valid_repeat = valid_repeat and bool(command_finished and command_finished[0] and stamp(command_finished[0]) <= stamp(repeat.get('finishedAt','1970-01-01T00:00:00Z')))
        if result is not None and name == 'queue': valid_repeat = valid_repeat and all(isinstance(result.get(key),list) and not result[key] for key in ('saved','failed','fallback')) and isinstance(result.get('downloads',{}).get('jobs'),list) and all(item.get('status') == 'already_imported' for item in result['downloads']['jobs'])
        if result is not None and name == 'plex': valid_repeat = valid_repeat and result.get('status') in {'not_needed','indexed'}
        if result is not None and name == 'playlists': valid_repeat = valid_repeat and result.get('status') == 'healthy' and all(result.get(key) == 0 for key in ('created','updated','tagged'))
    checks['repeatCycleIdempotent'] = bool(valid_repeat)
    tasks = dict(db.execute('SELECT name,enabled FROM tasks'))
    checks['savedSchedulerMatches'] = set(tasks) == set(transition.get('taskEnabled', {})) and all(type(value) is int and value in (0,1) for value in transition.get('taskEnabled', {}).values())
    return {'ready':all(checks.values()), 'checks':checks, 'testedCommit':deployment.get('sourceCommit'), 'artifactSha256':artifact}


def audit():
    deployment = read_evidence('deployed-release.json')
    proof = read_evidence('production-acceptance.json')
    cutover = read_evidence('cutover.json')
    transition = read_evidence('transition.json')
    from plex_invariants import runtime_snapshot, compare
    plex = runtime_snapshot()
    preservation = compare(read_evidence(cutover['plexBaseline']), plex)
    with sqlite3.connect(DB.as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        report = inspect(db, proof, deployment, cutover, transition, read_evidence(proof['outageFile']), read_evidence(proof['repeatFile']), plex, preservation, artifact_hash(APPLICATION))
    report['checks']['legacyTimerDisabled'] = subprocess.run(['systemctl','is-enabled','asmr-scraper.timer'], capture_output=True, text=True).stdout.strip() == 'disabled'
    report['checks']['legacyTimerStopped'] = subprocess.run(['systemctl','is-active','asmr-scraper.timer'], capture_output=True, text=True).stdout.strip() == 'inactive'
    report['checks']['legacyScraperStopped'] = subprocess.run(['systemctl','is-active','asmr-scraper.service'], capture_output=True, text=True).stdout.strip() == 'inactive'
    report['ready'] = all(report['checks'].values())
    return report


def enable():
    if (ACCEPTANCE/'production-accepted.json').exists():
        return {'ready':False, 'reason':'Existing production acceptance requires review; no scheduler changes made'}
    report = audit()
    if not report['ready']: return report
    subprocess.run(['systemctl','stop','asmarr'], check=True)
    try:
        # Recheck after the worker has stopped, before changing scheduler state.
        report = audit()
        if not report['ready']: return report
        from plex_invariants import write_protected
        report['acceptedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_protected(ACCEPTANCE/'production-accepted.json', report)
        transition = read_evidence('transition.json')
        with sqlite3.connect(DB) as db:
            for name, enabled in transition['taskEnabled'].items():
                interval = db.execute('SELECT interval_seconds FROM tasks WHERE name=?', (name,)).fetchone()[0]
                next_run = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=interval)).isoformat()
                db.execute('UPDATE tasks SET enabled=?,next_run=? WHERE name=?', (enabled,next_run,name))
    finally:
        subprocess.run(['systemctl','start','asmarr'], check=True)
    report['scheduler'] = 'restored approved task states; verify live health after restart'
    return report


def capture_snapshot(output):
    deployment=read_evidence('deployed-release.json')
    artifact=artifact_hash(APPLICATION)
    if artifact != deployment.get('artifactSha256'):raise ValueError('release_changed')
    from plex_invariants import runtime_snapshot,write_protected
    plex=runtime_snapshot()
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        if db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0] != 'production' or db.execute('SELECT count(*) FROM tasks WHERE enabled!=0').fetchone()[0] or db.execute("SELECT count(*) FROM commands WHERE state IN ('queued','running')").fetchone()[0]:
            raise ValueError('snapshot_requires_quiet_bounded_production')
        state=snapshot(db,plex)
    report={'recordedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'testedCommit':deployment['sourceCommit'],'artifactSha256':artifact,'state':state}
    if artifact_hash(APPLICATION)!=artifact:raise ValueError('release_changed_during_snapshot')
    write_protected(output,report)
    return {'ready':True,'scope':'read-only repeat snapshot; not production acceptance','evidence':str(output)}


def main():
    parser = argparse.ArgumentParser()
    options=parser.add_mutually_exclusive_group()
    options.add_argument('--enable-scheduler', action='store_true')
    options.add_argument('--snapshot-output',type=Path)
    args = parser.parse_args()
    try: report = capture_snapshot(args.snapshot_output) if args.snapshot_output else enable() if args.enable_scheduler else audit()
    except Exception as error:
        report = {'ready':False, 'reason':type(error).__name__, 'message':'Production acceptance evidence is missing, invalid or incomplete'}
    print(json.dumps(report))
    if not report['ready']: raise SystemExit(1)


if __name__ == '__main__': main()
