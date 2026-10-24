import base64
import hashlib
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization

def nested(value,path,default=None):
    for key in path.split('.'):value=value.get(key) if isinstance(value,dict) else None
    return default if value is None else value

def normalize_tls(result):
    handshake=nested(result,'handshake_log',result)
    hello=handshake.get('server_hello',{})
    certificates=handshake.get('server_certificates',{})
    def name(value):return value.get('name') if isinstance(value,dict) else value
    version=nested(hello,'supported_versions.selected_version',hello.get('version'))
    output={'version':name(version),'cipher_suite':name(hello.get('cipher_suite')),
            'alpn':hello.get('alpn_protocol'),'certificate_chain':certificates.get('chain',[]),'certificate_trust_evaluated':False}
    raw=certificates.get('certificate',{}).get('raw')
    if raw:
        try:
            cert=x509.load_der_x509_certificate(base64.b64decode(raw,validate=True))
            output.update(certificate_sha256=cert.fingerprint(hashes.SHA256()).hex(),
             spki_sha256=hashlib.sha256(cert.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest(),
             subject=cert.subject.rfc4514_string(),issuer=cert.issuer.rfc4514_string(),
             not_before=cert.not_valid_before_utc.isoformat(),not_after=cert.not_valid_after_utc.isoformat(),
             key_algorithm=type(cert.public_key()).__name__,signature_algorithm=cert.signature_algorithm_oid.dotted_string)
            try:
                san=cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                output['san']=san.get_values_for_type(x509.DNSName)+[str(v) for v in san.get_values_for_type(x509.IPAddress)]
            except x509.ExtensionNotFound:output['san']=[]
        except (ValueError,TypeError) as error:output['parse_error']=type(error).__name__
    return output

def normalize_http(result,path,scheme,limit):
    response=result.get('response') or {};body=str(response.get('body',''));encoded=body.encode()[:limit]
    return {'status_code':response.get('status_code'),'headers':{k.lower().replace('_','-'):v for k,v in response.get('headers',{}).items()},
     'body':encoded.decode(errors='replace'),'body_sha256':hashlib.sha256(encoded).hexdigest(),
     'body_truncated':len(body.encode())>limit or response.get('body_size',0)>=limit,'path':path,'scheme':scheme,'redirect_followed':False}
