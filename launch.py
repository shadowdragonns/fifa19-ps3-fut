#!/usr/bin/env python3
"""Launch FIFA 19 against a standalone FUT server or the same-PC backend."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from certificate_patch import temporary_patch
from profile_trace import SITES as PROFILE_TRACE_SITES
from endpoints import Endpoints, ipv4
from game_input import prepare_game, validate_update
import client_instances
import client_platform
from server_trust import check_server, fetch_certificate

HERE = Path(__file__).resolve().parent
GAME = HERE.parents[1]
PROFILE = Path.home() / '.config/rpcs3'
DEFAULT_RPCS3 = Path('rpcs3.exe') if client_platform.windows() else Path('/usr/bin/rpcs3')


def network_config(source, trace_network=False, host='127.0.0.1'):
    host = ipv4(host)
    text = source
    start = re.search(r'^Net:\s*\n', text, re.M)
    if not start:
        raise ValueError('RPCS3 configuration has no Net section')
    end = re.search(r'^\S', text[start.end():], re.M)
    end = start.end() + end.start() if end else len(text)
    section = text[start.start():end]
    swap = re.search(r'^  IP swap list: (.*)$', section, re.M)
    if not swap:
        raise ValueError('Expected Net/IP swap list setting')
    prior = swap.group(1).strip()
    if prior.startswith('"'):
        prior = json.loads(prior)
    else:
        prior = prior.strip("'")
    redirect = f'winter15.gosredirector.ea.com={host}&&gosca.ea.com={host}'
    if prior:
        kept = [mapping for mapping in prior.split('&&')
                if mapping.split('=', 1)[0] not in ('winter15.gosredirector.ea.com', 'gosca.ea.com')]
        if kept:
            redirect += '&&' + '&&'.join(kept)
    # FIFA requests sceNpManagerRequestTicket2 during startup. Simulated PSN
    # throws a fatal RPCS3 error on that path; use the configured RPCN account.
    for key, value in {'Internet enabled': 'Connected', 'PSN status': 'RPCN',
                       'UPNP Enabled': 'false', 'Bind address': '0.0.0.0',
                       'IP swap list': json.dumps(redirect)}.items():
        section, count = re.subn(r'^  ' + re.escape(key) + r':[^\n]*$', f'  {key}: {value}', section, flags=re.M)
        if count != 1:
            raise ValueError(f'Expected one Net/{key} setting')
    text = text[:start.start()] + section + text[end:]
    # The game polls NP status constantly. Full tracing is opt-in because it
    # adds thousands of synchronous diagnostic records while navigating FUT.
    levels = dict.fromkeys(('sys_net', 'sceNp', 'cellNetCtl'),
                           'Trace' if trace_network else 'Notice')
    levels['DnsHook'] = 'Trace'
    log = re.search(r'^Log:\s*\n', text, re.M)
    if log:
        following = re.search(r'^\S', text[log.end():], re.M)
        stop = log.end()+following.start() if following else len(text)
        section = text[log.end():stop]
        section = re.sub(r'^  \{\}\s*\n?', '', section, flags=re.M)
        for channel, level in levels.items():
            section, count = re.subn(r'^  '+re.escape(channel)+r':[^\n]*$',
                                     f'  {channel}: {level}', section, flags=re.M)
            if not count:
                section = section.rstrip('\n')+'\n' if section else ''
                section += f'  {channel}: {level}\n'
        text = text[:log.end()]+section+text[stop:]
    else:
        text = text.rstrip('\n')+'\nLog:\n'+''.join(
            f'  {channel}: {level}\n' for channel, level in levels.items())
    return text


def runtime_executable(image):
    """Use a persistent extraction: extract-and-run can exit before game boot."""
    if image.suffix.lower() != '.appimage':
        return image
    root = HERE / 'rpcs3-runtime'
    manifest = root / 'manifest.json'
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved['sha256'] != digest:
            raise RuntimeError('The AppImage changed. Preserve or move mods/fut-local/rpcs3-runtime before preparing a new extraction.')
    else:
        root.mkdir(exist_ok=True)
        env = dict(os.environ)
        env.pop('APPIMAGE_EXTRACT_AND_RUN', None)
        with (root / 'extract.log').open('w') as log:
            result = subprocess.run([str(image), '--appimage-extract'], cwd=root,
                                    stdout=log, stderr=subprocess.STDOUT, env=env, timeout=60)
        if result.returncode:
            raise RuntimeError(f'AppImage extraction failed; see {root / "extract.log"}')
        manifest.write_text(json.dumps({'appimage': str(image), 'sha256': digest}, indent=2) + '\n')
    executable = root / 'squashfs-root/AppRun'
    if not executable.is_file():
        raise RuntimeError(f'Extracted executable is absent: {executable}')
    return executable


def log_position(path):
    if not path.exists():
        return None
    size = path.stat().st_size
    with path.open('rb') as stream:
        stream.seek(max(0, size - 512))
        tail = stream.read()
    return size, tail


def copy_filtered_log(source, target, before, pattern):
    if not source.exists():
        return
    offset = 0
    if before:
        old_size, old_tail = before
        with source.open('rb') as stream:
            stream.seek(max(0, old_size - 512))
            tail = stream.read(len(old_tail))
        if source.stat().st_size >= old_size and tail == old_tail:
            offset = old_size
    with source.open('rb') as src, target.open('w') as out:
        src.seek(offset)
        for raw in src:
            line = raw.decode(errors='replace')
            if re.search(pattern, line):
                out.write(line)


def copy_network_log(source, target, before):
    copy_filtered_log(source, target, before,
        r'sys_net|sceNp|cellNetCtl|cl\.ini|Blaze|blaze|gosredirector|DnsHook|Internet enabled:|PSN status:|IP swap list:|PAT:|ppu_loader: PPU executable hash|FUTPRAN')


def copy_runtime_log(source, target, before):
    # Keep timing/graphics diagnostics separate from account/network messages.
    copy_filtered_log(source, target, before,
        r'(?:PPU|RSX).*[Cc]ompil|RSX:.*(?:shader|texture|pipeline)|(?:E|F) [^\n]*(?:ppu_loader|PPU:|RSX:)')


def copy_profile_trace(source, target, before):
    """Copy this launch's fixed, credential-free native diagnostic records."""
    if not source.exists():
        return
    offset = 0
    if before:
        old_size, old_tail = before
        with source.open('rb') as stream:
            stream.seek(max(0, old_size - 512))
            tail = stream.read(len(old_tail))
        if source.stat().st_size >= old_size and tail == old_tail:
            offset = old_size
    markers = b'|'.join(re.escape(site[2].encode()) for site in PROFILE_TRACE_SITES)
    with source.open('rb') as stream, target.open('w') as out:
        stream.seek(offset)
        for raw in stream:
            match = re.search(rb'FUTPRAN (?:' + markers + rb')(?: [a-z]+=[0-9A-F]{8})*(?=[\r\n\x00]|$)', raw)
            if match:
                out.write(match[0].decode('ascii') + '\n')


