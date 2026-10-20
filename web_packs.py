"""Server-priced custom web packs committed atomically to the existing FUT inventory."""
from copy import deepcopy
import hashlib
import json
import random
import time

import card_versions
import fut_packs
from web_accounts import digest

DEFAULT_PRICING = {'enabled': True, 'base_per_player': 750, 'filter_surcharge': 750,
                   'extra_filter_percent': 20, 'prime_surcharge': 30000,
                   'rating_step_surcharge': 500, 'rare_surcharge': 750}
PARAMETERS = {'size', 'edition', 'level', 'nation', 'club', 'league', 'position', 'min_rating', 'rare_only'}
POSITIONS = card_versions.POSITIONS


class PackError(ValueError): pass


def parameters(values):
    if not isinstance(values, dict) or set(values)-PARAMETERS: raise PackError('Unknown pack parameter.')
    p = dict(size=3, edition='standard', level='any', nation=None, club=None, league=None,
             position=None, min_rating=0, rare_only=False)
    p.update(values)
    if type(p['size']) is not int or p['size'] not in (1, 3, 6, 12): raise PackError('Choose 1, 3, 6 or 12 players.')
    if p['edition'] not in ('standard', 'prime') or p['level'] not in ('any', 'bronze', 'silver', 'gold'):
        raise PackError('Unknown card edition or base tier.')
    if p['position'] is not None and (not isinstance(p['position'], str) or p['position'] not in POSITIONS):
        raise PackError('Unknown position.')
    for key in ('nation', 'club', 'league'):
        if p[key] is not None and (type(p[key]) is not int or p[key] <= 0): raise PackError('Invalid '+key+'.')
    if type(p['min_rating']) is not int or not 0 <= p['min_rating'] <= 115: raise PackError('Rating must be 0–115.')
    if type(p['rare_only']) is not bool: raise PackError('Rare-only must be true or false.')
    return p


def eligible(p, settings):
    custom = settings.section('custom_cards')
    spec = custom['editions'].get('prime')
    if p['edition'] == 'prime' and (not custom['enabled'] or spec is None or not spec['enabled']): return []
    pool = []
    for asset, base in fut_packs.STANDARD_PLAYERS.items():
        if p['nation'] is not None and base['nation'] != p['nation']: continue
        if p['club'] is not None and base['teamid'] != p['club']: continue
        if p['league'] is not None and base['leagueId'] != p['league']: continue
        if p['level'] != 'any' and fut_packs.level(base) != p['level']: continue
        if p['rare_only'] and not fut_packs.rare(base): continue
        rating, position = base['rating'], base['preferredPosition']
        if p['edition'] == 'prime':
            if not card_versions._enabled(spec, asset): continue
            rule = spec['players'].get(str(asset), {})
            rating = card_versions._rating(spec, base, rule)
            position = rule.get('position', position)
        if p['position'] is not None and position != p['position']: continue
        if rating >= p['min_rating']: pool.append(asset)
    return pool


