# Creator audio source support

This plan covers public creator posts on Hotaudio, Whyp, and Audiochan. A creator identity should be linked explicitly before ASMarr monitors a profile. Acquisition should honor the creator's current visibility and download settings, validate the resulting audio with `ffprobe`, and preserve the source URL and original title.

## Download paths verified on 2026-11-02

| Source | Discovery | Acquisition | Current confidence |
| --- | --- | --- | --- |
| Whyp | Public profile track cards; link-only tracks need an explicit supplied URL | Use the track page's official **Download** control when the creator enables **Anyone** downloads. It serves an MP3, or a lossless choice on qualifying Pro tracks. | Official documented feature; a public example produced a temporary file URL. Skip alternate extraction. |
| Audiochan | Public creator content listing, paginated, with only published, public, unlocked audio entries | Resolve the public audio page, request a play intent for its audio file, request a temporary stream URL, then download it immediately with the site's normal page origin and referrer. | A complete 15,439,434-byte response validated with `ffprobe` as 466.7 seconds of AAC in an MP4/M4A container. This is an internal playback route and may change. |
| Hotaudio | Public creator profile links and track pages | Its player decrypts segments in the browser before appending them to Media Source Extensions. A browser extension can capture those decrypted segments and assemble an M4A; an existing Hotaudio Downloader extension describes this method. | Browser playback works, but an unattended Chromium test received `401 bad signature` from the listen endpoint. Automated capture is not verified and should not be advertised as working yet. |

## ASMarr integration sequence

1. Add `hotaudio`, `whyp`, and `audiochan` as linkable creator identity kinds. Match each discovered post to its linked identity and apply the creator's discovery profile.
2. Add public listing adapters with pagination or a visible backlog boundary. Save stable post IDs, titles, publication dates, and canonical post URLs. Reject private, paywalled, unpublished, or otherwise inaccessible entries.
3. Implement Whyp acquisition only through its enabled official Download route. Mark a track with disabled downloads as unavailable rather than extracting its playback audio.
4. Implement Audiochan acquisition through its public playback flow. Request a fresh stream URL for each attempt, do not persist signed URLs or play intents, identify the output as `.m4a`, and rate limit requests.
5. Prototype Hotaudio capture in a supported browser environment and verify an entire resulting file with `ffprobe`. Until that succeeds, monitor its public catalog but report acquisition as unsupported; never label a partially captured stream complete.
6. Test listing parsing, creator matching, visibility filters, expiry handling, and audio validation. Keep fixtures free of cookies, signed URLs, and private content. The main application agent owns production rollout.

## References

- [Whyp download feature](https://whyp.it/features/downloads)
- [Whyp privacy settings](https://whyp.it/features/privacy)
- [Hotaudio Downloader method](https://github.com/medy17/hotaudio-downloader)
