"""Owned CPU knockout cups using the retail tournament SDK routes.

The client builds and serializes its bracket. Results, advancement and coin
prizes are authoritative and committed with the owned match receipt.
"""
from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, urlsplit

from fut_inventory import integer
from fut_seasons import CPU_TIERS, decode_envelope
from pow_services import json_reply

BASE = '/ut/game/fifa19/tournament'
USER = BASE+'/user'
QUIT = '/ut/delete/game/fifa19/tournament/user/'
JOINED = '/ut/game/fifa19/user/action/JOINED'
CUPS = {
    1001: {'name':'Local Starter Cup','difficulty':1,'tier':0,'prize':1000},
    1002: {'name':'Local Champions Cup','difficulty':3,'tier':6,'prize':2500},
    1003: {'name':'Local World Cup','difficulty':4,'tier':9,'prize':5000},
}


def cup_id(value):
    if type(value) is not int or value not in CUPS:raise ValueError('Unknown CPU cup')
    return value


def teams(identifier):
    cup=CUPS[cup_id(identifier)];tier=cup['tier']
    # Fifteen distinct opponents plus the user's club make a sixteen-team draw.
    pool=list(dict.fromkeys(CPU_TIERS[tier]+CPU_TIERS[max(0,tier-1)]+CPU_TIERS[min(9,tier+1)]+tuple(t for group in reversed(CPU_TIERS) for t in group)))
    return pool[:15]


def definitions(backend=None):
    # Retail definition 0x16e1ff4: lowercase mode, singular tournament array,
    # one-based round IDs, numeric difficulty and an awards wrapper.
    return {'tournament':[{
        'id':identifier,'type':'offline','localizedName':cup['name'],
        'assetName':'t1','silhouetteName':'t1','imageFormat':'dds',
        'starttime':1,'endtime':2147483647,'timeUntilStart':0,'timeUntilEnd':2147483647,
        'visStart':0,'visEnd':0,'unlockreq':0,'trophyResourceId':1,'trophyUserCount':saved(backend.fut_inventory.state,identifier)['championships'] if backend else 0,
        'matchlength':4,'aigroup':identifier,'numTeams':16,'numRounds':4,'treeType':'knockout',
        'eligibilityOperation':'AND','elgReq':[],
        'rounds':[{'id':i,'difficulty':cup['difficulty'],'rewardMultiplier':100,'coins':0} for i in range(1,5)],
        'awardSet':{'awards':[{'awardType':1,'value':cup['prize'],'halid':0}]}}
        for identifier,cup in CUPS.items()]}


def fresh(cycle=0, championships=0):
    # Empty data is intentional: LoadData's 0x16e2f68 branch leaves the native
    # bracket empty, so FIFA constructs the initial draw from TournamentTeams.
    return {'cycle':cycle,'championships':championships,'wins':0,'status':'active','matchIds':[],
            'round':0,'dataVersion':'2','tournamentData':''}


def saved(state, identifier):
    return deepcopy(state.get('offlineTournaments',{}).get(str(cup_id(identifier)),fresh()))


def progress(backend, identifier):
    row=saved(backend.fut_inventory.state,identifier)
    return {key:deepcopy(row[key]) for key in ('round','dataVersion','tournamentData')}


def reserve(backend, identifier):
    row=saved(backend.fut_inventory.state,identifier)
    if row['status']!='active':
        row=fresh(row['cycle']+1,row['championships'])
        state=deepcopy(backend.fut_inventory.state)
        state.setdefault('offlineTournaments',{})[str(identifier)]=row
        backend.fut_inventory.commit(state)
    return {'id':identifier,'cycle':row['cycle'],'round':row['wins']+1}


