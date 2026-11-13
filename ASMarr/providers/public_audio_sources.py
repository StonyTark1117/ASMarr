"""Public catalog adapters with stable identities and transient delivery URLs."""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path, PurePosixPath
import random
import re
import socket
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET
import zipfile

import requests


KINDS = {'rss', 'internet_archive', 'bandcamp'}
FORMAT_EXTENSIONS = {
    'VBR MP3': '.mp3', 'MP3': '.mp3', 'MPEG-4 Audio': '.m4a',
    'MPEG4': '.m4a', 'Ogg Vorbis': '.opus', 'Ogg Opus': '.opus',
    'Flac': '.flac', 'WAVE': '.wav', 'AAC': '.aac',
}
EXTENSION_ORDER = ['.mp3', '.m4a', '.opus', '.flac', '.wav', '.aac']
PROMOTIONAL = re.compile(r'(?i)\b(?:trailer|teaser|announcement|channel update|q\s*&\s*a|questions?\s+and\s+answers?)\b')


class PublicSourceError(ValueError):
    def __init__(self, reason, stage='discovery'):
        super().__init__(reason)
        self.stage = stage


class SafeHTTP:
    """Validate every hop so public source URLs cannot pivot into local services."""
    def __init__(self, delegate, max_redirects=10):
        self.delegate=delegate;self.max_redirects=max_redirects
    def get(self,url,**kwargs):
        current=public_url(url)
        for _ in range(self.max_redirects+1):
            response=self.delegate.get(current,allow_redirects=False,**kwargs)
            if response.status_code not in {301,302,303,307,308}:return response
            location=response.headers.get('Location');response.close()
            if not location:raise PublicSourceError('redirect_location_missing')
            current=public_url(urljoin(current,location))
        raise PublicSourceError('redirect_limit_exceeded')


