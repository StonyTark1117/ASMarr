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
    def youtube_acquisition(self, *, enabled=True, legacy_channels=(), actual_channel=None):
        channel='UC'+'a'*22
        self.db.execute('CREATE TABLE identities(id INTEGER PRIMARY KEY,creator_id INTEGER,kind TEXT,handle TEXT,enabled INTEGER)')
        self.db.execute('INSERT INTO identities VALUES(1,1,?,?,?)',('youtube',channel,int(enabled)))
        self.db.execute('UPDATE assets SET targets=? WHERE key=?',(json.dumps([['youtube','https://www.youtube.com/watch?v=abcdefghijk']]),self.key))
        cfg=dict(self.cfg,youtube_enabled=True,youtube_channels=list(legacy_channels))
        info={'id':'abcdefghijk','channel_id':actual_channel or channel,'title':'ASMR sleep',
              'duration':600,'availability':'public','age_limit':0}
        source=self.audio()
        def transport(asset,effective,http):
            _,owner=s.youtube.metadata('https://www.youtube.com/watch?v=abcdefghijk',effective)
            if owner['creator']!=asset['creator']:raise s.MediaError('youtube_creator_mismatch')
            return source,'https://www.youtube.com/watch?v=abcdefghijk'
        with patch.object(s,'require_mount'),patch.object(s.youtube,'run_json',return_value=info),patch.object(s,'save_asset',side_effect=transport):
            result=b.acquire(self.db,cfg,self.key)
        return result,cfg

    def test_audio_acquisition_approves_current_gui_linked_youtube_identity(self):
        result,cfg=self.youtube_acquisition()
        self.assertEqual('complete',result['status'])
        self.assertEqual([],cfg['youtube_channels'],'Runtime approval must not rewrite protected source configuration')

    def test_disabled_youtube_identity_cannot_be_reapproved_by_legacy_configuration(self):
        with self.assertRaisesRegex(s.youtube.YouTubeError,'youtube_channel_not_approved'):
            self.youtube_acquisition(enabled=False,legacy_channels=[{'creator':'Creator','channel_id':'UC'+'a'*22}])

    def test_another_creators_configured_channel_cannot_be_used_for_this_creator(self):
        with self.assertRaisesRegex(s.youtube.YouTubeError,'youtube_channel_not_approved'):
            self.youtube_acquisition(actual_channel='UC'+'b'*22,legacy_channels=[{'creator':'Other','channel_id':'UC'+'b'*22}])

    def test_removed_identity_exclusion_cannot_be_bypassed_by_audio_acquisition(self):
        self.db.execute('CREATE TABLE creator_exclusions(name TEXT,aliases TEXT,identities TEXT,removed INTEGER)')
        self.db.execute('INSERT INTO creator_exclusions VALUES(?,?,?,0)',('Removed fixture','[]',json.dumps([{'kind':'youtube','handle':'UC'+'a'*22}])))
        with self.assertRaisesRegex(s.youtube.YouTubeError,'youtube_channel_not_approved'):
            self.youtube_acquisition(legacy_channels=[{'creator':'Creator','channel_id':'UC'+'a'*22}])

    def test_disabled_hotaudio_cannot_capture_direct_track(self):
        url='https://hotaudio.net/u/Creator/public-track'
        self.db.execute('CREATE TABLE identities(id INTEGER PRIMARY KEY,creator_id INTEGER,kind TEXT,handle TEXT,enabled INTEGER)')
        self.db.execute("INSERT INTO identities VALUES(1,1,'hotaudio','Creator',1)")
        self.db.execute("INSERT INTO settings VALUES('source.hotaudio.enabled','false')")
        self.db.execute('UPDATE assets SET source=?,targets=? WHERE key=?',
                        ('hotaudio:Creator',json.dumps([['hotaudio',url]]),self.key))
        with patch.object(s,'require_mount'),patch.object(s,'save_asset',side_effect=AssertionError('network reached')) as save:
            with self.assertRaisesRegex(ValueError,'source_disabled'):
                b.acquire(self.db,self.cfg,self.key)
        save.assert_not_called()

    def test_foreign_creator_hotaudio_identity_cannot_capture(self):
        url='https://hotaudio.net/u/Other/public-track'
        self.db.execute('CREATE TABLE identities(id INTEGER PRIMARY KEY,creator_id INTEGER,kind TEXT,handle TEXT,enabled INTEGER)')
        self.db.execute("INSERT INTO creators VALUES(2,'Other',1,1)")
        self.db.execute("INSERT INTO identities VALUES(1,2,'hotaudio','Other',1)")
        self.db.execute("INSERT INTO settings VALUES('source.hotaudio.enabled','true')")
        self.db.execute('UPDATE assets SET source=?,targets=? WHERE key=?',
                        ('hotaudio:Other',json.dumps([['hotaudio',url]]),self.key))
        with patch.object(s,'require_mount'),patch.object(s,'save_asset',side_effect=AssertionError('network reached')) as save:
            with self.assertRaisesRegex(ValueError,'source_identity_not_approved'):
                b.acquire(self.db,self.cfg,self.key)
        save.assert_not_called()

    def test_reddit_origin_hotaudio_target_cannot_capture(self):
        url='https://hotaudio.net/u/Creator/public-track'
        self.db.execute('UPDATE assets SET source=?,targets=? WHERE key=?',
                        ('reddit:Creator',json.dumps([['hotaudio',url]]),self.key))
        with patch.object(s,'require_mount'),patch.object(s,'save_asset',side_effect=AssertionError('network reached')) as save:
            with self.assertRaisesRegex(ValueError,'source_target_not_approved'):
                b.acquire(self.db,self.cfg,self.key)
        save.assert_not_called()

    def test_reddit_soundgasm_fallback_survives_hotaudio_filter(self):
        url='https://hotaudio.net/u/Creator/public-track'
        self.db.execute('UPDATE assets SET source=?,targets=? WHERE key=?',
                        ('reddit:Creator',json.dumps([['hotaudio',url],['soundgasm',self.key]]),self.key))
        def inspect(asset,effective,http):
            self.assertEqual(json.loads(asset['targets']),[['soundgasm',self.key]])
            self.assertEqual(effective['_authorized_creator_targets'],[])
            raise ValueError('fixture_stop')
        with patch.object(s,'require_mount'),patch.object(s,'save_asset',side_effect=inspect):
            with self.assertRaisesRegex(ValueError,'fixture_stop'):
                b.acquire(self.db,self.cfg,self.key)
    def test_shadow_cycle_release_identity_comes_from_runtime_manifest(self):
        identity=self.root/'deployed-release-runtime.json'
        identity.write_text(json.dumps({'sourceCommit':'a'*40,'artifactSha256':'b'*64}))
        with patch.object(b,'STATE',self.root):
            self.assertEqual(b.deployed_release_identity(),{'sourceCommit':'a'*40,'artifactSha256':'b'*64})
        identity.write_text('{invalid')
        with patch.object(b,'STATE',self.root):
            self.assertEqual(b.deployed_release_identity(),{'sourceCommit':None,'artifactSha256':None})
    def test_completed_import_is_verified_and_idempotent(self):
        source=self.audio();checksum=b.digest(source)
        with patch.object(s,'require_mount'):
            first=b.import_audio(self.db,self.cfg,self.key,str(source));second=b.import_audio(self.db,self.cfg,self.key,str(source))
        self.assertEqual(first['sha256'],checksum);self.assertEqual(second['status'],'already_imported')
        self.assertEqual(b.digest(Path(first['path'])),checksum)
        self.assertTrue(source.exists());self.assertEqual(self.db.execute('SELECT count(*) FROM downloads').fetchone()[0],1)
        self.assertEqual(s.meta_get(self.db,'plex_pending_paths'),[first['path']])
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT source FROM downloads').fetchone()[0],'qbittorrent')
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
    def test_video_master_derivation_tags_audio_and_records_correct_provenance(self):
        import mutagen
        source=self.downloads/'master.mkv'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=160x120:d=1',
                        '-f','lavfi','-i','sine=frequency=220:duration=1','-shortest',
                        '-c:v','libx264','-c:a','libopus','-y',str(source)],check=True)
        cfg=dict(self.cfg,video_root=str(self.root/'video'))
        with patch.object(s,'require_mount'):
            result=b.video_media.import_video(self.db,cfg,self.key,source,
                     {'provider':'youtube','provider_id':'abcdefghijk','published':1735776000},b.import_derived_audio)
        self.assertEqual(result['status'],'imported')
        audio=Path(result['derivedAudio']['path']);tags=mutagen.File(str(audio),easy=True)
        self.assertEqual(tags['artist'],['Creator'])
        self.assertEqual(tags['albumartist'],['Creator'])
        self.assertEqual(tags['title'],['Sleep hypnosis'])
        self.assertEqual(tags['album'],['Singles'])
        self.assertEqual(tags['date'],['2025-01-02'])
        self.assertEqual(self.db.execute('SELECT source FROM downloads').fetchone()[0],'video-master')
        event=json.loads(self.db.execute("SELECT details FROM history WHERE event='import'").fetchone()[0])
        self.assertEqual((event['provider'],event['mediaKind']),('video-master','Audio'))
        hashes=[]
        for path in (source,audio):
            p=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(path),'-map','0:a:0',
                              '-c','copy','-f','hash','-hash','sha256','-'],capture_output=True,text=True,check=True)
            hashes.append(p.stdout)
        self.assertEqual(hashes[0],hashes[1])
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue').fetchone()[0],0)

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
        with patch.object(b,'search',return_value={'candidates':[{'autoGrab':False}]}) as search,patch.object(b,'grab') as grab,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            b.process_queue(self.db,self.cfg)
            b.process_queue(self.db,self.cfg)
            grab.assert_not_called()
            search.assert_called_once()
        self.assertEqual(self.db.execute('SELECT event FROM history').fetchone()[0],'search-review')
        self.assertGreater(self.db.execute('SELECT retry_after FROM assets').fetchone()[0],0)

    def test_pending_direct_work_precedes_newer_failed_fallback(self):
        self.db.execute("UPDATE assets SET state='failed',attempts=2,retry_after=0,published=200")
        second='https://soundgasm.net/u/Creator/second'
        s.enqueue(self.db,'sg:second','soundgasm:Creator','Creator','Second',[['soundgasm',second]],published=100)
        with patch.object(b,'acquire',return_value={'status':'complete'}) as acquire,patch.object(b,'search') as search,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            result=b.process_queue(self.db,self.cfg)
        acquire.assert_called_once_with(self.db,self.cfg,second)
        search.assert_not_called()
        self.assertEqual(len(result['saved']),1)
    def test_fallback_timeout_backs_off_without_aborting_other_acquisitions(self):
        self.db.execute("UPDATE assets SET state='failed',attempts=2,retry_after=0,published=2")
        second='https://soundgasm.net/u/Creator/second'
        s.enqueue(self.db,'sg:second','soundgasm:Creator','Creator','Second',[['soundgasm',second]],published=1)
        with patch.object(b,'search',side_effect=requests.ReadTimeout('https://fixture.invalid/?apikey=fixture-secret')),patch.object(b,'acquire',return_value={'status':'complete'}) as acquire,patch.object(b,'monitor_downloads',return_value={'status':'idle'}) as monitor:
            result=b.process_queue(self.db,dict(self.cfg,max_downloads_per_run=2))
        acquire.assert_called_once_with(self.db,dict(self.cfg,max_downloads_per_run=2),second)
        monitor.assert_called_once()
        row=self.db.execute('SELECT attempts,retry_after,error FROM assets WHERE key=?',(self.key,)).fetchone()
        self.assertEqual(row['attempts'],2)
        self.assertGreater(row['retry_after'],0)
        self.assertEqual(row['error'],'fallback_ReadTimeout')
        self.assertEqual(len(result['saved']),1)
        self.assertEqual(result['failed'],[{'key':self.key,'error':'fallback_ReadTimeout'}])
        history=self.db.execute('SELECT event,details FROM history').fetchone()
        self.assertEqual(history['event'],'fallback-failure')
        self.assertNotIn('fixture-secret',history['details'])
    def test_fallback_submission_timeout_preserves_durable_job_and_does_not_resubmit(self):
        self.db.execute("UPDATE assets SET state='failed',attempts=2,retry_after=0")
        candidate={'guid':'fixture','indexerId':7,'autoGrab':True}
        def uncertain_submission(*args):
            self.db.execute("INSERT INTO queue VALUES('q',?,'hash','submitting','qbittorrent','{}','date','Audio')",(self.key,))
            self.db.commit()
            raise requests.ReadTimeout('fixture transport uncertainty')
        with patch.object(b,'search',return_value={'candidates':[candidate]}) as search,patch.object(b,'grab',side_effect=uncertain_submission) as grab,patch.object(b,'monitor_downloads',return_value={'status':'idle'}):
            first=b.process_queue(self.db,self.cfg)
            second=b.process_queue(self.db,self.cfg)
        self.assertEqual(len(first['failed']),1)
        self.assertEqual(second['failed'],[])
        self.assertEqual(search.call_count,1);self.assertEqual(grab.call_count,1)
        self.assertEqual(self.db.execute('SELECT state FROM queue').fetchone()[0],'submitting')
        self.assertEqual(self.db.execute('SELECT attempts FROM assets').fetchone()[0],2)
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
