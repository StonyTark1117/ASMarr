"""Verify installed providers against version-matched, isolated outage fixtures.

prepare creates a fixture archive from immutable Git objects. verify runs only
the named offline tests, never real source/client/Plex requests or live DB edits.
This proves runtime recovery, not live acquisition or production repeat acceptance.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

from release_integrity import artifact_hash

TESTS = (
    'test_acquisition.AcquisitionTests.test_direct_outage_retries_before_fallback',
    'test_acquisition.AcquisitionTests.test_qbittorrent_outage_preserves_job',
    'test_legacy_parsers.Tests.test_plex_retry_even_without_new_download',
    'test_acquisition.AcquisitionTests.test_completed_import_is_verified_and_idempotent',
    'test_acquisition.AcquisitionTests.test_interrupted_copy_preserves_source_and_cleans_partial',
    'test_playlists.PlaylistTests.test_interrupted_creation_recovers_without_duplicate',
)


def blob_hash(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def prepare(repository, commit, output):
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('full_source_commit_required')
    source = subprocess.run(['git', '-C', str(repository), 'rev-parse', commit+'^{commit}'],
                            check=True, capture_output=True, text=True).stdout.strip()
    if source != commit:
        raise ValueError('source_commit_mismatch')
    # Build from Git, not potentially dirty working-tree files.
    archive = subprocess.run(['git', '-C', str(repository), 'archive', commit, 'tests'],
                             check=True, capture_output=True).stdout
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as original:
        for member in original.getmembers():
            if member.isfile() and member.name.startswith('tests/') and member.name.endswith('.py'):
                files[member.name] = original.extractfile(member).read()
    manifest = {'sourceCommit': commit, 'files': {name: blob_hash(data) for name, data in files.items()}}
    with output.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as target:
        output.chmod(0o600)
        for name, data in dict(files, **{'fixture-manifest.json':json.dumps(manifest).encode()}).items():
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
            target.addfile(info, io.BytesIO(data))
    return manifest


def verify_fixture_files(root, deployment):
    root = Path(root)
    manifest = json.loads((root/'fixture-manifest.json').read_text())
    if manifest.get('sourceCommit') != deployment.get('sourceCommit'):
        raise ValueError('fixtures_do_not_match_deployed_source_commit')
    if not manifest.get('files'):
        raise ValueError('fixture_manifest_empty')
    for name, expected in manifest['files'].items():
        path = root/name
        if not name.startswith('tests/') or not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError('fixture_path_outside_bundle')
        if blob_hash(path.read_bytes()) != expected:
            raise ValueError('fixture_git_blob_mismatch')
    actual = {p.relative_to(root).as_posix() for p in (root/'tests').rglob('*.py')}
    if actual != set(manifest['files']):
        raise ValueError('fixture_bundle_contains_unverified_python')
    return manifest


class EvidenceResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.executed.append(test.id())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = []


def verify(root, application, deployment):
    manifest = verify_fixture_files(root, deployment)
    before = artifact_hash(application)
    if before != deployment.get('artifactSha256'):
        raise ValueError('deployed_artifact_does_not_match_manifest')
    sys.path.insert(0, str(Path(root)/'tests'))
    sys.path.insert(0, str(Path(application)/'providers'))
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(TESTS)
    with patch('socket.socket.connect', side_effect=AssertionError('runtime_fixtures_network_forbidden')):
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=2, resultclass=EvidenceResult).run(suite)
    # The loaders must import the installed code, not repository provider copies.
    provider_root = (Path(application)/'providers').resolve()
    for name in ('bridge', 'scraper', 'plex_playlists'):
        module = sys.modules.get(name)
        if not module or not Path(module.__file__).resolve().is_relative_to(provider_root):
            raise ValueError('fixture_did_not_use_installed_provider')
    after = artifact_hash(application)
    fixture_manifest_after = verify_fixture_files(root, deployment)
    passed = result.wasSuccessful() and not result.skipped and tuple(result.executed) == TESTS and before == after and manifest == fixture_manifest_after
    return {'scope': 'isolated installed-provider outages and recovery; live canaries still required',
            'recordedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'testedCommit': deployment['sourceCommit'],
            'artifactSha256': before, 'fixtureManifest': manifest, 'tests': result.executed,
            'testsRun': result.testsRun, 'failures': [test.id() for test, _ in result.failures],
            'errors': [test.id() for test, _ in result.errors], 'skipped': [test.id() for test, _ in result.skipped],
            'artifactUnchanged': before == after, 'networkPolicy': 'connections blocked', 'passed': passed}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='operation', required=True)
    stage = commands.add_parser('prepare')
    stage.add_argument('--repository', type=Path, required=True)
    stage.add_argument('--commit', required=True)
    stage.add_argument('--output', type=Path, required=True)
    run = commands.add_parser('verify')
    run.add_argument('--fixtures', type=Path, required=True)
    run.add_argument('--application', type=Path, default=Path('/opt/asmarr'))
    run.add_argument('--deployment', type=Path, default=Path('/var/lib/asmarr/acceptance/deployed-release.json'))
    run.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'prepare':
        manifest = prepare(args.repository, args.commit, args.output)
        print(json.dumps({'sourceCommit':manifest['sourceCommit'], 'fixtureFiles':len(manifest['files']), 'archive':str(args.output)}))
    else:
        from plex_invariants import write_protected
        report = verify(args.fixtures, args.application, json.loads(args.deployment.read_text()))
        write_protected(args.output, report)
        print(json.dumps({'evidence':str(args.output), 'testsRun':report['testsRun'], 'passed':report['passed']}))
        if not report['passed']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
