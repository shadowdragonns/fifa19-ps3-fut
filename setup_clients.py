#!/usr/bin/env python3
"""Create player1/player2 emulator copies for simultaneous testing."""
import argparse
from pathlib import Path
import subprocess

import client_instances

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path,default=Path.home()/'.config/rpcs3')
    parser.add_argument('--game',type=Path,default=HERE.parents[1])
    parser.add_argument('--cache',type=Path,default=Path.home()/'.cache/rpcs3')
    args=parser.parse_args()
    adapter=client_instances.port_adapter()
    print('RPCN port adapter ready: '+str(adapter),flush=True)
    for name in ('player1','player2'):
        print('Preparing '+name+' with independent emulator and game files...',flush=True)
        root=client_instances.prepare(name,args.profile.expanduser().resolve(),
            args.game.expanduser().resolve(),copy_rpcn=name=='player1',
            source_cache=args.cache.expanduser().resolve())
        print('Ready: '+str(root),flush=True)
    print('Player 1 retains your RPCN account. Configure a different RPCN account for player 2 with:')
    print('python3 '+str(HERE/'launch.py')+' --instance player2 --configure')


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:
        raise SystemExit(f'FUT clients: {error}')
