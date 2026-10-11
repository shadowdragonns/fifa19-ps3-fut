"""Acknowledge captured game reports; FUT match/end alone writes rewards.

Retail command-name mapping 0xc54c04..0xc54cdc identifies component 28,
command 2 as submitOfflineGameReport. This acknowledgement keeps the local
Blaze/FUT session alive; match/end alone saves FUT results and pays coins.
"""
from copy import deepcopy


def peer_completed_report(backend, session, request):
    """Record only the authenticated match's score/time in its live sessions."""
    from fut_inventory import integer
    if (not session.postauth or set(request) != {'FNSH','PRVT','RPRT'}
            or not integer(request['FNSH'],1,1) or request['PRVT'] != {'variable': None}):
        return False
    report = request['RPRT']
    if (not isinstance(report,dict) or set(report) != {'GAME','GRID','GTYP'}
            or report['GTYP']!='gameType86' or not integer(report['GRID'],1)):
        return False
    wrapper = report['GAME']
    if (not isinstance(wrapper,dict) or set(wrapper) != {'variable','field'}
            or wrapper['variable']!=2552107044 or not isinstance(wrapper['field'],tuple)
            or len(wrapper['field'])!=2 or wrapper['field'][0]!='GAME'):
        return False
    value = wrapper['field'][1]
    if (not isinstance(value,dict) or set(value)!={'CGRT','GAMR','IFPR','PLYR','TAMR'}
            or not isinstance(value['GAMR'],dict) or not isinstance(value['PLYR'],dict)):
        return False
    game_report = value['GAMR']
    if (game_report.get('TYPE')!='gameType86' or not integer(game_report.get('GRID'),0,0)
            or not integer(game_report.get('GTIM'),1,86400) or not integer(game_report.get('STUS'),1,1)):
        return False
    queue = getattr(session,'matchmaking_queue',None)
    if queue is None:
        return False
    with backend.root.lock, queue.lock:
        current = getattr(session,'online_fut_match',None)
        if (backend.root.sessions.get(session.key) is not session or not current
                or not current.get('playing') or current.get('game_id')!=report['GRID']):
            return False
        participants = current.get('participants',())
        if (len(participants)!=2 or session.persona not in participants
                or set(value['PLYR'])!={str(persona) for persona in participants}):
            return False
        scores = {}
        for persona,player in value['PLYR'].items():
            if not isinstance(player,dict) or not integer(player.get('SCOR'),0,65535):
                return False
            scores[persona] = player['SCOR']
        summary = {'secondsPlayed':game_report['GTIM'],'scores':scores}
        recipients = [s for s in backend.root.sessions.values()
                      if s.persona in participants and getattr(s,'online_fut_match',None)
                      and s.online_fut_match.get('game_id')==report['GRID']]
        if any(s.online_fut_match.get('peer_report',summary)!=summary for s in recipients):
            return False
        for s in recipients:
            s.online_fut_match['peer_report']=deepcopy(summary)
        return True


def peer_loading_report(backend, session, request):
    """Ack the captured zero-time gameType86 DNF without match results.

    Retail SubmitGameReportRequest uses FNSH=2 (DNF, enum 0xc55664).
    Only the owning live/last-ended peer game and its exact participant IDs
    qualify. The payload remains in the private capture; no rewards, history,
    result notifications or persisted report state are fabricated.
    """
    from fut_inventory import integer
    if (not session.postauth or set(request) != {'FNSH', 'PRVT', 'RPRT'}
            or not integer(request['FNSH'],2,2) or request['PRVT'] != {'variable': None}):
        return False
    report = request['RPRT']
    if (not isinstance(report,dict) or set(report) != {'GAME','GRID','GTYP'}
            or report['GTYP'] != 'gameType86' or not integer(report['GRID'],1)):
        return False
    wrapper = report['GAME']
    if (not isinstance(wrapper,dict) or set(wrapper) != {'variable','field'}
            or wrapper['variable'] != 2552107044 or not isinstance(wrapper['field'],tuple)
            or len(wrapper['field']) != 2 or wrapper['field'][0] != 'GAME'):
        return False
    value = wrapper['field'][1]
    if (not isinstance(value,dict) or set(value) != {'CGRT','GAMR','IFPR','PLYR','TAMR'}
            or not isinstance(value['GAMR'],dict) or not isinstance(value['PLYR'],dict)):
        return False
    game_report = value['GAMR']
    if (game_report.get('TYPE') != 'gameType86'
            or not integer(game_report.get('GTIM'),0,0)
            or not integer(game_report.get('GRID'),0,0)
            or not integer(game_report.get('STUS'),2,2)):
        return False
    queue = getattr(session,'matchmaking_queue',None)
    if queue is None:
        return False
    with backend.root.lock, queue.lock:
        current = getattr(session,'online_fut_match',None)
        # An accepted PlayGame request does not prove the simulation started.
        # Keep this zero-time DNF acknowledgement available after that request;
        # finished reports, nonzero time and scores remain excluded above/below.
        if backend.root.sessions.get(session.key) is not session or not current:
            return False
        try:
            game = queue.peers.get(session)
        except ValueError:
            game = None
        if game and game['id'] == report['GRID']:
            participants = tuple(player.persona for player in game['sessions'])
        else:
            ended = getattr(session,'ended_peer_game',None)
            if not ended or ended['id'] != report['GRID']:
                return False
            participants = ended.get('players',())
        if set(value['PLYR']) != {str(persona) for persona in participants} or len(participants) != 2:
            return False
        for player in value['PLYR'].values():
            if (not isinstance(player,dict) or any(not integer(player.get(field),0,0)
                    for field in ('SCOR','WINS','TIES','LOSS'))):
                return False
        return True


def skill_report(request, session):
    """Recognize gameType21 with its concrete skill-report TDF, no save writes."""
    if not session.postauth or set(request) != {'FNSH', 'PRVT', 'RPRT'}:
        return False
    report = request['RPRT']
    if (request['FNSH'] != 0 or request['PRVT'] != {'variable': None}
            or not isinstance(report, dict) or set(report) != {'GAME', 'GRID', 'GTYP'}
            or report['GTYP'] != 'gameType21' or report['GRID'] != 0):
        return False
    game = report['GAME']
    if (not isinstance(game, dict) or set(game) != {'variable', 'field'}
            or game['variable'] != 499389828
            or not isinstance(game['field'], tuple) or len(game['field']) != 2
            or game['field'][0] != 'GAME'):
        return False
    fields = game['field'][1]
    return (isinstance(fields, dict) and set(fields) == {'CTRY', 'PLID', 'SCOR', 'SKLG'}
            and isinstance(fields['CTRY'], str) and len(fields['CTRY'].encode()) <= 16
            and type(fields['PLID']) is int and fields['PLID'] in (0, session.persona)
            and all(type(fields[k]) is int and 0 <= fields[k] < 2**31 for k in ('SCOR', 'SKLG')))
