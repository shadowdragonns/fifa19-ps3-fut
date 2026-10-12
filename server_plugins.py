"""Trusted Python extensions receive domain events, never transport credentials."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
from contextlib import nullcontext

EVENTS = {'server.started', 'server.stopping', 'server.stopped',
          'player.online', 'player.offline', 'player.fut_entered', 'player.fut_left',
          'player.created', 'player.login', 'player.logout', 'pack.opened', 'cards.moved', 'cards.discarded',
          'squad.saved', 'squad.created', 'squad.deleted', 'squad.activated', 'equipment.changed',
          'match.reserved', 'match.paired', 'match.started', 'match.completed', 'season.completed'}
FILTERS = {'match.reward'}


class EventBus:
    def __init__(self, logger=None):
        self._hooks = {}; self._filters = {}; self._lock = threading.RLock()
        self.logger = logger or (lambda record: None)
        self._backend = None
        self._queries = None

    def bind_backend(self, backend):
        from plugin_queries import Queries
        self._backend = backend.root
        self._queries = Queries(backend)
        return self

    def _backend_lock(self):
        return self._backend.lock if self._backend is not None else nullcontext()

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
        with self._backend_lock(), self._lock:
            for plugin, callback in self._hooks.get(event_name, ()):
                try: callback(deepcopy({'event': event_name, **payload}))
                except Exception as error: self._failure(plugin, event_name, error)

    def reward(self, coins, **payload):
        with self._backend_lock(), self._lock:
            for plugin, callback in self._filters.get('match.reward', ()):
                try:
                    proposed = callback(deepcopy({'event': 'match.reward', **payload}), coins)
                    if type(proposed) is not int or not 0 <= proposed <= 100000000:
                        raise ValueError('Reward filter must return 0..100000000 integer coins')
                    coins = proposed
                except Exception as error: self._failure(plugin, 'match.reward', error)
        return coins


class PluginAPI:
    api_version = 2
    events = tuple(sorted(EVENTS))
    filters = tuple(sorted(FILTERS))
    def __init__(self, bus, name, data_dir, settings):
        self.name, self.data_dir, self.settings = name, data_dir, deepcopy(settings)
        self._bus = bus

    def _register(self, name, callback, filter):
        def attach(fn): self._bus.subscribe(name, fn, self.name, filter); return fn
        return attach(callback) if callback is not None else attach

    def on(self, name, callback=None): return self._register(name, callback, False)
    def filter(self, name, callback=None): return self._register(name, callback, True)

    @property
    def _queries(self):
        if self._bus._queries is None: raise RuntimeError('Queries require a bound running server backend')
        return self._bus._queries

    def get_players(self, **kwargs): return self._queries.get_players(**kwargs)
    def get_player(self, persona_id): return self._queries.get_player(persona_id)
    def get_clubs(self, **kwargs): return self._queries.get_clubs(**kwargs)
    def get_club(self, persona_id): return self._queries.get_club(persona_id)
    def get_cards(self, persona_id, **kwargs): return self._queries.get_cards(persona_id, **kwargs)
    def get_card(self, persona_id, item_id): return self._queries.get_card(persona_id, item_id)
    def get_equipment(self, persona_id, **kwargs): return self._queries.get_equipment(persona_id, **kwargs)
    def get_squads(self, persona_id): return self._queries.get_squads(persona_id)
    def get_squad(self, persona_id, squad_id=None): return self._queries.get_squad(persona_id, squad_id)
    def get_matches(self, persona_id, **kwargs): return self._queries.get_matches(persona_id, **kwargs)
    def get_match(self, persona_id, match_id): return self._queries.get_match(persona_id, match_id)
    def get_packs(self, **kwargs): return self._queries.get_packs(**kwargs)
    def get_pack(self, pack_id): return self._queries.get_pack(pack_id)
    def get_catalogue_card(self, asset_id, **kwargs): return self._queries.get_catalogue_card(asset_id, **kwargs)
    def search_catalogue(self, **kwargs): return self._queries.search_catalogue(**kwargs)
    def get_rankings(self, category='competitor', **kwargs): return self._queries.get_rankings(category, **kwargs)
    def get_config(self): return self._queries.get_config()
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
    from plugin_queries import player_snapshot
    return player_snapshot(backend)


def came_online(backend, session):
    if session.presence_online: return
    session.presence_online = True
    backend.root.events.emit('player.online', player=player(backend), reason='post_auth')


def entered_fut(backend, session):
    if session.presence_fut: return
    session.presence_fut = True
    backend.root.events.emit('player.fut_entered', player=player(backend))
    backend.root.events.emit('player.login', player=player(backend))


def left_fut(backend, session, reason='logout'):
    was_in_fut = session.presence_fut or bool(session.fut_key)
    session.fut_key = ''
    session.presence_fut = False
    if was_in_fut:
        backend.root.events.emit('player.fut_left', player=player(backend), reason=reason)
        backend.root.events.emit('player.logout', player=player(backend), reason=reason)


def went_offline(backend, session, reason='disconnect'):
    was_online = session.presence_online or session.postauth
    session.postauth = False
    session.presence_online = False
    left_fut(backend, session, reason)
    if was_online:
        import time
        backend.root.presence_last_seen[session.persona] = int(time.time())
        backend.root.events.emit('player.offline', player=player(backend), reason=reason)


def match_event(backend, current, **extra):
    return {'player': player(backend), 'match_id': current.get('id'),
            'game_id': current.get('game_id'), 'mode': current['request']['type'],
            'season_id': current['request'].get('seasonId', 0),
            'division_id': current['request'].get('divisionId', 0),
            'squad_id': current['squad']['id'], **extra}
