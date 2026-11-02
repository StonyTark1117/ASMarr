# Bounded live acceptance

This is an operator test plan, not a command to start transfers. Coordinate one
production mutation owner. Existing monitoring and downloads are user intent;
do not stop another operator's repair or disable creators to simplify a test.

## Preconditions and baseline

- Use the installed release's commit/artifact identity and passing CI, not an
  uncommitted local build. Record the current database backup and service state.
- Read current creator names, aliases, source identities, profiles, exclusions,
  task schedules, queue/backfill state, and the separate audio/video Plex bindings.
  Take a fresh baseline after any user-requested enrollment or library correction.
- Preserve the one-video-transfer default, 20 GiB reserve, source eligibility,
  creator exclusions, credentials, and download-client categories. Never print
  secrets or commit private subscription lists, media inventories, or evidence.
- Let active history scans/transfers finish safely. Normal scheduled acquisition
  can continue; do not start a second provider worker against the same state.
- Select one exact recording/provider ID and at most one additional transfer per
  case. Record a size/duration estimate and available capacity before starting.
  Use accessible creator-authorized/public-domain material; public availability
  alone is not evidence of redistribution rights.

## Video cases

| Case | Candidate gate | Required evidence |
| --- | --- | --- |
| Additional YouTube upload | Already-linked monitored creator; public, non-live, age/duration/content eligible; not already imported | Provider ID, available formats and selected format IDs, final ffprobe streams/duration/resolution, hashes, atomic import, correct creator and Plex video section |
| Reddit-hosted upload | An actual discovered `reddit` candidate from a linked creator, with public post and working manifest; outbound YouTube is not a Reddit-hosted test | Post/media IDs, manifest selection, audio availability, deduplicated cross-post ownership, one master transfer, separate media states and scoped Plex refresh |
| Manual Prowlarr video | Reviewed exact creator/title/source match and a lawful, bounded, single playable-video release | Interactive search and explicit grab audit; no automatic video grab; selected file priority, download-client category, successful ffprobe/import, retained source policy |
| Existing audio plus new video | A recording already has a verified audio asset and a verified matching provider video | Identical original audio hash/path before and after; one new video asset, no second audio import |

For missing audio, verify that derivation uses the downloaded master rather than
another source transfer; compare compressed audio packet hashes where applicable.
For genuinely silent video, require video success with audio independently wanted,
not a false two-asset success. Do not manufacture a source match from similar titles.

The first accepted YouTube canary does not establish Reddit delivery, capped
adaptive-stream selection, silent-video handling, or Prowlarr video import in the
real environment. Fixture coverage is necessary but is not live evidence for them.
Validate highest/capped selection against the formats available at test time;
an `Any` profile does not imply a particular resolution on every upload.

## Audio fallback case

1. Select one monitored, non-excluded recording whose audio is missing and whose
   direct-source failure is real and recorded. Confirm there is no active audio
   job or completed file before changing anything.
2. Preserve configured retries/backoff. Observe the normal direct attempts and
   their errors; do not break credentials/network access, falsify attempts, remove
   existing audio, or lower confidence thresholds to force fallback.
3. Inspect ranked results for the exact recording. Automatic acquisition must
   satisfy the existing creator/title/format/confidence rules. An ambiguous or
   empty result is a valid review-needed outcome, not successful fallback delivery.
4. For one eligible result, verify the normal audio-only fallback job, correct
   client category, selected-file priorities, actual completion, stream validation,
   atomic import, metadata, provenance, and the audio Plex binding.
5. Verify the video job/state remains independent. Keep download-client originals
   according to the configured retention policy; remove nothing for cleanup unless
   explicitly authorized.

A torrent added before a download-client configuration repair may need its wanted
file priorities restored or the exact job re-added. That expected recovery step is
not evidence the repair failed. Inspect current priorities and job provenance first,
then let the responsible operator perform any repair; do not interrupt their work.

No suitable live candidate means **not yet exercised**, not a pass. Use isolated
generated fixtures to prove retry/fallback control flow while awaiting a legitimate
candidate. Never resurrect a removed/excluded creator to obtain a convenient test.

## Completion and unattended follow-through

Capture each case separately: requested action, exact IDs, timestamps, selected
formats/file priorities, command states, paths/hashes, media/provenance history,
Plex section and item identity, and remaining limitations. Redact credentials.
Reconcile counts against concurrent legitimate imports rather than assuming a
static library. Distinguish enumeration, wanted work, transferred bytes, imported
files, and Plex-indexed items; a completed scan is not a completed library.

Observe subsequent discovery, queue, Plex and backup cycles without hand-triggering
every step. Verify backup freshness, an independently verified off-host copy and an
isolated restore; a fresh pull receipt for an old bundle is not a fresh backup.
Never boot an unheld restored production database beside the live writer.

The automated suites cover ranking/caps, provider-ID and fingerprint deduplication,
import collisions/interruption, video-only media, derived-audio failure isolation,
space/concurrency guards, direct retries, ambiguous fallback, client outages, and
Plex binding restrictions. Remaining live acceptance must distinguish mocked
provider behavior from real availability, format/codec support, torrent contents,
file-selection recovery, transfer capacity and Plex playback/indexing.

See [operations](operations.md) and [recovery](recovery.md) for deployment and
recovery controls. Runtime acceptance evidence belongs in protected operator
storage, not this repository.
