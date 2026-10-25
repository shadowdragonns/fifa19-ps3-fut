"""Native packed instructions persist per squad through real HTTP saves."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fut_matches
import fut_squads
from sessions import Backend
from test_project import club


# The shape and packed values of the rejected retail instruction edit;
# ownership and all account/HTTP identity use generated local fixtures.
INSTRUCTIONS=[0,8,1,1,8,320,320,134252544,134238208,134287360,83886112,
              0,8,8,16,8,8,8,8,16,16,16]


class PlayerInstructionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.backend=Backend(Path(temp.name)/'profile.json')
        self.session=club(self.backend);self.store=self.backend.fut_inventory

    def call(self,method,identifier,payload=None):
        body=b'' if payload is None else json.dumps(payload).encode()
        path=f'/ut/game/fifa19/squad/{identifier}'
        raw=f'{method} {path} HTTP/1.1\r\nX-UT-SID: {self.session.fut_key}\r\n\r\n'.encode()+body
        reply=fut_squads.response(self.backend,method,path,raw)
        return reply[0],json.loads(reply[1])

    def test_captured_full_save_and_partial_edits_round_trip_exactly_and_survive_restart(self):
        value=deepcopy(fut_squads.saved(self.backend))
        value.update(custom=json.dumps(INSTRUCTIONS,separators=(',',':')),chemistry=100,rating=80,starRating=5)
        before=deepcopy(self.store.state)
        self.assertEqual(self.call('PUT',1,value),('200 OK',{'id':1}))
        self.assertEqual(self.call('GET',1)[1]['custom'],value['custom'])
        for field in ('coins','items','supportItems'):
            self.assertEqual(self.store.state.get(field),before.get(field))
        # A subsequent captain-only patch must not discard instructions.
        self.assertEqual(self.call('PUT',1,{'id':1,'captain':value['captain']})[0],'200 OK')
        restored=Backend(self.backend.path)
        self.assertEqual(restored.fut_inventory.state,self.store.state)
        self.assertEqual(fut_squads.saved(restored)['custom'],value['custom'])

    def test_other_squads_activation_and_match_snapshots_keep_independent_instructions(self):
        first=deepcopy(fut_squads.saved(self.backend))
        first['custom']=json.dumps(INSTRUCTIONS);fut_squads.save(self.backend,first)
        second=fut_squads.create(self.backend,{'squadName':'Other instructions'})['id']
        other=deepcopy(first);other.update(id=second,squadName='Other instructions',custom=json.dumps([16]*22))
        self.assertEqual(self.call('PUT',second,other)[0],'200 OK')
        self.assertEqual(fut_squads.saved(self.backend)['custom'],first['custom'])
        fut_squads.activate(self.backend,second)
        reply=fut_matches.create(self.backend,self.session,{'type':'OFFLINE','squadId':second})
        self.assertEqual(reply['squad']['custom'],other['custom'])
        ids=[r['itemData']['id'] for r in reply['squad']['players'] if r['index']<11]
        fut_matches.play(self.backend,self.session,{'items':[{'id':i} for i in ids]})
        self.call('PUT',second,{'id':second,'custom':json.dumps([0]*22)})
        self.assertEqual(self.session.local_fut_match['squad']['custom'],other['custom'])
        self.assertEqual(fut_squads.saved(self.backend,1)['custom'],first['custom'])

    def test_signed_word_boundaries_are_supported_but_malformed_or_oversized_values_cannot_mutate_saves(self):
        limits=json.dumps([-2**31,2**31-1]+[0]*20)
        self.assertEqual(self.call('PUT',1,{'id':1,'custom':limits})[0],'200 OK')
        before=deepcopy(self.store.state);raw=self.store.path.read_bytes()
        invalid=['invalid','{}','null',json.dumps([0]*21),json.dumps([0]*23)]
        invalid += [json.dumps([v]+[0]*21) for v in (2**31,-2**31-1,True,1.5,'8',None,[])]
        for custom in invalid:
            with self.subTest(custom=custom):
                self.assertEqual(self.call('PUT',1,{'id':1,'custom':custom})[0],'400 Bad Request')
                self.assertEqual(self.store.state,before)
                self.assertEqual(self.store.path.read_bytes(),raw)

    def test_failed_instruction_save_preserves_previous_values_and_can_be_retried(self):
        payload={'id':1,'custom':json.dumps(INSTRUCTIONS)}
        before=deepcopy(self.store.state);raw=self.store.path.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('disk full')):
            self.assertEqual(self.call('PUT',1,payload)[0],'503 Service Unavailable')
        self.assertEqual(self.store.state,before);self.assertEqual(self.store.path.read_bytes(),raw)
        self.assertEqual(self.call('PUT',1,payload),('200 OK',{'id':1}))


if __name__=='__main__':unittest.main()
