#!/usr/bin/env python3
"""Bounded, local-only download checks for explicitly selected public audio.

Input: JSON list with id, source, page_url, media_url, and optional expected_bytes,
expected_seconds, expected_md5. URLs may be temporary; reports omit media URLs,
query strings, cookies, and response bodies. This does not import into ASMarr.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def probe(item, output, max_bytes):
    identity = item['id']
    if not re.fullmatch(r'[a-z0-9_-]{1,100}', identity):
        raise ValueError('invalid sample id')
    report = {k: item[k] for k in ('id', 'source', 'title', 'expected_bytes',
                                  'expected_seconds', 'expected_md5') if k in item}
    page = urlsplit(item['page_url'])
    report['page_url'] = page._replace(query='', fragment='').geturl()
    report['started_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    report['validation_warnings'] = []
    path = output / (identity + '.audio')
    partial = output / (identity + '.part')
    session = requests.Session()
    session.headers.update({'User-Agent': 'ASMarr-Public-Audio-Research/1.0',
                            'Accept-Encoding': 'identity'})
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET'], respect_retry_after_header=True)))
    started = time.monotonic()
    try:
        with session.get(item['media_url'], stream=True, timeout=(15, 45)) as response:
            report['http_status'] = response.status_code
            report['redirect_hosts'] = [urlsplit(x.url).hostname for x in response.history]
            report['media_host'] = urlsplit(response.url).hostname
            report['content_type'] = response.headers.get('Content-Type')
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('expected full HTTP 200 response')
            length = response.headers.get('Content-Length')
            length = int(length) if length is not None else None
            report['http_content_length'] = length
            if length is not None and length > max_bytes:
                raise ValueError('file exceeds configured byte limit')
            sha256, md5 = hashlib.sha256(), hashlib.md5()
            count = 0
            with partial.open('wb') as target:
                for chunk in response.iter_content(256 * 1024):
                    count += len(chunk)
                    if count > max_bytes or time.monotonic() - started > 600:
                        raise ValueError('transfer limit exceeded')
                    target.write(chunk)
                    sha256.update(chunk)
                    md5.update(chunk)
            report.update(bytes=count, sha256=sha256.hexdigest(), md5=md5.hexdigest())
            if length is not None and length != count:
                raise ValueError('HTTP length mismatch')
            expected = item.get('expected_bytes')
            report['catalog_size_matches'] = count == expected if expected else None
            if expected and count != expected:
                report['validation_warnings'].append('catalog_size_differs_from_delivered_file')
            if item.get('expected_md5') and item['expected_md5'] != md5.hexdigest():
                raise ValueError('catalog checksum mismatch')
        metadata = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
            'format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels',
            '-of', 'json', str(partial)], capture_output=True, text=True, timeout=30)
        if metadata.returncode:
            raise ValueError('ffprobe failed')
        media = json.loads(metadata.stdout)
        report['media'] = media
        if not any(x.get('codec_type') == 'audio' for x in media.get('streams', [])):
            raise ValueError('no audio stream')
        duration = float(media.get('format', {}).get('duration', 0))
        if duration <= 0:
            raise ValueError('missing positive audio duration')
        expected_duration = item.get('expected_seconds')
        report['duration_delta_seconds'] = round(duration - expected_duration, 3) if expected_duration else None
        if expected_duration and abs(duration - expected_duration) > 2:
            report['validation_warnings'].append('catalog_duration_differs_from_delivered_file')
        decode = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror',
            '-i', str(partial), '-map', '0:a:0', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=300)
        report['full_decode_exit_code'] = decode.returncode
        if decode.returncode:
            raise ValueError('full audio decode failed')
        partial.replace(path)
        report.update(status='downloaded_and_decoded', local_file=str(path.resolve()))
    except Exception as error:
        # Do not leak signed URLs from requests exception messages.
        report.update(status='failed', error_type=type(error).__name__)
        if isinstance(error, ValueError):
            report['error'] = str(error)
    finally:
        partial.unlink(missing_ok=True)
        session.close()
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        (output / (identity + '.json')).write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-mib', type=int, default=150)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    items = json.loads(args.manifest.read_text())
    failures = 0
    for item in items:
        report = probe(item, args.output, args.max_mib * 1024 * 1024)
        print(json.dumps(report), flush=True)
        failures += report['status'] == 'failed'
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
