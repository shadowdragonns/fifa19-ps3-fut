"""Repeatable local CPU seasons using retail list and v6 progress formats.

The client uploads its retail v6 progress after match completion. Reads do
archive completed seasons and persist local promotion/relegation rules.
"""
import base64
import gzip
import json
import struct
import re
import zlib
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from pow_services import json_reply

PREFIX = '/ut/game/fifa19'
SEASON_ID, DIVISION_ID = 1, 1  # Native internal division is 11 - wire division.
# Curated against the installed v1.02 database. Wire 1 is displayed Division 10.
# Keep boosted England out of the default ladder: its local 110 patch changes
# its strength independently of this server's progression.
CPU_TEAMS = (838, 111285, 563, 445, 305, 121, 357, 423, 1936, 1959)
CPU_TIERS = (
    CPU_TEAMS,
    (92, 361, 1944, 112254, 346, 1480, 1928, 1934, 143, 561),
    (27, 33, 127, 142, 149, 172, 83, 299, 487, 837),
    (3, 4, 15, 57, 64, 77, 80, 88, 89, 106),
    (2, 8, 12, 14, 28, 50, 76, 86, 91, 229),
    (13, 17, 31, 36, 38, 39, 72, 110, 175, 245),
    (7, 19, 22, 32, 34, 46, 47, 52, 236, 461),
    (1, 5, 11, 18, 44, 48, 105035, 111109, 1370, 1377),
    (105035, 111109, 1370, 1377, 1354, 1343, 1369, 1325, 1335, 1337),
    (1354, 1343, 1369, 1325, 1335, 1337, 1362, 1325, 1335, 1362))
DIFFICULTY_NAMES = ('beginner', 'amateur', 'semi_pro', 'professional', 'world_class', 'legendary', 'ultimate')
CPU_DIFFICULTIES = (1, 1, 1, 2, 2, 2, 3, 3, 4, 4)
LIST = PREFIX+'/season/list'
PROGRESS = PREFIX+'/season/user'
HISTORY = PROGRESS+'/history'
UPDATE = re.compile(re.escape(PREFIX)+r'/season/([1-9][0-9]*)/division/([1-9][0-9]*)/user')


def point_targets(division, settings=None):
    """Local rules: wire1=displayed10, wire10=displayed1."""
    from server_settings import DEFAULT
    return (settings or DEFAULT).targets(division)


def next_division(division, points, settings=None):
    targets = point_targets(division, settings)
    if points >= targets['promote']:
        return min(10, division+1)
    if points < targets['hold']:
        return max(1, division-1)
    return division


def active_division(backend):
    return backend.fut_inventory.state.get('offlineSeason', {}).get('divisionId', 1)


def accepted_divisions(backend):
    # An in-flight frontend request can still carry the completed division.
    values = {active_division(backend)}
    archived = backend.fut_inventory.state.get('offlineSeasonHistory', [])
    if archived:
        values.add(archived[-1]['divisionId'])
    return values


def season_list(backend=None):
    # Retail list parser 0x16d8e04, season definition 0x16d8870, fixture
    # 0x16d83c0. The client resolves each teamId against its installed DB.
    cycle = len(backend.fut_inventory.state.get('offlineSeasonHistory', [])) if backend else 0
    division = active_division(backend) if backend else 1
    from server_settings import DEFAULT
    rules = (backend.settings if backend else DEFAULT).snapshot().section('cpu_seasons')
    teams = rules['opponents'][division-1]
    difficulty = DIFFICULTY_NAMES.index(rules['difficulties'][division-1])
    shift = cycle % len(teams)
    teams = teams[shift:]+teams[:shift]
    return {'seasons': [{
        'id': SEASON_ID, 'type': 'OFFLINE', 'divisionId': division,
        'elgOperation': 'AND', 'elgReq': [], 'prizeSet': [],
        'numMatches': len(teams), 'matchLengthMin': 4,
        'trophyResourceId': 0, 'trophyUseCount': 0,
        'startDateTime': 1, 'endDateTime': 2147483647,
        'untilStartSeconds': 0, 'untilEndSeconds': 2147483647,
        'visStartDays': 0, 'visEndDays': 0,
        'matches': [{'teamId': team, 'difficulty': difficulty, 'roundId': i+1,
                     'rewardMult': 100, 'coins': 0} for i, team in enumerate(teams)]}]}


