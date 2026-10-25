"""Admin club resets: private backup, offline guard and one inventory commit."""
from copy import deepcopy
import hashlib
import json
import os
import re
import time
import uuid

from admin_cards import EditError


def validate_marker(value):
    if (not isinstance(value,dict) or set(value)!={'id','at','actor','club'}
            or not isinstance(value['id'],str) or not re.fullmatch(r'[0-9a-f]{32}',value['id'])
            or type(value['at']) is not int or value['at']<0
            or not isinstance(value['actor'],str) or not re.fullmatch(r'[a-z0-9_]{3,32}',value['actor'])):
        raise ValueError('Invalid club reset record')
    club=value['club']
    if (not isinstance(club,dict) or set(club)!={'name','established','asset_id'}
            or not isinstance(club['name'],str) or not 1<=len(club['name'])<=32
            or any(type(club[k]) is not int or not 0<=club[k]<2**32 for k in ('established','asset_id'))):
        raise ValueError('Invalid reset club metadata')


def generation(state):return state.get('clubReset',{}).get('id','original')


def revision(owner):
    return hashlib.sha256(json.dumps({'inventory':owner.fut_inventory.state,
        'club':owner.profile['fut_club']},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def connected(owner):
    if owner.root.recovery:owner.root.recovery.purge()
    return any(s.postauth and owner.root.for_session(s) is owner for s in owner.root.sessions.values())


def info(owner):
    from fut_squads import display_name
    return {'revision':revision(owner),'club_name':display_name(owner),
            'starting_coins':owner.settings.section('economy')['starting_coins'],
            'connected':connected(owner)}


def private_json(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as out:
        json.dump(value,out,ensure_ascii=False,indent=2)
        out.write('\n');out.flush();os.fsync(out.fileno())


def reset(owner,payload,actor):
    from fut_inventory import Inventory
    from sessions import fresh_club
    if (not isinstance(payload,dict) or set(payload)!={'persona','revision','confirmation'}
            or type(payload['persona']) is not int or payload['persona']!=owner.profile['persona']
            or not isinstance(payload['revision'],str) or not re.fullmatch(r'[0-9a-f]{64}',payload['revision'])
            or not isinstance(payload['confirmation'],str)):
        raise EditError('Invalid club reset request.')
    with owner.root.lock:
        details=info(owner)
        if payload['confirmation']!=details['club_name']:
            raise EditError('Type the selected club name exactly.')
        if payload['revision']!=details['revision']:
            raise EditError('This club changed. Refresh it before resetting.',409)
        if details['connected']:
            raise EditError('Close FIFA for this club before resetting.',409)
        store=owner.fut_inventory;old=store.state
        new=Inventory.initial_state(owner.settings)
        # Do not reuse old card/pack identifiers for a new club generation.
        new['nextItemId']=max(new['nextItemId'],old['nextItemId'])
        new['nextPackId']=max(new['nextPackId'],old['nextPackId'])
        marker={'id':uuid.uuid4().hex,'at':int(time.time()),'actor':actor,'club':fresh_club()}
        new['clubReset']=marker;Inventory.validate(new)
        if owner.path is None or owner.root.path is None:raise EditError('A saved club is required for reset.',409)
        backup=owner.root.path.parent/'club-reset-backups'/str(owner.profile['persona'])/marker['id']
        backup.mkdir(parents=True,mode=0o700,exist_ok=False)
        private_json(backup/'profile.json',owner.profile)
        private_json(backup/'fut-inventory.json',old)
        private_json(backup/'reset.json',{'id':marker['id'],'at':marker['at'],
            'actor':actor,'persona':owner.profile['persona'],'revision':details['revision']})
        store.commit(new)
        # Metadata is durable inside the same inventory transaction. Startup
        # overlays it if a crash occurs before a subsequent profile save.
        owner.profile['fut_club']=deepcopy(marker['club'])
        owner.profile['clubGeneration']=marker['id']
        owner.events.emit('club.reset',player=store.player(),actor=actor,reset_id=marker['id'])
        return {'reset':True,'persona':owner.profile['persona'],
                'club_name':marker['club']['name'],'coins':new['coins']}
