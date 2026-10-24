"""Persistent local coin packs and player inventory for the PS3 FUT service.

Schemas: pack offers 0x16aabe4, purchase request 0x16b4a48, purchase result
0x16b4ca4, items 0x16a7fb8, duplicates 0x16a9404, move 0x16cf08c/16cf5bc,
discard 0x16b7324/16b7504. This owns only the local club, never an EA account.
"""
from copy import deepcopy
import json
from pathlib import Path
import random
import re
import time
from urllib.parse import parse_qs, urlsplit

from pow_services import json_reply
import fut_packs
import card_versions
import fut_items

PREFIX = '/ut/game/fifa19'
DISCARD_PATH = '/ut/delete/game/fifa19/item'
PACK_ID, PACK_PRICE, PACK_SIZE, STARTING_COINS = 6, 7500, 12, 20000
CATALOG = json.loads((Path(__file__).parent/'data/england-pack-players.json').read_text(encoding='utf-8'))['players']
PLAYERS = {p['assetId']: p for p in CATALOG}
PLAYER_EDITIONS = {'england110': PLAYERS, 'standard': fut_packs.STANDARD_PLAYERS}
ITEM_PATH = re.compile(re.escape(PREFIX) + r'/item(?:/([0-9]{1,19}))?')
ROUTES = {
    PREFIX+'/hub': ('GET',), PREFIX+'/user/credits': ('GET',),
    PREFIX+'/store/purchasegroup/all': ('GET',),
    PREFIX+'/purchased/items': ('GET', 'POST'), PREFIX+'/club': ('GET',),
    PREFIX+'/clientdata/userHubData': ('GET', 'PUT'),
    PREFIX+'/tradePile': ('GET',),
    DISCARD_PATH: ('POST',),
}


def integer(value, low=0, high=2**63-1):
    return type(value) is int and low <= value <= high


