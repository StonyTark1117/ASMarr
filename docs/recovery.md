# Backups and recovery

ASMarr media and ASMarr state need different backups. Recovery bundles protect
the database, credentials, application and service configuration. They **do not
back up audio, video, artwork, download-client data, Plex's database, or Linux**.
Back up those systems separately. A bundle keeps paths and Plex identities; it
does not recreate missing media or roll Plex back in time.

## Set up encrypted backups

1. On a separate trusted computer, create the recovery recipient outside Git:

   ```sh
   python3 ops/backup_recovery.py init-key --directory /absolute/private/key-directory
   ```

2. Keep `recovery-key.pem` private (0600). Save another copy offline, separately
   from the backups. Test it before relying on the backup system. Losing every
   copy of this key makes the encrypted backups unrecoverable.
3. Copy **only** `recovery-cert.pem` to the server as
   `/etc/asmarr/backup-recipient.pem`, owned by `asmarr`, mode 0600. Never copy the
   recovery private key to the application server or source repository.
4. Create `/etc/asmarr/backup-policy.json`, owned by `asmarr`, mode 0600:

   ```json
   {"keepLocal": 14}
   ```

5. In **System → Backups**, choose **Create backup**, then check the command in
   **System → Tasks**. Verify an `asmarr-full-….cms` file appears. Daily scheduled
   backups use the same implementation. A missing recipient fails the command;
   it does not silently write an unencrypted replacement.

