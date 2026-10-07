import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import bridge as b
import scraper as s
import sfw_sources
import youtube_source
from categories import classify


class ProviderTests(unittest.TestCase):
    def test_normalization_and_exact_creator_boundaries(self):
        self.assertEqual(b.normalize('<b>Creator</b> — Sleep!'),'creator sleep')
        asset={'creator':'Ann','title':'Sleep hypnosis','published':0}
        valid=b.rank(asset,{'title':'Ann Sleep Hypnosis FLAC','size':1000},{})
        self.assertTrue(valid['autoGrab'])
        invalid=b.rank(asset,{'title':'Joanna Sleep Hypnosis FLAC','size':1000},{})
        self.assertFalse(invalid['exactCreator']);self.assertFalse(invalid['autoGrab'])

    def test_ambiguous_title_never_auto_grabs(self):
        asset={'creator':'Creator','title':'Deep sleep hypnosis','published':0}
        candidate=b.rank(asset,{'title':'Creator sleep MP3','size':1000},{})
        self.assertFalse(candidate['autoGrab'])

    def test_torrent_immutable_identity(self):
        import hashlib
        info=b'd6:lengthi123e4:name5:audioe'
        torrent=b'd4:info'+info+b'e'
        self.assertEqual(b.torrent_hash(torrent),hashlib.sha1(info).hexdigest())
        with self.assertRaises(ValueError):b.torrent_hash(b'not a torrent')

    def test_naming_contains_untrusted_titles(self):
        with tempfile.TemporaryDirectory() as root:
            db=sqlite3.connect(':memory:');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT INTO settings VALUES(?,?)',('root',root))
            asset={'creator':'../../Creator','title':'../Title','key':'https://source/item'}
            out=b.naming(db,asset,'.m4a');self.assertTrue(out.resolve().is_relative_to(Path(root)))
            db.execute('INSERT INTO settings VALUES(?,?)',('naming','../../outside/{Title}.{ext}'))
            with self.assertRaises(ValueError):b.naming(db,asset,'.m4a')

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as outside:
            (Path(root)/'Creator').symlink_to(outside,target_is_directory=True)
            db=sqlite3.connect(':memory:');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)');db.execute('INSERT INTO settings VALUES(?,?)',('root',root))
            with self.assertRaises(ValueError):b.naming(db,{'creator':'Creator','title':'Title','key':'id'},'.mp3')

    def test_shadow_mutations_are_denied(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO settings VALUES('mode','shadow')")
        for operation in [lambda:b.production(db),lambda:b.acquire(db,{},'key'),lambda:b.grab(db,'key',{}),lambda:b.import_audio(db,{},'key','/path'),lambda:b.process_queue(db,{})]:
            with self.assertRaises(ValueError):operation()

    def test_recorded_responses_omit_oauth_tokens(self):
        import requests
        response=requests.Response();response.status_code=200;response._content=b'{"access_token":"sensitive-example"}'
        capture=b.CaptureHTTP()
        with patch.object(s.HTTP,'request',return_value=response):capture.request('POST','https://www.reddit.com/api/v1/access_token',auth=('id','secret'))
        self.assertNotIn('sensitive-example',json.dumps(capture.entries));self.assertNotIn('secret',json.dumps(capture.entries))
        replay=capture.replay();self.assertEqual(replay.request('POST','https://www.reddit.com/api/v1/access_token').json()['access_token'],'fixture-token')

    def test_sfw_qualification_and_auto_enrollment(self):
        cfg={'fantasy_blocklist':[],'sfw_expansion':{'categories':['sleep']}}
        self.assertTrue(sfw_sources.qualify('[F4M] [SFW] sleep',cfg,s,profile=True)[0])
        self.assertFalse(sfw_sources.qualify('[F4M] sleep',cfg,s,profile=True)[0])
        self.assertFalse(sfw_sources.qualify('[F4M] [SFW] explicit sleep',cfg,s,profile=True)[0])
        records={str(i):{'published':i*86400*10,'eligible':True,'owner':'Creator'} for i in range(5)}
        self.assertTrue(sfw_sources.auto_candidate(records,'Creator',{}))
        records['0']['owner']='unrelated';self.assertFalse(sfw_sources.auto_candidate(records,'Creator',{}))

    def test_youtube_identity_duration_and_publication(self):
        self.assertEqual(s.canonical('https://youtu.be/abcdefghijk?t=30'),'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertFalse(youtube_source.eligibility({'title':'Sleep','duration':179},{})[0])
        self.assertFalse(youtube_source.eligibility({'title':'Sleep','duration':500,'availability':'subscriber_only'},{})[0])
        self.assertFalse(youtube_source.eligibility({'title':'Sleep','duration':500,'is_live':True},{})[0])
        self.assertTrue(youtube_source.eligibility({'title':'Sleep','duration':500,'availability':'public'},{})[0])

    def test_category_classification_ignores_generated_tags(self):
        import xml.etree.ElementTree as ET
        from categories import metadata_text
        track=ET.fromstring('<Track title="ordinary conversation"><Mood tag="ASMR: Sleep"/><Mood tag="Unrelated"/></Track>')
        self.assertNotIn('sleep',classify(metadata_text(track)))

    def test_plex_smart_playlist_filter_has_exact_scope(self):
        import plex_playlists
        from urllib.parse import urlencode
        uri='/library/sections/4/all?'+urlencode({'type':10,'track.mood':'ASMR: Sleep','sort':'addedAt:desc'})
        self.assertTrue(plex_playlists.filter_matches(uri,'4','ASMR: Sleep'))
        self.assertFalse(plex_playlists.filter_matches(uri,'5','ASMR: Sleep'))
        self.assertFalse(plex_playlists.filter_matches(uri,'4','ASMR: Hypnosis'))


if __name__=='__main__':unittest.main()
