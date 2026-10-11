"""Validated, per-server rules. Saved receipts never depend on current rules."""
from copy import deepcopy
import json
from pathlib import Path


def defaults():
    from fut_packs import PACKS
    return {
        'version': 1,
        'economy': {'starting_coins': 20000, 'win_coins': 1000,
                    'cpu_division_bonus': 500, 'draw_percent': 75, 'loss_percent': 50,
                    'quick_sell': {'bronze': [20, 75], 'silver': [100, 250], 'gold': [300, 600]}},
        'packs': {'prices': {str(k): v['price'] for k, v in PACKS.items() if not v.get('retired')},
                  'gold_rare_weights': [30, 30, 30, 10]},
        'points': {'win': 3, 'draw': 1, 'loss': 0},
        'cpu_seasons': {'hold': [5+i for i in range(10)], 'promote': [12+i for i in range(10)]},
        'matchmaking': {'search_seconds': 20, 'peer_timeout_seconds': 180},
        'plugins': {'enabled': True, 'disabled': [], 'settings': {}}
    }


def _merge(base, extra, trail='config'):
    if not isinstance(extra, dict): raise ValueError(trail+' must be an object')
    for key, value in extra.items():
        if key not in base: raise ValueError('Unknown setting: '+trail+'.'+key)
        if isinstance(base[key], dict) and key != 'settings':
            _merge(base[key], value, trail+'.'+key)
        else: base[key] = deepcopy(value)


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')


class Settings:
    def __init__(self, values=None):
        rules = defaults()
        _merge(rules, {} if values is None else values)
        _integer(rules['version'], 1, 1, 'version')
        economy = rules['economy']
        for name in ('starting_coins', 'win_coins', 'cpu_division_bonus'):
            _integer(economy[name], 0, 10000000, 'economy.'+name)
        for name in ('draw_percent', 'loss_percent'):
            _integer(economy[name], 0, 100, 'economy.'+name)
        if economy['loss_percent'] > economy['draw_percent']:
            raise ValueError('Loss rewards must not exceed draw rewards')
        for tier, amounts in economy['quick_sell'].items():
            if not isinstance(amounts, list) or len(amounts) != 2:
                raise ValueError('quick_sell.'+tier+' needs common and rare values')
            for amount in amounts: _integer(amount, 0, 1000000, 'quick_sell.'+tier)
        for key, price in rules['packs']['prices'].items(): _integer(price, 0, 10000000, 'pack price '+key)
        weights = rules['packs']['gold_rare_weights']
        if not isinstance(weights, list) or len(weights) != 4:
            raise ValueError('gold_rare_weights needs four rating-band weights')
        for value in weights: _integer(value, 1, 1000000, 'gold_rare_weights')
        for name, value in rules['points'].items(): _integer(value, 0, 1000, 'points.'+name)
        if not rules['points']['win'] > rules['points']['draw'] >= rules['points']['loss']:
            raise ValueError('Points must satisfy win > draw >= loss')
        hold, promote = (rules['cpu_seasons'][key] for key in ('hold', 'promote'))
        for name, values in (('hold', hold), ('promote', promote)):
            if not isinstance(values, list) or len(values) != 10:
                raise ValueError('cpu_seasons.'+name+' needs ten displayed Division 10-to-1 targets')
            for value in values: _integer(value, 0, 30, 'cpu_seasons.'+name)
        if any(a >= b for a, b in zip(hold, promote)):
            raise ValueError('Hold points must be below promotion points')
        _integer(rules['matchmaking']['search_seconds'], 1, 60, 'matchmaking.search_seconds')
        _integer(rules['matchmaking']['peer_timeout_seconds'], 30, 3600, 'matchmaking.peer_timeout_seconds')
        plugins = rules['plugins']
        if type(plugins['enabled']) is not bool or not isinstance(plugins['settings'], dict):
            raise ValueError('Invalid plugin settings')
        import re
        if (not isinstance(plugins['disabled'], list)
                or any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', n)
                       for n in plugins['disabled'])
                or len(set(plugins['disabled'])) != len(plugins['disabled'])):
            raise ValueError('Invalid disabled plugin names')
        if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', n)
               or not isinstance(v, dict) for n, v in plugins['settings'].items()):
            raise ValueError('plugins.settings maps plugin names to objects')
        self._rules = rules

    @classmethod
    def load(cls, path):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('Duplicate config key: '+key)
                result[key] = value
            return result
        def nonfinite(value): raise ValueError('Non-finite config number: '+value)
        return cls(json.loads(Path(path).read_text(), object_pairs_hook=unique, parse_constant=nonfinite))

    def section(self, name): return deepcopy(self._rules[name])
    def as_dict(self): return deepcopy(self._rules)

    def pack(self, pack_id):
        from fut_packs import PACKS
        spec = deepcopy(PACKS[pack_id])
        spec['price'] = self._rules['packs']['prices'].get(str(pack_id), spec['price'])
        return spec

    def reward_policy(self, season_id, division_id):
        economy = self._rules['economy']
        division = division_id if season_id == 1 and 1 <= division_id <= 10 else 1
        return {'baseWin': economy['win_coins']+economy['cpu_division_bonus']*(division-1),
                'drawPercent': economy['draw_percent'], 'lossPercent': economy['loss_percent'],
                'pluginAdjustment': 0}

    def result_points(self, reason):
        key = {'WIN': 'win', 'DRAW': 'draw', 'LOSS': 'loss'}.get(reason)
        return self._rules['points'][key] if key else 0

    def targets(self, division):
        return {key: self._rules['cpu_seasons'][key][division-1] for key in ('hold', 'promote')}


DEFAULT = Settings()
