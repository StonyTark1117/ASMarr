import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import public_audio_sources as sources
import scraper
import bridge


class Response:
    def __init__(self, body=b'', *, status=200, url='https://example.com/source', headers=None):
        self.content=body;self.text=body.decode() if isinstance(body,bytes) else body
        self.status_code=status;self.url=url;self.headers=headers or {}
    def json(self):return json.loads(self.text)
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*_):pass


class StaticHTTP:
    def __init__(self,responses):self.responses=list(responses);self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs));response=self.responses.pop(0);response.url=url;return response


class PublicAudioSourceTests(unittest.TestCase):
    def setUp(self):
        self.dns=patch('public_audio_sources.socket.getaddrinfo',side_effect=lambda host,_: [(2,1,6,'',(host if host=='127.0.0.1' else '93.184.216.34',0))]);self.dns.start()
    def tearDown(self):self.dns.stop()

    def test_rss_uses_guid_as_stable_identity_and_keeps_metadata_discrepancies(self):
        xml=b'''<rss><channel><title>Quiet Feed</title><item><guid>episode-7</guid>
          <title>Ordinary ASMR</title><pubDate>Tue, 10 Nov 2026 10:00:00 +0000</pubDate>
          <enclosure url="https://cdn.example.com/file.mp3?token=temporary" length="123" type="audio/mpeg"/>
          <duration>12:34</duration></item></channel></rss>'''
        result=sources.rss_catalog(StaticHTTP([Response(xml)]),'https://feed.example.com/rss')
        self.assertEqual('episode-7',result['items'][0]['providerId'])
        self.assertEqual(123,result['items'][0]['declaredBytes'])
        self.assertEqual(754,result['items'][0]['duration'])
        self.assertNotIn('token',sources.stable_id('rss',result['identity'],'episode-7'))

    def test_rss_conditional_request_and_entity_rejection(self):
        http=StaticHTTP([Response(b'',status=304)])
        result=sources.rss_catalog(http,'https://feed.example.com/rss',validators={'etag':'"v1"'})
        self.assertTrue(result['notModified']);self.assertEqual('"v1"',http.calls[0][1]['headers']['If-None-Match'])
        with self.assertRaisesRegex(ValueError,'entities_forbidden'):
            sources.rss_catalog(StaticHTTP([Response(b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss/>')]),'https://feed.example.com/rss')

    def test_archive_groups_derivatives_by_original_and_selects_one_format(self):
        payload={'metadata':{'title':'Two cups'},'files':[
            {'name':'one.wav','format':'WAVE','source':'original','size':'100'},
            {'name':'one.mp3','format':'VBR MP3','source':'derivative','original':'one.wav','size':'50','md5':'abc'},
            {'name':'two.wav','format':'WAVE','source':'original','size':'110'},
            {'name':'two.flac','format':'Flac','source':'derivative','original':'two.wav','size':'80'}]}
        result=sources.archive_catalog(StaticHTTP([Response(json.dumps(payload).encode())]),'item-id')
        self.assertEqual(['one.wav','two.wav'],[item['providerId'] for item in result['items']])
        self.assertEqual(['.mp3','.flac'],[item['extension'] for item in result['items']])

    def test_archive_collection_requires_creator_selector_and_keeps_item_identity(self):
        identity=sources.normalize_identity('internet_archive','https://archive.org/details/opensource_audio?creator=Quiet+Reader')
        self.assertEqual('collection:opensource_audio?creator=Quiet%20Reader',identity)
        search={'response':{'numFound':1,'docs':[{'identifier':'quiet-item'}]}}
        metadata={'metadata':{'creator':'Quiet Reader'},'files':[{'name':'one.wav','format':'WAVE','source':'original'},{'name':'one.mp3','format':'VBR MP3','source':'derivative','original':'one.wav'}]}
        result=sources.archive_catalog(StaticHTTP([Response(json.dumps(search).encode()),Response(json.dumps(metadata).encode())]),identity)
        self.assertEqual('quiet-item/one.wav',result['items'][0]['providerId'])
        with self.assertRaisesRegex(ValueError,'selector_required'):
            sources.normalize_identity('internet_archive','collection:opensource_audio')

    def test_bandcamp_refreshes_expired_url_through_statdownload(self):
        tralbum={'id':12,'freeDownloadPage':'https://artist.bandcamp.com/download?id=12','trackinfo':[{'track_id':7,'title':'Quiet','duration':60}]}
        blob={'download_items':[{'downloads':{'mp3-v0':{'url':'https://artist.bandcamp.com/download/album?enc=mp3-v0'}}}]}
        pages=[Response(('<div data-tralbum="'+json.dumps(tralbum).replace('"','&quot;')+'"></div>').encode()),
               Response(('<div data-blob="'+json.dumps(blob).replace('"','&quot;')+'"></div>').encode()),
               Response(b'callback({"result":"err","retry_url":"https://files.example.com/album.zip"});')]
        http=StaticHTTP(pages)
        url=sources.bandcamp_download(http,'https://artist.bandcamp.com/album/quiet')
        self.assertEqual('https://files.example.com/album.zip',url)
        self.assertIn('/statdownload/',http.calls[-1][0])

    def test_bandcamp_download_respects_profile_formats(self):
        tralbum={'freeDownloadPage':'https://artist.bandcamp.com/download?id=12'}
        blob={'download_items':[{'downloads':{'mp3-v0':{'url':'https://artist.bandcamp.com/download/a?enc=mp3'},'flac':{'url':'https://artist.bandcamp.com/download/a?enc=flac'}}}]}
        pages=[Response(('<div data-tralbum="'+json.dumps(tralbum).replace('"','&quot;')+'"></div>').encode()),
               Response(('<div data-blob="'+json.dumps(blob).replace('"','&quot;')+'"></div>').encode()),
               Response(b'{"retry_url":"https://files.example.com/lossless.zip"}')]
        http=StaticHTTP(pages)
        self.assertEqual('https://files.example.com/lossless.zip',sources.bandcamp_download(http,'https://artist.bandcamp.com/album/a',{'.flac'}))
        self.assertIn('enc=flac',http.calls[-1][0])

    def test_zip_validation_rejects_traversal_and_matches_unique_track(self):
        with tempfile.TemporaryDirectory() as temp:
            good=Path(temp)/'good.zip'
            with zipfile.ZipFile(good,'w') as archive:
                archive.writestr('Album/01 - First Track.mp3',b'a');archive.writestr('Album/02 - Second.mp3',b'b')
            members=sources.safe_zip_members(good)
            self.assertEqual('Album/01 - First Track.mp3',sources.match_bandcamp_member(members,'First Track',1).filename)
            bad=Path(temp)/'bad.zip'
            with zipfile.ZipFile(bad,'w') as archive:archive.writestr('../escape.mp3',b'x')
            with self.assertRaisesRegex(ValueError,'archive_member_unsafe'):sources.safe_zip_members(bad)

    def test_explicit_follow_bypasses_content_filters_but_not_qa(self):
        cfg={'hypno_required':True,'hypno_keywords':['hypnosis'],'fantasy_blocklist':['elf']}
        self.assertEqual((True,'accepted_followed_creator'),scraper.title_passes('Ordinary ASMR elf story',cfg,explicitly_followed=True))
        self.assertEqual((False,'promotional_or_qa'),scraper.title_passes('Creator Q&A',cfg,explicitly_followed=True))

    def test_embedded_cover_art_is_not_treated_as_video(self):
        attached={'codec_type':'video','disposition':{'attached_pic':1}}
        motion={'codec_type':'video','disposition':{'attached_pic':0}}
        self.assertEqual([],scraper.real_video_streams({'streams':[{'codec_type':'audio'},attached]}))
        self.assertEqual([motion],scraper.real_video_streams({'streams':[{'codec_type':'audio'},motion]}))

    def test_safe_http_validates_every_redirect(self):
        delegate=StaticHTTP([Response(status=302,headers={'Location':'http://127.0.0.1/private'})])
        with self.assertRaisesRegex(ValueError,'source_address_not_public'):
            sources.SafeHTTP(delegate).get('https://example.com/start')

    def test_libsyn_fallback_is_narrow_and_preserves_query(self):
        wrapper='https://dts.podtrac.com/redirect.mp3/pscrb.fm/rss/p/traffic.libsyn.com/secure/show/episode.mp3?dest-id=7'
        self.assertEqual('https://traffic.libsyn.com/secure/show/episode.mp3?dest-id=7',sources.libsyn_fallback(wrapper))
        self.assertIsNone(sources.libsyn_fallback('https://example.com/traffic.libsyn.com/show/file.mp3'))

    def test_repair_requires_reviewed_explicit_follow_and_never_queues_during_preview(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('''
          CREATE TABLE profiles(id INTEGER PRIMARY KEY,settings TEXT);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE creators(id INTEGER PRIMARY KEY,name TEXT,profile_id INTEGER,monitored INTEGER,follow_mode TEXT,follow_mode_review INTEGER);
          CREATE TABLE assets(key TEXT PRIMARY KEY,title TEXT,published INTEGER,source TEXT,creator TEXT,state TEXT,retry_after INTEGER,error TEXT);
          CREATE TABLE media_assets(recording_key TEXT,media_kind TEXT,state TEXT,wanted INTEGER,retry_after INTEGER,error TEXT,PRIMARY KEY(recording_key,media_kind));
          CREATE TABLE blocklist(recording_key TEXT);
          CREATE TABLE source_previews(id TEXT PRIMARY KEY,identity_id INTEGER,creator_id INTEGER,kind TEXT,identity TEXT,state TEXT,complete INTEGER,discovered INTEGER,eligible INTEGER,result TEXT,created TEXT,expires TEXT);
          INSERT INTO profiles VALUES(1,'{"hypno_required":true,"hypno_keywords":["hypnosis"],"minimumDuration":600}');
          INSERT INTO creators VALUES(1,'Reader',1,1,'explicit',1);
          INSERT INTO assets VALUES('ordinary','Ordinary ASMR',0,'youtube','Reader','suppressed',0,'old filter'),('qa','Creator Q&A',0,'youtube','Reader','suppressed',0,'old filter');
          INSERT INTO media_assets VALUES('ordinary','Audio','suppressed',0,0,'old filter'),('qa','Audio','suppressed',0,0,'old filter');
        ''')
        with self.assertRaisesRegex(ValueError,'classification_required'):
            bridge.eligibility_repair_preview(db,{'creatorId':1})
        db.execute('UPDATE creators SET follow_mode_review=0')
        preview=bridge.eligibility_repair_preview(db,{'creatorId':1})
        self.assertEqual(1,preview['eligible'])
        self.assertEqual('suppressed',db.execute("SELECT state FROM assets WHERE key='ordinary'").fetchone()[0])
        result=bridge.eligibility_repair_apply(db,{'previewId':preview['previewId'],'allEligible':True})
        self.assertEqual({'Audio':1,'Video':0},result['restored'])
        self.assertEqual('pending',db.execute("SELECT state FROM assets WHERE key='ordinary'").fetchone()[0])
        self.assertEqual('suppressed',db.execute("SELECT state FROM assets WHERE key='qa'").fetchone()[0])
        db.close()

    def test_catalog_preview_is_inert_and_backfill_uses_reviewed_stable_selection(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('''
          CREATE TABLE profiles(id INTEGER PRIMARY KEY,settings TEXT);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE creators(id INTEGER PRIMARY KEY,name TEXT,profile_id INTEGER,follow_mode TEXT,follow_mode_review INTEGER);
          CREATE TABLE identities(id INTEGER PRIMARY KEY,creator_id INTEGER,kind TEXT,handle TEXT);
          CREATE TABLE source_previews(id TEXT PRIMARY KEY,identity_id INTEGER,creator_id INTEGER,kind TEXT,identity TEXT,state TEXT,complete INTEGER,discovered INTEGER,eligible INTEGER,result TEXT,created TEXT,expires TEXT);
          CREATE TABLE source_catalog(id TEXT PRIMARY KEY,identity_id INTEGER,provider_id TEXT,title TEXT,published INTEGER,duration REAL,eligible INTEGER,eligibility_reason TEXT,metadata TEXT,recording_key TEXT,discovered TEXT,updated TEXT,UNIQUE(identity_id,provider_id));
          CREATE TABLE assets(key TEXT PRIMARY KEY,url TEXT,targets TEXT,source TEXT,creator TEXT,title TEXT,published INTEGER,state TEXT);
          CREATE TABLE aliases(id TEXT PRIMARY KEY,asset_key TEXT);
          CREATE TABLE backfill_jobs(id TEXT PRIMARY KEY,creator_id INTEGER,media_kind TEXT,state TEXT,discovered INTEGER,eligible INTEGER,total INTEGER,identity_id INTEGER,preview_id TEXT,selected INTEGER,selection TEXT,downloaded INTEGER DEFAULT 0,imported INTEGER DEFAULT 0,plex_indexed INTEGER DEFAULT 0,started TEXT,updated TEXT,finished TEXT);
          INSERT INTO profiles VALUES(1,'{"minimumDuration":300,"allowedFormats":[".mp3"]}');
          INSERT INTO creators VALUES(1,'Reader',1,'explicit',0);
          INSERT INTO identities VALUES(7,1,'rss','https://feed.example.com/show.xml');
        ''')
        listing={'identity':'https://feed.example.com/show.xml','title':'Show','complete':True,'pages':1,'scope':'feed','items':[
          {'providerId':'guid-1','title':'Ordinary ASMR','duration':900,'published':1,'promotional':False},
          {'providerId':'guid-2','title':'Show Q&A','duration':900,'published':2,'promotional':True}]}
        with patch('bridge.public_audio_sources.catalog',return_value=listing):
            preview=bridge.source_preview(db,{}, {'creatorId':1,'identityId':7})
        self.assertEqual(0,db.execute('SELECT count(*) FROM assets').fetchone()[0])
        result=bridge.audio_backfill(db,{'previewId':preview['previewId'],'providerIds':['guid-1']})
        self.assertEqual(1,result['queued'])
        asset=db.execute('SELECT key FROM assets').fetchone()
        self.assertEqual(sources.stable_id('rss',listing['identity'],'guid-1'),asset['key'])
        job=db.execute('SELECT selection,downloaded,imported,plex_indexed FROM backfill_jobs').fetchone()
        self.assertEqual(['guid-1'],json.loads(job['selection']))
        self.assertEqual((0,0,0),(job['downloaded'],job['imported'],job['plex_indexed']))
        db.close()


if __name__=='__main__':unittest.main()
