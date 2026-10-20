"""Live edition edits update online/offline ownership without resetting saves."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import card_rebalance
import card_versions
import fut_inventory
import fut_matches
import fut_squads
from plugin_queries import Queries
from server_settings import ConfigWatcher, Settings
from sessions import Backend
from test_project import club


BUY = {'packId': 180, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'}


class CardRebalanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.root = Backend(self.path/'state/profile.json', multiuser=True,
                            settings=Settings({'economy': {'starting_coins': 200000}}))
        self.root.for_user(10)
        self.loaded = self.root.for_user(20)
        self.offline = self.root.for_user(30)
        self.session = club(self.root)
        state = deepcopy(self.root.fut_inventory.state)
        _, variant = card_versions.catalogue_player(self.root.settings, 'prime', 20801)
        item_id = state['nextItemId']; state['nextItemId'] += 1
        state['items'].append({'id': item_id, 'assetId': 20801, 'edition': 'prime',
                               'variant': variant, 'pile': 'club', 'timestamp': 1,
                               'injuryGames': 2, 'injuryType': 'leg', 'suspension': 1})
        previous = state['squad']['players'][9]['itemData']['id']
        state['squad']['players'][9]['itemData']['id'] = item_id
        for kick in state['squad']['kicktakers']:
            if kick['id'] == previous: kick['id'] = item_id
        self.root.fut_inventory.commit(state)
        self.item_id = item_id
        for owner in (self.loaded, self.offline):
            with patch.dict(__import__('fut_packs').PACKS[180], retired=False), patch('fut_packs.random.SystemRandom.sample', return_value=[20801]):
                owner.fut_inventory.open_pack(BUY)
        for key, owner in list(self.root.users.items()):
            if owner is self.offline: self.root.users.pop(key)
        self.config = self.path/'config.json'
        self.now = 0
        self.logs = []
        self.watcher = ConfigWatcher(self.root, self.config, self.logs.append, clock=lambda: self.now)

    def apply(self, spec):
        self.config.write_text(json.dumps({'economy': {'starting_coins': 200000},
                                          'custom_cards': {'editions': {'prime': spec}}}))
        self.watcher.poll(); self.now += 1
        return self.watcher.poll()

    def states(self):
        return {owner.fut_inventory.path: deepcopy(owner.fut_inventory.state)
                for owner in (self.root, self.loaded, self.offline)}

    def test_live_update_rewrites_owned_and_pending_cards_in_loaded_and_offline_clubs(self):
        before = self.states()
        raw = {path: path.read_bytes() for path in before}
        self.assertTrue(self.apply({'rating_boost': 10, 'attribute_boost': 25,
                                   'discard_value': 7000, 'label': 'Prime updated'}))
        events = [row for row in self.logs if row['event'] == 'custom_cards.updated']
        self.assertEqual((events[0]['cards'], events[0]['clubs']), (3, 3))
        backup = Path(events[0]['backup'])
        if os.name != 'nt': self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        for owner in (self.root, self.loaded, self.offline):
            store = owner.fut_inventory
            actual = json.loads(store.path.read_text())
            expected = deepcopy(before[store.path])
            for record in expected['items']:
                if record.get('edition') == 'prime':
                    record['variant'].update(rating=104, attributes=[115, 118, 106, 114, 60, 104],
                                             discard_value=7000, label='Prime updated')
            self.assertEqual(actual, expected)
            self.assertEqual((backup/store.path.relative_to(self.root.path.parent)).read_bytes(), raw[store.path])
            if owner is not self.offline: self.assertEqual(store.state, expected)
        self.assertEqual(Queries(self.root).get_card(self.root.profile['persona'], self.item_id)['rating'], 104)
        retrieved = fut_squads.retrieve(self.root, self.session)
        self.assertEqual(retrieved['players'][9]['itemData']['rating'], 104)
        pending = self.loaded.fut_inventory.open_pack(BUY)
        self.assertEqual(pending['itemList'][0]['rating'], 104)
        self.assertEqual(self.loaded.fut_inventory.state['coins'], before[self.loaded.fut_inventory.path]['coins'])
        restored = Backend(self.offline.path, settings=self.root.settings)
        self.assertEqual(restored.fut_inventory.purchased()['itemData'][0]['rating'], 104)

    def test_startup_reconciliation_catches_edits_while_stopped_and_is_idempotent(self):
        settings = Settings({'custom_cards': {'editions': {'prime': {'attribute_boost': 25}}}})
        result = card_rebalance.reconcile(self.root, settings)
        self.assertEqual((result['cards'], result['clubs']), (3, 3))
        self.assertEqual(self.root.fut_inventory.state['items'][-1]['variant']['attributes'][0], 115)
        before = {path: path.read_bytes() for path in self.states()}
        backups = list((self.path/'state-backups').iterdir())
        self.assertEqual(card_rebalance.reconcile(self.root, settings), {'cards': 0, 'clubs': 0, 'backup': None})
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(list((self.path/'state-backups').iterdir()), backups)

    def test_disabled_and_excluded_owners_still_update_and_issued_resource_id_is_stable(self):
        identity = card_versions.resource_id(self.root.fut_inventory.state['items'][-1])
        self.assertTrue(self.apply({'enabled': False, 'all_players': False, 'version': 10,
                                   'rating_boost': 1, 'attribute_boost': 1,
                                   'players': {'20801': {'enabled': False, 'rating': 112,
                                                        'attributes': [110]*6, 'position': 'RW'}}}))
        record = self.root.fut_inventory.state['items'][-1]
        self.assertEqual(card_versions.resource_id(record), identity)
        self.assertEqual(record['variant']['version'], 1)
        self.assertEqual(record['variant']['rating'], 112)
        self.assertEqual(record['variant']['attributes'], [110]*6)
        self.assertEqual(record['variant']['position'], 'RW')
        self.assertTrue(self.root.settings.pack(180)['retired'])

    def test_active_match_snapshot_and_non_card_history_stay_unchanged(self):
        # Permit this injured player for the synthetic match setup, then retain
        # its captured squad while club data changes for the next match.
        state = deepcopy(self.root.fut_inventory.state)
        state['items'][-1].update(injuryGames=0, injuryType='none', suspension=0)
        self.root.fut_inventory.commit(state)
        fut_matches.create(self.root, self.session, {'type': 'OFFLINE', 'squadId': 1})
        match = deepcopy(self.session.local_fut_match)
        self.assertTrue(self.apply({'rating_boost': 5}))
        self.assertEqual(self.session.local_fut_match, match)
        self.assertEqual(self.root.fut_inventory.state['items'][-1]['variant']['rating'], 99)
        # Legacy/unknown custom editions remain readable using their snapshot.
        state = deepcopy(self.root.fut_inventory.state)
        record = state['items'][-1]; record['edition'] = 'retired'; record['variant']['version'] = 9
        updated, count = card_rebalance.updated_state(state, self.root.settings.section('custom_cards')['editions'])
        self.assertEqual((updated, count), (state, 0))

    def test_invalid_offline_inventory_rejects_the_edit_without_touching_other_clubs(self):
        before = {path: path.read_bytes() for path in self.states()}
        self.offline.fut_inventory.path.write_text('{bad json')
        previous = self.root.settings.as_dict()
        self.assertFalse(self.apply({'rating_boost': 5}))
        self.assertEqual(self.root.settings.as_dict(), previous)
        self.assertEqual(self.watcher.generation, 0)
        self.assertEqual(self.watcher.last_error_type, 'JSONDecodeError')
        for owner in (self.root, self.loaded):
            self.assertEqual(owner.fut_inventory.path.read_bytes(), before[owner.fut_inventory.path])

    def test_failed_publish_rolls_back_disk_and_keeps_memory_and_rules(self):
        before = self.states()
        raw = {path: path.read_bytes() for path in before}
        previous = self.root.settings.as_dict()
        original = Path.replace
        failed = []
        def replace(path, target):
            if Path(target) == self.loaded.fut_inventory.path and not failed:
                failed.append(True)
                raise OSError('Synthetic disk failure')
            return original(path, target)
        with patch.object(Path, 'replace', new=replace):
            self.assertFalse(self.apply({'rating_boost': 5}))
        self.assertTrue(failed)
        self.assertEqual(self.root.settings.as_dict(), previous)
        self.assertEqual(self.states(), before)
        self.assertEqual({path: path.read_bytes() for path in raw}, raw)
        self.assertFalse(list((self.path/'state').rglob('.custom-cards-*')))
        self.assertEqual(self.watcher.last_error_type, 'OSError')


if __name__ == '__main__': unittest.main()
