import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import bridge as b
import creator_audio_sources
import scraper as s
import sfw_sources
import youtube_source
from categories import classify


class ProviderTests(unittest.TestCase):
    def test_monitored_creator_audio_identities_enqueue_owned_recordings(self):
        with tempfile.TemporaryDirectory() as root:
            db=s.open_db(Path(root)/'state.db')
            db.executescript('''CREATE TABLE creators(id INTEGER PRIMARY KEY,name TEXT,monitored INTEGER,profile_id INTEGER);
                CREATE TABLE identities(id INTEGER PRIMARY KEY,creator_id INTEGER,kind TEXT,handle TEXT,enabled INTEGER);
                INSERT INTO creators VALUES(1,'Whyp Artist',1,1),(2,'Audiochan Artist',1,1),
                    (3,'Hotaudio Artist',1,1),(4,'Unmonitored Artist',0,1);
                INSERT INTO identities VALUES(1,1,'whyp','@whypartist',1),
                    (2,2,'audiochan','audioartist',1),(3,3,'hotaudio','hotartist',1),
                    (4,4,'whyp','@inactive',1);''')
            cfg=b.configured_discovery(db,{'creator_aliases':{},'allowlist':[],
                'soundgasm_creators':[],'sfw_expansion':{'creators':[]},'hypno_required':False})
            urls={'whyp':'https://whyp.it/tracks/sleep-ABC123',
                  'audiochan':'https://audiochan.com/a/ABC123',
                  'hotaudio':'https://hotaudio.net/u/hotartist/sleep'}
            owners={'whyp':'Whyp Artist','audiochan':'Audiochan Artist','hotaudio':'Hotaudio Artist'}
            def listing(_http,kind,handle):
                self.assertEqual(handle,{'whyp':'@whypartist','audiochan':'audioartist',
                                         'hotaudio':'hotartist'}[kind])
                return [{'id':kind+'-id','title':'[F4M] Sleep hypnosis',
                         'url':urls[kind],'published':123}]
            with patch.object(creator_audio_sources,'profile_listing',side_effect=listing) as profiles:
                reports=s.discover(db,cfg,{},None,['whyp','audiochan','hotaudio'])
            self.assertEqual(profiles.call_count,3)
            self.assertEqual([report['items']['queued'] for report in reports],[1,1,1])
            rows=db.execute('SELECT source,creator,key,published FROM assets ORDER BY source').fetchall()
            self.assertEqual([(row['source'],row['creator'],row['key'],row['published']) for row in rows],
                [(kind+':'+handle,owners[kind],urls[kind],123) for kind,handle in
                 [('audiochan','audioartist'),('hotaudio','hotartist'),('whyp','@whypartist')]])
            db.close()

    def test_linked_soundgasm_identity_uses_owning_creator_and_profile(self):
        with tempfile.TemporaryDirectory() as root:
            db=s.open_db(Path(root)/'state.db');seen=[]
            def rules(creator):
                seen.append(creator)
                return {'allowedSpeakers':['M'],'allowedAudiences':['F'],'requiredTopics':['nap']}
            cfg={'soundgasm_creators':['SharedHandle'],'creator_aliases':{'SharedHandle':'Reddit Creator'},
                 '_identity_creators':{'soundgasm:sharedhandle':'Soundgasm Creator','reddit:sharedhandle':'Reddit Creator'},
                 '_profile_rules':rules}
            post={'url':'https://soundgasm.net/u/SharedHandle/Nap','title':'[M4F] Nap','description':''}
            with patch.object(s,'sg_listing',return_value=[post]):
                reports=s.discover(db,cfg,{},None,['soundgasm'])
            self.assertEqual(seen,['Soundgasm Creator'])
            self.assertEqual(db.execute('SELECT creator FROM assets').fetchone()[0],'Soundgasm Creator')
            self.assertEqual(reports[0]['accepted'],1)
            db.close()

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
