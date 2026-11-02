# ASMarr

ASMarr is a single-administrator ASMR recording library manager for the existing
CT130 deployment. It uses ASP.NET Core .NET 10, SQLite in WAL mode, SignalR, and a
React/TypeScript frontend served by the backend.

The initial rollout starts in **shadow mode**. The existing scraper remains the
production writer until three daily discovery comparisons and production
acceptance pass. Shadow mode cannot acquire media or change Plex.

## Build

```sh
cd frontend
npm ci
npm run build
cd ..
dotnet publish ASMarr/ASMarr.csproj -c Release -r linux-x64 --self-contained true -o publish
```

Python provider dependencies are `requests`, `PyYAML`, and `mutagen`. Audio
validation uses ffprobe. YouTube uses the existing pinned yt-dlp and Deno binaries.
The production source parsers are preserved in `ASMarr/providers` behind explicit
.NET provider interfaces. Provider IPC carries operations and results; credentials
are read from protected configuration on the server.

## Deployment

- Application: `/opt/asmarr`
- Protected configuration: `/etc/asmarr`
- Database, logs, fixtures, and backups: `/var/lib/asmarr`
- Library: `/mnt/cephfs/media/asmr`
- qBittorrent category: `asmarr`; download path: `/mnt/downloads/asmarr`
- HTTPS: port `8787`

`ops/provision.py` runs inside CT130 as root. It backs up the old application,
configuration, credentials, service definitions, SQLite database, and playlist
mappings before provisioning an isolated `asmarr` account. It copies existing
credentials directly into protected server files without exposing them. Install
the published application and `ops/asmarr.service`, then start the service.

### First login and password recovery

There is no built-in/default password. On a fresh install the service generates
a unique cryptographically random 36-character administrator password, stores
only its salted PBKDF2-SHA256 hash in `auth.json`, and supplies the initial login
in `/etc/asmarr/initial-admin.txt` (mode 0600, inside the protected configuration
directory). After installing and starting the service, complete installation
from a private interactive terminal on the server:

```sh
sudo systemctl enable --now asmarr
sudo python3 ops/admin_login.py
```

Run the helper from your source checkout (or use its absolute path). It displays
the generated username/password only to a terminal, never to a pipe or redirected
log. Do not record that terminal. Connect using HTTPS and change the password
through Settings → Administrator password (at least 12 characters); this removes
the initial plaintext credential file. Restarting or upgrading an existing
installation preserves its login and does not generate or display another one.

If you lose the password, run `sudo python3 ops/admin_login.py --reset` in a
private interactive server terminal and confirm `RESET`. This generates and
displays a new random password once, preserves the API key and other auth fields,
atomically replaces the mode-0600 hash file with the same ownership, and removes
any obsolete initial-login file. It never saves the new password in plaintext,
accepts no password argument, and needs no service restart. It does not revoke
already-issued session cookies. This is an explicit recovery operation, not an
installation/upgrade step.

Never put personal passwords in source, examples, test fixtures, shell arguments,
Git commits, or issue/CI logs. Runtime credential files, password-recovery temp
files, SSH password files, environment files, and rollout backups are ignored by
Git; do not force-add them. Keep server-side configuration/backups protected and
outside source checkouts. API authentication uses `X-Api-Key`, with the key
stored in `/etc/asmarr/auth.json`. Session cookies are secure, HTTP-only and strict
same-site. Cookie-authenticated mutations require `X-ASMarr-Request: 1` and a
matching origin. The deployment certificate is local; trust it in your browser
or supply your own certificate.

REST resources are under `/api/v1`; authenticated OpenAPI is at
`/openapi/v1.json`. Live status is at `/api/v1/events`. Long operations are durable
commands. A renewable SQLite lease serializes provider execution across tasks;
interrupted commands remain visible rather than silently replaying mutations.

Settings → Sources exposes validated discovery filters and polling limits,
read-only discovery cursors, the latest credential-test status, and retry/rate
policy. Public overrides are stored in SQLite; credentials and pinned executable
paths remain in protected server configuration and cannot be set through that
payload. Source quota remaining is explicitly unknown, not assumed unlimited.
Changing discovery filters requires reviewing shadow parity again before cutover.

Creators → Add creator creates a monitored audio creator with an acquisition
profile, aliases, tags, and an optional initial source identity. Further identities
can be linked from its detail page. Registration is transactional and creates no
media directories or video backfills. Identity ownership is source-scoped, so
different Reddit/Soundgasm handles resolve to the same creator and profile without
cross-source handle collisions.

