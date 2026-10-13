"""Live source replacement is debounced, atomic, and independent of club saves."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from probe import Capture, Handler
from server_plugins import PluginManager
from server_settings import Settings
from sessions import Backend
from test_launcher import WireSocket


def source(bonus, label='working'):
    return f'''def register(api):
    @api.filter('match.reward')
    def reward(event, coins): return coins + {bonus}
    @api.on('match.completed')
    def done(event): api.log('{label}')
'''


class PluginReloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name); self.plugins = self.path/'plugins'; self.plugins.mkdir()
        self.logs = []; self.now = [0.0]
        self.backend = Backend(self.path/'state/profile.json', settings=Settings())
        self.manager = PluginManager(self.plugins, self.path/'data', self.backend.settings,
                                     self.logs.append, clock=lambda: self.now[0])
        self.manager.bind_backend(self.backend); self.backend.events = self.manager

    def settle(self):
        self.manager.poll(); self.now[0] += 1
        return self.manager.poll()

    def test_same_size_same_timestamp_edits_replace_hooks_and_preserve_persistent_data(self):
        path = self.plugins/'bonus.py'; path.write_text(source(10, 'old'))
        self.manager.load(); stamp = path.stat().st_mtime_ns
        state = {p.name:p.read_bytes() for p in (self.path/'state').glob('*.json')}
        data = self.path/'data/bonus/save.json'; data.write_text('{"count":7}')
        for value in (20, 30, 40):
            path.write_text(source(value, 'new')); os.utime(path, ns=(stamp, stamp))
            self.assertTrue(self.settle())
            self.assertEqual(self.manager.reward(100), 100+value)
            self.logs.clear(); self.manager.emit('match.completed')
            self.assertEqual([r['message'] for r in self.logs], ['new'])
        self.assertEqual(self.manager.loaded, ['bonus'])
        self.assertEqual(data.read_text(), '{"count":7}')
        self.assertEqual({p.name:p.read_bytes() for p in (self.path/'state').glob('*.json')}, state)

    def test_invalid_edits_retain_last_working_module_and_do_not_repeat_failures(self):
        path = self.plugins/'bonus.py'; path.write_text(source(10)); self.manager.load()
        module = self.manager._modules['bonus']
        for broken in ('def register(', 'def register(api):\n api.filter("match.reward", lambda e,c:c+999)\n raise ValueError("private")\n',
                       'value = 1\n'):
            path.write_text(broken); self.assertFalse(self.settle())
            self.assertIs(self.manager._modules['bonus'], module)
            self.assertEqual(self.manager.reward(100), 110)
            self.assertEqual(self.manager.status()['failed_count'], 1)
            count = len(self.logs); self.now[0] += 10; self.manager.poll()
            self.assertEqual(len(self.logs), count)
            self.assertNotIn('private', json.dumps(self.logs))
        path.write_text(source(80)); self.assertTrue(self.settle())
        self.assertEqual(self.manager.reward(100), 180)
        self.assertEqual(self.manager.status()['failed_count'], 0)

    def test_new_removed_and_readded_plugins_have_no_duplicate_filters(self):
        (self.plugins/'_ignored.py').write_text('invalid python')
        path = self.plugins/'bonus.py'; path.write_text(source(10))
        self.assertTrue(self.settle()); self.assertEqual(self.manager.reward(100), 110)
        path.unlink(); self.assertTrue(self.settle())
        self.assertEqual(self.manager.reward(100), 100); self.assertEqual(self.manager.loaded, [])
        self.assertTrue((self.path/'data/bonus').is_dir())
        path.write_text(source(20)); self.assertTrue(self.settle())
        self.assertEqual(self.manager.reward(100), 120)
        self.assertEqual([r['event'] for r in self.logs], ['plugin.loaded', 'plugin.unloaded', 'plugin.loaded'])

    def test_disabled_plugin_changes_wait_until_enabled_and_keep_filename_order(self):
        (self.plugins/'a.py').write_text('def register(api): api.filter("match.reward", lambda e,c:c*2)\n')
        path = self.plugins/'b.py'; path.write_text(source(10)); self.manager.load()
        self.assertEqual(self.manager.reward(100), 210)
        self.backend.settings.replace(Settings({'plugins': {'disabled': ['b']}}))
        path.write_text(source(20)); self.assertFalse(self.settle())
        self.assertEqual(self.manager.reward(100), 200)
        self.backend.settings.replace(Settings()); self.assertTrue(self.manager.poll())
        self.assertEqual(self.manager.reward(100), 220)
        for _ in range(3): self.settle()
        self.assertEqual(self.manager.reward(100), 220)
        self.assertEqual(self.manager.loaded, ['a', 'b'])

    def test_partial_edits_are_debounced(self):
        path = self.plugins/'bonus.py'; path.write_text(source(10)); self.manager.load()
        path.write_text('def register('); self.manager.poll(); self.now[0] += .2
        self.assertFalse(self.manager.poll())
        path.write_text(source(20)); self.manager.poll(); self.now[0] += .2
        self.assertFalse(self.manager.poll()); self.assertEqual(self.manager.reward(100), 110)
        self.now[0] += .6; self.assertTrue(self.manager.poll())
        self.assertFalse(any(r['event'] == 'plugin.error' for r in self.logs))

    def test_active_callback_completes_before_source_replacement(self):
        path = self.plugins/'bonus.py'; path.write_text(source(10)); self.manager.load()
        started, finish, reloaded = threading.Event(), threading.Event(), threading.Event()
        def running(event, coins):
            started.set(); finish.wait(2); return coins+10
        self.manager._filters['match.reward'] = [('bonus', running)]
        replies = []; worker = threading.Thread(target=lambda: replies.append(self.manager.reward(100)))
        worker.start(); self.assertTrue(started.wait(1))
        path.write_text(source(20))
        def replace(): self.settle(); reloaded.set()
        reload_worker = threading.Thread(target=replace); reload_worker.start()
        try:
            self.assertFalse(reloaded.wait(.05))
        finally:
            finish.set(); worker.join(2); reload_worker.join(2)
        self.assertEqual(replies, [110]); self.assertTrue(reloaded.is_set())
        self.assertEqual(self.manager.reward(100), 120)

    def test_background_watcher_and_health_status(self):
        manager = PluginManager(self.plugins, self.path/'data', self.backend.settings,
                                self.logs.append, interval=.02, settle=.04)
        manager.bind_backend(self.backend)
        done = threading.Event()
        manager.logger = lambda row: (self.logs.append(row), done.set())
        try:
            manager.start(); (self.plugins/'bonus.py').write_text(source(30))
            self.assertTrue(done.wait(2)); self.assertEqual(manager.reward(100), 130)
            with redirect_stdout(io.StringIO()):
                capture = Capture(self.path/'logs', self.backend); capture.plugin_manager = manager
                server = SimpleNamespace(capture=capture, server_address=('127.0.0.1',8099), blaze_port=8990)
                sock = WireSocket('/fut-local/health'); Handler(sock, ('127.0.0.1',50000), server)
                reply = json.loads(bytes(sock.output).split(b'\r\n\r\n',1)[1])
            self.assertIn('live-plugins', reply['features'])
            self.assertTrue(reply['plugins']['watching'])
            self.assertEqual(reply['plugins']['active_count'], 1)
            self.assertNotIn(str(self.path), json.dumps(reply))
        finally: manager.stop()
        self.assertFalse(manager.status()['watching'])


if __name__ == '__main__': unittest.main()
