import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ops'))
import runtime_fixtures as fixtures


class RuntimeFixtureSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'tests').mkdir()
        (self.root/'tests/test_example.py').write_bytes(b'# approved fixture\n')
        self.deployment = {'sourceCommit':'a'*40}
        self.manifest = {'sourceCommit':'a'*40, 'files':{'tests/test_example.py':fixtures.blob_hash(b'# approved fixture\n')}}
        (self.root/'fixture-manifest.json').write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_git_blob_bundle_passes(self):
        self.assertEqual(fixtures.verify_fixture_files(self.root, self.deployment), self.manifest)

    def test_fixture_commit_must_match_deployed_release(self):
        with self.assertRaisesRegex(ValueError, 'source_commit'):
            fixtures.verify_fixture_files(self.root, {'sourceCommit':'b'*40})

    def test_modified_fixture_cannot_claim_approved_git_blob(self):
        (self.root/'tests/test_example.py').write_bytes(b'# altered\n')
        with self.assertRaisesRegex(ValueError, 'git_blob'):
            fixtures.verify_fixture_files(self.root, self.deployment)

    def test_unlisted_python_file_is_not_importable_evidence(self):
        (self.root/'tests/test_unapproved.py').write_bytes(b'# unapproved\n')
        with self.assertRaisesRegex(ValueError, 'unverified_python'):
            fixtures.verify_fixture_files(self.root, self.deployment)

    def test_symlinked_fixture_is_rejected(self):
        path = self.root/'tests/test_example.py'
        path.unlink();path.symlink_to(self.root/'fixture-manifest.json')
        with self.assertRaisesRegex(ValueError, 'outside_bundle'):
            fixtures.verify_fixture_files(self.root, self.deployment)

    def test_path_traversal_in_manifest_is_rejected(self):
        self.manifest['files'] = {'tests/../../escape.py':'0'*40}
        (self.root/'fixture-manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'outside_bundle'):
            fixtures.verify_fixture_files(self.root, self.deployment)

    def test_empty_manifest_cannot_pass_without_tests(self):
        self.manifest['files'] = {}
        (self.root/'fixture-manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'empty'):
            fixtures.verify_fixture_files(self.root, self.deployment)