def initial_progress():
    # Native 0x16ea43c: byte 6, BE game-data type, played/points/flags,
    # BE count of 24-byte match records, BE count of 164-byte detail records.
    # Offline season uses game-data type 1 (constructor 0x16f8574).
    return bytes([6])+struct.pack('>I', 1)+bytes(3)+struct.pack('>II', 0, 0)


def renew_completed_season(backend):
    """Archive ten finished fixtures and atomically start a new saved season.

    Returning round 11 for a ten-fixture list makes the retail frontend select
    a missing opponent. Do not alter coins, cards, squads or match receipts.
    """
    with backend.lock:
        store = backend.fut_inventory
        previous = store.state.get('offlineSeason')
        if not previous or previous['played'] < len(CPU_TEAMS):
            return
        validate_saved_progress(previous)
        state = deepcopy(store.state)
        state.setdefault('offlineSeasonHistory', []).append(deepcopy(previous))
        division = next_division(previous['divisionId'], previous['userPoints'], backend.settings)
        state['offlineSeason'] = {**progress(division_id=division), 'played': 0}
        # Legacy saves lack this marker. Retain the last receipt so queued
        # uploads cannot count the completed match again in the new season.
        receipts = [row for row in state.get('matchHistory', []) if row['seasonId'] == SEASON_ID]
        if 'offlineSeasonMatchId' not in state and receipts:
            state['offlineSeasonMatchId'] = receipts[-1]['id']
        store.commit(state)
        from server_plugins import player
        backend.events.emit('season.completed', player=player(backend), mode='OFFLINE',
                            division_id=previous['divisionId'], next_division_id=division,
                            points=previous['userPoints'], played=previous['played'])


