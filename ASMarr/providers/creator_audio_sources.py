"""Public creator listings and permitted audio delivery on audio sharing sites."""
from datetime import datetime
from html.parser import HTMLParser
import re
from urllib.parse import urlparse


class SourceFailure(ValueError):
    pass


# Audiochan binds its public playback cookie to the User-Agent that requested
# the anonymous play intent. Keep that value consistent through media transfer.
AUDIOCHAN_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def audiochan_requires_tier(item):
    """Reject tracks whose current public metadata requires paid access."""
    return (any(item.get(key) for key in ('required_tier_id', 'requiredTierId',
                                         'required_tier', 'requiredTier',
                                         'required_patreon_tier', 'requiredPatreonTier'))
            or any(item.get(key) is True for key in ('is_locked', 'isLocked', 'locked',
                                                    'is_premium_stream', 'isPremiumStream')))


class TrackLinks(HTMLParser):
    def __init__(self, prefix, marker):
        super().__init__(convert_charrefs=True)
        self.prefix = prefix
        self.marker = marker
        self.current = None
        self.parts = []
        self.tracks = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        values = dict(attrs)
        path = values.get('href', '')
        if path.startswith(self.prefix) and self.marker in values.get('class', ''):
            self.current = path
            self.parts = []

    def handle_data(self, data):
        if self.current:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            title = ' '.join(''.join(self.parts).split())
            if title:
                self.tracks[self.current] = title
            self.current = None
            self.parts = []


def profile_listing(http, kind, handle):
    if kind == 'whyp':
        posts = {}
        cursor = None
        seen_cursors = set()
        for _ in range(100):
            params = {'cursor': cursor} if cursor else {}
            with http.get('https://api.whyp.it/api/users/' + handle + '/tracks',
                          params=params, headers={'Accept': 'application/json',
                                                   'Origin': 'https://whyp.it',
                                                   'Referer': 'https://whyp.it/'}) as response:
                body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get('tracks'), list):
                raise SourceFailure('malformed_whyp_listing')
            pinned = body.get('pinned', [])
            if not isinstance(pinned, list):
                raise SourceFailure('malformed_whyp_listing')
            for item in pinned + body['tracks']:
                if not isinstance(item, dict):
                    continue
                path, title, identity = item.get('url'), item.get('title'), item.get('public_id')
                if not (isinstance(path, str) and re.fullmatch(r'/tracks/[^/]+-[A-Za-z0-9]+', path)
                        and isinstance(title, str) and isinstance(identity, str)):
                    continue
                posts[identity] = {'id': identity, 'title': title,
                                   'url': 'https://whyp.it' + path,
                                   'published': timestamp(item.get('published_at') or item.get('created_at'))}
            cursor = body.get('next_cursor')
            if not cursor:
                return list(posts.values())
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise SourceFailure('whyp_listing_cursor_invalid')
            seen_cursors.add(cursor)
        raise SourceFailure('whyp_listing_page_limit')
    if kind == 'hotaudio':
        with http.get('https://hotaudio.net/u/' + handle) as response:
            response.encoding = 'utf-8'
            page = response.text
        parser = TrackLinks('/u/' + handle + '/', 'font-bold')
        parser.feed(page)
        return [{'id': path.split('/')[-1], 'title': title,
                 'url': 'https://hotaudio.net' + path, 'published': 0}
                for path, title in parser.tracks.items()]
    if kind == 'audiochan':
        items = []
        for page in range(1, 101):
            with http.get('https://api.audiochan.com/users/' + handle + '/content',
                          params={'page': page, 'limit': 50, 'type': 'audios',
                                  'content_mode': 'all', 'sort': 'new'}) as response:
                body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get('data'), list):
                raise SourceFailure('malformed_audiochan_listing')
            for item in body['data']:
                if not isinstance(item, dict) or item.get('content_type') != 'audio':
                    continue
                if item.get('visibility') != 'public' or item.get('status') != 'published':
                    continue
                if audiochan_requires_tier(item) or not item.get('audio_file_id'):
                    continue
                slug, title, identity = item.get('slug'), item.get('title'), item.get('id')
                if not (isinstance(slug, str) and re.fullmatch(r'[A-Za-z0-9_-]{5,100}', slug)
                        and isinstance(title, str) and isinstance(identity, str)):
                    continue
                items.append({'id': identity, 'title': title,
                              'url': 'https://audiochan.com/a/' + slug,
                              'published': timestamp(item.get('published_at') or item.get('created_at'))})
            if not body.get('has_more'):
                return items
        raise SourceFailure('audiochan_listing_page_limit')
    raise SourceFailure('unsupported_creator_source')


def timestamp(value):
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()) if value else 0
    except (TypeError, ValueError):
        return 0


def resolve_audio(http, kind, url):
    """Return a short-lived media URL and headers, without storing either."""
    parsed = urlparse(url)
    if kind == 'whyp':
        if parsed.hostname != 'whyp.it' or not re.fullmatch(r'/tracks/[^/]+-[A-Za-z0-9]+', parsed.path):
            raise SourceFailure('invalid_whyp_track')
        track_id = parsed.path.rsplit('-', 1)[-1]
        with http.get('https://api.whyp.it/api/tracks/' + track_id + '/download',
                      params={'token': '', 'lossless': 'false'}) as response:
            result = response.json()
        media = result.get('url') if isinstance(result, dict) else None
        if not isinstance(media, str) or urlparse(media).scheme != 'https' or urlparse(media).hostname != 'cdn.whyp.it':
            raise SourceFailure('whyp_download_unavailable')
        return media, {}
    if kind == 'audiochan':
        if parsed.hostname != 'audiochan.com' or not re.fullmatch(r'/a/[A-Za-z0-9_-]{5,100}', parsed.path):
            raise SourceFailure('invalid_audiochan_track')
        headers = {'Origin': 'https://audiochan.com', 'Referer': 'https://audiochan.com/',
                   'User-Agent': AUDIOCHAN_UA}
        # The public site creates an anonymous session before requesting a
        # play intent. Its cookies stay in this short-lived HTTP session.
        with http.request('POST', 'https://api.audiochan.com/auth/anonymous', headers=headers):
            pass
        slug = parsed.path.split('/')[-1]
        with http.get('https://api.audiochan.com/audios/slug/' + slug, headers=headers) as response:
            item = response.json()
        identity = item.get('id') if isinstance(item, dict) else None
        audio_file = item.get('audioFile') if isinstance(item, dict) else None
        file_id = audio_file.get('id') if isinstance(audio_file, dict) else None
        if (not identity or not file_id or item.get('visibility') != 'public'
                or item.get('status') != 'published' or audiochan_requires_tier(item)
                or audiochan_requires_tier(audio_file)):
            raise SourceFailure('audiochan_audio_unavailable')
        with http.request('POST', 'https://api.audiochan.com/audios/' + identity + '/play-intent',
                          json={'file_id': file_id}, headers=headers) as response:
            intent = response.json().get('play_intent')
        if not intent:
            raise SourceFailure('audiochan_play_intent_unavailable')
        with http.request('POST', 'https://api.audiochan.com/audios/' + identity + '/stream-url',
                          json={'file_id': file_id, 'play_intent': intent}, headers=headers) as response:
            media = response.json().get('url')
        if not isinstance(media, str) or urlparse(media).scheme != 'https' or urlparse(media).hostname != 'stream.audiochan.com':
            raise SourceFailure('audiochan_stream_unavailable')
        return media, dict(headers, Range='bytes=0-')
    if kind == 'hotaudio':
        raise SourceFailure('hotaudio_requires_browser_capture')
    raise SourceFailure('unsupported_creator_source')
