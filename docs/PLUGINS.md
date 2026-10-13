# Python event plugins

The current plugin API is **version 2** (`api.api_version == 2`). Existing
event handlers and `match.reward` filters continue to work. Version 2 adds
player presence and structured queries for live and offline saved clubs.

Place standalone `.py` files in `/path/to/fifa19-ps3-fut/plugins` on
your server. Names must start with a letter and contain only letters, numbers
and underscores (up to 64 characters). Files beginning with `_` are ignored.
Plugins load in filename order at startup and survive code deployments.
The server watches this directory and automatically loads added files, replaces
changed plugins and unloads deleted files after about one second of stable content.
Use trusted code: plugins run as the server's OS user with normal Python
access to files and network. The API gives domain events instead of raw
HTTP, Blaze packets, passwords or session tokens.

Each file exports `register(api)`. Subscribe with `api.on(event, callback)`
or its decorator form. Handlers receive an independent copy of a dictionary;
mutating it does not change server state or another handler's payload.
Exceptions are logged as `plugin.error`, isolated from the match response,
and do not undo a successful save. A registration failure removes all hooks
that plugin registered. During live replacement, a failed registration or syntax
error retains the previous working module and its hooks. Syntax errors fail
preflight; `--check` compiles
plugins without executing their code. Runtime registration errors skip the
affected plugin and are visible in the capture log.

```python
def register(api):
    @api.on("match.completed")
    def completed(event):
        api.log(f"{event['player']['club_name']} {event['result']}: "
                f"{event['coins']} coins")

    @api.filter("match.reward")
    def reward(event, coins):
        bonus = api.settings.get("bonus_coins", 250)
        return coins + bonus if event["result"] == "WIN" else coins
```

The bundled `_example_bonus.py` is disabled by its name. Copy it to
`win_bonus.py`, set `plugins.settings.win_bonus.bonus_coins` in the JSON config,
and it will load automatically. Its bonus is read in the reward
callback, so later JSON changes apply live. Set `plugins.disabled` to `["win_bonus"]` or
`plugins.enabled` to `false` to disable hooks live. Removing a plugin does not remove
its stored data or change previously paid rewards.

Every event includes `event`. A `player` snapshot contains `persona_id`,
`name`, `club_name`, `online`, `in_fut`, `session_count`, `connected_at`,
`last_login` and `last_seen`. `name` is the latest authenticated RPCN display
name, with the saved local name as fallback. The saved local profile name is
preserved. `connected_at` is null while offline, `last_login` survives restart,
and `last_seen` is an in-memory timestamp reset on server restart.

| Event | Additional fields | Timing |
|---|---|---|
| `server.started` | `advertise`, `plugins` | Listeners started |
| `server.stopping`, `server.stopped` | none | Graceful stop begins / listeners stop |
| `server.config_changed` | `generation`, `changed_sections` | Valid rules applied live; only top-level section names, no config values |
| `player.created` | `player` | A new RPCN-to-club registration is committed |
| `player.online` | `player`, `reason` | Successful Blaze post-authentication; repeated PostAuth does not repeat the event |
| `player.offline` | `player`, `reason` | Logout, disconnect, relogin replacement or graceful server shutdown |
| `player.fut_entered` | `player` | A new FUT session is issued |
| `player.fut_left` | `player`, `reason` | FUT logout or its underlying connection closes |
| `player.login` | `player` | A new FUT SID is issued; no SID is exposed |
| `player.logout` | `player`, `reason` | Compatibility companion to `player.login`, emitted alongside `player.fut_left` |
| `pack.opened` | `player`, `pack_id`, `purchase_id`, `price`, `cards` | New pack saved; cards contain `item_id`, `asset_id`, `resource_id` and `edition` |
| `cards.moved` | `player`, `items` | Club moves saved; per-item success is included |
| `cards.discarded` | `player`, `item_ids`, `coins` | Quick sell committed |
| `squad.created`, `squad.saved`, `squad.activated` | `player`, `squad_id`, `name` | Squad change committed |
| `squad.deleted` | `player`, `squad_id` | Deletion committed |
| `equipment.changed` | `player`, `slot`, `item_id`, `asset_id` | Kit/badge/ball/stadium choice committed |
| `match.reserved` | match fields below | New CPU/online reservation |
| `match.paired` | `match_id`, `game_id`, `players` | One game created for two players |
| `match.started` | match fields below | First successful PlayGame for this participant |
| `match.completed` | match fields, `result`, `score`, `coins`, `balance` | Participant's result and coins committed |
| `season.completed` | `player`, `mode`, `division_id`, `next_division_id`, `points`, `played` | Ten-game season archived |

