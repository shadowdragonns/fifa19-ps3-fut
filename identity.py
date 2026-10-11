"""Ephemeral local authorization codes for the console login bootstrap.

These codes belong only to this local process; they are not EA credentials.
Blaze login consumes these codes and creates a separate local session.
"""
import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit


class LocalIdentity:
    def __init__(self, http_port=8099, host='127.0.0.1'):
        from endpoints import Endpoints
        self.callback = Endpoints(host, http_port=http_port).callback
        self.codes = {}
        self.lock = threading.Lock()

    def response(self, method, path):
        url = urlsplit(path)
        if method != 'GET' or url.path != '/connect/auth':
            return None
        params = parse_qs(url.query, keep_blank_values=True, max_num_fields=64)
        # Keep redirects confined to the callback advertised in IdentityParams.
        fut = (params.get('client_id') == ['FOS-SERVER']
               and params.get('redirect_uri') == ['nucleus:rest'])
        if params.get('response_type') != ['code'] or (not fut and params.get('redirect_uri') != [self.callback]):
            return '400 Bad Request', b'{"error":"invalid_local_authorization_request"}', []
        with self.lock:
            now = time.monotonic()
            self.codes = {code: entry for code, entry in self.codes.items() if entry[0] > now}
            if len(self.codes) >= 128:
                return '503 Service Unavailable', b'{"error":"too_many_local_codes"}', []
            code = 'fifa19-local-' + secrets.token_hex(16)
            self.codes[code] = (now + 120, 'fut' if fut else 'blaze')
        # FUT's OAuth helper (0x16e62ac) expects HTTP 200 and a JSON code,
        # unlike the main Blaze login's callback redirect.
        if fut:
            return '200 OK', json.dumps({'code': code}).encode(), [('Cache-Control', 'no-store')]
        location = self.callback + '?' + urlencode({'code': code})
        # PS3's response reader explicitly handles HTTP 300 and 302, extracts
        # Location, compares redirect_uri, then reads its code query parameter.
        return '302 Found', b'', [('Location', location), ('Cache-Control', 'no-store')]

    def consume(self, code, purpose='blaze'):
        """Validate one local code once, confined to its issuing flow."""
        with self.lock:
            entry = self.codes.get(code)
            if entry is None or entry[1] != purpose:
                return False
            self.codes.pop(code)
        return entry[0] > time.monotonic()
