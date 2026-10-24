"""Durable ZMap -> PostgreSQL outbox -> Redis Streams -> ZGrab2 pipeline."""
import asyncio
import hashlib
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from scanlib.v2 import database as store
from scanlib.v2.scope import restore_scope,digest
from scanlib.v2.config import ScanConfig
from scanlib.v2.collect import zmap_command,zgrab,terminate
from scanlib.v2.transport import select_egress
from scanlib.v2.rules import evaluate

ROOT=Path(os.getenv('SCOPE_DATA_DIR','/data/scope'))/'v2'
GROUP='collectors-v2'
LOG=logging.getLogger('pipeline')
def decode(value):return json.loads(value) if isinstance(value,str) else value

def log(event,task_id,**fields):LOG.info(json.dumps({'event':event,'task_id':task_id,**fields},default=str))

async def registry_status(task_id,status,error=None):
    db=await store.control()
    try:
        await db.execute("""UPDATE v2_tasks SET status=$2,error=$3,
         started_at=CASE WHEN $2='running' THEN COALESCE(started_at,now()) ELSE started_at END,
         finished_at=CASE WHEN $2 IN ('completed','completed_with_errors','failed','cancelled') THEN now() ELSE NULL END
         WHERE id=$1 AND ($2!='running' OR status IN ('queued','running'))""",task_id,status,error)
    finally:await db.close()

async def enqueue(db,scope,config,ip,port):
    if port not in config.ports or not scope.authorized(ip,port):
        await store.event(db,'scope_rejected',{'ip':ip,'port':port},'error')
        raise PermissionError('ZMap emitted a destination outside authorized scope/ports')
    async with db.transaction():
        for sni in config.sni:
            row=await db.fetchrow('INSERT INTO endpoints(ip,port,sni) VALUES($1::inet,$2,$3) ON CONFLICT(ip,port,sni) DO NOTHING RETURNING id',ip,port,sni)
            if row:await db.execute('INSERT INTO outbox(endpoint_id) VALUES($1) ON CONFLICT DO NOTHING',row['id'])

async def discover(row,scope,config):
    directory=ROOT/str(row['id']);db=await store.task(row['database_name'])
    try:
        for entry in await db.fetch("SELECT * FROM coverage WHERE status!='done' ORDER BY port,shard"):
            port,shard=entry['port'],entry['shard']
            raw=directory/f'zmap-{port}-{shard}.csv';metadata=directory/f'zmap-{port}-{shard}.metadata.json'
            if raw.exists():
                with raw.open() as spool:
                    for line in spool:
                        ip=line.strip()
                        if ip and ip!='saddr':await enqueue(db,scope,config,ip,port)
            argv=zmap_command(config,directory,port,shard,metadata)
            await db.execute("UPDATE coverage SET status='running',attempts=attempts+1,started_at=now(),error=NULL,argv=$3::jsonb WHERE port=$1 AND shard=$2",port,shard,json.dumps(argv))
            await store.event(db,'discovery_started',{'port':port,'shard':shard,'argv':argv})
            stderr_path=directory/f'zmap-{port}-{shard}.stderr.log'
            with stderr_path.open('ab') as stderr,raw.open('ab',buffering=0) as spool:
                offset=raw.stat().st_size
                process=await asyncio.create_subprocess_exec(*argv,stdout=spool,stderr=stderr,start_new_session=True)
                deadline=time.monotonic()+max(60,scope.target_count/config.shards/config.rate_pps*3+30)
                try:
                    # Disk spool does not block the ZMap receive loop when L7 is backpressured.
                    with raw.open('rb') as reader:
                        reader.seek(offset)
                        while True:
                            if time.monotonic()>deadline:raise TimeoutError('ZMap shard deadline exceeded; inspect interface/gateway/raw networking')
                            position=reader.tell();line=reader.readline()
                            if not line or not line.endswith(b'\n'):
                                reader.seek(position)
                                if process.returncode is not None:
                                    if line:raise RuntimeError('Incomplete spool record')
                                    break
                                await asyncio.sleep(.1);continue
                            ip=line.decode().strip()
                            if not ip or ip=='saddr':continue
                            os.fsync(spool.fileno())
                            while await db.fetchval("SELECT count(*) FROM endpoints WHERE state IN ('pending','running')")>=config.max_queue:await asyncio.sleep(.5)
                            await enqueue(db,scope,config,ip,port)
                    await process.wait()
                    if process.returncode:
                        error=f'ZMap exited {process.returncode}: '+stderr_path.read_text(errors='replace')[-2000:]
                        await db.execute("UPDATE coverage SET status='failed',error=$3,finished_at=now() WHERE port=$1 AND shard=$2",port,shard,error)
                        raise RuntimeError(error)
                    meta=json.loads(metadata.read_text()) if metadata.exists() else {}
                    sent=meta.get('send',{}).get('packets_sent',meta.get('packets_sent',meta.get('total_sent')))
                    if sent is not None:sent=int(sent)
                    await db.execute("UPDATE coverage SET status='done',sent=$3,found=$4,metadata=$5::jsonb,finished_at=now() WHERE port=$1 AND shard=$2",port,shard,sent,int(meta.get('success_unique',0)),json.dumps(meta))
                    await store.event(db,'discovery_completed',{'port':port,'shard':shard,'sent':sent})
                finally:await terminate(process)
    finally:await db.close()

