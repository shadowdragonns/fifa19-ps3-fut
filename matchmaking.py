"""Retail search queue, compatible two-player pairing and cleanup.

Wire constants come from research/matchmaking-contracts.json. Search jobs are
owned by Blaze login sessions, never persisted and never turned into CPU games.
Peer setup is queued on each participant's socket after its search response.
"""
from copy import deepcopy
import threading
import time

import tdf
from fut_inventory import integer

COMPONENT, START, CANCEL, FAILED = 4, 13, 14, 10
TIMED_OUT, CANCELED = 3, 4


class MatchmakingQueue:
    def __init__(self, clock=None, settings=None):
        from server_settings import DEFAULT
        self.settings = settings or DEFAULT
        self.clock = clock or time.monotonic
        self.lock = threading.RLock()
        self.jobs = {}
        self.notices = {}
        self.next_id = 1
        from peer_games import PeerGames
        self.peers = PeerGames(self)

    def push(self, session, notice):
        self.notices.setdefault(session.key, []).append(notice)

    def acknowledged(self, session):
        with self.lock:
            job = self.jobs.get(session.key)
            if job and job['session'] is session:
                job['acknowledged'] = True

    def remove(self, session):
        with self.lock:
            job = self.jobs.get(session.key)
            if job and job['session'] is session:
                self.jobs.pop(session.key)
            self.peers.remove(session)

    def start(self, session, request):
        pending = getattr(session, 'online_fut_match', None)
        if not session.postauth or not session.fut_key or not pending or pending.get('cancelled'):
            raise ValueError('An authenticated FUT online reservation is required')
        if (not isinstance(request, dict) or set(request) != {'CMGD','CRD','GMCD','PLJD','SSDT'}
                or any(not isinstance(v, dict) for v in request.values())):
            raise ValueError('Invalid matchmaking structures')
        data, players = request['SSDT'], request['PLJD'].get('PLDL')
        if (not integer(data.get('DUR'), 1, 60000000) or not integer(data.get('MODE'), 3, 3)
                or not isinstance(players, list) or len(players) != 1
                or not isinstance(players[0], dict) or not isinstance(players[0].get('USID'), dict)):
            raise ValueError('Invalid local search duration/mode/player')
        user = players[0]['USID']
        if (not integer(user.get('ID'), session.persona, session.persona)
                or not integer(user.get('AID'), session.account, session.account)
                or not integer(user.get('EXID'), session.external, session.external)
                or request['PLJD'].get('BTPL') != [30722, 2, session.persona]):
            raise ValueError('Search player must own the login and reservation')
        if session.np_blob and (user.get('EXBB') != session.np_blob or user.get('NAME') != session.name):
            raise ValueError('Search platform identity must match the login')
        version = request['CMGD'].get('GVER')
        if not isinstance(version, str) or not 1 <= len(version) <= 64:
            raise ValueError('Invalid game protocol version')
        with self.lock:
            if getattr(session, 'peer_game_id', None):
                game = self.peers.get(session)
                job = next(j for j in game['jobs'] if j['session'] is session)
                if job['request'] != request:
                    raise ValueError('Leave the current peer game first')
                return self.start_reply(job)
            job = self.jobs.get(session.key)
            if job and (job['session'] is not session or job['request'] != request):
                raise ValueError('Cancel the previous search first')
            if not job:
                job = {'id':self.next_id, 'session':session, 'request':deepcopy(request),
                       'started':self.clock(), 'acknowledged':False,
                       'deadline':self.clock()+min(data['DUR']/1000000,
                                                 self.settings.section('matchmaking')['search_seconds'])}
                self.next_id += 1
                self.jobs[session.key] = job
                session.matchmaking_queue = self
                session.last_matchmaking_id = job['id']
            return self.start_reply(job)

    @staticmethod
    def start_reply(job):
        return (tdf.str_field('COID','')+tdf.str_field('ESNM','')
                +tdf.int_field('MSID',job['id'])+tdf.str_field('SCID','')+tdf.str_field('STMN',''))

    def finished(self, session, job, result):
        payload = (tdf.int_field('MAXF',0)+tdf.int_field('MSCD',0)
                   +tdf.int_field('MSID',job['id'])+tdf.int_field('RSLT',result)
                   +tdf.int_field('USID',session.persona))
        return COMPONENT, FAILED, payload, 'MatchmakingFailed'

    def cancel(self, session, request):
        if (not isinstance(request,dict) or set(request) != {'MSID'}
                or not integer(request['MSID'],1)
                or request['MSID'] != getattr(session,'last_matchmaking_id',None)):
            raise ValueError('Search must belong to this login session')
        with self.lock:
            job = self.jobs.pop(session.key,None)
            return [self.finished(session,job,CANCELED)] if job else []

    def poll(self, session):
        with self.lock:
            from peer_games import compatible, eligible
            self.peers.expire()
            waiting = [j for j in self.jobs.values() if j['acknowledged']
                       and self.clock() < j['deadline'] and eligible(j)]
            used = set()
            for index, first in enumerate(waiting):
                if first['id'] in used:continue
                for second in waiting[index+1:]:
                    if second['id'] not in used and compatible(first, second):
                        self.peers.create(first, second)
                        used.update((first['id'], second['id']))
                        break
            notices = self.notices.pop(session.key, [])
            job = self.jobs.get(session.key)
            if not job or job['session'] is not session or self.clock() < job['deadline']:
                return notices
            self.jobs.pop(session.key)
            return notices+[self.finished(session,job,TIMED_OUT)]

    def response(self, session, command, request):
        if command == START:
            return self.start(session,request), 'GameManager.StartMatchmaking', []
        if command == CANCEL:
            return b'', 'GameManager.CancelMatchmaking', self.cancel(session,request)
        with self.lock:
            return self.peers.response(session, command, request)
