"""RPCS3 client paths, process checks and locks on Linux and Windows."""
from contextlib import contextmanager
import csv
import io
import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


def windows():
    return sys.platform == 'win32'


def executable_path(path):
    path = path.expanduser()
    if not path.is_file() and not path.is_absolute():
        found = shutil.which(str(path))
        if found:
            path = Path(found)
    if windows() and path.suffix.lower() == '.appimage':
        raise ValueError('Windows needs the Windows RPCS3 rpcs3.exe, not a Linux AppImage')
    return path.resolve()


def profile_path(executable, requested=None):
    if not windows():
        return (requested or Path.home()/'.config/rpcs3').expanduser().resolve()
    portable = executable.parent/'portable'
    if portable.is_dir():
        if requested is not None and requested.expanduser().resolve() != portable:
            raise ValueError('RPCS3 uses its portable folder; omit --profile or select that folder')
        return portable
    if requested is not None:
        return requested.expanduser().resolve()
    configured = os.environ.get('RPCS3_CONFIG_DIR')
    if configured:
        # RPCS3's Windows resolver retains everything through the final slash.
        return (Path(configured) if configured.endswith(('/', '\\'))
                else Path(configured).parent).resolve()
    return executable.parent


def settings_directory(profile):
    return profile/'config' if windows() else profile


def hdd0_path(profile):
    """Resolve the two flat RPCS3 VFS scalars used by installed game updates."""
    source = settings_directory(profile)/'vfs.yml'
    values = {}
    if source.is_file():
        for match in re.finditer(r'^(\$\(EmulatorDir\)|/dev_hdd0/):[ \t]*(.*)$',
                                 source.read_text(encoding='utf-8'), re.M):
            key, text = match.groups()
            text = text.strip()
            if key in values:
                raise ValueError('Duplicate RPCS3 VFS path; check vfs.yml')
            if text.startswith('"'):
                value = json.loads(text)
            elif text.startswith("'") and text.endswith("'"):
                value = text[1:-1].replace("''", "'")
            elif text.startswith(('!', '&', '*', '|', '>', '{', '[')):
                raise ValueError('Unsupported RPCS3 VFS path syntax; use a plain or quoted directory')
            else:
                value = text.split(' #', 1)[0].rstrip()
            if not isinstance(value, str):
                raise ValueError('RPCS3 VFS path must be a directory string')
            values[key] = value
    emulator = values.get('$(EmulatorDir)') or str(profile)+os.sep
    if '$(' in emulator or not Path(emulator).is_absolute():
        raise ValueError('RPCS3 EmulatorDir must be an absolute directory')
    raw = values.get('/dev_hdd0/') or '$(EmulatorDir)dev_hdd0/'
    path = Path(raw.replace('$(EmulatorDir)', emulator.rstrip('/\\')+os.sep))
    if '$(' in str(path) or not path.is_absolute():
        raise ValueError('RPCS3 dev_hdd0 must use an absolute path or $(EmulatorDir)')
    return path.resolve()


def configuration_source(profile):
    settings = settings_directory(profile)
    custom = settings/'custom_configs/config_BLES02258.yml'
    if custom.is_file():
        return custom
    primary = settings/'config.yml'
    if primary.is_file():
        return primary
    # Older Windows builds kept config.yml beside rpcs3.exe.
    legacy = profile/'config.yml'
    if legacy.is_file():
        return legacy
    raise ValueError(f'RPCS3 configuration missing in {profile}; open RPCS3 and configure it first')


def environment(profile):
    env = dict(os.environ)
    env.pop('APPIMAGE_EXTRACT_AND_RUN', None)
    env.pop('APPIMAGE', None)
    if windows():
        # A trailing separator is required by RPCS3's Windows path resolver.
        env['RPCS3_CONFIG_DIR'] = str(profile)+os.sep
    return env


def log_directory(profile):
    if windows():
        current = profile/'log'
        if current.is_dir() or not (profile/'RPCS3.log').is_file():
            return current
        return profile  # Older Windows log layout.
    return Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'rpcs3'


def rpcs3_running(executable):
    if windows():
        result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                                text=True, errors='replace', check=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        names = {row[0].casefold() for row in csv.reader(io.StringIO(result.stdout)) if row}
        return bool(names & {'rpcs3.exe', executable.name.casefold()})
    result = subprocess.run(['ps', '-eo', 'comm='], capture_output=True,
                            text=True, check=True, timeout=10)
    return any(name.strip().casefold() == 'rpcs3' for name in result.stdout.splitlines())


@contextmanager
def file_lock(path, blocking=True):
    """Release a process lock on every exit; Windows locks the first byte."""
    with path.open('a+b') as stream:
        if windows():
            import msvcrt
            if not path.stat().st_size:
                stream.write(b'\0')
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (13, 11, 36):
                        raise
                    if not blocking:
                        raise BlockingIOError('Client files are already in use') from error
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
