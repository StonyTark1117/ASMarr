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
import zipfile

import requests
import yaml
import scraper as core
import plex_playlists
import video_media
import acquisition_profiles
import source_configuration
import creator_removal
import backup_snapshot
import public_audio_sources
from categories import CATEGORIES

STATE = Path(os.environ.get('ASMARR_STATE', '/var/lib/asmarr'))
CONFIG = Path(os.environ.get('ASMARR_CONFIG', '/etc/asmarr'))
DB = STATE / 'asmarr.db'
ALLOWED = {'.m4a', '.mp3', '.aac', '.opus', '.flac', '.wav'}
CREATOR_AUDIO_KINDS = {'whyp', 'audiochan', 'hotaudio'}


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


def has_column(db,table,name):
    return any(row[1]==name for row in db.execute('PRAGMA table_info('+table+')'))


def deployed_release_identity():
    # This minimal non-secret runtime identity is readable by the service.
    # Root-only acceptance evidence remains under STATE/acceptance.
    path=STATE/'deployed-release-runtime.json'
    try:
        value=json.loads(path.read_text())
    except (OSError,ValueError,json.JSONDecodeError):
        return {'sourceCommit':None,'artifactSha256':None}
    source=value.get('sourceCommit');artifact=value.get('artifactSha256')
    return {'sourceCommit':source if isinstance(source,str) and re.fullmatch(r'[a-f0-9]{40}',source) else None,
            'artifactSha256':artifact if isinstance(artifact,str) and re.fullmatch(r'[a-f0-9]{64}',artifact) else None}


def configuration(db):
    cfg = source_configuration.apply(db, yaml.safe_load((CONFIG / 'sources.yaml').read_text()))
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
        def identity(name, kind, handle, profile=1, enabled=True, enrollment=None):
            canonical = cfg.get('creator_aliases', {}).get(name,name)
            if canonical not in names:
                # Link migrated source accounts by actual targets/aliases.
                match = db.execute('SELECT creator FROM assets WHERE url LIKE ? LIMIT 1', ('https://soundgasm.net/u/' + handle + '/%',)).fetchone() if kind == 'soundgasm' else None
                canonical = match[0] if match else canonical
            if canonical in names:
                db.execute('INSERT OR IGNORE INTO identities(creator_id,kind,handle,enabled) VALUES(?,?,?,?)', (names[canonical],kind,handle,int(enabled)))
                if profile != 1:
                    db.execute('UPDATE creators SET profile_id=? WHERE id=?', (profile,names[canonical]))
                if enrollment=='explicit':
                    db.execute("UPDATE creators SET follow_mode='explicit',follow_mode_review=0 WHERE id=?",(names[canonical],))
                elif enrollment=='discovered' and not db.execute("SELECT 1 FROM creators WHERE id=? AND follow_mode='explicit' AND follow_mode_review=0",(names[canonical],)).fetchone():
                    db.execute("UPDATE creators SET follow_mode='discovered',follow_mode_review=0 WHERE id=?",(names[canonical],))
            elif not db.execute('SELECT id FROM identities WHERE creator_id IS NULL AND kind=? AND handle=?',(kind,handle)).fetchone():
                db.execute('INSERT INTO identities(creator_id,kind,handle,enabled) VALUES(NULL,?,?,?)',(kind,handle,int(enabled)))
        for name in cfg.get('allowlist',[]): identity(name,'reddit',name,enrollment='explicit')
        for name in cfg.get('soundgasm_creators',[]): identity(name,'soundgasm',name,enrollment='explicit')
        for c in cfg.get('youtube_channels',[]): identity(c['creator'],'youtube',c['channel_id'],2,enrollment='explicit')
        sfw = list(cfg.get('sfw_expansion',{}).get('creators',[])) + list(core.meta_get(db,'sfw_auto_creators',{}).values())
        for c in sfw:
            identity(c['creator'],'reddit',c['reddit'],3,enrollment='discovered')
            identity(c['creator'],'soundgasm',c['soundgasm'],3,enrollment='discovered')
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
    follow="CASE WHEN c.follow_mode='explicit' AND c.follow_mode_review=0 THEN 'explicit' ELSE 'discovered' END" if has_column(db,'creators','follow_mode_review') else ("c.follow_mode" if has_column(db,'creators','follow_mode') else "'explicit'")
    identities=list(db.execute('SELECT i.*,c.name,c.profile_id,'+follow+' AS follow_mode FROM identities i JOIN creators c ON c.id=i.creator_id WHERE i.enabled=1 AND c.monitored=1'))
    paused_rows=db.execute('SELECT i.kind,i.handle FROM identities i LEFT JOIN creators c ON c.id=i.creator_id WHERE i.enabled=0 OR c.monitored=0').fetchall()
    paused_reddit={r['handle'].casefold() for r in paused_rows if r['kind']=='reddit'}
    paused_soundgasm={r['handle'].casefold() for r in paused_rows if r['kind']=='soundgasm'}
    for removed in creator_removal.exclusions(db):
        names={removed['name'].casefold(),*(name.casefold() for name in json.loads(removed['aliases']))}
        paused_reddit.update(names);paused_soundgasm.update(names)
        for identity in json.loads(removed['identities']):
            if identity['kind']=='reddit':paused_reddit.add(identity['handle'].casefold())
            if identity['kind']=='soundgasm':paused_soundgasm.add(identity['handle'].casefold())
    cfg['allowlist']=list(dict.fromkeys([name for name in cfg.get('allowlist',[]) if name.casefold() not in paused_reddit]+[r['handle'] for r in identities if r['kind']=='reddit' and r['profile_id']!=3]))
    sfw_handles={c['soundgasm'].casefold() for c in cfg.get('sfw_expansion',{}).get('creators',[])}|{c['soundgasm'].casefold() for c in core.meta_get(db,'sfw_auto_creators',{}).values()}
    cfg['soundgasm_creators']=list(dict.fromkeys([name for name in cfg.get('soundgasm_creators',[]) if name.casefold() not in paused_soundgasm]+[r['handle'] for r in identities if r['kind']=='soundgasm' and r['handle'].casefold() not in sfw_handles]))
    unique_handles={}
    for handle in cfg['soundgasm_creators']:unique_handles.setdefault(handle.casefold(),handle)
    cfg['soundgasm_creators']=list(unique_handles.values())
    cfg['youtube_channels']=[{'creator':r['name'],'channel_id':r['handle']} for r in identities if r['kind']=='youtube']
    for kind in ('whyp','audiochan','hotaudio'):
        cfg[kind+'_creators']=list(dict.fromkeys(r['handle'] for r in identities if r['kind']==kind))
    cfg['creator_aliases'].update({r['handle']:r['name'] for r in identities if r['kind']=='reddit'})
    cfg['_identity_creators']={r['kind']+':'+r['handle'].casefold():r['name'] for r in identities}
    cfg['_creator_follow_modes']={r['name']:r['follow_mode'] for r in identities}
    cfg['_public_identities']=[dict(r) for r in identities if r['kind'] in public_audio_sources.KINDS]
    monitored={r['name'] for r in identities}
    cfg['sfw_expansion']['creators']=[c for c in cfg.get('sfw_expansion',{}).get('creators',[]) if c['creator'] in monitored]
    return cfg


def _public_eligibility(db, creator, item):
    review=',follow_mode_review' if has_column(db,'creators','follow_mode_review') else ',0 AS follow_mode_review'
    row=db.execute('SELECT follow_mode,profile_id'+review+' FROM creators WHERE name=?',(creator,)).fetchone()
    if not row:return False,'creator_missing'
    settings=profile(db,{'creator':creator})
    duration=item.get('duration')
    if item.get('available') is False:return False,item.get('availabilityReason','recording_unavailable')
    if 'freeDownload' in item and not item['freeDownload']:return False,'creator_free_download_not_enabled'
    if item.get('promotional'):return False,'promotional_or_qa'
    if duration is not None and float(duration)<float(settings.get('minimumDuration',0)):
        return False,'below_minimum_duration'
    if row['follow_mode']=='explicit' and not row['follow_mode_review']:return True,'accepted_followed_creator'
    return core.title_passes(item.get('title',''),settings,trusted=True)


