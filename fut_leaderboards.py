"""Local FUT rankings using saved, paid match receipts and registered clubs.

Retail options/result parsers: 0x16c46ac/0x16c339c; row/header:
0x16c31d4/0x16c3018; club comparison: 0x16c3a00. No EA rankings.
"""
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

from pow_services import json_reply

BASE = '/ut/game/fifa19/leaderboards'
LIST = re.compile(re.escape(BASE)+r'/local/(competitor|collector|seasonpoints|seasonplayed)/(all|top100|myclub)/alltime')
CLUB = re.compile(re.escape(BASE)+r'/club/([1-9][0-9]{0,18})/(general|seasons)')
LABELS = {'category': 'Leaderboard', 'view': 'Players', 'period': 'Period',
          'competitor': 'Match Points', 'collector': 'Club Value',
          'seasonpoints': 'Online Season Points', 'seasonplayed': 'Online Matches Played',
          'all': 'All Clubs', 'top100': 'Top 100', 'myclub': 'Around My Club',
          'alltime': 'All Time', 'rank': 'Rank', 'persona': 'Club',
          'badge': 'Badge', 'score': 'Score', 'wins': 'Wins', 'draws': 'Draws', 'losses': 'Losses'}
GENERAL = ('competitor', 'collector')
SEASONS = ('seasonpoints', 'seasonplayed')


def options(group):
    categories = SEASONS if group == 'seasons' else GENERAL
    choices = {'category': categories, 'view': ('all', 'top100', 'myclub'), 'period': ('alltime',)}
    def choice(ident, first):
        return {'id': ident, 'display': 'LOC_LB_'+ident,
                'description': 'LOC_LB_'+ident+'_DESC',
                'image': {'all': 'alltimeicon', 'myclub': 'friendsicon'}.get(ident, ident+'icon'),
                **({'default': True} if first else {})}
    return {'selection': {'category': categories[0], 'view': 'all', 'period': 'alltime'},
            'options': [{'id': key, key: [choice(ident, i == 0) for i, ident in enumerate(values)]}
                        for key, values in choices.items()],
            'permutations': [{'category': category, 'view': view, 'period': 'alltime',
                              'url': '/local/'+category+'/'+view+'/alltime'}
                             for category in categories for view in choices['view']], 'scrollDelay': 100}


def localization():
    # Outer parser 0x16a0cb8 requires target before calling 0x16a0ad0.
    # Omitting target makes its strcmp dereference NULL during FUT entry.
    labels = dict(LABELS)
    labels.update({key+'_DESC': text+' across clubs on this server.' for key, text in LABELS.items()})
    from fut_items import ITEM_LABELS
    labels = {'LOC_LB_'+key:text for key,text in labels.items()} | ITEM_LABELS
    return ('<?xml version="1.0" encoding="UTF-8"?><message_set target="fut-locstrings">'+''.join(
        '<locstring id="'+key+'">'+escape(text)+'</locstring>' for key, text in labels.items())+
        '</message_set>').encode()


def clubs(backend):
    """Include offline registered users without creating identities or saves."""
    from accounts import saved_backends
    return saved_backends(backend)


def statistics(owner, group):
    state = owner.fut_inventory.state
    receipts = [row for row in state.get('matchHistory', [])
                if (group != 'seasons' or row['seasonId'] == 2)
                and row['summary']['endReason'] in ('WIN', 'DRAW', 'LOSS')]
    wins, draws, losses = (sum(row['summary']['endReason'] == reason for row in receipts)
                          for reason in ('WIN', 'DRAW', 'LOSS'))
    return {'credits': min(state['coins'], 2**32-1), 'trophies': 0,
            'won': wins, 'draw': draws, 'loss': losses,
            'points': sum(owner.settings.result_points(row['summary']['endReason']) for row in receipts), 'played': len(receipts),
            'value': sum(owner.fut_inventory.card(card, owner.settings)['discardValue'] for card in state['items']
                         if card['pile'] == 'club')}


def scores(stats, group):
    return ({'seasonpoints': stats['points'], 'seasonplayed': stats['played']} if group == 'seasons'
            else {'competitor': stats['points'], 'collector': stats['value']})


