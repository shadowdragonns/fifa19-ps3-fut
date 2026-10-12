"""Installed club equipment, separate from the player-card inventory.

Wembley is in both retail fcc_stadium and the installed gameplay stadium DB.
The active card is supplied in club and squad actives. The home selector
requires club equipment category 7, cardsubtypeid 10 and reads assetId.
Subtype 271 is a stadium modifier; it cannot supply the home venue.
"""
from copy import deepcopy
import json
from pathlib import Path

STADIUM = json.loads((Path(__file__).parent/'data/home-stadium.json').read_text(encoding='utf-8'))['card']
STADIUMS = json.loads((Path(__file__).parent/'data/club-stadiums.json').read_text(encoding='utf-8'))['cards']
VENUES = {card['stadiumid']: card for card in STADIUMS}


def active_assets(backend):
    # Older inventories use Wembley until a venue is explicitly selected.
    # Stadium choices are equipment, separate from purchased player cards.
    stadium = backend.fut_inventory.state.get('homeStadium', 155)
    if type(stadium) is not int or stadium not in VENUES:
        raise ValueError('Unknown local home stadium')
    return [c for c in equipment(backend.fut_inventory) if c['itemState'] in SLOTS]


EQUIPMENT = json.loads((Path(__file__).parent/'data/club-equipment.json').read_text(encoding='utf-8'))['cards']
CARDS = {card['id']: card for card in STADIUMS+EQUIPMENT}
SLOTS = {'activeHomeKit': 'kit', 'activeAwayKit': 'kit', 'activeBadge': 'badge',
         'activeBall': 'ball', 'activeStadium': 'stadium'}
DEFAULTS = {
    'activeHomeKit': next(c['id'] for c in EQUIPMENT if c.get('teamid') == 1318 and c['itemType'] == 'kit' and c['category'] == 2),
    'activeAwayKit': next(c['id'] for c in EQUIPMENT if c.get('teamid') == 1318 and c['itemType'] == 'kit' and c['category'] == 3),
    'activeBadge': next(c['id'] for c in EQUIPMENT if c.get('teamid') == 1318 and c['itemType'] == 'badge'),
    'activeBall': next(c['id'] for c in EQUIPMENT if c['itemType'] == 'ball'),
    'activeStadium': STADIUM['id'],
}


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) != set(SLOTS):
        raise ValueError('Invalid club equipment preferences')
    for slot, card_id in value.items():
        card = CARDS.get(card_id)
        if type(card_id) is not int or card is None or card['itemType'] != SLOTS[slot]:
            raise ValueError('Equipment does not fit the active slot')
    if value['activeHomeKit'] == value['activeAwayKit']:
        raise ValueError('Choose two different kits')


def equipment(store):
    selected = store.state.get('equipment')
    if selected is None:
        selected = {**DEFAULTS, 'activeStadium': VENUES[store.state.get('homeStadium', 155)]['id']}
    validate_preferences(selected)
    states = {card_id: slot for slot, card_id in selected.items()}
    return [{**deepcopy(c), 'itemState': states.get(c['id'], 'free')}
            for c in STADIUMS+EQUIPMENT]


