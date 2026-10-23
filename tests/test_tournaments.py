"""CPU cup routing, authoritative results and durable per-club progression."""
import base64
import gzip
from copy import deepcopy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import fut_matches
import fut_results
import fut_tournaments as cups
from sessions import Backend
from test_plugin_api import login,enter_fut,seed_club


def bracket(round_number=0):
    data=bytes([6,round_number])+bytes(30)
    compressed=gzip.compress(data,mtime=0)
    payload=compressed if ((4+len(compressed)+2)//3)*3-4<=len(data) else data
    return base64.b64encode(struct.pack('>I',len(data))+payload).decode('ascii')


class TournamentTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'profile.json'
        self.root=Backend(self.path,multiuser=True)
        self.a=login(self.root,101,'First');self.b=login(self.root,202,'Second')
        for c in (self.a,self.b):enter_fut(self.root,c);seed_club(c.session.owner)
        self.owner=self.a.session.owner

    def call(self,method,route,body=None,connection=None):
        c=connection or self.a
        raw=(method+' '+route+' HTTP/1.1\r\nX-UT-SID: '+c.session.fut_key+'\r\n\r\n').encode()
        if body is not None:raw+=json.dumps(body).encode()
        reply=cups.response(self.root,method,route,raw)
        return reply[0],json.loads(reply[1])

    def match(self,identifier=1001,reason='WIN'):
        session=self.a.session
        request={'type':'OFFLINE','squadId':1,'tournamentId':identifier,'customData1':-1314516563}
        if cups.saved(self.owner.fut_inventory.state,identifier)['wins']==3:
            request['tournamentTrophyRound']='true'
        raw=(f'POST {fut_matches.BASE} HTTP/1.1\r\nX-UT-SID: {session.fut_key}\r\n\r\n').encode()+json.dumps(request).encode()
        reply=fut_matches.response(self.root,'POST',fut_matches.BASE,raw)
        self.assertEqual(reply[0],'200 OK')
        ids=[p['itemData']['id'] for p in session.local_fut_match['squad']['players'] if p['index']<11]
        fut_matches.play(self.root,session,{'items':[{'id':i} for i in ids]})
        report={'matchReportId':0,'endReason':reason,'secondsPlayed':5400,'items':[{'id':i} for i in ids],
                'matchData':'','myMatchStats':{k:0 for k in fut_results.STATS},
                'opponentMatchStats':{k:0 for k in fut_results.STATS}}
        if reason=='WIN':report['myMatchStats']['goals']=1
        if reason=='LOSS':report['opponentMatchStats']['goals']=1
        return report

    def test_native_list_teams_progress_and_missing_login_are_handled(self):
        status,value=self.call('GET',cups.BASE+'/list?active=true&count=99')
        self.assertEqual(status,'200 OK');self.assertEqual(len(value['tournament']),3)
        for row in value['tournament']:
            self.assertEqual((row['type'],row['treeType'],row['numTeams'],row['numRounds']),('offline','knockout',16,4))
            self.assertEqual(row['elgReq'],[])
            self.assertEqual([r['id'] for r in row['rounds']],[1,2,3,4])
            status,teams=self.call('GET',cups.BASE+f"/teams?groupId={row['aigroup']}&count=15")
            self.assertEqual(status,'200 OK');self.assertEqual(len(set(teams['teamId'])),15)
        self.assertEqual(self.call('GET',cups.USER+'/1001')[1],{'round':0,'dataVersion':'2','tournamentData':''})
        self.assertEqual(self.call('GET',cups.USER+'/list')[1],{'tournamentId':[]})
        self.assertEqual(self.call('PUT',cups.JOINED)[1],{'result':'SUCCESS'})
        self.assertEqual(self.call('GET',cups.BASE+'/list?type=online')[1],{'tournament':[]})
        raw=b'GET / HTTP/1.1\r\nX-UT-SID: invalid\r\n\r\n'
        self.assertEqual(cups.response(self.root,'GET',cups.BASE+'/list',raw)[0],'401 Unauthorized')
        for route in (cups.BASE+'/teams?groupId=1&count=15',cups.USER+'/999',cups.BASE+'/list?count=-1'):
            self.assertEqual(self.call('GET',route)[0],'400 Bad Request')

    def test_four_wins_award_one_prize_and_restart_preserves_bracket_and_receipts(self):
        balance=self.owner.fut_inventory.state['coins'];rewards=0
        for round_number in range(1,5):
            report=self.match();current=self.a.session.local_fut_match
            result=fut_results.finish(self.root,self.a.session,report)
            self.assertEqual(result['tournamentCoins'],1000 if round_number==4 else 0)
            rewards+=result['allCoins']
            self.assertEqual(fut_results.finish(self.root,self.a.session,report),result)
            payload={'round':round_number,'dataVersion':2,'tournamentData':bracket(round_number)}
            self.assertEqual(self.call('PUT',cups.USER+'/1001',payload)[0],'200 OK')
            self.assertEqual(self.call('PUT',cups.USER+'/1001',payload)[0],'200 OK')
            self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001)['wins'],round_number)
            self.assertTrue(current['finished'])
        state=deepcopy(self.owner.fut_inventory.state)
        self.assertEqual(state['coins'],balance+rewards)
        self.assertEqual((cups.saved(state,1001)['status'],cups.saved(state,1001)['championships']),('won',1))
        self.assertEqual(cups.saved(self.b.session.owner.fut_inventory.state,1001)['wins'],0)
        restored=Backend(self.path,multiuser=True).for_user(101)
        self.assertEqual(restored.fut_inventory.state,state)
        self.assertEqual(cups.progress(restored,1001)['tournamentData'],bracket(4))
        self.match()
        new=cups.saved(self.owner.fut_inventory.state,1001)
        self.assertEqual((new['cycle'],new['wins'],new['championships']),(1,0,1))

    def test_uploaded_bracket_cannot_advance_or_pay_and_failed_result_save_rolls_back(self):
        before=deepcopy(self.owner.fut_inventory.state)
        payload={'round':4,'dataVersion':2,'tournamentData':bracket(4)}
        self.assertEqual(self.call('PUT',cups.USER+'/1001',payload)[0],'400 Bad Request')
        self.assertEqual(self.owner.fut_inventory.state,before)
        payload['round']=0
        self.assertEqual(self.call('POST',cups.USER+'/1001',payload)[0],'200 OK')
        self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001)['wins'],0)
        self.assertEqual(self.owner.fut_inventory.state['coins'],before['coins'])
        report=self.match();before=deepcopy(self.owner.fut_inventory.state)
        with patch.object(self.owner.fut_inventory,'commit',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):fut_results.finish(self.root,self.a.session,report)
        self.assertEqual(self.owner.fut_inventory.state,before)
        self.assertFalse(self.a.session.local_fut_match.get('finished',False))
        fut_results.finish(self.root,self.a.session,report)
        self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001)['wins'],1)

    def test_trophy_flag_is_normalized_for_retries_and_does_not_skip_rounds_or_pay_early(self):
        request={'type':'OFFLINE','squadId':1,'tournamentId':1001,'tournamentTrophyRound':'true'}
        original=deepcopy(request);balance=self.owner.fut_inventory.state['coins']
        reply=fut_matches.create(self.root,self.a.session,request)
        current=self.a.session.local_fut_match
        for flag in (True,1,'true'):
            self.assertEqual(fut_matches.create(self.root,self.a.session,{**request,'tournamentTrophyRound':flag}),reply)
            self.assertIs(self.a.session.local_fut_match,current)
        self.assertEqual(request,original)
        self.assertIs(current['request']['tournamentTrophyRound'],True)
        self.assertEqual(current['tournament']['round'],1)
        self.assertEqual(self.owner.fut_inventory.state['coins'],balance)
        fut_matches.reset(self.a.session)
        for flag in ('TRUE','1','',2,-1,1.0,None,[],{}):
            with self.subTest(flag=flag),self.assertRaises(ValueError):
                fut_matches.create(self.root,self.a.session,{**request,'tournamentTrophyRound':flag})
            self.assertIsNone(self.a.session.local_fut_match)
        for flag in (False,0,'false'):
            fut_matches.create(self.root,self.a.session,{**request,'tournamentTrophyRound':flag})
            self.assertIs(self.a.session.local_fut_match['request']['tournamentTrophyRound'],False)
        self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001)['wins'],0)
        self.assertEqual(self.owner.fut_inventory.state['coins'],balance)

    def test_retail_empty_auxiliary_progress_and_repeated_initial_saves_are_accepted(self):
        payload={'round':1,'dataVersion':2,'tournamentData':bracket(),
                 'progressDataVersion':2,'progressData':'AAAAAA=='}
        for updated in (payload,{**payload,'tournamentData':bracket(1)}):
            self.assertEqual(self.call('PUT',cups.USER+'/1001',updated)[0],'200 OK')
            self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001)['wins'],0)
        restored=Backend(self.path,multiuser=True).for_user(101)
        self.assertEqual(cups.progress(restored,1001)['tournamentData'],bracket(1))
        invalid={**payload,'progressData':'AAAAAAEA'}
        before=deepcopy(self.owner.fut_inventory.state)
        self.assertEqual(self.call('PUT',cups.USER+'/1001',invalid)[0],'400 Bad Request')
        self.assertEqual(self.owner.fut_inventory.state,before)

    def test_loss_quit_and_new_attempt_do_not_change_other_cups_or_duplicate_awards(self):
        report=self.match(reason='LOSS');result=fut_results.finish(self.root,self.a.session,report)
        row=cups.saved(self.owner.fut_inventory.state,1001)
        self.assertEqual((row['status'],row['wins'],result['tournamentCoins']),('lost',0,0))
        coins=self.owner.fut_inventory.state['coins']
        self.assertEqual(self.call('POST',cups.QUIT+'1001',{'unexpected':True})[0],'400 Bad Request')
        self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001),row)
        self.assertEqual(self.call('POST',cups.QUIT+'1001')[0],'200 OK')
        self.assertEqual(self.call('POST',cups.QUIT+'1001')[0],'200 OK')
        self.assertEqual(self.owner.fut_inventory.state['coins'],coins)
        self.assertEqual(cups.saved(self.owner.fut_inventory.state,1002),cups.fresh())
        report=self.match();fut_results.finish(self.root,self.a.session,report)
        self.assertEqual(cups.saved(self.owner.fut_inventory.state,1001)['wins'],1)
        corrupt=deepcopy(self.owner.fut_inventory.state)
        corrupt['offlineTournaments']['1001']['wins']=3
        with self.assertRaises(ValueError):self.owner.fut_inventory.validate(corrupt)


if __name__=='__main__':unittest.main()
