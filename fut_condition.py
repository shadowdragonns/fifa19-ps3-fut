"""Saved injuries and red-card bans; recovery requires a completed match resting."""
from fut_inventory import integer


def validate(record):
    games, kind, ban = record.get('injuryGames', 0), record.get('injuryType', 'none'), record.get('suspension', 0)
    if (not integer(games, 0, 255) or not integer(ban, 0, 255)
            or not isinstance(kind, str) or not 1 <= len(kind) <= 32 or not kind.isalpha()
            or (games == 0) != (kind == 'none')):
        raise ValueError('Invalid saved player condition')


def available(card):
    return not card.get('injuryGames', 0) and not card.get('suspension', 0)


def apply(state, current, payload):
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
