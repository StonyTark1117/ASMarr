"""Create an isolated browser-test instance; no live connectors or production data."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
root=Path(__file__).resolve().parent
runtime=root/'runtime';runtime.mkdir(exist_ok=True)
state=runtime/'state';config=runtime/'config';state.mkdir(exist_ok=True);config.mkdir(exist_ok=True)
cert=runtime/'cert.pem';key=runtime/'key.pem'
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
env=dict(os.environ,ASMARR_STATE=str(state),ASMARR_CONFIG=str(config),ASPNETCORE_URLS='https://127.0.0.1:8789',ASPNETCORE_Kestrel__Certificates__Default__Path=str(cert),ASPNETCORE_Kestrel__Certificates__Default__KeyPath=str(key))
application=root.parent.parent/'publish'
process=subprocess.Popen([str(application/'ASMarr')],cwd=application,env=env,stdout=subprocess.DEVNULL)
def terminate(*args):process.terminate()
signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
try:
    for _ in range(100):
        if process.poll() is not None:raise RuntimeError('Browser fixture backend exited')
        try:
            if requests.get('https://127.0.0.1:8789/healthz',verify=False,timeout=1).status_code==200:break
        except requests.RequestException:pass
        time.sleep(.2)
    library=runtime/'library';library.mkdir(exist_ok=True)
    with sqlite3.connect(state/'asmarr.db') as db:
        db.execute('UPDATE settings SET value=? WHERE key=?',(str(library),'root'))
        db.execute('UPDATE tasks SET enabled=0')
        db.execute('INSERT OR IGNORE INTO profiles VALUES(1,?,?)',('Fixture profile',json.dumps({'directRetries':3,'allowedFormats':['.m4a']})))
        for id,name in [(1,'Quiet Creator'),(2,'Soft Voice')]:
            db.execute('INSERT OR IGNORE INTO creators(id,name,path) VALUES(?,?,?)',(id,name,str(library/name)))
            db.execute('INSERT OR IGNORE INTO identities(creator_id,kind,handle) VALUES(?,?,?)',(id,'soundgasm','Fixture'+str(id)))
        db.execute("INSERT OR IGNORE INTO assets(key,url,targets,source,creator,title,published,state) VALUES('fixture:bedtime','https://example.invalid/bedtime','[]','soundgasm:Fixture1','Quiet Creator','A quiet bedtime recording',1791400000,'pending')")
        db.execute("INSERT OR IGNORE INTO sources(name,status,last_success,details) VALUES('soundgasm:Fixture1','healthy',1791400000,'{}')")
    (runtime/'ready').touch()
    process.wait()
finally:
    if process.poll() is None:process.terminate();process.wait(timeout=20)
