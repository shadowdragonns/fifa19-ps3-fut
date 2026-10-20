"""Durable actual appearances, goals, assists, fitness and discipline totals."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import card_rebalance
import fut_inventory
import fut_matches
import fut_results
import player_statistics
from plugin_queries import Queries
from server_settings import Settings
from sessions import Backend
from test_project import club


class PlayerStatisticsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.backend=Backend(Path(temp.name)/'profile.json',settings=Settings())
        self.session=club(self.backend)
        self.store=self.backend.fut_inventory
        fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
        current=self.session.local_fut_match
        self.ids=[row['itemData']['id'] for row in current['squad']['players'] if row['index']<11]
        self.bench=[row['itemData']['id'] for row in current['squad']['players'] if 11<=row['index']<18]
        fut_matches.play(self.backend,self.session,{'items':[{'id':i} for i in self.ids]})
        self.report={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,'matchData':'',
            'items':[{'id':i,'fitness':75 if i in self.ids else 99,'goals':0,'assists':0,'redCards':0,'yellowCards':0}
                     for i in self.ids+self.bench],
            'myMatchStats':{k:0 for k in fut_results.STATS},'opponentMatchStats':{k:0 for k in fut_results.STATS}}
        self.report['myMatchStats']['goals']=2
        self.report['items'][1].update(goals=2,assists=1,yellowCards=1,redCards=1)

    def test_completed_report_updates_card_details_but_not_unused_bench_and_retry_is_once_only(self):
        result=fut_results.finish(self.backend,self.session,self.report)
        after=deepcopy(self.store.state)
        self.assertEqual(fut_results.finish(self.backend,self.session,self.report),result)
        self.assertEqual(self.store.state,after)
        played=next(r for r in after['items'] if r['id']==self.ids[1])
        self.assertEqual(played['statistics'],dict(gamesPlayed=1,goals=2,assists=1,yellowCards=1,redCards=1))
        self.assertEqual((played['fitness'],played['contract']),(75,6))
        self.assertTrue(all('statistics' not in r for r in after['items'] if r['id'] in self.bench))
        self.assertEqual(after['matchHistory'][0]['playedItems'],sorted(self.ids))
        card=self.store.card(played)
        self.assertEqual({r['index']:r['value'] for r in card['lifetimeStats']},{0:1,1:2,2:1,3:1,4:1})
        self.assertEqual(Queries(self.backend).get_card(self.backend.profile['persona'],played['id'])['statistics']['goals'],2)
        restored=Backend(self.backend.path,settings=self.backend.settings)
        self.assertEqual(restored.fut_inventory.state,after)
        # Served bans stop the red notification, while the lifetime red remains.
        played['suspension']=0
        card=self.store.card(played)
        self.assertFalse(any(r['index']==4 for r in card['statsList']))
        self.assertEqual(card['lifetimeStats'][4]['value'],1)

    def test_substitute_appearance_counts_but_unplayed_reported_reserve_does_not(self):
        current=self.session.local_fut_match
        current['played_items'] += (self.bench[0],)
        fut_results.finish(self.backend,self.session,self.report)
        rows={r['id']:r for r in self.store.state['items']}
        self.assertEqual(rows[self.bench[0]]['statistics']['gamesPlayed'],1)
        self.assertNotIn('statistics',rows[self.bench[1]])

    def test_no_contest_and_failed_commit_cannot_change_stats_or_fitness(self):
        before=deepcopy(self.store.state)
        with patch.object(Path,'replace',side_effect=OSError('synthetic failed save')):
            with self.assertRaises(OSError):fut_results.finish(self.backend,self.session,self.report)
        self.assertEqual(self.store.state,before)
        self.report['endReason']='NO_CONTEST'
        fut_results.finish(self.backend,self.session,self.report)
        self.assertEqual(self.store.state['items'],before['items'])

    def test_historical_backfill_is_idempotent_and_does_not_invent_old_bench_appearances(self):
        fut_results.finish(self.backend,self.session,self.report)
        state=deepcopy(self.store.state)
        expected=deepcopy(state['items'])
        for row in state['items']:row.pop('statistics',None)
        updated,count=card_rebalance.updated_state(state,self.backend.settings.section('custom_cards')['editions'])
        self.assertEqual(updated['items'],expected)
        self.assertEqual(count,11)
        self.assertEqual(player_statistics.backfill(updated),set())
        # Older records have no reliable played-ID list: recover the scorer's
        # recorded totals but do not count every unused bench card as an outing.
        for row in state['items']:row.pop('statistics',None)
        state['matchHistory'][0].pop('playedItems')
        player_statistics.backfill(state)
        rows={r['id']:r for r in state['items']}
        self.assertEqual(rows[self.ids[1]]['statistics']['goals'],2)
        self.assertNotIn('statistics',rows[self.bench[0]])


if __name__=='__main__':unittest.main()
