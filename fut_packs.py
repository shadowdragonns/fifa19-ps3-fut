"""Local player-pack choices; stock ratings with locally defined rarity/odds."""
from collections import Counter
import json
from pathlib import Path
import random

STANDARD = json.loads((Path(__file__).parent/'data/standard-pack-players.json').read_text(encoding='utf-8'))
corrections = json.loads((Path(__file__).parent/'data/standard-england-players.json').read_text(encoding='utf-8'))['players']
STANDARD['players'] = list({p['assetId']: p for p in STANDARD['players']+corrections}.values())
STANDARD_PLAYERS = {p['assetId']: p for p in STANDARD['players']}
LEAGUE_COUNTRIES = {int(k): v for k, v in STANDARD['leagueCountries'].items()}
PACKS = {
    3: dict(name='England 110 Player Pack', edition='england110', level='gold',
            size=12, price=7500, rares=12, artwork=3, retired=True),
    4: dict(name='Bronze Player Pack', edition='standard', level='bronze',
            size=6, price=400, rares=1, artwork=1),
    5: dict(name='Silver Player Pack', edition='standard', level='silver',
            size=6, price=2500, rares=1, artwork=2),
    6: dict(name='Gold Player Pack', edition='standard', level='gold',
            size=12, price=7500, rares=1, artwork=3),
    7: dict(name='Premium Gold Player Pack', edition='standard', level='gold',
            size=12, price=15000, rares=3, artwork=3)}


# FUT-style local offers. Rarity, prices and probabilities are local rules.
for offset, (name, nation) in enumerate((('England', 14), ('France', 18),
        ('Germany', 21), ('Spain', 45), ('Brazil', 54), ('Argentina', 52),
        ('Portugal', 38), ('Netherlands', 34), ('Belgium', 7))):
    PACKS[100+offset] = dict(name=name+' Gold Players Pack', edition='standard',
        level='gold', size=6, price=7500, rares=1, artwork=3, nation=nation,
        theme=name+' national team')
for offset, (name, league) in enumerate((('Premier League', 13), ('Ligue 1', 16),
        ('Bundesliga', 19), ('Serie A', 31), ('La Liga', 53))):
    PACKS[120+offset] = dict(name=name+' Premium Players Pack', edition='standard',
        level='gold', size=12, price=15000, rares=3, artwork=3, leagueId=league,
        theme=name)
for offset, (name, positions) in enumerate((('Goalkeepers', ('GK',)),
        ('Defenders', ('CB', 'LB', 'RB', 'LWB', 'RWB')),
        ('Midfielders', ('CDM', 'CM', 'CAM', 'LM', 'RM')),
        ('Forwards', ('LW', 'RW', 'CF', 'ST')))):
    PACKS[140+offset] = dict(name=name+' Gold Players Pack', edition='standard',
        level='gold', size=6, price=7500, rares=1, artwork=3, positions=positions,
        theme=name.lower())
for pack_id, name, size, price, minimum in (
        (160, 'Rare Gold Players Pack', 12, 50000, 75),
        (161, 'Jumbo Rare Players Pack', 24, 100000, 75),
        (162, 'Ultimate Players Pack', 30, 125000, 75),
        (163, '83+ Gold Players Pack', 3, 20000, 83),
        (164, '85+ Gold Players Pack', 3, 50000, 85)):
    PACKS[pack_id] = dict(name=name, edition='standard', level='gold', size=size,
        price=price, rares=size, artwork=3, min_rating=minimum)

PACKS[180] = dict(name='95+ Prime Player Pack', edition='prime', level='gold', size=1,
                  price=50000, rares=1, artwork=3, min_rating=95, retired=True)
PACKS[181] = dict(name='95+ Prime Trio Pack', edition='prime', level='gold', size=3,
                  price=125000, rares=3, artwork=3, min_rating=95, retired=True)
PACKS[190] = dict(name='Manager Pack', edition='support', support_type='manager',
                  level='gold', size=1, price=1000, rares=0, artwork=3)
PACKS[191] = dict(name='Training and Club Supplies Pack', edition='support', support_type='consumable',
                  level='gold', size=7, price=2000, rares=7, artwork=3)
PACKS[192] = dict(name='Contracts Pack', edition='support', support_type='contracts',
                  level='gold', size=12, price=2000, rares=12, artwork=3)


