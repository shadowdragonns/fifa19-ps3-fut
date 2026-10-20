#!/usr/bin/env python3
"""Standalone FIFA 19 PS3 FUT server: menus, packs and CPU seasons."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import socketserver
import subprocess
import threading

from endpoints import Endpoints
from probe import Capture, Handler, Server, QosUDPHandler
from sessions import Backend
from tls_bridge import server_context

HERE = Path(__file__).resolve().parent
PORTS = (8990, 8099, 42230, 44325)


def tls_files(host, directory):
    """Issue an IPv4 leaf from the same CA already trusted by the client patch."""
    if host == '127.0.0.1':
        return HERE/'tls/local.pem', HERE/'tls/local.key'
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory/'server.pem', directory/'server.key'
    marker = directory/'address.txt'
    if cert.is_file() and key.is_file() and marker.is_file() and marker.read_text() == host:
        return cert, key
    extension = directory/'leaf.ext'
    extension.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
        'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:winter15.gosredirector.ea.com,DNS:gosca.ea.com,'
        f'DNS:localhost,IP:127.0.0.1,IP:{host}\n')
    csr = directory/'server.csr'
    subprocess.run(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256',
        '-subj', '/CN=winter15.gosredirector.ea.com', '-keyout', str(key), '-out', str(csr)],
        check=True, capture_output=True)
    key.chmod(0o600)
    subprocess.run(['openssl', 'x509', '-req', '-in', str(csr), '-CA', str(HERE/'tls/root.pem'),
        '-CAkey', str(HERE/'tls/root.key'), '-set_serial', '0x'+__import__('secrets').token_hex(16),
        '-days', '3650', '-sha256', '-extfile', str(extension), '-out', str(cert)],
        check=True, capture_output=True)
    marker.write_text(host)
    return cert, key


class StateLock:
    """Refuse a second writer of the same profile/inventory, including launcher."""
    def __init__(self, directory):
        self.directory = directory
        self.path = directory/'server.lock'

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        import os
        try:
            self.handle = self.path.open('x')
        except FileExistsError:
            raise RuntimeError(f'State already locked: {self.path}. Stop the other server; after a forced crash remove this lock manually.') from None
        self.handle.write(str(os.getpid())+'\n'); self.handle.flush()
        return self

    def __exit__(self, *_):
        self.handle.close()
        self.path.unlink()


@contextmanager
def listeners(bind, capture, context):
    servers = []
    started = []
    try:
        for port in PORTS:
            server = Server((bind, port), Handler)
            server.capture, server.blaze_port, server.tls_context = capture, 8990, context
            servers.append(server)
        udp = socketserver.UDPServer((bind, 8099), QosUDPHandler)
        udp.capture, udp.capture_count, udp.allow_remote = capture, 0, bind != '127.0.0.1'
        servers.append(udp)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            started.append(server)
        yield servers
    finally:
        for server in servers:
            if server in started:
                server.shutdown()
            server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1', help='Listen address; use 0.0.0.0 for another PC')
    parser.add_argument('--advertise', default='127.0.0.1', help='IPv4 clients can reach, not 0.0.0.0')
    parser.add_argument('--state-dir', type=Path, default=HERE/'state')
    parser.add_argument('--logs', type=Path)
    parser.add_argument('--locale-archive', type=Path)
    parser.add_argument('--config', type=Path, help='JSON server rules; automatically reloaded after valid edits')
    parser.add_argument('--check-config', action='store_true', help='Validate only the config; safe while the server is running')
    parser.add_argument('--plugins-dir', type=Path, help='Trusted Python plugin directory')
    parser.add_argument('--plugin-data-dir', type=Path, help='Persistent private plugin data')
    parser.add_argument('--check', action='store_true', help='Validate configuration, saves and TLS without listening')
    args = parser.parse_args(argv)
    import signal
    def stopped(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stopped)
    import ipaddress
    bind = str(ipaddress.IPv4Address(args.bind))
    endpoints = Endpoints(args.advertise)
    from server_settings import Settings, ConfigWatcher
    from server_plugins import PluginManager
    config = args.config or HERE/'server-config.json'
    if args.check_config:
        Settings.load(config)
        print(json.dumps({'valid': True, 'scope': 'configuration'}))
        return
    settings = Settings.load(config) if args.config or config.exists() else Settings()
    backend = Backend(args.state_dir/'profile.json', endpoints, multiuser=True, settings=settings)
    plugins = PluginManager(args.plugins_dir or config.parent/'plugins',
                            args.plugin_data_dir or config.parent/'plugin-data', settings)
    backend.events = plugins
    backend.fut_inventory.events = plugins
    plugins.bind_backend(backend)
    if args.locale_archive:
        from localization import Localizer
        backend.localizer = Localizer(args.locale_archive)
    logs = args.logs or HERE/'logs'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    with StateLock(args.state_dir):
        cert, key = tls_files(endpoints.host, args.state_dir/'tls')
        context = server_context(cert, key)
        from tls_identity import certificate_details
        public_ca = (HERE/'tls/root.pem').read_bytes()
        public_ca_sha256 = certificate_details(public_ca)['sha256']
        if args.check:
            for key, row in backend.accounts['users'].items():
                if not row['legacy']:
                    owner = Backend(args.state_dir/'users'/key/'profile.json', endpoints, settings=settings)
                    if (owner.profile['persona'] != backend.profile['persona']+row['number']
                            or owner.profile['account'] != backend.profile['account']+row['number']):
                        raise ValueError('Saved player identity differs from registry')
            plugins.load(check=True)
            print(json.dumps({'bind': bind, 'advertise': endpoints.host, 'tcp_ports': PORTS,
                'udp_ports': [8099], 'state_dir': str(args.state_dir), 'valid': True}))
            return
        capture = Capture(logs, backend)
        from web_app import WebApp
        capture.web = WebApp(backend)
        capture.public_ca = public_ca
        capture.public_ca_sha256 = public_ca_sha256
        plugins.logger = lambda record: capture.save(record['event'], **{k:v for k,v in record.items() if k != 'event'})
        from card_rebalance import reconcile
        rebalanced = reconcile(backend, settings)
        if rebalanced['cards']:
            capture.save('custom_cards.updated', reason='startup', **rebalanced)
        plugins.load()
        watcher = ConfigWatcher(backend, config, plugins.logger)
        capture.config_watcher = watcher
        capture.plugin_manager = plugins
        with listeners(bind, capture, context):
            capture.save('ready', bind=bind, advertise=endpoints.host, ports=list(PORTS), udp_ports=[8099],
                status='Per-user FUT clubs, CPU seasons and paired online matches', plugins=plugins.loaded)
            plugins.emit('server.started', advertise=endpoints.host, plugins=plugins.loaded)
            try:
                watcher.start()
                plugins.start()
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            finally:
                watcher.stop()
                plugins.stop()
                plugins.emit('server.stopping')
                backend.close_sessions()
        plugins.emit('server.stopped')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'FUT server: {error}')
