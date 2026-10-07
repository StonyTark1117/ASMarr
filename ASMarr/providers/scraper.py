#!/usr/bin/env python3
"""Daily ASMR archival with explicit source health and recoverable download state."""
import argparse
import collections
import contextlib
import datetime as dt
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urlunparse, parse_qs, quote, unquote

import mutagen
import requests
import yaml
from titles import compact_title
import youtube_source as youtube
import sfw_sources
import plex_playlists
import acquisition_profiles

UA = 'linux:asmr-scraper:1.0 (personal media library)'
F4_RE = re.compile(r'\[\s*F4[FMA]+\s*\]', re.I)
M4_RE = re.compile(r'\[\s*M4[FMA]+\s*\]', re.I)
F4F_RE = re.compile(r'\[\s*F4F\s*\]', re.I)
URL_RE = re.compile(r'https?://[^\s)\]\'"<>]+')
AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.opus', '.wav', '.flac'}
BAD_STATES = {'unavailable', 'parse_failure', 'failed'}
PLURALS = {'elf': ['elves'], 'werewolf': ['werewolves'], 'fairy': ['fairies'],
           'witch coven': ['witch covens']}


class MediaError(ValueError):
    pass


class SourceError(Exception):
    def __init__(self, reason, status='unavailable'):
        super().__init__(reason)
        self.status = status


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain(text):
    p = TextParser()
    p.feed(text or '')
    return html.unescape(' '.join(p.parts))


def title_passes(title, cfg, trusted=False, topic_pattern=None):
    text = plain(title)
    custom_speaker = acquisition_profiles.speaker_decision(text, cfg, trusted)
    if custom_speaker is not None:
        if not custom_speaker[0]: return custom_speaker
    else:
        if M4_RE.search(text):
            return False, 'male_speaker_tag'
        if F4F_RE.search(text):
            return False, 'female_only_audience'
        if not trusted and not F4_RE.search(text):
            return False, 'missing_speaker_tag'
    custom_topic = acquisition_profiles.topic_decision(text, cfg)
    if custom_topic is not None:
        if not custom_topic[0]: return custom_topic
    elif topic_pattern is not None:
        if not re.search(topic_pattern, text, re.I):
            return False, 'missing_topic'
    elif cfg.get('hypno_required', True):
        keywords = cfg.get('hypno_keywords', [])
        if keywords and not any(k.casefold() in text.casefold() for k in keywords):
            return False, 'missing_topic'
    for term in cfg.get('fantasy_blocklist', []):
        variants = [term] + PLURALS.get(term.casefold(), [])
        # Exact words and phrases, including deliberate ordinary plural forms.
        if term[-1:].isalpha() and not term.endswith('s'):
            variants.append(term + 's')
        for variant in variants:
            pattern = r'(?<!\w)' + r'\s+'.join(re.escape(x) for x in variant.split()) + r'(?!\w)'
            if re.search(pattern, text, re.I):
                return False, 'blocked_term:' + term
    custom_exclusion = acquisition_profiles.excluded_decision(text, cfg)
    if not custom_exclusion[0]: return custom_exclusion
    return True, 'accepted'


def safe_filename(text, limit=140):
    text = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '_', plain(text))
    text = re.sub(r'\s+', ' ', text).strip(' .') or 'untitled'
    return text.encode('utf8')[:limit].decode('utf8', 'ignore')


def canonical(url):
    p = urlparse(html.unescape(url))
    host = (p.hostname or '').lower()
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}:
        video = p.path.strip('/') if host == 'youtu.be' else parse_qs(p.query).get('v', [''])[0]
        if not video and p.path.startswith('/shorts/'):
            video = p.path.split('/')[2]
        if video:
            return 'https://www.youtube.com/watch?v=' + video
    if host in {'soundgasm.net', 'www.soundgasm.net'}:
        return 'https://soundgasm.net' + quote(unquote(p.path).rstrip('/'), safe='/')
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, '', p.query, ''))


def targets(post, cfg):
    urls = [post.get('url_overridden_by_dest') or post.get('url', '')]
    urls += URL_RE.findall(post.get('selftext', '') or '')
    result = []
    for url in urls:
        host = (urlparse(url).hostname or '').lower()
        if host in {'soundgasm.net', 'www.soundgasm.net'}:
            if not re.fullmatch(r'/u/[^/]+/[^/]+/?', urlparse(url).path):
                continue
            kind = 'soundgasm'
        elif host in {'i.redd.it', 'v.redd.it'}:
            if Path(urlparse(url).path).suffix.lower() not in AUDIO_EXTS:
                continue
            kind = 'reddit_media'
        elif (cfg.get('youtube_enabled', False) and cfg.get('youtube_reddit_links_enabled', False)
              and host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}):
            kind = 'youtube'
        else:
            continue
        target = [kind, canonical(url)]
        if target not in result:
            result.append(target)
    return result


