"""Fetch and pin the chosen FUT server's public CA; never request private keys."""
import json
from pathlib import Path
import ssl
import tempfile
import urllib.error
import urllib.request

import client_platform
from tls_identity import certificate_details

MAX_CERTIFICATE = 16384
TLS_CIPHERS = ('AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256:'
               'AES128-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=0')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise ValueError('The server redirected its certificate/health request; check the address')


def read_url(url, limit, context=None):
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if context is not None: handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=5) as reply:
            body = reply.read(limit+1)
    except urllib.error.URLError as error:
        raise ValueError('Could not reach the FUT server: '+str(error.reason)) from error
    if len(body) > limit: raise ValueError('Server response exceeds its size limit')
    return body


def check_server(endpoints, context=None):
    base = endpoints.base_url if context is None else f'https://{endpoints.host}:44325'
    info = json.loads(read_url(base+'/fut-local/health', 4096, context))
    if not isinstance(info, dict) or info.get('service') != 'fifa19-fut' or info.get('version') != 1:
        raise ValueError('The address is not a compatible FIFA 19 FUT server')
    if info.get('advertise') != endpoints.host:
        raise ValueError(f'Server advertises {info.get("advertise")}; restart it with --advertise {endpoints.host}')
    return info


def fetch_certificate(endpoints, cache_dir, refresh=False):
    """Trust first contact, verify its TLS leaf, then pin the CA separately per IP."""
    info = check_server(endpoints)
    advertised = info.get('public_certificate', {})
    if not isinstance(advertised, dict) or advertised.get('path') != '/fut-local/root.pem':
        raise ValueError('This server does not offer automatic certificates. Update it or supply --trust-ca root.pem')
    # Never use a URL supplied by an untrusted response; fetch only this address.
    pem = read_url(endpoints.base_url+'/fut-local/root.pem', MAX_CERTIFICATE)
    details = certificate_details(pem)
    if advertised.get('sha256') != details['sha256']:
        raise ValueError('The downloaded certificate does not match the server health response')
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir/(endpoints.host+'.pem')
    with client_platform.file_lock(cache_dir/(endpoints.host+'.lock')):
        if target.exists():
            previous = certificate_details(target.read_bytes())
            if previous['sha256'] != details['sha256'] and not refresh:
                raise ValueError('The server certificate changed. Check with its operator, then use '
                                 '--refresh-server-cert or the GUI replacement option')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers(TLS_CIPHERS)
        context.load_verify_locations(cadata=pem.decode('ascii'))
        verified = check_server(endpoints, context)
        verified_ca = verified.get('public_certificate', {})
        if not isinstance(verified_ca, dict) or verified_ca.get('sha256') != details['sha256']:
            raise ValueError('TLS server identity differs from the certificate endpoint')
        # Keep an existing identical pin untouched; use atomic replacement on rotation.
        if not target.exists() or target.read_bytes() != pem:
            with tempfile.NamedTemporaryFile(dir=cache_dir, prefix='.ca-', delete=False) as stream:
                staging = Path(stream.name)
                stream.write(pem)
            try: staging.replace(target)
            finally: staging.unlink(missing_ok=True)
    return target, details['sha256']
