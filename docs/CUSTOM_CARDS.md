# Custom player versions

Custom editions keep a footballer's normal card available and create a separate
version with its own rating, face attributes and native resource ID. The default
**Prime** pool contains Cristiano Ronaldo, Messi, Kane, Neymar, Neuer,
Lewandowski, De Bruyne, Hazard, Salah, Van Dijk, Mbappé and De Gea.

| Player | Prime rating |
|---|---:|
| Cristiano Ronaldo / Messi | 115 |
| Neymar | 112 |
| De Bruyne | 111 |
| Neuer / Lewandowski / Mbappé | 110 |
| Hazard | 109 |
| Kane / Salah / De Gea | 108 |
| Van Dijk | 107 |

In FUT's store, **Prime Star Player Pack** (ID 180) gives one Prime card for
50,000 coins, and **Prime Star Trio Pack** (ID 181) gives three distinct Prime
footballers for 125,000 coins. Their prices use `packs.prices` like other packs.
Each enabled footballer has an equal chance; repeated purchases can draw a
version you already own.

Normal packs keep stock cards by default. Set
`custom_cards.standard_pack_chance_percent` to 1–100 to allow eligible cards
drawn from normal gold packs to become custom versions. This is a per-eligible
player chance, not a guarantee per pack. A gold player without a configured
custom version stays normal. Several eligible editions are chosen uniformly.
Leave it at 0 for dedicated custom packs only.

## Live definitions

`server-config.json` accepts a `custom_cards` section. Changes reload live and
affect future pack draws/catalogue queries. Defaults remain unless overridden;
player entries merge by asset ID. Disable an unwanted entry with
`enabled: false`. An additional edition needs an unused version number.

```json
{
  "packs": {"prices": {"180": 50000, "181": 125000}},
  "custom_cards": {
    "enabled": true,
    "standard_pack_chance_percent": 0,
    "editions": {
      "prime": {
        "label": "Prime",
        "version": 1,
        "rareflag": 3,
        "discard_value": 5000,
        "players": {
          "20801": {"rating": 115, "attributes": [115, 115, 105, 115, 60, 108]},
          "167495": {"rating": 112, "attributes": [115, 112, 115, 115, 88, 114]}
        }
      },
      "flashback": {
        "label": "Flashback",
        "version": 2,
        "players": {"20801": {"rating": 102, "position": "RW"}}
      }
    }
  }
}
```

This overrides Ronaldo and Neuer while retaining the other default Prime stars.
Flashback can be drawn through mixed normal gold packs when that chance is
enabled; dedicated Prime packs select Prime only.

Edition names use lowercase letters/numbers/underscores, starting with a letter,
up to 24 characters; `standard` and `england110` are reserved. At most 32 editions
and 2,000 entries per edition are accepted. Versions are unique integers **1–127**.
Keep version numbers stable once cards have been issued. Each player must be an
existing stock asset ID; the plugin catalogue can find IDs. Prime OVR is
**102–115**; other custom editions allow 1–115. `rareflag` is 1 or 3, using native
item styles. `discard_value` is 0–1,000,000 coins per card.

`attributes` optionally supplies six integers from 0–115. Outfield order is
**pace, shooting, passing, dribbling, defending, physical**; goalkeeper order is
**diving, handling, kicking, reflexes, speed, positioning**. Without an override,
the server adds the OVR increase to stock face values, capped at 115, retaining
strengths and weaknesses. These values are local estimates, as with the normal
catalogue. `position` optionally uses existing position names such as ST/RW/GK.
Invalid definitions retain the last valid config; `--check-config` validates
while running.

## Ownership, squads and persistence

The original `assetId` identifies the footballer/model. Custom `resourceId` is
`(version << 24) | assetId`; the retail parser splits its version byte and
original player ID. Owned `id` is a separate club instance ID. Duplicate handling
uses the complete resource ID: normal and Prime Ronaldo can both be in the club,
while two identical Prime Ronaldos require resolving the duplicate. Legacy
England 110 and stock cards retain existing resource IDs and save compatibility.

Only **one version of a footballer** can be selected in a squad, including bench
and reserves. Different squads can use different versions. Conditions and
match reports stay attached to owned item IDs. Choosing a custom version
preserves coins, squads, receipts and other cards.

Issued cards save their version/rating/attributes/position/label snapshot.
Changing definitions affects future pulls; owned cards keep issued stats and
remain readable when disabled. Quick-sell uses the current edition value if its
version matches, otherwise the saved value. Disabling custom cards hides their
packs; a pack also hides when too few enabled players remain for its distinct
player count. Pending packs still resume without charging again.

The game gets names/portraits from its original player database. Custom labels
appear in server/plugin metadata and store descriptions. The native parser was
executed against the matching v1.02 binary for version IDs, rarity and every
rating from 102 to 115, including Neuer's GK attributes. This validates the card
data path; full desktop rendering/gameplay at 115 still needs testing and does
not establish that all underlying match skills exceed the engine's normal
limits. No game/model/texture changes are needed to send these versioned cards.
