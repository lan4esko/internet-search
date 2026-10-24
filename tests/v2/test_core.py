import asyncio,base64,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from datetime import datetime,timedelta,timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from scanlib.v2.scope import compile_scope,restore_scope
from scanlib.v2.config import ScanConfig
from scanlib.v2.rules import load_rule,evaluate,validate_rule
from scanlib.v2.normalize import normalize_tls,normalize_http
from scanlib.v2.transport import open_destination,destination_relay
from scanlib.v2.collect import zmap_command
ROOT=Path(__file__).resolve().parents[2]
class CoreTests(unittest.TestCase):
    def test_large_cidr_without_expansion(self):
        s=compile_scope(b'11.0.0.1/12\n11.0.0.0/16');self.assertEqual(s.target_count,1048576);self.assertEqual(len(s.allow),1)
    def test_mandatory_exclusion(self):
        s=compile_scope(b'8.0.0.0/6');self.assertFalse(s.authorized('10.1.2.3',443));self.assertTrue(s.authorized('8.1.2.3',443))
    def test_precise_lab_scope(self):
        s=compile_scope(b'127.0.0.0/8',lab_cidrs='127.0.0.1/32');self.assertEqual(s.target_count,1);self.assertFalse(s.authorized('127.0.0.2',443))
    def test_user_deny_overrides_lab(self):
        with self.assertRaises(ValueError):compile_scope(b'127.0.0.1/32','127.0.0.1/32','127.0.0.1/32')
    def test_bad_scopes(self):
        for data in [b'',b'::1/128',b'invalid',b'10.0.0.0/8']:
            with self.subTest(data=data),self.assertRaises(ValueError):compile_scope(data)
    def test_manifest_integrity(self):
        scope=compile_scope(b'8.8.8.0/24')
        with tempfile.TemporaryDirectory() as tmp:
            manifest=scope.save(Path(tmp));self.assertEqual(restore_scope(manifest),scope);manifest['allow']=['1.1.1.0/24']
            with self.assertRaises(ValueError):restore_scope(manifest)
    def test_invalid_destinations(self):
        s=compile_scope(b'8.8.8.8/32')
        for ip,port in [('x.test',443),('::1',443),('8.8.8.8',0),('8.8.8.8',65536)]:self.assertFalse(s.authorized(ip,port))
    def test_invalid_config(self):
        for cfg in [{'args':['--follow-redirects']},{'paths':['//evil.test']},{'ports':[0]},{'sni':['host:443']},{'gateway_mac':'not-a-mac'}]:
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):ScanConfig(**cfg)
    def test_zmap_argv(self):
        cmd=zmap_command(ScanConfig(shards=4),Path('/scope'),443,2,Path('/meta'))
        for flag in ['--whitelist-file','--blacklist-file','--seed','--shards','--shard']:self.assertIn(flag,cmd)
    def test_http_confirmation(self):
        rule=load_rule((ROOT/'templates/v2/globalprotect-http.yaml').read_text());o={'http':{'status_code':200,'path':'/global-protect/login.esp','body':'GlobalProtect Portal id="login_form" name="prot"'}}
        self.assertEqual(evaluate(rule,o)['tier'],'http_confirmed');o['http']['status_code']=302;self.assertEqual(evaluate(rule,o)['tier'],'unknown')
    def test_empty_crypto_dataset(self):
        rule=load_rule((ROOT/'templates/v2/globalprotect-crypto.yaml').read_text());self.assertEqual(evaluate(rule,{'tls':{'spki_sha256':'a'*64}})['tier'],'unknown')
    def test_crypto_ceiling(self):
        rule={'version':1,'id':'crypto','classification':{'product':'example','on_match':'http_confirmed'},'match':{'field':'tls.issuer','op':'contains','value':'Example'}};validate_rule(rule)
        self.assertEqual(evaluate(rule,{'tls':{'issuer':'Example'}})['tier'],'crypto_candidate')
    def test_negative_and_threshold(self):
        rule={'version':1,'id':'generic','classification':{'product':'example','on_match':'candidate'},'match':{'field':'http.status_code','op':'equals','value':200},'negative':{'field':'http.body','op':'contains','value':'decoy'}};validate_rule(rule)
        o={'http':{'status_code':200,'body':'decoy'}};self.assertEqual(evaluate(rule,o)['tier'],'contradicted');rule['threshold']=99;self.assertEqual(evaluate(rule,o)['tier'],'unknown')
    def test_chain_boolean_regex(self):
        rule={'version':1,'id':'chain','classification':{'product':'example','on_match':'crypto_candidate'},'match':{'all':[{'field':'tls.chain.issuer','op':'regex','value':'Research.*CA'},{'not':{'field':'http.body','op':'contains','value':'decoy'}}]}};validate_rule(rule)
        self.assertEqual(evaluate(rule,{'tls':{'chain':[{'issuer':'Research CA'}]}})['tier'],'crypto_candidate')
    def test_recursive_yaml(self):
        with self.assertRaises(ValueError):load_rule('version: 1\nid: loop\nclassification: {product: x, on_match: candidate}\nmatch: &a {not: *a}')
    def test_bounded_body(self):
        value=normalize_http({'response':{'body':'a'*100,'headers':{'content_type':['text/plain']}}},'/','http',10)
        self.assertEqual(value['body'],'a'*10);self.assertTrue(value['body_truncated']);self.assertIn('content-type',value['headers'])
    def test_negotiated_tls13(self):
        value=normalize_tls({'handshake_log':{'server_hello':{'version':{'name':'TLSv1.2'},'supported_versions':{'selected_version':{'name':'TLSv1.3'}}}}});self.assertEqual(value['version'],'TLSv1.3')
    def test_real_certificate_hashes(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048);now=datetime.now(timezone.utc);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'lab.invalid')])
        cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('lab.invalid')]),False).sign(key,hashes.SHA256())
        result=normalize_tls({'handshake_log':{'server_certificates':{'certificate':{'raw':base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()}}}})
        self.assertEqual(result['certificate_sha256'],cert.fingerprint(hashes.SHA256()).hex());self.assertEqual(result['san'],['lab.invalid']);self.assertEqual(len(result['spki_sha256']),64)
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_connection_outside_scope(self):
        with patch('asyncio.open_connection',new_callable=AsyncMock) as connect:
            with self.assertRaises(PermissionError):await open_destination(compile_scope(b'8.8.8.8/32'),'1.1.1.1',443,{'mode':'direct'})
            connect.assert_not_called()
    async def test_relay(self):
        async def echo(reader,writer):
            await reader.read(100);writer.write(b'OK');await writer.drain();writer.close()
        server=await asyncio.start_server(echo,'127.0.0.1',0);port=server.sockets[0].getsockname()[1]
        try:
            async with destination_relay(compile_scope(b'127.0.0.1/32',lab_cidrs='127.0.0.1/32'),'127.0.0.1',port,{'mode':'direct'},3) as (local,errors):
                reader,writer=await asyncio.open_connection(*local);writer.write(b'test');await writer.drain();self.assertEqual(await reader.read(100),b'OK');writer.close();await writer.wait_closed()
            self.assertEqual(errors,[])
        finally:server.close();await server.wait_closed()
    async def test_connect_numeric_and_no_fallback(self):
        seen=[]
        async def proxy(reader,writer):
            seen.append(await reader.readuntil(b'\r\n\r\n'));writer.write(b'HTTP/1.1 403 Forbidden\r\n\r\n');await writer.drain();writer.close()
        server=await asyncio.start_server(proxy,'127.0.0.1',0)
        try:
            with patch.dict(os.environ,{'TEST_PROXY_URL':f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'}):
                with self.assertRaises(ConnectionError):await open_destination(compile_scope(b'8.8.8.8/32'),'8.8.8.8',443,{'mode':'http_connect','url_env':'TEST_PROXY_URL'})
            self.assertEqual(len(seen),1);self.assertIn(b'CONNECT 8.8.8.8:443 ',seen[0])
        finally:server.close();await server.wait_closed()
    async def test_socks_numeric(self):
        seen=[]
        async def proxy(reader,writer):
            head=await reader.readexactly(2);await reader.readexactly(head[1]);writer.write(b'\x05\x00');await writer.drain();seen.append(await reader.readexactly(10));writer.write(b'\x05\x00\x00\x01'+b'\x00'*6);await writer.drain();writer.close()
        server=await asyncio.start_server(proxy,'127.0.0.1',0)
        try:
            with patch.dict(os.environ,{'TEST_PROXY_URL':f'socks5://127.0.0.1:{server.sockets[0].getsockname()[1]}'}):
                _,writer=await open_destination(compile_scope(b'8.8.8.8/32'),'8.8.8.8',443,{'mode':'socks5','url_env':'TEST_PROXY_URL'});writer.close();await writer.wait_closed()
            self.assertEqual(seen[0][3:8],b'\x01\x08\x08\x08\x08')
        finally:server.close();await server.wait_closed()
