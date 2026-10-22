"""Real elapsed recovery across restarts; match and supply effects remain atomic."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sessions import Backend
from server_settings import Settings
import fut_condition
import fut_items
import fut_matches
import fut_results
from test_project import club


class FitnessRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.backend=Backend(Path(tmp.name)/'profile.json');self.session=club(self.backend)
        self.store=self.backend.fut_inventory;self.now=2000000000

    def test_elapsed_five_minute_ticks_include_offline_time_and_do_not_rewrite_reads(self):
        state=deepcopy(self.store.state);record=state['items'][0]
        record.update(fitness=50,fitnessUpdatedAt=self.now)
        self.store.commit(state);raw=self.store.path.read_bytes()
        for delta,expected in ((-600,50),(0,50),(299,50),(300,51),(899,52),(900,53),(100000,99)):
            with patch('fut_condition.time.time',return_value=self.now+delta):
                restored=Backend(self.backend.path)
                self.assertEqual(restored.fut_inventory.card(restored.fut_inventory.state['items'][0])['fitness'],expected)
                self.assertEqual(self.store.path.read_bytes(),raw)
        with self.assertRaises(ValueError):Settings({'fitness':{'seconds_per_point':0}})
        self.assertEqual(fut_condition.fitness(record,Settings({'fitness':{'seconds_per_point':60}}),self.now+300),55)
        self.assertEqual(fut_condition.fitness(record,Settings({'fitness':{'enabled':False}}),self.now+300),50)

    def test_finished_matches_reset_played_clock_but_do_not_restore_unused_players_by_match_count(self):
        state=deepcopy(self.store.state)
        for record in state['items']:record.update(fitness=50,fitnessUpdatedAt=self.now-299)
        self.store.commit(state)
        with patch('fut_condition.time.time',return_value=self.now):
            fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
            ids=[p['itemData']['id'] for p in self.session.local_fut_match['squad']['players'] if p['index']<11]
            fut_matches.play(self.backend,self.session,{'items':[{'id':i} for i in ids]})
            report={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,'matchData':'',
                    'items':[{'id':i,'fitness':35} for i in ids],
                    'myMatchStats':{k:0 for k in fut_results.STATS},'opponentMatchStats':{k:0 for k in fut_results.STATS}}
            report['myMatchStats']['goals']=1
            result=fut_results.finish(self.backend,self.session,report)
            after=deepcopy(self.store.state)
            self.assertEqual(fut_results.finish(self.backend,self.session,report),result)
            self.assertEqual(self.store.state,after)
        played=next(r for r in after['items'] if r['id'] in ids)
        rested=next(r for r in after['items'] if r['id'] not in ids)
        self.assertEqual((played['fitness'],played['fitnessUpdatedAt']),(35,self.now))
        self.assertEqual((rested['fitness'],rested['fitnessUpdatedAt']),(50,self.now-299))
        self.assertEqual(fut_condition.fitness(played,now=self.now+300),36)
        self.assertEqual(fut_condition.fitness(rested,now=self.now+1),51)

    def test_legacy_tired_card_clock_initialization_is_once_only_and_failed_save_is_safe(self):
        state=deepcopy(self.store.state);state['items'][0]['fitness']=70;self.store.commit(state)
        before=deepcopy(self.store.state)
        with patch.object(self.store,'commit',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):fut_condition.initialize_recovery(self.store)
        self.assertEqual(self.store.state,before)
        with patch('fut_condition.time.time',return_value=self.now):fut_condition.initialize_recovery(self.store)
        self.assertEqual(self.store.state['items'][0]['fitnessUpdatedAt'],self.now)
        raw=self.store.path.read_bytes()
        with patch('fut_condition.time.time',return_value=self.now+600):
            fut_condition.initialize_recovery(self.store)
            self.assertEqual(self.store.card(self.store.state['items'][0])['fitness'],72)
        self.assertEqual(self.store.path.read_bytes(),raw)

    def test_supply_uses_recovered_fitness_and_resets_clock_without_free_extra_ticks(self):
        state=deepcopy(self.store.state);target=state['items'][0]
        target.update(fitness=20,fitnessUpdatedAt=self.now-600)
        source=state['nextItemId'];state['nextItemId']+=1
        state['supportItems']=[{'id':source,'definitionId':6000219,'pile':'club','timestamp':1}]
        self.store.commit(state)
        with patch('fut_condition.time.time',return_value=self.now):
            fut_items.apply(self.backend,source,{'apply':[{'id':target['id']}]})
        record=self.store.state['items'][0]
        self.assertEqual((record['fitness'],record['fitnessUpdatedAt']),(82,self.now))
        self.assertEqual(fut_condition.fitness(record,now=self.now+299),82)
        self.assertEqual(fut_condition.fitness(record,now=self.now+300),83)

if __name__=='__main__':unittest.main()