class HTTP:
    def __init__(self, interval=1.1):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = UA
        self.interval = interval
        self.next_request = 0
    def request(self, method, url, **kwargs):
        for attempt in range(3):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval
            try:
                r = self.session.request(method, url, timeout=(10, 45), **kwargs)
            except requests.RequestException:
                if attempt == 2:
                    raise SourceError('network_error') from None
                time.sleep(2 ** attempt)
                continue
            if r.status_code in {429, 500, 502, 503, 504}:
                delay = 2 ** attempt
                retry = r.headers.get('Retry-After')
                if retry:
                    try:
                        delay = float(retry)
                    except ValueError:
                        from email.utils import parsedate_to_datetime
                        try:
                            delay = (parsedate_to_datetime(retry) - dt.datetime.now(dt.timezone.utc)).total_seconds()
                        except (ValueError, TypeError):
                            delay = 60
                r.close()
                # Do not retry sooner than a long server-requested cooldown.
                if attempt == 2 or delay > 60:
                    raise SourceError('http_' + str(r.status_code))
                time.sleep(max(0, delay))
                continue
            if r.status_code >= 400:
                status = r.status_code
                r.close()
                raise SourceError('http_' + str(status))
            return r
        raise SourceError('retry_exhausted')
    def get(self, url, **kw):
        return self.request('GET', url, **kw)


def open_db(path, readonly=False):
    conn = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) if readonly else sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if readonly:
        return conn
    ensure_schema(conn)
    return conn


