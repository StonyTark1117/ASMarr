import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import scraper as s

class Response:
    def __init__(self, text='', data=None, chunks=()):
        self.text, self.data, self.chunks = text, data, chunks
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def json(self): return self.data
    def iter_content(self,n): yield from self.chunks

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = s.open_db(self.root/'state.db')
        self.cfg = {'output_root':str(self.root/'media'),'hypno_keywords':['hypno','trance'],
                    'fantasy_blocklist':['elf','orc','werewolf','monster girl']}
        Path(self.cfg['output_root']).mkdir()
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def sample(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=1',
                        '-c:a','aac','-y',str(path)],check=True)
    def enqueue(self,url='https://soundgasm.net/u/a/one',sid='sg:one',title='[F4M] hypnosis yourself'):
        return s.enqueue(self.db,sid,'soundgasm:a','a',title,[['soundgasm',url]])
    def test_filter_boundaries_and_policy(self):
        for title in ['[F4M] hypnosis for yourself','[F4M] trance reinforcement',
                      '[F4M] hypnosis and self care','<b>[F4A]</b> hypnosis &amp; rest']:
            self.assertTrue(s.title_passes(title,self.cfg)[0],title)
        for title in ['[F4M] hypnosis elf','[F4M] hypnosis orcs','[F4M] hypnosis elves',
                      '[F4M] hypnosis werewolves','[F4M] hypnosis monster girls',
                      '[M4F] hypnosis','[F4F] hypnosis','hypnosis','[F4M] everyday chat']:
            self.assertFalse(s.title_passes(title,self.cfg)[0],title)
    def test_reddit_images_are_not_audio_candidates(self):
        self.assertEqual(s.targets({'url':'https://i.redd.it/picture.jpeg'},{}),[])
        self.assertEqual(s.targets({'url':'https://soundgasm.net/u/a'},{}),[])
        self.assertEqual(s.targets({'url':'https://i.redd.it/picture.gif','selftext':'https://soundgasm.net/u/a'},{}),[])
        result=s.targets({'url':'https://i.redd.it/picture.jpeg','selftext':'Listen at https://soundgasm.net/u/a/track'}, {})
        self.assertEqual(result,[['soundgasm','https://soundgasm.net/u/a/track']])
    def test_soundgasm_html_and_empty_detection(self):
        class Http:
            def get(self,*a,**kw):return Response('<div class="sound-details"><a href="https://soundgasm.net/u/a/one"><b>[F4M]</b> hypnosis</a><span class="soundDescription">relax &amp; rest</span></div>')
        items=s.sg_listing(Http(),'a')
        self.assertEqual(len(items),1);self.assertEqual(items[0]['title'],'[F4M] hypnosis')
        self.assertEqual(items[0]['description'],'relax & rest')
        with patch.object(Http,'get',return_value=Response('<html>captcha</html>')):
            with self.assertRaises(s.SourceError):s.sg_listing(Http(),'a')
    def test_cross_source_dedup_and_canonical_url(self):
        self.assertEqual(self.enqueue(),'queued')
        self.assertEqual(s.enqueue(self.db,'reddit:x','reddit:test','a','title',
            [['soundgasm','http://www.soundgasm.net/u/a/one/']]),'known_pending')
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0],1)
    def test_reconcile_only_existing_valid_moved_files(self):
        old=Path(self.cfg['output_root'])/'a'/'track.m4a';moved=old.parent/'Singles'/old.name
        self.sample(moved)
        self.db.execute('insert into downloads values (?,?,?,?,?,?,?)',('x','soundgasm','https://soundgasm.net/u/a/one','a','t',str(old),1));self.db.commit()
        self.assertEqual(s.reconcile(self.db,self.cfg['output_root']),{'moved':1,'missing':0,'applied':False})
        self.assertEqual(self.db.execute('select saved_path from downloads').fetchone()[0],str(old))
        s.migrate(self.db)
        self.assertEqual(self.db.execute('select state from assets').fetchone()[0],'missing')
        s.reconcile(self.db,self.cfg['output_root'],True)
        self.assertEqual(self.db.execute('select saved_path from assets').fetchone()[0],str(moved))
    def test_missing_and_suppressed_are_not_downloaded(self):
        self.enqueue();self.db.execute("update assets set state='suppressed'")
        self.assertEqual(self.enqueue(),'known_suppressed')
        self.assertEqual(s.download_pending(self.db,self.cfg,None,10),([],[]))
        self.db.execute("update assets set state='complete',saved_path=?",(str(self.root/'gone'),))
        self.assertEqual(self.enqueue(),'missing')
        self.assertEqual(s.download_pending(self.db,self.cfg,None,10),([],[]))
    def test_success_idempotence_tags_and_distinct_titles(self):
        source=self.root/'source.m4a';self.sample(source);media=source.read_bytes()
        class Http:
            def get(self,url,**kw):
                return Response(chunks=[media]) if kw.get('stream') else Response('m4a: "https://media.soundgasm.net/one.m4a"')
        self.enqueue();self.enqueue('https://soundgasm.net/u/a/two','sg:two')
        saved,failed=s.download_pending(self.db,self.cfg,Http(),10)
        self.assertEqual(len(saved),2);self.assertFalse(failed);self.assertNotEqual(saved[0],saved[1])
        self.assertEqual(s.mutagen.File(saved[0],easy=True)['artist'],['a'])
        self.assertTrue(s.meta_get(self.db,'plex_pending'))
        self.assertEqual(s.download_pending(self.db,self.cfg,Http(),10),([],[]))
    def test_invalid_media_not_completed_and_retry_delay(self):
        class Http:
            def get(self,url,**kw):
                return Response(chunks=[b'<html>error</html>']) if kw.get('stream') else Response('m4a: "https://media.soundgasm.net/one.m4a"')
        self.enqueue();saved,failed=s.download_pending(self.db,self.cfg,Http(),1)
        self.assertFalse(saved);self.assertEqual(len(failed),1)
        self.assertEqual(self.db.execute('select state from assets').fetchone()[0],'failed')
        self.assertEqual(s.download_pending(self.db,self.cfg,Http(),1),([],[]))
        self.assertFalse(list(Path(self.cfg['output_root']).rglob('*.part')))
    def test_interrupted_download_stays_retryable(self):
        class Broken(Response):
            def iter_content(self,n):
                yield b'partial';raise s.requests.ConnectionError('no secret logging')
        class Http:
            def get(self,url,**kw):
                return Broken() if kw.get('stream') else Response('m4a: "https://media.soundgasm.net/one.m4a"')
        self.enqueue();saved,failed=s.download_pending(self.db,self.cfg,Http(),1)
        self.assertFalse(saved);self.assertEqual(len(failed),1)
        self.assertEqual(self.db.execute('select count(*) from downloads').fetchone()[0],0)
    def test_plex_retry_even_without_new_download(self):
        s.meta_set(self.db,'plex_pending',True)
        s.meta_set(self.db,'plex_pending_paths',['/media/test.m4a'])
        cfg={'plex':{'url':'http://plex','section_id':4},'plex_scan_wait_seconds':0}
        class Http:
            def get(self,*a,**kw):raise s.SourceError('http_503')
        self.assertEqual(s.plex_refresh(self.db,cfg,{'plex_token':'SECRET'},Http()),'http_503')
        self.assertTrue(s.meta_get(self.db,'plex_pending'))
        calls=[]
        def scanning_get(url,**kw):
            calls.append((url,kw))
            if url.endswith('/all'):return Response('<MediaContainer/>')
            if url.endswith('/sections'):return Response('<MediaContainer><Directory key="4" refreshing="1"/></MediaContainer>')
            raise AssertionError('An active Plex scan must not be retriggered')
        with patch.object(Http,'get',side_effect=scanning_get):
            self.assertEqual(s.plex_refresh(self.db,cfg,{'plex_token':'SECRET'},Http()),'pending_indexing')
        self.assertTrue(s.meta_get(self.db,'plex_pending'))
        def accepted_get(url,**kw):
            calls.append((url,kw))
            if url.endswith('/all'):return Response('<MediaContainer/>')
            return Response('<MediaContainer><Directory key="4" refreshing="0"/></MediaContainer>')
        with patch.object(Http,'get',side_effect=accepted_get):
            self.assertEqual(s.plex_refresh(self.db,cfg,{'plex_token':'SECRET'},Http()),'pending_indexing')
        self.assertTrue(s.meta_get(self.db,'plex_pending'))
        with patch.object(Http,'get',return_value=Response('<MediaContainer><Track><Media><Part file="/media/test.m4a"/></Media></Track></MediaContainer>')):
            self.assertEqual(s.plex_refresh(self.db,cfg,{'plex_token':'SECRET'},Http()),'indexed')
        self.assertFalse(s.meta_get(self.db,'plex_pending'))
        self.assertEqual(s.meta_get(self.db,'plex_pending_paths'),[])
        for url,kw in calls:
            self.assertNotIn('SECRET',url)
            self.assertEqual(kw['headers']['X-Plex-Token'],'SECRET')
    def test_source_errors_visible_and_circuit_breaker(self):
        cfg=dict(self.cfg,subreddits=['a','b'],soundgasm_creators=['a'])
        with patch.object(s.Reddit,'page',side_effect=s.SourceError('http_403')) as page, patch.object(s,'sg_listing',return_value=[]):
            reports=s.discover(self.db,cfg,{},None,['reddit','soundgasm'])
        self.assertEqual(page.call_count,1)
        self.assertEqual([r['status'] for r in reports],['unavailable','unavailable','empty'])
    def test_pagination_continuation_and_queue_survive(self):
        def post(i):return {'id':str(i),'created_utc':i,'author':'a','title':'[F4M] hypnosis','url':f'https://soundgasm.net/u/a/{i}'}
        cfg=dict(self.cfg,subreddits=['a'],reddit_max_pages=2)
        with patch.object(s.Reddit,'page',side_effect=[([post(5)],'cursor5'),([post(4)],'cursor4')]):
            s.discover(self.db,cfg,{},None,['reddit'])
        self.assertEqual(s.meta_get(self.db,'reddit_checkpoint:a')['after'],'cursor4')
        with patch.object(s.Reddit,'page',side_effect=[([post(6)],'cursor6'),([post(3)],None)]) as page:
            s.discover(self.db,cfg,{},None,['reddit'])
            self.assertEqual(page.call_args.args,('a','cursor4'))
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0],4)
        self.assertIsNone(s.meta_get(self.db,'reddit_checkpoint:a')['after'])
    def test_mount_failure(self):
        with patch.object(s.subprocess,'run',return_value=type('R',(),{'returncode':0,'stdout':'ext4 /\n'})()):
            with self.assertRaisesRegex(RuntimeError,'not_mounted'):s.require_mount(self.cfg)
    def test_retry_is_bounded_and_auth_is_not_retried(self):
        http=s.HTTP(0)
        response=type('R',(),{'status_code':503,'headers':{},'close':lambda self:None})()
        with patch.object(http.session,'request',return_value=response) as req,patch.object(s.time,'sleep'):
            with self.assertRaises(s.SourceError):http.get('https://example.test')
            self.assertEqual(req.call_count,3)
        response.status_code=403
        with patch.object(http.session,'request',return_value=response) as req:
            with self.assertRaises(s.SourceError):http.get('https://example.test')
            self.assertEqual(req.call_count,1)

