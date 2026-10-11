"""Owned-card squads using the retail Save/Retrieve/SquadList contracts."""
from copy import deepcopy
import json
import re
import unicodedata
from urllib.parse import urlsplit, parse_qs

from fut_inventory import integer
from pow_services import json_reply

PREFIX = '/ut/game/fifa19/squad'
SQUAD = re.compile(re.escape(PREFIX)+r'/([1-9][0-9]{0,8})')
FORMATIONS = {'f3412', 'f3421', 'f343', 'f352', 'f41212', 'f4141', 'f4222',
              'f4231', 'f4312', 'f4321', 'f433', 'f4411', 'f442', 'f451',
              'f5212', 'f5221', 'f532', 'f541'}


def empty(name, squad_id=1):
    return {'id': squad_id, 'squadName': name, 'formation': 'f442',
            'squadType': 'REGULAR_SQUAD', 'custom': '['+','.join(['0']*22)+']',
            'players': [{'index': i, 'itemData': {'id': 0, 'dream': False},
                         'kitNumber': 0} for i in range(23)],
            'manager': [{'id': 0, 'dream': False}], 'captain': 0,
            'kicktakers': [{'index': i, 'id': 0, 'dream': False} for i in range(5)]}


def validate(value, items):
    if not isinstance(value, dict) or set(value) != set(empty('')):
        raise ValueError('Invalid stored squad')
    if (not integer(value['id'], 1, 999999999)
            or value['formation'] not in FORMATIONS or value['squadType'] != 'REGULAR_SQUAD'
            or not isinstance(value['squadName'], str) or not 1 <= len(value['squadName']) <= 32
            or any(ord(c) < 32 for c in value['squadName'])
            or not isinstance(value['custom'], str) or len(value['custom']) > 1024):
        raise ValueError('Invalid squad metadata')
    custom = json.loads(value['custom'])
    if not isinstance(custom, list) or len(custom) != 22 or any(not integer(n, -1000, 1000) for n in custom):
        raise ValueError('Invalid custom tactics')
    owned = {r['id'] for r in items if r['pile'] == 'club'}
    players = value['players']
    if not isinstance(players, list) or len(players) != 23:
        raise ValueError('Expected 23 squad slots')
    selected = set()
    for index, slot in enumerate(players):
        if (not isinstance(slot, dict) or set(slot) != {'index', 'itemData', 'kitNumber'}
                or type(slot['index']) is not int or slot['index'] != index
                or not integer(slot['kitNumber'], 0, 99)
                or not isinstance(slot['itemData'], dict) or set(slot['itemData']) != {'id', 'dream'}
                or slot['itemData']['dream'] is not False):
            raise ValueError('Invalid squad slot')
        item_id = slot['itemData']['id']
        if not integer(item_id) or (item_id and (item_id not in owned or item_id in selected)):
            raise ValueError('Squad needs distinct owned club cards')
        if item_id:
            selected.add(item_id)
    if value['manager'] != [{'id': 0, 'dream': False}]:
        raise ValueError('No local manager card is owned')
    if not integer(value['captain']) or value['captain'] not in selected | {0}:
        raise ValueError('Captain must be in this squad')
    kicks = value['kicktakers']
    if not isinstance(kicks, list) or len(kicks) != 5:
        raise ValueError('Expected five kick takers')
    for i, kick in enumerate(kicks):
        if (not isinstance(kick, dict) or set(kick) != {'index', 'id', 'dream'}
                or type(kick['index']) is not int or kick['index'] != i
                or kick['dream'] is not False or not integer(kick['id'])
                or kick['id'] not in selected | {0}):
            raise ValueError('Invalid kick taker')


def squads(backend):
    state = backend.fut_inventory.state
    active = state.get('squad') or empty(backend.profile['fut_club']['name'])
    return {active['id']: active, **{int(k): v for k, v in state.get('otherSquads', {}).items()}}


def saved(backend, squad_id=None):
    state = backend.fut_inventory.state
    active = state.get('squad') or empty(backend.profile['fut_club']['name'])
    if squad_id is None or squad_id == active['id']:
        return active
    return squads(backend)[squad_id]


def display_name(backend, session=None):
    """Use the reserved match squad or the active saved squad for team names."""
    if session is not None:
        backend = backend.for_session(session)
        for field in ('online_fut_match', 'local_fut_match'):
            match = getattr(session, field, None)
            if match and not match.get('finished') and not match.get('cancelled'):
                return match['squad']['squadName']
    return saved(backend)['squadName']


def abbreviation(name):
    # The scoreboard also reads clubAbbr; keep its three-character label
    # aligned with the full name instead of always displaying ENG.
    letters = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return ''.join(c for c in letters if c.isalnum()).upper()[:3] or 'FUT'