Match fields: `player`, `match_id`, `game_id`, `mode` (`OFFLINE`/`ONLINE`),
`season_id`, `division_id`, `squad_id`. An unpaired online reservation has
null match/game IDs; use `match.paired` for its eventual identity. Score is
`{for, against}`. Online completion runs once for each participant, with
their own result, score and reward.

`match.reward` is the supported modifying hook. Signature:
`callback(event, coins) -> int`. Its event has the same match identity,
result and score fields. Filters run in filename/registration order, passing
the previous filter's amount to the next. Invalid returns or exceptions keep
the previous amount. Results must be integer coins from 0 to 100,000,000.
The final amount is saved atomically with the match result and ban/injury
updates. This hook is called only for completed WIN/DRAW/LOSS results.
There is no API to bypass card ownership or edit raw protocol replies.

Hooks run synchronously under server locks, with callbacks serialized across
players. Keep them quick: avoid sleeps, slow network calls and recursive game
operations. Reward filters must be pure; failed disk writes can cause a filter
to run again on retry. Use `match.completed` for notifications and file writes.
Completed-result and new-pack retries do not fire their commit events twice
within a running server. Other edit requests can legitimately repeat. Events
are not a durable queue: a process crash between commit and callback can lose
a notification. Use `match_id` or `purchase_id` to deduplicate external effects.

`api.data_dir` is a private persistent path at
`/path/to/fifa19-ps3-fut/plugin-data/<plugin_name>`; it is separate from
club saves and remains on updates. Plugins own the format and atomic writes
of their files. Each `api.settings` read returns a copy of the plugin's current
JSON settings, including live edits. Read it inside callbacks when a value
should change without restart. A value cached during registration does not
update itself; `server.config_changed` can rebuild a plugin's own cache.
Disabled plugins receive no hooks/filters; re-enabling preserves their loaded
module and registrations without duplication. Previously unloaded enabled
files register once. Editing an enabled Python file replaces its module and
hooks automatically; disabled files wait until enabled. Broken new plugins are
logged/skipped while valid global rules apply. The previous working version
survives a broken edit, with one error logged per distinct attempted content.
`api.log(message)` writes `plugin.log` to the normal capture event index.
Do not put credentials or private player data in these messages.

## Live source reload

The watcher hashes file contents, so same-size edits with unchanged timestamps
still reload and do not reuse stale Python bytecode. It waits about half a
second after observing an edit before applying it. A callback already running
finishes before its plugin is replaced; subsequent events use the complete new
hook set. Reloading does not replay `server.started`, player logins or completed
match events. Successfully paid match receipts keep their original reward.

Module globals and callback closures start fresh after a successful source
replacement. Use `api.data_dir` for state that must survive reloads or restarts.
Deleting a file removes only its hooks/module; private data remains. Config
disable/re-enable without a source edit keeps the loaded module's memory.

Keep module initialization and `register(api)` quick and safe to repeat. Hook
registrations are rolled back on failure; arbitrary file writes, network calls
or threads created by plugin code cannot be rolled back. Background workers
belong to the plugin and are not automatically stopped on reload. The watcher
loads standalone files in this directory; imported dependencies elsewhere are
not watched or reloaded automatically.

The log records `plugin.loaded`, `plugin.reloaded` and `plugin.unloaded`, with
the plugin name and a generation counter. Failures use `plugin.error` without
source text or exception messages. `/fut-local/health` includes a `plugins`
status with `watching`, `generation`, `loaded_count`, `active_count` and
`failed_count`; the generation resets on server restart.

## Presence semantics

`online` means connected and authenticated to this replacement backend;
`in_fut` additionally means holding a live FUT session. Returning to FIFA's
main menu leaves the player online but outside FUT. It is not a disconnect.
On a disconnect inside FUT, the server emits FUT leave/logout before offline.
Snapshots and query results already reflect the new state during callbacks.
Leave reasons are `logout`, `disconnect`, `relogin` or `server_shutdown`.
Presence is not the player's global PSN/RPCN online status. A hard process
kill cannot send callbacks; graceful shutdown sends leave/offline events.

`player.created` fires before PostAuth and FUT entry. The new club can be
queried there, while the authenticated display name arrives during login.

## Query API

Queries return independent dictionaries/lists, including for offline saved
clubs. Mutating them does not save or change game state. Offline reads do not
create accounts, log players in, or write their saves. No session keys, web
tokens, RPCN blobs, account IDs, raw request data or save paths are returned.
These methods are for trusted server plugins, not public HTTP endpoints.

