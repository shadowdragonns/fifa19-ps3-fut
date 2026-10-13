"""CPU fixture progression and live rules retain club saves and season progress."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import fut_matches
import fut_seasons
from server_settings import Settings, ConfigWatcher
from sessions import Backend
from test_project import club


class CPUBalanceTests(unittest.TestCase):
    def test_displayed_ten_uses_amateur_and_displayed_one_uses_world_class_nations(self):
        backend = Backend()
        for division in range(1, 11):
            backend.fut_inventory.state['offlineSeason'] = {'divisionId': division}
            before = deepcopy(backend.fut_inventory.state)
            season = fut_seasons.season_list(backend)['seasons'][0]
            self.assertEqual(season['divisionId'], division)
            self.assertEqual(len(season['matches']), 10)
            self.assertEqual([m['roundId'] for m in season['matches']], list(range(1, 11)))
            self.assertEqual([m['teamId'] for m in season['matches']], list(fut_seasons.CPU_TIERS[division-1]))
            self.assertEqual({m['difficulty'] for m in season['matches']}, {fut_seasons.CPU_DIFFICULTIES[division-1]})
            self.assertEqual(backend.fut_inventory.state, before)
        self.assertEqual(fut_seasons.CPU_DIFFICULTIES[0], 1)  # Beginner=0, Amateur=1.
        self.assertEqual(fut_seasons.CPU_DIFFICULTIES[-1], 4)  # World Class.
        self.assertTrue(set(fut_seasons.CPU_TIERS[-1]) <= {1354, 1343, 1369, 1325, 1335, 1337, 1362})
        self.assertNotIn(1318, {team for tier in fut_seasons.CPU_TIERS for team in tier})

    def test_new_rules_apply_to_season_lists_without_resetting_an_existing_club_or_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            backend = Backend(path/'profile.json', settings=Settings())
            session = club(backend)
            fut_matches.create(backend, session, {'squadId': 1, 'type': 'OFFLINE'})
            match = session.local_fut_match
            before = {p.name: p.read_bytes() for p in path.glob('*.json')}
            rules = Settings().section('cpu_seasons')
            rules['difficulties'][0] = 'semi_pro'
            rules['opponents'][0] = list(reversed(rules['opponents'][0]))
            config = path/'server-config.json'
            config.write_text(json.dumps({'cpu_seasons': rules}))
            now = [0]
            watcher = ConfigWatcher(backend, config, clock=lambda: now[0])
            watcher.poll(); now[0] = 1
            self.assertTrue(watcher.poll())
            season = fut_seasons.season_list(backend)['seasons'][0]
            self.assertEqual({m['difficulty'] for m in season['matches']}, {2})
            self.assertEqual([m['teamId'] for m in season['matches']], rules['opponents'][0])
            self.assertEqual({p.name: p.read_bytes() for p in path.glob('*.json') if p != config}, before)
            self.assertIs(session.local_fut_match, match)
            backend.fut_inventory.state['offlineSeasonHistory'] = [{}]
            rotated = fut_seasons.season_list(backend)['seasons'][0]['matches']
            self.assertEqual([m['teamId'] for m in rotated], rules['opponents'][0][1:]+rules['opponents'][0][:1])

    def test_bad_curve_edit_keeps_last_good_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'server-config.json'
            backend = Backend(settings=Settings())
            original = backend.settings.as_dict()
            now = [0]
            watcher = ConfigWatcher(backend, path, clock=lambda: now[0])
            cases = [ {'difficulties': ['amateur']*9}, {'difficulties': ['worldclass']*10},
                      {'difficulties': [True]*10}, {'opponents': [[838]*10]*9},
                      {'opponents': [[838]*9]*10}, {'opponents': [[0]*10]*10},
                      {'opponents': [[True]*10]*10}, {'opponents': [[2**31]*10]*10} ]
            for rules in cases:
                path.write_text(json.dumps({'cpu_seasons': rules}))
                watcher.poll(); now[0] += 1; watcher.poll()
                self.assertEqual(backend.settings.as_dict(), original)
                self.assertEqual(watcher.last_error_type, 'ValueError')


if __name__ == '__main__': unittest.main()
