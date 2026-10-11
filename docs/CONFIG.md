# Server rules

Edit `/path/to/fifa19-ps3-fut/server-config.json`. The
operator creates it from the supplied template and preserves it on later updates. The
template is `data/server-config.example.json`. This is JSON: no comments or
trailing commas. Missing settings use defaults; unknown keys, duplicate keys
and invalid values fail preflight. Rules load once when the server starts.

After editing, validate before restarting:

```bash
systemctl --user stop fifa19-fut.service
cd "$(systemctl --user show fifa19-fut.service --property=WorkingDirectory --value)"
python3 server.py --check --state-dir /path/to/fifa19-ps3-fut/state \
  --advertise 192.0.2.55 --bind 0.0.0.0 \
  --config /path/to/fifa19-ps3-fut/server-config.json \
  --plugins-dir /path/to/fifa19-ps3-fut/plugins
systemctl --user start fifa19-fut.service
```

`--check` needs the server's save lock to be free. Stop the service before
running it (`systemctl --user stop fifa19-fut.service`); if validation fails,
correct the configuration and repeat the check, then start the service.
Coordinate restarts between matches. Re-enter FUT to refresh cached store
prices and card values; close/relaunch clients for a server release update.

| Setting | Default | Meaning |
|---|---:|---|
| `economy.starting_coins` | 20000 | Initial balance of a newly created inventory; existing balances are preserved |
| `economy.win_coins` | 1000 | Base full-time win reward |
| `economy.cpu_division_bonus` | 500 | Extra win coins for each CPU division above displayed Division 10 |
| `economy.draw_percent` | 75 | Draw reward as a percentage of the win base |
| `economy.loss_percent` | 50 | Loss reward as a percentage of the win base; cannot exceed draw percentage |
| `economy.quick_sell` | tier pairs | Common/rare quick-sell values for bronze, silver and gold |
| `packs.prices` | per-pack map | Coin prices keyed by string pack ID; applies to offers, purchases and descriptions |
| `packs.gold_rare_weights` | `[30,30,30,10]` | Positive weights for rating bands 80–82, 83–84, 85–89, 90–99 |
| `points.win`, `.draw`, `.loss` | 3, 1, 0 | Local leaderboard points per completed result |
| `cpu_seasons.hold` | `[5,6,7,8,9,10,11,12,13,14]` | Points needed to avoid relegation after ten CPU fixtures |
| `cpu_seasons.promote` | `[12,13,14,15,16,17,18,19,20,21]` | Points needed for CPU promotion after ten fixtures |
| `matchmaking.search_seconds` | 20 | Server search expiry, capped by the client's requested duration |
| `matchmaking.peer_timeout_seconds` | 180 | Inactivity timeout for paired games, refreshed by valid peer traffic |
| `plugins.enabled` | true | Enable loading of custom Python plugins |
| `plugins.disabled` | `[]` | Plugin filenames without `.py` to skip |
| `plugins.settings` | `{}` | Per-plugin JSON objects provided as `api.settings` |

Division arrays run from **displayed Division 10 to Division 1**. The wire
division numbers run in the opposite direction: wire 1 is displayed Division
10. Targets must be 0–30, with hold below promotion in each division. The
game's native season counters remain **three points per win, one per draw**;
the `points` settings affect leaderboards, not those compressed client season
counters. Online promotion/relegation is still unimplemented.

Gold-rare weights are relative chances across available bands. Restricted
packs renormalize them, then choose a player within the band. A higher top-band
weight improves elite odds without guaranteeing Messi or Ronaldo. Pack sizes,
rarity guarantees, eligible nations/leagues and stock ratings remain defined
in `fut_packs.py`; prices and odds need no code edits. England 110 stays retired.

New rewards snapshot their policy and plugin adjustment in the match receipt.
Changing rules never recalculates paid coins or breaks saved receipts. Retried
reports return the original award. Quit/DNF/no-contest pays zero, including
when reward plugins are enabled. Leaderboards intentionally recompute all-time
scores using the current point weights and club values using current quick-sell values.

An example partial configuration (all omitted rules keep defaults):

```json
{
  "economy": {"starting_coins": 20000, "win_coins": 1500},
  "packs": {"prices": {"6": 6000, "164": 40000}},
  "points": {"win": 5, "draw": 2, "loss": 0},
  "plugins": {"settings": {"win_bonus": {"bonus_coins": 300}}}
}
```
