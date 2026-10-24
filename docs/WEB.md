# FUT website

Open `http://SERVER_IP:8099/club/` using the same reachable server IP as your
launcher. The site runs inside the existing FUT service, with no separate
server process, Node runtime or extra port. Use your private/Tailscale address
for the initial installation. Website access requires its own club-linked login.

The **Club settings** panel contains the per-club automatic contract switch.
It is disabled by default. When enabled it uses owned compatible contract
cards first, then the displayed coin price if no card remains. Only expired
contracts in the active XI, bench and selected manager are renewed between
matches. See [ITEMS.md](ITEMS.md) for timing, costs and insufficient-funds behavior.

## Link a website account to a club

An operator issues a one-use registration code for an existing saved club.
The player enters that code on **Create account**, then chooses a
website username and password. The website never asks for RPCN credentials.
There is one website account per saved club. Usernames are case insensitive.

From the running server's source/release directory:

```bash
python3 web_accounts.py --state-dir /path/to/state clubs
python3 web_accounts.py --state-dir /path/to/state invite --persona 1000001
```

The invitation expires after 60 minutes by default. `--minutes 1440` allows one
day. Keep the code private: whoever redeems it can spend that club's local FUT
coins. It can be used only once. These commands can run while the service runs;
they use a separate transactional SQLite authentication store.

To recover a forgotten website password or replace the linked account:

```bash
python3 web_accounts.py --state-dir /path/to/state invite --persona 1000001 --reset
```

Reset revokes the old website login and its sessions, then issues a new invite.
It does not alter game accounts, cards, squads or coins. Website sessions last
30 days and persist across server restarts. Sign out on shared computers.
Passwords use individually salted scrypt hashes. Sessions/invites are stored as
SHA256 token hashes; no plaintext passwords or login tokens are stored.

Browser requests under `/club/` bypass raw game-protocol captures, so passwords,
cookies and invite codes do not appear in the FUT capture archive. API mutations
require same-origin JSON; authenticated mutations also require a session-bound
CSRF token. Cookies use HttpOnly and SameSite=Strict, plus Secure when using the
server's TLS connection. There is no wildcard CORS access. The initial allowed
hosts are the advertised server IP and localhost on port 8099.

## Build and open a pack

Choose 1, 3, 6 or 12 distinct players. Optional filters cover club, nationality,
league, position, base-card tier, minimum overall, rare base players, Prime/TOTW, TOTY and Ones to Watch.
The price and number of matching players update as selections change. The
minimum overall applies to the final edition; other filters retain the selected
footballer's club/nation/league. Prime stats and individual overrides use the
same live custom-card definitions as FUT. Prime packs are web-only;
the builder uses the minimum overall you select.

Standard custom packs explicitly restricted to gold use the usual
special-edition probabilities: Prime/TOTW 1 in 400, TOTY 1 in 2,000 and
OTW 1 in 10,000 per card. Edition-specific packs always draw that edition. Players are
sampled uniformly from the eligible pool without duplicates within a pack.
No purchase is possible if fewer distinct players match than the selected size.

A purchase debits real local FUT coins and adds new versions straight to the
saved club. Exact versions already owned are automatically quick-sold for their
normal configured value, with an explicit confirmation checkbox and a refund
shown in the result. Normal and Prime versions of one footballer remain distinct.
The website does not replace any card, active squad or item condition.

Resolve pending in-game packs and return from an active/reserved match before
buying. If FIFA is already running, re-enter FUT to refresh its cached cards
and coin balance. The website refreshes its balance periodically.

Every purchase saves its coin change, cards and receipt in one atomic inventory
commit. Retrying the same quote returns the original result without another
charge, including after a server restart. Quotes expire after ten minutes.
Expired receipt pruning cannot make an old quote charge again. If prices or
custom rules change, refresh the quote before purchasing.

## Live pricing

Add this section to `server-config.json`; it reloads using the existing watcher:

```json
{
  "web_packs": {
    "enabled": true,
    "base_per_player": 750,
    "filter_surcharge": 750,
    "extra_filter_percent": 20,
    "prime_surcharge": 30000,
    "toty_surcharge": 60000,
    "otw_surcharge": 100000,
    "rating_step_surcharge": 500,
    "rare_surcharge": 750
  }
}
```

All numeric prices/percentages must be integers from 1 to 1,000,000. Positive
surcharges ensure that adding a filter always increases the price. The base is
per player. Each filled filter adds `filter_surcharge` per player. Prime adds
`prime_surcharge`, TOTY adds `toty_surcharge`, OTW adds `otw_surcharge`, rare-only adds `rare_surcharge`, and minimum rating adds
`max(0, minimum - 74) * rating_step_surcharge`. Each filter after the first also
adds `extra_filter_percent` to the subtotal. The interface shows the full
breakdown. Size multiplies the per-player costs. All prices are calculated by
the server; browser-submitted prices cannot override them.

Plugins can subscribe to `web.pack.opened` for committed web purchases. Payloads
contain `player`, `parameters`, `price`, `duplicate_refund` and `cards` (item ID,
asset ID, edition, duplicate). They contain no website credentials. This is a
separate event from native `pack.opened`, whose pack IDs refer to fixed offers.

Back up the whole stopped state directory, including `web-accounts.sqlite3`.
Auth database and game inventories must stay private and outside releases.

## Administrator card editor

An operator grants access to an existing website account from the server:

```bash
python3 web_accounts.py --state-dir /path/to/state admin --username YOUR_WEB_USERNAME
# Remove access immediately, including for existing browser sessions:
python3 web_accounts.py --state-dir /path/to/state admin --username YOUR_WEB_USERNAME --revoke
```

Refresh the website after a grant. **Card editor** appears in the navigation.
Select a club, then a saved squad or all owned players; search by name, edition
or owned card ID. Select a card and edit OVR, six attributes, position, fitness,
contracts, injury or suspension. Save applies one atomic inventory update.

Edits affect that owned card in every squad using its ID. They do not replace
its asset, edition or other copies and survive edition-rule reloads. Temporary
training is added separately. **Remove stat overrides** restores edition stats
without resetting condition. Fitness edits reset the real-time recovery clock.
Native fields allow OVR 1–255 and attributes 0–4294967295; these storage limits
do not guarantee every value renders or plays sensibly in FIFA.

Exit matches and matchmaking before editing a club. Reopen the squad or re-enter
FUT to refresh cached cards. A stale editor cannot overwrite a newer card; reload
it first. Ordinary website accounts cannot use admin endpoints. Grants are
operator-only, and authenticated admin writes retain origin and CSRF checks.
The latest 500 edits are recorded privately in that club's inventory; credentials
are never included. Back up the stopped state before bulk manual experiments.