def eligibility_repair_preview(db,args):
    creator_id=int(args['creatorId'])
    creator=db.execute('SELECT * FROM creators WHERE id=?',(creator_id,)).fetchone()
    if not creator:raise ValueError('creator_not_found')
    if creator['follow_mode']!='explicit' or creator['follow_mode_review']:
        raise ValueError('explicit_follow_classification_required')
    rows=db.execute('''SELECT a.key,a.title,a.published,a.source,
          COALESCE((SELECT state FROM media_assets WHERE recording_key=a.key AND media_kind='Audio'),a.state) AS audio_state,
          (SELECT state FROM media_assets WHERE recording_key=a.key AND media_kind='Video') AS video_state
        FROM assets a WHERE a.creator=? AND (a.state='suppressed' OR EXISTS(
          SELECT 1 FROM media_assets m WHERE m.recording_key=a.key AND m.state='suppressed'))
          AND NOT EXISTS(SELECT 1 FROM blocklist b WHERE b.recording_key=a.key)
        ORDER BY COALESCE(a.published,0) DESC,a.key''',(creator['name'],)).fetchall()
    items=[]
    for row in rows:
        eligible,reason=core.title_passes(row['title'],profile(db,{'creator':creator['name']}),trusted=True,explicitly_followed=True)
        media=[]
        if row['audio_state']=='suppressed':media.append('Audio')
        if row['video_state']=='suppressed':media.append('Video')
        items.append({'providerId':row['key'],'recordingKey':row['key'],'title':row['title'],'published':row['published'],
                      'source':row['source'],'mediaKinds':media,'eligible':bool(eligible and media),
                      'eligibilityReason':reason if media else 'no_suppressed_media','requiresMetadata':True})
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());expires=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()+86400))
    preview_id=hashlib.sha256((str(creator_id)+'\0repair\0'+str(time.time_ns())).encode()).hexdigest()[:32]
    result={'items':items,'scope':'known recordings suppressed by prior filtering','complete':True}
    with db:db.execute('''INSERT INTO source_previews(id,creator_id,kind,identity,state,complete,discovered,eligible,result,created,expires)
                          VALUES(?,?,'eligibility_repair',?,'complete',1,?,?,?,?,?)''',
                       (preview_id,creator_id,creator['name'],len(items),sum(i['eligible'] for i in items),json.dumps(result),now,expires))
    return dict(result,previewId=preview_id,creatorId=creator_id,discovered=len(items),eligible=sum(i['eligible'] for i in items))


def eligibility_repair_apply(db,args):
    preview=db.execute("SELECT * FROM source_previews WHERE id=? AND kind='eligibility_repair'",(args['previewId'],)).fetchone()
    if not preview:raise ValueError('repair_preview_not_found')
    if preview['expires']<time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()):raise ValueError('repair_preview_expired')
    creator=db.execute('SELECT * FROM creators WHERE id=?',(preview['creator_id'],)).fetchone()
    if not creator or not creator['monitored'] or creator['follow_mode']!='explicit' or creator['follow_mode_review']:
        raise ValueError('explicit_follow_authorization_changed')
    selected=set(str(x) for x in args.get('recordingKeys',[]));all_items=bool(args.get('allEligible',False))
    snapshot=json.loads(preview['result']);eligible={i['recordingKey']:i for i in snapshot['items'] if i.get('eligible')}
    if not all_items and not selected:raise ValueError('select_recordings_or_all')
    if not selected.issubset(eligible):raise ValueError('selection_not_in_reviewed_preview')
    chosen=eligible.values() if all_items else (eligible[key] for key in selected)
    restored={'Audio':0,'Video':0}
    with db:
        for item in chosen:
            current=db.execute('SELECT creator FROM assets WHERE key=?',(item['recordingKey'],)).fetchone()
            if not current or current['creator']!=creator['name']:continue
            for media_kind in item['mediaKinds']:
                changed=db.execute("UPDATE media_assets SET state='wanted',wanted=1,retry_after=0,error=NULL WHERE recording_key=? AND media_kind=? AND state='suppressed'",(item['recordingKey'],media_kind)).rowcount
                restored[media_kind]+=changed
                if media_kind=='Audio' and changed:db.execute("UPDATE assets SET state='pending',retry_after=0,error=NULL WHERE key=? AND state='suppressed'",(item['recordingKey'],))
    return {'status':'complete','reviewedPreviewId':preview['id'],'selected':len(eligible) if all_items else len(selected),'restored':restored}