@contextmanager
def local_settings(target, desired=None):
    desired = desired if desired is not None else (HERE / 'cl.ini').read_bytes()
    if target.exists() or target.is_symlink():
        raise RuntimeError(f'{target} already exists; preserve and review it before installing discovery settings.')
    mode = stat.S_IMODE(target.parent.stat().st_mode)
    installed = False
    try:
        target.parent.chmod(mode | stat.S_IWUSR)
        with target.open('xb') as out:
            installed = True
            out.write(desired)
        target.parent.chmod(mode)
        yield
    finally:
        try:
            if installed and target.exists() and not target.is_symlink() and target.read_bytes() == desired:
                target.parent.chmod(mode | stat.S_IWUSR)
                target.unlink()
        finally:
            target.parent.chmod(mode)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rpcs3', type=Path, default=DEFAULT_RPCS3)
    parser.add_argument('--game', type=Path, default=GAME, help='JB folder or decrypted FIFA19.iso')
    parser.add_argument('--server', help='Server IPv4/hostname; omit to start the existing same-PC server')
    parser.add_argument('--trust-ca', type=Path, help='Public root.pem certificate supplied by the server operator')
    parser.add_argument('--refresh-server-cert', action='store_true', help='Replace the saved certificate after an intentional server CA change')
    parser.add_argument('--cert-cache', type=Path, default=HERE/'runtime/server-certs', help='Directory for automatically fetched public certificates')
    parser.add_argument('--gui', action='store_true', help='Open the graphical launcher with these settings')
    parser.add_argument('--open-rpcs3', action='store_true', help='Open RPCS3 settings without requiring a game or update')
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--profile', type=Path, help='RPCS3 data directory; defaults to ~/.config/rpcs3 on Linux, beside rpcs3.exe (or portable/) on Windows')
    parser.add_argument('--instance', help='Isolated Linux client prepared by setup_clients.py (player1/player2)')
    parser.add_argument('--configure', action='store_true', help='Open an isolated client GUI to configure RPCN and controllers')
    parser.add_argument('--background-input', action='store_true', help='Let isolated clients accept input while unfocused (use separate input devices)')
    parser.add_argument('--iso-cache', type=Path, default=HERE/'runtime/games')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--trace-network', action='store_true',
                        help='Enable verbose network syscalls for troubleshooting (may affect responsiveness)')
    parser.add_argument('--keep-upgrade-rewards', action='store_true',
                        help='Keep the original upgrade-rewards loader for research; it currently stalls local startup')
    args = parser.parse_args(argv)
    if args.gui:
        from launcher_gui import main as gui_main
        gui_main(args)
        return
    if args.open_rpcs3:
        if args.instance: raise ValueError('Use --configure for an isolated instance')
        executable = client_platform.executable_path(args.rpcs3)
        if not executable.is_file(): raise ValueError('Choose your RPCS3 executable first')
        profile = client_platform.profile_path(executable, args.profile)
        executable = runtime_executable(executable)
        subprocess.run([str(executable)], env=client_platform.environment(profile),
                       cwd=executable.parent if client_platform.windows() else None, check=True)
        return
    if args.configure and not args.instance:
        raise ValueError('--configure requires --instance')
    if args.instance:
        if sys.platform != 'linux':
            raise ValueError('--instance currently requires Linux; omit it for a single Windows client')
        root=client_instances.instance_root(args.instance)
        if not (root/'instance.json').is_file():
            raise ValueError('Run setup_clients.py first to prepare player1 and player2')
        with client_instances.instance_lock(root):
            run(args,root)
    else:
        run(args)


