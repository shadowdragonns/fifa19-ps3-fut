"""Reject private/runtime/game files in tracked source or public bundle ZIPs."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

HERE = Path(__file__).resolve().parent
PRIVATE_DIRS = {'state', 'state-backups', 'logs', 'runtime', 'rpcs3-runtime', 'tls',
                'plugin-data', 'PS3_GAME', 'PS3_UPDATE', 'research', '.github-auth'}
PRIVATE_NAMES = {'rpcn.yml', 'hosts.yml', 'server-config.json', 'launcher-defaults.json', '.env', 'PS3_DISC.SFB', 'BLES02258_patch.yml', 'rpcs3-discovery.yml'}
FORBIDDEN_SUFFIXES = {'.iso', '.pkg', '.elf', '.big', '.bin', '.self', '.sprx', '.key', '.pem', '.p12', '.pfx'}
SECRET_PATTERNS = (rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
                   rb'\bgh[pousr]_[A-Za-z0-9]{20,}\b', rb'\bgithub_pat_[A-Za-z0-9_]{20,}\b')


def check_file(name, content):
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name
            or PRIVATE_DIRS.intersection(path.parts) or path.name in PRIVATE_NAMES
            or path.name.startswith('.env.') or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or any(part.startswith(('dev_hdd', 'dev_flash')) for part in path.parts)):
        raise ValueError('Disallowed public path: '+name)
    if content[:4] in (b'\x7fELF', b'SCE\0', b'BIG4', b'BIGF'):
        raise ValueError('Executable/game archive content: '+name)
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, content): raise ValueError('Possible private key/token in '+name)


def tracked():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=HERE, check=True, capture_output=True)
    names = result.stdout.decode().split('\0')
    names = [name for name in names if name]
    if not names: raise ValueError('No tracked files; stage the reviewed publication files first')
    for name in names:
        path = HERE/name
        if path.is_symlink(): raise ValueError('Symlinks are not published: '+name)
        check_file(name, path.read_bytes())
        staged = subprocess.run(['git', 'show', ':'+name], cwd=HERE, check=True, capture_output=True).stdout
        check_file(name, staged)
    return len(names)


def bundle(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.namelist()
        if len(set(entries)) != len(entries): raise ValueError('Duplicate ZIP members')
        prefixes = {name.split('/')[0] for name in entries}
        if len(prefixes) != 1: raise ValueError('Bundle must have one root')
        prefix = prefixes.pop()+'/'
        manifest = json.loads(archive.read(prefix+'manifest.json'))
        if manifest['includes_saved_club'] or manifest.get('includes_private_keys', True):
            raise ValueError('Private data included in manifest')
        if set(entries) != {prefix+name for name in manifest['sha256']} | {prefix+'manifest.json'}:
            raise ValueError('ZIP member/manifest mismatch')
        for name, digest in manifest['sha256'].items():
            content = archive.read(prefix+name); check_file(name, content)
            if hashlib.sha256(content).hexdigest() != digest: raise ValueError('ZIP checksum mismatch')
    return len(entries)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.bundle:
        for path in args.bundle: print(f'{path.name}: {bundle(path)} public entries verified')
    else: print(f'{tracked()} tracked files verified; no game files, private runtime directories or key/token patterns')
