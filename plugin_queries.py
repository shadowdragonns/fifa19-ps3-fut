"""Read-only, credential-free snapshots for trusted server plugins."""
from copy import deepcopy
from functools import wraps
from types import SimpleNamespace
from itertools import islice


def _id(value, name):
    if type(value) is not int or not 1 <= value < 2**63:
        raise ValueError(name+' must be a positive integer')


def _page(start, count):
    if type(start) is not int or not 0 <= start <= 1000000 or type(count) is not int or not 1 <= count <= 1000:
        raise ValueError('start must be 0..1000000 and count 1..1000')


def snapshot(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self.root.lock:
            return deepcopy(function(self, *args, **kwargs))
    return wrapped


def player_snapshot(owner):
    root = owner.root
    sessions = [s for s in root.sessions.values() if s.persona == owner.profile['persona'] and s.postauth]
    from fut_squads import display_name
    latest = max(sessions, key=lambda s:s.login_time, default=None)
    return {'persona_id': owner.profile['persona'], 'name': latest.name if latest else owner.profile.get('display_name', owner.profile['name']),
            'club_name': display_name(owner), 'online': bool(sessions),
            'in_fut': any(bool(s.fut_key) for s in sessions), 'session_count': len(sessions),
            'connected_at': min((s.login_time for s in sessions), default=None),
            'last_login': owner.profile.get('last_login'),
            'last_seen': root.presence_last_seen.get(owner.profile['persona'])}


class Queries:
    def __init__(self, backend): self.root = backend.root

    def _owners(self):
        from accounts import saved_backends
        owners = saved_backends(self.root)
        if self.root.multiuser and not self.root.accounts['users']:
            owners.pop(self.root.profile['persona'], None)
        return owners

    def _owner(self, persona_id):
        _id(persona_id, 'persona_id')
        root = self.root
        if root.profile['persona'] == persona_id:
            if not root.multiuser or root.accounts['users']: return root
            raise KeyError(persona_id)
        for owner in root.users.values():
            if owner.profile['persona'] == persona_id: return owner
        return self._owners()[persona_id]

    @snapshot
    def get_players(self, *, online=None, in_fut=None, start=0, count=100):
        _page(start, count)
        if any(value is not None and type(value) is not bool for value in (online, in_fut)):
            raise ValueError('online/in_fut filters must be bool or None')
        rows = [player_snapshot(owner) for _,owner in sorted(self._owners().items())]
        return [row for row in rows if (online is None or row['online'] == online)
                and (in_fut is None or row['in_fut'] == in_fut)][start:start+count]

    @snapshot
    def get_player(self, persona_id): return player_snapshot(self._owner(persona_id))

    def _club(self, owner):
        from fut_squads import saved, display_name, abbreviation
        from fut_club_assets import active_assets
        from fut_leaderboards import statistics
        state = owner.fut_inventory.state
        name = display_name(owner)
        def progress(key):
            value = state.get(key, {})
            return {k:value[k] for k in ('divisionId', 'round', 'played', 'userPoints') if k in value}
        stats = statistics(owner, 'general')
        return {'persona_id': owner.profile['persona'], 'name': name, 'abbreviation': abbreviation(name),
                'established': owner.profile['fut_club']['established'], 'coins': state['coins'],
                'active_squad_id': saved(owner)['id'],
                'player_count': sum(r['pile'] == 'club' for r in state['items']),
                'unassigned_count': sum(r['pile'] == 'purchased' for r in state['items']),
                'equipment': active_assets(owner), 'statistics': {k:stats[k] for k in ('won', 'draw', 'loss', 'played', 'points', 'value')},
                'offline_season': progress('offlineSeason'), 'online_season': progress('onlineSeason')}

    @snapshot
    def get_club(self, persona_id): return self._club(self._owner(persona_id))

    @snapshot
    def get_clubs(self, *, start=0, count=100):
        _page(start, count)
        owners = sorted(self._owners().items())[start:start+count]
        return [self._club(owner) for _,owner in owners]

    def _metadata(self, player, edition, resource_id, rarity, value, label, version):
        return {'asset_id': player['assetId'], 'resource_id': resource_id, 'name': player['name'],
                'rating': player['rating'], 'position': player['preferredPosition'],
                'nation_id': player['nation'], 'team_id': player['teamid'], 'league_id': player['leagueId'],
                'rare': bool(rarity), 'edition': edition, 'edition_label': label, 'version': version,
                'attributes': deepcopy(player['attributeList']), 'quick_sell_value': value}

    def _catalog_card(self, asset_id, edition):
        from fut_inventory import PLAYER_EDITIONS
        from fut_packs import rare, level
        import card_versions
        if not isinstance(edition, str): raise ValueError('Unknown card edition')
        if edition in PLAYER_EDITIONS:
            player = PLAYER_EDITIONS[edition][asset_id]
            rarity = bool(rare(player)) if edition == 'standard' else True
            tier = level(player) if edition == 'standard' else 'gold'
            value = self.root.settings.section('economy')['quick_sell'][tier][int(rarity)]
            return self._metadata(player, edition, asset_id, rarity, value, edition, 0)
        if edition not in card_versions.edition_names(self.root.settings): raise ValueError('Unknown card edition')
        player, variant = card_versions.catalogue_player(self.root.settings, edition, asset_id)
        return self._metadata(player, edition, (variant['version'] << 24) | asset_id, True,
                              variant['discard_value'], variant['label'], variant['version'])

    def _card(self, record):
        from fut_inventory import PLAYER_EDITIONS
        import card_versions
        edition = record.get('edition', 'england110')
        if edition in PLAYER_EDITIONS:
            card = self._catalog_card(record['assetId'], edition)
        else:
            variant = record['variant']
            card = self._metadata(card_versions.variant_player(record['assetId'], variant), edition,
                                  card_versions.resource_id(record), True,
                                  card_versions.discard_value(record, self.root.settings),
                                  variant['label'], variant['version'])
        return {**card, 'item_id': record['id'], 'pile': record['pile'], 'created_at': record['timestamp'],
                'condition': {'injury_type': record.get('injuryType', 'none'), 'injury_games': record.get('injuryGames', 0),
                              'suspension': record.get('suspension', 0)}}

    def _filters(self, filters):
        allowed = {'min_rating', 'max_rating', 'name', 'position', 'nation_id', 'team_id', 'league_id', 'rare'}
        if set(filters)-allowed: raise ValueError('Unknown card filter')
        for k in ('min_rating', 'max_rating', 'nation_id', 'team_id', 'league_id'):
            if k in filters and (type(filters[k]) is not int or not 0 <= filters[k] <= (115 if 'rating' in k else 2**31-1)):
                raise ValueError('Invalid card filter: '+k)
        if filters.get('min_rating', 0) > filters.get('max_rating', 115): raise ValueError('Invalid rating range')
        if 'rare' in filters and type(filters['rare']) is not bool: raise ValueError('rare must be bool')
        for k in ('name', 'position'):
            if k in filters and (not isinstance(filters[k], str) or not 1 <= len(filters[k]) <= 80):
                raise ValueError('Invalid card text filter')
        return filters

    def _selected(self, card, filters):
        return (filters.get('min_rating', 0) <= card['rating'] <= filters.get('max_rating', 115)
                and all(card[k] == v for k,v in filters.items() if k in ('position', 'nation_id', 'team_id', 'league_id', 'rare'))
                and ('name' not in filters or filters['name'].casefold() in card['name'].casefold()))

    @snapshot
    def get_cards(self, persona_id, *, pile=None, edition=None, start=0, count=100, **filters):
        from fut_inventory import PLAYER_EDITIONS
        import card_versions
        _page(start, count); self._filters(filters)
        owner = self._owner(persona_id)
        editions = set(PLAYER_EDITIONS) | set(card_versions.edition_names(self.root.settings)) | {r.get('edition', 'england110') for r in owner.fut_inventory.state['items']}
        if pile not in (None, 'club', 'purchased'): raise ValueError('Unknown card pile')
        if edition is not None and (not isinstance(edition, str) or edition not in editions): raise ValueError('Unknown card edition')
        cards = (self._card(r) for r in sorted(owner.fut_inventory.state['items'], key=lambda r:r['id'])
                 if (pile is None or r['pile'] == pile) and (edition is None or r.get('edition', 'england110') == edition))
        return list(islice((card for card in cards if self._selected(card, filters)), start, start+count))

    @snapshot
    def get_card(self, persona_id, item_id):
        _id(item_id, 'item_id')
        owner = self._owner(persona_id)
        for record in owner.fut_inventory.state['items']:
            if record['id'] == item_id: return self._card(record)
        raise KeyError(item_id)

    @snapshot
    def get_catalogue_card(self, asset_id, *, edition='standard'):
        _id(asset_id, 'asset_id'); return self._catalog_card(asset_id, edition)

    @snapshot
    def search_catalogue(self, *, edition='standard', start=0, count=100, **filters):
        from fut_inventory import PLAYER_EDITIONS
        import card_versions
        _page(start, count); self._filters(filters)
        if not isinstance(edition, str): raise ValueError('Unknown card edition')
        if edition in PLAYER_EDITIONS: assets = sorted(PLAYER_EDITIONS[edition])
        elif edition in card_versions.edition_names(self.root.settings): assets = card_versions.catalogue_assets(self.root.settings, edition)
        else: raise ValueError('Unknown card edition')
        cards = (self._catalog_card(asset, edition) for asset in assets)
        return list(islice((card for card in cards if self._selected(card, filters)), start, start+count))

    @snapshot
    def get_equipment(self, persona_id, *, item_type=None, active_only=False, start=0, count=100):
        from fut_club_assets import equipment
        _page(start, count)
        if item_type not in (None, 'kit', 'badge', 'ball', 'stadium'): raise ValueError('Unknown equipment type')
        if type(active_only) is not bool: raise ValueError('active_only must be bool')
        cards = equipment(self._owner(persona_id).fut_inventory)
        return [c for c in cards if (item_type is None or c['itemType'] == item_type)
                and (not active_only or c['itemState'] != 'free')][start:start+count]

    def _squad(self, owner, value):
        from fut_squads import saved
        records = {r['id']:r for r in owner.fut_inventory.state['items']}
        return {'squad_id': value['id'], 'name': value['squadName'], 'formation': value['formation'],
                'active': saved(owner)['id'] == value['id'], 'captain_item_id': value['captain'],
                'players': [{'index':slot['index'], 'item_id':slot['itemData']['id'], 'kit_number':slot['kitNumber'],
                             'role':'starting' if slot['index'] < 11 else 'bench' if slot['index'] < 18 else 'reserve',
                             'card':self._card(records[slot['itemData']['id']]) if slot['itemData']['id'] else None}
                            for slot in value['players']]}

    @snapshot
    def get_squads(self, persona_id):
        from fut_squads import squads
        owner = self._owner(persona_id)
        return [self._squad(owner, squad) for _,squad in sorted(squads(owner).items())]

    @snapshot
    def get_squad(self, persona_id, squad_id=None):
        from fut_squads import saved
        if squad_id is not None: _id(squad_id, 'squad_id')
        owner = self._owner(persona_id); return self._squad(owner, saved(owner, squad_id))

    def _match(self, row):
        summary = row['summary']
        return {'match_id':row['id'], 'finished_at':row['finished'],
                'mode':'ONLINE' if row['seasonId'] == 2 else 'OFFLINE',
                'season_id':row['seasonId'], 'division_id':row['divisionId'],
                'result':summary['endReason'], 'seconds_played':summary['secondsPlayed'],
                'coins':row['award']['allCoins'], 'my_stats':summary.get('myMatchStats', {}),
                'opponent_stats':summary.get('opponentMatchStats', {}), 'items':summary['items']}

    @snapshot
    def get_matches(self, persona_id, *, mode=None, start=0, count=100):
        _page(start, count)
        if mode not in (None, 'ONLINE', 'OFFLINE'): raise ValueError('Unknown match mode')
        owner = self._owner(persona_id)
        rows = [self._match(r) for r in reversed(owner.fut_inventory.state.get('matchHistory', []))]
        return [row for row in rows if mode is None or row['mode'] == mode][start:start+count]

    @snapshot
    def get_match(self, persona_id, match_id):
        if not isinstance(match_id, str): raise ValueError('match_id must be a string')
        for row in self._owner(persona_id).fut_inventory.state.get('matchHistory', []):
            if row['id'] == match_id: return self._match(row)
        raise KeyError(match_id)

    @snapshot
    def get_packs(self, *, include_retired=False):
        from fut_packs import PACKS
        if type(include_retired) is not bool: raise ValueError('include_retired must be bool')
        return [self.get_pack(k) for k in sorted(PACKS) if include_retired or not self.root.settings.pack(k).get('retired')]

    @snapshot
    def get_pack(self, pack_id):
        _id(pack_id, 'pack_id')
        return {'pack_id':pack_id, **self.root.settings.pack(pack_id)}

    @snapshot
    def get_config(self):
        value = self.root.settings.as_dict()
        # Other plugins may configure passwords/webhook tokens; expose only
        # global rules. A plugin already receives its own api.settings.
        value['plugins'].pop('settings', None)
        return value

    @snapshot
    def get_rankings(self, category='competitor', *, start=0, count=100):
        from fut_leaderboards import entries, GENERAL, SEASONS
        _page(start, count)
        if category not in GENERAL+SEASONS: raise ValueError('Unknown ranking category')
        rows = entries(self.root, SimpleNamespace(persona=self.root.profile['persona']), category, 'all', start, count)['entries']
        return [{'rank':r['rank'], 'persona_id':int(r['persona']), 'club_name':r['clubName'],
                 'score':r['score']['value'], 'badge_asset_id':r['badge']} for r in rows]
