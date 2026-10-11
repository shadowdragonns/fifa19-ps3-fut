"""Socket-like TLS bridge for FIFA's captured RSA/TLS 1.2 ClientHello."""
from pathlib import Path
import ssl

HERE = Path(__file__).resolve().parent
CIPHERS = ('AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256:'
           'AES128-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=0')


def server_context(cert=None, key=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    # The actual PS3 hello offers static RSA suites, not OpenSSL's usual ECDHE
    # defaults, and has no signature-algorithms extension. Scope compatibility
    # to this localhost listener rather than changing system TLS settings.
    context.set_ciphers(CIPHERS)
    context.load_cert_chain(str(cert or HERE / 'tls/local.pem'), str(key or HERE / 'tls/local.key'))
    return context


class TLSStream:
    """Feed an already-read hello to OpenSSL without losing TCP fragments."""
    def __init__(self, socket, initial, context, capture):
        self.socket = socket
        self.capture = capture
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.session = context.wrap_bio(self.incoming, self.outgoing, server_side=True)
        self.incoming.write(initial)
        while True:
            try:
                self.session.do_handshake()
                self.flush()
                break
            except ssl.SSLWantReadError:
                self.flush()
                self.feed()
            except ssl.SSLError:
                self.flush()
                raise

    def flush(self):
        while self.outgoing.pending:
            raw = self.outgoing.read()
            self.socket.sendall(raw)
            self.capture.save('tls-server-record', raw)

    def feed(self):
        raw = self.socket.recv(65536)
        if not raw:
            raise EOFError('Peer closed during TLS exchange')
        self.capture.save('tls-client-record', raw)
        self.incoming.write(raw)

    def recv(self, size):
        while True:
            try:
                return self.session.read(size)
            except ssl.SSLWantReadError:
                self.flush()
                try:
                    self.feed()
                except EOFError:
                    return b''
            except ssl.SSLZeroReturnError:
                return b''

    def sendall(self, data):
        pending = memoryview(data)
        while pending:
            try:
                count = self.session.write(pending)
                pending = pending[count:]
                self.flush()
            except ssl.SSLWantReadError:
                self.flush()
                self.feed()

    def settimeout(self, timeout):
        self.socket.settimeout(timeout)
