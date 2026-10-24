import asyncio
import json
import os
import signal
from .transport import destination_relay
from .normalize import normalize_tls,normalize_http

async def terminate(process):
    if process.returncode is None:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:await asyncio.wait_for(process.wait(),3)
        except TimeoutError:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            await process.wait()

def zmap_command(config,directory,port,shard,metadata):
    argv=['zmap','--target-port',str(port),'--rate',str(config.rate_pps),'--whitelist-file',str(directory/'allowlist.txt'),
     '--blacklist-file',str(directory/'denylist.txt'),'--seed',str(config.seed),'--shards',str(config.shards),'--shard',str(shard),
     '--output-module','csv','--output-fields','saddr','--output-filter','success = 1 && repeat = 0','--metadata-file',str(metadata),'--cooldown-time','2']
    if config.interface:argv+=['--interface',config.interface]
    if config.source_ip:argv+=['--source-ip',config.source_ip]
    if config.gateway_mac:argv+=['--gateway-mac',config.gateway_mac]
    return argv

async def zgrab(scope,ip,port,sni,egress,config,module,path=None,scheme=None):
    if not scope.authorized(ip,port):raise PermissionError('Pre-probe scope rejection')
    async with destination_relay(scope,ip,port,egress,config.timeout+2,listen_port=port) as (local,errors):
        local_ip,local_port=local
        argv=['zgrab2','--blocklist-file','/dev/null','--read-limit-per-host',str(config.max_body_kb+128),
         '--senders','1','--connections-per-host','1',module,'--port',str(local_port),'--target-timeout',f'{config.timeout}s','--connect-timeout',f'{config.timeout}s']
        if sni:argv+=['--server-name',sni]
        elif module=='tls' or scheme=='https':argv+=['--no-sni']
        if module=='http':
            argv+=['--endpoint',path,'--max-redirects','0','--max-size',str(config.max_body_kb)]
            if scheme=='https':argv+=['--use-https']
        process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
        try:
            stdout,stderr=await asyncio.wait_for(process.communicate(f'{local_ip},{sni or ip},,{local_port}\n'.encode()),config.timeout+5)
            if process.returncode:raise RuntimeError(f'ZGrab2 exited {process.returncode}: '+stderr.decode(errors='replace')[-1500:])
            if errors:raise RuntimeError('Egress transport failed: '+errors[-1])
            lines=stdout.splitlines()
            if len(lines)!=1:raise RuntimeError('Expected exactly one ZGrab2 observation')
            raw=json.loads(lines[0]);data=raw.get('data',{}).get(module,{})
            result={'ip':ip,'port':port,'sni':sni,'collector':'zgrab2/0.2.0','status':data.get('status','unknown'),
             'error':data.get('error'),'egress_mode':egress['mode'],'transport_errors':list(errors)}
            if module=='tls':result['tls']=normalize_tls(data.get('result') or {})
            else:result['http']=normalize_http(data.get('result') or {},path,scheme,config.max_body_kb*1024)
            return result,raw
        finally:await terminate(process)
