import json
import sqlite3
import unittest
import source_configuration as sources


class SourceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE commands(name TEXT,arguments TEXT,created TEXT,finished TEXT,state TEXT,result TEXT);')

    def tearDown(self):
        self.db.close()

    def test_absent_overrides_preserve_original_behavior(self):
        original = {'youtube_enabled': True, 'sfw_expansion': {'creators': [{'reddit': 'quiet'}]}, 'secrets_file': '/protected'}
        effective = sources.apply(self.db, original)
        self.assertEqual(effective, original)
        effective['sfw_expansion']['creators'].clear()
        self.assertEqual(len(original['sfw_expansion']['creators']), 1)

    def test_nested_overrides_preserve_protected_and_unedited_fields(self):
        sources.save(self.db, 'sfw', {'reddit_pages': 4})
        effective = sources.apply(self.db, {'sfw_expansion': {'enabled': True, 'creators': ['quiet']}})
        self.assertEqual(effective['sfw_expansion'], {'enabled': True, 'creators': ['quiet'], 'reddit_pages': 4})

    def test_configuration_validation_fails_closed(self):
        for kind, value in [('youtube', {'yt_dlp': '/tmp/tool'}), ('reddit', {'client_secret': 'secret'}), ('reddit', {'reddit_max_pages': True}), ('soundgasm', {'soundgasm_creators': ['../escape']}), ('youtube', {'youtube_poll_limit': 0}), ('unknown', {})]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                sources.save(self.db, kind, value)
        self.assertEqual(self.db.execute('SELECT count(*) FROM settings').fetchone()[0], 0)

    def test_snapshot_exposes_only_source_cursors_and_safe_results(self):
        self.db.executemany('INSERT INTO meta VALUES(?,?)', [('reddit_checkpoint:asmr', '{"watermark":123}'), ('private', '"secret"'), ('youtube_seen:channel', '["video"]')])
        self.db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)', ('source-test', '{"kind":"reddit"}', '2026-10-07', None, 'completed', '{"status":"healthy","kind":"reddit","token":"secret"}'))
        public = sources.snapshot(self.db, {'client_secret': 'secret', 'request_interval': 2}, 'reddit')
        self.assertNotIn('secret', json.dumps(public))
        self.assertEqual(public['checkpoints'], [{'key': 'reddit_checkpoint:asmr', 'value': {'watermark': 123}}])
        self.assertEqual(public['rateLimits']['minimumHttpIntervalSeconds'], 2)
        self.assertIsNone(public['rateLimits']['remaining'])

    def test_runtime_uses_saved_filters(self):
        sources.save(self.db, 'reddit', {'subreddits': ['asmr'], 'reddit_max_pages': 2})
        self.assertEqual(sources.apply(self.db, {})['reddit_max_pages'], 2)
        self.assertEqual(sources.snapshot(self.db, {}, 'reddit')['configuration']['subreddits'], ['asmr'])
