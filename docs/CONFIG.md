# Server rules

Edit `/path/to/fifa19-ps3-fut/server-config.json`. The
operator creates it from the supplied template and preserves it on later updates. The
template is `data/server-config.example.json`. This is JSON: no comments or
trailing commas. Missing settings use defaults; unknown keys, duplicate keys
and invalid values are rejected. The running server watches this file and
reloads a stable, valid edit automatically, usually within **one second**.
No server restart or FIFA restart is needed for rule changes.

All rule sections swap together under the backend lock. Existing logged-in
clubs, coins, cards, squads, searches and matches are retained. Partial writes,
invalid JSON, unknown/duplicate keys, unreadable/deleted files and files over
256 KiB leave the last valid rules active. A later valid save recovers automatically.
Omitted fields still mean defaults, so preserve other settings when editing.

You can validate the config without stopping the running server:

```bash
python3 server.py --check-config --config /path/to/fifa19-ps3-fut/server-config.json
```

`--check-config` does not touch saves, load TLS or execute plugins.
The broader `--check` still requires stopping the server because it validates
authoritative saves/TLS/plugin syntax as well. Server code and TLS/address
changes require a restart between matches. Standalone Python plugin source
files reload automatically; see [PLUGINS.md](PLUGINS.md).

The event log records `config.reloaded` with a generation and changed sections,
or `config.error` (including line/column for JSON syntax errors), without
logging config values. `/fut-local/health` exposes a small `config` status:
`watching`, `generation`, `updated_at`, `last_error_type`. Generation resets
on server restart. Formatting-only edits do not advance it.

FIFA can cache prices, descriptions and card values already on screen. Close
and reopen the relevant FUT menu to request fresh data; if a label remains
cached, leave and re-enter FUT. Server purchases, quick sells and match rewards
use the latest accepted rules even while an old menu is visible. Automatic
reload does not push unsolicited HUD/menu packets or change match gameplay.

Custom-card stat edits also update already-owned cards in every saved club,
including offline users and pending packs. Affected inventory files are backed
up under `state-backups/custom-cards-<timestamp>/` before applying the edit.
The server also reconciles cards on startup for edits made while stopped.
Issued card IDs, coins, squads, conditions and historical receipts are preserved;
active matches keep their captured squads. See [CUSTOM_CARDS.md](CUSTOM_CARDS.md).

| Setting | Default | Meaning |
|---|---:|---|
| `economy.starting_coins` | 20000 | Initial balance of a newly created inventory; existing balances are preserved |
| `economy.win_coins` | 1000 | Base full-time win reward |
| `economy.cpu_division_bonus` | 500 | Extra win coins for each CPU division above displayed Division 10 |
| `economy.draw_percent` | 75 | Draw reward as a percentage of the win base |
| `economy.loss_percent` | 50 | Loss reward as a percentage of the win base; cannot exceed draw percentage |
| `economy.quick_sell` | tier pairs | Common/rare quick-sell values for bronze, silver and gold |
| `match_bonuses.enabled` | true | Add the performance breakdown below to completed match rewards |
| `packs.prices` | per-pack map | Coin prices keyed by string pack ID; applies to offers, purchases and descriptions |
| `contracts.player_price` | 200 | Coin fallback for +28 player contracts when the club opted into auto renewal and has no owned player contract card |
| `contracts.manager_price` | 200 | Coin fallback for +28 selected-manager contracts; owned manager contract cards are used first |
| `packs.gold_rare_weights` | `[30,30,30,10]` | Positive weights for rating bands 80–82, 83–84, 85–89, 90–99 |
| `points.win`, `.draw`, `.loss` | 3, 1, 0 | Local leaderboard points per completed result |
| `cpu_seasons.hold` | `[5,6,7,8,9,10,11,12,13,14]` | Points needed to avoid relegation after ten CPU fixtures |
| `cpu_seasons.promote` | `[12,13,14,15,16,17,18,19,20,21]` | Points needed for CPU promotion after ten fixtures |
| `cpu_seasons.difficulties` | Amateur through World Class | Ten named difficulties, ordered from displayed Division 10 to 1 |
| `cpu_seasons.opponents` | ten curated fixture lists | Ten installed team IDs per division, ordered from displayed Division 10 to 1 |
| `matchmaking.search_seconds` | 20 | Server search expiry, capped by the client's requested duration |
| `matchmaking.peer_timeout_seconds` | 180 | Inactivity timeout for paired games, refreshed by valid peer traffic |
| `plugins.enabled` | true | Live enable/disable of custom plugin hooks |
| `plugins.disabled` | `[]` | Plugin filenames without `.py` whose hooks are disabled |
| `plugins.settings` | `{}` | Current per-plugin JSON objects read through `api.settings` |
| `custom_cards.enabled` | true | Enable custom card draws and packs |
| `custom_cards.standard_pack_chance_percent` | null | Use per-edition odds; a numeric value explicitly selects legacy combined-percentage mode |
| `custom_cards.editions` | Prime/TOTW +10, TOTY +20, OTW +25 | Version IDs, catalogue coverage, stat boosts, player overrides and custom quick-sell prices; stat edits also refresh owned cards |

