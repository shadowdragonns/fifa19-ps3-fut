# Server installation and operation

Python 3.10+ and OpenSSL are required. No game installation is needed on the
server: generated catalogues supply the metadata used by the backend. The
server's Python runtime needs no third-party packages.

```bash
python3 initialize_tls.py
cp data/server-config.example.json server-config.json
python3 server.py --check --advertise 192.0.2.55 --bind 0.0.0.0
python3 server.py --advertise 192.0.2.55 --bind 0.0.0.0
```

Replace the example IPv4 address with one clients can reach. `--bind` can be
`0.0.0.0`; `--advertise` cannot. Clients receive this address in discovery
responses. TCP ports are 8990, 8099, 42230 and 44325; UDP 8099 serves QoS.
Use a reachable LAN/VPN address or your chosen external network setup.
Game traffic is peer-to-peer; backend reachability alone does not establish it.

## TLS identity

Each installation generates its own CA and localhost leaf. For a remote
advertised IP, the server issues a matching leaf under `state/tls/` from the
same CA. Existing keys are preserved. The server serves **only the public CA**
at `http://SERVER_IP:8099/fut-local/root.pem`; `/fut-local/health` advertises its
path and DER SHA256 fingerprint. The GUI and CLI clients fetch it automatically,
verify the server's TLS leaf on port 44325, then pin it by server address.
There is no general file-serving route and no endpoint for private `.key` files.
You can also supply `tls/root.pem` directly with `launch.py --trust-ca`.
The client embeds that RSA public modulus
in its temporary hash-specific RPCS3 patch. It does not disable all certificate
verification. The certificate must use RSA 2048 with exponent 65537.
Use the supplied initializer: its issuer labels match the game's native CA
entry. Its keys are unique private keys, and its self-signed certificate is
not a public GeoTrust-issued certificate.

Keep private `.key` files on the server. They are deliberately excluded from
Git and distributable ZIPs. Back them up privately with the saved clubs. If you
create a different CA, clients must explicitly replace their saved certificate
(`--refresh-server-cert` or the GUI's Advanced option). First-contact trust and
manual certificate options are explained in [CLIENT.md](CLIENT.md).

## Paths and flags

| Path/flag | Purpose |
|---|---|
| `server-config.json`, `--config` | Economy, packs, points, targets, timeouts and plugin rules |
| `state/`, `--state-dir` | Saved clubs, registry, PRAN data, remote leaf and save lock |
| `logs/`, `--logs` | Private request/response captures |
| `plugins/`, `--plugins-dir` | Trusted Python extensions |
| `plugin-data/`, `--plugin-data-dir` | Persistent private extension files |
| `--locale-archive` | Optional user-supplied game locale archive for additional account labels |
| `--check` | Validate config, saves, TLS and plugin syntax without opening listeners |
| `--check-config` | Validate only the JSON config, including while the server is running |

An explicit config path must exist. Without `--config`, the server reads the
file beside its code if present, otherwise uses defaults. Plugin/data paths
default to directories next to the selected config. Stop the server before
`--check` against its authoritative state: preflight takes the same save lock.
Preflight can issue a missing TLS leaf but does not execute custom plugins.
The config path is watched while running; valid edits apply in about a second,
and invalid/partial saves keep the last valid rules. Sessions and saves stay
intact. `--check-config` validates the file without taking the save lock.

Each RPCN identity owns a persistent club. `state/accounts.json` maps hashed
identities to `state/users/<hash>/`. Reuse an account to retain that club.
Never run two writers against the same state directory. A stale `server.lock`
can be removed only after its process has stopped. Back up the whole stopped
state, config, TLS, plugins and plugin-data directories. Use compatible code
with a restored state snapshot; old code may not recognize newer receipts.

## Optional Linux user service

Create `~/.config/systemd/user/fifa19-fut.service`, substituting **all paths
and the advertised IP**:

```ini
[Unit]
Description=FIFA 19 PS3 community FUT server

[Service]
Type=simple
WorkingDirectory=/path/to/fifa19-ps3-fut
ExecStart=/usr/bin/python3 /path/to/fifa19-ps3-fut/server.py --bind 0.0.0.0 --advertise 192.0.2.55 --config /path/to/fifa19-ps3-fut/server-config.json --state-dir /path/to/fifa19-ps3-fut/state
UMask=0077
KillSignal=SIGTERM
TimeoutStopSec=35
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now fifa19-fut.service
systemctl --user status fifa19-fut.service
journalctl --user -u fifa19-fut.service -n 80 --no-pager
```

Restart between matches after a code, TLS/address or Python plugin source update.
JSON config edits reload live. Login and
matchmaking state are in memory, so clients must reconnect afterward.
Long-running user services may need your system's user-session persistence
settings. The server can also run directly in a terminal.

## Updating and diagnosing

Stop the server, back up its private directories, update the source (`git pull`),
run preflight, then start it again. Git ignores config, saves, TLS and custom
runtime output. Keep custom plugins outside tracked example files, or use
`--plugins-dir` pointing outside the checkout. The disabled example is
`plugins/_example_bonus.py`.

Check `/fut-local/health` for service identity/features. Logs are indexed by
`events.jsonl`; captures contain session credentials and stay private. Redact
them before sharing an issue. For a rematch failure, note the stage and whether
there is an HTTP create error or a Blaze disconnect. Banned/injured cards in
the XI/bench also cause a create error: rest them in reserve slots 18–22 or
outside the squad for a completed match. Main-menu Seasons is not FUT Seasons.

See [CONFIG.md](CONFIG.md), [PLUGINS.md](PLUGINS.md) and [CLIENT.md](CLIENT.md).
