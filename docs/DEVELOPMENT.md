# Development

Runtime code is in root-level Python modules. `server.py` starts the backend;
`launch.py` is the RPCS3 client/patcher. `server_settings.py` validates editable
rules; `server_plugins.py` publishes domain events and the reward filter.
`sessions.py`/`accounts.py` route private saved clubs by RPCN identity.
Inventory, squads, equipment, CPU lifecycle and paired games each have their
own `fut_*` modules. `probe.py` dispatches HTTP, Blaze and TLS transport.

Run the publication-safe tests from the source checkout with no game files:

```bash
python3 -m unittest discover -s tests -v
python3 audit_public.py
python3 build_bundle.py --output dist
```

Tests exercise fresh local CA generation, custom-certificate patch rows,
TLS handshakes, backend packs/rules/reward persistence and plugin isolation.
The development environment has a larger protocol/native regression suite;
private session fixtures and game executable inputs are not published.
Automated checks do not replace a desktop test of kickoff, full-time return
and a second match with both players staying signed in.

`build_bundle.py` creates separate server/client ZIPs using an explicit file
allowlist and checksummed manifests. The server ZIP generates TLS locally
during setup. The client ZIP accepts a supplied public CA; it contains no
server keys. Bundles contain no state or game files. Release artifacts should
come from this builder, not a copy of a working installation.

The supported executable SHA256 and PPU hash are recorded in
`data/client-native-patch.json`. RPCS3 patch metadata contains generated
trampolines and a boot compatibility patch, not an EBOOT. Public-certificate
modulus rows are generated for each operator's own CA at launch.

Keep runtime files ignored. Run the audit before committing/publishing. It
rejects game archives/executables, private key/token patterns and private
runtime directories among Git-tracked files (or distribution entries). It
also verifies release ZIP manifests. Never publish captured requests, saved
clubs, raw firmware/game files, private certificates or GitHub auth storage.
For bug reports, provide the game/emulator revision and sanitized findings.
