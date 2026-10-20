# Managers, consumables and player statistics

The in-game store has a **Manager Pack** (ID 190, 1,000 coins) and a
**Training and Club Supplies Pack** (ID 191, 2,000 coins). Prices are live
`packs.prices` settings. Send pulled items to the club before selecting a
manager or using a consumable. Items, pending packs and balances persist per
club. Consumables can stack; duplicate copies of the same manager must be
resolved or quick sold. There are no automatic grants.

The manager catalogue contains 452 installed PS3 manager definitions, exported
as metadata without models/textures. Squad creation/saving, selection, retrieval
and quick-sell cleanup support an owned manager slot. Item subtype 4 reaches
the native manager constructor and local DB name lookup. Nationality and
contract metadata are supplied. Manager chemistry calculations/benefits and
faces need live-game verification; no new chemistry bonus is invented.

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
the resource-ID variant and consumable search routes are also handled.

After a completed match, played cards lose one contract, save reported fitness
and remove their temporary training. The selected manager loses one contract.
Unused players retain training and regain up to 15 saved fitness per completed
match resting. Injuries and bans continue to recover while resting. Zero
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