def quote(values, settings):
    p = parameters(values)
    rules = settings.section('web_packs')
    if not rules['enabled']: raise PackError('Custom web packs are currently disabled.')
    additions = [('Players', rules['base_per_player'])]
    filters = 0
    for key, label in (('level', 'Base card tier'), ('nation', 'Nationality'), ('club', 'Club'),
                       ('league', 'League'), ('position', 'Position')):
        if p[key] not in (None, 'any'):
            filters += 1
            additions.append((label, rules['filter_surcharge']))
    if p['edition'] == 'prime':
        filters += 1
        additions.append(('Prime edition', rules['prime_surcharge']+rules['filter_surcharge']))
    if p['min_rating']:
        filters += 1
        additions.append(('Minimum rating', rules['filter_surcharge']+
                          max(0, p['min_rating']-74)*rules['rating_step_surcharge']))
    if p['rare_only']:
        filters += 1
        additions.append(('Rare base players', rules['rare_surcharge']+rules['filter_surcharge']))
    breakdown = [{'label': label, 'coins': price*p['size']} for label, price in additions]
    subtotal = sum(row['coins'] for row in breakdown)
    extra = subtotal*max(0, filters-1)*rules['extra_filter_percent']//100
    if extra: breakdown.append({'label': 'Combined filters', 'coins': extra})
    pool = eligible(p, settings)
    fingerprint = hashlib.sha256(json.dumps({'parameters':p, 'pricing':rules,
        'custom_cards':settings.section('custom_cards')}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'parameters':p, 'price':subtotal+extra, 'breakdown':breakdown, 'matching_players':len(pool),
            'available':len(pool) >= p['size'], 'rules':fingerprint}, pool


def open_pack(owner, auth, token, *, duplicate_consent=False, clock=time.time):
    if duplicate_consent is not True: raise PackError('Confirm automatic quick-sell of exact duplicate versions.')
    if not isinstance(token, str) or not 20 <= len(token) <= 100: raise PackError('Invalid pack quote.')
    now = int(clock())
    key = digest(token)
    store = owner.fut_inventory
    with owner.lock:
        cached = store.state.get('webPackReceipts', {}).get(key)
        if cached: return deepcopy(cached['result'])
        saved, expires = auth.get_quote(owner.profile['persona'], token)
        settings = owner.settings.snapshot()
        current, pool = quote(saved['parameters'], settings)
        if current['price'] != saved['price'] or current['rules'] != saved['rules']:
            raise PackError('Server rules changed. Refresh the price before buying.')
        if not current['available']: raise PackError('Too few distinct players match these filters.')
        if store.state['coins'] < current['price']: raise PackError('Not enough FUT coins.')
        # A web purchase never disrupts an unresolved in-game pack or a live match.
        if store.state['pendingPack']: raise PackError('Resolve your unassigned FUT pack before opening a web pack.')
        for session in owner.root.sessions.values():
            if owner.root.for_session(session) is not owner: continue
            for field in ('local_fut_match', 'online_fut_match'):
                match = getattr(session, field, None)
                if match and not match.get('finished') and not match.get('cancelled'):
                    raise PackError('Return to the FUT menus before opening a web pack.')
        rng = random.SystemRandom()
        players = []
        for asset in rng.sample(pool, current['parameters']['size']):
            if current['parameters']['edition'] == 'prime':
                player, variant = card_versions.catalogue_player(settings, 'prime', asset)
                players.append({**player, '_edition':'prime', '_variant':variant})
            else: players.append(fut_packs.STANDARD_PLAYERS[asset])
        if current['parameters']['edition'] == 'standard' and current['parameters']['level'] == 'gold':
            players = card_versions.upgrade_standard_draw(players, settings, rng)
        state = deepcopy(store.state)
        owned = {card_versions.resource_id(row) for row in state['items'] if row['pile'] == 'club'}
        cards, refunds = [], 0
        for player in players:
            row = {'id':state['nextItemId'], 'assetId':player['assetId'], 'edition':player.get('_edition', 'standard'),
                   'pile':'club', 'timestamp':now}
            state['nextItemId'] += 1
            if '_variant' in player: row['variant'] = deepcopy(player['_variant'])
            card = store.card(row, settings)
            duplicate = card['resourceId'] in owned
            refund = card['discardValue'] if duplicate else 0
            if duplicate: refunds += refund
            else:
                state['items'].append(row)
                owned.add(card['resourceId'])
            cards.append({**card, 'name':player['name'], 'edition':row['edition'],
                          'duplicate':duplicate, 'quick_sell_coins':refund})
        state['coins'] += refunds-current['price']
        result = {'cards':cards, 'price':current['price'], 'duplicate_refund':refunds,
                  'coins':state['coins'], 'parameters':current['parameters'], 'opened_at':now}
        # Receipt + coins + owned cards are one atomic JSON commit. Expired quote
        # tokens cannot charge again after their old receipt has been pruned.
        receipts = state.setdefault('webPackReceipts', {})
        receipts = {k:v for k,v in receipts.items() if v['expires'] > now-86400}
        receipts[key] = {'created':now, 'expires':expires, 'result':deepcopy(result)}
        state['webPackReceipts'] = receipts
        store.commit(state)
        owner.events.emit('web.pack.opened', player=store.player(), price=current['price'],
                          parameters=current['parameters'], duplicate_refund=refunds,
                          cards=[{'item_id':r['id'], 'asset_id':r['assetId'], 'edition':r['edition'],
                                  'duplicate':r['duplicate']} for r in cards])
        return result


def validate_receipts(state):
    receipts = state.get('webPackReceipts', {})
    if not isinstance(receipts, dict): raise ValueError('Invalid web pack receipts')
    for key, row in receipts.items():
        if (not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key)
                or not isinstance(row, dict) or set(row) != {'created', 'expires', 'result'}
                or any(type(row[k]) is not int or row[k] < 0 for k in ('created','expires'))
                or not isinstance(row['result'], dict)):
            raise ValueError('Invalid web pack receipt')
        result = row['result']
        if (set(result) != {'cards','price','duplicate_refund','coins','parameters','opened_at'}
                or any(type(result[k]) is not int or result[k] < 0 for k in ('price','duplicate_refund','coins','opened_at'))
                or not isinstance(result['cards'], list)):
            raise ValueError('Invalid web pack receipt result')
        p = parameters(result['parameters'])
        if len(result['cards']) != p['size']: raise ValueError('Invalid web pack receipt cards')
        for card in result['cards']:
            if (not isinstance(card, dict) or type(card.get('id')) is not int or card['id'] < 1
                    or type(card.get('assetId')) is not int or card['assetId'] not in fut_packs.STANDARD_PLAYERS
                    or type(card.get('resourceId')) is not int or card['resourceId'] < 1
                    or card['resourceId'] & 0xffffff != card['assetId']
                    or card.get('edition') not in ('standard','prime')
                    or (card['resourceId'] >> 24 != 0 if card['edition'] == 'standard'
                        else not 1 <= card['resourceId'] >> 24 <= 127)
                    or type(card.get('rating')) is not int or not 1 <= card['rating'] <= 115
                    or not isinstance(card.get('name'), str) or not 1 <= len(card['name']) <= 128
                    or not isinstance(card.get('preferredPosition'), str) or card['preferredPosition'] not in POSITIONS
                    or type(card.get('duplicate')) is not bool
                    or type(card.get('quick_sell_coins')) is not int or card['quick_sell_coins'] < 0
                    or not card['duplicate'] and card['quick_sell_coins'] != 0
                    or not isinstance(card.get('attributeList'), list) or len(card['attributeList']) != 6):
                raise ValueError('Invalid saved web pack card')
            for index, stat in enumerate(card['attributeList']):
                if (not isinstance(stat, dict) or set(stat) != {'index','value'}
                        or type(stat['index']) is not int or stat['index'] != index
                        or type(stat['value']) is not int or not 0 <= stat['value'] <= 115):
                    raise ValueError('Invalid saved web pack attributes')
        if (sum(c['quick_sell_coins'] for c in result['cards']) != result['duplicate_refund']
                or row['created'] != result['opened_at'] or row['expires'] < row['created']):
            raise ValueError('Inconsistent web pack receipt')
