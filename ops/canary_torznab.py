"""Loopback-only metadata fixture for the real Prowlarr/qBittorrent canary.

Serves one single-file torrent with a public CC0 HTTP web seed. It does not
register indexers, edit application state, submit jobs, or approve acceptance.
Actual client transfer, cross-filesystem import and Plex indexing remain required.
"""
import argparse
import datetime as dt
import email.utils
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SOURCE = 'https://archive.org/download/raven/raven_poe_64kb.mp3'
SOURCE_SHA1 = 'b8e90f1b4b48a686eea5578754a4f20ec1491ba4'
SOURCE_SIZE = 4568002
LICENSE = 'http://creativecommons.org/publicdomain/zero/1.0/'
TITLE = 'Chris Goringe The Raven MP3 ASMarr Acceptance'
NAMESPACE = 'http://torznab.com/schemas/2015/feed'


def bencode(value):
    if isinstance(value, bytes): return str(len(value)).encode()+b':'+value
    if isinstance(value, str): return bencode(value.encode())
    if type(value) is int: return b'i'+str(value).encode()+b'e'
    if isinstance(value, list): return b'l'+b''.join(bencode(v) for v in value)+b'e'
    if isinstance(value, dict):
        return b'd'+b''.join(bencode(k)+bencode(value[k]) for k in sorted(value))+b'e'
    raise ValueError('unsupported_bencode_type')


def torrent(data, source=SOURCE):
    if not data or not source.startswith('https://archive.org/download/raven/'):
        raise ValueError('public_domain_canary_source_required')
    size=256*1024
    info={b'name':b'raven_poe_64kb.mp3', b'length':len(data), b'piece length':size,
          b'pieces':b''.join(hashlib.sha1(data[i:i+size]).digest() for i in range(0,len(data),size))}
    payload=bencode({b'info':info,b'url-list':[source]})
    return payload,hashlib.sha1(bencode(info)).hexdigest()


def prepare(directory):
    directory=Path(directory)
    directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    with urllib.request.urlopen('https://archive.org/metadata/raven',timeout=30) as response:
        metadata=json.load(response)
    item=next(f for f in metadata['files'] if f['name']=='raven_poe_64kb.mp3')
    if metadata['metadata'].get('licenseurl')!=LICENSE or item.get('sha1')!=SOURCE_SHA1 or int(item['size'])!=SOURCE_SIZE:
        raise ValueError('public_domain_source_metadata_changed')
    with urllib.request.urlopen(SOURCE,timeout=60) as response:
        data=response.read(SOURCE_SIZE+1)
    if len(data)!=SOURCE_SIZE or hashlib.sha1(data).hexdigest()!=SOURCE_SHA1:
        raise ValueError('public_domain_source_bytes_mismatch')
    payload,identifier=torrent(data)
    manifest={'title':TITLE,'infoHash':identifier,'size':len(data),'source':SOURCE,
              'sourceSha1':SOURCE_SHA1,'sourceSha256':hashlib.sha256(data).hexdigest(),
              'torrentSha256':hashlib.sha256(payload).hexdigest(),'license':LICENSE,
              'licenseEvidence':'https://archive.org/metadata/raven',
              'preparedAt':dt.datetime.now(dt.timezone.utc).isoformat()}
    for name,content in [('canary.torrent',payload),('manifest.json',json.dumps(manifest,indent=2).encode())]:
        path=directory/name
        with path.open('xb') as output:
            path.chmod(0o600);output.write(content)
    return manifest


def feed(manifest,base,query=''):
    ET.register_namespace('torznab',NAMESPACE)
    rss=ET.Element('rss',version='2.0');channel=ET.SubElement(rss,'channel')
    for name,value in [('title','ASMarr CC0 Acceptance'),('description','Controlled real-client canary'),('link',base+'/api')]:
        ET.SubElement(channel,name).text=value
    tokens=set(query.casefold().split())
    match=tokens.issubset(set(manifest['title'].casefold().split()))
    ET.SubElement(channel,'{'+NAMESPACE+'}response',offset='0',total='1' if match else '0')
    if match:
        item=ET.SubElement(channel,'item');ET.SubElement(item,'title').text=manifest['title']
        ET.SubElement(item,'guid',isPermaLink='false').text='urn:asmarr:canary:'+manifest['infoHash']
        ET.SubElement(item,'link').text=base+'/canary.torrent'
        ET.SubElement(item,'pubDate').text=email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
        ET.SubElement(item,'size').text=str(manifest['size'])
        ET.SubElement(item,'description').text='CC0 LibriVox reading; acceptance feed entry, not an original publication date'
        ET.SubElement(item,'enclosure',url=base+'/canary.torrent',length=str(manifest['size']),type='application/x-bittorrent')
        for name,value in [('category','3030'),('seeders','0'),('peers','0'),('infohash',manifest['infoHash']),('downloadvolumefactor','0'),('uploadvolumefactor','1')]:
            ET.SubElement(item,'{'+NAMESPACE+'}attr',name=name,value=value)
    return ET.tostring(rss,encoding='utf-8',xml_declaration=True)


CAPS=b'''<?xml version="1.0"?><caps><server version="1.0" title="ASMarr CC0 Acceptance"/><limits max="100" default="100"/><searching><search available="yes" supportedParams="q"/><audio-search available="yes" supportedParams="q,artist,album"/></searching><categories><category id="3000" name="Audio"><subcat id="3030" name="Audiobook"/></category></categories></caps>'''


def handler(directory,base):
    directory=Path(directory);manifest=json.loads((directory/'manifest.json').read_text())
    payload=(directory/'canary.torrent').read_bytes()
    if hashlib.sha256(payload).hexdigest()!=manifest['torrentSha256']:
        raise ValueError('canary_torrent_changed')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass  # Never persist query API keys.
        def do_GET(self):
            parsed=urllib.parse.urlsplit(self.path);query=urllib.parse.parse_qs(parsed.query)
            if parsed.path=='/canary.torrent':body=payload;content='application/x-bittorrent'
            elif parsed.path=='/api':
                body=CAPS if query.get('t')==['caps'] else feed(manifest,base,query.get('q',[''])[0])
                content='application/rss+xml'
            else:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',content)
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    return Handler


def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['prepare','serve'])
    parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--port',type=int,default=18787)
    args=parser.parse_args()
    if args.operation=='prepare':print(json.dumps(prepare(args.directory)))
    else:
        base='http://127.0.0.1:'+str(args.port)
        server=ThreadingHTTPServer(('127.0.0.1',args.port),handler(args.directory,base))
        print(json.dumps({'scope':'metadata fixture only; no application/client mutations','url':base}),flush=True)
        try:server.serve_forever()
        finally:server.server_close()


if __name__=='__main__':main()
