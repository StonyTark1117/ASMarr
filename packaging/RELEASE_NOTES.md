# ASMarr 0.3.0

Production audio/video monitoring, creator onboarding and single/bulk removal,
with backup/recovery and security hardening.

- Encrypted full application/configuration/database recovery bundles; bounded
  retention and restricted, verified off-host pull tools. Media is not included.
- Validated offline restore, held scheduling, no automatic command replay, and
  explicit recovery release. Recovery key stays off the application server.
- Browser-session revocation after password changes/resets, including live-status
  connections. Existing older cookies require signing in again. API keys persist.
- Trusted HTTPS/private-CA preparation and validation helpers; device trust is
  explicit, not bypassed.
- Self-contained Debian12/Linux x64 package, pinned yt-dlp/Deno with checksums and
  notices, immutable source/build identity, and refusal to overwrite existing
  deployments with the fresh installer.
- Fresh installs start held with a unique generated administrator password and
  explicit setup activation. Existing CT130 uses audited upgrade tooling.
- Short user guides for installation, operation, security, backups and recovery.

The package has no personal passwords, API keys, database, private keys, or media.
Verify SHA256SUMS and the release source identity before use. Set up the backup
recipient, off-host pull, and client HTTPS trust before relying on the service.

Read README.md, then docs/install.md. Never use the fresh installer to replace an
existing ASMarr/scraper instance. Preserve full media backups separately.

This release remains a draft until deployment, restore and live-operation
acceptance are completed and the maintainer chooses publication. A green package
build is not proof that every live provider has been tested.
