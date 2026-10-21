# FIFA 19 PS3 FUT server and client patcher

A community replacement backend and RPCS3 launcher/patcher for **FIFA 19 PS3
BLES02258 v1.02**. The client supports Linux and Windows. The server uses
Python's standard library and OpenSSL to issue its own local TLS certificates.

Includes per-user clubs, coins, player packs, named squads, equipment choices,
repeatable ten-game CPU seasons with promotion/relegation, local leaderboards,
and two-player FUT Online Seasons matchmaking and result saving. Server rules
and Python domain-event plugins let operators customize the economy.
Plugin API v2 adds online/offline and FUT entry/leave events, plus structured
queries for saved players, clubs, cards, equipment, squads and match history.
Every player has Prime/TOTW (+10), TOTY (+20) and Ones to Watch (+25) versions.
Gold-player odds are respectively 1 in 400, 1 in 2,000 and 1 in 10,000; edition
packs are available on the web. Rules reload live and refresh already-owned
cards. Managers and club supplies have their own in-game packs; fitness,
contracts, temporary training and player match totals persist.
FUT Lab adds a club-linked website for building custom packs with live prices
and opening them into the same saved FUT inventory.
Single-player divisions progress from weak clubs on Amateur in Division 10
to strong international teams on World Class in Division 1.
Standalone Python plugins reload automatically when added, edited or removed;
broken edits keep the last working hooks.

**This repository contains no ROM, game/update binaries, firmware, emulator,
private TLS keys, RPCN credentials, saved clubs or captured sessions.** Supply
your own game, update and RPCS3 installation. JSON catalogues contain player
and equipment metadata with locally calculated card attributes; they contain
no playable game code, models, textures, audio or video. This is an independent
project, not an EA service or official FUT economy.

## Quick start

Requirements: Python **3.10+**, OpenSSL on the server, RPCS3 with firmware and
your own BLES02258 game/update on each client. ISO inputs must be decrypted;
7-Zip is needed to extract them. Each multiplayer client needs a different
configured RPCN account.

### Server

```bash
git clone https://github.com/shadowdragonns/fifa19-ps3-fut.git
cd fifa19-ps3-fut
python3 initialize_tls.py
cp data/server-config.example.json server-config.json
python3 server.py --check --bind 0.0.0.0 --advertise 192.0.2.55
python3 server.py --bind 0.0.0.0 --advertise 192.0.2.55
```

Replace `192.0.2.55` with the server's reachable IPv4 address. Open TCP
**8990, 8099, 42230, 44325** and UDP **8099** on the network you use. Peer
gameplay runs between clients and needs working RPCN/peer connectivity.

`initialize_tls.py` creates a unique server identity. Clients automatically
fetch its public CA certificate from the server and pin it for that address.
Never share or commit `root.key` or `local.key`. Setup preserves an existing TLS directory.

### Client

Copy/clone this project on the client. Close RPCS3 before launching.
Open the GUI with `python3 launcher_gui.py` on Linux or **Start FUT Launcher.cmd**
on Windows. Choose the emulator/game paths, enter the server address, then click
**Check setup** or **Launch FIFA**. No manual certificate copy is needed.
See [client setup](docs/CLIENT.md) for Tkinter requirements, update installation
and certificate trust details.

The command line also works. On Linux:

```bash
python3 launch.py --server 192.0.2.55 \
  --rpcs3 /path/to/rpcs3.AppImage --game /path/to/FIFA19
```
the path /path/to/FIFA19 is the path to the folder with ```PS3_DISC.SFB  PS3_GAME  PS3_UPDATE``` inside 


On Windows, from PowerShell:

```powershell
py -3 launch.py --server 192.0.2.55 --rpcs3 "F:\rpcs3.exe" --game "D:\Games\FIFA19"
```

The patch is applied temporarily through RPCS3. Original ISO/EBOOT files are
preserved and emulator patch settings are restored after exit. The script
changes the game's network configuration for the launch. The public-certificate
reader uses Python only; Windows clients do not need OpenSSL. Use
`--prepare-only` to validate your files/profile without starting a game.

## Documentation

- [Server installation, hosting and backups](docs/SERVER.md)
- [Linux/Windows client, ISO input and update installation](docs/CLIENT.md)
- [Prices, rewards, points, divisions, odds and timeouts](docs/CONFIG.md)
- [Python plugins and game events](docs/PLUGINS.md)
- [Custom Prime cards, version IDs and packs](docs/CUSTOM_CARDS.md)
- [Web pack builder, club logins and pricing](docs/WEB.md)
- [Development and tests](docs/DEVELOPMENT.md)

Editable configuration lives in `server-config.json`; Python plugins live in
`plugins/` and their private files in `plugin-data/`. Valid JSON settings reload
live in about one second; invalid edits retain the last valid rules.
Defaults give new clubs 20,000 coins and full-time win/draw/loss
rewards of 1,000/750/500, with higher CPU division rewards. Reward receipts
snapshot the policy so future configuration changes preserve old awards.

## Status and limits

Full CPU games, substitutions and completion saving, and two-player gameplay
were tested in RPCS3 during development. The latest rematch cleanup fix needs
further desktop testing across repeated games. Public packaging and custom CA
support have automated coverage; a fresh Windows desktop installation still
needs testing. The patch targets the specific v1.02 executable hash and is
not a general patch for other title IDs or revisions.
Special IDs/rarity flags and OVR through 255 passed the native card parser.
The new special artwork, managers and consumable menu flows still need live
RPCS3 testing; parser/backend checks do not establish their match-engine behavior.

Online division promotion/relegation, full quit/disconnect outcomes, transfer
market trading, official EA accounts and global EA leaderboards are not
implemented. Season match counters remain native 3/1/0 points; configurable
point weights affect local leaderboards.

MIT licensed. FIFA, EA, PlayStation and other product names belong to their
respective owners. RPCS3 and RPCN are separate projects; neither is bundled.

A massive disclaimer is most of the code was made made with Codex
as it involed looking at the raw code of the PS3 rom and i wouldnt know where to start with that
but a lot of the testing was done by me and a friend and keep in mind you will sometimes just get kicked from fut (it hasnt happened to me while in a match "yet") 
but overall it works and for being made in about 2 ish days i think its quite decent 

## keep in mind that this is a personal project it was not made specifically for people in mind i simply thought that other people would want this as well 
