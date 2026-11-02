# Passwords, sessions and HTTPS

## Administrator access

ASMarr has one local administrator. There is no shared default password.
First startup generates a random password. From a private interactive terminal:

```sh
sudo python3 /opt/asmarr/ops/admin_login.py
```

This reads the protected first-login file; it refuses pipes, redirected output
and non-root callers. Do not record or share the terminal. Change the password
through **Settings → Administrator password** (at least 12 characters). That
removes the initial plaintext file. Normal restarts/upgrades preserve credentials.

For a lost password:

```sh
sudo python3 /opt/asmarr/ops/admin_login.py --reset
```

Confirm `RESET`. A new random password is displayed once. It is not saved in
plaintext. The salted PBKDF2 hash replaces the old one atomically with private
permissions; API keys and unrelated auth fields are preserved.

Changing or resetting the password invalidates existing browser sessions at the
next HTTP request and closes existing live-status connections within about two
seconds. Everyone must sign in again, including the browser changing the password.
Installing session hardening also logs out cookies issued by older releases.
API keys are independent and are **not** rotated by a password change.

The API key is in protected `/etc/asmarr/auth.json`; send it in `X-Api-Key`, never
in a URL. Keep API keys out of browser JavaScript, Git, screenshots, support
reports and shell history. Source/service credentials belong in protected
configuration, not in the repository. The service uses private configuration,
secure/HTTP-only/same-site cookies, mutation-origin checks, and login throttling.

## Trusted HTTPS

A self-signed leaf certificate is encrypted HTTPS but is not automatically
trusted. Clicking through warnings does not establish a trusted deployment.

Choose one approach:

- **Existing domain and trusted issuer:** supply a certificate chain and private
  key for the exact DNS name used in the browser. Keep its renewal mechanism.
- **Private CA for a home LAN:** create a private CA off-server, issue an ASMarr
  server certificate with matching hostnames/IPs, and install only the CA's
  public certificate on each intended client device/browser. This does not become
  publicly trusted automatically.

Publicly trusted certificates cannot be issued for private LAN IP addresses or
`.local` names. Use a domain you control if public trust without client setup is
required. Do not expose ASMarr to the Internet just to get a certificate.

### Private-CA preparation

Run on a trusted administrator computer, outside Git. Replace the reserved
documentation hostname below with the actual hostname used by your clients;
add additional `--host` arguments only for your own required names/IPs:

```sh
python3 ops/tls_certificate.py init-ca --directory /absolute/private/asmarr-ca
python3 ops/tls_certificate.py issue \
  --ca-directory /absolute/private/asmarr-ca \
  --directory /absolute/private/asmarr-leaf \
  --host asmarr.example.com
```

Keep `ca-key.pem` off the application server and back it up privately. Copy only the issued leaf
certificate/key to the server as `/etc/asmarr/tls.pem` and `/etc/asmarr/tls.key`,
owned by `asmarr`; key mode 0600. Coordinate a safe service stop/restart without
interrupting transfers. Preserve the prior certificate/key as a protected rollback.

Before use, validate trust, hostname, key match and at least 30 days of validity:

```sh
python3 ops/tls_certificate.py validate \
  --certificate /absolute/private/asmarr-leaf/tls.pem \
  --key /absolute/private/asmarr-leaf/tls.key \
  --ca /absolute/private/asmarr-ca/ca-cert.pem --host asmarr.example.com
```

Install **`ca-cert.pem`, not either private key**, as a trusted authority on
intended devices. Firefox/Zen may use their own authority store; import it through
the browser's certificate-authority settings if system trust is not used.
Trusting a private CA allows it to issue trusted certificates on that device;
protect the CA key accordingly. Verify the browser opens the normal ASMarr URL
without a certificate warning. Do not use `--insecure` as acceptance evidence.

Private-CA leaf certificates created by the helper last one year. Issue a new
leaf before the final 30 days, validate it, and replace it during a safe restart.
Existing CA trust remains valid; do not distribute the CA key or recreate the CA
for routine renewal. Track expiry in your maintenance reminders/monitoring.
