"""Fetch explicitly pinned public tools; verify hashes before extracting/running."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile


def fetch(url, limit=256*1024**2):
    if not url.startswith('https://'): raise ValueError('HTTPS required')
    with urllib.request.urlopen(url,timeout=120) as response:
        data=response.read(limit+1)
    if len(data)>limit: raise ValueError('Download size limit')
    return data


def prepare(manifest, output):
    output=Path(output)
    if output.exists():raise ValueError('Use a new toolchain output directory')
    output.mkdir(mode=0o700,parents=True)
    for name, entry in manifest.items():
        if name not in ('yt-dlp','deno'):raise ValueError('Unknown tool')
        data=fetch(entry['url'])
        if hashlib.sha256(data).hexdigest()!=entry['sha256']:raise ValueError('Pinned tool checksum mismatch')
        if entry.get('member'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info=archive.getinfo(entry['member'])
                if info.file_size>256*1024**2:raise ValueError('Tool archive size limit')
                data=archive.read(info)
        if hashlib.sha256(data).hexdigest()!=entry['binarySha256']:raise ValueError('Extracted tool checksum mismatch')
        target=output/name;target.write_bytes(data);target.chmod(0o755)
        license_data=fetch(entry['licenseUrl'],2*1024**2)
        (output/(name+'-LICENSE.txt')).write_bytes(license_data)
    return {'tools':list(manifest),'directory':str(output)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=Path(__file__).resolve().parents[1]/'packaging/toolchain.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(json.loads(args.manifest.read_text()),args.output)))
