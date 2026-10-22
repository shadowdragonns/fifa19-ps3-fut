"""Administrator grants, owned-card edits and atomic in-game propagation."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import admin_cards
import card_rebalance
import fut_matches
import fut_squads
from sessions import Backend
from server_settings import Settings
from web_accounts import WebAccounts
from web_app import WebApp,COOKIE
from test_plugin_api import login,seed_club


class AdminCardTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'state/profile.json'
        self.root=Backend(self.path,multiuser=True)
        self.a=login(self.root,101,'First');self.b=login(self.root,202,'Second')
        for c in (self.a,self.b):seed_club(c.session.owner)
        self.owner=self.a.session.owner
        self.auth=WebAccounts(self.path.with_name('web-accounts.sqlite3'))
        self.token=self.auth.register('operator','synthetic admin password',self.auth.invite(self.owner.profile['persona']))
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

    def card(self,persona=None,**extra):
        status,result=self.request('admin/inventory',{'persona':persona or self.a.session.persona,**extra})
        self.assertEqual(status,'200 OK');return result['cards'][0]

    def payload(self,card,**changes):
        return {'persona':self.a.session.persona,'item_id':card['id'],'revision':card['revision'],
                'overrides':{},'condition':{},**changes}

    def test_ordinary_accounts_cannot_read_or_mutate_other_clubs_and_revocation_is_immediate(self):
        for route,data in (('admin/clubs',None),('admin/inventory',{'persona':self.a.session.persona}),
                           ('admin/edit',self.payload(self.card()))):
            self.assertEqual(self.request(route,data,token=self.friend)[0],'403 Forbidden')
        self.assertFalse(self.request('me',token=self.friend)[1]['is_admin'])
        self.auth.set_admin('operator',False)
        self.assertEqual(self.request('admin/clubs')[0],'403 Forbidden')
        self.auth.set_admin('operator');self.assertTrue(self.request('me')[1]['is_admin'])
        self.assertEqual({r['persona'] for r in self.request('admin/clubs')[1]['clubs']},
                         {self.a.session.persona,self.b.session.persona})
        reloaded=WebAccounts(self.auth.path);self.assertTrue(reloaded.session(self.token)['is_admin'])
        # Website registration/login cannot ask for an administrator role.
        self.assertEqual(self.request('login',{'username':'friend','password':'synthetic friend password','is_admin':True})[0],'400 Bad Request')

    def test_squad_card_edit_reaches_native_squad_and_persists_without_changing_ids_or_other_clubs(self):
        card=self.card(squad_id=1);other=deepcopy(self.b.session.owner.fut_inventory.state)
        before=deepcopy(self.owner.fut_inventory.state)
        changes={'rating':140,'attributes':[111,112,113,114,115,116],'position':card['position']}
        condition={'fitness':63,'contract':90,'suspension':0,'injuryGames':0}
        payload=self.payload(card,overrides=changes,condition=condition)
        status,result=self.request('admin/edit',payload);self.assertEqual(status,'200 OK')
        self.assertEqual(result['card']['rating'],140)
        native=next(p['itemData'] for p in fut_squads.retrieve(self.root,self.a.session)['players'] if p['itemData']['id']==card['id'])
        original=next(r for r in before['items'] if r['id']==card['id'])
        self.assertEqual(native['assetId'],original['assetId'])
        self.assertEqual((native['rating'],[r['value'] for r in native['attributeList']]),(140,changes['attributes']))
        self.assertEqual((native['fitness'],native['contract']),(63,90))
        self.assertEqual(self.owner.fut_inventory.state['coins'],before['coins'])
        self.assertEqual(self.owner.fut_inventory.state['squad'],before['squad'])
        self.assertEqual(self.b.session.owner.fut_inventory.state,other)
        restored=Backend(self.path,multiuser=True).for_user(101)
        self.assertEqual(restored.fut_inventory.state,self.owner.fut_inventory.state)
        updated,_=card_rebalance.updated_state(restored.fut_inventory.state,Settings().section('custom_cards')['editions'])
        self.assertEqual(next(r for r in updated['items'] if r['id']==card['id'])['overrides'],changes)
        audit=updated['adminCardEdits'][-1]
        self.assertEqual((audit['actor'],audit['itemId']),('operator',card['id']))
        self.assertNotIn(self.token,json.dumps(audit))
        status,_=self.request('admin/edit',payload);self.assertEqual(status,'409 Conflict')
        fresh=result['card']
        self.assertEqual(self.request('admin/edit',self.payload(fresh))[0],'200 OK')
        self.assertNotIn('overrides',next(r for r in self.owner.fut_inventory.state['items'] if r['id']==card['id']))

    def test_bounds_csrf_match_lock_and_failed_commit_cannot_partially_change_inventory(self):
        card=self.card();before=deepcopy(self.owner.fut_inventory.state)
        for changes in ({'rating':256},{'rating':True},{'attributes':[10]*5},{'attributes':[0]*5+[2**32]},
                        {'position':'INVALID'}):
            self.assertEqual(self.request('admin/edit',self.payload(card,overrides=changes))[0],'400 Bad Request')
        for kwargs in ({'csrf':False},{'origin':'https://attacker.invalid'}):
            self.assertEqual(self.request('admin/edit',self.payload(card,overrides={'rating':130}),**kwargs)[0],'403 Forbidden')
        self.assertEqual(self.request('admin/edit',self.payload(card,condition={'fitness':100}))[0],'400 Bad Request')
        self.assertEqual(self.owner.fut_inventory.state,before)
        with patch.object(self.owner.fut_inventory,'commit',side_effect=OSError('disk full')):
            self.assertEqual(self.request('admin/edit',self.payload(card,overrides={'rating':130}))[0],'503 Service Unavailable')
        self.assertEqual(self.owner.fut_inventory.state,before)
        fut_matches.create(self.root,self.a.session,{'type':'OFFLINE','squadId':1})
        self.assertEqual(self.request('admin/edit',self.payload(card,overrides={'rating':130}))[0],'409 Conflict')
        self.assertEqual(self.owner.fut_inventory.state,before)

    def test_special_card_edits_survive_live_edition_rebalance_and_training_is_separate(self):
        import card_versions
        state=deepcopy(self.owner.fut_inventory.state)
        _,variant=card_versions.catalogue_player(self.owner.settings,'otw',20801)
        item_id=state['nextItemId'];state['nextItemId']+=1
        state['items'].append({'id':item_id,'assetId':20801,'edition':'otw','variant':variant,'pile':'club','timestamp':1,
                              'trainingBoost':[10]*6,'trainingDefinition':6000057,'trainingItemId':123})
        self.owner.fut_inventory.commit(state)
        card=self.card(search=str(item_id))
        self.assertEqual(card['id'],item_id)
        status,result=self.request('admin/edit',self.payload(card,overrides={'rating':150,'attributes':[135]*6}))
        self.assertEqual(status,'200 OK');self.assertEqual(result['card']['attributes'],[135]*6)
        record=next(r for r in self.owner.fut_inventory.state['items'] if r['id']==item_id)
        resource=card_versions.resource_id(record)
        settings=Settings({'custom_cards':{'editions':{'otw':{'rating_boost':40,'attribute_boost':40}}}})
        updated,count=card_rebalance.updated_state(self.owner.fut_inventory.state,settings.section('custom_cards')['editions'])
        self.assertGreater(count,0)
        changed=next(r for r in updated['items'] if r['id']==item_id)
        native=self.owner.fut_inventory.card(changed,settings)
        self.assertEqual((native['rating'],[s['value'] for s in native['attributeList']]),(150,[145]*6))
        self.assertEqual(card_versions.resource_id(changed),resource)

    def test_migration_preserves_existing_users_sessions_and_defaults_to_no_admin(self):
        path=self.path.with_name('old-accounts.sqlite3')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE users(username TEXT PRIMARY KEY,persona INTEGER UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL)')
            db.execute("INSERT INTO users VALUES('existing',100,'00','00')")
        auth=WebAccounts(path)
        with auth.db() as db:
            self.assertEqual(tuple(db.execute('SELECT username,persona,is_admin FROM users').fetchone()),('existing',100,0))
        auth.set_admin('existing')
        with auth.db() as db:self.assertEqual(db.execute('SELECT is_admin FROM users').fetchone()[0],1)

if __name__=='__main__':unittest.main()
