"""Local FUT entry services recovered from the BLES02258 v1.02 parsers.

This establishes club metadata and a session. Local packs and player inventory
are provided by fut_inventory; match services remain separate work.
"""
from datetime import datetime, timezone
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from pow_services import json_reply

ACCOUNT = '/ut/game/fifa19/user/accountinfo'
FRIENDS = re.compile(r'/personas/([0-9]{1,19})/buddies/easwfriends;full')
SETTINGS = '/ut/game/fifa19/settings'
TRUSTED_DEVICE = '/ut/game/fifa19/phishing/trusteddevice'
QUESTION = '/ut/game/fifa19/phishing/question'
VALIDATE = '/ut/game/fifa19/phishing/validate'
LOGOUT = '/ut/delete/auth'
MATCH_RESET = '/ut/game/fifa19/match/reset'
MASS_INFO = '/ut/game/fifa19/userMassInfo'
ACTIVE_SQUAD = '/ut/game/fifa19/squad/active'
USER_INFO = '/ut/game/fifa19/user'
SESSION_ROUTES = {SETTINGS: 'GET', TRUSTED_DEVICE: 'GET', QUESTION: 'GET',
                  VALIDATE: 'POST', LOGOUT: 'POST', MATCH_RESET: 'PUT', MASS_INFO: 'GET',
                  ACTIVE_SQUAD: 'GET', USER_INFO: 'GET'}

# 0x16b098c reads a configs array whose type is a string and value an integer.
# These are local service choices. Unspecified values keep native defaults.
SETTINGS_VALUES = {
    'getOperationTimeoutSec': 30, 'extendGameSessionTimerSec': 480,
    'maximumTradePileSize': 30, 'returningUserRewardsScreenEnabled': 0,
    'enableLiveMessaging': 0, 'processingStateEnabled': 0, 'tFAEnabled': 0,
    'tradingEnabled': 0, 'friendlySeasonsEnabled': 0, 'enableDraftMode': 0,
    'enableOfflineDraftMode': 0, 'storeEnabled': 1, 'storeEnabled_JP': 1,
    'coinEnabled': 1, 'coinEnabled_JP': 1,
    'cardPackStoreEnabled': 1, 'cardPackStoreEnabled_JP': 1,
    'fifaPointsEnabled': 0, 'fifaPointsEnabled_JP': 0,
    'mtxEnabled': 0, 'mtxEnabled_JP': 0, 'pointsPackStoreEnabled': 0,
}


def startup_settings():
    return {'configs': [{'type': name, 'value': value}
                        for name, value in SETTINGS_VALUES.items()]}


def trusted_device():
    # The login UI at 0xbcc39c checks tfaData.tfaState before its legacy
    # question flow. A true top-level trusted flag alone does not finish it.
    return {'trusted': True, 'changed': False, 'exists': False, 'locked': False,
            'tfaData': {'tfaState': 'TRUSTED'}}


def active_squad(backend, session):
    # LoadActiveSquad delegates to RetrieveSquad's result parser (0x16dc560),
    # which reads a top-level squad object, not a {squad: ...} envelope.
    # Share the persisted owned-card lineup with userMassInfo's squad section.
    backend = backend.for_session(session)
    from fut_squads import retrieve
    return retrieve(backend, session)


def user_info(backend, session=None):
    """Shared direct GetUserInfo and bulk-bootstrap club representation."""
    from fut_club_assets import active_assets
    from fut_squads import display_name, abbreviation
    if session is not None:
        backend = backend.for_session(session)
    with backend.lock:
        from fut_seasons import renew_completed_season, active_division
        renew_completed_season(backend)
        club = backend.profile['fut_club']
        inventory = backend.fut_inventory
        reports = [row['summary'] for row in inventory.state.get('matchHistory', [])]
        name = display_name(backend, session)
        return {
            # Retail user-info parser 0x16abfc4 stores this at +0x18;
            # SDK setter 0x168fda0 copies it to the own-persona cache.
            # RetrieveSquad routes other persona IDs into the opponent cache.
            'personaId': session.persona if session else backend.profile['persona'],
            'clubName': name, 'clubAbbr': abbreviation(name),
            'established': str(club['established']),
            'accountCreatedPlatformName': 'ps3', 'clubNameChangeAllowed': False,
            'sessionCoinsBankBalance': inventory.state['coins'],
            'divisionOffline': active_division(backend), 'divisionOnline': 1, **inventory.currencies(),
            'won': sum(r['endReason'] in ('WIN', 'DNF_WIN') for r in reports),
            'draw': sum(r['endReason'] in ('DRAW', 'DNF_DRAW') for r in reports),
            'loss': sum(r['endReason'] in ('LOSS', 'DNF_LOSS', 'DNF', 'QUIT') for r in reports),
            'trophies': sum(row['championships'] for row in inventory.state.get('offlineTournaments',{}).values()), 'purchased': False, 'actives': active_assets(backend)}


