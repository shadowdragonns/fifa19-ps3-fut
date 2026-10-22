"""Private, expiring login recovery across graceful server restarts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace

WINDOW = 15*60
FIELDS = ('persona','account','name','key','external','login_time','postauth',
          'web_key','web_token','pow_key','fut_key','peer')
TOKENS = ('key','web_key','web_token','pow_key','fut_key')


class Recovery:
    def __init__(self, backend, path, clock=time.time):
        self.root, self.path, self.clock = backend.root, Path(path), clock
        self.saved = {}

    def _write(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary = self.path.with_name('.'+self.path.name+'.'+secrets.token_hex(8)+'.tmp')
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump({'version':1,'sessions':list(self.saved.values())},stream)
                stream.flush();os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self):
        """Called under the root lock before shutdown removes live sessions."""
        with self.root.lock:
            now=self.clock();saved={}
            for session in self.root.sessions.values():
                expires=session.recovery_until or now+WINDOW
                if not session.postauth or expires<=now:continue
                routes=[key for key,connection in self.root.client_routes.items()
                        if connection.session is session][:16]
                saved[session.key]={'expires':expires,
                    'session':{name:getattr(session,name) for name in FIELDS},
                    'np_blob':session.np_blob.hex(),'routes':routes,
                    'cpu_match':deepcopy(getattr(session,'local_fut_match',None))}
            self.saved=saved;self._write()

    def forget(self, key):
        """Logout/disconnect revokes recovery on disk, not just in memory."""
        with self.root.lock:
            if key in self.saved:
                previous=self.saved.pop(key)
                try:self._write()
                except OSError:
                    self.saved[key]=previous
                    raise

    def touch(self, session):
        # Continued authenticated use keeps a recovered game connected; idle
        # credentials still expire. Fresh Blaze login clears the recovery flag.
        with self.root.lock:
            if session.recovery_until:session.recovery_until=self.clock()+WINDOW

    def purge(self):
        with self.root.lock:
            for key,session in list(self.root.sessions.items()):
                if session.recovery_until and session.recovery_until<=self.clock():
                    self.root.sessions.pop(key)
                    for route,connection in list(self.root.client_routes.items()):
                        if connection.session is session:self.root.client_routes.pop(route)

    def restore(self):
        from sessions import Session
        from accounts import user_key
        if not self.path.exists():return 0
        if self.path.stat().st_size>4*1024*1024:raise ValueError('Recovery file too large')
        value=json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(value,dict) or set(value)!={'version','sessions'}
                or value['version']!=1 or not isinstance(value['sessions'],list)
                or len(value['sessions'])>128):raise ValueError('Invalid recovery file')
        now=self.clock();staged=[];keys=set();externals=set();routes=set()
        for row in value['sessions']:
            if not isinstance(row,dict) or set(row)!={'expires','session','np_blob','routes','cpu_match'}:
                raise ValueError('Invalid recovery entry')
            if type(row['expires']) not in (int,float):raise ValueError('Invalid recovery expiry')
            if not now<row['expires']<=now+WINDOW:continue
            fields=row['session']
            if not isinstance(fields,dict) or set(fields)!=set(FIELDS):raise ValueError('Invalid recovery session')
            if (any(type(fields[k]) is not int or not 0<fields[k]<2**64 for k in ('persona','account','external','login_time'))
                    or fields['postauth'] is not True or not isinstance(fields['name'],str)
                    or not 1<=len(fields['name'])<=32 or not isinstance(fields['peer'],str)
                    or any(not isinstance(fields[k],str) or fields[k] and not re.fullmatch(r'[0-9a-f]{32}',fields[k])
                           for k in TOKENS) or not fields['key']
                    or fields['key'] in keys or fields['external'] in externals):
                raise ValueError('Invalid recovered identity or credential')
            if (not isinstance(row['routes'],list) or len(row['routes'])>16
                    or any(not isinstance(r,str) or not re.fullmatch(r'[0-9a-f]{32}',r) or r in routes for r in row['routes'])
                    or len(set(row['routes']))!=len(row['routes'])
                    or not isinstance(row['np_blob'],str) or not re.fullmatch(r'(?:[0-9a-f]{72})?',row['np_blob'])):
                raise ValueError('Invalid recovery callback')
            if self.root.multiuser and user_key(fields['external']) not in self.root.accounts['users']:
                raise ValueError('Unknown recovered club')
            match=row['cpu_match']
            if match is not None and (not isinstance(match,dict) or not isinstance(match.get('id'),str)
                    or not re.fullmatch(r'[0-9a-f]{32}',match['id']) or not isinstance(match.get('request'),dict)
                    or match['request'].get('type')!='OFFLINE'):
                raise ValueError('Invalid recovered CPU match')
            keys.add(fields['key']);externals.add(fields['external']);routes.update(row['routes']);staged.append(row)
        with self.root.lock:
            prepared=[]
            for row in staged:
                fields=row['session'];owner=self.root.for_user(fields['external'])
                if (owner.profile['persona'],owner.profile['account'])!=(fields['persona'],fields['account']):
                    raise ValueError('Recovered club identity differs')
                prepared.append((row,owner))
            for row,owner in prepared:
                fields=row['session']
                session=Session(**fields,owner=owner,np_blob=bytes.fromhex(row['np_blob']),recovery_until=row['expires'])
                session.local_fut_match=deepcopy(row['cpu_match'])
                self.root.sessions[session.key]=session
                for route in row['routes']:self.root.client_routes[route]=SimpleNamespace(session=session)
                self.saved[session.key]=row
        return len(staged)
