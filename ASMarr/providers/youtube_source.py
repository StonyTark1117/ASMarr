"""Bounded polling and audio extraction for explicitly approved YouTube channels."""
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
from categories import CATEGORIES

TOPIC_PATTERN = (r'\b(?:asmr|hypno(?:sis|tic|tized|tised)?|trance|relax(?:ation|ing)?|'
                 r'sleep(?:y|ing)?|bedtime|personal\s+attention|soft[ -]spoken|whisper(?:s|ing)?)\b')
TOPIC_PATTERN = '(?:' + TOPIC_PATTERN + '|' + '|'.join(p for _, p in CATEGORIES.values()) + ')'


class YouTubeError(ValueError):
    pass


def command(cfg):
    return [cfg.get('youtube_binary', '/usr/local/bin/yt-dlp'),
            '--ignore-config', '--no-cache-dir', '--no-playlist',
            '--js-runtimes', 'deno:' + cfg.get('youtube_deno', '/usr/local/bin/deno'),
            '--socket-timeout', '20', '--retries', '2', '--extractor-retries', '2']


def run_json(args, cfg, timeout=120):
    try:
        r = subprocess.run(command(cfg) + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise YouTubeError('youtube_' + type(e).__name__) from None
    if r.returncode:
        # Do not persist signed media URLs, cookies or arbitrary stderr in status.
        message = r.stderr.lower()
        reason = ('authentication_required' if 'sign in' in message else
                  'rate_limited' if '429' in message else
                  'unavailable' if 'unavailable' in message else 'extractor_failed')
        raise YouTubeError('youtube_' + reason)
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise YouTubeError('youtube_invalid_json') from None


def listing(channel, cfg, limit=None):
    cid = channel['channel_id']
    if not re.fullmatch(r'UC[\w-]{22}', cid):
        raise YouTubeError('youtube_invalid_channel_id')
    data = run_json(['--flat-playlist', '--dump-single-json', '--skip-download',
                     '--playlist-end', str(limit or cfg.get('youtube_poll_limit', 50)),
                     'https://www.youtube.com/channel/' + cid + '/videos'], cfg)
    if data.get('channel_id') != cid or not isinstance(data.get('entries'), list):
        raise YouTubeError('youtube_channel_identity_mismatch')
    entries = data['entries']
    if any(not isinstance(e, dict) or not re.fullmatch(r'[\w-]{11}', e.get('id', ''))
           or not e.get('title') for e in entries):
        raise YouTubeError('youtube_malformed_listing')
    return entries


def eligibility(info, cfg):
    if info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'} or info.get('is_live'):
        return False, 'youtube_live_or_upcoming'
    if info.get('availability') not in {None, 'public', 'unlisted'}:
        return False, 'youtube_not_public'
    if (info.get('age_limit') or 0) >= 18:
        return False, 'youtube_age_restricted'
    duration = info.get('duration')
    if duration is None or duration < cfg.get('youtube_min_duration_seconds', 180):
        return False, 'youtube_short_or_unknown_duration'
    if re.search(r'(?i)\b(?:trailer|teaser|announcement|channel update|q\s*&\s*a)\b', info.get('title', '')):
        return False, 'youtube_promotional_or_discussion'
    return True, 'accepted'


def metadata(url, cfg):
    info = run_json(['--dump-single-json', '--skip-download', url], cfg)
    channels = {c['channel_id']: c for c in cfg.get('youtube_channels', [])}
    channel = channels.get(info.get('channel_id'))
    if channel is None:
        raise YouTubeError('youtube_channel_not_approved')
    ok, reason = eligibility(info, cfg)
    if not ok:
        raise YouTubeError(reason)
    return info, channel


def published(info):
    stamp = info.get('release_timestamp') or info.get('timestamp')
    if stamp:
        return int(stamp)
    date = info.get('upload_date')
    if date:
        try:
            return int(dt.datetime.strptime(date, '%Y%m%d').replace(tzinfo=dt.timezone.utc).timestamp())
        except ValueError:
            pass
    return 0


def download(info, workdir, cfg):
    workdir = Path(workdir)
    metadata_path = workdir / 'source.json'
    metadata_path.write_text(json.dumps(info))
    metadata_path.chmod(0o600)
    args = command(cfg) + ['--quiet', '--no-progress', '--no-simulate',
            '--load-info-json', str(metadata_path),
            '-f', 'bestaudio[ext=m4a]/bestaudio', '-x', '--audio-format', 'm4a',
            '--max-filesize', str(cfg.get('max_file_bytes', 2147483648)),
            '--fragment-retries', '2', '--abort-on-unavailable-fragments',
            '--print', 'after_move:filepath', '-o', str(workdir / 'audio.%(ext)s')]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise YouTubeError('youtube_' + type(e).__name__) from None
    if r.returncode or not r.stdout.strip():
        raise YouTubeError('youtube_download_failed')
    output = Path(r.stdout.strip().splitlines()[-1])
    if output.resolve() != (workdir / 'audio.m4a').resolve() or not output.is_file():
        raise YouTubeError('youtube_unexpected_output')
    if output.stat().st_size > cfg.get('max_file_bytes', 2147483648):
        raise YouTubeError('youtube_file_too_large')
    return output
