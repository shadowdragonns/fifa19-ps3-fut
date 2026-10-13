#!/usr/bin/env python3
"""Shared FIFA 19 FUT protocol handlers and diagnostic request captures."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socketserver
import struct
import threading
import time
import uuid
from urllib.parse import urlsplit

from tls_bridge import server_context, TLSStream
import blaze
import tdf
import qos
import hospitality
import fut_services
import fut_inventory
import fut_squads
import fut_matches
import fut_seasons
import fut_online
import fut_content
import fut_leaderboards
import pow_services
import pran
from sessions import Backend, Connection

HERE = Path(__file__).resolve().parent
MAX_BYTES = 4 * 1024 * 1024


def read_fire2(buffer):
    """Consume one Fire2 frame, retaining incomplete TCP fragments."""
    if len(buffer) < 16:
        return None
    payload_size, metadata_size, component, command = struct.unpack_from('>IHHH', buffer)
    total = 16 + metadata_size + payload_size
    if total > MAX_BYTES:
        raise ValueError(f'Unsupported framing or oversized request: {total} bytes')
    if len(buffer) < total:
        return None
    raw = bytes(buffer[:total])
    del buffer[:total]
    return dict(component=component, command=command,
                message=int.from_bytes(raw[10:13], 'big'), kind=raw[13] >> 5,
                metadata_size=metadata_size, payload_size=payload_size, raw=raw)


def ping_reply(frame):
    header = bytearray(frame['raw'][:16])
    header[:6] = b'\0' * 6
    header[13] = 5 << 5
    return bytes(header)


class Capture:
    def __init__(self, directory, backend=None):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.backend = backend or Backend()
        self.identity = self.backend.identity
        self.public_ca = None
        self.public_ca_sha256 = None
        self.config_watcher = None
        self.plugin_manager = None

    def save(self, event, raw=b'', **details):
        details.update(event=event, time=datetime.now(timezone.utc).isoformat())
        if raw:
            name = f'{event}-{uuid.uuid4().hex}.bin'
            (self.directory / name).write_bytes(raw)
            details['capture'] = name
            details['bytes'] = len(raw)
        with self.lock:
            with (self.directory / 'events.jsonl').open('a') as out:
                out.write(json.dumps(details) + '\n')
            print(json.dumps(details), flush=True)

    def bind(self, listener):
        return CaptureConnection(self, listener)


class CaptureConnection:
    """Correlate one socket's events and sample accepted noisy telemetry."""
    def __init__(self, capture, listener):
        self.capture = capture
        self.connection_id = uuid.uuid4().hex
        self.listener = listener
        self.telemetry_count = self.telemetry_samples = self.telemetry_bytes = 0
        self.last_sample = time.monotonic()
        self.last_summary_count = 0

    def __getattr__(self, name):
        return getattr(self.capture, name)

    def save(self, event, raw=b'', **details):
        details.update(connection_id=self.connection_id)
        details.setdefault('listener', self.listener)
        self.capture.save(event, raw, **details)

    def sample_telemetry(self, raw_bytes):
        self.telemetry_count += 1
        self.telemetry_bytes += raw_bytes
        now = time.monotonic()
        if self.telemetry_count <= 2 or now-self.last_sample >= 5:
            self.telemetry_samples += 1
            self.last_sample = now
            if self.telemetry_count > 2:
                self.flush_summary()
            return True
        return False

    def flush_summary(self):
        if self.telemetry_count != self.last_summary_count:
            self.save('telemetry-summary', component=4, command=171,
                accepted_requests=self.telemetry_count, captured_requests=self.telemetry_samples,
                suppressed_requests=self.telemetry_count-self.telemetry_samples,
                request_bytes=self.telemetry_bytes, sample_interval_seconds=5)
            self.last_summary_count = self.telemetry_count


def send_notifications(sock, capture, notifications):
    for notification in notifications:
        if len(notification) == 3:
            command, body, name = notification
            component, prefix = 30722, 'UserSessions'
        else:
            component, command, body, name = notification
            prefix = 'GameManager' if component == 4 else str(component)
        raw = blaze.reply(dict(component=component, command=command, message=0), body, kind=2)
        sock.sendall(raw)
        capture.save('fire2-notification', raw, component=component, command=command, handler=prefix+'.'+name)


def http_request(sock, initial):
    data = bytearray(initial)
    while b'\r\n\r\n' not in data:
        if len(data) > 65536:
            raise ValueError('HTTP headers too large')
        part = sock.recv(65536)
        if not part:
            raise ValueError('Incomplete HTTP request')
        data.extend(part)
    head, body = bytes(data).split(b'\r\n\r\n', 1)
    lines = head.decode('iso-8859-1').split('\r\n')
    method, path, _ = lines[0].split(' ', 2)
    headers = {}
    for line in lines[1:]:
        key, value = line.split(':', 1)
        headers[key.lower()] = value.strip()
    if 'transfer-encoding' in headers:
        raise ValueError('Transfer-encoded HTTP requests need a decoder')
    length = int(headers.get('content-length', '0'))
    if not 0 <= length <= MAX_BYTES:
        raise ValueError('HTTP body too large')
    while len(body) < length:
        part = sock.recv(min(65536, length - len(body)))
        if not part:
            raise ValueError('Incomplete HTTP body')
        body += part
    return method, path, head + b'\r\n\r\n' + body[:length]


