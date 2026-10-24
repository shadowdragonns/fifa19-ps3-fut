"""Validated, per-server rules. Saved receipts never depend on current rules."""
from copy import deepcopy
import json
from pathlib import Path
import hashlib
import threading
import time


def defaults():
    from fut_packs import PACKS
    from card_versions import default_editions
    from web_packs import DEFAULT_PRICING
    from fut_seasons import CPU_TIERS, CPU_DIFFICULTIES, DIFFICULTY_NAMES
    return {
        'version': 1,
        'economy': {'starting_coins': 20000, 'win_coins': 1000,
                    'cpu_division_bonus': 500, 'draw_percent': 75, 'loss_percent': 50,
                    'quick_sell': {'bronze': [20, 75], 'silver': [100, 250], 'gold': [300, 600]}},
        'packs': {'prices': {str(k): v['price'] for k, v in PACKS.items() if k != 3},
                  'gold_rare_weights': [30, 30, 30, 10]},
        'points': {'win': 3, 'draw': 1, 'loss': 0},
        'cpu_seasons': {'hold': [5+i for i in range(10)], 'promote': [12+i for i in range(10)],
                        'difficulties': [DIFFICULTY_NAMES[level] for level in CPU_DIFFICULTIES],
                        'opponents': [list(teams) for teams in CPU_TIERS]},
        'fitness': {'enabled':True,'seconds_per_point':300},
        'contracts': {'player_price':200,'manager_price':200},
        'matchmaking': {'search_seconds': 20, 'peer_timeout_seconds': 180},
        'plugins': {'enabled': True, 'disabled': [], 'settings': {}},
        'custom_cards': {'enabled': True, 'standard_pack_chance_percent': None, 'editions': default_editions()},
        'web_packs': deepcopy(DEFAULT_PRICING)
    }


def _merge(base, extra, trail='config'):
    if not isinstance(extra, dict): raise ValueError(trail+' must be an object')
    for key, value in extra.items():
        if key not in base: raise ValueError('Unknown setting: '+trail+'.'+key)
        if key == 'editions' and trail == 'config.custom_cards':
            from card_versions import merge_editions
            base[key] = merge_editions(base[key], value)
        elif isinstance(base[key], dict) and key != 'settings':
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
        for key, price in rules['contracts'].items():_integer(price,0,1000000,'contracts.'+key)
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
        from fut_seasons import DIFFICULTY_NAMES
        difficulties = rules['cpu_seasons']['difficulties']
        opponents = rules['cpu_seasons']['opponents']
        if (not isinstance(difficulties, list) or len(difficulties) != 10
                or any(not isinstance(level, str) or level not in DIFFICULTY_NAMES for level in difficulties)):
            raise ValueError('cpu_seasons.difficulties needs ten named difficulty levels')
        if not isinstance(opponents, list) or len(opponents) != 10:
            raise ValueError('cpu_seasons.opponents needs ten division fixture lists')
        for teams in opponents:
            if not isinstance(teams, list) or len(teams) != 10:
                raise ValueError('Each CPU division needs ten opponent team IDs')
            for team in teams: _integer(team, 1, 2**31-1, 'opponent team ID')
        if type(rules['fitness']['enabled']) is not bool:raise ValueError('fitness.enabled must be a boolean')
        _integer(rules['fitness']['seconds_per_point'],1,86400,'fitness.seconds_per_point')
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
        from card_versions import validate_editions
        validate_editions(rules['custom_cards'])
        if type(rules['web_packs']['enabled']) is not bool: raise ValueError('web_packs.enabled must be a boolean')
        for name, value in rules['web_packs'].items():
            if name != 'enabled': _integer(value, 1, 1000000, 'web_packs.'+name)
        self._rules = rules

    @classmethod
    def load(cls, path):
        with Path(path).open('rb') as stream:
            data = stream.read(262145)
        if len(data) > 262144: raise ValueError('Config exceeds 256 KiB')
        return cls.parse(data)

    @classmethod
    def parse(cls, data):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('Duplicate config key: '+key)
                result[key] = value
            return result
        def nonfinite(value): raise ValueError('Non-finite config number: '+value)
        return cls(json.loads(data.decode('utf-8'), object_pairs_hook=unique, parse_constant=nonfinite))

    def replace(self, candidate):
        """Swap a validated snapshot without breaking references held by clubs/queues."""
        if not isinstance(candidate, Settings): raise TypeError('Expected validated Settings')
        self._rules = candidate.as_dict()

    def snapshot(self): return Settings(self.as_dict())

    def section(self, name): return deepcopy(self._rules[name])
    def as_dict(self): return deepcopy(self._rules)

    def pack(self, pack_id):
        from fut_packs import PACKS
        spec = deepcopy(PACKS[pack_id])
        spec['price'] = self._rules['packs']['prices'].get(str(pack_id), spec['price'])
        if spec['edition'] not in ('standard', 'england110', 'support'):
            from card_versions import catalogue_assets
            try:
                spec['retired'] = spec.get('retired', False) or len(catalogue_assets(self, spec['edition'], available=True,
                                                     min_rating=spec.get('min_rating', 0))) < spec['size']
            except KeyError: spec['retired'] = True
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