Each edition has `one_in` (400 / 2000 / 10000 by default). See [CUSTOM_CARDS.md](CUSTOM_CARDS.md) for the exact roll and native numeric bounds. Manager/supplies/contracts pack prices use keys 190/191/192 in `packs.prices`. See [ITEMS.md](ITEMS.md) for fitness, training, automatic renewal and player totals.

Division arrays run from **displayed Division 10 to Division 1**. The wire
division numbers run in the opposite direction: wire 1 is displayed Division
10. Targets must be 0–30, with hold below promotion in each division. The
game's native season counters remain **three points per win, one per draw**;
the `points` settings affect leaderboards, not those compressed client season
counters. Online promotion/relegation is still unimplemented.

The default single-player opponent curve uses team ratings checked against the
v1.02 database. Fixture order rotates each completed season; there are ten
matches per division, with some repeat opponents at the top.

| Displayed division | Opponents (team OVR) | Difficulty |
|---|---|---|
| 10 | Very weak clubs, 54–60; Bray Wanderers, Limerick, Crewe | Amateur |
| 9 | Lower-league clubs, 61–63 | Amateur |
| 8 | Stronger lower-league clubs, 64–65 | Amateur |
| 7 | Lower Championship and similar clubs, 67–69 | Semi-Pro |
| 6 | Strong Championship and similar clubs, 71–73 | Semi-Pro |
| 5 | Mid-table top-flight clubs, 75–77 | Semi-Pro |
| 4 | Strong clubs, 79–81 | Professional |
| 3 | Elite clubs and international teams, 80–83 | Professional |
| 2 | International teams, 80–85 | World Class |
| 1 | Strong international teams, 82–86; Portugal, Italy, Argentina, Belgium, France, Germany, Spain | World Class |

FIFA lists Beginner first, so Amateur is its second setting. Difficulty names
accepted in config are `beginner`, `amateur`, `semi_pro`, `professional`,
`world_class`, `legendary` and `ultimate` (native values 0–6). Each difficulty
array needs ten entries; each opponents array needs ten lists of ten positive
team IDs. Opponents must exist in the client's installed database. Boosted
England is excluded from the default curve because the earlier England 110
database mod changes its strength. Team/player database mods can change how
strong an opponent actually plays.

Opponent/difficulty edits reload for the next fetched season list, preserving
played games, points, cards and rewards. An already loaded match keeps its
client-selected opponent and difficulty; leave and reopen CPU Seasons to
refresh cached fixtures. Promotion/relegation thresholds and reward amounts
are independent of this opponent curve.

Gold-rare weights are relative chances across available bands. Restricted
packs renormalize them, then choose a player within the band. A higher top-band
weight improves elite odds without guaranteeing Messi or Ronaldo. Pack sizes,
rarity guarantees, eligible nations/leagues and stock ratings remain defined
in `fut_packs.py`; prices and odds need no code edits. England 110 stays retired.

Reloaded rewards apply when a result is first committed, including a match
already underway. New rewards snapshot their policy and plugin adjustment in the match receipt.
Changing rules never recalculates paid coins or breaks saved receipts. Retried
reports return the original award. Quit/DNF/no-contest pays zero, including
when reward plugins are enabled. Leaderboards intentionally recompute all-time
scores using the current point weights and club values using current quick-sell values.

Starting coins only affect newly created inventories; existing balances never
reset. An existing search retains its assigned deadline; new searches use the
new search duration. Peer timeouts use the new value on their next refresh.

