"""Local CPU match reports, division rewards and persistent player conditions.

Retail DestroyMatch result: 0x16b62a4. allCoins is a delta: SDK setter
0x1689b3c adds it to the client wallet and clears the active match flag.
No EA outcome validation or historical reward formula is claimed here.
"""
from copy import deepcopy
import hashlib
import json
import re
import time

from fut_inventory import integer

COMPLETE = {'WIN', 'DRAW', 'LOSS', 'DNF_WIN', 'DNF_DRAW', 'DNF_LOSS'}
REASONS = COMPLETE | {'DNF', 'QUIT', 'NO_CONTEST'}
PRACTICE_COINS = 400  # Legacy receipts only; never recalculate their paid reward.
STATS = {'goals', 'ownGoals', 'penaltyGoals', 'shotsOnTarget', 'successfulTackles',
         'corners', 'cleansheets', 'passingPercentage', 'possessionPercentage',
         'manOfTheMatch', 'fouls', 'yellowCards', 'redCards', 'offsides'}
ITEM_FIELDS = {'id', 'fitness', 'goals', 'assists', 'injuryType', 'injuryGames',
               'suspension', 'cards', 'yellowCards', 'redCards'}



def match_reward(season_id, division_id):
    # Wire1 is displayed Division10. Only validated CPU-season requests
    # receive the division multiplier; other local modes use the base bonus.
    division = division_id if season_id == 1 and 1 <= division_id <= 10 else 1
    return 1000+500*(division-1)


def result_reward(season_id, division_id, reason, version=3):
    """Keep old receipts intact; new full-time rewards depend on the result."""
    if reason not in {'WIN', 'DRAW', 'LOSS'}:
        return 0
    base = match_reward(season_id, division_id) if version else PRACTICE_COINS
    if version == 3:
        return base * {'WIN': 4, 'DRAW': 3, 'LOSS': 2}[reason] // 4
    return base


def policy_reward(policy, reason):
    if (not isinstance(policy, dict) or set(policy) != {'baseWin', 'drawPercent', 'lossPercent', 'pluginAdjustment'}
            or not integer(policy['baseWin'], 0, 100000000)
            or not integer(policy['drawPercent'], 0, 100) or not integer(policy['lossPercent'], 0, policy['drawPercent'])
            or not integer(policy['pluginAdjustment'], -100000000, 100000000)):
        raise ValueError('Invalid saved reward policy')
    if reason not in {'WIN', 'DRAW', 'LOSS'}:
        if policy['pluginAdjustment']: raise ValueError('Unfinished matches cannot earn a plugin bonus')
        return 0
    percent = 100 if reason == 'WIN' else policy['drawPercent'] if reason == 'DRAW' else policy['lossPercent']
    coins = policy['baseWin']*percent//100+policy['pluginAdjustment']
    if not integer(coins, 0, 100000000): raise ValueError('Invalid saved configured reward')
    return coins

def validate_report(payload):
    required = {'matchReportId', 'endReason', 'secondsPlayed', 'items', 'matchData'}
    allowed = required | {'myRating', 'opponentRating', 'myMatchStats', 'opponentMatchStats'}
    if (not isinstance(payload, dict) or not required <= set(payload) <= allowed
            or not integer(payload['matchReportId']) or payload['endReason'] not in REASONS
            or not integer(payload['secondsPlayed'], 0, 86400)
            or not isinstance(payload['matchData'], str) or len(payload['matchData']) > 131072
            or any(not integer(payload[k], 0, 100) for k in ('myRating', 'opponentRating') if k in payload)
            or not isinstance(payload['items'], list) or not 11 <= len(payload['items']) <= 23):
        raise ValueError('Invalid offline match report')
    for key in ('myMatchStats', 'opponentMatchStats'):
        if key not in payload:
            if payload['endReason'] in COMPLETE:
                raise ValueError('Completed match requires both statistics')
            continue
        stats = payload[key]
        if (not isinstance(stats, dict) or set(stats) != STATS
                or any(not integer(v, 0, 65535) for v in stats.values())
                or any(stats[k] > 100 for k in ('passingPercentage', 'possessionPercentage'))):
            raise ValueError('Invalid match statistics')
    selected = set()
    for item in payload['items']:
        if (not isinstance(item, dict) or 'id' not in item or set(item)-ITEM_FIELDS
                or not integer(item['id'], 1) or item['id'] in selected
                or any(not integer(v, 0, 65535) for k, v in item.items() if k not in ('id', 'injuryType'))
                or item.get('fitness', 99) > 99
                or 'injuryType' in item and (not isinstance(item['injuryType'], str)
                    or len(item['injuryType']) > 32 or not item['injuryType'].isalpha())):
            raise ValueError('Invalid reported player')
        selected.add(item['id'])
    if payload['endReason'] in {'WIN', 'DRAW', 'LOSS'}:
        ours, theirs = payload['myMatchStats']['goals'], payload['opponentMatchStats']['goals']
        expected = 'WIN' if ours > theirs else 'LOSS' if ours < theirs else 'DRAW'
        if payload['endReason'] != expected:
            raise ValueError('Score does not match the result')
    return selected


