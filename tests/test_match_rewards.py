"""Native reward breakdowns, immutable policies and exactly-once real coin awards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fut_matches
import fut_results
import fut_rewards
from server_settings import Settings,ConfigWatcher
from sessions import Backend
from test_project import club


def report(ids=range(1,12),ours=2,theirs=0,**stats):
    reason='WIN' if ours>theirs else 'LOSS' if ours<theirs else 'DRAW'
    return {'matchReportId':0,'endReason':reason,'secondsPlayed':5400,
            'items':[{'id':i} for i in ids],'matchData':'',
            'myMatchStats':dict(dict.fromkeys(fut_results.STATS,0),goals=ours,**stats),
            'opponentMatchStats':dict(dict.fromkeys(fut_results.STATS,0),goals=theirs)}


class MatchRewardsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name);self.backend=Backend(self.path/'profile.json',settings=Settings())
        self.session=club(self.backend);self.store=self.backend.fut_inventory

    def match(self,ours=2,theirs=0,**stats):
        fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
        current=self.session.local_fut_match
        ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
        fut_matches.play(self.backend,self.session,{'items':[{'id':i} for i in ids]})
        return current,report(ids,ours,theirs,**stats)

    def test_score_stats_and_discipline_populate_the_native_rows_and_pay_their_actual_total(self):
        current,payload=self.match(shotsOnTarget=8,successfulTackles=15,corners=3,
            passingPercentage=75,possessionPercentage=55,manOfTheMatch=1,fouls=1,yellowCards=1,offsides=2)
        before=self.store.state['coins'];result=fut_results.finish(self.backend,self.session,payload)
        rows={row['type']:row for row in result['matchCoinPartials']}
        self.assertEqual(len(rows),13)
        self.assertEqual(rows['GOALS'],{'type':'GOALS','quantity':2,'value':100,'total':200})
        # Native reports can leave the cleansheets counter at zero. The
        # validated opponent score is authoritative for this binary bonus.
        self.assertEqual(payload['myMatchStats']['cleansheets'],0)
        self.assertEqual(rows['CLEANSHEETS']['total'],150)
        self.assertEqual(rows['FOULS']['total'],-5)
        self.assertEqual(rows['YELLOW_CARDS']['total'],-10)
        self.assertEqual(sum(r['total'] for r in rows.values()),548)
        self.assertEqual((result['allCoins'],result['matchCoins'],result['participationAward']),(1548,1548,1000))
        self.assertEqual(self.store.state['coins'],before+1548)
        receipt=self.store.state['matchHistory'][-1]
        self.assertEqual(receipt['rewardVersion'],5)
        self.assertEqual(receipt['rewardPolicy']['matchBonuses'],Settings().section('match_bonuses'))

    def test_fewer_fouls_and_cards_pay_more_and_deductions_never_take_the_base_reward(self):
        rules=Settings().section('match_bonuses')
        clean=report();fouls=report(fouls=2);cards=report(fouls=2,yellowCards=1,redCards=1)
        self.assertEqual([fut_rewards.bonus(p,rules) for p in (clean,fouls,cards)],[350,340,290])
        current,payload=self.match(0,9,fouls=65535,yellowCards=65535,redCards=65535,offsides=65535)
        before=self.store.state['coins'];result=fut_results.finish(self.backend,self.session,payload)
        self.assertLess(sum(r['total'] for r in result['matchCoinPartials']),0)
        self.assertEqual(result['allCoins'],500)
        self.assertEqual(result['participationAward'],500)
        self.assertEqual(self.store.state['coins'],before+500)

    def test_goal_and_other_caps_and_opponent_stats_cannot_inflate_our_bonus(self):
        rules=Settings().section('match_bonuses')
        payload=report(ours=65535,theirs=65534,**{k:65535 for k in ('shotsOnTarget','successfulTackles','corners','manOfTheMatch','fouls','yellowCards','redCards','offsides')},passingPercentage=100,possessionPercentage=100,cleansheets=65535)
        fut_results.validate_report(payload)
        rows={r['type']:r for r in fut_rewards.partials(payload,rules)}
        self.assertEqual(rows['GOALS']['total'],500)
        self.assertEqual(rows['CLEANSHEETS']['total'],0)
        self.assertEqual(rows['GOALS_AGAINST']['total'],-60)
        self.assertEqual(rows['PASSING_PERCENTAGE']['quantity'],80)
        self.assertEqual(rows['POSSESSION_PERCENTAGE']['quantity'],60)
        self.assertEqual(fut_rewards.bonus(payload,rules),435)
        before=fut_rewards.partials(payload,rules)
        payload['opponentMatchStats'].update(fouls=65535,yellowCards=65535,redCards=65535)
        self.assertEqual(fut_rewards.partials(payload,rules),before)

    def test_wins_draws_losses_division_base_and_incomplete_results_keep_their_own_rules(self):
        for reason,ours,theirs,expected in (('WIN',1,0,1250),('DRAW',0,0,900),('LOSS',0,1,500)):
            payload=report(ours=ours,theirs=theirs)
            policy={**self.backend.settings.reward_policy(0,0),'matchBonuses':self.backend.settings.section('match_bonuses')}
            self.assertEqual(fut_results.configured_award(policy,payload)['allCoins'],expected)
            # Higher division completion reward stays separate from the
            # stat bonus; a goal is not multiplied twice by division.
            higher={**policy,**self.backend.settings.reward_policy(1,10)}
            extra=(5500*{'WIN':100,'DRAW':75,'LOSS':50}[reason]//100)-{'WIN':1000,'DRAW':750,'LOSS':500}[reason]
            self.assertEqual(fut_results.configured_award(higher,payload)['allCoins'],expected+extra)
        for reason in fut_results.REASONS-{'WIN','DRAW','LOSS'}:
            payload=report();payload['endReason']=reason
            result=fut_results.configured_award(policy,payload)
            self.assertEqual(result,fut_results.award(0))

    def test_live_changes_affect_next_completion_but_paid_retry_restart_and_receipt_stay_unchanged(self):
        current,payload=self.match();config=self.path/'server-config.json';now=[0]
        watcher=ConfigWatcher(self.backend,config,clock=lambda:now[0])
        def change(values):
            config.write_text(json.dumps(values),encoding='utf-8');watcher.poll();now[0]+=1
            self.assertTrue(watcher.poll())
        change({'match_bonuses':{'goals':{'coins':200},'clean_sheet':{'coins':300}}})
        first=fut_results.finish(self.backend,self.session,payload)
        self.assertEqual(first['allCoins'],1700)
        state=deepcopy(self.store.state)
        change({'match_bonuses':{'goals':{'coins':900},'clean_sheet':{'coins':900}}})
        self.assertEqual(fut_results.finish(self.backend,self.session,payload),first)
        self.assertEqual(self.store.state,state)
        restored=Backend(self.backend.path,settings=self.backend.settings)
        self.assertEqual(fut_results.save_result(restored,current,payload),first)
        self.assertEqual(restored.fut_inventory.state,state)
        _,next_payload=self.match()
        self.assertEqual(fut_results.finish(self.backend,self.session,next_payload)['allCoins'],3700)

    def test_saved_policies_and_breakdowns_are_checked_and_old_paid_versions_are_preserved(self):
        current,payload=self.match();fut_results.finish(self.backend,self.session,payload)
        original=deepcopy(self.store.state)
        for mutator in (
            lambda r:r['award']['matchCoinPartials'][0].update(total=999),
            lambda r:r['award'].update(allCoins=r['award']['allCoins']+1),
            lambda r:r['award'].update(participationAward=999),
            lambda r:r['rewardPolicy']['matchBonuses']['goals'].update(coins=True),
            lambda r:r.update(rewardVersion=4)):
            state=deepcopy(original);mutator(state['matchHistory'][-1])
            with self.assertRaises(ValueError):self.store.commit(state)
        for version in (None,2,3,4):
            state=deepcopy(original);receipt=state['matchHistory'][-1]
            receipt.pop('rewardVersion');receipt.pop('rewardPolicy')
            if version is not None:receipt['rewardVersion']=version
            coins=1000 if version else 400
            if version==4:receipt['rewardPolicy']=self.backend.settings.reward_policy(0,0)
            receipt['award']=fut_results.award(coins);self.store.commit(state)
            restored=Backend(self.backend.path)
            self.assertEqual(fut_results.save_result(restored,current,payload),receipt['award'])
            self.assertEqual(restored.fut_inventory.state['coins'],original['coins'])

    def test_save_failure_and_changed_report_cannot_pay_or_repeat_a_bonus(self):
        current,payload=self.match();before=deepcopy(self.store.state);raw=self.store.path.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):fut_results.finish(self.backend,self.session,payload)
        self.assertEqual(self.store.state,before);self.assertEqual(self.store.path.read_bytes(),raw)
        self.assertFalse(current.get('finished',False))
        first=fut_results.finish(self.backend,self.session,payload);after=deepcopy(self.store.state)
        changed=deepcopy(payload);changed['myMatchStats']['goals']+=1
        with self.assertRaises(ValueError):fut_results.finish(self.backend,self.session,changed)
        self.assertEqual(fut_results.finish(self.backend,self.session,payload),first)
        self.assertEqual(self.store.state,after)

    def test_plugins_see_the_full_bonus_and_can_adjust_or_zero_it_without_invalid_unsigned_ui_fields(self):
        for transform,expected,completion in ((lambda n:n+100,1450,1100),(lambda n:n//2,675,325),(lambda n:0,0,0)):
            backend=Backend(settings=Settings());session=club(backend)
            self.backend,self.session,self.store=backend,session,backend.fut_inventory
            current,payload=self.match();seen=[]
            backend.events.subscribe('match.reward',lambda event,coins:(seen.append(coins),transform(coins))[1],filter=True)
            result=fut_results.finish(backend,session,payload)
            self.assertEqual(seen,[1350]);self.assertEqual(result['allCoins'],expected)
            self.assertEqual(result['participationAward'],completion)
            fut_results.validate_history(self.store.state['matchHistory'])

    def test_disabled_settings_and_invalid_rules(self):
        self.backend.settings.replace(Settings({'match_bonuses':{'enabled':False}}))
        _,payload=self.match()
        result=fut_results.finish(self.backend,self.session,payload)
        self.assertEqual((result['allCoins'],result['participationAward'],result['matchCoinPartials']),(1000,1000,[]))
        for values in ({'enabled':1},{'goals':{'coins':-1}},{'fouls':{'coins':5}},
                       {'goals':{'cap':True}},{'goals':{'cap':1001}},
                       {'passing_percentage':{'cap':101}},{'clean_sheet':{'cap':2}},
                       {'goals':{'coins':10000,'cap':1000}},{'unknown':10}):
            with self.assertRaises(ValueError):Settings({'match_bonuses':values})

    def test_each_logged_in_club_receives_only_its_own_score_bonus_and_saved_coins(self):
        from test_plugin_api import login,seed_club
        root=Backend(self.path/'multi/profile.json',multiuser=True,settings=Settings())
        players=[login(root,101,'First'),login(root,202,'Second')]
        for connection in players:seed_club(connection.session.owner)
        starts=[c.session.owner.fut_inventory.state['coins'] for c in players]
        for index,connection in enumerate(players):
            owner=connection.session.owner
            fut_matches.create(root,connection.session,{'type':'OFFLINE','squadId':1})
            current=connection.session.local_fut_match
            ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
            fut_matches.play(root,connection.session,{'items':[{'id':i} for i in ids]})
            payload=report(ids,2 if index==0 else 0,0 if index==0 else 2)
            expected=(1350,500)[index]
            result=fut_results.finish(root,connection.session,payload)
            self.assertEqual(result['allCoins'],expected)
            self.assertEqual(fut_results.finish(root,connection.session,payload),result)
            self.assertEqual(owner.fut_inventory.state['coins'],starts[index]+expected)
            self.assertEqual(Backend(owner.path,settings=Settings()).fut_inventory.state,owner.fut_inventory.state)
        self.assertEqual([len(c.session.owner.fut_inventory.state['matchHistory']) for c in players],[1,1])


if __name__=='__main__':unittest.main()