Video monitoring is opt-in per creator. Enabling it starts a resumable scan of
known recordings, linked YouTube channels, configured Reddit feeds, and linked
Reddit accounts' submitted history. YouTube history uses the channel's uploads
playlist rather than just its Videos tab, and is paged without a total item cap;
Reddit follows the continuation tokens exposed by its API. These scans
cover history available from the providers, not deleted posts or history their
APIs no longer expose. Page checkpoints survive transient failures, and disabling
video monitoring stops further discovery without deleting imported media.
Each production discovery cycle also queues a deduplicated full-inventory scan
for opted-in video creators, so future archived streams and other eligible
uploads are found without changing the existing audio polling source.
The existing Disk Scan also reconciles imported video paths: a unique matching
content hash identifies a move or rename within the video root; missing files
remain recorded as missing rather than being silently reacquired. Restored files
return to imported. Each change queues a refresh of only the configured video
Plex section. Scanning never moves or deletes files, and an inaccessible root or
directory fails the scan without marking its contents missing.

Acquisition profiles preserve the migrated legacy text policy unless custom
fields are supplied. Custom fields are `allowedSpeakers` and `allowedAudiences`
(arrays of `F`, `M`, `NB`, `A`, or `ANY`), `requireSpeakerTag`,
`trustedMissingSpeakerTag`, `requiredTopics`, `topicMatch` (`any` or `all`), and
`excludedTerms`. Topics/exclusions are literal case-insensitive words or phrases,
not regexes; an explicit empty topic list removes the topic requirement without
removing speaker or fantasy exclusions. Trusted sources may omit tags but never
override contradictory explicit tags. `minimumDuration` controls audio validation
and YouTube duration eligibility; `backlogLimit` controls initial YouTube
enrollment, not future new uploads. Other sources retain their existing polling
and SFW qualification safeguards.

## Rollout requirements

### Removing creators

Open a creator and choose **Remove creator…**. The reviewed dialog defaults to
**keep files** and **prevent automatic re-addition**. It shows recording/file
counts, deletable size, and every tracked path. Keeping files leaves Plex alone
and retains recording associations privately for reconnection when the same
creator is explicitly re-added. The removed creator's recordings no longer
appear in the active library. Settings → Removed creators → Allow re-addition
reverses a discovery exclusion (including linked handles and aliases).

The Creators screen also exposes **Remove Creator** beside Monitor/Unmonitor
when one checkbox is selected, and **Remove Creators (N)** for multiple
selections. Bulk removal reviews every selected creator and path together,
defaults to keeping files, and requires typing `REMOVE N CREATORS` before
deleting files. Each creator uses the same individually validated, durable
removal operation. Results and failures are shown per creator; already queued
work continues if another request fails. Closing completed results clears only
successful selections, keeping failed/unqueued creators selected for review.
Closing while jobs run clears the queued selections; their results remain in
System → Tasks. **Clear selection** cancels the checkbox selection, not jobs.

**Remove creator and delete files** requires the exact creator name and deletes
only individually tracked files inside their respective audio/video library
roots. No folders, untracked artwork, shared/hardlinked files, or download-client
originals are deleted. Unsafe paths and failed deletions are reported. The
operation is durable, runs under the global provider lease after current work
finishes, cancels creator-scoped queued work, and disables monitoring before
filesystem changes. A changed preview must be reviewed again. An interrupted
removal leaves the creator unmonitored and its command visible for review.

After file deletion, Plex cleanup targets only exact metadata entries whose
entire set of files was deleted by that operation. It never empties library
trash, changes server deletion policy, edits other creators' metadata, or alters
playlists. If Plex refuses cleanup, the result explicitly reports it as pending.
File deletion is irreversible; ordinary protected rollout backups do not copy
the media library.

### Production upgrades

After video enrollment, a follow-up upgrade additionally requires
`--preserve-existing-video --prior-upgrade-proof <exact-current-release-proof>`.
This checks full video rows/candidates/backfills, creator/identity/settings/client
queue state, video content hashes and artwork inventory, and protected config
including authentication. Resumable backfills and active transfer jobs block the
upgrade. Use `--include-removal-tests` for a release containing creator removal;
installed offline fixtures then exercise both removal choices and file/Plex
safety in addition to the audio/video lifecycle. These switches do not enable
creators or approve new acquisitions. Exact successful CI and pre/post upgrade
preservation remain mandatory, with saved task state restored on success.

