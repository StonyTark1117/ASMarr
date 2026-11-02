import unittest

import creator_audio_sources as sources


class Response:
    def __init__(self, *, text='', body=None):
        self.text = text
        self.body = body
        self.encoding = 'utf-8'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def json(self):
        return self.body


class HTTP:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class CreatorAudioSourceTests(unittest.TestCase):
    def test_whyp_listing_follows_cursor_and_deduplicates_pins(self):
        first_track = {'public_id': 'ABC123', 'title': 'Owned', 'url': '/tracks/owned-ABC123'}
        second_track = {'public_id': 'DEF456', 'title': 'Older', 'url': '/tracks/older-DEF456'}
        http = HTTP([Response(body={'pinned': [first_track], 'tracks': [first_track],
                                    'next_cursor': 'cursor'}),
                     Response(body={'pinned': [], 'tracks': [second_track], 'next_cursor': None})])
        posts = sources.profile_listing(http, 'whyp', '@creator')
        self.assertEqual([(p['id'], p['title']) for p in posts],
                         [('ABC123', 'Owned'), ('DEF456', 'Older')])
        self.assertEqual(http.calls[1][2]['params'], {'cursor': 'cursor'})

    def test_audiochan_paginates_and_rejects_locked_or_nonpublic_entries(self):
        def item(identity, **overrides):
            return {'id': identity, 'title': 'Public audio', 'slug': identity * 6,
                    'content_type': 'audio', 'visibility': 'public', 'status': 'published',
                    'audio_file_id': 'file', 'required_tier_id': None, **overrides}
        first = {'data': [item('a'), item('b', visibility='private'),
                          item('c', required_tier_id='tier')], 'has_more': True}
        second = {'data': [item('d')], 'has_more': False}
        http = HTTP([Response(body=first), Response(body=second)])
        posts = sources.profile_listing(http, 'audiochan', 'creator')
        self.assertEqual([p['id'] for p in posts], ['a', 'd'])
        self.assertEqual(len(http.calls), 2)

    def test_whyp_only_uses_enabled_official_download(self):
        http = HTTP([Response(body={'url': 'https://cdn.whyp.it/track.mp3?expires=123'})])
        media, headers = sources.resolve_audio(http, 'whyp', 'https://whyp.it/tracks/title-ABC123')
        self.assertEqual(media.split('?')[0], 'https://cdn.whyp.it/track.mp3')
        self.assertEqual(headers, {})
        self.assertIn('/download', http.calls[0][1])
        with self.assertRaisesRegex(sources.SourceFailure, 'download_unavailable'):
            sources.resolve_audio(HTTP([Response(body={'url': None})]),
                                  'whyp', 'https://whyp.it/tracks/title-ABC123')

    def test_audiochan_requires_public_metadata_and_fresh_play_intent(self):
        item = {'id': 'audio-id', 'visibility': 'public', 'status': 'published',
                'audioFile': {'id': 'file-id'}}
        http = HTTP([Response(body=item), Response(body={'play_intent': 'intent'}),
                     Response(body={'url': 'https://stream.audiochan.com/path?expires=123'})])
        media, headers = sources.resolve_audio(http, 'audiochan', 'https://audiochan.com/a/ABC123')
        self.assertEqual(media.split('?')[0], 'https://stream.audiochan.com/path')
        self.assertEqual([call[0] for call in http.calls], ['GET', 'POST', 'POST'])
        self.assertEqual(http.calls[2][2]['json']['play_intent'], 'intent')
        self.assertEqual(headers['Referer'], 'https://audiochan.com/')
        with self.assertRaisesRegex(sources.SourceFailure, 'audio_unavailable'):
            sources.resolve_audio(HTTP([Response(body=dict(item, visibility='private'))]),
                                  'audiochan', 'https://audiochan.com/a/ABC123')

    def test_hotaudio_capture_is_not_reported_as_working(self):
        with self.assertRaisesRegex(sources.SourceFailure, 'capture_unverified'):
            sources.resolve_audio(HTTP([]), 'hotaudio', 'https://hotaudio.net/u/creator/post')


if __name__ == '__main__':
    unittest.main()
