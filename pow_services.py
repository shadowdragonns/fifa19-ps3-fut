"""PS3 EA Sports Football Club login, startup data and supported-club choice.

Local POW sessions are bound to the existing Blaze/EASW login. Compiled
parser evidence and route names are recorded in research/pow-findings.json.
This module does not implement FUT authentication, packs or matches.
"""
from datetime import datetime, timezone
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

# The PS3 searches the returned catalogue names byte-for-byte at 0x253450.
# These IDs belong only to the local store and contain no purchasable items.
CATALOGS = ((1, 'EASFC Store FIFA19 PS3'), (2, 'Sony Shared FIFA 19'))


def json_reply(value, handler, status='200 OK', headers=()):
    return (status, json.dumps(value, separators=(',', ':'), allow_nan=False).encode(),
            'application/json', [('Cache-Control', 'no-store'), *headers], handler)


def authenticate(backend, payload):
    """Validate local EASW credentials and return a session-scoped POW ID."""
    if not isinstance(payload, dict):
        return None
    identification = payload.get('identification')
    if (not isinstance(identification, dict) or payload.get('sku') != 'FFA19PS3'
            or payload.get('method') != 'cas' or type(payload.get('clientVersion')) is not int
            or payload['clientVersion'] != 1 or type(payload.get('isReadOnly')) is not bool
            or type(payload.get('nuc')) is not int or type(payload.get('nucleusPersonaId')) is not int):
        return None
    key, token = identification.get('EASW-Session'), identification.get('EASW-Token')
    if not isinstance(key, str) or not key or not isinstance(token, str) or not token:
        return None
    with backend.lock:
        for session in backend.sessions.values():
            if (session.postauth and session.web_key and session.web_token
                    and secrets.compare_digest(session.web_key.encode(), key.encode())
                    and secrets.compare_digest(session.web_token.encode(), token.encode())
                    and session.account == payload['nuc']
                    and session.persona == payload['nucleusPersonaId']):
                if not session.pow_key:
                    session.pow_key = secrets.token_hex(16)
                now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                last = datetime.fromtimestamp(session.login_time, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                # PS3 parses year/month/day/hour/minute/second at offsets
                # 0, 5, 8, 11, 14, 17. Epoch numbers are incompatible.
                return {'sid': session.pow_key, 'serverTime': now, 'lastOnlineTime': last}
    return None


def session_for(backend, headers):
    sid = headers.get('x-pow-sid', '')
    if not sid:
        return None
    with backend.lock:
        return next((s for s in backend.sessions.values()
                     if s.postauth and s.pow_key
                     and secrets.compare_digest(s.pow_key.encode(), sid.encode())), None)


def currency_cap():
    return [{'period': period, 'fundsEarned': 0} for period in ('daily', 'weekly')]


def profile():
    # Field types and names follow the PS3 UserLevel parser at 0x21e448.
    return {'level': 1, 'levelUp': False, 'xp': 0, 'xpCapCurrLevel': 0,
            'xpCapNextLevel': 1000, 'xpGained': 0, 'xpLoyalty': 0,
            'funds': [], 'notifications': []}


def startup_data(path, session=None, supported_club=0):
    """Select a finite set of recovered read-only routes; unknowns stay unknown."""
    if path == '/pow/bank/user/account':
        return {'currencies': [{'currency': 'pow_funds', 'funds': 0,
                                 'fundsCapInfo': currency_cap()}]}, 'POW.CurrencyAccount'
    if path == '/pow/bank/currency/pow_funds/cap/info':
        # This endpoint has a different parser from CurrencyAccount: period
        # and fundsCap belong directly in each currencies entry (0x2535a8).
        return {'currencies': [{'currency': 'pow_funds', 'period': period,
                                 'fundsCap': cap}
                                for period, cap in (('daily', 10000), ('weekly', 70000))]}, 'POW.CurrencyCap'
    if path == '/pow/healthcheck/system/all':
        # The request at 0x22d4b0 supplies no response listener. The HTTP
        # dispatcher at 0x1500ef4 therefore does not deserialize this body.
        return {}, 'POW.HealthCheck'
    if path == '/pow/inventory/item/list':
        return {'items': []}, 'POW.UserItems'
    if path == '/pow/mm/game/fifa19/message/list':
        return {'messageList': [], 'endOfList': True}, 'POW.UserMessages'
    if path == '/pow/user/friends':
        # The captured request is a POST with personaList: []. Its success
        # callback at 0x220b88 records completion without reading the body.
        return {}, 'POW.Friends'
    if path == '/pow/pfyc/user':
        # PFYCUserCacheData (0x24d540), not the /pfyc/club statistics parser.
        # A fresh local account has no supported club; IDs refer to this
        # session's nucleus account, not its persona or any RPCN friend.
        users = [] if session is None else [{'nucId': session.account, 'clubId': supported_club,
                    'pendingClubId': -1, 'numChangesAllowed': 1,
                    'leagueId': -1, 'globalLeagueId': -1}]
        return {'users': users}, 'POW.SupportedClub'
    if path == '/pow/pfyc/user/club':
        return {'userSupportedClub': {'clubId': supported_club,
                    'pendingClubId': -1, 'changesAllowed': 1}}, 'POW.SaveSupportedClub'
    if re.fullmatch(r'/pow/pfyc/[0-9]+/info', path):
        return {'club': {'leagueId': -1, 'prevLeagueId': -1,
                        'seasonRanking': 0, 'overallRanking': 0,
                        'numChangesAllowed': 1, 'numFans': 0, 'numMinFans': 0,
                        'numNewFans': 0, 'numPromotions': 0, 'numRelegations': 0,
                        'numRunnerUp': 0, 'numTopHalf': 0, 'numChampion': 0}}, 'POW.ClubInfo'
    if path == '/pow/lvl/user/tiergp/businessunit/tiertp/fifa':
        return profile(), 'POW.UserLevel'
    if path == '/pow/lvl/weight/tiergp/businessunit/tiertp/fifa':
        # LevelWeightCacheData requires a matching entry, including a numeric
        # game weight. Its parser rejects an empty tierweights array.
        return {'tierweights': [{'tiergroup': 'businessunit', 'tiername': 'fifa',
                                  'weights': [{'gametitle': 'fifa19', 'weight': 1.0}]}]}, 'POW.LevelWeight'
    if path in ('/pow/news/count/unread', '/pow/communication/all/countUnread'):
        return {'unreadCount': 0}, 'POW.UnreadCount'
    if path == '/pow/news/user':
        return {'news': [], 'endOfList': True}, 'POW.News'
    if path == '/pow/store/game/fifa19/catalog/list':
        return {'catalogs': [{'catalogId': ident, 'name': name}
                             for ident, name in CATALOGS]}, 'POW.CatalogList'
    if re.fullmatch(r'/pow/store/game/fifa19/catalog/[0-9]+/item/list', path):
        # This parser ignores endOfList. Native paging stops on a short page
        # after each named catalogue has been visited (0x250908).
        return {'items': []}, 'POW.CatalogItems'
    return None


def response(backend, method, path, raw):
    # ProtoHttp may send double leading slashes or an absolute HTTP URL.
    normalized = '/' + path.lstrip('/') if path.startswith('//') else path
    parsed = urlsplit(normalized)
    public_path = parsed.path.rstrip('/')
    is_auth = public_path in ('/auth', '/pow/auth')
    is_friends = public_path == '/pow/user/friends'
    is_save_club = public_path == '/pow/pfyc/user/club'
    data = None if is_auth else startup_data(public_path)
    if not is_auth and data is None:
        return None
    wanted_method = 'POST' if is_auth or is_friends or is_save_club else 'GET'
    if method != wanted_method:
        return json_reply({'error': 'Method not allowed'}, 'POW.WrongMethod',
                          '405 Method Not Allowed', [('Allow', wanted_method)])
    head, _, body = raw.partition(b'\r\n\r\n')
    if is_auth:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return json_reply({'error': 'Invalid JSON'}, 'POW.InvalidAuthRequest', '400 Bad Request')
        result = authenticate(backend, payload)
        if result is None:
            return json_reply({'error': 'Local login required'}, 'POW.LoginRequired', '401 Unauthorized')
        return json_reply(result, 'POW.Authentication', headers=[('X-POW-SID', result['sid'])])
    headers = {}
    for line in head.split(b'\r\n')[1:]:
        key, value = line.split(b':', 1)
        headers[key.decode('iso-8859-1').lower()] = value.strip().decode('iso-8859-1')
    session = session_for(backend, headers)
    if session is None:
        return json_reply({'error': 'Local POW session required'}, 'POW.LoginRequired', '401 Unauthorized')
    backend = backend.for_session(session)
    if is_friends:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = None
        personas = payload.get('personaList') if isinstance(payload, dict) else None
        if (not isinstance(personas, list) or len(personas) > 200
                or any(type(persona) is not int or persona <= 0 for persona in personas)):
            return json_reply({'error': 'Invalid persona list'}, 'POW.InvalidFriendsRequest', '400 Bad Request')
    if is_save_club:
        query = parse_qs(parsed.query, keep_blank_values=True)
        ids, immediate = query.get('id', []), query.get('immediate', [])
        if (body or set(query) != {'id', 'immediate'} or len(ids) != 1
                or not re.fullmatch(r'[0-9]{1,10}', ids[0])
                or not 0 < int(ids[0]) < 2**31 or immediate != ['true']):
            return json_reply({'error': 'Invalid supported club request'},
                              'POW.InvalidClubRequest', '400 Bad Request')
        with backend.lock:
            # Recheck after acquiring the lock: logout may race an HTTP POST.
            if backend.sessions.get(session.key) is not session:
                return json_reply({'error': 'Local POW session required'},
                                  'POW.LoginRequired', '401 Unauthorized')
            backend.profile['pow_supported_club'] = int(ids[0])
            backend.save()
    with backend.lock:
        club = backend.profile['pow_supported_club']
    value, handler = startup_data(public_path, session, club)
    return json_reply(value, handler)
