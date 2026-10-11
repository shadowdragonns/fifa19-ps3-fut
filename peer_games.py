"""Ephemeral two-player FUT GameManager sessions for peer connection testing.

Wire tags, enums and unions are recovered in research/peer-*.json. A paired
roster initiates the retail peer flow; CONNECTED is accepted only from each
participant's mesh report. This module never grants coins or writes clubs.
"""
from copy import deepcopy
import secrets
import time
import uuid

import tdf
from fut_inventory import integer

SETUP, STATE, ATTRIBUTES, PLAYER_ATTRIBUTES = 20, 100, 80, 90
PLAYER_STATE, JOINED, REMOVED, GAME_REMOVED, SESSION_UPDATED = 116, 30, 40, 16, 115
PLATFORM_HOST = 71
INITIALIZING, PRE_GAME, IN_GAME, POST_GAME = 1, 130, 131, 4
CONNECTING, CONNECTED = 2, 4
COMMANDS = {2,3,4,7,8,11,15,22,26,29,171}


def fields(values):
    return b''.join(values[k] for k in sorted(values))


def union(tag, index, name, content):
    return tdf.header(tag,6)+bytes([index])+tdf.group(name,content)


def network(tag, value):
    if not isinstance(value,dict) or value.get('union')!=2:
        raise ValueError('The captured PS3 peer flow requires an IP pair')
    member,pair=value.get('field',(None,None))
    if member!='VALU' or not isinstance(pair,dict):raise ValueError('Invalid IP pair')
    encoded={}
    for name in ('EXIP','INIP'):
        ip=pair.get(name)
        if (not isinstance(ip,dict) or not integer(ip.get('IP'),1,2**32-1)
                or not integer(ip.get('PORT'),1,65535) or not integer(ip.get('MACI',0),0,2**64-1)):
            raise ValueError('Invalid peer IP/port')
        encoded[name]=tdf.group(name,fields({k:tdf.int_field(k,ip.get(k,0)) for k in ('IP','MACI','PORT')}))
    if not integer(pair.get('MACI',0),0,2**64-1):raise ValueError('Invalid peer machine ID')
    encoded['MACI']=tdf.int_field('MACI',pair.get('MACI',0))
    return union(tag,2,'VALU',fields(encoded))


def attributes(value):
    if (not isinstance(value,dict) or len(value)>128 or any(
        not isinstance(k,str) or not 1<=len(k.encode())<=128 or not isinstance(v,str)
        or len(v.encode())>1024 for k,v in value.items())):raise ValueError('Invalid game/player attributes')
    return dict(value)


def compatible(a,b):
    if a['session'].external==b['session'].external:return False
    ra,rb=a['request'],b['request']
    if any(ra['CMGD'].get(k)!=rb['CMGD'].get(k) for k in ('GVER','GGTY')):return False
    if ra['GMCD'].get('NTOP')!=130 or rb['GMCD'].get('NTOP')!=130:return False
    if any(a['session'].online_fut_match['request'][k]!=b['session'].online_fut_match['request'][k]
           for k in ('seasonId','divisionId')):return False
    for source,other in ((ra,rb),(rb,ra)):
        if other['PLJD']['PLDL'][0]['USID']['ID'] in source['CRD'].get('APLR',{}).get('ALST',[]):return False
        if source['CRD'].get('PPLR',{}).get('REQP') and other['PLJD']['PLDL'][0]['USID']['ID'] not in source['CRD']['PPLR'].get('PLST',[]):return False
        for key,rule in source['CRD'].get('GARC',{}).items():
            if rule.get('THLD')=='OSDK_matchExact':
                theirs=other['CRD'].get('GARC',{}).get(key,{})
                if not set(rule.get('VALU',[]))&set(theirs.get('VALU',[])):return False
    return True


