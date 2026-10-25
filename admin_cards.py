"""Operator-authorized edits to individual owned cards, independent of editions."""
from copy import deepcopy
import hashlib
import json
import re
import time

from card_versions import MAX_OVR,MAX_ATTRIBUTE,POSITIONS


class EditError(ValueError):
    def __init__(self,message,status=400):self.status=status;super().__init__(message)


def integer(value,low=0,high=2**63-1):return type(value) is int and low<=value<=high


def validate_overrides(value):
    if (not isinstance(value,dict) or set(value)-{'rating','attributes','position'}
            or 'rating' in value and not integer(value['rating'],1,MAX_OVR)
            or 'position' in value and (not isinstance(value['position'],str) or value['position'] not in POSITIONS)
            or 'attributes' in value and (not isinstance(value['attributes'],list) or len(value['attributes'])!=6
                or any(not integer(v,0,MAX_ATTRIBUTE) for v in value['attributes']))):
        raise ValueError('Use OVR 1–255, six attributes 0–4294967295 and a valid position.')


def apply_overrides(player,value):
    validate_overrides(value);player=deepcopy(player)
    if 'rating' in value:player['rating']=value['rating']
    if 'position' in value:player['preferredPosition']=value['position']
    if 'attributes' in value:
        for stat,number in zip(player['attributeList'],value['attributes']):stat['value']=number
    return player


def revision(record):
    return hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def snapshot(store,record):
    from fut_inventory import PLAYERS
    from fut_packs import STANDARD_PLAYERS
    card=store.card(record,store.settings)
    # Show editable attributes before temporary match training is added.
    without_training={k:v for k,v in record.items() if k not in ('trainingBoost','trainingDefinition','trainingItemId')}
    editable=store.card(without_training,store.settings)
    player=(PLAYERS if record.get('edition','england110')=='england110' else STANDARD_PLAYERS)[record['assetId']]
    return {'id':record['id'],'asset_id':record['assetId'],'name':player['name'],
            'edition':record.get('edition','england110'),'rating':editable['rating'],
            'attributes':[r['value'] for r in editable['attributeList']],
            'position':editable['preferredPosition'],'training':card['training'],
            'fitness':card['fitness'],'contract':card['contract'],'injuryGames':card['injuryGames'],
            'injuryType':card['injuryType'],'suspension':card['suspension'],
            'overrides':deepcopy(record.get('overrides',{})),'revision':revision(record)}


def inventory(owner,payload):
    from fut_squads import squads,saved
    if (set(payload)-{'persona','squad_id','search','page'} or not integer(payload.get('persona'),1)
            or payload.get('squad_id') is not None and not integer(payload['squad_id'],1)
            or not isinstance(payload.get('search',''),str) or len(payload.get('search',''))>80
            or not integer(payload.get('page',0),0,100000)):raise EditError('Invalid club selection.')
    with owner.lock:
        from fut_condition import initialize_recovery
        initialize_recovery(owner.fut_inventory)
        all_squads=squads(owner);selected=payload.get('squad_id')
        if selected is not None and selected not in all_squads:raise EditError('Squad no longer exists. Refresh the club.')
        ids=None if selected is None else {p['itemData']['id'] for p in all_squads[selected]['players']}
        store=owner.fut_inventory;cards=[];search=payload.get('search','').casefold().strip()
        for record in store.state['items']:
            if record['pile']!='club' or ids is not None and record['id'] not in ids:continue
            row=snapshot(store,record)
            if search and search not in (row['name']+' '+str(row['id'])+' '+row['edition']).casefold():continue
            cards.append(row)
        cards.sort(key=lambda r:(-r['rating'],r['name'],r['id']))
        page=payload.get('page',0)
        from club_reset import info as reset_info
        return {'persona':owner.profile['persona'],'club_name':owner.profile['fut_club']['name'],
                'reset':reset_info(owner),
                'squads':[{'id':i,'name':s['squadName'],'active':i==saved(owner)['id']} for i,s in all_squads.items()],
                'cards':cards[page*100:(page+1)*100],'total':len(cards),'page':page,'page_size':100,
                'positions':sorted(POSITIONS)}


def edit(owner,payload,actor):
    required={'persona','item_id','revision','overrides','condition'}
    if (set(payload)!=required or not integer(payload['persona'],1) or not integer(payload['item_id'],1)
            or not isinstance(payload['revision'],str) or not re.fullmatch(r'[0-9a-f]{64}',payload['revision'])
            or not isinstance(payload['condition'],dict)
            or set(payload['condition'])-{'fitness','contract','injuryGames','injuryType','suspension'}):
        raise EditError('Invalid card edit.')
    validate_overrides(payload['overrides'])
    with owner.lock:
        for session in owner.root.sessions.values():
            if owner.root.for_session(session) is owner and any(
                getattr(session,k,None) and not getattr(session,k).get('finished') and not getattr(session,k).get('cancelled')
                for k in ('local_fut_match','online_fut_match')):
                raise EditError('Leave the match or matchmaking screen before editing this club.',409)
        store=owner.fut_inventory
        record=next((r for r in store.state['items'] if r['id']==payload['item_id'] and r['pile']=='club'),None)
        if record is None:raise EditError('Owned card no longer exists.',404)
        if revision(record)!=payload['revision']:raise EditError('This card changed. Reload it before saving.',409)
        state=deepcopy(store.state);target=next(r for r in state['items'] if r['id']==record['id'])
        if payload['overrides']:target['overrides']=deepcopy(payload['overrides'])
        else:target.pop('overrides',None)
        target.update(deepcopy(payload['condition']))
        if 'fitness' in payload['condition']:target['fitnessUpdatedAt']=int(time.time())
        if 'injuryGames' in payload['condition']:
            if target['injuryGames']==0:target['injuryType']='none'
            elif target.get('injuryType','none')=='none':target['injuryType']='leg'
        from fut_condition import validate
        validate(target)
        if target==record:return {'card':snapshot(store,record),'changed':False}
        fields={'overrides','fitness','fitnessUpdatedAt','contract','injuryGames','injuryType','suspension'}
        audit={'at':int(time.time()),'actor':actor,'itemId':record['id'],
               'before':{k:deepcopy(v) for k,v in record.items() if k in fields},
               'after':{k:deepcopy(v) for k,v in target.items() if k in fields}}
        state['adminCardEdits']=(state.get('adminCardEdits',[])+[audit])[-500:]
        store.commit(state)
        store.events.emit('card.edited',player=store.player(),item_id=target['id'],actor=actor,
                          fields=sorted(set(payload['overrides'])|set(payload['condition'])))
        return {'card':snapshot(store,target),'changed':True}


def validate_audit(state):
    rows=state.get('adminCardEdits',[])
    if not isinstance(rows,list) or len(rows)>500:raise ValueError('Invalid card edit history')
    for row in rows:
        if (not isinstance(row,dict) or set(row)!={'at','actor','itemId','before','after'}
                or not integer(row['at']) or not integer(row['itemId'],1)
                or not isinstance(row['actor'],str) or not re.fullmatch(r'[a-z0-9_]{3,32}',row['actor'])):
            raise ValueError('Invalid card edit receipt')
        for k in ('before','after'):
            value=row[k]
            if not isinstance(value,dict) or set(value)-{'overrides','fitness','fitnessUpdatedAt','contract','injuryGames','injuryType','suspension'}:
                raise ValueError('Invalid card edit snapshot')
            validate_overrides(value.get('overrides',{}))
            from fut_condition import validate
            validate(value)
