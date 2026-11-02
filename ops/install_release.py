"""Validate a release and install ONLY into an empty Debian 12/Linux x64 instance.

Does not start acquisition, overwrite an existing installation, or trust a CA.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path,PurePosixPath
import platform
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
from release_integrity import artifact_hash


def checksum(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def unpack(package,expected,destination):
    package,destination=Path(package),Path(destination)
    if not re.fullmatch('[a-f0-9]{64}',expected) or checksum(package)!=expected:raise ValueError('Release checksum mismatch')
    with tarfile.open(package,'r:gz') as archive:
        members=[];names=set();total=0
        for member in archive:
            path=PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or '..' in path.parts or str(path)!=member.name
                    or member.name in names or member.size<0 or member.size>1024**3
                    or not(member.name=='package-manifest.json' or member.name.startswith('application/'))):raise ValueError('Unsafe package entry')
            names.add(member.name);total+=member.size;members.append(member)
            if total>4*1024**3 or len(members)>10000:raise ValueError('Package size limit')
        item=next((m for m in members if m.name=='package-manifest.json'),None)
        if item is None or item.size>4*1024**2:raise ValueError('Release manifest missing')
        manifest=json.load(archive.extractfile(item))
        if manifest.get('format')!=1 or manifest.get('platform')!='debian12-linux-x64' or set(manifest.get('files',{}))!=names-{'package-manifest.json'}:raise ValueError('Manifest scope mismatch')
        for member in members:
            target=destination/member.name;target.parent.mkdir(parents=True,exist_ok=True)
            with archive.extractfile(member) as source,target.open('xb') as output:shutil.copyfileobj(source,output)
            entry=manifest['files'].get(member.name)
            if entry:
                if entry.get('mode') not in (0o644,0o755) or entry.get('size')!=member.size or checksum(target)!=entry.get('sha256'):raise ValueError('Package file integrity failure')
                target.chmod(entry['mode'])
    for name in ('ASMarr','providers/bridge.py','ops/admin_login.py','toolchain/yt-dlp','toolchain/deno','packaging/asmarr.service'):
        if not(destination/'application'/name).is_file():raise ValueError('Release is incomplete')
    if artifact_hash(destination/'application')!=manifest.get('artifactSha256'):raise ValueError('Release whole-tree fingerprint mismatch')
    return manifest


def media_root(value):
    path=Path(value)
    if (not path.is_absolute() or str(path)=='/' or not re.fullmatch(r'/[a-zA-Z0-9_./-]+',str(path))
            or '..' in path.parts or path.resolve()!=path or not path.is_dir()
            or any(path==Path(p) or path.is_relative_to(p) for p in ('/etc','/opt','/var/lib/asmarr','/root','/home'))):raise ValueError('Choose an existing dedicated media root under /mnt or /srv')
    return path


def install(package,sha,certificate,key,recipient,audio,video,host,ca=None,prefix=Path('/'),apply=False):
    prefix=Path(prefix)
    audio,video=media_root(audio),media_root(video)
    if audio.is_relative_to(video) or video.is_relative_to(audio):raise ValueError('Media roots overlap')
    if any(audio.iterdir()) or any(video.iterdir()):raise ValueError('Fresh installation needs empty dedicated media roots; existing media requires import/migration')
    app=prefix/'opt/asmarr';runtime=prefix/'opt/asmarr-runtime';config=prefix/'etc/asmarr';state=prefix/'var/lib/asmarr';unit=prefix/'etc/systemd/system/asmarr.service'
    for target in (app,runtime,config,state,unit,prefix/'var/lib/asmr-scraper/state.db'):
        if target.exists() or target.is_symlink():raise ValueError('Existing installation/state detected; use migration or audited upgrade instead')
    if apply and (prefix!=Path('/') or os.geteuid()!=0):raise ValueError('Apply requires root on the actual target; fixture prefixes are validate-only')
    if apply:
        release=Path('/etc/os-release').read_text()
        if platform.machine()!='x86_64' or not re.search(r'^ID=debian$',release,re.M) or not re.search(r'^VERSION_ID="?12"?$',release,re.M):raise ValueError('This package supports Debian12 x64')
    # Verify trust/name/key before any install-directory mutation. Client CA
    # trust remains a separate deliberate action; no system trust is modified.
    if Path(key).stat().st_mode&0o077:raise ValueError('TLS private key must be mode0600')
    subprocess.run(['openssl','x509','-in',str(certificate),'-noout','-checkend','2592000'],check=True,capture_output=True)
    cert_public=subprocess.check_output(['openssl','x509','-in',str(certificate),'-pubkey','-noout'])
    key_public=subprocess.check_output(['openssl','pkey','-in',str(key),'-pubout'])
    if cert_public!=key_public:raise ValueError('Certificate/key mismatch')
    verify=['openssl','verify','-purpose','sslserver']
    if ca:verify+=['-CAfile',str(ca)]
    try:ipaddress.ip_address(host);verify+=['-verify_ip',host]
    except ValueError:
        if not re.fullmatch('[a-zA-Z0-9.-]+',host):raise ValueError('Invalid certificate host')
        verify+=['-verify_hostname',host]
    subprocess.run([*verify,str(certificate)],check=True,capture_output=True)
    subprocess.run(['openssl','x509','-in',str(recipient),'-noout'],check=True,capture_output=True)
    mount=subprocess.check_output(['findmnt','-T',str(audio),'-n','-o','FSTYPE'],text=True).strip()
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+',mount):raise ValueError('Media filesystem must be mounted')
    with tempfile.TemporaryDirectory(prefix='asmarr-install-') as scratch:
        manifest=unpack(package,sha,Path(scratch))
        if not apply:return {'verified':True,'installed':False,'version':manifest['version'],'sourceCommit':manifest['sourceCommit'],'next':'Use --apply as root only on an empty Debian12 host after reviewing the installation guide.'}
        subprocess.run(['apt-get','update'],check=True)
        subprocess.run(['apt-get','install','-y','python3','python3-venv','python3-requests','python3-yaml','python3-mutagen','chromium','ffmpeg','openssl','acl','ca-certificates'],check=True)
        subprocess.run(['python3','-m','venv','--system-site-packages',str(runtime/'python')],check=True)
        subprocess.run([str(runtime/'python/bin/python3'),'-m','pip','install','--require-hashes','--only-binary=:all:',
                        '-r',str(Path(scratch)/'application/packaging/browser-runtime-linux-x64.txt')],check=True)
        try:account=pwd.getpwnam('asmarr')
        except KeyError:
            subprocess.run(['useradd','--system','--home-dir','/var/lib/asmarr','--shell','/usr/sbin/nologin','asmarr'],check=True);account=pwd.getpwnam('asmarr')
        app.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(Path(scratch)/'application',app)
        for folder in (config,state,state/'backups',state/'logs'):
            folder.mkdir(mode=0o700,parents=True,exist_ok=True);os.chown(folder,account.pw_uid,account.pw_gid)
        sources={'youtube_enabled':True,'youtube_channels':[],'soundgasm_creators':[],'subreddits':[],
                 'youtube_binary':'/opt/asmarr/toolchain/yt-dlp','youtube_deno':'/opt/asmarr/toolchain/deno',
                 'required_fstype':mount,'playlists':{'enabled':False}}
        contents={'sources.yaml':json.dumps(sources),'source-secrets.json':'{}','integrations.json':'{}',
                  'backup-policy.json':'{"keepLocal":14}',
                  'fresh-install.json':json.dumps({'kind':'fresh','audioRoot':str(audio),'videoRoot':str(video)})}
        for name,content in contents.items():(config/name).write_text(content)
        for source,name in ((certificate,'tls.pem'),(key,'tls.key'),(recipient,'backup-recipient.pem')):shutil.copyfile(source,config/name)
        if ca:shutil.copyfile(ca,config/'tls-ca.pem')
        for file in config.iterdir():file.chmod(0o600);os.chown(file,account.pw_uid,account.pw_gid)
        # Dedicated roots are not recursively chowned; grant only this account
        # root access/default inheritance, preserving existing ownership/content.
        for folder in (audio,video,Path('/mnt/downloads/asmarr')):
            folder.mkdir(parents=True,exist_ok=True)
            subprocess.run(['setfacl','-m','u:asmarr:rwx,d:u:asmarr:rwx',str(folder)],check=True)
        template=(app/'packaging/asmarr.service').read_text().replace('@AUDIO_ROOT@',str(audio)).replace('@VIDEO_ROOT@',str(video))
        unit.parent.mkdir(parents=True,exist_ok=True);unit.write_text(template);unit.chmod(0o644)
        subprocess.run(['systemctl','daemon-reload'],check=True)
    return {'installed':True,'version':manifest['version'],'sourceCommit':manifest['sourceCommit'],
            'serviceStarted':False,'next':'Start asmarr held, retrieve the generated login, configure integrations/creators, then complete setup. See docs/install.md.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('package','certificate','key','recipient','audio-root','video-root'):parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--sha256',required=True);parser.add_argument('--host',required=True);parser.add_argument('--ca',type=Path);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(install(args.package,args.sha256,args.certificate,args.key,args.recipient,args.audio_root,args.video_root,args.host,args.ca,apply=args.apply),indent=2))
    except (OSError,ValueError,KeyError,subprocess.SubprocessError,tarfile.TarError) as exc:
        print('Installation rejected or incomplete. Existing instances are never overwritten. Check platform, checksum, target paths, certificates, dependencies and permissions.');raise SystemExit(1)
