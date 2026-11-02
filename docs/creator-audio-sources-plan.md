# Creator audio source support

This plan covers public creator posts on Hotaudio, Whyp, and Audiochan. A creator identity should be linked explicitly before ASMarr monitors a profile. Acquisition should honor the creator's current visibility and download settings, validate the resulting audio with `ffprobe`, and preserve the source URL and original title.

All three sources now have public creator monitoring and acquisition adapters. Whyp cursor pagination enumerated all 195 tracks on a public test profile. Each provider produced a complete, tagged, `ffprobe`-validated file in a temporary local test. Hotaudio remains disabled by default until the production runtime and service sandbox have been qualified.

## Download paths verified on 2026-11-02

| Source | Discovery | Acquisition | Current confidence |
| --- | --- | --- | --- |
| Whyp | Public profile track cards; link-only tracks need an explicit supplied URL | Use the track page's official **Download** control when the creator enables **Anyone** downloads. It serves an MP3, or a lossless choice on qualifying Pro tracks. | Official documented feature; a public example produced a temporary file URL. Skip alternate extraction. |
| Audiochan | Public creator content listing, paginated, with only published, public, unlocked audio entries | Resolve the public audio page, request a play intent for its audio file, request a temporary stream URL, then download it immediately with the site's normal page origin and referrer. | A complete 15,439,434-byte response validated with `ffprobe` as 466.7 seconds of AAC in an MP4/M4A container. This is an internal playback route and may change. |
| Hotaudio | Public creator profile links and track pages | Run the public player in Chromium, capture the audio fragments it appends to Media Source Extensions, and assemble an M4A. An existing Hotaudio Downloader extension describes the same method. | A public 60.7-second track produced a complete 1,844,418-byte tagged M4A in an unattended local test. Longer tracks and the production service sandbox still need verification. |

## ASMarr integration sequence

1. Verify Whyp and Audiochan creator identities and current download rights at the time of acquisition.
2. Install Chromium and Python Playwright 1.64.1 with the provider runtime. The service must set `ASMARR_PYTHON` to the Python interpreter where Playwright is installed; fresh installs use `/opt/asmarr-runtime/python/bin/python3`. Hotaudio capture plays near the track end and checks the resulting file duration against the player's reported duration before publishing it. Keep partial files out of the library.
3. Test creator matching, visibility changes, expiry handling, and audio validation against maintained fixtures. Keep fixtures free of cookies, signed URLs, and private content. The main application agent owns production rollout.

## References

- [Whyp download feature](https://whyp.it/features/downloads)
- [Whyp privacy settings](https://whyp.it/features/privacy)
- [Hotaudio Downloader method](https://github.com/medy17/hotaudio-downloader)
- [yt-dlp discussion of browser-based Hotaudio extraction](https://github.com/yt-dlp/yt-dlp/issues/17201)
