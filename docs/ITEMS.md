# Managers, consumables and player statistics

The in-game store has a **Manager Pack** (ID 190, 1,000 coins), a
**Training and Club Supplies Pack** (ID 191, 2,000 coins), and a
**Contracts Pack** (ID 192, 2,000 coins: ten player and two manager cards,
each adding 28 matches). **Position Change Pack** (ID 193, 1,500 coins) and
**Chemistry Style Pack** (ID 194, 2,500 coins) each contain six different
cards randomly selected from their respective catalogues. Prices are live
`packs.prices` settings. Send pulled items to the club before selecting a
manager or using a consumable. Items, pending packs and balances persist per
club. Consumables can stack; duplicate copies of the same manager must be
resolved or quick sold. There are no automatic grants.

The manager catalogue contains 452 installed PS3 manager definitions, exported
as metadata without models/textures. Squad creation/saving, selection, retrieval
and quick-sell cleanup support an owned manager slot. Item subtype 4 reaches
the native manager constructor and local DB name lookup. Nationality and
contract metadata are supplied. Default leagues are supplied for 328 managers
matched to clubs in the installed FIFA 19 database, including Guardiola,
Klopp and Mourinho. Existing owned cards use these defaults without changing
their IDs or contracts; an explicitly saved league override takes precedence.
Unmatched managers retain no league. Manager chemistry calculations/benefits and
faces need live-game verification; no new chemistry bonus is invented.
Managers with no assigned league display **No league** (or **None** on a
collapsed card). The public FUT text response supplies the missing league-zero
labels instead of letting FIFA show internal localization keys.

| Supplies item | Server effect |
|---|---|
| Outfield training | +10 to six face attributes until that player completes a match |
| Goalkeeper training | +10 DIV/HAN/KIC/REF/SPD/POS until that keeper completes a match |
| Player contracts | +28 contracts, up to the native 99 limit |
| Manager contracts | +28 contracts, up to 99 |
| Player fitness | +60 fitness, up to 99 |
| Squad fitness | +30 fitness for requested active-squad targets, up to 99 |
| Healing | Remove up to five injury games; suspensions remain separate |

Each supplies pack contains all seven items. Training cannot be stacked on a
player who already has training. Outfield/GK compatibility is checked. Full
fitness/contracts and uninjured targets reject the application without using
the card. Consumables require club ownership and can only be applied between
matches. Applying is one atomic inventory commit: failed saves cannot consume
the card or change the target. Native ApplyCard requests use
`PUT /ut/game/fifa19/item/<consumed-id>` with `{"apply":[{"id":<target-id>}]}`;
the resource-ID variant and consumable search routes are also handled. Retail
stack applications use `POST /ut/game/fifa19/item/resource/<resource-id>` with
the same body; the earlier PUT compatibility form remains supported.

Successful apply responses include both the updated targets and a definition
snapshot of the consumed supply in `itemData`. FIFA's success callback looks
up that supply's subtype after invalidating its menu caches; omitting it can
crash the frontend after the server has already saved the application. The
snapshot does not restore an owned item: saves and subsequent club/stack
searches exclude the consumed copy, including the last copy of a stack.

The native staff/consumable menus read `GET /club/stats/staff` and
`GET /club/stats/consumables` below `/ut/game/fifa19`. Consumable counts include
club items only, including every copy in a stack. `GET /club/consumables/<type>`
returns `itemData` stack rows with `item`, `count` and `untradeableCount`.
Retail's apply screen requests `/club/consumables/development` for all owned
supplies, then filters compatible items for the selected player or manager;
the ordinary club search continues to return individual cards. Staff bonus
responses report zero additional contract/morale multipliers; supplies grant
the fixed amounts above.

Saved support definition IDs remain stable. Card responses use installed PS3
art resources 5000000/5000001 (training), 5000007/5000008 (contracts), 5000009
(healing), and 5000010/5000011 (fitness). Contracts populate the separate
`bronze`, `silver` and `gold` amounts used by native card display/eligibility.
The resource apply route accepts the installed resource and the previous
6000000-series definition alias, so existing owned supplies need no migration.
Retail wire subtype 67 identifies all-attribute outfield training; subtype 57
identifies goalkeeper training. Earlier server versions reversed those types,
causing FIFA's labels/eligibility and the server's target checks to disagree.
The correction also applies to previously pulled cards and attached training.

## Automatic contracts

Sign into the club website and use **Automatically renew expired contracts**
under Club settings. This is a per-club preference, disabled by default.
At zero remaining contracts, active XI/bench players and the selected manager
use an owned compatible contract card first. If none is available, renewal
costs **200 coins for +28 matches** by default. Insufficient funds leave the
card unchanged. Purchased/unassigned supplies and reserve players outside
the match squad are not charged or consumed.

Renewal happens between matches when reading the active squad, when enabling
the switch, or atomically with a completed result after its reward is added.
Nothing is renewed during gameplay. Save failures and result retries cannot
charge twice. The website shows the current prices and latest renewal total.
Operators can change `contracts.player_price` and `contracts.manager_price`
live. Changing a price does not enable the feature for a club.

