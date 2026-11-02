# Creator audio source support

This plan covers public creator posts on Hotaudio, Whyp, and Audiochan. A creator identity should be linked explicitly before ASMarr monitors a profile. Acquisition should honor the creator's current visibility and download settings, validate the resulting audio with `ffprobe`, and preserve the source URL and original title.

Whyp and Audiochan now have monitoring and direct acquisition adapters in source. Whyp cursor pagination enumerated all 195 tracks on a public test profile, and both providers produced complete, tagged, `ffprobe`-validated files in temporary local tests. Hotaudio has a public profile listing adapter, but its connector starts disabled and unattended acquisition remains unverified.

## Download paths verified on 2026-11-02

| Source | Discovery | Acquisition | Current confidence |
| --- | --- | --- | --- |
| Whyp | Public profile track cards; link-only tracks need an explicit supplied URL | Use the track page's official **Download** control when the creator enables **Anyone** downloads. It serves an MP3, or a lossless choice on qualifying Pro tracks. | Official documented feature; a public example produced a temporary file URL. Skip alternate extraction. |
| Audiochan | Public creator content listing, paginated, with only published, public, unlocked audio entries | Resolve the public audio page, request a play intent for its audio file, request a temporary stream URL, then download it immediately with the site's normal page origin and referrer. | A complete 15,439,434-byte response validated with `ffprobe` as 466.7 seconds of AAC in an MP4/M4A container. This is an internal playback route and may change. |
| Hotaudio | Public creator profile links and track pages | Its player decrypts segments in the browser before appending them to Media Source Extensions. A browser extension can capture those decrypted segments and assemble an M4A; an existing Hotaudio Downloader extension describes this method. | Browser playback works, but an unattended Chromium test received `401 bad signature` from the listen endpoint. Automated capture is not verified and should not be advertised as working yet. |

## ASMarr integration sequence

1. Verify Whyp and Audiochan creator identities and current download rights at the time of acquisition.
2. Prototype Hotaudio capture in a supported browser environment and verify an entire resulting file with `ffprobe`. Until that succeeds, keep its connector disabled by default and report acquisition as unsupported; never label a partially captured stream complete.
3. Test creator matching, visibility changes, expiry handling, and audio validation against maintained fixtures. Keep fixtures free of cookies, signed URLs, and private content. The main application agent owns production rollout.

## References

- [Whyp download feature](https://whyp.it/features/downloads)
- [Whyp privacy settings](https://whyp.it/features/privacy)
- [Hotaudio Downloader method](https://github.com/medy17/hotaudio-downloader)
- [yt-dlp discussion of browser-based Hotaudio extraction](https://github.com/yt-dlp/yt-dlp/issues/17201)
