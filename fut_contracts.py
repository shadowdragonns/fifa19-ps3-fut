"""Opt-in, club-scoped contract renewal using owned cards before coins."""
from copy import deepcopy
import time


def validate(state):
    if type(state.get('autoRenewContracts',False)) is not bool:
        raise ValueError('Invalid automatic contract preference')
    recent=state.get('lastContractRenewals')
    if recent is None:return
    from fut_inventory import integer
    if (not isinstance(recent,dict) or set(recent)!={'at','items','coins'}
            or not integer(recent['at']) or not integer(recent['coins'])
            or not isinstance(recent['items'],list) or not 1<=len(recent['items'])<=19):
        raise ValueError('Invalid contract renewal summary')
    for row in recent['items']:
        if (not isinstance(row,dict) or set(row)!={'id','kind','source_id','coins'}
                or not integer(row['id'],1) or row['kind'] not in ('player','manager')
                or not integer(row['source_id']) or not integer(row['coins'])
                or row['source_id'] and row['coins']):raise ValueError('Invalid contract renewal item')
    if recent['coins']!=sum(row['coins'] for row in recent['items']):raise ValueError('Invalid renewal total')


def info(backend):
    from fut_items import CATALOGUE
    return {'auto_renew':backend.fut_inventory.state.get('autoRenewContracts',False),
            **backend.settings.section('contracts'),
            'player_matches':CATALOGUE[6000201]['amount'],
            'manager_matches':CATALOGUE[6000202]['amount'],
            'last_renewal':deepcopy(backend.fut_inventory.state.get('lastContractRenewals'))}


def renew(state, squad, settings):
    """Mutate a transaction copy; caller commits coins, cards and result once."""
    if not state.get('autoRenewContracts',False) or not squad:return []
    from fut_items import CATALOGUE
    rules=settings.section('contracts');renewals=[]
    owned={row['id']:row for row in state['items'] if row['pile']=='club'}
    staff={row['id']:row for row in state.get('supportItems',[]) if row['pile']=='club'
           and CATALOGUE[row['definitionId']]['itemType']=='manager'}
    targets=[(owned.get(slot['itemData']['id']),'player',6000201)
             for slot in sorted(squad['players'],key=lambda row:row['index']) if slot['index']<18]
    targets.extend((staff.get(row['id']),'manager',6000202) for row in squad.get('manager',[]))
    seen=set()
    for target,kind,definition in targets:
        if target is None or target['id'] in seen or target.get('contract',7)>0:continue
        seen.add(target['id'])
        source=next((row for row in state.get('supportItems',[]) if row['pile']=='club'
                     and row['definitionId']==definition),None)
        cost=0 if source else rules[kind+'_price']
        if state['coins']<cost:continue
        state['coins']-=cost
        if source:state['supportItems'].remove(source)
        target['contract']=CATALOGUE[definition]['amount']
        renewals.append({'id':target['id'],'kind':kind,'source_id':source['id'] if source else 0,'coins':cost})
    if renewals:
        state['lastContractRenewals']={'at':int(time.time()),'items':renewals,'coins':sum(row['coins'] for row in renewals)}
    return renewals


def playing(backend):
    return any(backend.root.for_session(session) is backend and any(
        (match:=getattr(session,name,None)) and match.get('playing') and not match.get('finished')
        and not match.get('cancelled') for name in ('local_fut_match','online_fut_match'))
        for session in backend.root.sessions.values())


def ensure(backend):
    """Refresh expired active XI/bench/staff between matches, without repeat charges."""
    with backend.lock:
        store=backend.fut_inventory
        if not store.state.get('autoRenewContracts',False) or playing(backend):return
        state=deepcopy(store.state)
        if renew(state,state.get('squad'),backend.settings):store.commit(state)


def preference(backend, data):
    if not isinstance(data,dict) or set(data)!={'auto_renew'} or type(data['auto_renew']) is not bool:
        raise ValueError('Provide the automatic contract renewal switch.')
    with backend.lock:
        state=deepcopy(backend.fut_inventory.state)
        state['autoRenewContracts']=data['auto_renew']
        if not playing(backend):renew(state,state.get('squad'),backend.settings)
        if state!=backend.fut_inventory.state:backend.fut_inventory.commit(state)
        return info(backend)