async def publish(row,redis):
    db=await store.task(row['database_name']);stream=f"v2:{row['id']}:endpoints"
    try:
        while True:
            for event in await db.fetch('SELECT endpoint_id FROM outbox WHERE NOT published ORDER BY endpoint_id LIMIT 100'):
                async with db.transaction():
                    pending=await db.fetchval('SELECT NOT published FROM outbox WHERE endpoint_id=$1 FOR UPDATE',event['endpoint_id'])
                    if pending:
                        await redis.xadd(stream,{'endpoint_id':str(event['endpoint_id'])})
                        await db.execute('UPDATE outbox SET published=TRUE WHERE endpoint_id=$1',event['endpoint_id'])
            await asyncio.sleep(.2)
    finally:await db.close()

async def consumer(row,redis,scope,config,number):
    db=await store.task(row['database_name']);stream=f"v2:{row['id']}:endpoints";consumer_name=f'{socket.gethostname()}-{number}'
    try:
        while True:
            claim=await redis.xautoclaim(stream,GROUP,consumer_name,min_idle_time=30000,start_id='0-0',count=1)
            messages=claim[1]
            if not messages:
                batch=await redis.xreadgroup(GROUP,consumer_name,{stream:'>'},count=1,block=1000)
                messages=batch[0][1] if batch else []
            for message_id,message in messages:
                endpoint_id=int(message['endpoint_id'])
                endpoint=await db.fetchrow("UPDATE endpoints SET state='running',attempts=attempts+1 WHERE id=$1 AND state='pending' RETURNING *,host(ip) address",endpoint_id)
                if endpoint is None:
                    state=await db.fetchval('SELECT state FROM endpoints WHERE id=$1',endpoint_id)
                    if state!='running':await redis.xack(stream,GROUP,message_id);await redis.xdel(stream,message_id)
                    continue
                ip,port,sni=endpoint['address'],endpoint['port'],endpoint['sni']
                try:
                    egress=select_egress(config.egress,f'{ip}:{port}:{sni}')
                    directory=ROOT/str(row['id'])/'observations';directory.mkdir(exist_ok=True)
                    tls=None
                    probes=[('tls','','')]+[('http',path,scheme) for scheme in config.schemes for path in config.paths]
                    for module,path,scheme in probes:
                        existing=await db.fetchrow('SELECT observation FROM observations WHERE endpoint_id=$1 AND module=$2 AND path=$3 AND scheme=$4',endpoint_id,module,path,scheme)
                        if existing:
                            if module=='tls':tls=decode(existing['observation']).get('tls')
                            continue
                        observation,raw=await zgrab(scope,ip,port,sni,egress,config,module,path or None,scheme or None)
                        if module=='tls':tls=observation.get('tls')
                        elif tls:observation['tls']=tls
                        evidence=[evaluate(template,observation) for template in decode(row['templates'])]
                        artifact=directory/f'{endpoint_id}-{module}-{hashlib.sha256((path+scheme).encode()).hexdigest()[:16]}.json'
                        temporary=artifact.with_suffix('.tmp');temporary.write_text(json.dumps(raw))
                        with temporary.open('rb') as source:os.fsync(source.fileno())
                        temporary.replace(artifact)
                        await db.execute('''INSERT INTO observations(endpoint_id,module,path,scheme,observation,evidence,raw_path)
                         VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7) ON CONFLICT(endpoint_id,module,path,scheme) DO NOTHING''',endpoint_id,module,path,scheme,json.dumps(observation),json.dumps(evidence),str(artifact))
                    await db.execute("UPDATE endpoints SET state='done',error=NULL,finished_at=now() WHERE id=$1",endpoint_id)
                    await store.event(db,'endpoint_collected',{'endpoint_id':endpoint_id})
                except asyncio.CancelledError:raise
                except Exception as error:
                    text=str(error)[:2000];failed=endpoint['attempts']>=config.max_attempts
                    async with db.transaction():
                        await db.execute("UPDATE endpoints SET state=$2,error=$3,finished_at=CASE WHEN $2='failed' THEN now() ELSE NULL END WHERE id=$1",endpoint_id,'failed' if failed else 'pending',text)
                        if not failed:await db.execute('UPDATE outbox SET published=FALSE WHERE endpoint_id=$1',endpoint_id)
                        await store.event(db,'endpoint_failed' if failed else 'endpoint_retry',{'endpoint_id':endpoint_id,'error':text},'error')
                    if failed:await redis.xadd(f"v2:{row['id']}:dlq",{'endpoint_id':str(endpoint_id),'error':text})
                await redis.xack(stream,GROUP,message_id);await redis.xdel(stream,message_id)
    finally:await db.close()