| Method | Result |
|---|---|
| `api.get_players(online=None, in_fut=None, start=0, count=100)` | Saved players with presence; optional boolean filters |
| `api.get_player(persona_id)` | One player snapshot |
| `api.get_clubs(start=0, count=100)` | Club summaries, including offline clubs |
| `api.get_club(persona_id)` | Name, coins, equipment, counts, active squad, season progress and statistics |
| `api.get_cards(persona_id, pile=None, edition=None, start=0, count=100, **filters)` | Owned player cards; `pile` is `club`, `purchased` or null for both |
| `api.get_card(persona_id, item_id)` | An owned card; item IDs are scoped to the specified club |
| `api.get_equipment(persona_id, item_type=None, active_only=False, start=0, count=100)` | Kit, badge, ball and stadium choices with active states |
| `api.get_squads(persona_id)` | All saved squads with slots and card snapshots |
| `api.get_squad(persona_id, squad_id=None)` | A named squad ID or the active squad if omitted |
| `api.get_matches(persona_id, mode=None, start=0, count=100)` | Committed match receipts, newest first; mode is `ONLINE`/`OFFLINE` |
| `api.get_match(persona_id, match_id)` | One committed receipt's result, stats, card items and paid coins |
| `api.get_packs(include_retired=False)` | Configured pack definitions/prices |
| `api.get_pack(pack_id)` | One pack, including retired definitions |
| `api.get_catalogue_card(asset_id, edition="standard")` | A football player's card metadata, whether owned or not |
| `api.search_catalogue(edition="standard", start=0, count=100, **filters)` | Card catalogue search |
| `api.get_rankings(category="competitor", start=0, count=100)` | Local ranks and scores across saved clubs |
| `api.get_config()` | Current global rules; other plugins' settings are omitted |

The optional arguments after an ID are keyword arguments. Pagination uses
`start` from 0 to 1,000,000 and `count` from 1 to 1,000. Players/clubs sort by
persona ID, cards by item ID and catalogue results by asset ID. Invalid values
raise `ValueError`; unknown players/items/squads/pack IDs raise `KeyError`.
IDs are positive integers; the match receipt ID is a string. Queries are
available during `register(api)` as well as callbacks. An empty new multiuser
server lists zero players until the first registration.

Card filters: `min_rating`, `max_rating` (0–115), `name` (case-insensitive
substring), `position`, `nation_id`, `team_id`, `league_id`, `rare` (boolean).
Editions include `standard`, legacy `england110` and configured custom editions
such as `prime`. Owned snapshots remain queryable when an edition is disabled.
Rows also include `resource_id`, `version` and `edition_label`; catalogue queries
show current definitions, while owned-card queries show issued stat snapshots.
Owned-card rows include
`item_id`, `asset_id`, `name`, `rating`, `position`, nation/team/league IDs,
`rare`, `edition`, native indexed `attributes`, `quick_sell_value`, `pile`,
`created_at`, and `condition` with `injury_type`, `injury_games`, `suspension`.
Cards and equipment are separate query collections.

Squad slots include `index`, `item_id`, `kit_number`, `role` and `card`.
An empty slot has item ID zero and null `card`; roles are `starting`, `bench`
or `reserve`. Club statistics include `won`, `draw`, `loss`, `played`, local
`points` and quick-sell `value`. Season progress snapshots contain only saved
division/round/played/points fields; reading them does not roll a season over.
Match queries expose committed results, not live matchmaking state or raw
report payloads. Rankings use `competitor`, `collector`, `seasonpoints` or
`seasonplayed`, as described in the config docs.

## Example: join logging with club/card lookups

Copy `plugins/_example_presence.py` to `plugins/presence.py` to automatically
enable the supplied example. A shorter version:

```python
def register(api):
    @api.on("player.online")
    def joined(event):
        person = event["player"]
        club = api.get_club(person["persona_id"])
        cards = api.get_cards(person["persona_id"], pile="club", min_rating=85)
        api.log(f"{person['name']} joined {club['name']}; {club['coins']} coins")
        for card in cards:
            api.log(f"{card['name']} ({card['rating']})")

    @api.on("player.offline")
    def left(event):
        api.log(f"{event['player']['name']} left: {event['reason']}")
```

Callbacks remain synchronous; queries take the backend lock and return copies.
Use pagination instead of repeatedly collecting every card or match. Send slow
work to your own worker with captured snapshots, then use the read API there
when needed. No callback needs to manipulate transport objects or credentials.
The modifying hook remains `match.reward`; all new lookup methods are reads.