def activate(backend, squad_id):
    with backend.lock:
        state = deepcopy(backend.fut_inventory.state)
        previous = deepcopy(saved(backend))
        value = deepcopy(saved(backend, squad_id))
        if previous['id'] != squad_id:
            others = state.setdefault('otherSquads', {})
            others[str(previous['id'])] = previous
            others.pop(str(squad_id), None)
            state['squad'] = value
            backend.fut_inventory.commit(state)
            from server_plugins import player
            backend.events.emit('squad.activated', player=player(backend), squad_id=squad_id, name=value['squadName'])


def create(backend, payload):
    # Retail CreateSquadWithName (0x1675140) assigns the local cached squad
    # count to id, not a -1 sentinel. It can therefore equal an existing ID.
    # POST always allocates a new server ID; this number never selects a save.
    if (not isinstance(payload, dict) or type(payload.get('id', -1)) is not int
            or not -1 <= payload.get('id', -1) <= 999999999):
        raise ValueError('Invalid new squad')
    with backend.lock:
        collection = squads(backend)
        if len(collection) >= 30:
            raise ValueError('Squad limit reached')
        state = deepcopy(backend.fut_inventory.state)
        squad_id = max(state.get('nextSquadId', 2), max(collection)+1)
        value = empty(backend.profile['fut_club']['name'], squad_id)
        fields = set(value) | {'rating', 'starRating', 'chemistry'}
        if set(payload)-fields:
            raise ValueError('Invalid new squad fields')
        value.update({k: deepcopy(v) for k, v in payload.items() if k in value and k != 'id'})
        # The native constructor has zero occupied slots and serializes [].
        # Keep the persisted 23-slot representation used by later edits.
        if payload.get('players') == []:
            value['players'] = empty('', squad_id)['players']
        validate(value, state['items'])
        state.setdefault('squad', deepcopy(saved(backend)))
        state.setdefault('otherSquads', {})[str(squad_id)] = value
        state['nextSquadId'] = squad_id+1
        backend.fut_inventory.commit(state)
        from server_plugins import player
        backend.events.emit('squad.created', player=player(backend), squad_id=squad_id, name=value['squadName'])
        return {'id': squad_id}


def remove(backend, squad_id):
    with backend.lock:
        collection = squads(backend)
        if squad_id not in collection:
            raise KeyError(squad_id)
        if len(collection) == 1:
            raise ValueError('Keep at least one squad')
        state = deepcopy(backend.fut_inventory.state)
        if saved(backend)['id'] == squad_id:
            replacement = min(k for k in collection if k != squad_id)
            state['squad'] = deepcopy(collection[replacement])
            state['otherSquads'].pop(str(replacement))
        else:
            state['otherSquads'].pop(str(squad_id))
        backend.fut_inventory.commit(state)
        from server_plugins import player
        backend.events.emit('squad.deleted', player=player(backend), squad_id=squad_id)
        return {}


def retrieve(backend, session, squad_id=None):
    backend = backend.for_session(session)
    from fut_club_assets import active_assets
    with backend.lock:
        store = backend.fut_inventory
        value = deepcopy(saved(backend, squad_id))
        value['manager'] = []  # Constructor supplies the absent manager wrapper.
        by_id = {r['id']: r for r in store.state['items']}
        # Clear slots are represented by the constructor's empty wrappers.
        # Only occupied rows need full player objects in the response.
        value['players'] = [dict(slot, itemData=store.card(by_id[slot['itemData']['id']], store.settings))
                            for slot in value['players'] if slot['itemData']['id']]
        starters = [s['itemData'] for s in value['players'] if s['index'] < 11]
        value.update(personaId=session.persona, changed=0, actives=active_assets(backend),
                     starRating=5 if len(starters) == 11 else 0,
                     chemistry=0)  # Native client computes positional chemistry.
        return value


def save(backend, payload):
    # The captured native save contains calculated rating/chemistry fields.
    # Persist only card references/tactics; retrieve rebuilds all card data.
    if (not isinstance(payload, dict) or type(payload.get('id')) is not int
            or not integer(payload['id'], 1, 999999999)
            or set(payload) - (set(empty('')) | {'rating', 'starRating', 'chemistry'})):
        raise ValueError('Invalid squad request')
    with backend.lock:
        store = backend.fut_inventory
        # The match-settings screen saves only id/custom/captain/kicktakers.
        # Merge those edits with the saved lineup before validating ownership.
        if payload['id'] not in squads(backend):
            raise ValueError('Unknown squad')
        value = deepcopy(saved(backend, payload['id']))
        value.update({k: deepcopy(v) for k, v in payload.items() if k in value})
        validate(value, store.state['items'])
        state = deepcopy(store.state)
        if saved(backend)['id'] == value['id']:
            state['squad'] = value
        else:
            state.setdefault('otherSquads', {})[str(value['id'])] = value
        store.commit(state)
        from server_plugins import player
        backend.events.emit('squad.saved', player=player(backend), squad_id=value['id'], name=value['squadName'])
        return {'id': value['id']}  # Native result 0x16d5d1c reads this integer only.


