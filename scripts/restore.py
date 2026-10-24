#!/usr/bin/env python3
"""Restore a verified backup into an empty, separate Compose project."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from uuid import uuid4
from bootstrap import Compose,ROOT
from backup import sha256_file

def validate_backup(directory):
    manifest=json.loads((directory/'manifest.json').read_text())
    if manifest.get('format')!=1 or manifest.get('postgres_major')!=17 or manifest.get('redis_major')!=8:raise ValueError('Unsupported backup format')
    expected={'postgres.dump','scope.tar.gz','redis.tar.gz'}
    if set(manifest.get('files',{}))!=expected:raise ValueError('Unexpected backup files')
    for name in expected:
        file=directory/name;metadata=manifest['files'][name]
        if file.is_symlink() or not file.is_file() or file.stat().st_size!=metadata['size'] or sha256_file(file)!=metadata['sha256']:raise ValueError('Backup integrity check failed: '+name)
    for name in ['scope.tar.gz','redis.tar.gz']:
        with tarfile.open(directory/name,'r:gz') as archive:
            for member in archive:
                path=Path(member.name)
                if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):raise ValueError('Unsafe archive member')
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('backup',type=Path);parser.add_argument('--project-name',required=True)
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env');parser.add_argument('--api-port',type=int,default=28211);parser.add_argument('--web-port',type=int,default=28281);args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,50}',args.project_name):raise ValueError('Invalid project name')
    if not all(1<=p<=65535 for p in [args.api_port,args.web_port]) or args.api_port==args.web_port:raise ValueError('Invalid ports')
    directory=args.backup.resolve();validate_backup(directory)
    from dotenv import dotenv_values
    values=dotenv_values(args.env_file);original=values.get('COMPOSE_PROJECT_NAME','internet-search')
    if args.project_name==original:raise ValueError('Restore requires a separate project')
    for resource in ['container','volume','network']:
        command=['docker',resource,'ls',*(['--all'] if resource=='container' else []),'--filter',f'label=com.docker.compose.project={args.project_name}','--format','{{.ID}}' if resource=='container' else '{{.Name}}']
        if subprocess.run(command,check=True,capture_output=True,text=True).stdout.strip():raise ValueError('Target project is not empty')
    admin='restore_'+uuid4().hex[:12];services={name:{'image':f'{original}-{name}'} for name in ['api','web','task-runner','metrics-agent','metrics-proxy']}
    for service in services.values():subprocess.run(['docker','image','inspect',service['image']],check=True,stdout=subprocess.DEVNULL)
    services['postgres']={'environment':{'POSTGRES_USER':admin,'POSTGRES_DB':'postgres'},'healthcheck':{'test':['CMD','pg_isready','-U',admin,'-d','postgres']}}
    override=directory/(args.project_name+'.restore.json')
    with override.open('x') as stream:json.dump({'services':services},stream,indent=2)
    override.chmod(0o600);compose=Compose(args.env_file,args.project_name);compose.command+=['-f',str(ROOT/'docker-compose.yml'),'-f',str(override)]
    compose.environment.update(API_PORT=str(args.api_port),WEB_PORT=str(args.web_port));compose.run('config','--quiet');compose.run('up','-d','--wait','--wait-timeout','120','postgres')
    with (directory/'postgres.dump').open('rb') as source:compose.run('exec','-T','postgres','psql','-q','-v','ON_ERROR_STOP=1','-U',admin,'-d','postgres',stdin=source,stdout=subprocess.DEVNULL)
    sql="\\getenv restore_password POSTGRES_PASSWORD\nSELECT format('ALTER ROLE %I PASSWORD %L', :'app_role', :'restore_password') \\gexec\n"
    compose.run('exec','-T','postgres','psql','-q','-v','ON_ERROR_STOP=1','-v','app_role='+values.get('POSTGRES_USER','app'),'-U',admin,'-d','postgres',input=sql,text=True,stdout=subprocess.DEVNULL)
    with (directory/'scope.tar.gz').open('rb') as source:compose.run('run','--rm','--no-deps','-T','--entrypoint','python','api','-c',"import sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz').extractall('/data/scope',filter='data')",stdin=source)
    with (directory/'redis.tar.gz').open('rb') as source:compose.run('run','--rm','--no-deps','-T','--entrypoint','tar','redis','-C','/data','-xzf','-',stdin=source)
    compose.run('exec','-T','postgres','psql','-q','-v','ON_ERROR_STOP=1','-U',values.get('POSTGRES_USER','app'),'-d','postgres','-c',f'ALTER ROLE "{admin}" NOLOGIN')
    compose.run('up','-d','--wait','--wait-timeout','120')
    compose.run('exec','-T','api','python','-c',"import urllib.request; [urllib.request.urlopen('http://127.0.0.1:8000'+p,timeout=10).read() for p in ['/health/postgres','/health/redis','/v2/tasks']];print('Restore checks OK')")
    (directory/(args.project_name+'.start.txt')).write_text(f'API_PORT={args.api_port} WEB_PORT={args.web_port} docker compose --env-file {args.env_file.resolve()} --project-name {args.project_name} -f docker-compose.yml -f {override} up -d --wait\n')
    print(f'Restored into {args.project_name}; API {args.api_port}, web {args.web_port}')

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:print('Restore stopped: '+str(error),file=sys.stderr);raise SystemExit(1)