def progress(backend=None, division_id=1):
    if backend is not None:
        renew_completed_season(backend)
    if backend is not None and backend.fut_inventory.state.get('offlineSeason'):
        value = deepcopy(backend.fut_inventory.state['offlineSeason'])
        value.pop('played')
        return value
    data = initial_progress()
    compressed = gzip.compress(data, mtime=0)
    # 0x16ac894 compares the base64 allocation size minus four with the
    # declared output length. Larger payloads take its raw-copy branch;
    # gzip is only usable when its rounded allocation fits the output.
    payload = compressed if ((4+len(compressed)+2)//3)*3-4 <= len(data) else data
    encoded = base64.b64encode(struct.pack('>I', len(data))+payload).decode('ascii')
    # Result 0x16d9438 decodes dataVersion as a decimal STRING. Version 2
    # activates the sized base64/raw-or-gzip path at 0x16ac6f0. round is 1-based;
    # the native result stores round-1 and the progress loader starts at zero.
    return {'divisionId': division_id, 'seasonId': SEASON_ID, 'round': 1,
            'userPoints': 0, 'dataVersion': '2', 'data': encoded}


def decode_envelope(encoded, minimum=1, maximum=65536):
    if not isinstance(encoded, str) or not encoded or len(encoded) > 131072:
        raise ValueError('Invalid season data')
    try:
        envelope = base64.b64decode(encoded, validate=True)
        if len(envelope) < 5:
            raise ValueError('Missing season envelope')
        length = struct.unpack('>I', envelope[:4])[0]
        if not minimum <= length <= maximum:
            raise ValueError('Invalid season data length')
        if len(encoded)//4*3-4 > length:
            data = envelope[4:]
        else:
            decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
            data = decoder.decompress(envelope[4:], length+1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError('Invalid compressed season data')
        if len(data) != length:
            raise ValueError('Season data length mismatch')
        return data
    except (ValueError, zlib.error, struct.error) as error:
        raise ValueError('Invalid season progress envelope') from error


def decode_progress(encoded, game_type=1):
    data = decode_envelope(encoded, minimum=16)
    try:
        if data[0] != 6 or struct.unpack('>I', data[1:5])[0] != game_type:
            raise ValueError('Unsupported season data')
        count = struct.unpack('>I', data[8:12])[0]
        details_offset = 12+24*count
        # The first vector contains cumulative PLAYER records (0x16ea808),
        # not fixtures. The live first-match upload contains 18 such rows.
        if count > 23*len(CPU_TEAMS) or details_offset+4 > len(data):
            raise ValueError('Invalid season player records')
        details = struct.unpack('>I', data[details_offset:details_offset+4])[0]
        if details > 10 or details_offset+4+164*details != len(data):
            raise ValueError('Invalid season detail records')
        played, points = data[5:7]
        if played > 10 or points > 3*played:
            raise ValueError('Invalid season counters')
        return data, played, points
    except (ValueError, zlib.error, struct.error) as error:
        raise ValueError('Invalid season progress envelope') from error


def validate_saved_progress(value, season_id=SEASON_ID):
    required = {'divisionId', 'seasonId', 'round', 'userPoints', 'played', 'dataVersion', 'data'}
    auxiliary = {'progressDataVersion', 'progressData'}
    if (not isinstance(value, dict) or set(value) not in (required, required | auxiliary)
            or type(value['divisionId']) is not int or not 1 <= value['divisionId'] <= 10
            or type(value['seasonId']) is not int or value['seasonId'] != season_id
            or value['dataVersion'] != '2'):
        raise ValueError('Invalid saved offline season')
    _, played, points = decode_progress(value['data'], game_type=2 if season_id == 2 else 1)
    if any(type(value[k]) is not int for k in ('round', 'played', 'userPoints')) or (
            value['played'], value['userPoints'], value['round']) != (played, points, played+1):
        raise ValueError('Invalid saved season counters')
    if 'progressData' in value:
        if value['progressDataVersion'] != '2':
            raise ValueError('Invalid auxiliary progress version')
        decode_envelope(value['progressData'], maximum=1024)


def validate_season_history(state):
    history = state.get('offlineSeasonHistory', [])
    if not isinstance(history, list):
        raise ValueError('Invalid completed offline seasons')
    for value in history:
        validate_saved_progress(value)
        if value['played'] != len(CPU_TEAMS):
            raise ValueError('Archived season is incomplete')
    if 'offlineSeasonMatchId' in state:
        marker = state['offlineSeasonMatchId']
        if (not isinstance(marker, str) or not re.fullmatch(r'[0-9a-f]{32}', marker)
                or not any(row['id'] == marker and row['seasonId'] == SEASON_ID
                           for row in state.get('matchHistory', []))):
            raise ValueError('Invalid accepted season match marker')


def upload(backend, session, payload, division_id=None):
    backend = backend.for_session(session)
    division_id = active_division(backend) if division_id is None else division_id
    required = {'round', 'dataVersion', 'data'}
    auxiliary = {'progressDataVersion', 'progressData'}
    if (not isinstance(payload, dict) or set(payload) not in (required, required | auxiliary)
            or type(payload['round']) is not int or type(payload['dataVersion']) is not int
            or payload['dataVersion'] != 2):
        raise ValueError('Invalid season upload')
    _, played, points = decode_progress(payload['data'])
    if payload['round'] not in (played, played+1):
        raise ValueError('Upload round must agree with played games')
    # Live upload round=2, v6 played=1. GET subtracts one from round.
    value = {'divisionId': division_id, 'seasonId': SEASON_ID, 'round': played+1,
             'dataVersion': '2', 'played': played, 'userPoints': points, 'data': payload['data']}
    if 'progressData' in payload:
        if type(payload['progressDataVersion']) is not int or payload['progressDataVersion'] != 2:
            raise ValueError('Invalid auxiliary progress version')
        value.update(progressDataVersion='2', progressData=payload['progressData'])
    validate_saved_progress(value)
    with backend.lock:
        store = backend.fut_inventory
        current = getattr(session, 'local_fut_match', None)
        previous = store.state.get('offlineSeason')
        if previous == value:
            return {}  # Retry only; no second rewards or progress advancement.
        if value in store.state.get('offlineSeasonHistory', []):
            return {}  # Queued terminal upload after rollover; preserve new season.
        receipt = next((row for row in store.state.get('matchHistory', [])
                        if current and row['id'] == current['id'] and row['seasonId'] == SEASON_ID), None)
        # A previously rejected upload may leave a saved match receipt while
        # the client reloads the last accepted fixture. Advance from accepted
        # progress plus THIS receipt, not all reports ever saved in the club.
        reason = receipt['summary']['endReason'] if receipt else None
        expected_points = (previous['userPoints'] if previous else 0) + (
            3 if reason in ('WIN', 'DNF_WIN') else 1 if reason in ('DRAW', 'DNF_DRAW') else 0)
        if (division_id != active_division(backend)
                or not current or not current.get('finished') or current['request'].get('seasonId') != SEASON_ID
                or current['request'].get('divisionId', 1) != division_id
                or current.get('season_cycle', 0) != len(store.state.get('offlineSeasonHistory', []))
                or current['id'] == store.state.get('offlineSeasonMatchId')
                or not receipt or played != (previous['played'] if previous else 0)+1
                or points != expected_points):
            raise ValueError('Season progress requires a newly completed local match')
        state = deepcopy(store.state)
        state['offlineSeason'] = value
        state['offlineSeasonMatchId'] = current['id']
        store.commit(state)
        return {}  # Native SeasonUpdate result has no JSON field dispatch.


def history(backend):
    reports = [row['summary'] for row in backend.fut_inventory.state.get('matchHistory', [])
               if row['seasonId'] == SEASON_ID]
    state = backend.fut_inventory.state
    archived = state.get('offlineSeasonHistory', [])
    current = state.get('offlineSeason', {})
    # The native best-points field is one byte. Best season points, rather
    # than lifetime points, remain bounded at 30 across repeated seasons.
    totals = [value['userPoints'] for value in archived]+([current['userPoints']] if current else [])
    if not totals:
        totals = [sum(3 if r['endReason'] in ('WIN', 'DNF_WIN') else
                      1 if r['endReason'] in ('DRAW', 'DNF_DRAW') else 0 for r in reports[i:i+10])
                  for i in range(0, len(reports), 10)]
    completed = (len(archived)+int(current.get('played', 0) == 10)
                 if 'offlineSeasonHistory' in state else len(reports)//len(CPU_TEAMS))
    completed_values = archived+([current] if current.get('played') == 10 else [])
    best = max(archived+([current] if current else []), key=lambda v: v['userPoints'], default={})
    return {'type': 'offline', 'bestPointsSeasonId': best.get('divisionId', DIVISION_ID if reports else 0),
            'bestPointsSeasonValue': max(totals, default=0),
            'seasonGamesWon': sum(r['endReason'] in ('WIN', 'DNF_WIN') for r in reports),
            'seasonGamesDraw': sum(r['endReason'] in ('DRAW', 'DNF_DRAW') for r in reports),
            'seasonGamesLost': sum(r['endReason'] in ('LOSS', 'DNF_LOSS', 'DNF', 'QUIT') for r in reports),
            'seasonCompleted': completed,
            'seasonPromotions': sum(next_division(v['divisionId'], v['userPoints']) > v['divisionId'] for v in completed_values),
            'seasonRelegations': sum(next_division(v['divisionId'], v['userPoints']) < v['divisionId'] for v in completed_values),
            'seasonTitlesWon': sum(v['divisionId'] == 10 and v['userPoints'] >= point_targets(10)['promote'] for v in completed_values),
            'seasonsPassAccuracyTotal': sum(r.get('myMatchStats', {}).get('passingPercentage', 0) for r in reports),
            'seasonsPossesionTotal': sum(r.get('myMatchStats', {}).get('possessionPercentage', 0) for r in reports),
            'seasonConcededGoals': sum(r.get('opponentMatchStats', {}).get('goals', 0) for r in reports),
            'seasonsScoredGoals': sum(r.get('myMatchStats', {}).get('goals', 0) for r in reports)}


def response(backend, method, path, raw):
    from fut_services import request_headers, session_for
    parsed = urlsplit(path)
    updating = UPDATE.fullmatch(parsed.path)
    if parsed.path not in (LIST, PROGRESS, HISTORY) and not updating:
        return None
    methods = ('PUT',) if updating else ('GET',)
    if method not in methods:
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', ', '.join(methods))])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        backend = backend.for_session(session)
        try:
            if updating:
                season_id, division = map(int, updating.groups())
                if parsed.query or season_id != SEASON_ID or not 1 <= division <= 10:
                    raise ValueError('Unknown local season upload')
                return json_reply(upload(backend, session, json.loads(body), division), 'FUT.LocalCPUSeasonUpdate')
            if body:
                raise ValueError('Unexpected body')
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
            allowed = {'active', 'count', 'start', 'divisionList', 'type', 'divisionId', 'seasonId'} if parsed.path == LIST else {'type', 'divisionId', 'seasonId'}
            if set(query)-allowed or any(len(v) != 1 for v in query.values()):
                raise ValueError('Unexpected query')
            if query.get('type', ['offline']) != ['offline']:
                raise ValueError('Only offline CPU seasons are supported')
            for key in ('count', 'start', 'divisionId', 'seasonId'):
                if key in query and (not query[key][0].isdigit() or not 0 <= int(query[key][0]) <= 100000):
                    raise ValueError('Invalid numeric query')
            if 'active' in query and query['active'][0] not in ('true', 'false'):
                raise ValueError('Invalid active filter')
            if 'divisionList' in query and (not query['divisionList'][0] or
                    any(not n.isdigit() or not 1 <= int(n) <= 11 for n in query['divisionList'][0].split(','))):
                raise ValueError('Invalid division list')
            renew_completed_season(backend)
            division = active_division(backend)
            accepted = accepted_divisions(backend)
            if parsed.path == PROGRESS and ({'divisionId', 'seasonId'}.intersection(query)):
                if (not {'divisionId', 'seasonId'}.issubset(query)
                        or int(query['divisionId'][0]) not in accepted
                        or int(query['seasonId'][0]) != SEASON_ID):
                    raise ValueError('Unknown local season')
            value = progress(backend) if parsed.path == PROGRESS else history(backend) if parsed.path == HISTORY else season_list(backend)
            if parsed.path == LIST:
                divisions = {int(n) for n in query.get('divisionList', [str(division)])[0].split(',')}
                if (int(query.get('seasonId', [SEASON_ID])[0]) != SEASON_ID
                        or int(query.get('divisionId', [division])[0]) not in accepted
                        or not divisions.intersection(accepted | {11})):
                    value = {'seasons': []}
                else:
                    start, count = int(query.get('start', [0])[0]), int(query.get('count', [99])[0])
                    value['seasons'] = value['seasons'][start:start+count]
                # The live client sends wire division 1 for displayed division
                # 10. Also accept the pre-bootstrap capture's divisionList=11.
            return json_reply(value, 'FUT.LocalCPUSeason')
        except (ValueError, TypeError):
            return json_reply({'error': 'Invalid local CPU season request'},
                              'FUT.InvalidCPUSeason', '400 Bad Request')
        except OSError:
            return json_reply({'error': 'Could not save season; retry'},
                              'FUT.SeasonSaveFailed', '503 Service Unavailable')
