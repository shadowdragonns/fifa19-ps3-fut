"""Explicit admin club resets, backup/atomicity, stale requests and quote epochs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import club_reset
import fut_inventory
import fut_matches
import fut_results
import fut_squads
import web_packs
from server_settings import Settings
from sessions import Backend
from test_plugin_api import login,seed_club
from web_accounts import WebAccounts
from web_app import WebApp,COOKIE


class ClubResetTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'state/profile.json'
        self.root=Backend(self.path,multiuser=True,settings=Settings())
        self.a=login(self.root,101,'First');self.b=login(self.root,202,'Second')
        for c in (self.a,self.b):seed_club(c.session.owner)
        self.owner=self.a.session.owner
        self.auth=WebAccounts(self.path.with_name('web-accounts.sqlite3'))
        self.token=self.auth.register('operator','synthetic operator password',self.auth.invite(self.a.session.persona))
        self.friend=self.auth.register('friend','synthetic friend password',self.auth.invite(self.b.session.persona))
        self.auth.set_admin('operator');self.app=WebApp(self.root,self.auth)

    def request(self,route,data=None,token=None,csrf=True,origin='http://127.0.0.1:8099'):
        token=self.token if token is None else token;method='POST' if data is not None else 'GET'
        raw=(f'{method} /club/api/{route} HTTP/1.1\r\nHost: 127.0.0.1:8099\r\nCookie: {COOKIE}={token}\r\n').encode()
        if data is not None:
            raw+=f'Origin: {origin}\r\nContent-Type: application/json\r\n'.encode()
            if csrf:raw+=('X-CSRF-Token: '+self.app.csrf(token)+'\r\n').encode()
        raw+=b'\r\n'+(json.dumps(data).encode() if data is not None else b'')
        result=self.app.handle(method,'/club/api/'+route,raw,'127.0.0.1')
        return result[0],json.loads(result[1])

    def payload(self,owner=None):
        owner=owner or self.owner
        with owner.lock:info=club_reset.info(owner)
        return {'persona':owner.profile['persona'],'revision':info['revision'],'confirmation':info['club_name']}

    def test_admin_origin_csrf_exact_confirmation_and_offline_checks_precede_any_erase(self):
        payload=self.payload();before=deepcopy(self.owner.fut_inventory.state)
        for kwargs in ({'token':self.friend},{'csrf':False},{'origin':'https://attacker.invalid'}):
            self.assertEqual(self.request('admin/reset-club',payload,**kwargs)[0],'403 Forbidden')
        self.auth.set_admin('operator',False)
        self.assertEqual(self.request('admin/reset-club',payload)[0],'403 Forbidden')
        self.auth.set_admin('operator')
        self.assertEqual(self.request('admin/reset-club',{**payload,'confirmation':'wrong club'})[0],'400 Bad Request')
        self.assertEqual(self.request('admin/reset-club',payload)[0],'409 Conflict')
        self.assertEqual(self.owner.fut_inventory.state,before)
        self.assertFalse((self.path.parent/'club-reset-backups').exists())

    def test_whole_club_starts_fresh_but_account_website_login_other_club_and_old_ids_survive(self):
        store=self.owner.fut_inventory
        fut_matches.create(self.root,self.a.session,{'type':'OFFLINE','squadId':1})
        current=self.a.session.local_fut_match
        ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
        fut_matches.play(self.root,self.a.session,{'items':[{'id':i} for i in ids]})
        report={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,'items':[{'id':i} for i in ids],
                'matchData':'','myMatchStats':dict(dict.fromkeys(fut_results.STATS,0),goals=1),
                'opponentMatchStats':dict.fromkeys(fut_results.STATS,0)}
        fut_results.finish(self.root,self.a.session,report)
        self.assertEqual(len(store.state['matchHistory']),1)
        state=deepcopy(store.state);state.update(coins=987654,autoRenewContracts=True)
        state['items'][0].update(overrides={'rating':150},statistics={'gamesPlayed':10,'goals':20,'assists':3,'yellowCards':1,'redCards':0})
        store.commit(state)
        fut_squads.create(self.owner,{'squadName':'Extra squad'})
        before=deepcopy(store.state);profile=deepcopy(self.owner.profile)
        other=deepcopy(self.b.session.owner.fut_inventory.state)
        self.a.close();self.root.settings.replace(Settings({'economy':{'starting_coins':23456}}))
        payload=self.payload();status,result=self.request('admin/reset-club',payload)
        self.assertEqual(status,'200 OK');self.assertTrue(result['reset'])
        expected=fut_inventory.Inventory.initial_state(self.owner.settings)
        expected.update(nextItemId=before['nextItemId'],nextPackId=before['nextPackId'],clubReset=store.state['clubReset'])
        self.assertEqual(store.state,expected);self.assertEqual(store.state['coins'],23456)
        self.assertEqual(self.owner.profile['persona'],profile['persona'])
        self.assertEqual(self.owner.profile['account'],profile['account'])
        self.assertEqual(self.owner.profile['settings'],profile['settings'])
        self.assertEqual(self.owner.profile['fut_club']['name'],'England FC')
        self.assertEqual(self.b.session.owner.fut_inventory.state,other)
        self.assertTrue(self.auth.session(self.token)['is_admin'])
        self.assertIsNotNone(self.auth.session(self.friend))
        backups=list((self.path.parent/'club-reset-backups').glob('*/*'))
        self.assertEqual(len(backups),1)
        self.assertEqual(json.loads((backups[0]/'fut-inventory.json').read_text()),before)
        self.assertEqual(json.loads((backups[0]/'profile.json').read_text()),profile)
        self.assertNotIn(self.token,(backups[0]/'reset.json').read_text())
        if __import__('sys').platform!='win32':self.assertEqual((backups[0]/'fut-inventory.json').stat().st_mode&0o777,0o600)
        self.assertEqual(self.request('admin/reset-club',payload)[0],'409 Conflict')
        restored=Backend(self.path,multiuser=True,settings=self.owner.settings).for_user(101)
        self.assertEqual(restored.fut_inventory.state,store.state)
        self.assertEqual(restored.profile['fut_club'],self.owner.profile['fut_club'])
        restored.save()
        restored.profile['fut_club']['name']='Later name';restored.save()
        self.assertEqual(Backend(self.path).profile['fut_club']['name'],'Later name')

    def test_nonlegacy_club_reset_is_isolated_and_preserves_its_persona_mapping(self):
        target=self.b.session.owner;before=deepcopy(self.owner.fut_inventory.state)
        registry=deepcopy(self.root.accounts);persona=target.profile['persona']
        self.b.close()
        self.assertEqual(self.request('admin/reset-club',self.payload(target))[0],'200 OK')
        self.assertEqual(target.fut_inventory.state['items'],[])
        self.assertEqual(self.owner.fut_inventory.state,before);self.assertEqual(self.root.accounts,registry)
        restored=Backend(self.path,multiuser=True,settings=Settings()).for_user(202)
        self.assertEqual(restored.profile['persona'],persona)
        self.assertEqual(restored.fut_inventory.state,target.fut_inventory.state)

    def test_changed_inventory_stales_confirmation_and_failure_leaves_live_club_intact(self):
        self.a.close();payload=self.payload();store=self.owner.fut_inventory
        state=deepcopy(store.state);state['coins']+=1;store.commit(state)
        self.assertEqual(self.request('admin/reset-club',payload)[0],'409 Conflict')
        payload=self.payload();before=deepcopy(store.state);profile=deepcopy(self.owner.profile);raw=store.path.read_bytes()
        with patch.object(store,'commit',side_effect=OSError('disk full')):
            self.assertEqual(self.request('admin/reset-club',payload)[0],'503 Service Unavailable')
        self.assertEqual(store.state,before);self.assertEqual(self.owner.profile,profile)
        self.assertEqual(store.path.read_bytes(),raw)
        with patch('club_reset.private_json',side_effect=OSError('backup full')):
            self.assertEqual(self.request('admin/reset-club',payload)[0],'503 Service Unavailable')
        self.assertEqual(store.state,before)

    def test_pre_reset_quotes_and_paid_receipts_cannot_purchase_again_after_the_reset(self):
        self.a.close()
        status,old=self.request('quote',{'size':1,'edition':'standard'})
        self.assertEqual(status,'200 OK')
        self.assertEqual(self.request('open',{'quote_token':old['quote_token'],'duplicate_consent':True})[0],'200 OK')
        status,unused=self.request('quote',{'size':1,'edition':'standard'})
        self.assertEqual(status,'200 OK')
        self.assertEqual(self.request('admin/reset-club',self.payload())[0],'200 OK')
        before=deepcopy(self.owner.fut_inventory.state)
        for quoted in (old,unused):
            self.assertEqual(self.request('open',{'quote_token':quoted['quote_token'],'duplicate_consent':True})[0],'400 Bad Request')
        self.assertEqual(self.owner.fut_inventory.state,before)
        status,new=self.request('quote',{'size':1,'edition':'standard'})
        self.assertEqual(status,'200 OK')
        status,opened=self.request('open',{'quote_token':new['quote_token'],'duplicate_consent':True})
        self.assertEqual(status,'200 OK')
        self.assertGreaterEqual(opened['cards'][0]['id'],before['nextItemId'])
        self.assertEqual(len(self.owner.fut_inventory.state['items']),1)
        self.assertEqual(self.request('open',{'quote_token':new['quote_token'],'duplicate_consent':True})[1],opened)


if __name__=='__main__':unittest.main()
