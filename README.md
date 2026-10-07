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