def description(spec):
    if spec['edition'] == 'support':
        text = ('One manager for your squad.' if spec['support_type']=='manager' else
                '10 player and 2 manager contract cards, each adding 28 matches.' if spec['support_type']=='contracts' else
                'Player and goalkeeper training, player and manager contracts, player and squad fitness, and injury healing.')
    elif spec['edition'] == 'england110':
        text = f"{spec['size']} England players with 110 ratings."
    elif spec['edition'] == 'prime':
        text = f"{spec['size']} Prime player(s) rated {spec.get('min_rating', 1)} or higher. Prime packs are now web-only."
    else:
        text = f"{spec['size']} {spec['level']} players with normal ratings, including {spec['rares']} rare."
        if 'theme' in spec:
            text += f" All players: {spec['theme']}."
        if spec.get('min_rating', 75) > 75:
            text += f" Every player rated {spec['min_rating']} or higher."
    return text+f" Costs {spec['price']:,} local coins."


def level(player):
    return 'gold' if player['rating'] >= 75 else 'silver' if player['rating'] >= 65 else 'bronze'


def rare(player):
    # Rarity is a local rule, not historical EA rarity metadata.
    return int(player['rating'] >= {'bronze': 62, 'silver': 72, 'gold': 80}[level(player)])


def discard_value(player):
    return {'bronze': (20, 75), 'silver': (100, 250), 'gold': (300, 600)}[level(player)][rare(player)]


POOLS = {(tier, flag): [p for p in STANDARD['players'] if level(p) == tier and rare(p) == flag]
         for tier in ('bronze', 'silver', 'gold') for flag in (0, 1)}


# Available rating bands share probability mass rather than competing with
# hundreds of lower-rated players. Restricted packs renormalize these weights.
GOLD_RARE_BANDS = ((80, 82, 30), (83, 84, 30), (85, 89, 30), (90, 99, 10))


def eligible_pool(spec, flag):
    return [p for p in POOLS[spec['level'], flag] if
            ('nation' not in spec or p['nation'] == spec['nation']) and
            ('leagueId' not in spec or p['leagueId'] == spec['leagueId']) and
            ('positions' not in spec or p['preferredPosition'] in spec['positions']) and
            p['rating'] >= spec.get('min_rating', 0)]


def draw_weights(pool, tier, flag, weights=None):
    if tier == 'gold' and flag == 1:
        bands = {p['assetId']: next(i for i, (low, high, _) in enumerate(GOLD_RARE_BANDS)
                                   if low <= p['rating'] <= high) for p in pool}
        counts = Counter(bands.values())
        return [(weights[bands[p['assetId']]] if weights is not None else GOLD_RARE_BANDS[bands[p['assetId']]][2])
                /counts[bands[p['assetId']]] for p in pool]
    floor = min(p['rating'] for p in pool)
    return [1/(p['rating']-floor+1)**2 for p in pool]


def draw(spec, england, weights=None, settings=None):
    rng = random.SystemRandom()
    if spec['edition'] == 'england110':
        return rng.sample(england, spec['size'])
    if spec['edition'] != 'standard':
        from card_versions import catalogue_assets, catalogue_player
        if settings is None:
            from server_settings import DEFAULT
            settings = DEFAULT
        pool = catalogue_assets(settings, spec['edition'], available=True, min_rating=spec.get('min_rating', 0))
        if len(pool) < spec['size']: raise ValueError('Not enough enabled custom players for this pack')
        selected = []
        for asset in rng.sample(pool, spec['size']):
            player, variant = catalogue_player(settings, spec['edition'], asset)
            selected.append({**player, '_edition': spec['edition'], '_variant': variant})
        return selected
    chosen = []
    for flag, count in ((0, spec['size']-spec['rares']), (1, spec['rares'])):
        if not count:
            continue
        pool = eligible_pool(spec, flag)
        if len(pool) < count:
            raise ValueError('Pack has insufficient eligible players')
        for _ in range(count):
            player = rng.choices(pool, weights=draw_weights(pool, spec['level'], flag, weights), k=1)[0]
            chosen.append(player)
            pool.remove(player)
    rng.shuffle(chosen)
    if spec['level'] == 'gold':
        from card_versions import upgrade_standard_draw
        if settings is None:
            from server_settings import DEFAULT
            settings = DEFAULT
        chosen = upgrade_standard_draw(chosen, settings, rng)
    return chosen
