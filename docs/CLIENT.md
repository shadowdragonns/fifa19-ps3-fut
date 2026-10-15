# Client and patcher

Use Python 3.10+, RPCS3 with your firmware, your own **FIFA 19 BLES02258** game
and **v1.02** update, and a configured RPCN account/controller. Close RPCS3 before launching.
Each friend needs a distinct RPCN account. Reusing that account retains its
saved club, regardless of client IP. The launcher fetches the selected server's
public certificate automatically; no certificate copy or client OpenSSL setup
is needed.

## Graphical launcher

On Linux run `python3 launcher_gui.py` (or `./Start\ FUT\ Launcher.sh`).
On Windows double-click **Start FUT Launcher.cmd**, or run
`py -3 launcher_gui.py`. You can also use `launch.py --gui --server SERVER_IP`.
The GUI uses Tkinter, included with normal Windows Python installations;
Debian/Ubuntu users may need `sudo apt install python3-tk`. A minimal embedded
Python runtime needs Tcl/Tk added by its packager.

The **Play** tab lets you choose RPCS3, a JB game folder or decrypted ISO, and
the server IP/hostname. **Check setup** validates the game, update, RPCS3 profile,
server and certificate; **Launch FIFA** applies the temporary patch and starts
RPCS3. **Stop game** lets the launcher close RPCS3 and restore its patches. Keep
the launcher running until FIFA closes.

The **Setup** tab opens RPCS3 for account/controller setup and installs your own
extracted update. For a `.pkg` update use RPCS3's package installer. **Advanced**
contains an optional data-folder override, manual CA, prepared Linux instance,
trace logging and deliberate certificate replacement. **Activity** shows progress
and errors, with copy/open-log buttons. Settings are saved separately per OS user
in `~/.config/fifa19-fut/launcher.json` (respecting `XDG_CONFIG_HOME`) or
`%APPDATA%\fifa19-fut\launcher.json` on Windows.

## Command-line launcher

Linux:

```bash
python3 launch.py --server 192.0.2.55 \
  --rpcs3 /path/to/rpcs3.AppImage --game /path/to/FIFA19
```

Windows PowerShell:

```powershell
py -3 launch.py --server 192.0.2.55 --rpcs3 "F:\rpcs3.exe" --game "D:\Games\FIFA19"
```

Replace addresses and paths. The launcher accepts a server hostname resolving
to IPv4. `--profile` can select RPCS3's existing data root containing `dev_hdd0`.
Linux defaults to `~/.config/rpcs3`. Windows uses the folder beside `rpcs3.exe`
or its existing `portable` folder; do not point at its nested `config` folder.
Conflicting portable overrides are refused. Linux AppImage extraction is
cached automatically. The development setup used RPCS3 revision bb5dc93b;
other emulator revisions need testing, while the game executable hash remains
strictly checked against BLES02258 v1.02.

## Automatic public certificate

The launcher reads `http://SERVER_IP:8099/fut-local/health` and
`/fut-local/root.pem`, validates the public CA's native RSA format, then verifies
the server's TLS leaf and health response on TCP **44325** before using it.
Only that exact server address is contacted; redirects and proxy environment
variables are ignored/refused. CA SHA256 values use the DER certificate bytes.
No private key is downloaded or embedded in the game.

The first connection trusts the server reached at the address you entered;
the initial HTTP download is not independently authenticated. Use a trusted
LAN/VPN or obtain `root.pem` from the operator and pass `--trust-ca` for an
independently supplied CA. Downloaded certificates are pinned separately by
IPv4 address under `runtime/server-certs/` for CLI launches, or beside the GUI
settings file for GUI launches. The launcher rechecks the server on each launch;
an unexpected CA change fails without replacing the saved pin or applying patches.
After a deliberate server CA rotation, use `--refresh-server-cert` or select the
replacement option under Advanced. `--cert-cache` overrides the CLI cache path.
Older servers without certificate discovery still work with `--trust-ca`.

## ISO and update input

`--game` accepts a JB folder containing `PS3_GAME` or a **decrypted ISO**, whose
files can be read without disc encryption. The launcher does not decrypt
encrypted images. Install 7-Zip for ISO extraction; use `--iso-cache` to choose
a drive with room for the extracted game. Original images are preserved.

The repository does not supply an update. If you already have an extracted
v1.02 update folder from your own installation, install it into your selected
Windows RPCS3 setup with:

```powershell
py -3 install_update.py --rpcs3 "F:\rpcs3.exe" --source "D:\OwnUpdate\BLES02258"
```

The installer validates and copies only that title's update, respects RPCS3's
VFS hard-drive path, backs up an existing update and restores it on failure.
Other games, saves, firmware and RPCN settings are preserved. It does not
download game files. No private TLS key or OpenSSL installation is needed on
the client; the public CA reader uses Python's standard library.

## Temporary patches and validation

Add `--prepare-only` for game/update/profile/network and automatic certificate validation without
starting RPCS3 or installing temporary patches. A normal launch replaces the
game's per-launch `cl.ini`, redirects discovery to the chosen server, and
installs a hash-specific certificate/boot compatibility patch through RPCS3.
It restores patch settings and removes its temporary files after exit.
An existing launcher-generated FIFA patch, including one left by an interrupted
launch, is backed up and refreshed for the selected server, then restored on
exit. Existing settings for that patch are updated without duplicating its hash
or replacing other FIFA patch entries. Backups are saved in the launch log
directory as `BLES02258_patch.yml.before` and `patch_config.yml.before` when
those files existed. Unrecognised/custom title patches, linked files and
ambiguous settings are refused with an error identifying the conflict. The
launcher does not rewrite the ISO or EBOOT on disk. Network diagnostics are
captured privately; `--trace-network` enables verbose tracing if needed.

For same-PC testing with the source tree, initialize TLS locally and omit
`--server`; the launcher starts and stops its same-PC server. Separate portable
client ZIPs require `--server`. Windows supports one client per PC.

## Two Linux clients

Close RPCS3, then prepare isolated instances:

```bash
python3 setup_clients.py --game /path/to/FIFA19 --profile /path/to/rpcs3/data
python3 launch.py --instance player2 --configure --server 192.0.2.55
```

Set a different RPCN account for player 2, then close its configuration window.
The instances retain separate game/profile/cache files. Launch one per terminal:

```bash
python3 launch.py --instance player1 --server 192.0.2.55 --rpcs3 /path/to/rpcs3.AppImage
python3 launch.py --instance player2 --server 192.0.2.55 --rpcs3 /path/to/rpcs3.AppImage
```

Do not add `--game` or `--profile` to prepared instances. A C compiler is needed
for their process-local UDP port adapter. `--background-input` allows separate
controllers while a window is unfocused. Instance setup is Linux-only.

In both clients open **Ultimate Team → Online Seasons → Play Match** and
search within the configured search window. Ready both sides. Gameplay is
peer-to-peer, so RPCN and peer connectivity must work in addition to server
access. A second match should use the same login; test repeated full games and
report the stage of any failure. Reconnect after a server restart and re-enter
FUT to refresh changed prices/cards. Do not reset squads to apply an update.
