"""Retail Online Seasons, reservations and paired peer kickoff acknowledgements.

GameManager owns peer pairing. Online kickoff validates the frozen owned squad;
multiplayer results and rewards require a captured, validated completion flow.
"""
import base64
from copy import deepcopy
import json
import re
import struct
import time
from urllib.parse import parse_qs, urlsplit

import fut_seasons
from pow_services import json_reply

SEASON_ID, DIVISION_ID = 2, 1
USER_LIST = '/ut/game/fifa19/user/list'
PEER_SQUAD = re.compile(r'/ut/game/fifa19/squad/([1-9][0-9]{0,8})/user/([1-9][0-9]{0,18})')


def online_cards(backend, session, current, payload, starters_only=False):
    """Validate submitted IDs against this user's reserved owned XI/bench."""
    from fut_inventory import integer
    if (not isinstance(payload, dict) or set(payload) != {'items'}
            or not isinstance(payload['items'], list) or not 1 <= len(payload['items']) <= 23):
        raise ValueError('Invalid online player list')
    ids = []
    for item in payload['items']:
        if not isinstance(item, dict) or set(item) != {'id'} or not integer(item['id'], 1):
            raise ValueError('Invalid online card')
        ids.append(item['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('Online players must be distinct')
    inventory = backend.for_session(session).fut_inventory.state
    from fut_condition import available
    owned = {row['id'] for row in inventory['items'] if row['pile'] == 'club' and available(row)}
    slots = current['squad']['players']
    eligible = {slot['itemData']['id'] for slot in slots if slot['index'] < 18}
    starters = {slot['itemData']['id'] for slot in slots if slot['index'] < 11}
    if not set(ids) <= eligible & owned or (starters_only or not current.get('playing')) and set(ids) != starters:
        raise ValueError('Online cards must match the frozen owned XI/bench')
    return tuple(ids)


def club_record(backend, session, persona_id):
    """Public club metadata consumed by retail LoadUsers (0x16bbf3c).

    User rows (0x16bbb84) contain nested equipment objects and a squadList
    object containing squad summaries, not card arrays or integer squad IDs.
    Use the reserved squad/equipment when the user is in a peer match.
    """
    from fut_squads import retrieve, abbreviation
    owner = backend.for_session(session)
    club = owner.profile['fut_club']
    reserved = getattr(session, 'online_fut_match', None)
    squad = reserved['squad'] if reserved and not reserved.get('cancelled') else retrieve(backend, session)
    assets = {item['itemState']: item for item in squad['actives']}
    starters = [slot['itemData'] for slot in squad['players'] if slot['index'] < 11]
    summary = {key: squad[key] for key in ('id', 'squadName', 'squadType', 'formation', 'chemistry')}
    summary['rating'] = sum(card['rating'] for card in starters)//len(starters) if starters else 0
    value = {'personaId': persona_id, 'clubName': squad['squadName'],
             'clubAbbr': abbreviation(squad['squadName']),
             'established': str(club['established']), 'teamId': club['asset_id'],
             'squadList': {'squad': [summary]}}
    for field, slot in (('badge', 'activeBadge'), ('homekit', 'activeHomeKit'),
                        ('awaykit', 'activeAwayKit')):
        item = assets[slot]
        value[field] = {'resourceId': item['resourceId'], 'teamId': item['teamid']}
    return value


def user_list(backend, session, query):
    """Resolve public metadata only for self and the live paired opponent.

    The captured peer flow requests personaIdList=0. The native PSN persona
    accessor can return zero (0x169d990); retain that requested cache key on
    the wire, resolving it to this session's opponent rather than a global
    zero user. Authoritative numeric IDs and frozen squads remain unchanged.
    Called under Backend.lock, then MatchmakingQueue.lock as for kickoff.
    """
    if (set(query) != {'personaIdList'} or len(query['personaIdList']) != 1
            or not re.fullmatch(r'[0-9]{1,19}(?:,[0-9]{1,19})?', query['personaIdList'][0])):
        raise ValueError('Invalid user list query')
    ids = [int(number) for number in query['personaIdList'][0].split(',')]
    if len(set(ids)) != len(ids) or any(number >= 2**63 for number in ids):
        raise ValueError('Invalid user list IDs')
    queue = getattr(session, 'matchmaking_queue', None)
    if all(number == session.persona for number in ids):
        return {'user': [club_record(backend, session, number) for number in ids]}
    if queue is None:
        raise ValueError('A paired opponent is required')
    with queue.lock:
        game = queue.peers.get(session)
        opponent = next(player for player in game['sessions'] if player is not session)
        if (not opponent.postauth or not opponent.fut_key
                or backend.root.sessions.get(opponent.key) is not opponent
                or any(number not in (0, session.persona, opponent.persona) for number in ids)):
            raise ValueError('Unknown paired user')
        return {'user': [club_record(backend, session if number == session.persona else opponent, number)
                         for number in ids]}


def user_list_response(backend, method, parsed, raw):
    from fut_services import request_headers, session_for
    if method != 'GET':
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', 'GET')])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        try:
            if body:
                raise ValueError('Unexpected request body')
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
            return json_reply(user_list(backend, session, query), 'FUT.PeerClubLookup')
        except (ValueError, TypeError):
            return json_reply({'error': 'Invalid or unavailable paired club'},
                              'FUT.InvalidPeerClubLookup', '400 Bad Request')


def peer_squad_response(backend, method, parsed, raw, match):
    """Return only this live opponent's frozen match squad, without activation.

    Once userInfo supplies the own-persona cache, retail RetrieveSquad requests
    /squad/{id}/user/{opponent}. Its usual root parser selects opponent storage
    using personaId. Sharing an owned-squad GET would change the caller's save.
    """
    from fut_services import request_headers, session_for
    if method != 'GET':
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', 'GET')])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.root.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        try:
            squad_id, persona = map(int, match.groups())
            queue = getattr(session, 'matchmaking_queue', None)
            if parsed.query or body or queue is None or persona >= 2**63:
                raise ValueError('Invalid peer squad request')
            with queue.lock:
                game = queue.peers.get(session)
                opponent = next(player for player in game['sessions'] if player is not session)
                current = getattr(opponent, 'online_fut_match', None)
                if (persona != opponent.persona or not opponent.postauth or not opponent.fut_key
                        or backend.root.sessions.get(opponent.key) is not opponent
                        or not current or current.get('cancelled')
                        or current.get('game_id') != game['id'] or current['squad']['id'] != squad_id):
                    raise ValueError('Unknown paired squad')
                value = deepcopy(current['squad'])
                value['personaId'] = opponent.persona
                return json_reply(value, 'FUT.PeerSquad')
        except (ValueError, TypeError, KeyError):
            return json_reply({'error': 'Invalid or unavailable paired squad'},
                              'FUT.InvalidPeerSquad', '400 Bad Request')


def paired_action(backend, session, action, payload=None):
    """Acknowledge retail empty-result ready/play/keepalive for actual peers."""
    from fut_inventory import integer
    queue = getattr(session, 'matchmaking_queue', None)
    if queue is None:
        raise ValueError('A paired peer game is required')
    with queue.lock:
        game = queue.peers.get(session)
        current = session.online_fut_match
        if current.get('cancelled'):
            raise ValueError('The peer game was abandoned')
        if action == 'ready':
            if payload is not None:
                opponent = next(player for player in game['sessions'] if player is not session)
                if (not isinstance(payload, dict) or set(payload) != {'matchId', 'opponentPersonaId', 'items'}
                        or not integer(payload['matchId'], game['id'], game['id'])
                        or type(payload['opponentPersonaId']) is not int
                        or payload['opponentPersonaId'] not in (session.persona, opponent.persona)
                        or len(game['links']) != 2):
                    raise ValueError('Ready must identify this connected peer match')
                ids = online_cards(backend, session, current, {'items': payload['items']}, starters_only=True)
                # Captured X-UT-SID binds both Ready requests to their own
                # XI and own ID in this field. Treat that as a local alias;
                # the actual opponent always comes from the owned peer game.
                current['ready_opponent'] = opponent.persona
                current.setdefault('ready_items', ids)
            current['ready'] = True
        elif action == 'play':
            if len(game['links']) != 2:
                raise ValueError('Both peers must report connected before playing')
            if isinstance(payload, dict) and set(payload) == {'matchId', 'opponentPersonaId', 'items'}:
                opponent = next(player for player in game['sessions'] if player is not session)
                if (not integer(payload['matchId'], game['id'], game['id'])
                        or type(payload['opponentPersonaId']) is not int
                        or payload['opponentPersonaId'] not in (session.persona, opponent.persona)):
                    raise ValueError('Play must identify this peer match')
                # Retail online PlayGame adds the same context as ReadyGame.
                # Card ownership remains bound to the authenticated caller.
                payload = {'items': payload['items']}
            ids = online_cards(backend, session, current, payload)
            started = not current.get('playing')
            current['played_items'] = tuple(dict.fromkeys((*current.get('played_items', ()), *ids)))
            current['playing'] = True
            if started:
                from server_plugins import match_event
                owner = backend.for_session(session)
                owner.events.emit('match.started', **match_event(owner, current))
        elif action == 'keepalive':
            if not current.get('playing'):
                raise ValueError('No playing online match')
        else:
            raise ValueError('Unknown online action')
        game['deadline'] = queue.peers.deadline()
        return {}


def create_match(backend, session, payload):
    """Reserve the owned XI before the separate Blaze peer negotiation.

    Captured POST 20261010T202425Z supplies ONLINE, season 2, division 1 and
    a signed customData1. Shared retail CreateMatch parser 0x16b426c consumes
    startDateTime, reportIdEnabled and squad; it does not receive a Blaze GID.
    """
    from fut_inventory import integer
    from fut_matches import complete_squad
    backend = backend.for_session(session)
    required = {'squadId', 'type', 'seasonId', 'divisionId'}
    if (not isinstance(payload, dict) or not required <= payload.keys()
            or set(payload) - (required | {'customData1'})
            or payload['type'] != 'ONLINE' or not integer(payload['squadId'], 1, 999999999)
            or not integer(payload['seasonId'], SEASON_ID, SEASON_ID)
            or not integer(payload['divisionId'], DIVISION_ID, DIVISION_ID)
            or ('customData1' in payload and not integer(payload['customData1'], -2**31, 2**31-1))):
        raise ValueError('Invalid online season reservation')
    offline = getattr(session, 'local_fut_match', None)
    if offline and not offline.get('finished'):
        raise ValueError('Reset the CPU match first')
    renew_season(backend)
    squad = complete_squad(backend, session, payload['squadId'])
    current = getattr(session, 'online_fut_match', None)
    if current and (current.get('cancelled') or current.get('finished')):
        # Completion does not guarantee the client sends LeaveGame before
        # its next CreateMatch. Otherwise StartMatchmaking reuses the old
        # game's job even though this reservation has been replaced.
        queue = getattr(session, 'matchmaking_queue', None)
        if queue:
            queue.remove(session)
        current = None
    if current and current['request'] != payload:
        raise ValueError('Reset the previous online reservation first')
    if current is None:
        current = {'request': deepcopy(payload), 'start': int(time.time()), 'squad': squad}
        current['season_cycle'] = len(backend.fut_inventory.state.get('onlineSeasonHistory', []))
        session.online_fut_match = current
        from server_plugins import match_event
        backend.events.emit('match.reserved', **match_event(backend, current))
    return {'startDateTime': current['start'], 'reportIdEnabled': False,
            'squad': deepcopy(current['squad'])}


def finish_match(backend, session, payload):
    """Accept the owning peer's final report, including after LeaveGame.

    The native GameReporting summary confirms time and score; only match/end
    writes coins, conditions and the durable once-only receipt.
    """
    from fut_inventory import integer
    from fut_results import save_result, validate_report, COMPLETE
    owner = backend.for_session(session)
    with owner.lock:
        current = getattr(session, 'online_fut_match', None)
        if (not current or not current.get('playing') or not current.get('id')
                or not isinstance(payload, dict)
                or not integer(payload.get('matchId'), current['game_id'], current['game_id'])
                or not integer(payload.get('matchReportId'), current['game_id'], current['game_id'])):
            raise ValueError('Report must identify the owning played peer match')
        result = {k:v for k,v in payload.items() if k != 'matchId'}
        validate_report(result)
        if payload.get('endReason') in COMPLETE:
            summary = current.get('peer_report')
            if (not summary or payload.get('secondsPlayed') != summary['secondsPlayed']
                    or payload.get('myMatchStats', {}).get('goals') != summary['scores'][str(session.persona)]
                    or payload.get('opponentMatchStats', {}).get('goals') != summary['scores'][str(current['opponent'])]):
                raise ValueError('Peer result must agree with the native game report')
        return save_result(owner, current, result)


def abandon_match(session, payload):
    """Acknowledge the captured cleanup after a search without an opponent."""
    expected = {'matchId': 0, 'matchReportId': 0, 'endReason': 'DNF',
                'items': [], 'matchData': ''}
    if (not getattr(session, 'online_fut_match', None) or payload != expected
            or type(payload.get('matchId')) is not int
            or type(payload.get('matchReportId')) is not int):
        raise ValueError('Only an unstarted online search can be discarded')
    queue = getattr(session, 'matchmaking_queue', None)
    if queue:
        queue.remove(session)
    session.online_fut_match['cancelled'] = True
    from fut_results import award
    return award(0)  # Retail DestroyMatch parser also clears its active-match flag.


def season_list():
    definition = fut_seasons.season_list()['seasons'][0]
    definition.update(id=SEASON_ID, type='ONLINE', matches=[])
    return {'seasons': [definition]}


def progress(backend=None):
    # Retail online adapter 0x16f85d8 selects game-data type 2 at
    # 0x16f8604 before the shared v6 constructor/loader (0x16ea0fc/16ea43c).
    if backend is not None and 'onlineSeason' in backend.fut_inventory.state:
        value = deepcopy(backend.fut_inventory.state['onlineSeason'])
        value.pop('played')
        return value
    data = bytes([6])+struct.pack('>I', 2)+bytes(3)+struct.pack('>II', 0, 0)
    encoded = base64.b64encode(struct.pack('>I', len(data))+data).decode('ascii')
    return {'divisionId': DIVISION_ID, 'seasonId': SEASON_ID, 'round': 1,
            'userPoints': 0, 'dataVersion': '2', 'data': encoded}


def validate_season_state(state):
    """Online progress and receipts must remain separate from CPU seasons."""
    archived = state.get('onlineSeasonHistory', [])
    if not isinstance(archived, list):
        raise ValueError('Invalid online season history')
    values = archived + ([state['onlineSeason']] if 'onlineSeason' in state else [])
    for value in values:
        fut_seasons.validate_saved_progress(value, season_id=SEASON_ID)
        if value['divisionId'] != DIVISION_ID:
            raise ValueError('Unknown saved online division')
    if any(value['played'] != 10 for value in archived):
        raise ValueError('Incomplete archived online season')
    if 'onlineSeasonMatchId' in state:
        marker = state['onlineSeasonMatchId']
        if (not isinstance(marker, str) or not re.fullmatch(r'[0-9a-f]{32}', marker)
                or not any(row['id'] == marker and row['seasonId'] == SEASON_ID
                           for row in state.get('matchHistory', []))):
            raise ValueError('Invalid accepted online season match')


def renew_season(backend):
    """Archive ten completed online games and keep the current test division."""
    with backend.lock:
        previous = backend.fut_inventory.state.get('onlineSeason')
        if not previous or previous['played'] < 10:
            return False
        state = deepcopy(backend.fut_inventory.state)
        state.setdefault('onlineSeasonHistory', []).append(deepcopy(previous))
        state['onlineSeason'] = {**progress(), 'played': 0}
        backend.fut_inventory.commit(state)
        from server_plugins import player
        backend.events.emit('season.completed', player=player(backend), mode='ONLINE',
                            division_id=DIVISION_ID, next_division_id=DIVISION_ID,
                            points=previous['userPoints'], played=previous['played'])
        return True


def upload_progress(backend, session, payload):
    """Save the captured v6/type-2 upload after an owned completed receipt."""
    owner = backend.for_session(session)
    required = {'round', 'dataVersion', 'data'}
    auxiliary = {'progressDataVersion', 'progressData'}
    if (not isinstance(payload, dict) or set(payload) not in (required, required | auxiliary)
            or type(payload['round']) is not int or type(payload['dataVersion']) is not int
            or payload['dataVersion'] != 2):
        raise ValueError('Invalid online season upload')
    _, played, points = fut_seasons.decode_progress(payload['data'], game_type=2)
    if payload['round'] != played+1:
        raise ValueError('Online round must agree with played games')
    value = {'divisionId': DIVISION_ID, 'seasonId': SEASON_ID, 'round': played+1,
             'played': played, 'userPoints': points, 'dataVersion': '2', 'data': payload['data']}
    if auxiliary <= payload.keys():
        if type(payload['progressDataVersion']) is not int or payload['progressDataVersion'] != 2:
            raise ValueError('Invalid auxiliary online progress version')
        value.update(progressDataVersion='2', progressData=payload['progressData'])
    fut_seasons.validate_saved_progress(value, season_id=SEASON_ID)
    with owner.lock:
        store = owner.fut_inventory
        previous = store.state.get('onlineSeason')
        if value == previous or value in store.state.get('onlineSeasonHistory', []):
            return {}
        current = getattr(session, 'online_fut_match', None)
        receipt = next((row for row in store.state.get('matchHistory', [])
                        if current and row['id'] == current.get('id') and row['seasonId'] == SEASON_ID), None)
        reason = receipt['summary']['endReason'] if receipt else None
        expected_points = (previous['userPoints'] if previous else 0) + (
            3 if reason in ('WIN', 'DNF_WIN') else 1 if reason in ('DRAW', 'DNF_DRAW') else 0)
        if (not current or not current.get('finished') or not receipt
                or current['request'].get('seasonId') != SEASON_ID
                or current['request'].get('divisionId') != DIVISION_ID
                or current.get('season_cycle', 0) != len(store.state.get('onlineSeasonHistory', []))
                or current['id'] == store.state.get('onlineSeasonMatchId')
                or played != (previous['played'] if previous else 0)+1 or points != expected_points):
            raise ValueError('Online progress requires a newly completed owned match')
        state = deepcopy(store.state)
        state.update(onlineSeason=value, onlineSeasonMatchId=current['id'])
        store.commit(state)
        return {}  # Retail SeasonUpdate uses the empty success parser.


def progress_response(backend, method, parsed, raw, match):
    from fut_services import request_headers, session_for
    if method != 'PUT':
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', 'PUT')])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.root.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        try:
            if parsed.query or int(match[2]) != DIVISION_ID:
                raise ValueError('Unknown online season upload')
            return json_reply(upload_progress(backend, session, json.loads(body)), 'FUT.OnlineSeasonUpdate')
        except (ValueError, TypeError, KeyError):
            return json_reply({'error': 'Invalid online season update'}, 'FUT.InvalidOnlineSeason', '400 Bad Request')
        except OSError:
            return json_reply({'error': 'Could not save season; retry'}, 'FUT.SeasonSaveFailed', '503 Service Unavailable')


def history(backend=None):
    value = {'type': 'online', 'bestPointsSeasonId': 0, 'bestPointsSeasonValue': 0,
            **{key: 0 for key in ('seasonGamesWon', 'seasonGamesDraw', 'seasonGamesLost',
               'seasonCompleted', 'seasonPromotions', 'seasonRelegations', 'seasonTitlesWon',
               'seasonsPassAccuracyTotal', 'seasonsPossesionTotal',
               'seasonConcededGoals', 'seasonsScoredGoals')}}
    if backend is not None:
        state = backend.fut_inventory.state
        reports = [row['summary'] for row in state.get('matchHistory', []) if row['seasonId'] == SEASON_ID]
        value.update(seasonGamesWon=sum(r['endReason'] in ('WIN','DNF_WIN') for r in reports),
                     seasonGamesDraw=sum(r['endReason'] in ('DRAW','DNF_DRAW') for r in reports),
                     seasonGamesLost=sum(r['endReason'] in ('LOSS','DNF_LOSS','QUIT','DNF') for r in reports),
                     seasonCompleted=len(state.get('onlineSeasonHistory', [])))
        for source, target in (('myMatchStats','seasonsScoredGoals'), ('opponentMatchStats','seasonConcededGoals')):
            value[target] = sum(r.get(source, {}).get('goals', 0) for r in reports)
        values = state.get('onlineSeasonHistory', []) + ([state['onlineSeason']] if 'onlineSeason' in state else [])
        value.update(bestPointsSeasonId=DIVISION_ID if values else 0,
                     bestPointsSeasonValue=max((v['userPoints'] for v in values), default=0))
    return value


def response(backend, method, path, raw):
    from fut_services import request_headers, session_for
    parsed = urlsplit(path)
    if parsed.path == USER_LIST:
        return user_list_response(backend, method, parsed, raw)
    if match := PEER_SQUAD.fullmatch(parsed.path):
        return peer_squad_response(backend, method, parsed, raw, match)
    if (match := fut_seasons.UPDATE.fullmatch(parsed.path)) and int(match[1]) == SEASON_ID:
        return progress_response(backend, method, parsed, raw, match)
    if parsed.path not in (fut_seasons.LIST, fut_seasons.PROGRESS, fut_seasons.HISTORY):
        return None
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        return None  # Existing season handler owns malformed query errors.
    if 'online' not in query.get('type', []) and str(SEASON_ID) not in query.get('seasonId', []):
        return None
    if method != 'GET':
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', 'GET')])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        try:
            owner = backend.for_session(session)
            allowed = {'type', 'divisionId', 'seasonId'}
            if parsed.path == fut_seasons.LIST:
                allowed |= {'active', 'count', 'start', 'divisionList'}
            if (body or set(query)-allowed or any(len(v) != 1 for v in query.values())
                    or query.get('type', ['online']) != ['online']):
                raise ValueError('Invalid online season query')
            for key in ('count', 'start', 'divisionId', 'seasonId'):
                if key in query and (not query[key][0].isdigit() or not 0 <= int(query[key][0]) <= 100000):
                    raise ValueError('Invalid online season number')
            if 'active' in query and query['active'][0] not in ('true', 'false'):
                raise ValueError('Invalid online active filter')
            if 'divisionList' in query and (not query['divisionList'][0] or any(
                    not n.isdigit() or not 1 <= int(n) <= 11 for n in query['divisionList'][0].split(','))):
                raise ValueError('Invalid online division list')
            selected = (int(query.get('seasonId', [SEASON_ID])[0]) == SEASON_ID
                        and int(query.get('divisionId', [DIVISION_ID])[0]) == DIVISION_ID)
            if parsed.path == fut_seasons.LIST:
                renew_season(owner)
                divisions = set(map(int, query.get('divisionList', [str(DIVISION_ID)])[0].split(',')))
                value = season_list() if selected and divisions.intersection({DIVISION_ID, 11}) else {'seasons': []}
                start, count = int(query.get('start', [0])[0]), int(query.get('count', [99])[0])
                value['seasons'] = value['seasons'][start:start+count]
            else:
                if not selected or bool({'divisionId','seasonId'} & query.keys()) and not {'divisionId','seasonId'} <= query.keys():
                    raise ValueError('Unknown online season')
                renew_season(owner)
                value = progress(owner) if parsed.path == fut_seasons.PROGRESS else history(owner)
            return json_reply(value, 'FUT.OnlineSeasonEntry')
        except (ValueError, TypeError):
            return json_reply({'error': 'Invalid online season request'}, 'FUT.InvalidOnlineSeason', '400 Bad Request')
        except OSError:
            return json_reply({'error': 'Could not save season; retry'}, 'FUT.SeasonSaveFailed', '503 Service Unavailable')