def details(owner, group):
    stats = statistics(owner, group)
    return {'clubInfo': {key: stats[key] for key in ('credits', 'trophies', 'won', 'draw', 'loss')},
            'category': [{'id': key, 'score': {'value': min(value, 2**32-1)}}
                         for key, value in scores(stats, group).items()]}


def entries(backend, session, category, view, start=0, count=100):
    from fut_club_assets import CARDS, DEFAULTS
    from fut_squads import display_name
    group = 'seasons' if category in SEASONS else 'general'
    ranked = []
    for persona, owner in clubs(backend).items():
        stats = statistics(owner, group)
        prefs = owner.fut_inventory.state.get('equipment', DEFAULTS)
        badge = CARDS[prefs['activeBadge']]
        ranked.append({'persona': str(persona), 'clubName': display_name(owner),
                       'score': {'value': min(scores(stats, group)[category], 2**32-1)},
                       'badge': badge['assetId'], 'teamId': badge['teamid'],
                       'est': str(owner.profile['fut_club']['established']),
                       'insetUrl': '/club/'+str(persona)+'/'+group,
                       'seasonOnlineWins': stats['won'], 'seasonOnlineDraws': stats['draw'],
                       'seasonOnlineLosses': stats['loss']})
    ranked.sort(key=lambda row: (-row['score']['value'], int(row['persona'])))
    for rank, row in enumerate(ranked, 1):
        row['rank'] = rank
    if view == 'myclub':
        index = next((i for i, row in enumerate(ranked) if row['persona'] == str(session.persona)), 0)
        start = max(0, index-count//2)
    if view == 'top100':
        ranked = ranked[:100]
    return {'header': {'rank': 'LOC_LB_rank', 'persona': 'LOC_LB_persona', 'badge': 'LOC_LB_badge',
                       'score': 'LOC_LB_'+category, 'insetUrl': '', 'inset': []},
            'entries': ranked[start:start+count]}


def response(backend, method, path, raw):
    from fut_services import request_headers, session_for
    parsed = urlsplit(path)
    route = parsed.path
    localized = re.fullmatch(r'/fut/loc/PS3/leaderboards\.[A-Za-z_]{2,16}\.xml', route)
    listing, club = LIST.fullmatch(route), CLUB.fullmatch(route)
    if route not in (BASE, BASE+'/options') and not localized and not listing and not club:
        return None
    if method != 'GET':
        return json_reply({'error': 'GET required'}, 'FUT.Leaderboards.WrongMethod',
                          '405 Method Not Allowed', [('Allow', 'GET')])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.root.lock:
        session = session_for(backend.root, request_headers(head))
        if not localized and session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.Leaderboards.LoginRequired', '401 Unauthorized')
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            if body or any(len(v) != 1 for v in query.values()):
                raise ValueError('Invalid leaderboard request')
            if localized:
                if query:
                    raise ValueError('Invalid localization query')
                return ('200 OK', localization(), 'application/xml; charset=utf-8',
                        [('Cache-Control', 'no-store')], 'FUT.Leaderboards.Localization')
            owner = backend.for_session(session)
            if route in (BASE, BASE+'/options'):
                if set(query)-{'group'} or query.get('group', ['general'])[0] not in ('general', 'seasons'):
                    raise ValueError('Invalid leaderboard group')
                group = query.get('group', ['general'])[0]
                value = options(group) if route.endswith('/options') else details(owner, group)
            elif club:
                if query:
                    raise ValueError('Invalid club comparison query')
                value = details(clubs(backend)[int(club[1])], club[2])
            else:
                if set(query)-{'start', 'count', 'save'} or query.get('save', ['true']) != ['true']:
                    raise ValueError('Invalid leaderboard page')
                start, count = int(query.get('start', ['0'])[0]), int(query.get('count', ['100'])[0])
                if not 0 <= start <= 1000000 or not 1 <= count <= 100:
                    raise ValueError('Invalid leaderboard page')
                value = entries(backend, session, listing[1], listing[2], start, count)
            return json_reply(value, 'FUT.Leaderboards')
        except (ValueError, KeyError, TypeError, UnicodeError):
            return json_reply({'error': 'Invalid local leaderboard request'}, 'FUT.Leaderboards.InvalidRequest', '400 Bad Request')
        except OSError:
            return json_reply({'error': 'Leaderboard saves unavailable'}, 'FUT.Leaderboards.SaveUnavailable', '503 Service Unavailable')