class Inventory:
    def __init__(self, path, lock, settings=None):
        from server_settings import DEFAULT
        from server_plugins import EventBus
        self.settings = settings or DEFAULT
        self.events = EventBus()
        self.player = lambda: {}
        self.path, self.lock = path, lock
        self.state = {'version': 1, 'coins': self.settings.section('economy')['starting_coins'], 'nextItemId': 10000000,
                      'nextPackId': 1, 'purchaseCount': 0, 'items': [],
                      'pendingPack': None, 'userHubData': []}
        if path and path.exists():
            self.state = json.loads(path.read_text(encoding='utf-8'))
        self.validate(self.state)

    @staticmethod
    def validate(state):
        if (not isinstance(state, dict) or state.get('version') != 1
                or any(not integer(state.get(k)) for k in
                       ('coins', 'nextItemId', 'nextPackId', 'purchaseCount'))
                or not isinstance(state.get('items'), list)
                or not isinstance(state.get('userHubData'), list)):
            raise ValueError('Invalid local FUT inventory')
        ids, club_assets = set(), set()
        for record in state['items']:
            if (not isinstance(record, dict) or not integer(record.get('id'), 1)
                    or not integer(record.get('assetId'), 1, (1 << 24)-1)
                    or record.get('pile') not in ('club', 'purchased')
                    or not integer(record.get('timestamp')) or record['id'] in ids):
                raise ValueError('Invalid local FUT player card')
            edition = record.get('edition', 'england110')
            if not isinstance(edition, str): raise ValueError('Invalid card edition')
            if edition in PLAYER_EDITIONS:
                if record['assetId'] not in PLAYER_EDITIONS[edition] or 'variant' in record:
                    raise ValueError('Invalid base player card')
            else:
                card_versions.validate_variant(edition, record['assetId'], record.get('variant'))
            identity = card_versions.resource_id(record)
            if record['pile'] == 'club' and identity in club_assets:
                raise ValueError('Duplicate club card version')
            from fut_condition import validate as validate_condition
            validate_condition(record)
            from player_statistics import validate as validate_statistics
            validate_statistics(record)
            from admin_cards import validate_overrides
            validate_overrides(record.get('overrides',{}))
            if ('position' in record and (not isinstance(record['position'],str) or record['position'] not in card_versions.POSITIONS)
                    or 'playStyle' in record and not integer(record['playStyle'],250,273)
                    or 'firstOwner' in record and type(record['firstOwner']) is not bool):
                raise ValueError('Invalid saved player chemistry/position')
            ids.add(record['id'])
            if record['pile'] == 'club':
                club_assets.add(identity)
        if ids and state['nextItemId'] <= max(ids):
            raise ValueError('Invalid local FUT next item ID')
        from fut_items import records as support_records, validate as validate_support
        validate_support(state, ids)
        pending = state.get('pendingPack')
        unassigned = {r['id'] for r in state['items']+support_records(state) if r['pile'] == 'purchased'}
        if pending is None:
            if unassigned:
                raise ValueError('Unassigned cards have no local pack')
        elif (not isinstance(pending, dict) or not integer(pending.get('packId'))
              or pending['packId'] not in fut_packs.PACKS
              or not integer(pending.get('id'), 1) or pending['id'] >= state['nextPackId']
              or not isinstance(pending.get('itemIds'), list)
              or len(pending['itemIds']) != fut_packs.PACKS[pending['packId']]['size']
              or any(not integer(i, 1) for i in pending['itemIds'])
              or len(set(pending['itemIds'])) != len(pending['itemIds'])
              or not unassigned or not unassigned <= set(pending['itemIds'])):
            raise ValueError('Invalid local FUT pending pack')
        counts = state.get('packPurchaseCounts', {})
        if (not isinstance(counts, dict) or any(k not in {str(i) for i in fut_packs.PACKS}
                or not integer(v) for k, v in counts.items())
                or counts and sum(counts.values()) != state['purchaseCount']):
            raise ValueError('Invalid per-pack purchase counts')
        validate_entries({'entries': state['userHubData']})
        if state.get('squad') is not None:
            from fut_squads import validate
            validate(state['squad'], state['items'], support_records(state))
        from fut_squads import validate
        others = state.get('otherSquads', {})
        if not isinstance(others, dict) or len(others) > 29:
            raise ValueError('Invalid saved squads')
        active_id = (state.get('squad') or {'id': 1})['id']
        for key, squad in others.items():
            validate(squad, state['items'], support_records(state))
            if key != str(squad['id']) or squad['id'] == active_id:
                raise ValueError('Duplicate squad ID')
        if 'nextSquadId' in state and not integer(state['nextSquadId'], max([active_id]+[int(k) for k in others])+1, 1000000000):
            raise ValueError('Invalid next squad ID')
        if 'equipment' in state:
            from fut_club_assets import validate_preferences
            validate_preferences(state['equipment'])
        if 'matchHistory' in state:
            from fut_results import validate_history
            validate_history(state['matchHistory'])
        if 'offlineSeason' in state:
            from fut_seasons import validate_saved_progress
            validate_saved_progress(state['offlineSeason'])
        from fut_seasons import validate_season_history
        validate_season_history(state)
        from admin_cards import validate_audit
        validate_audit(state)
        from fut_tournaments import validate_state
        validate_state(state)
        from fut_online import validate_season_state
        validate_season_state(state)
        if 'homeStadium' in state:
            from fut_club_assets import VENUES
            if type(state['homeStadium']) is not int or state['homeStadium'] not in VENUES:
                raise ValueError('Unknown local home stadium')
        if 'webPackReceipts' in state:
            from web_packs import validate_receipts
            validate_receipts(state)
        from fut_contracts import validate as validate_contracts
        validate_contracts(state)

    def commit(self, state):
        self.validate(state)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, indent=2)+'\n')
            temporary.replace(self.path)
        self.state = state

    def ensure_saved(self):
        with self.lock:
            if self.path and not self.path.exists():
                self.commit(deepcopy(self.state))

    def currencies(self):
        with self.lock:
            return {'currencies': [{'name': n, 'funds': self.state['coins'] if n == 'coins' else 0,
                                    'finalFunds': self.state['coins'] if n == 'coins' else 0}
                                   for n in ('coins', 'points', 'DRAFT_TOKEN')],
                    'unopenedPacks': {'preOrderPacks': 0, 'recoveredPacks': 0}}

    @staticmethod
    def card(record, settings=None):
        from server_settings import DEFAULT
        settings = settings or DEFAULT
        if 'definitionId' in record:
            from fut_items import card
            return card(record)
        edition = record.get('edition', 'england110')
        standard = edition == 'standard'
        custom = edition not in PLAYER_EDITIONS
        player = (card_versions.variant_player(record['assetId'], record['variant']) if custom
                  else PLAYER_EDITIONS[edition][record['assetId']])
        if 'position' in record:
            player = dict(player,preferredPosition=record['position'])
        if record.get('overrides'):
            from admin_cards import apply_overrides
            player=apply_overrides(player,record['overrides'])
        if record.get('trainingBoost'):
            player = deepcopy(player)
            for stat, boost in zip(player['attributeList'], record['trainingBoost']):
                stat['value'] = min(card_versions.MAX_ATTRIBUTE, stat['value']+boost)
        tier, rarity = (fut_packs.level(player), fut_packs.rare(player)) if standard else ('gold', 1)
        from fut_condition import fitness
        from player_statistics import lists as statistics_lists
        current_stats, lifetime_stats = statistics_lists(record)
        keeper = player['preferredPosition']=='GK'
        style = record.get('playStyle',273 if keeper else 250)
        # Admin/config position edits can cross the GK boundary. Keep the
        # saved choice but never send an incompatible style to the client.
        if (style>=269) != keeper: style = 273 if keeper else 250
        # Display names and portraits come from the game's DB using assetId.
        card = {'id': record['id'], 'resourceId': card_versions.resource_id(record),
                **{k: deepcopy(v) for k, v in player.items() if k != 'name'},
                'cardsubtypeid': 1, 'itemType': 'player', 'itemState': 'free',
                'contract': record.get('contract',7), 'fitness': fitness(record,settings), 'injuryGames': record.get('injuryGames', 0),
                'injuryType': record.get('injuryType', 'none'),
                'suspension': record.get('suspension', 0), 'rareflag': record['variant']['rareflag'] if custom else fut_packs.rare(player) if standard else 1, 'owners': 1,
                # All existing cards were issued directly to their club. A
                # future transferred card can explicitly set firstOwner=False.
                'loyaltyBonus': int(record.get('firstOwner',True) or record.get('statistics',{}).get('gamesPlayed',0)>=10),
                'discardValue': card_versions.discard_value(record, settings) if custom else settings.section('economy')['quick_sell'][tier][rarity], 'timestamp': record['timestamp'],
                'untradeable': False, 'dream': False, 'training': int(bool(record.get('trainingBoost'))),
                'playStyle': style,
                # Retail notification getter 0x13eefe4 checks statsList[4],
                # independently of the suspension byte used for eligibility.
                # This is the current ban, not a lifetime red-card total.
                'statsList': current_stats,
                'lifetimeStats': lifetime_stats}
        if record.get('trainingDefinition'):
            from fut_items import card as support_card
            card['trainingItem'] = support_card({'id':record['trainingItemId'],
                'definitionId':record['trainingDefinition'], 'timestamp':record['timestamp']})
        return card

    def all_items(self, state=None):
        state = self.state if state is None else state
        return state['items']+state.get('supportItems',[])

    @staticmethod
    def identity(record):
        return record['definitionId'] if 'definitionId' in record else card_versions.resource_id(record)

    @staticmethod
    def stackable(record):
        from fut_items import CATALOGUE
        return 'definitionId' in record and CATALOGUE[record['definitionId']]['itemType'] != 'manager'

    def duplicates(self, records):
        club = {self.identity(r): r['id'] for r in self.all_items() if r['pile'] == 'club' and not self.stackable(r)}
        return [{'itemId': r['id'], 'itemLoans': 0,
                 'duplicateItemId': club[self.identity(r)], 'duplicateItemLoans': 0}
                for r in records if not self.stackable(r) and self.identity(r) in club and r['id'] != club[self.identity(r)]]

    def purchased(self, purchase=False):
        with self.lock:
            records = [r for r in self.all_items() if r['pile'] == 'purchased']
            value = {'itemList' if purchase else 'itemData': [self.card(r, self.settings) for r in records],
                     'duplicateItemIdList': self.duplicates(records)}
            if purchase:
                value.update(numberItems=len(records), purchasedPackId=self.state['pendingPack']['id'])
            return value

    def hub(self):
        with self.lock:
            return {'clubPlayers': sum(r['pile'] == 'club' for r in self.state['items']),
                    'auctionCount': 0, 'liveMessagesAvailable': False}

    def offers(self):
        with self.lock:
            counts = self.state.get('packPurchaseCounts', {'3': self.state['purchaseCount']})
            return {'purchase': [{
                'id': pack_id, 'assetId': spec['artwork'], 'description': spec['name'],
                'dealType': 'standard', 'displayGroup': {'value': spec['level'], 'priority': order},
                'displayGroupAssetId': spec['artwork'], 'displayGroupUseDefaultImage': True,
                'currencies': [{'name': 'coins', 'funds': spec['price'], 'finalFunds': spec['price']}],
                'isPremium': spec['rares'] > 1, 'unopened': False, 'useDefaultImage': True, 'visible': True,
                'sortPriority': order, 'state': 'active', 'saleType': 'NONE', 'quantity': 0,
                'purchaseLimit': 2147483647, 'purchaseCount': counts.get(str(pack_id), 0),
                'start': 1, 'end': 2147483647, 'packType': 'CARDPACK', 'actionType': 'CREATEPACK',
                'packContentInfo': {'itemQuantity': spec['size'],
                                    'goldQuantity': spec['size'] if spec['level'] == 'gold' else 0,
                                    'silverQuantity': spec['size'] if spec['level'] == 'silver' else 0,
                                    'bronzeQuantity': spec['size'] if spec['level'] == 'bronze' else 0,
                                    'rareQuantity': spec['rares']},
                'packOdds': []} for order, (pack_id, spec) in enumerate(
                    ((key, self.settings.pack(key)) for key in fut_packs.PACKS), 1) if not spec.get('retired')], 'timestamp': int(time.time()),
                'packOddsAvailabilityState': 'AVAILABLE'}

    def open_pack(self, payload):
        if (not isinstance(payload, dict) or set(payload) !=
                {'packId', 'useCredits', 'usePreOrder', 'currency'}
                or not integer(payload.get('packId')) or payload['packId'] not in fut_packs.PACKS
                or type(payload.get('useCredits')) is not int or payload['useCredits'] != 1
                or type(payload.get('usePreOrder')) is not int or payload['usePreOrder'] != 0
                or payload.get('currency') != 'COINS'):
            raise ValueError('Only local coin player packs are supported')
        with self.lock:
            # No native idempotency key exists. While cards remain unassigned,
            # a retry resumes them without charging or drawing another pack.
            if self.state['pendingPack']:
                if self.state['pendingPack']['packId'] != payload['packId']:
                    raise Conflict('Resolve your unassigned cards before buying a different pack')
                return self.purchased(purchase=True)
            pack_id = payload['packId']
            spec = self.settings.pack(pack_id)
            if spec.get('retired'):
                raise Conflict('This pack has been retired')
            if self.state['coins'] < spec['price']:
                raise Conflict('Not enough local coins')
            state = deepcopy(self.state)
            if spec['edition']=='support':
                from fut_items import draw
                selected = [{'definitionId': i} for i in draw(spec)]
            else:
                selected = fut_packs.draw(spec, CATALOG, self.settings.section('packs')['gold_rare_weights'], settings=self.settings)
            now = int(time.time())
            item_ids = []
            for player in selected:
                item = {'id': state['nextItemId'], 'pile': 'purchased', 'timestamp': now}
                if 'definitionId' in player: item['definitionId'] = player['definitionId']
                else: item.update(assetId=player['assetId'], edition=player.get('_edition', spec['edition']))
                if '_variant' in player: item['variant'] = deepcopy(player['_variant'])
                item_ids.append(item['id'])
                state.setdefault('supportItems' if 'definitionId' in item else 'items', []).append(item)
                state['nextItemId'] += 1
            state['pendingPack'] = {'packId': pack_id, 'id': state['nextPackId'], 'itemIds': item_ids}
            state['nextPackId'] += 1
            state.setdefault('packPurchaseCounts', {'3': state['purchaseCount']})
            state['packPurchaseCounts'][str(pack_id)] = state['packPurchaseCounts'].get(str(pack_id), 0)+1
            state['purchaseCount'] += 1
            state['coins'] -= spec['price']
            self.commit(state)
            self.events.emit('pack.opened', player=self.player(), pack_id=pack_id,
                             purchase_id=state['pendingPack']['id'], price=spec['price'],
                             cards=[{'item_id': r['id'], 'asset_id': self.card(r,self.settings)['assetId'],
                                     'edition': r.get('edition','support' if 'definitionId' in r else 'england110'),
                                     'resource_id': self.identity(r)}
                                    for r in self.all_items(state) if r['id'] in item_ids])
            return self.purchased(purchase=True)

    @staticmethod
    def finish_pending(state):
        if not any(r['pile'] == 'purchased' for r in state['items']+state.get('supportItems',[])):
            state['pendingPack'] = None

    def move(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'itemData'}:
            raise ValueError('Expected itemData')
        moves = payload['itemData']
        if not isinstance(moves, list) or not 1 <= len(moves) <= 100:
            raise ValueError('Invalid itemData')
        ids = []
        for move in moves:
            if (not isinstance(move, dict) or set(move) - {'id', 'pile', 'swap', 'tradeId'}
                    or not integer(move.get('id'), 1) or move.get('pile') != 'club'
                    or any(not integer(move.get(k, 0)) or move.get(k, 0) != 0
                           for k in ('swap', 'tradeId'))):
                raise ValueError('Only sending local cards to the club is supported')
            ids.append(move['id'])
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate item IDs')
        with self.lock:
            state = deepcopy(self.state)
            by_id = {r['id']: r for r in self.all_items(state)}
            if any(i not in by_id for i in ids):
                raise ValueError('Unknown local item')
            owned = {self.identity(r) for r in self.all_items(state) if r['pile'] == 'club' and not self.stackable(r)}
            replies = []
            for i in ids:
                record = by_id[i]
                success = record['pile'] == 'club' or self.stackable(record) or self.identity(record) not in owned
                if success:
                    record['pile'] = 'club'
                    if not self.stackable(record): owned.add(self.identity(record))
                replies.append({'id': i, 'pile': 'club', 'success': success,
                                **({} if success else {'reason': 'Destination Full'})})
            self.finish_pending(state)
            self.commit(state)
            self.events.emit('cards.moved', player=self.player(), items=deepcopy(replies))
            return {'itemData': replies}

    def discard(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {'itemId'}
                or not isinstance(payload['itemId'], list)
                or not 1 <= len(payload['itemId']) <= 100
                or any(not integer(i, 1) for i in payload['itemId'])
                or len(set(payload['itemId'])) != len(payload['itemId'])):
            raise ValueError('Expected itemId array')
        ids = set(payload['itemId'])
        with self.lock:
            state = deepcopy(self.state)
            owned = {r['id'] for r in self.all_items(state)}
            if not ids <= owned:
                raise ValueError('Unknown local item')
            credits = sum(self.card(r, self.settings)['discardValue'] for r in self.all_items(state) if r['id'] in ids)
            state['coins'] += credits
            state['items'] = [r for r in state['items'] if r['id'] not in ids]
            if 'supportItems' in state: state['supportItems'] = [r for r in state['supportItems'] if r['id'] not in ids]
            for squad in ([state['squad']] if state.get('squad') else []) + list(state.get('otherSquads', {}).values()):
                for slot in squad['players']:
                    if slot['itemData']['id'] in ids:
                        slot['itemData']['id'] = 0
                if squad['manager'][0]['id'] in ids: squad['manager'][0]['id'] = 0
                if squad['captain'] in ids:
                    squad['captain'] = 0
                for kick in squad['kicktakers']:
                    if kick['id'] in ids:
                        kick['id'] = 0
            self.finish_pending(state)
            self.commit(state)
            self.events.emit('cards.discarded', player=self.player(), item_ids=sorted(ids), coins=credits)
            return {'totalCredits': state['coins'], 'items': [{'id': i} for i in payload['itemId']]}


