"""Read the RSA public modulus from a server CA using Python's standard library."""
from pathlib import Path
import ssl
import hashlib


def _tlv(data, offset=0):
    if offset+2 > len(data): raise ValueError('Truncated DER certificate')
    tag, size = data[offset:offset+2]; start = offset+2
    if size & 0x80:
        count = size & 0x7f
        if not 1 <= count <= 4 or start+count > len(data): raise ValueError('Invalid DER length')
        size = int.from_bytes(data[start:start+count], 'big'); start += count
    end = start+size
    if end > len(data): raise ValueError('Truncated DER value')
    return tag, data[start:end], end


def _children(data):
    result = []; offset = 0
    while offset < len(data):
        tag, value, offset = _tlv(data, offset); result.append((tag, value))
    return result


def certificate_details(pem):
    """Validate one public PEM and return its DER fingerprint and native modulus."""
    if isinstance(pem, bytes):
        try: pem = pem.decode('ascii')
        except UnicodeDecodeError as error: raise ValueError('Certificate must be ASCII PEM') from error
    if (not isinstance(pem, str) or len(pem) > 16384
            or not pem.strip().startswith('-----BEGIN CERTIFICATE-----')
            or not pem.strip().endswith('-----END CERTIFICATE-----')
            or pem.count('-----BEGIN') != 1 or pem.count('-----END') != 1):
        raise ValueError('Expected one public CA certificate, not a key or certificate bundle')
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
        tag, certificate, end = _tlv(der)
        if tag != 0x30 or end != len(der): raise ValueError('Invalid certificate sequence')
        outer = _children(certificate)
        if outer[0][0] != 0x30: raise ValueError('Invalid certificate body')
        tbs = _children(outer[0][1])
        if tbs[0][0] == 0xa0: tbs = tbs[1:]
        subject = {}
        for tag, rdn in _children(tbs[4][1]):
            if tag != 0x31: raise ValueError('Invalid certificate subject')
            for at, attribute in _children(rdn):
                if at != 0x30: raise ValueError('Invalid subject attribute')
                fields = _children(attribute)
                if len(fields) != 2 or fields[0][0] != 6: raise ValueError('Invalid subject name')
                subject[fields[0][1]] = fields[1][1]
        expected = {bytes.fromhex('550406'): b'US', bytes.fromhex('55040a'): b'GeoTrust, Inc.',
                    bytes.fromhex('550403'): b'GeoTrust SSL CA'}
        if any(subject.get(key) != value for key,value in expected.items()):
            raise ValueError('Use the public CA created by initialize_tls.py; the native issuer labels must match')
        if tbs[5][0] != 0x30: raise ValueError('Missing subject public key')
        spki = _children(tbs[5][1]); algorithm = _children(spki[0][1])
        if algorithm[0] != (6, bytes.fromhex('2a864886f70d010101')) or spki[1][0] != 3 or spki[1][1][0] != 0:
            raise ValueError('CA must use RSA')
        key_tag, key, key_end = _tlv(spki[1][1][1:])
        if key_tag != 0x30 or key_end != len(spki[1][1])-1: raise ValueError('Invalid RSA key')
        numbers = _children(key)
        if len(numbers) != 2 or any(tag != 2 or not value or value[0]&0x80 for tag,value in numbers):
            raise ValueError('Invalid RSA integers')
        modulus = int.from_bytes(numbers[0][1], 'big'); exponent = int.from_bytes(numbers[1][1], 'big')
        if modulus.bit_length() != 2048 or exponent != 65537:
            raise ValueError('CA must have a 2048-bit RSA key and exponent 65537')
        return {'sha256': hashlib.sha256(der).hexdigest(), 'modulus': modulus.to_bytes(256, 'big')}
    except (IndexError, TypeError, ssl.SSLError) as error:
        raise ValueError('Unsupported server CA certificate') from error


def public_modulus(path):
    """Only the retail patch's 2048-bit RSA / 65537 exponent is supported."""
    return certificate_details(Path(path).read_bytes())['modulus']
