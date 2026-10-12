"""Local PS3 login/session services, using the executable's TDF schemas.

RPCN identities select separate saved clubs on this replacement backend.
Session credentials stay in memory; fut_inventory persists coins and cards.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import re
import threading
import time

from identity import LocalIdentity
from fut_inventory import Inventory
from localization import Localizer
from pran import ProfileStore
import tdf

LOCALE = int.from_bytes(b'enGB', 'big')
MAIL = 'player@fifa19.local'
CREATED = '2026-10-09T00:00:00Z'


class Backend:
    def __init__(self, profile_path=None, endpoints=None, multiuser=False, settings=None, events=None):
        from endpoints import Endpoints
        from server_settings import DEFAULT
        from server_plugins import EventBus
        self.settings = settings or DEFAULT
        self.events = events or EventBus()
        self.endpoints = endpoints or Endpoints()
        self.identity = LocalIdentity(self.endpoints.http_port, self.endpoints.host)
        self.localizer = Localizer()
        self.lock = threading.RLock()
        self.path = Path(profile_path) if profile_path else None
        self.sessions = {}
        self.presence_last_seen = {}
        self.client_routes = {}
        from matchmaking import MatchmakingQueue
        self.matchmaking = MatchmakingQueue(settings=self.settings)
        self.root = self
        self.multiuser = multiuser
        self.users = {}
        self.accounts_path = self.path.with_name('accounts.json') if self.path else None
        self.accounts = {'version': 1, 'users': {}}
        if multiuser and self.accounts_path and self.accounts_path.exists():
            self.accounts = json.loads(self.accounts_path.read_text())
            from accounts import validate_registry
            validate_registry(self.accounts)
        self.pran = ProfileStore(self.path.with_name('pran.json') if self.path else None)
        self.fut_inventory = Inventory(self.path.with_name('fut-inventory.json') if self.path else None,
                                       self.lock, self.settings)
        self.profile = dict(persona=1000001, account=1100000001,
                            name='Local Player', settings={}, telemetry_opt=0,
                            pow_supported_club=0)
        self.profile['fut_club'] = dict(name='England FC', established=int(time.time()), asset_id=0)
        if self.path and self.path.exists():
            self.profile.update(json.loads(self.path.read_text()))
        p = self.profile
        if (any(type(p[k]) is not int or not 0 < p[k] < 2**63 for k in ('persona', 'account'))
                or not isinstance(p['name'], str) or not 1 <= len(p['name']) <= 32
                or not isinstance(p['settings'], dict)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in p['settings'].items())
                or p['telemetry_opt'] not in (0, 1)
                or type(p['pow_supported_club']) is not int
                or not 0 <= p['pow_supported_club'] < 2**31):
            raise ValueError('Invalid local account profile')
        if 'last_login' in p and (type(p['last_login']) is not int or not 0 <= p['last_login'] < 2**63):
            raise ValueError('Invalid saved login time')
        if 'display_name' in p and (not isinstance(p['display_name'], str) or not 1 <= len(p['display_name']) <= 32):
            raise ValueError('Invalid saved display name')
        club = self.profile['fut_club']
        if (not isinstance(club, dict) or not isinstance(club.get('name'), str)
                or not 1 <= len(club['name']) <= 32
                or any(type(club.get(k)) is not int or not 0 <= club[k] < 2**32
                       for k in ('established', 'asset_id'))):
            raise ValueError('Invalid local FUT club metadata')
        from server_plugins import player
        self.fut_inventory.events = self.events
        self.fut_inventory.player = lambda: player(self)
        if self.events._backend is None:
            self.events.bind_backend(self)

    def save(self):
        with self.lock:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.profile, indent=2) + '\n')
                temporary.replace(self.path)

    def for_session(self, session):
        return session.owner or self

    def for_user(self, external):
        from accounts import user_backend
        return user_backend(self.root, external) if self.root.multiuser else self

    def close_sessions(self, reason='server_shutdown'):
        from server_plugins import went_offline
        root = self.root
        with root.lock:
            for session in list(root.sessions.values()):
                root.matchmaking.remove(session)
                root.sessions.pop(session.key, None)
                went_offline(root.for_session(session), session, reason)

    def extend_web_session(self, peer=None, client_route=None):
        with self.lock:
            if client_route is not None:
                connection = self.root.client_routes.get(client_route)
                session = connection.session if connection else None
                active = [session] if (session and session.postauth and session.peer == peer
                    and self.root.sessions.get(session.key) is session) else []
            else:
                active = [s for s in self.sessions.values() if s.postauth
                          and (peer is None or s.peer == peer)]
            if len(active) != 1:
                return None
            session = active[0]
            if not session.web_key:
                session.web_key = secrets.token_hex(16)
                session.web_token = secrets.token_hex(16)
            return session.web_key, session.web_token


@dataclass
class Session:
    persona: int
    account: int
    name: str
    key: str
    external: int
    login_time: int
    postauth: bool = False
    web_key: str = ''
    web_token: str = ''
    pow_key: str = ''
    fut_key: str = ''
    owner: object = None
    peer: str = ''
    np_blob: bytes = b''
    presence_online: bool = False
    presence_fut: bool = False


class Connection:
    """Authenticated state is scoped to one Blaze connection."""
    def __init__(self, backend, peer=''):
        self.backend = backend
        self.root = backend.root
        self.peer = peer
        self.session = None
        self.client_route = ''
        self.room_updates = 0
        self.room_notifications = False

    def close(self, keep_client_route=False, reason='disconnect'):
        with self.root.lock:
            if self.client_route and not keep_client_route:
                self.root.client_routes.pop(self.client_route, None)
                self.client_route = ''
            if self.session:
                self.root.matchmaking.remove(self.session)
                self.backend.sessions.pop(self.session.key, None)
                from server_plugins import went_offline
                went_offline(self.backend, self.session, reason)
                self.session = None
                self.backend = self.root
            self.room_updates = 0
            self.room_notifications = False

    def poll_notifications(self):
        # Queries inside pairing hooks use the same root->queue lock order
        # as HTTP match actions. Never invoke a plugin holding only queue.lock.
        with self.root.lock:
            return self.root.matchmaking.poll(self.session) if self.session else []

    def response_sent(self, frame):
        if self.session and (frame['component'], frame['command']) == (4, 13):
            self.root.matchmaking.acknowledged(self.session)

    def persona_details(self):
        s = self.session
        # PersonaStatus ACTIVE=2, unlike AccountStatus ACTIVE=1.
        return (tdf.str_field('DSNM', s.name) + tdf.int_field('LAST', s.login_time)
                + tdf.int_field('PID', s.persona) + tdf.int_field('PLAT', 2)
                + tdf.str_field('PNAM', s.name) + tdf.int_field('STAS', 2)
                + tdf.int_field('XREF', s.external))

    def login_response(self):
        s = self.session
        info = (tdf.int_field('1CON', 0) + tdf.int_field('BUID', s.persona)
                + tdf.int_field('FRST', 0) + tdf.str_field('KEY', s.key)
                + tdf.int_field('LLOG', s.login_time) + tdf.str_field('MAIL', MAIL)
                + tdf.group('PDTL', self.persona_details()) + tdf.int_field('UID', s.account))
        return (tdf.int_field('ANON', 0) + tdf.int_field('NTOS', 0)
                + tdf.group('SESS', info) + tdf.int_field('SPAM', 1)
                + tdf.int_field('UNDR', 0))

    def extended_data(self):
        network = b''.join(tdf.int_field(k, v) for k, v in
                          [('BWHR', 0), ('DBPS', 0), ('NAHR', 0), ('NATT', 2), ('UBPS', 0)])
        return (tdf.str_field('BPS', 'local') + tdf.str_field('CTY', 'GB')
                + tdf.int_field('HWFG', 0) + tdf.int_list('PSLM', [0])
                + tdf.group('QDAT', network) + tdf.str_field('TZ', 'Europe/London')
                + tdf.int_field('UATT', 0))

    def notifications(self):
        s = self.session
        user = self.user_identification()
        info = (tdf.int_field('1CON', 0) + tdf.int_field('ALOC', LOCALE)
                + tdf.int_field('BUID', s.persona)
                + tdf.object_id('CGID', 30722, 2, s.persona)
                + tdf.str_field('DSNM', s.name) + tdf.int_field('FRST', 0)
                + tdf.str_field('KEY', s.key) + tdf.int_field('LAST', s.login_time)
                + tdf.int_field('LLOG', s.login_time) + tdf.str_field('MAIL', MAIL)
                + tdf.str_field('NASP', 'ps3') + tdf.int_field('PID', s.persona)
                + tdf.int_field('PLAT', 2) + tdf.str_field('PNAM', s.name)
                + tdf.int_field('UID', s.account) + tdf.int_field('USTP', 0)
                + tdf.int_field('XREF', s.external))
        data = tdf.group('DATA', self.extended_data())
        # IDs recovered from PS3 UserSessions::getNotificationName.
        return [(2, data + tdf.group('USER', user), 'UserAdded'),
                (8, info, 'UserAuthenticated'),
                (1, data + tdf.int_field('SUBS', 1) + tdf.int_field('USID', s.persona),
                 'UserSessionExtendedDataUpdate'),
                (5, tdf.int_field('FLGS', 3) + tdf.int_field('ID', s.persona), 'UserUpdated')]

    def user_identification(self):
        s = self.session
        return (tdf.int_field('AID', s.account) + tdf.int_field('ALOC', LOCALE)
                + tdf.str_field('DNAM', s.name) + tdf.blob_field('EXBB', s.np_blob)
                + tdf.int_field('EXID', s.external) + tdf.int_field('ID', s.persona)
                + tdf.str_field('NAME', s.name) + tdf.str_field('NASP', 'ps3')
                + tdf.int_field('ORIG', s.persona) + tdf.int_field('PIDI', s.account))

    def lookup_persona_names(self, request):
        namespace, names = request.get('NASP', ''), request.get('PLST', [])
        if (not isinstance(namespace, str) or not isinstance(names, list) or len(names) > 4096
                or any(not isinstance(n, str) or len(n.encode('utf-8')) > 256 for n in names)):
            raise ValueError('Invalid persona-name lookup')
        # There is one local player. Remote RPCN friends have no accounts on
        # this backend, so their names must not produce invented identities.
        users = []
        if namespace in ('', 'ps3') and self.session.name.casefold() in {n.casefold() for n in names}:
            # Lookup UserData differs from the UserAdded notification:
            # EDAT plus FLGS, rather than the notification's DATA field.
            users.append(tdf.group('EDAT', self.extended_data())
                         + tdf.int_field('FLGS', 3)
                         + tdf.group('USER', self.user_identification()))
        return tdf.group_list('ULST', users)

    def telemetry(self):
        # Telemetry is disabled for this offline account.
        return (tdf.str_field('ADRS', '127.0.0.1') + tdf.int_field('ANON', 0)
                + tdf.str_field('DISA', 'ALL') + tdf.int_field('EDCT', 0)
                + tdf.str_field('FILT', '') + tdf.int_field('LOC', LOCALE)
                + tdf.int_field('MINR', 0) + tdf.str_field('NOOK', '')
                + tdf.int_field('PORT', 0) + tdf.int_field('SDLY', 15000)
                + tdf.str_field('SESS', 'LOCALFIFA19') + tdf.str_field('SKEY', '')
                + tdf.int_field('SPCT', 0) + tdf.str_field('STIM', '')
                + tdf.str_field('SVNM', 'local'))

    @staticmethod
    def ticker():
        return tdf.str_field('ADRS', '127.0.0.1') + tdf.int_field('PORT', 0) + tdf.str_field('SKEY', '')

    def options(self):
        with self.backend.lock:
            opt = self.backend.profile['telemetry_opt']
        return tdf.int_field('TMOP', opt) + tdf.int_field('UID', self.session.persona)

    def persona_info(self):
        s = self.session
        return (tdf.str_field('DSNM', s.name) + tdf.str_field('DTCR', CREATED)
                + tdf.int_field('LADT', s.login_time) + tdf.str_field('NSNM', 'ps3')
                + tdf.int_field('PID', s.persona) + tdf.str_field('PNAM', s.name)
                + tdf.int_field('STAS', 2) + tdf.int_field('STRC', 0))

    def account_info(self):
        now = datetime.fromtimestamp(self.session.login_time, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        strings = {'ASRC': 'local', 'CO': 'GB', 'DOB': '1990-01-01', 'DTCR': CREATED,
                   'LATH': now, 'LN': 'en', 'MAIL': MAIL, 'PML': ''}
        ints = {'AMU': 0, 'GOPT': 0, 'RC': 0, 'STAS': 1, 'STAT': 0,
                'TPOT': 0, 'UDU': 0, 'UID': self.session.account}
        return b''.join(tdf.str_field(k, strings[k]) if k in strings else tdf.int_field(k, ints[k])
                        for k in sorted(strings.keys() | ints.keys()))

    def entitlements(self, request):
        # Local online-access flag, not a remote license or store transaction.
        groups = request.get('GNLS', ['FIFA19PS3'])
        if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
            raise ValueError('Invalid entitlement group filter')
        if request.get('TAG', 'ONLINE_ACCESS') not in ('', 'ONLINE_ACCESS'):
            return tdf.group_list('NLST', [])
        values = []
        for index, group in enumerate(groups[:32] or ['FIFA19PS3']):
            strings = {'DEVI': '', 'GDAY': CREATED, 'GNAM': group, 'PJID': 'FIFA19PS3',
                       'PRID': 'FIFA19PS3', 'TAG': 'ONLINE_ACCESS', 'TDAY': ''}
            ints = {'ID': index + 1, 'ISCO': 0, 'PID': self.session.persona, 'PRCA': 0,
                    'STAT': 1, 'STRC': 0, 'TYPE': 1, 'UCNT': 0, 'VER': 1}
            values.append(b''.join(tdf.str_field(k, strings[k]) if k in strings else tdf.int_field(k, ints[k])
                                   for k in sorted(strings.keys() | ints.keys())))
        return tdf.group_list('NLST', values)

    def response(self, frame, payload):
        if frame['kind'] != 0:
            return None
        pair = frame['component'], frame['command']
        # The login EXTB is a 36-byte SceNpId, not an authorization ticket.
        # Retain it for the user cache and GameManager; diagnostic summaries
        # still use the length-only decoder and never print the identity.
        request = tdf.decode(payload, preserve_blobs=pair[0] == 4 or pair == (1, 10))
        if pair == (9, 1) and request.get('CFID') == 'OSDK_CORE':
            # The retail formatter appends /extension to this URL. Bind its
            # opaque prefix to this live Blaze connection, not a source IP.
            import blaze
            with self.root.lock:
                if not self.client_route:
                    self.client_route = secrets.token_hex(16)
                    self.root.client_routes[self.client_route] = self
                e = self.root.endpoints
                values = blaze.client_config('OSDK_CORE', e.http_port, e.host)
                values['OSDK_EASW_AUTH_URL'] = e.base_url+'/client/'+self.client_route
            return tdf.string_map('CONF', values), 'Util.FetchClientConfig(OSDK_CORE)', []
        if pair == (1, 10):
            code, external = request.get('AUTH'), request.get('EXTI', 0)
            if not isinstance(code, str) or type(external) is not int or not 0 <= external < 2**64:
                raise ValueError('Invalid local login fields')
            blob = request.get('EXTB', b'')
            if not isinstance(blob, bytes) or len(blob) not in (0, 36):
                raise ValueError('Invalid PS3 player identity')
            np_name = None
            if any(blob):
                handle = blob[:16].split(b'\0', 1)[0]
                if not re.fullmatch(rb'[A-Za-z0-9_-]{3,16}', handle):
                    raise ValueError('Invalid PS3 online name')
                np_name = handle.decode('ascii')
            else:
                blob = b''  # Keep legacy offline/test logins without an NP ID.
            if not self.backend.identity.consume(code):
                # Reject locally; no guessed Blaze wire error codes.
                raise ValueError('Local authorization code invalid, expired or already used')
            self.close(keep_client_route=self.session is None, reason='relogin')
            with self.backend.lock:
                if self.root.multiuser and any(s.external == external for s in self.root.sessions.values()):
                    raise ValueError('This player already has an active game connection')
                self.backend = self.root.for_user(external)
                p = self.backend.profile
                self.session = Session(p['persona'], p['account'], np_name or p['name'],
                                       secrets.token_hex(16), external, int(time.time()),
                                       owner=self.backend, peer=self.peer, np_blob=blob)
                self.backend.sessions[self.session.key] = self.session
                if np_name: p['display_name'] = np_name
                p['last_login'] = self.session.login_time
                self.backend.save()
            return self.login_response(), 'Authentication.Login', self.notifications()
        if pair == (1, 70):
            self.close(reason='logout')
            return b'', 'Authentication.Logout', []
        if self.session is None:
            return None
        if pair[0] == 4:
            with self.root.lock:
                return self.root.matchmaking.response(self.session, pair[1], request)
        body, name = b'', None
        if pair == (9, 8):
            with self.root.lock:
                self.session.postauth = True
                from server_plugins import came_online
                came_online(self.backend, self.session)
            body = (tdf.group('TELE', self.telemetry()) + tdf.group('TICK', self.ticker())
                    + tdf.group('UROP', self.options()))
            name = 'Util.PostAuth'
        elif pair == (9, 5):
            body, name = self.telemetry(), 'Util.GetTelemetryServer'
        elif pair == (9, 6):
            body, name = self.ticker(), 'Util.GetTickerServer'
        elif pair == (9, 25):
            body, name = self.options(), 'Util.GetUserOptions'
        elif pair == (9, 26):
            opt = request.get('TMOP', 0)
            if type(opt) is not int or opt not in (0, 1):
                raise ValueError('Invalid telemetry preference')
            with self.backend.lock:
                self.backend.profile['telemetry_opt'] = opt
                self.backend.save()
            name = 'Util.SetUserOptions'
        elif pair == (9, 4):
            body = tdf.string_map('SMAP', self.backend.localizer.strings(request))
            name = 'Util.LocalizeStrings'
        elif pair == (9, 20):
            import blaze
            body, name = blaze.filter_user_text(request), 'Util.FilterForProfanity'
        elif pair in ((9, 10), (9, 11), (9, 12), (9, 14)):
            key = request.get('KEY', '')
            if not isinstance(key, str) or len(key) > 1024:
                raise ValueError('Invalid local settings key')
            with self.backend.lock:
                settings = self.backend.profile['settings']
                if pair == (9, 10):
                    default = '0' if key == 'FirstTimeFlag' else ''
                    body = tdf.str_field('DATA', settings.get(key, default)) + tdf.str_field('KEY', key)
                    name = 'Util.UserSettingsLoad'
                elif pair == (9, 12):
                    body = tdf.string_map('SMAP', dict(sorted(settings.items())))
                    name = 'Util.UserSettingsLoadAll'
                elif pair == (9, 11):
                    value = request.get('DATA', '')
                    if not isinstance(value, str) or len(value.encode('utf-8')) > 1024 * 1024:
                        raise ValueError('Invalid local settings value')
                    if key not in settings and len(settings) >= 1024:
                        raise ValueError('Local settings limit reached')
                    settings[key] = value
                    self.backend.save()
                    name = 'Util.UserSettingsSave'
                else:
                    settings.pop(key, None)
                    self.backend.save()
                    name = 'Util.DeleteUserSettings'
        elif pair[0] == 9 and pair[1] in (22, 23, 27, 28):
            name = {22: 'Util.SetClientMetrics', 23: 'Util.SetConnectionState',
                    27: 'Util.SuspendUserPing', 28: 'Util.SetClientState'}[pair[1]]
        elif pair[0] == 30722 and pair[1] in (8, 20):
            name = {8: 'UserSessions.UpdateHardwareFlags', 20: 'UserSessions.UpdateNetworkInfo'}[pair[1]]
        elif pair == (30722, 50):
            body, name = self.lookup_persona_names(request), 'UserSessions.LookupUsersByPersonaNames'
        elif pair == (28, 1):
            from game_reporting import peer_loading_report, peer_completed_report
            if peer_loading_report(self.backend, self.session, request):
                name = 'GameReporting.SubmitPeerLoadingDNF'
            elif peer_completed_report(self.backend, self.session, request):
                name = 'GameReporting.SubmitPeerCompletedReport'
        elif pair == (28, 2):
            from game_reporting import skill_report
            if skill_report(request, self.session):
                name = 'GameReporting.SubmitOfflineGameReport'
        elif pair == (21, 10):
            updates = request.get('UPDT', 0)
            if type(updates) is not int or updates not in (0, 1):
                raise ValueError('Invalid room update subscription')
            self.room_updates = updates
            name = 'Rooms.SelectViewUpdates'
        elif pair == (21, 11) and request.get('VWID', 0) == 0:
            # There are no multiplayer room views in the local backend.
            # Selecting zero clears the client's category subscription.
            body, name = tdf.int_field('VWID', 0), 'Rooms.SelectCategoryUpdates'
        elif pair == (21, 150):
            enabled = request.get('ENBL', 0)
            if type(enabled) is not int or enabled not in (0, 1):
                raise ValueError('Invalid room notification preference')
            self.room_notifications = bool(enabled)
            name = 'Rooms.ToggleJoinedRoomNotifications'
        elif pair == (2249, 1):
            # PS3 FetchSettingsResponse is LSIN, not the PC reference's LSST.
            # This offline service has no remotely supplied ticker settings.
            body, name = tdf.group_list('LSIN', []), 'OSDKSettings.FetchSettings'
        elif pair == (2249, 2):
            body, name = tdf.group_list('LGRP', []), 'OSDKSettings.FetchSettingsGroups'
        elif pair[0] == 15 and pair[1] in (2, 3, 5):
            # Fetch returns the number of later NotifyMessage events, not
            # the message array. Zero requires no subsequent notifications.
            # GetMessages has a different response: MSLT (ServerMessage list).
            body = tdf.group_list('MSLT', []) if pair[1] == 5 else tdf.int_field('MCNT', 0)
            name = {2: 'Messaging.FetchMessages', 3: 'Messaging.PurgeMessages',
                    5: 'Messaging.GetMessages'}[pair[1]]
        elif pair == (1, 30):
            body, name = self.account_info(), 'Authentication.GetAccount'
        elif pair == (1, 36):
            body, name = tdf.str_field('AUTH', self.session.key), 'Authentication.GetAuthToken'
        elif pair == (1, 90):
            body = tdf.group('PINF', self.persona_info()) + tdf.int_field('UID', self.session.account)
            name = 'Authentication.GetPersona'
        elif pair == (1, 100):
            body, name = tdf.group_list('PINF', [self.persona_info()]), 'Authentication.ListPersonas'
        elif pair[0] == 1 and pair[1] in (29, 32, 48):
            body = self.entitlements(request)
            name = {29: 'Authentication.ListUserEntitlements2', 32: 'Authentication.ListEntitlements',
                    48: 'Authentication.ListPersonaEntitlements2'}[pair[1]]
        elif pair == (1, 51):
            body = tdf.int_field('SPAM', 1) + tdf.int_field('UAGE', 0) + tdf.int_field('UNDR', 1)
            name = 'Authentication.CheckAgeReq'
        elif pair == (1, 242):
            body = tdf.int_field('EAMC', 0) + tdf.int_field('PMC', 0)
            name = 'Authentication.GetEmailOptInSettings'
        elif pair == (1, 241):
            name = 'Authentication.AcceptLegalDocs'
        return (body, name, []) if name else None