def _public_host(host):
    if not host:
        raise PublicSourceError('source_host_missing')
    try:
        addresses = {item[4][0].split('%')[0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise PublicSourceError('source_host_unresolvable') from None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise PublicSourceError('source_address_not_public')


def public_url(value, *, hosts=None):
    parsed = urlparse(html.unescape(str(value).strip()))
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        raise PublicSourceError('public_http_url_required')
    host = (parsed.hostname or '').lower().rstrip('.')
    if hosts and host not in hosts:
        raise PublicSourceError('unexpected_source_host')
    _public_host(host)
    port = parsed.port
    if port not in {None, 80, 443}:
        raise PublicSourceError('source_port_not_allowed')
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', '', parsed.query, ''))


def normalize_identity(kind, value):
    if kind not in KINDS:
        raise PublicSourceError('unknown_public_source')
    raw = str(value).strip()
    if kind == 'rss':
        return public_url(raw)
    if kind == 'internet_archive':
        if raw.startswith(('http://', 'https://')):
            parsed = urlparse(public_url(raw, hosts={'archive.org', 'www.archive.org'}))
            match = re.match(r'^/(?:details|metadata)/([^/?#]+)', parsed.path)
            if not match:
                raise PublicSourceError('archive_item_url_required')
            identifier=unquote(match.group(1));selector=parse_qs(parsed.query).get('creator',[''])[0].strip()
            raw=('collection:'+identifier+'?creator='+quote(selector,safe='')) if selector else identifier
        if raw.startswith('collection:'):
            match=re.fullmatch(r'collection:([A-Za-z0-9_.-]{1,160})\?creator=([^&#]+)',raw)
            if not match or not unquote(match.group(2)).strip():raise PublicSourceError('archive_collection_creator_selector_required')
            return 'collection:'+match.group(1)+'?creator='+quote(unquote(match.group(2)).strip(),safe='')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', raw):
            raise PublicSourceError('archive_identifier_invalid')
        return raw
    parsed = urlparse(public_url(raw))
    if parsed.hostname == 'bandcamp.com' or not (parsed.hostname or '').endswith('.bandcamp.com'):
        raise PublicSourceError('bandcamp_artist_url_required')
    path = re.sub(r'/+$', '', parsed.path)
    if path == '/music':
        path = ''
    if path and not re.fullmatch(r'/(?:album|track)/[^/?#]+', path):
        raise PublicSourceError('bandcamp_artist_release_or_track_required')
    return urlunparse(('https', parsed.hostname.lower(), path or '/', '', '', ''))


def stable_id(kind, identity, provider_id):
    digest = hashlib.sha256((kind + '\0' + identity + '\0' + str(provider_id)).encode()).hexdigest()
    return kind + ':' + digest[:32]


def _text(node, names):
    for child in list(node):
        if child.tag.rsplit('}', 1)[-1].lower() in names and child.text:
            return child.text.strip()
    return ''


def _duration(value):
    if not value:
        return None
    try:
        parts = [float(p) for p in value.strip().split(':')]
        total = 0.0
        for part in parts:
            total = total * 60 + part
        return total if total > 0 else None
    except (ValueError, TypeError):
        return None


def _published(value):
    if not value:
        return 0
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except (ValueError, TypeError, OverflowError):
        try:
            import datetime as dt
            return int(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0


def rss_catalog(http, identity, *, limit=5000, validators=None, cursor=None):
    identity = normalize_identity('rss', identity)
    pages, seen, items, cursor = 0, set(), [], (cursor or identity);response_validators={}
    while cursor and pages < 25 and len(items) < limit:
        cursor = public_url(cursor)
        if cursor in seen:
            raise PublicSourceError('rss_pagination_loop')
        seen.add(cursor); pages += 1
        headers={'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'}
        if pages==1 and validators:
            if validators.get('etag'):headers['If-None-Match']=validators['etag']
            if validators.get('lastModified'):headers['If-Modified-Since']=validators['lastModified']
        with http.get(cursor, headers=headers) as response:
            if response.status_code==304:
                return {'identity':identity,'title':'','items':[],'complete':True,'pages':1,'notModified':True,
                        'validators':validators or {},'scope':'Recordings currently available in this public feed'}
            body = response.content
            if pages==1:response_validators={'etag':response.headers.get('ETag'),'lastModified':response.headers.get('Last-Modified')}
        if len(body) > 20 * 1024 * 1024:
            raise PublicSourceError('rss_document_too_large')
        if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b',body,re.I):
            raise PublicSourceError('rss_external_entities_forbidden')
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise PublicSourceError('rss_xml_invalid') from None
        if root.tag.lower().endswith('rss'):
            entries = root.findall('./channel/item')
        else:
            entries = [n for n in root.iter() if n.tag.rsplit('}', 1)[-1].lower() == 'entry']
        for entry in entries:
            enclosure = None; declared = None
            for child in list(entry):
                local = child.tag.rsplit('}', 1)[-1].lower()
                if local == 'enclosure' or (local == 'link' and child.attrib.get('rel') == 'enclosure'):
                    candidate = child.attrib.get('url') or child.attrib.get('href')
                    media_type = child.attrib.get('type', '')
                    if candidate and (media_type.startswith('audio/') or not media_type):
                        enclosure = urljoin(cursor, candidate)
                        try: declared = int(child.attrib.get('length', '0')) or None
                        except ValueError: declared = None
                        break
            if not enclosure:
                continue
            title = _text(entry, {'title'}) or 'Untitled episode'
            guid = _text(entry, {'guid', 'id'})
            description=_text(entry, {'description','summary','content'})
            episode_type=_text(entry, {'episodetype','episode_type'})
            page = _text(entry, {'link'})
            if not page:
                link = next((c for c in list(entry) if c.tag.rsplit('}', 1)[-1].lower() == 'link' and c.attrib.get('rel', 'alternate') != 'enclosure'), None)
                page = link.attrib.get('href', '') if link is not None else ''
            provider_id = guid or page or enclosure.split('?', 1)[0]
            items.append({'providerId': provider_id, 'title': title,
                          'published': _published(_text(entry, {'pubdate', 'published', 'updated'})),
                          'duration': _duration(_text(entry, {'duration'})),
                          'description':description,'episodeType':episode_type or None,
                          'enclosureType':next((c.attrib.get('type') for c in list(entry) if (c.tag.rsplit('}',1)[-1].lower()=='enclosure' or (c.tag.rsplit('}',1)[-1].lower()=='link' and c.attrib.get('rel')=='enclosure'))),None),
                          'pageUrl': page, 'declaredBytes': declared,
                          'promotional': bool(PROMOTIONAL.search(title))})
        cursor = ''
        for node in root.iter():
            if node.tag.rsplit('}', 1)[-1].lower() == 'link' and node.attrib.get('rel') in {'next', 'next-archive'}:
                cursor = urljoin(identity, node.attrib.get('href', '')); break
    channel=root.find('./channel')
    return {'identity': identity, 'title': _text(channel if channel is not None else root, {'title'}),
            'items': items[:limit], 'complete': not cursor, 'pages': pages,
            'nextCursor':cursor or None,
            'validators':response_validators,
            'scope': 'Recordings currently available in this public feed'}


def _archive_files(payload, allowed=None):
    allowed = set(allowed or EXTENSION_ORDER)
    groups = {}
    for entry in payload.get('files', []):
        ext = FORMAT_EXTENSIONS.get(entry.get('format')) or Path(entry.get('name', '')).suffix.lower()
        if ext not in EXTENSION_ORDER:
            continue
        original = entry.get('original') or entry.get('name')
        groups.setdefault(original, []).append(dict(entry, extension=ext))
    items = []
    for original, choices in groups.items():
        choices = [c for c in choices if c['extension'] in allowed]
        if not choices:
            continue
        choices.sort(key=lambda c:(EXTENSION_ORDER.index(c['extension']), c.get('source') != 'original'))
        choice = choices[0]
        try: declared = int(choice.get('size') or 0) or None
        except ValueError: declared = None
        items.append({'providerId': original, 'title': Path(original).stem,
                      'published': _published(payload.get('metadata', {}).get('date')),
                      'duration': _duration(choice.get('length')),
                      'declaredBytes': declared, 'expectedMd5': choice.get('md5'),
                      'filename': choice['name'], 'extension': choice['extension'],
                      'promotional': bool(PROMOTIONAL.search(Path(original).stem))})
    return items


def _archive_item(http, identifier, allowed=None):
    with http.get('https://archive.org/metadata/' + quote(identifier, safe='')) as response:
        payload = response.json()
    metadata=payload.get('metadata') or {}
    restricted=bool(metadata.get('is_dark') or metadata.get('access-restricted-item') in {'true',True})
    return payload,metadata,restricted


def archive_catalog(http, identity, *, allowed=None, limit=5000, cursor=None):
    identifier = normalize_identity('internet_archive', identity)
    if identifier.startswith('collection:'):
        collection,selector=identifier[len('collection:'):].split('?creator=',1);selector=unquote(selector)
        page=max(1,int(cursor or 1));start_page=page;items=[];item_count=0;total=0
        while len(items)<limit and page<start_page+20:
            query=f'collection:{collection} AND creator:"{selector.replace(chr(34), "")}"'
            with http.get('https://archive.org/advancedsearch.php',params={'q':query,'fl[]':['identifier'],'rows':50,'page':page,'output':'json'}) as response:search=response.json()
            docs=(search.get('response') or {}).get('docs') or [];total=int((search.get('response') or {}).get('numFound') or 0)
            if not docs:break
            for doc in docs:
                item_id=str(doc.get('identifier') or '')
                if not item_id:continue
                payload,metadata,restricted=_archive_item(http,item_id,allowed);item_count+=1
                if restricted:
                    items.append({'providerId':item_id+'/restricted','title':metadata.get('title') or item_id,'published':0,
                                  'duration':None,'available':False,'availabilityReason':'archive_item_restricted','promotional':False})
                    continue
                for item in _archive_files(payload,allowed):
                    item['providerId']=item_id+'/'+item['providerId'];item['itemIdentifier']=item_id;items.append(item)
                    if len(items)>=limit:break
                if len(items)>=limit:break
            if item_count>=total:break
            page+=1
        complete=(start_page-1)*50+item_count>=total and len(items)<=limit
        return {'identity':identifier,'title':selector+' in '+collection,'creator':selector,'items':items[:limit],
                'complete':complete,'nextCursor':None if complete else page,'pages':page-start_page+1,'scope':'Audio recordings in matching Internet Archive items',
                'collection':collection,'creatorSelector':selector,'itemsInspected':item_count,'itemsMatched':total}
    payload,metadata,restricted=_archive_item(http,identifier,allowed)
    if restricted:
        return {'identity':identifier,'title':metadata.get('title') or identifier,'creator':metadata.get('creator'),
                'items':[{'providerId':'restricted','title':metadata.get('title') or identifier,'published':0,'duration':None,
                          'available':False,'availabilityReason':'archive_item_restricted','promotional':False}],
                'complete':True,'pages':1,'scope':'Audio recordings in this Internet Archive item'}
    return {'identity': identifier, 'title': metadata.get('title') or identifier,
            'creator': metadata.get('creator'), 'licenseUrl': metadata.get('licenseurl'),
            'items': _archive_files(payload, allowed), 'complete': True, 'pages': 1,
            'scope': 'Audio recordings in this Internet Archive item'}


class _BandcampParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.data = {}; self.links = []
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for key in ('data-tralbum', 'data-blob', 'data-client-items'):
            if key in values:
                try:self.data[key] = json.loads(html.unescape(values[key]))
                except (ValueError, TypeError):pass
        href = values.get('href', '')
        if re.match(r'^/(?:album|track)/', href):self.links.append(href.split('?', 1)[0])


def _bandcamp_page(http, url):
    with http.get(url) as response:
        text = response.text
    if 'Client Challenge' in text or re.search(r'<title>\s*(?:Human Verification|Verification Required)',text,re.I):
        raise PublicSourceError('bandcamp_verification_challenge')
    parser = _BandcampParser();parser.feed(text)
    return parser


def bandcamp_catalog(http, identity, *, limit=1000):
    identity = normalize_identity('bandcamp', identity)
    parsed = urlparse(identity)
    paths = [parsed.path] if parsed.path not in {'', '/'} else []
    if not paths:
        artist = _bandcamp_page(http, identity)
        paths = list(dict.fromkeys(artist.links))
    items=[]; releases=[]
    for path in paths[:200]:
        url = urljoin(identity, path)
        page = _bandcamp_page(http, url)
        tralbum = page.data.get('data-tralbum') or {}
        current = tralbum.get('current') or {}
        release_id = str(tralbum.get('id') or current.get('id') or path)
        releases.append({'id': release_id, 'url': url, 'free': bool(tralbum.get('freeDownloadPage'))})
        for track in tralbum.get('trackinfo') or []:
            if not track.get('duration'):
                continue
            track_id = str(track.get('track_id') or track.get('id') or '')
            if not track_id:continue
            title = track.get('title') or 'Untitled track'
            items.append({'providerId': track_id, 'releaseId': release_id, 'releaseUrl': url,
                          'title': title, 'duration': track.get('duration'),
                          'published': _published(current.get('publish_date') or tralbum.get('album_publish_date')),
                          'trackNumber': track.get('track_num'), 'freeDownload': bool(tralbum.get('freeDownloadPage')),
                          'promotional': bool(PROMOTIONAL.search(title))})
            if len(items)>=limit:break
        if len(items)>=limit:break
    return {'identity': identity, 'title': parsed.hostname.split('.')[0], 'items': items,
            'releases': releases, 'complete': len(paths)<=200 and len(items)<limit,
            'pages': len(paths), 'scope': 'Public releases currently listed by this Bandcamp artist or release'}


def catalog(http, kind, identity, *, allowed=None, limit=5000, validators=None, cursor=None):
    if kind == 'rss':return rss_catalog(http, identity, limit=limit, validators=validators, cursor=cursor)
    if kind == 'internet_archive':return archive_catalog(http, identity, allowed=allowed, limit=limit, cursor=cursor)
    if kind == 'bandcamp':return bandcamp_catalog(http, identity, limit=limit)
    raise PublicSourceError('unknown_public_source')


def rss_enclosure(http, identity, provider_id):
    # Refresh the feed so transient enclosure URLs never become durable state.
    identity = normalize_identity('rss', identity)
    pages, seen, cursor = 0, set(), identity
    while cursor and pages < 25:
        cursor = public_url(cursor)
        if cursor in seen:break
        seen.add(cursor);pages += 1
        with http.get(cursor) as response:body=response.content
        if len(body)>20*1024*1024:raise PublicSourceError('rss_document_too_large','resolution')
        if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b',body,re.I):raise PublicSourceError('rss_external_entities_forbidden','resolution')
        root = ET.fromstring(body)
        entries = root.findall('./channel/item') if root.tag.lower().endswith('rss') else [n for n in root.iter() if n.tag.rsplit('}',1)[-1].lower()=='entry']
        for entry in entries:
            guid = _text(entry, {'guid','id'});page = _text(entry, {'link'})
            found = None
            for child in list(entry):
                local=child.tag.rsplit('}',1)[-1].lower()
                if local=='enclosure' or (local=='link' and child.attrib.get('rel')=='enclosure'):
                    found=child.attrib.get('url') or child.attrib.get('href');break
            if found and str(provider_id) in {guid, page, urljoin(cursor,found).split('?',1)[0]}:
                return urljoin(cursor,found)
        cursor=''
        for node in root.iter():
            if node.tag.rsplit('}',1)[-1].lower()=='link' and node.attrib.get('rel') in {'next','next-archive'}:
                cursor=urljoin(identity,node.attrib.get('href',''));break
    raise PublicSourceError('rss_recording_no_longer_available', 'resolution')


def libsyn_fallback(enclosure):
    """Recover only the tested nested public Libsyn path, never arbitrary redirects."""
    parsed=urlparse(enclosure)
    if parsed.hostname not in {'dts.podtrac.com','pscrb.fm'}:
        return None
    decoded=unquote(parsed.path)
    marker='traffic.libsyn.com/'
    position=decoded.find(marker)
    if position<0:return None
    path=decoded[position+len(marker):]
    return public_url('https://traffic.libsyn.com/'+path+('?' + parsed.query if parsed.query else ''),hosts={'traffic.libsyn.com'})


def archive_download(http, identity, provider_id, allowed=None):
    normalized=normalize_identity('internet_archive',identity)
    if normalized.startswith('collection:'):
        item_identifier,separator,original=str(provider_id).partition('/')
        if not separator or original=='restricted':raise PublicSourceError('archive_recording_no_longer_available','resolution')
        payload,metadata,restricted=_archive_item(http,item_identifier,allowed)
        if restricted:raise PublicSourceError('archive_item_restricted','resolution')
        current={'identity':item_identifier,'items':_archive_files(payload,allowed)};provider_id=original
    else:current=archive_catalog(http,identity,allowed=allowed)
    item=next((x for x in current['items'] if x['providerId']==provider_id),None)
    if not item:raise PublicSourceError('archive_recording_no_longer_available','resolution')
    item['url']='https://archive.org/download/'+quote(current['identity'],safe='')+'/'+quote(item['filename'])
    return item


def bandcamp_download(http, release_url, allowed=None):
    page=_bandcamp_page(http,release_url);tralbum=page.data.get('data-tralbum') or {}
    free=tralbum.get('freeDownloadPage')
    if not free:raise PublicSourceError('bandcamp_free_download_unavailable','resolution')
    download=_bandcamp_page(http,free);blob=download.data.get('data-blob') or {}
    records=(blob.get('digital_items') or blob.get('download_items') or [])
    if not records:raise PublicSourceError('bandcamp_download_metadata_missing','resolution')
    downloads=records[0].get('downloads') or {}
    allowed=set(allowed or EXTENSION_ORDER);selection=None
    for name,extension in (('mp3-v0','.mp3'),('mp3-320','.mp3'),('flac','.flac'),('wav','.wav'),('aac-hi','.aac')):
        if extension not in allowed:continue
        if downloads.get(name,{}).get('url'):
            selection=downloads[name];break
    if not selection:raise PublicSourceError('bandcamp_supported_download_unavailable','resolution')
    stat=selection['url'].replace('/download/','/statdownload/')
    stat += ('&' if '?' in stat else '?')+'.rand='+str(int(time.time()*1000*random.random()))
    with http.get(stat) as response:text=response.text
    try:status=json.loads(text[text.index('{'):text.rindex('}')+1])
    except (ValueError, json.JSONDecodeError):raise PublicSourceError('bandcamp_download_refresh_invalid','resolution') from None
    retry=status.get('retry_url')
    if not retry:raise PublicSourceError('bandcamp_download_refresh_failed','resolution')
    return retry


def safe_zip_members(path, *, max_files=2000, max_bytes=4*1024**3):
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist()
        if len(infos)>max_files or sum(i.file_size for i in infos)>max_bytes:
            raise PublicSourceError('archive_extraction_limit','validation')
        bad=archive.testzip()
        if bad:raise PublicSourceError('archive_crc_failed','validation')
        result=[]
        for info in infos:
            pure=PurePosixPath(info.filename)
            mode=info.external_attr >> 16
            if pure.is_absolute() or '..' in pure.parts or (mode & 0o170000)==0o120000:
                raise PublicSourceError('archive_member_unsafe','validation')
            if Path(info.filename).suffix.lower() in EXTENSION_ORDER:result.append(info)
        return result


def match_bandcamp_member(members, title, track_number=None):
    def norm(value):return ' '.join(re.findall(r'[a-z0-9]+',value.casefold()))
    wanted=norm(title);number=str(track_number or '').zfill(2)
    ranked=[]
    for member in members:
        stem=Path(member.filename).stem
        candidate=norm(re.sub(r'^\s*\d+[ ._-]+','',stem))
        score=int(candidate==wanted)*100 + len(set(candidate.split())&set(wanted.split()))
        if number and re.match(r'^\s*'+re.escape(number)+r'\D',stem):score+=20
        ranked.append((score,member))
    ranked.sort(key=lambda x:x[0],reverse=True)
    if not ranked or ranked[0][0]<=0 or (len(ranked)>1 and ranked[0][0]==ranked[1][0]):
        raise PublicSourceError('bandcamp_archive_member_ambiguous','validation')
    return ranked[0][1]
