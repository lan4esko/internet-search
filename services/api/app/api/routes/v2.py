"""V2 TASK API: scope, snapshots, evidence and server-sent events."""
import asyncio
import csv
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4
import yaml
from fastapi import APIRouter,File,Form,HTTPException,Query,Request,UploadFile
from fastapi.responses import FileResponse,StreamingResponse
from pydantic import BaseModel,ConfigDict,Field
from scanlib.v2 import database as store
from scanlib.v2.scope import compile_scope
from scanlib.v2.config import ScanConfig
from scanlib.v2.rules import load_rule
from scanlib.v2.transport import egress_pool,select_egress
from app.task_db import create_database,drop_database

router=APIRouter(prefix='/v2',tags=['v2'])
ROOT=Path(os.getenv('SCOPE_DATA_DIR','/data/scope'))/'v2'
TEMPLATES=Path(os.getenv('V2_TEMPLATE_DIR','/app/templates/v2'))
TERMINAL={'completed','completed_with_errors','failed','cancelled'}
class ScopeValidation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    cidr_text: str=Field(min_length=1,max_length=10485760)
    deny_cidrs: str=Field(default='',max_length=65536)
def decode(value):return json.loads(value) if isinstance(value,str) else value
def serialize(row):
    result=dict(row)
    for key in ['config','manifest','templates','definition','observation','evidence','payload','argv','metadata']:
        if key in result:result[key]=decode(result[key])
    return result
async def get_task(task_id):
    db=await store.control()
    try:row=await db.fetchrow('SELECT * FROM v2_tasks WHERE id=$1',task_id)
    finally:await db.close()
    if not row:raise HTTPException(404,'TASK not found')
    return row
async def available_templates():
    values={}
    for file in sorted(TEMPLATES.glob('*.yaml')):
        rule=load_rule(file.read_text());values[rule['id']]=rule
    db=await store.control()
    try:
        for row in await db.fetch('SELECT definition FROM v2_templates'):
            rule=decode(row['definition']);values[rule['id']]=rule
    finally:await db.close()
    return values

@router.get('/templates')
async def templates():return {'items':list((await available_templates()).values())}
@router.put('/templates/{template_id}')
async def save_template(template_id: str,request: Request):
    raw=await request.body()
    if len(raw)>262144:raise HTTPException(413,'Template too large')
    try:
        rule=load_rule(raw.decode())
        if rule['id']!=template_id:raise ValueError('Template ID differs from URL')
    except (ValueError,TypeError,KeyError,RecursionError,yaml.YAMLError) as error:raise HTTPException(422,str(error)) from error
    definition=json.dumps(rule,sort_keys=True);sha=hashlib.sha256(definition.encode()).hexdigest();db=await store.control()
    try:await db.execute('INSERT INTO v2_templates(id,definition,sha256) VALUES($1,$2::jsonb,$3) ON CONFLICT(id) DO UPDATE SET definition=EXCLUDED.definition,sha256=EXCLUDED.sha256,updated_at=now()',template_id,definition,sha)
    finally:await db.close()
    return {'id':template_id,'sha256':sha}
@router.get('/egress')
async def egress():return {'items':[{'name':name,'modes':[v['mode'] for v in values],'members':len(values)} for name,values in egress_pool().items()]}
@router.post('/scope/validate')
async def validate_scope(value: ScopeValidation):
    try:return compile_scope(value.cidr_text.encode(),value.deny_cidrs,os.getenv('V2_LAB_CIDRS','')).manifest()
    except ValueError as error:raise HTTPException(422,str(error)) from error

@router.post('/tasks',status_code=202)
async def create_task(name: str=Form(...,min_length=1,max_length=255),config: str=Form('{}'),file: UploadFile=File(...)):
    source=await file.read(10485761)
    try:
        if not name.strip() or '\x00' in name:raise ValueError('Invalid task name')
        cfg=ScanConfig.model_validate_json(config)
        scope=compile_scope(source,cfg.deny_cidrs,os.getenv('V2_LAB_CIDRS',''))
        if cfg.shards>scope.target_count:raise ValueError('Shards cannot exceed authorized IP count')
        select_egress(cfg.egress,'validate');all_templates=await available_templates();selected=[all_templates[key] for key in cfg.template_ids]
    except (ValueError,KeyError,TypeError) as error:raise HTTPException(422,str(error)) from error
    database='task_'+uuid4().hex;control=await store.control();task_id=None;directory=None
    try:
        task_id=await control.fetchval('''INSERT INTO v2_tasks(name,database_name,config,manifest,templates,source_filename,source_sha256)
         VALUES($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6,$7) RETURNING id''',name.strip(),database,cfg.model_dump_json(),json.dumps(scope.manifest()),json.dumps(selected),Path(file.filename or 'scope.txt').name,scope.source_sha256)
        directory=ROOT/str(task_id);directory.mkdir(parents=True,mode=0o700)
        with (directory/'source.txt').open('xb') as output:output.write(source);output.flush();os.fsync(output.fileno())
        (directory/'source.txt').chmod(0o400);manifest=scope.save(directory);await create_database(database);db=await store.task(database)
        try:
            async with db.transaction():
                await db.execute(store.SCHEMA)
                await db.execute('INSERT INTO scope_manifest(manifest) VALUES($1::jsonb)',json.dumps(manifest))
                for port in cfg.ports:
                    for shard in range(cfg.shards):
                        planned=scope.target_count//cfg.shards+int(shard<scope.target_count%cfg.shards)
                        await db.execute('INSERT INTO coverage(port,shard,planned) VALUES($1,$2,$3)',port,shard,planned)
                await store.event(db,'task_created',{'manifest_sha256':manifest['manifest_sha256']})
        finally:await db.close()
        await control.execute("UPDATE v2_tasks SET status='queued',manifest=$2::jsonb WHERE id=$1",task_id,json.dumps(manifest))
    except BaseException:
        if task_id:
            try:
                await drop_database(database);await control.execute('DELETE FROM v2_tasks WHERE id=$1',task_id)
                if directory:shutil.rmtree(directory)
            except Exception:await control.execute("UPDATE v2_tasks SET status='failed',error='Provisioning interrupted; inspect database' WHERE id=$1",task_id)
        raise
    finally:await control.close()
    return serialize(await get_task(task_id))

