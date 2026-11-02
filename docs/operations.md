# Everyday operation and troubleshooting

## Creators and recordings

Add a creator, select an acquisition profile, and link supported source identities.
Use aliases when source names differ. Audio monitoring and video monitoring are
independent. A video toggle authorizes the creator's full available history;
disabling it cancels unstarted work but retains imported media and history.

The default video profile accepts any resolution and prefers the highest
available quality. Capped profiles reject higher resolutions. Video acquisition
is limited to one transfer by default and pauses below 20 GiB free space.
Sources cannot return deleted/private/unexposed history; unavailable historical
items do not imply a broken history scan.

Watch **Wanted**, **Queue**, **History**, and the creator's backfill progress.
Keep audio/video states separate when diagnosing a recording. A video can finish
without audio, and importing video must not replace an existing audio file.

### Remove creators

Open a creator and choose **Remove creator…**, or select checkboxes on **Creators**
and choose **Remove Creator / Remove Creators (N)** beside monitoring controls.

Removal defaults to **keep files** and **prevent automatic re-addition**. Review
the paths and counts. Keeping files leaves Plex unchanged and retains private
associations for a later explicit re-addition. Reverse an exclusion in
**Settings → Removed creators → Allow re-addition** before adding that creator.

Deleting files requires the exact creator name, or `REMOVE N CREATORS` for a
bulk operation. It deletes only individually tracked safe files inside their
respective media roots, not folders, untracked artwork, shared files or
download-client originals. Deletion is irreversible unless a separate media
backup exists. Plex cleanup targets exact deleted entries, never global trash.

Each creator has a durable removal command and an individual result. Bulk
removal is not one atomic transaction: some can succeed while others fail.
If the browser loses a response, check **System → Tasks** before retrying;
the server may already have accepted it. **Clear selection** clears checkboxes,
not queued jobs. Failed deletions/Plex cleanup remain reported for review.

## Routine checks

- **Dashboard:** source health, queue activity and free space.
- **System → Tasks:** recent discovery, backup, Plex and playlist results;
  commands must finish successfully, not just be queued.
- **System → Backups:** a recent encrypted bundle; separately check the
  destination's `last-pull.json` for off-host success.
- **History:** completed imports and actionable failures.
- **Plex:** new media indexed in the correct dedicated section; existing
  playback/metadata and playlist identities retained.

Daily discovery, disk scans, playlists and backups run internally; queue/health/
Plex reconciliation run more often. Scheduling enabled is not proof a cycle
completed. Verify actual command timestamps/results after installation or updates.

## Common problems

### Cannot sign in

Use HTTPS and the correct administrator credentials. Old sessions are rejected
after a password change/reset or installation of session hardening; sign in again.
After repeated failed attempts wait for the login throttle to clear. If needed,
use the private terminal recovery helper in [security](security.md).

### Certificate warning

Check the URL matches the certificate, its validity period, and client CA trust.
Use [HTTPS setup](security.md#trusted-https); do not treat a warning bypass as a fix.

### Nothing is downloading

Check creator/identity monitoring, profile eligibility, wanted state, retry time,
free space, source health and the most recent queue command. A held recovery or
shadow mode intentionally prevents acquisition. Video also needs a valid,
non-overlapping root and dedicated Plex Other Videos binding. A creator with no
supported video identity will not gain video merely by being audio-monitored.

### A torrent has no selected files or zero bytes

Inspect qBittorrent's global excluded-file rules and this torrent's file
priorities. Shared exclusions can affect ASMarr and Lidarr. Correct the shared
cause without changing the VPN binding or bypassing policy in ASMarr. Previously
created torrents keep their old file selection after a global rule changes.
Resume the intended existing torrent rather than adding duplicate jobs.

### Import succeeded but Plex is late

Check the configured section/root mapping and media-server health. Run scoped
Plex reconciliation/verification and allow delayed indexing to retry. Do not
redownload a valid file just because Plex has not indexed it yet. Never empty
global library trash as an ASMarr repair step.

### Backup failed or off-host copy is old

Check recipient certificate, private policy permissions, space, and the command
result. On the destination check timer status, host-key trust, restricted SSH
access and `last-pull.json`. An offline workstation cannot receive a copy.
Do not delete older backups to conceal a failed copy. [Recovery guide](recovery.md).

### Service will not start

On the server inspect `systemctl status asmarr` and `journalctl -u asmarr`.
Check mounts, config ownership, certificate/key permissions and dependencies.
Detailed provider failures stay in protected runtime diagnostics. Redact tokens,
signed URLs, passwords and source configuration before sharing any output.

## Updates

Use a release matching your platform. Verify its checksum/release identity,
read the release notes and take a recovery backup/off-host copy first. Coordinate
a safe maintenance window: do not kill active transfers, imports, removals or
history scans. Do not use a fresh-install command over an existing installation.

CT130 production updates use `ops/production_upgrade.py` and exact successful CI,
matching installed fixtures, backup and pre/post library/Plex preservation checks.
For existing video state, keep the `--preserve-existing-video` and exact prior
proof requirements. Removal releases also require their installed fixtures.
See [operator audit details](rollout-audit.md#production-upgrades).

After an update verify login/HTTPS, creator and task settings, normal discovery,
backup/off-host copy, queue work, and Plex/playlists. An application rollback must
not blindly overwrite the live database or media with older copies.
