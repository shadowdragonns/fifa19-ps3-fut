"""Local QoS metadata and UDP replies recovered from the PS3 client parser."""
import ipaddress
import struct
from urllib.parse import urlsplit, parse_qs


def response(path, port=8099, host='127.0.0.1'):
    address = int(ipaddress.IPv4Address(host))
    parsed = urlsplit(path)
    query = parse_qs(parsed.query)
    if parsed.path == '/qos/qos' and query.get('qtyp', [''])[0] in ('1', '2'):
        # This PS3 parser requires >=2 bandwidth probes and nonzero size.
        # Its sockaddr builder emits this integer most-significant byte first.
        return (f'<?xml version="1.0" encoding="UTF-8"?><qos>'
                f'<numprobes>2</numprobes><qosport>{port}</qosport>'
                f'<probesize>32</probesize><qosip>{address}</qosip>'
                '<requestid>1</requestid><reqsecret>0</reqsecret></qos>').encode()
    if parsed.path == '/qos/firewall':
        return (f'<?xml version="1.0" encoding="UTF-8"?><firewall>'
                f'<ips><ips>{address}</ips><ips>{address}</ips></ips>'
                '<numinterfaces>2</numinterfaces>'
                f'<ports><ports>{port}</ports><ports>{port}</ports></ports>'
                '<requestid>1</requestid><reqsecret>0</reqsecret></firewall>').encode()
    if parsed.path == '/qos/firetype':
        return b'<?xml version="1.0" encoding="UTF-8"?><firetype><firetype>2</firetype></firetype>'
    return None


def datagram(data, peer, allow_remote=False):
    if not allow_remote and peer[0] != '127.0.0.1':
        return None
    if len(data) == 20:
        client_id, kind, _, _, _ = struct.unpack('>5I', data)
        if not client_id or kind != 1:
            return None
        # Latency receiver requires >=30 bytes, echoed timestamp at offset 16,
        # and the client's externally visible IPv4/port at offsets 20/24.
        address = struct.pack('>IH', int(ipaddress.IPv4Address(peer[0])), peer[1])
        return data + address + b'\0' * 4
    if len(data) == 32:
        client_id, request_id, secret, index, count = struct.unpack_from('>5I', data)
        if not client_id or request_id != 1 or secret != 0 or count != 2 or index >= count:
            return None
        result = bytearray(data)
        # Bandwidth receiver uses a 1-based response count and exact packet size.
        struct.pack_into('>I', result, 12, index + 1)
        return bytes(result)
    # Firewall's eight-byte probes are observed by the service; final status
    # comes through /qos/firetype. They do not need a UDP reply.
    return None
