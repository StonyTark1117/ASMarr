# Creator audio source support

This plan covers public creator posts on Hotaudio, Whyp, and Audiochan. A creator identity must be linked explicitly before ASMarr monitors a profile. Acquisition requires the connector to be enabled and the linked identity to belong to the monitored creator. Reddit cross-post links to these sites are excluded because the post alone does not establish profile ownership. Acquisition should honor the creator's current visibility and download settings, validate the resulting audio with `ffprobe`, and preserve the source URL and original title.

All three sources have public creator monitoring and acquisition adapters. Each produced a complete, tagged, `ffprobe`-validated file in an earlier temporary test. Those tests do not establish that a monitored creator's post currently reaches the production library and Plex. Hotaudio remains disabled by default until the production runtime and service sandbox have been qualified.

## Download paths and current checks

| Source | Discovery | Acquisition | Current confidence |
| --- | --- | --- | --- |
| Whyp | Public profile track cards; link-only tracks need an explicit supplied URL | Use the track page's official **Download** control when the creator enables **Anyone** downloads. It serves an MP3, or a lossless choice on qualifying Pro tracks. | The official path works for tracks with creator-enabled downloads. On 2026-11-11, monitored creator Lurkydip's profile had 670 unique tracks: 8 matched ASMarr's hypnosis profile and 7 enabled downloads, with no overlap. A production check needs a download-enabled, in-profile post or an explicitly chosen one-track exception. |
| Audiochan | Public creator content listing, paginated, with only published, public, unlocked audio entries | Create a public anonymous session, resolve the audio page, request a play intent and temporary stream URL, then download with the same browser-style User-Agent, playback cookie, page origin and referrer. | A complete, tagged 15,455,995-byte M4A validated with `ffprobe` at 466.7 seconds in a 2026-11-02 scratch test. On 2026-11-11, both CT130 and the local host reached public metadata and play intent, then received HTTP 403 verification responses when requesting a stream URL. A normal first-party browser session played the same public post: its `stream-url` request returned HTTP 200 and carried a `cf_clearance` cookie absent from the connector's anonymous session. CT130 Chromium previously showed the site's Turnstile verification dialog without obtaining a token. Current unattended production acquisition is unproven. No evidence establishes that an account is required. |
| Hotaudio | Public creator profile links and track pages; an example prolific profile exposed 126 links in its HTML | Run the public player in Chromium, capture the audio fragments it appends to Media Source Extensions, and assemble an M4A. An existing Hotaudio Downloader extension describes the same method. | A public 60.7-second track produced a complete, tagged M4A on the final adapter. A second 1,519.722-second track produced a 35,286,400-byte M4A whose `ffprobe` duration matched the player within 0.001 second. Both used Chromium's sandbox locally. Chromium launched under equivalent production service restrictions; the complete Playwright capture path on the production host still needs verification. |

## ASMarr integration sequence

1. Verify creator identities and current download rights at the time of acquisition. Keep Whyp posts with disabled downloads out of the acquisition queue. For Audiochan, establish a supported unattended route through the current first-party playback verification before relying on scheduled downloads. Do not transfer a browser clearance cookie into the server or treat a previous scratch success as production proof.
2. Install Chromium and Python Playwright 1.64.1 with the provider runtime. The service must set `ASMARR_PYTHON` to the Python interpreter where Playwright is installed; fresh installs use `/opt/asmarr-runtime/python/bin/python3`. Hotaudio capture plays near the track end and checks the resulting file duration against the player's reported duration before publishing it. Keep partial files out of the library.
3. Test creator matching, visibility changes, expiry handling, and audio validation against maintained fixtures. Keep fixtures free of cookies, signed URLs, and private content. The main application agent owns production rollout.

## References

- [Whyp download feature](https://whyp.it/features/downloads)
- [Whyp privacy settings](https://whyp.it/features/privacy)
- [Hotaudio Downloader method](https://github.com/medy17/hotaudio-downloader)
- [yt-dlp discussion of browser-based Hotaudio extraction](https://github.com/yt-dlp/yt-dlp/issues/17201)
