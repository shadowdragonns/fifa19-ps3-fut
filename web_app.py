"""Same-origin club website. Browser requests bypass protocol credential captures."""
from email.parser import BytesHeaderParser
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from web_accounts import WebAccounts, AuthError
import web_packs

HERE = Path(__file__).resolve().parent
COOKIE = 'fut_web_session'


class HTTPError(ValueError):
    def __init__(self, status, message): self.status, self.message = status, message


class WebApp:
    def __init__(self, backend, accounts=None):
        self.root = backend.root
        self.auth = accounts or WebAccounts(self.root.path.parent/'web-accounts.sqlite3')
        self.labels = json.loads((HERE/'data/web-pack-labels.json').read_text())

    def owner(self, persona):
        root = self.root
        with root.lock:
            if root.profile['persona'] == persona: return root
            for key, row in root.accounts['users'].items():
                if root.profile['persona']+row['number'] != persona or row['legacy']: continue
                if key not in root.users:
                    from sessions import Backend
                    path = root.path.parent/'users'/key/'profile.json'
                    if not path.is_file(): break
                    owner = Backend(path, root.endpoints, settings=root.settings, events=root.events)
                    if (owner.profile['persona'] != persona
                            or owner.profile['account'] != root.profile['account']+row['number']):
                        raise HTTPError(403, 'Saved club identity differs.')
                    owner.root, owner.lock, owner.sessions = root, root.lock, root.sessions
                    owner.identity, owner.localizer = root.identity, root.localizer
                    owner.fut_inventory.lock = root.lock
                    root.users[key] = owner
                return root.users[key]
        raise HTTPError(403, 'Linked club is unavailable. Ask the server operator.')

    @staticmethod
    def csrf(token): return hmac.new(token.encode(), b'fifa19-web-csrf-v1', hashlib.sha256).hexdigest()

    def handle(self, method, path, raw, peer, secure=False):
        headers = [('Cache-Control','no-store'), ('X-Content-Type-Options','nosniff'),
                   ('X-Frame-Options','DENY'), ('Referrer-Policy','no-referrer'),
                   ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
        cookie = lambda token, age: (COOKIE+'='+token+'; Path=/club; HttpOnly; SameSite=Strict; Max-Age='+str(age)+('; Secure' if secure else ''))
        try:
            head, _, body = raw.partition(b'\r\n\r\n')
            request = BytesHeaderParser().parsebytes(head.split(b'\r\n', 1)[1]+b'\r\n\r\n')
            for name in ('Host','Origin','Cookie','Content-Type','X-CSRF-Token'):
                if len(request.get_all(name, [])) > 1: raise HTTPError(400, 'Duplicate request header.')
            host = request.get('Host', '')
            port = self.root.endpoints.http_port
            allowed = {f'{self.root.endpoints.host}:{port}', f'127.0.0.1:{port}', f'localhost:{port}'}
            if host not in allowed: raise HTTPError(403, f'Open the website using the server IP and port {port}.')
            uri = urlsplit(path)
            route = uri.path
            if uri.query: raise HTTPError(400, 'Query parameters are not supported.')
            if method == 'GET' and route == '/club':
                return '302 Found', b'', 'text/plain', headers+[('Location','/club/')]
            static = {'/club/':('index.html','text/html; charset=utf-8'),
                      '/club/app.js':('app.js','text/javascript; charset=utf-8'),
                      '/club/app.css':('app.css','text/css; charset=utf-8')}
            if method == 'GET' and route in static:
                name, mime = static[route]
                return '200 OK', (HERE/'web'/name).read_bytes(), mime, headers
            if method not in ('GET','POST'): raise HTTPError(405, 'Method not allowed.')
            data = {}
            if method == 'POST':
                expected = ('https://' if secure else 'http://')+host
                if request.get('Origin') != expected: raise HTTPError(403, 'Request origin rejected.')
                if request.get_content_type() != 'application/json' or len(body) > 16384:
                    raise HTTPError(400, 'A small JSON request is required.')
                data = json.loads(body)
                if not isinstance(data, dict): raise HTTPError(400, 'Expected a JSON object.')
            if method == 'POST' and route in ('/club/api/login','/club/api/register'):
                self.auth.limit('login:'+peer)
                if route.endswith('register'):
                    if set(data) != {'username','password','invite'}: raise HTTPError(400, 'Provide username, password and club invite.')
                    token = self.auth.register(**data)
                else:
                    if set(data) != {'username','password'}: raise HTTPError(400, 'Provide username and password.')
                    token = self.auth.login(**data)
                headers.append(('Set-Cookie',cookie(token,30*86400)))
                result = {'ok':True}
            else:
                cookies = SimpleCookie()
                cookies.load(request.get('Cookie',''))
                token = cookies[COOKIE].value if COOKIE in cookies else ''
                session = self.auth.session(token)
                if session is None: raise HTTPError(401, 'Sign in to your club.')
                if method == 'POST' and not hmac.compare_digest(request.get('X-CSRF-Token',''),self.csrf(token)):
                    raise HTTPError(403, 'Session check failed. Refresh the page.')
                owner = self.owner(session['persona'])
                if method == 'GET' and route == '/club/api/me':
                    with owner.lock:
                        state = owner.fut_inventory.state
                        history = sorted(state.get('webPackReceipts',{}).values(), key=lambda row:row['created'], reverse=True)[:12]
                        result = {'username':session['username'], 'club_name':owner.profile['fut_club']['name'],
                                  'persona':session['persona'], 'coins':state['coins'],
                                  'card_count':sum(row['pile']=='club' for row in state['items']),
                                  'csrf':self.csrf(token), 'history':[row['result'] for row in history]}
                elif method == 'GET' and route == '/club/api/options':
                    result = {key:[{'id':int(k),'name':v} for k,v in sorted(values.items(),key=lambda row:row[1].casefold())]
                              for key,values in self.labels.items()}
                    result['positions'] = sorted(web_packs.POSITIONS)
                elif method == 'POST' and route == '/club/api/quote':
                    self.auth.limit('quotes:'+str(session['persona']), count=120, seconds=60)
                    with owner.lock:
                        result, _ = web_packs.quote(data, owner.settings.snapshot())
                        if result['available']:
                            quote_token, expires = self.auth.quote(session['persona'],result)
                            result.update(quote_token=quote_token,expires=expires)
                elif method == 'POST' and route == '/club/api/open':
                    if set(data) != {'quote_token','duplicate_consent'}: raise HTTPError(400, 'Provide a valid pack quote.')
                    self.auth.limit('open:'+str(session['persona']),count=30,seconds=60)
                    result = web_packs.open_pack(owner,self.auth,data['quote_token'],duplicate_consent=data['duplicate_consent'])
                elif method == 'POST' and route == '/club/api/logout':
                    if data: raise HTTPError(400, 'Unexpected logout data.')
                    self.auth.logout(token)
                    headers.append(('Set-Cookie',cookie('',0)))
                    result = {'ok':True}
                else: raise HTTPError(404, 'Page not found.')
            return '200 OK', json.dumps(result,ensure_ascii=False).encode(), 'application/json; charset=utf-8', headers
        except HTTPError as error:
            status, message = error.status, error.message
        except (AuthError,web_packs.PackError,ValueError,UnicodeError,CookieError) as error:
            status, message = 400, str(error) or 'Invalid request.'
        except (OSError,sqlite3.Error):
            status, message = 503, 'Club storage is temporarily unavailable. Please retry the same pack.'
        labels = {400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',503:'Service Unavailable'}
        return f'{status} {labels[status]}', json.dumps({'error':message}).encode(), 'application/json', headers
