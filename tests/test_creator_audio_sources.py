import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import creator_audio_sources as sources
import hotaudio_capture
import scraper


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
        with self.assertRaisesRegex(sources.SourceFailure, 'download_unavailable'):
            sources.resolve_audio(HTTP([Response(body={'url': 'http://cdn.whyp.it/track.mp3'})]),
                                  'whyp', 'https://whyp.it/tracks/title-ABC123')

    def test_audiochan_requires_public_metadata_and_fresh_play_intent(self):
        item = {'id': 'audio-id', 'visibility': 'public', 'status': 'published',
                'audioFile': {'id': 'file-id'}}
        http = HTTP([Response(), Response(body=item), Response(body={'play_intent': 'intent'}),
                     Response(body={'url': 'https://stream.audiochan.com/path?expires=123'})])
        media, headers = sources.resolve_audio(http, 'audiochan', 'https://audiochan.com/a/ABC123')
        self.assertEqual(media.split('?')[0], 'https://stream.audiochan.com/path')
        self.assertEqual([call[0] for call in http.calls], ['POST', 'GET', 'POST', 'POST'])
        self.assertEqual(http.calls[0][1], 'https://api.audiochan.com/auth/anonymous')
        self.assertEqual(http.calls[3][2]['json']['play_intent'], 'intent')
        self.assertEqual(headers['Referer'], 'https://audiochan.com/')
        self.assertEqual(headers['User-Agent'], sources.AUDIOCHAN_UA)
        self.assertEqual(headers['Range'], 'bytes=0-')
        self.assertTrue(all(call[2]['headers']['User-Agent'] == sources.AUDIOCHAN_UA
                            for call in http.calls))
        with self.assertRaisesRegex(sources.SourceFailure, 'audio_unavailable'):
            sources.resolve_audio(HTTP([Response(), Response(body=dict(item, visibility='private'))]),
                                  'audiochan', 'https://audiochan.com/a/ABC123')
        with self.assertRaisesRegex(sources.SourceFailure, 'stream_unavailable'):
            sources.resolve_audio(HTTP([Response(), Response(body=item),
                                        Response(body={'play_intent': 'intent'}),
                                        Response(body={'url': 'http://stream.audiochan.com/path'})]),
                                  'audiochan', 'https://audiochan.com/a/ABC123')

    def test_audiochan_rechecks_tier_after_discovery_before_playback(self):
        public = {'id': 'audio-id', 'visibility': 'public', 'status': 'published',
                  'audioFile': {'id': 'file-id'}}
        for detail in (dict(public, required_tier_id='paid-tier'),
                       dict(public, requiredTierId='paid-tier'),
                       dict(public, required_patreon_tier='paid-tier'),
                       dict(public, audioFile={'id': 'file-id', 'isLocked': True})):
            with self.subTest(detail=detail):
                http = HTTP([Response(), Response(body=detail)])
                with self.assertRaisesRegex(sources.SourceFailure, 'audio_unavailable'):
                    sources.resolve_audio(http, 'audiochan', 'https://audiochan.com/a/ABC123')
                self.assertEqual([call[0] for call in http.calls], ['POST', 'GET'])

    def test_hotaudio_uses_separate_browser_capture(self):
        with self.assertRaisesRegex(sources.SourceFailure, 'requires_browser_capture'):
            sources.resolve_audio(HTTP([]), 'hotaudio', 'https://hotaudio.net/u/creator/post')

    def test_creator_audio_targets_need_runtime_approval_before_network(self):
        cases = [('whyp', 'https://whyp.it/tracks/title-ABC123'),
                 ('audiochan', 'https://audiochan.com/a/ABC123'),
                 ('hotaudio', 'https://hotaudio.net/u/creator/post')]
        with tempfile.TemporaryDirectory() as directory:
            for kind, url in cases:
                asset = {'key': kind + ':fixture', 'creator': 'Example', 'title': 'Fixture',
                         'published': 0, 'targets': json.dumps([[kind, url]])}
                http = HTTP([])
                with mock.patch.object(hotaudio_capture, 'capture',
                                       side_effect=AssertionError('browser reached')) as browser:
                    with self.assertRaisesRegex(scraper.SourceError, 'source_target_not_approved'):
                        scraper.save_asset(asset, {'output_root': directory}, http)
                    browser.assert_not_called()
                self.assertEqual(http.calls, [])

    def test_hotaudio_rejects_nonpublic_track_urls(self):
        for url in ('http://hotaudio.net/u/creator/post',
                    'https://example.com/u/creator/post',
                    'https://hotaudio.net/account/private'):
            with self.assertRaisesRegex(hotaudio_capture.CaptureError, 'invalid_hotaudio_track'):
                hotaudio_capture.capture(url, '/tmp/unused.m4a', max_bytes=1024)

    def test_hotaudio_rejects_invalid_capture_limits(self):
        url = 'https://hotaudio.net/u/creator/public-post'
        with self.assertRaisesRegex(hotaudio_capture.CaptureError, 'limit_invalid'):
            hotaudio_capture.capture(url, '/tmp/unused.m4a', max_bytes=0)

    def test_hotaudio_partial_capture_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            def incomplete(_, destination, **kwargs):
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                                '-i', 'sine=frequency=440:duration=1', '-c:a', 'aac',
                                '-f', 'ipod', str(destination)], check=True)
                return 10.0
            asset = {'key': 'hotaudio:short', 'creator': 'Example', 'title': 'Short',
                     'published': 0, 'targets': json.dumps([['hotaudio',
                        'https://hotaudio.net/u/creator/public-post']])}
            url = 'https://hotaudio.net/u/creator/public-post'
            with mock.patch.object(hotaudio_capture, 'capture', side_effect=incomplete):
                with self.assertRaisesRegex(scraper.SourceError, 'hotaudio_capture_incomplete'):
                    scraper.save_asset(asset, {'output_root': directory,
                                               '_authorized_creator_targets': [['hotaudio', url]]}, HTTP([]))
            self.assertFalse(list(Path(directory).rglob('*.m4a')))
            self.assertFalse(list(Path(directory).rglob('*.part')))

    def test_hotaudio_size_failure_removes_partial(self):
        url = 'https://hotaudio.net/u/creator/public-post'
        asset = {'key': 'hotaudio:short', 'creator': 'Example', 'title': 'Short',
                 'published': 0, 'targets': json.dumps([['hotaudio', url]])}
        def oversized(_, destination, **kwargs):
            Path(destination).write_bytes(b'partial')
            raise hotaudio_capture.CaptureError('hotaudio_capture_size_limit')
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(hotaudio_capture, 'capture', side_effect=oversized):
                with self.assertRaisesRegex(scraper.SourceError, 'hotaudio_capture_size_limit'):
                    scraper.save_asset(asset, {'output_root': directory,
                                               '_authorized_creator_targets': [['hotaudio', url]]}, HTTP([]))
            self.assertFalse(list(Path(directory).rglob('*.part')))

    def test_hotaudio_capture_script_rejects_before_copy(self):
        source = hotaudio_capture.CAPTURE_SCRIPT.replace('__ASMARR_MAX_BYTES__', '4')
        script = '''globalThis.global = globalThis;
          global.window = global;
          global.Audio = function() {};
          global.MediaSource = function() {};
          let nativeCalls = 0;
          MediaSource.prototype.addSourceBuffer = function() {
            return {appendBuffer() { nativeCalls++; }};
          };
          eval(''' + json.dumps(source) + ''');
          const buffer = new MediaSource().addSourceBuffer('audio/mp4');
          buffer.appendBuffer(new Uint8Array(4));
          let rejected = false;
          try { buffer.appendBuffer(new Uint8Array(5)); }
          catch (error) { rejected = error instanceof RangeError; }
          console.log(JSON.stringify({rejected, nativeCalls,
            bytes: window.__asmarrCapture.bytes,
            chunks: window.__asmarrCapture.chunks.length,
            overflow: window.__asmarrCapture.overflow}));'''
        bundled_deno = Path(hotaudio_capture.__file__).resolve().parents[1] / 'toolchain' / 'deno'
        deno = str(bundled_deno) if bundled_deno.is_file() else shutil.which('deno')
        node = shutil.which('node')
        if deno:
            command = [deno, 'eval', script]
        elif node:
            command = [node, '-e', script]
        else:
            self.fail('JavaScript runtime unavailable for capture safety test')
        result = subprocess.run(command, check=True,
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout),
                         {'rejected': True, 'nativeCalls': 1,
                          'bytes': 4, 'chunks': 1, 'overflow': True})


if __name__ == '__main__':
    unittest.main()