if __name__=='__main__':unittest.main(verbosity=2)

class TitleTests(unittest.TestCase):
    def test_version_is_visible_first_and_original_is_retained(self):
        from titles import compact_title
        t='A Long Descriptive Title (many descriptive details) — A Long Descriptive Title (VOCAL EDIT)'
        short,labels=compact_title(t)
        self.assertTrue(short.startswith('[Vocal]'));self.assertLessEqual(len(short),90)
    def test_library_variants_never_collapse(self):
        from titles import library_titles
        rows=[{'artist':'Artist','title':t} for t in ['Escape Room [The Attempt]', 'Escape Room [The Acceptance]',
              'Story (p1)','Story (p2)','Relax (No Wake Up Version)','Relax (Wake Up Version)']]
        plans=library_titles(rows)
        self.assertEqual(len({p['new_title'] for p in plans}),len(rows))
        for p in plans:self.assertLessEqual(len(p['new_title']),90)

class InspectionTests(unittest.TestCase):
    setUp = Tests.setUp
    tearDown = Tests.tearDown
    def test_inspection_leaves_database_and_media_unchanged(self):
        import hashlib,io,yaml
        cfg=dict(self.cfg,state_db=str(self.root/'state.db'),secrets_file=str(self.root/'secrets.json'),lock_file=str(self.root/'lock'))
        (self.root/'secrets.json').write_text('{}');(self.root/'config.yaml').write_text(yaml.safe_dump(cfg))
        before=hashlib.sha256((self.root/'state.db').read_bytes()).hexdigest()
        with patch.object(s.sys,'argv',['scraper','--config',str(self.root/'config.yaml'),'--inspect','--sources','']),patch.object(s,'require_mount'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s.main(),0)
        self.assertEqual(before,hashlib.sha256((self.root/'state.db').read_bytes()).hexdigest())
        self.assertFalse(list(Path(self.cfg['output_root']).rglob('*')))
