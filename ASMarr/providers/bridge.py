"""ASMarr provider host. JSON IPC only; credentials never travel in IPC or logs.

The mature source parsers are preserved as vendored provider modules while the
ASP.NET host owns authentication, API, scheduling, locking and durable commands.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET

import requests
import yaml
import scraper as core
import plex_playlists
import video_media
import acquisition_profiles
from categories import CATEGORIES

STATE = Path(os.environ.get('ASMARR_STATE', '/var/lib/asmarr'))
CONFIG = Path(os.environ.get('ASMARR_CONFIG', '/etc/asmarr'))
DB = STATE / 'asmarr.db'
ALLOWED = {'.m4a', '.mp3', '.aac', '.opus', '.flac', '.wav'}


def connect(path=DB):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA foreign_keys=ON')
    return db


def setting(db, key, default=''):
    r = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return r[0] if r else default


def has_table(db,name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def configuration(db):
    cfg = yaml.safe_load((CONFIG / 'sources.yaml').read_text())
    cfg.update(state_db=str(DB), output_root=setting(db, 'root', '/mnt/cephfs/media/asmr'),
               secrets_file=str(CONFIG / 'source-secrets.json'), lock_file=str(STATE / 'provider.lock'))
    secrets = json.loads((CONFIG / 'source-secrets.json').read_text())
    plex = integrations().get('plex', {})
    if plex:
        cfg['plex'] = dict(cfg.get('plex', {}), **{k:v for k,v in plex.items() if k != 'token'})
        if plex.get('token'): secrets['plex_token'] = plex['token']
    cfg.update(video_root=setting(db, 'video.root', '/mnt/cephfs/media/asmr-video'),
               video_free_space_gib=float(setting(db, 'video.free_space_gib', '20')),
               video_concurrency=int(setting(db, 'video.concurrency', '1')))
    return cfg, secrets


def production(db):
    if setting(db, 'mode', 'shadow') != 'production':
        raise ValueError('operation_requires_production_cutover')


def event(db, name, key, details):
    db.execute('INSERT INTO history(at,event,recording_key,details) VALUES(?,?,?,?)',
               (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), name, key, json.dumps(details)))
    db.commit()


def legacy_snapshot(destination):
    with contextlib.closing(sqlite3.connect('file:/var/lib/asmr-scraper/state.db?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def media_manifest(db):
    result, missing, outside = {}, [], []
    root = Path(setting(db, 'root', '/mnt/cephfs/media/asmr')).resolve()
    for row in db.execute("SELECT key,saved_path FROM assets WHERE state='complete'"):
        p = Path(row['saved_path'] or '')
        if not p.resolve().is_relative_to(root):
            outside.append(row['key'])
        elif not p.is_file() or not p.stat().st_size:
            missing.append(row['key'])
        else:
            s = p.stat()
            result[row['key']] = {'path': str(p), 'size': s.st_size, 'mtime_ns': s.st_mtime_ns}
    return {'files': result, 'missing': missing, 'outside': outside}


def production_fingerprint():
    with contextlib.closing(sqlite3.connect('file:/var/lib/asmr-scraper/state.db?mode=ro',uri=True)) as legacy:
        h=hashlib.sha256()
        for table in ['assets','downloads','aliases','meta']:
            h.update(table.encode())
            for row in legacy.execute('SELECT * FROM '+table+' ORDER BY 1'):h.update(json.dumps(row,ensure_ascii=False).encode())
        return h.hexdigest()


def migrate(db, cfg):
    if setting(db,'mode','shadow')!='shadow':raise ValueError('migration_requires_shadow_mode')
    backup = STATE / 'backups' / ('legacy-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '.db')
    backup.parent.mkdir(parents=True, exist_ok=True)
    legacy_snapshot(backup)
    db.execute('ATTACH DATABASE ? AS legacy', (str(backup),))
    before = media_manifest(db)
    with db:
        # Explicit columns keep migrations stable and preserve every legacy field.
        for table in ['downloads', 'assets', 'aliases', 'meta', 'sources', 'runs']:
            columns = [r[1] for r in db.execute('PRAGMA legacy.table_info(' + table + ')')]
            names = ','.join(columns)
            db.execute('INSERT OR REPLACE INTO ' + table + '(' + names + ') SELECT ' + names + ' FROM legacy.' + table)
        if db.execute('SELECT count(*) FROM profiles').fetchone()[0] == 0:
            shared = {k: cfg.get(k) for k in ['hypno_required', 'hypno_keywords', 'fantasy_blocklist']}
            shared.update(minimumDuration=0, allowedFormats=sorted(ALLOWED), sourcePriorities=['soundgasm','youtube','reddit'], backlogLimit=3, directRetries=3, fallback=True, confidenceThreshold=.92)
            db.execute('INSERT INTO profiles VALUES(1,?,?)', ('Legacy hypnosis', json.dumps(shared)))
            db.execute('INSERT INTO profiles VALUES(2,?,?)', ('Approved YouTube', json.dumps(dict(shared, minimumDuration=180, trusted=True))))
            db.execute('INSERT INTO profiles VALUES(3,?,?)', ('SFW discovery', json.dumps(dict(shared, hypno_required=False, requireSfw=True))))
        # The migration inventory is the 32 distinct recorded creators. Configured
        # aliases and identities are attached without silently inventing creators.
        for row in db.execute('SELECT DISTINCT creator FROM legacy.assets').fetchall():
            name = row[0]
            aliases = [k for k,v in cfg.get('creator_aliases', {}).items() if v == name]
            paths = [r[0] for r in db.execute('SELECT saved_path FROM legacy.assets WHERE creator=? AND saved_path IS NOT NULL', (name,))]
            path = str(Path(paths[0]).parent.parent if paths and Path(paths[0]).parent.name == 'Singles' else Path(paths[0]).parent) if paths else str(Path(cfg['output_root']) / core.safe_filename(name, 100))
            db.execute('INSERT OR IGNORE INTO creators(name,aliases,path) VALUES(?,?,?)', (name,json.dumps(aliases),path))
        names = {r['name']:r['id'] for r in db.execute('SELECT id,name FROM creators')}
        def identity(name, kind, handle, profile=1, enabled=True):
            canonical = cfg.get('creator_aliases', {}).get(name,name)
            if canonical not in names:
                # Link migrated source accounts by actual targets/aliases.
                match = db.execute('SELECT creator FROM assets WHERE url LIKE ? LIMIT 1', ('https://soundgasm.net/u/' + handle + '/%',)).fetchone() if kind == 'soundgasm' else None
                canonical = match[0] if match else canonical
            if canonical in names:
                db.execute('INSERT OR IGNORE INTO identities(creator_id,kind,handle,enabled) VALUES(?,?,?,?)', (names[canonical],kind,handle,int(enabled)))
                if profile != 1:
                    db.execute('UPDATE creators SET profile_id=? WHERE id=?', (profile,names[canonical]))
            elif not db.execute('SELECT id FROM identities WHERE creator_id IS NULL AND kind=? AND handle=?',(kind,handle)).fetchone():
                db.execute('INSERT INTO identities(creator_id,kind,handle,enabled) VALUES(NULL,?,?,?)',(kind,handle,int(enabled)))
        for name in cfg.get('allowlist',[]): identity(name,'reddit',name)
        for name in cfg.get('soundgasm_creators',[]): identity(name,'soundgasm',name)
        for c in cfg.get('youtube_channels',[]): identity(c['creator'],'youtube',c['channel_id'],2)
        sfw = list(cfg.get('sfw_expansion',{}).get('creators',[])) + list(core.meta_get(db,'sfw_auto_creators',{}).values())
        for c in sfw:
            identity(c['creator'],'reddit',c['reddit'],3)
            identity(c['creator'],'soundgasm',c['soundgasm'],3)
        # Derive any linked Soundgasm identity absent from static configuration.
        for r in db.execute('SELECT DISTINCT creator,url FROM assets WHERE url LIKE ?',( 'https://soundgasm.net/u/%',)).fetchall():
            parts=urllib.parse.unquote(urllib.parse.urlparse(r['url']).path).split('/')
            if len(parts)>2: identity(r['creator'],'soundgasm',parts[2],enabled=False)
        if not core.meta_get(db,'identity_migration_v2',False):
            configured_handles={x.casefold() for x in cfg.get('soundgasm_creators',[])}|{c['soundgasm'].casefold() for c in sfw}
            for r in db.execute("SELECT id,handle FROM identities WHERE kind='soundgasm'").fetchall():
                if r['handle'].casefold() not in configured_handles:db.execute('UPDATE identities SET enabled=0 WHERE id=?',(r['id'],))
            core.meta_set(db,'identity_migration_v2',True)
    counts = dict(db.execute('SELECT state,count(*) FROM assets GROUP BY state'))
    legacy_counts = dict(db.execute('SELECT state,count(*) FROM legacy.assets GROUP BY state'))
    mismatch = db.execute('SELECT count(*) FROM legacy.assets a LEFT JOIN assets b ON a.key=b.key WHERE b.key IS NULL OR a.saved_path IS NOT b.saved_path OR a.state IS NOT b.state').fetchone()[0]
    aliases_missing=db.execute('SELECT count(*) FROM legacy.aliases a LEFT JOIN aliases b ON a.id=b.id WHERE b.id IS NULL OR a.asset_key IS NOT b.asset_key').fetchone()[0]
    meta_missing=db.execute('SELECT count(*) FROM legacy.meta a LEFT JOIN meta b ON a.key=b.key WHERE b.key IS NULL OR a.value IS NOT b.value').fetchone()[0]
    manifest = media_manifest(db)
    preserved = all(v==manifest['files'].get(k) for k,v in before['files'].items())
    audit = {'counts':counts,'legacyCounts':legacy_counts,'creators':len(names),'sources':db.execute('SELECT count(*) FROM sources').fetchone()[0],
             'identities':db.execute('SELECT count(*) FROM identities').fetchone()[0], 'pathOrStateMismatch':mismatch,'aliasesMissing':aliases_missing,'metadataMismatch':meta_missing,
             'integrity':db.execute('PRAGMA integrity_check').fetchone()[0], 'files':len(manifest['files']),'missing':manifest['missing'],'outside':manifest['outside'],'existingFilesPreserved':preserved,'backup':str(backup)}
    audit['passed'] = counts==legacy_counts and not (mismatch or aliases_missing or meta_missing or manifest['missing'] or manifest['outside']) and preserved and audit['integrity']=='ok'
    with db:
        db.execute('INSERT INTO migration_audits(at,result) VALUES(?,?)',(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),json.dumps(audit)))
        core.meta_set(db,'migration_media_manifest',manifest)
    db.execute('DETACH DATABASE legacy')
    return audit


def configured_discovery(db,cfg):
    cfg=json.loads(json.dumps(cfg))
    identities=list(db.execute('SELECT i.*,c.name,c.profile_id FROM identities i JOIN creators c ON c.id=i.creator_id WHERE i.enabled=1 AND c.monitored=1'))
    paused_rows=db.execute('SELECT i.kind,i.handle FROM identities i LEFT JOIN creators c ON c.id=i.creator_id WHERE i.enabled=0 OR c.monitored=0').fetchall()
    paused_reddit={r['handle'].casefold() for r in paused_rows if r['kind']=='reddit'}
    paused_soundgasm={r['handle'].casefold() for r in paused_rows if r['kind']=='soundgasm'}
    cfg['allowlist']=list(dict.fromkeys([name for name in cfg.get('allowlist',[]) if name.casefold() not in paused_reddit]+[r['handle'] for r in identities if r['kind']=='reddit' and r['profile_id']!=3]))
    sfw_handles={c['soundgasm'].casefold() for c in cfg.get('sfw_expansion',{}).get('creators',[])}|{c['soundgasm'].casefold() for c in core.meta_get(db,'sfw_auto_creators',{}).values()}
    cfg['soundgasm_creators']=list(dict.fromkeys([name for name in cfg.get('soundgasm_creators',[]) if name.casefold() not in paused_soundgasm]+[r['handle'] for r in identities if r['kind']=='soundgasm' and r['handle'].casefold() not in sfw_handles]))
    unique_handles={}
    for handle in cfg['soundgasm_creators']:unique_handles.setdefault(handle.casefold(),handle)
    cfg['soundgasm_creators']=list(unique_handles.values())
    cfg['youtube_channels']=[{'creator':r['name'],'channel_id':r['handle']} for r in identities if r['kind']=='youtube']
    cfg['creator_aliases'].update({r['handle']:r['name'] for r in identities if r['kind']=='reddit'})
    monitored={r['name'] for r in identities}
    cfg['sfw_expansion']['creators']=[c for c in cfg.get('sfw_expansion',{}).get('creators',[]) if c['creator'] in monitored]
    return cfg


def discover(db,cfg,secrets,kind='all',shadow=True):
    if not shadow: production(db)
    started=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    production_before=production_fingerprint() if shadow else None
    app_meta_before=dict(db.execute('SELECT key,value FROM meta'))
    working=sqlite3.connect(':memory:') if shadow else db
    if shadow:
        legacy_path=Path(os.environ.get('ASMARR_LEGACY_CODE','/opt/asmr-scraper/scraper.py'))
        spec=importlib.util.spec_from_file_location('asmarr_legacy_reference',legacy_path)
        legacy=importlib.util.module_from_spec(spec);sys.modules[spec.name]=legacy;spec.loader.exec_module(legacy)
        baseline_cfg_path=CONFIG/'legacy-sources.yaml'
        legacy_cfg=yaml.safe_load(baseline_cfg_path.read_text()) if baseline_cfg_path.exists() else cfg
        db.backup(working); working.row_factory=sqlite3.Row
    before_manifest=media_manifest(db)
    reference=sqlite3.connect(':memory:');db.backup(reference);reference.row_factory=sqlite3.Row
    if shadow:
        # Each daily comparison starts from the current legacy state, so the old
        # writer may continue between observations without making parity stale.
        snapshot=sqlite3.connect(':memory:')
        with contextlib.closing(sqlite3.connect('file:/var/lib/asmr-scraper/state.db?mode=ro',uri=True)) as legacy_state:legacy_state.backup(snapshot)
        for table in ['assets','downloads','aliases','meta','sources','runs']:
            columns=[r[1] for r in snapshot.execute('PRAGMA table_info('+table+')')]
            names=','.join(columns);rows=snapshot.execute('SELECT '+names+' FROM '+table).fetchall()
            sql='INSERT OR REPLACE INTO '+table+'('+names+') VALUES('+','.join('?' for _ in columns)+')'
            working.executemany(sql,rows);reference.executemany(sql,rows)
        working.commit();reference.commit();snapshot.close()
    before={r['key'] for r in working.execute('SELECT key FROM assets')}
    baseline_meta=dict(working.execute('SELECT key,value FROM meta'))
    selected=['reddit','soundgasm','youtube','sfw'] if kind=='all' else [kind]
    selected=[k for k in selected if setting(db,'source.'+k+'.enabled','true')=='true']
    active=configured_discovery(db,cfg)
    def profile_rules(creator):
        name=active.get('creator_aliases',{}).get(creator,creator)
        return acquisition_profiles.discovery_config(active,profile(db,{'creator':name}))
    active['_profile_rules']=profile_rules
    original_enqueue=core.enqueue
    original_listing=core.youtube.listing
    capture=CaptureHTTP()
    youtube_fixtures={}
    def listing(channel,config,limit=None):
        result=original_listing(channel,config,limit)
        youtube_fixtures[(channel['channel_id'],limit)]=result
        return result
    core.youtube.listing=listing
    monitored={r['name'] for r in db.execute('SELECT name FROM creators WHERE monitored=1')}
    def monitored_enqueue(conn,sid,source,creator,title,options,published=0):
        if creator not in monitored:
            configured=set(active.get('allowlist',[]))|set(active.get('soundgasm_creators',[]))
            automatic=core.meta_get(conn,'sfw_auto_creators',{})
            qualified=next((c for c in automatic.values() if c['creator']==creator),None)
            if conn.execute('SELECT id FROM creators WHERE name=?',(creator,)).fetchone():return 'creator_unmonitored'
            if creator not in configured and not qualified:return 'creator_unmonitored'
            conn.execute('INSERT INTO creators(name,path,profile_id) VALUES(?,?,?)',(creator,str(Path(cfg['output_root'])/core.safe_filename(creator,100)),3 if qualified else 1))
            creator_id=conn.execute('SELECT id FROM creators WHERE name=?',(creator,)).fetchone()[0]
            if qualified:
                for k,h in [('reddit',qualified['reddit']),('soundgasm',qualified['soundgasm'])]:conn.execute('INSERT OR IGNORE INTO identities(creator_id,kind,handle) VALUES(?,?,?)',(creator_id,k,h))
            monitored.add(creator)
        result=original_enqueue(conn,sid,source,creator,title,options,published)
        creator_row=conn.execute('SELECT monitor_video FROM creators WHERE name=?',(creator,)).fetchone()
        if creator_row and creator_row[0]:
            recording=core.lookup(conn,sid)
            if recording:
                for target_kind,url in options:
                    host=(urllib.parse.urlparse(url).hostname or '').lower()
                    provider='youtube' if target_kind=='youtube' else 'reddit' if host=='v.redd.it' else None
                    if provider:video_media.add_candidate(conn,recording['key'],provider,url)
        return result
    core.enqueue=monitored_enqueue
    try:
        reports=core.discover(working,active,secrets,capture,selected,inspect=shadow)
    finally:
        core.enqueue=original_enqueue;core.youtube.listing=original_listing
    playlists=plex_playlists.sync(working,cfg,secrets,core,dry_run=True) if shadow else {'status':'scheduled'}
    proposed=[dict(r) for r in working.execute('SELECT * FROM assets') if r['key'] not in before]
    baseline=core.meta_get(db,'migration_media_manifest',before_manifest)
    after_manifest=media_manifest(db)
    unchanged=before_manifest==after_manifest and all(after_manifest['files'].get(k)==v for k,v in baseline['files'].items())
    original_meta=baseline_meta
    checkpoint_changes={r['key']:r['value'] for r in working.execute('SELECT * FROM meta') if ('checkpoint' in r['key'] or '_seen:' in r['key']) and original_meta.get(r['key'])!=r['value']} if shadow else {}
    summary={'mode':'shadow' if shadow else 'production','sources':reports,'proposed':proposed,'playlists':playlists,'mediaUnchanged':unchanged,'checkpointChanges':checkpoint_changes}
    # Eligibility comes from the preserved exact parser implementation. A separate
    # legacy inspection comparison must still be recorded before cycle is clean.
    summary['status']='healthy' if all(r['status'] in {'healthy','empty'} for r in reports) and playlists['status']!='failed' and unchanged else 'degraded'
    if shadow:
        def replay_listing(channel,config,limit=None):
            k=(channel['channel_id'],limit)
            if k not in youtube_fixtures: raise core.youtube.YouTubeError('fixture_missing')
            return youtube_fixtures[k]
        core.youtube.listing=replay_listing
        try: reference_reports=legacy.discover(reference,legacy_cfg,secrets,capture.replay(),selected,inspect=True)
        finally: core.youtube.listing=original_listing
        reference_proposed=[dict(r) for r in reference.execute('SELECT * FROM assets') if r['key'] not in before]
        compare_fields=['name','status','parsed','pages','accepted','items','rejected','backlog_pending']
        simplified=lambda rs:[{k:r.get(k) for k in compare_fields} for r in rs]
        reference_checkpoints={r['key']:r['value'] for r in reference.execute('SELECT * FROM meta') if ('checkpoint' in r['key'] or '_seen:' in r['key']) and original_meta.get(r['key'])!=r['value']}
        comparison={'legacyImplementationSha256':digest(legacy_path),'discoveryParity':sorted(proposed,key=lambda r:r['key'])==sorted(reference_proposed,key=lambda r:r['key']),
                    'eligibilityAndSourceParity':simplified(reports)==simplified(reference_reports),
                    'checkpointCalculationParity':checkpoint_changes==reference_checkpoints,
                    'productionCheckpointsUnchanged':production_fingerprint()==production_before,
                    'applicationCheckpointsUnchanged':dict(db.execute('SELECT key,value FROM meta'))==app_meta_before,
                    'mediaUnchanged':unchanged,'referenceSources':reference_reports,'referenceProposed':reference_proposed,
                    'playlistPreviewHealthy':playlists['status'] in {'preview','healthy','disabled'}}
        # Compare playlist calculations against the original database on the same
        # unchanged catalog. No Plex writes are permitted in either calculation.
        reference_playlists=plex_playlists.sync(reference,legacy_cfg,secrets,legacy,dry_run=True)
        comparison['playlistCalculationParity']=all(playlists.get(k)==reference_playlists.get(k) for k in ['tracks','categories','tagged','unclassified','status'])
        clean=summary['status']=='healthy' and all(comparison[k] for k in ['discoveryParity','eligibilityAndSourceParity','checkpointCalculationParity','productionCheckpointsUnchanged','applicationCheckpointsUnchanged','mediaUnchanged','playlistPreviewHealthy','playlistCalculationParity'])
        with db:
            db.execute('INSERT INTO shadow_cycles(started,finished,result,clean,comparison) VALUES(?,?,?,?,?)',(started,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),json.dumps(summary),int(clean),json.dumps(comparison)))
            for r in working.execute('SELECT * FROM sources'):
                if r['name'] in {s['name'] for s in reports}:
                    db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?)',tuple(r))
        fixture_path=STATE/'fixtures'/('discovery-'+started.replace(':','')+'.json')
        fixture_path.parent.mkdir(mode=0o700,exist_ok=True)
        fixture_path.write_text(json.dumps({'http':capture.entries,'youtube':[[list(k),v] for k,v in youtube_fixtures.items()]}));fixture_path.chmod(0o600)
        summary['comparison']=comparison;summary['clean']=clean
        working.close()
    else: event(db,'discovery',None,summary)
    reference.close()
    return summary


class CaptureHTTP(core.HTTP):
    """Record public source payloads for deterministic legacy/ASMarr comparison."""
    def __init__(self): super().__init__(); self.entries=[]
    def request(self,method,url,**kwargs):
        key=[method,url,kwargs.get('params',{})]
        try:
            response=super().request(method,url,**kwargs)
            # Never persist access tokens. Replay only needs a placeholder token.
            body=json.dumps({'access_token':'fixture-token'}) if '/access_token' in url else response.text
            self.entries.append({'key':key,'status':response.status_code,'body':body})
            return response
        except core.SourceError as e:
            self.entries.append({'key':key,'error':str(e),'state':e.status});raise
    def replay(self):
        entries=list(self.entries)
        class Replay:
            def request(self,method,url,**kwargs):
                key=[method,url,kwargs.get('params',{})]
                for i,e in enumerate(entries):
                    if e['key']==key:
                        entries.pop(i)
                        if 'error' in e: raise core.SourceError(e['error'],e['state'])
                        response=requests.Response();response.status_code=e['status'];response._content=e['body'].encode();response.encoding='utf-8';return response
                raise core.SourceError('fixture_request_missing','parse_failure')
            def get(self,url,**kw): return self.request('GET',url,**kw)
        return Replay()


def integrations():
    path=CONFIG/'integrations.json'
    return json.loads(path.read_text()) if path.exists() else {}


def normalize(value):
    return ' '.join(re.findall(r'\w+',core.plain(value).casefold()))


def rank(asset,candidate,opts):
    creator=normalize(asset['creator']); title=normalize(asset['title']); text=normalize(candidate.get('title',''))
    creator_ok=bool(creator) and (' '+creator+' ') in (' '+text+' ')
    wanted=set(title.split()); got=set(text.split()); similarity=len(wanted&got)/max(1,len(wanted))
    audio=bool(re.search(r'(?i)\b(m4a|mp3|aac|opus|flac|wav)\b',candidate.get('title','')))
    size=candidate.get('size',0) or 0
    published=asset.get('published') or 0
    proximity=0
    if published and candidate.get('publishDate'):
        import datetime
        try: proximity=max(0,1-abs(datetime.datetime.fromisoformat(candidate['publishDate'].replace('Z','+00:00')).timestamp()-published)/(30*86400))
        except ValueError: pass
    priority=opts.get('indexerPriorities',{}).get(str(candidate.get('indexerId')),0)
    score=.46*creator_ok+.36*similarity+.08*audio+.05*(0<size<=2147483648)+.03*proximity+.02*max(0,min(1,priority/100))
    return dict(candidate,confidence=round(score,4),exactCreator=creator_ok,titleMatch=round(similarity,4),autoGrab=creator_ok and similarity==1 and audio and score>=opts.get('confidenceThreshold',.92))


def search(db,key,media_kind='Audio'):
    row=db.execute('SELECT * FROM assets WHERE key=?',(key,)).fetchone()
    if not row: raise ValueError('recording_not_found')
    opts=integrations().get('prowlarr',{})
    if not opts.get('url') or not opts.get('apiKey'): return {'status':'not_configured','candidates':[]}
    response=requests.get(opts['url'].rstrip('/')+'/api/v1/search',headers={'X-Api-Key':opts['apiKey']},params={'query':normalize(row['creator']+' '+row['title']),'type':'search'},timeout=(10,60))
    response.raise_for_status()
    blocked={r[0] for r in db.execute('SELECT download_id FROM blocklist WHERE recording_key=?',(key,))}
    items=[rank(dict(row),c,opts) for c in response.json() if c.get('guid') not in blocked]
    if media_kind=='Video':
        for item in items:
            title=item.get('title','')
            match=re.search(r'(?i)\b(2160|1440|1080|720|480)p\b',title)
            item['resolution']=int(match.group(1)) if match else 0
            item['autoGrab']=False
            item['interactiveOnly']=True
        creator=db.execute('SELECT video_quality_profile_id FROM creators WHERE name=?',(row['creator'],)).fetchone()
        profile_row=db.execute('SELECT resolution,settings FROM video_quality_profiles WHERE id=?',(creator[0] if creator else 1,)).fetchone()
        profile_opts={'resolution':profile_row['resolution'],**json.loads(profile_row['settings'])} if profile_row else {'resolution':'Any'}
        items=[x for x in (video_media.rank_candidate(item,profile_opts) for item in items) if x]
        items.sort(key=lambda c:(c['rank'],c['confidence']),reverse=True)
    else:items.sort(key=lambda c:c['confidence'],reverse=True)
    with db: core.meta_set(db,'search_results:'+media_kind+':'+key,items)
    public=[{k:v for k,v in c.items() if k not in {'downloadUrl','infoUrl','magnetUrl'}} for c in items]
    result={'status':'ok','candidates':public}
    event(db,'interactive-search',key,{'count':len(items),'mediaKind':media_kind})
    return result


def qbit():
    opts=integrations().get('qbittorrent',{})
    if not opts.get('url'): raise ValueError('qbittorrent_not_configured')
    s=requests.Session(); base=opts['url'].rstrip('/')
    r=s.post(base+'/api/v2/auth/login',data={'username':opts.get('username',''),'password':opts.get('password','')},timeout=(10,30))
    if r.status_code!=200 or r.text.strip()!='Ok.': raise ValueError('qbittorrent_authentication_failed')
    return s,base,opts


def acquire(db,cfg,key):
    production(db); core.require_mount(cfg)
    row=db.execute('SELECT a.* FROM assets a JOIN creators c ON c.name=a.creator WHERE a.key=? AND c.monitored=1',(key,)).fetchone()
    if not row: raise ValueError('recording_missing_or_unmonitored')
    if row['state'] not in {'pending','failed','missing'}: return {'status':'already_terminal'}
    if row['saved_path'] and Path(row['saved_path']).is_file():
        s=Path(row['saved_path']);root=Path(cfg['output_root']).resolve()
        if not s.resolve().is_relative_to(root):raise ValueError('existing_path_outside_library')
        core.validate_audio(s)
        with db:db.execute("UPDATE assets SET state='complete',error=NULL,retry_after=0 WHERE key=?",(key,))
        return {'status':'already_imported','path':str(s)}
    rules=profile(db,row)
    effective=dict(cfg,naming_template=setting(db,'naming'),minimum_duration=rules.get('minimumDuration',0),allowed_formats=rules.get('allowedFormats',sorted(ALLOWED)))
    priorities=rules.get('sourcePriorities',['soundgasm','youtube','reddit'])
    asset=dict(row);asset['targets']=json.dumps(sorted(json.loads(row['targets']),key=lambda t:priorities.index(t[0]) if t[0] in priorities else len(priorities)))
    out,url=core.save_asset(asset,effective,core.HTTP())
    with db:
        now=int(time.time()); db.execute("UPDATE assets SET saved_path=?,state='complete',acquired=?,error=NULL,retry_after=0 WHERE key=?",(str(out),now,key))
        if has_table(db,'media_assets'):
            video_media.ensure_asset_rows(db,key)
            db.execute("UPDATE media_assets SET state='imported',wanted=1,saved_path=?,acquired=?,error=NULL,retry_after=0 WHERE recording_key=? AND media_kind='Audio'",(str(out),now,key))
        sid='asset:'+hashlib.sha256(key.encode()).hexdigest()
        db.execute('INSERT OR REPLACE INTO downloads VALUES(?,?,?,?,?,?,?)',(sid,row['source'],url,row['creator'],row['title'],str(out),now))
        core.meta_set(db,'plex_pending',True)
        core.meta_set(db,'plex_pending_paths',sorted(set(core.meta_get(db,'plex_pending_paths',[]))|{str(out)}))
    event(db,'import',key,{'path':str(out),'provider':'direct'})
    return {'status':'complete','path':str(out)}


def torrent_hash(payload):
    def parse(pos):
        marker=payload[pos:pos+1]
        if marker==b'i':
            end=payload.index(b'e',pos);return int(payload[pos+1:end]),end+1
        if marker in {b'l',b'd'}:
            result=[] if marker==b'l' else {};pos+=1
            while payload[pos:pos+1]!=b'e':
                value,pos=parse(pos)
                if marker==b'd': next_value,pos=parse(pos);result[value]=next_value
                else: result.append(value)
            return result,pos+1
        colon=payload.index(b':',pos);size=int(payload[pos:colon]);start=colon+1
        if size<0 or start+size>len(payload):raise ValueError('invalid_torrent')
        return payload[start:start+size],start+size
    if payload[:1]!=b'd':raise ValueError('invalid_torrent')
    pos=1
    while payload[pos:pos+1]!=b'e':
        key,pos=parse(pos);start=pos;_,pos=parse(pos)
        if key==b'info':return hashlib.sha1(payload[start:pos]).hexdigest()
    raise ValueError('torrent_info_missing')


def grab(db,key,candidate,media_kind='Audio'):
    production(db)
    candidates=core.meta_get(db,'search_results:'+media_kind+':'+key,[])
    resolved=next((c for c in candidates if c.get('guid')==candidate.get('guid') and c.get('indexerId')==candidate.get('indexerId')),None)
    if resolved is None:raise ValueError('search_candidate_expired')
    if db.execute('SELECT 1 FROM blocklist WHERE recording_key=? AND (download_id=? OR download_id IS NULL)',(key,resolved.get('guid'))).fetchone():raise ValueError('candidate_blocklisted')
    existing=db.execute("SELECT * FROM queue WHERE recording_key=? AND state NOT IN ('removed','failed')",(key,)).fetchone()
    if existing:return dict(existing)
    magnet=resolved.get('magnetUrl') or resolved.get('downloadUrl','')
    payload=None
    if magnet.startswith('magnet:'):
        hashes=urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query).get('xt',[])
        download_id=next((x.split(':')[-1] for x in hashes if x.startswith('urn:btih:')),'')
        if len(download_id)==32:
            import base64
            download_id=base64.b32decode(download_id.upper()).hex()
        if not re.fullmatch('[0-9a-fA-F]{40}',download_id):raise ValueError('magnet_hash_missing')
        download_id=download_id.lower()
    else:
        if not magnet.startswith(('http://','https://')):raise ValueError('candidate_download_url_missing')
        r=requests.get(magnet,timeout=(10,60));r.raise_for_status();payload=r.content
        if len(payload)>10*1024*1024:raise ValueError('torrent_metadata_too_large')
        download_id=torrent_hash(payload)
    s,base,opts=qbit()
    categories=s.get(base+'/api/v2/torrents/categories',timeout=30);categories.raise_for_status()
    if 'asmarr' not in categories.json():
        r=s.post(base+'/api/v2/torrents/createCategory',data={'category':'asmarr','savePath':'/mnt/downloads/asmarr'},timeout=30);r.raise_for_status()
    queue_id='torrent:'+download_id
    details={'guid':resolved.get('guid'),'indexerId':resolved.get('indexerId'),'confidence':resolved.get('confidence')}
    with db:
        db.execute("INSERT INTO queue(id,recording_key,download_id,state,provider,details,created,media_kind) VALUES(?,?,?,'submitting','qbittorrent',?,?,?) ON CONFLICT(id) DO NOTHING",(queue_id,key,download_id,json.dumps(details),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),media_kind))
    existing=s.get(base+'/api/v2/torrents/info',params={'hashes':download_id},timeout=30);existing.raise_for_status()
    if not existing.json():
        data={'category':'asmarr','savepath':'/mnt/downloads/asmarr'}
        if payload is None:data['urls']=magnet
        r=s.post(base+'/api/v2/torrents/add',data=data,files={'torrents':('recording.torrent',payload,'application/x-bittorrent')} if payload else None,timeout=60)
        if r.status_code!=200 or r.text.strip()!='Ok.':raise ValueError('qbittorrent_submission_unconfirmed')
    with db:db.execute("UPDATE queue SET state='downloading' WHERE id=?",(queue_id,))
    event(db,'grab',key,{'downloadId':download_id,'provider':'qbittorrent','mediaKind':media_kind})
    return {'status':'downloading','downloadId':download_id,'id':queue_id}


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def naming(db,asset,ext):
    template=setting(db,'naming','{Creator}/Singles/{Title} [{SourceId}].{ext}')
    values={'Creator':core.safe_filename(asset['creator'],100),'Title':core.safe_filename(core.compact_title(asset['title'])[0],110),
            'SourceId':hashlib.sha256(asset['key'].encode()).hexdigest()[:16],'ext':ext.lstrip('.')}
    if set(re.findall(r'\{([^{}]+)\}',template))-set(values):raise ValueError('unknown_naming_token')
    result=template
    for k,v in values.items():result=result.replace('{'+k+'}',v)
    root=Path(setting(db,'root','/mnt/cephfs/media/asmr')).resolve();out=root/result
    if not out.resolve().is_relative_to(root) or out.resolve()==root:raise ValueError('naming_path_outside_library')
    return out


def profile(db,asset):
    r=db.execute('SELECT p.settings FROM profiles p JOIN creators c ON c.profile_id=p.id WHERE c.name=?',(asset['creator'],)).fetchone()
    return json.loads(r[0]) if r else {}


def import_audio(db,cfg,key,path):
    production(db);core.require_mount(cfg)
    asset=db.execute('SELECT * FROM assets WHERE key=?',(key,)).fetchone()
    if not asset:raise ValueError('recording_not_found')
    if asset['state']=='complete' and asset['saved_path'] and Path(asset['saved_path']).is_file():return {'status':'already_imported','path':asset['saved_path']}
    source=Path(path).resolve();download_root=Path(cfg.get('download_root','/mnt/downloads/asmarr')).resolve()
    if not source.is_relative_to(download_root):raise ValueError('import_path_outside_download_root')
    if source.suffix.lower() not in ALLOWED or not source.is_file():raise ValueError('unsupported_import_file')
    settings=profile(db,asset)
    if source.suffix.lower() not in settings.get('allowedFormats',sorted(ALLOWED)):raise ValueError('profile_format_rejected')
    info=core.validate_audio(source)
    if any(s.get('codec_type')=='video' for s in info.get('streams',[])):raise ValueError('import_contains_video')
    if float(info['format']['duration'])<settings.get('minimumDuration',0):raise ValueError('profile_duration_rejected')
    destination=naming(db,asset,source.suffix.lower());destination.parent.mkdir(parents=True,exist_ok=True)
    expected=digest(source)
    if destination.exists():
        if digest(destination)!=expected:raise ValueError('import_destination_conflict')
    else:
        partial=destination.with_name(destination.name+'.asmarr-part')
        try:
            with source.open('rb') as src,partial.open('wb') as dst:
                shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
            if digest(partial)!=expected:raise ValueError('import_hash_mismatch')
            core.validate_audio(partial);partial.chmod(0o644)
            # Hard-link publication is atomic and fails if a destination appears.
            os.link(partial,destination);partial.unlink()
        except Exception:
            if partial.exists():partial.unlink()
            raise
    with db:
        now=int(time.time());db.execute("UPDATE assets SET state='complete',saved_path=?,acquired=?,error=NULL,retry_after=0 WHERE key=?",(str(destination),now,key))
        if has_table(db,'media_assets'):
            video_media.ensure_asset_rows(db,key)
            db.execute("UPDATE media_assets SET state='imported',wanted=1,saved_path=?,acquired=?,error=NULL,retry_after=0 WHERE recording_key=? AND media_kind='Audio'",(str(destination),now,key))
        db.execute('INSERT OR REPLACE INTO downloads VALUES(?,?,?,?,?,?,?)',('asset:'+hashlib.sha256(key.encode()).hexdigest(),'qbittorrent',asset['url'],asset['creator'],asset['title'],str(destination),now))
        core.meta_set(db,'plex_pending',True);core.meta_set(db,'plex_pending_paths',sorted(set(core.meta_get(db,'plex_pending_paths',[]))|{str(destination)}))
    event(db,'import',key,{'provider':'qbittorrent','path':str(destination),'sha256':expected})
    return {'status':'complete','path':str(destination),'sha256':expected}


def monitor_downloads(db,cfg):
    production(db)
    jobs=db.execute("SELECT * FROM queue WHERE provider='qbittorrent' AND state IN ('submitting','downloading','importing','imported')").fetchall()
    if not jobs:return {'status':'idle','jobs':[]}
    s,base,opts=qbit();results=[]
    for job in jobs:
        r=s.get(base+'/api/v2/torrents/info',params={'hashes':job['download_id']},timeout=30);r.raise_for_status();info=r.json()
        if not info:results.append({'id':job['id'],'status':'job_missing'});continue
        info=info[0]
        if info.get('category')!='asmarr':results.append({'id':job['id'],'status':'category_mismatch'});continue
        if info.get('progress',0)<1 or info.get('state') in {'checkingDL','checkingUP','moving','checkingResumeData'}:results.append({'id':job['id'],'status':info.get('state'),'progress':info.get('progress')});continue
        try:
            path=Path(info.get('content_path') or info.get('save_path',''))
            media_kind=job['media_kind'] if 'media_kind' in job.keys() else 'Audio'
            extensions=video_media.VIDEO_EXTENSIONS if media_kind=='Video' else ALLOWED
            candidates=[path] if path.is_file() and path.suffix.lower() in extensions else [p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in extensions]
            if len(candidates)!=1:raise ValueError('completed_torrent_media_ambiguous')
            if media_kind=='Video':
                cached=core.meta_get(db,'search_results:Video:'+job['recording_key'],[])
                detail=json.loads(job['details'] or '{}')
                selected=next((c for c in cached if c.get('guid')==detail.get('guid')),{'provider':'prowlarr','provider_id':job['download_id']})
                result=video_media.import_video(db,cfg,job['recording_key'],str(candidates[0]),selected,import_audio)
            else:result=import_audio(db,cfg,job['recording_key'],str(candidates[0]))
            with db:db.execute("UPDATE queue SET state='imported' WHERE id=?",(job['id'],))
            # Destination is durable and validated before any torrent removal.
            if opts.get('retention','keep')=='remove-torrent':
                response=s.post(base+'/api/v2/torrents/delete',data={'hashes':job['download_id'],'deleteFiles':'false'},timeout=30);response.raise_for_status()
                with db:db.execute("UPDATE queue SET state='removed' WHERE id=?",(job['id'],))
            results.append(dict(result,id=job['id']))
        except (ValueError,OSError) as e:
            with db:db.execute("UPDATE queue SET state='failed',details=? WHERE id=?",(json.dumps({'error':str(e)}),job['id']))
            results.append({'id':job['id'],'status':'failed','reason':str(e)})
    return {'status':'ok','jobs':results}


def process_queue(db,cfg):
    production(db);saved=[];failed=[];fallback=[]
    now=int(time.time())
    rows=db.execute("SELECT a.* FROM assets a JOIN creators c ON c.name=a.creator WHERE c.monitored=1 AND a.state IN ('pending','failed') AND a.retry_after<=? AND NOT EXISTS(SELECT 1 FROM queue q WHERE q.recording_key=a.key AND q.state NOT IN ('failed','removed')) AND NOT EXISTS(SELECT 1 FROM blocklist b WHERE b.recording_key=a.key AND b.download_id IS NULL) ORDER BY COALESCE(a.published,0) DESC LIMIT ?",(now,cfg.get('max_downloads_per_run',25))).fetchall()
    for row in rows:
        rules=profile(db,row)
        if not json.loads(row['targets'] or '[]') or row['attempts']>=rules.get('directRetries',3):
            if rules.get('fallback',True):
                found=search(db,row['key'],'Audio')
                candidate=next((c for c in found['candidates'] if c['autoGrab']),None)
                if candidate:fallback.append(grab(db,row['key'],candidate))
                else:event(db,'search-review',row['key'],{'candidates':len(found['candidates'])})
            continue
        try:saved.append(acquire(db,cfg,row['key']))
        except Exception as e:
            attempts=row['attempts']+1;reason=str(e) if isinstance(e,(core.SourceError,core.MediaError,ValueError)) else type(e).__name__
            with db:db.execute("UPDATE assets SET state='failed',attempts=?,retry_after=?,error=? WHERE key=?",(attempts,now+min(86400,300*2**min(attempts,8)),reason,row['key']))
            event(db,'direct-failure',row['key'],{'attempt':attempts,'error':reason});failed.append({'key':row['key'],'error':reason})
    return {'saved':saved,'failed':failed,'fallback':fallback,'downloads':monitor_downloads(db,cfg)}


def dispatch(operation,args):
    with contextlib.closing(connect()) as db:
        cfg,secrets=configuration(db)
        if operation=='migrate': return migrate(db,cfg)
        if operation=='scan':
            manifest=media_manifest(db); original=core.meta_get(db,'migration_media_manifest',manifest)
            return {'files':len(manifest['files']),'missing':manifest['missing'],'outside':manifest['outside'],'unchanged':all(manifest['files'].get(k)==v for k,v in original['files'].items())}
        if operation=='discover': return discover(db,cfg,secrets,args.get('kind','all'),args.get('shadow',True))
        if operation=='metadata':
            row=db.execute('SELECT * FROM assets WHERE key=?',(args['key'],)).fetchone()
            return dict(row) if row else {'status':'not_found'}
        if operation=='acquire': return acquire(db,cfg,args['key'])
        if operation=='process-queue':return process_queue(db,cfg)
        if operation=='video-backfill':return video_media.backfill(db,cfg,secrets,args,core)
        if operation=='video-process-queue':return video_media.process_queue(db,cfg,import_audio)
        if operation=='search': return search(db,args['key'],args.get('mediaKind','Audio'))
        if operation=='grab':return grab(db,args['key'],args['candidate'],args.get('mediaKind','Audio'))
        if operation=='downloads':return monitor_downloads(db,cfg)
        if operation=='manual-import':
            if args.get('mediaKind','Audio')=='Video':
                return video_media.import_video(db,cfg,args['key'],args['path'],args.get('candidate',{'provider':'manual'}),import_audio)
            return import_audio(db,cfg,args['key'],args['path'])
        if operation=='rename-preview':
            rows=db.execute('SELECT * FROM assets WHERE key=?',(args['key'],)).fetchall() if args.get('key') else db.execute("SELECT * FROM assets WHERE state='complete'").fetchall()
            return [{'key':r['key'],'existing':r['saved_path'],'proposed':str(naming(db,r,Path(r['saved_path']).suffix if r['saved_path'] else '.m4a'))} for r in rows]
        if operation=='remove-download':
            production(db);job=db.execute('SELECT * FROM queue WHERE download_id=?',(args['downloadId'],)).fetchone()
            if not job or job['state'] not in {'imported','removed'}:raise ValueError('torrent_not_imported')
            s,base,_=qbit();r=s.post(base+'/api/v2/torrents/delete',data={'hashes':job['download_id'],'deleteFiles':'false'},timeout=30);r.raise_for_status()
            with db:db.execute("UPDATE queue SET state='removed' WHERE id=?",(job['id'],))
            return {'status':'removed','filesPreserved':True}
        if operation=='integration-test':
            kind=args['kind']
            if kind=='qbittorrent':
                s,base,_=qbit();r=s.get(base+'/api/v2/app/version',timeout=30);r.raise_for_status();return {'status':'healthy','version':r.text}
            if kind=='prowlarr':
                opts=integrations().get(kind,{})
                r=requests.get(opts['url'].rstrip('/')+'/api/v1/system/status',headers={'X-Api-Key':opts['apiKey']},timeout=30);r.raise_for_status();return {'status':'healthy','version':r.json().get('version')}
            if kind=='plex':return {'status':'healthy','machine':plex_playlists.Client(cfg,secrets).call('GET','/identity').get('machineIdentifier')}
            raise ValueError('unknown_integration')
        if operation=='test-source':
            kind=args['kind']
            if kind=='reddit': core.Reddit(core.HTTP(),secrets.get('reddit',{})).authenticate()
            elif kind=='soundgasm': core.sg_listing(core.HTTP(),cfg['soundgasm_creators'][0])
            elif kind=='youtube': core.youtube.listing(cfg['youtube_channels'][0],cfg,1)
            elif kind=='sfw':core.Reddit(core.HTTP(),secrets.get('reddit',{})).authenticate()
            else: raise ValueError('unknown_source')
            return {'status':'healthy','kind':kind}
        if operation=='playlists':
            preview=args.get('preview',True)
            if not preview: production(db)
            return plex_playlists.sync(db,cfg,secrets,core,dry_run=preview)
        if operation=='plex': production(db); return {'status':core.plex_refresh(db,cfg,secrets,core.HTTP())}
        if operation=='plex-video': production(db); return video_media.plex_refresh(db,cfg,secrets)
        if operation=='plex-video-verify': return video_media.plex_library_validation(cfg,secrets)
        if operation=='plex-verify':
            client=plex_playlists.Client(cfg,secrets)
            rows=plex_playlists.all_items(client,'/library/sections/'+str(cfg['plex']['section_id'])+'/all','Track',type=10)
            indexed={p.get('file'):t.get('ratingKey') for t in rows for p in t.findall('.//Part')}
            missing=[r['saved_path'] for r in db.execute("SELECT saved_path FROM assets WHERE state='complete'") if r['saved_path'] not in indexed]
            return {'tracks':len(rows),'unindexed':missing,'indexedImported':db.execute("SELECT count(*) FROM assets WHERE state='complete'").fetchone()[0]-len(missing)}
        if operation=='playlists-verify':
            client=plex_playlists.Client(cfg,secrets);section=str(cfg['plex']['section_id'])
            tracks=plex_playlists.all_items(client,'/library/sections/'+section+'/all','Track',type=10)
            tracks=plex_playlists.details(client,[t.get('ratingKey') for t in tracks],section)
            titles=dict(db.execute("SELECT saved_path,title FROM assets WHERE state='complete' AND saved_path IS NOT NULL"))
            from categories import classify,metadata_text
            expected={cat:set() for cat in cfg.get('playlists',{}).get('categories',list(CATEGORIES))}
            for track in tracks:
                originals=[titles[p.get('file')] for p in track.findall('.//Part') if p.get('file') in titles]
                for cat in classify(metadata_text(track,originals)):
                    if cat in expected:expected[cat].add(track.get('ratingKey'))
            registry=core.meta_get(db,'managed_playlists',{});report=[]
            for cat,record in registry.items():
                key=record.get('id');detail=client.call('GET','/playlists/'+key).find('Playlist')
                if detail is None:report.append({'category':cat,'id':key,'status':'missing'});continue
                wanted=expected.get(cat,set());members=plex_playlists.all_items(client,'/playlists/'+key+'/items','Track')
                actual={t.get('ratingKey') for t in members}
                valid=detail.get('guid')==record.get('guid') and detail.get('smart')=='1' and plex_playlists.filter_matches(detail.get('content'),section,'ASMR: '+CATEGORIES[cat][0]) and actual==wanted
                report.append({'category':cat,'id':key,'status':'ok' if valid else 'mismatch','members':len(actual),'expected':len(wanted)})
            return {'status':'ok' if all(r['status']=='ok' for r in report) and len(report)==15 else 'degraded','managedCount':len(report),'playlists':report}
        raise ValueError('unknown_operation')


if __name__=='__main__':
    try:
        request=json.load(sys.stdin)
        print(json.dumps(dispatch(request['operation'],request.get('arguments',{}))))
    except Exception as exc:
        # Detailed diagnostics are local and protected; HTTP and process output
        # must never include URLs with tokens, passwords or signed media URLs.
        log=STATE/'provider-errors.log'
        with log.open('a') as f: f.write(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())+' '+type(exc).__name__+' operation failed\n')
        log.chmod(0o600)
        print(json.dumps({'error':type(exc).__name__}),file=sys.stderr)
        sys.exit(1)
