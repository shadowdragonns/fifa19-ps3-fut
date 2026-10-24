"""Owned position/style transactions and the inputs to native FIFA chemistry."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import card_versions
import fut_condition
import fut_inventory
import fut_items
import fut_matches
import fut_packs
import fut_squads
import player_statistics
from server_settings import Settings
from sessions import Backend
from test_project import club


class ChemistryItemTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.backend=Backend(Path(temp.name)/'profile.json',settings=Settings({'economy':{'starting_coins':100000}}))
        self.session=club(self.backend);self.store=self.backend.fut_inventory
        self.target=next(r['id'] for r in self.store.state['items'] if self.store.card(r)['preferredPosition']!='GK')

    def record(self):return next(r for r in self.store.state['items'] if r['id']==self.target)

    def grant(self,subtype,position=None):
        state=deepcopy(self.store.state);ident=state['nextItemId'];state['nextItemId']+=1
        state.setdefault('supportItems',[]).append({'id':ident,'definitionId':6000000+subtype,'pile':'club','timestamp':1})
        if position:
            next(r for r in state['items'] if r['id']==self.target)['position']=position
        self.store.commit(state)
        return ident

    def apply(self,source,resource=False):
        return fut_items.apply(self.backend,source,{'apply':[{'id':self.target}]},by_resource=resource)

    def test_both_packs_resume_persist_and_expose_native_stack_categories(self):
        for pack_id,price,kind,size in ((193,1500,'position',20),(194,2500,'playStyle',24)):
            payload={'packId':pack_id,'useCredits':1,'usePreOrder':0,'currency':'COINS'}
            before=self.store.state['coins'];result=self.store.open_pack(payload)
            self.assertEqual(self.store.state['coins'],before-price)
            self.assertEqual(len(result['itemList']),6)
            self.assertEqual({r['itemType'] for r in result['itemList']},{kind})
            self.assertEqual(len({r['resourceId'] for r in result['itemList']}),6)
            restored=Backend(self.backend.path,settings=self.backend.settings).fut_inventory
            self.assertEqual(restored.open_pack(payload),result)
            self.assertEqual(restored.state['coins'],before-price)
            self.store.move({'itemData':[{'id':r['id'],'pile':'club'} for r in result['itemList']]})
            self.assertEqual(len(fut_items.search(self.store,{'type':[kind]})['itemData']),6)
            self.assertEqual(len([k for k,v in fut_items.CATALOGUE.items() if v['itemType']==kind]),size)
        stacks=fut_items.consumable_list(self.store,'development',{})['itemData']
        self.assertEqual(sum(s['count'] for s in stacks),12)
        self.assertEqual(len(fut_items.consumable_list(self.store,'69',{})['itemData']),6)
        self.assertEqual(sum(len(fut_items.consumable_list(self.store,str(i),{})['itemData']) for i in (75,76)),6)
        # Existing supplies retain their original guaranteed contents.
        self.assertEqual(set(fut_items.draw(fut_packs.PACKS[191])),set(fut_items.SUPPLIES))

    def test_all_native_conversions_require_the_printed_source_and_leave_base_catalogue_intact(self):
        original=deepcopy(fut_packs.STANDARD_PLAYERS)
        for subtype,(origin,destination) in fut_items.POSITION_CHANGES.items():
            source=self.grant(subtype,origin);before=self.store.card(self.record())
            result=self.apply(6000000+subtype,resource=True)
            updated=self.store.card(self.record())
            self.assertEqual(updated['preferredPosition'],destination)
            self.assertEqual(updated['resourceId'],before['resourceId'])
            self.assertEqual(updated['attributeList'],before['attributeList'])
            self.assertEqual(updated['rating'],before['rating'])
            self.assertEqual([r['id'] for r in result['itemData']],[self.target,source])
            self.assertNotIn(source,{r['id'] for r in fut_items.records(self.store.state)})
            self.assertEqual(fut_inventory.club_search(self.store,{'position':[destination]})['itemData'][0]['preferredPosition'],destination)
        self.assertEqual(fut_packs.STANDARD_PLAYERS,original)

    def test_chained_conversion_preserves_special_card_admin_stats_conditions_and_all_squads(self):
        state=deepcopy(self.store.state);record=next(r for r in state['items'] if r['id']==self.target)
        record['edition']='otw';_,record['variant']=card_versions.catalogue_player(self.backend.settings,'otw',record['assetId'])
        record.update(overrides={'position':'ST','rating':123},fitness=54,contract=42)
        self.store.commit(state);original=self.store.card(self.record());squad=deepcopy(state['squad'])
        for subtype in (110,108,104,106):self.apply(self.grant(subtype))
        card=self.store.card(self.record())
        self.assertEqual((card['preferredPosition'],card['rating'],card['fitness'],card['contract']),('CDM',123,54,42))
        self.assertEqual(card['attributeList'],original['attributeList'])
        self.assertEqual(card['resourceId'],original['resourceId'])
        self.assertEqual(self.store.state['squad'],squad)
        retrieved=fut_squads.retrieve(self.backend,self.session)
        self.assertEqual(next(r['itemData'] for r in retrieved['players'] if r['itemData']['id']==self.target)['preferredPosition'],'CDM')
        state=deepcopy(self.store.state);next(r for r in state['items'] if r['id']==self.target).pop('overrides')
        self.store.commit(state)
        self.assertEqual(self.store.card(self.record())['preferredPosition'],'CDM')
        self.assertEqual(Backend(self.backend.path,settings=self.backend.settings).fut_inventory.state,state)

    def test_every_style_has_persistent_native_id_without_flat_stat_or_rating_changes(self):
        for subtype in fut_items.CHEMISTRY_STYLES:
            source=self.grant(subtype,'GK' if subtype>=269 else 'CM')
            # Make the selected basic style an actual change too.
            state=deepcopy(self.store.state);r=next(r for r in state['items'] if r['id']==self.target)
            r['playStyle']=270 if subtype==273 else 251 if subtype==250 else 273 if subtype>=269 else 250
            self.store.commit(state);before=self.store.card(self.record())
            self.apply(source);card=self.store.card(self.record())
            self.assertEqual(card['playStyle'],subtype)
            self.assertEqual((card['rating'],card['attributeList']),(before['rating'],before['attributeList']))
            state=deepcopy(self.store.state)
            fut_condition.apply(state,{'played_items':[self.target],'squad':{'manager':[]}},
                                {'endReason':'WIN','items':[{'id':self.target}]})
            self.store.commit(state)
            self.assertEqual(self.store.card(self.record())['playStyle'],subtype)
        self.assertEqual(Backend(self.backend.path,settings=self.backend.settings).fut_inventory.state,self.store.state)

    def test_invalid_incompatible_repeated_and_failed_apply_do_not_consume_or_change_cards(self):
        for subtype,position in ((110,'CM'),(110,'GK'),(266,'GK'),(272,'ST'),(250,'ST'),(273,'GK')):
            source=self.grant(subtype,position)
            # Clear any previous selected style; defaults must reject repeated Basic.
            state=deepcopy(self.store.state);next(r for r in state['items'] if r['id']==self.target).pop('playStyle',None)
            self.store.commit(state);before=deepcopy(self.store.state)
            with self.assertRaises((ValueError,fut_inventory.Conflict)):self.apply(source)
            self.assertEqual(self.store.state,before)
        source=self.grant(266,'ST');before=deepcopy(self.store.state);raw=self.store.path.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.apply(source)
        self.assertEqual(self.store.state,before);self.assertEqual(self.store.path.read_bytes(),raw)
        stranger=Backend(settings=self.backend.settings)
        with self.assertRaises(ValueError):fut_items.apply(stranger,source,{'apply':[{'id':self.target}]})
        fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':1})
        with self.assertRaises(fut_inventory.Conflict):self.apply(source)
        self.assertEqual(self.store.state,before)

    def test_first_owner_loyalty_and_ten_actual_appearances_and_correct_keeper_defaults(self):
        for asset in (158023,167495):
            record={'id':1,'assetId':asset,'edition':'standard','timestamp':1}
            card=self.store.card(record)
            self.assertEqual(card['playStyle'],273 if asset==167495 else 250)
            self.assertEqual(card['loyaltyBonus'],1)
            record.update(firstOwner=False,statistics=player_statistics.empty())
            record['statistics']['gamesPlayed']=9
            self.assertEqual(self.store.card(record)['loyaltyBonus'],0)
            state={'items':[dict(record,pile='club')]}
            report={'endReason':'WIN','secondsPlayed':5400,'items':[{'id':1}]}
            player_statistics.apply(state,{'played_items':[]},report)
            self.assertEqual(self.store.card(state['items'][0])['loyaltyBonus'],0)
            player_statistics.apply(state,{'played_items':[1]},{**report,'endReason':'NO_CONTEST'})
            self.assertEqual(self.store.card(state['items'][0])['loyaltyBonus'],0)
            player_statistics.apply(state,{'played_items':[1]},report)
            self.assertEqual(self.store.card(state['items'][0])['loyaltyBonus'],1)

    def test_saved_fields_reject_malformed_values_and_admin_gk_edits_use_compatible_styles(self):
        for key,value in (('position','RANDOM'),('position',[]),('playStyle',249),('playStyle',274),('playStyle',True),('firstOwner',1)):
            state=deepcopy(self.store.state);state['items'][0][key]=value
            with self.assertRaises(ValueError):self.store.commit(state)
        record={'id':1,'assetId':158023,'edition':'standard','timestamp':1,'playStyle':266,'overrides':{'position':'GK'}}
        self.assertEqual(self.store.card(record)['playStyle'],273)


if __name__=='__main__':unittest.main()