def redirect_response(port, host='127.0.0.1'):
    from endpoints import Endpoints
    endpoint = Endpoints(host, blaze_port=port)
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<serverinstanceinfo><address member="0"><valu>'
            f'<hostname>{endpoint.host}</hostname><ip>{endpoint.address_number}</ip><port>{port}</port>'
            f'</valu></address><secure>0</secure></serverinstanceinfo>').encode()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(30)
        capture = self.server.capture.bind(self.server.server_address[1])
        endpoints = capture.backend.endpoints
        capture.save('connect', listener=self.server.server_address[1])
        buffer = bytearray()
        connection = Connection(capture.backend, self.client_address[0])
        try:
            # Do not assume the first recv contains the protocol prefix.
            while len(buffer) < 5:
                part = sock.recv(65536)
                if not part:
                    return
                buffer.extend(part)
            if buffer[0] == 0x16 and buffer[1] == 3:
                record_size = 5 + int.from_bytes(buffer[3:5], 'big')
                if record_size > 18437:
                    raise ValueError('TLS record too large')
                while len(buffer) < record_size:
                    part = sock.recv(record_size - len(buffer))
                    if not part:
                        raise ValueError('Incomplete TLS record')
                    buffer.extend(part)
                capture.save('tls-client-hello', bytes(buffer),
                             listener=self.server.server_address[1],
                             record_version=f'{buffer[1]}.{buffer[2]}',
                             note='PS3 TLS request; certificate acceptance is recorded by handshake completion.')
                context = getattr(self.server, 'tls_context', None)
                if context is None:
                    return
                try:
                    sock = TLSStream(sock, bytes(buffer), context, capture)
                except (OSError, EOFError) as error:
                    capture.save('tls-handshake-failed', listener=self.server.server_address[1], error=str(error))
                    return
                capture.save('tls-handshake-complete', listener=self.server.server_address[1], version=sock.session.version(), cipher=sock.session.cipher())
                buffer = bytearray()
                while len(buffer) < 5:
                    part = sock.recv(65536)
                    if not part:
                        return
                    buffer.extend(part)
            if buffer.startswith((b'GET ', b'POST ', b'PUT ', b'DELET', b'HEAD ', b'OPTIO')):
                method, path, raw = http_request(sock, buffer)
                # Keep query values (including authorization codes) out of the
                # console/event summary. Original wire bytes remain in captures.
                public_path = hospitality.public_path(path)
                capture.save('http-request', raw, listener=self.server.server_address[1], method=method, path=public_path)
                extra_headers = []
                if method == 'GET' and path == '/fut-local/health':
                    info = {'service': 'fifa19-fut', 'version': 1,
                        'title': 'BLES02258', 'game_version': '01.02',
                        'advertise': endpoints.host, 'features': ['login', 'packs', 'squads', 'cpu-seasons', 'per-user-clubs', 'club-equipment',
                            'online-peer-play', 'leaderboards', 'server-config', 'python-plugins']}
                    if capture.public_ca is not None:
                        info['features'].append('public-certificate')
                        info['public_certificate'] = {'path': '/fut-local/root.pem',
                                                       'sha256': capture.public_ca_sha256}
                    if capture.config_watcher is not None:
                        info['features'].append('live-config')
                        info['config'] = capture.config_watcher.status()
                    if capture.plugin_manager is not None:
                        info['features'].append('live-plugins')
                        info['plugins'] = capture.plugin_manager.status()
                    body = json.dumps(info).encode()
                    status, mime = '200 OK', 'application/json'
                elif method == 'GET' and path == '/fut-local/root.pem':
                    if capture.public_ca is None:
                        body, status, mime = b'{"error":"Public certificate unavailable"}', '404 Not Found', 'application/json'
                    else:
                        body, status, mime = capture.public_ca, '200 OK', 'application/x-pem-file'
                        extra_headers = [('Cache-Control', 'no-store'),
                                         ('Content-Disposition', 'attachment; filename="root.pem"')]
                elif path.split('?', 1)[0].endswith('/redirector/getServerInstance'):
                    body, status, mime = redirect_response(self.server.blaze_port, endpoints.host), '200 OK', 'application/xml'
                elif method == 'GET' and (qos_body := qos.response(path, endpoints.http_port, endpoints.host)) is not None:
                    body, status, mime = qos_body, '200 OK', 'application/xml'
                elif (identity_reply := capture.identity.response(method, path)) is not None:
                    status, body, extra_headers = identity_reply
                    mime = 'application/json'
                elif (web_reply := hospitality.response(capture.backend, method, path, raw, self.client_address[0])) is not None:
                    status, body, mime, extra_headers, handler = web_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (content_reply := fut_content.response(method, path, capture.backend.settings)) is not None:
                    status, body, mime, extra_headers, handler = content_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (leaderboard_reply := fut_leaderboards.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = leaderboard_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (inventory_reply := fut_inventory.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = inventory_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (squad_reply := fut_squads.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = squad_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (match_reply := fut_matches.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = match_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (online_reply := fut_online.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = online_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (season_reply := fut_seasons.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = season_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (fut_reply := fut_services.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = fut_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (pow_reply := pow_services.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = pow_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                elif (pran_reply := pran.response(capture.backend, method, path, raw)) is not None:
                    status, body, mime, extra_headers, handler = pran_reply
                    capture.save('http-handler', path=public_path, handler=handler)
                else:
                    body, status, mime = b'{"error":"FUT protocol research: route not implemented"}', '501 Not Implemented', 'application/json'
                headers = ''.join(f'{key}: {value}\r\n' for key, value in extra_headers)
                reply = (f'HTTP/1.1 {status}\r\n{headers}Content-Type: {mime}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
                sock.sendall(reply)
                capture.save('http-response', reply, path=public_path, status=status)
                return
            # Poll the search timer on this connection, so idle searches get
            # their native notification without needing another client packet.
            sock.settimeout(1)
            idle_deadline = time.monotonic()+30
            while True:
                send_notifications(sock, capture, connection.poll_notifications())
                frame = read_fire2(buffer)
                if frame is not None:
                    raw = frame.pop('raw')
                    telemetry = (frame['component'], frame['command'], frame['kind']) == (4, 171, 0)
                    log_frame = True
                    if not telemetry:
                        capture.save('fire2-request', raw, **frame)
                    if frame['kind'] == 4:
                        frame['raw'] = raw
                        reply = ping_reply(frame)
                        sock.sendall(reply)
                        capture.save('fire2-ping-response', reply)
                    elif frame['kind'] == 0:
                        payload = raw[16 + frame['metadata_size']:]
                        fields = {}
                        logged_invalid = False
                        try:
                            fields = tdf.decode(payload)
                            # Only the observed, ticket-free PreAuth request is
                            # printed with values. Other requests expose tags
                            # for diagnosis without printing account tokens.
                            details = {'fields': fields} if (frame['component'], frame['command']) == (9, 7) else {'tags': list(fields)}
                            if not telemetry:
                                capture.save('tdf-request', component=frame['component'], command=frame['command'], **details)
                        except ValueError as error:
                            if telemetry:
                                capture.save('fire2-request', raw, **frame)
                                logged_invalid = True
                            capture.save('tdf-decode-failed', error=str(error))
                        try:
                            result = connection.response(frame, payload)
                        except (ValueError, OSError):
                            if telemetry and not logged_invalid:
                                capture.save('fire2-request', raw, **frame)
                            raise
                        if result is None:
                            bootstrap = blaze.response(frame, payload, endpoints.http_port, endpoints.host)
                            result = (*bootstrap, []) if bootstrap is not None else None
                        if telemetry:
                            # Only validated, handled telemetry is sampled.
                            # Unsupported/invalid requests always retain raw evidence.
                            if result is not None and result[1] == 'GameManager.ReportTelemetry':
                                log_frame = capture.sample_telemetry(len(raw))
                            if log_frame:
                                capture.save('fire2-request', raw, **frame)
                                capture.save('tdf-request', component=4, command=171, tags=list(fields))
                        if result is not None:
                            body, handler, notifications = result
                            reply = blaze.reply(frame, body)
                            sock.sendall(reply)
                            connection.response_sent(frame)
                            if log_frame:
                                capture.save('fire2-response', reply, component=frame['component'], command=frame['command'],
                                             message=frame['message'], handler=handler,
                                             note='Local PS3 login/session services; local FUT pack backend awaits desktop validation.')
                            send_notifications(sock, capture, notifications)
                        else:
                            capture.save('missing-handler', component=frame['component'], command=frame['command'],
                                         note='Request captured; this service is not implemented.')
                    continue
                try:
                    part = sock.recv(65536)
                except TimeoutError:
                    if time.monotonic() >= idle_deadline:
                        return
                    continue
                if not part:
                    if buffer:
                        capture.save('incomplete-request', bytes(buffer))
                    return
                buffer.extend(part)
                idle_deadline = time.monotonic()+30
        except (ValueError, OSError, EOFError) as error:
            capture.save('protocol-error', bytes(buffer), error=str(error))
        finally:
            capture.flush_summary()
            connection.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class QosUDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        # Bound packet logging for retransmits. The service is loopback only.
        if self.server.capture_count < 200:
            self.server.capture.save('qos-udp-request', data[:128], packet_bytes=len(data))
            self.server.capture_count += 1
        reply = qos.datagram(data, self.client_address, getattr(self.server, 'allow_remote', False))
        if reply is not None:
            sock.sendto(reply, self.client_address)
            if self.server.capture_count < 200:
                self.server.capture.save('qos-udp-response', reply)


def main():
    # Preserve the original launcher/probe entry point; both use the same
    # standalone server, state lock, listeners and shutdown cleanup.
    from server import main as serve
    serve()


if __name__ == '__main__':
    main()
