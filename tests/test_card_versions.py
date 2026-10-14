"""Separate card versions, immutable owned snapshots and unchanged base player cards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import card_versions
import rebalance_primes
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

    def test_every_player_has_exact_plus15_prime_stats_and_base_data_is_unmodified(self):
        before = deepcopy(fut_packs.STANDARD_PLAYERS)
        self.assertEqual(set(card_versions.catalogue_assets(self.settings, 'prime', available=True)), set(before))
        for asset, base in before.items():
            player, snapshot = card_versions.catalogue_player(self.settings, 'prime', asset)
            card_versions.validate_variant('prime', asset, snapshot)
            self.assertEqual(player['rating'], base['rating']+15)
            self.assertEqual([a['value'] for a in player['attributeList']],
                             [a['value']+15 for a in base['attributeList']])
            self.assertEqual(player['preferredPosition'], base['preferredPosition'])
        neuer = fut_inventory.Inventory.card(self.variant(167495), self.settings)
        self.assertEqual(neuer['preferredPosition'], 'GK')
        self.assertEqual(neuer['rating'], 105)
        self.assertEqual([a['value'] for a in neuer['attributeList']], [106, 103, 106, 103, 72, 103])
        self.assertEqual(neuer['resourceId'], (1 << 24) | 167495)
        self.assertEqual(neuer['rareflag'], 3)
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
        self.assertEqual(store.card(store.state['items'][1], self.settings)['rating'], 109)
        new, _ = card_versions.catalogue_player(self.settings, 'prime', 20801)
        self.assertEqual(new['rating'], 102)
        restored = Backend(self.backend.path, settings=Settings({'custom_cards': {'enabled': False}}))
        self.assertEqual(restored.fut_inventory.state['coins'], coins)
        self.assertEqual([restored.fut_inventory.card(r, restored.settings)['rating'] for r in restored.fut_inventory.state['items']], [94, 109])

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
        self.assertEqual((prime['assetId'], prime['resourceId'], prime['rating']), (20801, (1 << 24) | 20801, 109))

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
        self.assertEqual(current['squad']['players'][9]['itemData']['rating'], 109)
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
        prime = api.get_cards(persona, edition='prime', min_rating=105, max_rating=115)[0]
        self.assertEqual(prime['version'], 1)
        self.assertEqual(prime['edition_label'], 'Prime')
        self.assertEqual(api.get_catalogue_card(167495, edition='prime')['position'], 'GK')
        self.assertEqual(len(api.search_catalogue(edition='prime')), 100)
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
        self.assertEqual(Queries(self.backend).get_card(self.backend.profile['persona'], 2)['rating'], 109)
        self.assertFalse(any(p['pack_id'] == 180 for p in Queries(self.backend).get_packs()))

    def test_normal_gold_prime_odds_are_exact_and_can_be_disabled(self):
        from unittest.mock import Mock
        standard = fut_packs.STANDARD_PLAYERS[20801]
        rng = Mock()
        rng.choice.side_effect = lambda choices: choices[0]
        rng.randrange.return_value = 0
        chosen = card_versions.upgrade_standard_draw([standard], self.settings, rng)[0]
        rng.randrange.assert_called_once_with(100000)
        self.assertEqual((chosen['assetId'], chosen['_edition'], chosen['rating']), (20801, 'prime', 109))
        rng.randrange.return_value = 1
        self.assertEqual(card_versions.upgrade_standard_draw([standard], self.settings, rng), [standard])
        for value in (1, 99999):
            rng.randrange.return_value = value
            self.assertNotIn('_edition', card_versions.upgrade_standard_draw([standard], self.settings, rng)[0])
        disabled = Settings({'custom_cards': {'standard_pack_chance_percent': 0}})
        self.assertEqual(card_versions.upgrade_standard_draw([standard], disabled, object()), [standard])
        always = Settings({'custom_cards': {'standard_pack_chance_percent': 100}})
        rng.randrange.return_value = 0
        self.assertEqual(card_versions.upgrade_standard_draw([standard], always, rng)[0]['_edition'], 'prime')
        self.assertEqual(standard['rating'], 94)

    def test_all_gold_pack_types_upgrade_same_player_and_retain_theme_filters(self):
        always = Settings({'custom_cards': {'standard_pack_chance_percent': 100}})
        for pack_id, spec in fut_packs.PACKS.items():
            if spec['edition'] != 'standard': continue
            with self.subTest(pack=pack_id):
                cards = fut_packs.draw(spec, [], settings=always)
                self.assertEqual(len(cards), spec['size'])
                self.assertEqual(len({p['assetId'] for p in cards}), spec['size'])
                for card in cards:
                    base = fut_packs.STANDARD_PLAYERS[card['assetId']]
                    if spec['level'] == 'gold':
                        self.assertEqual(card['_edition'], 'prime')
                        self.assertEqual(card['rating'], base['rating']+15)
                    else: self.assertNotIn('_edition', card)
                    self.assertEqual(fut_packs.level(base), spec['level'])
                    for field in ('nation', 'leagueId'):
                        if field in spec: self.assertEqual(card[field], spec[field])
                    if 'positions' in spec: self.assertIn(card['preferredPosition'], spec['positions'])
                    self.assertGreaterEqual(base['rating'], spec.get('min_rating', 0))
        with patch.object(fut_packs.random.SystemRandom, 'randrange', return_value=0):
            self.assertTrue(all(p['_edition'] == 'prime' for p in fut_packs.draw(fut_packs.PACKS[6], [])))

    def test_low_rated_prime_purchase_persists_and_pending_pack_retry_is_free(self):
        asset = min(fut_packs.STANDARD_PLAYERS, key=lambda a: fut_packs.STANDARD_PLAYERS[a]['rating'])
        store = self.backend.fut_inventory
        payload = {'packId': 180, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'}
        with patch.object(fut_packs.random.SystemRandom, 'sample', return_value=[asset]):
            purchase = store.open_pack(payload)
        self.assertEqual(purchase['itemList'][0]['rating'], 62)
        restored = Backend(self.backend.path, settings=self.settings).fut_inventory
        self.assertEqual(restored.state['coins'], 150000)
        self.assertEqual(restored.open_pack(payload), purchase)
        self.assertEqual(restored.state, store.state)

    def test_all_player_rule_allows_individual_exclusions_and_selected_pools(self):
        disabled = Settings({'custom_cards': {'editions': {'prime': {'players': {'20801': {'enabled': False}}}}}})
        self.assertNotIn(20801, card_versions.catalogue_assets(disabled, 'prime', available=True))
        self.assertIn(20801, card_versions.catalogue_assets(disabled, 'prime'))
        selected = Settings({'custom_cards': {'editions': {'prime': {'all_players': False,
                            'players': {'167495': {'enabled': True}}}}}})
        self.assertEqual(card_versions.catalogue_assets(selected, 'prime', available=True), [167495])
        self.assertTrue(selected.pack(181)['retired'])
        with self.assertRaises(KeyError): card_versions.catalogue_player(selected, 'prime', 20801)

    def test_existing_prime_rebalance_preserves_identity_squads_conditions_and_receipts(self):
        club(self.backend)
        store = self.backend.fut_inventory
        state = deepcopy(store.state)
        item_id = state['nextItemId']
        record = self.variant(item_id=item_id)
        record.update(suspension=1, fitness=80)
        record['variant'].update(rating=115, attributes=[115]*6)
        state['items'].append(record); state['nextItemId'] += 1
        previous = state['squad']['players'][9]['itemData']['id']
        state['squad']['players'][9]['itemData']['id'] = item_id
        for kick in state['squad']['kicktakers']:
            if kick['id'] == previous: kick['id'] = item_id
        store.commit(state)
        updated, count = rebalance_primes.rebalance(state)
        expected = deepcopy(state)
        expected['items'][-1]['variant'].update(rating=109, attributes=[105, 108, 96, 104, 50, 94])
        self.assertEqual((updated, count), (expected, 1))
        self.assertEqual(state['items'][-1]['variant']['rating'], 115)
        state_dir = self.backend.path.parent
        before = store.path.read_bytes()
        self.assertEqual(rebalance_primes.migrate(state_dir), (1, None))
        self.assertEqual(store.path.read_bytes(), before)
        count, backup = rebalance_primes.migrate(state_dir, apply=True)
        self.assertEqual(count, 1)
        self.assertEqual((backup/'fut-inventory.json').read_bytes(), before)
        self.assertEqual(Backend(self.backend.path, settings=self.settings).fut_inventory.state, expected)
        self.assertEqual(rebalance_primes.migrate(state_dir, apply=True), (0, None))
        (state_dir/'server.lock').write_text('test')
        with self.assertRaises(RuntimeError): rebalance_primes.migrate(state_dir, apply=True)
        (state_dir/'server.lock').unlink()

    def test_rebalance_includes_user_pending_packs_but_leaves_historical_backups_untouched(self):
        store = self.backend.fut_inventory
        payload = {'packId': 180, 'useCredits': 1, 'usePreOrder': 0, 'currency': 'COINS'}
        with patch.object(fut_packs.random.SystemRandom, 'sample', return_value=[20801]):
            store.open_pack(payload)
        state = deepcopy(store.state)
        state['items'][0]['variant'].update(rating=115, attributes=[115]*6)
        store.commit(state)
        owner = self.path/'users'/('a'*64)/'fut-inventory.json'
        owner.parent.mkdir(parents=True)
        owner.write_bytes(store.path.read_bytes())
        historical = self.path/'before-per-user'/'fut-inventory.json'
        historical.parent.mkdir()
        historical.write_bytes(store.path.read_bytes())
        old = historical.read_bytes()
        count, backup = rebalance_primes.migrate(self.path, apply=True)
        self.assertEqual(count, 2)
        self.assertEqual(historical.read_bytes(), old)
        self.assertEqual(owner.read_bytes(), store.path.read_bytes())
        self.assertEqual((backup/owner.relative_to(self.path)).read_bytes(), old)
        restored = Backend(self.backend.path, settings=self.settings).fut_inventory
        coins = restored.state['coins']
        self.assertEqual(restored.open_pack(payload)['itemList'][0]['rating'], 109)
        self.assertEqual(restored.state['coins'], coins)

    def test_invalid_definitions_and_saved_snapshots_are_rejected(self):
        cases = [{'standard_pack_chance_percent': True}, {'editions': {'prime': {'version': 128}}},
                 {'editions': {'prime': {'rareflag': 255}}}, {'editions': {'standard': {'version': 2}}},
                 {'editions': {'flashback': {'version': 1, 'players': {'20801': {'rating': 99}}}}},
                 {'standard_pack_chance_percent': -0.001}, {'standard_pack_chance_percent': 100.001},
                 {'standard_pack_chance_percent': float('nan')}, {'standard_pack_chance_percent': float('inf')},
                 {'editions': {'prime': {'all_players': 1}}}, {'editions': {'prime': {'attribute_boost': True}}},
                 {'editions': {'prime': {'rating_boost': -1}}},
                 {'editions': {'prime': {'players': {'20801': {'rating': 0}}}}},
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
