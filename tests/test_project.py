"""Publication-safe checks using generated TLS identities and synthetic users."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import audit_public
import build_bundle
import certificate_patch
import fut_inventory
import fut_matches
import fut_results
import fut_squads
import initialize_tls
import launch
from server_plugins import EventBus, PluginManager
from server_settings import Settings
from sessions import Backend, Session
from tls_identity import public_modulus
from tls_bridge import server_context, CIPHERS

HERE = Path(__file__).resolve().parents[1]


def club(backend):
    players = sorted(fut_inventory.CATALOG, key=lambda p:p['preferredPosition'] != 'GK')
    state = deepcopy(backend.fut_inventory.state)
    state['items'] = [{'id':10000000+i,'assetId':p['assetId'],'pile':'club','timestamp':1}
                      for i,p in enumerate(players[:23])]
    state['nextItemId'] = 10000000+len(state['items'])
    backend.fut_inventory.commit(state)
    fut_squads.prepare_owned_xi(backend)
    session = Session(backend.profile['persona'],backend.profile['account'],'Synthetic user',
                      'test-session',1,1,postauth=True,fut_key='test-fut',owner=backend)
    backend.sessions[session.key] = session
    return session


class PublicProjectTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for server setup tests')
    def test_fresh_tls_identity_is_private_and_never_overwrites_existing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = initialize_tls.initialize(root/'first')
            another = initialize_tls.initialize(root/'second')
            self.assertNotEqual(public_modulus(ca),public_modulus(another))
            before = (ca.parent/'root.key').read_bytes()
            with self.assertRaises(ValueError):initialize_tls.initialize(ca.parent)
            self.assertEqual((ca.parent/'root.key').read_bytes(),before)
            if sys.platform != 'win32':self.assertEqual((ca.parent/'root.key').stat().st_mode&0o777,0o600)
            subprocess.run(['openssl','verify','-CAfile',str(ca),str(ca.parent/'local.pem')],check=True,capture_output=True)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for generated test certificate')
    def test_custom_public_ca_generates_patch_without_elf_or_private_key_on_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); server = root/'server'; server.mkdir()
            ca = initialize_tls.initialize(server/'tls'); client=root/'client'; (client/'data').mkdir(parents=True)
            shutil.copy2(HERE/'data/client-native-patch.json',client/'data/client-native-patch.json')
            public = client/'root.pem'; shutil.copy2(ca,public)
            with patch.object(certificate_patch,'HERE',client), patch.object(certificate_patch,'ELF',root/'missing.elf'):
                generated = certificate_patch.prepare(True,True,trust_ca=public)
                rows = generated.read_text()
                modulus = public_modulus(public)
                for at in range(0,256,4):
                    self.assertIn(f'0x{certificate_patch.KEY_ADDRESS+at:08x}, 0x{int.from_bytes(modulus[at:at+4],"big"):08x}',rows)
                self.assertIn('calloc',rows)
                self.assertIn('0x005f9530, 0x38600000',rows)
            self.assertFalse(list(client.rglob('*.key')))

    def test_bad_certificate_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'root.pem'; path.write_text('not a certificate')
            with self.assertRaises(ValueError):public_modulus(path)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for generated TLS tests')
    def test_fresh_identity_completes_verified_tls12_static_rsa_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            ca=initialize_tls.initialize(Path(directory)/'tls')
            server=server_context(ca.parent/'local.pem',ca.parent/'local.key')
            client=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.minimum_version=client.maximum_version=ssl.TLSVersion.TLSv1_2
            client.set_ciphers(CIPHERS);client.load_verify_locations(cafile=str(ca))
            ci,co,si,so=(ssl.MemoryBIO() for _ in range(4))
            c=client.wrap_bio(ci,co,server_hostname='winter15.gosredirector.ea.com')
            s=server.wrap_bio(si,so,server_side=True)
            ready=set()
            for _ in range(20):
                for name,stream in (('client',c),('server',s)):
                    if name not in ready:
                        try:stream.do_handshake();ready.add(name)
                        except ssl.SSLWantReadError:pass
                while co.pending:si.write(co.read())
                while so.pending:ci.write(so.read())
                if len(ready)==2:break
            self.assertEqual(ready,{'client','server'})
            c.write(b'public setup test');si.write(co.read())
            self.assertEqual(s.read(),b'public setup test')

    def test_configured_packs_and_inventory_reload_preserve_coins(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Settings({'economy':{'starting_coins':50000},'packs':{'prices':{'6':100}}})
            backend = Backend(Path(directory)/'profile.json',settings=rules)
            store=backend.fut_inventory; events=[]; backend.events.subscribe('pack.opened',events.append)
            payload={'packId':6,'useCredits':1,'usePreOrder':0,'currency':'COINS'}
            purchase=store.open_pack(payload)
            self.assertEqual(store.state['coins'],49900)
            self.assertEqual(store.open_pack(payload),purchase)
            self.assertEqual(len(events),1)
            store.move({'itemData':[{'id':c['id'],'pile':'club'} for c in purchase['itemList']]})
            restored=Backend(backend.path,settings=Settings())
            self.assertEqual(restored.fut_inventory.state,store.state)

    def test_full_time_reward_snapshot_survives_rule_change_and_report_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=Backend(Path(directory)/'profile.json',settings=Settings({'economy':{'win_coins':1500}}))
            session=club(backend)
            fut_matches.create(backend,session,{'type':'OFFLINE','squadId':1})
            current=session.local_fut_match
            ids=[p['itemData']['id'] for p in current['squad']['players'] if p['index']<11]
            fut_matches.play(backend,session,{'items':[{'id':i} for i in ids]})
            report={'matchReportId':0,'endReason':'WIN','secondsPlayed':5400,'items':[{'id':i} for i in ids],
                    'matchData':'','myMatchStats':{k:0 for k in fut_results.STATS},
                    'opponentMatchStats':{k:0 for k in fut_results.STATS}}
            report['myMatchStats']['goals']=1
            events=[];backend.events.subscribe('match.completed',events.append)
            backend.events.subscribe('match.reward',lambda event,coins:coins+100,filter=True)
            first=fut_results.finish(backend,session,report)
            self.assertEqual(first['allCoins'],1600)
            self.assertEqual(fut_results.finish(backend,session,report),first)
            self.assertEqual(len(events),1)
            restored=Backend(backend.path,settings=Settings({'economy':{'win_coins':8000}}))
            self.assertEqual(fut_results.save_result(restored,current,report),first)
            self.assertEqual(restored.fut_inventory.state,backend.fut_inventory.state)

    def test_bans_include_the_native_red_card_notification_field(self):
        record={'id':1,'assetId':fut_inventory.CATALOG[0]['assetId'],'timestamp':1,'suspension':1}
        self.assertEqual(fut_inventory.Inventory.card(record)['statsList'],[{'index':4,'value':1}])
        record['suspension']=0
        self.assertEqual(fut_inventory.Inventory.card(record)['statsList'],[])

    def test_plugin_exceptions_and_context_mutation_do_not_escape(self):
        logs=[];bus=EventBus(logs.append);seen=[]
        def bad(event): event['player']['club_name']='changed';raise ValueError('private')
        bus.subscribe('match.completed',bad,'bad');bus.subscribe('match.completed',seen.append)
        bus.emit('match.completed',player={'club_name':'Test'})
        self.assertEqual(seen[0]['player']['club_name'],'Test')
        self.assertEqual(logs[0]['error_type'],'ValueError')
        self.assertNotIn('private',json.dumps(logs))
        bus.subscribe('match.reward',lambda event,coins:-1,filter=True)
        self.assertEqual(bus.reward(1000),1000)

    def test_example_plugin_loads_with_custom_bonus(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); plugins=root/'plugins';plugins.mkdir()
            shutil.copy2(HERE/'plugins/_example_bonus.py',plugins/'win_bonus.py')
            rules=Settings({'plugins':{'settings':{'win_bonus':{'bonus_coins':300}}}})
            bus=PluginManager(plugins,root/'plugin-data',rules)
            self.assertEqual(bus.load(),['win_bonus'])
            self.assertEqual(bus.reward(1000,result='WIN'),1300)
            self.assertEqual(bus.reward(500,result='LOSS'),500)

    def test_launcher_help_exposes_public_certificate_without_game_files(self):
        result=subprocess.run([sys.executable,str(HERE/'launch.py'),'--help'],check=True,capture_output=True,text=True)
        self.assertIn('--trust-ca',result.stdout);self.assertIn('--server',result.stdout)

    def test_bundles_exclude_keys_game_files_and_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            packages=build_bundle.build(Path(directory)/'dist')
            for path in packages:self.assertGreater(audit_public.bundle(path),10)

    def test_public_audit_rejects_private_inputs(self):
        for name,content in (('state/profile.json',b'{}'),('tls/root.pem',b'public'),
                             ('game.iso',b'data'),('EBOOT.BIN',b'data'),
                             ('notgame.txt',b'\x7fELF'),('credentials.txt',b'-----BEGIN '+b'PRIVATE KEY-----')):
            with self.subTest(name=name),self.assertRaises(ValueError):audit_public.check_file(name,content)


if __name__=='__main__':unittest.main()
