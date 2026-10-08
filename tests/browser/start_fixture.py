"""Create an isolated browser-test instance; no live connectors or production data."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import tempfile
import requests
import urllib3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
root=Path(__file__).resolve().parent
runtime=root/'runtime';runtime.mkdir(exist_ok=True)
session=Path(tempfile.mkdtemp(prefix='session-',dir=runtime))
state=session/'state';config=session/'config';state.mkdir(mode=0o700);config.mkdir(mode=0o700)
manifest=runtime/'session.json'
with manifest.open('w') as output:
    manifest.chmod(0o600);json.dump({'config':str(config),'state':str(state)},output)
cert=session/'cert.pem';key=session/'key.pem'
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
env=dict(os.environ,ASMARR_STATE=str(state),ASMARR_CONFIG=str(config),ASMARR_PYTHON=sys.executable,ASPNETCORE_URLS='https://127.0.0.1:8789',ASPNETCORE_Kestrel__Certificates__Default__Path=str(cert),ASPNETCORE_Kestrel__Certificates__Default__KeyPath=str(key))
application=root.parent.parent/'publish'
process=subprocess.Popen([str(application/'ASMarr')],cwd=application,env=env,stdout=subprocess.DEVNULL)
plex_server=None
def terminate(*args):process.terminate()
signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
try:
    # First boot generates an intentionally expensive password hash before
    # Kestrel starts. Allow it most of Playwright's 60-second web-server budget
    # so a loaded CI runner cannot be mistaken for a schema/startup failure.
    for _ in range(250):
        if process.poll() is not None:raise RuntimeError('Browser fixture backend exited')
        try:
            healthy=requests.get('https://127.0.0.1:8789/healthz',verify=False,timeout=1).status_code==200
            schema=False
            if (state/'asmarr.db').exists():
                with sqlite3.connect(state/'asmarr.db',timeout=1) as probe:
                    schema=probe.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone() is not None
            if healthy and schema:break
        except (requests.RequestException,sqlite3.Error):pass
        time.sleep(.2)
    else:
        raise RuntimeError('Browser fixture backend schema did not become ready')
    library=session/'library';library.mkdir(exist_ok=True)
    video_library=session/'video-library';video_library.mkdir(exist_ok=True)
    class PlexFixture(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split('?',1)[0] != '/library/sections':
                self.send_response(404);self.end_headers();return
            body=(f'<MediaContainer>'
                  f'<Directory key="2" type="movie" agent="tv.plex.agents.movie" scanner="Plex Movie" title="Movies"><Location path="{session / "movies"}"/></Directory>'
                  f'<Directory key="4" type="artist" agent="tv.plex.agents.none" scanner="Plex Music" title="ASMR Audio"><Location path="{library}"/></Directory>'
                  f'<Directory key="9" type="movie" agent="com.plexapp.agents.none" scanner="Plex Video Files" title="ASMarr Videos"><Location path="{video_library}"/></Directory>'
                  f'<Directory key="12" type="movie" agent="com.plexapp.agents.none" scanner="Plex Video Files" title="ASMarr Videos 2"><Location path="{video_library}"/></Directory>'
                  f'</MediaContainer>').encode()
            self.send_response(200);self.send_header('Content-Type','application/xml')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    plex_server=ThreadingHTTPServer(('127.0.0.1',0),PlexFixture)
    Thread(target=plex_server.serve_forever,daemon=True).start()
    with sqlite3.connect(state/'asmarr.db',timeout=30) as db:
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('UPDATE settings SET value=? WHERE key=?',(str(library),'root'))
        db.execute('UPDATE settings SET value=? WHERE key=?',(str(video_library),'video.root'))
        db.execute('UPDATE tasks SET enabled=0')
        db.execute('INSERT OR IGNORE INTO profiles VALUES(1,?,?)',('Fixture profile',json.dumps({'directRetries':3,'allowedFormats':['.m4a']})))
        for id,name in [(1,'Quiet Creator'),(2,'Soft Voice')]:
            db.execute('INSERT OR IGNORE INTO creators(id,name,path) VALUES(?,?,?)',(id,name,str(library/name)))
            db.execute('INSERT OR IGNORE INTO identities(creator_id,kind,handle) VALUES(?,?,?)',(id,'soundgasm','Fixture'+str(id)))
        db.execute("INSERT OR IGNORE INTO assets(key,url,targets,source,creator,title,published,state) VALUES('fixture:bedtime','https://example.invalid/bedtime',?,'soundgasm:Fixture1','Quiet Creator','A quiet bedtime recording',1791400000,'pending')",
                   (json.dumps([['youtube','https://www.youtube.com/watch?v=abcdefghijk']]),))
        db.execute("INSERT OR IGNORE INTO sources(name,status,last_success,details) VALUES('soundgasm:Fixture1','healthy',1791400000,'{}')")
    (config/'sources.yaml').write_text('{}\n')
    (config/'source-secrets.json').write_text('{}\n')
    (config/'integrations.json').write_text(json.dumps({'plex':{'url':f'http://127.0.0.1:{plex_server.server_port}','section_id':4,'token':'fixture-secret','video':{'section_id':9}}}))
    for path in (config/'sources.yaml',config/'source-secrets.json',config/'integrations.json'):path.chmod(0o600)
    (session/'ready').touch()
    process.wait()
finally:
    if plex_server is not None:
        plex_server.shutdown();plex_server.server_close()
    if process.poll() is None:process.terminate();process.wait(timeout=20)
