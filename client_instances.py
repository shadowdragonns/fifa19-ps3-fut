"""Independent Linux RPCS3/FIFA files, locks and RPCN/gameplay host UDP ports."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from game_input import validate_folder, validate_update

HERE=Path(__file__).resolve().parent


def instance_root(name):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}',name):
        raise ValueError('Instance name must use 1-32 letters, digits, underscores or hyphens')
    return HERE/'runtime/clients'/name


def rpcn_port(name):
    return {'player1':13658,'player2':14658}.get(name,16000+int(hashlib.sha256(name.encode()).hexdigest()[:8],16)%40000)


def rpcn_fields(profile):
    path=profile/'rpcn.yml'
    if not path.is_file():return {}
    values={}
    # Parse only these flat cfg_rpcn scalar fields. Never print their values.
    for key in ('NPID','Password'):
        match=re.search(r'^'+key+r':[ \t]*([^\n]*)$',path.read_text(),re.M)
        if match:
            value=match[1].strip()
            if value.startswith('"'):value=json.loads(value)
            elif value.startswith("'") and value.endswith("'"):value=value[1:-1].replace("''","'")
            values[key]=value
    return values


def validate_rpcn(root):
    values=rpcn_fields(root/'config/rpcs3')
    if not values.get('NPID') or not values.get('Password'):
        raise ValueError(f'Configure an RPCN account first: launch.py --instance {root.name} --configure')
    for sibling in root.parent.iterdir():
        if sibling==root or not (sibling/'instance.json').is_file():continue
        other=rpcn_fields(sibling/'config/rpcs3')
        if other.get('NPID','').casefold()==values['NPID'].casefold():
            raise ValueError('Isolated clients must use different RPCN accounts; configure player2 with its own account')


def environment(root, port=None):
    if sys.platform!='linux':
        raise ValueError('Isolated client instances currently require Linux')
    env=dict(os.environ)
    env['XDG_CONFIG_HOME']=str(root/'config')
    env['XDG_CACHE_HOME']=str(root/'cache')
    env['XDG_DATA_HOME']=str(root/'data')
    if port is not None:
        if type(port) is not int or not 1024<=port<=65533 or any(p in (3658,3659,9999) for p in range(port,port+3)):
            raise ValueError('Choose three distinct host UDP ports starting from 1024 to 65533')
        env['FIFA19_RPCN_PORT']=str(port)
        adapter=port_adapter()
        env['LD_PRELOAD']=str(adapter)+((':'+env['LD_PRELOAD']) if env.get('LD_PRELOAD') else '')
    return env


def port_adapter():
    source=HERE/'rpcn_port.c'
    digest=hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    target=HERE/'runtime/adapters'/('rpcn-port-'+digest+'.so')
    if not target.is_file():
        compiler=shutil.which('cc')
        if not compiler:
            raise RuntimeError('Isolated instances need a C compiler (cc) for the RPCN port adapter')
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try:
            subprocess.run([compiler,'-shared','-fPIC','-O2','-Wall','-Wextra','-Werror',
                            '-o',str(temporary),str(source),'-ldl','-pthread'],check=True)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


@contextmanager
def instance_lock(root):
    if sys.platform != 'linux':
        raise ValueError('Isolated client instances currently require Linux; use --rpcs3 and --game for a single Windows client')
    import fcntl
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    native_lock=root/'cache/rpcs3/RPCS3.buf'
    if native_lock.exists():
        with native_lock.open('r+b') as native:
            try:
                fcntl.flock(native,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f'Close the RPCS3 window for {root.name} first') from None
            fcntl.flock(native,fcntl.LOCK_UN)
    with (root/'client.lock').open('a+') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'This emulator profile is already in use: {root.name}') from None
        try:
            yield
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def copy(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    # Reflink where supported; otherwise full independent copies, never hard links.
    subprocess.run(['cp','-a','--reflink=auto',str(source),str(target)],check=True)


def prepare(name,source_profile,source_game,copy_rpcn=False,source_cache=None):
    root=instance_root(name)
    profile=root/'config/rpcs3'
    marker=root/'instance.json'
    with instance_lock(root):
        if marker.is_file():
            validate_update(profile)
            validate_folder(root/'game')
            return root
        if any((root/p).exists() for p in ('config','game','cache','data')):
            raise RuntimeError(f'Incomplete instance exists: {root}; preserve it before retrying')
        validate_update(source_profile)
        validate_folder(source_game)
        if ((source_profile/'patches/BLES02258_patch.yml').exists()
                or (source_game/'PS3_GAME/USRDIR/cl.ini').exists()):
            raise RuntimeError('Close the original FIFA launcher before copying its temporary settings')
        if (source_profile/'vfs.yml').is_file():
            text=(source_profile/'vfs.yml').read_text()
            # The installed profile uses self-relative mounts. Absolute mounts
            # would silently send this instance back to the original saves.
            if re.search(r'(?m)^[^:\n]+:[ \t]*["\']?/',text):
                raise ValueError('Source VFS has absolute mounts; separate them before cloning')
        profile.mkdir(parents=True,mode=0o700)
        for filename in ('config.yml','vfs.yml','patch_config.yml','gem.yml','gem_mouse.yml',
                         'gem_real.yml','usio.yml','evdev_positive_axis.yml'):
            source=source_profile/filename
            if source.is_file(): copy(source,profile/filename)
        if copy_rpcn and (source_profile/'rpcn.yml').is_file():
            copy(source_profile/'rpcn.yml',profile/'rpcn.yml')
            (profile/'rpcn.yml').chmod(0o600)
        for directory in ('dev_flash','dev_flash2','dev_flash3','input_configs','GuiConfigs','patches','custom_configs'):
            source=source_profile/directory
            if source.is_dir(): copy(source,profile/directory)
        for relative in ('dev_hdd0/game/BLES02258','dev_hdd0/home'):
            source=source_profile/relative
            if source.is_dir(): copy(source,profile/relative)
        for directory in ('dev_hdd1','dev_bdvd','dev_usb000','games','captures','savestates','sounds'):
            (profile/directory).mkdir(parents=True,exist_ok=True)
        (profile/'uuid').write_text(str(uuid.uuid4()))
        if source_cache and source_cache.is_dir():
            # Copy compiled shaders/PPU caches without process locks or logs.
            for source in source_cache.iterdir():
                if source.is_dir(): copy(source,root/'cache/rpcs3'/source.name)
        (root/'game').mkdir()
        copy(source_game/'PS3_GAME',root/'game/PS3_GAME')
        if (source_game/'PS3_DISC.SFB').is_file(): copy(source_game/'PS3_DISC.SFB',root/'game/PS3_DISC.SFB')
        validate_update(profile)
        validate_folder(root/'game')
        marker.write_text(json.dumps({'version':1,'name':name,'rpcn_copied':copy_rpcn,
            'source_game':str(source_game),'rpcn_host_udp_port':rpcn_port(name)},indent=2)+'\n')
    return root
