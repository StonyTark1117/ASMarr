# Public audio source research — 2026-11-11

The strongest additions are **public podcast RSS, Internet Archive, and
creator-enabled free Bandcamp downloads**. Six RSS publishing/delivery routes
worked, plus Internet Archive and Bandcamp. These are three provider families,
not eight unrelated adapters. General ASMR was in scope; hypnosis was not a
qualification requirement.

This is local source research, not a production rollout. No creators were added,
no library or Plex state was changed, and no production-host acquisition was exercised.
Sources already implemented in ASMarr were reviewed for context, not retested.

## What actually passed

Every successful direct transfer reached HTTP 200, delivered exactly its HTTP
Content-Length, contained a positive-duration audio stream in `ffprobe`, and
decoded from beginning to end with `ffmpeg -xerror`. SHA-256 hashes were recorded
locally. Bandcamp additionally passed ZIP CRC validation and full decoding of
every MP3. Internet Archive's file matched its published size and MD5 checksum.

There were **16 decoded audio files**: nine individual downloads and seven tracks
inside one Bandcamp album. Feed counts below describe the feed returned during
testing, not a promise of complete creator history or verified ownership of every
entry. Sizes are decimal MB; durations describe the delivered files.

| Source route | Tested public material | Observed discovery | Verified download |
| --- | --- | --- | --- |
| [Podbean](https://tiraradeguello.podbean.com/) | TirarADeguello, “Episode 320: Be Good to Yourself ASMR” | 320 episodes, all with enclosures | 33.69 MB MP3, 34:26.741; feed size matched and duration differed by less than one second |
| [Self-hosted WordPress / Sleep Whispers](https://sleepwhispers.com/2026/11/08/pure-a221-trivia-time/) | “Trivia Time: 30 More Curious Questions & Answers (A221)” | 300 episodes with enclosures | 25.60 MB MP3, 26:39.785; feed size matched and duration differed by less than one second |
| [Buzzsprout](https://sleeptakeout.buzzsprout.com/) | Sleep Takeout, “Can Self-Compassion Help Insomnia? Calm a Racing Mind Without Forcing Sleep” | 142 episodes with enclosures | 14.52 MB MP3, 20:05.327; feed size matched and duration differed by less than one second |
| [Spotify for Creators / Anchor public RSS](https://podcasts.apple.com/us/podcast/asmr-with-ozley/id1604415381) | Ozley, “ASMR Shhh, It's Okay” and “ASMR Deep Ear Attention” | 177 episodes with enclosures | 25.96 MB / 26:59.983 and 30.43 MB / 31:39.285 MP3s; both passed full decoding, with feed metadata differences noted below |
| [Megaphone public RSS](https://slumberstudios.com/getsleepy/) | Get Sleepy, “A Cozy Autumn Evening with a Family of Beavers” | 844 episodes with enclosures | 117.04 MB MP3, 48:38.008; feed metadata difference noted below |
| [Libsyn](https://sites.libsyn.com/48608/1264-hypnosis-identity-by-design) | Joseph Clough, “#1264 Hypnosis Identity By Design” and “#1 Complete Relaxation Hypnosis” | 1,296 entries, 1,294 with enclosures | 14.12 MB / 29:50.879 and 56.99 MB / 23:32.598 MP3s, after resolving a failed tracking hop; feed metadata differences noted below |
| [Internet Archive](https://archive.org/details/TeaCupSecond10Minutes) | Free To Use Sounds, “Japanese and Chinese Tea Cup ASMR” | Public item metadata exposes original and derivative formats; this item declares CC BY-SA 3.0 | 28.18 MB MP3, exactly 21:00; published MD5 and size both matched |
| [Bandcamp](https://cryptic-ruse.bandcamp.com/album/asmr) | Cryptic Ruse, “ASMR” | Public Free Download page offers format choices | 175.36 MB MP3 V0 ZIP, seven valid tracks, CRC checks passed; this is a music-album transport test, not evidence of a large spoken-ASMR catalog |

### RSS URLs ready for further provider work

```text
https://feed.podbean.com/tiraradeguello/feed.xml
https://sleepwhispers.com/feed/podcast/
https://feeds.buzzsprout.com/1692604.rss
https://anchor.fm/s/751ec3e4/podcast/rss
https://feeds.megaphone.fm/SGP8517078272
https://feeds.libsyn.com/48608/rss
```

All six feeds were read anonymously. The Anchor and Megaphone examples ultimately
use Megaphone delivery infrastructure. This verifies public podcast files, not
Spotify music, private podcast feeds, or subscription-only episodes. Ozley's
public episodes are distinct from the paid ad-free versions advertised by the
publisher.

## Failures that needed further investigation

### Libsyn: a tracker failure was not a media-server failure

The Clough enclosure chain started at `dts.podtrac.com`, then failed with a
connection error at `pscrb.fm`. The public enclosure itself contains the underlying
`traffic.libsyn.com/secure/josephclough/...` media URL. Requesting that HTTPS URL,
with its original query preserved, reached `delivery-edge.libsyn.com` and produced
two complete, decodable audio files. No account, token, or private URL was used.

Do not generically strip redirects from arbitrary URLs. A future adapter should
preserve normal publisher redirects and implement any fallback only for a known,
explicit wrapper format, retaining the original enclosure as provenance.

### Bandcamp: the first download URL was not the final transfer URL

The public free-download page exposed an MP3 V0 link, but requesting it directly
returned HTTP 403 with **Download Expired**. Retrying with a page referrer and a
browser-style User-Agent produced the same explanation. A separate browser visit
to the album also encountered a CAPTCHA, so a plain browser visit did not qualify
the file either.

The successful route followed the site's download-status mechanism:

1. Read `data-tralbum.freeDownloadPage` from the public album page.
2. Read `download_items` / `digital_items` and the desired format URL from the
   download page's `data-blob`.
3. Request the corresponding `/statdownload/` URL and read its `retry_url`.
4. Download that returned URL immediately, then validate the archive and audio.

The upstream [yt-dlp Bandcamp extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bandcamp.py)
implements this same status/refresh step. The refreshed URL returned HTTP 200 and
the full 175,355,440-byte archive. Signed URLs belong in transient state, not saved
creator identities or committed fixtures. This test establishes the free-download
path; it does not establish unattended paid purchases, email-gated downloads, or
that every Bandcamp page can be fetched without a challenge.

### Feed metadata is not always the delivered file's metadata

Podbean, Sleep Whispers, and Buzzsprout matched their published byte counts and
durations closely. The others had measurable discrepancies:

| Sample | Delivered duration minus feed duration |
| --- | ---: |
| Ozley, “Shhh, It's Okay” | +90.983 seconds |
| Ozley, “Deep Ear Attention” | +91.285 seconds |
| Get Sleepy | +79.008 seconds |
| Clough #1264 | −155.121 seconds |
| Clough #1 | +29.598 seconds |

Dynamic advertising or stale feed metadata could explain these differences, but
the cause was not established. The tests prove receipt and successful decoding of
each entire delivered HTTP object. They do not prove byte-for-byte equality with
an ad-free master or explain the shorter Clough #1264 recording. Preserve these
warnings; do not silently turn them into exact-duration passes. Get Sleepy's
enclosure declared a zero length even though its real file was 117 MB.

## Other candidates: accurately bounded conclusions

| Candidate | What was actually checked | Conclusion |
| --- | --- | --- |
| [Audio.com](https://audio.com/listen/audio/electronic-asmr) | Plain HTTP returned a verification challenge. A new isolated Chromium profile initially rendered the creator listing and “Electronic ASMR” with Download enabled. Clicking Download created no browser download; subsequent navigation showed a human-verification gate. The [public API documentation](https://api.audio.com/v1/docs) was reachable and specifies OAuth for its documented search endpoint. | Promising, but no file delivery verified. Neither “unusable” nor “requires an account to listen” was established. |
| [Clyp](https://clyp.it/) | Two different examples supplied by the site's own `/example-url` route returned HTTP 200 metadata with `Status: DownloadDisabled`; the second browser page had no Download control. | Those examples are disabled. This is not a source-wide failure, and an enabled creator track still needs testing. |
| [Vocaroo](https://vocaroo.com/) | An old public tutorial's sample returned media HTTP 404; the normal browser player explicitly reported expired/deleted media. | Invalid historical sample. Other current links remain untested; this result cannot establish service-wide failure. |
| [SoundCloud](https://help.soundcloud.com/hc/en-us/articles/115003448787-Downloading-tracks) | Official documentation says file downloads require sign-in and per-track creator permission. No signed-in download was attempted. | Consider as an account-backed connector; not established as an anonymous file source here. |

An indexed Joey Zeed Bandcamp ASMR page returned 404; other Bandcamp material did
work. Hypnotic World's “free MP3” page explicitly advertises samples rather than a
free full-length catalog. Neither result should be generalized to unrelated pages.

## Integration priorities

1. **Generic public podcast RSS.** One provider covers the six tested routes.
   Bind a feed explicitly to a creator; enumerate enclosures; retain GUID,
   publication date, episode URL and title; follow redirects; stream to a bounded
   temporary file; validate and import atomically. Reject teaser-only episodes
   when full recordings are wanted. Feeds can contain trailers, non-audio items,
   reruns, and incomplete history. A directory match is a lead, not proof of
   creator ownership.
2. **Internet Archive metadata/download API.** Read `/metadata/{identifier}` and
   choose an appropriate audio file from its metadata; use `/download/` for the
   transfer. Compare the published checksum and size when available. Avoid
   importing both original and transcoded versions of the same recording. Confirm
   creator identity and item scope before monitoring a collection or search.
3. **Bandcamp free downloads.** Use the creator's current Free Download route and
   the status/refresh step above. Select individual tracks where available; safely
   validate/extract albums otherwise. The tested music album establishes delivery,
   while relevant ASMR creator coverage still needs evaluation.

Source health should report discovery, resolution, transfer, media validation and
profile eligibility separately. A creator-specific topic rejection must not mark
the provider unavailable. General ASMR must not acquire an extra hypnosis rule.

The code supports explicit unrestricted topics (`requiredTopics: []`) and
`hypno_required: false`, while migrated legacy profiles can retain topic rules.
This research did not inspect or change deployed creator-profile assignments.

## Reproduction and evidence

`ops/probe_public_audio.py` is a local-only probe for explicitly selected public
media. It records HTTP status, redirect hosts, byte counts, hashes, codec/duration,
catalog discrepancies and full-decoding outcome. It does not import into ASMarr.

```sh
python ops/probe_public_audio.py selected-samples.json \
  --output /path/to/private/evidence --max-mib 150
```

The manifest is a JSON array containing `id`, `source`, `title`, `page_url`,
`media_url`, and optional `expected_bytes`, `expected_seconds`, `expected_md5`.
Resolve transient URLs again before retesting. A successful
`downloaded_and_decoded` result can still carry `validation_warnings`; review
them before claiming agreement with source metadata.

For this local run, raw reports, manifests, audio files, and a Bandcamp-specific
reproduction script are in the ignored `sources/public-audio-audit-20261111/`
directory. That directory has restricted access and is excluded from Git. It is
not production acceptance evidence.
