"""Categorize ASMR tracks and maintain native Plex smart playlists."""
import collections
import json
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, unquote, urlsplit, parse_qs, quote

import requests
from categories import CATEGORIES, classify, metadata_text


class PlaylistError(Exception):
    pass


class Client:
    def __init__(self, cfg, secrets):
        self.base = cfg['plex']['url'].rstrip('/')
        self.session = requests.Session()
        self.session.headers['X-Plex-Token'] = secrets.get('plex_token') or cfg['plex'].get('token', '')
        if not self.session.headers['X-Plex-Token']:
            raise PlaylistError('playlist_credentials_missing')

    def call(self, method, path, **params):
        # Do not retry POST: an interrupted response may already have created a playlist.
        try:
            with self.session.request(method, self.base + path, params=params, timeout=(10, 60)) as r:
                if r.status_code >= 400:
                    raise PlaylistError('plex_http_' + str(r.status_code))
                return ET.fromstring(r.text) if r.text.strip() else ET.Element('MediaContainer')
        except (requests.RequestException, ET.ParseError) as e:
            raise PlaylistError('plex_' + type(e).__name__) from None


def all_items(client, path, element, **params):
    result, offset = [], 0
    while True:
        page = client.call('GET', path, **params,
                           **{'X-Plex-Container-Start': offset, 'X-Plex-Container-Size': 500})
        items = page.findall(element)
        result.extend(items)
        total = int(page.get('totalSize', page.get('size', len(items))))
        if len(result) >= total:
            break
        if not items or int(page.get('offset', offset)) != offset:
            raise PlaylistError('plex_incomplete_catalog')
        offset += len(items)
    keys = [x.get('ratingKey') for x in result]
    if len(keys) != len(set(keys)) or None in keys or len(result) != total:
        raise PlaylistError('plex_inconsistent_catalog')
    return result


def details(client, keys, section):
    tracks = []
    for i in range(0, len(keys), 100):
        rows = client.call('GET', '/library/metadata/' + ','.join(keys[i:i+100])).findall('Track')
        if {t.get('ratingKey') for t in rows} != set(keys[i:i+100]):
            raise PlaylistError('plex_incomplete_track_metadata')
        if any(t.get('librarySectionID') != section for t in rows):
            raise PlaylistError('plex_wrong_library')
        tracks.extend(rows)
    return tracks


def mood_names(track):
    return {m.get('tag') for m in track.findall('Mood')}


def filter_matches(content, section, mood):
    # Plex stores the query path URL-encoded inside its content URI.
    content = content or ''
    if content.startswith('library://'):
        content = unquote(content)
    path = '/library/sections/' + section + '/all'
    if path + '?' not in content:
        return False
    query = parse_qs(content.split(path + '?', 1)[1])
    return query == {'type': ['10'], 'track.mood': [mood], 'sort': ['addedAt:desc']}


