"""Live rules update existing clubs without reconnects, partial configs or repaying matches."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import fut_content
import fut_inventory
import fut_leaderboards
import fut_matches
import fut_results
import fut_seasons
from matchmaking import MatchmakingQueue
from probe import Capture, Handler
from server_plugins import PluginAPI, PluginManager
from server_settings import ConfigWatcher, Settings
from sessions import Backend, Session
from test_launcher import WireSocket
from test_project import club
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
BUY = {'packId': 6, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'}


class LiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.config = self.path/'server-config.json'
        self.config.write_text('{}', encoding='utf-8')
        self.root = Backend(self.path/'state/profile.json', multiuser=True, settings=Settings())
        self.time = 0.0
        self.logs = []
        self.watcher = ConfigWatcher(self.root, self.config, self.logs.append, clock=lambda: self.time)

    def apply(self, rules):
        self.config.write_text(json.dumps(rules), encoding='utf-8')
        self.watcher.poll()
        self.time += 0.75
        return self.watcher.poll()

    def files(self):
        return {str(p): p.read_bytes() for p in (self.path/'state').rglob('*.json')}

    def test_reload_updates_existing_and_new_clubs_without_resetting_saves_sessions_or_searches(self):
        owners = [self.root.for_user(n) for n in (10, 20)]
        before = self.files()
        session = Session(owners[1].profile['persona'], owners[1].profile['account'], 'Test',
                          'synthetic-key', 1, 1, postauth=True, fut_key='synthetic-fut', owner=owners[1])
        self.root.sessions[session.key] = session
        self.root.matchmaking.jobs[session.key] = {'session': session, 'deadline': 20}
        events = []
        self.root.events.subscribe('server.config_changed', events.append)
        self.assertTrue(self.apply({'economy': {'starting_coins': 99000, 'win_coins': 2500,
                                              'quick_sell': {'gold': [333, 777]}},
                                   # This assertion covers ordinary gold quick-sell
                                   # rules; special editions have their own prices.
                                   'custom_cards': {'standard_pack_chance_percent': 0},
                                   'packs': {'prices': {'6': 100}, 'gold_rare_weights': [1, 1, 1, 97]},
                                   'points': {'win': 5}, 'cpu_seasons': {'promote': [20]*10},
                                   'matchmaking': {'search_seconds': 2, 'peer_timeout_seconds': 90}}))
        self.assertEqual(self.files(), before)
        self.assertIs(self.root.sessions[session.key], session)
        self.assertTrue(session.postauth)
        self.assertEqual(self.root.matchmaking.jobs[session.key]['deadline'], 20)
        self.assertEqual(self.root.matchmaking.settings.section('matchmaking')['search_seconds'], 2)
        self.assertEqual([o.fut_inventory.state['coins'] for o in owners], [20000, 20000])
        for owner in owners:
            self.assertIs(owner.settings, self.root.settings)
            self.assertIs(owner.fut_inventory.settings, self.root.settings)
            offers = {p['id']: p for p in owner.fut_inventory.offers()['purchase']}
            self.assertEqual(offers[6]['currencies'][0]['funds'], 100)
            self.assertEqual(fut_seasons.next_division(1, 12, owner.settings), 1)
            self.assertEqual(owner.settings.reward_policy(1, 1)['baseWin'], 2500)
        self.assertIn(b'Costs 100 local coins', fut_content.response('GET', fut_content.PACK_DESCRIPTIONS_PATH,
                                                                   self.root.settings)[1])
        self.assertEqual(self.root.for_user(30).fut_inventory.state['coins'], 99000)
        purchase = owners[1].fut_inventory.open_pack(BUY)
        self.assertEqual(owners[1].fut_inventory.state['coins'], 19900)
        self.assertEqual({c['discardValue'] for c in purchase['itemList']}, {333, 777})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['generation'], 1)
        self.assertNotIn('synthetic-key', json.dumps(events))

    def test_special_gold_draw_uses_edition_discard_value_after_live_reload(self):
        owner = self.root.for_user(10)
        rules = {'economy': {'quick_sell': {'gold': [333, 777]}},
                 'packs': {'prices': {'6': 100}},
                 'custom_cards': {'editions': {'toty': {'discard_value': 12345}}}}
        self.assertTrue(self.apply(rules))
        # Tickets 25..29 on the 10,000-ticket wheel select TOTY. Exercise
        # the same draw that made the ordinary-gold test fail intermittently.
        with patch('fut_packs.random.SystemRandom.randrange', return_value=25):
            purchase = owner.fut_inventory.open_pack(BUY)
        self.assertEqual({row['discardValue'] for row in purchase['itemList']}, {12345})
        self.assertEqual({row['edition'] for row in owner.fut_inventory.state['items']}, {'toty'})
        self.assertEqual(owner.fut_inventory.state['coins'], 19900)
        before = deepcopy(owner.fut_inventory.state)
        self.assertEqual(owner.fut_inventory.open_pack(BUY), purchase)
        self.assertEqual(owner.fut_inventory.state, before)

    def test_partial_invalid_missing_duplicate_and_oversized_files_keep_last_valid_rules(self):
        self.apply({'packs': {'prices': {'6': 123}}})
        previous = self.root.settings.as_dict()
        for content in ('{"economy":', '{"economy":{"win_coin":123}}', '{"version":1,"version":1}',
                        '{"economy":{"win_coins":NaN}}', 'x'*262145):
            self.config.write_text(content, encoding='utf-8')
            self.watcher.poll(); self.time += 0.75; self.watcher.poll()
            count = len(self.logs)
            self.time += 1; self.watcher.poll()
            self.assertEqual(len(self.logs), count)
            self.assertEqual(self.root.settings.as_dict(), previous)
            self.assertEqual(self.watcher.generation, 1)
            self.assertIsNotNone(self.watcher.status()['last_error_type'])
        self.config.unlink()
        self.watcher.poll(); self.time += 0.75; self.watcher.poll()
        self.assertEqual(self.root.settings.as_dict(), previous)
        self.assertEqual(self.watcher.last_error_type, 'FileNotFoundError')
        self.assertTrue(self.apply({'packs': {'prices': {'6': 456}}}))
        self.assertEqual(self.root.settings.pack(6)['price'], 456)
        self.assertIsNone(self.watcher.last_error_type)

    def test_debounce_accepts_only_stable_content_and_same_effective_config_is_a_noop(self):
        self.config.write_text('{"economy":{"win_coins":1234}}')
        self.watcher.poll(); self.time += 0.2
        self.assertFalse(self.watcher.poll())
        self.config.write_text('{"economy":{"win_coins":5678}}')
        self.watcher.poll(); self.time += 0.2
        self.assertFalse(self.watcher.poll())
        self.time += 0.6
        self.assertTrue(self.watcher.poll())
        self.assertEqual(self.root.settings.reward_policy(1, 1)['baseWin'], 5678)
        self.assertFalse(self.apply({'economy': {'win_coins': 5678}}))
        self.assertEqual(self.watcher.generation, 1)

    def test_absent_optional_config_can_be_created_live_and_failed_recovery_clears_status(self):
        self.config.unlink()
        watcher = ConfigWatcher(self.root, self.config, self.logs.append, clock=lambda: self.time)
        self.assertFalse(watcher.poll())
        self.assertEqual(self.logs, [])
        self.watcher = watcher
        self.assertTrue(self.apply({'economy': {'win_coins': 2345}}))
        self.config.write_text('broken'); watcher.poll(); self.time += 1; watcher.poll()
        self.assertIsNotNone(watcher.last_error_type)
        self.assertFalse(self.apply({'economy': {'win_coins': 2345}}))
        self.assertIsNone(watcher.last_error_type)

    def test_thread_reloads_and_health_exposes_status_without_config_contents(self):
        seen = threading.Event()
        self.root.events.subscribe('server.config_changed', lambda event: seen.set())
        watcher = ConfigWatcher(self.root, self.config, self.logs.append, interval=0.02, settle=0.04)
        try:
            watcher.start()
            self.config.write_text('{"economy":{"win_coins":4321}}')
            self.assertTrue(seen.wait(2))
            self.assertEqual(self.root.settings.reward_policy(1, 1)['baseWin'], 4321)
            with redirect_stdout(io.StringIO()):
                capture = Capture(self.path/'logs', self.root)
                capture.config_watcher = watcher
                server = SimpleNamespace(capture=capture, server_address=('127.0.0.1', 8099), blaze_port=8990)
                sock = WireSocket('/fut-local/health')
                Handler(sock, ('127.0.0.1', 50000), server)
                body = json.loads(bytes(sock.output).split(b'\r\n\r\n', 1)[1])
            self.assertIn('live-config', body['features'])
            self.assertTrue(body['config']['watching'])
            self.assertEqual(body['config']['generation'], 1)
            self.assertNotIn('win_coins', json.dumps(body))
        finally: watcher.stop()
        self.assertFalse(watcher.status()['watching'])

    def test_completed_rewards_and_purchase_retries_keep_original_payment(self):
        session = club(self.root)
        fut_matches.create(self.root, session, {'type': 'OFFLINE', 'squadId': 1})
        current = session.local_fut_match
        ids = [p['itemData']['id'] for p in current['squad']['players'] if p['index'] < 11]
        fut_matches.play(self.root, session, {'items': [{'id': i} for i in ids]})
        report = {'matchReportId': 0, 'endReason': 'WIN', 'secondsPlayed': 5400,
                  'items': [{'id': i} for i in ids], 'matchData': '',
                  'myMatchStats': {k: 0 for k in fut_results.STATS},
                  'opponentMatchStats': {k: 0 for k in fut_results.STATS}}
        report['myMatchStats']['goals'] = 1
        self.apply({'economy': {'win_coins': 2500}, 'packs': {'prices': {'6': 100}}})
        first = fut_results.finish(self.root, session, report)
        self.assertEqual(first['allCoins'], 2500)
        self.root.fut_inventory.open_pack(BUY)
        coins = self.root.fut_inventory.state['coins']
        state = deepcopy(self.root.fut_inventory.state)
        self.apply({'economy': {'win_coins': 9000}, 'points': {'win': 7}, 'packs': {'prices': {'6': 900}}})
        self.assertEqual(fut_results.finish(self.root, session, report), first)
        self.root.fut_inventory.open_pack(BUY)
        self.assertEqual(self.root.fut_inventory.state['coins'], coins)
        self.assertEqual(self.root.fut_inventory.state, state)
        self.assertEqual(fut_leaderboards.statistics(self.root, 'general')['points'], 7)

    def test_plugin_settings_toggles_and_newly_enabled_plugins_update_without_duplicate_registration(self):
        plugins = self.path/'plugins'; plugins.mkdir()
        shutil.copy2(HERE/'plugins/_example_bonus.py', plugins/'win_bonus.py')
        (plugins/'another.py').write_text("def register(api):\n @api.filter('match.reward')\n def bonus(event, coins): return coins+10\n")
        self.root.settings.replace(Settings({'plugins': {'disabled': ['another']}}))
        manager = PluginManager(plugins, self.path/'data', self.root.settings, self.logs.append)
        manager.bind_backend(self.root)
        self.root.events = manager
        self.root.fut_inventory.events = manager
        self.assertEqual(manager.load(), ['win_bonus'])
        api = PluginAPI(manager, 'win_bonus', self.path/'data', {})
        self.assertEqual(manager.reward(1000, result='WIN'), 1250)
        before = api.settings
        self.apply({'plugins': {'settings': {'win_bonus': {'bonus_coins': 500, 'private_setting': 'not-for-logs'}}}})
        self.assertEqual(manager.reward(1000, result='WIN'), 1510)
        self.assertEqual(before, {})
        self.assertEqual(api.settings['bonus_coins'], 500)
        snapshot = api.settings; snapshot['bonus_coins'] = 999
        self.assertEqual(api.settings['bonus_coins'], 500)
        self.assertNotIn('private_setting', json.dumps(self.logs))
        self.apply({'plugins': {'disabled': ['win_bonus']}})
        self.assertEqual(manager.reward(1000, result='WIN'), 1010)
        self.apply({'plugins': {'enabled': False}})
        self.assertEqual(manager.reward(1000, result='WIN'), 1000)
        self.apply({'plugins': {'settings': {'win_bonus': {'bonus_coins': 500}}}})
        self.assertEqual(manager.reward(1000, result='WIN'), 1510)
        self.assertEqual(manager.loaded.count('win_bonus'), 1)
        self.assertEqual(manager.loaded.count('another'), 1)

    def test_invalid_new_plugin_does_not_block_valid_server_config_and_preflight_still_rejects_it(self):
        plugins = self.path/'plugins'; plugins.mkdir()
        (plugins/'broken.py').write_text('this is broken python')
        self.root.settings.replace(Settings({'plugins': {'enabled': False}}))
        manager = PluginManager(plugins, self.path/'data', self.root.settings, self.logs.append)
        manager.bind_backend(self.root); self.root.events = manager
        self.assertTrue(self.apply({'economy': {'win_coins': 2222}, 'plugins': {'enabled': True}}))
        self.assertEqual(self.root.settings.reward_policy(1, 1)['baseWin'], 2222)
        self.assertEqual(manager.loaded, [])
        self.assertTrue(any(r['event'] == 'plugin.error' for r in self.logs))
        with self.assertRaises(SyntaxError): manager.load(check=True)

    def test_check_config_does_not_require_tls_or_unlock_running_state(self):
        locked = self.path/'state/server.lock'; locked.parent.mkdir(exist_ok=True)
        locked.write_text('synthetic lock')
        result = subprocess.run([sys.executable, str(HERE/'server.py'), '--check-config',
                                 '--config', str(self.config), '--state-dir', str(locked.parent)],
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), {'valid': True, 'scope': 'configuration'})
        self.assertEqual(locked.read_text(), 'synthetic lock')
        self.assertFalse(list(self.path.rglob('*.key')))


if __name__ == '__main__': unittest.main()