def prepare_owned_xi(backend):
    """One explicit preparation of the existing club; never grants cards."""
    with backend.lock:
        store = backend.fut_inventory
        if store.state.get('squad'):
            return False
        records = [r for r in store.state['items'] if r['pile'] == 'club']
        if len(records) < 11:
            return False
        value = empty(backend.profile['fut_club']['name'])
        remaining = list(records)
        positions = [('GK',), ('RB',), ('CB',), ('CB',), ('LB',),
                     ('RM', 'RW'), ('CM', 'CDM'), ('CM', 'CAM', 'CDM'),
                     ('LM', 'LW'), ('ST', 'CF', 'LW'), ('ST', 'CF', 'CAM')]
        # Reserve attacking cards first when the pack has no natural striker.
        for i in (0, 9, 10, 1, 2, 3, 4, 5, 6, 7, 8):
            preferred = positions[i]
            candidates = [r for r in remaining if store.card(r, store.settings)['preferredPosition'] in preferred]
            record = candidates[0] if candidates else remaining[0]
            remaining.remove(record)
            value['players'][i]['itemData']['id'] = record['id']
            value['players'][i]['kitNumber'] = i+1
        for i, record in enumerate(remaining[:12], 11):
            value['players'][i]['itemData']['id'] = record['id']
            value['players'][i]['kitNumber'] = i+1
        value['captain'] = value['players'][6]['itemData']['id']
        for kick in value['kicktakers']:
            kick['id'] = value['players'][9]['itemData']['id']
        save(backend, value)
        return True


def fill_owned_bench(backend):
    """Explicit preparation: fill empty bench/reserves from existing club cards.

    Preserves starters and every selected card; grants nothing. This is never
    called on normal login or reads, so later user edits remain authoritative.
    """
    with backend.lock:
        store = backend.fut_inventory
        if not store.state.get('squad'):
            return 0
        value = deepcopy(saved(backend))
        selected = {slot['itemData']['id'] for slot in value['players']}
        available = [r for r in store.state['items'] if r['pile'] == 'club' and r['id'] not in selected]
        added = 0
        for slot in value['players'][11:]:
            if slot['itemData']['id'] or not available:
                continue
            slot['itemData']['id'] = available.pop(0)['id']
            slot['kitNumber'] = slot['index']+1
            added += 1
        if added:
            save(backend, value)
        return added


DELETE = re.compile(r'/ut/delete/game/fifa19/squad/([1-9][0-9]{0,8})')


def response(backend, method, path, raw):
    from fut_services import request_headers, session_for
    parsed = urlsplit(path)
    match = SQUAD.fullmatch(parsed.path)
    deleting = DELETE.fullmatch(parsed.path)
    listing = parsed.path == PREFIX+'/list'
    creating = parsed.path == PREFIX
    if not match and not deleting and not listing and not creating:
        return None
    methods = ('GET',) if listing else ('POST',) if creating or deleting else ('GET', 'PUT', 'DELETE')
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
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
            if ((query and not (match and method == 'GET' and query == {'active': ['true']}))
                    or (method == 'GET' and body)):
                raise ValueError('Unexpected query/body')
            squad_id = int((deleting or match)[1]) if deleting or match else None
            if squad_id is not None and squad_id not in squads(backend):
                return json_reply({'error': 'Unknown local squad'}, 'FUT.UnknownSquad', '404 Not Found')
            if deleting or method == 'DELETE':
                if body:
                    payload = json.loads(body)
                    if (not isinstance(payload, dict) or set(payload)-{'id', 'squadName'}
                            or payload.get('id') != squad_id):
                        raise ValueError('Squad ID mismatch')
                value = remove(backend, squad_id)
            elif creating:
                created = create(backend, json.loads(body))
                # A full squad also satisfies SaveSquad's id-only parser.
                value = retrieve(backend, session, created['id'])
            elif method == 'PUT':
                payload = json.loads(body)
                if not isinstance(payload, dict) or payload.get('id') != squad_id:
                    raise ValueError('Squad ID mismatch')
                value = save(backend, payload)
                if set(payload) <= {'id','squadName'}:
                    value = retrieve(backend, session, squad_id)
            elif listing:
                summaries = []
                active_id = saved(backend)['id']
                order = [active_id]+sorted(k for k in squads(backend) if k != active_id)
                for squad_id in order:
                    squad = retrieve(backend, session, squad_id)
                    starters = [p['itemData'] for p in squad['players'] if p['index'] < 11]
                    squad['rating'] = sum(p['rating'] for p in starters)//len(starters) if starters else 0
                    summaries.append({k: squad[k] for k in ('id', 'squadName', 'squadType',
                                                          'formation', 'chemistry', 'rating')})
                value = {'squad': summaries}
            else:
                activate(backend, squad_id)
                value = retrieve(backend, session, squad_id)
            return json_reply(value, 'FUT.LocalSquad')
        except (ValueError, TypeError, KeyError):
            return json_reply({'error': 'Invalid local squad request'}, 'FUT.InvalidSquad', '400 Bad Request')
        except OSError:
            return json_reply({'error': 'Could not save squad; retry'}, 'FUT.SquadSaveFailed', '503 Service Unavailable')
