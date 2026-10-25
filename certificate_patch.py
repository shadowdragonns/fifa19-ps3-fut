"""Prepare and temporarily enable a hash-specific local certificate key patch."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import client_platform
import profile_trace
import upgrade_rewards_patch
import tournament_patch

HERE = Path(__file__).resolve().parent
ELF = HERE.parent / 'england-110/update-research/EBOOT.elf'
ELF_SHA256 = '2f20b4f507e93283fefd3f14ac85462191ca9a66608f1d649981173f63f73be9'
PPU_HASH = 'PPU-b66f913dac5aa6ae5f00cad90aaf5d992e10ea26'
DESCRIPTION = 'Local FUT certificate key'
CA_ADDRESS = 0x024b79ec
KEY_ADDRESS = 0x01f1e81c
KEY_SIZE = 256
ORIGINAL_KEY_SHA256 = '4a9f06ce32bfa4b6410d0eba50ed0b5558a2675864ffd11a64a306415f71294d'


def prepare(include_profile_trace=False, skip_upgrade_rewards=False, trust_ca=None):
    """Validate the CA structure and optional hash-specific profile tracepoints."""
    if trust_ca is not None or sys.platform == 'win32' or not ELF.is_file():
        return prepare_portable(include_profile_trace, skip_upgrade_rewards, trust_ca)
    data = ELF.read_bytes()
    if hashlib.sha256(data).hexdigest() != ELF_SHA256:
        raise ValueError('The researched executable changed; certificate patch must be reanalysed.')
    ca = CA_ADDRESS - 0x10000
    expected = {0: b'US', 0x20: b'', 0x40: b'', 0x60: b'GeoTrust, Inc.',
                0x80: b'', 0x180: b'GeoTrust SSL CA'}
    for offset, value in expected.items():
        if data[ca + offset:].split(b'\0', 1)[0] != value:
            raise ValueError('Certificate authority subject layout does not match research.')
    if struct.unpack_from('>III', data, ca + 0x1c4) != (KEY_SIZE, KEY_ADDRESS, 3):
        raise ValueError('Certificate authority key layout does not match research.')
    if data[ca + 0x1d0:ca + 0x1d3] != b'\x01\x00\x01':
        raise ValueError('Unsupported certificate authority RSA exponent.')
    old = data[KEY_ADDRESS - 0x10000:KEY_ADDRESS - 0x10000 + KEY_SIZE]
    if hashlib.sha256(old).hexdigest() != ORIGINAL_KEY_SHA256:
        raise ValueError('Certificate authority modulus does not match research.')
    output = subprocess.run(['openssl', 'x509', '-in', str(HERE / 'tls/root.pem'),
                             '-noout', '-modulus'], check=True, capture_output=True, text=True).stdout.strip()
    if not output.startswith('Modulus='):
        raise ValueError('Could not read local certificate authority public key.')
    key = bytes.fromhex(output.split('=', 1)[1])
    if len(key) != KEY_SIZE:
        raise ValueError('Local certificate authority must have a 2048-bit RSA key.')
    patch = (f'Version: 1.2\n{PPU_HASH}:\n  "{DESCRIPTION}":\n'
             '    Games:\n      "FIFA 19":\n        BLES02258: ["01.02"]\n'
             '    Author: Local FUT research\n    Patch Version: 1.0\n'
             '    Notes: Local CA key; optional temporary profile diagnostics and upgrade-rewards skip.\n'
             '    Patch:\n')
    for offset in range(0, KEY_SIZE, 4):
        value = int.from_bytes(key[offset:offset + 4], 'big')
        patch += f'      - [ be32, 0x{KEY_ADDRESS + offset:08x}, 0x{value:08x} ]\n'
    patch += tournament_patch.patch_rows(data)
    if include_profile_trace:
        patch += profile_trace.patch_rows(data)
    if skip_upgrade_rewards:
        patch += upgrade_rewards_patch.patch_rows(data)
    target = HERE / 'BLES02258_patch.yml'
    target.write_text(patch)
    (HERE / 'research/certificate-key-patch.json').write_text(json.dumps({
        'executable_sha256': ELF_SHA256, 'ppu_hash': PPU_HASH,
        'ca_address': hex(CA_ADDRESS), 'key_address': hex(KEY_ADDRESS),
        'key_size': KEY_SIZE, 'original_key_sha256': ORIGINAL_KEY_SHA256,
        'replacement_key_sha256': hashlib.sha256(key).hexdigest(),
        'changes': '64 big-endian words in one static CA RSA modulus',
        'profile_tracepoints': [{'address': hex(address), 'marker': marker}
                                for address, _, marker, _, _ in profile_trace.SITES] if include_profile_trace else [],
        'profile_trace_behavior': 'RPCS3 calloc trampolines emit integer callback fields through TTY, restore CPU state and execute the replaced instruction; no login or completion result is changed',
        'upgrade_rewards_skip': {'enabled': skip_upgrade_rewards, 'address': hex(upgrade_rewards_patch.ADDRESS),
                                 'behavior': 'Only rewards ONLINE_ACCESS read returns false; native downloadFailed event continues boot flow. No global login change or rewards granted.'},
        'game_validation': 'See investigation.json for desktop evidence; this report validates generated patch bytes.',
    }, indent=2) + '\n')
    return target


def prepare_portable(include_profile_trace, skip_upgrade_rewards, trust_ca=None):
    """Use rows checked against the retail ELF when building the client bundle."""
    metadata = json.loads((HERE/'data/client-native-patch.json').read_text())
    if metadata['elf_sha256'] != ELF_SHA256 or metadata['ppu_hash'] != PPU_HASH:
        raise ValueError('Portable patch metadata does not match this client')
    from tls_identity import public_modulus
    ca = Path(trust_ca) if trust_ca is not None else HERE/'tls/root.pem'
    if not ca.is_file():
        raise ValueError('Supply --trust-ca with the server public root.pem certificate; no private keys are needed by clients')
    key = public_modulus(ca)
    if len(key) != KEY_SIZE:
        raise ValueError('Unsupported portable certificate modulus')
    patch = (f'Version: 1.2\n{PPU_HASH}:\n  "{DESCRIPTION}":\n'
        '    Games:\n      "FIFA 19":\n        BLES02258: ["01.02"]\n'
        '    Author: Local FUT research\n    Patch Version: 1.0\n'
        '    Notes: Local CA key; optional temporary profile diagnostics and upgrade-rewards skip.\n'
        '    Patch:\n')
    for offset in range(0, KEY_SIZE, 4):
        value = int.from_bytes(key[offset:offset+4], 'big')
        patch += f'      - [ be32, 0x{KEY_ADDRESS+offset:08x}, 0x{value:08x} ]\n'
    patch += tournament_patch.patch_rows()
    if include_profile_trace:
        patch += metadata['trace_rows']
    if skip_upgrade_rewards:
        patch += metadata['rewards_rows']
    target = HERE/'BLES02258_patch.yml'
    target.write_text(patch)
    return target


def _launcher_patch(content, desired):
    """Recognise our exact schema/rows, allowing a different server public key."""
    try:
        text = content.decode('utf-8').replace('\r\n', '\n')
        header, separator, body = text.partition('    Patch:\n')
        wanted_header = desired.decode('utf-8').replace('\r\n', '\n').partition('    Patch:\n')[0]
        if not separator or header != wanted_header: return False
        lines = body.splitlines(keepends=True)
        if len(lines) < KEY_SIZE//4: return False
        for offset in range(0, KEY_SIZE, 4):
            pattern = rf'      - \[ be32, 0x{KEY_ADDRESS+offset:08x}, 0x[0-9a-fA-F]{{8}} \]\n'
            if not re.fullmatch(pattern, lines[offset//4]): return False
        metadata = json.loads((HERE/'data/client-native-patch.json').read_text())
        tail = ''.join(lines[KEY_SIZE//4:])
        optional = ('', metadata['trace_rows'], metadata['rewards_rows'],
                    metadata['trace_rows']+metadata['rewards_rows'])
        # Recognise older launcher patches as well as the current mandatory
        # compatibility fixes, so an interrupted launch upgrades cleanly.
        return tail in optional or tail in tuple(tournament_patch.patch_rows()+rows for rows in optional)
    except (UnicodeError, ValueError):
        return False


def _enabled_settings(prior):
    """Splice our entry into RPCS3's block mapping, preserving other entries."""
    text = prior.decode('utf-8') if prior is not None else ''
    entry = (f'  "{DESCRIPTION}":\n    "FIFA 19":\n'
             '      BLES02258:\n        "01.02":\n          Enabled: true\n')
    if text.strip() in ('', '{}'): return (f'{PPU_HASH}:\n'+entry).encode()
    if re.search(r'^(?:---|\.\.\.|\{|\[)', text, re.M):
        raise RuntimeError('Existing patch configuration needs a manual merge; it has not been changed.')
    if not re.search(r'^\S[^\n]*:\s*$', text, re.M):
        raise RuntimeError('Unrecognised patch configuration; it has not been changed.')
    lines = text.splitlines(keepends=True)
    hash_key = re.escape(PPU_HASH)
    hash_heads = [i for i, line in enumerate(lines)
                  if re.match(rf'(?:{hash_key}|"{hash_key}"|\'{hash_key}\'):', line)]
    headers = [i for i, line in enumerate(lines)
               if re.fullmatch(rf'(?:{hash_key}|"{hash_key}"|\'{hash_key}\'):[ \t]*(?:#[^\r\n]*)?\r?\n?', line)]
    if hash_heads != headers:
        raise RuntimeError('Unsupported FIFA patch configuration; it has not been changed.')
    if not headers:
        if PPU_HASH in text:
            raise RuntimeError('Ambiguous FIFA patch configuration; it has not been changed.')
        return (text+('' if text.endswith('\n') else '\n')+f'{PPU_HASH}:\n'+entry).encode()
    if len(headers) != 1:
        raise RuntimeError('Duplicate FIFA patch configuration; it has not been changed.')
    start = headers[0]+1
    end = next((i for i in range(start, len(lines)) if re.match(r'[^\s#]', lines[i])), len(lines))
    name = re.escape(DESCRIPTION)
    entry_heads = [i for i in range(start, end)
                   if re.match(rf'  (?:{name}|"{name}"|\'{name}\'):', lines[i])]
    entries = [i for i in range(start, end)
               if re.fullmatch(rf'  (?:{name}|"{name}"|\'{name}\'):[ \t]*(?:#[^\r\n]*)?\r?\n?', lines[i])]
    if len(entry_heads) > 1:
        raise RuntimeError('Duplicate local certificate patch settings; they have not been changed.')
    if entry_heads != entries:
        raise RuntimeError('Unsupported local certificate patch settings; they have not been changed.')
    if entries:
        begin = entries[0]
        finish = next((i for i in range(begin+1, end) if re.match(r'  \S', lines[i])), end)
        # Replace only our named entry, not other FIFA patches under this hash.
        lines[begin:finish] = [entry]
    else:
        if end and not lines[end-1].endswith('\n'): lines[end-1] += '\n'
        lines.insert(end, entry)
    return ''.join(lines).encode()