@router.get('/tasks')
async def list_tasks(limit: int=Query(100,ge=1,le=500),offset: int=Query(0,ge=0)):
    db=await store.control()
    try:
        rows=await db.fetch('SELECT * FROM v2_tasks ORDER BY id DESC LIMIT $1 OFFSET $2',limit,offset);total=await db.fetchval('SELECT count(*) FROM v2_tasks')
    finally:await db.close()
    return {'items':[serialize(r) for r in rows],'total':total}
@router.get('/tasks/{task_id}')
async def detail(task_id: int):
    row=await get_task(task_id);db=await store.task(row['database_name'])
    try:
        stats=await store.stats(db);coverage=[serialize(r) for r in await db.fetch('SELECT * FROM coverage ORDER BY port,shard')]
        events=[serialize(r) for r in await db.fetch('SELECT * FROM events ORDER BY id DESC LIMIT 50')]
    finally:await db.close()
    return {**serialize(row),'stats':stats,'coverage':coverage,'events':events}
@router.get('/tasks/{task_id}/events')
async def events(task_id: int,request: Request):
    await get_task(task_id)
    async def stream():
        while not await request.is_disconnected():
            yield 'event: task\ndata: '+json.dumps(await detail(task_id),default=str)+'\n\n';await asyncio.sleep(2)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
@router.post('/tasks/{task_id}/cancel',status_code=202)
async def cancel(task_id: int):
    row=await get_task(task_id)
    if row['status'] in TERMINAL:raise HTTPException(409,'TASK already terminal')
    db=await store.control()
    try:await db.execute("UPDATE v2_tasks SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,finished_at=CASE WHEN status='queued' THEN now() ELSE NULL END WHERE id=$1 AND status NOT IN ('completed','completed_with_errors','failed','cancelled')",task_id)
    finally:await db.close()
    return {'status':'cancelling'}
@router.post('/tasks/{task_id}/retry',status_code=202)
async def retry(task_id: int):
    row=await get_task(task_id)
    if row['status'] not in {'failed','completed_with_errors','cancelled'}:raise HTTPException(409,'TASK must be failed or cancelled')
    db=await store.task(row['database_name'])
    try:
        if not await db.fetchval('SELECT pg_try_advisory_lock($1::bigint)',task_id):raise HTTPException(409,'Worker still stopping')
        async with db.transaction():
            await db.execute("UPDATE coverage SET status='pending',error=NULL WHERE status!='done'")
            await db.execute("UPDATE endpoints SET state='pending',attempts=0 WHERE state!='done'")
            await db.execute("UPDATE outbox SET published=FALSE WHERE endpoint_id IN (SELECT id FROM endpoints WHERE state='pending')")
    finally:await db.close()
    control=await store.control()
    try:await control.execute("UPDATE v2_tasks SET status='queued',error=NULL,finished_at=NULL WHERE id=$1",task_id)
    finally:await control.close()
    return {'status':'queued'}
@router.delete('/tasks/{task_id}',status_code=204)
async def delete(task_id: int,request: Request):
    row=await get_task(task_id)
    if row['status'] not in TERMINAL:raise HTTPException(409,'Cancel and wait before deleting')
    db=await store.task(row['database_name'])
    try:
        if not await db.fetchval('SELECT pg_try_advisory_lock($1::bigint)',task_id):raise HTTPException(409,'Worker still stopping')
    finally:await db.close()
    await drop_database(row['database_name']);control=await store.control()
    try:await control.execute('DELETE FROM v2_tasks WHERE id=$1',task_id)
    finally:await control.close()
    await request.app.state.redis.delete(f'v2:{task_id}:endpoints',f'v2:{task_id}:dlq',f'v2:{task_id}:stats');shutil.rmtree(ROOT/str(task_id),ignore_errors=True)
