"""Explicit, backed-up rebalance of existing Prime cards to stock +15 stats."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from fut_inventory import Inventory
from fut_packs import STANDARD_PLAYERS
from server import StateLock


def rebalance(state):
    """Return a validated copy; only Prime OVR and six face attributes change."""
    Inventory.validate(state)
    updated = deepcopy(state)
    count = 0
    for record in updated['items']:
        if record.get('edition') != 'prime': continue
        base = STANDARD_PLAYERS[record['assetId']]
        variant = record['variant']
        rating = min(115, base['rating']+15)
        attributes = [min(115, row['value']+15) for row in base['attributeList']]
        if variant['rating'] != rating or variant['attributes'] != attributes:
            variant.update(rating=rating, attributes=attributes)
            count += 1
    Inventory.validate(updated)
    return updated, count


def migrate(directory, *, apply=False):
    directory = Path(directory).resolve()
    with StateLock(directory):
        paths = [directory/'fut-inventory.json', *sorted((directory/'users').glob('*/fut-inventory.json'))]
        changes = []
        for path in paths:
            if not path.is_file(): continue
            state, count = rebalance(json.loads(path.read_text(encoding='utf-8')))
            if count: changes.append((path, state, count))
        count = sum(count for _, _, count in changes)
        if not apply or not changes: return count, None
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        backup = directory.parent/'state-backups'/('prime-plus15-'+stamp)
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(directory, backup)
        backup.chmod(0o700)
        # A backup represents stopped state, not an active server lock.
        (backup/'server.lock').unlink()
        written = []
        try:
            for path, state, _ in changes:
                temporary = path.with_suffix('.prime-tmp')
                try:
                    temporary.write_text(json.dumps(state, indent=2)+'\n', encoding='utf-8')
                    temporary.chmod(path.stat().st_mode & 0o777)
                    temporary.replace(path)
                    written.append(path)
                finally:
                    temporary.unlink(missing_ok=True)
        except BaseException:
            for path in written: shutil.copy2(backup/path.relative_to(directory), path)
            raise
        return count, backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True, help='Stopped server state directory')
    parser.add_argument('--apply', action='store_true', help='Back up state and write changes; default is preview only')
    args = parser.parse_args()
    if not args.state_dir.is_dir(): parser.error('State directory does not exist')
    count, backup = migrate(args.state_dir, apply=args.apply)
    print(f'Prime cards {"updated" if args.apply else "to update"}: {count}')
    if backup: print(f'Full state backup: {backup}')


if __name__ == '__main__': main()