class Conflict(Exception):
    pass


def club_search(store, query):
    # Query names and encodings come from the retail builder 0x16deb00..
    # 0x16df4f0, including year=2019 and position strings from 0x171ab0c.
    allowed = {'type', 'year', 'start', 'count', 'position', 'level', 'rare',
               'nation', 'country', 'league', 'playStyle', 'state', 'filter',
               'sort', 'defId', 'formation', 'team'}
    if (set(query) - allowed or query.get('type', ['player']) != ['player']
            or query.get('year', ['2019']) != ['2019']):
        raise ValueError('Invalid club query')
    values = {k: v[0] for k, v in query.items()}
    start, count = int(values.get('start', '0')), int(values.get('count', '100'))
    if not 0 <= start <= 100000 or not 1 <= count <= 200:
        raise ValueError('Invalid club page')
    if values.get('sort', 'desc') not in ('asc', 'desc') or values.get('filter', 'any') not in ('any', 'available', 'base', 'exact'):
        raise ValueError('Invalid club sort/filter')
    cards = [store.card(r, store.settings) for r in store.state['items'] if r['pile'] == 'club']
    positions = card_versions.POSITIONS
    if 'position' in values:
        if values['position'] not in positions:
            raise ValueError('Invalid player position')
        cards = [p for p in cards if p['preferredPosition'] == values['position']]
    if 'level' in values:
        if values['level'] not in ('any', 'gold', 'silver', 'bronze'):
            raise ValueError('Invalid player level')
        if values['level'] != 'any':
            cards = [p for p in cards if ('gold' if p['rating'] >= 75 else 'silver' if p['rating'] >= 65 else 'bronze') == values['level']]
    for key, field in (('nation', 'nation'), ('league', 'leagueId'), ('team', 'teamid'), ('playStyle', 'playStyle'), ('rare', 'rareflag')):
        if key in values:
            number = int(values[key])
            if not -1 <= number < 2**31:
                raise ValueError('Invalid club numeric filter')
            if number != -1:
                cards = [p for p in cards if p[field] == number]
    if 'country' in values:
        country = int(values['country'])
        if not 0 <= country < 2**31:
            raise ValueError('Invalid league country')
        cards = [p for p in cards if fut_packs.LEAGUE_COUNTRIES.get(p['leagueId']) == country]
    if 'state' in values:
        if values['state'] not in ('any', 'free', 'inGame', 'onSale', 'offered'):
            raise ValueError('Invalid item state')
        if values['state'] != 'any':
            cards = [p for p in cards if p['itemState'] == values['state']]
    if 'defId' in values:
        if not re.fullmatch(r'[0-9]{1,10}(?:,[0-9]{1,10}){0,99}', values['defId']):
            raise ValueError('Invalid definition IDs')
        assets = set(map(int, values['defId'].split(',')))
        if values.get('filter') == 'exact': cards = [p for p in cards if p['resourceId'] in assets]
        else: cards = [p for p in cards if p['assetId'] in {i & 0xffffff for i in assets}]
    if 'formation' in values:
        from fut_squads import FORMATIONS
        if values['formation'] not in FORMATIONS:
            raise ValueError('Invalid formation')
    # Equal-rated local cards retain stable IDs across page reads.
    cards.sort(key=lambda p: (-p['rating'], p['id']) if values.get('sort', 'desc') == 'desc' else (p['rating'], p['id']))
    return {'itemData': cards[start:start+count]}


