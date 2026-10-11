"""Install the included FIFA v1.02 update into an existing RPCS3 data folder."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

import client_platform
from game_input import validate_update, validate_update_directory


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.digest()


def verify_copy(source, target):
    original = {p.relative_to(source) for p in source.rglob('*') if p.is_file()}
    copied = {p.relative_to(target) for p in target.rglob('*') if p.is_file()}
    if original != copied or any(file_hash(source/p) != file_hash(target/p) for p in original):
        raise ValueError('Copied update differs from its source; the installed update has not been changed')
    validate_update_directory(target)
    return len(original)


def install(executable, source, requested_profile=None):
    executable = client_platform.executable_path(executable)
    if not executable.is_file():
        raise ValueError('RPCS3 executable missing: '+str(executable))
    profile = client_platform.profile_path(executable, requested_profile)
    source = source.expanduser().resolve()
    validate_update_directory(source)
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('Linked update files are unsupported; use the included complete update folder')
    hdd0 = client_platform.hdd0_path(profile)
    target = hdd0/'game/BLES02258'
    if (source == target.resolve() or target.resolve() in source.parents
            or source in target.resolve().parents):
        raise ValueError('Source and installed update must be separate folders')
    if target.is_symlink() or (target.exists() and
            os.path.normcase(str(target.resolve())) != os.path.normcase(str(target.absolute()))):
        raise ValueError('The installed update is linked; review its location before replacing it')
    if client_platform.rpcs3_running(executable):
        raise RuntimeError('Close RPCS3 and FIFA before installing the update')
    profile.mkdir(parents=True, exist_ok=True)
    with client_platform.file_lock(profile/'.fifa19-fut-launch.lock', blocking=False):
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.BLES02258-install-', dir=target.parent))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+'-'+uuid.uuid4().hex[:8]
        backup = hdd0/'fifa19-update-backups'/stamp/'BLES02258'
        moved_old = False
        installed = False
        try:
            shutil.copytree(source, stage, dirs_exist_ok=True)
            count = verify_copy(source, stage)
            if target.exists():
                backup.parent.mkdir(parents=True, exist_ok=False)
                target.rename(backup)
                moved_old = True
            stage.rename(target)
            installed = True
            validate_update(profile)
        except BaseException:
            if installed:
                target.rename(stage)
            if moved_old:
                backup.rename(target)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return {'profile': profile, 'target': target, 'backup': backup if moved_old else None,
                'files': count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rpcs3', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True, help='Included BLES02258 update folder')
    parser.add_argument('--profile', type=Path, help='Override the existing RPCS3 data directory')
    args = parser.parse_args()
    result = install(args.rpcs3, args.source, args.profile)
    print('Installed verified FIFA 19 v1.02 update ('+str(result['files'])+' files): '+str(result['target']))
    if result['backup']:
        print('Previous update preserved: '+str(result['backup']))
    print('RPCN settings, saves and other games are preserved. Start FIFA with the FUT launcher to apply its temporary network patch.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print('FIFA update install: '+str(error), file=sys.stderr)
        sys.exit(1)
