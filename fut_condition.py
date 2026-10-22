"""Wall-clock fitness recovery, saved injuries and match-based red-card bans."""
import time
from copy import deepcopy
from fut_inventory import integer


def validate(record):
    games, kind, ban = record.get('injuryGames', 0), record.get('injuryType', 'none'), record.get('suspension', 0)
    if (not integer(games, 0, 255) or not integer(ban, 0, 255)
            or not isinstance(kind, str) or not 1 <= len(kind) <= 32 or not kind.isalpha()
            or (games == 0) != (kind == 'none')):
        raise ValueError('Invalid saved player condition')
    if (not integer(record.get('contract',7),0,99) or not integer(record.get('fitness',99),0,99)):
        raise ValueError('Invalid player contracts/fitness')
    if 'fitnessUpdatedAt' in record and not integer(record['fitnessUpdatedAt']):
        raise ValueError('Invalid fitness recovery timestamp')
    training = {'trainingBoost','trainingDefinition','trainingItemId'} & set(record)
    if training:
        from fut_items import CATALOGUE
        spec = CATALOGUE.get(record.get('trainingDefinition'))
        if (training != {'trainingBoost','trainingDefinition','trainingItemId'}
                or spec is None or spec['itemType'] not in ('playerTraining','GKTraining')
                or record['trainingBoost'] != [spec['amount']]*6
                or any(type(v) is not int for v in record['trainingBoost'])
                or not integer(record['trainingItemId'],1)):
            raise ValueError('Invalid temporary player training')


def fitness(record, settings=None, now=None):
    from server_settings import DEFAULT
    rules=(settings or DEFAULT).section('fitness')
    value=record.get('fitness',99)
    anchor=record.get('fitnessUpdatedAt')
    if value>=99 or anchor is None or not rules['enabled']:return value
    now=int(time.time()) if now is None else now
    return min(99,value+max(0,int(now)-anchor)//rules['seconds_per_point'])


def initialize_recovery(store):
    """Seed old tired cards from their last owned appearance, once per card.

    Reads compute elapsed recovery without ticking or rewriting every five
    minutes. Match reports, supplies and admin condition edits reset the anchor.
    """
    with store.lock:
        missing=[r for r in store.state['items'] if r.get('fitness',99)<99 and 'fitnessUpdatedAt' not in r]
        if not missing:return
        now=int(time.time());last={}
        for receipt in store.state.get('matchHistory',[]):
            if receipt['summary']['endReason'] not in ('WIN','DRAW','LOSS'):continue
            for item_id in receipt.get('playedItems',[]):last[item_id]=max(last.get(item_id,0),receipt['finished'])
        state=deepcopy(store.state)
        for record in state['items']:
            if record.get('fitness',99)<99 and 'fitnessUpdatedAt' not in record:
                record['fitnessUpdatedAt']=min(now,last.get(record['id'],now))
        store.commit(state)


def available(card):
    return not card.get('injuryGames', 0) and not card.get('suspension', 0)


def apply(state, current, payload, settings=None):
    played = set(current['played_items'])
    complete = payload['endReason'] in {'WIN', 'DRAW', 'LOSS'}
    reported = {row['id']: row for row in payload['items']}
    for record in state['items']:
        if record['pile'] != 'club':
            continue
        if complete and record['id'] not in played:
            if record.get('injuryGames', 0):
                record['injuryGames'] -= 1
                if not record['injuryGames']:
                    record['injuryType'] = 'none'
            if record.get('suspension', 0):
                record['suspension'] -= 1
        if record['id'] not in played or payload['endReason'] == 'NO_CONTEST':
            continue
        report = reported[record['id']]
        if complete:
            record['contract'] = max(0,record.get('contract',7)-1)
            record['fitness'] = report.get('fitness',fitness(record,settings))
            record['fitnessUpdatedAt'] = int(time.time())
            for field in ('trainingBoost','trainingDefinition','trainingItemId'): record.pop(field,None)
        kind = report.get('injuryType', 'none').lower()
        games = report.get('injuryGames', 1 if kind != 'none' else 0)
        if games:
            record['injuryGames'] = max(record.get('injuryGames', 0), games)
            record['injuryType'] = kind if kind != 'none' else record.get('injuryType', 'leg')
            if record['injuryType'] == 'none':
                record['injuryType'] = 'leg'
        ban = max(report.get('suspension', 0), int(report.get('redCards', 0) > 0))
        if ban:
            record['suspension'] = max(record.get('suspension', 0), ban)
    if complete:
        selected = {row['id'] for row in current['squad'].get('manager',[])}
        for manager in state.get('supportItems',[]):
            if manager['id'] in selected: manager['contract'] = max(0,manager.get('contract',7)-1)