async def run_task(row,redis):
    task_id=row['id'];db=await store.task(row['database_name']);components=[]
    try:
        if not await db.fetchval('SELECT pg_try_advisory_lock($1::bigint)',task_id):return
        config=ScanConfig.model_validate(decode(row['config']));manifest=decode(row['manifest']);scope=restore_scope(manifest);directory=ROOT/str(task_id)
        if digest((directory/'source.txt').read_bytes())!=scope.source_sha256:raise ValueError('Immutable source checksum mismatch')
        for name in ['allowlist.txt','denylist.txt']:
            if digest((directory/name).read_bytes())!=manifest[name+'.sha256']:raise ValueError('Scope artifact checksum mismatch')
        select_egress(config.egress,str(task_id))
        await db.execute("UPDATE endpoints SET state='pending' WHERE state='running'")
        await db.execute("UPDATE outbox SET published=FALSE WHERE endpoint_id IN (SELECT id FROM endpoints WHERE state='pending')")
        stream=f'v2:{task_id}:endpoints'
        try:await redis.xgroup_create(stream,GROUP,id='0-0',mkstream=True)
        except ResponseError as error:
            if 'BUSYGROUP' not in str(error):raise
        await registry_status(task_id,'running')
        control=await store.control()
        try:initial=await control.fetchval('SELECT status FROM v2_tasks WHERE id=$1',task_id)
        finally:await control.close()
        if initial in {'cancelled','cancelling'}:await registry_status(task_id,'cancelled');return
        log('task_started',task_id)
        components=[asyncio.create_task(publish(row,redis))]+[asyncio.create_task(consumer(row,redis,scope,config,n)) for n in range(config.l7_workers)]
        discovery=asyncio.create_task(discover(row,scope,config));components.append(discovery)
        while True:
            for component in components:
                if component.done() and component.exception():raise component.exception()
            control=await store.control()
            try:status=await control.fetchval('SELECT status FROM v2_tasks WHERE id=$1',task_id)
            finally:await control.close()
            if status=='cancelling':await registry_status(task_id,'cancelled');break
            if discovery.done() and not await db.fetchval("SELECT EXISTS(SELECT 1 FROM endpoints WHERE state IN ('pending','running'))"):
                failures=await db.fetchval("SELECT count(*) FROM endpoints WHERE state='failed'")
                await registry_status(task_id,'completed_with_errors' if failures else 'completed');break
            await redis.set(f'v2:{task_id}:stats',json.dumps(await store.stats(db),default=str));await asyncio.sleep(.5)
        await redis.set(f'v2:{task_id}:stats',json.dumps(await store.stats(db),default=str));log('task_finished',task_id)
    except asyncio.CancelledError:raise
    except Exception as error:log('task_failed',task_id,error=str(error)[:2000]);await registry_status(task_id,'failed',str(error)[:2000])
    finally:
        for component in components:component.cancel()
        await asyncio.gather(*components,return_exceptions=True);await db.close()

async def heartbeat(redis):
    while True:
        try:
            value=json.dumps({'host':socket.gethostname(),'pipeline':'zmap-zgrab2','version':'2.0.0-restored'})
            await redis.set('v2:worker:heartbeat',value,ex=15)
            await redis.set('v2:worker:'+socket.gethostname()+':heartbeat',value,ex=15)
        except Exception:LOG.warning('{"event":"heartbeat_unavailable"}')
        await asyncio.sleep(3)

async def main():
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,asyncio.current_task().cancel)
    redis=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True);pulse=asyncio.create_task(heartbeat(redis))
    try:
        while True:
            try:
                db=await store.control()
                try:rows=await db.fetch("SELECT * FROM v2_tasks WHERE status IN ('queued','running','cancelling') ORDER BY id")
                finally:await db.close()
                for row in rows:await run_task(row,redis)
            except asyncio.CancelledError:raise
            except Exception as error:LOG.error(json.dumps({'event':'scheduler_error','error':str(error)[:1000]}))
            await asyncio.sleep(1)
    finally:
        pulse.cancel();await asyncio.gather(pulse,return_exceptions=True);await redis.aclose()

if __name__=='__main__':asyncio.run(main())