def eligible(job):
    request=job['request']
    user=request['PLJD']['PLDL'][0]['USID']
    # EXBB is the real PS3 identifier blob. A length-only diagnostic decode
    # must never substitute zero bytes when setting up another live player.
    if not isinstance(user.get('EXBB'),bytes) or len(user['EXBB'])!=36:return False
    try:
        network('PNET',request['CMGD'].get('PNET'))
        attributes(request['GMCD'].get('ATTR',{}))
        if not isinstance(request['CRD'].get('GARC',{}),dict):return False
        for rule in request['CRD'].get('GARC',{}).values():
            if (not isinstance(rule,dict) or not isinstance(rule.get('THLD'),str)
                or not isinstance(rule.get('VALU'),list) or any(not isinstance(v,str) for v in rule['VALU'])):return False
    except (ValueError,TypeError):return False
    return True


class PeerGames:
    """Called under MatchmakingQueue.lock; notifications use the owning socket."""
    def __init__(self,queue):
        self.queue=queue
        self.games={}
        self.next_id=secrets.randbelow(2**30)+1

    def deadline(self):
        return self.queue.clock()+self.queue.settings.section('matchmaking')['peer_timeout_seconds']

    def get(self,session):
        game=self.games.get(getattr(session,'peer_game_id',None))
        if not game or not any(s is session for s in game['sessions']):raise ValueError('No owned peer game')
        return game

    def create(self,a,b):
        now=time.time();host=a['session'];game_id=self.next_id;self.next_id+=1
        attrs=attributes(a['request']['GMCD'].get('ATTR',{}))
        for key,rule in a['request']['CRD'].get('GARC',{}).items():
            if rule.get('VALU'):attrs.setdefault(key,rule['VALU'][0])
        attributes(attrs)
        game={'id':game_id,'host':host,'sessions':(host,b['session']),'jobs':(a,b),
              'created':int(now*1000000),'uuid':str(uuid.uuid4()),'state':INITIALIZING,
              'settings':a['request']['GMCD']['GSET'],'attrs':attrs,'np_session':'',
              'nonce':b'','xses':b'','links':set(),'completed':set(),
              'player_attrs':[attributes(j['request']['PLJD']['PLDL'][0].get('PLYA',{})) for j in (a,b)],
              'player_states':[CONNECTING,CONNECTING],'deadline':self.deadline()}
        # Build both notifications before assigning the game, so malformed
        # input cannot leave a half-created roster or consume either search.
        notices=[(4,SETUP,self.setup(game,i),'GameSetup') for i in (0,1)]
        self.games[game_id]=game
        for i,job in enumerate((a,b)):
            s=job['session'];s.peer_game_id=game_id
            s.online_fut_match.update(game_id=game_id,id=game['uuid'].replace('-', ''),
                opponent=game['sessions'][1-i].persona,
                participants=tuple(player.persona for player in game['sessions']))
            self.queue.jobs.pop(s.key,None)
            self.queue.push(s,notices[i])
        if host.owner:
            from server_plugins import player
            host.owner.events.emit('match.paired', game_id=game_id, match_id=game['uuid'].replace('-', ''),
                                   players=[player(s.owner) for s in game['sessions']])
        return game

    def host_info(self,game):
        s=game['host'];group=game['jobs'][0]['request']['PLJD']['BTPL'][2]
        return fields({k:tdf.int_field(k,v) for k,v in {'CONG':group,'CSID':0,'HPID':s.persona,'HSES':s.persona,'HSLT':0}.items()})

    def data(self,game):
        request=game['jobs'][0]['request'];creation=request['GMCD'];host=self.host_info(game)
        vals={k:tdf.int_field(k,v) for k,v in {'APRS':1,'CTIM':game['created'],'GGTY':request['CMGD']['GGTY'],
            'GID':game['id'],'GMRG':creation.get('GMRG',0),'GPVH':0,'GSET':game['settings'],'GSID':game['id'],
            'GSTA':game['state'],'MCAP':2,'MNCP':2,'NRES':0,'NTOP':creation['NTOP'],
            'PRES':creation.get('PRES',1),'QCAP':0,'SEED':game['id'],'VOIP':creation.get('VOIP',2)}.items()}
        vals.update({k:tdf.str_field(k,v) for k,v in {'COID':'','ESNM':'','GNAM':creation.get('GNAM') or 'Local FUT',
            'GTYP':'gameType86','GURL':'','NPSI':game['np_session'],'PGID':'','PSAS':'local','SCID':'',
            'STMN':creation.get('STMN',''),'UUID':game['uuid'],'VSTR':request['CMGD']['GVER']}.items()})
        vals.update(ADMN=tdf.int_list('ADMN',[game['host'].persona]),ATTR=tdf.string_map('ATTR',game['attrs']),
            CAP=tdf.int_list('CAP',[2,0,0,0]),CRIT=tdf.string_map('CRIT',{}),
            DHST=tdf.group('DHST',fields({k:tdf.int_field(k,0) for k in ('CONG','CSID','HPID','HSES','HSLT')})),
            DNET=tdf.header('DNET',4)+b'\6\0',HNET=tdf.header('HNET',4)+b'\6\1'+network('VALU',request['CMGD']['PNET'])[4:],
            MATR=tdf.string_map('MATR',{}),NQOS=tdf.group('NQOS',fields({k:tdf.int_field(k,v) for k,v in {'BWHR':0,'DBPS':0,'NAHR':0,'NATT':2,'UBPS':0}.items()})),
            PGSR=tdf.blob_field('PGSR',b''),PHST=tdf.group('PHST',host),THST=tdf.group('THST',host),
            RNFO=tdf.group('RNFO',tdf.group_map('CRIT',{})+tdf.string_map('RCRT',{})),
            TIDS=tdf.int_list('TIDS',[0,1]),XNNC=tdf.blob_field('XNNC',game['nonce']),XSES=tdf.blob_field('XSES',game['xses']))
        return fields(vals)

    def player(self,game,index):
        s=game['sessions'][index];request=game['jobs'][index]['request'];join=request['PLJD']['PLDL'][0];user=join['USID']
        vals={k:tdf.int_field(k,v) for k,v in {'CONG':request['PLJD']['BTPL'][2],'CSID':index,'DSUI':0,
            'EXID':s.external,'GID':game['id'],'JFPS':1,'JVMM':1,'LOC':user.get('ALOC',1701726018),
            'PID':s.persona,'PSET':0,'RCRE':0,'SID':index,'SLOT':0,'STAT':game['player_states'][index],
            'TIDX':index,'TIME':game['created'],'UID':s.persona}.items()}
        vals.update({k:tdf.str_field(k,v) for k,v in {'DNAM':user.get('DNAM',s.name),'NAME':user.get('NAME',s.name),
            'NASP':'ps3','ROLE':join.get('RLNM',''),'UUID':str(uuid.uuid5(uuid.UUID(game['uuid']),str(index)))}.items()})
        vals.update(BLOB=tdf.blob_field('BLOB',b''),EXBL=tdf.blob_field('EXBL',user['EXBB']),
            PATT=tdf.string_map('PATT',game['player_attrs'][index]),PNET=network('PNET',request['CMGD']['PNET']),
            UGID=tdf.object_id('UGID',*request['PLJD']['BTPL']))
        return fields(vals)

    def setup(self,game,index):
        job=game['jobs'][index];s=job['session']
        reason=fields({k:tdf.int_field(k,v) for k,v in {'FIT':100,'MAXF':100,'MSCD':0,'MSID':job['id'],
            'RSLT':index,'TTM':max(0,int((self.queue.clock()-job['started'])*1000000)),'USID':s.persona}.items()})
        return fields({'GAME':tdf.group('GAME',self.data(game)),'LFPJ':tdf.int_field('LFPJ',0),
            'MNAM':tdf.str_field('MNAM','OSDK_gameMode'),
            'PROS':tdf.group_list('PROS',[self.player(game,i) for i in (0,1)]),
            'QOSS':tdf.group('QOSS',fields({k:tdf.int_field(k,0) for k in ('DURA','INTV','SIZE')})),
            'QOSV':tdf.int_field('QOSV',0),'QUEU':tdf.group_list('QUEU',[]),
            'REAS':union('REAS',3,'MMSC',reason),'TELM':tdf.int_field('TELM',0)})

    def broadcast(self,game,command,payload,name):
        for s in game['sessions']:self.queue.push(s,(4,command,payload,name))

    def end(self,game,reason=4):
        if self.games.pop(game['id'],None) is None:return
        for s in game['sessions']:
            self.queue.notices.pop(s.key,None)  # No stale setup/state after teardown.
            index=next(i for i,p in enumerate(game['sessions']) if p is s)
            s.ended_peer_game={'id':game['id'],
                'own_group':deepcopy(game['jobs'][index]['request']['PLJD']['BTPL']),
                'peer_group':deepcopy(game['jobs'][1-index]['request']['PLJD']['BTPL']),
                'players':tuple(player.persona for player in game['sessions']),
                'host':game['host'].persona,
                'topology':130}
            s.peer_game_id=None
            if getattr(s,'online_fut_match',None):s.online_fut_match['cancelled']=True
        self.broadcast(game,GAME_REMOVED,tdf.int_field('GID',game['id'])+tdf.int_field('REAS',reason),'GameRemoved')

    def remove(self,session):
        game=self.games.get(getattr(session,'peer_game_id',None))
        if game and any(s is session for s in game['sessions']):self.end(game)
        self.queue.notices.pop(session.key,None)

    def expire(self):
        for game in list(self.games.values()):
            if self.queue.clock()>=game['deadline']:self.end(game)

    def response(self,session,command,request):
        if command not in COMMANDS:return None
        if not isinstance(request,dict) or not integer(request.get('GID'),1):raise ValueError('Invalid peer game ID')
        ended=getattr(session,'ended_peer_game',None)
        # The retail client can send queued callbacks after GameRemoved.
        # Acknowledge only the caller's last owned game, without touching a
        # new game, recreating a roster or dropping the FUT login.
        if ended and request['GID']==ended['id'] and command in (3,11,22,29,171):
            if command==3:
                if (set(request)!={'GID','GSTA'} or type(request['GSTA']) is not int
                        or request['GSTA']!=POST_GAME or ended.get('host')!=session.persona):
                    raise ValueError('Only an ended game host may acknowledge post-game state')
            elif command in (11,22):
                self.own_removal(request,session,ended['own_group'],require_group=command==22)
            elif command==29:
                self.mesh_groups(request,ended['own_group'],ended['peer_group'])
            else:
                self.telemetry(request,ended['own_group'],ended['peer_group'],ended['topology'])
            return b'','GameManager.'+{3:'AdvanceGameState',11:'RemovePlayer',22:'LeaveGameByGroup',29:'UpdateMeshConnection',171:'ReportTelemetry'}[command],[]
        game=self.get(session)
        if not isinstance(request,dict) or not integer(request.get('GID'),game['id'],game['id']):raise ValueError('Game must belong to this session')
        index=next(i for i,s in enumerate(game['sessions']) if s is session)
        host=game['host'] is session
        names={2:'DestroyGame',3:'AdvanceGameState',4:'SetGameSettings',7:'SetGameAttributes',8:'SetPlayerAttributes',
               11:'RemovePlayer',15:'FinalizeGameCreation',22:'LeaveGameByGroup',26:'UpdateGameSession',29:'UpdateMeshConnection',171:'ReportTelemetry'}
        if command in (2,3,4,7,15,26) and not host:raise ValueError('Only this game host can change its state')
        if command==3:
            if set(request)!={'GID','GSTA'} or type(request['GSTA']) is not int:raise ValueError('Invalid game state')
            wanted=request['GSTA']
            allowed={INITIALIZING:{PRE_GAME},PRE_GAME:{IN_GAME},IN_GAME:{POST_GAME},POST_GAME:set()}
            if wanted!=game['state'] and wanted not in allowed[game['state']]:raise ValueError('Invalid game state transition')
            if wanted==IN_GAME and len(game['links'])!=2:raise ValueError('Both peers must report connected before kickoff')
            if wanted!=game['state']:
                game['state']=wanted
                self.broadcast(game,STATE,tdf.int_field('GID',game['id'])+tdf.int_field('GSTA',wanted),'GameStateChange')
        elif command==15:
            # PS3 finalizeGameCreation uses UpdateGameSessionRequest, with
            # NPSI/XNNC/XSES even when they are empty (captured 23:20:57 UTC).
            self.external_session(game,request,apply=game['state']==INITIALIZING)
            if game['state']==INITIALIZING:
                game['state']=PRE_GAME
                self.broadcast(game,PLATFORM_HOST,tdf.int_field('GID',game['id'])+tdf.int_field('PHID',session.persona)+tdf.int_field('PHST',0),'PlatformHostInitialized')
                self.broadcast(game,STATE,tdf.int_field('GID',game['id'])+tdf.int_field('GSTA',PRE_GAME),'GameStateChange')
        elif command in (7,8):
            expected={'GID','ATTR'}|({'PID'} if command==8 else set())
            if set(request)!=expected:raise ValueError('Invalid attributes request')
            value=attributes(request['ATTR'])
            if command==8:
                if request['PID']!=session.persona:raise ValueError('Only own player attributes may change')
                game['player_attrs'][index].update(value)
                payload=tdf.string_map('ATTR',value)+tdf.int_field('GID',game['id'])+tdf.int_field('PID',session.persona)
                self.broadcast(game,PLAYER_ATTRIBUTES,payload,'PlayerAttribChange')
            else:
                game['attrs'].update(value)
                self.broadcast(game,ATTRIBUTES,tdf.string_map('ATTR',value)+tdf.int_field('GID',game['id']),'GameAttribChange')
        elif command==4:
            if set(request)!={'GID','GSET'} or not integer(request['GSET'],0,2**32-1):raise ValueError('Invalid game settings')
            game['settings']=request['GSET']
            self.broadcast(game,110,tdf.int_field('GID',game['id'])+tdf.int_field('GSET',game['settings']),'GameSettingsChange')
        elif command==26:
            self.external_session(game,request)
        elif command==29:
            own=game['jobs'][index]['request']['PLJD']['BTPL'];other=game['jobs'][1-index]['request']['PLJD']['BTPL']
            self.mesh_groups(request,own,other)
            if request['TCG']==other:
                if request['STAT']==0:
                    self.end(game)
                elif request['STAT']==2:
                    game['links'].add(index)
                    if len(game['links'])==2:
                        pending=[i for i in (0,1) if i not in game['completed']]
                        for i in pending:
                            s=game['sessions'][i]
                            game['completed'].add(i);game['player_states'][i]=CONNECTED
                            self.broadcast(game,PLAYER_STATE,tdf.int_field('GID',game['id'])+tdf.int_field('PID',s.persona)+tdf.int_field('STAT',CONNECTED),'GamePlayerStateChange')
                        # Publish the full connected roster before invoking
                        # frontend join callbacks. Each client handles its
                        # own player first, then the remote participant.
                        joined_time=int(time.time()*1000000)
                        for own,recipient in enumerate(game['sessions']):
                            for i in (own,1-own):
                                if i not in pending:continue
                                s=game['sessions'][i]
                                self.queue.push(recipient,(4,JOINED,
                                    tdf.int_field('GID',game['id'])+tdf.int_field('PID',s.persona)+tdf.int_field('TIME',joined_time),
                                    'PlayerJoinCompleted'))
        elif command==171:
            self.telemetry(request,game['jobs'][index]['request']['PLJD']['BTPL'],
                           game['jobs'][1-index]['request']['PLJD']['BTPL'],130)
            # Retail keeps sending peer telemetry throughout the match;
            # its first HTTP keepalive can arrive nine minutes after kickoff.
            # Only a connected, playing pair may refresh the game this way.
            if len(game['links'])==2 and all(
                    getattr(player,'online_fut_match',{}).get('playing') for player in game['sessions']):
                game['deadline']=self.deadline()
        elif command in (11,22):
            self.own_removal(request,session,game['jobs'][index]['request']['PLJD']['BTPL'],require_group=command==22)
            self.end(game)
        elif command==2:
            if set(request)-{'GID','REAS'}:raise ValueError('Invalid destroy request')
            self.end(game)
        if command!=171:
            game['deadline']=self.deadline()
        return b'','GameManager.'+names[command],[]

    def external_session(self,game,request,apply=True):
        if set(request)-{'GID','NPSI','XNNC','XSES'}:raise ValueError('Invalid external game session')
        npsi=request.get('NPSI',game['np_session'])
        nonce=request.get('XNNC',game['nonce']);xses=request.get('XSES',game['xses'])
        if not isinstance(npsi,str) or len(npsi.encode())>64 or any(not isinstance(v,bytes) or len(v)>4096 for v in (nonce,xses)):
            raise ValueError('Invalid external session fields')
        if not apply:
            return  # A finalize retry must not erase a later PS3 session update.
        changed=(npsi,nonce,xses)!=(game['np_session'],game['nonce'],game['xses'])
        game.update(np_session=npsi,nonce=nonce,xses=xses)
        if changed:
            self.broadcast(game,SESSION_UPDATED,fields({'GID':tdf.int_field('GID',game['id']),'HPRS':tdf.int_field('HPRS',1),
                'NPSI':tdf.str_field('NPSI',npsi),'XNNC':tdf.blob_field('XNNC',nonce),'XSES':tdf.blob_field('XSES',xses)}),'GameSessionUpdated')

    @staticmethod
    def own_removal(request,session,own,require_group=False):
        if (set(request)-{'BTPL','CNTX','GID','PID','REAS','SCTX'}
                or not integer(request.get('PID'),session.persona,session.persona)
                # Captured delayed RemovePlayer uses the default zero object
                # ID. Its explicit PID still identifies the authenticated
                # owner. LeaveGameByGroup continues to require the real group.
                or 'BTPL' in request and request['BTPL']!=own
                   and (require_group or request['BTPL'] != [0, 0, 0])
                or require_group and request.get('BTPL')!=own
                or 'CNTX' in request and not integer(request['CNTX'],0,2**32-1)
                or 'REAS' in request and not integer(request['REAS'],0,65535)
                or 'SCTX' in request and (not isinstance(request['SCTX'],str) or len(request['SCTX'].encode())>128)):
            raise ValueError('Only owned player/group removal supported')

    @staticmethod
    def mesh_groups(request,own,other):
        if set(request)-{'GID','SCG','TCG','STAT','FLGS','QOSI'} or not {'SCG','TCG','STAT'}<=request.keys():raise ValueError('Invalid mesh report')
        if request['SCG']!=own or request['TCG'] not in (own,other) or not integer(request['STAT'],0,2):raise ValueError('Mesh groups must belong to these peers')

    @staticmethod
    def telemetry(request,own,other,topology):
        if set(request)!={'GID','LCID','NTOP','RPTS'} or request['LCID']!=own[2] or request['NTOP']!=topology:
            raise ValueError('Telemetry must belong to the reporting peer')
        if not isinstance(request['RPTS'],list) or len(request['RPTS'])>2:raise ValueError('Invalid telemetry report list')
        for report in request['RPTS']:
            if (not isinstance(report,dict) or set(report)!={'LATC','PKTL','RCID','RCVD','SENT'}
                    or report['RCID'] not in (own[2],other[2])
                    or any(not integer(report[k],0,2**64-1) for k in ('LATC','PKTL','RCID','RCVD','SENT'))):
                raise ValueError('Invalid participant telemetry')
