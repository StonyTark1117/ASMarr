"""Generated credentials only; no personal passwords or live configuration."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('admin_login', ROOT / 'ops/admin_login.py')
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class AdminLoginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name)
        self.password = secrets.token_hex(18).upper()
        salt = secrets.token_bytes(16)
        self.auth = {'username': 'admin', 'salt': base64.b64encode(salt).decode(),
                     'hash': base64.b64encode(hashlib.pbkdf2_hmac('sha256', self.password.encode(), salt, 600000, 32)).decode(),
                     'apiKey': secrets.token_hex(32), 'preserveFutureField': 'unchanged'}
        self.auth_path = self.config / 'auth.json'
        self.auth_path.write_text(json.dumps(self.auth))
        self.auth_path.chmod(0o600)
        self.bootstrap = self.config / 'initial-admin.txt'
        self.bootstrap.write_text('Username: admin\nPassword: ' + self.password + '\n')
        self.bootstrap.chmod(0o600)

    def invoke(self, args=(), tty=True, root=True, confirmation='RESET'):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch.object(admin.sys.stdin, 'isatty', return_value=tty), \
                patch.object(output, 'isatty', return_value=tty), \
                patch.object(admin.os, 'geteuid', return_value=0 if root else 1000), \
                patch('builtins.input', return_value=confirmation):
            result = admin.main(['--config', str(self.config), *args])
        return result, output.getvalue(), error.getvalue()

    def test_initial_login_matches_service_hash_without_mutation(self):
        before = self.auth_path.read_bytes()
        self.assertEqual(('admin', self.password), admin.initial_login(self.config))
        self.assertEqual(before, self.auth_path.read_bytes())

    def test_interactive_installer_supplies_generated_login(self):
        result, output, error = self.invoke()
        self.assertEqual(0, result)
        self.assertIn('Password: ' + self.password, output)
        self.assertEqual('', error)

    def test_redirected_reset_neither_reads_nor_changes_credentials(self):
        before = self.auth_path.read_bytes()
        with patch.object(admin, 'read_protected', side_effect=AssertionError('must not read')):
            result, output, error = self.invoke(['--reset'], tty=False)
        self.assertEqual(1, result)
        self.assertEqual('', output)
        self.assertNotIn(self.password, error)
        self.assertEqual(before, self.auth_path.read_bytes())

    def test_nonroot_cannot_retrieve_credentials(self):
        result, output, error = self.invoke(root=False)
        self.assertEqual(1, result)
        self.assertNotIn(self.password, output + error)

    def test_reset_requires_explicit_confirmation(self):
        before = self.auth_path.read_bytes()
        result, output, error = self.invoke(['--reset'], confirmation='no')
        self.assertEqual(1, result)
        self.assertEqual(before, self.auth_path.read_bytes())
        self.assertNotIn(self.password, output + error)

    def test_random_reset_preserves_key_fields_owner_and_protected_mode(self):
        before = self.auth_path.stat()
        username, password = admin.reset_login(self.config)
        changed = json.loads(self.auth_path.read_text())
        self.assertEqual('admin', username)
        self.assertRegex(password, r'^[0-9A-F]{36}$')
        self.assertNotEqual(self.password, password)
        self.assertNotEqual(self.auth['salt'], changed['salt'])
        self.assertEqual(self.auth['apiKey'], changed['apiKey'])
        self.assertEqual('unchanged', changed['preserveFutureField'])
        expected = base64.b64encode(hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(changed['salt']), 600000, 32)).decode()
        self.assertEqual(expected, changed['hash'])
        self.assertEqual(0o600, stat.S_IMODE(self.auth_path.stat().st_mode))
        self.assertEqual((before.st_uid, before.st_gid), (self.auth_path.stat().st_uid, self.auth_path.stat().st_gid))
        self.assertFalse(self.bootstrap.exists())
        self.assertEqual([self.auth_path], list(self.config.iterdir()))
        self.assertNotIn(password, self.auth_path.read_text())

    def test_recovery_password_is_displayed_only_once_and_not_stored(self):
        result, output, error = self.invoke(['--reset'])
        self.assertEqual(0, result)
        password = next(line.removeprefix('Password: ') for line in output.splitlines() if line.startswith('Password: '))
        self.assertNotIn(password, self.auth_path.read_text() + error)
        self.assertFalse(self.bootstrap.exists())
        result, output, error = self.invoke()
        self.assertEqual(1, result)
        self.assertNotIn(password, output + error)

    def test_missing_or_stale_bootstrap_does_not_expose_old_password(self):
        self.auth['hash'] = base64.b64encode(secrets.token_bytes(32)).decode()
        self.auth_path.write_text(json.dumps(self.auth))
        result, output, error = self.invoke()
        self.assertEqual(1, result)
        self.assertNotIn(self.password, output + error)

    def test_unprotected_files_are_rejected(self):
        self.bootstrap.chmod(0o644)
        with self.assertRaises(ValueError):
            admin.initial_login(self.config)
        self.auth_path.chmod(0o644)
        with self.assertRaises(ValueError):
            admin.reset_login(self.config)

    def test_symlinks_are_rejected(self):
        self.auth_path.rename(self.config / 'target')
        self.auth_path.symlink_to(self.config / 'target')
        with self.assertRaises(OSError):
            admin.reset_login(self.config)

    def test_failed_atomic_replace_keeps_old_login_and_cleans_temporary_file(self):
        before = self.auth_path.read_bytes()
        with patch.object(admin.os, 'replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                admin.reset_login(self.config)
        self.assertEqual(before, self.auth_path.read_bytes())
        self.assertTrue(self.bootstrap.exists())
        self.assertEqual([], list(self.config.glob('.auth-reset-*')))

    def test_password_argument_is_not_supported(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            admin.main(['--password', secrets.token_hex(18)])


class GitCredentialHygieneTests(unittest.TestCase):
    def test_runtime_secrets_and_backups_are_ignored_even_inside_subdirectories(self):
        names = ['auth.json', 'initial-admin.txt', 'integrations.json', 'source-secrets.json',
                 'tls.key', 'sshforcodex', 'sshpass', 'sshpass.txt', '.env', '.env.local',
                 '.auth-reset-random', 'asmarr-rollout-backups/snapshot.json']
        paths = [prefix + name for prefix in ('', 'nested/') for name in names]
        ignored = subprocess.check_output(['git', 'check-ignore', '--no-index', '--stdin'],
                                          input='\n'.join(paths) + '\n', text=True, cwd=ROOT).splitlines()
        self.assertEqual(set(paths), set(ignored))

    def test_no_tracked_runtime_credentials(self):
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        forbidden = {'auth.json', 'initial-admin.txt', 'integrations.json', 'source-secrets.json',
                     'tls.key', 'sshforcodex', 'sshpass', 'sshpass.txt', '.env'}
        self.assertEqual([], [p for p in paths if Path(p).name in forbidden or 'asmarr-rollout-backups' in Path(p).parts])


if __name__ == '__main__':
    unittest.main()
