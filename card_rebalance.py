"""Apply live custom-card definitions to owned cards with private rollback copies."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from card_versions import BASE_EDITIONS, edition_variant


def updated_state(state, editions):
    """Refresh issued stats, retaining ownership, native identity and receipts."""
    from fut_inventory import Inventory
    Inventory.validate(state)
    updated = deepcopy(state)
    from player_statistics import backfill
    changed = backfill(updated)
    for record in updated['items']:
        edition = record.get('edition', 'england110')
        if edition in BASE_EDITIONS or edition not in editions: continue
        variant = edition_variant(editions[edition], record['assetId'])
        # Edition version changes only affect new issues. Existing resource IDs
        # must remain stable for squad references, duplicates and match reports.
        variant['version'] = record['variant']['version']
        if variant != record['variant']:
            record['variant'] = variant
            changed.add(record['id'])
    Inventory.validate(updated)
    return updated, len(changed)


def _stage(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.custom-cards-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def reconcile(backend, settings):
    """Update loaded and offline inventories under the server's shared lock.

    Call on startup while holding StateLock, or before swapping live Settings.
    Validate/stage every change before publishing any; memory changes last.
    """
    root = backend.root
    with root.lock:
        editions = settings.section('custom_cards')['editions']
        stores = {id(owner.fut_inventory): owner.fut_inventory
                  for owner in [root, *root.users.values()]}
        paths = {store.path: store for store in stores.values() if store.path is not None}
        if root.path is not None:
            directory = root.path.parent
            for path in (directory/'users').glob('*/fut-inventory.json'):
                if re.fullmatch(r'[0-9a-f]{64}', path.parent.name): paths.setdefault(path, None)
        changes = []
        for path, store in [*paths.items(), *((None, s) for s in stores.values() if s.path is None)]:
            if path is not None and (path.is_symlink() or path.parent.is_symlink()):
                raise ValueError('Refusing linked custom-card inventory')
            original = path.read_bytes() if path is not None and path.exists() else None
            if store is None and original is None: continue
            state = store.state if store is not None else json.loads(original)
            updated, count = updated_state(state, editions)
            if count: changes.append((path, store, updated, count, original))
        result = {'cards': sum(row[3] for row in changes), 'clubs': len(changes), 'backup': None}
        if not changes: return result
        backup = None
        if any(row[4] is not None for row in changes):
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            backup = root.path.parent.parent/'state-backups'/('custom-cards-'+stamp)
            backup.mkdir(parents=True, mode=0o700)
            result['backup'] = str(backup)
        staged, replaced = [], []
        try:
            for path, store, updated, count, original in changes:
                if path is None: continue
                if original is not None:
                    saved = backup/path.relative_to(root.path.parent)
                    saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with saved.open('xb') as stream: stream.write(original)
                    saved.chmod(0o600)
                content = (json.dumps(updated, indent=2)+'\n').encode('utf-8')
                mode = path.stat().st_mode & 0o777 if original is not None else 0o600
                staged.append((_stage(path, content, mode), path, original, mode))
            for temporary, path, original, mode in staged:
                temporary.replace(path)
                replaced.append((path, original, mode))
        except BaseException:
            failures = []
            for path, original, mode in reversed(replaced):
                try:
                    if original is None: path.unlink(missing_ok=True)
                    else:
                        temporary = _stage(path, original, mode)
                        try: temporary.replace(path)
                        finally: temporary.unlink(missing_ok=True)
                except OSError: failures.append(path)
            if failures:
                raise RuntimeError(f'Custom-card rollback needs recovery from {backup}')
            raise
        finally:
            for temporary, *_ in staged: temporary.unlink(missing_ok=True)
        for path, store, updated, count, original in changes:
            if store is not None: store.state = updated
        return result