def source_preview(db,cfg,args):
    creator_id=int(args['creatorId'])
    creator=db.execute('SELECT * FROM creators WHERE id=?',(creator_id,)).fetchone()
    if not creator:raise ValueError('creator_not_found')
    identity_row=None
    if args.get('identityId'):
        identity_row=db.execute('SELECT * FROM identities WHERE id=? AND creator_id=?',(int(args['identityId']),creator_id)).fetchone()
        if not identity_row:raise ValueError('source_identity_not_found')
        kind,identity=identity_row['kind'],identity_row['handle']
    else:
        kind=args['kind'];identity=public_audio_sources.normalize_identity(kind,args['identity'])
        identity_row=db.execute('SELECT * FROM identities WHERE creator_id=? AND kind=? AND handle=?',(creator_id,kind,identity)).fetchone()
    if kind not in public_audio_sources.KINDS:raise ValueError('source_preview_not_supported')
    allowed=profile(db,{'creator':creator['name']}).get('allowedFormats',sorted(ALLOWED))
    validator_key='rss_validators:'+hashlib.sha256(identity.encode()).hexdigest() if kind=='rss' else None
    cursor=args.get('cursor');validators=core.meta_get(db,validator_key,{}) if validator_key and not cursor else None
    result=public_audio_sources.catalog(public_audio_sources.SafeHTTP(core.HTTP()),kind,identity,allowed=allowed,limit=int(args.get('limit',5000)),validators=validators,cursor=cursor)
    if result.get('notModified') and identity_row:
        cached=[]
        for row in db.execute('SELECT * FROM source_catalog WHERE identity_id=? ORDER BY published DESC LIMIT ?',(identity_row['id'],int(args.get('limit',5000)))):
            item=json.loads(row['metadata']);item.update(providerId=row['provider_id'],title=row['title'],published=row['published'],duration=row['duration']);cached.append(item)
        result['items']=cached;result['title']=identity
    elif cursor and identity_row:
        combined={str(item['providerId']):item for item in result['items']}
        for row in db.execute('SELECT * FROM source_catalog WHERE identity_id=?',(identity_row['id'],)):
            if str(row['provider_id']) in combined:continue
            item=json.loads(row['metadata']);item.update(providerId=row['provider_id'],title=row['title'],published=row['published'],duration=row['duration']);combined[str(row['provider_id'])]=item
        result['items']=list(combined.values())
    for item in result['items']:
        item['eligible'],item['eligibilityReason']=_public_eligibility(db,creator['name'],item)
        item['requiresMetadata']=item.get('duration') is None
        item['catalogId']=public_audio_sources.stable_id(kind,result['identity'],item['providerId'])
    preview_id=hashlib.sha256((str(creator_id)+'\0'+kind+'\0'+result['identity']+'\0'+str(time.time_ns())).encode()).hexdigest()[:32]
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());expires=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()+86400))
    public_result={k:v for k,v in result.items() if k!='releases'}
    with db:
        if validator_key and result.get('validators') and result.get('complete'):core.meta_set(db,validator_key,result['validators'])
        db.execute('''INSERT INTO source_previews(id,identity_id,creator_id,kind,identity,state,complete,discovered,eligible,result,created,expires)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (preview_id,identity_row['id'] if identity_row else None,creator_id,kind,result['identity'],'complete' if result['complete'] else 'partial',int(result['complete']),len(result['items']),sum(i['eligible'] for i in result['items']),json.dumps(public_result),now,expires))
        if identity_row:
            for item in result['items']:
                metadata={k:v for k,v in item.items() if k not in {'catalogId','eligible','eligibilityReason'}}
                db.execute('''INSERT INTO source_catalog(id,identity_id,provider_id,title,published,duration,eligible,eligibility_reason,metadata,discovered,updated)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_id,provider_id) DO UPDATE SET
                              title=excluded.title,published=excluded.published,duration=excluded.duration,eligible=excluded.eligible,
                              eligibility_reason=excluded.eligibility_reason,metadata=excluded.metadata,updated=excluded.updated''',
                           (item['catalogId'],identity_row['id'],str(item['providerId']),item['title'],int(item.get('published') or 0),item.get('duration'),int(item['eligible']),item['eligibilityReason'],json.dumps(metadata),now,now))
    return dict(public_result,previewId=preview_id,creatorId=creator_id,kind=kind,
                discovered=len(result['items']),eligible=sum(i['eligible'] for i in result['items']))


def _enqueue_catalog(db,catalog_id):
    row=db.execute('''SELECT sc.*,i.kind,i.id AS source_identity_id,c.name AS creator
                      FROM source_catalog sc JOIN identities i ON i.id=sc.identity_id
                      JOIN creators c ON c.id=i.creator_id WHERE sc.id=?''',(catalog_id,)).fetchone()
    if not row or not row['eligible']:return 'ineligible'
    if row['recording_key']:
        return 'known'
    # The catalog id is derived from the normalized provider identity and
    # provider recording id. Database row ids must never influence it.
    key=row['id']
    existing=db.execute('SELECT key FROM assets WHERE key=?',(key,)).fetchone()
    if not existing:
        db.execute('''INSERT INTO assets(key,url,targets,source,creator,title,published,state)
                      VALUES(?,?,?,?,?,?,?,'pending')''',
                   (key,key,json.dumps([[row['kind'],'catalog:'+row['id']]]),row['kind']+':'+str(row['source_identity_id']),row['creator'],row['title'],row['published']))
        db.execute('INSERT OR IGNORE INTO aliases(id,asset_key) VALUES(?,?)',(row['kind']+':'+row['provider_id'],key))
    db.execute('UPDATE source_catalog SET recording_key=? WHERE id=?',(key,row['id']))
    return 'queued' if not existing else 'known'


def audio_backfill(db,args):
    preview=db.execute('SELECT * FROM source_previews WHERE id=?',(args['previewId'],)).fetchone()
    if not preview:raise ValueError('source_preview_not_found')
    if preview['state'] not in {'complete','partial'} or preview['expires']<time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()):raise ValueError('source_preview_expired')
    if not preview['identity_id']:raise ValueError('link_source_before_backfill')
    selected=set(str(x) for x in args.get('providerIds',[]));all_items=bool(args.get('allEligible',False))
    if not all_items and not selected:raise ValueError('select_recordings_or_all')
    if all_items and not preview['complete']:raise ValueError('catalog_scan_incomplete')
    snapshot=json.loads(preview['result']);reviewed={str(item['providerId']) for item in snapshot.get('items',[]) if item.get('eligible')}
    if not all_items and not selected.issubset(reviewed):raise ValueError('selection_not_in_reviewed_preview')
    rows=db.execute('SELECT id,provider_id FROM source_catalog WHERE identity_id=? AND eligible=1 ORDER BY published DESC',(preview['identity_id'],)).fetchall()
    rows=[r for r in rows if str(r['provider_id']) in reviewed and (all_items or str(r['provider_id']) in selected)]
    job_id='audio:'+hashlib.sha256((preview['id']+'\0'+str(time.time_ns())).encode()).hexdigest()[:24]
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());queued=known=0
    with db:
        db.execute('''INSERT INTO backfill_jobs(id,creator_id,media_kind,state,discovered,eligible,total,identity_id,preview_id,selected,selection,started,updated)
                      VALUES(?,?,'Audio','running',?,?,?,?,?,?,?,?,?)''',
                   (job_id,preview['creator_id'],preview['discovered'],preview['eligible'],len(rows),preview['identity_id'],preview['id'],len(rows),json.dumps([str(r['provider_id']) for r in rows]),now,now))
        for row in rows:
            status=_enqueue_catalog(db,row['id']);queued+=status=='queued';known+=status=='known'
        db.execute("UPDATE backfill_jobs SET state='complete',finished=?,updated=? WHERE id=?",(now,now,job_id))
    return {'status':'complete','jobId':job_id,'selected':len(rows),'queued':queued,'known':known}


def discover_public(db,cfg,selected):
    reports=[]
    for identity in configured_discovery(db,cfg).get('_public_identities',[]):
        if identity['kind'] not in selected:continue
        name=identity['kind']+':'+str(identity['id']);before={r[0] for r in db.execute('SELECT provider_id FROM source_catalog WHERE identity_id=?',(identity['id'],))}
        try:
            result=source_preview(db,cfg,{'creatorId':identity['creator_id'],'identityId':identity['id']})
            new=[i for i in result['items'] if str(i['providerId']) not in before]
            queued=0
            # The first successful scan establishes the reviewed baseline.
            if before:
                with db:
                    for item in new:
                        if item['eligible']:queued+=_enqueue_catalog(db,item['catalogId'])=='queued'
            report={'name':name,'status':'healthy','parsed':result['discovered'],'accepted':result['eligible'],
                    'items':{'new':len(new),'queued':queued,'baselineEstablished':not bool(before)},'previewId':result['previewId']}
        except Exception as error:
            report={'name':name,'status':getattr(error,'stage','unavailable'),'error':str(error),'parsed':0,'accepted':0,'items':{}}
        reports.append(report)
    now=int(time.time())
    with db:
        for report in reports:
            previous=db.execute('SELECT last_success,last_download FROM sources WHERE name=?',(report['name'],)).fetchone()
            last_success=now if report['status']=='healthy' else (previous['last_success'] if previous else None)
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?)',(report['name'],report['status'],now,last_success,previous['last_download'] if previous else None,json.dumps(report)))
    return reports


def _download_response(http,url,path,max_bytes=2147483648):
    # Buzzsprout's signed audio host requires an ordinary browser user agent.
    # Other publishers can vary dynamic-ad bytes by user agent, so keep the
    # provider client identity everywhere else.
    host=(urllib.parse.urlparse(url).hostname or '').lower()
    headers={'Accept-Encoding':'identity'}
    if host.endswith('.buzzsprout.com') or host=='buzzsprout.com':
        headers['User-Agent']='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36'
    with http.get(url,stream=True,headers=headers) as response:
        public_audio_sources.public_url(response.url)
        size=0;expected=int(response.headers.get('Content-Length') or 0)
        with path.open('wb') as output:
            for chunk in response.iter_content(1024*1024):
                size+=len(chunk)
                if size>max_bytes:raise ValueError('download_limit_exceeded')
                output.write(chunk)
            output.flush();os.fsync(output.fileno())
        content_type=response.headers.get('Content-Type','').split(';',1)[0]
    if expected and size!=expected:raise ValueError('download_length_mismatch')
    return {'bytes':size,'contentType':content_type,'url':url}


def _update_audio_backfill_counts(db,identity_id):
    for job in db.execute("SELECT id,selection FROM backfill_jobs WHERE identity_id=? AND media_kind='Audio'",(identity_id,)).fetchall():
        selected=set(json.loads(job['selection'] or '[]'))
        rows=db.execute('''SELECT sc.provider_id,sc.metadata,a.state FROM source_catalog sc LEFT JOIN assets a ON a.key=sc.recording_key
                           WHERE sc.identity_id=?''',(identity_id,)).fetchall()
        relevant=[r for r in rows if str(r['provider_id']) in selected]
        downloaded=sum(bool(json.loads(r['metadata'] or '{}').get('transferSha256')) for r in relevant)
        imported=sum(r['state']=='complete' for r in relevant)
        db.execute('UPDATE backfill_jobs SET downloaded=?,imported=?,updated=? WHERE id=?',
                   (downloaded,imported,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),job['id']))


def acquire_public(db,cfg,key):
    production(db);core.require_mount(cfg)
    row=db.execute('''SELECT a.*,sc.id AS catalog_id,sc.provider_id,sc.duration AS catalog_duration,sc.metadata,
                      i.id AS identity_id,i.kind,i.handle,i.enabled,c.monitored,c.follow_mode
                      FROM assets a JOIN source_catalog sc ON sc.recording_key=a.key
                      JOIN identities i ON i.id=sc.identity_id JOIN creators c ON c.id=i.creator_id
                      WHERE a.key=?''',(key,)).fetchone()
    if not row:raise ValueError('public_recording_not_found')
    if not row['enabled'] or not row['monitored']:raise ValueError('source_identity_not_approved')
    if setting(db,'source.'+row['kind']+'.enabled','false')!='true':raise ValueError('source_disabled')
    if creator_removal.excluded(db,row['creator'],[(row['kind'],row['handle'])]):raise ValueError('source_identity_not_approved')
    settings=profile(db,row);allowed=set(settings.get('allowedFormats',sorted(ALLOWED)))
    details=json.loads(row['metadata']);download_root=Path(cfg.get('download_root','/mnt/downloads/asmarr')).resolve();download_root.mkdir(parents=True,exist_ok=True)
    current_item=dict(details,title=row['title'],duration=row['catalog_duration'])
    eligible,reason=_public_eligibility(db,row['creator'],current_item)
    if not eligible:
        with db:
            db.execute("UPDATE assets SET state='suppressed',error=? WHERE key=?",('Eligibility changed before acquisition: '+reason,key))
            db.execute("UPDATE media_assets SET state='suppressed',wanted=0,error=? WHERE recording_key=? AND media_kind='Audio'",('Eligibility changed before acquisition: '+reason,key))
            db.execute('UPDATE source_catalog SET eligible=0,eligibility_reason=?,updated=? WHERE id=?',(reason,time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),row['catalog_id']))
        return {'status':'suppressed','reason':reason}
    bandcamp_cache=None
    with tempfile.TemporaryDirectory(prefix='asmarr-public-',dir=download_root) as work:
        work=Path(work);transfer=work/'transfer'
        expected_md5=None
        if row['kind']=='rss':
            url=public_audio_sources.rss_enclosure(public_audio_sources.SafeHTTP(core.HTTP()),row['handle'],row['provider_id'])
        elif row['kind']=='internet_archive':
            resolved=public_audio_sources.archive_download(public_audio_sources.SafeHTTP(core.HTTP()),row['handle'],row['provider_id'],allowed)
            url=resolved['url'];expected_md5=resolved.get('expectedMd5');details.update(resolved)
        else:
            url=public_audio_sources.bandcamp_download(public_audio_sources.SafeHTTP(core.HTTP()),details['releaseUrl'],allowed)
        fallback_used=False
        if row['kind']=='bandcamp':
            cache_dir=download_root/'.bandcamp-cache';cache_dir.mkdir(mode=0o700,exist_ok=True)
            for stale in list(cache_dir.glob('*.zip'))+list(cache_dir.glob('*.partial')):
                if time.time()-stale.stat().st_mtime>86400:stale.unlink(missing_ok=True)
            cache_key=hashlib.sha256((str(details.get('releaseId'))+'\0'+','.join(sorted(allowed))).encode()).hexdigest()
            bandcamp_cache=cache_dir/(cache_key+'.zip')
            if bandcamp_cache.is_file():
                public_audio_sources.safe_zip_members(bandcamp_cache,max_bytes=int(cfg.get('max_file_bytes',2147483648))*2)
                transfer=bandcamp_cache;transfer_result={'bytes':transfer.stat().st_size,'contentType':'application/zip','url':'cached-release-archive'}
            else:
                partial=cache_dir/(cache_key+'.partial')
                try:
                    transfer_result=_download_response(public_audio_sources.SafeHTTP(core.HTTP()),url,partial,int(cfg.get('max_file_bytes',2147483648)))
                    public_audio_sources.safe_zip_members(partial,max_bytes=int(cfg.get('max_file_bytes',2147483648))*2)
                    os.replace(partial,bandcamp_cache);transfer=bandcamp_cache
                except Exception:
                    partial.unlink(missing_ok=True);raise
        else:
            try:transfer_result=_download_response(public_audio_sources.SafeHTTP(core.HTTP()),url,transfer,int(cfg.get('max_file_bytes',2147483648)))
            except (core.SourceError,requests.RequestException,public_audio_sources.PublicSourceError):
                fallback=public_audio_sources.libsyn_fallback(url) if row['kind']=='rss' else None
                if not fallback:raise
                fallback_used=True;details['libsynOriginalEnclosure']=url
                transfer_result=_download_response(public_audio_sources.SafeHTTP(core.HTTP()),fallback,transfer,int(cfg.get('max_file_bytes',2147483648)))
        transfer_hash=digest(transfer)
        if expected_md5:
            md5=hashlib.md5(usedforsecurity=False)
            with transfer.open('rb') as source:
                for chunk in iter(lambda:source.read(1024*1024),b''):md5.update(chunk)
            if md5.hexdigest().lower()!=expected_md5.lower():raise ValueError('published_checksum_mismatch')
        if row['kind']=='bandcamp':
            members=public_audio_sources.safe_zip_members(transfer,max_bytes=int(cfg.get('max_file_bytes',2147483648))*2)
            member=public_audio_sources.match_bandcamp_member(members,row['title'],details.get('trackNumber'))
            ext=Path(member.filename).suffix.lower();media=work/('track'+ext)
            with zipfile.ZipFile(transfer) as archive,archive.open(member) as source,media.open('wb') as output:shutil.copyfileobj(source,output)
        else:
            ext=details.get('extension') or {'audio/mpeg':'.mp3','audio/mp4':'.m4a','audio/aac':'.aac','audio/ogg':'.opus','audio/flac':'.flac','audio/wav':'.wav'}.get(transfer_result['contentType']) or Path(urllib.parse.urlparse(url).path).suffix.lower()
            if ext not in ALLOWED:raise ValueError('unsupported_media_format')
            media=work/('recording'+ext);os.replace(transfer,media)
        if ext not in allowed:raise ValueError('profile_format_rejected')
        info=core.validate_audio(media)
        if core.real_video_streams(info):raise ValueError('output_contains_video')
        actual=float(info['format']['duration']);expected=float(row['catalog_duration'] or 0);warnings=[]
        if expected and expected-actual>max(10,expected*.02):
            review=dict(details,transferSha256=transfer_hash,deliveredBytes=transfer_result['bytes'],deliveredDuration=actual,
                        catalogDuration=expected,reviewReason='delivered_duration_shorter_than_catalog')
            with db:
                db.execute("UPDATE assets SET state='review',error=? WHERE key=?",('Delivered duration is materially shorter than the refreshed catalog duration',key))
                db.execute("UPDATE media_assets SET state='review',wanted=0,error=? WHERE recording_key=? AND media_kind='Audio'",('Delivered duration is materially shorter than the refreshed catalog duration',key))
                db.execute("UPDATE source_catalog SET eligible=0,eligibility_reason='duration_discrepancy_review',metadata=?,updated=? WHERE id=?",(json.dumps(review),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),row['catalog_id']))
                _update_audio_backfill_counts(db,row['identity_id'])
            return {'status':'review_hold','reason':'delivered_duration_shorter_than_catalog','catalogDuration':expected,
                    'deliveredDuration':actual,'transferSha256':transfer_hash}
        if expected and actual-expected>max(10,expected*.02):warnings.append('delivered_duration_longer_than_catalog')
        core.fully_decode_audio(media);core.tag_audio(media,row['creator'],row['title'],row['published']);core.validate_audio(media);core.fully_decode_audio(media)
        result=import_audio(db,cfg,key,str(media),provider=row['kind'])
        final_hash=digest(Path(result['path']))
    audit=dict(details,transferSha256=transfer_hash,finalSha256=final_hash,deliveredBytes=transfer_result['bytes'],deliveredDuration=actual,warnings=warnings,fallbackUsed=fallback_used)
    with db:
        db.execute('UPDATE source_catalog SET metadata=?,updated=? WHERE id=?',(json.dumps(audit),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),row['catalog_id']))
        _update_audio_backfill_counts(db,row['identity_id'])
    if bandcamp_cache and not db.execute('''SELECT 1 FROM source_catalog sc JOIN assets a ON a.key=sc.recording_key
          WHERE sc.identity_id=? AND json_extract(sc.metadata,'$.releaseId')=? AND a.state IN ('pending','failed','missing') LIMIT 1''',
          (row['identity_id'],str(details.get('releaseId')))).fetchone():bandcamp_cache.unlink(missing_ok=True)
    return dict(result,transferSha256=transfer_hash,finalSha256=final_hash,warnings=warnings)


def verify_public_download(db,cfg,kind,identity,provider_id=None):
    creator=db.execute('''SELECT c.name,c.profile_id FROM identities i JOIN creators c ON c.id=i.creator_id
                          WHERE i.kind=? AND i.handle=? AND i.enabled=1''',(kind,identity)).fetchone()
    if not creator:raise ValueError('source_identity_not_approved')
    allowed=profile(db,{'creator':creator['name']}).get('allowedFormats',sorted(ALLOWED))
    result=public_audio_sources.catalog(public_audio_sources.SafeHTTP(core.HTTP()),kind,identity,allowed=allowed,limit=5000 if provider_id else 10)
    item=next((x for x in result['items'] if (provider_id is None or str(x['providerId'])==str(provider_id)) and _public_eligibility(db,creator['name'],x)[0]),None)
    if not item:return {'status':'empty','kind':kind,'stage':'eligibility'}
    root=Path(tempfile.mkdtemp(prefix='asmarr-source-test-',dir=STATE/'logs'))
    try:
        transfer=root/'transfer';expected_md5=None
        if kind=='rss':url=public_audio_sources.rss_enclosure(public_audio_sources.SafeHTTP(core.HTTP()),identity,item['providerId'])
        elif kind=='internet_archive':
            resolved=public_audio_sources.archive_download(public_audio_sources.SafeHTTP(core.HTTP()),identity,item['providerId'],allowed);url=resolved['url'];expected_md5=resolved.get('expectedMd5')
        else:url=public_audio_sources.bandcamp_download(public_audio_sources.SafeHTTP(core.HTTP()),item['releaseUrl'],allowed)
        fallback_used=False
        try:transfer_result=_download_response(public_audio_sources.SafeHTTP(core.HTTP()),url,transfer,int(cfg.get('max_file_bytes',2147483648)))
        except (core.SourceError,requests.RequestException,public_audio_sources.PublicSourceError):
            fallback=public_audio_sources.libsyn_fallback(url) if kind=='rss' else None
            if not fallback:raise
            fallback_used=True
            transfer_result=_download_response(public_audio_sources.SafeHTTP(core.HTTP()),fallback,transfer,int(cfg.get('max_file_bytes',2147483648)))
        if expected_md5:
            checksum=hashlib.md5(usedforsecurity=False)
            with transfer.open('rb') as source:
                for chunk in iter(lambda:source.read(1024*1024),b''):checksum.update(chunk)
            if checksum.hexdigest().lower()!=expected_md5.lower():raise ValueError('published_checksum_mismatch')
        media=transfer;archive_tracks_validated=None
        if kind=='bandcamp':
            members=public_audio_sources.safe_zip_members(transfer);member=public_audio_sources.match_bandcamp_member(members,item['title'],item.get('trackNumber'))
            extracted={};archive_tracks_validated=0
            with zipfile.ZipFile(transfer) as archive:
                for index,candidate in enumerate(members):
                    target=root/('track-'+str(index)+Path(candidate.filename).suffix.lower())
                    with archive.open(candidate) as source,target.open('wb') as output:shutil.copyfileobj(source,output)
                    track_info=core.validate_audio(target)
                    if core.real_video_streams(track_info):raise ValueError('output_contains_video')
                    core.fully_decode_audio(target);archive_tracks_validated+=1;extracted[candidate.filename]=target
            media=extracted[member.filename]
        info=core.validate_audio(media)
        if core.real_video_streams(info):raise ValueError('output_contains_video')
        core.fully_decode_audio(media)
        return {'status':'healthy','kind':kind,'stage':'transfer','downloadVerified':True,
                'bytes':transfer_result['bytes'],'duration':float(info['format']['duration']),
                'sha256':digest(media),'providerId':item['providerId'],'fallbackUsed':fallback_used,
                'archiveTracksValidated':archive_tracks_validated}
    finally:shutil.rmtree(root,ignore_errors=True)


def source_suggestions(db,args):
    creator_id=args.get('creatorId')
    creators=db.execute('''SELECT * FROM creators WHERE monitored=1 AND follow_mode='explicit' AND follow_mode_review=0
                           AND (? IS NULL OR id=?) ORDER BY id LIMIT 100''',(creator_id,creator_id)).fetchall()
    now=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());created=0;errors=[];cached=0
    def save(creator,kind,identity,title,evidence,score):
        nonlocal created
        try:identity=public_audio_sources.normalize_identity(kind,identity)
        except (ValueError,TypeError):return
        if db.execute('SELECT 1 FROM identities WHERE kind=? AND handle=?',(kind,identity)).fetchone():return
        if creator_removal.excluded(db,creator['name'],[(kind,identity)]):return
        sid=hashlib.sha256((str(creator['id'])+'\0'+kind+'\0'+identity).encode()).hexdigest()[:32]
        db.execute('''INSERT INTO source_suggestions(id,creator_id,kind,identity,title,evidence,score,state,created,updated)
                      VALUES(?,?,?,?,?,?,?,'pending',?,?) ON CONFLICT(creator_id,kind,identity) DO UPDATE SET
                      title=excluded.title,evidence=excluded.evidence,score=excluded.score,updated=excluded.updated,
                      state=CASE WHEN source_suggestions.state='dismissed' THEN 'dismissed' ELSE 'pending' END''',
                   (sid,creator['id'],kind,identity,title,json.dumps(evidence),score,now,now));created+=1
    for creator in creators:
        last=core.meta_get(db,'source_suggestion_scan:'+str(creator['id']),0)
        if not args.get('refresh') and time.time()-float(last or 0)<7*86400:
            cached+=1;continue
        aliases=json.loads(creator['aliases'] or '[]');terms=[creator['name'],*aliases[:3]]
        try:
            response=requests.get('https://itunes.apple.com/search',params={'term':creator['name'],'media':'podcast','entity':'podcast','limit':10,'country':'US'},timeout=(10,30));response.raise_for_status()
            for item in response.json().get('results',[]):
                feed=item.get('feedUrl');candidate=item.get('collectionName') or ''
                if not feed:continue
                exact=normalize(candidate)==normalize(creator['name']) or normalize(item.get('artistName',''))==normalize(creator['name'])
                save(creator,'rss',feed,candidate,{'directory':'Apple Podcasts','artist':item.get('artistName'),'page':item.get('collectionViewUrl'),'exactName':exact},.9 if exact else .55)
        except (requests.RequestException,ValueError,KeyError) as error:errors.append({'creatorId':creator['id'],'kind':'rss','error':type(error).__name__})
        try:
            response=requests.get('https://archive.org/advancedsearch.php',params={'q':'mediatype:audio AND creator:"'+creator['name'].replace('"','')+'"','fl[]':['identifier','title','creator'],'rows':10,'page':1,'output':'json'},timeout=(10,30));response.raise_for_status()
            for item in response.json().get('response',{}).get('docs',[]):
                identifier=item.get('identifier');
                if identifier:save(creator,'internet_archive',identifier,item.get('title') or identifier,{'directory':'Internet Archive','creator':item.get('creator'),'exactCreator':normalize(str(item.get('creator','')))==normalize(creator['name'])},.85)
        except (requests.RequestException,ValueError,KeyError) as error:errors.append({'creatorId':creator['id'],'kind':'internet_archive','error':type(error).__name__})
        try:
            response=requests.get('https://bandcamp.com/search',params={'q':creator['name'],'item_type':'b'},timeout=(10,30));response.raise_for_status()
            if 'Client Challenge' in response.text:raise ValueError('verification_challenge')
            links=list(dict.fromkeys(re.findall(r'https://[A-Za-z0-9-]+\.bandcamp\.com(?:/(?:album|track)/[^\"\'<> ]+)?',response.text)))[:10]
            for link in links:save(creator,'bandcamp',link,urllib.parse.urlparse(link).hostname.split('.')[0],{'directory':'Bandcamp','query':creator['name']},.5)
        except (requests.RequestException,ValueError) as error:errors.append({'creatorId':creator['id'],'kind':'bandcamp','error':str(error) if isinstance(error,ValueError) else type(error).__name__})
        core.meta_set(db,'source_suggestion_scan:'+str(creator['id']),time.time())
    db.commit()
    return {'status':'complete' if not errors else 'partial','creators':len(creators),'cachedCreators':cached,'suggestionsUpdated':created,'errors':errors}


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
    selected=['reddit','soundgasm','youtube','sfw','whyp','audiochan','hotaudio',*sorted(public_audio_sources.KINDS)] if kind=='all' else [kind]
    selected=[k for k in selected if setting(db,'source.'+k+'.enabled','false' if k=='hotaudio' or k in public_audio_sources.KINDS else 'true')=='true']
    active=configured_discovery(db,cfg)
    def profile_rules(creator):
        name=active.get('creator_aliases',{}).get(creator,creator)
        return acquisition_profiles.discovery_config(active,profile(db,{'creator':name}))
    active['_profile_rules']=profile_rules
    original_enqueue=core.enqueue
    original_listing=core.youtube.listing
    original_targets=core.targets
    capture=CaptureHTTP()
    youtube_fixtures={}
    reddit_video_posts={}
    def listing(channel,config,limit=None):
        result=original_listing(channel,config,limit)
        youtube_fixtures[(channel['channel_id'],limit)]=result
        return result
    core.youtube.listing=listing
    monitored={r['name'] for r in db.execute('SELECT name FROM creators WHERE monitored=1')}
    def discovery_targets(post, config):
        # Reddit cross-posts do not prove ownership of a creator-audio profile.
        # Those sites are monitored only through an approved linked identity.
        options=[target for target in original_targets(post,config)
                 if target[0] not in CREATOR_AUDIO_KINDS]
        author=post.get('author','')
        creator=active.get('_identity_creators',{}).get('reddit:'+author.casefold(),active.get('creator_aliases',{}).get(author,author))
        enabled=working.execute('SELECT monitor_video FROM creators WHERE name=?',(creator,)).fetchone()
        if enabled and enabled[0]:
            for provider,url in video_media.reddit_video_targets(post,config.get('youtube_min_duration_seconds',180)):
                target=['youtube' if provider=='youtube' else 'reddit_media',url]
                if target not in options:options.append(target)
                reddit_video_posts[url]=post
        return options
    core.targets=discovery_targets
    def monitored_enqueue(conn,sid,source,creator,title,options,published=0):
        source_kind,_,source_handle=source.partition(':')
        if creator_removal.excluded(conn,creator,[(source_kind,source_handle)]):return 'creator_removed'
        if creator not in monitored:
            configured=set(active.get('allowlist',[]))|set(active.get('soundgasm_creators',[]))
            automatic=core.meta_get(conn,'sfw_auto_creators',{})
            qualified=next((c for c in automatic.values() if c['creator']==creator),None)
            if conn.execute('SELECT id FROM creators WHERE name=?',(creator,)).fetchone():return 'creator_unmonitored'
            if creator not in configured and not qualified:return 'creator_unmonitored'
            conn.execute('INSERT INTO creators(name,path,profile_id,follow_mode) VALUES(?,?,?,?)',(creator,str(Path(cfg['output_root'])/core.safe_filename(creator,100)),3 if qualified else 1,'discovered' if qualified else 'explicit'))
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
                    if provider:video_media.add_candidate(conn,recording['key'],provider,url,
                                                          post=reddit_video_posts.get(video_media.canonical_url(url)))
        return result
    core.enqueue=monitored_enqueue
    try:
        reports=core.discover(working,active,secrets,capture,selected,inspect=shadow)
        if not shadow and any(k in public_audio_sources.KINDS for k in selected):
            reports.extend(discover_public(working,active,selected))
    finally:
        core.enqueue=original_enqueue;core.youtube.listing=original_listing;core.targets=original_targets
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
        legacy_selected=[source for source in selected if source in {'reddit','soundgasm','youtube','sfw'}]
        try: reference_reports=legacy.discover(reference,legacy_cfg,secrets,capture.replay(),legacy_selected,inspect=True)
        finally: core.youtube.listing=original_listing
        reference_proposed=[dict(r) for r in reference.execute('SELECT * FROM assets') if r['key'] not in before]
        compare_fields=['name','status','parsed','pages','accepted','items','rejected','backlog_pending']
        simplified=lambda rs:[{k:r.get(k) for k in compare_fields} for r in rs]
        reference_checkpoints={r['key']:r['value'] for r in reference.execute('SELECT * FROM meta') if ('checkpoint' in r['key'] or '_seen:' in r['key']) and original_meta.get(r['key'])!=r['value']}
        legacy_proposed=[asset for asset in proposed if asset['source'].split(':')[0] in legacy_selected]
        legacy_reports=[item for item in reports if item['name'].split(':')[0] in legacy_selected]
        comparison={**deployed_release_identity(),'legacyImplementationSha256':digest(legacy_path),'discoveryParity':sorted(legacy_proposed,key=lambda r:r['key'])==sorted(reference_proposed,key=lambda r:r['key']),
                    'eligibilityAndSourceParity':simplified(legacy_reports)==simplified(reference_reports),
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
    source_kind,_,source_handle=row['source'].partition(':')
    if source_kind in public_audio_sources.KINDS:
        return acquire_public(db,cfg,key)
    if source_kind in CREATOR_AUDIO_KINDS:
        if setting(db,'source.'+source_kind+'.enabled','false' if source_kind=='hotaudio' else 'true')!='true':
            raise ValueError('source_disabled')
        approved=db.execute('''SELECT 1 FROM identities i JOIN creators c ON c.id=i.creator_id
                WHERE c.name=? AND c.monitored=1 AND i.enabled=1 AND i.kind=?
                  AND i.handle=? COLLATE NOCASE''',(row['creator'],source_kind,source_handle)).fetchone()
        if not approved or creator_removal.excluded(db,row['creator'],[(source_kind,source_handle)]):
            raise ValueError('source_identity_not_approved')
    if row['state'] not in {'pending','failed','missing'}: return {'status':'already_terminal'}
    if row['saved_path'] and Path(row['saved_path']).is_file():
        s=Path(row['saved_path']);root=Path(cfg['output_root']).resolve()
        if not s.resolve().is_relative_to(root):raise ValueError('existing_path_outside_library')
        core.validate_audio(s)
        with db:db.execute("UPDATE assets SET state='complete',error=NULL,retry_after=0 WHERE key=?",(key,))
        return {'status':'already_imported','path':str(s)}
    rules=profile(db,row)
    effective=dict(cfg,naming_template=setting(db,'naming'),minimum_duration=rules.get('minimumDuration',0),allowed_formats=rules.get('allowedFormats',sorted(ALLOWED)))
    # GUI-linked identities are the live approval authority, not the immutable
    # legacy YAML channel list. Scope approval to this creator; disabled or
    # excluded identities must never be resurrected by old configuration.
    effective['youtube_channels']=[]
    if has_table(db,'identities'):
        for identity in db.execute('''SELECT i.handle,c.name FROM identities i
                JOIN creators c ON c.id=i.creator_id
                WHERE i.kind='youtube' AND i.enabled=1 AND c.monitored=1 AND c.name=?
                ORDER BY i.id''',(row['creator'],)):
            if not creator_removal.excluded(db,identity['name'],[('youtube',identity['handle'])]):
                effective['youtube_channels'].append({'creator':identity['name'],'channel_id':identity['handle']})
    if has_column(db,'creators','follow_mode_review'):
        follow=db.execute('SELECT follow_mode,follow_mode_review FROM creators WHERE name=?',(row['creator'],)).fetchone()
    elif has_column(db,'creators','follow_mode'):
        follow=db.execute('SELECT follow_mode,0 AS follow_mode_review FROM creators WHERE name=?',(row['creator'],)).fetchone()
    else:follow=None
    if follow and follow['follow_mode']=='explicit' and not follow['follow_mode_review']:
        rules=dict(rules,_explicit_follow=True)
    priorities=rules.get('sourcePriorities',['soundgasm','youtube','reddit'])
    targets=[]
    for target in json.loads(row['targets']):
        kind,url=target
        if kind in CREATOR_AUDIO_KINDS:
            if kind!=source_kind:
                continue
            if kind=='hotaudio':
                parts=urllib.parse.urlparse(url).path.split('/')
                if len(parts)<4 or parts[2].casefold()!=source_handle.casefold():
                    continue
        targets.append(target)
    if not targets:raise ValueError('source_target_not_approved')
    effective['_authorized_creator_targets']=[target for target in targets if target[0] in CREATOR_AUDIO_KINDS]
    asset=dict(row);asset['targets']=json.dumps(sorted(targets,key=lambda t:priorities.index(t[0]) if t[0] in priorities else len(priorities)))
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
    existing=db.execute("""SELECT * FROM queue WHERE recording_key=? AND media_kind=?
                           AND state NOT IN ('removed','failed')""",(key,media_kind)).fetchone()
    if existing:
        if media_kind=='Video' and has_table(db,'media_assets') and existing['state'] in {'submitting','downloading','importing'}:
            video_media.ensure_asset_rows(db,key,video_wanted=True)
            with db:
                db.execute("""UPDATE media_assets SET state='downloading',wanted=1,error=NULL
                              WHERE recording_key=? AND media_kind='Video'""",(key,))
        status='downloading' if existing['state'] in {'submitting','downloading','importing'} else existing['state']
        return {'status':status,'downloadId':existing['download_id'],'id':existing['id'],
                'key':key,'mediaKind':media_kind}
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
    with db:
        db.execute("UPDATE queue SET state='downloading' WHERE id=?",(queue_id,))
        if media_kind=='Video' and has_table(db,'media_assets'):
            video_media.ensure_asset_rows(db,key,video_wanted=True)
            db.execute("""UPDATE media_assets SET state='downloading',wanted=1,error=NULL
                          WHERE recording_key=? AND media_kind='Video'""",(key,))
    event(db,'grab',key,{'downloadId':download_id,'provider':'qbittorrent','mediaKind':media_kind})
    return {'status':'downloading','downloadId':download_id,'id':queue_id,
            'key':key,'mediaKind':media_kind}


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


def import_audio(db,cfg,key,path,*,provider='qbittorrent'):
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
    if core.real_video_streams(info):raise ValueError('import_contains_video')
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
        db.execute('INSERT OR REPLACE INTO downloads VALUES(?,?,?,?,?,?,?)',('asset:'+hashlib.sha256(key.encode()).hexdigest(),provider,asset['url'],asset['creator'],asset['title'],str(destination),now))
        core.meta_set(db,'plex_pending',True);core.meta_set(db,'plex_pending_paths',sorted(set(core.meta_get(db,'plex_pending_paths',[]))|{str(destination)}))
    event(db,'import',key,{'provider':provider,'mediaKind':'Audio','path':str(destination),'sha256':expected})
    return {'status':'complete','path':str(destination),'sha256':expected}


def import_derived_audio(db,cfg,key,path):
    return import_audio(db,cfg,key,path,provider='video-master')


def monitor_downloads(db,cfg):
    production(db)
    jobs=db.execute("SELECT * FROM queue WHERE provider='qbittorrent' AND state IN ('submitting','downloading','importing','imported')").fetchall()
    if not jobs:return {'status':'idle','jobs':[]}
    s,base,opts=qbit();results=[]
    for job in jobs:
        r=s.get(base+'/api/v2/torrents/info',params={'hashes':job['download_id']},timeout=30);r.raise_for_status();info=r.json()
        media_kind=job['media_kind'] if 'media_kind' in job.keys() else 'Audio'
        identity={'id':job['id'],'key':job['recording_key'],'mediaKind':media_kind}
        if not info:results.append(dict(identity,status='job_missing'));continue
        info=info[0]
        if info.get('category')!='asmarr':results.append(dict(identity,status='category_mismatch'));continue
        if info.get('progress',0)<1 or info.get('state') in {'checkingDL','checkingUP','moving','checkingResumeData'}:results.append(dict(identity,status=info.get('state'),progress=info.get('progress')));continue
        try:
            path=Path(info.get('content_path') or info.get('save_path',''))
            extensions=video_media.VIDEO_EXTENSIONS if media_kind=='Video' else ALLOWED
            candidates=[path] if path.is_file() and path.suffix.lower() in extensions else [p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in extensions]
            if len(candidates)!=1:raise ValueError('completed_torrent_media_ambiguous')
            if media_kind=='Video':
                cached=core.meta_get(db,'search_results:Video:'+job['recording_key'],[])
                detail=json.loads(job['details'] or '{}')
                selected=next((c for c in cached if c.get('guid')==detail.get('guid')),{'provider':'prowlarr','provider_id':job['download_id']})
                result=video_media.import_video(db,cfg,job['recording_key'],str(candidates[0]),selected,import_derived_audio)
            else:result=import_audio(db,cfg,job['recording_key'],str(candidates[0]))
            with db:db.execute("UPDATE queue SET state='imported' WHERE id=?",(job['id'],))
            # Destination is durable and validated before any torrent removal.
            if opts.get('retention','keep')=='remove-torrent':
                response=s.post(base+'/api/v2/torrents/delete',data={'hashes':job['download_id'],'deleteFiles':'false'},timeout=30);response.raise_for_status()
                with db:db.execute("UPDATE queue SET state='removed' WHERE id=?",(job['id'],))
            results.append(dict(result,**identity))
        except video_media.VideoUnavailable as e:
            reason=str(e)
            with db:
                db.execute("UPDATE queue SET state='failed',details=? WHERE id=?",(json.dumps({'error':reason}),job['id']))
                if media_kind=='Video' and has_table(db,'media_assets'):
                    db.execute("""UPDATE media_assets SET state='unavailable',wanted=1,
                                  retry_after=0,error=? WHERE recording_key=? AND media_kind='Video'""",
                               (reason,job['recording_key']))
            results.append(dict(identity,status='unavailable',reason=reason))
        except (ValueError,OSError) as e:
            reason=str(e);retry=int(time.time())+300
            with db:
                db.execute("UPDATE queue SET state='failed',details=? WHERE id=?",(json.dumps({'error':reason}),job['id']))
                if media_kind=='Video' and has_table(db,'media_assets'):
                    db.execute("""UPDATE media_assets SET state='failed',wanted=1,
                                  attempts=attempts+1,retry_after=?,error=?
                                  WHERE recording_key=? AND media_kind='Video'""",
                               (retry,reason,job['recording_key']))
            results.append(dict(identity,status='failed',reason=reason))
    return {'status':'ok','jobs':results}


def _fair_creator_batch(db, rows, limit, cursor_key):
    """Select work round-robin by creator while preserving each creator's row order."""
    if limit <= 0 or not rows:
        return []
    groups={}
    for row in rows:
        groups.setdefault(row['creator'],[]).append(row)
    creators=sorted(groups,key=str.casefold)
    last=core.meta_get(db,cursor_key)
    if last:
        after=[name for name in creators if name.casefold()>str(last).casefold()]
        before=[name for name in creators if name.casefold()<=str(last).casefold()]
        creators=after+before
    positions={name:0 for name in creators}
    selected=[]
    while len(selected)<limit:
        added=False
        for creator in creators:
            position=positions[creator]
            if position>=len(groups[creator]):
                continue
            selected.append(groups[creator][position])
            positions[creator]=position+1
            added=True
            if len(selected)>=limit:
                break
        if not added:
            break
    if selected:
        core.meta_set(db,cursor_key,selected[-1]['creator'])
    return selected


def process_queue(db,cfg):
    production(db);saved=[];failed=[];fallback=[]
    now=int(time.time())
    rows=db.execute("SELECT a.* FROM assets a JOIN creators c ON c.name=a.creator WHERE c.monitored=1 AND a.state IN ('pending','failed') AND a.retry_after<=? AND NOT EXISTS(SELECT 1 FROM queue q WHERE q.recording_key=a.key AND q.media_kind='Audio' AND q.state NOT IN ('failed','removed')) AND NOT EXISTS(SELECT 1 FROM blocklist b WHERE b.recording_key=a.key AND b.download_id IS NULL) ORDER BY CASE a.state WHEN 'pending' THEN 0 ELSE 1 END, COALESCE(a.published,0) DESC",(now,)).fetchall()
    rows=_fair_creator_batch(db,rows,cfg.get('max_downloads_per_run',25),'audio_queue_last_creator')
    for row in rows:
        rules=profile(db,row)
        if not json.loads(row['targets'] or '[]') or row['attempts']>=rules.get('directRetries',3):
            if rules.get('fallback',True):
                try:
                    found=search(db,row['key'],'Audio')
                    candidate=next((c for c in found['candidates'] if c['autoGrab']),None)
                    if candidate:fallback.append(grab(db,row['key'],candidate))
                    else:
                        review_after=now+86400
                        with db:db.execute("UPDATE assets SET retry_after=? WHERE key=?",(review_after,row['key']))
                        event(db,'search-review',row['key'],{'candidates':len(found['candidates']),'retryAfter':review_after})
                except requests.RequestException as exc:
                    # Indexer/client outages must not starve unrelated direct
                    # acquisitions. Keep exhausted direct attempts and any
                    # durable submitting job; never log credential-bearing URLs.
                    reason='fallback_'+type(exc).__name__
                    retry_after=now+3600
                    with db:db.execute("UPDATE assets SET state='failed',retry_after=?,error=? WHERE key=?",(retry_after,reason,row['key']))
                    event(db,'fallback-failure',row['key'],{'error':reason,'retryAfter':retry_after})
                    failed.append({'key':row['key'],'error':reason})
            continue
        try:saved.append(acquire(db,cfg,row['key']))
        except Exception as e:
            attempts=row['attempts']+1;reason=str(e) if isinstance(e,(core.SourceError,core.MediaError,ValueError)) else type(e).__name__
            with db:db.execute("UPDATE assets SET state='failed',attempts=?,retry_after=?,error=? WHERE key=?",(attempts,now+min(86400,300*2**min(attempts,8)),reason,row['key']))
            event(db,'direct-failure',row['key'],{'attempt':attempts,'error':reason});failed.append({'key':row['key'],'error':reason})
    return {'saved':saved,'failed':failed,'fallback':fallback,'downloads':monitor_downloads(db,cfg)}


def dispatch(operation,args):
    if operation=='backup':
        return backup_snapshot.create(STATE, CONFIG)
    with contextlib.closing(connect()) as db:
        if operation=='creator-removal-preview':
            # Preview needs library roots only, not source/API credentials.
            return creator_removal.preview(db,{'output_root':setting(db,'root'), 'video_root':setting(db,'video.root')},args['creatorId'])
        if operation in {'source-configuration', 'set-source-configuration'}:
            try:
                if operation == 'set-source-configuration':
                    return source_configuration.save(db, args['kind'], args['configuration'])
                path = CONFIG / 'sources.yaml'
                return source_configuration.snapshot(db, yaml.safe_load(path.read_text()) or {} if path.exists() else {}, args['kind'])
            except ValueError as error:
                return {'error': str(error)}
        cfg,secrets=configuration(db)
        if operation=='creator-remove':
            return creator_removal.remove(db,cfg,args,qbit,lambda paths:creator_removal.cleanup_plex(cfg,secrets,paths))
        if operation in {'acquire','search','grab','manual-import'} and not db.execute('SELECT 1 FROM assets a JOIN creators c ON c.name=a.creator WHERE a.key=?',(args['key'],)).fetchone():
            raise ValueError('Recording belongs to a removed or missing creator.')
        if operation=='migrate':
            if creator_removal.exclusions(db):raise ValueError('Migration would overwrite creator-removal history; review the removal exclusions first.')
            return migrate(db,cfg)
        if operation=='scan':
            manifest=media_manifest(db); original=core.meta_get(db,'migration_media_manifest',manifest)
            return {'files':len(manifest['files']),'missing':manifest['missing'],'outside':manifest['outside'],'unchanged':all(manifest['files'].get(k)==v for k,v in original['files'].items()),
                    'video':video_media.scan_library(db,cfg)}
        if operation=='discover': return discover(db,cfg,secrets,args.get('kind','all'),args.get('shadow',True))
        if operation=='source-preview':return source_preview(db,cfg,args)
        if operation=='eligibility-repair-preview':return eligibility_repair_preview(db,args)
        if operation=='eligibility-repair-apply':
            production(db);return eligibility_repair_apply(db,args)
        if operation=='audio-backfill':
            production(db);return audio_backfill(db,args)
        if operation=='source-suggest':return source_suggestions(db,args)
        if operation=='metadata':
            row=db.execute('SELECT * FROM assets WHERE key=?',(args['key'],)).fetchone()
            return dict(row) if row else {'status':'not_found'}
        if operation=='acquire': return acquire(db,cfg,args['key'])
        if operation=='process-queue':return process_queue(db,cfg)
        if operation=='video-backfill':return video_media.backfill(db,cfg,secrets,args,core)
        if operation=='video-process-queue':return video_media.process_queue(db,cfg,import_derived_audio)
        if operation=='search': return search(db,args['key'],args.get('mediaKind','Audio'))
        if operation=='grab':return grab(db,args['key'],args['candidate'],args.get('mediaKind','Audio'))
        if operation=='downloads':return monitor_downloads(db,cfg)
        if operation=='manual-import':
            if args.get('mediaKind','Audio')=='Video':
                return video_media.import_video(db,cfg,args['key'],args['path'],args.get('candidate',{'provider':'manual'}),import_derived_audio)
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
            elif kind in {'whyp','audiochan','hotaudio'}:
                handles=configured_discovery(db,cfg).get(kind+'_creators',[])
                if not handles:return {'status':'empty','kind':kind}
                core.creator_audio_sources.profile_listing(core.HTTP(),kind,handles[0])
            elif kind in public_audio_sources.KINDS:
                identity=db.execute('''SELECT i.handle,i.creator_id FROM identities i JOIN creators c ON c.id=i.creator_id
                                       WHERE i.kind=? AND i.enabled=1 AND c.monitored=1 ORDER BY i.id LIMIT 1''',(kind,)).fetchone()
                if not identity:return {'status':'empty','kind':kind,'stage':'discovery'}
                result=source_preview(db,cfg,{'creatorId':identity['creator_id'],'kind':kind,'identity':identity['handle'],'limit':1})
                mode=args.get('mode','discovery')
                if mode=='download':
                    return verify_public_download(db,cfg,kind,identity['handle'],args.get('providerId'))
                return {'status':'healthy','kind':kind,'stage':'discovery','items':result['discovered']}
            else: raise ValueError('unknown_source')
            return {'status':'healthy','kind':kind,'stage':'discovery'}
        if operation=='playlists':
            preview=args.get('preview',True)
            if not preview: production(db)
            return plex_playlists.sync(db,cfg,secrets,core,dry_run=preview)
        if operation=='plex':
            production(db);status=core.plex_refresh(db,cfg,secrets,core.HTTP())
            if status=='indexed':
                with db:db.execute("UPDATE backfill_jobs SET plex_indexed=imported,updated=? WHERE media_kind='Audio'",(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),))
            return {'status':status}
        if operation=='plex-video': production(db); return video_media.plex_refresh(db,cfg,secrets)
        if operation=='plex-video-validate-binding':
            # Validate the proposed section against the effective configuration
            # before the host persists it. This prevents a Movies, TV, Music,
            # unreachable, inaccessible or overlapping library from ever
            # becoming the active video binding.
            proposed=dict(cfg)
            plex=dict(cfg.get('plex', {}));video=dict(plex.get('video') or {})
            video['section_id']=int(args['sectionId']);plex['video']=video;proposed['plex']=plex
            try:
                return dict(video_media.plex_library_validation(proposed,secrets), valid=True)
            except (ValueError, requests.RequestException, ET.ParseError) as error:
                reason=(str(error) if isinstance(error,ValueError)
                        else 'plex_video_library_unreachable')
                return {'valid':False,'error':reason}
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
