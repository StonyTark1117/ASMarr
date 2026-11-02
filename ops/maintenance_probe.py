"""Prove an exact candidate leaves queued work inert BEFORE production activation.

Uses generated temporary config/database/media paths, no production credentials,
disabled provider execution, and local HTTPS only. It never opens the live DB.
"""
import argparse
import contextlib
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import urllib.request

from release_integrity import artifact_hash


def snapshot(state):
    with contextlib.closing(sqlite3.connect('file:'+str(state/'asmarr.db')+'?mode=ro',uri=True)) as db:
        return {table:sorted(db.execute('SELECT * FROM '+table).fetchall(),key=repr)
                for table in ('commands','tasks','backfill_jobs','creators','assets','media_assets')}


def probe(application,expected_hash,commit):
    application=Path(application).resolve()
    if not re.fullmatch('[a-f0-9]{64}',expected_hash) or not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('Exact identity required')
    if artifact_hash(application)!=expected_hash:raise ValueError('Candidate artifact hash mismatch')
    identity=json.loads((application/'build-identity.json').read_text())
    if identity.get('sourceCommit')!=commit:raise ValueError('Candidate source identity mismatch')
    started=datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='asmarr-maintenance-probe-') as name:
        root=Path(name);state=root/'state';config=root/'config';audio=root/'audio';video=root/'video'
        for folder in (state,config,audio,video):folder.mkdir(mode=0o700)
        cert,key=config/'tls.pem',config/'tls.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                        '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=127.0.0.1',
                        '-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True,timeout=30)
        key.chmod(0o600)
        account=None
        if os.geteuid()==0:
            try:account=pwd.getpwnam('asmarr')
            except KeyError:account=pwd.getpwnam('nobody')
            for path in [root,*root.rglob('*')]:os.chown(path,account.pw_uid,account.pw_gid)
        def drop_privileges():
            os.umask(0o077)
            if account:
                os.setgroups([]);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url='https://127.0.0.1:'+str(port)
        context=ssl.create_default_context(cafile=str(cert))
        env=dict(os.environ,ASMARR_STATE=str(state),ASMARR_CONFIG=str(config),
                 ASMARR_MAINTENANCE_HOLD='1',ASMARR_PYTHON='/usr/bin/false',
                 ASPNETCORE_URLS=url,ASPNETCORE_ENVIRONMENT='Production',
                 ASPNETCORE_Kestrel__Certificates__Default__Path=str(cert),
                 ASPNETCORE_Kestrel__Certificates__Default__KeyPath=str(key))
        def status(process):
            if process.poll() is not None:raise ValueError('Probe candidate exited')
            auth=json.loads((config/'auth.json').read_text())
            request=urllib.request.Request(url+'/api/v1/system/status',headers={'X-Api-Key':auth['apiKey']})
            with urllib.request.urlopen(request,context=context,timeout=2) as response:
                return json.load(response)
        def start():
            process=subprocess.Popen([str(application/'ASMarr')],cwd=application,env=env,
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                     preexec_fn=drop_privileges,start_new_session=True)
            try:
                for _ in range(200):
                    if process.poll() is not None:raise ValueError('Probe candidate exited')
                    try:
                        value=status(process)
                        if value.get('maintenanceHeld') is not True:raise ValueError('Candidate does not support verified maintenance hold')
                        return process
                    except (OSError,FileNotFoundError,json.JSONDecodeError):time.sleep(.1)
                raise ValueError('Probe candidate did not start')
            except Exception:
                stop(process);raise
        def stop(process):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        # First boot initializes a genuinely empty temporary schema. No source
        # file exists yet, so even older binaries cannot run legacy migration.
        process=start();stop(process)
        with contextlib.closing(sqlite3.connect(state/'asmarr.db')) as db:
            with db:
                db.execute('UPDATE tasks SET enabled=0')
                db.execute("UPDATE tasks SET enabled=1,next_run='2000-01-01T00:00:00Z' WHERE name='health'")
                db.execute("UPDATE settings SET value='production' WHERE key='mode'")
                db.execute("UPDATE settings SET value=? WHERE key='root'",(str(audio),))
                db.execute("UPDATE settings SET value=? WHERE key='video.root'",(str(video),))
                db.execute("INSERT INTO creators(id,name,path,monitor_video) VALUES(1,'Generated hold fixture',?,1)",(str(audio/'fixture'),))
                db.execute("INSERT INTO assets(key,creator,title,targets,state) VALUES('fixture:hold','Generated hold fixture','Generated fixture','[]','pending')")
                db.execute("INSERT INTO commands(id,name,arguments,state,created) VALUES('fixture:queued','health','{}','queued','2000-01-01T00:00:00Z')")
                db.execute("INSERT INTO backfill_jobs(id,creator_id,media_kind,state,started,updated,cursor) VALUES('fixture:running',1,'Video','running','2000-01-01','2000-01-01','{}')")
        for filename in ('sources.yaml','source-secrets.json','integrations.json'):
            path=config/filename;path.write_text('{}');path.chmod(0o600)
            if account:os.chown(path,account.pw_uid,account.pw_gid)
        before=snapshot(state)
        process=start()
        try:
            for _ in range(3):
                time.sleep(2)
                if status(process).get('maintenanceHeld') is not True or snapshot(state)!=before:
                    raise ValueError('Candidate consumed or changed queued work under maintenance hold')
        finally:stop(process)
        if snapshot(state)!=before or artifact_hash(application)!=expected_hash:
            raise ValueError('Candidate state/artifact changed during hold verification')
    return {'passed':True,'sourceCommit':commit,'artifactSha256':expected_hash,
            'startedAt':started,'finishedAt':datetime.now(timezone.utc).isoformat(),
            'providerExecutionDisabled':True,'queuedWorkUnchanged':True,
            'syntheticConfigurationOnly':True,'productionDatabaseOpened':False,
            'probeToolSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application',type=Path,required=True);parser.add_argument('--expected-hash',required=True)
    parser.add_argument('--commit',required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:report=probe(args.application,args.expected_hash,args.commit)
    except (OSError,ValueError,KeyError,sqlite3.Error,subprocess.SubprocessError):
        print('Candidate maintenance hold not proven. Production must not be activated.');return 1
    with args.output.open('x') as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(report,stream,indent=2)
    print(json.dumps({'passed':True,'sourceCommit':args.commit,'artifactSha256':args.expected_hash}))
    return 0


if __name__=='__main__':raise SystemExit(main())
