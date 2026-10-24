"""Local TLS/HTTP fixture; no outbound connections."""
import json,ssl,tempfile,threading,time
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
observations=[]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        observations.append({'path':self.path,'host':self.headers.get('Host')})
        if self.path=='/slow':time.sleep(3)
        if self.path=='/redirect':
            self.send_response(302);self.send_header('Location','https://198.51.100.10/outside-scope');self.end_headers();return
        body=json.dumps(observations).encode() if self.path=='/metrics' else b'<title>GlobalProtect Portal</title><form id="login_form"><input name="prot"></form>'
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass
if __name__=='__main__':
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'lab.invalid')]);now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(42).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=3)).add_extension(x509.SubjectAlternativeName([x509.DNSName('lab.invalid')]),False).sign(key,hashes.SHA256())
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM));(root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'cert.pem',root/'key.pem');context.set_servername_callback(lambda sock,name,ctx:observations.append({'sni':name}))
        server=ThreadingHTTPServer(('0.0.0.0',8443),Handler);server.socket=context.wrap_socket(server.socket,server_side=True);threading.Thread(target=server.serve_forever,daemon=True).start()
        ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
