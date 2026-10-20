"""CPU match lifecycle and separate FUT online reservations."""
import json
import time
import uuid
from urllib.parse import urlsplit

from fut_inventory import integer
from fut_squads import retrieve
from pow_services import json_reply

BASE = '/ut/game/fifa19/match'


def reset(session):
    queue = getattr(session, 'matchmaking_queue', None)
    if queue:
        queue.remove(session)
    session.local_fut_match = None
    session.online_fut_match = None


def complete_squad(backend, session, squad_id):
    squad = retrieve(backend, session, squad_id)
    starters = [p for p in squad['players'] if p['index'] < 11]
    if len(starters) != 11 or starters[0]['index'] != 0 or starters[0]['itemData']['preferredPosition'] != 'GK':
        raise ValueError('A complete XI with a goalkeeper is required')
    from fut_condition import available
    if any(not available(p['itemData']) for p in squad['players'] if p['index'] < 18):
        raise ValueError('Rest injured or suspended players outside the match XI and bench')
    return squad


def owned_match_managers(backend, squad):
    """Authorize only selected staff still owned by this club.

    Retail PlayGame includes the selected manager after the XI. Keep staff
    separate from played_items, which drives player appearances and reports.
    """
    from fut_items import CATALOGUE
    selected = {row['id'] for row in squad.get('manager', [])}
    return {row['id'] for row in backend.fut_inventory.state.get('supportItems', [])
            if row['id'] in selected and row['pile'] == 'club'
            and CATALOGUE[row['definitionId']]['itemType'] == 'manager'}


def create(backend, session, payload):
    backend = backend.for_session(session)
    if isinstance(payload, dict) and payload.get('type') == 'ONLINE':
        from fut_online import create_match
        return create_match(backend, session, payload)
    allowed = {'squadId', 'type', 'tournamentId', 'tournamentTrophyRound',
               'seasonId', 'divisionId', 'gameMode', 'grantsGameModePrizes', 'customData1'}
    if (not isinstance(payload, dict) or set(payload) - allowed
            or payload.get('type') != 'OFFLINE' or type(payload.get('squadId')) is not int
            or not integer(payload['squadId'], 1, 999999999)
            or any(not integer(payload[k], 0, 2**31-1) for k in
                   ('tournamentId', 'tournamentTrophyRound', 'seasonId', 'divisionId') if k in payload)
            or ('gameMode' in payload and payload['gameMode'] not in ('GENERIC', 'TOTW_CHALLENGE'))
            or ('grantsGameModePrizes' in payload and type(payload['grantsGameModePrizes']) is not bool)
            or ('customData1' in payload and not integer(payload['customData1'], -2**31, 2**31-1))):
        raise ValueError('Invalid offline match request')
    if ((online := getattr(session, 'online_fut_match', None))
            and not online.get('cancelled') and not online.get('finished')):
        raise ValueError('Reset the online reservation first')
    session.online_fut_match = None
    if payload.get('seasonId') == 1:
        from fut_seasons import renew_completed_season, active_division, accepted_divisions
        renew_completed_season(backend)
        if payload.get('divisionId', 1) not in accepted_divisions(backend):
            raise ValueError('Unknown active CPU division')
        payload = {**payload, 'divisionId': active_division(backend)}
    squad = complete_squad(backend, session, payload['squadId'])
    current = getattr(session, 'local_fut_match', None)
    if current and current.get('finished'):
        current = None
    if current and current['request'] != payload:
        raise ValueError('Reset the previous local match first')
    if current is None:
        current = {'id': uuid.uuid4().hex, 'request': dict(payload), 'start': int(time.time()), 'ready': False,
                   'squad': squad}
        if payload.get('seasonId') == 1:
            current['season_cycle'] = len(backend.fut_inventory.state.get('offlineSeasonHistory', []))
        session.local_fut_match = current
        from server_plugins import match_event
        backend.events.emit('match.reserved', **match_event(backend, current))
    # Native CreateMatch result 0x16b426c consumes these three fields. The
    # frontend owns CPU opponent selection; no multiplayer session is created.
    return {'startDateTime': current['start'], 'reportIdEnabled': False, 'squad': current['squad']}