For the first video release after audio production acceptance,
`ops/production_upgrade.py` provides a separate production upgrade path. It
requires the exact green release CI, a matching immutable fixture bundle, an
accepted unchanged installed artifact, no active commands, and no video opt-ins
or video work. It backs up the application, database, configuration, evidence,
service units, and task schedule; holds scheduling; verifies the installed
replacement with isolated audio/video fixtures and fresh Plex preservation;
then restores the saved schedule. It does not opt in a creator or approve live
video acquisition. The old audio acceptance remains historical evidence for its
original release. On failure it restores the old application without rewriting
media or the database; unexpected concurrent state changes leave scheduling held
for review. Before any video opt-in, a follow-up correction can use
`--prior-upgrade-proof` with the protected report for the currently installed
release. Its installed-runtime and Plex-preservation evidence must both verify;
the historical audio acceptance is not relabeled for a new binary. Do not use
the shadow-only deployment tool on production.

The original implementation plan remains the acceptance scope. In particular:

1. Import all 32 creators, 25 source endpoints, and 696 legacy assets without
   changing paths or media. Preserve aliases, retries, timestamps, metadata and
   playlist identities; verify repeat imports and database integrity.
2. Run three daily shadow cycles, comparing discoveries, eligibility, source
   health, checkpoint calculations and playlist calculations against legacy
   behavior. Record unchanged-media evidence. Source fixtures omit OAuth tokens.
3. Complete recorded-fixture unit/integration and browser tests, including direct
   acquisition, fallback import, outages, interrupted transfers, Plex delays and
   playlist recovery.
4. At cutover, stop and disable the old timer, wait for an active run to finish,
   back up and import its final delta, enable production scheduling and run a
   bounded cycle. Verify one direct acquisition with Plex indexing, one controlled
   Prowlarr/qBittorrent import, all playlists and an idempotent repeat cycle.
5. Keep the old application intact and disabled for one release cycle. Rollback
   stops ASMarr and re-enables the old timer without deleting or rewriting media.

Cutover is not considered complete merely because the application starts or the
migration succeeds. Current rollout evidence lives in the protected SQLite
`migration_audits` and `shadow_cycles` tables and in the System screens.

`ops/validate_library.py` performs a read-only ffprobe audit of every migrated
completed audio file, checking file statistics before and after validation.
`ops/record_audio_acceptance.py --tested-sha <full-sha> --ci-run <run-id>` records
that audit together with fresh path, indexing, and playlist verification after
the specified GitHub run has been independently checked. Its protected
`acceptance/audio-baseline.json` is historical evidence, **not** cutover approval:
subsequent application changes must pass the combined regression suite again.
`ops/cutover.py` defaults to a read-only gate report; `--apply` additionally
requires three qualified daily observations and explicit `pre-cutover.json`
test evidence. It leaves scheduling disabled after the bounded cycle until the
remaining live production acceptance checks pass.
For a source-scoped canary, `--apply --discovery-kind sfw` limits discovery in
that single bounded cycle. This does not change normal scheduled polling or
restrict processing of any already-pending recordings: inspect the final legacy
queue before using it. The deployed application and its qualified shadow evidence
do not need to change for this operator-only option.
Approval also records `testedCommit` and `artifactSha256`, matching the protected
`acceptance/deployed-release.json` manifest (`sourceCommit`, `artifactSha256`).
`ops/release_integrity.py` fingerprints the entire deployed application tree,
including managed DLLs, provider source and compiled UI, not just the .NET
apphost. A changed tree or mismatched tested commit prevents cutover; generated
Python caches are excluded.

`ops/runtime_fixtures.py prepare --repository . --commit <deployed-full-sha>
--output <new-archive>` packages isolated fixtures from immutable Git objects.
After extracting that archive to a protected scratch directory on CT130, run
`ops/runtime_fixtures.py verify --fixtures <scratch-directory> --output
/var/lib/asmarr/acceptance/<new-evidence-file>.json`. It requires matching fixture
and deployed commits, verifies every fixture Git blob and the deployed artifact
tree, blocks network connections, and runs six temporary-database outage/recovery
checks using the installed providers. This is runtime fixture evidence only—not
a substitute for live direct/fallback canaries or a production repeat cycle.