def _write_atomic(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.fut-patch-', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try: temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)


@contextmanager
def temporary_patch(profile, backup_directory, skip_upgrade_rewards=True, trust_ca=None):
    """Run by the user's desktop launcher; restore existing patch settings."""
    # Two isolated clients can generate different research settings at once.
    # Read our complete patch while holding the generator's process lock.
    with client_platform.file_lock(HERE/'.patch-generation.lock'):
        source = prepare(include_profile_trace=True, skip_upgrade_rewards=skip_upgrade_rewards, trust_ca=trust_ca)
        desired = source.read_bytes()
    target = profile / 'patches/BLES02258_patch.yml'
    settings = client_platform.settings_directory(profile) / 'patch_config.yml'
    if target.is_symlink() or settings.is_symlink():
        linked = target if target.is_symlink() else settings
        raise RuntimeError(f'Linked patch file requires review before installing the local certificate patch: {linked}')
    prior_patch = target.read_bytes() if target.exists() else None
    if prior_patch is not None and not _launcher_patch(prior_patch, desired):
        raise RuntimeError(f'Existing FIFA patch is not a recognised launcher patch: {target}. It has not been changed.')
    prior = settings.read_bytes() if settings.exists() else None
    enabled = _enabled_settings(prior)
    backup_directory.mkdir(parents=True, exist_ok=True)
    (backup_directory / 'patch-config-before.json').write_text(json.dumps({'existed': prior is not None}) + '\n')
    if prior_patch is not None:
        (backup_directory / 'BLES02258_patch.yml.before').write_bytes(prior_patch)
        print('Refreshing an existing launcher-generated FIFA patch; original backed up for restoration on exit.', flush=True)
    if prior is not None:
        (backup_directory / 'patch_config.yml.before').write_bytes(prior)
    installed = False
    changed_settings = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, desired)
        installed = True
        settings.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(settings, enabled)
        changed_settings = True
        yield
    finally:
        try:
            if changed_settings and settings.exists() and settings.read_bytes() == enabled:
                if prior is None:
                    settings.unlink()
                else:
                    _write_atomic(settings, prior)
        finally:
            if installed and target.exists() and not target.is_symlink() and target.read_bytes() == desired:
                if prior_patch is None: target.unlink()
                else: _write_atomic(target, prior_patch)


if __name__ == '__main__':
    print(prepare())
