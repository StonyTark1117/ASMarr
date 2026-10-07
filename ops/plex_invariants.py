"""Read-only Plex snapshots proving preservation across a bounded rollout.

Playback activity or manual metadata edits also produce a mismatch requiring
review; they are never silently attributed to ASMarr or accepted as preservation.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

ACCEPTANCE=Path('/var/lib/asmarr/acceptance')
PLAYBACK=('viewCount','lastViewedAt','viewOffset','userRating')
LOCK_ATTRIBUTES={'artist':'grandparentTitle','albumartist':'grandparentTitle',
                 'album':'parentTitle','sorttitle':'titleSort'}


def capture(cfg,secrets,registry,client=None):
    import plex_playlists as plex
    from categories import CATEGORIES
    client=client or plex.Client(cfg,secrets)
    section=str(cfg['plex']['section_id'])
    catalog=plex.all_items(client,'/library/sections/'+section+'/all','Track',type=10)
    tracks=plex.details(client,[t.get('ratingKey') for t in catalog],section)
    managed={'ASMR: '+name for name,_ in CATEGORIES.values()}
    result={'capturedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'section':section,'tracks':{},'playlists':{}}
    for track in tracks:
        tags={}
        for child in track:
            if child.get('tag') is not None and not (child.tag=='Mood' and child.get('tag') in managed):
                tags.setdefault(child.tag,[]).append(child.get('tag'))
        tags={key:sorted(values) for key,values in tags.items()}
        locks={f.get('name'):f.get('locked','0') for f in track.findall('Field')}
        locked_values={name:{'value':track.get(LOCK_ATTRIBUTES.get(name.lower(),name)),'tags':tags.get(name.title(),[])}
                       for name,locked in locks.items() if locked=='1' and name!='mood'}
        result['tracks'][track.get('ratingKey')]={
            'paths':sorted(p.get('file') for p in track.findall('.//Part') if p.get('file')),
            'playback':{key:track.get(key) for key in PLAYBACK},'locks':locks,
            'lockedValues':locked_values,'unrelatedTags':tags}
    managed_ids={r.get('id') for r in registry.values()}
    for playlist in plex.all_items(client,'/playlists','Playlist'):
        key=playlist.get('ratingKey')
        fields=('guid',) if key in managed_ids else ('guid','title','summary','smart','content')
        result['playlists'][key]={name:playlist.get(name) for name in fields}
    return result


def compare(before,after):
    changes=[]
    if before['section']!=after['section']:changes.append({'kind':'section_changed'})
    for key,original in before['tracks'].items():
        current=after['tracks'].get(key)
        if current is None:
            changes.append({'kind':'rating_key_missing','ratingKey':key});continue
        for field in ('paths','playback','lockedValues','unrelatedTags'):
            if original[field]!=current[field]:changes.append({'kind':field+'_changed','ratingKey':key})
        names=set(original['locks'])|set(current['locks'])
        if any(original['locks'].get(name,'0')!=current['locks'].get(name,'0') for name in names):
            changes.append({'kind':'locks_changed','ratingKey':key})
    for key,original in before['playlists'].items():
        current=after['playlists'].get(key)
        if current is None or any(current.get(name)!=value for name,value in original.items()):
            changes.append({'kind':'playlist_identity_or_metadata_changed','ratingKey':key})
    return {'passed':not changes,'baselineAt':before['capturedAt'],'checkedAt':after['capturedAt'],
            'existingTracksChecked':len(before['tracks']),'existingPlaylistsChecked':len(before['playlists']),
            'changes':changes,'newTracks':len(set(after['tracks'])-set(before['tracks']))}


def runtime_snapshot():
    sys.path.insert(0,'/opt/asmarr/providers')
    import bridge
    with sqlite3.connect(bridge.DB.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        cfg,secrets=bridge.configuration(db)
        registry=bridge.core.meta_get(db,'managed_playlists',{})
    return capture(cfg,secrets,registry)


def write_protected(path,value):
    if not path.resolve().is_relative_to(ACCEPTANCE.resolve()):raise ValueError('evidence_path_outside_acceptance_root')
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with path.open('x') as output:
        path.chmod(0o600);json.dump(value,output,indent=2)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['capture','compare'])
    parser.add_argument('--baseline',type=Path,default=ACCEPTANCE/'plex-before-cutover.json')
    parser.add_argument('--output',type=Path,default=ACCEPTANCE/'plex-preservation.json')
    args=parser.parse_args()
    if args.operation=='capture':
        snapshot=runtime_snapshot();write_protected(args.baseline,snapshot)
        print(json.dumps({'evidence':str(args.baseline),'tracks':len(snapshot['tracks']),'playlists':len(snapshot['playlists'])}))
    else:
        report=compare(json.loads(args.baseline.read_text()),runtime_snapshot())
        write_protected(args.output,report)
        print(json.dumps({'evidence':str(args.output),'passed':report['passed'],'changes':len(report['changes'])}))
        if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
