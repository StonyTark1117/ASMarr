"""Private-CA certificate preparation and strict validation; no trust-store edits.

Keep the CA private key off the application server. Trust only the CA certificate
on intended client devices. Existing files are never silently overwritten.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def run(*args):
    return subprocess.run(['openssl', *map(str, args)], check=True,
                          capture_output=True, timeout=120).stdout


def new_directory(path):
    path = Path(path)
    if (not path.is_absolute() or path.exists() or path.is_symlink()
            or any((p / '.git').exists() for p in [path, *path.parents])):
        raise ValueError('Use a new absolute directory outside source checkouts.')
    path.mkdir(mode=0o700)
    return path


def init_ca(directory):
    directory = new_directory(directory)
    key, cert = directory / 'ca-key.pem', directory / 'ca-cert.pem'
    run('req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-keyout', key,
        '-out', cert, '-days', '3650', '-subj', '/CN=ASMarr private TLS CA',
        '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    key.chmod(0o600); cert.chmod(0o644)
    return {'caCertificate': str(cert), 'privateKey': str(key),
            'warning': 'Keep CA key off-server. Installing CA trust allows this CA to issue trusted certificates on that device.'}


def subject_alt_names(hosts):
    values = []
    for host in hosts:
        try: values.append('IP:' + str(ipaddress.ip_address(host)))
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}\Z)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', host):
                raise ValueError('Invalid certificate hostname.')
            if any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-') for part in host.split('.')):
                raise ValueError('Invalid certificate hostname.')
            values.append('DNS:' + host)
    if not values: raise ValueError('Specify at least one hostname or IP.')
    return ','.join(values)


def issue(ca_directory, directory, hosts):
    sans = subject_alt_names(hosts)
    ca_directory = Path(ca_directory)
    ca_key = ca_directory / 'ca-key.pem'
    if ca_key.is_symlink() or ca_key.stat().st_mode & 0o077:
        raise ValueError('CA key must be a private regular file.')
    directory = new_directory(directory)
    key, cert = directory / 'tls.key', directory / 'tls.pem'
    with tempfile.TemporaryDirectory(prefix='.issue-', dir=directory) as scratch:
        csr, extensions = Path(scratch) / 'leaf.csr', Path(scratch) / 'leaf.ext'
        run('req', '-new', '-newkey', 'rsa:3072', '-nodes', '-keyout', key,
            '-out', csr, '-subj', '/CN=ASMarr')
        key.chmod(0o600)
        extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=' + sans + '\n')
        run('x509', '-req', '-in', csr, '-CA', ca_directory / 'ca-cert.pem',
            '-CAkey', ca_key, '-set_serial', '0x' + os.urandom(16).hex(),
            '-days', '365', '-sha256', '-extfile', extensions, '-out', cert)
    cert.chmod(0o644)
    validate(cert, key, ca_directory / 'ca-cert.pem', hosts[0])
    return {'certificate': str(cert), 'privateKey': str(key), 'subjectAltNames': sans,
            'renewBefore': '30 days before expiry; distribute a newly issued leaf, not the CA key'}


def validate(cert, key, ca, host):
    cert, key = Path(cert), Path(key)
    if cert.is_symlink() or key.is_symlink() or not cert.is_file() or not key.is_file() or key.stat().st_mode & 0o077:
        raise ValueError('Certificate/key must be regular files; key must be mode0600.')
    verify = ['verify', '-purpose', 'sslserver']
    if ca: verify += ['-CAfile', str(ca)]
    try:
        ipaddress.ip_address(host); verify += ['-verify_ip', host]
    except ValueError:
        subject_alt_names([host]); verify += ['-verify_hostname', host]
    run(*verify, cert)
    run('x509', '-in', cert, '-noout', '-checkend', '2592000')
    cert_public = run('x509', '-in', cert, '-pubkey', '-noout')
    key_public = run('pkey', '-in', key, '-pubout')
    if cert_public != key_public: raise ValueError('Certificate and key do not match.')
    return {'verified': True, 'host': host, 'fingerprintSha256': hashlib.sha256(run('x509', '-in', cert, '-outform', 'DER')).hexdigest(),
            'deviceTrustRequired': bool(ca)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    init = subs.add_parser('init-ca'); init.add_argument('--directory', required=True, type=Path)
    leaf = subs.add_parser('issue'); leaf.add_argument('--ca-directory', required=True, type=Path)
    leaf.add_argument('--directory', required=True, type=Path); leaf.add_argument('--host', action='append', required=True)
    check = subs.add_parser('validate'); check.add_argument('--certificate', required=True, type=Path)
    check.add_argument('--key', required=True, type=Path); check.add_argument('--ca', type=Path)
    check.add_argument('--host', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init-ca': result = init_ca(args.directory)
        elif args.command == 'issue': result = issue(args.ca_directory, args.directory, args.host)
        else: result = validate(args.certificate, args.key, args.ca, args.host)
        print(json.dumps(result, indent=2)); return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print('TLS operation failed. Check private paths, hostname, issuer trust, expiry, and certificate/key match. No existing certificate was replaced.'); return 1


if __name__ == '__main__': raise SystemExit(main())
