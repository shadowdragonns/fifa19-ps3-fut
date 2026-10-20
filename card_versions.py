"""Configurable custom player editions with stable native IDs and saved card stats."""
from copy import deepcopy
from fractions import Fraction
import math
import re

BASE_EDITIONS = {'standard', 'england110'}
MAX_OVR = 255  # Native card field is an unsigned byte.
MAX_ATTRIBUTE = (1 << 32)-1  # Native indexed card attributes are uint32.
POSITIONS = {'GK', 'RB', 'RWB', 'CB', 'LB', 'LWB', 'CDM', 'CM', 'CAM', 'RM', 'LM', 'RW', 'LW', 'CF', 'ST'}


def default_editions():
    return {'prime': {'enabled': True, 'label': 'Prime', 'version': 1, 'rareflag': 3,
                      'discard_value': 5000, 'all_players': True, 'rating_boost': 10,
                      'attribute_boost': 10, 'one_in': 400, 'players': {}},
            'toty': {'enabled': True, 'label': 'Team of the Year', 'version': 2, 'rareflag': 5,
                     'discard_value': 10000, 'all_players': True, 'rating_boost': 20,
                     'attribute_boost': 20, 'one_in': 2000, 'players': {}},
            'otw': {'enabled': True, 'label': 'Ones to Watch', 'version': 3, 'rareflag': 21,
                    'discard_value': 15000, 'all_players': True, 'rating_boost': 25,
                    'attribute_boost': 25, 'one_in': 10000, 'players': {}}}


def merge_editions(base, extra):
    if not isinstance(extra, dict): raise ValueError('custom_cards.editions must be an object')
    result = deepcopy(base)
    for name, fields in extra.items():
        if not isinstance(fields, dict): raise ValueError('Custom edition must be an object')
        allowed = {'enabled', 'label', 'version', 'rareflag', 'discard_value', 'players',
                   'all_players', 'rating_boost', 'attribute_boost', 'one_in'}
        if set(fields)-allowed: raise ValueError('Unknown custom edition setting')
        spec = result.setdefault(name, {'enabled': True, 'label': name, 'version': None,
                                       'rareflag': 3, 'discard_value': 5000, 'players': {},
                                       'all_players': False, 'rating_boost': 0, 'attribute_boost': None, 'one_in': 0})
        for key, value in fields.items():
            if key != 'players': spec[key] = deepcopy(value); continue
            if not isinstance(value, dict): raise ValueError('Custom players must be an object')
            for asset, override in value.items():
                if not isinstance(override, dict): raise ValueError('Custom player definition must be an object')
                spec['players'].setdefault(asset, {'enabled': True}).update(deepcopy(override))
    return result


def _integer(value, low, high): return type(value) is int and low <= value <= high


def _label(value):
    return isinstance(value, str) and 1 <= len(value) <= 32 and not any(ord(c) < 32 for c in value)


def validate_editions(section):
    from fut_packs import STANDARD_PLAYERS
    chance = section['standard_pack_chance_percent']
    if (type(section['enabled']) is not bool
            or chance is not None and (type(chance) not in (int, float) or not 0 <= chance <= 100 or not math.isfinite(chance))
            or not isinstance(section['editions'], dict) or len(section['editions']) > 32):
        raise ValueError('Invalid custom card settings')
    versions = set()
    for name, spec in section['editions'].items():
        if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', name)
                or name in BASE_EDITIONS or type(spec['enabled']) is not bool or not _label(spec['label'])
                or not _integer(spec['version'], 1, 127) or spec['version'] in versions
                or not _integer(spec['rareflag'], 0, 255)
                or not _integer(spec['discard_value'], 0, 1000000)
                or type(spec['all_players']) is not bool
                or not _integer(spec['rating_boost'], 0, MAX_ATTRIBUTE)
                or spec['attribute_boost'] is not None and not _integer(spec['attribute_boost'], 0, MAX_ATTRIBUTE)
                or not _integer(spec['one_in'], 0, 1000000000)
                or not isinstance(spec['players'], dict) or len(spec['players']) > 2000):
            raise ValueError('Invalid custom edition or reused version number')
        versions.add(spec['version'])
        for asset, player in spec['players'].items():
            if (not isinstance(asset, str) or not re.fullmatch(r'[1-9][0-9]{0,7}', asset)
                    or int(asset) not in STANDARD_PLAYERS or int(asset) >= 1 << 24
                    or not isinstance(player, dict) or set(player)-{'enabled', 'rating', 'attributes', 'position'}
                    or type(player.get('enabled')) is not bool
                    or 'rating' in player and not _integer(player['rating'], 1, MAX_OVR)
                    or 'position' in player and (not isinstance(player['position'], str) or player['position'] not in POSITIONS)):
                raise ValueError('Invalid custom player definition')
            if 'attributes' in player and (not isinstance(player['attributes'], list)
                    or len(player['attributes']) != 6 or any(not _integer(v, 0, MAX_ATTRIBUTE) for v in player['attributes'])):
                raise ValueError('Custom attributes need six unsigned 32-bit integers')
    if sum((Fraction(1, spec['one_in']) for spec in section['editions'].values()
            if spec['enabled'] and spec['one_in']), Fraction()) > 1:
        raise ValueError('Custom card probabilities must total at most 100%')


def edition_names(settings): return sorted(settings.section('custom_cards')['editions'])


