"""Restricted export and verified pull, with fake SSH transport (no network)."""
import contextlib
from datetime import datetime,timezone,timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ('ops/' + name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
puller, exporter = load('backup_pull'), load('backup_export')


class BackupTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / 'offhost'
        self.policy = {'host':'root@fixture.invalid', 'destination':str(self.dest),
                       'identityFile':str(self.root / 'key'), 'knownHostsFile':str(self.root / 'hosts'),
                       'keepOffHost':2}
        for name in ('key','hosts'):
            (self.root / name).touch(mode=0o600)
        self.data = b'encrypted fixture bytes'
        self.names = ['asmarr-full-20261102T010101000000Z-0000000' + str(n) + '.cms' for n in (1,2,3)]
        self.rows = [{'name':n, 'size':len(self.data), 'sha256':hashlib.sha256(self.data).hexdigest()} for n in self.names]

    def transport(self, policy, *command, **kwargs):
        if command == ('list',): return subprocess.CompletedProcess([],0,stdout=json.dumps(self.rows).encode())
        self.assertEqual('get', command[0]); self.assertIn(command[1],self.names)
        kwargs['stdout'].write(self.data)
        return subprocess.CompletedProcess([],0)

    def test_pull_verifies_content_keeps_only_limit_and_repeats_idempotently(self):
        with patch.object(puller,'ssh',side_effect=self.transport):
            result = puller.pull(self.policy)
            self.assertEqual(2,result['copied'])
            self.assertFalse((self.dest/self.names[0]).exists())
            self.assertEqual(self.data,(self.dest/self.names[-1]).read_bytes())
            self.assertEqual(0,puller.pull(self.policy)['copied'])
            self.assertTrue(json.loads((self.dest/'last-pull.json').read_text())['encryptedOnly'])

    def test_failed_remote_list_does_not_prune_old_backups(self):
        self.dest.mkdir(mode=0o700)
        for name in self.names: (self.dest/name).write_bytes(self.data)
        with patch.object(puller,'ssh',side_effect=subprocess.CalledProcessError(1,'ssh')):
            with self.assertRaises(subprocess.CalledProcessError): puller.pull(self.policy)
        self.assertEqual(3,len(list(self.dest.glob('*.cms'))))

    def test_incomplete_transfer_is_not_published_and_old_backups_untouched(self):
        original=self.transport
        def truncated(policy,*command,**kwargs):
            if command[0]=='get':
                kwargs['stdout'].write(b'partial');return subprocess.CompletedProcess([],0)
            return original(policy,*command,**kwargs)
        with patch.object(puller,'ssh',side_effect=truncated):
            with self.assertRaises(ValueError):puller.pull(self.policy)
        self.assertEqual([],list(self.dest.iterdir()))

    def test_bad_remote_filename_and_empty_inventory_fail_closed(self):
        for rows in ([{'name':'../outside','size':1,'sha256':'a'*64}],[]):
            self.rows=rows
            with patch.object(puller,'ssh',side_effect=self.transport):
                with self.assertRaises(ValueError): puller.pull(self.policy)

    def test_existing_mismatched_copy_is_not_overwritten(self):
        self.dest.mkdir(mode=0o700)
        target=self.dest/self.names[1];target.write_bytes(b'other');target.chmod(0o600)
        with patch.object(puller,'ssh',side_effect=self.transport):
            with self.assertRaises(ValueError):puller.pull(self.policy)
        self.assertEqual(b'other',target.read_bytes())

    def test_export_excludes_plaintext_and_rejects_traversal_and_links(self):
        (self.root/'auth.json').write_text('fixture-private')
        (self.root/self.names[0]).write_bytes(self.data)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):exporter.export(['list'],self.root)
        self.assertEqual([self.names[0]],[r['name'] for r in json.loads(output.getvalue())])
        for command in (['get','../auth.json'],['get','auth.json'],['sh','-c','id'],['list',';id']):
            with self.assertRaises(ValueError):exporter.export(command,self.root)
        (self.root/self.names[1]).symlink_to(self.root/'auth.json')
        with self.assertRaises(OSError):exporter.read_file(self.root,self.names[1])

    def test_transport_requires_strict_host_key_checking_and_no_shell(self):
        with patch.object(puller.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run:
            puller.ssh(self.policy,'list',stdout=subprocess.PIPE)
        args,kwargs=run.call_args
        self.assertIn('StrictHostKeyChecking=yes',args[0]);self.assertIn('BatchMode=yes',args[0])
        self.assertNotIn('shell',kwargs)

    def test_fresh_pull_receipt_does_not_claim_an_old_source_backup_is_fresh(self):
        old=(datetime.now(timezone.utc)-timedelta(days=3)).strftime('%Y%m%dT%H%M%S%fZ')
        self.names=['asmarr-full-'+old+'-00000001.cms']
        self.rows=[{'name':self.names[0],'size':len(self.data),'sha256':hashlib.sha256(self.data).hexdigest()}]
        with patch.object(puller,'ssh',side_effect=self.transport):result=puller.pull(self.policy)
        self.assertFalse(result['sourceBackupFresh']);self.assertTrue(result['encryptedOnly'])
        self.assertGreater(result['sourceBackupAgeSeconds'],36*3600)


if __name__=='__main__':unittest.main()
