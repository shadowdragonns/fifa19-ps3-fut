"""Exact special-edition odds and owned, persistent staff/consumable actions."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import card_versions
import fut_condition
import fut_inventory
import fut_items
import fut_leaderboards
import fut_matches
import fut_online
import fut_packs
import fut_results
import fut_squads
from plugin_queries import Queries
from server_settings import Settings
from sessions import Backend
from test_project import club


def purchase(pack_id):
    return {'packId':pack_id,'useCredits':1,'usePreOrder':0,'currency':'COINS'}


class SpecialEditionTests(unittest.TestCase):
    def test_full_probability_wheel_has_exact_unconditional_odds_and_no_overlap(self):
        class Wheel:
            def __init__(self): self.rolls=iter(range(10000))
            def randrange(self, size):
                if size!=10000: raise AssertionError('Incorrect probability wheel')
                return next(self.rolls)
        base=fut_packs.STANDARD_PLAYERS[20801]
        drawn=card_versions.upgrade_standard_draw([base]*10000,Settings(),Wheel())
        self.assertEqual(Counter(row.get('_edition','standard') for row in drawn),
                         {'prime':25,'toty':5,'otw':1,'standard':9969})
        for row in drawn[:31]:
            boost={'prime':10,'toty':20,'otw':25}[row['_edition']]
            self.assertEqual(row['rating'],base['rating']+boost)
            self.assertEqual([a['value'] for a in row['attributeList']],
                             [a['value']+boost for a in base['attributeList']])
        self.assertEqual(base['rating'],94)

    def test_ineligible_player_tickets_do_not_redistribute_and_disabling_all_is_a_noop(self):
        settings=Settings({'custom_cards':{'editions':{'prime':{'players':{'20801':{'enabled':False}}}}}})
        rng=type('Roll',(),{'randrange':lambda self,size:0})()
        base=fut_packs.STANDARD_PLAYERS[20801]
        self.assertEqual(card_versions.upgrade_standard_draw([base],settings,rng),[base])
        disabled=Settings({'custom_cards':{'enabled':False}})
        self.assertEqual(card_versions.upgrade_standard_draw([base],disabled,object()),[base])
        with self.assertRaises(ValueError): Settings({'custom_cards':{'editions':{'prime':{'one_in':1}}}})

    def test_115_cap_is_removed_up_to_native_field_widths(self):
        settings=Settings({'custom_cards':{'editions':{'prime':{
            'rating_boost':1000,'attribute_boost':1000}}}})
        player,variant=card_versions.catalogue_player(settings,'prime',20801)
        self.assertEqual(player['rating'],255)
        self.assertGreater(min(a['value'] for a in player['attributeList']),1000)
        card_versions.validate_variant('prime',20801,variant)
        backend=Backend(settings=settings)
        state=deepcopy(backend.fut_inventory.state)
        state['items']=[{'id':1,'assetId':20801,'edition':'prime','variant':variant,'pile':'club','timestamp':1}]
        backend.fut_inventory.commit(state)
        self.assertEqual(Queries(backend).get_cards(backend.profile['persona'],min_rating=200)[0]['rating'],255)
        for edition,rating in (('prime',104),('toty',114),('otw',119)):
            player,variant=card_versions.catalogue_player(Settings(),edition,20801)
            self.assertEqual(player['rating'],rating)
            self.assertEqual(card_versions.resource_id({'edition':edition,'assetId':20801,'variant':variant})>>24,
                             {'prime':1,'toty':2,'otw':3}[edition])

    def test_prime_offers_are_removed_but_legacy_pending_pack_can_be_resolved(self):
        backend=Backend(settings=Settings({'economy':{'starting_coins':200000}}))
        store=backend.fut_inventory
        self.assertFalse({180,181}&{p['id'] for p in store.offers()['purchase']})
        self.assertTrue({190,191}<={p['id'] for p in store.offers()['purchase']})
        before=deepcopy(store.state)
        for pack_id in (180,181):
            with self.assertRaises(fut_inventory.Conflict):store.open_pack(purchase(pack_id))
        self.assertEqual(store.state,before)
        _,variant=card_versions.catalogue_player(backend.settings,'prime',20801)
        state=deepcopy(before)
        state.update(nextItemId=2,nextPackId=2,purchaseCount=1,packPurchaseCounts={'180':1},
            pendingPack={'id':1,'packId':180,'itemIds':[1]},items=[
                {'id':1,'assetId':20801,'edition':'prime','variant':variant,'pile':'purchased','timestamp':1}])
        store.commit(state)
        self.assertEqual(store.open_pack(purchase(180))['itemList'][0]['id'],1)
        store.move({'itemData':[{'id':1,'pile':'club'}]})
        self.assertIsNone(store.state['pendingPack'])


class SupportItemTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.backend=Backend(self.root/'profile.json',settings=Settings({'economy':{'starting_coins':100000}}))
        self.session=club(self.backend)
        self.store=self.backend.fut_inventory

    def buy(self,pack_id):
        result=self.store.open_pack(purchase(pack_id))
        ids=[row['id'] for row in result['itemList']]
        self.store.move({'itemData':[{'id':i,'pile':'club'} for i in ids]})
        return result

    def source(self,subtype):
        return next(row['id'] for row in fut_items.records(self.store.state)
                    if fut_items.CATALOGUE[row['definitionId']]['cardsubtypeid']==subtype)

    def target(self,keeper=False):
        return next(row for row in self.store.state['items']
                    if (self.store.card(row)['preferredPosition']=='GK')==keeper)

    def test_manager_purchase_selection_creation_and_quick_sell_survive_restart(self):
        opened=self.buy(190);manager=opened['itemList'][0]
        self.assertEqual(manager['itemType'],'manager')
        self.assertEqual(manager['cardsubtypeid'],4)
        self.assertIn(manager['assetId'],{row['assetId'] for row in fut_items.MANAGERS})
        squad=deepcopy(fut_squads.saved(self.backend));squad['manager']=[{'id':manager['id'],'dream':False}]
        fut_squads.save(self.backend,squad)
        retrieved=fut_squads.retrieve(self.backend,self.session)
        self.assertEqual(retrieved['manager'][0]['id'],manager['id'])
        saved=Backend(self.backend.path,settings=self.backend.settings)
        self.assertEqual(saved.fut_inventory.state,self.store.state)
        self.assertEqual(fut_items.search(self.store,{'type':['manager']})['itemData'][0]['id'],manager['id'])
        created=fut_squads.create(self.backend,{'squadName':'Managed XI','manager':[{'id':manager['id'],'dream':False}]})
        self.assertGreater(created['id'],1)
        self.store.discard({'itemId':[manager['id']]})
        self.assertTrue(all(s['manager']==[{'id':0,'dream':False}] for s in fut_squads.squads(self.backend).values()))

    def test_supplies_resume_without_recharge_stack_and_reject_cross_club_actions(self):
        first=self.store.open_pack(purchase(191));coins=self.store.state['coins']
        self.assertEqual(self.store.open_pack(purchase(191)),first)
        self.assertEqual(self.store.state['coins'],coins)
        ids=[row['id'] for row in first['itemList']]
        self.store.move({'itemData':[{'id':i,'pile':'club'} for i in ids]})
        self.buy(191)
        self.assertEqual(len(fut_items.records(self.store.state)),14)
        source=self.source(57)
        stranger=Backend(settings=self.backend.settings)
        with self.assertRaises(ValueError):fut_items.apply(stranger,source,{'apply':[{'id':self.target()['id']}]})
        before=deepcopy(self.store.state)
        with self.assertRaises(ValueError):fut_items.apply(self.backend,source,{'apply':[{'id':99999999}]})
        self.assertEqual(self.store.state,before)

    def test_training_contracts_fitness_healing_and_single_completed_match_expiry(self):
        self.buy(191)
        outfield,keeper=self.target(),self.target(True)
        base=self.store.card(outfield)
        fut_items.apply(self.backend,self.source(57),{'apply':[{'id':outfield['id']}]})
        trained=next(row for row in self.store.state['items'] if row['id']==outfield['id'])
        self.assertEqual([a['value'] for a in self.store.card(trained)['attributeList']],
                         [a['value']+10 for a in base['attributeList']])
        self.assertEqual(self.store.card(trained)['trainingItem']['cardsubtypeid'],57)
        fut_items.apply(self.backend,self.source(67),{'apply':[{'id':keeper['id']}]})
        fut_items.apply(self.backend,self.source(201),{'apply':[{'id':outfield['id']}]})
        state=deepcopy(self.store.state)
        record=next(r for r in state['items'] if r['id']==outfield['id'])
        record.update(fitness=20,injuryGames=3,injuryType='leg')
        self.store.commit(state)
        fut_items.apply(self.backend,self.source(219),{'apply':[{'id':outfield['id']}]})
        fut_items.apply(self.backend,self.source(218),{'apply':[{'id':outfield['id']}]})
        record=next(r for r in self.store.state['items'] if r['id']==outfield['id'])
        self.assertEqual((record['fitness'],record['injuryGames'],record['injuryType'],record['contract']),(80,0,'none',35))
        self.assertEqual(Backend(self.backend.path,settings=self.backend.settings).fut_inventory.state,self.store.state)
        state=deepcopy(self.store.state)
        fut_condition.apply(state,{'played_items':[outfield['id']],'squad':{'manager':[]}},
                            {'endReason':'WIN','items':[{'id':outfield['id'],'fitness':73}]})
        record=next(r for r in state['items'] if r['id']==outfield['id'])
        self.assertNotIn('trainingBoost',record)
        self.assertEqual((record['fitness'],record['contract']),(73,34))
        self.assertIn('trainingBoost',next(r for r in state['items'] if r['id']==keeper['id']))

    def test_failed_apply_wrong_position_and_active_match_do_not_consume_anything(self):
        self.buy(191)
        source=self.source(57);before=deepcopy(self.store.state);raw=self.store.path.read_bytes()
        with self.assertRaises(ValueError):fut_items.apply(self.backend,source,{'apply':[{'id':self.target(True)['id']}]})
        with patch.object(Path,'replace',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):fut_items.apply(self.backend,source,{'apply':[{'id':self.target()['id']}]})
        self.assertEqual(self.store.state,before);self.assertEqual(self.store.path.read_bytes(),raw)
        fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
        with self.assertRaises(fut_inventory.Conflict):fut_items.apply(self.backend,source,{'apply':[{'id':self.target()['id']}]})
        self.assertEqual(self.store.state,before)

    def test_full_match_consumes_training_and_manager_contract_once_even_on_report_retry(self):
        manager=self.buy(190)['itemList'][0]
        self.buy(191)
        squad=deepcopy(fut_squads.saved(self.backend))
        squad['manager']=[{'id':manager['id'],'dream':False}]
        fut_squads.save(self.backend,squad)
        target=self.target()['id']
        fut_items.apply(self.backend,self.source(57),{'apply':[{'id':target}]})
        fut_items.apply(self.backend,self.source(202),{'apply':[{'id':manager['id']}]})
        fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
        current=self.session.local_fut_match
        ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
        self.assertIn(target,ids)
        # Retail includes the selected manager after the XI at kickoff.
        fut_matches.play(self.backend,self.session,{'items':[{'id':i} for i in ids+[manager['id']]]})
        self.assertEqual(current['played_items'],tuple(ids))
        reported=[p['itemData']['id'] for p in current['squad']['players']]+[manager['id']]
        report={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,
                'items':[{'id':i} for i in reported],'matchData':'',
                'myMatchStats':{k:0 for k in fut_results.STATS},
                'opponentMatchStats':{k:0 for k in fut_results.STATS}}
        report['myMatchStats']['goals']=1
        before=deepcopy(self.store.state)
        for bad_items in (report['items']+[{'id':manager['id']}],
                          report['items'][:-1]+[{'id':self.source(201)}],
                          report['items'][1:],report['items'][:-1]+[{'id':99999999}]):
            with self.assertRaises(ValueError):
                fut_results.finish(self.backend,self.session,{**report,'items':bad_items})
            self.assertEqual(self.store.state,before)
        first=fut_results.finish(self.backend,self.session,report)
        after=deepcopy(self.store.state)
        self.assertEqual(fut_results.finish(self.backend,self.session,report),first)
        self.assertEqual(self.store.state,after)
        self.assertNotIn('trainingBoost',next(r for r in after['items'] if r['id']==target))
        row=next(r for r in after['supportItems'] if r['id']==manager['id'])
        self.assertEqual(row['contract'],34)
        self.assertNotIn(manager['id'],after['matchHistory'][-1]['playedItems'])
        self.assertEqual(Backend(self.backend.path,settings=self.backend.settings).fut_inventory.state,after)
        self.assertEqual(Queries(self.backend).get_support_items(self.backend.profile['persona'],item_type='manager')[0]['contract'],34)

    def test_kickoff_with_manager_accepts_retail_payload_but_rejects_other_owned_cards(self):
        manager=self.buy(190)['itemList'][0]
        self.buy(191)
        squad=deepcopy(fut_squads.saved(self.backend))
        squad['manager']=[{'id':manager['id'],'dream':False}]
        fut_squads.save(self.backend,squad)
        fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
        current=self.session.local_fut_match
        ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
        bench=next(p['itemData']['id'] for p in current['squad']['players'] if p['index']==11)
        supplies=self.source(57)
        def request(selected):
            payload={'items':[{'id':i} for i in selected]}
            raw=b'PUT /ut/game/fifa19/match HTTP/1.1\r\nX-UT-SID: test-fut\r\n\r\n'+json.dumps(payload).encode()
            return fut_matches.response(self.backend,'PUT',fut_matches.BASE,raw)[0]
        before=deepcopy(self.store.state)
        for invalid in (ids+[supplies],ids+[bench],ids+[manager['id']]*2,
                        ids[:-1]+[manager['id']],ids+[99999999]):
            self.assertEqual(request(invalid),'400 Bad Request')
            self.assertFalse(current.get('playing'))
        self.assertEqual(request(ids+[manager['id']]),'200 OK')
        self.assertEqual(current['played_items'],tuple(ids))
        self.assertEqual(request([bench]),'200 OK')
        self.assertNotIn(manager['id'],current['played_items'])
        self.assertEqual(request([manager['id']]),'400 Bad Request')
        self.assertEqual(self.store.state,before)

    def test_online_ready_kickoff_and_substitution_only_track_players_with_selected_manager(self):
        manager=self.buy(190)['itemList'][0]
        squad=deepcopy(fut_squads.saved(self.backend))
        squad['manager']=[{'id':manager['id'],'dream':False}]
        fut_squads.save(self.backend,squad)
        current={'squad':fut_squads.retrieve(self.backend,self.session)}
        ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
        bench=next(p['itemData']['id'] for p in current['squad']['players'] if p['index']==11)
        def cards(selected,ready=False):
            return fut_online.online_cards(self.backend,self.session,current,
                {'items':[{'id':i} for i in selected]},starters_only=ready)
        self.assertEqual(cards(ids+[manager['id']],True),tuple(ids))
        self.assertEqual(cards(ids+[manager['id']]),tuple(ids))
        self.assertEqual(cards(ids),tuple(ids))
        current['playing']=True
        self.assertEqual(cards([bench,manager['id']]),(bench,))
        for invalid in ([manager['id']],[99999999],ids+[manager['id']]*2):
            with self.assertRaises(ValueError):cards(invalid)
        # A different selected squad or a manager no longer owned by the club
        # cannot authorize this staff card.
        current['squad']['manager']=[]
        with self.assertRaises(ValueError):cards(ids+[manager['id']])
        current['squad']['manager']=[manager]
        self.store.discard({'itemId':[manager['id']]})
        with self.assertRaises(ValueError):cards(ids+[manager['id']])

    def test_native_item_search_apply_and_quick_sell_routes_are_authenticated(self):
        self.buy(191)
        source=self.source(57);target=self.target()['id']
        def request(method,path,payload=None,sid='test-fut'):
            body=json.dumps(payload).encode() if payload is not None else b''
            raw=(f'{method} {path} HTTP/1.1\r\nX-UT-SID: {sid}\r\n\r\n').encode()+body
            response=fut_inventory.response(self.backend,method,path,raw)
            return response[0],json.loads(response[1])
        path=f'/ut/game/fifa19/item/{source}'
        self.assertEqual(request('GET',path,sid='invalid')[0],'401 Unauthorized')
        self.assertEqual(request('GET','/ut/game/fifa19/club?type=training')[0],'200 OK')
        self.assertEqual(request('GET','/ut/game/fifa19/club/consumables/all')[0],'200 OK')
        status,result=request('PUT',path,{'apply':[{'id':target}]})
        self.assertEqual(status,'200 OK');self.assertEqual(result['itemData'][0]['training'],1)
        self.assertNotIn(source,{r['id'] for r in fut_items.records(self.store.state)})

    def test_club_consumable_menus_stack_saved_cards_and_apply_by_installed_resource(self):
        def request(method,path,payload=None,sid='test-fut'):
            raw=(f'{method} {path} HTTP/1.1\r\nX-UT-SID: {sid}\r\n\r\n').encode()
            if payload is not None: raw+=json.dumps(payload).encode()
            response=fut_inventory.response(self.backend,method,path,raw)
            self.assertIsNotNone(response)
            return response[0],json.loads(response[1])
        base='/ut/game/fifa19/club'
        first=self.store.open_pack(purchase(191))
        self.assertTrue(all(row['typeValue']==0 for row in request('GET',base+'/stats/consumables')[1]['stat']))
        self.store.move({'itemData':[{'id':row['id'],'pile':'club'} for row in first['itemList']]})
        self.buy(191)
        manager=self.buy(190)['itemList'][0]
        before=deepcopy(self.store.state)
        status,stats=request('GET',base+'/stats/consumables')
        self.assertEqual(status,'200 OK')
        counts={row['type']:row['typeValue'] for row in stats['stat']}
        self.assertEqual((counts['consumables'],counts['consumablesContractPlayer'],counts['consumablesTrainingPlayer']),(14,2,2))
        self.assertEqual(counts['consumablesTrainingManagerLeagueModifier'],0)
        status,groups=request('GET',base+'/consumables/all')
        self.assertEqual(status,'200 OK');self.assertEqual(len(groups['itemData']),7)
        self.assertTrue(all(row['count']==2 and row['untradeableCount']==0 for row in groups['itemData']))
        self.assertTrue(all(row['discardValue']==row['item']['discardValue']==50 for row in groups['itemData']))
        self.assertNotIn(manager['id'],{row['item']['id'] for row in groups['itemData']})
        contracts=request('GET',base+'/consumables/consumablesContractPlayer')[1]['itemData']
        self.assertEqual(contracts,request('GET',base+'/consumables/66')[1]['itemData'])
        self.assertEqual(len(contracts),1)
        card=contracts[0]['item']
        self.assertEqual((card['resourceId'],card['assetId']),(5000007,5000007))
        self.assertEqual([card[k] for k in ('bronze','silver','gold')],[28]*3)
        self.assertEqual(request('GET',base+'/stats/staff')[0],'200 OK')
        for path in (base+'/stats/staff',base+'/stats/consumables',base+'/consumables/all'):
            self.assertEqual(request('GET',path,sid='invalid')[0],'401 Unauthorized')
            self.assertEqual(request('PUT',path)[0],'405 Method Not Allowed')
        self.assertEqual(request('GET',base+'/consumables/manager')[0],'400 Bad Request')
        self.assertEqual(request('GET',base+'/stats/staff?unexpected=1')[0],'400 Bad Request')
        self.assertEqual(self.store.state,before)
        target=self.target()['id']
        status,updated=request('PUT','/ut/game/fifa19/item/resource/5000007',{'apply':[{'id':target}]})
        self.assertEqual(status,'200 OK');self.assertEqual(updated['itemData'][0]['contract'],35)
        self.assertEqual(request('GET',base+'/consumables/66')[1]['itemData'][0]['count'],1)
        self.assertEqual(Backend(self.backend.path,settings=self.backend.settings).fut_inventory.state,self.store.state)
        # Unowned definitions and targets cannot consume the remaining stack.
        self.assertEqual(request('PUT','/ut/game/fifa19/item/resource/5000007',{'apply':[{'id':manager['id']}]})[0],'400 Bad Request')
        self.assertEqual(request('GET',base+'/consumables/66')[1]['itemData'][0]['count'],1)

    def test_existing_support_definitions_use_installed_art_without_rewriting_saves(self):
        resources={57:5000000,67:5000001,201:5000007,202:5000008,218:5000009,219:5000010,220:5000011}
        for subtype,resource in resources.items():
            row={'id':1,'definitionId':6000000+subtype,'pile':'club','timestamp':1}
            before=deepcopy(row)
            card=fut_items.card(row)
            self.assertEqual((card['resourceId'],card['assetId']),(resource,resource))
            self.assertEqual(card['cardsubtypeid'],subtype)
            self.assertEqual(row,before)
        self.buy(191)
        target=self.target()['id']
        # Older clients still referring to the old definition resource work.
        fut_items.apply(self.backend,6000201,{'apply':[{'id':target}]},by_resource=True)
        self.assertEqual(next(row for row in self.store.state['items'] if row['id']==target)['contract'],35)
        labels={row.attrib['id']:row.text for row in ET.fromstring(fut_leaderboards.localization())}
        self.assertEqual(labels['LeagueName_Abbr15_0'],'No league')
        self.assertEqual(labels['LAbbr_0'],'None')
        self.assertIn('LOC_LB_rank',labels)

    def test_squad_fitness_accepts_a_mix_of_full_and_tired_players_without_wasting_a_full_squad_card(self):
        self.buy(191)
        source=self.source(220)
        ids=[s['itemData']['id'] for s in fut_squads.saved(self.backend)['players'] if s['itemData']['id']]
        before=deepcopy(self.store.state)
        with self.assertRaises(fut_inventory.Conflict):
            fut_items.apply(self.backend,source,{'apply':[{'id':i} for i in ids]})
        self.assertEqual(self.store.state,before)
        state=deepcopy(before);state['items'][0]['fitness']=45;self.store.commit(state)
        fut_items.apply(self.backend,source,{'apply':[{'id':i} for i in ids]})
        self.assertEqual(self.store.state['items'][0]['fitness'],75)
        self.assertNotIn(source,{row['id'] for row in self.store.state['supportItems']})


if __name__=='__main__':unittest.main()
