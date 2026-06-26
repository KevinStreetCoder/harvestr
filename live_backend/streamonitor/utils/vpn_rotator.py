# streamonitor/utils/vpn_rotator.py
# Optional Mullvad VPN auto-rotation on rate-limit.
#
# When a site (e.g. Chaturbate) rate-limits the current exit IP -- HTTP 429 /
# Cloudflare "Just a moment" 403 / repeated RATELIMIT status -- this rotates the
# Mullvad exit to the NEXT configured location and wakes the affected bots so
# they retry on a fresh IP. It pairs with the per-site residential proxy
# (proxy_pool / site_proxies.json): the proxy gives a stable clean IP, and VPN
# rotation is the fallback for when an exit (proxy OR Mullvad) gets rate-limited.
#
# NOTHING here is hard-coded to one machine. All configuration is optional:
#   * the `mullvad` CLI is AUTO-DETECTED (PATH, then common install dirs);
#     override with the MULLVAD_CLI env var or vpn_config.json "cli_path".
#   * rotation is DISABLED until you give it a list of locations, via either
#     vpn_config.json (gitignored -- copy vpn_config.example.json) or the
#     STRMNTR_VPN_ROTATE env var (comma-separated mullvad location codes, e.g.
#     "nl,se,de,gb,us-nyc").
#   * thresholds / cooldown are configurable with sane defaults.
# When unconfigured or the CLI is missing, EVERY function is a safe no-op, so the
# whole feature carries no regression risk for users who don't set it up.

import os
import json
import time
import shutil
import subprocess
import threading
from pathlib import Path
from collections import deque
from typing import Optional, List, Dict, Callable

_lock = threading.Lock()
_cfg: Optional[dict] = None
_cli: Optional[str] = None          # "" = looked, not found; None = not looked
_rotate_idx = 0
_last_rotate = 0.0
_last_restart: Dict[str, float] = {}
_grace_until = 0.0          # monotonic deadline; downloaders ride through the gap while now < this
_events: Dict[str, deque] = {}

_DEFAULTS = {
    "enabled": True,
    "cli_path": None,                # auto-detect when null
    "rotate_locations": [],          # e.g. ["nl", "se", "de", "gb"] -- empty = disabled
    # TIERED escalation -- don't rotate the whole VPN on every blip:
    "restart_threshold": 10,         # RATELIMIT events in window -> RESTART (wake) the bots on the SAME IP first
    "rotate_threshold": 25,          # RATELIMIT events in window -> the restart didn't help, ROTATE the exit IP
    "ratelimit_window_sec": 120,
    "restart_cooldown_sec": 60,      # min seconds between bot-restarts per site
    "rotate_cooldown_sec": 300,      # min seconds between rotations
    "connect_wait_sec": 40,          # wait for "Connected" after a rotation
}


def _project_root() -> Path:
    # vpn_rotator.py -> utils -> streamonitor -> live_backend -> <project root>
    return Path(__file__).resolve().parents[3]


def _load_cfg() -> dict:
    global _cfg
    if _cfg is not None:
        return _cfg
    with _lock:
        if _cfg is not None:
            return _cfg
        cfg = dict(_DEFAULTS)
        try:
            f = _project_root() / "vpn_config.json"
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            pass
        # env overrides (take precedence over the file)
        env = os.environ.get("STRMNTR_VPN_ROTATE", "")
        if env.strip():
            cfg["rotate_locations"] = [x.strip() for x in env.split(",") if x.strip()]
        for key, var in (("ratelimit_threshold", "STRMNTR_VPN_RL_THRESHOLD"),
                         ("rotate_cooldown_sec", "STRMNTR_VPN_COOLDOWN")):
            v = os.environ.get(var)
            if v:
                try:
                    cfg[key] = int(v)
                except Exception:
                    pass
        if not isinstance(cfg.get("rotate_locations"), list):
            cfg["rotate_locations"] = []
        _cfg = cfg
        return cfg


def reload() -> None:
    """Drop the cached config + CLI path so the next call re-reads
    vpn_config.json / env. Lets the webui apply rotation-setting changes live,
    no restart needed."""
    global _cfg, _cli
    with _lock:
        _cfg = None
        _cli = None


def _find_cli() -> Optional[str]:
    global _cli
    if _cli is not None:
        return _cli or None
    cfg = _load_cfg()
    cands: List[str] = []
    if cfg.get("cli_path"):
        cands.append(str(cfg["cli_path"]))
    if os.environ.get("MULLVAD_CLI"):
        cands.append(os.environ["MULLVAD_CLI"])
    w = shutil.which("mullvad")
    if w:
        cands.append(w)
    cands += [
        r"C:\Program Files\Mullvad VPN\resources\mullvad.exe",
        "/usr/bin/mullvad", "/usr/local/bin/mullvad", "/opt/homebrew/bin/mullvad",
    ]
    for c in cands:
        try:
            if c and os.path.exists(c):
                _cli = c
                return c
        except Exception:
            pass
    _cli = ""  # cache the "not found" result
    return None


