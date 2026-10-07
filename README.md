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

The service creates an initial administrator password in
`/etc/asmarr/initial-admin.txt` (mode 0600). Retrieve it locally as root and change
the password through Settings. API authentication uses `X-Api-Key`, with the key
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
