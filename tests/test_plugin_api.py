"""Plugin lookups and presence use real login/logout paths and saved clubs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import fut_inventory
import fut_matches
import fut_results
import fut_services
import fut_squads
from server_plugins import PluginAPI, PluginManager, EventBus
from server_settings import Settings
from sessions import Backend, Connection
import tdf


def frame(component, command): return {'component':component, 'command':command, 'message':1, 'kind':0}


def login(backend, external=101, name='Test_Player', *, postauth=True):
    query=urlencode({'response_type':'code','redirect_uri':backend.identity.callback})
    _,_,headers=backend.identity.response('GET','/connect/auth?'+query)
    code=parse_qs(urlsplit(dict(headers)['Location']).query)['code'][0]
    connection=Connection(backend)
    blob=name.encode().ljust(36,b'\0')
    connection.response(frame(1,10),tdf.str_field('AUTH',code)+tdf.blob_field('EXTB',blob)+tdf.int_field('EXTI',external))
    if postauth:connection.response(frame(9,8),b'')
    return connection


def enter_fut(backend, connection):
    _,body,_=backend.identity.response('GET','/connect/auth?'+urlencode({
        'response_type':'code','client_id':'FOS-SERVER','redirect_uri':'nucleus:rest'}))
    return fut_services.authenticate(backend,{'sku':'FFA19PS3','method':'authcode',
        'nucleusPersonaPlatform':'ps3','clientVersion':3,'isReadOnly':False,
        'nuc':connection.session.account,'nucleusPersonaId':connection.session.persona,
        'identification':{'redirectUrl':'nucleus:rest','authCode':json.loads(body)['code']}})


def seed_club(owner):
    state=deepcopy(owner.fut_inventory.state)
    players=sorted(fut_inventory.CATALOG,key=lambda p:p['preferredPosition']!='GK')
    state['items']=[{'id':10000000+i,'assetId':p['assetId'],'pile':'club','timestamp':1}
                    for i,p in enumerate(players[:23])]
    state['nextItemId']=10000000+len(state['items'])
    owner.fut_inventory.commit(state)
    fut_squads.prepare_owned_xi(owner)


class PluginAPITests(unittest.TestCase):
    def prepare(self, path=None, rules=None):
        backend=Backend(path,multiuser=True,settings=rules)
        api=PluginAPI(backend.events,'test',Path('/tmp/plugin-api-test'),{})
        return backend,api

    def test_presence_postauth_fut_logout_disconnect_are_distinct_and_idempotent(self):
        backend,api=self.prepare();observed=[]
        for event in ('player.created','player.online','player.fut_entered','player.login',
                      'player.fut_left','player.logout','player.offline'):
            backend.events.subscribe(event,lambda payload:observed.append((payload,api.get_player(payload['player']['persona_id']))))
        self.assertEqual(api.get_players(),[])
        c=login(backend);persona=c.session.persona
        c.response(frame(9,8),b'')
        self.assertEqual([e[0]['event'] for e in observed],['player.created','player.online'])
        self.assertTrue(api.get_player(persona)['online']);self.assertFalse(api.get_player(persona)['in_fut'])
        enter_fut(backend,c);enter_fut(backend,c)
        self.assertEqual(len(api.get_players(in_fut=True)),1)
        key=c.session.fut_key
        raw=b'POST /ut/delete/auth HTTP/1.1\r\nX-UT-SID: '+key.encode()+b'\r\n\r\n'
        self.assertEqual(fut_services.response(backend,'POST',fut_services.LOGOUT,raw)[0],'200 OK')
        self.assertTrue(api.get_player(persona)['online']);self.assertFalse(api.get_player(persona)['in_fut'])
        c.response(frame(1,70),b'');c.close()
        self.assertEqual([e[0]['event'] for e in observed],['player.created','player.online',
            'player.fut_entered','player.login','player.fut_left','player.logout','player.offline'])
        self.assertFalse(observed[-1][1]['online']);self.assertEqual(observed[-1][0]['reason'],'logout')
        self.assertEqual(api.get_player(persona)['name'],'Test_Player')
        self.assertIsNotNone(api.get_player(persona)['last_seen'])

    def test_disconnect_inside_fut_and_graceful_shutdown_leave_once(self):
        backend,api=self.prepare();events=[]
        for event in ('player.fut_left','player.offline'):backend.events.subscribe(event,events.append)
        first=login(backend);enter_fut(backend,first)
        first.close();first.close()
        self.assertEqual([(e['event'],e['reason']) for e in events],
                         [('player.fut_left','disconnect'),('player.offline','disconnect')])
        second=login(backend,202,'Other_Player');enter_fut(backend,second)
        backend.close_sessions();second.close()
        self.assertEqual([(e['event'],e['reason']) for e in events[-2:]],
                         [('player.fut_left','server_shutdown'),('player.offline','server_shutdown')])
        self.assertEqual(api.get_players(online=True),[])

    def test_offline_players_clubs_cards_squads_survive_restart_without_save_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';backend,api=self.prepare(path)
            a=login(backend,101,'Player_One');b=login(backend,202,'Player_Two')
            for c in (a,b):seed_club(c.backend)
            other=fut_squads.create(b.backend,{'squadName':'Second XI'})['id']
            personas=[c.session.persona for c in (a,b)]
            a.close();b.close()
            before={p:p.read_bytes() for p in Path(directory).rglob('*.json')}
            restored,read=self.prepare(path)
            self.assertEqual([p['name'] for p in read.get_players()],['Player_One','Player_Two'])
            self.assertTrue(all(not p['online'] for p in read.get_players()))
            self.assertEqual([c['persona_id'] for c in read.get_clubs()],personas)
            cards=read.get_cards(personas[1],pile='club',min_rating=100,count=1000)
            self.assertEqual(len(cards),23)
            self.assertEqual(read.get_card(personas[1],cards[0]['item_id']),cards[0])
            self.assertEqual(len(read.get_squads(personas[1])),2)
            self.assertFalse(read.get_squad(personas[1],other)['active'])
            self.assertEqual(len(read.get_squad(personas[1])['players']),23)
            self.assertEqual(len(read.get_equipment(personas[1],active_only=True)),5)
            self.assertTrue(read.get_equipment(personas[1],item_type='stadium'))
            self.assertEqual(restored.users,{})
            self.assertEqual({p:p.read_bytes() for p in Path(directory).rglob('*.json')},before)

    def test_snapshots_and_config_never_expose_tokens_or_mutate_live_records(self):
        rules=Settings({'plugins':{'settings':{'other':{'token':'private-plugin-token'}}}})
        backend,api=self.prepare(rules=rules);c=login(backend);seed_club(c.backend);enter_fut(backend,c)
        c.session.web_token='private-web-token';c.session.pow_key='private-pow-key'
        before=deepcopy(c.backend.fut_inventory.state)
        club=api.get_club(c.session.persona);club['coins']=0;club['equipment'][0]['assetId']=0
        squad=api.get_squad(c.session.persona);squad['players'][0]['card']['rating']=0
        cards=api.get_cards(c.session.persona);cards[0]['condition']['suspension']=99
        self.assertEqual(c.backend.fut_inventory.state,before)
        serialized=json.dumps([api.get_player(c.session.persona),api.get_club(c.session.persona),api.get_config()])
        for secret in (c.session.key,c.session.fut_key,c.session.web_token,c.session.pow_key,'private-plugin-token'):
            self.assertNotIn(secret,serialized)
        self.assertNotIn('settings',api.get_config()['plugins'])
        config=api.get_config();config['economy']['starting_coins']=0
        self.assertEqual(api.get_config()['economy']['starting_coins'],20000)

    def test_catalogue_filters_pagination_packs_and_rankings(self):
        backend,api=self.prepare();c=login(backend)
        messi=api.get_catalogue_card(158023)
        self.assertEqual((messi['name'],messi['rating']),('Lionel Messi',94))
        matches=api.search_catalogue(name='messi',min_rating=90,rare=True)
        self.assertTrue(any(card['asset_id']==158023 for card in matches))
        first=api.search_catalogue(count=2);second=api.search_catalogue(start=1,count=1)
        self.assertEqual(first[1:],second)
        self.assertFalse(any(p['pack_id']==3 for p in api.get_packs()))
        self.assertTrue(api.get_pack(3)['retired'])
        self.assertEqual(api.get_rankings()[0]['persona_id'],c.session.persona)
        for function in (lambda:api.get_cards(c.session.persona,count=True),
                         lambda:api.get_cards(c.session.persona,rare=1),
                         lambda:api.get_cards(c.session.persona,pile='trade'),
                         lambda:api.search_catalogue(edition='unknown'),
                         lambda:api.search_catalogue(min_rating=100,max_rating=50),
                         lambda:api.get_players(online='yes'),lambda:api.get_player(True)):
            with self.assertRaises(ValueError):function()
        with self.assertRaises(KeyError):api.get_player(9999999)
        with self.assertRaises(KeyError):api.get_card(c.session.persona,9999999)

    def test_committed_matches_are_readable_by_owner_without_raw_report_data(self):
        backend,api=self.prepare();c=login(backend);seed_club(c.backend)
        fut_matches.create(backend,c.session,{'type':'OFFLINE','squadId':1,'seasonId':1,'divisionId':1})
        current=c.session.local_fut_match
        ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
        fut_matches.play(backend,c.session,{'items':[{'id':i} for i in ids]})
        payload={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,'matchData':'',
                 'items':[{'id':i} for i in ids],'myMatchStats':{k:0 for k in fut_results.STATS},
                 'opponentMatchStats':{k:0 for k in fut_results.STATS}}
        payload['myMatchStats']['goals']=1
        fut_results.finish(backend,c.session,payload)
        matches=api.get_matches(c.session.persona,mode='OFFLINE')
        self.assertEqual(len(matches),1)
        self.assertEqual(api.get_match(c.session.persona,current['id']),matches[0])
        self.assertEqual(matches[0]['coins'],1250)  # Includes goal and clean-sheet performance coins.
        self.assertNotIn('matchData',matches[0]);self.assertNotIn('requestHash',matches[0])
        self.assertEqual(api.get_matches(c.session.persona,mode='ONLINE'),[])

    def test_plugin_can_query_from_registration_and_presence_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plugins=root/'plugins';plugins.mkdir();logs=[]
            (plugins/'presence.py').write_text("def register(api):\n api.log(str(len(api.get_players())))\n @api.on('player.online')\n def joined(event):\n  api.log(api.get_club(event['player']['persona_id'])['name'])\n")
            backend=Backend(multiuser=True)
            manager=PluginManager(plugins,root/'plugin-data',backend.settings,logs.append).bind_backend(backend)
            backend.events=manager;backend.fut_inventory.events=manager
            self.assertEqual(manager.load(),['presence'])
            c=login(backend)
            self.assertEqual([log['message'] for log in logs],['0','England FC'])
            c.close()

    def test_querying_events_and_other_threads_obey_root_lock_order(self):
        backend,api=self.prepare();c=login(backend)
        errors=[];counts=[];threads=[]
        backend.events.subscribe('match.completed',lambda event:counts.append(len(api.get_players(online=True))))
        def publish():
            try:
                for _ in range(30):backend.events.emit('match.completed')
            except Exception as error:errors.append(error)
        def locked_publish():
            try:
                for _ in range(30):
                    with backend.lock:backend.events.emit('match.completed')
            except Exception as error:errors.append(error)
        for target in (publish,locked_publish):
            t=threading.Thread(target=target,daemon=True);threads.append(t);t.start()
        for t in threads:t.join(timeout=3)
        self.assertFalse(any(t.is_alive() for t in threads),'Query callback deadlock')
        self.assertEqual(errors,[]);self.assertEqual(counts,[1]*60)
        c.close()

    def test_offline_lookup_validates_identity_and_does_not_rewrite_corrupt_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json';backend,api=self.prepare(path)
            a=login(backend);b=login(backend,202,'Other_Player')
            persona=b.session.persona;owner_path=b.backend.path
            a.close();b.close()
            value=json.loads(owner_path.read_text());value['persona']+=100
            owner_path.write_text(json.dumps(value))
            before=owner_path.read_bytes();restored,api=self.prepare(path)
            with self.assertRaises(ValueError):api.get_player(persona)
            self.assertEqual(owner_path.read_bytes(),before);self.assertEqual(restored.users,{})

    def test_card_ids_are_scoped_to_clubs_and_incomplete_login_is_not_online(self):
        backend,api=self.prepare();a=login(backend);b=login(backend,202,'Other_Player')
        seed_club(a.backend);seed_club(b.backend)
        foreign=b.backend.fut_inventory.state['items'][-1]['id']
        b.backend.fut_inventory.discard({'itemId':[foreign]})
        self.assertEqual(api.get_card(a.session.persona,foreign)['item_id'],foreign)
        with self.assertRaises(KeyError):api.get_card(b.session.persona,foreign)
        observed=[];backend.events.subscribe('player.offline',observed.append)
        pending=login(backend,303,'Pending_Player',postauth=False)
        self.assertFalse(api.get_player(pending.session.persona)['online'])
        pending.close()
        self.assertEqual(observed,[])


if __name__=='__main__':unittest.main()