def configured() -> bool:
    """Rotation is set up: enabled, has locations, and the CLI is present."""
    cfg = _load_cfg()
    return bool(cfg.get("enabled") and cfg.get("rotate_locations") and _find_cli())


def _run(args: List[str], timeout: int = 30):
    cli = _find_cli()
    if not cli:
        return None
    try:
        return subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def status_text() -> str:
    r = _run(["status"])
    return (r.stdout or "").strip() if r else ""


def in_grace() -> bool:
    """True while a rotation is dropping / re-establishing the Mullvad tunnel
    (plus a short settle). EVERY Mullvad-routed recording sees a network gap
    then. Downloaders check this to RIDE THROUGH the gap -- they skip their
    stall / no-data aborts so ffmpeg's own -reconnect + seg_max_retry resume the
    SAME file on the new IP, instead of the Python watchdog killing ffmpeg and
    the bot loop starting a brand-new file. Cheap (one monotonic compare) and a
    safe no-op when no rotation is happening."""
    return time.monotonic() < _grace_until


def _arm_grace(seconds: float) -> None:
    """Extend the ride-through window to at least `seconds` from now."""
    global _grace_until
    g = time.monotonic() + float(seconds)
    if g > _grace_until:
        _grace_until = g


def report_ratelimit(site: str) -> None:
    """A bot calls this when its status poll is rate-limited (RATELIMIT/429/403).
    Cheap no-op unless rotation is configured."""
    if not configured():
        return
    now = time.monotonic()
    win = _load_cfg()["ratelimit_window_sec"]
    with _lock:
        dq = _events.setdefault(site, deque())
        dq.append(now)
        while dq and now - dq[0] > win:
            dq.popleft()


def _count(site: str, now: float) -> int:
    dq = _events.get(site)
    if not dq:
        return 0
    win = _load_cfg()["ratelimit_window_sec"]
    while dq and now - dq[0] > win:
        dq.popleft()
    return len(dq)


def should_restart(site: str) -> bool:
    """TIER 1: enough rate-limits to RESTART (wake) the bots on the SAME exit IP
    -- cheap, no VPN disruption, often the rate-limit was transient. Returns
    False once it's time to rotate (tier 2 takes precedence)."""
    if not configured():
        return False
    cfg = _load_cfg()
    now = time.monotonic()
    with _lock:
        if now - _last_restart.get(site, 0.0) < cfg["restart_cooldown_sec"]:
            return False
        n = _count(site, now)
        # if we're already at the rotate threshold (and off cooldown), defer
        if n >= cfg["rotate_threshold"] and now - _last_rotate >= cfg["rotate_cooldown_sec"]:
            return False
        return n >= cfg["restart_threshold"]


def mark_restart(site: str) -> None:
    """Record a tier-1 restart. Events are deliberately NOT cleared, so if the
    restart doesn't help they keep accumulating toward the rotate threshold."""
    with _lock:
        _last_restart[site] = time.monotonic()


def should_rotate(site: str) -> bool:
    """TIER 2: the same-IP restart didn't help (events kept climbing) -- ROTATE
    the Mullvad exit IP."""
    if not configured():
        return False
    cfg = _load_cfg()
    now = time.monotonic()
    with _lock:
        if now - _last_rotate < cfg["rotate_cooldown_sec"]:
            return False
        return _count(site, now) >= cfg["rotate_threshold"]


def rotate(reason: str = "", log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Switch to the NEXT configured Mullvad location and reconnect. Returns the
    new location code, or None if unavailable/failed."""
    global _rotate_idx, _last_rotate
    if not configured():
        return None
    cfg = _load_cfg()
    locs = cfg["rotate_locations"]
    # Arm the ride-through grace BEFORE we drop the tunnel, so every in-flight
    # recording tolerates the whole disconnect->reconnect gap (cover the connect
    # wait + a settle buffer). Without this, the per-downloader stall watchdogs
    # kill ffmpeg mid-gap and the bot starts a new file.
    _arm_grace(int(cfg.get("connect_wait_sec", 40)) + 30)
    with _lock:
        loc = locs[_rotate_idx % len(locs)]
        _rotate_idx += 1
        _last_rotate = time.monotonic()
        _events.clear()
    # location may be "nl" or "nl ams" (country [city [host]])
    parts = str(loc).split()
    _run(["relay", "set", "location", *parts], timeout=30)
    _run(["connect"], timeout=15)
    deadline = time.time() + int(cfg.get("connect_wait_sec", 40))
    new_ok = False
    while time.time() < deadline:
        if "Connected" in status_text():
            new_ok = True
            break
        time.sleep(2)
    # Extend the grace a little past "Connected" so routing/DNS settle and
    # ffmpeg has reconnected before the stall detectors re-arm.
    _arm_grace(25)
    if log:
        try:
            log(f"[vpn] rotated Mullvad exit -> '{loc}' "
                f"({'connected' if new_ok else 'reconnecting'}){' :: ' + reason if reason else ''}")
        except Exception:
            pass
    return loc
