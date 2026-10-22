"""Website-only credentials linked to saved FUT clubs by operator-issued invites."""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time


class AuthError(ValueError): pass


def digest(value): return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()


class WebAccounts:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, persona INTEGER UNIQUE NOT NULL,
                 salt TEXT NOT NULL, password_hash TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS invites(token_hash TEXT PRIMARY KEY, persona INTEGER NOT NULL, expires INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                 expires INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS quotes(token_hash TEXT PRIMARY KEY, persona INTEGER NOT NULL, payload TEXT NOT NULL,
                 expires INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, starts INTEGER NOT NULL, count INTEGER NOT NULL);
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(users)')}
            if 'is_admin' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1))')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA foreign_keys=ON')
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def invite(self, persona, *, reset=False, minutes=60):
        if type(persona) is not int or persona <= 0: raise ValueError('Invalid club persona')
        token = secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if reset: db.execute('DELETE FROM users WHERE persona=?', (persona,))
            if db.execute('SELECT 1 FROM users WHERE persona=?', (persona,)).fetchone():
                raise ValueError('Club already linked. Use --reset to revoke its web login and issue a replacement invite.')
            db.execute('DELETE FROM invites WHERE persona=? OR expires<=?', (persona, int(self.clock())))
            db.execute('INSERT INTO invites VALUES(?,?,?)', (digest(token), persona, int(self.clock())+minutes*60))
        return token

    def register(self, username, password, invite):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
            raise AuthError('Use a username of 3–32 letters, numbers or underscores.')
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise AuthError('Use a website password of 12–128 characters. Do not reuse your RPCN password.')
        if not isinstance(invite, str) or len(invite) > 100: raise AuthError('Invalid or expired club invite.')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT persona FROM invites WHERE token_hash=? AND expires>?',
                             (digest(invite.strip()), int(self.clock()))).fetchone()
            if row is None: raise AuthError('Invalid or expired club invite.')
            try: db.execute('INSERT INTO users(username,persona,salt,password_hash) VALUES(?,?,?,?)', (username.lower(), row['persona'], salt, hashed))
            except sqlite3.IntegrityError: raise AuthError('Username or club already linked.') from None
            db.execute('DELETE FROM invites WHERE token_hash=?', (digest(invite.strip()),))
        return self.login(username, password)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            raise AuthError('Incorrect username or password.')
        with self.db() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username.lower(),)).fetchone()
            salt = row['salt'] if row else '00'*16
            hashed = password_hash(password, salt)
            if row is None or not hmac.compare_digest(hashed, row['password_hash']):
                raise AuthError('Incorrect username or password.')
            token = secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires<=?', (int(self.clock()),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), row['username'], int(self.clock())+30*86400))
        return token

    def session(self, token):
        if not isinstance(token, str) or len(token) > 100: return None
        with self.db() as db:
            row = db.execute('SELECT users.username,users.persona,users.is_admin FROM sessions JOIN users USING(username) '
                             'WHERE token_hash=? AND expires>?', (digest(token), int(self.clock()))).fetchone()
        return dict(row) if row else None

    def set_admin(self, username, enabled=True):
        if not isinstance(username,str) or type(enabled) is not bool:raise ValueError('Invalid administrator')
        with self.db() as db:
            if db.execute('UPDATE users SET is_admin=? WHERE username=?',(int(enabled),username.lower())).rowcount!=1:
                raise AuthError('Website user does not exist.')

    def logout(self, token):
        with self.db() as db: db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))

    def limit(self, key, count=20, seconds=600):
        now = int(self.clock())
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM limits WHERE starts<=?', (now-86400,))
            row = db.execute('SELECT starts,count FROM limits WHERE key=?', (key,)).fetchone()
            if row and row['starts'] > now-seconds:
                if row['count'] >= count: raise AuthError('Too many attempts. Please wait and try again.')
                db.execute('UPDATE limits SET count=count+1 WHERE key=?', (key,))
            else:
                db.execute('INSERT OR REPLACE INTO limits VALUES(?,?,1)', (key, now))

    def quote(self, persona, payload):
        token = secrets.token_urlsafe(32)
        expires = int(self.clock())+600
        with self.db() as db:
            db.execute('DELETE FROM quotes WHERE expires<=?', (int(self.clock()),))
            db.execute('INSERT INTO quotes VALUES(?,?,?,?)', (digest(token), persona, json.dumps(payload), expires))
        return token, expires

    def get_quote(self, persona, token):
        if not isinstance(token, str) or len(token) > 100: raise AuthError('Invalid quote.')
        with self.db() as db:
            row = db.execute('SELECT payload,expires FROM quotes WHERE token_hash=? AND persona=? AND expires>?',
                             (digest(token), persona, int(self.clock()))).fetchone()
        if row is None: raise AuthError('This quote expired. Refresh the price before opening another pack.')
        return json.loads(row['payload']), row['expires']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('clubs', help='List saved club persona IDs without exposing RPCN credentials')
    invite = sub.add_parser('invite', help='Issue a one-use website registration code for a club')
    invite.add_argument('--persona', type=int, required=True)
    invite.add_argument('--reset', action='store_true', help='Revoke the existing website login first; does not alter the FUT club')
    invite.add_argument('--minutes', type=int, default=60)
    admin=sub.add_parser('admin',help='Grant or revoke website card-editor access')
    admin.add_argument('--username',required=True)
    admin.add_argument('--revoke',action='store_true')
    args = parser.parse_args()
    root = args.state_dir.resolve()
    if args.command=='admin':
        WebAccounts(root/'web-accounts.sqlite3').set_admin(args.username,not args.revoke)
        print('Admin access '+('revoked for ' if args.revoke else 'granted to ')+args.username)
        return
    profiles = [root/'profile.json', *sorted((root/'users').glob('*/profile.json'))]
    clubs = {}
    for path in profiles:
        if not path.is_file(): continue
        row = json.loads(path.read_text())
        clubs[row['persona']] = row['fut_club']['name']
    if args.command == 'clubs':
        for persona, name in sorted(clubs.items()): print(f'{persona}: {name}')
        return
    if args.persona not in clubs: parser.error('Persona is not a saved club; list clubs first')
    if not 1 <= args.minutes <= 1440: parser.error('Invite lifetime must be 1–1440 minutes')
    token = WebAccounts(root/'web-accounts.sqlite3').invite(args.persona, reset=args.reset, minutes=args.minutes)
    print(f'Club: {clubs[args.persona]} ({args.persona})\nOne-use website registration code: {token}\nExpires in {args.minutes} minutes. Keep it private.')


if __name__ == '__main__': main()
