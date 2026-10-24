"""Opt-in spending, consumable ownership and atomic match renewal."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fut_contracts
import fut_inventory
import fut_items
import fut_matches
import fut_results
import fut_squads
from server_settings import Settings
from sessions import Backend
from test_project import club


class ContractTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.backend=Backend(Path(temp.name)/'profile.json',settings=Settings({'economy':{'starting_coins':10000}}))
        self.session=club(self.backend);self.store=self.backend.fut_inventory

    def buy(self,identifier):
        pack=self.store.open_pack({'packId':identifier,'useCredits':1,'usePreOrder':0,'currency':'COINS'})
        self.store.move({'itemData':[{'id':r['id'],'pile':'club'} for r in pack['itemList']]})
        return pack

    def test_contract_pack_contains_ten_player_two_manager_cards_and_resumes_without_recharging(self):
        payload={'packId':192,'useCredits':1,'usePreOrder':0,'currency':'COINS'}
        before=self.store.state['coins'];result=self.store.open_pack(payload)
        self.assertEqual([r['cardsubtypeid'] for r in result['itemList']],[201]*10+[202]*2)
        self.assertEqual(self.store.state['coins'],before-2000)
        restored=Backend(self.backend.path,settings=self.backend.settings).fut_inventory
        self.assertEqual(restored.open_pack(payload),result)
        self.assertEqual(restored.state['coins'],before-2000)

    def test_owned_cards_then_coins_only_for_expired_active_xi_bench_and_selected_staff(self):
        self.buy(191);manager=self.buy(190)['itemList'][0]
        state=deepcopy(self.store.state);squad=state['squad']
        squad['manager']=[{'id':manager['id'],'dream':False}]
        targets=[squad['players'][i]['itemData']['id'] for i in (0,11)]
        reserve=squad['players'][18]['itemData']['id']
        for row in state['items']:
            if row['id'] in targets+[reserve]:row['contract']=0
        next(r for r in state['supportItems'] if r['id']==manager['id'])['contract']=0
        state['coins']=200;self.store.commit(state)
        fut_contracts.preference(self.backend,{'auto_renew':True})
        self.assertEqual(self.store.state['coins'],0)
        self.assertEqual([next(r for r in self.store.state['items'] if r['id']==i)['contract'] for i in targets],[28,28])
        self.assertEqual(next(r for r in self.store.state['items'] if r['id']==reserve)['contract'],0)
        self.assertEqual(next(r for r in self.store.state['supportItems'] if r['id']==manager['id'])['contract'],28)
        self.assertFalse({6000201,6000202}&{r['definitionId'] for r in self.store.state['supportItems']})
        before=deepcopy(self.store.state)
        fut_squads.retrieve(self.backend,self.session);fut_contracts.ensure(self.backend)
        self.assertEqual(self.store.state,before)
        self.assertEqual(Backend(self.backend.path,settings=self.backend.settings).fut_inventory.state,before)

    def test_disabled_insufficient_funds_and_failed_save_preserve_coins_and_cards(self):
        state=deepcopy(self.store.state);state['items'][0]['contract']=0;state['coins']=199;self.store.commit(state)
        fut_contracts.ensure(self.backend)
        self.assertEqual(self.store.state,state)
        fut_contracts.preference(self.backend,{'auto_renew':True})
        self.assertEqual(self.store.state['coins'],199);self.assertEqual(self.store.state['items'][0]['contract'],0)
        fut_contracts.preference(self.backend,{'auto_renew':False})
        state=deepcopy(self.store.state);state['coins']=500;self.store.commit(state)
        with patch.object(self.store,'commit',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):fut_contracts.preference(self.backend,{'auto_renew':True})
        self.assertEqual(self.store.state,state)
        for value in (1,'true',None):
            with self.assertRaises(ValueError):fut_contracts.preference(self.backend,{'auto_renew':value})

    def test_match_reward_can_fund_renewal_and_retries_cannot_charge_twice(self):
        state=deepcopy(self.store.state);state['coins']=0;state['autoRenewContracts']=True
        target=state['squad']['players'][0]['itemData']['id']
        next(r for r in state['items'] if r['id']==target)['contract']=1
        self.store.commit(state)
        fut_matches.create(self.backend,self.session,{'squadId':1,'type':'OFFLINE'})
        current=self.session.local_fut_match
        ids=[s['itemData']['id'] for s in current['squad']['players'] if s['index']<11]
        fut_matches.play(self.backend,self.session,{'items':[{'id':i} for i in ids]})
        report={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,'items':[{'id':i} for i in ids],
                'matchData':'','myMatchStats':{k:0 for k in fut_results.STATS},'opponentMatchStats':{k:0 for k in fut_results.STATS}}
        report['myMatchStats']['goals']=1
        before=deepcopy(self.store.state)
        with patch.object(self.store,'commit',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):fut_results.finish(self.backend,self.session,report)
        self.assertEqual(self.store.state,before)
        award=fut_results.finish(self.backend,self.session,report)
        self.assertEqual(self.store.state['coins'],award['allCoins']-200)
        self.assertEqual(next(r for r in self.store.state['items'] if r['id']==target)['contract'],28)
        before=deepcopy(self.store.state)
        self.assertEqual(fut_results.finish(self.backend,self.session,report),award)
        self.assertEqual(self.store.state,before)

    def test_website_preference_does_not_renew_mid_match_and_prices_validate(self):
        self.session.local_fut_match={'playing':True}
        state=deepcopy(self.store.state);state['items'][0]['contract']=0;self.store.commit(state)
        fut_contracts.preference(self.backend,{'auto_renew':True})
        self.assertEqual(self.store.state['coins'],10000);self.assertEqual(self.store.state['items'][0]['contract'],0)
        self.session.local_fut_match['finished']=True
        fut_contracts.ensure(self.backend)
        self.assertEqual(self.store.state['coins'],9800);self.assertEqual(self.store.state['items'][0]['contract'],28)
        with self.assertRaises(ValueError):Settings({'contracts':{'player_price':-1}})
        self.assertEqual(Settings({'contracts':{'player_price':123}}).section('contracts')['player_price'],123)


if __name__=='__main__':unittest.main()
