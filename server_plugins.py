"""Trusted Python extensions receive domain events, never transport credentials."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading

EVENTS = {'server.started', 'player.login', 'pack.opened', 'cards.moved', 'cards.discarded',
          'squad.saved', 'squad.created', 'squad.deleted', 'squad.activated', 'equipment.changed',
          'match.reserved', 'match.paired', 'match.started', 'match.completed', 'season.completed'}
FILTERS = {'match.reward'}


class EventBus:
    def __init__(self, logger=None):
        self._hooks = {}; self._filters = {}; self._lock = threading.RLock()
        self.logger = logger or (lambda record: None)

    def subscribe(self, name, callback, plugin='local', filter=False):
        if name not in (FILTERS if filter else EVENTS) or not callable(callback):
            raise ValueError('Unknown event/filter or non-callable handler: '+name)
        table = self._filters if filter else self._hooks
        table.setdefault(name, []).append((plugin, callback))

    def _failure(self, plugin, event, error):
        # Do not log the payload or exception text: they could contain user data.
        try: self.logger({'event': 'plugin.error', 'plugin': plugin, 'hook': event,
                          'error_type': type(error).__name__})
        except Exception: pass

    def emit(self, event_name, **payload):
        with self._lock:
            for plugin, callback in self._hooks.get(event_name, ()):
                try: callback(deepcopy({'event': event_name, **payload}))
                except Exception as error: self._failure(plugin, event_name, error)

    def reward(self, coins, **payload):
        with self._lock:
            for plugin, callback in self._filters.get('match.reward', ()):
                try:
                    proposed = callback(deepcopy({'event': 'match.reward', **payload}), coins)
                    if type(proposed) is not int or not 0 <= proposed <= 100000000:
                        raise ValueError('Reward filter must return 0..100000000 integer coins')
                    coins = proposed
                except Exception as error: self._failure(plugin, 'match.reward', error)
        return coins


class PluginAPI:
    def __init__(self, bus, name, data_dir, settings):
        self.name, self.data_dir, self.settings = name, data_dir, deepcopy(settings)
        self._bus = bus

    def _register(self, name, callback, filter):
        def attach(fn): self._bus.subscribe(name, fn, self.name, filter); return fn
        return attach(callback) if callback is not None else attach

    def on(self, name, callback=None): return self._register(name, callback, False)
    def filter(self, name, callback=None): return self._register(name, callback, True)
    def log(self, message):
        self._bus.logger({'event': 'plugin.log', 'plugin': self.name, 'message': str(message)[:2000]})


class PluginManager(EventBus):
    def __init__(self, directory, data_dir, settings, logger=None):
        super().__init__(logger)
        self.directory, self.data_dir = Path(directory), Path(data_dir)
        self.settings = settings.section('plugins'); self.loaded = []

    def load(self, check=False):
        if not self.settings['enabled']: return []
        if not self.directory.exists(): return []
        for path in sorted(self.directory.glob('*.py')):
            name = path.stem
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name) or name in self.settings['disabled']: continue
            # Preflight checks syntax without executing trusted custom code.
            compile(path.read_text(), str(path), 'exec')
            if not check:
                hooks = {key:list(value) for key,value in self._hooks.items()}
                filters = {key:list(value) for key,value in self._filters.items()}
                module_name = '_fifa19_plugin_'+name
                try:
                    spec = importlib.util.spec_from_file_location(module_name, path)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module; spec.loader.exec_module(module)
                    if not callable(getattr(module, 'register', None)):
                        raise ValueError('Plugin must define register(api)')
                    directory = self.data_dir/name; directory.mkdir(parents=True, exist_ok=True)
                    module.register(PluginAPI(self, name, directory, self.settings['settings'].get(name, {})))
                except Exception as error:
                    self._hooks, self._filters = hooks, filters
                    sys.modules.pop(module_name, None)
                    self._failure(name, 'register', error)
                    continue
            self.loaded.append(name)
        return list(self.loaded)


def player(backend):
    from fut_squads import display_name
    return {'persona_id': backend.profile['persona'], 'club_name': display_name(backend)}


def match_event(backend, current, **extra):
    return {'player': player(backend), 'match_id': current.get('id'),
            'game_id': current.get('game_id'), 'mode': current['request']['type'],
            'season_id': current['request'].get('seasonId', 0),
            'division_id': current['request'].get('divisionId', 0),
            'squad_id': current['squad']['id'], **extra}
