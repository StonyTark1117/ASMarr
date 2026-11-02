# Install ASMarr

The first packaged release targets **Debian 12, Linux x64**, with systemd.
It includes a self-contained .NET application and pinned yt-dlp/Deno tools.
Python, ffmpeg/ffprobe, OpenSSL and supporting packages are installed through apt.
Other platforms are not yet packaged/tested.

## Already using ASMarr or the old scraper?

**Do not run the fresh installer.** It refuses existing ASMarr or legacy scraper
state. Existing CT130 uses the [audited upgrade procedure](operations.md#updates).
Migrating an old scraper requires its state-preserving migration/cutover process,
not a blank database. See the [historical audit guide](rollout-audit.md).

## Before a fresh install

Prepare:

1. Empty, dedicated audio and video folders under `/mnt` or `/srv`, on mounted storage.
   They must not overlap. Keep media separate from application/configuration.
   Existing libraries need deliberate import/migration, not a fresh install.
2. A dedicated Plex **Music** library for audio and **Other Videos** for video.
   Ensure Plex can access the same media paths (or the intended matching mounts).
3. A valid server certificate/key for the actual hostname/IP you will use.
   [HTTPS options and private-CA setup](security.md#trusted-https).
4. A backup recipient certificate. Keep its recovery private key on a separate
   trusted computer. [Backup setup](recovery.md#set-up-encrypted-backups).

The installer does not open firewall ports, configure DNS, expose the service to
the Internet, import CA trust on your devices, or recreate Plex libraries.

## Download and verify

Download the Linux x64 tarball and `SHA256SUMS` from the same trusted ASMarr
release. In the download directory:

```sh
sha256sum --check SHA256SUMS
mkdir asmarr-install
tar -xzf asmarr-0.3.0-linux-x64.tar.gz -C asmarr-install
cd asmarr-install
```

Check `release.json` on the release page for the exact source commit/platform.
Checksums detect transfer corruption; obtain the release and checksum through
the trusted repository, not unrelated mirrors. Never use a package containing
someone else's credentials, database, private keys or media.

## Install the empty instance

Ensure Python 3 and OpenSSL are available. Use the tarball's actual SHA-256 from
`SHA256SUMS`. Replace the example paths/host with yours:

```sh
sudo python3 application/ops/install_release.py \
  --package /absolute/downloads/asmarr-0.3.0-linux-x64.tar.gz \
  --sha256 THE_64_CHARACTER_SHA256 \
  --certificate /absolute/private/tls.pem \
  --key /absolute/private/tls.key \
  --ca /absolute/private/ca-cert.pem \
  --host asmarr.example.net \
  --recipient /absolute/private/recovery-cert.pem \
  --audio-root /mnt/media/asmr \
  --video-root /mnt/media/asmr-video \
  --apply
```

Omit `--ca` for a certificate chaining to the host's existing public trust store.
The installer validates the package, hostname/trust/key match and certificate
validity before installing. It creates the `asmarr` account, protected directories,
new configuration and service, and grants that account access to the selected
root folders. It does not recursively change existing media ownership.

The service is **not automatically started**. An interrupted installation can
leave a partial tree; preserve and inspect it rather than repeatedly forcing an
installer over it. Existing targets are never silently overwritten.

## First startup and setup

```sh
sudo systemctl start asmarr
sudo python3 /opt/asmarr/ops/admin_login.py
```

Open `https://YOUR_HOST:8787`, trust the intended certificate authority on your
device if using a private CA, and sign in with the generated login. Change the
password in Settings, then sign in again.

New installations start held: no automatic downloads, history scans, discovery
or legacy migration run while you configure them. Configure/test sources and
Prowlarr/qBittorrent/Plex integrations in Settings. Source credentials stay in
protected `/etc/asmarr/source-secrets.json`; public discovery policies can be
edited in the GUI. Link creator identities and review profiles/monitoring.
Video monitoring authorizes each selected creator's complete available history.

Plex video binding must point to a dedicated Other Videos library. Confirm audio
and video roots, supported source identities, storage access and disk reserve.
Do not enable unknown creators merely to test installation.

When configuration is reviewed, stop the held service and activate this **new**
instance explicitly:

```sh
sudo systemctl stop asmarr
sudo python3 /opt/asmarr/ops/complete_setup.py --confirm START_MONITORING
sudo systemctl enable --now asmarr
```

Activation enables the normal nine scheduled tasks and production processing;
the first cycles run shortly after startup. It cannot activate an existing
migration or a recovered database. A recovered instance uses its separate
[recovery hold procedure](recovery.md#full-application-recovery).

## Verify before relying on it

Confirm a successful normal discovery, a valid import and Plex indexing, queue
progress, playlists if configured, and an encrypted backup. Configure and verify
the [off-host pull](recovery.md#off-host-copy), then perform an isolated restore.
Task enablement alone is not proof a scheduled run succeeded.

Read [operations](operations.md) for daily use, troubleshooting and updates.
