import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import scraper as s
import test_legacy_parsers as fixtures
import youtube_source as y


CHANNEL = {'channel_id': 'UC6gLlIAnzg7eJ8VuXDCZ_vg', 'creator': 'Gentle Whispering ASMR'}


class YouTubeTests(unittest.TestCase):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown
    sample = fixtures.Tests.sample

    def config(self):
        return dict(self.cfg, youtube_enabled=True, youtube_channels=[CHANNEL],
                    youtube_initial_downloads=3)

    def entries(self, start=9):
        return [{'id': f'video{i:06d}', 'title': 'ASMR Relaxing Personal Attention',
                 'duration': 600} for i in range(start, 0, -1)]

    def test_trust_is_only_missing_tag_override(self):
        cfg = self.config()
        self.assertTrue(s.title_passes('ASMR relaxing sleep', cfg, True, y.TOPIC_PATTERN)[0])
        for title in ['[M4A] ASMR sleep', '[F4F] ASMR sleep', 'ASMR elf', 'entrance tickets']:
            self.assertFalse(s.title_passes(title, cfg, True, y.TOPIC_PATTERN)[0], title)
        self.assertFalse(s.title_passes('ASMR relaxing sleep', cfg)[0])

    def test_initial_cap_new_uploads_and_queue_survive_repeat(self):
        cfg = self.config()
        with patch.object(y, 'listing', return_value=self.entries()):
            report = s.discover(self.db, cfg, {}, None, ['youtube'])[0]
            self.assertEqual(report['items']['queued'], 3)
            self.assertEqual(report['items']['older_backlog_not_imported'], 6)
            s.discover(self.db, cfg, {}, None, ['youtube'])
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 3)
        with patch.object(y, 'listing', return_value=self.entries(10)):
            report = s.discover(self.db, cfg, {}, None, ['youtube'])[0]
        self.assertEqual(report['items']['queued'], 1)
        self.assertEqual(self.db.execute("select count(*) from assets where state='pending'").fetchone()[0], 4)

    def test_failed_poll_does_not_advance_checkpoint(self):
        cfg = self.config()
        with patch.object(y, 'listing', side_effect=y.YouTubeError('youtube_rate_limited')):
            report = s.discover(self.db, cfg, {}, None, ['youtube'])[0]
        self.assertEqual(report['status'], 'unavailable')
        self.assertIsNone(s.meta_get(self.db, 'youtube_seen:' + CHANNEL['channel_id']))

    def test_early_access_is_rechecked_when_it_becomes_public(self):
        cfg = self.config()
        entries = self.entries()
        entries[0]['availability'] = 'subscriber_only'
        with patch.object(y, 'listing', return_value=entries):
            s.discover(self.db, cfg, {}, None, ['youtube'])
        self.assertNotIn(entries[0]['id'], s.meta_get(self.db, 'youtube_seen:' + CHANNEL['channel_id']))
        key = 'youtube_seen:' + CHANNEL['channel_id']
        s.meta_set(self.db, key, s.meta_get(self.db, key) + [entries[0]['id']])
        with patch.object(y, 'listing', return_value=entries):
            s.discover(self.db, cfg, {}, None, ['youtube'])
        self.assertNotIn(entries[0]['id'], s.meta_get(self.db, key))
        entries[0]['availability'] = 'public'
        with patch.object(y, 'listing', return_value=entries):
            report = s.discover(self.db, cfg, {}, None, ['youtube'])[0]
        self.assertEqual(report['items']['queued'], 1)
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 4)

    def test_gap_expands_poll_before_advancing(self):
        cfg = self.config()
        s.meta_set(self.db, 'youtube_seen:' + CHANNEL['channel_id'], ['video000001'])
        with patch.object(y, 'listing', side_effect=[self.entries(10)[:2], self.entries(10)]) as listing:
            report = s.discover(self.db, cfg, {}, None, ['youtube'])[0]
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(report['items']['queued'], 9)
        before = s.meta_get(self.db, 'youtube_seen:' + CHANNEL['channel_id'])
        new = [{'id': 'newvid00001', 'title': 'ASMR', 'duration': 600}]
        with patch.object(y, 'listing', return_value=new):
            report = s.discover(self.db, cfg, {}, None, ['youtube'])[0]
        self.assertEqual(report['error'], 'youtube_listing_gap_requires_review')
        self.assertEqual(s.meta_get(self.db, 'youtube_seen:' + CHANNEL['channel_id']), before)

    def test_nonpublic_live_and_short_uploads_excluded(self):
        for changes in [{'availability': 'subscriber_only'}, {'live_status': 'is_live'},
                        {'live_status': 'is_upcoming'}, {'duration': 60}, {'age_limit': 18},
                        {'title': 'ASMR Channel Update'}]:
            entry = dict(self.entries()[0], **changes)
            self.assertFalse(y.eligibility(entry, self.config())[0], changes)

    def test_identity_is_verified_at_listing_and_download(self):
        with patch.object(y, 'run_json', return_value={'channel_id': 'foreign', 'entries': []}):
            with self.assertRaisesRegex(y.YouTubeError, 'identity_mismatch'):
                y.listing(CHANNEL, self.config())
        with patch.object(y, 'run_json', return_value={'channel_id': 'foreign'}):
            with self.assertRaisesRegex(y.YouTubeError, 'not_approved'):
                y.metadata('https://www.youtube.com/watch?v=video000001', self.config())

    def test_saved_file_is_audio_only_tagged_and_crash_recoverable(self):
        cfg = self.config()
        url = 'https://www.youtube.com/watch?v=video000001'
        s.enqueue(self.db, 'youtube:video000001', 'youtube:' + CHANNEL['channel_id'],
                  CHANNEL['creator'], 'ASMR relaxing sleep', [['youtube', url]])
        info = {'title': 'ASMR relaxing sleep', 'channel_id': CHANNEL['channel_id'],
                'upload_date': '20260920', 'duration': 600, 'id': 'video000001'}
        def download(info, work, config):
            output = Path(work) / 'audio.m4a'
            self.sample(output)
            return output
        asset = self.db.execute('select * from assets').fetchone()
        with patch.object(y, 'metadata', return_value=(info, CHANNEL)), patch.object(y, 'download', side_effect=download) as fetch:
            output, _ = s.save_asset(asset, cfg, None)
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(output.is_file())
            self.assertEqual([x['codec_type'] for x in s.validate_audio(output)['streams']], ['audio'])
            tags = s.mutagen.File(output, easy=True)
            self.assertEqual(tags['artist'], [CHANNEL['creator']])
            self.assertEqual(tags['date'], ['2026-09-20'])
            self.assertEqual(s.save_asset(asset, cfg, None)[0], output)
            self.assertEqual(fetch.call_count, 1)
        self.assertFalse(list(Path(cfg['output_root']).rglob('*.part')))

    def test_canonical_youtube_dedup_across_sources(self):
        urls = ['https://youtu.be/video000001?t=1',
                'https://www.youtube.com/watch?v=video000001&list=abc']
        for i, url in enumerate(urls):
            s.enqueue(self.db, str(i), 'test', CHANNEL['creator'], 'ASMR', [['youtube', url]])
        self.assertEqual(self.db.execute('select count(*) from assets').fetchone()[0], 1)

    def test_disabled_youtube_does_not_poll(self):
        with patch.object(y, 'listing') as listing:
            s.discover(self.db, dict(self.config(), youtube_enabled=False), {}, None, ['youtube'])
        listing.assert_not_called()

    def test_channel_subscription_does_not_enable_arbitrary_reddit_video_links(self):
        post = {'url': 'https://www.youtube.com/watch?v=video000001'}
        self.assertEqual(s.targets(post, self.config()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
