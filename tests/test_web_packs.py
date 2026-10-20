"""Club-scoped authentication, real inventory commits and server-priced web packs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from http.cookies import SimpleCookie
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe import Capture, Handler
from server_settings import Settings
from sessions import Backend, Session
from web_accounts import WebAccounts, AuthError, digest
from web_app import WebApp, COOKIE
from accounts import user_key
import web_packs
import fut_packs


class WebPackTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.backend=Backend(self.root/'state/profile.json',multiuser=True,
                             settings=Settings({'economy':{'starting_coins':10000000}}))
        self.backend.save();self.backend.fut_inventory.ensure_saved()
        self.backend.for_user(111)
        self.other=self.backend.for_user(222)
        self.auth=WebAccounts(self.root/'state/web-accounts.sqlite3')
        self.app=WebApp(self.backend,self.auth)
        self.password='synthetic website password'
        code=self.auth.invite(self.backend.profile['persona'])
        self.token=self.auth.register('test_user',self.password,code)

    def request(self,route,data=None,token=None,origin='http://127.0.0.1:8099',csrf=True,host='127.0.0.1:8099'):
        token=self.token if token is None else token
        body=json.dumps(data).encode() if data is not None else b''
        headers=f'Host: {host}\r\nCookie: {COOKIE}={token}\r\n'
        if data is not None:
            headers+=f'Origin: {origin}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n'
            if csrf:headers+='X-CSRF-Token: '+self.app.csrf(token)+'\r\n'
        method='POST' if data is not None else 'GET'
        raw=f'{method} /club/api/{route} HTTP/1.1\r\n{headers}\r\n'.encode()+body
        status,content,mime,extra=self.app.handle(method,'/club/api/'+route,raw,'127.0.0.1')
        return status,json.loads(content),extra

    def quote(self,params=None,owner=None):
        owner=owner or self.backend
        q,_=web_packs.quote(params or {'size':3,'level':'gold'},owner.settings)
        self.assertTrue(q['available'])
        token,expires=self.auth.quote(owner.profile['persona'],q)
        return token,q

    def test_registration_requires_single_use_club_invite_and_passwords_are_hashed(self):
        with self.assertRaises(AuthError):self.auth.register('intruder',self.password,'not-a-club-invite')
        code=self.auth.invite(self.other.profile['persona'])
        token=self.auth.register('friend',self.password,code)
        self.assertEqual(self.auth.session(token)['persona'],self.other.profile['persona'])
        with self.assertRaises(AuthError):self.auth.register('another_friend',self.password,code)
        self.assertNotIn(self.password.encode(),self.auth.path.read_bytes())
        self.assertNotIn(token.encode(),self.auth.path.read_bytes())
        self.assertNotIn(code.encode(),self.auth.path.read_bytes())
        reloaded=WebAccounts(self.auth.path)
        self.assertEqual(reloaded.session(reloaded.login('FRIEND',self.password))['persona'],self.other.profile['persona'])
        self.auth.logout(token);self.assertIsNone(self.auth.session(token))

    def test_invites_expire_and_reset_revokes_old_sessions_without_changing_club(self):
        now=[time.time()]
        auth=WebAccounts(self.auth.path,clock=lambda:now[0])
        code=auth.invite(self.other.profile['persona'],minutes=1)
        now[0]+=61
        with self.assertRaises(AuthError):auth.register('late_friend',self.password,code)
        state=deepcopy(self.backend.fut_inventory.state)
        auth.invite(self.backend.profile['persona'],reset=True)
        self.assertIsNone(auth.session(self.token))
        self.assertEqual(self.backend.fut_inventory.state,state)

    def test_every_added_filter_increases_price_and_filters_match_final_cards(self):
        values={'size':1}
        old=web_packs.quote(values,self.backend.settings)[0]['price']
        for key,value in [('club',45),('nation',38),('league',31),('position','ST'),
                          ('level','gold'),('rare_only',True),('edition','prime'),('min_rating',95)]:
            values[key]=value
            q,pool=web_packs.quote(values,self.backend.settings)
            self.assertGreater(q['price'],old);old=q['price']
        self.assertIn(20801,pool)
        for asset in pool:
            base=fut_packs.STANDARD_PLAYERS[asset]
            self.assertEqual((base['teamid'],base['nation'],base['leagueId'],base['preferredPosition']), (45,38,31,'ST'))
            self.assertGreaterEqual(base['rating']+10,95)
        with self.assertRaises(web_packs.PackError):web_packs.quote({'price':1},self.backend.settings)
        with self.assertRaises(web_packs.PackError):web_packs.quote({'size':True},self.backend.settings)

    def test_empty_pool_cannot_charge_and_rule_changes_require_a_new_quote(self):
        token,q=self.quote()
        self.backend.settings.replace(Settings({'web_packs':{'filter_surcharge':1500}}))
        before=deepcopy(self.backend.fut_inventory.state)
        with self.assertRaisesRegex(web_packs.PackError,'rules changed'):
            web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        self.assertEqual(self.backend.fut_inventory.state,before)
        q,pool=web_packs.quote({'club':99999999},self.backend.settings)
        self.assertEqual(pool,[]);self.assertFalse(q['available'])
        token,_=self.auth.quote(self.backend.profile['persona'],q)
        with self.assertRaises(web_packs.PackError):web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        self.assertEqual(self.backend.fut_inventory.state,before)

    def test_prime_open_saves_real_coins_cards_stats_and_is_idempotent_after_restart(self):
        token,q=self.quote({'size':3,'edition':'prime','min_rating':90,'nation':14,'league':13})
        before=self.backend.fut_inventory.state['coins'];events=[]
        self.backend.events.subscribe('web.pack.opened',events.append)
        with ThreadPoolExecutor(max_workers=5) as executor:
            results=list(executor.map(lambda _:web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True),range(5)))
        self.assertTrue(all(row==results[0] for row in results))
        result=results[0]
        self.assertEqual(result['coins'],before-q['price'])
        self.assertEqual(len(events),1)
        self.assertEqual(len(self.backend.fut_inventory.state['items']),3)
        for card in result['cards']:
            base=fut_packs.STANDARD_PLAYERS[card['assetId']]
            self.assertGreaterEqual(card['rating'],90)
            self.assertEqual(card['rating'],base['rating']+10)
            self.assertEqual([r['value'] for r in card['attributeList']],[r['value']+10 for r in base['attributeList']])
            self.assertEqual(card['resourceId']>>24,1)
        restored=Backend(self.backend.path,multiuser=True,settings=self.backend.settings)
        auth=WebAccounts(self.auth.path)
        self.assertEqual(web_packs.open_pack(restored,auth,token,duplicate_consent=True),result)
        self.assertEqual(restored.fut_inventory.state,self.backend.fut_inventory.state)
        self.assertEqual(self.other.fut_inventory.state['items'],[])

    def test_duplicate_versions_refund_normally_and_do_not_replace_existing_cards(self):
        token,q=self.quote({'size':1,'level':'gold'})
        with patch.object(fut_packs.random.SystemRandom,'sample',return_value=[20801]),patch.object(fut_packs.random.SystemRandom,'randrange',return_value=9999):
            first=web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
            token,q=self.quote({'size':1,'level':'gold'})
            second=web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        self.assertFalse(first['cards'][0]['duplicate'])
        self.assertTrue(second['cards'][0]['duplicate'])
        self.assertEqual(second['duplicate_refund'],600)
        self.assertEqual(len(self.backend.fut_inventory.state['items']),1)
        self.assertEqual(second['coins'],first['coins']-q['price']+600)

    def test_failed_save_and_live_match_cannot_charge_or_grant_cards(self):
        token,q=self.quote();store=self.backend.fut_inventory;before=deepcopy(store.state);saved=store.path.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        self.assertEqual(store.state,before);self.assertEqual(store.path.read_bytes(),saved)
        session=Session(1000001,1100000001,'Synthetic','key',0,0,owner=self.backend)
        session.local_fut_match={'playing':True,'finished':False}
        self.backend.sessions[session.key]=session
        with self.assertRaises(web_packs.PackError):web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        self.assertEqual(store.state,before)
        session.local_fut_match['finished']=True
        self.assertEqual(len(web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)['cards']),3)

    def test_club_scope_csrf_origin_and_cookie_flags(self):
        status,me,_=self.request('me');self.assertEqual(status,'200 OK')
        self.assertEqual(me['persona'],self.backend.profile['persona'])
        for kwargs in ({'origin':'https://attacker.invalid'},{'csrf':False},{'host':'attacker.invalid'}):
            status,_,_=self.request('quote',{'size':1},**kwargs);self.assertTrue(status.startswith('403'))
        status,_,_=self.request('me',token='not-a-session');self.assertTrue(status.startswith('401'))
        status,_,headers=self.request('login',{'username':'test_user','password':self.password},token='')
        self.assertEqual(status,'200 OK')
        cookie=dict(headers)['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        token,q=self.quote(owner=self.other)
        status,_,_=self.request('open',{'quote_token':token,'duplicate_consent':True});self.assertTrue(status.startswith('400'))
        self.assertEqual(self.backend.fut_inventory.state['items'],[])

    def test_offline_web_club_and_later_game_login_share_the_same_loaded_inventory(self):
        persona=self.other.profile['persona']
        code=self.auth.invite(persona)
        token=self.auth.register('friend',self.password,code)
        self.backend.users.pop(user_key(222))
        root_before=deepcopy(self.backend.fut_inventory.state)
        status,q,_=self.request('quote',{'size':1,'edition':'prime','min_rating':95},token=token)
        self.assertEqual(status,'200 OK')
        status,result,_=self.request('open',{'quote_token':q['quote_token'],'duplicate_consent':True},token=token)
        self.assertEqual(status,'200 OK')
        owner=self.app.owner(persona)
        self.assertIs(owner,self.backend.for_user(222))
        self.assertIs(owner.lock,self.backend.lock)
        self.assertEqual(owner.fut_inventory.state['coins'],result['coins'])
        self.assertEqual(len(owner.fut_inventory.state['items']),1)
        self.assertEqual(self.backend.fut_inventory.state,root_before)

    def test_browser_passwords_cookies_and_invites_are_never_written_to_protocol_captures(self):
        capture=Capture(self.root/'logs',self.backend);capture.web=self.app
        payload=json.dumps({'username':'test_user','password':self.password}).encode()
        wire=(f'POST /club/api/login HTTP/1.1\r\nHost: 127.0.0.1:8099\r\nOrigin: http://127.0.0.1:8099\r\nContent-Type: application/json\r\nCookie: {COOKIE}={self.token}\r\nContent-Length: {len(payload)}\r\n\r\n').encode()+payload
        class Socket:
            def __init__(self):self.input=bytearray(wire);self.output=bytearray()
            def settimeout(self,_):pass
            def recv(self,count):value=bytes(self.input[:count]);del self.input[:count];return value
            def sendall(self,data):self.output.extend(data)
        sock=Socket();server=SimpleNamespace(capture=capture,server_address=('127.0.0.1',8099),blaze_port=8990)
        with redirect_stdout(io.StringIO()):Handler(sock,('127.0.0.1',50000),server)
        self.assertIn(b'200 OK',sock.output)
        for path in (self.root/'logs').iterdir():
            content=path.read_bytes()
            self.assertNotIn(self.password.encode(),content);self.assertNotIn(self.token.encode(),content)
        self.assertFalse(list((self.root/'logs').glob('http-*.bin')))

    def test_expired_quote_cannot_recharge_after_its_receipt_is_pruned(self):
        token,q=self.quote();web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        state=deepcopy(self.backend.fut_inventory.state);state['webPackReceipts']={}
        self.backend.fut_inventory.commit(state)
        self.auth.clock=lambda:time.time()+601
        with self.assertRaises(AuthError):web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
        self.assertEqual(self.backend.fut_inventory.state,state)

    def test_toty_and_otw_web_versions_above_115_keep_distinct_ids_and_durable_receipts(self):
        for edition,rating,version in (('prime',104,1),('toty',114,2),('otw',119,3)):
            token,q=self.quote({'size':1,'edition':edition,'club':45,'nation':38,'position':'ST','min_rating':rating})
            with patch.object(fut_packs.random.SystemRandom,'sample',return_value=[20801]):
                result=web_packs.open_pack(self.backend,self.auth,token,duplicate_consent=True)
            card=result['cards'][0]
            self.assertEqual((card['edition'],card['rating'],card['resourceId']>>24),(edition,rating,version))
            restored=Backend(self.backend.path,multiuser=True,settings=self.backend.settings)
            self.assertEqual(web_packs.open_pack(restored,self.auth,token,duplicate_consent=True),result)
        self.assertEqual(len({r['variant']['version'] for r in self.backend.fut_inventory.state['items']}),3)


if __name__=='__main__':unittest.main()
