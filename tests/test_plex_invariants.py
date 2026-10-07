import copy
import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as E

import test_playlists

spec=importlib.util.spec_from_file_location('plex_invariants',Path(__file__).resolve().parents[1]/'ops/plex_invariants.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


class PlexInvariantTests(unittest.TestCase):
    def setUp(self):
        self.plex=test_playlists.Plex()
        track=self.plex.tracks['1']
        track.set('viewCount','3');track.set('summary','User annotation')
        E.SubElement(track,'Field',name='summary',locked='1')
        E.SubElement(track,'Field',name='mood',locked='0')
        E.SubElement(track,'Genre',tag='My genre')
        self.cfg={'plex':{'section_id':4}}
        self.before=audit.capture(self.cfg,{}, {},self.plex)

    def compare(self):return audit.compare(self.before,audit.capture(self.cfg,{}, {},self.plex))

    def test_readonly_snapshot_and_repeat_preserve_everything(self):
        self.assertTrue(self.compare()['passed'])
        self.assertEqual([],self.plex.writes)

    def test_new_import_and_managed_mood_additions_are_allowed(self):
        E.SubElement(self.plex.tracks['1'],'Mood',tag='ASMR: Sleep')
        self.plex.tracks['3']=E.fromstring('<Track ratingKey="3" librarySectionID="4" title="New import"/>')
        report=self.compare()
        self.assertTrue(report['passed']);self.assertEqual(1,report['newTracks'])

    def test_playback_changes_require_review(self):
        self.plex.tracks['1'].set('viewCount','0')
        self.assertFalse(self.compare()['passed'])

    def test_recreated_track_with_same_content_is_not_preserved(self):
        self.plex.tracks['10']=self.plex.tracks.pop('1');self.plex.tracks['10'].set('ratingKey','10')
        self.assertIn('rating_key_missing',[c['kind'] for c in self.compare()['changes']])

    def test_locked_summary_and_lock_flags_are_protected(self):
        self.plex.tracks['1'].set('summary','Overwritten')
        self.plex.tracks['1'].find('Field').set('locked','0')
        kinds={c['kind'] for c in self.compare()['changes']}
        self.assertIn('lockedValues_changed',kinds);self.assertIn('locks_changed',kinds)

    def test_locked_artist_uses_plex_grandparent_metadata(self):
        self.plex.tracks['1'].set('grandparentTitle','Locked Artist')
        E.SubElement(self.plex.tracks['1'],'Field',name='artist',locked='1')
        self.before=audit.capture(self.cfg,{}, {},self.plex)
        self.plex.tracks['1'].set('grandparentTitle','Changed Artist')
        self.assertFalse(self.compare()['passed'])

    def test_unrelated_tags_are_protected_even_with_asmr_prefix(self):
        self.plex.tracks['1'].find('Genre').set('tag','Removed custom value')
        self.assertFalse(self.compare()['passed'])
        self.plex.tracks['1'].find('Genre').set('tag','My genre')
        E.SubElement(self.plex.tracks['1'],'Mood',tag='ASMR: User-made category')
        self.assertFalse(self.compare()['passed'])

    def test_user_playlist_and_guid_changes_are_protected(self):
        self.plex.playlists['99'].set('guid','Replacement')
        self.assertFalse(self.compare()['passed'])

    def test_added_unlocked_field_is_semantically_equivalent_to_absent_field(self):
        E.SubElement(self.plex.tracks['2'],'Field',name='mood',locked='0')
        self.assertTrue(self.compare()['passed'])


if __name__=='__main__':unittest.main()