def run(args, root=None):
    if root:
        if args.profile not in (None, PROFILE) or args.game!=GAME:
            raise ValueError('--instance uses its own copied profile/game; omit --profile and --game')
        args.profile=root/'config/rpcs3'
        args.game=root/'game'
        if not args.server:
            raise ValueError('Supply --server with the address of your server')
    if not args.server and not (HERE/'server.py').is_file():
        raise ValueError('This portable client requires --server SERVER_IP; start the separate server package first')
    host = ipv4(args.server, resolve=True) if args.server else '127.0.0.1'
    endpoints = Endpoints(host)
    args.rpcs3 = client_platform.executable_path(args.rpcs3)
    if not args.rpcs3.is_file():
        raise RuntimeError('RPCS3 executable not found; supply --rpcs3 /path/to/executable')
    game_path = prepare_game(args.game, args.iso_cache)
    profile = client_platform.profile_path(args.rpcs3, args.profile)
    validate_update(profile)
    source = client_platform.configuration_source(profile)
    config = (root or HERE) / 'rpcs3-discovery.yml'
    text=network_config(source.read_text(encoding='utf-8'), args.trace_network, host)
    if root:
        for key,value in {'Background input enabled':'true' if args.background_input else 'false',
                          'Pause emulation on RPCS3 focus loss':'false',
                          'Start games in fullscreen mode':'false'}.items():
            text=re.sub(r'^  '+re.escape(key)+r':[^\n]*$',f'  {key}: {value}',text,flags=re.M)
        text=re.sub(r'^  Window Title Format:[^\n]*$',
                    '  Window Title Format: '+json.dumps('['+args.instance+'] FPS: %F | %R | %V | %T [%t]'),text,flags=re.M)
    config.write_text(text, encoding='utf-8')
    if not args.configure:
        if args.server and args.trust_ca is None:
            args.trust_ca, fingerprint = fetch_certificate(endpoints, args.cert_cache, args.refresh_server_cert)
            print(f'Server certificate ready: {host} (SHA256 {fingerprint}).', flush=True)
        elif args.trust_ca is not None:
            from tls_identity import public_modulus
            public_modulus(args.trust_ca)
    if args.prepare_only:
        print(f'Prepared {config} for {host}, game {game_path}; no live emulator or game files changed.')
        return
    executable = runtime_executable(args.rpcs3.resolve())
    env = client_instances.environment(root,client_instances.rpcn_port(args.instance)) if root else client_platform.environment(profile)
    working_directory = executable.parent if client_platform.windows() else None
    env.pop('APPIMAGE_EXTRACT_AND_RUN', None)
    env.pop('APPIMAGE', None)
    if args.configure:
        print('Configure a distinct RPCN account under Configuration > RPCN, and choose this client\'s controller in Pads. Close the GUI before launching FIFA.',flush=True)
        subprocess.run([str(executable),'--config',str(config)],env=env,cwd=working_directory,check=True)
        return
    if root:
        client_instances.validate_rpcn(root)
    # RPCS3 must load the per-launch network configuration during a fresh boot.
    if not root and client_platform.rpcs3_running(executable):
        raise RuntimeError('Close RPCS3 first, then run this launcher again.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+(('-'+args.instance) if root else '')
    logs = HERE / 'logs' / stamp
    logs.mkdir(parents=True, exist_ok=False)
    print('Local FUT login, packs and saved squad ready. CPU match candidate: FUT > Play > Single Player Seasons.', flush=True)
    print('Network logging: '+('full tracing.' if args.trace_network else 'reduced; use --trace-network for full tracing.'), flush=True)
    print('Upgrade rewards: ' + ('original loader retained for research.' if args.keep_upgrade_rewards else 'temporarily using the native unavailable/skip path to continue startup.'), flush=True)
    print(f'Logs: {logs}', flush=True)
    cache = root/'cache/rpcs3' if root else client_platform.log_directory(profile)
    rpcs3_log = cache/'RPCS3.log'
    before_log = log_position(rpcs3_log)
    tty_log = cache/'TTY.log'
    before_tty = log_position(tty_log)
    print('Temporarily enabling local game patches and native diagnostics; patch settings are backed up and restored on exit.', flush=True)
    with client_platform.file_lock(profile/'.fifa19-fut-launch.lock', blocking=False), temporary_patch(profile, logs, skip_upgrade_rewards=not args.keep_upgrade_rewards, trust_ca=args.trust_ca), local_settings(game_path / 'PS3_GAME/USRDIR/cl.ini', endpoints.settings()):
        with (logs / 'probe-console.log').open('w') as console:
            server = None
            game = None
            try:
                if args.server:
                    check_server(endpoints)
                    print(f'Connected to standalone FUT server {host}.', flush=True)
                else:
                    server = subprocess.Popen([sys.executable, str(HERE / 'server.py'), '--logs', str(logs)], stdout=console, stderr=subprocess.STDOUT)
                    for _ in range(50):
                        if server.poll() is not None:
                            raise RuntimeError(f'Local listener failed to start; see {logs / "probe-console.log"}')
                        events = logs / 'events.jsonl'
                        if events.exists() and '"ready"' in events.read_text():
                            break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError('Local listener did not report readiness')
                command = [str(executable), '--no-gui', '--config', str(config), str(game_path)]
                (logs / 'launch.json').write_text(json.dumps({'command': command, 'source_config': str(source), 'server': host, 'standalone_server': bool(args.server), 'instance':args.instance,'rpcn_host_udp_port':client_instances.rpcn_port(args.instance) if root else None, 'ports': [8990, 8099, 42230, 44325], 'udp_ports': [8099], 'skip_upgrade_rewards': not args.keep_upgrade_rewards, 'trace_network': args.trace_network}, indent=2) + '\n')
                with (logs / 'rpcs3-console.log').open('w') as game_console:
                    game = subprocess.Popen(command, env=env, cwd=working_directory, stdout=game_console, stderr=subprocess.STDOUT)
                    print('Try entering Ultimate Team once. Record the message shown, then close the game window to finish.', flush=True)
                    started = time.monotonic()
                    code, stopped = wait_for_game(game, args.stop_file)
                (logs / 'exit.json').write_text(json.dumps({'exit_code': code, 'elapsed_seconds': round(time.monotonic() - started, 2)}) + '\n')
                if not stopped and (code or time.monotonic() - started < 5):
                    raise RuntimeError(f'RPCS3 exited before a useful test (code {code}); see {logs / "rpcs3-console.log"}')
            except KeyboardInterrupt:
                if game and game.poll() is None:
                    game.terminate()
                    try:
                        game.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        game.kill()
                        game.wait()
            finally:
                if server:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                copy_network_log(rpcs3_log, logs / 'rpcs3-network.log', before_log)
                copy_runtime_log(rpcs3_log, logs / 'rpcs3-runtime.log', before_log)
                copy_profile_trace(tty_log, logs / 'profile-trace.log', before_tty)
    print(f'Discovery complete. Network captures: {logs}. Local cl.ini removed.', flush=True)


def wait_for_game(game, stop_file=None):
    """Allow the GUI to stop a game while the launcher still restores its patches."""
    while True:
        if stop_file is not None and stop_file.exists():
            print('Stopping RPCS3 and restoring temporary patches.', flush=True)
            game.terminate()
            try: code = game.wait(timeout=10)
            except subprocess.TimeoutExpired:
                game.kill()
                code = game.wait()
            return code, True
        try: return game.wait(timeout=0.5), False
        except subprocess.TimeoutExpired: pass


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f'FUT discovery: {error}', file=sys.stderr)
        sys.exit(1)