def validate_entries(payload):
    if not isinstance(payload, dict) or set(payload) != {'entries'}:
        raise ValueError('Expected entries')
    entries = payload['entries']
    if (not isinstance(entries, list) or len(entries) > 64
            or any(not isinstance(e, dict) or set(e) != {'key', 'value'}
                   or not integer(e['key'], 0, 2**31-1)
                   or not integer(e['value'], -2**31, 2**31-1) for e in entries)
            or len({e['key'] for e in entries}) != len(entries)):
        raise ValueError('Invalid entries')
    return entries


def response(backend, method, path, raw):
    # Import here so entry bootstrap can reuse Inventory without import cycles.
    from fut_services import request_headers, session_for
    parsed = urlsplit(path)
    route = parsed.path
    item = ITEM_PATH.fullmatch(route)
    resource = re.fullmatch(re.escape(PREFIX)+r'/item/resource/([0-9]{1,19})', route)
    consumables = re.fullmatch(re.escape(PREFIX)+r'/club/consumables/([A-Za-z0-9]{1,48})', route)
    support_stats = re.fullmatch(re.escape(PREFIX)+r'/club/stats/(staff|consumables)', route)
    if route not in ROUTES and not item and not resource and not consumables and not support_stats:
        return None
    # Retail single-item quick sell deletes one card by URL, without a body.
    # DiscardCard (0x2b) uses the separate POST itemId-array route.
    methods = ('POST', 'PUT') if resource else ('GET',) if consumables or support_stats else ROUTES[route] if not item else (('GET', 'PUT', 'DELETE') if item[1] else ('GET', 'PUT'))
    if method not in methods:
        return json_reply({'error': 'Method not allowed'}, 'FUT.WrongMethod',
                          '405 Method Not Allowed', [('Allow', ', '.join(methods))])
    head, _, body = raw.partition(b'\r\n\r\n')
    with backend.lock:
        session = session_for(backend, request_headers(head))
        if session is None:
            return json_reply({'error': 'Local FUT session required'}, 'FUT.LoginRequired', '401 Unauthorized')
        backend = backend.for_session(session)
        store = backend.fut_inventory
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError('Repeated query parameter')
            payload = json.loads(body) if body else None
            if resource:
                if query: raise ValueError('Unexpected consumable query')
                from fut_items import apply
                value = apply(backend,int(resource[1]),payload,by_resource=True)
            elif support_stats:
                if query or body: raise ValueError('Unexpected staff/consumable stats parameters')
                from fut_items import club_statistics
                value = club_statistics(store,support_stats[1])
            elif consumables:
                if body: raise ValueError('Unexpected consumable body')
                from fut_items import consumable_list
                value = consumable_list(store,consumables[1],query)
            elif route == PREFIX+'/store/purchasegroup/all':
                if set(query) - {'ppInfo'} or query.get('ppInfo', ['true']) != ['true'] or body:
                    raise ValueError('Invalid pack-list request')
                store.ensure_saved()
                value = store.offers()
            elif route == PREFIX+'/club':
                if body:
                    raise ValueError('Invalid club query')
                if query.get('type', ['player']) == ['player']:
                    value = club_search(store, query)
                elif query.get('type',[''])[0] in fut_items.TYPES | {'staff','training','consumable','consumables'}:
                    from fut_items import search
                    value = search(store,query)
                else:
                    from fut_club_assets import search
                    value = search(store, query)
            elif item and method == 'GET':
                if body or set(query) - {'idList'} or (item[1] and query):
                    raise ValueError('Invalid item query')
                text = item[1] or query.get('idList', [''])[0]
                if not re.fullmatch(r'[0-9]{1,19}(?:,[0-9]{1,19}){0,99}', text):
                    raise ValueError('Expected item IDs')
                ids = set(map(int, text.split(',')))
                from fut_club_assets import equipment
                cosmetics = [c for c in equipment(store) if c['id'] in ids]
                records = [r for r in store.all_items() if r['id'] in ids]
                if len(records)+len(cosmetics) != len(ids):
                    raise ValueError('Unknown local item')
                value = {'itemData': [store.card(r, store.settings) for r in records]+cosmetics,
                         'duplicateItemIdList': store.duplicates(records)}
            else:
                if query or (method == 'GET' and body):
                    raise ValueError('Invalid request')
                if route == PREFIX+'/hub':
                    value = store.hub()
                elif route == PREFIX+'/user/credits':
                    store.ensure_saved()
                    value = store.currencies()
                elif route == PREFIX+'/purchased/items':
                    value = store.open_pack(payload) if method == 'POST' else store.purchased()
                elif route == PREFIX+'/tradePile':
                    # 0x16c7428 delegates to 0x16a9558. Trading is disabled;
                    # an empty auctionInfo array completes the native cache.
                    value = {'auctionInfo': [], 'duplicateItemIdList': [],
                             'credits': store.state['coins'], 'total': 0}
                elif item and method == 'DELETE':
                    if body:
                        raise ValueError('Single-item quick sell has no body')
                    value = store.discard({'itemId': [int(item[1])]})
                elif item and item[1]:
                    if isinstance(payload,dict) and 'apply' in payload:
                        from fut_items import apply
                        value = apply(backend,int(item[1]),payload)
                    else:
                        from fut_club_assets import activate
                        value = activate(backend, int(item[1]), payload)
                elif item:
                    value = store.move(payload)
                elif route == DISCARD_PATH:
                    value = store.discard(payload)
                elif route == PREFIX+'/clientdata/userHubData':
                    if method == 'PUT':
                        entries = validate_entries(payload)
                        state = deepcopy(store.state)
                        # Native client data writes are patches, not replacements.
                        combined = {e['key']: e['value'] for e in state['userHubData']}
                        combined.update({e['key']: e['value'] for e in entries})
                        state['userHubData'] = [{'key': k, 'value': v} for k, v in sorted(combined.items())]
                        store.commit(state)
                    value = {'entries': deepcopy(store.state['userHubData'])}
                else:
                    value = {'selection': {'category': '', 'view': '', 'period': ''},
                             'options': [], 'permutations': [], 'scrollDelay': 0}
            return json_reply(value, 'FUT.LocalInventory')
        except fut_items.WrongTrainingTarget as error:
            return json_reply({'error':str(error)},'FUT.TrainingWrongTarget','400 Bad Request')
        except Conflict as error:
            return json_reply({'error': str(error)}, 'FUT.LocalPackConflict', '409 Conflict')
        except (ValueError, TypeError, UnicodeError):
            return json_reply({'error': 'Invalid local inventory request'},
                              'FUT.InvalidInventoryRequest', '400 Bad Request')