def user_mass_info(backend, session):
    """Local club bootstrap; native composite parser at 0x16c9110.

    Include the empty collections so native caches complete their loading
    callbacks. Stored pack cards load into purchasedItems; the same saved
    owned-card lineup is used here and by the individual squad routes.
    """
    backend = backend.for_session(session)
    with backend.lock:
        backend.save()
        inventory = backend.fut_inventory
        inventory.ensure_saved()
        value = {
            'settings': startup_settings(),
            'userInfo': user_info(backend, session),
            'userData': {'onlineELORating': 0, 'onlineRatedUser': False,
                         'accountResetCount': 0},
            'hub': inventory.hub(),
            'squad': active_squad(backend, session),
            'purchasedItems': inventory.purchased(),
            'clubUser': {'user': []}, 'loanPlayers': [],
            'pileSizeClientData': {'entries': []},
            'tutorialClientData': {'entries': []},
            'userHubClientData': {'entries': [dict(e) for e in inventory.state['userHubData']]},
            'loanPlayerClientData': {'entries': []}, 'squadBuildingSetsClientData': {'entries': []}}
        return value


def request_headers(head):
    headers = {}
    for line in head.split(b'\r\n')[1:]:
        key, separator, value = line.partition(b':')
        if separator:
            headers[key.decode('iso-8859-1').lower()] = value.strip().decode('iso-8859-1')
    return headers


def session_for(backend, headers):
    sid = headers.get('x-ut-sid', '')
    with backend.lock:
        if backend.root.recovery:backend.root.recovery.purge()
        session = next((s for s in backend.sessions.values() if s.postauth and s.fut_key
                        and secrets.compare_digest(s.fut_key.encode(), sid.encode())), None)
        if session and backend.root.recovery:backend.root.recovery.touch(session)
        return session


def account_info(backend, session):
    backend = backend.for_session(session)
    with backend.lock:
        # Persist metadata on first access so establishment time survives restarts.
        backend.save()
        from fut_seasons import renew_completed_season, active_division
        renew_completed_season(backend)
        club = backend.profile['fut_club']
        from fut_squads import display_name
        return {'userAccountInfo': {'personas': [{
            'personaId': session.persona, 'personaName': session.name,
            'returningUser': 0, 'userClubList': [{
                'year': '2019', 'platform': 'ps3', 'clubName': display_name(backend, session),
                'established': club['established'], 'assetId': club['asset_id']}]}]}}


def authenticate(backend, payload):
    identification = payload.get('identification') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('sku') != 'FFA19PS3'
            or payload.get('method') != 'authcode'
            or payload.get('nucleusPersonaPlatform') != 'ps3'
            or type(payload.get('clientVersion')) is not int or payload['clientVersion'] != 3
            or type(payload.get('isReadOnly')) is not bool
            or type(payload.get('nuc')) is not int
            or type(payload.get('nucleusPersonaId')) is not int
            or not isinstance(identification, dict)
            or identification.get('redirectUrl') != 'nucleus:rest'
            or not isinstance(identification.get('authCode'), str)):
        return None
    with backend.lock:
        # The executable's persona accessor returns zero when the local PSN
        # persona object is unavailable (0x169d990). Account + a FUT-purpose
        # code still bind that alias to exactly one authenticated local player.
        if backend.root.recovery:backend.root.recovery.purge()
        active = [s for s in backend.sessions.values() if s.postauth
                  and s.account == payload['nuc']
                  and payload['nucleusPersonaId'] in (0, s.persona)]
        if len(active) != 1 or not backend.identity.consume(identification['authCode'], 'fut'):
            return None
        session = active[0]
        if not session.fut_key:
            session.fut_key = secrets.token_hex(16)
            from server_plugins import entered_fut
            owner = backend.for_session(session)
            entered_fut(owner, session)
        # FUT calls the same WebSession JSON parser as POW at 0x1500b8c.
        fmt = '%Y-%m-%dT%H:%M:%SZ'
        return {'sid': session.fut_key,
                'serverTime': datetime.now(timezone.utc).strftime(fmt),
                'lastOnlineTime': datetime.fromtimestamp(session.login_time, timezone.utc).strftime(fmt)}