The bundles use OpenSSL CMS AES-256-GCM authenticated encryption. See the
[OpenSSL encryption documentation](https://docs.openssl.org/3.0/man3/CMS_EnvelopedData_create/).
The server needs the public recipient certificate, not a decryption password.
Local retention keeps the newest 14 complete encrypted bundles by default;
`keepLocal` accepts 2–365. Incomplete snapshots never displace completed backups.
Existing legacy `.db` backups are retained and are **database-only**.
Full bundles require a hold-aware ASMarr 0.3.0-or-later application; older
binaries cannot safely honor the restore hold and are rejected rather than
silently booted against recovered queued work.

Do not put the recovery key inside the directory being backed up. Protect
recovery bundles even though encrypted, and restore only bundles from a trusted
source. Encryption protects contents; it does not prove who created a bundle.

## Off-host copy

Use a separate always-available host where possible. A second directory inside
the application host is not off-host. A network-storage mount can protect against
loss of the application host but does not independently protect against loss of
the same storage system.

The supplied tools support a pull-only SSH arrangement:

- `ops/backup_export.py` on the source exports only completed encrypted bundles.
- A dedicated SSH key is restricted to that exporter: no shell, forwarding,
  agent forwarding, X11, or terminal. Do not reuse a general administrator key.
- For Proxmox, a root-owned forced command can run the exporter with
  `--container <container-id>`; it accepts only `list` and `get <validated backup name>`.
- `ops/backup_pull.py` runs on the destination. Host keys are pinned; unknown or
  changed host keys fail rather than being automatically accepted.

Create a private `offhost-policy.json` on the destination (0600):

```json
{
  "host": "backup-user@source-host",
  "identityFile": "/absolute/private/ssh-key",
  "knownHostsFile": "/absolute/private/known_hosts",
  "destination": "/absolute/private/asmarr-backups",
  "keepOffHost": 30
}
```

Use the actual SSH account hosting the restricted exporter. The identity and
known-hosts files must be private (0600). Run:

```sh
python3 ops/backup_pull.py --policy /absolute/private/offhost-policy.json
```

Schedule this command using the destination host's timer. An unavailable or
sleeping destination cannot receive backups: monitor its last successful copy.
Every transferred file is checked against the source's size and SHA-256 before
publication. Failed transfers retain older copies and do not run retention.
The destination's `last-pull.json` records the latest verified file and time.
It also records the source backup's actual creation time/age. A new copy receipt
does not make an old backup fresh: source age over 36 hours (or a clock more than
five minutes ahead) returns a nonzero status even when the encrypted copy itself
verified successfully.
Retention is deferred while source freshness/clock checks fail; an invalid
source timestamp is rejected before any existing copy can be pruned.
Default off-host retention is 30 completed bundles (configurable 2–365).

`packaging/asmarr-backup-pull.service` and `.timer` provide an hourly systemd
template for the destination host. They use a dedicated `asmarr-backup` account,
private `/etc/asmarr-backup/offhost-policy.json`, destination
`/var/lib/asmarr-backup`, and root-owned tools in `/usr/local/lib/asmarr`.
Install/review these on the **destination**, not the application host. Configure ownership and
the restricted SSH key before enabling the timer. The SSH forced-command form is
`/usr/bin/python3 /usr/local/lib/asmarr/backup_export.py --container <container-id>` on
Proxmox, with the key's `restrict` option; omit `--container` for a direct source.
Verify the source host-key fingerprint through an existing trusted admin channel
before writing known_hosts. An unverified key scan is not authentication.

## Test a restore without touching production

Use an empty private directory on a trusted recovery computer with OpenSSL and
Python installed:

```sh
python3 ops/backup_recovery.py restore \
  --bundle /absolute/path/asmarr-full-TIMESTAMP-ID.cms \
  --key /absolute/private/recovery-key.pem \
  --output /absolute/private/new-restored-directory
```

The output must not already exist. The tool authenticates/decrypts the bundle,
rejects unsafe archive entries, checks every file hash, and verifies SQLite
integrity. It never replaces a running installation or writes to media roots.

Successful output contains `application/`, `config/`, `state/`, `service/`, the
original manifest, and `recovery-report.json`. The report records the saved
schedule. All tasks and provider work are held; commands that were queued or
running become interrupted rather than being replayed. The recovered database
starts in shadow mode with an additional recovery hold. Historical browser
cookie keys are archived outside active configuration, so old sessions cannot
be resurrected by restoring a backup. The administrator password and API key
remain the ones saved in the bundle.

## Full application recovery

1. **Stop and disable the old instance.** Never run two writers against the same
   libraries or download client.
2. Restore a verified off-host bundle as above. Preserve the recovery report.
3. On the replacement Linux host, install the release's documented OS/Python
   dependencies and pinned yt-dlp/Deno toolchain. The self-contained application
   does not need a separately installed .NET runtime.
4. Mount the existing audio/video/download roots at their original paths. Check
   access for the `asmarr` account. Do not format, rename, rescan with destructive
   tools, or copy missing media from this bundle: it contains no media.
5. Install the recovered `application/`, `config/`, and `state/` as
   `/opt/asmarr`, `/etc/asmarr`, and `/var/lib/asmarr` on the replacement host.
   Do this only into absent/empty destinations; preserve any existing installation
   separately. Own config/state with the new `asmarr` account (directories 0700,
   secret files 0600), and retain executable permissions on the application.
6. Review and install the saved service unit and drop-ins. Check paths, mount
   dependencies, account, certificate and sandbox permissions for the new host.
   Do not automatically enable it yet. Reload systemd, then start it held.
7. Confirm HTTPS and login; verify creators, monitoring states, paths, profiles,
   exclusions, queue history and Plex bindings. Restart held if needed. The
   recovery hold prevents even automatically resumable history scans.
8. Stop the recovered service. After confirming the old instance is stopped,
   restore the saved scheduling/mode with:

   ```sh
   sudo python3 /opt/asmarr/ops/backup_recovery.py release-hold \
     --state /var/lib/asmarr \
     --report /absolute/private/new-restored-directory/recovery-report.json \
     --confirm OLD_INSTANCE_STOPPED
   ```

9. Start and enable only the recovered instance. Review interrupted commands
   individually; never blindly retry a transfer/removal whose outcome is unknown.
   Resumable video history can continue normally after release of the hold.
10. Verify discovery, the queue, Plex indexing/playlists, a new encrypted backup,
    and its off-host copy. Keep the previous instance disabled until this passes.

A backup can contain an old password, expired TLS certificate, or revoked source
credential. Repair those deliberately without printing credentials. Keep the
recovery private key off the replacement server once the drill is complete.

## Upgrade rollback is different

Ordinary upgrade rollback restores the prior application, not an old live
database or media tree. Do not restore an earlier DB over a running instance:
that can replay imports or lose newer monitoring/removal decisions. Use full
recovery only when the current state is unavailable or explicitly rejected.

See [operations](operations.md#updates) for normal upgrades and the
[historical operator audit reference](rollout-audit.md) for strict
release/CI/fixture/Plex preservation gates.
