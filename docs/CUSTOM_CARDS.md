# Custom player editions

Every stock footballer has separate standard, Prime/TOTW, Team of the Year
and Ones to Watch versions. These are community server rules, not EA's
historical special-card selections or dynamic Ones to Watch upgrades.

| Edition | OVR and all six attributes | Chance per eligible gold player | Version / rarity flag |
|---|---|---|---|
| Prime / Team of the Week (`prime`) | Base +10 | 1 in 400 (0.25%) | 1 / 3 |
| Team of the Year (`toty`) | Base +20 | 1 in 2,000 (0.05%) | 2 / 5 |
| Ones to Watch (`otw`) | Base +25 | 1 in 10,000 (0.01%) | 3 / 21 |

For example, standard Ronaldo is 94 OVR; his versions are 104, 114 and 119.
All six outfield or goalkeeper attributes receive the edition's boost.
Normal cards remain available. One mutually exclusive roll preserves each
edition's exact unconditional chance; total special chance is 0.31% per card.
A disabled or excluded player's tickets become ordinary cards, with no reroll
or redistribution. Bronze and silver packs do not roll special editions.
Nation, league, position and rating-restricted gold packs upgrade the same
footballer selected by their base rules.

Prime packs 180/181 are retired from the in-game store and reject new purchases.
Already-bought pending packs still resume and can be moved or quick sold.
The web builder keeps guaranteed Prime packs and also supports TOTY/OTW packs;
its minimum-OVR filter determines eligibility, with no implicit 95+ guarantee.

## Live configuration

The defaults below can be edited in `server-config.json`:

```json
{
  "custom_cards": {
    "enabled": true,
    "standard_pack_chance_percent": null,
    "editions": {
      "prime": {"version": 1, "rareflag": 3, "one_in": 400, "rating_boost": 10, "attribute_boost": 10},
      "toty": {"version": 2, "rareflag": 5, "one_in": 2000, "rating_boost": 20, "attribute_boost": 20},
      "otw": {"version": 3, "rareflag": 21, "one_in": 10000, "rating_boost": 25, "attribute_boost": 25}
    }
  }
}
```

`one_in` is an integer denominator from 1 to 1,000,000,000; zero disables that
edition's ordinary-gold-pack drops while keeping dedicated web purchases.
Enabled chances must sum to at most 100%. Leave
`standard_pack_chance_percent: null` for independent per-edition odds. An
explicit legacy percentage from 0 to 100 retains the former combined-chance,
uniform-edition behavior; old configs must clear it to use `one_in`.

Each edition also accepts `enabled`, `label`, `discard_value`, `all_players`
and `players`. Defaults cover every player. Set `all_players: false` for only
explicitly listed players. `players` merges optional overrides by asset ID:

```json
{"custom_cards":{"editions":{"prime":{"players":{
  "20801":{"enabled":false},
  "167495":{"rating":120,"attributes":[125,120,125,120,90,120],"position":"GK"}
}}}}}
```

Overrides take precedence over edition boosts. Six-attribute order is
PAC/SHO/PAS/DRI/DEF/PHY for outfield players and DIV/HAN/KIC/REF/SPD/POS for
keepers. Face values originate from local weighted stock-DB summaries.

## Numeric storage

The artificial 115 limit is removed. The game's OVR field is an unsigned byte:
configured OVR accepts 1–255 and generated OVR saturates at 255. Its six indexed
attribute fields accept unsigned 32-bit values (0–4,294,967,295); generated
attributes only saturate at that storage boundary. Boosts accept 0–4,294,967,295.
A null `attribute_boost` uses the positive final-OVR difference from the base.
These field widths do not prove that extreme stats render correctly or affect
every underlying match skill. PS3 parsing is verified separately from gameplay.

Rarity flags accept 0–255; flags 3/5/21 select the requested special styles.
Actual artwork depends on the installed PS3 assets and needs in-game testing.
Edition names begin with a lowercase letter and use letters/numbers/underscores,
up to 24 characters; `standard` and `england110` are reserved. At most 32 editions
and 2,000 explicit overrides per edition are accepted; `all_players` is independent
of that override limit. Versions are unique integers 1–127. Keep issued version
IDs stable and choose unused version IDs when upgrading older custom configs.
Discard values accept 0–1,000,000 coins.

## Owned cards, squads and persistence

Valid edits update already-owned cards in every club, including offline users
and pending packs. Startup reconciliation applies edits made while stopped.
Disabled/excluded owned cards still receive the edition's new stats. Removing
an edition entirely preserves its last saved snapshot. Stats, position, label,
rarity and discard values refresh; owned IDs, issued version/resource IDs,
coins, squads, training, fitness, injuries, bans and match totals are preserved.
An active match keeps its captured squad. Reopen the squad menu or re-enter FUT
to refresh cached values; no game restart is required for rule edits.

Before changes, the server validates all inventories, backs up affected files
to `state-backups/custom-cards-<timestamp>/`, stages updates under the shared
backend lock and rolls back failed writes. Repeating identical rules is a no-op.
The `custom_cards.updated` event records affected card/club counts and backup.
The same startup pass recovers available historical player statistics.

Player `assetId` identifies the original footballer. Custom resource ID is
`(version << 24) | assetId`; an owned `id` identifies that club's instance.
Versions coexist in the club, but only one version of a footballer can be
selected in a squad, including bench/reserves. Exact duplicates need resolving.
The game's original database supplies names/portraits. Custom edition labels
also appear in web/plugin metadata; no player models or textures are supplied.

The old `rebalance_primes.py` command is retained for stopped-state recovery:
it forcibly sets existing Prime OVR/attributes to base +15, independently of
config. Ordinary startup reapplies the configured rules, so use live config
edits for routine rebalancing instead.
