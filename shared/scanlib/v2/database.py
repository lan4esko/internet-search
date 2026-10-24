import json
import os
import re
from urllib.parse import urlsplit,urlunsplit
import asyncpg

SCHEMA='''
CREATE TABLE scope_manifest(id INTEGER PRIMARY KEY DEFAULT 1 CHECK(id=1),manifest JSONB NOT NULL);
CREATE TABLE coverage(port INTEGER NOT NULL,shard INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',planned BIGINT NOT NULL,
 sent BIGINT,found BIGINT NOT NULL DEFAULT 0,attempts INT NOT NULL DEFAULT 0,argv JSONB,metadata JSONB,error TEXT,
 started_at TIMESTAMPTZ,finished_at TIMESTAMPTZ,PRIMARY KEY(port,shard));
CREATE TABLE endpoints(id BIGSERIAL PRIMARY KEY,ip INET NOT NULL,port INT NOT NULL CHECK(port BETWEEN 1 AND 65535),
 sni TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'pending',attempts INT NOT NULL DEFAULT 0,error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),finished_at TIMESTAMPTZ,UNIQUE(ip,port,sni));
CREATE INDEX endpoints_state ON endpoints(state,id);
CREATE TABLE outbox(endpoint_id BIGINT PRIMARY KEY REFERENCES endpoints(id),published BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE observations(id BIGSERIAL PRIMARY KEY,endpoint_id BIGINT NOT NULL REFERENCES endpoints(id),module TEXT NOT NULL,
 path TEXT NOT NULL DEFAULT '',scheme TEXT NOT NULL DEFAULT '',observation JSONB NOT NULL,evidence JSONB NOT NULL,
 raw_path TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(endpoint_id,module,path,scheme));
CREATE TABLE exports(id BIGSERIAL PRIMARY KEY,stage TEXT NOT NULL,format TEXT NOT NULL,watermark BIGINT NOT NULL,
 sha256 TEXT NOT NULL,path TEXT NOT NULL,row_count BIGINT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE events(id BIGSERIAL PRIMARY KEY,level TEXT NOT NULL,event TEXT NOT NULL,payload JSONB NOT NULL DEFAULT '{}',created_at TIMESTAMPTZ NOT NULL DEFAULT now());
'''

def control_url():return os.environ['DATABASE_URL'].replace('+asyncpg','')
def task_url(name):
    if not re.fullmatch(r'task_[0-9a-f]{32}',name):raise ValueError('Invalid task database')
    p=urlsplit(control_url());return urlunsplit((p.scheme,p.netloc,'/'+name,p.query,p.fragment))
async def control():return await asyncpg.connect(control_url())
async def task(name):return await asyncpg.connect(task_url(name))
async def event(db,name,payload=None,level='info'):
    await db.execute('INSERT INTO events(level,event,payload) VALUES($1,$2,$3::jsonb)',level,name,json.dumps(payload or {}))
async def stats(db):
    endpoints=await db.fetchrow("SELECT count(*) total,count(*) FILTER(WHERE state='pending') queued,count(*) FILTER(WHERE state='running') running,count(*) FILTER(WHERE state='done') processed,count(*) FILTER(WHERE state='failed') errors FROM endpoints")
    coverage=await db.fetchrow("SELECT count(*) total,count(*) FILTER(WHERE status='done') completed,count(*) FILTER(WHERE status='failed') errors,COALESCE(sum(planned),0) planned,sum(sent) sent,count(sent) measured_shards FROM coverage")
    observations=await db.fetchrow("SELECT count(*) FILTER(WHERE module='tls') tls,count(*) FILTER(WHERE module='http') http,count(*) FILTER(WHERE observation->>'status'!='success') errors,count(DISTINCT endpoint_id) FILTER(WHERE EXISTS(SELECT 1 FROM jsonb_array_elements(evidence) e WHERE e->>'tier'='http_confirmed')) confirmed FROM observations")
    tiers=await db.fetch("SELECT e->>'tier' tier,count(DISTINCT endpoint_id) count FROM observations CROSS JOIN LATERAL jsonb_array_elements(evidence) e GROUP BY e->>'tier'")
    rate=await db.fetchval("SELECT count(*)/10.0 FROM endpoints WHERE finished_at>now()-interval '10 seconds'")
    return {'endpoints':dict(endpoints),'coverage':dict(coverage),'observations':dict(observations),'classifications':{r['tier']:r['count'] for r in tiers},'endpoints_per_second':float(rate)}
