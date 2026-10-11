"""Stable RPCN identity -> private saved club. Never key saves by a client IP."""
from copy import deepcopy
import hashlib
import json
import re
import shutil


def user_key(external):
    if type(external) is not int or not 0 < external < 2**64:
        raise ValueError('A nonzero RPCN player identity is required')
    return hashlib.sha256(b'fifa19-rpcn:' + str(external).encode()).hexdigest()


def validate_registry(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'users'}
            or value['version'] != 1 or not isinstance(value['users'], dict)):
        raise ValueError('Invalid player registry')
    ids = set()
    for key, row in value['users'].items():
        if (not re.fullmatch(r'[0-9a-f]{64}', key)
                or not isinstance(row, dict) or set(row) != {'number', 'legacy'}
                or type(row['number']) is not int or not 0 <= row['number'] < 1000000
                or type(row['legacy']) is not bool or row['legacy'] != (row['number'] == 0)
                or row['number'] in ids):
            raise ValueError('Invalid saved player mapping')
        ids.add(row['number'])
    if ids and ids != set(range(len(ids))):
        raise ValueError('Invalid player sequence')


def user_backend(root, external):
    from sessions import Backend
    key = user_key(external)
    with root.lock:
        if key in root.users:
            return root.users[key]
        row = root.accounts['users'].get(key)
        if row is None:
            number = len(root.accounts['users'])
            row = {'number': number, 'legacy': number == 0}
        if row['legacy']:
            owner = root
            if root.path and not root.accounts['users']:
                # Retain untouched copies before claiming the pre-user club.
                backup = root.path.parent/'before-per-user'
                backup.mkdir(parents=True, exist_ok=True)
                for name in ('profile.json', 'fut-inventory.json', 'pran.json'):
                    source = root.path.with_name(name)
                    if source.is_file() and not (backup/name).exists():
                        shutil.copy2(source, backup/name)
        else:
            path = root.path.parent/'users'/key/'profile.json' if root.path else None
            owner = Backend(path, root.endpoints, settings=root.settings, events=root.events)
            owner.root = root
            owner.sessions = root.sessions
            owner.identity = root.identity
            owner.localizer = root.localizer
            owner.lock = root.lock
            owner.fut_inventory.lock = root.lock
            if path is None or not path.exists():
                owner.profile.update(persona=root.profile['persona']+row['number'],
                                     account=root.profile['account']+row['number'],
                                     name='Local Player '+str(row['number']+1))
                owner.save()
                owner.fut_inventory.ensure_saved()
            if (owner.profile['persona'] != root.profile['persona']+row['number']
                    or owner.profile['account'] != root.profile['account']+row['number']):
                raise ValueError('Saved player identity differs from registry')
        # Write profile first: an interrupted registration can safely retry.
        if key not in root.accounts['users']:
            registry = deepcopy(root.accounts)
            registry['users'][key] = row
            validate_registry(registry)
            if root.accounts_path:
                root.accounts_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = root.accounts_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(registry, indent=2)+'\n')
                temporary.replace(root.accounts_path)
            root.accounts = registry
        root.users[key] = owner
        return owner