## Position changes and chemistry

Send the new pack contents to your club, select a player in the squad and
choose **Apply Consumable**. The Positioning tab contains position modifiers;
the chemistry-style tabs distinguish outfield players from goalkeepers.
The printed starting position must match the card's current preferred
position. These are permanent changes to that owned card, across all squads
using it; they do not change the base player or other versions/copies.

The PS3 catalogue supports both directions for LB/LWB, RB/RWB, LW/LM,
RW/RM, LF/LW, RF/RW, CAM/CM, CM/CDM, CF/CAM and ST/CF (20 cards).
For example, ST → CF → CAM → CM → CDM requires four separate cards.
There is no goalkeeper conversion or arbitrary jump between positions.
The website's admin editor can still set a position directly. Resetting admin
overrides leaves paid position conversions intact.

Outfield styles are Basic, Sniper, Finisher, Deadeye, Marksman, Hawk, Artist,
Architect, Powerhouse, Maestro, Engine, Sentinel, Guardian, Gladiator,
Backbone, Anchor, Hunter, Catalyst and Shadow. Goalkeeper styles are Wall,
Shield, Cat, Glove and Goalkeeper Basic. A new style replaces the previous
one and persists after matches; Basic restores the default. Incompatible
styles and reapplying the current style reject without consuming a card.
Temporary attribute training remains separate and expires after a played
match. Position changes preserve the version, OVR, attributes and condition.

FIFA calculates links and squad/player chemistry natively using the current
positions, club, league, nationality, manager and loyalty. Captured squad
saves already contain client-calculated chemistry; the server does not
replace this with a simplified formula. Chemistry styles use the retail
`playStyle` IDs 250–273, with 273 as the correct goalkeeper default. The
game applies their chemistry-dependent effects; the server does not add a
second flat bonus to face attributes. Live match effects still need in-game
verification. Pack-issued players receive first-owner loyalty immediately.
Cards explicitly marked as transferred (`firstOwner: false`) earn loyalty
after ten recorded appearances; unused substitutes and no-contest matches
do not count. Existing directly issued cards keep their IDs and saves.

Position/style items use unique 6000000-series resources so stack applications
select the exact modifier. The native bio renderer chooses installed position
art 5000013 and style art 5000015_4 from the subtype. Chemistry cards send
the native all-tier compatibility value (`amount: 4`); zero would hide them
from the apply menu. No new textures or client
patches are required. These types use retail subtypes 91–110 and 250–273;
71–86 are old formation modifiers and must not be substituted.

## Fitness and match conditions

After a completed match, played cards lose one contract, save reported fitness
and remove their temporary training. The selected manager loses one contract.
Fitness recovers by **one point every five real minutes**, including time
offline, up to 99. The live `fitness.enabled` and `fitness.seconds_per_point`
settings control this. Recovery is calculated from a saved timestamp when cards
are read; no five-minute timer needs to keep running. A match report or fitness
consumable resets the timestamp. Resting for another completed match no longer
gives a flat fitness bonus. Injuries and bans still recover through matches
resting; time alone does not remove them. Unused players retain training. Zero
contracts are reported to FIFA; native match-menu eligibility needs testing.

## Appearances, goals, assists and discipline

Owned cards save `statistics` with `gamesPlayed`, `goals`, `assists`,
`yellowCards` and `redCards`. Only players recorded as having entered the match
gain an appearance; an unused substitute does not. Report retries return the
saved receipt and cannot count another appearance, goal, contract or reward.
No-contest reports do not change these totals.

FUT card responses populate indexed `statsList` and `lifetimeStats` fields.
The current red-card notification remains tied to the outstanding suspension;
historical red cards remain in lifetime statistics after a ban is served.
Item conditions/totals are independent for each owned edition and survive
live Prime-stat changes, squad switches and server restarts.

Startup recovers available totals from saved match history and backs up changed
inventories using the existing reconciliation mechanism. Older reports did not
save which bench players entered a match; their appearances cannot always be
reconstructed. Recorded goals/assists/cards are recovered where available.
Future receipts explicitly save `playedItems` so this ambiguity is avoided.

The backend and relevant native field/ApplyCard writers have automated checks.
The live FIFA manager/search/apply menus and the match engine's training effect
still need user testing. Capture a failed menu action before changing item
IDs or types; missing engine assets cannot be supplied by the server alone.

Goalkeeper training (resource 5000001, goalkeeper artwork) applies to keepers.
Outfield training (5000000, footballers artwork) applies to outfield players.
Using the wrong type rejects the request without consuming either card.
For example, Neuer needs the goalkeeper training card; Messi needs the
outfield training card. Both are included in every Training and Club Supplies
Pack. FIFA may display its generic communication error for a rejected target;
the server log records the compatibility reason. Training is temporary and
expires after the trained player completes a match.
The native contracts list uses the plural `/club/consumables/contracts` route.
