"""Package integrity and empty-install guards; no host/package-manager mutations."""
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ops'))
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/('ops/'+name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
packager,installer,setup=load('package_release'),load('install_release'),load('complete_setup')


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=self.root/'repository';self.repo.mkdir()
        for name in ('ops','packaging','frontend'):(self.repo/name).mkdir()
        (self.repo/'ops/admin_login.py').write_text('generated fixture helper')
        (self.repo/'packaging/asmarr.service').write_text('[Service]\n')
        (self.repo/'frontend/package.json').write_text('{"dependencies":{}}')
        self.tools=self.root/'tools';self.tools.mkdir()
        pins={}
        for name in ('yt-dlp','deno'):
            data=('generated fixture '+name).encode();(self.tools/name).write_bytes(data)
            (self.tools/(name+'-LICENSE.txt')).write_text('fixture notice')
            pins[name]={'binarySha256':hashlib.sha256(data).hexdigest()}
        (self.repo/'packaging/toolchain.json').write_text(json.dumps(pins))
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','Generated fixture'],check=True)
        self.commit=subprocess.check_output(['git','-C',str(self.repo),'rev-parse','HEAD'],text=True).strip()
        self.publish=self.root/'publish';self.publish.mkdir();(self.publish/'providers').mkdir()
        (self.publish/'providers/bridge.py').write_text('fixture provider')
        for name in ('Prefix','Prefix.Core'):
            (self.publish/name).mkdir();(self.publish/name/'notice.txt').write_text('fixture notice')
        (self.publish/'ASMarr').write_text('fixture application');(self.publish/'ASMarr').chmod(0o755)
        (self.publish/'ASMarr.deps.json').write_text('{"libraries":{"GeneratedFixture/42.0.0":{"type":"package"}}}')
        (self.publish/'build-identity.json').write_text(json.dumps({'sourceCommit':self.commit,'version':'0.3.0'}))

    def package(self,folder='output'):
        return packager.build(self.repo,self.publish,self.tools,self.root/folder,self.commit,'0.3.0')

    def test_package_is_source_bound_verified_and_reproducible(self):
        first=self.package();second=self.package('second')
        self.assertEqual(first['sha256'],second['sha256'])
        output=self.root/'unpacked';output.mkdir()
        result=installer.unpack(first['package'],first['sha256'],output)
        self.assertEqual(self.commit,result['sourceCommit'])
        self.assertEqual('0.3.0',result['version'])
        self.assertTrue(first['package'].endswith('asmarr-0.3.0-linux-x64.tar.gz'))
        self.assertEqual(first['artifactSha256'],result['artifactSha256'])
        self.assertEqual(installer.artifact_hash(output/'application'),result['artifactSha256'])
        self.assertTrue((output/'application/ops/admin_login.py').is_file())
        self.assertEqual(0o755,(output/'application/toolchain/deno').stat().st_mode&0o777)
        with tarfile.open(first['productionPayload']) as archive:
            self.assertIn('ASMarr',archive.getnames());self.assertNotIn('application/ASMarr',archive.getnames())

    def test_bad_checksum_and_tampered_toolchain_rejected(self):
        package=self.package();output=self.root/'bad';output.mkdir()
        with self.assertRaises(ValueError):installer.unpack(package['package'],'0'*64,output)
        (self.tools/'deno').write_bytes(b'tampered')
        with self.assertRaises(ValueError):self.package('tampered')

    def test_stale_build_or_dirty_source_cannot_be_labeled_current_release(self):
        (self.publish/'build-identity.json').write_text(json.dumps({'sourceCommit':'0'*40,'version':'0.3.0'}))
        with self.assertRaises(ValueError):self.package()
        (self.repo/'ops/admin_login.py').write_text('dirty fixture')
        with self.assertRaises(subprocess.CalledProcessError):self.package()

    def test_accidental_runtime_credentials_are_rejected_before_publication(self):
        (self.publish/'auth.json').write_text('generated fixture credential')
        with self.assertRaises(ValueError):self.package()
        self.assertFalse((self.root/'output').exists())

    def test_existing_installation_guard_runs_before_package_or_system_changes(self):
        prefix=self.root/'prefix';(prefix/'opt/asmarr').mkdir(parents=True)
        audio=self.root/'audio';audio.mkdir();video=self.root/'video';video.mkdir()
        with patch.object(installer.subprocess,'run',side_effect=AssertionError('must not run host commands')):
            with self.assertRaises(ValueError):installer.install('unused','0'*64,'unused','unused','unused',audio,video,'fixture.invalid',prefix=prefix)


class FreshActivationTests(unittest.TestCase):
    def test_only_held_fresh_instance_can_activate_and_unknown_service_fails_closed(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);state=root/'state';state.mkdir();config=root/'config';config.mkdir()
            for file in ('auth.json','sources.yaml','source-secrets.json','integrations.json','backup-policy.json','backup-recipient.pem','fresh-install.json'):
                (config/file).write_text('{}');(config/file).chmod(0o600)
            audio=root/'audio';audio.mkdir();video=root/'video';video.mkdir()
            with contextlib.closing(sqlite3.connect(state/'asmarr.db')) as db,db:
                db.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE tasks(enabled INTEGER,next_run TEXT);INSERT INTO tasks VALUES(0,NULL);CREATE TABLE creators(monitored INTEGER,monitor_video INTEGER);")
                db.executemany('INSERT INTO settings VALUES(?,?)',[('installation.kind','fresh'),('recovery.hold','true'),('mode','shadow'),('root',str(audio)),('video.root',str(video))])
            stopped=subprocess.CompletedProcess([],0,stdout='LoadState=loaded\nActiveState=inactive\n')
            with patch.object(setup.os,'geteuid',return_value=0),patch.object(setup.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout='')):
                with self.assertRaises(ValueError):setup.activate(state,config,'START_MONITORING')
            with patch.object(setup.os,'geteuid',return_value=0),patch.object(setup.subprocess,'run',return_value=stopped):
                result=setup.activate(state,config,'START_MONITORING')
                self.assertTrue(result['activated'])
                with self.assertRaises(ValueError):setup.activate(state,config,'START_MONITORING')
            with contextlib.closing(sqlite3.connect(state/'asmarr.db')) as db:
                self.assertEqual('production',db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0])
                self.assertEqual(1,db.execute('SELECT enabled FROM tasks').fetchone()[0])


if __name__=='__main__':unittest.main()
