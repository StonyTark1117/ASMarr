"""Read-only ffprobe validation of every migrated completed audio record."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import time

state=Path('/var/lib/asmarr')
with sqlite3.connect('file:'+str(state/'asmarr.db')+'?mode=ro',uri=True) as db:
    db.row_factory=sqlite3.Row
    rows=[dict(r) for r in db.execute("SELECT key,saved_path FROM assets WHERE state='complete'")]
    root=Path(db.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]).resolve()

def validate(row):
    path=Path(row['saved_path'])
    if not path.resolve().is_relative_to(root):return {'key':row['key'],'status':'outside_library'}
    try:
        before=path.stat()
        process=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,codec_name','-of','json',str(path)],capture_output=True,text=True,timeout=30)
        if process.returncode:return {'key':row['key'],'status':'ffprobe_failed'}
        result=json.loads(process.stdout)
        if not any(s.get('codec_type')=='audio' for s in result.get('streams',[])):return {'key':row['key'],'status':'no_audio'}
        if any(s.get('codec_type')=='video' for s in result.get('streams',[])):return {'key':row['key'],'status':'contains_video'}
        duration=float(result.get('format',{}).get('duration',0))
        if duration<=0:return {'key':row['key'],'status':'invalid_duration'}
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return {'key':row['key'],'status':'changed_during_validation'}
        return {'key':row['key'],'status':'ok','path':str(path),'duration':duration,'size':after.st_size,'mtime_ns':after.st_mtime_ns,'codecs':[s.get('codec_name') for s in result.get('streams',[]) if s.get('codec_type')=='audio']}
    except (OSError,ValueError,subprocess.TimeoutExpired) as e:return {'key':row['key'],'status':type(e).__name__}

started=time.monotonic()
with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(validate,rows))
report={'checkedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'total':len(rows),'valid':sum(r['status']=='ok' for r in results),'failures':[r for r in results if r['status']!='ok'],'elapsedSeconds':round(time.monotonic()-started,2),'records':results}
directory=state/'acceptance';directory.mkdir(mode=0o700,exist_ok=True)
path=directory/'migrated-audio-validation.json';path.write_text(json.dumps(report));path.chmod(0o600)
print(json.dumps({k:v for k,v in report.items() if k!='records'}))
raise SystemExit(0 if not report['failures'] else 1)
