"""Run as root inside CT130 after publishing /opt/asmarr. Prints no secrets."""
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET

stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
backup=Path('/root/asmarr-rollout-backups')/stamp
backup.mkdir(parents=True,mode=0o700)
for source in ['/opt/asmr-scraper','/etc/asmr-scraper','/etc/systemd/system/asmr-scraper.service','/etc/systemd/system/asmr-scraper.timer','/etc/systemd/system/asmr-scraper-failure.service']:
    p=Path(source)
    dest=backup/p.relative_to('/')
    dest.parent.mkdir(parents=True,exist_ok=True)
    if p.is_dir(): shutil.copytree(p,dest,ignore=shutil.ignore_patterns('__pycache__'))
    elif p.exists(): shutil.copy2(p,dest)
with sqlite3.connect('file:/var/lib/asmr-scraper/state.db?mode=ro',uri=True) as src:
    with sqlite3.connect(backup/'state.db') as dst:src.backup(dst)
    mappings=dict(src.execute("SELECT key,value FROM meta WHERE key='managed_playlists' OR key LIKE 'playlist_mood_backup:%'"))
    (backup/'plex-playlist-mappings.json').write_text(json.dumps(mappings))
for p in backup.rglob('*'):
    if p.is_file():p.chmod(0o600)
try: account=pwd.getpwnam('asmarr')
except KeyError:
    subprocess.run(['useradd','--system','--home-dir','/var/lib/asmarr','--shell','/usr/sbin/nologin','asmarr'],check=True)
    account=pwd.getpwnam('asmarr')
for folder in ['/etc/asmarr','/var/lib/asmarr','/var/lib/asmarr/backups','/var/lib/asmarr/logs','/var/lib/asmarr/fixtures','/var/lib/asmarr/keys']:
    p=Path(folder);p.mkdir(parents=True,exist_ok=True);p.chmod(0o700);os.chown(p,account.pw_uid,account.pw_gid)
for source,dest in [('/etc/asmr-scraper/config.yaml','/etc/asmarr/sources.yaml'),('/etc/asmr-scraper/config.yaml','/etc/asmarr/legacy-sources.yaml'),('/etc/asmr-scraper/secrets.json','/etc/asmarr/source-secrets.json')]:
    p=Path(dest)
    if not p.exists():shutil.copy2(source,p)
    p.chmod(0o600);os.chown(p,account.pw_uid,account.pw_gid)
config=Path('/etc/asmarr/integrations.json')
if not config.exists():
    prowlarr=ET.parse('/var/lib/prowlarr/config.xml').getroot()
    qbit={'url':'','retention':'keep'}
    with sqlite3.connect('file:/var/lib/radarr/radarr.db?mode=ro',uri=True) as arr:
        row=arr.execute("SELECT Settings FROM DownloadClients WHERE Implementation='QBittorrent' AND Enable=1 LIMIT 1").fetchone()
        if row:
            options=json.loads(row[0]);qbit.update(url=('https://' if options.get('useSsl') else 'http://')+options['host']+':'+str(options['port'])+options.get('urlBase',''),username=options.get('username',''),password=options.get('password',''))
    config.write_text(json.dumps({'prowlarr':{'url':'http://127.0.0.1:'+prowlarr.findtext('Port','9696'),'apiKey':prowlarr.findtext('ApiKey'),'confidenceThreshold':.92},'qbittorrent':qbit,'notifications':{}}))
config.chmod(0o600);os.chown(config,account.pw_uid,account.pw_gid)
cert=Path('/etc/asmarr/tls.pem');key=Path('/etc/asmarr/tls.key')
if not cert.exists():
    subprocess.run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-keyout',str(key),'-out',str(cert),'-days','825','-subj','/CN=arr-stack','-addext','subjectAltName=DNS:arr-stack,DNS:asmarr,IP:192.168.1.5,IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for p in [cert,key]:p.chmod(0o600);os.chown(p,account.pw_uid,account.pw_gid)
# Shadow mode has read access to the library and legacy state only. The cutover
# procedure grants library writes after acceptance. Directory traversal permits
# reading source backup state through the dedicated account.
subprocess.run(['setfacl','-m','u:asmarr:rx','/var/lib/asmr-scraper'],check=True)
subprocess.run(['setfacl','-m','u:asmarr:r','/var/lib/asmr-scraper/state.db'],check=True)
Path('/mnt/downloads/asmarr').mkdir(parents=True,exist_ok=True)
video_root=Path('/mnt/cephfs/media/asmr-video')
video_root.mkdir(parents=True,exist_ok=True)
subprocess.run(['setfacl','-m','u:asmarr:rx',str(video_root)],check=True)
print(json.dumps({'backup':str(backup),'account':'asmarr','mode':'shadow','configuration':'/etc/asmarr','secretsCopied':True}))