def apply_result(state, current, payload):
    reservation=current.get('tournament')
    if reservation is None:return None
    identifier=cup_id(reservation['id']);row=saved(state,identifier)
    if (row['status']!='active' or row['cycle']!=reservation['cycle']
            or row['wins']+1!=reservation['round']):raise ValueError('Cup reservation is no longer current')
    won=payload['endReason']=='WIN'
    row['matchIds'].append(current['id'])
    if won:
        row['wins']+=1
        if row['wins']==4:row['status']='won';row['championships']+=1
    else:row['status']='lost'
    prize=CUPS[identifier]['prize'] if row['status']=='won' else 0
    state.setdefault('offlineTournaments',{})[str(identifier)]=row
    return {**reservation,'prize':prize}


def upload(backend, session, identifier, payload):
    required={'round','dataVersion','tournamentData'};aux={'progressDataVersion','progressData'}
    if (not isinstance(payload,dict) or set(payload) not in (required,required|aux)
            or not integer(payload['round'],0,5) or type(payload['dataVersion']) is not int
            or payload['dataVersion']!=2):raise ValueError('Invalid cup progress')
    decode_envelope(payload['tournamentData'],maximum=65536)
    if 'progressData' in payload:
        if type(payload['progressDataVersion']) is not int or payload['progressDataVersion']!=2:
            raise ValueError('Invalid auxiliary cup version')
        decode_envelope(payload['progressData'],minimum=0,maximum=65536)
    row=saved(backend.fut_inventory.state,identifier)
    value={key:deepcopy(payload[key]) for key in payload}
    value['dataVersion']='2'
    if 'progressDataVersion' in value:value['progressDataVersion']='2'
    if all(row.get(k)==v for k,v in value.items()):return {'result':'SUCCESS'}
    current=getattr(session,'local_fut_match',None)
    if current and current.get('playing') and not current.get('finished'):
        raise ValueError('Finish the current match before updating its bracket')
    marker=row['matchIds'][-1] if row['matchIds'] else 'initial'
    if not row['matchIds'] and payload['round'] not in (0,1):raise ValueError('No completed cup match')
    # Opaque native bracket bytes are never trusted to advance a round or pay
    # coins. Only a committed owned match can change wins/status/championships.
    row.update(value,uploadedMatchId=marker)
    state=deepcopy(backend.fut_inventory.state)
    state.setdefault('offlineTournaments',{})[str(identifier)]=row
    backend.fut_inventory.commit(state)
    return {'result':'SUCCESS'}


def quit_cup(backend, session, identifier):
    current=getattr(session,'local_fut_match',None)
    if current and not current.get('finished') and current.get('request',{}).get('tournamentId')==identifier:
        if current.get('playing'):raise ValueError('End the current match before leaving its cup')
        session.local_fut_match=None
    row=saved(backend.fut_inventory.state,identifier)
    if row==fresh(row['cycle'],row['championships']):return {}
    state=deepcopy(backend.fut_inventory.state)
    state.setdefault('offlineTournaments',{})[str(identifier)]=fresh(row['cycle']+1,row['championships'])
    backend.fut_inventory.commit(state)
    return {}


def validate_state(state):
    values=state.get('offlineTournaments',{})
    if not isinstance(values,dict) or set(values)-{str(i) for i in CUPS}:raise ValueError('Invalid saved cups')
    receipts={r['id']:r for r in state.get('matchHistory',[])}
    for key,row in values.items():
        required=set(fresh());optional={'uploadedMatchId','progressDataVersion','progressData'}
        if (not isinstance(row,dict) or not required<=set(row)<=required|optional
                or any(not integer(row[k]) for k in ('cycle','championships'))
                or not integer(row['wins'],0,4) or row['status'] not in ('active','won','lost')
                or (row['wins']==4)!=(row['status']=='won')
                or not integer(row['round'],0,5) or row['dataVersion']!='2'
                or not isinstance(row['tournamentData'],str)
                or not isinstance(row['matchIds'],list) or len(row['matchIds'])>4
                or len(set(row['matchIds']))!=len(row['matchIds'])):raise ValueError('Invalid saved cup')
        if row['tournamentData']:decode_envelope(row['tournamentData'])
        if ('progressDataVersion' in row)!=('progressData' in row):raise ValueError('Invalid cup auxiliary data')
        if 'progressData' in row:
            if row['progressDataVersion']!='2':raise ValueError('Invalid cup auxiliary version')
            decode_envelope(row['progressData'],minimum=0)
        if 'uploadedMatchId' in row and row['uploadedMatchId'] not in ['initial']+row['matchIds']:
            raise ValueError('Unknown cup upload receipt')
        expected_wins=0
        for n,match_id in enumerate(row['matchIds'],1):
            receipt=receipts.get(match_id,{})
            metadata=receipt.get('tournament',{})
            if (metadata.get('id')!=int(key) or metadata.get('cycle')!=row['cycle'] or metadata.get('round')!=n
                    or expected_wins!=n-1):raise ValueError('Invalid cup match reference')
            expected_wins+=receipt['summary']['endReason']=='WIN'
        if expected_wins!=row['wins'] or len(row['matchIds'])!=row['wins']+(row['status']=='lost'):
            raise ValueError('Cup counters differ from saved results')