def response(backend, method, path, raw):
    parsed = urlsplit(path)
    route = parsed.path
    friends = FRIENDS.fullmatch(route)
    auth = route == '/ut/auth'
    if route != ACCOUNT and not friends and not auth and route not in SESSION_ROUTES:
        return None
    wanted = SESSION_ROUTES.get(route, 'POST' if auth else 'GET')
    if method != wanted:
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', wanted)])
    head, _, body = raw.partition(b'\r\n\r\n')
    if auth:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return json_reply({'error': 'Invalid JSON'}, 'FUT.InvalidAuthRequest', '400 Bad Request')
        value = authenticate(backend, payload)
        if value is None:
            return json_reply({'error': 'Local FUT login required'}, 'FUT.LoginRequired', '401 Unauthorized')
        return json_reply(value, 'FUT.Authentication', headers=[('X-UT-SID', value['sid'])])
    headers = request_headers(head)
    if route in SESSION_ROUTES:
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
        except ValueError:
            query = None
        valid_query = not parsed.query
        if route == ACTIVE_SQUAD and query is not None:
            # Captured after purchase in 20261010T060152Z; native URL builder
            # 0x16dc3f4 appends /active?active=true.
            valid_query = not parsed.query or query == {'active': ['true']}
        if route in (TRUSTED_DEVICE, QUESTION, VALIDATE) and query is not None:
            device = query.get('deviceId', [])
            valid_query = (len(device) == 1 and 1 <= len(device[0]) <= 1024)
            if route == TRUSTED_DEVICE:
                valid_query = (valid_query and set(query) <= {'deviceId', 'tfaResend'}
                               and query.get('tfaResend', ['true']) == ['true'])
            elif route == QUESTION:
                valid_query = valid_query and set(query) == {'deviceId'}
            else:
                answer = query.get('answer', [])
                valid_query = (valid_query and set(query) == {'deviceId', 'answer'}
                               and len(answer) == 1
                               and re.fullmatch(r'[0-9a-fA-F]{32}', answer[0]) is not None)
        if body or not valid_query:
            return json_reply({'error': 'Invalid request'}, 'FUT.InvalidStartupRequest', '400 Bad Request')
        with backend.lock:
            session = session_for(backend, headers)
            if session is None:
                return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
            backend = backend.for_session(session)
            if route == SETTINGS:
                return json_reply(startup_settings(), 'FUT.Settings')
            if route == TRUSTED_DEVICE:
                # These flags apply only to the issued local FUT session, not
                # a remote EA account/device. Native parser: 0x16c80e0.
                return json_reply(trusted_device(), 'FUT.LocalTrustedDevice')
            if route == QUESTION:
                # Native 0x16c4cf8 reads these three integer fields. This is a
                # compatibility reply for a stale legacy screen; fresh login
                # uses the explicit local TRUSTED state instead.
                return json_reply({'question': 0, 'attempts': 0, 'recoverAttempts': 0},
                                  'FUT.LocalQuestion')
            if route == VALIDATE:
                # The player is already authenticated by local OAuth + SID.
                # This acknowledges the legacy screen, not an EA credential
                # check. Do not retain or echo the submitted answer digest.
                # Vtable 0x2076070 +0x30 uses 0x16a3690 -> 0x16a4470:
                # HTTP 200 maps to result code zero without reading JSON.
                return json_reply({}, 'FUT.LocalQuestionAcknowledgement')
            if route == MATCH_RESET:
                # Native 0x16d22fc reads a boolean reset. Drop a pending local
                # offline handshake; resetting an idle session is idempotent.
                from fut_matches import reset
                reset(session)
                return json_reply({'reset': True}, 'FUT.LocalMatchReset')
            if route == MASS_INFO:
                return json_reply(user_mass_info(backend, session), 'FUT.UserMassInfo')
            if route == USER_INFO:
                # GetUserInfo result 0x16c89c0 passes the root object to
                # 0x16abd10. It does not expect a userInfo envelope.
                return json_reply(user_info(backend, session), 'FUT.UserInfo')
            if route == ACTIVE_SQUAD:
                return json_reply(active_squad(backend, session), 'FUT.ActiveSquad')
            from fut_matches import reset
            reset(session)
            from server_plugins import left_fut
            if backend.root.recovery:backend.root.recovery.forget(session.key)
            left_fut(backend.for_session(session), session)
            return json_reply({}, 'FUT.Logout')
    if body or parsed.query:
        return json_reply({'error': 'Invalid request'}, 'FUT.InvalidEntryRequest', '400 Bad Request')
    with backend.lock:
        if backend.root.recovery:backend.root.recovery.purge()
        active = [s for s in backend.sessions.values() if s.postauth and s.web_token]
        if route == ACCOUNT:
            active = [s for s in active
                      if headers.get('easw-session-data-nucleus-id') == str(s.account)]
        else:
            # Startup currently requests persona 0; accept that alias only for
            # the one active local session presenting its issued EASW token.
            active = [s for s in active if int(friends[1]) in (0, s.persona)
                      and secrets.compare_digest(s.web_token.encode(),
                                                 headers.get('easw-token', '').encode())]
        if len(active) != 1:
            return json_reply({'error': 'Local login required'}, 'FUT.LoginRequired', '401 Unauthorized')
        if route == ACCOUNT:
            return json_reply(account_info(backend, active[0]), 'FUT.AccountInfo')
        # 0x308f48 searches buddies.buddylist.buddy; zero entries completes
        # the success callback without a second user-info lookup (0x30a63c).
        return ('200 OK', b'<?xml version="1.0"?><buddies><buddylist/></buddies>',
                'text/xml', [('Cache-Control', 'no-store')], 'EASW.EmptyFriends')
