import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import creator_removal as removal
import plex_playlists


class CreatorRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root/'audio';self.audio.mkdir()
        self.video = self.root/'video';self.video.mkdir()
        self.cfg = {'output_root': str(self.audio), 'video_root': str(self.video)}
        self.path = self.audio/'test.mp3';self.path.write_bytes(b'fixture recording')
        self.other = self.audio/'unrelated.mp3';self.other.write_bytes(b'other recording')
        self.db = sqlite3.connect(':memory:');self.db.row_factory = sqlite3.Row;self.addCleanup(self.db.close)
        self.db.executescript('''PRAGMA foreign_keys=ON;
            CREATE TABLE creators(id INTEGER PRIMARY KEY,name TEXT,aliases TEXT,monitored INTEGER,monitor_video INTEGER);
            CREATE TABLE identities(id INTEGER PRIMARY KEY,creator_id INTEGER REFERENCES creators(id),kind TEXT,handle TEXT,enabled INTEGER);
            CREATE TABLE assets(key TEXT PRIMARY KEY,creator TEXT,state TEXT,saved_path TEXT);
            CREATE TABLE downloads(creator TEXT,saved_path TEXT);
            CREATE TABLE media_assets(recording_key TEXT,media_kind TEXT,state TEXT,saved_path TEXT,wanted INTEGER);
            CREATE TABLE creator_exclusions(name TEXT PRIMARY KEY,aliases TEXT,identities TEXT,removed INTEGER);
            CREATE TABLE queue(id TEXT,recording_key TEXT,download_id TEXT,provider TEXT,state TEXT);
            CREATE TABLE commands(id TEXT,name TEXT,arguments TEXT,state TEXT,finished TEXT,error TEXT);
            CREATE TABLE backfill_jobs(creator_id INTEGER REFERENCES creators(id));
            CREATE TABLE settings(key TEXT,value TEXT);
            CREATE TABLE history(at TEXT,event TEXT,details TEXT);
            INSERT INTO creators VALUES(1,'Test Reader','["Reader Alias"]',1,1),(2,'Other Reader','[]',1,0);
            INSERT INTO identities VALUES(1,1,'reddit','ReaderHandle',1);
            INSERT INTO backfill_jobs VALUES(1);
            INSERT INTO settings VALUES('mode','production');
            INSERT INTO commands VALUES('pending-grab','grab','{"key":"owned"}','queued',NULL,NULL),
            ('pending-video','video-backfill','{"creatorId":1}','queued',NULL,NULL),
            ('other-grab','grab','{"key":"other"}','queued',NULL,NULL);
        ''')
        self.db.execute('INSERT INTO assets VALUES(?,?,?,?)', ('owned','Test Reader','complete',str(self.path)))
        self.db.execute('INSERT INTO assets VALUES(?,?,?,?)', ('other','Other Reader','complete',str(self.other)))
        self.db.execute('INSERT INTO media_assets VALUES(?,?,?,?,?)', ('owned','Audio','complete',str(self.path),1))
        self.db.execute('INSERT INTO media_assets VALUES(?,?,?,?,?)', ('other','Audio','complete',str(self.other),1))
        self.db.commit()
        self.qbit=Mock(side_effect=AssertionError('must not contact download client'))
        self.plex=Mock(return_value={'status':'cleaned'})

    def args(self, delete=False, prevent=True):
        plan=removal.preview(self.db,self.cfg,1)
        return {'creatorId':1,'deleteFiles':delete,'preventReadd':prevent,'confirmation':'Test Reader' if delete else '', 'planToken':plan['planToken']}

    def run_remove(self, **kwargs):
        return removal.remove(self.db,self.cfg,self.args(**kwargs),self.qbit,self.plex)

    def test_keep_files_removes_creator_and_cancels_only_owned_work(self):
        result=self.run_remove()
        self.assertEqual('removed',result['status'])
        self.assertTrue(self.path.exists());self.assertTrue(self.other.exists())
        self.plex.assert_not_called();self.qbit.assert_not_called()
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM creators WHERE id=1').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM identities WHERE creator_id=1').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM backfill_jobs WHERE creator_id=1').fetchone()[0])
        self.assertEqual('complete',self.db.execute("SELECT state FROM assets WHERE key='owned'").fetchone()[0])
        states=dict(self.db.execute('SELECT id,state FROM commands'))
        self.assertEqual({'pending-grab':'cancelled','pending-video':'cancelled','other-grab':'queued'},states)
        self.assertTrue(removal.excluded(self.db,'reader alias'))
        self.assertTrue(removal.excluded(self.db,'readerhandle'))
        self.assertTrue(removal.excluded(self.db,'different',[('reddit','readerhandle')]))
        self.assertFalse(removal.excluded(self.db,'Other Reader'))

    def test_delete_files_does_not_remove_untracked_files_or_directories(self):
        unrelated=self.audio/'untracked.txt';unrelated.write_text('keep')
        result=self.run_remove(delete=True)
        self.assertFalse(self.path.exists());self.assertTrue(self.audio.is_dir())
        self.assertTrue(self.other.exists());self.assertTrue(unrelated.exists())
        self.assertEqual([str(self.path)],result['deleted'])
        self.plex.assert_called_once_with([str(self.path)])
        self.assertIsNone(self.db.execute("SELECT saved_path FROM assets WHERE key='owned'").fetchone()[0])

    def test_audio_and_video_are_both_previewed_and_deleted(self):
        video=self.video/'test.mp4';video.write_bytes(b'video')
        self.db.execute('INSERT INTO media_assets VALUES(?,?,?,?,?)',('owned','Video','imported',str(video),1));self.db.commit()
        self.assertEqual(2,len(removal.preview(self.db,self.cfg,1)['files']))
        result=self.run_remove(delete=True)
        self.assertCountEqual([str(self.path),str(video)],result['deleted'])

    def test_confirmation_required_and_case_sensitive(self):
        args=self.args(delete=True);args['confirmation']='test reader'
        with self.assertRaisesRegex(ValueError,'exact creator name'):
            removal.remove(self.db,self.cfg,args,self.qbit,self.plex)
        self.assertTrue(self.path.exists())

    def test_stale_preview_fails_before_any_mutation(self):
        args=self.args(delete=True);self.path.write_bytes(b'changed size')
        with self.assertRaisesRegex(ValueError,'fresh removal preview'):
            removal.remove(self.db,self.cfg,args,self.qbit,self.plex)
        self.assertEqual(1,self.db.execute('SELECT monitored FROM creators WHERE id=1').fetchone()[0])
        self.assertTrue(self.path.exists())

    def test_shared_same_path_and_normalized_path_are_preserved(self):
        alternate=str(self.audio)+'/./test.mp3'
        self.db.execute("UPDATE assets SET saved_path=? WHERE key='other'",(alternate,));self.db.commit()
        result=self.run_remove(delete=True)
        self.assertTrue(self.path.exists())
        self.assertEqual('shared_file_preserved',result['warnings'][0]['reason'])
        self.plex.assert_not_called()

    def test_hardlinked_files_are_preserved(self):
        os.link(self.path,self.audio/'linked.mp3')
        result=self.run_remove(delete=True)
        self.assertTrue(self.path.exists());self.assertEqual([],result['deleted'])

    def test_another_creators_download_history_claim_preserves_the_file(self):
        self.db.execute('INSERT INTO downloads VALUES(?,?)',('Other Reader',str(self.path)));self.db.commit()
        result=self.run_remove(delete=True)
        self.assertTrue(self.path.exists());self.assertEqual([],result['deleted'])

    def test_symlink_file_is_preserved_without_following(self):
        self.path.unlink();self.path.symlink_to(self.other)
        result=self.run_remove(delete=True)
        self.assertTrue(self.path.is_symlink());self.assertEqual(b'other recording',self.other.read_bytes())
        self.assertEqual('unsafe_file_type',result['warnings'][0]['reason'])

    def test_symlink_directory_is_never_followed(self):
        link=self.audio/'linked';link.symlink_to(self.video,target_is_directory=True)
        outside=self.video/'linked.mp3';outside.write_bytes(b'keep')
        self.db.execute("UPDATE assets SET saved_path=? WHERE key='owned'",(str(link/'linked.mp3'),))
        self.db.execute("UPDATE media_assets SET saved_path=? WHERE recording_key='owned'",(str(link/'linked.mp3'),));self.db.commit()
        result=self.run_remove(delete=True)
        self.assertTrue(outside.exists());self.assertEqual([],result['deleted'])

    def test_outside_root_and_directories_are_preserved(self):
        self.db.execute("UPDATE assets SET saved_path=? WHERE key='owned'",(str(self.root),))
        self.db.execute("UPDATE media_assets SET saved_path=? WHERE recording_key='owned'",(str(self.root),));self.db.commit()
        self.assertEqual([],self.run_remove(delete=True)['deleted'])
        self.assertTrue(self.root.is_dir())

    def test_filesystem_root_and_nonmedia_tracked_files_are_not_deletion_targets(self):
        self.cfg['output_root']='/'
        self.assertEqual([],self.run_remove(delete=True)['deleted'])
        self.assertTrue(self.path.exists())

    def test_nonmedia_tracked_file_is_preserved_even_inside_the_library(self):
        nonmedia=self.audio/'private-config.json';nonmedia.write_text('{}')
        self.db.execute("UPDATE assets SET saved_path=? WHERE key='owned'",(str(nonmedia),))
        self.db.execute("UPDATE media_assets SET saved_path=? WHERE recording_key='owned'",(str(nonmedia),));self.db.commit()
        self.assertEqual([],self.run_remove(delete=True)['deleted'])
        self.assertTrue(nonmedia.exists())

    def test_delete_failure_reports_preserved_file(self):
        with patch.object(removal.os,'unlink',side_effect=PermissionError('denied')):
            result=self.run_remove(delete=True)
        self.assertTrue(self.path.exists());self.assertEqual([str(self.path)],result['preserved'])
        self.assertEqual('file_changed_or_delete_failed',result['warnings'][0]['reason'])

    def test_retry_after_missing_file_clears_stale_association_without_reacquisition(self):
        self.path.unlink()
        result=self.run_remove(delete=True)
        self.assertEqual([],result['deleted'])
        self.assertEqual([str(self.path)],result['alreadyMissing'])
        self.assertIsNone(self.db.execute("SELECT saved_path FROM assets WHERE key='owned'").fetchone()[0])
        self.plex.assert_called_once_with([str(self.path)])

    def test_missing_shared_path_does_not_trigger_plex_cleanup(self):
        self.path.unlink()
        self.db.execute("UPDATE assets SET saved_path=? WHERE key='other'",(str(self.path),));self.db.commit()
        result=self.run_remove(delete=True)
        self.assertEqual([],result['alreadyMissing']);self.plex.assert_not_called()

    def test_optional_readdition_block_can_be_disabled(self):
        self.run_remove(prevent=False)
        self.assertFalse(removal.excluded(self.db,'Test Reader'))

    def test_keep_files_readd_reconnects_original_recording_without_download(self):
        self.run_remove()
        self.db.execute('DELETE FROM creator_exclusions')
        self.db.execute("INSERT INTO creators VALUES(3,'Test Reader','[]',1,0)")
        self.assertEqual(str(self.path),self.db.execute('SELECT a.saved_path FROM assets a JOIN creators c ON c.name=a.creator WHERE c.id=3').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM queue').fetchone()[0])

    def test_shadow_mode_cannot_remove(self):
        self.db.execute("UPDATE settings SET value='shadow'");self.db.commit()
        with self.assertRaisesRegex(ValueError,'production'):
            self.run_remove(delete=True)
        self.assertTrue(self.path.exists())

    def test_active_torrent_is_removed_without_deleting_download_data(self):
        self.db.execute("INSERT INTO queue VALUES('q','owned','hash','qbittorrent','downloading')");self.db.commit()
        session=Mock();session.get.return_value.json.return_value=[{'hash':'hash','category':'asmarr'}]
        self.qbit=Mock(return_value=(session,'http://fixture',{}))
        self.run_remove()
        self.assertEqual('false',session.post.call_args.kwargs['data']['deleteFiles'])
        self.assertEqual('cancelled',self.db.execute('SELECT state FROM queue').fetchone()[0])

    def test_wrong_category_or_shared_active_torrent_blocks_all_removal(self):
        self.db.execute("INSERT INTO queue VALUES('q','owned','hash','qbittorrent','downloading')");self.db.commit()
        session=Mock();session.get.return_value.json.return_value=[{'hash':'hash','category':'other'}]
        self.qbit=Mock(return_value=(session,'http://fixture',{}))
        with self.assertRaises(ValueError):self.run_remove(delete=True)
        self.assertTrue(self.path.exists());session.post.assert_not_called()
        self.db.execute("INSERT INTO queue VALUES('other-q','other','hash','qbittorrent','downloading')");self.db.commit()
        with self.assertRaisesRegex(ValueError,'shared'):self.run_remove(delete=True)

    def test_plex_failure_does_not_undo_removal_or_claim_success(self):
        self.plex.side_effect=RuntimeError('offline')
        result=self.run_remove(delete=True)
        self.assertEqual('cleanup_pending',result['plex']['status'])
        self.assertFalse(self.path.exists())


class PlexRemovalTests(unittest.TestCase):
    def call(self, multiple=False, forbidden=False, changed=False):
        cfg={'output_root':'/fixture/audio','video_root':'/fixture/video','plex':{'url':'http://fixture','section_id':4}}
        client=Mock()
        self.client=client
        body='<MediaContainer size="1"><Track ratingKey="11" librarySectionID="4"><Media><Part file="/fixture/audio/removed.mp3"/>'+ ('<Part file="/fixture/audio/other.mp3"/>' if multiple else '')+'</Media></Track></MediaContainer>'
        def request(method,path,**kwargs):
            if method=='DELETE' and forbidden:raise plex_playlists.PlaylistError('plex_http_403')
            if path=='/library/metadata/11' and method=='GET' and changed:return ET.fromstring(body.replace('librarySectionID="4"','librarySectionID="8"'))
            return ET.fromstring(body) if path.endswith('/all') or path=='/library/metadata/11' and method=='GET' else ET.Element('MediaContainer')
        client.call.side_effect=request
        result=removal.cleanup_plex(cfg,{},['/fixture/audio/removed.mp3'],lambda *a:client)
        return result,client

    def test_exact_item_cleanup_never_empties_trash_or_touches_tags(self):
        result,client=self.call()
        self.assertEqual(['11'],result['removedRatingKeys'])
        mutations=[c.args for c in client.call.call_args_list if c.args[0]!='GET']
        self.assertEqual([('DELETE','/library/metadata/11')],mutations)

    def test_multipart_or_changed_section_is_never_deleted(self):
        result,client=self.call(multiple=True)
        self.assertEqual('cleanup_pending',result['status'])
        self.assertFalse(any(c.args[0]=='DELETE' for c in client.call.call_args_list))
        with self.assertRaises(plex_playlists.PlaylistError):self.call(changed=True)
        self.assertFalse(any(c.args[0]=='DELETE' for c in self.client.call.call_args_list))

    def test_plex_disallowing_deletion_is_reported_without_policy_change(self):
        result,client=self.call(forbidden=True)
        self.assertEqual('cleanup_pending',result['status'])
        self.assertFalse(any(c.args[0]=='PUT' for c in client.call.call_args_list))


if __name__=='__main__':unittest.main()
