#!/usr/bin/env python3
"""List installed FUT kit colours and select a user's home or away kit."""
import argparse
import json
from pathlib import Path

import fut_club_assets as assets
from accounts import validate_registry
from server import HERE, StateLock
from sessions import Backend


def colour(rgb):
    return '#'+''.join(f'{n:02x}' for n in rgb)


def user_profile(state_dir, number):
    path = state_dir/'accounts.json'
    if not path.exists():
        if number != 0:
            raise ValueError('This user has not logged in yet')
        return state_dir/'profile.json'
    registry = json.loads(path.read_text())
    validate_registry(registry)
    # User numbers are local registry indexes, never RPCN IDs or credentials.
    for key, row in registry['users'].items():
        if row['number'] == number:
            return state_dir/'profile.json' if row['legacy'] else state_dir/'users'/key/'profile.json'
    raise ValueError('This user has not logged in yet')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='Show kit IDs, names and primary/secondary colours')
    parser.add_argument('--filter', default='', help='Filter kit labels, for example Liverpool or England')
    parser.add_argument('--state-dir', type=Path, default=HERE/'state')
    parser.add_argument('--user', type=int, default=0, help='Local user number; 0 is the original club')
    parser.add_argument('--slot', choices=('home','away'))
    parser.add_argument('--kit', type=int, help='Kit ID from --list; stop the server before selecting')
    args = parser.parse_args(argv)
    if args.list:
        for card in assets.EQUIPMENT:
            if card['itemType'] == 'kit' and args.filter.casefold() in card['label'].casefold():
                colours = card['colours']
                print(f"{card['id']}  {card['label']:<34}  {colour(colours['primary'])}  {colour(colours['secondary'])}")
        return
    if args.slot is None or args.kit is None or args.user < 0:
        parser.error('Use --list, or supply --slot home|away --kit ID and optional --user NUMBER')
    card = assets.CARDS.get(args.kit)
    if card is None or card['itemType'] != 'kit':
        parser.error('Unknown kit ID; use --list')
    with StateLock(args.state_dir):
        owner = Backend(user_profile(args.state_dir, args.user))
        assets.activate(owner, args.kit, {'itemState': 'active',
                        'activateSlotNumber': '101' if args.slot == 'home' else '102'})
    print(f"User {args.user} {args.slot} kit: {card['label']}. Restart the server and relaunch FIFA.")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(f'FUT kits: {error}')
