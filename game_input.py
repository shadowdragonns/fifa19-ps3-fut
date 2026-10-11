"""Validate a JB folder or extract a decrypted ISO to a separate folder."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import client_platform

UPDATE_SELF_SHA256 = '551777d06d2bab068a7fbeacd12059b8ff8e4f491aa34feccc9619e71adc3478'


def sfo(raw):
    if len(raw) < 20 or raw[:4] != b'\0PSF':
        raise ValueError('Not a decrypted PS3 PARAM.SFO')
    _, keys, values, count = struct.unpack_from('<4I', raw, 4)
    if count > 1024 or 20+count*16 > len(raw):
        raise ValueError('Invalid PARAM.SFO directory')
    result = {}
    for index in range(count):
        key, kind, size, allocated, offset = struct.unpack_from('<HHIII', raw, 20+index*16)
        begin = keys+key
        end = raw.find(b'\0', begin)
        start = values+offset
        if not 0 <= begin < len(raw) or end < begin or size > allocated or start+size > len(raw):
            raise ValueError('Invalid PARAM.SFO field bounds')
        name = raw[begin:end].decode('utf-8')
        value = raw[start:start+size]
        result[name] = value.rstrip(b'\0').decode('utf-8') if kind == 0x204 else value
    return result


def validate_folder(path):
    path = path.resolve()
    if path.name == 'PS3_GAME':
        path = path.parent
    game = path/'PS3_GAME'
    info = sfo((game/'PARAM.SFO').read_bytes())
    if info.get('TITLE_ID') != 'BLES02258':
        raise ValueError('This client patch supports FIFA 19 PS3 BLES02258 only')
    boot = game/'USRDIR/EBOOT.BIN'
    with boot.open('rb') as stream:
        if stream.read(4) != b'SCE\0':
            raise ValueError('Expected the original PS3 EBOOT.BIN SELF container')
    return path


def validate_update(profile):
    validate_update_directory(client_platform.hdd0_path(profile)/'game/BLES02258')


def validate_update_directory(directory):
    path = directory/'PARAM.SFO'
    if not path.is_file() or sfo(path.read_bytes()).get('APP_VER') != '01.02':
        raise ValueError('Install your FIFA 19 BLES02258 v1.02 update in this RPCS3 profile first; the client patch is specific to that executable')
    boot = directory/'USRDIR/EBOOT.BIN'
    if hashlib.sha256(boot.read_bytes()).hexdigest() != UPDATE_SELF_SHA256:
        raise ValueError('The installed update EBOOT differs from the tested v1.02 executable; this client patch cannot safely target it')


def iso_entries(output):
    # 7-Zip's archive header also has a Path entry. Only inspect file records.
    if '----------' not in output:
        raise ValueError('ISO extractor returned no file listing')
    listing = output.split('----------', 1)[1]
    paths = []
    for record in listing.split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in record.splitlines() if ' = ' in line)
        if 'Path' not in fields:
            continue
        text = fields['Path'].replace('\\', '/')
        path = PurePosixPath(text)
        if path.is_absolute() or '..' in path.parts or ':' in text or '\x00' in text:
            raise ValueError('Unsafe path in ISO')
        if 'Symbolic Link' in fields or 'Hard Link' in fields or 'l' in fields.get('Mode', ''):
            raise ValueError('Links in ISO are unsupported')
        paths.append(text)
    if 'PS3_GAME/PARAM.SFO' not in paths or 'PS3_GAME/USRDIR/EBOOT.BIN' not in paths:
        raise ValueError('Expected a decrypted PS3 ISO with PS3_GAME at its root')
    return paths


def prepare_game(path, cache, extractor=None):
    path = path.expanduser().resolve()
    if path.is_dir():
        return validate_folder(path)
    if not path.is_file() or path.suffix.lower() != '.iso':
        raise ValueError('Supply a FIFA 19 JB folder or decrypted .iso file')
    metadata = {'source': str(path), 'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()[:16]
    target = cache/f'{path.stem}-{digest}'
    marker = target/'.fut-iso.json'
    if marker.is_file() and json.loads(marker.read_text()) == metadata:
        return validate_folder(target)
    if target.exists():
        raise ValueError(f'Incomplete/existing extraction: {target}; preserve or remove it before retrying')
    program = extractor or shutil.which('7zz') or shutil.which('7z') or shutil.which('7z.exe')
    if not program and os.name == 'nt':
        for variable in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
            directory = os.environ.get(variable)
            candidate = Path(directory)/'7-Zip/7z.exe' if directory else None
            if candidate and candidate.is_file():
                program = candidate
                break
    if not program:
        raise ValueError('ISO input needs 7-Zip (7z or 7zz); install it or supply an extracted JB folder')
    listing = subprocess.run([str(program), 'l', '-slt', str(path)], check=True, capture_output=True, text=True)
    iso_entries(listing.stdout.replace('\r\n', '\n'))
    cache.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    # The immutable source ISO is never rewritten. A failed extraction remains
    # visible for diagnosis; only a successfully validated folder is cached.
    print(f'Extracting ISO to {target}; this can take several minutes.', flush=True)
    with (target/'.extract.log').open('w') as log:
        subprocess.run([str(program), 'x', '-y', f'-o{target}', str(path)], check=True,
                       stdout=log, stderr=subprocess.STDOUT)
    validate_folder(target)
    marker.write_text(json.dumps(metadata, indent=2)+'\n')
    return target