def response(backend, method, path, raw):
    from fut_services import request_headers,session_for
    parsed=urlsplit(path);route=parsed.path
    selected=re.fullmatch(re.escape(USER)+r'/([1-9][0-9]*)',route)
    leaving=re.fullmatch(re.escape(QUIT)+r'([1-9][0-9]*)',route)
    if route not in (BASE+'/list',BASE+'/teams',USER+'/list',JOINED) and not selected and not leaving:return None
    methods=('POST','PUT') if route==JOINED else ('POST','DELETE') if leaving else ('GET','PUT','POST') if selected else ('GET',)
    if method not in methods:return json_reply({'error':'Method not allowed'},'FUT.CupWrongMethod','405 Method Not Allowed',[('Allow',', '.join(methods))])
    head,_,body=raw.partition(b'\r\n\r\n')
    with backend.lock:
        session=session_for(backend,request_headers(head))
        if session is None:return json_reply({'error':'Local FUT session required'},'FUT.LoginRequired','401 Unauthorized')
        owner=backend.for_session(session)
        try:
            if (method=='GET' or leaving) and body:raise ValueError('Unexpected cup request body')
            query=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=4)
            if any(len(v)!=1 for v in query.values()):raise ValueError('Repeated cup selector')
            if route==BASE+'/list':
                if set(query)-{'active','count','type'} or query.get('active',['true'])[0]!='true':raise ValueError('Invalid cup list')
                if not re.fullmatch(r'[1-9][0-9]{0,2}',query.get('count',['99'])[0]):raise ValueError('Invalid cup count')
                mode=query.get('type',['offline'])[0].lower()
                if mode not in ('offline','online'):raise ValueError('Invalid cup mode')
                value=definitions(owner) if mode=='offline' else {'tournament':[]}
            elif route==BASE+'/teams':
                if set(query)!={'groupId','count'} or any(not re.fullmatch(r'[1-9][0-9]{0,8}',v[0]) for v in query.values()):
                    raise ValueError('Invalid cup opponent query')
                value={'teamId':teams(int(query['groupId'][0]))[:min(int(query['count'][0]),15)]}
            elif route==JOINED:
                if query or body:raise ValueError('Unexpected tournament action body')
                value={'result':'SUCCESS'}
            elif route==USER+'/list':
                if query:raise ValueError('Unexpected cup list query')
                value={'tournamentId':[int(k) for k,r in owner.fut_inventory.state.get('offlineTournaments',{}).items()
                                       if r['status']=='active' and (r['tournamentData'] or r['matchIds'])]}
            else:
                if query:raise ValueError('Unexpected cup progress query')
                identifier=cup_id(int((selected or leaving)[1]))
                value=(quit_cup(owner,session,identifier) if leaving else progress(owner,identifier) if method=='GET'
                       else upload(owner,session,identifier,json.loads(body)))
            return json_reply(value,'FUT.OfflineTournament')
        except (ValueError,KeyError,TypeError,OverflowError):
            return json_reply({'error':'Invalid local cup request'},'FUT.InvalidTournament','400 Bad Request')