def catalogue_assets(settings, edition, available=False, min_rating=0):
    from fut_packs import STANDARD_PLAYERS
    section = settings.section('custom_cards')
    spec = section['editions'][edition]
    if available and (not section['enabled'] or not spec['enabled']): return []
    assets = set(STANDARD_PLAYERS) if spec['all_players'] else set()
    assets.update(int(asset) for asset in spec['players'])
    return sorted(asset for asset in assets if (not available or _enabled(spec, asset))
                  and _rating(spec, STANDARD_PLAYERS[asset], spec['players'].get(str(asset), {})) >= min_rating)


def _enabled(spec, asset):
    return spec['players'].get(str(asset), {}).get('enabled', spec['all_players'])


def _rating(spec, base, rule):
    return rule.get('rating', min(MAX_OVR, base['rating']+spec['rating_boost']))


def catalogue_player(settings, edition, asset):
    spec = settings.section('custom_cards')['editions'][edition]
    if not spec['all_players'] and str(asset) not in spec['players']: raise KeyError(asset)
    variant = edition_variant(spec, asset)
    return variant_player(asset, variant), variant


def edition_variant(spec, asset):
    """Current stats, independent of whether this player is available to draw."""
    from fut_packs import STANDARD_PLAYERS
    rule = spec['players'].get(str(asset), {})
    base = STANDARD_PLAYERS[asset]
    rating = _rating(spec, base, rule)
    # Keep each footballer's strengths/weaknesses; custom attributes may override
    # these six locally estimated faces. GK slots retain their native GK order.
    boost = spec['attribute_boost'] if spec['attribute_boost'] is not None else max(0, rating-base['rating'])
    attributes = rule.get('attributes', [min(MAX_ATTRIBUTE, row['value']+boost)
                                         for row in base['attributeList']])
    return {'version': spec['version'], 'label': spec['label'], 'rareflag': spec['rareflag'],
            'discard_value': spec['discard_value'], 'rating': rating,
            'attributes': deepcopy(attributes), 'position': rule.get('position', base['preferredPosition'])}


def validate_variant(edition, asset, variant):
    from fut_packs import STANDARD_PLAYERS
    if (not isinstance(edition, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', edition)
            or edition in BASE_EDITIONS or not _integer(asset, 1, (1 << 24)-1) or asset not in STANDARD_PLAYERS
            or not isinstance(variant, dict)
            or set(variant) != {'version', 'label', 'rareflag', 'discard_value', 'rating', 'attributes', 'position'}
            or not _integer(variant['version'], 1, 127) or not _label(variant['label'])
            or not _integer(variant['rareflag'], 0, 255)
            or not _integer(variant['discard_value'], 0, 1000000)
            or not _integer(variant['rating'], 1, MAX_OVR)
            or not isinstance(variant['position'], str) or variant['position'] not in POSITIONS or not isinstance(variant['attributes'], list)
            or len(variant['attributes']) != 6 or any(not _integer(v, 0, MAX_ATTRIBUTE) for v in variant['attributes'])):
        raise ValueError('Invalid saved custom card snapshot')


def variant_player(asset, variant):
    from fut_packs import STANDARD_PLAYERS
    player = deepcopy(STANDARD_PLAYERS[asset])
    player.update(name=variant['label']+' '+player['name'], rating=variant['rating'],
                  preferredPosition=variant['position'],
                  attributeList=[{'index': i, 'value': value} for i, value in enumerate(variant['attributes'])])
    return player


def resource_id(record):
    if record.get('edition', 'england110') in BASE_EDITIONS: return record['assetId']
    return (record['variant']['version'] << 24) | record['assetId']


def discard_value(record, settings):
    section = settings.section('custom_cards')
    current = section['editions'].get(record['edition'])
    if current is not None and current['version'] == record['variant']['version']:
        return current['discard_value']
    return record['variant']['discard_value']


def upgrade_standard_draw(players, settings, rng):
    section = settings.section('custom_cards')
    chance = section['standard_pack_chance_percent']
    if not section['enabled']: return players
    if chance is None:
        # One mutually exclusive roll per card gives exact, unconditional odds.
        # Unavailable editions leave their tickets as base cards, not rerolls.
        editions = [(name, spec) for name, spec in section['editions'].items()
                    if spec['enabled'] and spec['one_in']]
        if not editions: return players
        wheel = math.lcm(*(spec['one_in'] for _, spec in editions))
        chosen = []
        for player in players:
            roll, cursor, edition = rng.randrange(wheel), 0, None
            for name, spec in editions:
                cursor += wheel//spec['one_in']
                if roll < cursor:
                    if _enabled(spec, player['assetId']): edition = name
                    break
            if edition is None: chosen.append(player)
            else:
                upgraded, variant = catalogue_player(settings, edition, player['assetId'])
                chosen.append({**upgraded, '_edition': edition, '_variant': variant})
        return chosen
    if not chance: return players
    # Explicit legacy percentages retain the older combined-chance behavior.
    # Use the exact decimal percentage: 0.001% is precisely 1 in 100,000.
    odds = Fraction(str(chance))/100
    chosen = []
    for player in players:
        editions = [name for name, spec in section['editions'].items() if spec['enabled']
                    and _enabled(spec, player['assetId'])]
        if editions and rng.randrange(odds.denominator) < odds.numerator:
            edition = rng.choice(editions)
            upgraded, variant = catalogue_player(settings, edition, player['assetId'])
            chosen.append({**upgraded, '_edition': edition, '_variant': variant})
        else: chosen.append(player)
    return chosen
