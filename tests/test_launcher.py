"""Certificate bootstrap and GUI commands, using generated identities and fake games."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from endpoints import Endpoints
import initialize_tls
import launch
import launcher_gui
from probe import Capture, Handler
from sessions import Backend
import server_trust
from tls_identity import certificate_details


class WireSocket:
    def __init__(self, path):
        self.input = bytearray(f'GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
        self.output = bytearray()
    def settimeout(self, _): pass
    def recv(self, count):
        value = bytes(self.input[:count]); del self.input[:count]; return value
    def sendall(self, data): self.output.extend(data)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for fresh test identities')
class CertificateDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.pem = initialize_tls.initialize(root/'first').read_bytes()
        cls.another = initialize_tls.initialize(root/'second').read_bytes()
    @classmethod
    def tearDownClass(cls): cls.directory.cleanup()

    def health(self, pem=None):
        return {'service': 'fifa19-fut', 'version': 1, 'advertise': '127.0.0.1',
                'public_certificate': {'path': '/fut-local/root.pem',
                                       'sha256': certificate_details(pem or self.pem)['sha256']}}

    def transport(self, pem=None):
        pem = pem or self.pem
        def reply(url, limit, context=None):
            if url.endswith('/fut-local/root.pem'): return pem
            self.assertTrue(url.endswith('/fut-local/health'))
            if url.startswith('https:'):
                self.assertIsNotNone(context)
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, 2)
                self.assertEqual(len(context.get_ca_certs()), 1)
            return json.dumps(self.health(pem)).encode()
        return reply

    def test_http_route_serves_only_the_public_ca_and_advertises_its_der_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            capture = Capture(Path(directory)/'logs', Backend(Path(directory)/'state/profile.json'))
            capture.public_ca = self.pem
            capture.public_ca_sha256 = certificate_details(self.pem)['sha256']
            server = SimpleNamespace(capture=capture, server_address=('127.0.0.1', 8099), blaze_port=8990)
            for path in ('/fut-local/health', '/fut-local/root.pem', '/fut-local/root.key',
                         '/fut-local/../tls/root.key', '/tls/root.key'):
                sock = WireSocket(path)
                Handler(sock, ('127.0.0.1', 50000), server)
                header, body = bytes(sock.output).split(b'\r\n\r\n', 1)
                if path.endswith('health'):
                    self.assertEqual(json.loads(body)['public_certificate']['sha256'], capture.public_ca_sha256)
                elif path.endswith('root.pem'):
                    self.assertEqual(body, self.pem)
                    self.assertIn(b'Cache-Control: no-store', header)
                else: self.assertNotIn(b'200 OK', header)

    def test_fetch_pins_per_address_and_verifies_the_leaf_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server_trust, 'read_url', side_effect=self.transport()) as reader:
                target, fingerprint = server_trust.fetch_certificate(Endpoints(), Path(directory))
                self.assertEqual(target.read_bytes(), self.pem)
                self.assertEqual(fingerprint, self.health()['public_certificate']['sha256'])
                self.assertTrue(any(call.args[0].startswith('https:') for call in reader.call_args_list))
                before = target.stat().st_mtime_ns
                server_trust.fetch_certificate(Endpoints(), Path(directory))
                self.assertEqual(target.stat().st_mtime_ns, before)
            self.assertFalse(list(Path(directory).glob('*.key')))

    def test_changed_ca_is_rejected_and_explicit_rotation_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server_trust, 'read_url', side_effect=self.transport()):
                target, _ = server_trust.fetch_certificate(Endpoints(), Path(directory))
            with patch.object(server_trust, 'read_url', side_effect=self.transport(self.another)):
                with self.assertRaisesRegex(ValueError, 'certificate changed'):
                    server_trust.fetch_certificate(Endpoints(), Path(directory))
                self.assertEqual(target.read_bytes(), self.pem)
                server_trust.fetch_certificate(Endpoints(), Path(directory), refresh=True)
                self.assertEqual(target.read_bytes(), self.another)

    def test_failed_tls_verification_does_not_save_or_replace_a_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            reply = self.transport()
            def fail_tls(url, limit, context=None):
                if context is not None: raise ValueError('TLS verification failed')
                return reply(url, limit, context)
            with patch.object(server_trust, 'read_url', side_effect=fail_tls):
                with self.assertRaisesRegex(ValueError, 'TLS verification'):
                    server_trust.fetch_certificate(Endpoints(), Path(directory))
            self.assertFalse(list(Path(directory).glob('*.pem')))

    def test_bad_metadata_certificate_and_redirect_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for reply, message in (({}, 'compatible'), ({'service': 'fifa19-fut', 'version': 1,
                                                       'advertise': '127.0.0.1'}, 'automatic')):
                with patch.object(server_trust, 'read_url', return_value=json.dumps(reply).encode()):
                    with self.assertRaisesRegex(ValueError, message):
                        server_trust.fetch_certificate(Endpoints(), Path(directory))
            with patch.object(server_trust, 'read_url', side_effect=[json.dumps(self.health()).encode(), self.another]):
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    server_trust.fetch_certificate(Endpoints(), Path(directory))
            for pem in (self.pem+b'\n-----BEGIN '+b'PRIVATE KEY-----\nsecret', self.pem+self.pem,
                        b'x'*16385, b'not PEM'):
                with self.assertRaises(ValueError): certificate_details(pem)
            with self.assertRaisesRegex(ValueError, 'redirected'):
                server_trust.NoRedirect().redirect_request(None, None, None, None, None)


class LauncherFrontendTests(unittest.TestCase):
    def values(self):
        values = dict.fromkeys(launcher_gui.TEXT_FIELDS, '')
        values.update(dict.fromkeys(launcher_gui.BOOL_FIELDS, False))
        values.update(server='192.0.2.55', rpcs3='F:/My RPCS3/rpcs3.exe', game='//server/share/FIFA 19')
        return values

    def test_gui_settings_round_trip_and_ignore_unrelated_or_wrongly_typed_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings/launcher.json'
            values = self.values(); values['trace_network'] = True
            launcher_gui.save_settings(path, values)
            self.assertEqual(launcher_gui.load_settings(path), values)
            path.write_text(json.dumps({'server': '192.0.2.1', 'trace_network': 'false', 'token': 'secret'}))
            restored = launcher_gui.load_settings(path)
            self.assertEqual(restored['server'], '192.0.2.1')
            self.assertFalse(restored['trace_network'])
            self.assertNotIn('token', restored)

    def test_gui_launch_and_check_use_auto_certificate_and_keep_paths_as_single_arguments(self):
        values = self.values()
        for action in ('play', 'check'):
            command = launcher_gui.build_command(values, action, Path('/tmp/gui settings'))
            self.assertEqual(command[command.index('--rpcs3')+1], values['rpcs3'])
            self.assertEqual(command[command.index('--game')+1], values['game'])
            self.assertIn('--cert-cache', command)
            self.assertNotIn('--trust-ca', command)
            self.assertEqual('--prepare-only' in command, action == 'check')

    def test_gui_update_settings_and_isolated_profile_commands(self):
        values = self.values()
        values['game'] = ''
        self.assertIn('--open-rpcs3', launcher_gui.build_command(values, 'configure', Path('/tmp')))
        values['update_source'] = 'D:/Own Update/BLES02258'
        command = launcher_gui.build_command(values, 'install', Path('/tmp'))
        self.assertIn('--source', command)
        self.assertNotIn('--server', command)
        values['instance'] = 'player2'
        values['profile'] = '/ignored'
        command = launcher_gui.build_command(values, 'play', Path('/tmp'), refresh=True)
        self.assertIn('--instance', command)
        self.assertNotIn('--profile', command)
        self.assertNotIn('--game', command)
        self.assertIn('--refresh-server-cert', command)
        with self.assertRaises(ValueError): launcher_gui.build_command(values, 'install', Path('/tmp'))

    def test_gui_stop_requests_termination_and_returns_for_patch_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            signal = Path(directory)/'stop'; signal.touch()
            class Game:
                terminated = False
                def terminate(self): self.terminated = True
                def wait(self, timeout=None): return 0
            game = Game()
            with redirect_stdout(io.StringIO()): result = launch.wait_for_game(game, signal)
            self.assertTrue(game.terminated)
            self.assertEqual(result, (0, True))

    def test_normal_exit_does_not_terminate_the_game_and_timeout_is_polled(self):
        from unittest.mock import Mock
        game = Mock()
        game.wait.side_effect = [subprocess.TimeoutExpired('fake', 0.5), 0]
        self.assertEqual(launch.wait_for_game(game), (0, False))
        game.terminate.assert_not_called()


if __name__ == '__main__': unittest.main()
