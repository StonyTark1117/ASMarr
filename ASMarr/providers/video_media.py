"""Visual-ASMR discovery, ranking, acquisition and atomic import support."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse, urlunparse

import requests


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.mov', '.m4v'}
ARTWORK_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
RESOLUTIONS = {'Any': None, '2160p': 2160, '1440p': 1440, '1080p': 1080,
               '720p': 720, '480p': 480}
COMPATIBLE_VIDEO = {'h264', 'hevc', 'av1', 'vp9'}
COMPATIBLE_AUDIO = {'aac', 'ac3', 'eac3', 'mp3', 'opus', 'vorbis', 'flac'}


def canonical_url(url):
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}:
        media_id = p.path.strip('/') if host == 'youtu.be' else parse_qs(p.query).get('v', [''])[0]
        if not media_id and p.path.startswith('/shorts/'):
            media_id = p.path.split('/')[2]
        if media_id:
            return 'https://www.youtube.com/watch?v=' + media_id
    path = re.sub(r'/DASH_[^/]+\.mp4$', '', p.path.rstrip('/'))
    return urlunparse((p.scheme.lower() or 'https', p.netloc.lower(), path, '', '', ''))


def provider_identity(provider, url, post=None):
    normalized = canonical_url(url)
    if provider == 'youtube':
        return parse_qs(urlparse(normalized).query).get('v', [normalized])[0], normalized
    if provider == 'reddit':
        post = post or {}
        crossposts = post.get('crosspost_parent_list') or []
        original = crossposts[0] if crossposts else post
        media = (original.get('secure_media') or original.get('media') or {}).get('reddit_video') or {}
        fallback = media.get('fallback_url') or normalized
        media_id = (original.get('id') or urlparse(fallback).path.strip('/').split('/')[0]
                    or hashlib.sha256(normalized.encode()).hexdigest()[:24])
        return media_id, canonical_url(fallback)
    return hashlib.sha256(normalized.encode()).hexdigest()[:24], normalized


def fingerprint(provider_id, normalized_url, details=None):
    details = details or {}
    stable = details.get('duration') or details.get('filesize') or ''
    return hashlib.sha256(f'{provider_id}|{normalized_url}|{stable}'.encode()).hexdigest()


def profile_limit(profile):
    target = profile.get('resolution', 'Any') if isinstance(profile, dict) else str(profile)
    if target not in RESOLUTIONS:
        raise ValueError('unknown_video_resolution')
    return RESOLUTIONS[target]


def rank_candidate(candidate, profile):
    height = int(candidate.get('resolution') or candidate.get('height') or 0)
    limit = profile_limit(profile)
    # YouTube and Reddit manifests are adaptive: a 2160p upload can still
    # supply a 1080p stream without transcoding. Fixed releases (for example
    # an interactive indexer result) must be rejected when they exceed the cap.
    adaptive = candidate.get('provider') in {'youtube', 'reddit'}
    if limit is not None and height > limit and not adaptive:
        return None
    source = int(candidate.get('source_quality') or candidate.get('sourceQuality') or 0)
    bitrate = int(candidate.get('bitrate') or candidate.get('tbr') or 0)
    video_codec = (candidate.get('video_codec') or candidate.get('vcodec') or '').split('.')[0].lower()
    audio_codec = (candidate.get('audio_codec') or candidate.get('acodec') or '').split('.')[0].lower()
    compatibility = int(video_codec in COMPATIBLE_VIDEO) + int(not audio_codec or audio_codec in COMPATIBLE_AUDIO)
    no_reencode = int(not candidate.get('requires_transcode') and not candidate.get('requiresTranscode'))
    score = (min(height, limit) if limit is not None and height else height,
             source, bitrate, compatibility, no_reencode)
    return dict(candidate, rank=score)


def choose_candidate(candidates, profile):
    ranked = [rank_candidate(c, profile) for c in candidates]
    ranked = [c for c in ranked if c is not None]
    return max(ranked, key=lambda c: c['rank']) if ranked else None


def probe(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError('empty_or_missing_video')
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration,format_name:stream=codec_type,codec_name,width,height,bit_rate',
                        '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise ValueError('invalid_video')
    data = json.loads(p.stdout)
    if not any(s.get('codec_type') == 'video' for s in data.get('streams', [])):
        raise ValueError('no_video_stream')
    if float(data.get('format', {}).get('duration') or 0) <= 0:
        raise ValueError('invalid_duration')
    return data


def paths_overlap(first, second):
    a, b = Path(first).resolve(), Path(second).resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def validate_roots(audio_root, video_root):
    if not Path(audio_root).is_absolute() or not Path(video_root).is_absolute():
        raise ValueError('media_roots_must_be_absolute')
    if paths_overlap(audio_root, video_root):
        raise ValueError('audio_and_video_roots_overlap')
    return {'audioRoot': str(Path(audio_root)), 'videoRoot': str(Path(video_root))}


def _safe(value, limit=120):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip(' .')[:limit] or 'Untitled'


def video_name(asset, candidate, extension, root):
    published = int(asset['published'] or 0)
    date = dt.datetime.fromtimestamp(published, dt.timezone.utc) if published else dt.datetime.now(dt.timezone.utc)
    provider = _safe(candidate.get('provider') or 'source', 24)
    source_id = _safe(candidate.get('provider_id') or hashlib.sha256(asset['key'].encode()).hexdigest()[:16], 64)
    base = f'{date:%Y-%m-%d} - {_safe(asset["title"])} [{provider}-{source_id}]{extension.lower()}'
    destination = Path(root).resolve() / _safe(asset['creator'], 100) / f'{date:%Y}' / base
    if not destination.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('video_path_outside_library')
    return destination


def ensure_asset_rows(db, recording_key, *, video_wanted=False, source_url=None, provider_id=None):
    row = db.execute('SELECT state,saved_path,attempts,retry_after,error,acquired FROM assets WHERE key=?',
                     (recording_key,)).fetchone()
    if not row:
        raise ValueError('recording_not_found')
    audio_state = 'wanted' if row['state'] == 'pending' else row['state']
    db.execute('''INSERT OR IGNORE INTO media_assets
                  (recording_key,media_kind,wanted,state,saved_path,attempts,retry_after,error,acquired)
                  VALUES (?,'Audio',?,?,?,?,?,?,?)''',
               (recording_key, int(row['state'] != 'suppressed'), audio_state, row['saved_path'],
                row['attempts'], row['retry_after'], row['error'], row['acquired']))
    if video_wanted:
        db.execute('''INSERT INTO media_assets(recording_key,media_kind,wanted,state,source_url,provider_id)
                      VALUES (?,'Video',1,'wanted',?,?)
                      ON CONFLICT(recording_key,media_kind) DO UPDATE SET
                      wanted=CASE WHEN media_assets.state='imported' THEN media_assets.wanted ELSE 1 END,
                      state=CASE WHEN media_assets.state IN ('imported','downloading') THEN media_assets.state ELSE 'wanted' END,
                      source_url=COALESCE(excluded.source_url,media_assets.source_url),
                      provider_id=COALESCE(excluded.provider_id,media_assets.provider_id),error=NULL''',
                   (recording_key, source_url, provider_id))


def add_candidate(db, recording_key, provider, url, post=None, details=None, interactive=False):
    provider_id, normalized = provider_identity(provider, url, post)
    details = details or {}
    fp = fingerprint(provider_id, normalized, details)
    duplicate = db.execute('''SELECT recording_key FROM video_candidates
                              WHERE (provider=? AND provider_id=?) OR normalized_url=? OR fingerprint=? LIMIT 1''',
                           (provider, provider_id, normalized, fp)).fetchone()
    if duplicate:
        # Cross-posts and alternate YouTube URLs point at the original recording.
        return duplicate['recording_key'], False
    db.execute('''INSERT INTO video_candidates(recording_key,provider,provider_id,url,normalized_url,fingerprint,
                  resolution,source_quality,bitrate,video_codec,audio_codec,container,requires_transcode,interactive_only,details)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(provider,provider_id) DO UPDATE SET details=excluded.details''',
               (recording_key, provider, provider_id, url, normalized, fp,
                details.get('height'), details.get('quality') or 0, details.get('tbr') or 0,
                details.get('vcodec'), details.get('acodec'), details.get('ext'),
                int(details.get('requires_transcode', False)), int(interactive), json.dumps(details)))
    ensure_asset_rows(db, recording_key, video_wanted=not interactive,
                      source_url=normalized, provider_id=provider_id)
    return recording_key, True


def _recording(db, core, sid, source, creator, title, url, published):
    existing = core.lookup(db, sid) or core.lookup(db, core.canonical(url))
    if existing:
        return existing['key']
    core.enqueue(db, sid, source, creator, title, [['youtube' if 'youtu' in url else 'reddit_media', url]], published)
    return core.lookup(db, sid)['key']


def backfill(db, cfg, secrets, args, core):
    creator_id = int(args['creatorId'])
    creator = db.execute('SELECT * FROM creators WHERE id=?', (creator_id,)).fetchone()
    if not creator:
        raise ValueError('creator_not_found')
    job = db.execute("SELECT * FROM backfill_jobs WHERE creator_id=? AND media_kind='Video' ORDER BY started DESC LIMIT 1",
                     (creator_id,)).fetchone()
    job_id = job['id'] if job and job['state'] in {'queued', 'running', 'interrupted', 'failed'} else hashlib.sha256(f'{creator_id}:Video'.encode()).hexdigest()[:24]
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    db.execute('''INSERT INTO backfill_jobs(id,creator_id,media_kind,state,started,updated)
                  VALUES (?,?,'Video','running',?,?) ON CONFLICT(id) DO UPDATE SET state='running',updated=excluded.updated,error=NULL''',
               (job_id, creator_id, now, now)); db.commit()
    discovered = eligible = total = 0
    try:
        # First convert all already-known provider links without repeating discovery.
        for asset in db.execute('SELECT * FROM assets WHERE creator=? ORDER BY published', (creator['name'],)).fetchall():
            if not db.execute('SELECT monitor_video FROM creators WHERE id=?', (creator_id,)).fetchone()[0]:
                raise InterruptedError('video_monitoring_disabled')
            for kind, url in json.loads(asset['targets'] or '[]'):
                provider = 'youtube' if kind == 'youtube' else 'reddit' if urlparse(url).hostname in {'v.redd.it'} else None
                if provider:
                    discovered += 1
                    _, added = add_candidate(db, asset['key'], provider, url)
                    eligible += int(added)
            total += 1
        identities = db.execute('SELECT kind,handle FROM identities WHERE creator_id=? AND enabled=1', (creator_id,)).fetchall()
        channels = [r['handle'] for r in identities if r['kind'] == 'youtube']
        for channel_id in channels:
            channel = {'creator': creator['name'], 'channel_id': channel_id}
            entries = core.youtube.listing(channel, cfg, cfg.get('video_history_limit', 10000))
            for entry in entries:
                if not db.execute('SELECT monitor_video FROM creators WHERE id=?', (creator_id,)).fetchone()[0]:
                    raise InterruptedError('video_monitoring_disabled')
                discovered += 1
                ok, _ = core.youtube.eligibility(entry, cfg)
                if not ok:
                    continue
                ok, _ = core.title_passes(entry['title'], cfg, trusted=True,
                                           topic_pattern=core.youtube.TOPIC_PATTERN)
                if not ok:
                    continue
                url = 'https://www.youtube.com/watch?v=' + entry['id']
                key = _recording(db, core, 'youtube:' + entry['id'], 'youtube:' + channel_id,
                                 creator['name'], entry['title'], url, core.youtube.published(entry))
                _, added = add_candidate(db, key, 'youtube', url, details=entry)
                eligible += int(added); total += 1
                if total % 25 == 0:
                    db.execute('UPDATE backfill_jobs SET discovered=?,eligible=?,total=?,updated=? WHERE id=?',
                               (discovered, eligible, total, now, job_id)); db.commit()
        # Reddit account history is read from configured subreddit feeds and is
        # paged until exhausted. Outbound YouTube links share provider IDs with the
        # channel path, so cross-posts cannot create a second transfer.
        reddit_handles = {r['handle'].casefold() for r in identities if r['kind'] == 'reddit'}
        if reddit_handles:
            reddit = core.Reddit(core.HTTP(), secrets.get('reddit', {}))
            for sub in cfg.get('subreddits', []):
                after = None
                for _ in range(cfg.get('video_reddit_history_pages', 100)):
                    posts, after = reddit.page(sub, after)
                    for post in posts:
                        if post.get('author', '').casefold() not in reddit_handles:
                            continue
                        discovered += 1
                        if post.get('over_18'):
                            continue
                        ok, _ = core.title_passes(post.get('title', ''), cfg)
                        if not ok:
                            continue
                        urls = [post.get('url_overridden_by_dest') or post.get('url', '')]
                        urls += re.findall(r'https?://[^\s<>()]+', post.get('selftext', '') or '')
                        for url in urls:
                            host = (urlparse(url).hostname or '').lower()
                            provider = 'youtube' if host in {'youtube.com','www.youtube.com','youtu.be','m.youtube.com'} else 'reddit' if host == 'v.redd.it' else None
                            if not provider:
                                continue
                            pid, normalized = provider_identity(provider, url, post)
                            key = _recording(db, core, 'reddit:' + post['id'], 'reddit:' + sub,
                                             creator['name'], post.get('title', ''), normalized,
                                             int(post.get('created_utc', 0)))
                            _, added = add_candidate(db, key, provider, normalized, post=post)
                            eligible += int(added); total += 1
                    if not after:
                        break
        finished = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        db.execute("UPDATE backfill_jobs SET state='completed',discovered=?,eligible=?,total=?,updated=?,finished=? WHERE id=?",
                   (discovered, eligible, total, finished, finished, job_id)); db.commit()
        return {'id': job_id, 'state': 'completed', 'discovered': discovered,
                'eligible': eligible, 'total': total}
    except InterruptedError as exc:
        db.execute("UPDATE backfill_jobs SET state='cancelled',updated=?,finished=?,error=? WHERE id=?",
                   (now, now, str(exc), job_id)); db.commit()
        return {'id': job_id, 'state': 'cancelled', 'discovered': discovered,
                'eligible': eligible, 'total': total}
    except Exception as exc:
        db.execute("UPDATE backfill_jobs SET state='failed',updated=?,error=? WHERE id=?",
                   (now, type(exc).__name__, job_id)); db.commit()
        raise


def _nearest_existing(path):
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _copy_atomic(source, destination, validator, mismatch_error):
    source, destination = Path(source), Path(destination)
    partial = destination.with_name(destination.name + '.asmarr-part')
    expected = _digest(source)
    if destination.exists():
        if _digest(destination) != expected:
            raise ValueError(mismatch_error)
        return expected, False
    try:
        with source.open('rb') as src, partial.open('wb') as dst:
            shutil.copyfileobj(src, dst); dst.flush(); os.fsync(dst.fileno())
        if _digest(partial) != expected:
            raise ValueError(mismatch_error)
        validator(partial); partial.chmod(0o644); os.link(partial, destination); partial.unlink()
        return expected, True
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _validate_artwork(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError('video_artwork_invalid')
    result = subprocess.run(['ffprobe', '-v', 'error', '-f', 'image2', '-select_streams', 'v:0',
                             '-show_entries', 'stream=codec_name', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('video_artwork_invalid')
    streams = json.loads(result.stdout).get('streams', [])
    if not streams or streams[0].get('codec_name') not in {'mjpeg', 'png', 'webp'}:
        raise ValueError('video_artwork_invalid')


def import_video(db, cfg, key, source, candidate, audio_importer=None, artwork=None):
    source = Path(source).resolve()
    download_root = Path(cfg.get('download_root', '/mnt/downloads/asmarr')).resolve()
    if not source.is_relative_to(download_root) or source.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError('video_import_path_invalid')
    info = probe(source)
    asset = db.execute('SELECT * FROM assets WHERE key=?', (key,)).fetchone()
    if not asset:
        raise ValueError('recording_not_found')
    root = Path(cfg['video_root']).resolve()
    destination = video_name(asset, candidate, source.suffix, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected, video_created = _copy_atomic(source, destination, probe, 'video_import_destination_conflict')
    artwork_destination = None
    if artwork:
        artwork = Path(artwork).resolve()
        if (not artwork.is_relative_to(download_root)
                or artwork.suffix.lower() not in ARTWORK_EXTENSIONS):
            if video_created: destination.unlink(missing_ok=True)
            raise ValueError('video_artwork_path_invalid')
        artwork_destination = destination.with_suffix(artwork.suffix.lower().replace('.jpeg', '.jpg'))
        try:
            _copy_atomic(artwork, artwork_destination, _validate_artwork,
                         'video_artwork_destination_conflict')
        except Exception:
            if video_created: destination.unlink(missing_ok=True)
            raise
    now = int(time.time())
    with db:
        db.execute("""UPDATE media_assets SET state='imported',wanted=1,saved_path=?,acquired=?,error=NULL,retry_after=0,details=?
                    WHERE recording_key=? AND media_kind='Video'""",
                   (str(destination), now, json.dumps(info), key))
        _meta_set(db, 'plex_video_pending_paths', sorted(set(_meta_get(db, 'plex_video_pending_paths', [])) | {str(destination)}))
    derived = None
    has_audio = any(s.get('codec_type') == 'audio' for s in info.get('streams', []))
    audio = db.execute("SELECT * FROM media_assets WHERE recording_key=? AND media_kind='Audio'", (key,)).fetchone()
    if has_audio and audio_importer and (not audio or audio['state'] not in {'complete', 'imported'}):
        codec = next((s.get('codec_name') for s in info['streams'] if s.get('codec_type') == 'audio'), '')
        ext = {'aac': '.m4a', 'mp3': '.mp3', 'opus': '.opus', 'flac': '.flac'}.get(codec)
        if ext:
            fd, extracted_name = tempfile.mkstemp(prefix='asmarr-derived-', suffix=ext, dir=download_root)
            os.close(fd); extracted = Path(extracted_name)
            try:
                p = subprocess.run(['ffmpeg','-v','error','-i',str(source),'-vn','-c:a','copy','-y',str(extracted)], timeout=900)
                if p.returncode == 0:
                    derived = audio_importer(db, cfg, key, str(extracted))
            finally:
                extracted.unlink(missing_ok=True)
    return {'status': 'imported', 'path': str(destination), 'sha256': expected,
            'artworkPath': str(artwork_destination) if artwork_destination else None,
            'hasAudio': has_audio, 'derivedAudio': derived}


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _meta_get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _meta_set(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, json.dumps(value)))


def download_video(db, cfg, key, candidate, audio_importer=None):
    work_root = Path(cfg.get('download_root', '/mnt/downloads/asmarr')).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='asmarr-video-', dir=work_root) as work:
        output = Path(work) / 'video.%(ext)s'
        command = [cfg.get('youtube_binary', '/usr/local/bin/yt-dlp'), '--ignore-config', '--no-playlist',
                   '--no-progress', '--no-simulate', '--socket-timeout', '20', '--retries', '2',
                   '--write-thumbnail', '--convert-thumbnails', 'jpg',
                   '-f', _format_selector(candidate.get('max_height')), '--print', 'after_move:filepath',
                   '-o', str(output), candidate['url']]
        p = subprocess.run(command, capture_output=True, text=True, timeout=7200)
        if p.returncode or not p.stdout.strip():
            raise ValueError('video_download_failed')
        downloaded = Path(p.stdout.strip().splitlines()[-1]).resolve()
        if not downloaded.is_relative_to(Path(work).resolve()) or downloaded.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError('video_download_output_invalid')
        artwork = next((path for path in Path(work).glob('video.*')
                        if path.suffix.lower() in ARTWORK_EXTENSIONS), None)
        return import_video(db, cfg, key, downloaded, candidate, audio_importer, artwork)


def _format_selector(max_height=None):
    if max_height is None:
        return 'bestvideo*+bestaudio/best'
    height = int(max_height)
    if height not in set(RESOLUTIONS.values()):
        raise ValueError('unknown_video_resolution')
    return f'bestvideo*[height<={height}]+bestaudio/best[height<={height}]'


def process_queue(db, cfg, audio_importer=None):
    root = Path(cfg['video_root'])
    minimum = int(float(cfg.get('video_free_space_gib', 20)) * 1024 ** 3)
    free = shutil.disk_usage(_nearest_existing(root)).free
    if free < minimum:
        return {'status': 'paused_low_space', 'available': free, 'required': minimum, 'jobs': []}
    concurrency = max(1, int(cfg.get('video_concurrency', 1)))
    rows = db.execute("""SELECT m.*,a.creator,a.title,a.published,c.video_quality_profile_id
        FROM media_assets m JOIN assets a ON a.key=m.recording_key JOIN creators c ON c.name=a.creator
        WHERE m.media_kind='Video' AND m.wanted=1 AND c.monitor_video=1
          AND m.state IN ('wanted','failed') AND m.retry_after<=?
        ORDER BY COALESCE(a.published,0) DESC LIMIT ?""", (int(time.time()), concurrency)).fetchall()
    results = []
    for row in rows:
        candidates = [dict(c) for c in db.execute('SELECT * FROM video_candidates WHERE recording_key=? AND interactive_only=0', (row['recording_key'],))]
        profile = db.execute('SELECT resolution,settings FROM video_quality_profiles WHERE id=?', (row['video_quality_profile_id'],)).fetchone()
        options = {'resolution': profile['resolution'], **json.loads(profile['settings'])} if profile else {'resolution': 'Any'}
        candidate = choose_candidate(candidates, options)
        if not candidate:
            with db: db.execute("UPDATE media_assets SET state='unavailable',error='no_candidate_within_profile' WHERE recording_key=? AND media_kind='Video'", (row['recording_key'],))
            results.append({'key': row['recording_key'], 'status': 'unavailable'}); continue
        candidate['max_height'] = profile_limit(options)
        with db: db.execute("UPDATE media_assets SET state='downloading',error=NULL WHERE recording_key=? AND media_kind='Video'", (row['recording_key'],))
        try:
            results.append(dict(download_video(db, cfg, row['recording_key'], candidate, audio_importer), key=row['recording_key']))
        except Exception as exc:
            attempts = int(row['attempts']) + 1
            retry = int(time.time()) + min(86400, 300 * 2 ** min(attempts, 8))
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            with db: db.execute("UPDATE media_assets SET state='failed',attempts=?,retry_after=?,error=? WHERE recording_key=? AND media_kind='Video'", (attempts, retry, reason, row['recording_key']))
            results.append({'key': row['recording_key'], 'status': 'failed', 'error': reason})
    return {'status': 'ok', 'jobs': results, 'concurrency': concurrency}


def plex_library_validation(cfg, secrets):
    validate_roots(cfg['output_root'], cfg['video_root'])
    plex = cfg.get('plex', {})
    video = plex.get('video') or {}
    token = secrets.get('plex_token') or plex.get('token')
    if not plex.get('url') or not token or video.get('section_id') is None:
        raise ValueError('plex_video_binding_missing')
    response = requests.get(plex['url'].rstrip('/') + '/library/sections', headers={'X-Plex-Token': token}, timeout=(10, 30))
    response.raise_for_status()
    import xml.etree.ElementTree as et
    section = next((d for d in et.fromstring(response.text).findall('Directory') if d.get('key') == str(video['section_id'])), None)
    if section is None:
        raise ValueError('plex_video_library_unreachable')
    agent = section.get('agent', '')
    scanner = section.get('scanner', '')
    if section.get('type') != 'movie' or (agent != 'com.plexapp.agents.none' and 'video' not in scanner.casefold()):
        raise ValueError('plex_video_library_must_be_other_videos')
    # Plex includes library roots on each Directory returned by the section
    # inventory. Some versions omit them from /library/sections/{id}, whose
    # response is primarily a media listing, so prefer the authoritative
    # inventory element and retain the older endpoint as a compatibility fallback.
    location_paths = [x.get('path') for x in section.findall('.//Location') if x.get('path')]
    if not location_paths:
        locations = requests.get(plex['url'].rstrip('/') + f'/library/sections/{video["section_id"]}', headers={'X-Plex-Token': token}, timeout=(10,30))
        locations.raise_for_status()
        location_paths = [x.get('path') for x in et.fromstring(locations.text).findall('.//Location') if x.get('path')]
    if not location_paths or not any(Path(cfg['video_root']).resolve().is_relative_to(Path(p).resolve()) for p in location_paths):
        raise ValueError('plex_video_root_not_in_library')
    if any(paths_overlap(cfg['output_root'], p) for p in location_paths):
        raise ValueError('plex_audio_and_video_libraries_overlap')
    return {'status': 'healthy', 'sectionId': video['section_id'], 'title': section.get('title'), 'type': 'Other Videos'}


def plex_refresh(db, cfg, secrets):
    pending = _meta_get(db, 'plex_video_pending_paths', [])
    if not pending:
        return {'status': 'not_needed'}
    validation = plex_library_validation(cfg, secrets)
    plex = cfg['plex']; section = str(plex['video']['section_id'])
    token = secrets.get('plex_token') or plex.get('token')
    response = requests.get(plex['url'].rstrip('/') + f'/library/sections/{section}/refresh', headers={'X-Plex-Token': token}, timeout=(10,30))
    response.raise_for_status()
    with db: _meta_set(db, 'plex_video_pending_paths', [])
    return dict(validation, status='refresh_requested', paths=len(pending))
