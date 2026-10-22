"""Graceful restart keeps credentials private, scoped and explicitly revocable."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fut_matches
import fut_services
from session_recovery import Recovery, WINDOW
from sessions import Backend
from test_plugin_api import login,enter_fut,frame,seed_club
import tdf


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'state/profile.json';self.now=1000000000
        self.root=Backend(self.path,multiuser=True)
        self.root.recovery=Recovery(self.root,self.path.with_name('connection-recovery.json'),clock=lambda:self.now)
        self.first=login(self.root,101,'Player_One');self.second=login(self.root,202,'Player_Two')
        for n,c in enumerate((self.first,self.second)):
            c.peer=c.session.peer='peer'+str(n)
            c.response(frame(9,1),tdf.str_field('CFID','OSDK_CORE'))
            self.root.extend_web_session(c.peer,c.client_route)
            enter_fut(self.root,c)

    def restart(self):
        self.root.recovery.snapshot()
        self.root.close_sessions()
        # Old socket cleanup must not revoke the just-written restart file.
        self.first.close();self.second.close()
        root=Backend(self.path,multiuser=True)
        root.recovery=Recovery(root,self.path.with_name('connection-recovery.json'),clock=lambda:self.now)
        self.assertEqual(root.recovery.restore(),2)
        return root

    def test_restart_restores_scoped_fut_sid_callback_tokens_and_cpu_reservation_without_save_changes(self):
        seed_club(self.first.backend)
        fut_matches.create(self.root,self.first.session,{'type':'OFFLINE','squadId':1})
        ids=[p['itemData']['id'] for p in self.first.session.local_fut_match['squad']['players'] if p['index']<11]
        fut_matches.play(self.root,self.first.session,{'items':[{'id':i} for i in ids]})
        original=deepcopy(self.first.session.local_fut_match)
        first_key,second_key=self.first.session.fut_key,self.second.session.fut_key
        route=self.first.client_route;tokens=(self.first.session.web_key,self.first.session.web_token)
        files={p:p.read_bytes() for p in self.path.parent.rglob('fut-inventory.json')}
        restored=self.restart()
        session=fut_services.session_for(restored,{'x-ut-sid':first_key})
        other=fut_services.session_for(restored,{'x-ut-sid':second_key})
        self.assertNotEqual(session.persona,other.persona)
        self.assertEqual(session.local_fut_match['id'],original['id'])
        self.assertEqual(list(session.local_fut_match['played_items']),list(original['played_items']))
        self.assertEqual(restored.extend_web_session('peer0',route),tokens)
        self.assertIsNone(restored.extend_web_session('peer1',route))
        self.assertIsNone(restored.extend_web_session('peer0','0'*32))
        self.assertEqual({p:p.read_bytes() for p in files},files)
        if os.name!='nt':self.assertEqual(restored.recovery.path.stat().st_mode&0o777,0o600)

    def test_fresh_blaze_login_reclaims_only_its_own_recovered_session(self):
        key=self.first.session.key;sid=self.first.session.fut_key;route=self.first.client_route
        root=self.restart()
        c=login(root,101,'Player_One')
        self.assertEqual((c.session.key,c.session.fut_key),(key,sid))
        self.assertEqual(c.session.recovery_until,0)
        self.assertIs(root.client_routes[route].session,c.session)
        with self.assertRaises(ValueError):login(root,101,'Player_One')

    def test_expired_recovery_is_not_extended_by_unused_restarts_or_accepted_by_sid_or_callback(self):
        key=self.first.session.fut_key;route=self.first.client_route
        root=self.restart();self.now+=WINDOW+1
        self.assertIsNone(fut_services.session_for(root,{'x-ut-sid':key}))
        self.assertIsNone(root.extend_web_session('peer0',route))
        root.recovery.snapshot()
        again=Backend(self.path,multiuser=True)
        self.assertEqual(Recovery(again,root.recovery.path,clock=lambda:self.now).restore(),0)

    def test_continued_authenticated_use_keeps_the_recovered_game_connected(self):
        key=self.first.session.fut_key;root=self.restart()
        for _ in range(3):
            self.now+=WINDOW-1
            self.assertIsNotNone(fut_services.session_for(root,{'x-ut-sid':key}))
        self.now+=WINDOW+1
        self.assertIsNone(fut_services.session_for(root,{'x-ut-sid':key}))

    def test_logout_and_disconnect_revoke_recovery_on_disk(self):
        first=self.first.session.fut_key;second=self.second.session.fut_key
        root=self.restart()
        raw=b'POST /ut/delete/auth HTTP/1.1\r\nX-UT-SID: '+first.encode()+b'\r\n\r\n'
        self.assertEqual(fut_services.response(root,'POST',fut_services.LOGOUT,raw)[0],'200 OK')
        c=login(root,202,'Player_Two');c.close()
        again=Backend(self.path,multiuser=True)
        self.assertEqual(Recovery(again,root.recovery.path,clock=lambda:self.now).restore(),0)
        self.assertIsNone(fut_services.session_for(again,{'x-ut-sid':first}))
        self.assertIsNone(fut_services.session_for(again,{'x-ut-sid':second}))

    def test_failed_revocation_can_retry_without_leaving_old_disk_credentials(self):
        key=self.first.session.key;root=self.restart()
        with patch.object(root.recovery,'_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):root.recovery.forget(key)
        self.assertIn(key,root.recovery.saved)
        root.recovery.forget(key)
        self.assertNotIn(key,json.dumps(json.loads(root.recovery.path.read_text())))

    def test_invalid_recovery_file_cannot_restore_an_unknown_club_or_malformed_credential(self):
        self.root.recovery.snapshot()
        path=self.root.recovery.path;original=json.loads(path.read_text())
        for mutate in (lambda v:v['sessions'][0]['session'].update(fut_key=False),
                       lambda v:v['sessions'][0]['session'].update(external=99999),
                       lambda v:v['sessions'][0]['session'].update(account=99999)):
            value=deepcopy(original);mutate(value);path.write_text(json.dumps(value))
            root=Backend(self.path,multiuser=True)
            with self.assertRaises(ValueError):Recovery(root,path,clock=lambda:self.now).restore()
            self.assertEqual(root.sessions,{})


if __name__=='__main__':unittest.main()
