"""Offline coverage for the independent Visual ASMR asset lifecycle."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge as b
import video_media as v


SCHEMA = '''
CREATE TABLE assets(key TEXT PRIMARY KEY,url TEXT,targets TEXT,source TEXT,creator TEXT,title TEXT,
 published INTEGER,saved_path TEXT,state TEXT,attempts INTEGER DEFAULT 0,retry_after INTEGER DEFAULT 0,error TEXT,acquired INTEGER);
CREATE TABLE creators(id INTEGER PRIMARY KEY,name TEXT UNIQUE,monitored INTEGER,profile_id INTEGER,
 monitor_video INTEGER DEFAULT 0,video_quality_profile_id INTEGER DEFAULT 1);
CREATE TABLE media_assets(recording_key TEXT,media_kind TEXT,wanted INTEGER,state TEXT,saved_path TEXT,
 source_url TEXT,provider_id TEXT,attempts INTEGER DEFAULT 0,retry_after INTEGER DEFAULT 0,error TEXT,acquired INTEGER,details TEXT DEFAULT '{}',PRIMARY KEY(recording_key,media_kind));
CREATE TABLE video_candidates(id INTEGER PRIMARY KEY,recording_key TEXT,provider TEXT,provider_id TEXT,url TEXT,
 normalized_url TEXT,fingerprint TEXT,resolution INTEGER,source_quality INTEGER DEFAULT 0,bitrate INTEGER DEFAULT 0,
 video_codec TEXT,audio_codec TEXT,container TEXT,requires_transcode INTEGER DEFAULT 0,interactive_only INTEGER DEFAULT 0,
 details TEXT DEFAULT '{}',UNIQUE(provider,provider_id),UNIQUE(normalized_url));
CREATE TABLE video_quality_profiles(id INTEGER PRIMARY KEY,name TEXT,resolution TEXT,settings TEXT,is_builtin INTEGER);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE history(id INTEGER PRIMARY KEY,at TEXT,event TEXT,recording_key TEXT,details TEXT);
'''


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.audio = self.root / 'audio'; self.video = self.root / 'video'; self.downloads = self.root / 'downloads'
        for path in (self.audio, self.video, self.downloads): path.mkdir()
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row; self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO creators VALUES(1,'Creator',1,1,1,1)")
        self.db.execute("INSERT INTO video_quality_profiles VALUES(1,'Any','Any','{}',1)")
        self.db.execute("INSERT INTO assets(key,url,targets,source,creator,title,published,state) VALUES('recording','https://youtu.be/abcdefghijk','[]','youtube','Creator','A visual recording',1704067200,'pending')")
        self.db.execute("INSERT INTO media_assets(recording_key,media_kind,wanted,state) VALUES('recording','Audio',1,'wanted')")
        self.cfg = {'output_root': str(self.audio), 'video_root': str(self.video),
                    'download_root': str(self.downloads), 'video_free_space_gib': 0,
                    'video_concurrency': 1, 'youtube_binary': '/usr/bin/false'}
        self.db.commit()
    def tearDown(self): self.db.close(); self.temp.cleanup()

    def video_file(self, name='source.mp4', audio=False):
        path = self.downloads / name
        command = ['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=320x240:d=1']
        if audio: command += ['-f','lavfi','-i','sine=frequency=220:duration=1','-shortest','-c:a','aac']
        command += ['-c:v','libx264','-pix_fmt','yuv420p','-y',str(path)]
        subprocess.run(command, check=True)
        return path

    def artwork_file(self, name='source.jpg'):
        path = self.downloads / name
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=320x180',
                        '-frames:v','1','-y',str(path)], check=True)
        return path

    def test_provider_ids_deduplicate_youtube_urls_and_reddit_crossposts(self):
        v.add_candidate(self.db, 'recording', 'youtube', 'https://youtu.be/abcdefghijk?t=2')
        _, added = v.add_candidate(self.db, 'recording', 'youtube', 'https://www.youtube.com/watch?v=abcdefghijk&list=x')
        self.assertFalse(added)
        self.assertEqual(self.db.execute("SELECT count(*) FROM video_candidates").fetchone()[0], 1)
        post = {'id':'crosspost','crosspost_parent_list':[{'id':'original'}]}
        v.add_candidate(self.db, 'recording', 'reddit', 'https://v.redd.it/media/DASH_720.mp4', post)
        provider = self.db.execute("SELECT provider_id FROM video_candidates WHERE provider='reddit'").fetchone()[0]
        self.assertEqual(provider, 'original')

    def test_any_prefers_highest_and_caps_reject_above_target(self):
        candidates = [
            {'resolution':1080,'source_quality':8,'bitrate':5000,'video_codec':'h264','audio_codec':'aac'},
            {'resolution':2160,'source_quality':7,'bitrate':12000,'video_codec':'av1','audio_codec':'opus'},
        ]
        self.assertEqual(v.choose_candidate(candidates, {'resolution':'Any'})['resolution'], 2160)
        self.assertEqual(v.choose_candidate(candidates, {'resolution':'1080p'})['resolution'], 1080)
        self.assertIsNone(v.choose_candidate([dict(candidates[1], provider='prowlarr')],
                                             {'resolution':'1080p'}))
        adaptive = v.choose_candidate([dict(candidates[1], provider='youtube')],
                                      {'resolution':'1080p'})
        self.assertEqual(adaptive['rank'][0], 1080)
        self.assertEqual(v._format_selector(1080),
                         'bestvideo*[height<=1080]+bestaudio/best[height<=1080]')
        self.assertEqual(v._format_selector(), 'bestvideo*+bestaudio/best')

    def test_video_only_import_preserves_audio_wanted_and_names_by_date(self):
        source = self.video_file()
        candidate = {'provider':'youtube','provider_id':'abcdefghijk'}
        result = v.import_video(self.db, self.cfg, 'recording', source, candidate)
        self.assertEqual(result['status'], 'imported'); self.assertFalse(result['hasAudio'])
        saved = Path(result['path']); self.assertTrue(saved.is_file())
        self.assertEqual(saved.relative_to(self.video).parts[:2], ('Creator','2024'))
        self.assertIn('2024-01-01 - A visual recording [youtube-abcdefghijk]', saved.name)
        self.assertEqual(self.db.execute("SELECT state FROM media_assets WHERE media_kind='Audio'").fetchone()[0], 'wanted')

    def test_single_video_master_can_derive_missing_audio(self):
        source = self.video_file(audio=True); calls = []
        def importer(db, cfg, key, path): calls.append((key, Path(path).suffix)); return {'status':'imported'}
        result = v.import_video(self.db, self.cfg, 'recording', source,
                                {'provider':'youtube','provider_id':'abcdefghijk'}, importer)
        self.assertTrue(result['hasAudio']); self.assertEqual(calls, [('recording','.m4a')])

    def test_audio_derivation_failure_does_not_fail_imported_video(self):
        source = self.video_file(audio=True)
        def importer(*_): raise ValueError('audio_import_failed')
        result = v.import_video(self.db, self.cfg, 'recording', source,
                                {'provider':'youtube','provider_id':'abcdefghijk'}, importer)
        states = dict(self.db.execute(
            "SELECT media_kind,state FROM media_assets WHERE recording_key='recording'"))
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['derivedAudio']['status'], 'failed')
        self.assertEqual(states, {'Audio':'failed', 'Video':'imported'})
        self.assertTrue(Path(result['path']).is_file())

    def test_compatible_artwork_is_stored_with_matching_basename(self):
        source, artwork = self.video_file(), self.artwork_file()
        result = v.import_video(self.db, self.cfg, 'recording', source,
                                {'provider':'youtube','provider_id':'abcdefghijk'}, artwork=artwork)
        saved, saved_artwork = Path(result['path']), Path(result['artworkPath'])
        self.assertTrue(saved_artwork.is_file())
        self.assertEqual(saved.with_suffix(''), saved_artwork.with_suffix(''))

    def test_invalid_artwork_rolls_back_new_video(self):
        source, artwork = self.video_file(), self.downloads / 'invalid.jpg'
        artwork.write_bytes(b'not an image')
        with self.assertRaisesRegex(ValueError, 'artwork_invalid'):
            v.import_video(self.db, self.cfg, 'recording', source,
                           {'provider':'youtube','provider_id':'abcdefghijk'}, artwork=artwork)
        self.assertFalse(list(self.video.rglob('*.mp4')))
        self.assertFalse(list(self.video.rglob('*.asmarr-part')))

    def test_atomic_import_cleans_partial_on_interruption(self):
        source = self.video_file()
        with patch.object(v.shutil, 'copyfileobj', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                v.import_video(self.db, self.cfg, 'recording', source, {'provider':'youtube','provider_id':'id'})
        self.assertFalse(list(self.video.rglob('*.asmarr-part'))); self.assertTrue(source.exists())

    def test_low_space_pauses_without_starting_transfer(self):
        self.cfg['video_free_space_gib'] = 10**12
        v.add_candidate(self.db, 'recording', 'youtube', 'https://youtu.be/abcdefghijk')
        with patch.object(v, 'download_video') as download:
            result = v.process_queue(self.db, self.cfg)
        self.assertEqual(result['status'], 'paused_low_space'); download.assert_not_called()

    def test_default_concurrency_runs_one_transfer(self):
        v.add_candidate(self.db, 'recording', 'youtube', 'https://youtu.be/abcdefghijk', details={'height':1080})
        with patch.object(v, 'download_video', return_value={'status':'imported'}) as download:
            result = v.process_queue(self.db, self.cfg)
        self.assertEqual(result['concurrency'], 1); self.assertEqual(download.call_count, 1)

    def test_roots_and_interactive_prowlarr_safety(self):
        with self.assertRaisesRegex(ValueError, 'overlap'): v.validate_roots('/media/asmr', '/media/asmr/video')
        candidate = v.rank_candidate({'resolution':1080,'requires_transcode':False}, {'resolution':'Any'})
        self.assertIsNotNone(candidate)
        # Video Prowlarr results are tagged interactive-only by the bridge and
        # video queue processing only reads candidates with interactive_only=0.
        self.db.execute("INSERT INTO video_candidates(recording_key,provider,provider_id,url,normalized_url,interactive_only) VALUES('recording','prowlarr','p','magnet:x','magnet:x',1)")
        self.db.execute("INSERT OR REPLACE INTO media_assets(recording_key,media_kind,wanted,state) VALUES('recording','Video',1,'wanted')")
        with patch.object(v, 'download_video') as download:
            result = v.process_queue(self.db, self.cfg)
        download.assert_not_called(); self.assertEqual(result['jobs'][0]['status'], 'unavailable')

    def plex_response(self, body):
        response = Mock()
        response.text = body
        response.raise_for_status.return_value = None
        return response

    def test_plex_accepts_only_dedicated_other_videos_library(self):
        self.cfg['plex'] = {'url':'http://plex:32400','video':{'section_id':9}}
        sections = ('<MediaContainer><Directory key="9" type="movie" '
                    'agent="com.plexapp.agents.none" scanner="Plex Video Files Scanner" '
                    f'title="ASMarr Videos"><Location path="{self.video}"/>'
                    '</Directory></MediaContainer>')
        with patch.object(v.requests, 'get', return_value=self.plex_response(sections)) as get:
            result = v.plex_library_validation(self.cfg, {'plex_token':'secret'})
        self.assertEqual(result['type'], 'Other Videos')
        self.assertEqual(result['sectionId'], 9)
        get.assert_called_once()

    def test_plex_rejects_movie_tv_audio_and_overlapping_bindings(self):
        self.cfg['plex'] = {'url':'http://plex:32400','video':{'section_id':9}}
        invalid = [
            '<Directory key="9" type="movie" agent="tv.plex.agents.movie" scanner="Plex Movie"/>',
            '<Directory key="9" type="show" agent="tv.plex.agents.series" scanner="Plex TV Series"/>',
            '<Directory key="9" type="artist" agent="tv.plex.agents.music" scanner="Plex Music"/>',
        ]
        for section in invalid:
            with self.subTest(section=section), patch.object(
                    v.requests, 'get', return_value=self.plex_response(
                        '<MediaContainer>' + section + '</MediaContainer>')):
                with self.assertRaisesRegex(ValueError, 'must_be_other_videos'):
                    v.plex_library_validation(self.cfg, {'plex_token':'secret'})
        common_parent = self.root
        valid_section = ('<MediaContainer><Directory key="9" type="movie" '
                         'agent="com.plexapp.agents.none" scanner="Plex Video Files Scanner">'
                         f'<Location path="{common_parent}"/></Directory></MediaContainer>')
        with patch.object(v.requests, 'get', return_value=self.plex_response(valid_section)):
            with self.assertRaisesRegex(ValueError, 'libraries_overlap'):
                v.plex_library_validation(self.cfg, {'plex_token':'secret'})


if __name__ == '__main__': unittest.main()