def activate(backend, card_id, payload):
    # Native SetActiveItem 0x16b1e00/16b1efc appends /<id> and sends
    # {itemState:active, activateSlotNumber:"101"|"102"} for kits.
    if (not isinstance(payload, dict) or set(payload)-{'itemState', 'activateSlotNumber'}
            or payload.get('itemState') != 'active'):
        raise ValueError('Invalid equipment activation')
    card = CARDS.get(card_id)
    if card is None:
        raise ValueError('Unknown club equipment')
    slot = next((s for s,k in SLOTS.items() if k == card['itemType']), None)
    if card['itemType'] == 'kit':
        slot = {'101': 'activeHomeKit', '102': 'activeAwayKit'}.get(str(payload.get('activateSlotNumber')))
        if slot is None:
            raise ValueError('Choose home or away kit slot')
    elif 'activateSlotNumber' in payload:
        raise ValueError('Only kits take an activation slot')
    with backend.lock:
        state = deepcopy(backend.fut_inventory.state)
        selected = state.setdefault('equipment', {**DEFAULTS,
            'activeStadium': VENUES[state.get('homeStadium', 155)]['id']})
        # Switching the two already active kits exchanges their slots.
        other = 'activeAwayKit' if slot == 'activeHomeKit' else 'activeHomeKit'
        if card['itemType'] == 'kit' and selected[other] == card_id:
            selected[other] = selected[slot]
        selected[slot] = card_id
        if card['itemType'] == 'stadium':
            state['homeStadium'] = card['stadiumid']
        validate_preferences(selected)
        backend.fut_inventory.commit(state)
        from server_plugins import player
        backend.events.emit('equipment.changed', player=player(backend), slot=slot, item_id=card_id,
                            asset_id=card['assetId'])
        return {}  # Retail SetActiveItem uses the empty action result parser.


def search(store, query):
    from fut_inventory import integer
    kind = query.get('type', ['clubInfo'])[0]
    allowed = {'type', 'year', 'start', 'count', 'level', 'rare', 'state', 'filter',
               'sort', 'defId', 'team', 'league'}
    if (set(query)-allowed or any(len(v)!=1 for v in query.values())
            or query.get('year', ['2019']) != ['2019']
            or kind not in {'kit','badge','ball','stadium','clubInfo','custom'}):
        raise ValueError('Invalid equipment search')
    start, count = int(query.get('start', ['0'])[0]), int(query.get('count', ['100'])[0])
    if not integer(start,0,100000) or not integer(count,1,200):
        raise ValueError('Invalid equipment page')
    cards = [c for c in equipment(store) if kind in ('clubInfo','custom') or c['itemType'] == kind]
    for field, value_field in [('team','teamid'),('rare','rareflag')]:
        if field in query:
            number = int(query[field][0])
            if not integer(number,-1,2**31-1):
                raise ValueError('Invalid equipment filter')
            if number != -1:
                cards = [c for c in cards if c.get(value_field) == number]
    if 'league' in query:
        from fut_packs import STANDARD
        leagues = {p['teamid']: p['leagueId'] for p in STANDARD['players']}
        number = int(query['league'][0])
        if not integer(number,-1,2**31-1):
            raise ValueError('Invalid equipment league')
        if number != -1:
            cards = [c for c in cards if leagues.get(c.get('teamid')) == number]
    state = query.get('state',['any'])[0]
    if state not in ('any','free','active',*SLOTS):
        raise ValueError('Invalid equipment state')
    if state != 'any':
        cards = [c for c in cards if c['itemState'] == state or state == 'active' and c['itemState'] in SLOTS]
    level = query.get('level',['any'])[0]
    if level not in ('any','gold','silver','bronze'):
        raise ValueError('Invalid equipment level')
    if level != 'any':
        cards = [c for c in cards if ('gold' if c['rating']>=75 else 'silver' if c['rating']>=65 else 'bronze') == level]
    if 'defId' in query:
        import re
        if not re.fullmatch(r'[0-9]{1,10}(?:,[0-9]{1,10}){0,99}',query['defId'][0]):
            raise ValueError('Invalid equipment definitions')
        ids = set(map(int,query['defId'][0].split(',')))
        cards = [c for c in cards if c['resourceId'] in ids or c['assetId'] in ids]
    if query.get('sort',['desc'])[0] not in ('asc','desc') or query.get('filter',['any'])[0] not in ('any','available','base','exact'):
        raise ValueError('Invalid equipment sort/filter')
    cards.sort(key=lambda c:(c['rating'],c['id']),reverse=query.get('sort',['desc'])[0]=='desc')
    return {'itemData': cards[start:start+count]}
