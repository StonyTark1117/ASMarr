"""Run inside CT130. Tests the live API without printing credentials."""
import json
from pathlib import Path
import time
import urllib3
import requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
base='https://127.0.0.1:8787'
key=json.loads(Path('/etc/asmarr/auth.json').read_text())['apiKey']
s=requests.Session();s.verify=False;s.headers['X-Api-Key']=key
def request(method,path,**kw):
    r=s.request(method,base+'/api/v1/'+path,timeout=45,**kw);r.raise_for_status();return r.json() if r.content else None
def command(name,args=None):return request('POST','commands',json={'name':name,'arguments':args or {}})['id']
def wait(id,deadline=180):
    end=time.monotonic()+deadline
    while time.monotonic()<end:
        r=request('GET','commands/'+id)
        if r['state']=='completed':return json.loads(r['result'])
        if r['state'] in {'failed','interrupted'}:raise RuntimeError(r['error'])
        time.sleep(2)
    return {'status':'running','commandId':id}
if __name__=='__main__':
    import sys
    operation=sys.argv[1] if len(sys.argv)>1 else 'status'
    if operation=='migrate':
        result=wait(command('migration'));print(json.dumps(result));assert result.get('passed')
    elif operation=='audit':
        status=request('GET','system/status')
        print(json.dumps({'status':status,'scan':wait(command('disk-scan')),'plex':wait(command('plex-verify'))}))
    elif operation=='tests':
        results={}
        results['unauthenticatedDenied']=requests.get(base+'/api/v1/creators',verify=False).status_code==401
        initial=Path('/etc/asmarr/initial-admin.txt').read_text().splitlines()
        login=requests.Session();login.verify=False
        r=login.post(base+'/api/v1/auth/login',headers={'X-ASMarr-Request':'1'},json={'username':'admin','password':initial[1].split(': ',1)[1]},timeout=30)
        results['formLogin']=r.status_code==200
        cookie=r.headers.get('Set-Cookie','').lower();results['secureCookie']=all(flag in cookie for flag in ['secure','httponly','samesite=strict'])
        results['csrfDenied']=login.post(base+'/api/v1/commands',json={'name':'health','arguments':{}}).status_code==403
        results['crossOriginDenied']=login.post(base+'/api/v1/commands',headers={'X-ASMarr-Request':'1','Origin':'https://unrelated.invalid'},json={'name':'health','arguments':{}}).status_code==403
        for path in ['creators','identities','recordings','wanted','queue','history',
                     'video/wanted','video/queue','video/history','video/profiles',
                     'connectors','profiles','searches','download-clients','media-servers',
                     'commands','tasks','health','logs','system/status','calendar','settings',
                     'backups','system/updates']:
            request('GET',path);results['api:'+path]=True
        creators=request('GET','creators')
        results['existingCreatorsVideoDisabled']=all(not c.get('monitor_video') for c in creators)
        results['shadowGrabDenied']=s.post(base+'/api/v1/commands',json={'name':'grab','arguments':{}},timeout=30).status_code==409
        results['shadowVideoQueueDenied']=s.post(base+'/api/v1/commands',json={'name':'video-queue','arguments':{}},timeout=30).status_code==409
        print(json.dumps(results));assert all(results.values())
    elif operation=='discovery':print(json.dumps({'commandId':command('discovery')}))
    else:print(json.dumps(request('GET','system/status')))
