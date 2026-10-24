"""Fail-closed transport: proxy gets a verified numeric destination, never SNI."""
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import struct
from contextlib import asynccontextmanager
from urllib.parse import urlsplit,unquote

def egress_pool():
    path=os.getenv('V2_EGRESS_FILE')
    if path:
        with open(path) as source:pools=json.load(source)
    else:pools=json.loads(os.getenv('V2_EGRESS_POOLS','{}'))
    pools.setdefault('direct',[{'mode':'direct'}]);return pools

def select_egress(name,identity):
    values=egress_pool().get(name)
    if not isinstance(values,list) or not values:raise ValueError('Egress pool unavailable')
    item=values[int(hashlib.sha256(identity.encode()).hexdigest(),16)%len(values)]
    if item.get('mode') not in {'direct','source_ip','socks5','http_connect'}:raise ValueError('Invalid egress mode')
    if item['mode']=='source_ip':ipaddress.IPv4Address(item.get('source_ip',''))
    if item['mode'] in {'socks5','http_connect'} and not os.getenv(item.get('url_env','')):raise ValueError('Proxy binding missing')
    return item

async def open_destination(scope,ip,port,egress):
    if not scope.authorized(ip,port):raise PermissionError('Destination outside immutable scope')
    mode=egress['mode']
    if mode in {'direct','source_ip'}:
        local=(str(ipaddress.IPv4Address(egress['source_ip'])),0) if mode=='source_ip' else None
        return await asyncio.open_connection(ip,port,local_addr=local)
    proxy=urlsplit(os.getenv(egress.get('url_env',''),''))
    if not proxy.hostname or not proxy.port or proxy.scheme not in {'http','https','socks5'}:raise ValueError('Invalid proxy binding')
    reader,writer=await asyncio.open_connection(proxy.hostname,proxy.port,ssl=True if proxy.scheme=='https' else None)
    try:
        if mode=='http_connect':
            auth=''
            if proxy.username is not None:
                token=base64.b64encode((unquote(proxy.username)+':'+unquote(proxy.password or '')).encode()).decode()
                auth=f'Proxy-Authorization: Basic {token}\r\n'
            writer.write(f'CONNECT {ip}:{port} HTTP/1.1\r\nHost: {ip}:{port}\r\n{auth}\r\n'.encode());await writer.drain()
            reply=await reader.readuntil(b'\r\n\r\n')
            if len(reply)>16384 or reply.split(b' ',2)[1]!=b'200':raise ConnectionError('Proxy CONNECT rejected')
        else:
            methods=b'\x00\x02' if proxy.username else b'\x00'
            writer.write(b'\x05'+bytes([len(methods)])+methods);await writer.drain()
            response=await reader.readexactly(2)
            if response==b'\x05\x02':
                user=unquote(proxy.username or '').encode();password=unquote(proxy.password or '').encode()
                if len(user)>255 or len(password)>255:raise ValueError('SOCKS credentials too long')
                writer.write(b'\x01'+bytes([len(user)])+user+bytes([len(password)])+password);await writer.drain()
                if await reader.readexactly(2)!=b'\x01\x00':raise ConnectionError('SOCKS authentication rejected')
            elif response!=b'\x05\x00':raise ConnectionError('SOCKS method rejected')
            writer.write(b'\x05\x01\x00\x01'+ipaddress.IPv4Address(ip).packed+struct.pack('!H',port));await writer.drain()
            response=await reader.readexactly(4)
            if response[:2]!=b'\x05\x00':raise ConnectionError('SOCKS CONNECT rejected')
            length=(await reader.readexactly(1))[0] if response[3]==3 else {1:4,4:16}.get(response[3],0)
            if not length:raise ConnectionError('Invalid SOCKS response')
            await reader.readexactly(length+2)
        return reader,writer
    except BaseException:
        writer.close();await writer.wait_closed();raise

@asynccontextmanager
async def destination_relay(scope,ip,port,egress,timeout,listen_port=0):
    connections=set();errors=[]
    async def copy(reader,writer):
        while data:=await reader.read(65536):writer.write(data);await writer.drain()
    async def accept(reader,writer):
        task=asyncio.current_task();connections.add(task);remote=None
        try:
            async with asyncio.timeout(timeout):
                incoming,remote=await open_destination(scope,ip,port,egress)
                tasks=[asyncio.create_task(copy(reader,remote)),asyncio.create_task(copy(incoming,writer))]
                try:
                    done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for item in done:item.result()
                finally:
                    for item in tasks:item.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except Exception as error:errors.append(type(error).__name__)
        finally:
            writer.close()
            if remote:remote.close()
            connections.discard(task)
    address='127.'+'.'.join(str(x) for x in os.urandom(3)) if listen_port else '127.0.0.1'
    server=await asyncio.start_server(accept,address,listen_port)
    try:yield server.sockets[0].getsockname()[:2],errors
    finally:
        server.close();await server.wait_closed()
        pending=list(connections)
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
