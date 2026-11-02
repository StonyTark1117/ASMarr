"""Keep private network deployment details out of published operator guides."""
import ast
import ipaddress
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublicDocumentationTests(unittest.TestCase):
    def test_guides_use_examples_not_private_network_addresses(self):
        private_networks = tuple(map(ipaddress.ip_network, (
            '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
        )))
        paths = [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md')),
                 *sorted((ROOT / 'packaging').glob('*.md'))]
        for path in paths:
            contents = path.read_text()
            with self.subTest(path=str(path.relative_to(ROOT))):
                for value in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', contents):
                    address = ipaddress.ip_address(value)
                    self.assertFalse(any(address in net for net in private_networks),
                                     'Use reserved documentation addresses, not private LAN addresses.')
                self.assertIsNone(re.search(r'\b[\w-]+(?:\.[\w-]+)*\.local\b', contents),
                                  'Use a reserved example domain, not a private DNS name.')

    def test_legacy_bootstrap_certificate_is_loopback_only(self):
        tree = ast.parse((ROOT / 'ops/provision.py').read_text())
        sans = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.startswith('subjectAltName=')]
        self.assertEqual(sans, ['subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1'])


if __name__ == '__main__':
    unittest.main()
