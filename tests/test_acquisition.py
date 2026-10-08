"""Recorded-shape service fixtures exercise transfers and outage recovery offline."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import requests
import bridge as b
import scraper as s


class Response:
    def __init__(self,value=None,text='',status=200):self.value=value;self.text=text;self.status_code=status
    def json(self):return self.value
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError('fixture_http_'+str(self.status_code))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.library=self.root/'library';self.library.mkdir();self.downloads=self.root/'downloads';self.downloads.mkdir()
        self.db=s.open_db(self.root/'app.db')
        self.db.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE creators(id INTEGER PRIMARY KEY,name TEXT UNIQUE,monitored INTEGER,profile_id INTEGER);
        CREATE TABLE profiles(id INTEGER PRIMARY KEY,settings TEXT);
        CREATE TABLE history(id INTEGER PRIMARY KEY,at TEXT,event TEXT,recording_key TEXT,details TEXT);
        CREATE TABLE queue(id TEXT PRIMARY KEY,recording_key TEXT,download_id TEXT UNIQUE,state TEXT,provider TEXT,details TEXT,created TEXT,media_kind TEXT DEFAULT 'Audio');
        CREATE TABLE media_assets(recording_key TEXT,media_kind TEXT,wanted INTEGER DEFAULT 1,state TEXT DEFAULT 'wanted',saved_path TEXT,source_url TEXT,provider_id TEXT,attempts INTEGER DEFAULT 0,retry_after INTEGER DEFAULT 0,error TEXT,acquired INTEGER,details TEXT DEFAULT '{}',PRIMARY KEY(recording_key,media_kind));
        CREATE TABLE blocklist(id INTEGER PRIMARY KEY,recording_key TEXT,download_id TEXT,reason TEXT,created TEXT);''')
        self.db.executemany('INSERT INTO settings VALUES(?,?)',[('mode','production'),('root',str(self.library)),('naming','{Creator}/Singles/{Title} [{SourceId}].{ext}')])
        self.db.execute("INSERT INTO creators VALUES(1,'Creator',1,1)")
        self.db.execute('INSERT INTO profiles VALUES(1,?)',(json.dumps({'minimumDuration':0,'directRetries':2,'fallback':True}),))
        self.key='https://soundgasm.net/u/Creator/sleep';s.enqueue(self.db,'sg:sleep','soundgasm:Creator','Creator','Sleep hypnosis',[['soundgasm',self.key]])
        self.cfg={'output_root':str(self.library),'download_root':str(self.downloads),'max_downloads_per_run':1}
        self.db.commit()
    def tearDown(self):self.db.close();self.temp.cleanup()
    def audio(self,path=None):
        path=path or self.downloads/'audio.m4a'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=220:duration=1','-c:a','aac','-y',str(path)],check=True)
        return path
    def test_completed_import_is_verified_and_idempotent(self):
        source=self.audio();checksum=b.digest(source)
        with patch.object(s,'require_mount'):
            first=b.import_audio(self.db,self.cfg,self.key,str(source));second=b.import_audio(self.db,self.cfg,self.key,str(source))
        self.assertEqual(first['sha256'],checksum);self.assertEqual(second['status'],'already_imported')
        self.assertEqual(b.digest(Path(first['path'])),checksum)
        self.assertTrue(source.exists());self.assertEqual(self.db.execute('SELECT count(*) FROM downloads').fetchone()[0],1)
        self.assertEqual(s.meta_get(self.db,'plex_pending_paths'),[first['path']])
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],1)
    def test_corrupt_completed_audio_stays_unimported(self):
        path=self.downloads/'corrupt.m4a';path.write_bytes(b'invalid-audio')
        with patch.object(s,'require_mount'),self.assertRaises(s.MediaError):b.import_audio(self.db,self.cfg,self.key,str(path))
        self.assertEqual(self.db.execute('SELECT state FROM assets').fetchone()[0],'pending');self.assertTrue(path.exists())
    def test_interrupted_copy_preserves_source_and_cleans_partial(self):
        source=self.audio()
        with patch.object(s,'require_mount'),patch.object(b.shutil,'copyfileobj',side_effect=OSError('interrupted')),self.assertRaises(OSError):b.import_audio(self.db,self.cfg,self.key,str(source))
        self.assertTrue(source.exists());self.assertFalse(list(self.library.rglob('*.asmarr-part')))
        self.assertEqual(self.db.execute('SELECT state FROM assets').fetchone()[0],'pending')
    def test_import_rejects_other_roots_and_escaping_symlinks(self):
        path=self.audio(self.root/'outside.m4a')
        with patch.object(s,'require_mount'),self.assertRaises(ValueError):b.import_audio(self.db,self.cfg,self.key,str(path))
        symlink=self.downloads/'linked.m4a';symlink.symlink_to(path)
        with patch.object(s,'require_mount'),self.assertRaises(ValueError):b.import_audio(self.db,self.cfg,self.key,str(symlink))
    def test_direct_outage_retries_before_fallback(self):
        with patch.object(b,'acquire',side_effect=s.SourceError('http_503')),patch.object(b,'search') as search,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            result=b.process_queue(self.db,self.cfg)
            search.assert_not_called();self.assertEqual(len(result['failed']),1)
        row=self.db.execute('SELECT attempts,retry_after,state FROM assets').fetchone()
        self.assertEqual(row['attempts'],1);self.assertEqual(row['state'],'failed');self.assertGreater(row['retry_after'],0)

    def test_active_video_job_does_not_block_audio_acquisition(self):
        self.db.execute("INSERT INTO queue VALUES('video',?,'video-hash','downloading','qbittorrent','{}','date','Video')",(self.key,))
        with patch.object(b,'acquire',return_value={'status':'complete'}) as acquire,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            result=b.process_queue(self.db,self.cfg)
        acquire.assert_called_once_with(self.db,self.cfg,self.key)
        self.assertEqual(result['saved'],[{'status':'complete'}])

    def test_active_audio_job_still_prevents_duplicate_acquisition(self):
        self.db.execute("INSERT INTO queue VALUES('audio',?,'audio-hash','downloading','qbittorrent','{}','date','Audio')",(self.key,))
        with patch.object(b,'acquire') as acquire,patch.object(b,'search') as search,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            result=b.process_queue(self.db,self.cfg)
        acquire.assert_not_called();search.assert_not_called()
        self.assertEqual(result['saved'],[])
    def test_terminal_direct_retry_enters_ranked_fallback(self):
        self.db.execute("UPDATE assets SET state='failed',attempts=2,retry_after=0")
        candidate={'guid':'fixture','indexerId':7,'autoGrab':True}
        with patch.object(b,'acquire') as acquire,patch.object(b,'search',return_value={'candidates':[candidate]}),patch.object(b,'grab',return_value={'downloadId':'hash'}) as grab,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            result=b.process_queue(self.db,self.cfg)
            acquire.assert_not_called();grab.assert_called_once_with(self.db,self.key,candidate);self.assertEqual(len(result['fallback']),1)
    def test_ambiguous_fallback_requires_manual_search(self):
        self.db.execute("UPDATE assets SET state='failed',attempts=2,retry_after=0")
        with patch.object(b,'search',return_value={'candidates':[{'autoGrab':False}]}),patch.object(b,'grab') as grab,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            b.process_queue(self.db,self.cfg);grab.assert_not_called()
        self.assertEqual(self.db.execute('SELECT event FROM history').fetchone()[0],'search-review')
    def test_qbittorrent_outage_preserves_job(self):
        self.db.execute("INSERT INTO queue VALUES('q',?,'hash','downloading','qbittorrent','{}','date','Audio')",(self.key,))
        with patch.object(b,'qbit',side_effect=requests.ConnectionError('offline')),self.assertRaises(requests.ConnectionError):b.monitor_downloads(self.db,self.cfg)
        self.assertEqual(self.db.execute('SELECT state FROM queue').fetchone()[0],'downloading')

    def test_audio_and_video_download_jobs_are_independent(self):
        self.db.execute("INSERT INTO queue VALUES('audio',?,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','downloading','qbittorrent','{}','date','Audio')",(self.key,))
        candidate={'guid':'video-fixture','indexerId':7,'magnetUrl':'magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'}
        s.meta_set(self.db,'search_results:Video:'+self.key,[candidate])
        class Session:
            def get(self,url,**kwargs):
                return Response({'asmarr':{}}) if url.endswith('/categories') else Response([])
            def post(self,*args,**kwargs):return Response(text='Ok.')
        with patch.object(b,'qbit',return_value=(Session(),'http://fixture',{})):
            result=b.grab(self.db,self.key,{'guid':'video-fixture','indexerId':7},'Video')
            duplicate=b.grab(self.db,self.key,{'guid':'video-fixture','indexerId':7},'Video')
        self.assertEqual((result['status'],result['key'],result['mediaKind']),
                         ('downloading',self.key,'Video'))
        self.assertEqual((duplicate['status'],duplicate['key'],duplicate['mediaKind']),
                         ('downloading',self.key,'Video'))
        self.assertEqual(dict(self.db.execute('SELECT media_kind,count(*) FROM queue GROUP BY media_kind')),
                         {'Audio':1,'Video':1})
        self.assertEqual(tuple(self.db.execute("SELECT state,wanted FROM media_assets WHERE media_kind='Video'").fetchone()),
                         ('downloading',1))

    def test_failed_video_torrent_import_updates_independent_asset_state(self):
        folder=self.downloads/'ambiguous';folder.mkdir()
        (folder/'first.mp4').write_bytes(b'first');(folder/'second.mkv').write_bytes(b'second')
        b.video_media.ensure_asset_rows(self.db,self.key,video_wanted=True)
        self.db.execute("UPDATE media_assets SET state='downloading' WHERE recording_key=? AND media_kind='Video'",(self.key,))
        self.db.execute("INSERT INTO queue VALUES('video',?,'hash','downloading','qbittorrent','{}','date','Video')",(self.key,))
        class Session:
            def get(self,*args,**kwargs):return Response([{'category':'asmarr','progress':1,'state':'uploading','content_path':str(folder)}])
        with patch.object(b,'qbit',return_value=(Session(),'http://fixture',{})):
            result=b.monitor_downloads(self.db,self.cfg)
        self.assertEqual((result['jobs'][0]['status'],result['jobs'][0]['key'],result['jobs'][0]['mediaKind']),
                         ('failed',self.key,'Video'))
        state=self.db.execute("SELECT state,wanted,attempts,retry_after,error FROM media_assets WHERE media_kind='Video'").fetchone()
        self.assertEqual(state[:3],('failed',1,1));self.assertGreater(state[3],0)
        self.assertEqual(state[4],'completed_torrent_media_ambiguous')

    def test_terminal_video_torrent_import_is_unavailable_not_retrying(self):
        source=self.downloads/'private.mp4';source.write_bytes(b'fixture')
        b.video_media.ensure_asset_rows(self.db,self.key,video_wanted=True)
        self.db.execute("UPDATE media_assets SET state='downloading' WHERE recording_key=? AND media_kind='Video'",(self.key,))
        self.db.execute("INSERT INTO queue VALUES('video',?,'hash','downloading','qbittorrent','{}','date','Video')",(self.key,))
        class Session:
            def get(self,*args,**kwargs):return Response([{'category':'asmarr','progress':1,'state':'uploading','content_path':str(source)}])
        with patch.object(b,'qbit',return_value=(Session(),'http://fixture',{})),patch.object(b.video_media,'import_video',side_effect=b.video_media.VideoUnavailable('video_source_unavailable')):
            result=b.monitor_downloads(self.db,self.cfg)
        self.assertEqual(result['jobs'][0]['status'],'unavailable')
        self.assertEqual(tuple(self.db.execute("SELECT state,wanted,retry_after,error FROM media_assets WHERE media_kind='Video'").fetchone()),
                         ('unavailable',1,0,'video_source_unavailable'))
    def test_qbittorrent_removal_happens_after_verified_import(self):
        source=self.audio();self.db.execute("INSERT INTO queue VALUES('q',?,'hash','downloading','qbittorrent','{}','date','Audio')",(self.key,))
        test=self
        class Session:
            def get(self,*args,**kwargs):return Response([{'hash':'hash','category':'asmarr','progress':1,'state':'uploading','content_path':str(source)}])
            def post(self,url,**kwargs):
                row=test.db.execute('SELECT saved_path,state FROM assets').fetchone();test.assertEqual(row['state'],'complete');test.assertTrue(Path(row['saved_path']).exists())
                test.assertEqual(kwargs['data']['deleteFiles'],'false');return Response()
        with patch.object(s,'require_mount'),patch.object(b,'qbit',return_value=(Session(),'http://fixture',{'retention':'remove-torrent'})):
            result=b.monitor_downloads(self.db,self.cfg)
        self.assertEqual((result['jobs'][0]['status'],result['jobs'][0]['key'],result['jobs'][0]['mediaKind']),
                         ('complete',self.key,'Audio'));self.assertEqual(self.db.execute('SELECT state FROM queue').fetchone()[0],'removed');self.assertTrue(source.exists())
    def test_external_client_category_is_never_imported(self):
        self.db.execute("INSERT INTO queue VALUES('q',?,'hash','downloading','qbittorrent','{}','date','Audio')",(self.key,))
        class Session:
            def get(self,*a,**k):return Response([{'category':'radarr','progress':1}])
        with patch.object(b,'qbit',return_value=(Session(),'http://fixture',{})):
            result=b.monitor_downloads(self.db,self.cfg)
        self.assertEqual(result['jobs'][0]['status'],'category_mismatch');self.assertEqual(self.db.execute('SELECT state FROM assets').fetchone()[0],'pending')


if __name__=='__main__':unittest.main()
