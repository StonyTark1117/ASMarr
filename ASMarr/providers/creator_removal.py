"""Previewed creator removal, executed only under the host's provider lease.

Retained recording rows reconnect on explicit re-addition. No directory-tree
deletion, torrent data deletion, global Plex trash emptying, or tag editing.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlencode


def exclusions(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='creator_exclusions'").fetchone():
        return []
    return [dict(r) for r in db.execute('SELECT * FROM creator_exclusions')]


def excluded(db, name, identities=()):
    names = {name.casefold()}
    handles = {(kind, handle.casefold()) for kind, handle in identities}
    for row in exclusions(db):
        linked = json.loads(row['identities'])
        if names & {row['name'].casefold(), *(a.casefold() for a in json.loads(row['aliases'])), *(i['handle'].casefold() for i in linked)}:
            return True
        if handles & {(i['kind'], i['handle'].casefold()) for i in linked}:
            return True
    return False


def owned_paths(db, name):
    return list(db.execute('''SELECT a.key,m.media_kind,m.saved_path FROM assets a
        JOIN media_assets m ON m.recording_key=a.key WHERE a.creator=? AND m.saved_path IS NOT NULL
        UNION SELECT key,'Audio',saved_path FROM assets WHERE creator=? AND saved_path IS NOT NULL''', (name, name)))


def root_for(path, kind, cfg):
    root = Path(cfg['video_root'] if kind == 'Video' else cfg['output_root'])
    candidate = Path(path)
    if not root.is_absolute() or not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('outside_library_root')
    relative = candidate.relative_to(root)
    if not relative.parts:
        raise ValueError('not_a_media_file')
    return root, relative


@contextlib.contextmanager
def parent_handle(path, kind, cfg):
    root, relative = root_for(path, kind, cfg)
    # Pin each directory with openat + O_NOFOLLOW, including ancestors of root.
    # A symlink in any component must never escape the reviewed library path.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, relative.name
    finally:
        os.close(fd)


def file_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink]


def preview(db, cfg, creator_id):
    row = db.execute('SELECT * FROM creators WHERE id=?', (creator_id,)).fetchone()
    if not row:
        return {'error': 'Creator no longer exists.'}
    creator = dict(row)
    files = []
    seen = set()
    other_claims = set()
    for other in db.execute('''SELECT a.saved_path FROM assets a WHERE a.creator<>? AND a.saved_path IS NOT NULL
            UNION SELECT m.saved_path FROM media_assets m JOIN assets a ON a.key=m.recording_key
            WHERE a.creator<>? AND m.saved_path IS NOT NULL''', (creator['name'], creator['name'])):
        try:
            other_claims.add(str(Path(other[0]).resolve()))
        except (OSError, RuntimeError):
            other_claims.add(os.path.abspath(other[0]))
    for other in db.execute('SELECT saved_path FROM downloads WHERE creator<>? AND saved_path IS NOT NULL',(creator['name'],)):
        try:
            other_claims.add(str(Path(other[0]).resolve()))
        except (OSError, RuntimeError):
            other_claims.add(os.path.abspath(other[0]))
    for tracked in owned_paths(db, creator['name']):
        path = tracked['saved_path']
        if path in seen:
            continue
        seen.add(path)
        entry = {'path': path, 'mediaKind': tracked['media_kind'], 'bytes': 0, 'status': 'deletable'}
        shared = os.path.abspath(path) in other_claims
        try:
            with parent_handle(path, tracked['media_kind'], cfg) as (fd, name):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            entry['identity'] = file_identity(info)
            entry['bytes'] = info.st_size
            if not stat.S_ISREG(info.st_mode):
                entry['status'] = 'unsafe_file_type'
            elif shared or info.st_nlink != 1:
                entry['status'] = 'shared_file_preserved'
        except FileNotFoundError:
            entry['status'] = 'shared_file_preserved' if shared else 'missing'
        except (OSError, ValueError):
            entry['status'] = 'unsafe_or_inaccessible_path'
        files.append(entry)
    identities = [dict(r) for r in db.execute('SELECT kind,handle,enabled FROM identities WHERE creator_id=? ORDER BY id', (creator_id,))]
    recordings = [dict(r) for r in db.execute('SELECT key,state,saved_path FROM assets WHERE creator=? ORDER BY key', (creator['name'],))]
    jobs = [dict(r) for r in db.execute('SELECT q.id,q.download_id,q.provider,q.state FROM queue q JOIN assets a ON a.key=q.recording_key WHERE a.creator=? ORDER BY q.id', (creator['name'],))]
    fingerprint = {'creator': creator, 'files': files, 'identities': identities, 'recordings': recordings, 'jobs': jobs,
                   'roots': [cfg['output_root'],cfg['video_root']]}
    token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return {'creatorId': creator_id, 'name': creator['name'], 'recordings': len(recordings),
            'files': files, 'bytes': sum(f['bytes'] for f in files if f['status'] == 'deletable'),
            'folders': sorted({str(Path(f['path']).parent) for f in files}),
            'planToken': token, 'downloadOriginalsPreserved': True}


def stop_downloads(db, creator, qbit):
    jobs = list(db.execute('''SELECT q.* FROM queue q JOIN assets a ON a.key=q.recording_key
        WHERE a.creator=? AND q.provider='qbittorrent' AND q.state IN ('submitting','downloading','importing')''', (creator,)))
    if not jobs:
        return
    session, base, _ = qbit()
    for job in jobs:
        if db.execute("SELECT 1 FROM queue q JOIN assets a ON a.key=q.recording_key WHERE a.creator<>? AND q.download_id=? AND q.state NOT IN ('cancelled','removed','failed')", (creator, job['download_id'])).fetchone():
            raise ValueError('Active torrent is shared with another creator; resolve it before removal.')
        response = session.get(base + '/api/v2/torrents/info', params={'hashes': job['download_id']}, timeout=30)
        response.raise_for_status()
        torrents = response.json()
        if not torrents:
            continue
        if len(torrents) != 1 or torrents[0].get('category') != 'asmarr' or torrents[0].get('hash', '').lower() != job['download_id'].lower():
            raise ValueError('Download ownership could not be verified; nothing was deleted.')
        response = session.post(base + '/api/v2/torrents/delete', data={'hashes': job['download_id'], 'deleteFiles': 'false'}, timeout=30)
        response.raise_for_status()


def remove(db, cfg, args, qbit, plex_cleanup):
    if db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0] != 'production':
        raise ValueError('Creator removal requires production mode.')
    plan = preview(db, cfg, args['creatorId'])
    if 'error' in plan:
        raise ValueError(plan['error'])
    if not isinstance(args.get('deleteFiles'), bool) or not isinstance(args.get('preventReadd'), bool):
        raise ValueError('Choose file handling and re-addition policy explicitly.')
    if args.get('planToken') != plan['planToken']:
        raise ValueError('Creator or files changed. Open a fresh removal preview and confirm again.')
    if args['deleteFiles'] and args.get('confirmation') != plan['name']:
        raise ValueError('Type the exact creator name to delete files.')
    # Global provider serialization means no import/scan/discovery is executing
    # during removal. Finish client ownership checks before deleting any files.
    stop_downloads(db, plan['name'], qbit)
    # Durable quiescence comes before filesystem mutations. A process crash
    # leaves this creator visibly unmonitored, not silently downloading again.
    with db:
        db.execute('UPDATE creators SET monitored=0,monitor_video=0 WHERE id=?', (args['creatorId'],))
        db.execute('UPDATE identities SET enabled=0 WHERE creator_id=?', (args['creatorId'],))
        db.execute("UPDATE queue SET state='cancelled' WHERE recording_key IN (SELECT key FROM assets WHERE creator=?)", (plan['name'],))
        db.execute("UPDATE media_assets SET wanted=0,state=CASE WHEN state IN ('complete','imported') THEN state ELSE 'cancelled' END WHERE recording_key IN (SELECT key FROM assets WHERE creator=?)", (plan['name'],))
        db.execute("UPDATE commands SET state='cancelled',finished=datetime('now'),error='Creator removal in progress' WHERE state='queued' AND (json_extract(arguments,'$.creatorId')=? OR json_extract(arguments,'$.key') IN (SELECT key FROM assets WHERE creator=?))", (args['creatorId'], plan['name']))
        db.execute('INSERT INTO history(at,event,details) VALUES(?,?,?)', (str(int(time.time())), 'creator-removal-started', json.dumps({'name': plan['name'], 'deleteFiles': args['deleteFiles']})))
    deleted, preserved, warnings, missing = [], [], [], []
    for item in plan['files']:
        if not args['deleteFiles']:
            preserved.append(item['path'])
            continue
        if item['status'] != 'deletable':
            if item['status'] == 'missing':
                missing.append(item['path'])
            else:
                warnings.append({'path': item['path'], 'reason': item['status']})
                preserved.append(item['path'])
            continue
        try:
            with parent_handle(item['path'], item['mediaKind'], cfg) as (fd, filename):
                info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
                if file_identity(info) != item['identity'] or not stat.S_ISREG(info.st_mode):
                    raise ValueError('file_changed_after_preview')
                os.unlink(filename, dir_fd=fd)
            deleted.append(item['path'])
        except (OSError, ValueError):
            warnings.append({'path': item['path'], 'reason': 'file_changed_or_delete_failed'})
            preserved.append(item['path'])
    now = int(time.time())
    with db:
        creator = dict(db.execute('SELECT * FROM creators WHERE id=?', (args['creatorId'],)).fetchone())
        identities = [dict(r) for r in db.execute('SELECT kind,handle FROM identities WHERE creator_id=?', (args['creatorId'],))]
        if args['preventReadd']:
            db.execute('INSERT OR REPLACE INTO creator_exclusions(name,aliases,identities,removed) VALUES(?,?,?,?)',
                       (creator['name'], creator['aliases'], json.dumps(identities), now))
        for path in deleted + missing:
            db.execute("UPDATE assets SET saved_path=NULL,state='removed' WHERE creator=? AND saved_path=?", (creator['name'], path))
            db.execute("UPDATE media_assets SET saved_path=NULL,state='cancelled',wanted=0 WHERE saved_path=? AND recording_key IN (SELECT key FROM assets WHERE creator=?)", (path, creator['name']))
        db.execute("UPDATE media_assets SET wanted=0,state=CASE WHEN state IN ('complete','imported') THEN state ELSE 'cancelled' END WHERE recording_key IN (SELECT key FROM assets WHERE creator=?)", (creator['name'],))
        db.execute("UPDATE queue SET state='cancelled' WHERE recording_key IN (SELECT key FROM assets WHERE creator=?)", (creator['name'],))
        db.execute("UPDATE commands SET state='cancelled',finished=datetime('now'),error='Creator removed' WHERE state='queued' AND (json_extract(arguments,'$.creatorId')=? OR json_extract(arguments,'$.key') IN (SELECT key FROM assets WHERE creator=?))", (args['creatorId'], creator['name']))
        db.execute('DELETE FROM backfill_jobs WHERE creator_id=?', (args['creatorId'],))
        db.execute('DELETE FROM identities WHERE creator_id=?', (args['creatorId'],))
        db.execute('DELETE FROM creators WHERE id=?', (args['creatorId'],))
        result = {'status': 'removed', 'name': creator['name'], 'deleted': deleted, 'preserved': preserved,
                  'alreadyMissing':missing,'warnings': warnings, 'preventReadd': args['preventReadd'], 'downloadOriginalsPreserved': True}
        db.execute('INSERT INTO history(at,event,details) VALUES(?,?,?)', (str(now), 'creator-removed', json.dumps(result)))
    # Keep-files removal makes NO Plex requests. Never empty a section's trash.
    if deleted or missing:
        try:
            result['plex'] = plex_cleanup(deleted + missing)
        except Exception:
            result['plex'] = {'status': 'cleanup_pending', 'message': 'Files removed; Plex cleanup failed. Review System tasks.'}
    else:
        result['plex'] = {'status': 'unchanged'}
    return result


def cleanup_plex(cfg, secrets, deleted, client_factory=None):
    """Delete only exact single-recording metadata entries with all parts gone.

    Plex may disallow item deletion. Report pending cleanup instead of changing
    server policy or emptying any section's trash (which affects other users).
    """
    import plex_playlists
    if not cfg.get('plex', {}).get('url'):
        return {'status': 'not_configured'}
    client = (client_factory or plex_playlists.Client)(cfg, secrets)
    removed, pending = [], []
    plex = cfg['plex']
    sections = [('Audio', plex.get('section_id'), cfg['output_root']),
                ('Video', plex.get('video', {}).get('section_id'), cfg['video_root'])]
    for kind, section, root in sections:
        paths = {path for path in deleted if Path(path).is_relative_to(Path(root))}
        if not paths:
            continue
        if section is None or not str(section).isdigit():
            pending.extend(sorted(paths));continue
        items = plex_playlists.all_items(client, f'/library/sections/{section}/all', 'Track' if kind == 'Audio' else 'Video', **({'type': 10} if kind == 'Audio' else {}))
        if kind == 'Audio':
            items = plex_playlists.details(client,[item.get('ratingKey') for item in items],str(section))
        else:
            detailed=[]
            for offset in range(0,len(items),100):
                keys=[item.get('ratingKey') for item in items[offset:offset+100]]
                rows=client.call('GET','/library/metadata/'+','.join(keys)).findall('Video')
                if {row.get('ratingKey') for row in rows}!=set(keys) or any(row.get('librarySectionID')!=str(section) for row in rows):
                    raise ValueError('Plex video ownership could not be verified.')
                detailed.extend(rows)
            items=detailed
        indexed = set()
        for item in items:
            parts = {p.get('file') for p in item.findall('.//Part')}
            matched = parts & paths
            if not matched:
                continue
            indexed.update(matched)
            key = item.get('ratingKey', '')
            if not key.isdigit() or not parts or not parts <= paths or any(os.path.lexists(p) for p in parts):
                pending.extend(sorted(matched));continue
            # Re-read exact item before mutation to catch multipart changes.
            detail = client.call('GET', '/library/metadata/' + key)
            actual_parts = {p.get('file') for p in detail.findall('.//Part')}
            rows = list(detail)
            if len(rows) != 1 or rows[0].get('ratingKey') != key or rows[0].get('librarySectionID') != str(section) or actual_parts != parts:
                pending.extend(sorted(matched));continue
            try:
                client.call('DELETE', '/library/metadata/' + key)
                removed.append(key)
            except plex_playlists.PlaylistError:
                pending.extend(sorted(matched))
        # A targeted refresh reconciles deleted paths, never a full-library scan.
        for folder in sorted({str(Path(path).parent) for path in paths}):
            try:
                client.call('GET', f'/library/sections/{section}/refresh?' + urlencode({'path': folder}))
            except plex_playlists.PlaylistError:
                pending.extend(sorted(paths))
    return {'status': 'cleanup_pending' if pending else 'cleaned', 'removedRatingKeys': removed,
            'pendingPaths': sorted(set(pending))}
