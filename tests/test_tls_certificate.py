"""Generated private-CA fixtures only; never edit OS/browser trust stores."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('tls_certificate',ROOT/'ops/tls_certificate.py')
tls=importlib.util.module_from_spec(spec);spec.loader.exec_module(tls)


class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        tls.init_ca(cls.root/'ca')
        tls.issue(cls.root/'ca',cls.root/'leaf',['192.0.2.130','asmarr.example.invalid'])

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_issued_leaf_validates_trust_hostname_key_and_validity(self):
        for host in ('192.0.2.130','asmarr.example.invalid'):
            result=tls.validate(self.root/'leaf/tls.pem',self.root/'leaf/tls.key',self.root/'ca/ca-cert.pem',host)
            self.assertTrue(result['verified']);self.assertEqual(64,len(result['fingerprintSha256']))

    def test_wrong_hostname_and_missing_trust_fail(self):
        for ca,host in ((self.root/'ca/ca-cert.pem','192.0.2.131'),(None,'asmarr.example.invalid')):
            with self.assertRaises(subprocess.CalledProcessError):
                tls.validate(self.root/'leaf/tls.pem',self.root/'leaf/tls.key',ca,host)

    def test_injection_and_invalid_hostnames_rejected(self):
        for host in ('asmarr,IP:8.8.8.8','asmarr\nCA:TRUE','../asmarr','-unsafe','a..b'):
            with self.assertRaises(ValueError):tls.subject_alt_names([host])

    def test_private_keys_never_world_readable_and_existing_paths_not_replaced(self):
        self.assertEqual(0o600,(self.root/'ca/ca-key.pem').stat().st_mode&0o777)
        self.assertEqual(0o600,(self.root/'leaf/tls.key').stat().st_mode&0o777)
        with self.assertRaises(ValueError):tls.init_ca(self.root/'ca')
        self.assertFalse((self.root/'leaf/ca-key.pem').exists())


if __name__=='__main__':unittest.main()
