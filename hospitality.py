"""Local web services requested during FIFA 19 PS3 online startup.

Parser evidence is in research/hospitality-disassembly.txt. These services
offer no downloadable database or live configuration changes.
"""
from urllib.parse import urlsplit
import re
import secrets
import xml.etree.ElementTree as ET

SCHEMA = 'b31c71206ef9d001cc828afb6a025a9ccb6aa1bc'
ROSTER_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<squad><squadInfoSet><squadInfo platform="ps3">
<dbSchemaCRC>{SCHEMA}</dbSchemaCRC>
<dbMajor>1</dbMajor><dbMinor>1</dbMinor>
<dbMajorCRC>0</dbMajorCRC><dbMinorCRC>0</dbMinorCRC>
<dbMajorLoc/><dbMinorLoc/>
<dbFUTVer>0</dbFUTVer><dbFUTCRC>0</dbFUTCRC><dbFUTLoc/>
</squadInfo></squadInfoSet></squad>'''.encode()
# The routing parser reads these root attributes and optional <file> entries.
# An empty list keeps the game's packaged file locations.
ROUTING_XML = b'<?xml version="1.0" encoding="UTF-8"?><routing fileVersion="1" refresh="0"/>'
MATCH_RESULTS = re.compile(r'/personas/([0-9]+)/sku/FFA19PS3/match_results')
CLIENT_EXTENSION = re.compile(r'/client/([0-9a-f]{32})/extension')


def public_path(path):
    """Keep bootstrap bearer routes out of console and event summaries."""
    path = urlsplit(path).path
    return '/client/<redacted>/extension' if path.startswith('/client/') else path


def match_results(backend, method, path, raw, route):
    headers = [('Cache-Control', 'no-store')]
    if method != 'POST':
        return '405 Method Not Allowed', b'', 'text/plain', headers+[('Allow', 'POST')], 'EASW.WrongMethod'
    head, _, body = raw.partition(b'\r\n\r\n')
    fields = {}
    for line in head.split(b'\r\n')[1:]:
        name, _, value = line.partition(b':')
        fields[name.decode('iso-8859-1').lower()] = value.strip().decode('iso-8859-1')
    token = fields.get('easw-token', '')
    with backend.lock:
        session = next((s for s in backend.sessions.values() if s.postauth and s.web_token and token
                        and secrets.compare_digest(s.web_token.encode(), token.encode())
                        and int(route[1]) in (0, s.persona)), None)
        if session is None:
            return '401 Unauthorized', b'', 'text/plain', headers, 'EASW.LoginRequired'
        current = getattr(session, 'local_fut_match', None)
        if not current or not current.get('playing'):
            return '409 Conflict', b'', 'text/plain', headers, 'EASW.NoLocalMatch'
        try:
            if not body or len(body) > 16384 or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
                raise ValueError('Invalid report XML')
            report = ET.fromstring(body)
            if (report.tag != 'create_match_results' or len(list(report.iter())) > 128
                    or report.findtext('mode') != 'Kick Off' or len(report.findall('team')) != 2
                    or report.find('gameplay_mode') is None):
                raise ValueError('Unsupported local match report')
            for team in report.findall('team'):
                score = team.findtext('score', '')
                if not score.isascii() or not score.isdigit() or not 0 <= int(score) <= 65535:
                    raise ValueError('Invalid score')
        except (ET.ParseError, ValueError):
            return '400 Bad Request', b'', 'text/plain', headers, 'EASW.InvalidMatchReport'
        # Retail 0x30c438 calls the optional credit/notification parser
        # 0x300ec0. An empty widget acknowledges this separate EASW report;
        # FUT match/end owns the durable result and coin award.
        return '200 OK', b'<?xml version="1.0" encoding="UTF-8"?><widget-full/>', 'application/xml', headers, 'EASW.LocalMatchReport'


def response(backend, method, path, raw, peer=None):
    """Return (status, body, MIME, headers, handler), or None for unknown routes."""
    path = urlsplit(path).path
    client_route = None
    if path.startswith('/client/'):
        match = CLIENT_EXTENSION.fullmatch(path)
        if not match:
            return '401 Unauthorized', b'', 'text/plain', [('Cache-Control','no-store')], 'EASW.LoginRequired'
        client_route, path = match[1], '/extension'
    if route := MATCH_RESULTS.fullmatch(path):
        return match_results(backend, method, path, raw, route)
    methods = {'/extension': 'POST', '/roster': 'GET', '/ctl': 'GET', '/routing': 'GET'}
    if path not in methods:
        return None
    headers = [('Cache-Control', 'no-store')]
    if method != methods[path]:
        return '405 Method Not Allowed', b'', 'text/plain', headers + [('Allow', methods[path])], 'Hospitality.WrongMethod'
    if path == '/extension':
        # Retail sends no account identifier here. Match the HTTP peer to
        # exactly one active Blaze login; ambiguous peers fail closed.
        if raw.partition(b'\r\n\r\n')[2]:
            return '400 Bad Request', b'', 'text/plain', headers, 'EASW.InvalidExtension'
        credentials = backend.extend_web_session(peer if backend.root.multiuser or client_route else None,
                                                 client_route)
        if credentials is None:
            return '401 Unauthorized', b'', 'text/plain', headers, 'EASW.LoginRequired'
        session, token = credentials
        headers += [('EASW-Session', session), ('EASW-Token', token)]
        return '200 OK', b'<?xml version="1.0" encoding="UTF-8"?><result/>', 'text/xml', headers, 'EASW.Extension'
    if path == '/roster':
        return '200 OK', ROSTER_XML, 'application/xml', headers, 'Roster.LocalVersion'
    if path == '/routing':
        return '200 OK', ROUTING_XML, 'application/xml', headers, 'Routing.PackagedFiles'
    # LiveCTLConfigUpdater::ApplyNewCTLConfig explicitly ignores an empty
    # download (0x5142e8). Do not supply unrelated XML as a CTL configuration.
    return '200 OK', b'', 'application/octet-stream', headers, 'CTL.NoOverrides'