@router.get('/tasks/{task_id}/observations')
async def observations(task_id: int,limit: int=Query(100,ge=1,le=1000),offset: int=Query(0,ge=0),matches: bool=False,module: str|None=Query(None,pattern='^(tls|http)$')):
    row=await get_task(task_id);db=await store.task(row['database_name'])
    predicate=" WHERE ($1::text IS NULL OR o.module=$1) AND (NOT $2::boolean OR EXISTS(SELECT 1 FROM jsonb_array_elements(o.evidence) e WHERE e->>'tier'='http_confirmed'))"
    try:
        total=await db.fetchval('SELECT count(*) FROM observations o'+predicate,module,matches)
        rows=await db.fetch('SELECT o.*,host(e.ip) ip,e.port,e.sni FROM observations o JOIN endpoints e ON e.id=o.endpoint_id'+predicate+' ORDER BY o.id LIMIT $3 OFFSET $4',module,matches,limit,offset)
    finally:await db.close()
    return {'items':[{k:v for k,v in serialize(r).items() if k!='raw_path'} for r in rows],'total':total,'limit':limit,'offset':offset}

@router.get('/tasks/{task_id}/export')
async def export(task_id: int,stage: str=Query('all',pattern='^(discovery|tls|http|matches|all)$'),format: str=Query('jsonl',pattern='^(csv|jsonl)$')):
    row=await get_task(task_id);db=await store.task(row['database_name']);directory=ROOT/str(task_id)/'exports';directory.mkdir(exist_ok=True)
    output=directory/(uuid4().hex+'.'+format);count=0;sha=hashlib.sha256()
    try:
        async with db.transaction(isolation='repeatable_read',readonly=True):
            watermark=await db.fetchval('SELECT COALESCE(max(id),0) FROM observations');endpoint_watermark=await db.fetchval('SELECT COALESCE(max(id),0) FROM endpoints')
            with output.open('wb') as file:
                def emit(record):
                    nonlocal count
                    if format=='jsonl':data=json.dumps(record,default=str,ensure_ascii=False)+'\n'
                    else:
                        buffer=io.StringIO();csv.writer(buffer).writerow([record['record_type'],record['id'],json.dumps(record,default=str,ensure_ascii=False)]);data=buffer.getvalue()
                    data=data.encode();file.write(data);sha.update(data);count+=1
                if format=='csv':file.write(b'record_type,id,payload\r\n');sha.update(b'record_type,id,payload\r\n')
                if stage in {'discovery','all'}:
                    async for item in db.cursor('SELECT id,host(ip) ip,port,sni,state,error,created_at FROM endpoints WHERE id<=$1 ORDER BY id',endpoint_watermark,prefetch=200):emit({'record_type':'discovery',**dict(item)})
                if stage!='discovery':
                    condition=''
                    if stage in {'tls','http'}:condition=" AND o.module='"+stage+"'"
                    if stage=='matches':condition=" AND EXISTS(SELECT 1 FROM jsonb_array_elements(o.evidence) e WHERE e->>'tier'='http_confirmed')"
                    async for item in db.cursor('SELECT o.id,host(e.ip) ip,e.port,e.sni,o.module,o.observation,o.evidence,o.created_at FROM observations o JOIN endpoints e ON e.id=o.endpoint_id WHERE o.id<=$1'+condition+' ORDER BY o.id',watermark,prefetch=200):emit({'record_type':item['module'],**serialize(item)})
                file.flush();os.fsync(file.fileno())
        export_id=await db.fetchval('INSERT INTO exports(stage,format,watermark,sha256,path,row_count) VALUES($1,$2,$3,$4,$5,$6) RETURNING id',stage,format,watermark,sha.hexdigest(),str(output),count)
        output.with_suffix(output.suffix+'.manifest.json').write_text(json.dumps({'id':export_id,'stage':stage,'format':format,'watermark':watermark,'endpoint_watermark':endpoint_watermark,'sha256':sha.hexdigest(),'row_count':count,'scope_sha256':row['source_sha256']},indent=2))
    finally:await db.close()
    return FileResponse(output,filename=f'task-{task_id}-{stage}.{format}',media_type='text/csv' if format=='csv' else 'application/x-ndjson',headers={'X-SHA256':sha.hexdigest(),'X-Watermark':str(watermark),'X-Endpoint-Watermark':str(endpoint_watermark),'X-Row-Count':str(count),'X-Export-ID':str(export_id)})
@router.get('/tasks/{task_id}/exports')
async def exports(task_id: int):
    row=await get_task(task_id);db=await store.task(row['database_name'])
    try:rows=await db.fetch('SELECT id,stage,format,watermark,sha256,row_count,created_at FROM exports ORDER BY id DESC LIMIT 100')
    finally:await db.close()
    return {'items':[dict(r) for r in rows]}
@router.get('/ready')
async def ready(request: Request):
    db=await store.control()
    try:await db.fetchval('SELECT count(*) FROM v2_tasks')
    finally:await db.close()
    await request.app.state.redis.ping();heartbeat=await request.app.state.redis.get('v2:worker:heartbeat')
    if not heartbeat:raise HTTPException(503,'Worker heartbeat unavailable')
    return {'status':'ready','worker':json.loads(heartbeat)}
