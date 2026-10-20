"""Trusted Python extensions receive domain events, never transport credentials."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
from types import ModuleType
from contextlib import nullcontext

EVENTS = {'server.started', 'server.stopping', 'server.stopped', 'server.config_changed',
          'player.online', 'player.offline', 'player.fut_entered', 'player.fut_left',
          'player.created', 'player.login', 'player.logout', 'pack.opened', 'web.pack.opened', 'cards.moved', 'cards.discarded',
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

    def _active(self, plugin): return True

    def subscribe(self, name, callback, plugin='local', filter=False):
        if name not in (FILTERS if filter else EVENTS) or not callable(callback):
            raise ValueError('Unknown event/filter or non-callable handler: '+name)
        with self._lock:
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
                if not self._active(plugin): continue
                try: callback(deepcopy({'event': event_name, **payload}))
                except Exception as error: self._failure(plugin, event_name, error)

    def reward(self, coins, **payload):
        with self._backend_lock(), self._lock:
            for plugin, callback in self._filters.get('match.reward', ()):
                if not self._active(plugin): continue
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
        self.name, self.data_dir, self._settings = name, data_dir, deepcopy(settings)
        self._bus = bus

    @property
    def settings(self):
        if isinstance(self._bus, PluginManager):
            return deepcopy(self._bus.settings['settings'].get(self.name, {}))
        return deepcopy(self._settings)

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
    def __init__(self, directory, data_dir, settings, logger=None, clock=None, interval=0.25, settle=0.5):
        super().__init__(logger)
        self.directory, self.data_dir = Path(directory), Path(data_dir)
        self.config = settings; self.loaded = []
        self.clock = clock or time.monotonic
        self.interval, self.settle = interval, settle
        self._modules = {}; self._digests = {}; self._observed = {}; self._attempted = {}
        self._errors = {}; self.generation = 0
        self._stop = threading.Event(); self._thread = None

    @property
    def settings(self): return self.config.section('plugins')

    def _active(self, plugin):
        rules = self.settings
        return rules['enabled'] and plugin not in rules['disabled']

    def active_names(self):
        with self._lock: return [name for name in self.loaded if self._active(name)]

    def _paths(self):
        return {path.stem: path for path in sorted(self.directory.glob('*.py'))
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', path.stem)}

    def _remove_hooks(self, name):
        for table in (self._hooks, self._filters):
            for key, rows in list(table.items()):
                table[key] = [row for row in rows if row[0] != name]

    def _replace(self, name, path, source, live=False):
        """Call under both locks: callbacks see the entire old or new plugin."""
        hooks = {key:list(value) for key,value in self._hooks.items()}
        filters = {key:list(value) for key,value in self._filters.items()}
        module_name = '_fifa19_plugin_'+name
        previous = sys.modules.get(module_name)
        replacing = name in self.loaded
        try:
            # Execute these exact bytes: timestamp-based .pyc caches can serve
            # stale source after same-length edits within one second.
            code = compile(source, str(path), 'exec')
            module = ModuleType(module_name)
            module.__file__ = str(path); module.__package__ = ''
            sys.modules[module_name] = module
            exec(code, module.__dict__)
            if not callable(getattr(module, 'register', None)):
                raise ValueError('Plugin must define register(api)')
            self._remove_hooks(name)
            directory = self.data_dir/name; directory.mkdir(parents=True, exist_ok=True)
            module.register(PluginAPI(self, name, directory, self.settings['settings'].get(name, {})))
        except Exception as error:
            self._hooks, self._filters = hooks, filters
            if previous is None: sys.modules.pop(module_name, None)
            else: sys.modules[module_name] = previous
            self._errors[name] = type(error).__name__
            self._failure(name, 'reload' if replacing else 'register', error)
            return False
        self._modules[name] = module
        self._digests[name] = hashlib.sha256(source).digest()
        self._errors.pop(name, None)
        if not replacing: self.loaded.append(name)
        self.loaded.sort()
        for table in (self._hooks, self._filters):
            for callbacks in table.values(): callbacks.sort(key=lambda pair: pair[0])
        if live:
            self.generation += 1
            self._log_change('plugin.reloaded' if replacing else 'plugin.loaded', name)
        return True

    def _log_change(self, event, name):
        try: self.logger({'event': event, 'plugin': name, 'generation': self.generation})
        except Exception: pass

    def load(self, check=False, live=False):
        with self._backend_lock(), self._lock:
            if not self.settings['enabled']: return []
            checked = []
            for name, path in self._paths().items():
                if name in self.settings['disabled'] or not check and name in self.loaded: continue
                source = path.read_bytes()
                if check or not live: compile(source, str(path), 'exec')
                checked.append(name)
                if not check:
                    signature = hashlib.sha256(source).digest()
                    if live and self._attempted.get(name) == signature: continue
                    self._attempted[name] = signature
                    self._replace(name, path, source, live=live)
            return checked if check else self.active_names()

    def poll(self):
        """Debounce source additions, edits and removals; retry failures on edits."""
        paths = self._paths()
        now = self.clock(); changed = False
        with self._backend_lock(), self._lock:
            for name in sorted(set(paths) | set(self.loaded) | set(self._observed)):
                path = paths.get(name)
                error = None; source = None
                try:
                    if path is not None: source = path.read_bytes()
                    signature = hashlib.sha256(source).digest() if source is not None else None
                except OSError as failure:
                    error = failure; signature = ('read-error', type(failure).__name__)
                observed = self._observed.get(name)
                if observed is None or signature != observed[0]:
                    self._observed[name] = (signature, now); continue
                if now-observed[1] < self.settle or self._attempted.get(name, object()) == signature: continue
                if error is not None:
                    self._attempted[name] = signature
                    self._errors[name] = type(error).__name__
                    self._failure(name, 'reload', error); continue
                if source is None:
                    self._attempted[name] = None
                    self._errors.pop(name, None)
                    if name in self.loaded:
                        self._remove_hooks(name); self.loaded.remove(name)
                        module = self._modules.pop(name, None)
                        if sys.modules.get('_fifa19_plugin_'+name) is module:
                            sys.modules.pop('_fifa19_plugin_'+name, None)
                        self._digests.pop(name, None)
                        self.generation += 1; self._log_change('plugin.unloaded', name)
                        changed = True
                    continue
                if not self._active(name): continue
                self._attempted[name] = signature
                if self._digests.get(name) != signature:
                    changed = self._replace(name, path, source, live=True) or changed
        return changed

    def status(self):
        with self._backend_lock(), self._lock:
            return {'watching': self._thread is not None and self._thread.is_alive(),
                    'generation': self.generation, 'loaded_count': len(self.loaded),
                    'active_count': len(self.active_names()), 'failed_count': len(self._errors)}

    def start(self):
        if self._thread is not None: raise RuntimeError('Plugin watcher already started')
        self._thread = threading.Thread(target=self._run, name='FUT-plugins', daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try: self.poll()
            except Exception as error: self._failure('watcher', 'reload', error)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread is not None: self._thread.join()


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
