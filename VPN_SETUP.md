# Exit-IP strategy: Mullvad rotation + residential proxy

Cam sites (Chaturbate especially) rate-limit / Cloudflare-block a single exit IP
once it sends enough status/stream requests. At 600+ models you WILL get
flagged. This project gives you two complementary tools. **Both are optional and
off by default** — nothing here is hard-coded to one machine, so a fresh clone
runs fine without any of it.

| Tool | What it does | Config file (gitignored) |
|------|--------------|--------------------------|
| **Mullvad auto-rotation** | When a site rate-limits the current exit IP, switch the Mullvad location and retry on a fresh IP. | `vpn_config.json` |
| **Per-site residential proxy** | Route one site's traffic through a dedicated proxy IP, independent of the machine's VPN. | `site_proxies.json` |

They compose: a site listed in `site_proxies.json` uses that proxy; every other
site uses the machine's connection (your Mullvad VPN), which the rotator manages.

---

## 1. Mullvad auto-rotation

### Install the Mullvad CLI
Install the Mullvad app (https://mullvad.net/download). It ships a `mullvad` CLI.
The app **auto-detects** it — checked in this order:
1. `cli_path` in `vpn_config.json`
2. `MULLVAD_CLI` environment variable
3. your `PATH` (`mullvad` — this is the normal case)
4. common install dirs (`C:\Program Files\Mullvad VPN\resources\mullvad.exe`, `/usr/bin/mullvad`, ...)

Verify it works:
```
mullvad status
mullvad relay list          # shows the location codes you can rotate between
```

### Configure rotation
Copy the template and edit it (the real file is gitignored):
```
cp vpn_config.example.json vpn_config.json
```
```jsonc
{
  "enabled": true,
  "cli_path": null,                         // null = auto-detect; or "C:\\...\\mullvad.exe"
  "rotate_locations": ["nl","se","de","gb","ch","us"],  // mullvad codes; EMPTY = disabled
  "restart_threshold": 10,                  // TIER 1: events -> restart bots on the SAME IP
  "rotate_threshold": 25,                   // TIER 2: events -> rotate the Mullvad exit IP
  "ratelimit_window_sec": 120,
  "restart_cooldown_sec": 60,               // min seconds between same-IP restarts
  "rotate_cooldown_sec": 300,               // min seconds between rotations
  "connect_wait_sec": 40
}
```
- `rotate_locations` are Mullvad codes from `mullvad relay list` — a country
  (`nl`), or country + city (`"us nyc"`). The rotator cycles through them.
- Leave `rotate_locations` empty (or `enabled: false`) to turn rotation OFF.
- Override locations without editing the file via env:
  `STRMNTR_VPN_ROTATE="nl,se,de"`.

### How it triggers (TIERED — it does NOT rotate on every blip)
Each bot reports a rate-limited status (HTTP 429 / Cloudflare 403 →
`Status.RATELIMIT`) to the rotator. The LiveManager watchdog then escalates:
1. **Tier 1 — restart (same IP).** At `restart_threshold` events in the window it
   **wakes the site's bots** (clears their ratelimit backoff) so they re-poll
   immediately on the *current* IP — most rate-limits are transient and this
   fixes them with zero VPN disruption. Throttled by `restart_cooldown_sec`.
2. **Tier 2 — rotate (new IP).** Only if the rate-limits keep climbing to
   `rotate_threshold` (the restart didn't help) does it run
   `mullvad relay set location <next>` + `mullvad connect`, wait for `Connected`,
   and wake the bots onto the fresh IP. Throttled by `rotate_cooldown_sec`.

So a brief 429 spike just restarts the bots; a genuinely-flagged IP escalates to
a rotation.

On boot you'll see `[live] VPN auto-rotation armed: locations=[...]` (or
`not configured (no-op)`), and on each rotation
`[vpn] rotated Mullvad exit -> 'se' (connected) :: CB rate-limited`.

> Rotating Mullvad changes the exit for **all** machine traffic, so the other
> Mullvad-routed sites reconnect briefly. That's why high-volume sites can also
> use a dedicated proxy (below) to avoid rotating the whole VPN.

---

## 2. Per-site residential proxy

For a high-volume site you can pin a dedicated exit IP (a residential ISP proxy
won't get datacenter-flagged like a VPN exit). Copy the template:
```
cp site_proxies.example.json site_proxies.json   # if present; otherwise create it
```
```json
{ "CB": "http://USER:PASS@HOST:PORT" }
```
Keys are **site slugs** (`CB`, `SC`, `CS`, `SM`, `BC`, `C4`, `XLC`). Every bot of
that site sends its Cloudflare-gated requests through that proxy; the bulk video
**segments** still go direct (they aren't IP-bound), so a 100 Mbit proxy isn't a
bottleneck.

> Note: a single residential proxy can itself be rate-limited (HTTP 429) by 600+
> models of ajax. If you hit that, leave the site OFF the proxy and rely on
> Mullvad rotation instead, or use a higher-concurrency proxy plan.

---

## 3. Verifying your setup
```
mullvad status                       # CLI reachable + connected
python -c "import sys; sys.path.insert(0,'live_backend'); from streamonitor.utils import vpn_rotator as v; print('configured:', v.configured()); print(v.status_text().splitlines()[:2])"
```
`configured: True` means the CLI was found and `rotate_locations` is non-empty.

## Files
- `vpn_config.json` — your rotation config (gitignored)
- `vpn_config.example.json` — template (committed)
- `site_proxies.json` — your per-site proxies, with credentials (gitignored)
- `live_backend/streamonitor/utils/vpn_rotator.py` — the rotator
- `live_backend/streamonitor/utils/proxy_pool.py` — the per-site proxy resolver
