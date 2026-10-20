"""Per-owned-card match totals; report retries are covered by the match receipt."""
FIELDS = ('gamesPlayed','goals','assists','yellowCards','redCards')
MAX = (1 << 32)-1


def empty(): return dict.fromkeys(FIELDS,0)


def validate(record):
    if 'statistics' not in record: return
    value = record['statistics']
    if (not isinstance(value,dict) or set(value)!=set(FIELDS)
            or any(type(n) is not int or not 0 <= n <= MAX for n in value.values())):
        raise ValueError('Invalid saved player statistics')


def _add(stats, report, appeared):
    stats['gamesPlayed'] = min(MAX,stats['gamesPlayed']+int(appeared))
    for key in FIELDS[1:]: stats[key] = min(MAX,stats[key]+report.get(key,0))


def backfill(state):
    """Recover available historical totals without guessing unused substitutes."""
    missing = {row['id']:row for row in state['items'] if 'statistics' not in row}
    missing_fitness = {row['id']:row for row in state['items'] if 'fitness' not in row}
    totals,fitness = {},{}
    for match in state.get('matchHistory',[]):
        summary = match['summary']
        if summary['endReason']=='NO_CONTEST' or not summary['secondsPlayed']: continue
        reports = summary['items']
        played = match.get('playedItems')
        for report in reports:
            item_id = report['id']
            if item_id in missing_fitness and 'fitness' in report: fitness[item_id] = report['fitness']
            if item_id not in missing: continue
            if played is not None and item_id not in played: continue
            appeared = (item_id in played if played is not None else len(reports)==11
                        or report.get('fitness',99)<99
                        or any(report.get(key,0) for key in FIELDS[1:]))
            # Reports before playedItems tracked the entire squad. Goals,
            # assists and cards are recoverable, but bench appearances are not.
            if not appeared and not any(report.get(key,0) for key in FIELDS[1:]): continue
            _add(totals.setdefault(item_id,empty()),report,appeared)
    for item_id,stats in totals.items(): missing[item_id]['statistics'] = stats
    changed = set(totals)
    for item_id,value in fitness.items():
        if value != 99:  # Missing fitness already represents a fresh 99.
            missing_fitness[item_id]['fitness'] = value
            changed.add(item_id)
    return changed


def apply(state, current, payload):
    backfill(state)
    if payload['endReason']=='NO_CONTEST' or not payload['secondsPlayed']: return
    played = set(current['played_items'])
    reports = {row['id']:row for row in payload['items']}
    for row in state['items']:
        if row['pile']=='club' and row['id'] in played:
            _add(row.setdefault('statistics',empty()),reports[row['id']],True)


def lists(record):
    stats = record.get('statistics')
    current = [] if stats is None else [{'index':i,'value':stats[key]} for i,key in enumerate(FIELDS[:3])]
    if record.get('suspension'): current.append({'index':4,'value':record['suspension']})
    lifetime = [] if stats is None else [{'index':i,'value':stats[key]} for i,key in enumerate(FIELDS)]
    return current,lifetime
