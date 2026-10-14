# Custom player versions

Every one of the 17,656 stock players has a separate **Prime** version by
default. Prime overall and all six face attributes are **base +15**, including
goalkeeper attributes. For example, Ronaldo and Messi are 109 overall, Kane is
104 and Neuer is 105. The complete default Prime pool spans 62–109 overall.
Normal player cards remain available with their original ratings and IDs.

In FUT's store, **Prime Player Pack** (ID 180) gives one Prime for 50,000 coins,
and **Prime Trio Pack** (ID 181) gives three distinct Prime footballers for
125,000 coins. These packs draw uniformly from the entire enabled pool,
including Primes of bronze and silver players; they do not guarantee a star.
Prices use `packs.prices`. Repeated purchases can draw a version you already own.

Every standard **gold** pack also has a **0.001% chance per player card** to
upgrade that drawn player to Prime: precisely **1 in 100,000**. This includes
premium, rare, jumbo, ultimate, rating-restricted, nation, league and position
packs. The same footballer is upgraded, preserving the pack's eligibility rules.
A 12-player pack has approximately a 0.012% chance of at least one Prime.
Bronze and silver packs do not upgrade. These are local server odds, not EA odds.

## Live definitions

`custom_cards.standard_pack_chance_percent` accepts finite decimal percentages
from 0 to 100. Set 0 for dedicated custom packs only, or 100 for every eligible
gold card. This controls the combined custom-edition chance; if several enabled
editions cover that player, one is chosen uniformly after a successful roll.
Disabling a player/edition prevents new draws but keeps owned cards readable.

`server-config.json` changes reload live and affect future draws/catalogue
queries. The compact `all_players` rule covers the full stock catalogue without
listing thousands of IDs. `players` contains optional individual overrides;
entries merge by asset ID. An additional edition needs an unused version number.

```json
{
  "custom_cards": {
    "enabled": true,
    "standard_pack_chance_percent": 0.001,
    "editions": {
      "prime": {
        "enabled": true,
        "label": "Prime",
        "version": 1,
        "rareflag": 3,
        "discard_value": 5000,
        "all_players": true,
        "rating_boost": 15,
        "attribute_boost": 15,
        "players": {}
      }
    }
  }
}
```

An override such as `"players": {"20801": {"enabled": false}}` excludes Ronaldo
from new draws. Set `all_players: false` to use only listed players. An entry
such as `"167495": {"rating": 107, "attributes": [110, 105, 108, 110, 80, 108]}`
replaces Neuer's generated stats. Overrides take precedence over edition boosts.

Edition names use lowercase letters/numbers/underscores, starting with a letter,
up to 24 characters; `standard` and `england110` are reserved. At most 32 editions
and 2,000 explicit overrides per edition are accepted. `all_players` covers the
whole catalogue independently of that override limit. Versions are unique
integers **1–127**; keep them stable once cards have been issued. Player IDs must
exist in the stock catalogue. OVR is **1–115**, `rareflag` is 1 or 3, and
`discard_value` is 0–1,000,000 coins per card.

`rating_boost` and `attribute_boost` accept integers from 0–115; generated stats
are capped at 115. Other editions default to `all_players: false`,
`rating_boost: 0`, and `attribute_boost: null`. A null attribute boost uses the
positive difference between that card's OVR and its base OVR, retaining the
previous behavior for individually configured editions.

`attributes` optionally supplies six integers from 0–115. Outfield order is
**pace, shooting, passing, dribbling, defending, physical**; goalkeeper order is
**diving, handling, kicking, reflexes, speed, positioning**. These are the server's
locally estimated face values, as in the normal catalogue. This does not establish
that every underlying engine match skill exceeds the game's normal limits.
`position` optionally overrides the stock position (ST/RW/GK, etc.). Invalid
config edits retain the last valid rules; `--check-config` validates while running.

## Ownership, squads and persistence

The original `assetId` identifies the footballer/model. Custom `resourceId` is
`(version << 24) | assetId`; the retail parser splits its version byte and
original player ID. Owned `id` is a separate club instance ID. Duplicate handling
uses the complete resource ID: normal and Prime Ronaldo can both be in the club,
while two identical Prime Ronaldos require resolving the duplicate. Legacy
England 110 and stock cards retain existing resource IDs and save compatibility.

Only **one version of a footballer** can be selected in a squad, including bench
and reserves. Different squads can use different versions. Conditions and match
reports stay attached to owned item IDs. Choosing a custom version preserves
coins, squads, receipts and other cards.

Issued cards save their version/rating/attributes/position/label snapshot.
Later config edits affect future pulls; owned cards keep issued stats. Quick-sell
uses the current edition value if its version matches, otherwise the saved value.
Disabling custom cards hides their packs; a pack also hides when too few enabled
players remain for its distinct player count. Pending packs resume without
charging again. The game gets names/portraits from its original player database.
Custom labels appear in server/plugin metadata and store descriptions.

## Updating already-owned Primes to +15

Existing installations may still have the former selected-star rating overrides
in their config. Replace `custom_cards` with the compact example above to enable
the new rules for future draws. Other server settings can remain as they are.

To explicitly rebalance already-owned Primes, **stop the server** first, then run:

```bash
python3 rebalance_primes.py --state-dir /path/to/state
python3 rebalance_primes.py --state-dir /path/to/state --apply
```

The first command previews the count. The second backs up the entire stopped
state into a timestamped sibling `state-backups` directory, then sets only owned
Prime OVR and six attributes to stock +15. It includes unassigned pending-pack
cards and per-user inventories, preserves IDs, coins, squads, conditions and
receipts, and leaves historical backups untouched. It refuses an active server
lock and is safe to repeat; no automatic rebalance runs on ordinary startup.
Restart the server and re-enter FUT to refresh cached cards.
