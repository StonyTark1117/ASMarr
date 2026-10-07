"""Capture the verified audio baseline; this does not authorize cutover.

Run inside CT130 after independently checking the supplied GitHub CI run.
The combined release must pass CI again before pre-cutover.json is approved.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3

from smoke import command, wait


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tested-sha', required=True)
    parser.add_argument('--ci-run', required=True, type=int)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{40}', args.tested_sha):
        parser.error('tested-sha must be a full Git commit SHA')
    media = json.loads(Path('/var/lib/asmarr/acceptance/migrated-audio-validation.json').read_text())
    if media['total'] != 687 or media['valid'] != 687 or media['failures']:
        raise RuntimeError('Migrated audio validation is incomplete')
    playlists = wait(command('playlists-verify'), 180)
    scan = wait(command('disk-scan'), 180)
    plex = wait(command('plex-verify'), 180)
    if any(result.get('status') == 'running' for result in (playlists, scan, plex)):
        raise RuntimeError('Verification remains in progress; evidence was not approved')
    with sqlite3.connect('/var/lib/asmarr/asmarr.db') as db:
        states = dict(db.execute('SELECT state,count(*) FROM assets GROUP BY state'))
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        latest = db.execute('SELECT result FROM migration_audits ORDER BY id DESC LIMIT 1').fetchone()
        migration = json.loads(latest[0]) if latest else {}
    if states != {'complete': 687, 'skipped': 8, 'superseded': 1} or integrity != 'ok' or not migration.get('passed'):
        raise RuntimeError('Migration baseline does not match the approved production snapshot')
    if playlists.get('status') != 'ok' or playlists.get('managedCount') != 15:
        raise RuntimeError('Managed playlist verification failed')
    if scan.get('missing') or scan.get('outside') or not scan.get('unchanged'):
        raise RuntimeError('Library path preservation verification failed')
    if plex.get('unindexed') or plex.get('indexedImported') != 687:
        raise RuntimeError('Imported Plex indexing verification failed')
    evidence = {
        'scope': 'audio baseline only; combined release approval still required',
        'recordedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
        'testedCommit': args.tested_sha,
        'ciRun': f'https://github.com/StonyTark1117/ASMarr/actions/runs/{args.ci_run}',
        'providerTests': 70, 'sqliteAssertions': 13, 'browserWorkflows': 5,
        'states': states, 'integrity': integrity, 'migration': migration,
        'mediaValidation': media, 'scan': scan, 'plex': plex, 'playlists': playlists,
        'productionAcceptance': 'pending',
    }
    target = Path('/var/lib/asmarr/acceptance/audio-baseline.json')
    target.parent.mkdir(mode=0o700, exist_ok=True)
    with target.open('w') as output:
        target.chmod(0o600)
        json.dump(evidence, output, indent=2)
    print(json.dumps({'evidence': str(target), 'states': states, 'integrity': integrity,
                      'productionAcceptance': 'pending'}))


if __name__ == '__main__':
    main()
