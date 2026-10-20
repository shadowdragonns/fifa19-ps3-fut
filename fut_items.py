"""Owned managers and consumables using the PS3 squad and ApplyCard contracts."""
from copy import deepcopy
import json
from pathlib import Path
import random

MANAGERS = json.loads((Path(__file__).parent/'data/support-items.json').read_text())['managers']
CATALOGUE = {row['resourceId']: dict(row, itemType='manager', cardsubtypeid=4,
                                  discardValue=250 if row['rareflag'] else 100)
             for row in MANAGERS}
for subtype, kind, name, amount, target in (
        (57, 'playerTraining', 'All attributes player training', 10, 'outfield'),
        (67, 'GKTraining', 'All attributes goalkeeper training', 10, 'keeper'),
        (201, 'contract', 'Player contracts', 28, 'player'),
        (202, 'contract', 'Manager contracts', 28, 'manager'),
        (219, 'fitness', 'Player fitness', 60, 'player'),
        (220, 'fitness', 'Squad fitness', 30, 'squad'),
        (218, 'healing', 'All injuries healing', 5, 'player')):
    asset = 6000000+subtype
    CATALOGUE[asset] = dict(assetId=asset, resourceId=asset, cardsubtypeid=subtype,
        itemType=kind, name=name, rating=80, rareflag=1, amount=amount,
        target=target, discardValue=50)
CONSUMABLES = [key for key, row in CATALOGUE.items() if row['itemType'] != 'manager']
TYPES = {row['itemType'] for row in CATALOGUE.values()}


def records(state): return state.get('supportItems', [])


def validate(state, player_ids):
    items = records(state)
    if not isinstance(items, list): raise ValueError('Invalid staff/consumable inventory')
    seen, managers = set(player_ids), set()
    for row in items:
        if (not isinstance(row, dict) or set(row)-{'id','definitionId','pile','timestamp','contract','leagueId'}
                or any(type(row.get(k)) is not int or row[k] < 1 for k in ('id','definitionId'))
                or row['definitionId'] not in CATALOGUE or row['id'] in seen
                or row.get('pile') not in ('club','purchased')
                or type(row.get('timestamp')) is not int or row['timestamp'] < 0
                or type(row.get('contract',7)) is not int or not 0 <= row.get('contract',7) <= 99
                or type(row.get('leagueId',0)) is not int or not 0 <= row.get('leagueId',0) < 2**31):
            raise ValueError('Invalid saved staff/consumable card')
        seen.add(row['id'])
        if row['pile'] == 'club' and CATALOGUE[row['definitionId']]['itemType'] == 'manager':
            if row['definitionId'] in managers: raise ValueError('Duplicate owned manager')
            managers.add(row['definitionId'])
    if seen and state['nextItemId'] <= max(seen): raise ValueError('Invalid next support item ID')


def card(record):
    spec = deepcopy(CATALOGUE[record['definitionId']])
    spec.pop('target', None)
    spec.update(id=record['id'], itemState='free', timestamp=record['timestamp'],
                owners=1, untradeable=False, itemQuantity=1)
    if spec['itemType'] == 'manager':
        spec.update(contract=record.get('contract',7), leagueId=record.get('leagueId',0))
    return spec


def draw(spec):
    rng = random.SystemRandom()
    if spec['support_type'] == 'manager':
        pool = [row['resourceId'] for row in MANAGERS]
        return rng.sample(pool, spec['size'])
    # Include training, contracts, fitness and healing in every supplies pack.
    pool = list(CONSUMABLES); rng.shuffle(pool)
    return pool[:spec['size']]


def search(store, query):
    from fut_inventory import integer
    allowed = {'type','year','start','count','level','rare','nation','league','state','filter','sort','defId','category'}
    if set(query)-allowed or any(len(v)!=1 for v in query.values()): raise ValueError('Invalid support search')
    values = {key: value[0] for key,value in query.items()}
    if values.get('year','2019') != '2019': raise ValueError('Invalid item year')
    kind = values.get('type','manager')
    if kind not in TYPES | {'staff','training','consumable','consumables','any'}: raise ValueError('Unknown item type')
    start,count = int(values.get('start',0)),int(values.get('count',100))
    if not integer(start,0,100000) or not integer(count,1,200): raise ValueError('Invalid support page')
    cards = [card(row) for row in records(store.state) if row['pile']=='club']
    if kind == 'staff': kind = 'manager'
    if kind == 'training': cards = [c for c in cards if c['itemType'] in ('playerTraining','GKTraining')]
    elif kind in ('consumable','consumables'): cards = [c for c in cards if c['itemType']!='manager']
    elif kind != 'any': cards = [c for c in cards if c['itemType']==kind]
    for key, field in (('rare','rareflag'),('nation','nation'),('league','leagueId'),('category','cardsubtypeid')):
        if key in values:
            value = int(values[key])
            if not integer(value,-1,2**31-1): raise ValueError('Invalid support filter')
            if value != -1: cards = [c for c in cards if c.get(field)==value]
    level = values.get('level','any')
    if level not in ('any','gold','silver','bronze'): raise ValueError('Invalid support tier')
    if level != 'any':
        cards = [c for c in cards if ('gold' if c['rating']>=75 else 'silver' if c['rating']>=65 else 'bronze')==level]
    if values.get('state','any') not in ('any','free') or values.get('filter','any') not in ('any','available','base','exact'):
        raise ValueError('Invalid support state/filter')
    if 'defId' in values:
        import re
        if not re.fullmatch(r'[0-9]{1,10}(?:,[0-9]{1,10}){0,99}',values['defId']): raise ValueError('Invalid support definition')
        ids = set(map(int,values['defId'].split(',')))
        cards = [c for c in cards if c['resourceId'] in ids or c['assetId'] in ids]
    if values.get('sort','desc') not in ('asc','desc'): raise ValueError('Invalid support sort')
    cards.sort(key=lambda c:(c['rating'],c['id']), reverse=values.get('sort','desc')=='desc')
    return {'itemData': cards[start:start+count]}


