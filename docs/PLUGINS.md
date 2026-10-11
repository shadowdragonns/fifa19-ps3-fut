# Python event plugins

Place standalone `.py` files in `/path/to/fifa19-ps3-fut/plugins` on
your server. Names must start with a letter and contain only letters, numbers
and underscores (up to 64 characters). Files beginning with `_` are ignored.
Plugins load in filename order at startup and survive code deployments.
Use trusted code: plugins run as the server's OS user with normal Python
access to files and network. The API gives domain events instead of raw
HTTP, Blaze packets, passwords or session tokens.

Each file exports `register(api)`. Subscribe with `api.on(event, callback)`
or its decorator form. Handlers receive an independent copy of a dictionary;
mutating it does not change server state or another handler's payload.
Exceptions are logged as `plugin.error`, isolated from the match response,
and do not undo a successful save. A registration failure removes all hooks
that plugin registered. Syntax errors fail preflight; `--check` compiles
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
then restart between matches. Set `plugins.disabled` to `["win_bonus"]` or
`plugins.enabled` to `false` to disable it. Removing a plugin does not remove
its stored data or change previously paid rewards.

Every event includes `event`. A `player` is `{persona_id, club_name}`; it
identifies the saved club and active squad name, not a login credential.

| Event | Additional fields | Timing |
|---|---|---|
| `server.started` | `advertise`, `plugins` | Listeners started |
| `player.login` | `player` | A new FUT SID is issued; no SID is exposed |
| `pack.opened` | `player`, `pack_id`, `purchase_id`, `price`, `cards` | New pack saved; cards contain `item_id` and `asset_id` |
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
of their files. `api.settings` is a copy of their JSON settings.
`api.log(message)` writes `plugin.log` to the normal capture event index.
Do not put credentials or private player data in these messages.