def sync(db, cfg, secrets, api, *, dry_run=False, client=None):
    opts = cfg.get('playlists', {})
    if not opts.get('enabled', False):
        return {'status': 'disabled'}
    started = int(time.time())
    report = {'status': 'healthy', 'tracks': 0, 'tagged': 0, 'created': 0, 'updated': 0,
              'categories': {}, 'checked_at': started}
    try:
        client = client or Client(cfg, secrets)
        section = str(cfg['plex']['section_id'])
        catalog = all_items(client, '/library/sections/' + section + '/all', 'Track', type=10)
        tracks = details(client, [t.get('ratingKey') for t in catalog], section)
        if not tracks:
            raise PlaylistError('plex_empty_library')
        report['tracks'] = len(tracks)
        titles = dict(db.execute("SELECT saved_path,title FROM assets WHERE state='complete' AND saved_path IS NOT NULL"))
        enabled = opts.get('categories', list(CATEGORIES))
        if set(enabled) - CATEGORIES.keys():
            raise PlaylistError('unknown_playlist_category')
        managed = {'ASMR: ' + name for name, _ in CATEGORIES.values()}
        members = {k: set() for k in enabled}
        changes = collections.defaultdict(list)
        expected_moods = {}
        for track in tracks:
            key = track.get('ratingKey')
            original = [titles[p.get('file')] for p in track.findall('.//Part') if p.get('file') in titles]
            cats = set(classify(metadata_text(track, original))) & set(enabled)
            for cat in cats:
                members[cat].add(key)
            current = mood_names(track)
            desired = (current - managed) | {'ASMR: ' + CATEGORIES[k][0] for k in cats}
            if current != desired:
                changes[(tuple(sorted(current)), tuple(sorted(desired)))].append(key)
                expected_moods[key] = desired
                if not dry_run and api.meta_get(db, 'playlist_mood_backup:' + key) is None:
                    api.meta_set(db, 'playlist_mood_backup:' + key,
                                 {'moods': sorted(current), 'locked': next((f.get('locked') for f in track.findall('Field') if f.get('name') == 'mood'), '0')})
        report['categories'] = {k: len(v) for k, v in members.items()}
        report['unclassified'] = len(tracks) - len(set().union(*members.values()))
        report['tagged'] = len(expected_moods)
        if dry_run:
            report['status'] = 'preview'
            return report
        db.commit()  # Save rollback metadata before changing Plex.
        for (old, desired), keys in changes.items():
            for i in range(0, len(keys), 40):
                params = {'type': 10, 'id': ','.join(keys[i:i+40]), 'mood.locked': 1}
                removed = set(old) - set(desired)
                if removed:
                    params['mood[].tag.tag-'] = ','.join(quote(x, safe='') for x in sorted(removed))
                params.update({'mood[%d].tag.tag' % j: tag for j, tag in enumerate(desired)})
                client.call('PUT', '/library/sections/' + section + '/all', **params)
        for track in details(client, list(expected_moods), section):
            if mood_names(track) != expected_moods[track.get('ratingKey')]:
                raise PlaylistError('plex_mood_verification_failed')
        playlists = all_items(client, '/playlists', 'Playlist')
        machine = client.call('GET', '/identity').get('machineIdentifier')
        if not machine:
            raise PlaylistError('plex_identity_missing')
        registry = api.meta_get(db, 'managed_playlists', {})
        for cat, wanted in members.items():
            title = 'ASMR — ' + CATEGORIES[cat][0]
            marker = 'Managed by ASMR downloader; section=' + section + '; category=' + cat + '.'
            mood = 'ASMR: ' + CATEGORIES[cat][0]
            uri = 'server://' + machine + '/com.plexapp.plugins.library/library/sections/' + section + '/all?' + urlencode({'type': 10, 'track.mood': mood, 'sort': 'addedAt:desc'})
            record = registry.get(cat, {})
            matches = [p for p in playlists if p.get('summary') == marker or
                       (p.get('ratingKey') == record.get('id') and p.get('guid') == record.get('guid'))]
            # Recover creation after an interrupted response; preexisting IDs are excluded.
            if not matches and 'before_ids' in record:
                for p in playlists:
                    if p.get('ratingKey') not in record['before_ids'] and p.get('title') == title:
                        detail = client.call('GET', '/playlists/' + p.get('ratingKey')).find('Playlist')
                        if detail is not None and filter_matches(detail.get('content'), section, mood):
                            matches.append(detail)
            if len(matches) > 1:
                raise PlaylistError('duplicate_managed_playlist:' + cat)
            playlist = matches[0] if matches else None
            if playlist is None and not wanted:
                continue  # Create an empty category only when it first receives a track.
            if playlist is None:
                if any(p.get('title') == title for p in playlists):
                    raise PlaylistError('playlist_title_conflict:' + cat)
                registry[cat] = {'before_ids': [p.get('ratingKey') for p in playlists]}
                with db:
                    api.meta_set(db, 'managed_playlists', registry)
                playlist = client.call('POST', '/playlists', type='audio', title=title, smart=1, uri=uri).find('Playlist')
                if playlist is None:
                    raise PlaylistError('playlist_creation_unconfirmed')
                report['created'] += 1
                playlists.append(playlist)
            key = playlist.get('ratingKey')
            registry[cat] = {'id': key, 'guid': playlist.get('guid')}
            with db:
                api.meta_set(db, 'managed_playlists', registry)
            detail = client.call('GET', '/playlists/' + key).find('Playlist')
            if detail is None or detail.get('smart') != '1':
                raise PlaylistError('managed_playlist_not_smart')
            if not filter_matches(detail.get('content'), section, mood):
                client.call('PUT', '/playlists/' + key + '/items', uri=uri)
                report['updated'] += 1
            if detail.get('summary') != marker:
                client.call('PUT', '/playlists/' + key, summary=marker)
            actual = all_items(client, '/playlists/' + key + '/items', 'Track')
            if {t.get('ratingKey') for t in actual} != wanted:
                raise PlaylistError('playlist_membership_mismatch:' + cat)
        report['playlists'] = {k: v.get('id') for k, v in registry.items()}
    except (PlaylistError, KeyError, ValueError, TypeError) as e:
        report['status'] = 'failed'
        report['error'] = str(e) if isinstance(e, PlaylistError) else type(e).__name__
    if not dry_run:
        with db:
            api.meta_set(db, 'playlist_status', report)
    return report
