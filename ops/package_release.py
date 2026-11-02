"""Build a checksummed, source-bound Linux x64 release; never include live data."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET


def git(repository,*args):
    return subprocess.check_output(['git','-C',str(repository),*args])


def check_public_path(path):
    name=path.name.lower()
    if (name in {'auth.json','integrations.json','source-secrets.json','initial-admin.txt',
                 'sshforcodex','sshpass','backup-policy.json','backup-recipient.pem'}
            or name.startswith('.env') or name.endswith(('.db','.db-wal','.db-shm','.cms','.key','.pem'))
            or any(part in {'.git','backups','acceptance'} for part in path.parts)):
        raise ValueError('Runtime state or credentials must never enter a public release')


def build(repository,publish,toolchain,output,commit,version):
    repository,publish,toolchain,output=map(Path,(repository,publish,toolchain,output))
    if not re.fullmatch('[a-f0-9]{40}',commit) or not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Full commit and semantic version required')
    if git(repository,'rev-parse','HEAD').decode().strip()!=commit:raise ValueError('Checkout does not match release commit')
    for args in (('diff','--quiet'),('diff','--cached','--quiet')):
        subprocess.run(['git','-C',str(repository),*args],check=True)
    identity=json.loads((publish/'build-identity.json').read_text())
    if identity!={'sourceCommit':commit,'version':version}:raise ValueError('Publish identity does not match exact release source/version')
    epoch=int(git(repository,'show','-s','--format=%ct',commit))
    files={}
    def add(name,data,executable=False):
        if name in files:raise ValueError('Package name collision')
        files[name]=(data,0o755 if executable else 0o644)
    for path in sorted(publish.rglob('*')):
        relative=path.relative_to(publish)
        if '__pycache__' in relative.parts or path.suffix=='.pyc':continue
        check_public_path(relative)
        if path.is_symlink():raise ValueError('Published tree must not contain symlinks')
        if path.is_file():add('application/'+relative.as_posix(),path.read_bytes(),bool(path.stat().st_mode&0o111))
    names=git(repository,'ls-tree','-r','--name-only',commit,'--','ops','docs','packaging','README.md').decode().splitlines()
    for name in names:
        add('application/'+name,git(repository,'show',commit+':'+name))
    # Carry notices for the actual resolved NuGet runtime dependencies and the
    # frontend's installed production dependency graph, not developer caches.
    nuget=Path(__import__('os').environ.get('NUGET_PACKAGES',str(Path.home()/'.nuget/packages')))
    dependencies=[]
    deps=json.loads((publish/'ASMarr.deps.json').read_text())
    for identity,item in sorted(deps.get('libraries',{}).items()):
        if item.get('type') not in ('package','runtimepack'):continue
        name,version=identity.rsplit('/',1);name=name.removeprefix('runtimepack.');folder=nuget/name.lower()/version
        record={'package':name,'version':version}
        nuspec=next(iter(folder.glob('*.nuspec')),None)
        if nuspec:
            tree=ET.parse(nuspec)
            for field in ('license','copyright','authors','projectUrl'):
                node=next((n for n in tree.iter() if n.tag.rsplit('}',1)[-1]==field),None)
                if node is not None:record[field]=node.text
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            if path.is_file() and re.search(r'license|notice|copying',path.name,re.I) and path.stat().st_size<2*1024**2:
                add('application/notices/nuget/'+name+'/'+version+'/'+path.name,path.read_bytes())
        dependencies.append(record)
    frontend=repository/'frontend/node_modules';visited=set()
    def node_notices(name):
        if name in visited:return
        visited.add(name);folder=frontend/name
        package=json.loads((folder/'package.json').read_text())
        dependencies.append({'package':'npm:'+name,'version':package['version'],'license':package.get('license')})
        for path in sorted(folder.iterdir()):
            if path.is_file() and re.match(r'license|notice|copying',path.name,re.I) and path.stat().st_size<2*1024**2:
                add('application/notices/npm/'+name+'/'+path.name,path.read_bytes())
        for child in package.get('dependencies',{}):node_notices(child)
    package=json.loads(git(repository,'show',commit+':frontend/package.json'))
    for name in package.get('dependencies',{}):node_notices(name)
    add('application/notices/dependencies.json',json.dumps(dependencies,sort_keys=True,indent=2).encode())
    pins=json.loads(git(repository,'show',commit+':packaging/toolchain.json'))
    for path in sorted(toolchain.iterdir()):
        if path.name not in ('yt-dlp','deno','yt-dlp-LICENSE.txt','deno-LICENSE.txt') or path.is_symlink() or not path.is_file():raise ValueError('Unexpected toolchain file')
        data=path.read_bytes()
        if path.name in ('yt-dlp','deno') and hashlib.sha256(data).hexdigest()!=pins[path.name]['binarySha256']:raise ValueError('Toolchain binary does not match pinned release')
        add('application/toolchain/'+path.name,data,path.name in ('yt-dlp','deno'))
    if not all('application/toolchain/'+name in files for name in ('yt-dlp','deno','yt-dlp-LICENSE.txt','deno-LICENSE.txt')):raise ValueError('Toolchain incomplete')
    fingerprint=hashlib.sha256()
    for name,(data,_) in sorted(files.items()):
        fingerprint.update(name.removeprefix('application/').encode()+b'\0'+hashlib.sha256(data).hexdigest().encode()+b'\n')
    manifest={'format':1,'version':version,'sourceCommit':commit,'platform':'debian12-linux-x64',
              'artifactSha256':fingerprint.hexdigest(),
              'files':{name:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'mode':mode} for name,(data,mode) in sorted(files.items())}}
    add('package-manifest.json',json.dumps(manifest,sort_keys=True,indent=2).encode())
    output.mkdir(parents=True,exist_ok=True)
    target=output/('asmarr-'+version+'-linux-x64.tar.gz')
    with target.open('xb') as raw,gzip.GzipFile(fileobj=raw,mode='wb',mtime=epoch,filename='') as zipped,tarfile.open(fileobj=zipped,mode='w') as archive:
        for name,(data,mode) in sorted(files.items()):
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=mode;info.mtime=epoch
            archive.addfile(info,io.BytesIO(data))
    checksum=hashlib.sha256(target.read_bytes()).hexdigest()
    sums=output/'SHA256SUMS'
    with sums.open('x') as stream:stream.write(checksum+'  '+target.name+'\n')
    with (output/'release.json').open('x') as stream:json.dump({k:v for k,v in manifest.items() if k!='files'},stream,indent=2)
    return {'package':str(target),'sha256':checksum,'artifactSha256':manifest['artifactSha256'],'sourceCommit':commit}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--publish',type=Path,required=True);parser.add_argument('--toolchain',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--commit',required=True);parser.add_argument('--version',required=True)
    args=parser.parse_args();print(json.dumps(build(args.repository,args.publish,args.toolchain,args.output,args.commit,args.version),indent=2))