`ops/canary_torznab.py prepare --directory <new-protected-directory>` prepares
one small, pinned CC0 LibriVox audio torrent after verifying the Internet Archive
license, file size and SHA-1. `serve --directory <directory>` exposes its metadata
only on loopback port 18787. An operator may temporarily register that endpoint
as a Generic Torznab indexer for the controlled live fallback test. Its single
public HTTPS web seed works without changing the client's VPN interface. This
tool does not register indexers, alter recordings, submit jobs, or generate
acceptance results: actual Prowlarr search, qBittorrent transfer, verified import
and Plex indexing must still succeed. Remove only the operator-created temporary
indexer and stop the fixture after verification; preserve downloaded media and
the protected evidence.

Before diagnosing a stalled audio torrent, inspect qBittorrent's effective
excluded-file rules and the torrent's individual file priorities. Global audio
exclusions affect every client sharing qBittorrent, including Lidarr. Correct
erroneous shared rules at their source, preserving unrelated exclusions and the
VPN binding; an ASMarr-only bypass does not repair the shared configuration.
Existing torrents retain their prior file selections after the rules change.
Select the intended audio files and resume the existing torrent, preserving its
hash and queue identity. This retained selection is expected, not a failed repair.
`AllFilesSkippedByQBit` records in Cleanuparr indicate the skipped-file outcome;
they do not by themselves establish that Cleanuparr caused the exclusion.
Verify transferred bytes, file integrity, native import and Plex indexing before
claiming recovery. Review any affected Lidarr blocklist entries against recorded
causes separately; changing exclusions does not restore removed downloads.

Final scheduler enablement uses `ops/production_acceptance.py`. Its default is a
read-only audit. Protected `production-acceptance.json` must name the current
`testedCommit` and `artifactSha256`, distinct `directKey`/`fallbackKey`, their
completed queue `directCommandId`/`fallbackCommandId`, fresh `indexCommandId`
(`plex-verify`) and `playlistCommandId` (`playlists-verify`), and protected
`outageFile`/`repeatFile` evidence references. These are real record/command IDs,
not pass/fail declarations. The verifier independently checks imported media,
torrent identity, Plex paths, migration preservation, release binding, and the
disabled legacy timer.

For the repeat evidence, capture protected before/after snapshots with
`--snapshot-output /var/lib/asmarr/acceptance/<new-name>.json`, around completed
`queue`, `plex`, and `playlists` commands while production scheduling remains
held. The repeat report contains `testedCommit`, `artifactSha256`, `startedAt`
and `finishedAt` from those snapshots, their `state` objects as `before`/`after`,
and the three actual command IDs under `commands`. Unknown/time-out commands
remain pending; retain their IDs and do not submit replacement runs. The final
gate requires zero new imports/grabs or playlist changes and snapshot digests
still matching current state. Only `--enable-scheduler`, after every gate passes,
restores the saved task enablement states with future run times. It rechecks after
stopping the worker, records exclusive approval, and restarts the service even
when that recheck fails. Live health must still be verified after restart.

Every qualifying shadow cycle records the deployed source commit and whole-tree
artifact hash. Cutover counts only cycles produced by the current exact release
at or after its protected deployment timestamp; observations from an older
binary can never approve a newer one.

The root-only acceptance manifest remains the cutover authority. Deployment also
publishes `/var/lib/asmarr/deployed-release-runtime.json`, containing only the
commit, artifact hash and deployment timestamp, with the database owner's read
permissions. The service uses that non-secret identity for shadow evidence and
status reporting; deployment rollback restores it atomically with the binary.

Shadow updates use `ops/shadow_deploy.py --payload <published-archive> --commit
<full-source-sha> --ci-run <run-id> --artifact-sha256 <published-tree-sha256>
--ops-commit <full-tooling-sha>`. It verifies the exact successful GitHub run,
rejects unsafe/incomplete archives, requires an idle read-only shadow baseline,
and preserves online database/config/unit backups and the previous application.
Asset rows, audio states, checkpoints, source settings, task settings and media
statistics must match after restart. Creator completed-audio totals are checked
against all saved paths, including legacy `complete` states. Failed payloads are
retained and the previous application is restored without rewriting the database
or media. This updater cannot perform a production cutover.

Cutover captures Plex metadata immediately before production writes and compares
it after the bounded cycle. The protected snapshots check every existing audio
rating key, path, playback/rating field, metadata-lock flag, locked value,
unrelated tag, and playlist identity. Manual playback or metadata edits during
the window are flagged for review rather than silently accepted. Independent
read-only checks are available with `ops/plex_invariants.py capture` and
`ops/plex_invariants.py compare`; evidence files are exclusive-created under the
protected acceptance directory and are never overwritten.

Public packaging, additional media servers/download clients, multi-user accounts,
and unattended application updates are deferred.
