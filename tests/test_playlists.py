import copy
import json
import unittest
import xml.etree.ElementTree as E
from urllib.parse import quote, unquote, urlsplit, parse_qs

import categories as c
import plex_playlists as p
import scraper as s
import test_legacy_parsers as fixtures


def container(items):
    root = E.Element('MediaContainer', size=str(len(items)))
    root.extend(copy.deepcopy(items))
    return root


class Plex:
    def __init__(self):
        self.tracks = {
            '1': E.fromstring('<Track ratingKey="1" librarySectionID="4" title="Bedtime whispering"><Mood tag="Manual favorite"/></Track>'),
            '2': E.fromstring('<Track ratingKey="2" librarySectionID="4" title="Everyday conversation"/>'),
        }
        self.playlists = {'99': E.Element('Playlist', ratingKey='99', guid='user-list', title='My favorites', smart='0')}
        self.writes = []
        self.fail_post = False

    def call(self, method, path, **params):
        if method != 'GET':
            self.writes.append((method, path, params))
        if path == '/identity':
            return E.Element('MediaContainer', machineIdentifier='server')
        if path == '/library/sections/4/all':
            if method == 'GET':
                return container(list(self.tracks.values()))
            for key in params['id'].split(','):
                track = self.tracks[key]
                old = p.mood_names(track)
                removed = {unquote(x) for x in params.get('mood[].tag.tag-', '').split(',')}
                added = {v for k, v in params.items() if k.endswith('].tag.tag')}
                for child in track.findall('Mood'):
                    track.remove(child)
                for name in sorted((old - removed) | added):
                    E.SubElement(track, 'Mood', tag=name)
            return container([])
        if path.startswith('/library/metadata/'):
            return container([self.tracks[k] for k in path.split('/')[-1].split(',')])
        if path == '/playlists':
            if method == 'GET':
                return container(list(self.playlists.values()))
            key = str(100 + len(self.playlists))
            obj = E.Element('Playlist', ratingKey=key, guid='managed-' + key, title=params['title'],
                            smart='1', content='library://x/directory/' + quote(params['uri'].split('/com.plexapp.plugins.library')[1], safe=''))
            self.playlists[key] = obj
            if self.fail_post:
                self.fail_post = False
                raise p.PlaylistError('plex_ConnectionError')
            return container([obj])
        key = path.split('/')[2]
        obj = self.playlists[key]
        if path.endswith('/items'):
            if method == 'PUT':
                obj.set('content', 'library://x/directory/' + quote(params['uri'].split('/com.plexapp.plugins.library')[1], safe=''))
                return container([])
            query = parse_qs(unquote(obj.get('content')).split('?', 1)[1])
            return container([t for t in self.tracks.values() if query['track.mood'][0] in p.mood_names(t)])
        if method == 'PUT':
            obj.attrib.update(params)
        return container([obj])


class PlaylistTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown

    def config(self):
        return dict(self.cfg, plex={'url': 'http://plex', 'section_id': 4}, playlists={'enabled': True})

    def test_themes_overlap_and_word_boundaries_and_negations(self):
        self.assertEqual(set(c.classify('Sleep aid, whispers, personal attention')), {'sleep', 'whispering', 'personal_attention'})
        self.assertFalse(c.classify('entrance, brainstorm, raincoat, ASMR'))
        self.assertNotIn('hypnosis', c.classify('[No hypnosis] [No Talking]'))
        self.assertEqual(c.classify('[No Talking] [No whispering]'), ['no_talking'])
        for text, category in [('affirmations', 'affirmations'), ('mindfulness', 'meditation'), ('box breathing', 'breathing'),
                               ('tapping and brushing', 'triggers'), ('rain sounds', 'nature'), ('binaural', 'binaural'),
                               ('soft-spoken', 'soft_spoken'), ('bedtime stories', 'stories')]:
            self.assertIn(category, c.classify(text))

    def test_original_title_and_unmanaged_tags_but_not_bio_or_generated_tags(self):
        track = E.fromstring('<Track title="Session" summary="Creator enjoys hypnosis.&#10;Original title: Bedtime [whispering]"><Mood tag="ASMR: Hypnosis"/><Genre tag="Meditation"/></Track>')
        found = c.classify(c.metadata_text(track))
        self.assertEqual(set(found), {'sleep', 'whispering', 'meditation'})

    def test_smart_filter_roundtrip_with_ampersand_and_wrong_library(self):
        uri = '/library/sections/4/all?type=10&amp;track.mood=ASMR%3A+Comfort+%26+Reassurance&amp;sort=addedAt%3Adesc'.replace('&amp;', '&')
        content = 'library://x/directory/' + quote(uri, safe='')
        self.assertTrue(p.filter_matches(content, '4', 'ASMR: Comfort & Reassurance'))
        self.assertFalse(p.filter_matches(content, '5', 'ASMR: Comfort & Reassurance'))

    def test_create_repeat_new_track_and_reclassification_preserve_user_data(self):
        plex = Plex()
        original = E.tostring(plex.playlists['99'])
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['status'], 'healthy', result)
        self.assertEqual(result['created'], 2)
        self.assertIn('Manual favorite', p.mood_names(plex.tracks['1']))
        plex.writes.clear()
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['status'], 'healthy', result)
        self.assertEqual(plex.writes, [])
        plex.tracks['2'].set('title', 'Sleep')
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['categories']['sleep'], 2)
        plex.tracks['1'].set('title', 'Everyday conversation')
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['categories']['whispering'], 0)
        self.assertEqual(p.mood_names(plex.tracks['1']), {'Manual favorite'})
        self.assertEqual(E.tostring(plex.playlists['99']), original)
        self.assertEqual(s.meta_get(self.db, 'playlist_mood_backup:1')['moods'], ['Manual favorite'])

    def test_readonly_preview_does_not_mutate_plex_or_database(self):
        plex = Plex()
        before = self.db.total_changes
        result = p.sync(self.db, self.config(), {}, s, client=plex, dry_run=True)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(plex.writes, [])
        self.assertEqual(self.db.total_changes, before)

    def test_interrupted_creation_recovers_without_duplicate(self):
        plex = Plex()
        plex.fail_post = True
        self.assertEqual(p.sync(self.db, self.config(), {}, s, client=plex)['status'], 'failed')
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['status'], 'healthy', result)
        self.assertEqual(len(plex.playlists), 3)

    def test_user_playlist_with_same_title_is_never_adopted(self):
        plex = Plex()
        plex.playlists['99'].set('title', 'ASMR — Sleep')
        original = E.tostring(plex.playlists['99'])
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['error'], 'playlist_title_conflict:sleep')
        self.assertEqual(E.tostring(plex.playlists['99']), original)

    def test_partial_catalog_fails_before_mutations(self):
        plex = Plex()
        call = plex.call
        def broken(method, path, **params):
            if path.startswith('/library/metadata/'):
                return container([])
            return call(method, path, **params)
        plex.call = broken
        result = p.sync(self.db, self.config(), {}, s, client=plex)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(plex.writes, [])

    def test_catalog_pagination_and_duplicate_page_guard(self):
        class Pages:
            def call(self, method, path, **params):
                start = params['X-Plex-Container-Start']
                root = container([E.Element('Track', ratingKey=str(start + 1))])
                root.set('totalSize', '2')
                root.set('offset', str(start))
                return root
        self.assertEqual(len(p.all_items(Pages(), '/tracks', 'Track')), 2)
        class Broken(Pages):
            def call(self, *args, **kwargs):
                root = super().call(*args, **kwargs)
                root.find('Track').set('ratingKey', '1')
                return root
        with self.assertRaises(p.PlaylistError):
            p.all_items(Broken(), '/tracks', 'Track')


if __name__ == '__main__':
    unittest.main()
