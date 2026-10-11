"""Local CMS storage for the eight compiled PS3 hospitality XML filenames.

PRAN copies are separate from RPCS3 saves and squad databases. Credentials
are checked against an active local EASW session and never persisted here.
Wire contracts come from research/pran-schema.json and pran-disassembly.txt.
"""
import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

FILES = ('pran_versions.xml', 'pran_settings.xml', 'pran_formations.xml',
         'pran_tactics.xml', 'pran_careerstats.xml', 'pran_dlc.xml',
         'pran_vpro.xml', 'pran_vprostats.xml')
SKU = 'FFAPRAN1'
CONTENT_SKU = 'FFA19PS3'
LIMIT = 512 * 1024


def reply(value, handler, status='200 OK', mime='application/json', headers=()):
    body = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    return status, body, mime, [('Cache-Control', 'no-store'), *headers], handler


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.files = {}
        if self.path and self.path.exists():
            self.files = json.loads(self.path.read_text())
            if not isinstance(self.files, dict) or set(self.files) - set(FILES):
                raise ValueError('Invalid local PRAN file index')
            for name, row in self.files.items():
                if (not isinstance(row, dict) or set(row) != {'body', 'created', 'modified'}
                        or not all(isinstance(row[k], str) for k in row)
                        or len(base64.b64decode(row['body'], validate=True)) > LIMIT):
                    raise ValueError('Invalid local PRAN file')

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.files, indent=2) + '\n')
            temporary.replace(self.path)

    def put(self, name, body):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        previous = self.files.get(name, {})
        self.files[name] = {'body': base64.b64encode(body).decode(),
                            'created': previous.get('created', now), 'modified': now}

    def summary(self, name, user):
        row = self.files[name]
        return {'accessed_date': row['modified'], 'bytes_received': len(base64.b64decode(row['body'])),
                'created_date': row['created'], 'id': FILES.index(name) + 1,
                'link': f'/pran/file/{FILES.index(name) + 1}', 'modified_date': row['modified'],
                'metadata': [], 'name': name, 'sku': SKU, 'user_id': user}

    def status(self, name, user):
        value = self.summary(name, user)
        del value['metadata'], value['sku']
        value.update(bytes_remaining=0, blocks_remaining=[], status='complete', content_type='application/xml')
        return value


def multipart(headers, body):
    """Read CMS metadata and file parts without interpreting uploaded XML."""
    content_type = headers.get('content-type', '')
    if not content_type.lower().startswith(('multipart/mixed;', 'multipart/form-data;')):
        raise ValueError('Expected CMS multipart upload')
    message = BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode('iso-8859-1') + body)
    if not message.is_multipart() or message.defects:
        raise ValueError('Malformed CMS multipart upload')
    files = {}
    for part in message.iter_parts():
        if part.defects or part.is_multipart() or part.get('Content-Transfer-Encoding'):
            raise ValueError('Unsupported multipart part')
        data = part.get_payload(decode=True)
        name = part.get_filename()
        if name is None:
            if part.get_param('name', header='content-disposition') != 'metadata':
                raise ValueError('Unknown CMS part')
            metadata = json.loads(data)
            if not isinstance(metadata, (dict, list)):
                raise ValueError('Invalid CMS metadata')
            continue  # Auth tokens and client metadata are not stored.
        if name not in FILES or name in files or not data or len(data) > LIMIT:
            raise ValueError('Invalid PRAN filename or size')
        files[name] = data
    if not files:
        raise ValueError('No PRAN file supplied')
    # Whole-file uploads only; do not report incomplete chunks as complete.
    if 'x-block-total-segments' in headers and headers['x-block-total-segments'] != '1':
        raise ValueError('Chunked PRAN uploads require assembly')
    return files