Plugin settings/toggles reload live. Newly enabled, previously unloaded files
register once; disabling/re-enabling a loaded plugin keeps its registrations
and in-memory state without duplicating hooks. Source edits replace the plugin's
module and hooks automatically after a short debounce. Broken edits keep the
last working version. A plugin which caches a setting during registration
must read `api.settings` in its callbacks or handle `server.config_changed`.
See [PLUGINS.md](PLUGINS.md).
Custom definitions affect new cards; issued custom cards preserve their stat
snapshots. See [CUSTOM_CARDS.md](CUSTOM_CARDS.md) for Prime packs and version IDs.

An example partial configuration (all omitted rules keep defaults):

```json
{
  "economy": {"starting_coins": 20000, "win_coins": 1500},
  "packs": {"prices": {"6": 6000, "164": 40000}},
  "points": {"win": 5, "draw": 2, "loss": 0},
  "plugins": {"settings": {"win_bonus": {"bonus_coins": 300}}}
}
```

## Match performance rewards

Completed WIN/DRAW/LOSS reports receive performance coins in addition to the
normal result/division reward. The FIFA results screen uses its native rows
for each statistic. Both players in an online match earn their own breakdown;
CPU seasons and tournaments use the same rules. Cup prizes remain separate.

Each `match_bonuses` entry has `coins` per unit and a maximum counted `cap`.
For example, `"goals": {"coins": 100, "cap": 5}` pays up to 500 goal coins.
Settings reload live and apply when the next result is first committed.

| Entry | Coins per unit | Cap |
|---|---:|---:|
| `goals` | 100 | 5 |
| `shots_on_target` | 5 | 10 |
| `successful_tackles` | 1 | 25 |
| `corners` | 5 | 10 |
| `clean_sheet` | 150 | 1 |
| `passing_percentage` | 1 per percentage point | 80 |
| `possession_percentage` | 1 per percentage point | 60 |
| `man_of_the_match` | 15 | 1 |
| `goals_against` | −20 | 3 |
| `fouls` | −5 | 5 |
| `yellow_cards` | −10 | 5 |
| `red_cards` | −40 | 5 |
| `offsides` | −1 | 10 |

Fewer fouls/cards/offsides retain more of the bonus. Deductions have a zero
floor on the combined performance bonus and do not reduce the normal result
reward. A clean sheet means the validated opponent score is zero, including
a 0–0 draw. Goal counts come from the final score, without adding penalty goals
or own goals again. Other values come from the client's validated match report;
the server does not independently simulate the match. These are local rules,
not a claim to reproduce EA's historical reward formula.

For example, a basic 2–0 win with one foul and all other reported statistics at
zero pays **1,345 coins**: 1,000 completion + 200 goals + 150 clean sheet − 5
foul. Passing, possession and other reported stats can increase this further.
The native screen shows completion separately from the skill/performance total.

Set `match_bonuses.enabled` to `false` to keep only the existing result/division
pay. Setting an individual rate or cap to zero disables that contribution.
Positive categories accept nonnegative rates; deduction categories accept
nonpositive rates. Rates are limited to 10,000 coins in magnitude. Caps are
0–1 for clean sheet/man of the match, 0–100 for percentages, and 0–1,000 for
other counters; configured positive bonuses cannot exceed 1,000,000 per match.

New receipts save the complete bonus policy and native breakdown. Retries,
server restarts and later rule changes return the original paid amount.
Existing historical receipts and balances are unchanged. Abandoned, DNF and
no-contest reports earn no new performance reward. Reward plugins receive the
combined result and performance amount; tournament prizes are added afterward.

## Custom web packs

The `web_packs` section controls live pack-builder prices. See [WEB.md](WEB.md)
for pricing formulas, authentication, the web interface and operator commands.

## Fitness recovery

`fitness.enabled` defaults to `true`; `fitness.seconds_per_point` defaults to
`300` (+1 every five real minutes). It accepts 1–86400 seconds and reloads live.
Recovery includes offline time and stops at 99. The current interval applies to
elapsed time since the card's saved fitness timestamp. Injuries and suspensions
remain match-based. Existing tired cards initialize their clock from their last
recorded completed appearance, or first authenticated access if no receipt is
available. Inventory reads do not require a background recovery job.
