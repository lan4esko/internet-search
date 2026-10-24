"""Immutable IPv4 scope compilation, without enumerating addresses."""
import hashlib
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

MANDATORY_DENY = ('0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8',
 '169.254.0.0/16','172.16.0.0/12','192.0.0.0/24','192.0.2.0/24','192.168.0.0/16',
 '198.18.0.0/15','198.51.100.0/24','203.0.113.0/24','224.0.0.0/4','240.0.0.0/4')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def networks(text):
    result=[]
    for number,line in enumerate(text.splitlines(),1):
        line=line.split('#',1)[0].strip()
        if not line: continue
        try:
            net=ipaddress.ip_network(line,strict=False)
            if net.version!=4: raise ValueError('Only IPv4 is supported')
        except ValueError as error: raise ValueError(f'CIDR line {number}: {error}') from error
        result.append(net)
        if len(result)>100000: raise ValueError('Too many CIDRs')
    return list(ipaddress.collapse_addresses(result))

def subtract(allowed,denied):
    for block in denied:
        remaining=[]
        for net in allowed:
            if net.subnet_of(block): continue
            remaining.extend(net.address_exclude(block) if block.subnet_of(net) else [net])
        allowed=remaining
    return sorted(allowed)

@dataclass(frozen=True)
class Scope:
    allow: tuple
    deny: tuple
    source_sha256: str
    target_count: int

    def authorized(self,ip,port):
        try:
            address=ipaddress.IPv4Address(ip)
            return (1<=port<=65535 and any(address in ipaddress.ip_network(n) for n in self.allow)
                    and not any(address in ipaddress.ip_network(n) for n in self.deny))
        except (ValueError,TypeError): return False

    def manifest(self):
        result=dict(version=2,allow=list(self.allow),deny=list(self.deny),source_sha256=self.source_sha256,target_count=self.target_count)
        result['manifest_sha256']=digest(json.dumps(result,sort_keys=True).encode())
        return result

    def save(self,directory):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        manifest=self.manifest()
        for name,values in [('allowlist.txt',self.allow),('denylist.txt',self.deny)]:
            data=('\n'.join(values)+'\n').encode();(directory/name).write_bytes(data)
            manifest[name+'.sha256']=digest(data)
        (directory/'manifest.json').write_text(json.dumps(manifest,indent=2))
        return manifest

def compile_scope(source,extra_deny='',lab_cidrs=''):
    if not source or len(source)>10485760: raise ValueError('CIDR file must contain 1..10485760 bytes')
    allow=networks(source.decode('utf-8-sig'))
    lab=networks(lab_cidrs.replace(',','\n')) if lab_cidrs else []
    deny=list(ipaddress.collapse_addresses(subtract(networks('\n'.join(MANDATORY_DENY)),lab)+networks(extra_deny)))
    effective=subtract(allow,deny)
    if not effective: raise ValueError('Scope is empty after mandatory exclusions')
    return Scope(tuple(map(str,effective)),tuple(map(str,deny)),digest(source),sum(n.num_addresses for n in effective))

def restore_scope(manifest):
    result=Scope(tuple(manifest['allow']),tuple(manifest['deny']),manifest['source_sha256'],manifest['target_count'])
    if result.manifest()['manifest_sha256']!=manifest['manifest_sha256']: raise ValueError('Invalid manifest checksum')
    return result
