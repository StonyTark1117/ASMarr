"""Offline metadata-fixture checks; no Internet or production clients."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec=importlib.util.spec_from_file_location('canary_torznab',Path(__file__).resolve().parents[1]/'ops/canary_torznab.py')
canary=importlib.util.module_from_spec(spec);spec.loader.exec_module(canary)


class CanaryMetadataTests(unittest.TestCase):
    def test_bencoding_is_canonical_and_rejects_unknown_types(self):
        self.assertEqual(b'd1:ai1e1:bl1:xee',canary.bencode({b'b':[b'x'],b'a':1}))
        with self.assertRaises(ValueError):canary.bencode(None)

    def test_single_file_torrent_has_exact_piece_identity_and_public_webseed(self):
        import bridge
        data=b'public domain fixture'*(30000)
        payload,identifier=canary.torrent(data)
        self.assertEqual(identifier,bridge.torrent_hash(payload))
        self.assertIn(canary.SOURCE.encode(),payload)
        self.assertIn(hashlib.sha1(data[:256*1024]).digest(),payload)
        self.assertIn(hashlib.sha1(data[512*1024:]).digest(),payload)
        self.assertNotIn(b'5:files',payload)

    def test_torrent_rejects_empty_or_nonapproved_source(self):
        with self.assertRaises(ValueError):canary.torrent(b'')
        with self.assertRaises(ValueError):canary.torrent(b'data','https://untrusted.invalid/file.mp3')

    def test_search_is_exactly_scoped_to_public_canary(self):
        manifest={'title':canary.TITLE,'infoHash':'a'*40,'size':50}
        matched=ET.fromstring(canary.feed(manifest,'http://127.0.0.1:18787','chris goringe raven'))
        self.assertEqual(1,len(matched.findall('.//item')))
        self.assertEqual('http://127.0.0.1:18787/canary.torrent',matched.find('.//enclosure').get('url'))
        self.assertEqual(0,len(ET.fromstring(canary.feed(manifest,'http://127.0.0.1:18787','unrelated')).findall('.//item')))

    def test_caps_expose_audio_and_ordinary_search(self):
        caps=ET.fromstring(canary.CAPS)
        self.assertEqual('yes',caps.find('./searching/search').get('available'))
        self.assertEqual('3030',caps.find('.//subcat').get('id'))

    def test_serving_rejects_changed_torrent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'canary.torrent').write_bytes(b'changed')
            (root/'manifest.json').write_text(json.dumps({'torrentSha256':'0'*64}))
            with self.assertRaisesRegex(ValueError,'canary_torrent_changed'):canary.handler(root,'http://127.0.0.1:18787')


if __name__=='__main__':unittest.main()