def apply(backend, source_id, payload, by_resource=False):
    from fut_inventory import Conflict, integer
    store = backend.fut_inventory
    if (not isinstance(payload,dict) or set(payload)!={'apply'} or not isinstance(payload['apply'],list)
            or not 1 <= len(payload['apply']) <= 23
            or any(not isinstance(row,dict) or set(row)!={'id'} or not integer(row['id'],1) for row in payload['apply'])):
        raise ValueError('Expected apply array of owned target IDs')
    ids = [row['id'] for row in payload['apply']]
    if len(ids)!=len(set(ids)): raise ValueError('Duplicate consumable targets')
    with backend.lock:
        for session in backend.root.sessions.values():
            if backend.root.for_session(session) is backend and any(
                    getattr(session,name,None) and not getattr(session,name).get('finished')
                    and not getattr(session,name).get('cancelled') for name in ('local_fut_match','online_fut_match')):
                raise Conflict('Apply consumables between matches')
        state = deepcopy(store.state)
        source = next((row for row in records(state) if row['pile']=='club' and
                       (row['definitionId'] if by_resource else row['id'])==source_id),None)
        if source is None: raise ValueError('Consumable is not owned by this club')
        spec = CATALOGUE[source['definitionId']]
        if spec['itemType']=='manager': raise ValueError('Managers cannot be consumed')
        owned = {row['id']:row for row in state['items'] if row['pile']=='club'}
        managers = {row['id']:row for row in records(state) if row['pile']=='club'
                    and CATALOGUE[row['definitionId']]['itemType']=='manager'}
        candidates = managers if spec['target']=='manager' else owned
        if any(i not in candidates for i in ids): raise ValueError('Target is not an owned eligible card')
        if spec['target'] != 'squad' and len(ids)!=1: raise ValueError('This card applies to one target')
        if spec['target']=='squad':
            squad = state.get('squad')
            selected = {slot['itemData']['id'] for slot in squad['players']} if squad else set()
            if not set(ids)<=selected: raise ValueError('Squad consumable targets must be in the active squad')
            if not any(candidates[i].get('fitness',99)<99 for i in ids):
                raise Conflict('Squad fitness is already full')
        for item_id in ids:
            target = candidates[item_id]
            if spec['itemType'] in ('playerTraining','GKTraining'):
                current = store.card(target,store.settings)
                if (current['preferredPosition']=='GK') != (spec['target']=='keeper'):
                    raise ValueError('Choose the appropriate outfield/goalkeeper training card')
                if target.get('trainingBoost'): raise Conflict('This player already has training applied')
                target['trainingBoost'] = [spec['amount']]*6
                target['trainingDefinition'] = source['definitionId']
                target['trainingItemId'] = source['id']
            elif spec['itemType']=='contract':
                if target.get('contract',7)>=99: raise Conflict('Contracts are already full')
                target['contract'] = min(99,target.get('contract',7)+spec['amount'])
            elif spec['itemType']=='fitness':
                if spec['target']!='squad' and target.get('fitness',99)>=99: raise Conflict('Fitness is already full')
                target['fitness'] = min(99,target.get('fitness',99)+spec['amount'])
            elif spec['itemType']=='healing':
                if not target.get('injuryGames'): raise Conflict('This player is not injured')
                target['injuryGames'] = max(0,target['injuryGames']-spec['amount'])
                if not target['injuryGames']: target['injuryType'] = 'none'
        state['supportItems'].remove(source)
        store.commit(state)
        backend.events.emit('consumable.applied', player=store.player(), item_id=source['id'],
                            definition_id=source['definitionId'], target_ids=ids)
        return {'itemData': [card(candidates[i]) if i in managers else store.card(candidates[i],store.settings) for i in ids]}