def play(backend, session, payload):
    backend = backend.for_session(session)
    """Acknowledge kickoff and subsequent incoming players without charging cards."""
    current = getattr(session, 'local_fut_match', None)
    if (not current or current.get('finished')
            or not isinstance(payload, dict) or set(payload) != {'items'}
            or not isinstance(payload['items'], list) or not 1 <= len(payload['items']) <= 24):
        raise ValueError('A pending offline match and valid player list are required')
    ids = []
    for item in payload['items']:
        if (not isinstance(item, dict) or set(item) != {'id'}
                or not integer(item['id'], 1)):
            raise ValueError('Invalid starting card')
        ids.append(item['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('Match players must be distinct')
    squad = (current['squad'] if current.get('playing') else
             complete_squad(backend, session, current['request']['squadId']))
    managers = owned_match_managers(backend, squad)
    ids = [item_id for item_id in ids if item_id not in managers]
    if not ids:
        raise ValueError('At least one match player is required')
    if current.get('playing'):
        # FIFA reuses PlayGame for a substitution, sending just the incoming
        # card(s). Native 0x16d0994 serializes the supplied item count, not a
        # fixed XI. Validate against the frozen match XI/bench, then retain
        # every used card for final-whistle report validation. No squad save.
        eligible = {slot['itemData']['id'] for slot in current['squad']['players'] if slot['index'] < 18}
        owned = {row['id'] for row in backend.fut_inventory.state['items'] if row['pile'] == 'club'}
        from fut_condition import available
        available_ids = {r['id'] for r in backend.fut_inventory.state['items'] if available(r)}
        if not set(ids) <= eligible & owned & available_ids:
            raise ValueError('Incoming players must be owned match starters or substitutes')
        current['played_items'] = tuple(dict.fromkeys((*current['played_items'], *ids)))
        return {}
    starters = [slot for slot in squad['players'] if slot['index'] < 11]
    if (len(ids) != 11 or set(ids) != {slot['itemData']['id'] for slot in starters}
            or len(starters) != 11 or starters[0]['itemData']['preferredPosition'] != 'GK'):
        raise ValueError('Match starters must match the saved owned XI')
    current['played_items'] = tuple(ids)
    current['squad'] = squad  # Freeze the selected substitutes at submission.
    current['playing'] = True
    from server_plugins import match_event
    backend.events.emit('match.started', **match_event(backend, current))
    # PlayGame 0x34 uses result vtable 0x20745c0: its parser is 0x1eff0
    # (li r3,1; blr). It requires transport success, with no JSON fields.
    return {}


def response(backend, method, path, raw):
    from fut_services import request_headers, session_for
    parsed = urlsplit(path)
    if parsed.path not in (BASE, BASE+'/ready', BASE+'/end', BASE+'/keepalive'):
        return None
    methods = (('POST', 'PUT') if parsed.path == BASE else ('PUT',) if parsed.path == BASE+'/end'
               else ('GET',) if parsed.path == BASE+'/keepalive' else ('GET', 'POST', 'PUT'))
    # Retail online ReadyGame is a captured PUT with match/opponent/owned XI.
    # Retain the existing empty CPU ready compatibility variants.
    if method not in methods:
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', ', '.join(methods))])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        backend = backend.for_session(session)
        online = bool(getattr(session, 'online_fut_match', None))
        try:
            if parsed.query:
                raise ValueError('Unexpected query')
            if parsed.path == BASE:
                payload = json.loads(body)
                if method == 'POST':
                    online = isinstance(payload, dict) and payload.get('type') == 'ONLINE'
                    value = create(backend, session, payload)
                    if online:
                        return json_reply(value, 'FUT.OnlineMatchReservation')
                else:
                    if online:
                        from fut_online import paired_action
                        if not getattr(session, 'peer_game_id', None):
                            return online_unavailable()
                        return json_reply(paired_action(backend, session, 'play', payload), 'FUT.OnlinePeerPlay')
                    value = play(backend, session, payload)
            elif online:
                if parsed.path == BASE+'/end':
                    from fut_online import abandon_match
                    if getattr(session, 'online_fut_match', {}).get('playing'):
                        from fut_online import finish_match
                        return json_reply(finish_match(backend,session,json.loads(body)), 'FUT.OnlineMatchResult')
                    return json_reply(abandon_match(session, json.loads(body)), 'FUT.OnlineSearchDiscarded')
                if not getattr(session, 'peer_game_id', None):
                    return online_unavailable()
                from fut_online import paired_action
                action = 'keepalive' if parsed.path == BASE+'/keepalive' else 'ready'
                payload = None
                if action == 'ready' and method == 'PUT':
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        raise ValueError('Invalid online ready body')
                elif body:
                    raise ValueError('Unexpected online ready/keepalive body')
                return json_reply(paired_action(backend, session, action, payload), 'FUT.OnlinePeer'+action.title())
            elif parsed.path == BASE+'/end':
                from fut_results import finish
                value = finish(backend, session, json.loads(body))
            elif parsed.path == BASE+'/keepalive':
                current = getattr(session, 'local_fut_match', None)
                if body or not current or not current.get('playing') or current.get('finished'):
                    raise ValueError('No playing local match')
                # Observed GET; KeepAlive result vtable 0x2073fc8 uses the
                # empty success parser, like PlayGame. No rewards or writes.
                value = {}
            else:
                if body or not getattr(session, 'local_fut_match', None):
                    raise ValueError('No pending local match')
                session.local_fut_match['ready'] = True
                value = {}  # Native ready result has no JSON-reading callback.
            return json_reply(value, 'FUT.LocalOfflineMatch')
        except (ValueError, TypeError, KeyError):
            if online:
                return json_reply({'error': 'Invalid online match reservation'},
                                  'FUT.InvalidOnlineMatch', '400 Bad Request')
            return json_reply({'error': 'Invalid local offline match request'},
                              'FUT.InvalidOfflineMatch', '400 Bad Request')
        except OSError:
            return json_reply({'error': 'Could not save local match; retry'},
                              'FUT.MatchSaveFailed', '503 Service Unavailable')


def online_unavailable():
    # Reservation alone does not establish peers. Never run an online request
    # through CPU kickoff, completion or its coin-award code.
    return json_reply({'error': 'A paired peer game is required; online completion is not implemented yet'},
                      'FUT.OnlineMatchmakingRequired', '501 Not Implemented')