class ConfigWatcher:
    """Debounce file edits and atomically apply valid rules to a running backend."""
    def __init__(self, backend, path, logger=None, clock=None, interval=0.25, settle=0.5):
        self.backend, self.path = backend.root, Path(path)
        self.logger = logger or (lambda record: None)
        self.clock = clock or time.monotonic
        self.interval, self.settle = interval, settle
        self.generation = 0
        self.updated_at = None
        self.last_error_type = None
        self._observed, self._attempted = object(), object()
        self._observed_at = 0
        self._saw_file = self.path.is_file()
        self._stop = threading.Event()
        self._thread = None

    def status(self):
        with self.backend.lock:
            return {'watching': self._thread is not None and self._thread.is_alive(),
                    'generation': self.generation, 'updated_at': self.updated_at,
                    'last_error_type': self.last_error_type}

    def _log(self, **record):
        try: self.logger(record)
        except Exception: pass

    def poll(self):
        error = None
        try:
            with self.path.open('rb') as stream: data = stream.read(262145)
            self._saw_file = True
            signature = hashlib.sha256(data).digest()
            if len(data) > 262144: error = ValueError('Config exceeds 256 KiB')
        except OSError as failure:
            data, signature, error = None, ('file-error', type(failure).__name__), failure
            # Optional absent startup config uses defaults until the file is created.
            if isinstance(failure, FileNotFoundError) and not self._saw_file: return False
        now = self.clock()
        if signature != self._observed:
            self._observed, self._observed_at = signature, now
            return False
        if now-self._observed_at < self.settle or signature == self._attempted: return False
        self._attempted = signature
        try:
            if error is not None: raise error
            candidate = Settings.parse(data)
        except (OSError, ValueError) as failure:
            with self.backend.lock: self.last_error_type = type(failure).__name__
            details = {'event': 'config.error', 'error_type': type(failure).__name__,
                       'retained_generation': self.generation}
            if isinstance(failure, json.JSONDecodeError):
                details.update(line=failure.lineno, column=failure.colno)
            self._log(**details)
            return False
        root = self.backend
        with root.lock:
            previous, current = root.settings.as_dict(), candidate.as_dict()
            changed = sorted(key for key in current if current[key] != previous[key])
            self.last_error_type = None
            if not changed: return False
            if 'custom_cards' in changed:
                from card_rebalance import reconcile
                try:
                    rebalanced = reconcile(root, candidate)
                except (OSError, ValueError, RuntimeError) as failure:
                    self.last_error_type = type(failure).__name__
                    self._log(event='config.error', error_type=type(failure).__name__,
                              retained_generation=self.generation)
                    return False
                if rebalanced['cards']:
                    self._log(event='custom_cards.updated', reason='config', **rebalanced)
            root.settings.replace(candidate)
            # Plugin modules keep their registrations/state; newly enabled files
            # register once. Errors in custom code cannot reject valid server rules.
            if hasattr(root.events, 'load'):
                try: root.events.load(live=True)
                except Exception as failure:
                    self._log(event='plugin.error', hook='config.reload', error_type=type(failure).__name__)
            self.generation += 1
            self.updated_at = int(time.time())
            self._log(event='config.reloaded', generation=self.generation, changed_sections=changed)
            root.events.emit('server.config_changed', generation=self.generation, changed_sections=changed)
        return True

    def start(self):
        if self._thread is not None: raise RuntimeError('Config watcher already started')
        self._thread = threading.Thread(target=self._run, name='FUT-config', daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try: self.poll()
            except Exception as failure:
                self._log(event='config.error', error_type=type(failure).__name__, retained_generation=self.generation)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread is not None: self._thread.join()
