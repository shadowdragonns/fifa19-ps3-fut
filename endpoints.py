"""IPv4 endpoints shared by the server, launcher and native QoS replies."""
from dataclasses import dataclass
import ipaddress
import socket


def ipv4(value, resolve=False):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        if not resolve:
            raise ValueError('Supply an IPv4 address') from None
        try:
            address = ipaddress.IPv4Address(socket.gethostbyname(value))
        except (OSError, ipaddress.AddressValueError):
            raise ValueError('Server address does not resolve to IPv4') from None
    if address.is_unspecified or address.is_multicast or str(address) == '255.255.255.255':
        raise ValueError('Supply a reachable unicast server IPv4 address')
    return str(address)


@dataclass(frozen=True)
class Endpoints:
    host: str = '127.0.0.1'
    blaze_port: int = 8990
    http_port: int = 8099

    def __post_init__(self):
        object.__setattr__(self, 'host', ipv4(self.host))
        if any(type(p) is not int or not 1 <= p <= 65535 for p in (self.blaze_port, self.http_port)):
            raise ValueError('Invalid service port')

    @property
    def base_url(self):
        return f'http://{self.host}:{self.http_port}'

    @property
    def address_number(self):
        return int(ipaddress.IPv4Address(self.host))

    @property
    def callback(self):
        return self.base_url+'/local/auth-callback'

    def settings(self):
        return ('; FIFA 19 PS3 community FUT server\nFUT_ENABLE_MENU = 1\n'
                f'ONLINE/BLAZEIP = {self.host}\nONLINE/BLAZEPORT = {self.blaze_port}\n'
                f'FUT_TARGET_HOSTNAME = {self.host}\nFUT_TARGET_PORT = {self.http_port}\n'
                f'FUT_RS4_BASE_URL = {self.base_url}/\n'
                f'FUTDYNAMICMESSAGES_URL_BASE = {self.base_url}\n'
                f'FIFA_POW_NUCLEUS_PROXY_URL = {self.base_url}/\nONLINE/BLAZE_LOGGING = 1\n').encode()
