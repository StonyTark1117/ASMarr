import unittest
from unittest.mock import patch

import acquisition_profiles as profiles
import scraper
import test_legacy_parsers as fixtures
import youtube_source


class ProfileRuleTests(unittest.TestCase):
    def test_legacy_reason_order_and_trusted_behavior_are_unchanged(self):
        cfg={'hypno_keywords':['hypno','trance'],'fantasy_blocklist':['elf']}
        cases=[('[M4F] hypnosis',False,(False,'male_speaker_tag')),
               ('[F4F] hypnosis',True,(False,'female_only_audience')),
               ('hypnosis',False,(False,'missing_speaker_tag')),
               ('hypnosis',True,(True,'accepted')),
               ('[F4M] everyday chat',False,(False,'missing_topic')),
               ('[F4A] hypnosis elves',False,(False,'blocked_term:elf')),
               ('<b>[F4M]</b> hypnosis for yourself',False,(True,'accepted'))]
        for title,trusted,expected in cases:
            self.assertEqual(expected,scraper.title_passes(title,cfg,trusted))

    def test_custom_speaker_and_audience_policy(self):
        cfg={'allowedSpeakers':['M'],'allowedAudiences':['F','A'],'requiredTopics':['sleep']}
        self.assertTrue(scraper.title_passes('[M4F] sleep',cfg)[0])
        self.assertEqual((False,'speaker_not_allowed'),scraper.title_passes('[F4M] sleep',cfg))
        self.assertEqual((False,'audience_not_allowed'),scraper.title_passes('[M4M] sleep',cfg))

    def test_nonbinary_and_multiple_audience_tags(self):
        cfg={'allowedSpeakers':['NB'],'allowedAudiences':['F','M'],'requiredTopics':[]}
        self.assertTrue(scraper.title_passes('[NB4FM] comforting attention',cfg)[0])
        self.assertFalse(scraper.title_passes('[NB4FMA] comforting attention',cfg)[0])

    def test_trusted_source_never_overrides_explicit_conflicts(self):
        cfg={'allowedSpeakers':['F'],'allowedAudiences':['M','A'],'requiredTopics':[]}
        self.assertFalse(scraper.title_passes('[F4M] [M4A] sleep',cfg,True)[0])
        self.assertFalse(scraper.title_passes('[F4F] sleep',cfg,True)[0])
        self.assertFalse(scraper.title_passes('[F4X] sleep',cfg,True)[0])
        self.assertFalse(scraper.title_passes('[X4M] sleep',cfg,True)[0])
        self.assertTrue(scraper.title_passes('sleep',cfg,True)[0])
        cfg['trustedMissingSpeakerTag']=False
        self.assertFalse(scraper.title_passes('sleep',cfg,True)[0])

    def test_any_speaker_audience_and_optional_missing_tags(self):
        cfg={'allowedSpeakers':['ANY'],'allowedAudiences':['ANY'],'requireSpeakerTag':False,'requiredTopics':[]}
        self.assertTrue(scraper.title_passes('[M4F] quiet story',cfg)[0])
        self.assertTrue(scraper.title_passes('quiet story',cfg)[0])

    def test_topics_use_literal_phrase_boundaries_and_any_or_all(self):
        cfg={'requiredTopics':['sleep','personal attention']}
        self.assertTrue(scraper.title_passes('[F4M] personal   attention',cfg)[0])
        self.assertFalse(scraper.title_passes('[F4M] sleepy chat',cfg)[0])
        cfg['topicMatch']='all'
        self.assertFalse(scraper.title_passes('[F4M] sleep',cfg)[0])
        self.assertTrue(scraper.title_passes('[F4M] sleep personal attention',cfg)[0])
        cfg['requiredTopics']=['.*']
        self.assertFalse(scraper.title_passes('[F4M] arbitrary text',cfg)[0])

    def test_exclusions_do_not_remove_legacy_fantasy_policy(self):
        cfg={'requiredTopics':[],'excludedTerms':['advertisement'],'fantasy_blocklist':['elf']}
        self.assertEqual((False,'excluded_term:advertisement'),scraper.title_passes('[F4M] advertisement',cfg))
        self.assertTrue(scraper.title_passes('[F4M] advertisementfree',cfg)[0])
        self.assertEqual((False,'blocked_term:elf'),scraper.title_passes('[F4M] elves',cfg))

    def test_explicit_topics_override_source_pattern_but_not_speaker_policy(self):
        cfg={'requiredTopics':['mathematics']}
        self.assertTrue(scraper.title_passes('[F4M] mathematics',cfg,topic_pattern=r'\bsleep\b')[0])
        self.assertFalse(scraper.title_passes('[M4A] mathematics',cfg,True,topic_pattern=r'\bsleep\b')[0])

    def test_invalid_profiles_fail_closed(self):
        invalid=[{'allowedSpeakers':[]},{'allowedAudiences':['unknown']},{'requiredTopics':'sleep'},
                 {'excludedTerms':['']},{'requireSpeakerTag':'false'},{'topicMatch':'regex'},{'topicMatch':[]},
                 {'minimumDuration':float('nan')},{'minimumDuration':-1},{'backlogLimit':True},
                 {'backlogLimit':2.5}]
        for settings in invalid:
            with self.subTest(settings=settings),self.assertRaises(ValueError):profiles.validate(settings)

    def test_discovery_translation_does_not_mutate_config_or_profile(self):
        base={'youtube_initial_downloads':3,'youtube_min_duration_seconds':180,'hypno_required':True}
        settings={'backlogLimit':1,'minimumDuration':600,'requiredTopics':['sleep'],'unrelated':'ignored'}
        result=profiles.discovery_config(base,settings)
        self.assertEqual(1,result['youtube_initial_downloads'])
        self.assertEqual(600,result['youtube_min_duration_seconds'])
        self.assertNotIn('unrelated',result)
        self.assertEqual(3,base['youtube_initial_downloads'])
        self.assertNotIn('youtube_initial_downloads',settings)


class ProfileDiscoveryTests(unittest.TestCase):
    setUp=fixtures.Tests.setUp
    tearDown=fixtures.Tests.tearDown

    def test_creator_profile_controls_youtube_initial_backlog_and_future_uploads(self):
        channel={'channel_id':'UC6gLlIAnzg7eJ8VuXDCZ_vg','creator':'Creator'}
        rules={'backlogLimit':1,'minimumDuration':300,'requiredTopics':['meditation']}
        cfg=dict(self.cfg,youtube_enabled=True,youtube_channels=[channel],
                 _profile_rules=lambda name: profiles.discovery_config(self.cfg,rules))
        entries=[{'id':'video000003','title':'meditation','duration':600},
                 {'id':'video000002','title':'meditation','duration':600},
                 {'id':'video000001','title':'meditation','duration':200}]
        with patch.object(youtube_source,'listing',return_value=entries):
            report=scraper.discover(self.db,cfg,{},None,['youtube'])[0]
        self.assertEqual(1,report['items']['queued'])
        self.assertEqual(1,report['items']['older_backlog_not_imported'])
        self.assertEqual(1,report['rejected']['youtube_short_or_unknown_duration'])
        future={'id':'video000004','title':'meditation','duration':600}
        with patch.object(youtube_source,'listing',return_value=[future]+entries):
            report=scraper.discover(self.db,cfg,{},None,['youtube'])[0]
        self.assertEqual(1,report['items']['queued'])
        self.assertEqual(2,self.db.execute('SELECT count(*) FROM assets').fetchone()[0])


if __name__=='__main__': unittest.main()
