"""Separate card versions, immutable owned snapshots and unchanged base player cards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import card_versions
import fut_inventory
import fut_matches
import fut_packs
import fut_results
import fut_squads
from plugin_queries import Queries
from server_settings import Settings, ConfigWatcher
from sessions import Backend, Session
from test_project import club


class CardVersionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.settings = Settings({'economy': {'starting_coins': 200000}})
        self.backend = Backend(self.path/'profile.json', settings=self.settings)

    def variant(self, asset=20801, edition='prime', item_id=2, pile='club'):
        player, snapshot = card_versions.catalogue_player(self.settings, edition, asset)
        return {'id': item_id, 'assetId': asset, 'edition': edition, 'variant': snapshot, 'pile': pile, 'timestamp': 1}

    def own_base_and_prime(self):
        state = deepcopy(self.backend.fut_inventory.state)
        state['items'] = [{'id': 1, 'assetId': 20801, 'edition': 'standard', 'pile': 'club', 'timestamp': 1}, self.variant()]
        state['nextItemId'] = 3
        self.backend.fut_inventory.commit(state)

    def test_selected_stars_include_neuer_and_keep_base_data_unmodified(self):
        before = deepcopy(fut_packs.STANDARD_PLAYERS)
        for asset in card_versions.STARTERS:
            record = self.variant(asset)
            card = fut_inventory.Inventory.card(record, self.settings)
            self.assertEqual(card['assetId'], asset)
            self.assertEqual(card['resourceId'] & 0xffffff, asset)
            self.assertEqual(card['resourceId'] >> 24, 1)
            self.assertTrue(102 <= card['rating'] <= 115)
            self.assertEqual(card['rareflag'], 3)
            self.assertEqual(len(card['attributeList']), 6)
        neuer = fut_inventory.Inventory.card(self.variant(167495), self.settings)
        self.assertEqual(neuer['preferredPosition'], 'GK')
        self.assertEqual(neuer['rating'], 110)
        self.assertEqual([a['value'] for a in neuer['attributeList']], [111, 108, 111, 108, 77, 108])
        base = fut_inventory.Inventory.card({'id': 1, 'assetId': 20801, 'edition': 'standard', 'timestamp': 1})
        self.assertEqual(base['rating'], 94)
        self.assertEqual(base['resourceId'], 20801)
        self.assertEqual(fut_packs.STANDARD_PLAYERS, before)

    def test_base_and_prime_can_coexist_in_club_but_exact_duplicates_remain_unassigned(self):
        self.own_base_and_prime()
        store = self.backend.fut_inventory
        with patch.object(fut_packs.random.SystemRandom, 'sample', return_value=[20801]):
            purchase = store.open_pack({'packId': 180, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'})
        item = purchase['itemList'][0]['id']
        self.assertEqual(purchase['duplicateItemIdList'][0]['duplicateItemId'], 2)
        moved = store.move({'itemData': [{'id': item, 'pile': 'club'}]})
        self.assertFalse(moved['itemData'][0]['success'])
        self.assertEqual({r['id'] for r in store.state['items'] if r['pile'] == 'club'}, {1, 2})
        store.discard({'itemId': [item]})
        self.assertIsNone(store.state['pendingPack'])
        self.assertEqual(store.duplicates([{'id': 99, 'assetId': 20801, 'edition': 'standard'}])[0]['duplicateItemId'], 1)

    def test_prime_purchase_moves_alongside_base_and_survives_restart_and_rule_changes(self):
        store = self.backend.fut_inventory
        state = deepcopy(store.state)
        state['items'] = [{'id': 1, 'assetId': 20801, 'edition': 'standard', 'pile': 'club', 'timestamp': 1}]
        state['nextItemId'] = 2; store.commit(state)
        with patch.object(fut_packs.random.SystemRandom, 'sample', return_value=[20801]):
            purchase = store.open_pack({'packId': 180, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'})
        self.assertEqual(purchase['duplicateItemIdList'], [])
        self.assertTrue(store.move({'itemData': [{'id': 2, 'pile': 'club'}]})['itemData'][0]['success'])
        coins = store.state['coins']
        self.settings.replace(Settings({'custom_cards': {'editions': {'prime': {'players': {'20801': {'rating': 102}}}}}}))
        self.assertEqual(store.card(store.state['items'][1], self.settings)['rating'], 115)
        new, _ = card_versions.catalogue_player(self.settings, 'prime', 20801)
        self.assertEqual(new['rating'], 102)
        restored = Backend(self.backend.path, settings=Settings({'custom_cards': {'enabled': False}}))
        self.assertEqual(restored.fut_inventory.state['coins'], coins)
        self.assertEqual([restored.fut_inventory.card(r, restored.settings)['rating'] for r in restored.fut_inventory.state['items']], [94, 115])

    def test_one_version_per_squad_and_versions_in_separate_squads_are_valid(self):
        self.own_base_and_prime()
        squad = fut_squads.empty('Base XI')
        squad['players'][9]['itemData']['id'] = 1
        fut_squads.save(self.backend, squad)
        invalid = deepcopy(squad); invalid['players'][10]['itemData']['id'] = 2
        before = deepcopy(self.backend.fut_inventory.state)
        with self.assertRaisesRegex(ValueError, 'one version'): fut_squads.save(self.backend, invalid)
        self.assertEqual(self.backend.fut_inventory.state, before)
        made = fut_squads.create(self.backend, {'squadName': 'Prime XI'})
        second = fut_squads.empty('Prime XI', made['id'])
        second['players'][9]['itemData']['id'] = 2
        fut_squads.save(self.backend, second)
        fut_squads.activate(self.backend, second['id'])
        session = Session(self.backend.profile['persona'], self.backend.profile['account'], 'Synthetic', 'test', 0, 0)
        retrieved = fut_squads.retrieve(self.backend, session)
        prime = next(p['itemData'] for p in retrieved['players'] if p['index'] == 9)
        self.assertEqual((prime['assetId'], prime['resourceId'], prime['rating']), (20801, (1 << 24) | 20801, 115))

    def test_custom_card_is_selectable_for_cpu_match_and_commits_a_normal_result(self):
        session = club(self.backend)
        store = self.backend.fut_inventory
        state = deepcopy(store.state); item_id = state['nextItemId']
        state['items'].append(self.variant(item_id=item_id)); state['nextItemId'] += 1
        previous = state['squad']['players'][9]['itemData']['id']
        state['squad']['players'][9]['itemData']['id'] = item_id
        for kick in state['squad']['kicktakers']:
            if kick['id'] == previous: kick['id'] = item_id
        store.commit(state)
        fut_matches.create(self.backend, session, {'type': 'OFFLINE', 'squadId': 1})
        current = session.local_fut_match
        self.assertEqual(current['squad']['players'][9]['itemData']['rating'], 115)
        ids = [p['itemData']['id'] for p in current['squad']['players'] if p['index'] < 11]
        fut_matches.play(self.backend, session, {'items': [{'id': i} for i in ids]})
        report = {'matchReportId': 0, 'endReason': 'WIN', 'secondsPlayed': 5400,
                  'items': [{'id': i} for i in ids], 'matchData': '',
                  'myMatchStats': {k: 0 for k in fut_results.STATS},
                  'opponentMatchStats': {k: 0 for k in fut_results.STATS}}
        report['myMatchStats']['goals'] = 1
        awarded = fut_results.finish(self.backend, session, report)
        self.assertEqual(awarded['allCoins'], 1000)
        self.assertEqual(fut_results.finish(self.backend, session, report), awarded)
        self.assertEqual(Backend(self.backend.path, settings=self.settings).fut_inventory.state, store.state)

    def test_queries_and_exact_resource_search_distinguish_versions(self):
        self.own_base_and_prime()
        api = Queries(self.backend); persona = self.backend.profile['persona']
        cards = api.get_cards(persona)
        self.assertEqual({c['edition'] for c in cards}, {'standard', 'prime'})
        prime = api.get_cards(persona, edition='prime', min_rating=111, max_rating=115)[0]
        self.assertEqual(prime['version'], 1)
        self.assertEqual(prime['edition_label'], 'Prime')
        self.assertEqual(api.get_catalogue_card(167495, edition='prime')['position'], 'GK')
        self.assertEqual(len(api.search_catalogue(edition='prime')), len(card_versions.STARTERS))
        prime['attributes'][0]['value'] = 0
        self.assertGreater(api.get_card(persona, 2)['attributes'][0]['value'], 0)
        store = self.backend.fut_inventory
        self.assertEqual(len(fut_inventory.club_search(store, {'defId': ['20801'], 'filter': ['base']})['itemData']), 2)
        result = fut_inventory.club_search(store, {'defId': [str((1 << 24) | 20801)], 'filter': ['exact']})['itemData']
        self.assertEqual([c['id'] for c in result], [2])

    def test_live_custom_definitions_disable_packs_without_orphaning_owned_cards(self):
        self.own_base_and_prime()
        path = self.path/'server-config.json'
        path.write_text(json.dumps({'custom_cards': {'enabled': False}}))
        times = [0]
        watcher = ConfigWatcher(self.backend, path, clock=lambda: times[0])
        watcher.poll(); times[0] = 1; self.assertTrue(watcher.poll())
        offers = {o['id'] for o in self.backend.fut_inventory.offers()['purchase']}
        self.assertNotIn(180, offers)
        self.assertNotIn(181, offers)
        with self.assertRaises(fut_inventory.Conflict):
            self.backend.fut_inventory.open_pack({'packId': 180, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'})
        self.assertEqual(Queries(self.backend).get_card(self.backend.profile['persona'], 2)['rating'], 115)
        self.assertFalse(any(p['pack_id'] == 180 for p in Queries(self.backend).get_packs()))

    def test_normal_packs_stay_stock_by_default_and_optional_mixed_versions_preserve_player_ids(self):
        standard = fut_packs.STANDARD_PLAYERS[20801]
        self.assertEqual(card_versions.upgrade_standard_draw([standard], self.settings, object()), [standard])
        settings = Settings({'custom_cards': {'standard_pack_chance_percent': 100}})
        import random
        chosen = card_versions.upgrade_standard_draw([standard], settings, random.SystemRandom())[0]
        self.assertEqual(chosen['assetId'], standard['assetId'])
        self.assertEqual(chosen['_edition'], 'prime')
        self.assertEqual(chosen['rating'], 115)
        self.assertEqual(standard['rating'], 94)

    def test_invalid_definitions_and_saved_snapshots_are_rejected(self):
        cases = [{'standard_pack_chance_percent': True}, {'editions': {'prime': {'version': 128}}},
                 {'editions': {'prime': {'rareflag': 255}}}, {'editions': {'standard': {'version': 2}}},
                 {'editions': {'flashback': {'version': 1, 'players': {'20801': {'rating': 99}}}}},
                 {'editions': {'prime': {'players': {'20801': {'rating': 101}}}}},
                 {'editions': {'prime': {'players': {'20801': {'rating': 116}}}}},
                 {'editions': {'prime': {'players': {'20801': {'position': []}}}}},
                 {'editions': {'prime': {'players': {'20801': {'attributes': [116]*6}}}}},
                 {'editions': {'prime': {'players': {'9999999': {'rating': 110}}}}}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError): Settings({'custom_cards': value})
        self.own_base_and_prime()
        for key, value in (('rating', 116), ('version', 128), ('attributes', [True]*6), ('position', [])):
            state = deepcopy(self.backend.fut_inventory.state); state['items'][1]['variant'][key] = value
            with self.assertRaises(ValueError): self.backend.fut_inventory.validate(state)

    def test_additional_editions_and_neuer_attribute_overrides_use_distinct_native_ids(self):
        settings = Settings({'custom_cards': {'editions': {'prime': {'players': {'167495': {'attributes': [115, 110, 113, 115, 88, 114]}}},
                                                          'flashback': {'label': 'Flashback', 'version': 2,
                                                                        'players': {'20801': {'rating': 102, 'position': 'RW'}}}}}})
        player, _ = card_versions.catalogue_player(settings, 'prime', 167495)
        self.assertEqual([a['value'] for a in player['attributeList']], [115, 110, 113, 115, 88, 114])
        player, snapshot = card_versions.catalogue_player(settings, 'flashback', 20801)
        self.assertEqual(player['preferredPosition'], 'RW')
        record = {'assetId': 20801, 'edition': 'flashback', 'variant': snapshot}
        self.assertEqual(card_versions.resource_id(record), (2 << 24) | 20801)
        self.assertNotEqual(card_versions.resource_id(record), card_versions.resource_id(self.variant()))


if __name__ == '__main__': unittest.main()
