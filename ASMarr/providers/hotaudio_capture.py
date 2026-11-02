"""Capture the browser's already decoded-to-MSE audio segments as an M4A file."""
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlparse


class CaptureError(ValueError):
    pass


# Hook the SourceBuffer instance created by the site's player. Its prototype
# method is read-only and replacing it before the page loads breaks playback.
CAPTURE_SCRIPT = r'''
window.__asmarrCapture = {chunks: [], bytes: 0, maxBytes: __ASMARR_MAX_BYTES__, overflow: false, audio: null};
const NativeAudio = window.Audio;
window.Audio = function(...args) {
  const element = new NativeAudio(...args);
  window.__asmarrCapture.audio = element;
  return element;
};
window.Audio.prototype = NativeAudio.prototype;
const nativeToString = Function.prototype.toString;
const nativeAdd = MediaSource.prototype.addSourceBuffer;
const captureAdd = function(...args) {
  const buffer = nativeAdd.apply(this, args);
  const nativeAppend = buffer.appendBuffer.bind(buffer);
  Object.defineProperty(buffer, 'appendBuffer', {
    configurable: true,
    value: function(data) {
      if (window.__asmarrCapture.bytes + data.byteLength > window.__asmarrCapture.maxBytes) {
        window.__asmarrCapture.overflow = true;
        throw new RangeError('asmarr_capture_size_limit');
      }
      const chunk = new Uint8Array(data).slice();
      window.__asmarrCapture.chunks.push(chunk);
      window.__asmarrCapture.bytes += chunk.length;
      return nativeAppend(data);
    }
  });
  return buffer;
};
MediaSource.prototype.addSourceBuffer = captureAdd;
Function.prototype.toString = function() {
  if (this === captureAdd) return nativeToString.call(nativeAdd);
  return nativeToString.call(this);
};
'''


def capture(url, destination, *, max_bytes, max_seconds=1200):
    """Return expected seconds after saving an entire public track to destination."""
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'hotaudio.net', 'www.hotaudio.net'} or not re.fullmatch(
            r'/u/[A-Za-z0-9_-]+/[^/]+/?', parsed.path):
        raise CaptureError('invalid_hotaudio_track')
    # The player buffers captured segments in browser memory until publication.
    # Bound memory even if a broader library profile permits larger files.
    max_bytes = min(int(max_bytes), 256 * 1024 * 1024)
    max_seconds = min(int(max_seconds), 1200)
    if max_bytes < 1 or max_seconds < 1:
        raise CaptureError('hotaudio_capture_limit_invalid')
    browser_path = shutil.which('chromium') or shutil.which('chromium-browser')
    if not browser_path:
        raise CaptureError('chromium_not_installed')
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        raise CaptureError('playwright_not_installed') from None

    destination = Path(destination)
    started = time.monotonic()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path, headless=True,
                chromium_sandbox=True,
                args=['--autoplay-policy=no-user-gesture-required',
                      '--disable-blink-features=AutomationControlled'])
            try:
                page = browser.new_page(accept_downloads=True)
                page.add_init_script(CAPTURE_SCRIPT.replace('__ASMARR_MAX_BYTES__', str(max_bytes)))
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if urlparse(page.url).hostname not in {'hotaudio.net', 'www.hotaudio.net'}:
                    raise CaptureError('hotaudio_track_redirected')
                page.locator('#player-playpause').click(timeout=15000)
                for _ in range(30):
                    ready = page.evaluate('''() => {
                      const audio = window.__asmarrCapture.audio;
                      return !!(audio && Number.isFinite(audio.duration) && audio.duration > 0);
                    }''')
                    if ready:
                        break
                    page.wait_for_timeout(1000)
                else:
                    raise CaptureError('hotaudio_player_unavailable')
                expected = float(page.evaluate('window.__asmarrCapture.audio.duration'))
                page.evaluate('''() => {
                  const audio = window.__asmarrCapture.audio;
                  audio.playbackRate = 16;
                  audio.play();
                }''')
                last_position = -1.0
                last_progress = time.monotonic()
                while time.monotonic() - started < max_seconds:
                    page.wait_for_timeout(2000)
                    state = page.evaluate('''() => {
                      const capture = window.__asmarrCapture;
                      return {position: capture.audio.currentTime,
                              ended: capture.audio.ended, bytes: capture.bytes,
                              chunks: capture.chunks.length,
                              overflow: capture.overflow};
                    }''')
                    if state['overflow'] or state['bytes'] > max_bytes:
                        raise CaptureError('hotaudio_capture_size_limit')
                    if state['position'] > last_position + 0.1:
                        last_position, last_progress = state['position'], time.monotonic()
                    if state['chunks'] and (state['ended'] or state['position'] >= expected - 2.5):
                        break
                    if time.monotonic() - last_progress > 30:
                        raise CaptureError('hotaudio_playback_stalled')
                else:
                    raise CaptureError('hotaudio_capture_time_limit')
                with page.expect_download(timeout=30000) as download:
                    page.evaluate('''() => {
                      const a = document.createElement('a');
                      a.href = URL.createObjectURL(new Blob(window.__asmarrCapture.chunks,
                                                           {type: 'audio/mp4'}));
                      a.download = 'asmarr-capture.m4a';
                      a.click();
                    }''')
                download.value.save_as(destination)
                if destination.stat().st_size <= 0 or destination.stat().st_size > max_bytes:
                    raise CaptureError('hotaudio_capture_size_limit')
                return expected
            finally:
                browser.close()
    except CaptureError:
        raise
    except (PlaywrightError, OSError, ValueError) as error:
        raise CaptureError('hotaudio_capture_failed:' + type(error).__name__) from None
