import copy
import json
import time
import unittest
from unittest.mock import patch

import scraper as s
import sfw_sources as sfw
import test_legacy_parsers as fixtures


class SFWTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown

    def config(self):
        return dict(self.cfg, sfw_expansion={
            'enabled': True, 'subreddits': ['AudioCandy'], 'reddit_pages': 3,
            'initial_per_creator': 3, 'max_new_per_run': 12, 'auto_add': True,
            'creators': [{'reddit': 'Alice', 'soundgasm': 'Alice', 'creator': 'Alice',
                          'trusted_speaker': True, 'added_at': 2000}]})

    def post(self, i, **changes):
        return dict({'id': str(i), 'author': 'Alice', 'title': '[F4A] Sleep and comfort [SFW]',
                     'created_utc': 1000 + i, 'over_18': False,
                     'url': f'https://soundgasm.net/u/Alice/v{i}'}, **changes)

    def entry(self, i, **changes):
        return dict({'title': '[F4A] Sleep and comfort [SFW]', 'description': '',
                     'url': f'https://soundgasm.net/u/Alice/v{i}'}, **changes)

    def run_sources(self, cfg, posts=(), entries=()):
        with patch.object(s.Reddit, 'page', return_value=(list(posts), None)), \
             patch.object(s, 'sg_listing', return_value=list(entries)):
            return s.discover(self.db, cfg, {}, None, ['sfw'])

    def test_new_rules_do_not_change_legacy_filter_or_configuration(self):
        cfg = self.config()
        before = copy.deepcopy(cfg)
        self.assertTrue(s.title_passes('[F4M] hypnosis [NSFW]', cfg)[0])
        self.assertFalse(sfw.qualify('[F4M] hypnosis [NSFW]', cfg, s)[0])
        self.assertFalse(s.title_passes('[F4A] sleep and comfort', cfg)[0])
        self.assertTrue(sfw.qualify('[F4A] sleep and comfort', cfg, s)[0])
        self.run_sources(cfg, [self.post(1)], [self.entry(1)])
        self.assertEqual(cfg, before)
        self.assertTrue(s.title_passes('[F4M] hypnosis [NSFW]', cfg)[0])
        self.assertFalse(s.title_passes('[F4A] sleep and comfort', cfg)[0])

    def test_all_four_categories_match(self):
        titles = ['[F4A] sleep aid', '[F4A] relaxation', '[F4A] personal attention', '[F4A] hypnosis']
        for title, name in zip(titles, ['sleep', 'relaxation', 'personal_attention', 'hypnosis']):
            self.assertIn(name, sfw.qualify(title, self.config(), s)[2])
        self.assertFalse(sfw.qualify('[F4A] entrance tickets', self.config(), s)[0])

    def test_profile_sfw_evidence_cannot_come_from_a_footer(self):
        cfg = self.config()
        for title in ['[F4A] Sleep', '[F4A] NSFW hypnosis', '[F4A] Sex [SFW] [Comfort]']:
            self.assertFalse(sfw.qualify(title, cfg, s, profile=True)[0])
        reports = self.run_sources(cfg, [], [self.entry(1, title='[F4A] Sleep',
                                          description='All SFW and NSFW audios in my archive')])
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 0)
        self.assertEqual(reports[-1]['rejected']['missing_sfw_recording_tag'], 1)

    def test_explicit_conflicts_and_script_offers_are_rejected(self):
        for title in ['[M4A] sleep [SFW]', '(F4F) sleep [SFW]', '[F4A] elf comfort [SFW]',
                      '[F4A] Script Offer - sleep', '[F4A] fantasy relaxation [SFW]']:
            self.assertFalse(sfw.qualify(title, self.config(), s, trusted=True)[0], title)
        self.assertTrue(sfw.qualify('Sleep (SFW | F4A)', self.config(), s)[0])

    def test_cross_source_dedup_initial_cap_and_repeat(self):
        cfg = self.config()
        posts = [self.post(i) for i in range(1, 6)]
        entries = [self.entry(i) for i in range(1, 7)]
        self.run_sources(cfg, posts, entries)
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 3)
        self.run_sources(cfg, posts, entries)
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 3)
        self.run_sources(cfg, [self.post(10, created_utc=3000)] + posts, [self.entry(10)] + entries)
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 4)

    def test_marked_nsfw_and_foreign_profile_cannot_enter_queue(self):
        posts = [self.post(1, over_18=True), self.post(2, url='https://soundgasm.net/u/Other/v2')]
        self.run_sources(self.config(), posts, [self.entry(3, url='https://soundgasm.net/u/Other/v3')])
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 0)

    def test_automatic_addition_requires_history_ratio_and_matching_identity(self):
        records = {str(i): {'published': i * 10 * 86400, 'owner': 'NewVoice', 'eligible': True}
                   for i in range(5)}
        self.assertTrue(sfw.auto_candidate(records, 'newvoice', {}))
        self.assertFalse(sfw.auto_candidate(records, 'differentvoice', {}))
        self.assertFalse(sfw.auto_candidate(dict(list(records.items())[:4]), 'newvoice', {}))
        recent = {k: dict(v, published=100 + i) for i, (k, v) in enumerate(records.items())}
        self.assertFalse(sfw.auto_candidate(recent, 'newvoice', {}))
        records.update({str(i): {'published': i * 10 * 86400, 'owner': 'NewVoice', 'eligible': False}
                        for i in range(5, 8)})
        self.assertFalse(sfw.auto_candidate(records, 'newvoice', {}))

    def test_automatic_creator_is_scoped_and_requires_explicit_speaker_tags(self):
        cfg = self.config()
        posts = [self.post(i, author='NewVoice', url=f'https://soundgasm.net/u/NewVoice/v{i}',
                           created_utc=i * 10 * 86400) for i in range(1, 6)]
        self.run_sources(cfg, posts)
        registry = s.meta_get(self.db, 'sfw_auto_creators')
        self.assertIn('newvoice', registry)
        self.assertFalse(registry['newvoice']['trusted_speaker'])
        self.assertNotIn('allowlist', cfg)
        self.assertEqual(self.db.execute("select count(*) from assets where creator='NewVoice'").fetchone()[0], 3)

    def test_run_limit_does_not_drop_deferred_profile_entries_or_bypass_initial_cap(self):
        cfg = self.config()
        cfg['sfw_expansion']['max_new_per_run'] = 1
        entries = [self.entry(i) for i in range(6)]
        for _ in range(6):
            self.run_sources(cfg, [], entries)
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 3)

    def test_disabling_expansion_does_not_call_network(self):
        cfg = self.config()
        cfg['sfw_expansion']['enabled'] = False
        with patch.object(s.Reddit, 'page') as page, patch.object(s, 'sg_listing') as listing:
            self.assertEqual(s.discover(self.db, cfg, {}, None, ['sfw']), [])
        page.assert_not_called()
        listing.assert_not_called()

    def test_deferred_reddit_recordings_survive_leaving_listing_window(self):
        cfg = self.config()
        cfg['sfw_expansion']['max_new_per_run'] = 1
        self.run_sources(cfg, [self.post(i, created_utc=3000+i) for i in range(3)])
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 1)
        self.run_sources(cfg, [])
        self.run_sources(cfg, [])
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 3)
        self.assertEqual(s.meta_get(self.db, 'sfw_reddit_deferred:sfw:reddit:AudioCandy'), {})

    def test_expanded_categories_retain_source_and_speaker_restrictions(self):
        for title in ['[F4A] Rain sounds [SFW]', '[F4A] Binaural tapping [SFW]',
                      '[F4A] Mindfulness [SFW]', '[F4A] Bedtime stories [SFW]']:
            self.assertTrue(sfw.qualify(title, self.config(), s, profile=True)[0])
            self.assertFalse(sfw.qualify(title.replace('[F4A]', '[M4A]'), self.config(), s, profile=True)[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
