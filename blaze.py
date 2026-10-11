"""Schema-based PS3 bootstrap replies; connection state lives in sessions.py."""
import struct
import time

import tdf

CORE_CONFIG = {
    'connIdleTimeout': '120s', 'defaultRequestTimeout': '15s',
    'pingPeriod': '20s', 'voipHeadsetUpdateRate': '1000',
    'xlspConnectionIdleTimeout': '300',
}
BOOTSTRAP_CONFIGS = {'OSDK_CORE', 'OSDK_CLIENT', 'OSDK_NUCLEUS', 'OSDK_WEBOFFER',
                    'OSDK_ABUSE_REPORTING', 'IdentityParams', 'LegalParams'}
POSTAUTH_CONFIGS = {'OSDK_TICKER', 'OSDK_ROSTER'}
OBSERVED_CONFIGS = BOOTSTRAP_CONFIGS | POSTAUTH_CONFIGS


def client_config(name, http_port, host='127.0.0.1'):
    from endpoints import Endpoints
    endpoints = Endpoints(host, http_port=http_port)
    if name == 'BlazeSDK':
        # The PreAuth callback copies this map into the connection manager.
        # Its URL getter reads nucleusConnect when starting console login.
        values = {**CORE_CONFIG, 'nucleusConnect': endpoints.base_url}
        return dict(sorted(values.items()))
    if name == 'IdentityParams':
        return {'redirect_uri': endpoints.callback}
    if name == 'OSDK_CORE':
        base = endpoints.base_url
        # These exact key strings occur in the PS3 executable. HTTP services
        # still need handlers; the mappings bring their requests to localhost.
        return {
            'PROFILEANYWHERE_IS_ON': '1',
            'CMS_BASE_URL': base + '/pran/',
            'CMS_SKUID_PRAN': 'FFAPRAN1',
            'CMS_APIKEY_PRAN': 'local-pran',
            'CTL_UPDATE_URL': base + '/ctl',
            'FIFA_POW_CONTENT_SERVER_URL': base + '/',
            'FIFA_POW_NUCLEUS_PROXY_URL': base + '/',
            'FIFA_POW_URL': base + '/',
            'FUT_RS4_BASE_URL': base + '/',
            # 0x4daf68 loads this separately from the REST API base. The FUT
            # content loader appends /fut/ and the pack-localization filename.
            'FUTDYNAMICMESSAGES_URL_BASE': base,
            'OSDK_EASW_AUTH_URL': base,
            'OSDK_EASW_EVENT_URL': base,
            'OSDK_EASW_MEDIA_URL': base,
            'OSDK_EASW_REQ_URL': base,
            'ROSTERUPDATE_URL': base + '/roster',
            'ROUTINGCFGFILE_URL': base + '/routing',
        }
    if name in OBSERVED_CONFIGS:
        # A typed empty map supplies no overrides. The game keeps its local
        # defaults; this does not provide account, web-offer or abuse services.
        return {}
    return None


def reply(frame, payload, kind=1):
    return (struct.pack('>IHHH', len(payload), 0, frame['component'], frame['command'])
            + frame['message'].to_bytes(3, 'big') + bytes([kind << 5, 0, 0]) + payload)


def filter_user_text(request):
    """Complete the retail name-check RPC for this private local server.

    The observed request uses FilterUserTextResponse in both directions:
    TLST contains structs with DIRT=2 (unprocessed) and UTXT. The executable's
    FilterResult enum defines passed=0. Local names pass through unchanged;
    this does not reproduce EA's remote profanity dictionary.
    """
    values = request.get('TLST')
    if not isinstance(values, list) or len(values) > tdf.MAX_ITEMS:
        raise ValueError('Invalid local name-check list')
    output = []
    for entry in values:
        if not isinstance(entry, dict):
            raise ValueError('Invalid local name-check entry')
        text, result = entry.get('UTXT'), entry.get('DIRT', 2)
        if (not isinstance(text, str) or '\0' in text
                or len(text.encode('utf-8')) > 512
                or type(result) is not int or result not in range(5)):
            raise ValueError('Invalid local name-check fields')
        output.append(tdf.int_field('DIRT', 0) + tdf.str_field('UTXT', text))
    return tdf.group_list('TLST', output)


def preauth(request, http_port, host='127.0.0.1'):
    """PS3 PreAuthResponse fields recovered from the executable member table."""
    service = request.get('CDAT', {}).get('SVCN', 'fifa-2019-ps3')
    if service != 'fifa-2019-ps3':
        raise ValueError('PreAuth service does not match FIFA 19 PS3')
    config = tdf.group('CONF', tdf.string_map('CONF', client_config('BlazeSDK', http_port, host)))
    ping_site = (tdf.str_field('PSA', host) + tdf.int_field('PSP', http_port)
                 + tdf.str_field('SNA', 'local'))
    qos = (tdf.group('BWPS', ping_site) + tdf.int_field('LNP', 10)
           + tdf.group_map('LTPS', {'local': ping_site})
           + tdf.int_field('SVID', 1161889797) + tdf.int_field('TIME', 5000))
    return (tdf.str_field('ASRC', 'local') + tdf.int_list('CIDS', [1, 9, 35, 30722])
            + tdf.str_field('CLID', 'fifa19-local') + config
            + tdf.str_field('ESRC', 'local') + tdf.str_field('INST', service)
            + tdf.int_field('MAID', 1) + tdf.int_field('MINR', 0)
            + tdf.str_field('NASP', 'ps3') + tdf.str_field('PILD', 'fifa-2019')
            + tdf.str_field('PLAT', 'ps3') + tdf.group('QOSS', qos)
            + tdf.str_field('RSRC', 'local') + tdf.str_field('SVER', 'Blaze 15.1.1.0.0')
            + tdf.int_field('TMOP', 0))


def response(frame, payload, http_port=8099, host='127.0.0.1'):
    if frame['kind'] != 0:
        return None
    if (frame['component'], frame['command']) == (9, 7):
        return preauth(tdf.decode(payload), http_port, host), 'Util.PreAuth'
    if (frame['component'], frame['command']) == (9, 2):
        return tdf.int_field('STIM', int(time.time())), 'Util.Ping'
    if (frame['component'], frame['command']) == (9, 1):
        name = tdf.decode(payload).get('CFID', '')
        values = client_config(name, http_port, host)
        if values is not None:
            return tdf.string_map('CONF', values), f'Util.FetchClientConfig({name})'
    if (frame['component'], frame['command']) == (1, 70) and not payload:
        # Observed logout after failed configuration requests. There is no
        # authenticated local session to retain; this acknowledges cleanup.
        return b'', 'Authentication.Logout'
    return None