def award(coins):
    # Empty breakdown collections are valid native vector paths. Local
    # rewards scale with CPU division. Owned conditions are persisted separately.
    return {'allCoins': coins, 'matchCoins': coins, 'seasonCoins': 0,
            'tournamentCoins': 0, 'participationAward': 0, 'boostConis': 0,
            'boostCountLeft': 0, 'gameModeAward': {'coins': 0},
            'teamOfTournamentWinner': False,
            'matchCoinPartials': [], 'matchCoinMultipliers': []}


def finish(backend, session, payload):
    backend = backend.for_session(session)
    with backend.lock:
        current = getattr(session, 'local_fut_match', None)
        if not current or not current.get('playing'):
            raise ValueError('No playing local offline match')
        return save_result(backend, current, payload)


def save_result(backend, current, payload):
    """Persist one owned match receipt and its coins/conditions atomically.

    CPU and online callers validate their own session/correlation first.
    """
    with backend.lock:
        reported = validate_report(payload)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        store = backend.fut_inventory
        previous = next((row for row in store.state.get('matchHistory', [])
                         if row['id'] == current['id']), None)
        if previous:
            if previous['requestHash'] != digest:
                raise ValueError('This match already has a different report')
            current['finished'] = True
            return deepcopy(previous['award'])
        selected = {slot['itemData']['id'] for slot in current['squad']['players']}
        owned = {row['id'] for row in store.state['items'] if row['pile'] == 'club'}
        if not reported <= selected & owned or not set(current['played_items']) <= reported:
            raise ValueError('Report must contain owned match starters and selected substitutes')
        # Only a completed full game earns the local practice bonus. Quit,
        # DNF and no-contest reports are acknowledged without a coin reward.
        from server_plugins import match_event
        policy = backend.settings.reward_policy(current['request'].get('seasonId', 0),
                                                current['request'].get('divisionId', 0))
        coins = policy_reward(policy, payload['endReason'])
        event = match_event(backend, current, result=payload['endReason'],
                            score={'for': payload.get('myMatchStats', {}).get('goals', 0),
                                   'against': payload.get('opponentMatchStats', {}).get('goals', 0)})
        if payload['endReason'] in {'WIN', 'DRAW', 'LOSS'}:
            filtered = backend.events.reward(coins, **event)
            policy['pluginAdjustment'] = filtered-coins
            coins = filtered
        result = award(coins)
        summary = {k: deepcopy(v) for k, v in payload.items() if k not in ('matchReportId', 'matchData')}
        record = {'id': current['id'], 'requestHash': digest, 'finished': int(time.time()),
                  'playedItems': sorted(current['played_items']),
                  'seasonId': current['request'].get('seasonId', 0),
                  'divisionId': current['request'].get('divisionId', 0),
                  'summary': summary, 'award': result, 'rewardVersion': 4, 'rewardPolicy': policy}
        state = deepcopy(store.state)
        from player_statistics import apply as apply_statistics
        apply_statistics(state,current,payload)
        from fut_condition import apply as apply_condition
        apply_condition(state, current, payload)
        state['coins'] += coins
        state.setdefault('matchHistory', []).append(record)
        store.commit(state)
        # Persistence precedes session completion, so a failed write or retry
        # cannot leave a paid reward without its saved result.
        current['finished'] = True
        backend.events.emit('match.completed', **event, coins=coins, balance=state['coins'])
        return deepcopy(result)


def validate_history(history):
    if not isinstance(history, list):
        raise ValueError('Invalid local match history')
    seen = set()
    for row in history:
        if (not isinstance(row, dict) or set(row)-{'playedItems'} not in ({'id', 'requestHash', 'finished', 'seasonId', 'divisionId', 'summary', 'award'},
                                     {'id', 'requestHash', 'finished', 'seasonId', 'divisionId', 'summary', 'award', 'rewardVersion'},
                                     {'id', 'requestHash', 'finished', 'seasonId', 'divisionId', 'summary', 'award', 'rewardVersion', 'rewardPolicy'})
                or not isinstance(row['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', row['id'])
                or row['id'] in seen or not isinstance(row['requestHash'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['requestHash'])
                or any(not integer(row[k]) for k in ('finished', 'seasonId', 'divisionId'))
                or not isinstance(row['summary'], dict)):
            raise ValueError('Invalid saved match record')
        validate_report({**row['summary'], 'matchReportId': 0, 'matchData': ''})
        if 'playedItems' in row:
            played = row['playedItems']
            if (not isinstance(played,list) or not 11 <= len(played) <= 23
                    or any(not integer(i,1) for i in played) or len(set(played))!=len(played)
                    or not set(played)<={r['id'] for r in row['summary']['items']}):
                raise ValueError('Invalid saved played-card IDs')
        if 'rewardVersion' in row and (type(row['rewardVersion']) is not int or row['rewardVersion'] not in (2, 3, 4)):
            raise ValueError('Invalid saved reward version')
        if ('rewardPolicy' in row) != (row.get('rewardVersion') == 4):
            raise ValueError('Configured reward requires a saved policy')
        expected = (policy_reward(row['rewardPolicy'], row['summary']['endReason']) if row.get('rewardVersion') == 4
                    else result_reward(row['seasonId'], row['divisionId'],
                                       row['summary']['endReason'], row.get('rewardVersion')))
        if row['award'] != award(expected):
            raise ValueError('Invalid saved match award')
        seen.add(row['id'])