def response(backend, method, path, raw):
    normalized = '/' + path.lstrip('/') if path.startswith('//') else path
    parsed = urlsplit(normalized)
    route = parsed.path.rstrip('/')
    match = re.fullmatch(r'/pran/file/([^/]+)(?:/(status|metadata))?', route)
    if match and match[1] not in FILES and match[1] not in tuple(map(str, range(1, 9))):
        return None
    if route not in ('/pran/search', '/pran/file') and not match:
        return None
    allowed = ('POST',) if not match else ('GET',) if match[2] else ('GET', 'PUT', 'DELETE')
    if method not in allowed:
        return reply({'error': 'Method not allowed'}, 'PRAN.WrongMethod', '405 Method Not Allowed',
                     headers=[('Allow', ', '.join(allowed))])
    head, _, body = raw.partition(b'\r\n\r\n')
    headers = {}
    for line in head.split(b'\r\n')[1:]:
        key, value = line.split(b':', 1)
        headers[key.decode('iso-8859-1').lower()] = value.strip().decode('iso-8859-1')
    query = parse_qs(parsed.query, keep_blank_values=True)

    def parameter(name):
        values = query.get(name)
        if values is not None:
            if len(values) != 1 or name in headers and headers[name] != values[0]:
                raise ValueError('Ambiguous CMS parameter')
            return values[0]
        return headers.get(name, '')

    try:
        auth = parameter('auth')
        # Captured PS3 requests distinguish the hospitality service SKU from
        # the game content SKU; neither field substitutes for the other.
        sku, content_sku = parameter('sku'), parameter('content_sku')
        if sku != SKU or content_sku not in ('', CONTENT_SKU) or parameter('apikey') != 'local-pran':
            return reply({'error': 'Local PRAN configuration required'}, 'PRAN.InvalidConfig', '400 Bad Request')
        reference_type = parameter('type')
        if match and (reference_type not in ('', 'name', 'id') or
                      reference_type == 'name' and match[1] not in FILES or
                      reference_type == 'id' and match[1] in FILES):
            raise ValueError('Invalid CMS file reference type')
        with backend.lock:
            session = next((s for s in backend.sessions.values() if s.postauth and auth and
                            any(token and secrets.compare_digest(auth.encode(), token.encode())
                                for token in (s.web_key, s.web_token))), None)
            if session is None:
                return reply({'error': 'Local EASW login required'}, 'PRAN.LoginRequired', '401 Unauthorized')
            user = str(session.account)
            owner = parameter('user')
            if owner and owner not in (user, str(session.persona)):
                return reply({'error': 'Local profile required'}, 'PRAN.InvalidOwner', '403 Forbidden')
            backend = backend.for_session(session)
            store = backend.pran
            if route == '/pran/search':
                criteria = json.loads(body)
                if not isinstance(criteria, dict) or set(criteria) - {'filename', 'user', 'sku', 'metadata', 'orderby', 'content_type', 'created_date', 'last_accessed', 'last_modified'}:
                    raise ValueError('Unsupported CMS search')
                for key, permitted in (('user', {user, str(session.persona)}), ('sku', {SKU})):
                    values = criteria.get(key, [])
                    if not isinstance(values, list) or any(v not in permitted for v in values):
                        raise ValueError('Nonlocal CMS search')
                names = criteria.get('filename', list(FILES))
                if not isinstance(names, list) or any(name not in FILES for name in names):
                    raise ValueError('Unsupported CMS filenames')
                found = [store.summary(name, user) for name in FILES if name in names and name in store.files]
                return reply({'count': len(found), 'files': found}, 'PRAN.Search')
            if route == '/pran/file':
                files = multipart(headers, body)
                for name, content in files.items():
                    store.put(name, content)
                store.save()
                # The native publish continuation at 0x19f2208 requires 201.
                return reply({'files': [store.status(name, user) for name in files]}, 'PRAN.Publish', '201 Created')
            name = match[1] if match[1] in FILES else FILES[int(match[1]) - 1]
            if name not in store.files:
                return reply({'error': 'Local file not found'}, 'PRAN.FileMissing', '404 Not Found')
            if match[2] == 'status':
                return reply(store.status(name, user), 'PRAN.Status')
            if match[2] == 'metadata':
                return reply({'metadata': []}, 'PRAN.Metadata')
            if method == 'GET':
                return reply(base64.b64decode(store.files[name]['body']), 'PRAN.Download', mime='application/xml')
            if method == 'DELETE':
                del store.files[name]
                store.save()
                return reply(b'', 'PRAN.Delete', '204 No Content')
            # UpdateContent uses a multipart upload or the raw XML file body.
            files = multipart(headers, body) if headers.get('content-type', '').lower().startswith('multipart/') else {name: body}
            if set(files) != {name} or not body or len(files[name]) > LIMIT:
                raise ValueError('Invalid PRAN update')
            store.put(name, files[name])
            store.save()
            return reply(store.status(name, user), 'PRAN.Update')
    except (ValueError, UnicodeError, TypeError):
        return reply({'error': 'Invalid local profile request'}, 'PRAN.InvalidRequest', '400 Bad Request')
