"""Build public server/client ZIPs from an explicit allowlist; never copy keys/saves."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
SERVER_MODULES = ('server', 'initialize_tls', 'tls_identity', 'kits', 'accounts', 'probe', 'endpoints', 'sessions', 'tdf',
    'blaze', 'identity', 'tls_bridge', 'qos', 'hospitality', 'fut_services', 'fut_inventory',
    'fut_squads', 'fut_club_assets', 'fut_condition', 'fut_leaderboards', 'fut_matches', 'fut_results',
    'fut_seasons', 'fut_tournaments', 'session_recovery', 'fut_online', 'matchmaking', 'peer_games', 'fut_content', 'fut_packs',
    'pow_services', 'pran', 'localization', 'game_reporting', 'server_settings', 'server_plugins', 'plugin_queries', 'card_versions', 'rebalance_primes',
    'admin_cards', 'web_accounts', 'web_packs', 'web_app', 'card_rebalance', 'fut_items', 'fut_contracts', 'player_statistics')
CLIENT_MODULES = ('launch', 'launcher_gui', 'server_trust', 'game_input', 'endpoints', 'certificate_patch', 'tls_identity',
    'profile_trace', 'upgrade_rewards_patch', 'client_platform', 'install_update',
    'client_instances', 'setup_clients')
SERVER_DATA = ('england-pack-players.json', 'standard-pack-players.json', 'standard-england-players.json',
    'home-stadium.json', 'club-stadiums.json', 'club-equipment.json',
    'storepackdescriptions.en_us.xml', 'server-config.example.json', 'web-pack-labels.json', 'support-items.json')


def build(output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    targets = [output/f'fifa19-fut-{kind}.zip' for kind in ('server', 'client')]
    if any(path.exists() for path in targets): raise ValueError('Choose a new output directory; existing bundles are preserved')
    for kind, modules, target in zip(('server', 'client'), (SERVER_MODULES, CLIENT_MODULES), targets):
        files = {name+'.py': (HERE/(name+'.py')).read_bytes() for name in modules}
        files['README.md'] = (HERE/'README.md').read_bytes()
        files['LICENSE'] = (HERE/'LICENSE').read_bytes()
        for name in ('SERVER.md', 'CLIENT.md', 'CONFIG.md', 'PLUGINS.md', 'DEVELOPMENT.md', 'CUSTOM_CARDS.md', 'WEB.md', 'ITEMS.md', 'TOURNAMENTS.md'):
            files['docs/'+name] = (HERE/'docs'/name).read_bytes()
        if kind == 'server':
            for name in ('index.html','app.css','app.js'): files['web/'+name] = (HERE/'web'/name).read_bytes()
            for name in SERVER_DATA: files['data/'+name] = (HERE/'data'/name).read_bytes()
            files['plugins/_example_bonus.py'] = (HERE/'plugins/_example_bonus.py').read_bytes()
            files['plugins/_example_presence.py'] = (HERE/'plugins/_example_presence.py').read_bytes()
        else:
            for name in ('Start FUT Launcher.cmd', 'Start FUT Launcher.sh'):
                files[name] = (HERE/name).read_bytes()
            files['rpcn_port.c'] = (HERE/'rpcn_port.c').read_bytes()
            files['cl.ini'] = (HERE/'cl.ini').read_bytes()
            files['data/client-native-patch.json'] = (HERE/'data/client-native-patch.json').read_bytes()
        manifest = {'format_version': 1, 'kind': kind, 'title': 'BLES02258', 'game_version': '01.02',
                    'includes_saved_club': False, 'includes_private_keys': False,
                    'sha256': {name: hashlib.sha256(value).hexdigest() for name,value in sorted(files.items())}}
        files['manifest.json'] = (json.dumps(manifest, indent=2)+'\n').encode()
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, value in sorted(files.items()): archive.writestr(f'fifa19-fut-{kind}/'+name, value)
    return targets


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE/'dist')
    for path in build(parser.parse_args().output): print(path)
