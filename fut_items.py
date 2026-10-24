"""Owned managers and consumables using the PS3 squad and ApplyCard contracts."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import random

MANAGERS = json.loads((Path(__file__).parent/'data/support-items.json').read_text(encoding='utf-8'))['managers']
CATALOGUE = {row['resourceId']: dict(row, itemType='manager', cardsubtypeid=4,
                                  discardValue=250 if row['rareflag'] else 100)
             for row in MANAGERS}
for subtype, resource, kind, name, amount, target in (
        (57, 5000000, 'playerTraining', 'All attributes player training', 10, 'outfield'),
        (67, 5000001, 'GKTraining', 'All attributes goalkeeper training', 10, 'keeper'),
        (201, 5000007, 'contract', 'Player contracts', 28, 'player'),
        (202, 5000008, 'contract', 'Manager contracts', 28, 'manager'),
        (219, 5000010, 'fitness', 'Player fitness', 60, 'player'),
        (220, 5000011, 'fitness', 'Squad fitness', 30, 'squad'),
        (218, 5000009, 'healing', 'All injuries healing', 5, 'player')):
    # Retain saved definition IDs, but send the installed PS3 art resource.
    # The native consumable constructor derives assetId from resourceId.
    asset = 6000000+subtype
    # Retail 0x1d1e214 identifies 51..57 as GK training; 0x1d1e23c
    # identifies 61..67 as outfield training. Keep saved definition IDs and
    # their effects stable while correcting the wire subtype/UI eligibility.
    native_subtype = {57:67,67:57}.get(subtype,subtype)
    CATALOGUE[asset] = dict(assetId=resource, resourceId=resource, cardsubtypeid=native_subtype,
        itemType=kind, name=name, rating=80, rareflag=1, amount=amount,
        target=target, discardValue=50)
class WrongTrainingTarget(ValueError):pass


# Retail PS3 subtypes 91..110 (71..86 are obsolete formation cards).
POSITION_CHANGES = dict(enumerate((
    ('LWB','LB'), ('LB','LWB'), ('RWB','RB'), ('RB','RWB'),
    ('LM','LW'), ('RM','RW'), ('LW','LM'), ('RW','RM'),
    ('LW','LF'), ('RW','RF'), ('LF','LW'), ('RF','RW'),
    ('CM','CAM'), ('CAM','CM'), ('CDM','CM'), ('CM','CDM'),
    ('CAM','CF'), ('CF','CAM'), ('CF','ST'), ('ST','CF')), 91))
CHEMISTRY_STYLES = dict(enumerate((
    'Basic', 'Sniper', 'Finisher', 'Deadeye', 'Marksman', 'Hawk',
    'Artist', 'Architect', 'Powerhouse', 'Maestro', 'Engine',
    'Sentinel', 'Guardian', 'Gladiator', 'Backbone', 'Anchor',
    'Hunter', 'Catalyst', 'Shadow', 'Wall', 'Shield', 'Cat', 'Glove',
    'Goalkeeper Basic'), 250))
# Keep the original seven supplies guaranteed in their existing pack.
SUPPLIES = [key for key, row in CATALOGUE.items() if row['itemType'] != 'manager']
for subtype, (origin, destination) in POSITION_CHANGES.items():
    definition = 6000000+subtype
    CATALOGUE[definition] = dict(assetId=definition, resourceId=definition,
        cardsubtypeid=subtype, itemType='position', name=f'{origin} > {destination}',
        rating=80, rareflag=1, amount=0, target='player', discardValue=50,
        sourcePosition=origin, destinationPosition=destination)
for subtype, name in CHEMISTRY_STYLES.items():
    definition = 6000000+subtype
    # Native bio rendering replaces these unique resources with installed
    # position art 5000013 or style art 5000015_4. The unique resource lets
    # ApplyCardByRes select the correct conversion/style from owned stacks.
    CATALOGUE[definition] = dict(assetId=definition, resourceId=definition,
        cardsubtypeid=subtype, itemType='playStyle', name=name,
        # Native compatibility accepts amount 4 for every nonempty player
        # tier. Zero is not eligible. It also selects the _4 artwork suffix.
        rating=80, rareflag=1, amount=4,
        target='keeper' if subtype>=269 else 'outfield', discardValue=50)


CONSUMABLES = [key for key, row in CATALOGUE.items() if row['itemType'] != 'manager']
TYPES = {row['itemType'] for row in CATALOGUE.values()}
CONSUMABLE_TYPES = {
    'consumables': set(CONSUMABLES),
    'consumablesContract': {6000201,6000202},
    'consumablesContractPlayer': {6000201}, 'consumablesContractManager': {6000202},
    'consumablesTraining': {6000057,6000067},
    'consumablesTrainingPlayer': {6000057}, 'consumablesTrainingGk': {6000067},
    'consumablesFitness': {6000219,6000220},
    'consumablesFitnessPlayer': {6000219}, 'consumablesFitnessTeam': {6000220},
    'consumablesHealing': {6000218},
    'consumablesFormationManager': set(), 'consumablesTrainingManagerLeagueModifier': set(),
    'consumablesTrainingPlayerPlayStyle': {6000000+i for i in range(250,269)},
    'consumablesTrainingGkPlayStyle': {6000000+i for i in range(269,274)},
    'consumablesPosition': {6000000+i for i in POSITION_CHANGES}, 'consumablesTrainingManager': set(),
}
CONSUMABLE_CATEGORY_IDS = {
    'consumables':60, 'consumablesContractPlayer':66, 'consumablesContractManager':71,
    'consumablesFitnessPlayer':68, 'consumablesFitnessTeam':74,
    'consumablesFormationManager':72, 'consumablesTrainingManagerLeagueModifier':77,
    'consumablesHealing':65, 'consumablesTrainingPlayerPlayStyle':75,
    'consumablesTrainingGkPlayStyle':76, 'consumablesPosition':69,
    'consumablesTrainingPlayer':67, 'consumablesTrainingManager':73,
    'consumablesTrainingGk':70,
}
ITEM_LABELS = {'LeagueName_0':'No league', 'LeagueName_Abbr15_0':'No league',
               'LAbbr_0':'None'}


def records(state): return state.get('supportItems', [])


def club_statistics(store, kind):
    """Native LoadClubStats stat array and LoadStaffBonus bonus array."""
    counts = Counter(row['definitionId'] for row in records(store.state) if row['pile']=='club')
    if kind == 'staff':
        # Supplies currently grant their fixed amount; do not advertise an
        # additional staff multiplier that the save transaction does not use.
        return {'bonus': [{'type':'contract','value':0},
                          {'type':'managerTalk','value':0}]}
    return {'stat': [{'contextId':0,'contextValue':0,'type':name,
                      'typeValue':sum(counts[key] for key in CONSUMABLE_TYPES[name])}
                     for name in CONSUMABLE_CATEGORY_IDS]}


def consumable_list(store, selector, query):
    """Retail LoadConsumables needs stacks, not ordinary itemData cards."""
    from fut_inventory import integer
    if set(query)-{'start','count'} or any(len(v)!=1 for v in query.values()):
        raise ValueError('Invalid consumable list query')
    start,count = int(query.get('start',['0'])[0]),int(query.get('count',['100'])[0])
    if not integer(start,0,100000) or not integer(count,1,200):
        raise ValueError('Invalid consumable list page')
    # Retail LoadConsumables uses "development" for the complete owned
    # supplies list before the apply screen filters compatible targets.
    aliases = {'development':'consumables','all':'consumables','any':'consumables',
               'positioning':'consumablesPosition','contracts':'consumablesContract','contract':'consumablesContract','training':'consumablesTraining',
               'playerTraining':'consumablesTrainingPlayer','GKTraining':'consumablesTrainingGk',
               'fitness':'consumablesFitness','healing':'consumablesHealing',
               'position':'consumablesPosition', 'playStyle':'consumablesTrainingPlayerPlayStyle',
               'GKPlayStyle':'consumablesTrainingGkPlayStyle'}
    # UI enum IDs returned by retail 0x16e0474.
    aliases.update({str(ident):name for name,ident in CONSUMABLE_CATEGORY_IDS.items()})
    definitions = CONSUMABLE_TYPES.get(aliases.get(selector,selector))
    if definitions is None: raise ValueError('Unknown consumable category')
    groups = {}
    for row in records(store.state):
        if row['pile']!='club' or row['definitionId'] not in definitions: continue
        group = groups.get(row['definitionId'])
        if group is None:
            item = card(row)
            group = groups[row['definitionId']] = {'item':item,'count':0,
                'untradeableCount':0,'discardValue':item['discardValue']}
        group['count'] += 1
    return {'itemData': [groups[key] for key in sorted(groups)][start:start+count]}


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
    spec.pop('sourcePosition', None)
    spec.pop('destinationPosition', None)
    spec.update(id=record['id'], itemState='free', timestamp=record['timestamp'],
                owners=1, untradeable=False, itemQuantity=1)
    if spec['itemType'] == 'manager':
        spec.update(contract=record.get('contract',7), leagueId=record.get('leagueId',spec.get('leagueId',0)))
    elif spec['itemType'] == 'contract':
        # Native contracts display/eligibility reads these three fields,
        # separately from the generic amount used by training and fitness.
        spec.update(bronze=spec['amount'],silver=spec['amount'],gold=spec['amount'])
    return spec


def draw(spec):
    rng = random.SystemRandom()
    if spec['support_type'] == 'manager':
        pool = [row['resourceId'] for row in MANAGERS]
        return rng.sample(pool, spec['size'])
    if spec['support_type'] == 'contracts':
        return [6000201]*10+[6000202]*2
    if spec['support_type'] in ('position','playStyle'):
        return rng.sample([key for key,row in CATALOGUE.items()
                           if row['itemType']==spec['support_type']], spec['size'])
    # Include training, contracts, fitness and healing in every supplies pack.
    pool = list(SUPPLIES); rng.shuffle(pool)
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
        from fut_condition import fitness as current_fitness
        source = next((row for row in records(state) if row['pile']=='club' and
                       (source_id in (row['definitionId'],CATALOGUE[row['definitionId']]['resourceId'])
                        if by_resource else row['id']==source_id)),None)
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
            if not any(current_fitness(candidates[i],store.settings)<99 for i in ids):
                raise Conflict('Squad fitness is already full')
        for item_id in ids:
            target = candidates[item_id]
            if spec['itemType']=='position':
                if store.card(target,store.settings)['preferredPosition'] != spec['sourcePosition']:
                    raise Conflict('This position card requires a '+spec['sourcePosition']+' player')
                target['position'] = spec['destinationPosition']
                # A paid conversion supersedes an older admin position edit;
                # independent rating/attribute overrides remain intact.
                target.get('overrides',{}).pop('position',None)
            elif spec['itemType']=='playStyle':
                current = store.card(target,store.settings)
                if (current['preferredPosition']=='GK') != (spec['target']=='keeper'):
                    raise WrongTrainingTarget('Use a chemistry style for this player type')
                if current['playStyle']==spec['cardsubtypeid']:
                    raise Conflict('This player already has this chemistry style')
                target['playStyle'] = spec['cardsubtypeid']
            elif spec['itemType'] in ('playerTraining','GKTraining'):
                current = store.card(target,store.settings)
                if (current['preferredPosition']=='GK') != (spec['target']=='keeper'):
                    raise WrongTrainingTarget('Use goalkeeper training for a goalkeeper.' if spec['target']=='outfield'
                                              else 'This is a goalkeeper training card. Use outfield training for this player.')
                if target.get('trainingBoost'): raise Conflict('This player already has training applied')
                target['trainingBoost'] = [spec['amount']]*6
                target['trainingDefinition'] = source['definitionId']
                target['trainingItemId'] = source['id']
            elif spec['itemType']=='contract':
                if target.get('contract',7)>=99: raise Conflict('Contracts are already full')
                target['contract'] = min(99,target.get('contract',7)+spec['amount'])
            elif spec['itemType']=='fitness':
                if spec['target']!='squad' and current_fitness(target,store.settings)>=99: raise Conflict('Fitness is already full')
                target['fitness'] = min(99,current_fitness(target,store.settings)+spec['amount'])
                import time
                target['fitnessUpdatedAt'] = int(time.time())
            elif spec['itemType']=='healing':
                if not target.get('injuryGames'): raise Conflict('This player is not injured')
                target['injuryGames'] = max(0,target['injuryGames']-spec['amount'])
                if not target['injuryGames']: target['injuryType'] = 'none'
        # ApplyCard/ApplyCardByRes invalidate the native consumable caches
        # before making the request. Their frontend success callback still
        # looks up the selected supply by its owned ID to read its subtype.
        # Return its definition as well as the updated targets so that lookup
        # works even when this was the last copy in the stack. This snapshot
        # is only for the response; the consumed item stays out of the save
        # and subsequent club/stack searches.
        applied_card = card(source)
        updated_cards = [card(candidates[i]) if i in managers else store.card(candidates[i],store.settings) for i in ids]
        state['supportItems'].remove(source)
        store.commit(state)
        backend.events.emit('consumable.applied', player=store.player(), item_id=source['id'],
                            definition_id=source['definitionId'], target_ids=ids)
        return {'itemData': updated_cards + [applied_card]}
