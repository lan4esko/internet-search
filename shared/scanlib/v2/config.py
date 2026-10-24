import ipaddress
import re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator

class ScanConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ports: list[int]=Field(default_factory=lambda:[443],min_length=1,max_length=64)
    rate_pps: int=Field(default=100,ge=1,le=100000)
    shards: int=Field(default=1,ge=1,le=256)
    seed: int=Field(default=1,ge=0,le=2**32-1)
    interface: str|None=None
    source_ip: str|None=None
    gateway_mac: str|None=None
    l7_workers: int=Field(default=2,ge=1,le=32)
    timeout: int=Field(default=10,ge=1,le=120)
    max_body_kb: int=Field(default=64,ge=1,le=1024)
    paths: list[str]=Field(default_factory=lambda:['/global-protect/login.esp'],min_length=1,max_length=16)
    schemes: list[Literal['http','https']]=Field(default_factory=lambda:['https'],min_length=1,max_length=2)
    sni: list[str]=Field(default_factory=lambda:[''],min_length=1,max_length=16)
    deny_cidrs: str=Field(default='',max_length=65536)
    egress: str=Field(default='direct',pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    template_ids: list[str]=Field(default_factory=lambda:['globalprotect-http','globalprotect-crypto'],max_length=32)
    max_attempts: int=Field(default=3,ge=1,le=10)
    max_queue: int=Field(default=10000,ge=100,le=1000000)

    @field_validator('ports')
    @classmethod
    def valid_ports(cls,values):
        if any(not 1<=v<=65535 for v in values): raise ValueError('Ports must be 1..65535')
        return sorted(set(values))
    @field_validator('paths')
    @classmethod
    def valid_paths(cls,values):
        if any(not p.startswith('/') or p.startswith('//') or len(p)>2048 or any(c in p for c in '\r\n\x00#') for p in values): raise ValueError('Invalid relative HTTP path')
        return list(dict.fromkeys(values))
    @field_validator('sni')
    @classmethod
    def valid_sni(cls,values):
        for name in values:
            if name and (len(name)>253 or not all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',p) for p in name.split('.'))): raise ValueError('SNI must be a DNS name')
        return list(dict.fromkeys(values))
    @field_validator('source_ip')
    @classmethod
    def valid_source(cls,value): return str(ipaddress.IPv4Address(value)) if value else None
    @field_validator('interface')
    @classmethod
    def valid_interface(cls,value):
        if value and not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,64}',value): raise ValueError('Invalid interface')
        return value
    @field_validator('gateway_mac')
    @classmethod
    def valid_mac(cls,value):
        if value and not re.fullmatch(r'(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}',value): raise ValueError('Invalid MAC')
        return value