def ensure_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, source TEXT, url TEXT,
      creator TEXT, title TEXT, saved_path TEXT, ts INTEGER);
    CREATE TABLE IF NOT EXISTS assets (key TEXT PRIMARY KEY, url TEXT, targets TEXT, source TEXT,
      creator TEXT, title TEXT, published INTEGER, saved_path TEXT, state TEXT,
      attempts INTEGER DEFAULT 0, retry_after INTEGER DEFAULT 0, error TEXT, acquired INTEGER);
    CREATE TABLE IF NOT EXISTS aliases (id TEXT PRIMARY KEY, asset_key TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, status TEXT, last_attempt INTEGER,
      last_success INTEGER, last_download INTEGER, details TEXT);
    CREATE TABLE IF NOT EXISTS runs (started INTEGER PRIMARY KEY, finished INTEGER, status TEXT, details TEXT);
    ''')


def meta_get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def meta_set(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


def validate_audio(path):
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError('empty_or_missing_media')
    process = subprocess.Popen(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration:stream=codec_type', '-of', 'json', str(path)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, _ = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        # A task stuck in kernel filesystem I/O may not reap immediately.
        raise RuntimeError('media_read_timeout') from None
    if process.returncode:
        raise MediaError('invalid_media')
    data = json.loads(stdout)
    if not any(s.get('codec_type') == 'audio' for s in data.get('streams', [])):
        raise MediaError('no_audio_stream')
    if float(data.get('format', {}).get('duration', 0)) <= 0:
        raise MediaError('invalid_duration')
    return data


def reconcile(db, root, apply=False):
    root = Path(root).resolve()
    fixes, missing = [], []
    for row in db.execute('SELECT id,saved_path FROM downloads'):
        p = Path(row['saved_path'])
        if not p.resolve().is_relative_to(root):
            raise MediaError('state_path_outside_library')
        if p.is_file():
            continue
        moved = p.parent / 'Singles' / p.name
        if moved.is_file() and moved.resolve().is_relative_to(root):
            if moved.stat().st_size <= 0:
                raise MediaError('empty_reconciliation_target')
            fixes.append((row['id'], str(moved)))
        else:
            missing.append(row['id'])
    if apply:
        with db:
            for sid, new_path in fixes:
                old_path = db.execute('SELECT saved_path FROM downloads WHERE id=?', (sid,)).fetchone()[0]
                db.execute("UPDATE assets SET saved_path=?,state=CASE WHEN state='missing' THEN 'complete' ELSE state END,error=CASE WHEN state='missing' THEN NULL ELSE error END WHERE saved_path=?", (new_path, old_path))
                db.execute('UPDATE downloads SET saved_path=? WHERE id=?', (new_path, sid))
    return {'moved': len(fixes), 'missing': len(missing), 'applied': apply}


def migrate(db):
    if meta_get(db, 'migrated_v1'):
        return
    with db:
        for row in db.execute('SELECT * FROM downloads ORDER BY ts'):
            key = canonical(row['url'])
            existing = db.execute('SELECT * FROM assets WHERE key=?', (key,)).fetchone()
            state = 'complete' if Path(row['saved_path']).is_file() else 'missing'
            if existing is None or (existing['state'] == 'missing' and state == 'complete'):
                db.execute('''INSERT OR REPLACE INTO assets
                  (key,url,targets,source,creator,title,saved_path,state,acquired)
                  VALUES (?,?,?,?,?,?,?,?,?)''',
                  (key, key, json.dumps([['soundgasm', key]]), row['source'], row['creator'],
                   row['title'], row['saved_path'], state, row['ts']))
            db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)', (row['id'], key))
            db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?)', (key, key))
        meta_set(db, 'migrated_v1', True)


def lookup(db, alias):
    return db.execute('SELECT a.* FROM aliases b JOIN assets a ON a.key=b.asset_key WHERE b.id=?', (alias,)).fetchone()


def enqueue(db, sid, source, creator, title, options, published=0):
    existing = lookup(db, sid)
    for _, url in options:
        if existing:
            break
        existing = lookup(db, canonical(url))
    if existing:
        if existing['state'] == 'complete' and not Path(existing['saved_path']).is_file():
            db.execute("UPDATE assets SET state='missing',error='requires_reconciliation' WHERE key=?", (existing['key'],))
            result = 'missing'
        else:
            result = 'known_' + existing['state']
        db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (sid, existing['key']))
        return result
    key = canonical(options[0][1])
    db.execute('''INSERT INTO assets (key,url,targets,source,creator,title,published,state)
                VALUES (?,?,?,?,?,?,?,'pending')''',
               (key, key, json.dumps(options), source, creator, title, int(published or 0)))
    db.executemany('INSERT INTO aliases VALUES (?,?)', [(sid, key), (key, key)])
    return 'queued'


class SoundgasmParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.current = None
        self.div_depth = 0
        self.anchor = False
        self.description = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'div':
            if self.current is not None:
                self.div_depth += 1
            elif 'sound-details' in a.get('class', '').split():
                self.current = {'url': '', 'title': '', 'description': ''}
                self.div_depth = 1
        if self.current is None:
            return
        if tag == 'a' and not self.current['url']:
            self.current['url'] = a.get('href', '')
            self.anchor = True
        if tag == 'span' and 'soundDescription' in a.get('class', '').split():
            self.description = True
    def handle_data(self, data):
        if self.current is not None:
            if self.anchor:
                self.current['title'] += data
            if self.description:
                self.current['description'] += data
    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == 'a':
            self.anchor = False
        if tag == 'span':
            self.description = False
        if tag == 'div':
            self.div_depth -= 1
            if not self.div_depth:
                self.items.append(self.current)
                self.current = None


def sg_listing(http, creator):
    with http.get('https://soundgasm.net/u/' + quote(creator, safe='')) as r:
        text = r.text
    parser = SoundgasmParser()
    parser.feed(text)
    if parser.current is not None or ('sound-details' in text and not parser.items):
        raise SourceError('unrecognized_soundgasm_listing', 'parse_failure')
    if not parser.items and 'Soundgasm.net Logo' not in text:
        raise SourceError('unrecognized_soundgasm_page', 'parse_failure')
    if any(not x['url'] or not x['title'].strip() for x in parser.items):
        raise SourceError('malformed_soundgasm_entry', 'parse_failure')
    return parser.items


class Reddit:
    def __init__(self, http, credentials):
        self.http, self.credentials = http, credentials
        self.token = None
    def authenticate(self):
        c = self.credentials
        if not c.get('client_id') or not c.get('client_secret'):
            raise SourceError('oauth_credentials_missing')
        data = {'grant_type': 'client_credentials'}
        if c.get('refresh_token'):
            data = {'grant_type': 'refresh_token', 'refresh_token': c['refresh_token']}
        with self.http.request('POST', 'https://www.reddit.com/api/v1/access_token',
                               auth=(c['client_id'], c['client_secret']), data=data) as r:
            body = r.json()
        self.token = body.get('access_token')
        if not self.token:
            raise SourceError('oauth_token_missing')
    def page(self, sub, after=None):
        if not self.token:
            self.authenticate()
        params = {'limit': 100, 'raw_json': 1, 'include_over_18': 'on'}
        if after:
            params['after'] = after
        for attempt in range(2):
            try:
                with self.http.get('https://oauth.reddit.com/r/' + quote(sub, safe='') + '/new.json',
                                   headers={'Authorization': 'Bearer ' + self.token}, params=params) as r:
                    body = r.json()
                break
            except SourceError as e:
                if str(e) == 'http_401' and attempt == 0:
                    self.authenticate()
                else:
                    raise
        data = body.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('children'), list):
            raise SourceError('malformed_reddit_listing', 'parse_failure')
        posts = [x.get('data') for x in data['children'] if x.get('kind') == 't3']
        if any(not isinstance(p, dict) or not p.get('id') for p in posts):
            raise SourceError('malformed_reddit_post', 'parse_failure')
        return posts, data.get('after')


def discover(db, cfg, secrets, http, selected, inspect=False):
    reports = []
    allow = {a.casefold() for a in cfg.get('allowlist', [])}
    def accept(report, sid, creator, title, options, published=0, haystack=None,
               trusted=False, topic_pattern=None):
        creator = cfg.get('_identity_creators', {}).get(report['name'].split(':')[0]+':'+creator.casefold(), cfg.get('creator_aliases', {}).get(creator, creator))
        rules = cfg.get('_profile_rules', lambda creator: cfg)(creator)
        ok, reason = title_passes(haystack or title, rules, trusted, topic_pattern)
        if not ok:
            report['rejected'][reason] += 1
            return
        report['accepted'] += 1
        if not options:
            report['rejected']['no_supported_target'] += 1
            return
        result = enqueue(db, sid, report['name'], creator, title, options, published)
        report['items'][result] += 1
    def report(name):
        return {'name': name, 'status': 'healthy', 'parsed': 0, 'pages': 0,
                'accepted': 0, 'items': collections.Counter(), 'rejected': collections.Counter()}
    if 'reddit' in selected:
        reddit = Reddit(http, secrets.get('reddit', {}))
        auth_failure = None
        for sub in cfg.get('subreddits', []):
            stats = report('reddit:' + sub)
            try:
                if auth_failure:
                    raise SourceError(auth_failure)
                checkpoint_key = 'reddit_checkpoint:' + sub
                checkpoint = meta_get(db, checkpoint_key, {})
                # Always check newest posts; a separate continuation catches backlog.
                recent, recent_after = reddit.page(sub)
                stats['pages'] += 1
                batches = [recent]
                boundary = checkpoint.get('watermark', 0)
                after = checkpoint.get('after') or recent_after
                newest = max([int(p.get('created_utc', 0)) for p in recent] + [checkpoint.get('newest', 0)])
                reached = not after or (not checkpoint.get('after') and any(int(p.get('created_utc', 0)) <= boundary for p in recent))
                while not reached and stats['pages'] < cfg.get('reddit_max_pages', 5):
                    posts, after = reddit.page(sub, after)
                    stats['pages'] += 1
                    batches.append(posts)
                    reached = not after or not posts or any(int(p.get('created_utc', 0)) <= boundary for p in posts)
                for posts in batches:
                    for p in posts:
                        stats['parsed'] += 1
                        author = p.get('author', '')
                        if allow and author.casefold() not in allow:
                            stats['rejected']['creator_not_allowed'] += 1
                            continue
                        accept(stats, 'reddit:' + p['id'], author, p.get('title', ''), targets(p, cfg), p.get('created_utc', 0))
                meta_set(db, checkpoint_key, {'watermark': newest if reached else boundary,
                                             'after': None if reached else after, 'newest': newest})
                stats['backlog_pending'] = not reached
                if stats['parsed'] == 0:
                    stats['status'] = 'empty'
            except (SourceError, requests.RequestException, ValueError, KeyError) as e:
                stats['status'] = getattr(e, 'status', 'parse_failure')
                stats['error'] = str(e) if isinstance(e, (SourceError, MediaError)) else type(e).__name__
                if stats['error'] in {'http_401', 'http_403', 'oauth_credentials_missing', 'oauth_token_missing'}:
                    auth_failure = stats['error']
            reports.append(stats)
    if 'soundgasm' in selected:
        for creator in cfg.get('soundgasm_creators', []):
            stats = report('soundgasm:' + creator)
            try:
                posts = sg_listing(http, creator)
                stats['pages'] = 1
                stats['parsed'] = len(posts)
                if not posts:
                    stats['status'] = 'empty'
                    stats['note'] = 'No public recordings returned; profile identity requires verification.'
                for p in posts:
                    url = canonical(p['url'])
                    if urlparse(url).hostname != 'soundgasm.net':
                        raise SourceError('unexpected_listing_host', 'parse_failure')
                    sid = 'sg:' + hashlib.sha1(p['url'].encode()).hexdigest()[:16]
                    accept(stats, sid, creator, plain(p['title']).strip(), [['soundgasm', url]],
                           haystack=p['title'] + '\n' + p['description'])
            except (SourceError, requests.RequestException, ValueError) as e:
                stats['status'] = getattr(e, 'status', 'parse_failure')
                stats['error'] = str(e) if isinstance(e, (SourceError, MediaError)) else type(e).__name__
            reports.append(stats)
    if 'youtube' in selected and cfg.get('youtube_enabled', False):
        for channel in cfg.get('youtube_channels', []):
            stats = report('youtube:' + channel['channel_id'])
            stats['creator'] = channel['creator']
            try:
                rules = cfg.get('_profile_rules', lambda creator: cfg)(channel['creator'])
                rules = acquisition_profiles.discovery_config(cfg, rules)
                checkpoint_key = 'youtube_seen:' + channel['channel_id']
                previous = meta_get(db, checkpoint_key)
                entries = youtube.listing(channel, cfg)
                stats['pages'] += 1
                seen = set(previous or [])
                if seen and entries and not any(e['id'] in seen for e in entries):
                    entries = youtube.listing(channel, cfg, cfg.get('youtube_catchup_limit', 500))
                    stats['pages'] += 1
                    if not any(e['id'] in seen for e in entries):
                        raise youtube.YouTubeError('youtube_listing_gap_requires_review')
                seeded = 0
                deferred = set()
                for entry in entries:
                    stats['parsed'] += 1
                    ok, reason = youtube.eligibility(entry, rules)
                    # Early-access uploads and scheduled premieres can become
                    # public later. Recheck their eligibility on the next poll.
                    if not ok and reason in {'youtube_not_public', 'youtube_live_or_upcoming'}:
                        stats['rejected'][reason] += 1
                        deferred.add(entry['id'])
                        continue
                    if entry['id'] in seen:
                        stats['items']['previously_seen'] += 1
                        continue
                    if ok:
                        ok, reason = title_passes(entry['title'], rules, trusted=True,
                                                 topic_pattern=youtube.TOPIC_PATTERN)
                    if not ok:
                        stats['rejected'][reason] += 1
                        continue
                    if previous is None and seeded >= rules.get('youtube_initial_downloads', 3):
                        stats['items']['older_backlog_not_imported'] += 1
                        continue
                    url = 'https://www.youtube.com/watch?v=' + entry['id']
                    accept(stats, 'youtube:' + entry['id'], channel['creator'], entry['title'],
                           [['youtube', url]], youtube.published(entry), trusted=True,
                           topic_pattern=youtube.TOPIC_PATTERN)
                    seeded += 1
                if entries:
                    updated_seen = list(dict.fromkeys(
                        [e['id'] for e in entries if e['id'] not in deferred] +
                        [vid for vid in (previous or []) if vid not in deferred]))
                    meta_set(db, checkpoint_key, updated_seen[:1000])
                else:
                    stats['status'] = 'empty'
            except (youtube.YouTubeError, ValueError, KeyError) as e:
                stats['status'] = 'unavailable'
                stats['error'] = str(e) if isinstance(e, youtube.YouTubeError) else type(e).__name__
            reports.append(stats)
    if 'sfw' in selected:
        reports.extend(sfw_sources.discover(db, cfg, secrets, http, sys.modules[__name__]))
    now = int(time.time())
    with db:
        for stats in reports:
            previous = db.execute('SELECT * FROM sources WHERE name=?', (stats['name'],)).fetchone()
            success = now if stats['status'] in {'healthy', 'empty'} else (previous['last_success'] if previous else None)
            db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)',
                       (stats['name'], stats['status'], now, success,
                        previous['last_download'] if previous else None, json.dumps(stats)))
    return reports


def tag_audio(path, creator, title, published=0):
    notes = 'Original title: ' + title
    versions = compact_title(title)[1]
    if versions:
        notes += '\nVersion: ' + ', '.join(versions)
    from mutagen.wave import WAVE
    from mutagen.aac import AAC
    raw = mutagen.File(str(path))
    if isinstance(raw,AAC):return
    if isinstance(raw,WAVE):
        from mutagen.id3 import TPE1,TPE2,TALB,TIT2,TCON,TDRC,COMM,TXXX
        if raw.tags is None:raw.add_tags()
        for frame,value in [(TPE1,creator),(TPE2,creator),(TALB,'Singles'),
                            (TIT2,compact_title(title)[0]),(TCON,'ASMR')]:
            raw.tags.add(frame(encoding=3,text=[value]))
        if published:raw.tags.add(TDRC(encoding=3,text=[dt.datetime.fromtimestamp(published,dt.timezone.utc).strftime('%Y-%m-%d')]))
        raw.tags.add(COMM(encoding=3,lang='eng',desc='ASMR source',text=notes))
        raw.tags.add(TXXX(encoding=3,desc='ORIGINALTITLE',text=title))
        raw.tags.add(TXXX(encoding=3,desc='VERSION',text=', '.join(versions)))
        raw.save();return
    if raw is None:
        # Raw ADTS AAC has no writable metadata container. Preserve its bytes
        # instead of rejecting valid audio or silently remuxing/transcoding it.
        # Creator/title provenance remains in the database and naming layout.
        try:AAC(str(path))
        except mutagen.MutagenError:raise MediaError('unsupported_tag_format') from None
        return
    f = mutagen.File(str(path), easy=True)
    if f is None:
        raise MediaError('unsupported_tag_format')
    for key, value in [('artist', creator), ('albumartist', creator), ('album', 'Singles'),
                       ('title', compact_title(title)[0]), ('genre', 'ASMR')]:
        f[key] = [value]
    if published:
        f['date'] = [dt.datetime.fromtimestamp(published, dt.timezone.utc).strftime('%Y-%m-%d')]
    f.save()
    raw = mutagen.File(str(path))
    from mutagen.mp4 import MP4
    from mutagen.mp3 import MP3
    if isinstance(raw, MP4):
        raw['\xa9cmt'] = [notes]
        raw['\xa9grp'] = versions or ['ASMR']
    elif isinstance(raw, MP3):
        from mutagen.id3 import COMM, TXXX
        raw.tags.add(COMM(encoding=3, lang='eng', desc='ASMR source', text=notes))
        raw.tags.add(TXXX(encoding=3, desc='ORIGINALTITLE', text=title))
        raw.tags.add(TXXX(encoding=3, desc='VERSION', text=', '.join(versions)))
    else:
        raw['comment'] = [notes]
    raw.save()


def target_audio(http, kind, url):
    if kind == 'soundgasm':
        with http.get(url) as r:
            match = re.search(r'(?:m4a|mp3):\s*[\'"]([^\'"]+)', r.text)
        if not match:
            raise SourceError('audio_url_missing', 'parse_failure')
        return html.unescape(match[1])
    if kind == 'reddit_media':
        if urlparse(url).hostname == 'v.redd.it' and not Path(urlparse(url).path).suffix:
            raise SourceError('reddit_video_requires_audio_extraction', 'parse_failure')
        return url
    raise SourceError('unsupported_target', 'parse_failure')


def save_asset(asset, cfg, http):
    root = Path(cfg['output_root']).resolve()
    folder = root / safe_filename(asset['creator'], 100) / 'Singles'
    if not folder.resolve().is_relative_to(root):
        raise MediaError('output_path_outside_library')
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(asset['key'].encode()).hexdigest()[:16]
    base = safe_filename(compact_title(asset['title'])[0], 110) + ' [' + digest + ']'
    def destination(ext):
        template = cfg.get('naming_template', '{Creator}/Singles/{Title} [{SourceId}].{ext}')
        values = {'Creator': safe_filename(asset['creator'], 100),
                  'Title': safe_filename(compact_title(asset['title'])[0], 110), 'SourceId': digest, 'ext': ext.lstrip('.')}
        if set(re.findall(r'\{([^{}]+)\}', template)) - set(values):
            raise MediaError('unknown_naming_token')
        for key, value in values.items(): template = template.replace('{' + key + '}', value)
        path = root / template
        if not path.resolve().is_relative_to(root) or path.resolve() == root:
            raise MediaError('naming_path_outside_library')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    last_error = None
    for kind, url in json.loads(asset['targets']):
        tmp = None
        try:
            if kind == 'youtube':
                if not cfg.get('youtube_enabled', False):
                    raise MediaError('youtube_disabled')
                out = destination('.m4a')
                if out.exists():
                    data = validate_audio(out)
                    if any(s.get('codec_type') == 'video' for s in data['streams']):
                        raise MediaError('youtube_output_contains_video')
                    return out, url
                info, channel = youtube.metadata(url, cfg)
                ok, reason = title_passes(info['title'], cfg, trusted=True,
                                         topic_pattern=youtube.TOPIC_PATTERN)
                if not ok:
                    raise MediaError(reason)
                if channel['creator'] != asset['creator']:
                    raise MediaError('youtube_creator_mismatch')
                asset = dict(asset, title=info['title'], published=youtube.published(info))
                # Download/convert on local storage; publish only finished audio to Ceph.
                with tempfile.TemporaryDirectory(prefix='asmr-youtube-') as work:
                    audio = youtube.download(info, work, cfg)
                    data = validate_audio(audio)
                    if any(s.get('codec_type') == 'video' for s in data['streams']):
                        raise MediaError('youtube_output_contains_video')
                    tag_audio(audio, asset['creator'], asset['title'], asset['published'])
                    tmp = out.with_name(out.name + '.part')
                    with audio.open('rb') as src, tmp.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
            else:
                media = target_audio(http, kind, url)
                ext = Path(urlparse(media).path).suffix.lower()
                if ext not in AUDIO_EXTS:
                    raise MediaError('unsupported_media_extension')
                if ext not in cfg.get('allowed_formats', AUDIO_EXTS):
                    raise MediaError('profile_format_rejected')
                out = destination(ext)
                tmp = out.with_name(out.name + '.part')
                # A crash after atomic publication but before commit is recoverable.
                if out.exists():
                    validate_audio(out)
                    return out, url
                with http.get(media, stream=True) as r, tmp.open('wb') as f:
                    size = 0
                    start = time.monotonic()
                    for chunk in r.iter_content(65536):
                        size += len(chunk)
                        if size > cfg.get('max_file_bytes', 2147483648) or time.monotonic() - start > 900:
                            raise MediaError('download_limit_exceeded')
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            validation = validate_audio(tmp)
            if any(s.get('codec_type') == 'video' for s in validation.get('streams', [])):
                raise MediaError('output_contains_video')
            if float(validation['format']['duration']) < cfg.get('minimum_duration', 0):
                raise MediaError('profile_duration_rejected')
            tag_audio(tmp, asset['creator'], asset['title'], asset['published'])
            validate_audio(tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, out)
            return out, url
        except (SourceError, requests.RequestException, OSError, ValueError, subprocess.SubprocessError, mutagen.MutagenError) as e:
            last_error = str(e) if isinstance(e, (SourceError, MediaError, youtube.YouTubeError)) else type(e).__name__
            if tmp is not None and tmp.exists() and tmp.resolve().is_relative_to(folder.resolve()):
                tmp.unlink()
    raise SourceError(last_error or 'no_targets')


def download_pending(db, cfg, http, limit):
    saved, failed = [], []
    now = int(time.time())
    rows = db.execute("SELECT * FROM assets WHERE state IN ('pending','failed') AND retry_after<=? ORDER BY COALESCE(published,0) DESC,key LIMIT ?", (now, limit)).fetchall()
    for asset in rows:
        try:
            out, actual_url = save_asset(asset, cfg, http)
            with db:
                db.execute("UPDATE assets SET saved_path=?,state='complete',acquired=?,error=NULL,retry_after=0 WHERE key=?", (str(out), now, asset['key']))
                db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (canonical(actual_url), asset['key']))
                sid = 'asset:' + hashlib.sha256(asset['key'].encode()).hexdigest()
                db.execute('INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?,?,?)',
                           (sid, asset['source'], actual_url, asset['creator'], asset['title'], str(out), now))
                meta_set(db, 'plex_pending', True)
                pending_paths = set(meta_get(db, 'plex_pending_paths', []))
                pending_paths.add(str(out))
                meta_set(db, 'plex_pending_paths', sorted(pending_paths))
                db.execute('UPDATE sources SET last_download=? WHERE name=?', (now, asset['source']))
            saved.append(str(out))
            logging.info('saved asset=%s source=%s', sid, asset['source'])
        except RuntimeError:
            raise
        except Exception as e:
            reason = str(e) if isinstance(e, (SourceError, MediaError)) else type(e).__name__
            attempts = asset['attempts'] + 1
            with db:
                db.execute("UPDATE assets SET state='failed',attempts=?,retry_after=?,error=? WHERE key=?",
                           (attempts, now + min(86400, 300 * 2 ** min(attempts, 8)), reason, asset['key']))
            failed.append({'key': asset['key'], 'error': reason})
    return saved, failed


def plex_refresh(db, cfg, secrets, http):
    if not meta_get(db, 'plex_pending', False):
        return 'not_needed'
    plex = cfg.get('plex', {})
    token = secrets.get('plex_token') or plex.get('token')
    if not plex.get('url') or not token or plex.get('section_id') is None:
        return 'credentials_missing'
    base = plex['url'].rstrip('/')
    section = str(plex['section_id'])
    headers = {'X-Plex-Token': token}
    deadline = time.monotonic() + cfg.get('plex_scan_wait_seconds', 60)
    requested = False
    while True:
        paths = meta_get(db, 'plex_pending_paths', [])
        try:
            if paths:
                with http.get(base + '/library/sections/' + section + '/all',
                              headers=headers, params={'type': 10}) as r:
                    indexed = {p.get('file') for p in ET.fromstring(r.text).findall('.//Part')}
                remaining = sorted(set(paths) - indexed)
                with db:
                    meta_set(db, 'plex_pending_paths', remaining)
                    if not remaining:
                        meta_set(db, 'plex_pending', False)
                        meta_set(db, 'plex_last_indexed', int(time.time()))
                        return 'indexed'
            with http.get(base + '/library/sections', headers=headers) as r:
                directory = next((d for d in ET.fromstring(r.text).findall('Directory')
                                  if d.get('key') == section), None)
            if directory is None:
                return 'library_section_missing'
            scanning = directory.get('refreshing') == '1'
            if not scanning and not requested:
                affected = sorted({str(Path(p).parent) for p in remaining}) if paths else []
                if not affected:
                    return 'pending_paths_required'
                for folder in affected:
                    with http.get(base + '/library/sections/' + section + '/refresh',
                                  headers=headers, params={'path': folder}):
                        pass
                requested = True
                with db:
                    meta_set(db, 'plex_last_refresh', int(time.time()))
            elif not scanning and requested and not paths:
                # Compatibility for a pending scan created before path tracking existed.
                with db:
                    meta_set(db, 'plex_pending', False)
                return 'indexed'
        except SourceError as e:
            return str(e)
        except ET.ParseError:
            return 'catalog_parse_failure'
        if time.monotonic() >= deadline:
            return 'pending_indexing'
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def require_mount(cfg):
    root = Path(cfg['output_root'])
    r = subprocess.run(['findmnt', '-T', str(root), '-n', '-o', 'FSTYPE,TARGET'], capture_output=True,text=True)
    parts = r.stdout.strip().split()
    if r.returncode or len(parts) != 2 or parts[0] != cfg.get('required_fstype', 'ceph'):
        raise RuntimeError('required_media_filesystem_not_mounted')
    if not root.is_dir() or not root.resolve().is_relative_to(Path(parts[1]).resolve()):
        raise RuntimeError('invalid_media_root')
    if shutil.disk_usage(root).free < cfg.get('min_free_bytes', 5 * 1024 ** 3):
        raise RuntimeError('insufficient_media_space')


def status_report(db):
    now = int(time.time())
    latest = db.execute('SELECT max(acquired) FROM assets WHERE state="complete"').fetchone()[0]
    return {'sources': [dict(row) | {'details': json.loads(row['details'])} for row in db.execute('SELECT * FROM sources ORDER BY name')],
            'assets': dict(db.execute('SELECT state,count(*) FROM assets GROUP BY state')),
            'plex_pending': meta_get(db, 'plex_pending', False), 'latest_download': latest,
            'playlists': meta_get(db, 'playlist_status', {'status': 'not_run'}),
            'stale_sources': [row['name'] for row in db.execute('SELECT name,last_success FROM sources') if not row['last_success'] or now-row['last_success']>172800],
            'days_since_download': round((now - latest) / 86400, 1) if latest else None,
            'last_run': dict(db.execute('SELECT * FROM runs ORDER BY started DESC LIMIT 1').fetchone() or {})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default='/etc/asmr-scraper/config.yaml')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--inspect', action='store_true')
    mode.add_argument('--reconcile', action='store_true')
    mode.add_argument('--status', action='store_true')
    mode.add_argument('--playlists-only', action='store_true', help='Sync categories and playlists without source polling or downloads')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--sources', default='reddit,soundgasm,youtube,sfw')
    ap.add_argument('--max-downloads', type=int)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = yaml.safe_load(Path(args.config).read_text())
    credential_dir = os.environ.get('CREDENTIALS_DIRECTORY')
    secrets_path = Path(credential_dir) / 'secrets.json' if credential_dir else Path(cfg['secrets_file'])
    secrets = json.loads(secrets_path.read_text())
    if args.status:
        db = open_db(cfg['state_db'], readonly=True)
        print(json.dumps(status_report(db), indent=2))
        return 0
    require_mount(cfg)
    with open(cfg.get('lock_file', '/run/asmr-scraper/lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.error('another_run_is_active')
            return 3
        if args.reconcile:
            db = open_db(cfg['state_db'], readonly=not args.apply)
            print(json.dumps(reconcile(db, cfg['output_root'], args.apply)))
            return 0
        if args.inspect:
            original = open_db(cfg['state_db'], readonly=True)
            db = sqlite3.connect(':memory:')
            original.backup(db)
            db.row_factory = sqlite3.Row
            original.close()
            ensure_schema(db)
        else:
            db = open_db(cfg['state_db'])
        migrate(db)
        started = int(time.time())
        http = HTTP(cfg.get('request_interval', 1.1))
        reports = [] if args.playlists_only else discover(db, cfg, secrets, http, args.sources.split(','), args.inspect)
        saved, failed = ([], []) if args.inspect or args.playlists_only else download_pending(db, cfg, http,
            args.max_downloads if args.max_downloads is not None else cfg.get('max_downloads_per_run', 25))
        plex = 'inspection' if args.inspect else plex_refresh(db, cfg, secrets, http)
        playlists = plex_playlists.sync(db, cfg, secrets, sys.modules[__name__], dry_run=args.inspect)
        missing = db.execute("SELECT count(*) FROM assets WHERE state='missing'").fetchone()[0]
        failure_count = db.execute("SELECT count(*) FROM assets WHERE state='failed'").fetchone()[0]
        degraded = any(r['status'] in BAD_STATES for r in reports) or bool(failed) or missing or failure_count or plex not in {'not_needed', 'indexed', 'inspection'} or playlists['status'] == 'failed'
        summary = {'status': 'degraded' if degraded else 'healthy', 'inspection': args.inspect,
                   'sources': reports, 'saved': len(saved), 'download_failures': failed,
                   'plex': plex, 'missing': missing, 'failed_assets': failure_count,
                   'playlists': playlists,
                   'queued': db.execute("SELECT count(*) FROM assets WHERE state='pending'").fetchone()[0],
                   'elapsed_seconds': round(time.time() - started, 2)}
        if not args.inspect:
            with db:
                db.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?)',
                           (started, int(time.time()), summary['status'], json.dumps(summary)))
            status_path = Path(cfg.get('status_file', '/var/lib/asmr-scraper/status.json'))
            temp = status_path.with_suffix('.tmp')
            temp.write_text(json.dumps(status_report(db), indent=2))
            os.replace(temp, status_path)
        print(json.dumps(summary, indent=2))
        return 2 if degraded else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        logging.error('fatal: %s', str(exc) if isinstance(exc, (RuntimeError, SourceError)) else type(exc).__name__)
        sys.exit(1)
