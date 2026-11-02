"""Explicit offline activation of a NEW installation, not migration or recovery."""
import argparse
import contextlib
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess


def activate(state,config,confirmation):
    if os.geteuid()!=0 or confirmation!='START_MONITORING':raise ValueError('Root and START_MONITORING confirmation required')
    query=subprocess.run(['systemctl','show','asmarr','--property=LoadState,ActiveState'],check=False,capture_output=True,text=True)
    properties=dict(line.split('=',1) for line in query.stdout.splitlines() if '=' in line)
    if query.returncode!=0 or properties.get('LoadState')!='loaded' or properties.get('ActiveState') not in ('inactive','failed'):raise ValueError('Stop ASMarr and verify its service state first')
    state,config=Path(state),Path(config)
    for path in (state,config):
        if not path.is_absolute() or path.is_symlink():raise ValueError('Explicit real installation directories required')
    for name in ('auth.json','sources.yaml','source-secrets.json','integrations.json','backup-policy.json','backup-recipient.pem','fresh-install.json'):
        path=config/name
        if not path.is_file() or path.is_symlink() or path.stat().st_mode&0o077:raise ValueError('Complete protected configuration required')
    with contextlib.closing(sqlite3.connect('file:'+str(state/'asmarr.db')+'?mode=rw',uri=True)) as db:
        settings=dict(db.execute('SELECT key,value FROM settings'))
        if settings.get('installation.kind')!='fresh' or settings.get('recovery.hold')!='true' or settings.get('recovery.restored')=='true':raise ValueError('Not an initial held fresh installation; use recovery/cutover instead')
        integrations=json.loads((config/'integrations.json').read_text())
        if db.execute('SELECT count(*) FROM creators WHERE monitored=1').fetchone()[0]:
            plex=integrations.get('plex',{})
            if not plex.get('url') or not plex.get('section_id'):raise ValueError('Configure/test dedicated Plex Music integration before activation')
        if db.execute('SELECT count(*) FROM creators WHERE monitor_video=1').fetchone()[0]:
            if not integrations.get('plex',{}).get('video',{}).get('section_id'):raise ValueError('Configure/test dedicated Plex video binding before activation')
        for key in ('root','video.root'):
            if not Path(settings[key]).is_dir():raise ValueError('Media root unavailable')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Database integrity failed')
        with db:
            db.execute("UPDATE settings SET value='production' WHERE key='mode'")
            db.execute("DELETE FROM settings WHERE key='recovery.hold'")
            db.execute('UPDATE tasks SET enabled=1,next_run=?',((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),))
    return {'activated':True,'serviceStarted':False,'next':'Start and enable ASMarr. Verify the first normal discovery, backup, queue and Plex/playlist cycles; set up off-host copying.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,default=Path('/var/lib/asmarr'))
    parser.add_argument('--config',type=Path,default=Path('/etc/asmarr'))
    parser.add_argument('--confirm',required=True)
    args=parser.parse_args()
    try:print(json.dumps(activate(args.state,args.config,args.confirm),indent=2))
    except (ValueError,OSError,KeyError,sqlite3.Error,subprocess.SubprocessError):
        print('Activation rejected. Keep the service stopped; check fresh-install identity, protected settings, mounts and service state.');raise SystemExit(1)
