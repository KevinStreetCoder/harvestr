#!/usr/bin/env python3
"""
Web UI for Harvestr (universal video downloader).

Features:
  - Add/remove performers
  - Select which sites to probe (or all)
  - Start/stop background downloads
  - Live progress: running, completed, failed
  - View history.json / failed.json entries
  - Trigger dedup
  - Browse downloaded files
  - Tail log in real-time

Usage:
  python webui.py [--port 7860] [--host 127.0.0.1]

Then open http://127.0.0.1:7860 in a browser.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, jsonify, render_template_string, request, send_file
except ImportError:
    print("ERROR: Flask is required. Install with: pip install flask")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
HISTORY_PATH = DOWNLOADS_DIR / "history.json"
FAILED_PATH = DOWNLOADS_DIR / "failed.json"
LOG_PATH = DOWNLOADS_DIR / "universal.log"

app = Flask(__name__)

# ── Live recording manager (lazy — imports StreaMonitor if available) ────────
try:
    from live_recording import LiveManager as _LiveManager, available as _live_available
    _live = _LiveManager(downloads_dir=DOWNLOADS_DIR)
except Exception as _le:
    _live = None
    _live_available = False

# ── Disk manager ─────────────────────────────────────────────────────────────
try:
    from disk_manager import DiskManager as _DiskManager
    _disk = _DiskManager(downloads_dir=DOWNLOADS_DIR)
except Exception:
    _disk = None


# ── State shared with background task ────────────────────────────────────────
_state = {
    "running": False,
    "pid": None,
    "started_at": None,
    "current_performer": "",
    "last_output_line": "",
    "log_tail": deque(maxlen=500),
}
_state_lock = threading.Lock()
_runner_thread: subprocess.Popen | None = None


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"performers": [], "enabled_sites": []}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_json(path: Path, data: dict) -> None:
    """Atomic-rename write for JSON files. Shared by history/failed-reset
    endpoints so the downloader process doesn't see a half-written file."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                prefix=f"._{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise


def load_sites() -> list[str]:
    """Return a simple list of all site names (yt-dlp + custom scrapers)."""
    return [s["name"] for s in load_sites_detailed()]


# Sites that benefit from or require cookie auth — surfaced in the UI's
# auth panel with inline setup instructions.
AUTH_SITES: dict[str, dict] = {
    "recume": {
        "label": "Recu.me / Recurbate",
        "why": "Cloudflare-blocked without cookies. Free account = 5 plays/day, premium = unlimited + official downloads.",
        "cookies": ["cf_clearance", "PHPSESSID", "im18"],
        "signup_url": "https://recu.me/account/signup",
        "paid_url": "https://recu.me/account/subscribe",
    },
    "xcom": {
        "label": "X.com / Twitter",
        "why": "Premium X = 10x daily quota, full archive, long videos. Without auth you get at most 1k posts/day.",
        "cookies": ["auth_token", "ct0"],
        "signup_url": "https://x.com/i/flow/signup",
        "paid_url": "https://x.com/i/premium_sign_up",
    },
    "camwhores_tv": {
        "label": "camwhores.tv (private videos)",
        "why": "Private uploads need you to be a 'friend' of the uploader. Public videos work without login.",
        "cookies": ["phpsessid"],
        "signup_url": "https://www.camwhores.tv/signup/",
    },
    "camvault": {
        "label": "camvault.to",
        "why": "Premium = full downloads; free = 10-second previews.",
        "cookies": ["session"],
        "signup_url": "https://camvault.to/register",
        "paid_url": "https://camvault.to/premium",
    },
    "archivebate": {
        "label": "archivebate.com",
        "why": "Some archives require login for HD stream access.",
        "cookies": ["laravel_session"],
        "signup_url": "https://archivebate.com/",
    },
    "camsmut": {
        "label": "camsmut.com",
        "why": "Video pages return 404 without a logged-in session. Free account works (no premium tier needed).",
        "cookies": ["laravel_session", "camsmut_session"],
        "signup_url": "https://camsmut.com/register",
        "uses_credentials": True,
    },
}


def load_sites_detailed() -> list[dict]:
    """Return structured site metadata: name, category, backend, auth_info.

    Categories used by the UI picker:
      mainstream · adult · cam · mirror · archive
    """
    cat_override = {
        # Mirror sites that the downloader treats as the "creator mirror" bucket
        "coomer": "mirror",
        "kemono": "mirror",
        # Cam-archive sites (KVS mirror family, archivebates, recu.me)
        "camwhores_tv": "cam", "camwhores_video": "cam", "camwhores_co": "cam",
        "camwhoreshd": "cam", "camwhoresbay": "cam", "camwhoresbay_tv": "cam",
        "camwhores_bz": "cam", "camwhorescloud": "cam", "camvideos_tv": "cam",
        "camhub_cc": "cam", "camwh_com": "cam", "cambro_tv": "cam",
        "camcaps_tv": "cam", "camcaps_io": "cam", "camstreams_tv": "cam",
        "porntrex": "cam", "camsrip": "cam", "recordbate": "cam",
        "archivebate": "cam", "camvault": "cam", "recume": "cam",
    }

    out: list[dict] = []
    seen: set[str] = set()

    # yt-dlp sites from sites.json
    sites_json = SCRIPT_DIR / "sites.json"
    if sites_json.exists():
        try:
            data = json.loads(sites_json.read_text(encoding="utf-8"))
            for name, info in data.get("sites", {}).items():
                if name.startswith("_"):
                    continue
                seen.add(name)
                cat = cat_override.get(name, info.get("category", "mainstream"))
                out.append({
                    "name": name,
                    "category": cat,
                    "backend": "yt-dlp",
                    "notes": info.get("notes", ""),
                    "needs_auth": name in AUTH_SITES,
                    "auth_info": AUTH_SITES.get(name),
                })
        except Exception:
            pass

    # Custom scrapers
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from custom_scrapers import ALL_SCRAPER_CLASSES
        for cls in ALL_SCRAPER_CLASSES:
            if cls.NAME in seen:
                continue
            seen.add(cls.NAME)
            cat = cat_override.get(cls.NAME, getattr(cls, "CATEGORY", "adult"))
            has_cookie = bool(getattr(cls, "COOKIE_DOMAIN", "") or "")
            out.append({
                "name": cls.NAME,
                "category": cat,
                "backend": "custom",
                "notes": (cls.__doc__ or "").strip().split("\n")[0][:120],
                "needs_auth": cls.NAME in AUTH_SITES or has_cookie,
                "auth_info": AUTH_SITES.get(cls.NAME),
            })
    except Exception:
        pass

    out.sort(key=lambda s: (s["category"], s["name"]))
    return out


def read_progress() -> dict:
    """Read the live progress JSON written by the downloader."""
    path = DOWNLOADS_DIR / "_progress.json"
    if not path.exists():
        return {"session": {"running": False}, "active": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"session": {"running": False}, "active": []}


def cookies_diagnostics() -> dict:
    """Report, per auth-required site, whether usable cookies are loaded."""
    cfg = load_config()
    cookies_file = cfg.get("cookies_file") or ""
    report: dict[str, dict] = {}

    loaded_domains: dict[str, set[str]] = {}
    if cookies_file and Path(cookies_file).exists():
        try:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(cookies_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            for c in jar:
                dom = c.domain.lstrip(".")
                loaded_domains.setdefault(dom, set()).add(c.name)
        except Exception:
            pass

    def _match(dom_needle: str) -> set[str]:
        hits: set[str] = set()
        for d, names in loaded_domains.items():
            if dom_needle in d:
                hits |= names
        return hits

    domain_map = {
        "recume": "recu.me",
        "xcom": "x.com",
        "camwhores_tv": "camwhores.tv",
        "camvault": "camvault.to",
        "archivebate": "archivebate.com",
        "camsmut": "camsmut.com",
    }

    cfg_for_creds = cfg  # for scrapers that auth via username/password

    for key, info in AUTH_SITES.items():
        found = _match(domain_map.get(key, key))
        missing = [c for c in info["cookies"] if c.lower() not in {n.lower() for n in found}]
        status = "ok" if not missing else ("partial" if found else "none")
        # Sites authenticating via username/password (camsmut) — treat as OK
        # when credentials are present in config even if no cookies are loaded.
        if info.get("uses_credentials"):
            if key == "camsmut" and cfg.get("camsmut_username") and cfg.get("camsmut_password"):
                status = "ok"
                found = found | {"<username>", "<password>"}
                missing = []
        report[key] = {
            "label": info["label"],
            "cookies_required": info["cookies"],
            "cookies_found": sorted(found),
            "missing": missing,
            "status": status,
        }
    return {
        "cookies_file": cookies_file,
        "cookies_file_exists": bool(cookies_file and Path(cookies_file).exists()),
        "sites": report,
    }


# ── HTML UI ──────────────────────────────────────────────────────────────────
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Harvestr — video downloader</title>
<link rel="icon" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%23a0d7ff'/><stop offset='1' stop-color='%232a6cb3'/></linearGradient></defs><circle cx='32' cy='32' r='28' fill='%23181b22' stroke='url(%23g)' stroke-width='2'/><path d='M32 14 L32 40 M22 32 L32 42 L42 32' stroke='url(%23g)' stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/><path d='M18 48 L46 48' stroke='url(%23g)' stroke-width='3' stroke-linecap='round'/></svg>"/>
<style>
  :root {
    --bg: #0b0d12;
    --bg-2: #141821;
    --bg-3: #1c2230;
    --border: #262c3a;
    --border-2: #323a4d;
    --text: #e6e9ef;
    --text-2: #9aa4b8;
    --text-3: #6b7691;
    --accent: #5cb8ff;
    --accent-2: #2a6cb3;
    --good: #4ade80;
    --warn: #fbbf24;
    --bad: #f87171;
    --purple: #a78bfa;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font: 14px/1.55 "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(ellipse at top, #13182580 0%, var(--bg) 60%) fixed, var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  header {
    background: linear-gradient(180deg, #151a25, #10141c);
    padding: 12px 24px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 14px;
    position: sticky; top: 0; z-index: 500;
    backdrop-filter: blur(8px);
    /* overflow: visible is the default but the sticky + backdrop-filter
       still creates a stacking context that traps tooltips to within its
       own layer. Keep nothing here that clips (no overflow: hidden). */
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand svg { width: 30px; height: 30px; }
  .brand h1 { margin: 0; font-size: 19px; font-weight: 700; letter-spacing: -.3px;
              background: linear-gradient(135deg, #e6e9ef 10%, var(--accent) 90%);
              -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
  .brand .tagline { color: var(--text-3); font-size: 11.5px; letter-spacing: .3px; }
  .status-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 20px; background: #1a2030;
    border: 1px solid var(--border); font-size: 12px; color: var(--text-2);
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
  .status-dot.running { background: var(--good); box-shadow: 0 0 8px #4ade8080; animation: pulse 1.8s infinite; }
  .status-dot.error { background: var(--bad); }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.4;} }
  .container { max-width: 1480px; margin: 0 auto; padding: 20px 24px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: linear-gradient(180deg, var(--bg-2), #10141c);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 18px 20px; margin-bottom: 18px;
    box-shadow: 0 1px 3px #00000040, 0 0 0 1px #ffffff05 inset;
  }
  .card h2 {
    margin: 0 0 12px 0; font-size: 13px; font-weight: 600;
    color: var(--text-2); text-transform: uppercase; letter-spacing: .8px;
    display: flex; align-items: center; gap: 8px;
  }
  .card h2 .icon { width: 15px; height: 15px; color: var(--accent); flex-shrink: 0; }

  button {
    background: var(--bg-3); color: var(--text); border: 1px solid var(--border-2);
    padding: 7px 14px; border-radius: 7px; cursor: pointer;
    font-size: 13px; font-weight: 500; font-family: inherit;
    transition: all .15s; display: inline-flex; align-items: center; gap: 5px;
  }
  button:hover { background: #2a3248; border-color: #3d4662; }
  button.primary { background: linear-gradient(180deg, #3b8ce6, #2a6cb3);
                   border-color: #3b8ce6; color: white; font-weight: 600; }
  button.primary:hover { background: linear-gradient(180deg, #4c9bf5, #3478c0); }
  button.danger { background: linear-gradient(180deg, #e65252, #a8381b);
                  border-color: #e65252; color: white; font-weight: 600; }
  button.danger:hover { background: linear-gradient(180deg, #f56363, #c04830); }
  button.success { background: linear-gradient(180deg, #48d37c, #2ea85c);
                   border-color: #48d37c; color: white; }
  button.warn { background: linear-gradient(180deg, #f0a53a, #c6781b);
                border-color: #f0a53a; color: #1a1206; font-weight: 600; }
  button.warn:hover { background: linear-gradient(180deg, #ffb84f, #d78825); }
  button.ghost { background: transparent; }
  button.ghost:hover { background: var(--bg-3); }
  button:disabled { opacity: 0.35; cursor: not-allowed; }
  button.xs { padding: 3px 8px; font-size: 11px; border-radius: 5px; }

  input[type="text"], input[type="password"], textarea, select {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 7px;
    padding: 8px 10px; font-size: 13px; font-family: inherit;
    transition: border-color .15s;
  }
  input:focus, textarea:focus, select:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px #5cb8ff20;
  }
  textarea { resize: vertical; min-height: 100px; }

  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
  th { background: #10141c; font-weight: 600; color: var(--text-2); position: sticky; top: 0;
       text-transform: uppercase; font-size: 11px; letter-spacing: .5px; }
  tbody tr { transition: background .12s; }
  tbody tr:hover { background: #1a2030; }
  td.mono { font-family: "JetBrains Mono", Consolas, monospace; font-size: 11.5px; color: var(--text-2); }

  .log-viewer {
    background: #06070c; color: #c8d0da; border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px; height: 320px; overflow: auto;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11.5px; white-space: pre-wrap; line-height: 1.45;
  }
  .log-viewer .INFO { color: #c8d0da; }
  .log-viewer .WARN, .log-viewer .WARNING { color: var(--warn); }
  .log-viewer .ERROR { color: var(--bad); }
  .log-viewer .DEBUG { color: var(--text-3); }

  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
    background: var(--bg-3); color: var(--text-2); border: 1px solid var(--border-2);
    font-weight: 500;
  }
  .pill.ok { background: #103d1d; color: #6dea8c; border-color: #225a2d; }
  .pill.fail { background: #3d1010; color: #ea6d6d; border-color: #5a2225; }
  .pill.private { background: #3d3010; color: #eac96d; border-color: #5a4522; }
  .pill.info { background: #0f2941; color: #6db7ea; border-color: #1e4773; }
  .pill.custom { background: #2b1a4d; color: #b79cff; border-color: #432a70; }
  .pill.ytdlp { background: #1a3a4d; color: #6fcdef; border-color: #285573; }

  .flex { display: flex; gap: 10px; align-items: center; }
  .mb { margin-bottom: 12px; }

  /* Add-row: one line, flex-wrap on narrow screens, input grows */
  .add-row {
    display: flex; gap: 8px; align-items: stretch;
    flex-wrap: wrap; margin-bottom: 12px;
  }
  .add-row > input[type="text"],
  .add-row > select { flex: 1 1 200px; min-width: 0; }
  .add-row > button { flex: 0 0 auto; white-space: nowrap; }
  /* Primary buttons are wider so they feel distinct */
  .add-row > button.primary { min-width: 86px; justify-content: center; }
  .add-row > button.ghost   { min-width: 72px; justify-content: center; }
  .add-row svg { flex-shrink: 0; }
  @media (max-width: 540px) {
    .add-row > input, .add-row > select, .add-row > button { flex: 1 1 100%; }
  }

  .perf-row {
    display: flex; align-items: center; gap: 10px; padding: 9px 12px;
    border: 1px solid transparent; border-radius: 7px;
    margin-bottom: 4px; cursor: pointer; transition: all .12s;
  }
  .perf-row:hover { background: var(--bg-3); border-color: var(--border); }
  .perf-row.selected { background: linear-gradient(90deg, #2a6cb340, #2a6cb310);
                        border-color: var(--accent-2); }
  .perf-row .name { font-weight: 500; }
  .perf-row .count { margin-left: auto; color: var(--text-3); font-size: 11.5px; }
  .perf-list { max-height: 420px; overflow-y: auto; padding-right: 4px; }

  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
  .stat-box {
    background: linear-gradient(180deg, var(--bg-2), #10141c);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; text-align: left; position: relative; overflow: hidden;
  }
  .stat-box::before {
    content: ''; position: absolute; left: 0; top: 0; width: 3px; height: 100%;
    background: var(--accent);
  }
  .stat-box .value { font-size: 22px; font-weight: 700; color: var(--text); }
  .stat-box .label { font-size: 11px; color: var(--text-3); margin-top: 2px;
                     text-transform: uppercase; letter-spacing: .5px; }
  .stat-box.good::before { background: var(--good); }
  .stat-box.good .value { color: var(--good); }
  .stat-box.bad::before { background: var(--bad); }
  .stat-box.bad .value { color: var(--bad); }
  .stat-box.warn::before { background: var(--warn); }

  /* Site picker */
  .site-tabs { display: flex; gap: 2px; background: var(--bg); padding: 3px;
               border-radius: 8px; border: 1px solid var(--border); margin-bottom: 12px; }
  .site-tabs .tab {
    flex: 1; padding: 6px 10px; text-align: center; font-size: 12px;
    border-radius: 5px; cursor: pointer; color: var(--text-2); transition: all .12s;
  }
  .site-tabs .tab.active { background: var(--bg-3); color: var(--accent); font-weight: 600; }
  .site-tabs .tab:hover:not(.active) { background: #1a2030; }
  .site-list { max-height: 300px; overflow-y: auto; padding: 2px;
               background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .site-row {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 10px; border-radius: 5px;
    cursor: pointer; font-size: 12.5px; transition: background .1s;
  }
  .site-row:hover { background: #1a2030; }
  .site-row input { margin: 0; accent-color: var(--accent); width: 14px; height: 14px; }
  .site-row .site-name { font-weight: 500; flex: 1; }
  .site-row .site-badge { font-size: 10px; padding: 1px 6px; border-radius: 8px;
                          background: var(--bg-3); color: var(--text-3); }
  .site-row .auth-icon {
    width: 12px; height: 12px; color: var(--warn);
    display: inline-flex; align-items: center; position: relative;
  }
  .site-row .auth-icon[data-status="ok"] { color: var(--good); }
  .site-row .auth-icon[data-status="partial"] { color: var(--warn); }
  .site-row .auth-icon[data-status="none"] { color: var(--text-3); }

  /* Progress bar */
  .progress-card { padding: 16px 20px; background: linear-gradient(180deg, #0e2036, #0b1726);
                    border: 1px solid #1e4773; }
  .progress-card h2 { color: var(--accent); }
  .dl-active { margin-bottom: 10px; padding: 10px 12px;
                background: #0b1320; border: 1px solid #1c2438; border-radius: 8px;
                transition: border-color .18s, background .18s, opacity .18s; }
  .dl-active:hover { border-color: #284a77; }
  .dl-active.cancelling { opacity: 0.55; border-color: #5b2a2a; background: #1a0f14; }
  .dl-active .top { display: flex; align-items: center; gap: 10px; font-size: 12.5px; margin-bottom: 6px; }
  .dl-active .top .title { flex: 1; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dl-active .top .meta { color: var(--text-3); font-size: 11.5px; font-family: "JetBrains Mono", monospace; }
  .dl-cancel {
    display: inline-flex; align-items: center; justify-content: center;
    width: 24px; height: 24px; padding: 0; margin: 0;
    background: transparent; color: var(--text-3);
    border: 1px solid #2a3244; border-radius: 6px;
    cursor: pointer; font-size: 13px; line-height: 1;
    transition: all .15s ease; flex-shrink: 0;
  }
  .dl-cancel:hover {
    background: #3d1420; color: #ff6b7d; border-color: #7a2b3b;
    transform: scale(1.05);
  }
  .dl-cancel:active { transform: scale(0.95); }
  .dl-cancel:disabled { opacity: .5; cursor: not-allowed; transform: none; }
  .dl-cancel svg { width: 12px; height: 12px; }
  .progress-bar {
    height: 8px; background: #0a0e18; border-radius: 4px; overflow: hidden;
    position: relative; border: 1px solid #1c2438;
  }
  .progress-bar .fill {
    height: 100%; width: 0;
    background: linear-gradient(90deg, var(--accent-2), var(--accent), var(--purple));
    background-size: 200% 100%; animation: shimmer 2.5s linear infinite;
    transition: width .3s;
  }
  @keyframes shimmer { 0%{background-position:0% 0;} 100%{background-position:200% 0;} }
  .dl-empty { color: var(--text-3); font-size: 12.5px; padding: 12px; text-align: center; }

  .phase-panel {
    background: #0b1320; border: 1px solid #1c2438; border-radius: 8px;
    padding: 10px 12px; margin-bottom: 10px;
  }
  .phase-panel.done { background: #0b2318; border-color: #1d4d31; }
  .phase-panel .phase-line {
    display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px;
  }
  .phase-panel .phase-label {
    font-size: 12.5px; font-weight: 600; color: var(--accent);
  }
  .phase-panel.done .phase-label { color: var(--good); }
  .phase-panel .phase-meta {
    margin-left: auto; font-size: 11.5px; color: var(--text-3);
    font-family: "JetBrains Mono", Consolas, monospace;
  }
  .phase-panel code {
    background: var(--bg-3); padding: 1px 5px; border-radius: 3px;
    font-size: 10.5px; color: var(--accent);
  }

  .hits-row {
    padding: 8px 12px; background: #0a1320; border: 1px solid #1c2438;
    border-radius: 8px; margin-bottom: 10px; line-height: 1.9;
    font-size: 12px;
  }
  .hits-row .hits-label {
    color: var(--text-3); margin-right: 6px; font-weight: 500;
  }
  .pill.hit-pill {
    background: #103d1d; color: #6dea8c; border-color: #225a2d;
  }
  .hit-pill-link {
    display: inline-flex; align-items: center; gap: 5px;
    text-decoration: none;
    transition: background .14s, color .14s, border-color .14s, transform .14s;
    cursor: pointer;
  }
  .hit-pill-link:hover {
    background: #1b5a2e; color: #9cff9c; border-color: #2f7d42;
    transform: translateY(-1px);
    box-shadow: 0 3px 8px #0f3b1f60;
  }
  .hit-pill-link .ext-icon {
    width: 10px; height: 10px; opacity: 0.7; flex-shrink: 0;
    margin-left: 1px; transition: opacity .14s;
  }
  .hit-pill-link:hover .ext-icon { opacity: 1; }

  /* Clickable video title in active download row */
  .dl-active .top .title.clickable {
    cursor: pointer;
    transition: color .15s, text-decoration-color .15s;
    text-decoration: underline dashed transparent;
    text-underline-offset: 3px;
  }
  .dl-active .top .title.clickable:hover {
    color: var(--accent);
    text-decoration-color: var(--accent);
  }

  /* Tooltips — use max z-index + escape via filter so cards can't clip */
  [data-tip] { position: relative; }
  [data-tip]:hover::after,
  [data-tip]:focus-visible::after {
    content: attr(data-tip);
    position: absolute;
    z-index: 9999;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: #0a0e18;
    color: var(--text);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 11.5px;
    line-height: 1.3;
    white-space: nowrap;
    border: 1px solid var(--border-2);
    box-shadow: 0 4px 12px #00000080;
    pointer-events: none;
    opacity: 0;
    animation: tip-in .14s ease-out forwards;
    animation-delay: .35s;
  }
  [data-tip]:hover::before,
  [data-tip]:focus-visible::before {
    content: '';
    position: absolute;
    z-index: 9999;
    bottom: calc(100% + 2px);
    left: 50%;
    transform: translateX(-50%);
    border: 4px solid transparent;
    border-top-color: var(--border-2);
    pointer-events: none;
    opacity: 0;
    animation: tip-in .14s ease-out forwards;
    animation-delay: .35s;
  }
  @keyframes tip-in { from { opacity: 0; transform: translateX(-50%) translateY(2px); }
                        to   { opacity: 1; transform: translateX(-50%) translateY(0); } }
  @keyframes tip-in-down { from { opacity: 0; transform: translateX(-50%) translateY(-2px); }
                            to   { opacity: 1; transform: translateX(-50%) translateY(0); } }

  /* Tooltips on header buttons: flip to BELOW the element, since going
     above would put them off the top of the viewport (header is sticky-top-0).
     Also applies to any element explicitly marked data-tip-pos="bottom". */
  header [data-tip]:hover::after,
  header [data-tip]:focus-visible::after,
  [data-tip-pos="bottom"][data-tip]:hover::after,
  [data-tip-pos="bottom"][data-tip]:focus-visible::after {
    bottom: auto;
    top: calc(100% + 6px);
    animation-name: tip-in-down;
  }
  header [data-tip]:hover::before,
  header [data-tip]:focus-visible::before,
  [data-tip-pos="bottom"][data-tip]:hover::before,
  [data-tip-pos="bottom"][data-tip]:focus-visible::before {
    bottom: auto;
    top: calc(100% + 2px);
    border-top-color: transparent;
    border-bottom-color: var(--border-2);
    animation-name: tip-in-down;
  }

  /* Cards need visible overflow so tooltips can escape — but inner panels
     with internal scroll areas keep their overflow for real content. */
  .card { overflow: visible; }

  /* Config */
  .config-table { font-size: 13px; }
  .config-table td { padding: 6px 4px; border: none; }
  .config-table td:first-child { color: var(--text-3); width: 42%; font-size: 12.5px; }
  .config-table td:last-child { padding-left: 10px; }
  .config-table tr:not(:last-child) td { border-bottom: 1px solid var(--border); }

  /* Auth panel */
  .auth-site { padding: 10px 12px; border: 1px solid var(--border);
                border-radius: 8px; margin-bottom: 8px; background: var(--bg); }
  .auth-site .header { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
  .auth-site .header .title { font-weight: 600; flex: 1; }
  .auth-site .header .status {
    font-size: 10.5px; padding: 2px 8px; border-radius: 8px;
  }
  .auth-site .status.ok { background: #103d1d; color: #6dea8c; }
  .auth-site .status.partial { background: #3d3010; color: #eac96d; }
  .auth-site .status.none { background: #3d1010; color: #ea6d6d; }
  .auth-site .why { color: var(--text-3); font-size: 12px; margin-bottom: 6px; }
  .auth-site .cookies { font-size: 11.5px; color: var(--text-2); }
  .auth-site .cookies code { background: var(--bg-3); padding: 1px 5px;
                              border-radius: 3px; font-size: 11px; color: var(--accent); }
  .auth-site details { margin-top: 6px; }
  .auth-site details summary { cursor: pointer; color: var(--accent);
                                font-size: 12px; padding: 3px 0; }

  /* Toasts — stacking container */
  #toast-stack {
    position: fixed; top: 74px; right: 24px; z-index: 200;
    display: flex; flex-direction: column; gap: 8px; max-width: min(420px, 90vw);
    pointer-events: none;
  }
  #toast-stack .toast-item {
    padding: 12px 16px;
    background: linear-gradient(180deg, #2f8ae0, #2a6cb3);
    color: white; border-radius: 8px; font-size: 13px; font-weight: 500;
    box-shadow: 0 6px 20px #00000080;
    border: 1px solid #3b8ce6;
    animation: toast-in .28s cubic-bezier(.2,.9,.3,1.4);
    pointer-events: auto;
    display: flex; align-items: center; gap: 10px;
  }
  #toast-stack .toast-item.hide { animation: toast-out .25s forwards; }
  #toast-stack .toast-item.error { background: linear-gradient(180deg, #e85656, #a8381b); border-color: #e85656; }
  #toast-stack .toast-item.success { background: linear-gradient(180deg, #34c26e, #1b7d3b); border-color: #34c26e; }
  #toast-stack .toast-item.info { background: linear-gradient(180deg, #5a5e7e, #323546); border-color: #525879; }
  #toast-stack .toast-item .icon-small { flex-shrink: 0; width: 15px; height: 15px; opacity: .9; }
  @keyframes toast-in {
    0%   { opacity: 0; transform: translateY(-14px) scale(.92); }
    100% { opacity: 1; transform: translateY(0) scale(1); }
  }
  @keyframes toast-out {
    0%   { opacity: 1; transform: translateX(0); }
    100% { opacity: 0; transform: translateX(40px); }
  }
  /* Back-compat (old .toast if still referenced) */
  .toast { display: none; }

  /* Modal backdrop blur */
  .modal-backdrop { backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); }
  .modal-backdrop.show { animation: modal-in .2s ease-out; }
  @keyframes modal-in { from { opacity: 0; } to { opacity: 1; } }
  .modal-card { animation: card-in .24s cubic-bezier(.2,.9,.3,1.2); }
  @keyframes card-in { from { opacity: 0; transform: translateY(-10px) scale(.97); }
                         to { opacity: 1; transform: translateY(0) scale(1); } }

  /* Scrollbar polish (Webkit) */
  *::-webkit-scrollbar { width: 10px; height: 10px; }
  *::-webkit-scrollbar-track { background: transparent; }
  *::-webkit-scrollbar-thumb { background: var(--border-2); border-radius: 5px;
                                border: 2px solid transparent; background-clip: padding-box; }
  *::-webkit-scrollbar-thumb:hover { background: #4d5570;
                                      border: 2px solid transparent; background-clip: padding-box; }

  /* Professional confirm / prompt dialog */
  .modal-card.dialog {
    width: min(440px, 92vw); padding: 0; overflow: hidden;
    background: var(--bg-2); border: 1px solid var(--border-2);
    box-shadow: 0 24px 60px #00000090;
  }
  .dialog-head { padding: 18px 20px 4px; }
  .dialog-head .icon-wrap {
    width: 38px; height: 38px; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    background: var(--bg-3); margin-bottom: 10px;
  }
  .dialog-head .icon-wrap svg { width: 20px; height: 20px; }
  .dialog-head.danger  .icon-wrap { background: #3d1010; color: var(--bad); }
  .dialog-head.warn    .icon-wrap { background: #3d3010; color: var(--warn); }
  .dialog-head.info    .icon-wrap { background: #0f2941; color: var(--accent); }
  .dialog-head h3 { margin: 0 0 6px; font-size: 16px; font-weight: 700; }
  .dialog-head .msg { margin: 0; font-size: 13.5px; line-height: 1.55; color: var(--text-2); }
  .dialog-head .msg code { background: var(--bg-3); padding: 1px 6px; border-radius: 3px;
                            color: var(--accent); font-size: 12px; }
  .dialog-input-wrap { padding: 14px 20px 4px; }
  .dialog-input-wrap input {
    width: 100%; padding: 10px 12px; font-size: 14px;
    font-family: "JetBrains Mono", Consolas, monospace;
  }
  .dialog-actions {
    padding: 14px 20px 16px; display: flex; justify-content: flex-end; gap: 8px;
  }
  .dialog-actions button { min-width: 88px; justify-content: center; }

  /* Bulk-add modal */
  .modal-card.bulkadd {
    width: min(640px, 94vw); padding: 0; overflow: hidden;
    background: var(--bg-2); border: 1px solid var(--border-2);
    box-shadow: 0 24px 60px #00000080;
  }
  .bulkadd-head {
    padding: 18px 20px 12px; display: flex; align-items: flex-start; gap: 12px;
    border-bottom: 1px solid var(--border);
  }
  .bulkadd-head h3 { margin: 0; font-size: 16px; font-weight: 700; }
  .bulkadd-head p { margin: 4px 0 0; font-size: 12.5px; }
  .modal-card.bulkadd textarea {
    width: 100%; min-height: 220px; resize: vertical;
    background: var(--bg); color: var(--text); border: none; outline: none;
    padding: 14px 20px; font: 13px "JetBrains Mono", Consolas, monospace;
    border-bottom: 1px solid var(--border); border-radius: 0;
  }
  .bulkadd-actions {
    padding: 12px 20px; display: flex; align-items: center; gap: 8px;
  }

  /* Live settings modal */
  .modal-card.livesettings-card {
    width: min(640px, 94vw); padding: 0; overflow: hidden;
    background: var(--bg-2); border: 1px solid var(--border-2);
    box-shadow: 0 24px 60px #00000090;
    display: flex; flex-direction: column;
    max-height: 88vh;
  }
  .livesettings-head {
    padding: 18px 22px 14px; display: flex; align-items: flex-start; gap: 12px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, #10182a, var(--bg-2));
  }
  .livesettings-head h3 {
    margin: 0; font-size: 16.5px; font-weight: 700;
    display: inline-flex; align-items: center; gap: 8px;
  }
  .livesettings-head p { margin: 4px 0 0; font-size: 12.5px; }
  .livesettings-body {
    padding: 8px 22px 18px; overflow-y: auto; flex: 1;
  }
  .ls-section {
    padding: 14px 0; border-bottom: 1px solid var(--border);
  }
  .ls-section:last-child { border-bottom: none; }
  .ls-section-title {
    font-size: 10.5px; color: var(--text-3); font-weight: 600;
    letter-spacing: 1px; text-transform: uppercase; margin-bottom: 10px;
  }
  .ls-row {
    display: grid; grid-template-columns: 1fr auto;
    align-items: center; gap: 12px; padding: 6px 0;
    min-height: 32px;
  }
  .ls-row label {
    font-size: 13px; color: var(--text); font-weight: 500;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .ls-row .ls-unit {
    color: var(--text-3); font-size: 11px; font-weight: 400;
    font-family: "JetBrains Mono", monospace;
    padding: 1px 6px; background: var(--bg-3); border-radius: 4px;
  }
  .ls-row input[type="number"] {
    width: 130px; text-align: right;
    background: var(--bg); border: 1px solid var(--border-2); color: var(--text);
    padding: 6px 10px; border-radius: 6px; font-size: 13px;
    font-family: "JetBrains Mono", monospace;
    transition: border-color .15s;
  }
  .ls-row input[type="number"]:focus {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 2px #5cb8ff30;
  }
  .ls-row.switch-row { grid-template-columns: 1fr auto; }

  /* iOS-style toggle */
  .switch {
    position: relative; display: inline-block; width: 40px; height: 22px;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch .slider {
    position: absolute; cursor: pointer; inset: 0;
    background: var(--bg-3); border: 1px solid var(--border-2);
    transition: .2s; border-radius: 22px;
  }
  .switch .slider::before {
    content: ''; position: absolute; height: 16px; width: 16px;
    left: 2px; bottom: 2px; background: var(--text-3);
    transition: .2s; border-radius: 50%;
  }
  .switch input:checked + .slider {
    background: linear-gradient(180deg, #3b8ce6, #2a6cb3);
    border-color: var(--accent);
  }
  .switch input:checked + .slider::before {
    transform: translateX(17px); background: #fff;
  }
  .livesettings-actions {
    padding: 12px 22px; display: flex; align-items: center; gap: 8px;
    border-top: 1px solid var(--border); background: var(--bg);
  }
  .livesettings-actions button { min-width: 110px; }
  .livesettings-actions button.primary { margin-left: auto; }
  .file-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px; background: var(--bg-3); color: var(--text);
    border: 1px solid var(--border-2); border-radius: 7px; cursor: pointer;
    font-size: 13px; font-weight: 500; transition: all .15s;
  }
  .file-btn:hover { background: #2a3248; border-color: #3d4662; }

  /* Skeleton loaders */
  .skel {
    background: linear-gradient(90deg, var(--bg-3) 25%, #252a38 50%, var(--bg-3) 75%);
    background-size: 200% 100%;
    animation: skel 1.4s ease-in-out infinite;
    border-radius: 4px;
  }
  @keyframes skel { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

  .muted { color: var(--text-3); font-size: 11.5px; }
  .clickable { cursor: pointer; }

  /* Video preview modal */
  .modal-backdrop {
    position: fixed; inset: 0; background: #000000b0;
    display: none; align-items: center; justify-content: center; z-index: 300;
  }
  .modal-backdrop.show { display: flex; }
  .modal-card {
    background: var(--bg-2); border: 1px solid var(--border); border-radius: 12px;
    max-width: 90vw; max-height: 90vh; padding: 14px;
  }
  .modal-card video { max-width: 85vw; max-height: 75vh; border-radius: 8px; }
  .modal-card .top { display: flex; align-items: center; margin-bottom: 10px; }
  .modal-card .top .title { flex: 1; font-weight: 600; margin-right: 10px; }

  /* Filter chip row */
  .filter-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .filter-row input, .filter-row select { width: auto; min-width: 120px; }
  .filter-row .chip {
    padding: 4px 10px; border-radius: 20px; background: var(--bg-3);
    border: 1px solid var(--border-2); font-size: 11.5px; cursor: pointer;
    color: var(--text-2); transition: all .12s;
  }
  .filter-row .chip:hover { border-color: var(--accent); color: var(--accent); }
  .filter-row .chip.active { background: var(--accent-2); color: white; border-color: var(--accent); }

  /* Tab bar (top navigation) */
  .tab-bar { display: flex; gap: 2px; background: var(--bg); padding: 3px;
             border-radius: 9px; border: 1px solid var(--border); margin: 0 8px; }
  .tab-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 14px; background: transparent; border: 1px solid transparent;
    color: var(--text-2); cursor: pointer; font-size: 13px; font-weight: 500;
    border-radius: 6px; transition: all .15s; position: relative;
  }
  .tab-btn .icon { width: 14px; height: 14px; }
  .tab-btn:hover { background: var(--bg-3); color: var(--text); }
  .tab-btn.active { background: var(--bg-3); color: var(--accent);
                    border-color: var(--border-2); font-weight: 600; }
  .tab-btn .tab-badge {
    padding: 1px 7px; border-radius: 10px; font-size: 11px;
    background: var(--good); color: #06121a; font-weight: 700; line-height: 1.2;
    margin-left: 2px;
  }

  /* Pages */
  .page { display: block; }
  .page[hidden] { display: none !important; }

  /* Live model grid */
  .live-grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  }
  .live-card {
    background: linear-gradient(180deg, var(--bg-2), #10141c);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; transition: border-color .15s, transform .15s;
    position: relative; overflow: hidden;
  }
  .live-card:hover { border-color: var(--border-2); transform: translateY(-1px); }
  .live-card.recording {
    border-color: var(--good);
    box-shadow: 0 0 0 1px var(--good), 0 0 12px #4ade8030;
  }
  .live-card.recording::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, transparent, var(--good), transparent);
    animation: scan 2s linear infinite;
  }
  @keyframes scan {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(100%); }
  }
  .live-card .top { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
  .live-card .username { font-weight: 700; font-size: 15px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .live-card .site-chip { font-size: 10.5px; padding: 1px 6px; border-radius: 4px;
                          background: var(--bg-3); color: var(--text-2); font-family: "JetBrains Mono", monospace; }
  .live-card .status-row {
    display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
    font-size: 12px;
  }
  .live-card .state-dot {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  }
  .live-card .state-dot.good     { background: var(--good);  box-shadow: 0 0 6px #4ade80; animation: pulse-dot 1.5s ease-in-out infinite; }
  .live-card .state-dot.accent   { background: var(--accent); box-shadow: 0 0 6px #5cb8ff; }
  .live-card .state-dot.purple   { background: var(--purple); box-shadow: 0 0 6px #a78bfa; }
  .live-card .state-dot.warn     { background: var(--warn);   box-shadow: 0 0 6px #fbbf2450; }
  .live-card .state-dot.bad      { background: var(--bad);    box-shadow: 0 0 6px #f87171; }
  .live-card .state-dot.text-3   { background: #3a4253; }
  @keyframes pulse-dot { 0%,100%{opacity:1;} 50%{opacity:.45;} }
  .live-card .state-label { font-weight: 500; }
  .live-card .state-label.good { color: var(--good); }
  .live-card .state-label.accent { color: var(--accent); }
  .live-card .state-label.purple { color: var(--purple); }
  .live-card .state-label.warn { color: var(--warn); }
  .live-card .state-label.bad { color: var(--bad); }
  .live-card .state-label.text-3 { color: var(--text-3); }
  .live-card .meta { font-size: 11.5px; color: var(--text-3); font-family: "JetBrains Mono", monospace; }
  .live-card .actions { display: flex; gap: 6px; margin-top: 12px; }
  .live-card .actions button { flex: 1; padding: 6px 8px; }
  .live-card .actions button.icon-only { flex: 0 0 34px; padding: 6px; }
  .live-card .actions button svg { width: 12px; height: 12px; }

  /* Thumbnail header — shown when the site returned an avatar/preview */
  .live-card .hero {
    margin: -14px -16px 10px -16px;
    height: 90px;
    background-size: cover; background-position: center;
    background-color: #0e1421;
    position: relative; overflow: hidden;
  }
  .live-card .hero::after {
    content: ''; position: absolute; inset: 0;
    background: linear-gradient(180deg, transparent 30%, #10141c 100%);
  }
  .live-card .hero.small { height: 52px; }

  /* Badges row under status */
  .live-card .badges {
    display: flex; flex-wrap: wrap; gap: 4px 5px; margin-bottom: 8px;
  }
  .live-card .badge {
    font-size: 10.5px; padding: 1px 7px; border-radius: 11px;
    background: #121826; color: var(--text-2); border: 1px solid var(--border);
    display: inline-flex; align-items: center; gap: 3px; line-height: 1.5;
  }
  .live-card .badge.country { background: #0e1e2e; color: #7fd0ff; }
  .live-card .badge.age { background: #1e1830; color: #c8a8ff; }
  .live-card .badge.language { background: #11241e; color: #5ee2a6; }
  .live-card .badge.viewers { background: #2a1211; color: #ff9595; }
  .live-card .badge.duration { background: #13171f; color: #ffc878; }
  .live-card .badge .mini-dot {
    width: 5px; height: 5px; border-radius: 50%; display: inline-block;
  }
  .live-card .tags {
    display: flex; flex-wrap: wrap; gap: 3px; margin: 0 0 8px 0;
    font-size: 10.5px; line-height: 1.4;
  }
  .live-card .tag-chip {
    background: #0b1322; border: 1px solid #1c2438;
    color: #7aa4cf; padding: 1px 6px; border-radius: 10px;
  }

  /* Freq/history row */
  .live-card .freq-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 4px 10px; font-size: 11px; margin-bottom: 10px;
    padding: 7px 9px; background: #0b1320; border-radius: 6px;
    border: 1px solid #141c2d;
  }
  .live-card .freq-grid .k {
    color: var(--text-3); font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: .3px;
  }
  .live-card .freq-grid .v {
    color: var(--text); text-align: right;
    font-family: "JetBrains Mono", monospace; font-size: 11px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .live-card .freq-grid .v.good    { color: var(--good); }
  .live-card .freq-grid .v.accent  { color: var(--accent); }
  .live-card .freq-grid .v.warn    { color: var(--warn); }

  .live-card.error-state { border-color: #6b2a2a; }
  .live-card.paused-state { border-color: #5a4a1e; }
  .live-card.paused-state::after {
    content: 'PAUSED'; position: absolute; top: 10px; right: 12px;
    font-size: 9.5px; font-weight: 700; letter-spacing: 1px;
    color: var(--warn); background: #1f1508; padding: 2px 7px;
    border-radius: 4px; border: 1px solid #5a4a1e;
  }
  .live-card.live-now {
    background: linear-gradient(180deg, #0d2916, #10141c);
    border-color: #2f7d42;
  }
  .live-card.live-now::after {
    content: '● LIVE'; position: absolute; top: 10px; right: 12px;
    font-size: 10px; font-weight: 700; letter-spacing: .5px;
    color: #5ee2a6; background: #072416; padding: 2px 8px;
    border-radius: 4px; border: 1px solid #145a3b;
    animation: pulse-dot 1.5s ease-in-out infinite;
  }

  /* Stat-box accent variant */
  .stat-box.accent::before { background: var(--accent); }
  .stat-box.accent .value { color: var(--accent); }

  /* Command palette */
  .modal-card.palette {
    width: min(560px, 90vw); padding: 0; overflow: hidden;
    background: var(--bg-2); border: 1px solid var(--border-2);
    box-shadow: 0 20px 60px #00000080;
  }
  .modal-card.palette input {
    border: none; background: transparent; color: var(--text);
    padding: 16px 18px; font-size: 14px; width: 100%;
    border-bottom: 1px solid var(--border);
  }
  .modal-card.palette input:focus { outline: none; box-shadow: none; border-bottom-color: var(--accent); }
  #palette-list { max-height: 340px; overflow-y: auto; padding: 4px; }
  #palette-list .pcmd {
    padding: 9px 12px; border-radius: 6px; cursor: pointer;
    display: flex; align-items: center; gap: 10px; font-size: 13px;
    color: var(--text);
  }
  #palette-list .pcmd:hover,
  #palette-list .pcmd.selected { background: var(--bg-3); }
  #palette-list .pcmd .kbd {
    margin-left: auto; font-size: 11px; color: var(--text-3);
    padding: 1px 6px; border: 1px solid var(--border-2); border-radius: 3px;
    font-family: "JetBrains Mono", monospace;
  }
  #palette-list .pcmd .desc { color: var(--text-3); font-size: 11.5px; margin-left: 4px; }
  .palette-footer {
    padding: 8px 14px; font-size: 11.5px; border-top: 1px solid var(--border);
    display: flex; gap: 14px;
  }

  /* Sort chips */
  .chip[data-sort] { background: var(--bg); }
  .chip[data-sort].active { background: var(--accent-2); color: white; border-color: var(--accent); }

  /* Disk / Storage */
  .drive-bar {
    display: flex; height: 22px; border-radius: 6px; overflow: hidden;
    background: var(--bg); border: 1px solid var(--border);
    transition: all .3s;
  }
  .drive-bar .seg { height: 100%; transition: width .4s ease; }
  .drive-bar .seg-archive { background: linear-gradient(90deg, var(--accent), var(--purple)); }
  .drive-bar .seg-used    { background: var(--bg-3); }
  .drive-bar .seg-free    { background: linear-gradient(90deg, #2a543a, #3a7d52); }
  .drive-legend {
    display: flex; gap: 16px; font-size: 12px; padding: 8px 4px 0;
    flex-wrap: wrap;
  }
  .drive-legend .sw {
    display: inline-block; width: 10px; height: 10px; border-radius: 2px;
    margin-right: 4px; vertical-align: middle;
  }
  .drive-legend .sw-archive { background: linear-gradient(135deg, var(--accent), var(--purple)); }
  .drive-legend .sw-used    { background: var(--bg-3); border: 1px solid var(--border-2); }
  .drive-legend .sw-free    { background: #3a7d52; }

  .disk-perf-row {
    display: grid; grid-template-columns: 180px 1fr 90px 70px auto;
    gap: 10px; align-items: center; padding: 8px 10px;
    border-bottom: 1px solid var(--border); font-size: 12.5px;
    transition: background .12s;
  }
  .disk-perf-row:hover { background: var(--bg-3); }
  .disk-perf-row:last-child { border-bottom: none; }
  .disk-perf-row .pname { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .disk-perf-row .pmeter {
    height: 8px; background: var(--bg); border-radius: 4px; overflow: hidden;
    border: 1px solid var(--border);
  }
  .disk-perf-row .pmeter-fill {
    height: 100%; background: linear-gradient(90deg, var(--accent-2), var(--accent), var(--purple));
    transition: width .3s;
  }
  .disk-perf-row .psize { text-align: right; font-family: "JetBrains Mono", monospace; font-size: 11.5px; color: var(--text-2); }
  .disk-perf-row .pcount { text-align: right; color: var(--text-3); font-size: 11.5px; }

  .drive-legend #disk-lbl-warn.warn { color: var(--warn); font-weight: 600; }
  .drive-legend #disk-lbl-warn.bad  { color: var(--bad);  font-weight: 600; }

  /* Danger zone (history reset) */
  .danger-zone-title {
    margin-top: 22px; margin-bottom: 4px; font-size: 12.5px;
    color: var(--bad); text-transform: uppercase; letter-spacing: .7px;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .danger-zone {
    background: #1a0f14; border: 1px solid #4b2023; border-radius: 8px;
    padding: 10px 12px; margin-top: 4px;
  }
  .danger-zone .danger-row {
    display: flex; gap: 8px; align-items: center;
  }
  .danger-zone .danger-row select {
    background: #0f0a0c; border-color: #4b2023; color: var(--text);
  }
  .danger-zone button.danger {
    padding: 5px 10px; font-size: 12px; white-space: nowrap;
  }

  /* Empty state */
  .empty-state {
    text-align: center; padding: 48px 24px; color: var(--text-3);
    border: 2px dashed var(--border); border-radius: 12px; font-size: 13.5px;
  }
  .empty-state .big-icon { width: 48px; height: 48px; margin-bottom: 12px; color: var(--border-2); }
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg viewBox="0 0 64 64" aria-hidden="true">
      <defs>
        <linearGradient id="logo-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#5cb8ff"/>
          <stop offset=".6" stop-color="#2a6cb3"/>
          <stop offset="1" stop-color="#a78bfa"/>
        </linearGradient>
      </defs>
      <circle cx="32" cy="32" r="28" fill="#141821" stroke="url(#logo-grad)" stroke-width="2"/>
      <!-- Arrow -->
      <path d="M32 14 L32 40 M22 32 L32 42 L42 32"
            stroke="url(#logo-grad)" stroke-width="3" fill="none"
            stroke-linecap="round" stroke-linejoin="round"/>
      <!-- Shelf -->
      <path d="M18 48 L46 48" stroke="url(#logo-grad)" stroke-width="3" stroke-linecap="round"/>
    </svg>
    <div>
      <h1>Harvestr</h1>
      <div class="tagline">ONE NAME · EVERY VIDEO</div>
    </div>
  </div>

  <nav class="tab-bar" role="tablist" aria-label="Main sections">
    <button class="tab-btn active" role="tab" data-page="archive"
            aria-selected="true" aria-controls="page-archive"
            onclick="switchTab('archive')">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
      Archive
    </button>
    <button class="tab-btn" role="tab" data-page="live"
            aria-selected="false" aria-controls="page-live"
            onclick="switchTab('live')">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="9" opacity=".35"/><circle cx="12" cy="12" r="6" opacity=".55"/></svg>
      Live
      <span id="tab-live-badge" class="tab-badge" hidden>0</span>
    </button>
  </nav>

  <span class="status-pill" role="status" aria-live="polite">
    <span class="status-dot" id="status-dot"></span>
    <span id="status-text">idle</span>
  </span>

  <div style="flex:1"></div>

  <button class="ghost" data-tip="Command palette (Ctrl+K)"
          onclick="openPalette()" aria-label="Open command palette">
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <span style="opacity:.7; font-size:11px; padding:0 4px; border:1px solid var(--border-2); border-radius:3px;">⌘K</span>
  </button>

  <div id="archive-controls" style="display:flex; gap:8px;">
    <button class="primary" id="start-btn" onclick="startDownload()"
            data-tip="Run every performer in config">▶&nbsp; Start all</button>
    <button class="danger" id="stop-btn" onclick="stopDownload()" disabled
            data-tip="Kill the running subprocess">■&nbsp; Stop</button>
    <button onclick="runDedup()" data-tip="Scan + delete duplicate files">⌥&nbsp; Dedup</button>
    <button class="ghost" onclick="refreshAll()" aria-label="Refresh"
            data-tip="Reload config / sites / history">↻</button>
  </div>
  <div id="live-controls" style="display:none; gap:8px;">
    <button class="success" onclick="liveToggleAll(true)" data-tip="Start polling every model">▶ Start all live</button>
    <button class="danger" onclick="liveToggleAll(false)" data-tip="Stop polling every model">■ Stop all</button>
    <button class="ghost" onclick="liveRefresh()" aria-label="Refresh">↻</button>
  </div>
</header>

<div id="toast-stack" role="status" aria-live="polite" aria-atomic="false"></div>

<div class="modal-backdrop" id="preview-modal" onclick="closePreview(event)">
  <div class="modal-card" onclick="event.stopPropagation()">
    <div class="top">
      <div class="title" id="preview-title"></div>
      <button class="xs" onclick="closePreview()">✕</button>
    </div>
    <video id="preview-video" controls></video>
  </div>
</div>

<div class="container">

<section id="page-archive" class="page" role="tabpanel" aria-labelledby="tab-archive">

  <!-- Header stats strip -->
  <div class="stats">
    <div class="stat-box"><div class="value" id="stat-perf">–</div><div class="label">Performers</div></div>
    <div class="stat-box good"><div class="value" id="stat-hist">–</div><div class="label">Downloaded</div></div>
    <div class="stat-box bad"><div class="value" id="stat-fail">–</div><div class="label">Permanently Failed</div></div>
    <div class="stat-box warn"><div class="value" id="stat-disk">–</div><div class="label">Total Size</div></div>
  </div>

  <!-- Active downloads progress -->
  <div class="card progress-card" id="progress-card" style="display:none;">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
      Active downloads
      <span id="progress-session" class="muted" style="text-transform:none; letter-spacing:0; margin-left:auto; font-weight:normal;"></span>
    </h2>
    <div id="progress-list"></div>
  </div>

  <div class="grid">

    <!-- Performers -->
    <div class="card">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        Performers
      </h2>
      <div class="add-row">
        <input id="new-perf" type="text" placeholder="Add performer (e.g. blondie_254)"
               aria-label="Performer username"
               onkeydown="if(event.key==='Enter'){addPerformer();}" />
        <button class="primary" onclick="addPerformer()" data-tip="Add one performer">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
          Add
        </button>
        <button class="ghost" onclick="openBulkAdd()" data-tip="Paste a list or import a JSON config"
                aria-label="Bulk add / Import">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M12 18v-6M9 15h6"/></svg>
          Bulk
        </button>
      </div>
      <div class="perf-list" id="perf-list"></div>
      <div class="flex" style="margin-top: 12px; border-top: 1px solid var(--border); padding-top: 12px;">
        <button class="success" onclick="runSinglePerformer()">▶ Run selected</button>
        <span class="muted">Click a performer to select</span>
      </div>
    </div>

    <!-- Settings -->
    <div class="card">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        Settings
      </h2>
      <table class="config-table">
        <tr><td>Output dir</td><td><input id="cfg-output-dir" type="text"/></td></tr>
        <tr><td>Max videos per site</td><td><input id="cfg-max-videos" type="text"/></td></tr>
        <tr><td>Max parallel downloads</td><td><input id="cfg-max-parallel" type="text"/></td></tr>
        <tr><td>aria2c connections</td><td><input id="cfg-aria2c-conn" type="text"/></td></tr>
        <tr><td>Min disk GB</td><td><input id="cfg-min-disk" type="text"/></td></tr>
        <tr><td>Min duration (s)</td><td><input id="cfg-min-dur" type="text"/></td></tr>
        <tr><td>Rate limit</td><td><input id="cfg-rate" type="text" placeholder="e.g. 500K, 2M, blank = unlimited"/></td></tr>
        <tr><td>Cookies file</td><td><input id="cfg-cookies" type="text" placeholder="Path to cookies.txt (Netscape)"/></td></tr>
        <tr><td>Impersonate</td><td><input id="cfg-imp" type="text" placeholder="chrome"/></td></tr>
        <tr><td>Download proxy</td><td>
          <div style="display:flex; gap:6px; align-items:center;">
            <input id="cfg-proxy" type="text" placeholder="http://host:port, socks5://127.0.0.1:9055 (Tor), blank = none" style="flex:1;"/>
            <button class="xs" onclick="enableTor()" data-tip="Auto-start embedded Tor and fill in SOCKS URL">Use Tor</button>
          </div>
        </td></tr>
        <tr><td>CamSmut user</td><td><input id="cfg-cs-user" type="text" placeholder="(empty = skip camsmut)"/></td></tr>
        <tr><td>CamSmut password</td><td><input id="cfg-cs-pass" type="password" placeholder=""/></td></tr>
      </table>

      <div style="margin-top: 12px;">
        <button class="primary" onclick="saveSettings()">Save settings</button>
      </div>

      <!-- Danger zone: reset history per performer or all -->
      <h3 class="danger-zone-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px; height:13px; color:var(--bad);"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        Danger zone
      </h3>
      <p class="muted" style="font-size: 12px; margin: 4px 0 10px 0;">
        Reset history to re-download videos that were already recorded as OK.
        Useful after deleting files from disk, or when testing scraper changes.
      </p>
      <div class="danger-zone">
        <div class="danger-row">
          <div style="flex:1;">
            <select id="reset-performer-select" style="width:100%;">
              <option value="">Pick a performer…</option>
            </select>
          </div>
          <button class="danger" onclick="resetHistoryOne()" data-tip="Clear this performer's download + failure history">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
            Reset this one
          </button>
        </div>
        <div class="danger-row" style="margin-top: 8px;">
          <div class="muted" style="flex:1; font-size:12px;">
            Reset <b>ALL</b> performers' history — next run will re-probe every site.
          </div>
          <button class="danger" onclick="resetHistoryAll()">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            Reset everything
          </button>
        </div>
      </div>
    </div>

    <!-- Sites picker with category tabs -->
    <div class="card">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
        Sites to scrape <span class="muted" style="margin-left:auto; font-weight:normal;" id="sites-count"></span>
      </h2>
      <div class="site-tabs" id="site-tabs">
        <div class="tab active" data-cat="all" onclick="setSiteCat('all')">All</div>
        <div class="tab" data-cat="mainstream" onclick="setSiteCat('mainstream')">Mainstream</div>
        <div class="tab" data-cat="adult" onclick="setSiteCat('adult')">Adult</div>
        <div class="tab" data-cat="cam" onclick="setSiteCat('cam')">Cam archives</div>
        <div class="tab" data-cat="mirror" onclick="setSiteCat('mirror')">Mirrors</div>
        <div class="tab" data-cat="archive" onclick="setSiteCat('archive')">Archive</div>
      </div>
      <div class="flex mb">
        <button class="xs" onclick="setSitesAll(true)">Select all visible</button>
        <button class="xs" onclick="setSitesAll(false)">Clear visible</button>
        <span class="muted" style="margin-left:auto;"><span style="color: var(--warn)">●</span> = needs cookies</span>
      </div>
      <div class="site-list" id="sites-list"></div>
    </div>

    <!-- Live log -->
    <div class="card">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
        Live log
        <span class="muted" style="font-weight: normal; margin-left:auto;">
          <label style="display:inline-flex; align-items:center; gap:4px;">
            <input type="checkbox" id="log-autoscroll" checked style="width:12px; height:12px; margin:0;"/>
            auto-scroll
          </label>
        </span>
      </h2>
      <div class="log-viewer" id="log-viewer"></div>
    </div>

    <!-- Auth setup (full width) -->
    <div class="card" style="grid-column: 1 / -1;">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        Authentication &amp; paid accounts
        <span class="muted" style="font-weight:normal; margin-left:auto;" id="auth-summary"></span>
      </h2>
      <div id="auth-list"></div>
    </div>

    <!-- Storage / Disk Management -->
    <div class="card" style="grid-column: 1 / -1;">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
        Storage
        <span class="muted" style="margin-left:auto; font-weight:normal;" id="disk-summary"></span>
      </h2>

      <!-- Drive status bar -->
      <div id="disk-drive-bar" class="drive-bar">
        <div class="seg seg-archive" id="disk-seg-archive"
             data-tip="Harvestr downloads"></div>
        <div class="seg seg-used" id="disk-seg-other"
             data-tip="Other files on this drive"></div>
        <div class="seg seg-free" id="disk-seg-free" data-tip="Free"></div>
      </div>
      <div class="drive-legend muted">
        <span><span class="sw sw-archive"></span> <span id="disk-lbl-archive">–</span> Harvestr</span>
        <span><span class="sw sw-used"></span> <span id="disk-lbl-other">–</span> Other</span>
        <span><span class="sw sw-free"></span> <span id="disk-lbl-free">–</span> Free</span>
        <span style="margin-left:auto;" id="disk-lbl-warn"></span>
      </div>

      <!-- Cleanup actions -->
      <div class="filter-row" style="margin-top: 14px;" role="toolbar" aria-label="Cleanup actions">
        <button onclick="diskPruneOlder()" data-tip="Remove files older than N days">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          Prune older than…
        </button>
        <button onclick="diskPruneToFree()" data-tip="Delete oldest until N GB free">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
          Free up space…
        </button>
        <button onclick="runDedup()" data-tip="Content-based dedup scan">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="10" height="10" rx="1"/><rect x="11" y="11" width="10" height="10" rx="1"/></svg>
          Dedup
        </button>
        <div style="margin-left:auto; display:flex; gap:6px; align-items:center;">
          <span class="muted" style="font-size:11.5px;">Sort:</span>
          <button class="chip" data-disk-sort="size" onclick="diskSetSort('size')">size</button>
          <button class="chip" data-disk-sort="count" onclick="diskSetSort('count')">count</button>
          <button class="chip" data-disk-sort="name" onclick="diskSetSort('name')">name</button>
          <button class="chip" data-disk-sort="newest" onclick="diskSetSort('newest')">newest</button>
        </div>
      </div>

      <!-- Per-performer usage -->
      <div id="disk-performers" style="margin-top:10px;"></div>
    </div>

    <!-- Downloaded videos -->
    <div class="card" style="grid-column: 1 / -1;">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
        Downloaded (<span id="hist-count">0</span>)
      </h2>
      <div class="filter-row">
        <input id="hist-filter" type="text" placeholder="Search performer or title..." oninput="renderHistory()" style="min-width:240px;"/>
        <select id="hist-site" onchange="renderHistory()"><option value="">All sites</option></select>
        <select id="hist-sort" onchange="renderHistory()">
          <option value="date-desc">Newest first</option>
          <option value="date-asc">Oldest first</option>
          <option value="size-desc">Largest first</option>
          <option value="size-asc">Smallest first</option>
          <option value="perf">By performer</option>
        </select>
      </div>
      <div style="max-height: 480px; overflow-y: auto;">
        <table>
          <thead>
            <tr><th>Performer</th><th>Site</th><th>Title</th><th style="text-align:right;">Size</th><th>Date</th><th></th></tr>
          </thead>
          <tbody id="hist-body"></tbody>
        </table>
      </div>
    </div>

    <!-- Failed / skipped -->
    <div class="card" style="grid-column: 1 / -1;">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        Failed / Skipped (<span id="fail-count">0</span>)
      </h2>
      <div class="filter-row">
        <select id="fail-perm-filter" onchange="renderFailed()">
          <option value="">All</option>
          <option value="perm">Permanent only</option>
          <option value="retry">Retry-able only</option>
        </select>
        <input id="fail-filter" type="text" placeholder="Search by reason or ID..." oninput="renderFailed()" style="min-width:240px;"/>
      </div>
      <div style="max-height: 320px; overflow-y: auto;">
        <table>
          <thead>
            <tr><th>ID</th><th>Site</th><th>Reason</th><th>Attempts</th><th></th></tr>
          </thead>
          <tbody id="fail-body"></tbody>
        </table>
      </div>
    </div>

  </div>
</section><!-- /page-archive -->

<section id="page-live" class="page" role="tabpanel" aria-labelledby="tab-live" hidden>
  <div id="live-unavailable" class="card" style="display:none;">
    <h2>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
      Live recording failed to start
    </h2>
    <p class="muted" style="font-size:13px;">
      The vendored Live backend (<code>live_backend/streamonitor/</code>) failed
      to import. This is unusual — run
      <code>python -c "from live_recording import available; print(available)"</code>
      to see the error, or check <code>downloads/universal.log</code>.
    </p>
    <p id="live-error" class="muted" style="font-size:12px; color: var(--bad);"></p>
  </div>

  <div id="live-available">
    <!-- Live stats strip -->
    <div class="stats">
      <div class="stat-box"><div class="value" id="live-stat-total">–</div><div class="label">Models tracked</div></div>
      <div class="stat-box accent"><div class="value" id="live-stat-running">–</div><div class="label">Polling</div></div>
      <div class="stat-box good"><div class="value" id="live-stat-recording">–</div><div class="label">Recording now</div></div>
      <div class="stat-box warn"><div class="value" id="live-stat-size">–</div><div class="label">Recorded size</div></div>
    </div>

    <!-- Add model + site picker -->
    <div class="card">
      <h2>
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
        Track a model
        <button class="ghost" style="margin-left:auto; padding:5px 10px; font-size:12px;"
                onclick="openLiveSettings()" data-tip="Live recording settings"
                aria-label="Open live settings">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
          Settings
        </button>
      </h2>
      <div class="add-row">
        <input id="live-new-username" type="text" placeholder="Model username"
               aria-label="Model username to add"
               onkeydown="if(event.key==='Enter'){liveAdd()}"/>
        <select id="live-new-site" aria-label="Cam site">
          <option value="">— Site —</option>
        </select>
        <button class="primary" id="live-add-btn" onclick="liveAdd()"
                data-tip="Auto-resolves the room ID from username">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
          Track
        </button>
        <button class="ghost" onclick="openLiveBulkAdd()" data-tip="Paste list or import JSON"
                aria-label="Bulk add live models">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M12 18v-6M9 15h6"/></svg>
          Bulk
        </button>
      </div>
      <p class="muted" style="margin-top: 8px; font-size: 12px;" id="live-site-hint"></p>
    </div>

    <!-- Filters -->
    <div class="filter-row" role="toolbar" aria-label="Model filters">
      <input id="live-filter" type="text" placeholder="Filter by username..."
             oninput="renderLiveModels()" style="min-width:220px;"
             aria-label="Filter models"/>
      <select id="live-site-filter" onchange="renderLiveModels()" aria-label="Filter by site">
        <option value="">All sites</option>
      </select>
      <select id="live-status-filter" onchange="renderLiveModels()" aria-label="Filter by status">
        <option value="">All states</option>
        <option value="PUBLIC">Recording (PUBLIC)</option>
        <option value="PRIVATE">Private</option>
        <option value="OFFLINE,LONG_OFFLINE">Offline</option>
        <option value="ONLINE">Connecting</option>
        <option value="RATELIMIT,CLOUDFLARE,RESTRICTED,ERROR">Problems</option>
        <option value="NOTRUNNING">Not polling</option>
      </select>
      <div style="margin-left:auto; display:flex; gap:6px;">
        <button class="chip" data-sort="status"  onclick="liveSetSort('status')">by status</button>
        <button class="chip" data-sort="name"    onclick="liveSetSort('name')">by name</button>
        <button class="chip" data-sort="site"    onclick="liveSetSort('site')">by site</button>
        <button class="chip" data-sort="size"    onclick="liveSetSort('size')">by size</button>
      </div>
    </div>

    <!-- Model grid -->
    <div id="live-models" class="live-grid"></div>
  </div>
</section><!-- /page-live -->

</div>

<!-- Command palette -->
<div class="modal-backdrop" id="palette-modal" onclick="closePalette(event)" role="dialog" aria-label="Command palette" aria-modal="true">
  <div class="modal-card palette" onclick="event.stopPropagation()">
    <input id="palette-input" type="text" placeholder="Type a command or search..."
           autocomplete="off" oninput="paletteFilter()"
           onkeydown="paletteKey(event)"/>
    <div id="palette-list" role="listbox"></div>
    <div class="palette-footer muted">
      <span>↑↓ navigate</span> · <span>↵ run</span> · <span>Esc close</span>
    </div>
  </div>
</div>

<!-- Confirm / prompt dialog (replaces native alert/confirm/prompt) -->
<div class="modal-backdrop" id="dialog-modal" onclick="closeDialog(event)"
     role="alertdialog" aria-modal="true" aria-labelledby="dialog-title">
  <div class="modal-card dialog" onclick="event.stopPropagation()">
    <div class="dialog-head" id="dialog-head">
      <div class="icon-wrap" id="dialog-icon"></div>
      <h3 id="dialog-title"></h3>
      <p class="msg" id="dialog-msg"></p>
    </div>
    <div class="dialog-input-wrap" id="dialog-input-wrap" hidden>
      <input id="dialog-input" type="text" autocomplete="off"
             onkeydown="if(event.key==='Enter'){dialogConfirm()}"/>
    </div>
    <div class="dialog-actions">
      <button class="ghost" onclick="dialogCancel()" id="dialog-cancel-btn">Cancel</button>
      <button class="primary" onclick="dialogConfirm()" id="dialog-ok-btn">OK</button>
    </div>
  </div>
</div>

<!-- Live settings modal (Live-tab only) -->
<div class="modal-backdrop" id="livesettings-modal" onclick="closeLiveSettings(event)"
     role="dialog" aria-modal="true" aria-labelledby="livesettings-title">
  <div class="modal-card livesettings-card" onclick="event.stopPropagation()">
    <div class="livesettings-head">
      <div>
        <h3 id="livesettings-title">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
               style="vertical-align: -2px; color: var(--accent);">
            <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
          </svg>
          Live recording settings
        </h3>
        <p class="muted" style="font-size: 12px; margin: 3px 0 0 0;">
          Applied to new recordings on the next poll cycle. No restart needed.
        </p>
      </div>
      <button class="xs ghost" onclick="closeLiveSettings()" aria-label="Close">✕</button>
    </div>

    <div class="livesettings-body">
      <div class="ls-section">
        <div class="ls-section-title">Segmentation</div>
        <div class="ls-row">
          <label for="cfg-live-break-mb" data-tip="Max file size per segment. Recorder rolls to a new file when reached.">Break size
            <span class="ls-unit">MB</span></label>
          <input id="cfg-live-break-mb" type="number" min="0" step="100" placeholder="0 = unlimited"/>
        </div>
        <div class="ls-row">
          <label for="cfg-live-break-min" data-tip="Max recording length before forcing a new segment.">Break length
            <span class="ls-unit">min</span></label>
          <input id="cfg-live-break-min" type="number" min="0" step="5" placeholder="0 = unlimited"/>
        </div>
      </div>

      <div class="ls-section">
        <div class="ls-section-title">Network</div>
        <div class="ls-row">
          <label for="cfg-live-poll-int" data-tip="How often to re-check each model's online status.">Poll interval
            <span class="ls-unit">s</span></label>
          <input id="cfg-live-poll-int" type="number" min="5" step="5" placeholder="30"/>
        </div>
        <div class="ls-row">
          <label for="cfg-live-retry-delay" data-tip="Delay after a failed stream before trying again.">Retry delay
            <span class="ls-unit">s</span></label>
          <input id="cfg-live-retry-delay" type="number" min="1" step="5" placeholder="5"/>
        </div>
        <div class="ls-row">
          <label for="cfg-live-min-speed" data-tip="If the HLS download speed falls below this, log a warning. 0 = no check.">Min speed
            <span class="ls-unit">KB/s</span></label>
          <input id="cfg-live-min-speed" type="number" min="0" step="10" placeholder="0"/>
        </div>
      </div>

      <div class="ls-section">
        <div class="ls-section-title">Reliability</div>
        <div class="ls-row">
          <label for="cfg-live-max-errors" data-tip="Max consecutive errors before auto-pausing. Reset on success.">Max errors → pause</label>
          <input id="cfg-live-max-errors" type="number" min="1" step="1" placeholder="10"/>
        </div>
        <div class="ls-row switch-row">
          <label for="cfg-live-autoresume" data-tip="Auto-resume paused recorders when they come online again.">Auto-resume on online</label>
          <label class="switch"><input id="cfg-live-autoresume" type="checkbox"/><span class="slider"></span></label>
        </div>
      </div>

      <div class="ls-section">
        <div class="ls-section-title">Post-processing</div>
        <div class="ls-row switch-row">
          <label for="cfg-live-postprocess" data-tip="Convert .ts to .mp4 automatically after each session.">Post-process to MP4</label>
          <label class="switch"><input id="cfg-live-postprocess" type="checkbox"/><span class="slider"></span></label>
        </div>
        <div class="ls-row">
          <label for="cfg-live-keep-n" data-tip="Keep only the last N recorded files per model. 0 = keep all.">Keep last N per model</label>
          <input id="cfg-live-keep-n" type="number" min="0" step="1" placeholder="0 = keep all"/>
        </div>
      </div>
    </div>

    <div class="livesettings-actions">
      <button class="ghost" onclick="closeLiveSettings()">Cancel</button>
      <button class="primary" onclick="saveLiveSettings()">Save live settings</button>
    </div>
  </div>
</div>

<!-- Bulk add / import modal (shared between Archive + Live) -->
<div class="modal-backdrop" id="bulkadd-modal" onclick="closeBulkAdd(event)" role="dialog" aria-modal="true" aria-labelledby="bulkadd-title">
  <div class="modal-card bulkadd" onclick="event.stopPropagation()">
    <div class="bulkadd-head">
      <div>
        <h3 id="bulkadd-title">Bulk add</h3>
        <p class="muted" id="bulkadd-sub">Paste one username per line, or upload a JSON config.</p>
      </div>
      <button class="xs ghost" onclick="closeBulkAdd()" aria-label="Close">✕</button>
    </div>
    <textarea id="bulkadd-text" rows="10"
              placeholder="alice_example&#10;bob_example&#10;# lines starting with # are comments"
              aria-label="Bulk list"></textarea>
    <div class="bulkadd-actions">
      <label class="file-btn">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
        Upload JSON
        <input type="file" accept=".json,application/json"
               onchange="bulkaddLoadFile(event)" hidden/>
      </label>
      <div style="flex:1"></div>
      <button class="ghost" onclick="closeBulkAdd()">Cancel</button>
      <button class="primary" onclick="bulkaddSubmit()">Add all</button>
    </div>
  </div>
</div>

<script>
// ── State ────────────────────────────────────────────────────────────────
let _config = {};
let _sites = [];                    // [{name, category, backend, needs_auth, auth_info, notes}, ...]
let _history = {};
let _failed = {};
let _auth = {};                     // cookie diagnostics
let _selectedPerformer = null;
let _siteCat = 'all';

// ── Helpers ──────────────────────────────────────────────────────────────
function toast(msg, type = '') {
  const stack = document.getElementById('toast-stack');
  const item = document.createElement('div');
  item.className = 'toast-item ' + (type || 'info');
  const icons = {
    success: '<svg class="icon-small" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    error:   '<svg class="icon-small" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info:    '<svg class="icon-small" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };
  item.innerHTML = (icons[type] || icons.info) + '<span>' + escapeHtml(msg) + '</span>';
  stack.appendChild(item);
  // Keep a rolling max of 5 toasts
  while (stack.children.length > 5) stack.removeChild(stack.firstChild);
  setTimeout(() => {
    item.classList.add('hide');
    setTimeout(() => item.remove(), 300);
  }, 3500);
}
function escapeHtml(s) {
  return (s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function bytesHuman(n) {
  if (!n && n !== 0) return '—';
  const u = ['B','KB','MB','GB','TB']; let i=0; let x=n;
  while (x >= 1024 && i < u.length-1) { x /= 1024; i++; }
  return (i === 0 ? x : x.toFixed(x < 10 ? 2 : 1)) + ' ' + u[i];
}
function secsHuman(n) {
  if (!n || n < 0) return '—';
  if (n < 60) return n + 's';
  if (n < 3600) return Math.floor(n/60) + 'm ' + (n%60).toString().padStart(2,'0') + 's';
  const h = Math.floor(n/3600); const m = Math.floor((n%3600)/60);
  return h + 'h ' + m + 'm';
}
async function api(path, opts={}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

// ── Status + live log ────────────────────────────────────────────────────
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    if (s.running) {
      dot.className = 'status-dot running';
      txt.textContent = (s.current_performer || 'running...');
    } else {
      dot.className = 'status-dot';
      txt.textContent = 'idle';
    }
    document.getElementById('start-btn').disabled = s.running;
    document.getElementById('stop-btn').disabled = !s.running;

    // Live log
    const lv = document.getElementById('log-viewer');
    const autoScroll = document.getElementById('log-autoscroll').checked;
    const shouldScroll = autoScroll && (lv.scrollTop + lv.clientHeight >= lv.scrollHeight - 20);
    lv.innerHTML = (s.log_tail || []).map(line => {
      let cls = '';
      if (line.includes('ERROR')) cls = 'ERROR';
      else if (line.includes('WARN')) cls = 'WARN';
      else if (line.includes('DEBUG')) cls = 'DEBUG';
      return `<span class="${cls}">${escapeHtml(line)}</span>`;
    }).join('\n');
    if (shouldScroll || (autoScroll && s.running)) lv.scrollTop = lv.scrollHeight;
  } catch (e) { console.error(e); }
}

// ── Progress ─────────────────────────────────────────────────────────────
async function refreshProgress() {
  try {
    const p = await api('/api/progress');
    const card = document.getElementById('progress-card');
    const listEl = document.getElementById('progress-list');
    const sessEl = document.getElementById('progress-session');
    const active = p.active || [];
    const sess = p.session || {};
    if (!sess.running && active.length === 0) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'block';

    // Session summary (top-right of the card header)
    const bits = [];
    if (sess.performer) bits.push('<b>' + escapeHtml(sess.performer) + '</b>');
    if (sess.total_queued) bits.push(`${sess.ok||0}/${sess.total_queued} done`);
    else if ((sess.ok||0) > 0 || (sess.fail||0) > 0) bits.push(`${sess.ok||0} ok`);
    if (sess.fail) bits.push(`<span style="color:var(--bad)">${sess.fail} failed</span>`);
    if (sess.skip) bits.push(`<span style="color:var(--warn)">${sess.skip} skipped</span>`);
    sessEl.innerHTML = bits.join(' · ');

    // Phase / activity summary
    const phase = sess.phase || '';
    const phaseIcon = {probing:'🛰', enumerating:'📋', downloading:'⬇', done:'✔'}[phase] || '⏳';
    let phaseHtml = '';
    if (phase === 'probing' && sess.probe_total) {
      const pct = Math.min(100, Math.floor(100 * (sess.probe_done||0) / sess.probe_total));
      phaseHtml = `<div class="phase-panel">
        <div class="phase-line">
          <span class="phase-label">${phaseIcon} ${escapeHtml(sess.phase_label || 'Probing sites...')}</span>
          <span class="phase-meta">${sess.probe_done||0} / ${sess.probe_total} probes · ${pct}%${sess.current_site ? ' · latest <code>'+escapeHtml(sess.current_site)+'</code>' : ''}</span>
        </div>
        <div class="progress-bar"><div class="fill" style="width:${pct}%"></div></div>
      </div>`;
    } else if (phase === 'enumerating') {
      phaseHtml = `<div class="phase-panel">
        <div class="phase-line">
          <span class="phase-label">${phaseIcon} ${escapeHtml(sess.phase_label || 'Enumerating hits...')}</span>
          <span class="phase-meta">${sess.videos_found||0} videos found so far</span>
        </div>
      </div>`;
    } else if (phase === 'downloading' && sess.total_queued) {
      const done = (sess.ok||0) + (sess.fail||0) + (sess.skip||0);
      const pct = Math.min(100, Math.floor(100 * done / sess.total_queued));
      phaseHtml = `<div class="phase-panel">
        <div class="phase-line">
          <span class="phase-label">${phaseIcon} ${escapeHtml(sess.phase_label || 'Downloading...')}</span>
          <span class="phase-meta">${done} / ${sess.total_queued} videos · ${pct}%</span>
        </div>
        <div class="progress-bar"><div class="fill" style="width:${pct}%"></div></div>
      </div>`;
    } else if (phase === 'done') {
      phaseHtml = `<div class="phase-panel done">
        <span class="phase-label">${phaseIcon} ${escapeHtml(sess.phase_label || 'Done')}</span>
      </div>`;
    }

    // Hits summary: which sites found content
    let hitsHtml = '';
    if ((sess.sites_hit || []).length > 0) {
      hitsHtml = `<div class="hits-row"><span class="hits-label">Sites with videos:</span> ` +
        sess.sites_hit.slice().sort((a,b) => (b.count||0) - (a.count||0))
          .map(h => {
            const url = h.url || '';
            const tip = url
              ? `Open ${escapeHtml(h.site)} page for '${escapeHtml(sess.performer || '')}' in new tab — verify it's the right ${h.count} videos`
              : '';
            const inner = `${escapeHtml(h.site)} · ${h.count}`;
            if (url) {
              return `<a class="pill hit-pill hit-pill-link" href="${escapeHtml(url)}"
                         target="_blank" rel="noopener noreferrer"
                         data-tip="${tip}">${inner}
                         <svg class="ext-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                              stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                           <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                           <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
                         </svg></a>`;
            }
            return `<span class="pill hit-pill">${inner}</span>`;
          })
          .join(' ') + '</div>';
    }

    // Active downloads rows
    let activeHtml = '';
    if (active.length) {
      activeHtml = active.map(a => {
        const pct = Math.min(100, Math.max(0, a.percent || 0));
        const done = bytesHuman(a.bytes_done || 0);
        const total = a.bytes_total ? bytesHuman(a.bytes_total) : '?';
        const speed = a.speed_bps ? bytesHuman(a.speed_bps) + '/s' : '—';
        const eta = a.eta_seconds ? secsHuman(a.eta_seconds) : '—';
        const slot = (a.slot !== undefined && a.slot !== null) ? a.slot : -1;
        const cancelling = (p.cancelled_slots || []).includes(slot);
        const btnTitle = cancelling ? 'Cancelling…' : 'Skip this download';
        // Video-verify link: if we know the source URL, title becomes clickable
        const vurl = a.video_url || '';
        const titleText = escapeHtml(a.title || a.video_id || '');
        const titleFull = escapeHtml(a.title || '');
        const titleEl = vurl
          ? `<a class="title clickable" href="${escapeHtml(vurl)}" target="_blank"
                rel="noopener noreferrer" title="${titleFull}"
                data-tip="Open on ${escapeHtml(a.site || '')} in new tab — verify this is ${escapeHtml(sess.performer || 'the right performer')}">${titleText}</a>`
          : `<span class="title" title="${titleFull}">${titleText}</span>`;
        return `<div class="dl-active${cancelling ? ' cancelling' : ''}" data-slot="${slot}">
          <div class="top">
            <span class="pill ${a.backend === 'yt-dlp' ? 'ytdlp' : 'custom'}">${escapeHtml(a.site || '?')}</span>
            ${titleEl}
            <span class="meta">${done} / ${total} · ${speed} · ETA ${eta} · ${pct.toFixed(1)}%</span>
            <button class="dl-cancel" title="${btnTitle}" aria-label="${btnTitle}"
                    ${slot < 0 || cancelling ? 'disabled' : ''}
                    onclick="cancelSlot(${slot}, event)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                   stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="progress-bar"><div class="fill" style="width:${pct}%"></div></div>
        </div>`;
      }).join('');
    } else if (phase === 'downloading') {
      activeHtml = '<div class="dl-empty">Queue ready — waiting for next slot…</div>';
    } else if (!phase || phase === 'idle') {
      activeHtml = '<div class="dl-empty">Session starting — initializing scrapers…</div>';
    }

    listEl.innerHTML = phaseHtml + hitsHtml + activeHtml;
  } catch (e) { console.error('progress', e); }
}

// Cancel one active download (skip it, keep the session running)
async function cancelSlot(slot, ev) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  if (slot === undefined || slot === null || slot < 0) return;
  const row = document.querySelector('.dl-active[data-slot="' + slot + '"]');
  const titleEl = row ? row.querySelector('.title') : null;
  const title = titleEl ? titleEl.textContent.trim() : 'this download';

  const ok = await confirmDialog(
    'Skip <b>' + escapeHtml(title.slice(0, 80)) + '</b>? It will be marked as skipped and the queue will move to the next video.',
    {title: 'Skip download?', tone: 'warn', confirmLabel: 'Skip', cancelLabel: 'Keep'}
  );
  if (!ok) return;

  // Optimistic UI — grey out the row immediately
  if (row) {
    row.classList.add('cancelling');
    const btn = row.querySelector('.dl-cancel');
    if (btn) { btn.disabled = true; btn.title = 'Cancelling…'; }
  }
  try {
    const res = await fetch('/api/progress/cancel', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({slot})
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({error: 'failed'}));
      if (row) row.classList.remove('cancelling');
      await confirmDialog('Could not cancel: ' + escapeHtml(err.error || 'unknown error'),
        {title: 'Cancel failed', tone: 'danger', confirmLabel: 'OK', hideCancel: true});
    }
  } catch (e) {
    console.error('cancelSlot', e);
    if (row) row.classList.remove('cancelling');
  }
}

// ── Config ───────────────────────────────────────────────────────────────
async function loadConfig() {
  _config = await api('/api/config');
  const g = (id) => document.getElementById(id);
  g('cfg-output-dir').value = _config.output_dir || '';
  g('cfg-max-videos').value = _config.max_videos_per_site || '';
  g('cfg-max-parallel').value = _config.max_parallel_downloads || '';
  g('cfg-aria2c-conn').value = _config.aria2c_connections || '';
  g('cfg-min-disk').value = _config.min_disk_gb || '';
  g('cfg-min-dur').value = _config.min_duration_seconds || '';
  g('cfg-rate').value = _config.rate_limit || '';
  g('cfg-cookies').value = _config.cookies_file || '';
  g('cfg-imp').value = _config.impersonate_target || '';
  g('cfg-proxy').value = _config.download_proxy || '';
  g('cfg-cs-user').value = _config.camsmut_username || '';
  g('cfg-cs-pass').value = _config.camsmut_password || '';
  // Live recording settings
  const live = _config.live || {};
  g('cfg-live-break-mb').value      = live.break_size_mb ?? '';
  g('cfg-live-break-min').value     = live.break_length_min ?? '';
  g('cfg-live-poll-int').value      = live.poll_interval_s ?? '';
  g('cfg-live-retry-delay').value   = live.retry_delay_s ?? '';
  g('cfg-live-max-errors').value    = live.max_errors ?? '';
  g('cfg-live-min-speed').value     = live.min_speed_kbps ?? '';
  g('cfg-live-autoresume').checked  = !!(live.auto_resume ?? true);
  g('cfg-live-postprocess').checked = !!(live.post_process_mp4 ?? false);
  // Populate the "reset history" performer dropdown in the Danger zone
  const rsel = g('reset-performer-select');
  if (rsel) {
    const cur = rsel.value;
    rsel.innerHTML = '<option value="">Pick a performer…</option>' +
      (_config.performers || []).map(p =>
        `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join('');
    if (cur) rsel.value = cur;
  }
  renderPerformers();
  renderSites();
}

// ── Live settings modal ────────────────────────────────────────────────
function openLiveSettings() {
  // Re-populate from the latest config (loadConfig has already run at boot)
  const g = (id) => document.getElementById(id);
  const live = (_config && _config.live) || {};
  g('cfg-live-break-mb').value      = live.break_size_mb ?? '';
  g('cfg-live-break-min').value     = live.break_length_min ?? '';
  g('cfg-live-poll-int').value      = live.poll_interval_s ?? '';
  g('cfg-live-retry-delay').value   = live.retry_delay_s ?? '';
  g('cfg-live-max-errors').value    = live.max_errors ?? '';
  g('cfg-live-min-speed').value     = live.min_speed_kbps ?? '';
  g('cfg-live-autoresume').checked  = !!(live.auto_resume ?? true);
  g('cfg-live-postprocess').checked = !!(live.post_process_mp4 ?? false);
  g('cfg-live-keep-n').value        = live.keep_last_n ?? '';
  document.getElementById('livesettings-modal').classList.add('show');
  setTimeout(() => g('cfg-live-break-mb').focus(), 40);
}
function closeLiveSettings(e) {
  if (e && e.target && e.target.id !== 'livesettings-modal') return;
  document.getElementById('livesettings-modal').classList.remove('show');
}
async function saveLiveSettings() {
  const g = (id) => document.getElementById(id);
  const liveCfg = {
    break_size_mb:    parseInt(g('cfg-live-break-mb').value)    || 0,
    break_length_min: parseInt(g('cfg-live-break-min').value)   || 0,
    poll_interval_s:  parseInt(g('cfg-live-poll-int').value)    || 30,
    retry_delay_s:    parseInt(g('cfg-live-retry-delay').value) || 5,
    max_errors:       parseInt(g('cfg-live-max-errors').value)  || 10,
    min_speed_kbps:   parseInt(g('cfg-live-min-speed').value)   || 0,
    auto_resume:      !!g('cfg-live-autoresume').checked,
    post_process_mp4: !!g('cfg-live-postprocess').checked,
    keep_last_n:      parseInt(g('cfg-live-keep-n').value)      || 0,
  };
  const merged = {..._config, live: liveCfg};
  try {
    await api('/api/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(merged),
    });
    _config = merged;
    toast('Live settings saved', 'success');
    closeLiveSettings();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ── History reset (danger zone) ───────────────────────────────────────
async function resetHistoryOne() {
  const sel = document.getElementById('reset-performer-select');
  const name = (sel && sel.value || '').trim();
  if (!name) { toast('Pick a performer first', 'error'); return; }
  const ok = await confirmDialog(
    `Clear download + failure history for <b>${escapeHtml(name)}</b>?<br><br>` +
    `Files on disk stay, but Harvestr will treat every video as "never seen" — ` +
    `next run will re-probe every site and re-download anything new. ` +
    `Useful if you deleted files from disk or you're testing scraper changes.`,
    {title: 'Reset history for one performer', tone: 'warn',
     confirmLabel: 'Reset', cancelLabel: 'Keep'});
  if (!ok) return;
  try {
    const r = await api('/api/history/reset', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({performer: name, include_failed: true})
    });
    toast(`Cleared ${r.removed_history} history + ${r.removed_failed} failed`, 'success');
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

async function resetHistoryAll() {
  const cnt = (_config.performers || []).length;
  const ok = await confirmDialog(
    `Clear history for <b>ALL ${cnt}</b> performers?<br><br>` +
    `This wipes history.json AND failed.json completely. Next full run ` +
    `will re-probe every site for every performer and re-download anything ` +
    `not already on disk. <b>This cannot be undone</b>.`,
    {title: 'Reset ALL history', tone: 'danger',
     confirmLabel: 'Reset everything', cancelLabel: 'Cancel'});
  if (!ok) return;
  try {
    const r = await api('/api/history/reset', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({all: true, include_failed: true})
    });
    toast(`Cleared ${r.removed_history} history + ${r.removed_failed} failed`, 'success');
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

async function resetHistoryFor(name) {
  // Called from per-performer row button
  const ok = await confirmDialog(
    `Clear <b>${escapeHtml(name)}</b>'s history so the next run re-downloads everything new?`,
    {title: 'Reset performer history', tone: 'warn',
     confirmLabel: 'Reset', cancelLabel: 'Keep'});
  if (!ok) return;
  try {
    const r = await api('/api/history/reset', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({performer: name, include_failed: true})
    });
    toast(`Cleared ${r.removed_history} history + ${r.removed_failed} failed`, 'success');
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

// ── Performers ───────────────────────────────────────────────────────────
function renderPerformers() {
  const list = document.getElementById('perf-list');
  const perfs = _config.performers || [];
  if (!perfs.length) {
    list.innerHTML = '<div class="muted" style="padding:16px; text-align:center;">No performers. Add a username above.</div>';
  } else {
    list.innerHTML = perfs.map(p => {
      const hist_count = Object.keys(_history[p.toLowerCase()] || {}).length;
      const isSel = (p === _selectedPerformer);
      return `<div class="perf-row ${isSel ? 'selected' : ''}" onclick="togglePerf('${escapeHtml(p)}')">
        <span class="name">${escapeHtml(p)}</span>
        <span class="count">${hist_count} videos</span>
        <button class="xs" onclick="runSingleByName('${escapeHtml(p)}'); event.stopPropagation()" data-tip="Run just this one">▶</button>
        <button class="xs" onclick="resetHistoryFor('${escapeHtml(p)}'); event.stopPropagation()"
                data-tip="Reset history so next run re-downloads everything" aria-label="Reset history">
          <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </button>
        <button class="xs danger" onclick="removePerformer('${escapeHtml(p)}'); event.stopPropagation()">✕</button>
      </div>`;
    }).join('');
  }
  document.getElementById('stat-perf').textContent = perfs.length;
}
function togglePerf(name) {
  _selectedPerformer = (_selectedPerformer === name) ? null : name;
  renderPerformers();
}
async function addPerformer() {
  const name = document.getElementById('new-perf').value.trim();
  if (!name) return;
  try {
    await api('/api/config/performer/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})
    });
    document.getElementById('new-perf').value = '';
    toast('Added ' + name, 'success');
    loadConfig();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function removePerformer(name) {
  if (!await confirmDialog(
        `Remove <code>${escapeHtml(name)}</code> from the performer list?`,
        {title: 'Remove performer', tone: 'warn',
         confirmLabel: 'Remove', cancelLabel: 'Keep'})) return;
  try {
    await api('/api/config/performer/remove', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})
    });
    toast('Removed ' + name);
    loadConfig();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ── Sites ────────────────────────────────────────────────────────────────
async function loadSites() {
  const d = await api('/api/sites/detailed');
  _sites = d.sites || [];
}
function setSiteCat(cat) {
  _siteCat = cat;
  document.querySelectorAll('#site-tabs .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.cat === cat);
  });
  renderSites();
}
function _visibleSites() {
  if (_siteCat === 'all') return _sites;
  return _sites.filter(s => s.category === _siteCat);
}
function renderSites() {
  const el = document.getElementById('sites-list');
  const enabled = new Set(_config.enabled_sites || []);
  const isEmpty = enabled.size === 0;
  const visible = _visibleSites();
  document.getElementById('sites-count').textContent =
    visible.length + ' sites · ' + (isEmpty ? 'all enabled' : `${enabled.size} selected`);
  if (!visible.length) {
    el.innerHTML = '<div class="muted" style="padding:20px; text-align:center;">No sites in this category.</div>';
    return;
  }
  const authReport = _auth.sites || {};
  el.innerHTML = visible.map(s => {
    const isOn = isEmpty || enabled.has(s.name);
    let authHtml = '';
    if (s.needs_auth) {
      const rep = authReport[s.name];
      const st = rep ? rep.status : 'none';
      const tip = s.auth_info ? s.auth_info.why.replace(/"/g, '&quot;')
                               : 'Some features require cookie auth';
      authHtml = `<span class="auth-icon" data-status="${st}" data-tip="${tip}">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 17a2 2 0 0 1-2-2V11a2 2 0 1 1 4 0v4a2 2 0 0 1-2 2zm6-6V8a6 6 0 0 0-12 0v3H4v10h16V11h-2zm-10-3a4 4 0 0 1 8 0v3H8V8z"/></svg>
      </span>`;
    }
    const badge = s.backend === 'custom' ? `<span class="site-badge" data-tip="Custom scraper">custom</span>`
                                         : `<span class="site-badge" data-tip="yt-dlp extractor">yt-dlp</span>`;
    return `<div class="site-row" onclick="toggleSite('${s.name}', this)">
      <input type="checkbox" ${isOn ? 'checked' : ''} onclick="event.stopPropagation()" onchange="toggleSite('${s.name}', this)"/>
      <span class="site-name">${escapeHtml(s.name)}</span>
      ${authHtml}
      ${badge}
    </div>`;
  }).join('');
}
function toggleSite(name, target) {
  const enabled = new Set(_config.enabled_sites || []);
  const isEmpty = enabled.size === 0;
  if (isEmpty) _sites.forEach(s => enabled.add(s.name));
  // Toggle based on the checkbox if passed target, else flip
  let cb;
  if (target && target.tagName === 'INPUT') cb = target;
  else cb = target && target.querySelector ? target.querySelector('input[type="checkbox"]') : null;
  if (cb && !(target && target.tagName === 'INPUT')) cb.checked = !cb.checked;
  if (cb ? cb.checked : !enabled.has(name)) enabled.add(name);
  else enabled.delete(name);
  const arr = (enabled.size === _sites.length) ? [] : Array.from(enabled);
  _config.enabled_sites = arr;
  clearTimeout(window._sitesSaveT);
  window._sitesSaveT = setTimeout(saveSettings, 500);
  renderSites();
}
function setSitesAll(on) {
  const enabled = new Set(_config.enabled_sites || []);
  const wasEmpty = enabled.size === 0;
  if (wasEmpty) _sites.forEach(s => enabled.add(s.name));
  const visible = _visibleSites();
  visible.forEach(s => { if (on) enabled.add(s.name); else enabled.delete(s.name); });
  const arr = (enabled.size === _sites.length) ? [] : Array.from(enabled);
  _config.enabled_sites = arr;
  saveSettings();
  renderSites();
}

// ── Settings save ────────────────────────────────────────────────────────
async function saveSettings() {
  const g = (id) => document.getElementById(id);
  const cfg = {..._config,
    output_dir: g('cfg-output-dir').value,
    max_videos_per_site: parseInt(g('cfg-max-videos').value) || 10,
    max_parallel_downloads: parseInt(g('cfg-max-parallel').value) || 3,
    aria2c_connections: parseInt(g('cfg-aria2c-conn').value) || 16,
    min_disk_gb: parseFloat(g('cfg-min-disk').value) || 5.0,
    min_duration_seconds: parseFloat(g('cfg-min-dur').value) || 30.0,
    rate_limit: g('cfg-rate').value,
    cookies_file: g('cfg-cookies').value,
    impersonate_target: g('cfg-imp').value,
    download_proxy: g('cfg-proxy').value,
    camsmut_username: g('cfg-cs-user').value,
    camsmut_password: g('cfg-cs-pass').value,
    // Live settings: preserved as-is (edited via the Live tab's gear modal,
    // not from this Archive-side Settings panel).
    live: _config.live || {},
  };
  try {
    await api('/api/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg)
    });
    _config = cfg;
    toast('Settings saved', 'success');
    // Re-check auth status after cookie file change
    loadAuth();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

// ── Auth panel ───────────────────────────────────────────────────────────
async function loadAuth() {
  _auth = await api('/api/auth');
  renderAuth();
  renderSites();  // re-render to update auth indicators
}
function renderAuth() {
  const el = document.getElementById('auth-list');
  const summary = document.getElementById('auth-summary');
  const sites = _auth.sites || {};
  const total = Object.keys(sites).length;
  const ok = Object.values(sites).filter(s => s.status === 'ok').length;
  summary.textContent = `${ok} of ${total} auth sites configured · cookies: ${_auth.cookies_file_exists ? 'loaded' : 'none'}`;

  // Ordered list of site keys with their info
  const siteMeta = {
    recume:        {label:'Recu.me / Recurbate',
                    why:'Cloudflare-blocked without cookies. Free account = ~5 plays/day; premium = unlimited + official downloads.',
                    signup:'https://recu.me/account/signup',
                    paid:'https://recu.me/account/subscribe',
                    howto:`<ol>
  <li>Sign up at <a href="https://recu.me/account/signup" target="_blank">recu.me/account/signup</a> (free, email only).</li>
  <li><b>For unlimited access</b>, buy a premium plan at <a href="https://recu.me/account/subscribe" target="_blank">recu.me/account/subscribe</a> ($10-$20/month).</li>
  <li>Log into recu.me in Chrome/Firefox.</li>
  <li>Install the "Get cookies.txt LOCALLY" extension, click it → Export.</li>
  <li>Save the file somewhere (e.g. <code>C:\\Users\\&lt;you&gt;\\harvestr\\cookies.txt</code>).</li>
  <li>In Settings above, paste the path into <b>Cookies file</b>, then save.</li>
</ol>
<p class="muted">The <code>cf_clearance</code> cookie expires in 30-60 min. If scraping stops working, just re-export from your browser.</p>`},
    xcom:          {label:'X.com / Twitter',
                    why:'Premium X = 10× daily quota + long videos + full timeline history. Without auth you get ~1k posts/day, capped at 3200 lifetime per user (X-wide limit).',
                    signup:'https://x.com/i/flow/signup',
                    paid:'https://x.com/i/premium_sign_up',
                    howto:`<ol>
  <li>Log in at <a href="https://x.com" target="_blank">x.com</a> with your <b>premium</b> account (free works too but with much stricter limits).</li>
  <li>Export cookies using "Get cookies.txt LOCALLY" extension.</li>
  <li>Append the exported file to your existing cookies.txt (or use a separate one).</li>
  <li>In Settings, point <b>Cookies file</b> at the path.</li>
</ol>
<p class="muted">Required cookies: <code>auth_token</code>, <code>ct0</code>. Optional but helpful: <code>guest_id</code>, <code>personalization_id</code>.</p>`},
    camwhores_tv:  {label:'camwhores.tv (private videos)',
                    why:'Public videos work without auth. Private/friend-locked uploads require you to be a "friend" of the uploader.',
                    signup:'https://www.camwhores.tv/signup/',
                    howto:`<ol>
  <li>Create account at <a href="https://www.camwhores.tv/signup/" target="_blank">camwhores.tv</a>.</li>
  <li>Upload at least 1 video yourself to become a "member" (required to request friends).</li>
  <li>Add the uploader as a friend (they must accept).</li>
  <li>Log in, export cookies, add to <b>Cookies file</b>.</li>
</ol>`},
    camvault:      {label:'camvault.to',
                    why:'Premium members get full downloads; free accounts see 10-second previews only.',
                    paid:'https://camvault.to/premium'},
    archivebate:   {label:'archivebate.com',
                    why:'HD stream access sometimes requires a logged-in session.'},
    camsmut:       {label:'camsmut.com',
                    why:'Video pages return 404 without a logged-in session. Free account works (no premium tier needed). Harvestr logs in automatically for you when you supply username + password in Settings above.',
                    signup:'https://camsmut.com/register',
                    howto:`<ol>
  <li>Create a free account at <a href="https://camsmut.com/register" target="_blank">camsmut.com/register</a>.</li>
  <li>Come back here → <b>Settings</b> (above) → fill <b>CamSmut user</b> and <b>CamSmut password</b>.</li>
  <li>Click <b>Save settings</b>. Harvestr will auto-login on the next scrape.</li>
</ol>
<p class="muted">No cookies.txt required — credentials are stored in <code>config.json</code>. Delete them anytime by clearing those fields and saving.</p>`},
  };

  // Build cards in our preferred order
  const order = ['recume','xcom','camsmut','camwhores_tv','camvault','archivebate'];
  el.innerHTML = order.map(key => {
    if (!sites[key]) return '';
    const rep = sites[key];
    const meta = siteMeta[key] || {};
    const statusLabel = {ok:'Cookies OK', partial:'Partial cookies', none:'No cookies'}[rep.status];
    return `<div class="auth-site">
      <div class="header">
        <span class="title">${escapeHtml(meta.label || rep.label)}</span>
        ${meta.paid ? `<a href="${meta.paid}" target="_blank" class="pill info">Buy premium ↗</a>` : ''}
        ${meta.signup ? `<a href="${meta.signup}" target="_blank" class="pill">Free signup ↗</a>` : ''}
        <span class="status ${rep.status}">${statusLabel}</span>
      </div>
      <div class="why">${escapeHtml(meta.why || '')}</div>
      <div class="cookies">
        <b>Required:</b>
        ${(rep.cookies_required||[]).map(c => {
          const found = (rep.cookies_found||[]).map(x => x.toLowerCase()).includes(c.toLowerCase());
          return `<code style="color:${found?'var(--good)':'var(--bad)'}">${escapeHtml(c)}${found?' ✓':' ✗'}</code>`;
        }).join(' · ')}
      </div>
      ${meta.howto ? `<details>
        <summary>Show cookie-export instructions</summary>
        <div style="padding: 6px 0; color: var(--text-2); font-size: 12.5px;">${meta.howto}</div>
      </details>` : ''}
    </div>`;
  }).join('');
}

// ── History ──────────────────────────────────────────────────────────────
async function loadHistory() {
  _history = await api('/api/history');
  _failed = await api('/api/failed');
  _populateHistSites();
  renderHistory();
  renderFailed();
  // Performer counts depend on history — re-render so each row shows
  // its real video count instead of 0.
  renderPerformers();
}
function _populateHistSites() {
  const sel = document.getElementById('hist-site');
  const existing = new Set(Array.from(sel.options).map(o => o.value));
  const sites = new Set();
  for (const entries of Object.values(_history)) {
    for (const info of Object.values(entries)) {
      if (info.site) sites.add(info.site);
    }
  }
  Array.from(sites).sort().forEach(s => {
    if (!existing.has(s)) {
      const o = document.createElement('option'); o.value = s; o.textContent = s;
      sel.appendChild(o);
    }
  });
}
function renderHistory() {
  const filter = document.getElementById('hist-filter').value.toLowerCase();
  const siteFilter = document.getElementById('hist-site').value;
  const sort = document.getElementById('hist-sort').value;
  const tbody = document.getElementById('hist-body');
  let rows = [];
  let totalSize = 0;
  for (const [perf, entries] of Object.entries(_history)) {
    for (const [gid, info] of Object.entries(entries)) {
      if (filter && !perf.toLowerCase().includes(filter) &&
          !(info.title || '').toLowerCase().includes(filter)) continue;
      if (siteFilter && info.site !== siteFilter) continue;
      rows.push({perf, ...info, gid});
      totalSize += info.filesize || 0;
    }
  }
  const sorts = {
    'date-desc': (a,b) => (b.date || '').localeCompare(a.date || ''),
    'date-asc':  (a,b) => (a.date || '').localeCompare(b.date || ''),
    'size-desc': (a,b) => (b.filesize||0) - (a.filesize||0),
    'size-asc':  (a,b) => (a.filesize||0) - (b.filesize||0),
    'perf':      (a,b) => a.perf.localeCompare(b.perf),
  };
  rows.sort(sorts[sort] || sorts['date-desc']);
  const totalCount = Object.values(_history).reduce((a, v) => a + Object.keys(v).length, 0);
  document.getElementById('stat-hist').textContent = totalCount;
  document.getElementById('hist-count').textContent = rows.length;
  document.getElementById('stat-disk').textContent = bytesHuman(totalSize);

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:20px;">
      No downloads yet — start one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.slice(0, 500).map(r => `
    <tr>
      <td><b>${escapeHtml(r.perf)}</b></td>
      <td><span class="pill">${escapeHtml(r.site || '')}</span></td>
      <td title="${escapeHtml(r.title||'')}">${escapeHtml((r.title||'').slice(0,100))}</td>
      <td style="text-align:right;" class="mono">${bytesHuman(r.filesize||0)}</td>
      <td class="mono">${escapeHtml((r.date||'').slice(0,16).replace('T',' '))}</td>
      <td>
        <button class="xs" onclick="playVideo(${JSON.stringify(r.output || '').replace(/"/g, '&quot;')}, ${JSON.stringify(r.title || '').replace(/"/g, '&quot;')})" data-tip="Play in-browser">▶</button>
      </td>
    </tr>
  `).join('');
  if (rows.length > 500) {
    tbody.innerHTML += `<tr><td colspan="6" class="muted" style="text-align:center;">
      ...+${rows.length - 500} more (filter to narrow)</td></tr>`;
  }
}

function renderFailed() {
  const tbody = document.getElementById('fail-body');
  const filter = document.getElementById('fail-filter').value.toLowerCase();
  const permF = document.getElementById('fail-perm-filter').value;
  let rows = Object.entries(_failed).map(([gid, info]) => ({gid, ...info}));
  if (permF === 'perm') rows = rows.filter(r => r.permanent);
  else if (permF === 'retry') rows = rows.filter(r => !r.permanent);
  if (filter) rows = rows.filter(r =>
    (r.gid || '').toLowerCase().includes(filter) ||
    (r.reason || '').toLowerCase().includes(filter) ||
    (r.site || '').toLowerCase().includes(filter));
  const permCount = Object.values(_failed).filter(r => r.permanent).length;
  document.getElementById('stat-fail').textContent = permCount;
  document.getElementById('fail-count').textContent = rows.length;
  rows.sort((a,b) => (b.date || '').localeCompare(a.date || ''));
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted" style="text-align:center;padding:20px;">
      Nothing failed. Nice.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.slice(0, 500).map(r => `
    <tr>
      <td class="mono">${escapeHtml(r.gid)}</td>
      <td><span class="pill">${escapeHtml(r.site || '')}</span></td>
      <td>${escapeHtml((r.reason||'').slice(0,80))}</td>
      <td class="mono">${r.fail_count || 0}</td>
      <td>${r.permanent ? '<span class="pill fail">permanent</span>' : '<span class="pill">retry</span>'}</td>
    </tr>
  `).join('');
}

// ── Video preview modal ──────────────────────────────────────────────────
function playVideo(path, title) {
  if (!path) { toast('No file path', 'error'); return; }
  const modal = document.getElementById('preview-modal');
  const vid = document.getElementById('preview-video');
  document.getElementById('preview-title').textContent = title || 'Preview';
  vid.src = '/file?path=' + encodeURIComponent(path);
  modal.classList.add('show');
  vid.play().catch(()=>{});
}
function closePreview(e) {
  if (e && e.target && e.target.id !== 'preview-modal') return;
  document.getElementById('preview-modal').classList.remove('show');
  document.getElementById('preview-video').pause();
  document.getElementById('preview-video').src = '';
}

// ── Run / stop ───────────────────────────────────────────────────────────
async function startDownload() {
  const perfs = _config.performers || [];
  if (!perfs.length) { toast('No performers configured', 'error'); return; }
  if (!await confirmDialog(
        `This will probe every enabled site for <b>${perfs.length}</b> performers and download any new videos found.`,
        {title: 'Start all downloads', tone: 'info',
         confirmLabel: 'Start', cancelLabel: 'Cancel'})) return;
  try {
    await api('/api/run', {method: 'POST'});
    toast('Started', 'success');
    setTimeout(refreshStatus, 600);
    setTimeout(refreshProgress, 600);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function runSinglePerformer() {
  if (!_selectedPerformer) { toast('Click a performer to select first', 'error'); return; }
  runSingleByName(_selectedPerformer);
}
async function runSingleByName(name) {
  try {
    await api('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({performer: name})});
    toast('Started ' + name, 'success');
    setTimeout(refreshStatus, 600);
    setTimeout(refreshProgress, 600);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function stopDownload() {
  if (!await confirmDialog(
        'Kill the current download subprocess? Partial files in progress will be deleted.',
        {title: 'Stop download', tone: 'danger',
         confirmLabel: 'Stop now', cancelLabel: 'Keep running'})) return;
  try {
    await api('/api/stop', {method:'POST'});
    toast('Stopped');
    setTimeout(refreshStatus, 800);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function enableTor() {
  toast('Starting Tor... this takes 20-60s on first run', 'info');
  try {
    const r = await api('/api/tor', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'start'})});
    if (r.proxy) {
      document.getElementById('cfg-proxy').value = r.proxy;
      _config.download_proxy = r.proxy;
      toast('Tor running → ' + r.proxy, 'success');
      loadConfig();
    } else {
      toast('Tor start failed: ' + (r.error || 'unknown'), 'error');
    }
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function runDedup() {
  if (!await confirmDialog(
        'Scan every performer folder for duplicate video files (using byte-level fingerprints) and <b>delete</b> the extras. Keeps the most descriptive filename.',
        {title: 'Run dedup', tone: 'warn',
         confirmLabel: 'Run dedup', cancelLabel: 'Cancel'})) return;
  try {
    const r = await api('/api/dedup', {method:'POST'});
    toast(r.message || 'Dedup complete', 'success');
    loadHistory();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function refreshAll() {
  await Promise.all([loadSites(), loadAuth(), loadConfig(), loadHistory(),
                     refreshStatus(), refreshProgress(), loadDisk(true)]);
  toast('Refreshed');
}

// ── Tab switching ────────────────────────────────────────────────────────
let _currentPage = 'archive';
let _liveSort = 'status';

function switchTab(page) {
  _currentPage = page;
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const active = btn.dataset.page === page;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.getElementById('page-archive').hidden = page !== 'archive';
  document.getElementById('page-live').hidden = page !== 'live';
  document.getElementById('archive-controls').style.display = page === 'archive' ? 'flex' : 'none';
  document.getElementById('live-controls').style.display = page === 'live' ? 'flex' : 'none';
  if (page === 'live') {
    liveLoadSites();
    liveRefresh();
  }
  // Update URL hash so it's shareable / refresh-friendly
  if (history.replaceState) history.replaceState(null, '', '#' + page);
}

// ── Live recording ───────────────────────────────────────────────────────
let _liveSnapshot = {available: false, models: [], summary: {}};
let _liveSites = [];

async function liveLoadSites() {
  if (_liveSites.length) return;
  try {
    const d = await api('/api/live/sites');
    _liveSites = d.sites || [];
    const sel = document.getElementById('live-new-site');
    const fsel = document.getElementById('live-site-filter');
    _liveSites.forEach(s => {
      const o1 = document.createElement('option');
      o1.value = s.name; o1.textContent = `${s.name} (${s.slug})`;
      if (s.needs_room_id) o1.dataset.needsRoomId = '1';
      sel.appendChild(o1);
      const o2 = document.createElement('option');
      o2.value = s.name; o2.textContent = s.name;
      fsel.appendChild(o2);
    });
    sel.onchange = () => {
      const opt = sel.options[sel.selectedIndex];
      const needsRoomId = opt && opt.dataset.needsRoomId === '1';
      document.getElementById('live-site-hint').textContent = needsRoomId
        ? "We'll auto-resolve the room ID from the username — no extra input needed."
        : '';
    };
  } catch(e) {
    console.error('liveLoadSites', e);
  }
}

async function liveRefresh() {
  try {
    _liveSnapshot = await api('/api/live/status');
  } catch(e) {
    console.error('liveRefresh', e);
    return;
  }
  const avail = !!_liveSnapshot.available;
  document.getElementById('live-available').style.display = avail ? '' : 'none';
  document.getElementById('live-unavailable').style.display = avail ? 'none' : '';
  if (!avail) {
    document.getElementById('live-error').textContent =
      _liveSnapshot.import_error ? 'Error: ' + _liveSnapshot.import_error : '';
    return;
  }
  const s = _liveSnapshot.summary || {};
  document.getElementById('live-stat-total').textContent = s.total ?? 0;
  document.getElementById('live-stat-running').textContent = s.running ?? 0;
  document.getElementById('live-stat-recording').textContent = s.recording ?? 0;
  document.getElementById('live-stat-size').textContent = bytesHuman(s.total_bytes || 0);
  // Top bar "Live" tab badge (only show count when > 0)
  const badge = document.getElementById('tab-live-badge');
  if ((s.recording || 0) > 0) {
    badge.textContent = s.recording; badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  renderLiveModels();
}

function liveSetSort(mode) {
  _liveSort = mode;
  document.querySelectorAll('.chip[data-sort]').forEach(el => {
    el.classList.toggle('active', el.dataset.sort === mode);
  });
  renderLiveModels();
}

function renderLiveModels() {
  const root = document.getElementById('live-models');
  let models = [...(_liveSnapshot.models || [])];
  const filter = document.getElementById('live-filter').value.toLowerCase();
  const siteF = document.getElementById('live-site-filter').value;
  const statusF = document.getElementById('live-status-filter').value;
  if (filter) models = models.filter(m => m.username.toLowerCase().includes(filter));
  if (siteF) models = models.filter(m => m.site === siteF);
  if (statusF) {
    const ok = new Set(statusF.split(','));
    models = models.filter(m => ok.has(m.status));
  }
  const statusRank = {PUBLIC:0, PRIVATE:1, ONLINE:2, OFFLINE:3, LONG_OFFLINE:4,
                      RATELIMIT:5, CLOUDFLARE:5, RESTRICTED:5, ERROR:6,
                      DELETED:7, NOTEXIST:7, NOTRUNNING:8, UNKNOWN:9};
  const sorts = {
    status: (a,b) => (statusRank[a.status]||9) - (statusRank[b.status]||9) || a.username.localeCompare(b.username),
    name:   (a,b) => a.username.localeCompare(b.username),
    site:   (a,b) => a.site.localeCompare(b.site) || a.username.localeCompare(b.username),
    size:   (a,b) => b.size_bytes - a.size_bytes,
  };
  models.sort(sorts[_liveSort] || sorts.status);

  if (!models.length) {
    root.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;">
      <svg class="big-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="9" opacity=".35"/></svg>
      <div>No models tracked. Add one above.</div>
    </div>`;
    return;
  }

  root.innerHTML = models.map(m => {
    const dot = `<span class="state-dot ${m.status_color}" aria-hidden="true"></span>`;
    const cls = [];
    if (m.recording) cls.push('recording');
    if (m.status === 'PUBLIC') cls.push('live-now');
    if (m.status === 'ERROR' || m.status === 'NOTEXIST' || m.status === 'DELETED') cls.push('error-state');
    if (!m.running && (m.status === 'OFFLINE' || m.status === 'NOTRUNNING')) cls.push('paused-state');
    const size = m.size_bytes ? bytesHuman(m.size_bytes) : '—';

    // Hero thumbnail
    const hero = m.thumb_url || m.avatar_url;
    const heroHtml = hero
      ? `<div class="hero${m.avatar_url && !m.thumb_url ? ' small' : ''}"
              style="background-image: url('${escapeHtml(hero)}');"></div>`
      : '';

    // Badges: country / age / gender / language / viewers / duration
    const badges = [];
    if (m.country) badges.push(`<span class="badge country" data-tip="Country">${_flagEmoji(m.country)} ${escapeHtml(m.country)}</span>`);
    if (m.gender) badges.push(`<span class="badge" data-tip="Gender">${_genderIcon(m.gender)} ${escapeHtml(m.gender)}</span>`);
    if (m.age) badges.push(`<span class="badge age" data-tip="Age">${m.age}</span>`);
    if (m.language) badges.push(`<span class="badge language">${escapeHtml(m.language)}</span>`);
    if (m.spectators != null && m.status === 'PUBLIC')
      badges.push(`<span class="badge viewers" data-tip="Viewers now">👁 ${numHuman(m.spectators)}</span>`);
    if (m.stream_duration_s && m.status === 'PUBLIC')
      badges.push(`<span class="badge duration" data-tip="Streaming for">${secsHuman(m.stream_duration_s)}</span>`);
    const badgesHtml = badges.length
      ? `<div class="badges">${badges.join('')}</div>` : '';

    // Tags (top 5)
    const tags = (m.tags || []).slice(0, 5);
    const tagsHtml = tags.length
      ? `<div class="tags">${tags.map(t =>
          `<span class="tag-chip">#${escapeHtml(t)}</span>`).join('')}</div>`
      : '';

    // Frequency / history
    const lastOn = m.last_online_ts ? relTime(m.last_online_ts) : '—';
    const nextPred = m.next_predicted_ts ? relTime(m.next_predicted_ts) : '—';
    const hoursW = m.online_hours_7d ? m.online_hours_7d.toFixed(1) + 'h' : '—';
    const sessW = m.online_sessions_7d || 0;
    const avgMin = m.avg_session_minutes ? m.avg_session_minutes + 'm' : '—';
    const lastOnClass = m.last_online_ts ? 'good' : '';
    const freqGrid = (m.last_online_ts || m.next_predicted_ts || sessW > 0)
      ? `<div class="freq-grid">
          <span class="k">Last online</span>
          <span class="v ${lastOnClass}" title="${escapeHtml(m.last_online_ts || '')}">${lastOn}</span>
          <span class="k">Next (pred.)</span>
          <span class="v accent" title="${escapeHtml(m.next_predicted_ts || '')}">${nextPred}</span>
          <span class="k">Sessions/7d</span>
          <span class="v">${sessW}</span>
          <span class="k">Hours/7d</span>
          <span class="v">${hoursW}</span>
          <span class="k">Avg session</span>
          <span class="v">${avgMin}</span>
          <span class="k">Total recorded</span>
          <span class="v good">${size}</span>
        </div>`
      : '';

    // Actions: state-appropriate
    // running + recording → Pause (stops recording, keeps polling) + Stop
    // running + not recording (polling) → Pause + Stop
    // not running → Resume (= Start polling)
    const u = escapeHtml(m.username);
    const s = escapeHtml(m.site);
    let actions = '';
    if (m.running) {
      actions = `
        <button class="warn" onclick="livePause('${u}','${s}')"
                data-tip="Pause polling (can resume later)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="4" x2="6" y2="20"/><line x1="18" y1="4" x2="18" y2="20"/></svg>
          Pause
        </button>
        <button class="danger" onclick="liveStop('${u}','${s}')"
                data-tip="Stop + close any active recording">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="14" height="14" rx="1"/></svg>
          Stop
        </button>`;
    } else {
      actions = `
        <button class="success" onclick="liveStart('${u}','${s}')"
                data-tip="Resume polling">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          Start
        </button>`;
    }
    // Folder — opens downloads/_live/<user> [SITE]/ in explorer
    actions += `
      <button class="ghost icon-only" onclick="liveOpenFolder('${u}','${escapeHtml(m.site_slug || m.site)}')"
              data-tip="Open recordings folder on disk" aria-label="Open folder">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      </button>`;
    actions += `
      <button class="ghost icon-only" onclick="liveRemove('${u}','${s}')"
              data-tip="Remove from tracking (X)" aria-label="Remove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>`;

    return `<div class="live-card ${cls.join(' ')}">
      ${heroHtml}
      <div class="top">
        <span class="username" title="${escapeHtml(m.username)}">${escapeHtml(m.username)}</span>
        <span class="site-chip" data-tip="Site slug: ${escapeHtml(m.site_slug)}">${escapeHtml(m.site)}</span>
      </div>
      <div class="status-row">
        ${dot}
        <span class="state-label ${m.status_color}">${escapeHtml(m.status_label)}</span>
        <span style="flex:1"></span>
        ${m.followers != null ? `<span class="meta" data-tip="Followers">♥ ${numHuman(m.followers)}</span>` : ''}
      </div>
      ${badgesHtml}
      ${tagsHtml}
      ${freqGrid}
      <div class="actions">${actions}</div>
    </div>`;
  }).join('');
}

function _flagEmoji(cc) {
  if (!cc || cc.length !== 2) return '🌐';
  const base = 0x1F1E6;
  const A = 'A'.charCodeAt(0);
  return String.fromCodePoint(base + (cc.toUpperCase().charCodeAt(0) - A))
       + String.fromCodePoint(base + (cc.toUpperCase().charCodeAt(1) - A));
}
function _genderIcon(g) {
  const s = (g || '').toLowerCase();
  return s.startsWith('f') ? '♀'
       : s.startsWith('m') ? '♂'
       : s.startsWith('t') ? '⚧'
       : s.startsWith('c') || s.startsWith('b') ? '⚤'
       : '';
}

function numHuman(n) {
  if (n == null) return '';
  if (n >= 1e6) return (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1).replace(/\.0$/,'') + 'k';
  return '' + Math.round(n);
}

function relTime(iso) {
  if (!iso) return '—';
  const now = Date.now();
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '—';
  const diffSec = Math.floor((t - now) / 1000);
  const future = diffSec > 0;
  const d = Math.abs(diffSec);
  let out;
  if (d < 60) out = 'now';
  else if (d < 3600) out = Math.floor(d/60) + 'm';
  else if (d < 86400) out = Math.floor(d/3600) + 'h';
  else if (d < 7*86400) out = Math.floor(d/86400) + 'd';
  else out = Math.floor(d/86400) + 'd';
  return future ? 'in ' + out : out + ' ago';
}

async function liveAdd() {
  const username = document.getElementById('live-new-username').value.trim();
  const site = document.getElementById('live-new-site').value;
  if (!username || !site) { toast('Pick a site + enter username', 'error'); return; }
  const btn = document.getElementById('live-add-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Resolving…';
  try {
    await api('/api/live/add', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, site})});
    toast(`Tracking ${username} [${site}]`, 'success');
    document.getElementById('live-new-username').value = '';
    liveRefresh();
  } catch(e) {
    toast('Could not add: '+e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
async function liveRemove(username, site) {
  if (!await confirmDialog(
        `Stop tracking <b>${escapeHtml(username)}</b> on <b>${escapeHtml(site)}</b>? Existing recorded files stay on disk.`,
        {title: 'Remove live model', tone: 'warn',
         confirmLabel: 'Remove', cancelLabel: 'Keep'})) return;
  try {
    await api('/api/live/remove', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, site})});
    toast(`Removed ${username}`);
    liveRefresh();
  } catch(e) { toast('Error: '+e.message, 'error'); }
}
async function liveStart(username, site) {
  try {
    await api('/api/live/start', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, site})});
    toast(`Started ${username}`, 'success');
    setTimeout(liveRefresh, 400);
  } catch(e) { toast('Error: '+e.message, 'error'); }
}
async function liveStop(username, site) {
  try {
    await api('/api/live/stop', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, site})});
    toast(`Stopped ${username}`);
    setTimeout(liveRefresh, 400);
  } catch(e) { toast('Error: '+e.message, 'error'); }
}
async function livePause(username, site) {
  // Soft-stop: keep the model in the tracking list but stop polling.
  // Resume via the Start button. Backend just calls stop() without
  // removing — same endpoint, different UX framing.
  try {
    await api('/api/live/pause', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, site})});
    toast(`Paused ${username}`);
    setTimeout(liveRefresh, 400);
  } catch(e) { toast('Error: '+e.message, 'error'); }
}
async function liveOpenFolder(username, siteSlug) {
  // Recordings live at downloads/_live/<username> [SLUG]/
  const folder = `_live/${username} [${siteSlug}]`;
  try {
    await api('/api/open-folder', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: folder})});
    toast(`Opened folder for ${username}`);
  } catch(e) { toast('Folder not found — no recordings yet?', 'error'); }
}
async function openLocalPath(path) {
  // Generic "open this file/folder on disk" helper used across the UI.
  try {
    await api('/api/open-folder', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path})});
  } catch(e) { toast(e.message || 'Could not open', 'error'); }
}
async function liveToggleAll(on) {
  if (!await confirmDialog(
        on ? 'Start polling every tracked model. Recording will begin automatically when any of them go live.'
           : 'Stop polling all models. Running recordings will be terminated.',
        {title: on ? 'Start all live' : 'Stop all live',
         tone: on ? 'info' : 'danger',
         confirmLabel: on ? 'Start all' : 'Stop all'})) return;
  try {
    const r = await api('/api/live/toggle_all', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({running: on})});
    toast(`${on ? 'Started' : 'Stopped'} ${r.count} models`, 'success');
    setTimeout(liveRefresh, 500);
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

// ── Disk / Storage ───────────────────────────────────────────────────────
let _diskSnapshot = null;
let _diskSort = 'size';

async function loadDisk(force = false) {
  try {
    _diskSnapshot = await api('/api/disk' + (force ? '?force=1' : ''));
    renderDisk();
  } catch(e) { console.error('loadDisk', e); }
}

function diskSetSort(mode) {
  _diskSort = mode;
  document.querySelectorAll('[data-disk-sort]').forEach(el => {
    el.classList.toggle('active', el.dataset.diskSort === mode);
  });
  renderDisk();
}

function renderDisk() {
  if (!_diskSnapshot) return;
  const d = _diskSnapshot;
  const drv = d.drive || {};
  const total = drv.total_bytes || 1;
  const archivePct = 100 * (drv.archive_bytes || 0) / total;
  const otherPct = 100 * ((drv.used_bytes || 0) - (drv.archive_bytes || 0)) / total;
  const freePct = 100 * (drv.free_bytes || 0) / total;

  document.getElementById('disk-seg-archive').style.width = archivePct + '%';
  document.getElementById('disk-seg-other').style.width = Math.max(0, otherPct) + '%';
  document.getElementById('disk-seg-free').style.width = freePct + '%';

  document.getElementById('disk-lbl-archive').textContent = bytesHuman(drv.archive_bytes || 0);
  document.getElementById('disk-lbl-other').textContent = bytesHuman(Math.max(0, (drv.used_bytes||0) - (drv.archive_bytes||0)));
  document.getElementById('disk-lbl-free').textContent = bytesHuman(drv.free_bytes || 0);

  // Free-space warning
  const warn = document.getElementById('disk-lbl-warn');
  const freeGB = (drv.free_bytes || 0) / (1024**3);
  warn.className = '';
  if (freeGB < 2) {
    warn.textContent = `⚠ only ${freeGB.toFixed(1)} GB free`;
    warn.classList.add('bad');
  } else if (freeGB < 10) {
    warn.textContent = `low space: ${freeGB.toFixed(1)} GB free`;
    warn.classList.add('warn');
  } else {
    warn.textContent = '';
  }

  // Summary top-right
  document.getElementById('disk-summary').textContent =
    `${d.performers.length} performers · ${bytesHuman(drv.archive_bytes || 0)} archived`;

  // Per-performer bars
  const perfs = [...d.performers];
  const sorts = {
    size:   (a,b) => b.bytes - a.bytes,
    count:  (a,b) => b.files - a.files,
    name:   (a,b) => a.name.localeCompare(b.name),
    newest: (a,b) => (b.newest || '').localeCompare(a.newest || ''),
  };
  perfs.sort(sorts[_diskSort] || sorts.size);
  const maxBytes = perfs.length ? perfs[0].bytes || 1 : 1;

  const root = document.getElementById('disk-performers');
  root.innerHTML = perfs.map(p => {
    const pct = Math.min(100, 100 * p.bytes / maxBytes);
    return `<div class="disk-perf-row">
      <span class="pname" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
      <div class="pmeter"><div class="pmeter-fill" style="width:${pct}%"></div></div>
      <span class="psize">${bytesHuman(p.bytes)}</span>
      <span class="pcount">${p.files} files</span>
      <button class="xs danger" onclick="diskWipe('${escapeHtml(p.name).replace(/'/g,'&#39;')}')"
              data-tip="Delete all videos for this performer" aria-label="Delete ${escapeHtml(p.name)}">✕</button>
    </div>`;
  }).join('') || `<div class="empty-state">No downloads yet.</div>`;
}

async function diskWipe(performer) {
  if (!await confirmDialog(
        `Permanently delete every video for <b>${escapeHtml(performer)}</b>?<br><br>` +
        `This also removes matching entries from <code>history.json</code>, ` +
        `so the next run will re-download from scratch. This cannot be undone.`,
        {title: 'Wipe performer', tone: 'danger',
         confirmLabel: 'Delete all', cancelLabel: 'Cancel'})) return;
  try {
    const r = await api('/api/disk/wipe', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({performer, confirm: true})});
    toast(`Removed ${r.removed} files · freed ${bytesHuman(r.bytes_freed)}`, 'success');
    loadDisk(true);
    loadHistory();
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

async function diskPruneOlder() {
  const raw = await promptDialog(
    'Delete videos older than how many days? (We will show a dry-run preview before anything is deleted.)',
    {title: 'Prune by age', tone: 'warn',
     defaultValue: '90', inputType: 'number', placeholder: 'days',
     confirmLabel: 'Preview'});
  if (raw === null || raw === '') return;
  const days = parseInt(raw, 10);
  if (!days || days < 1) { toast('Enter a positive integer', 'error'); return; }
  try {
    const preview = await api('/api/disk/prune_older', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({days})});
    if (preview.file_count === 0) { toast('Nothing older than ' + days + ' days'); return; }
    if (!await confirmDialog(
          `Found <b>${preview.file_count}</b> files older than ${days} days.<br>` +
          `This would free <b>${bytesHuman(preview.would_free_bytes)}</b>.`,
          {title: 'Confirm prune', tone: 'danger',
           confirmLabel: 'Delete them', cancelLabel: 'Cancel'})) return;
    const r = await api('/api/disk/prune_older', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({days, apply: true})});
    toast(`Removed ${r.removed} files · freed ${bytesHuman(r.bytes_freed)}`, 'success');
    loadDisk(true); loadHistory();
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

async function diskPruneToFree() {
  const raw = await promptDialog(
    'Delete oldest videos until how many GB are free on this drive? (Dry-run first.)',
    {title: 'Free up space', tone: 'warn',
     defaultValue: '25', inputType: 'number', placeholder: 'GB',
     confirmLabel: 'Preview'});
  if (raw === null || raw === '') return;
  const gb = parseFloat(raw);
  if (!gb || gb <= 0) { toast('Enter a positive number', 'error'); return; }
  try {
    const preview = await api('/api/disk/prune_to_free', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({target_free_gb: gb})});
    if (preview.nothing_to_do) {
      toast(`Already have ${preview.already_free_gb.toFixed(1)} GB free — no action needed`);
      return;
    }
    const shortfall = preview.still_needed_bytes > 0
      ? `<br><br><span style="color: var(--warn)">Not enough old content — would still be ${bytesHuman(preview.still_needed_bytes)} short of target.</span>` : '';
    if (!await confirmDialog(
          `Would delete <b>${preview.would_delete}</b> files to free <b>${bytesHuman(preview.would_free_bytes)}</b>.${shortfall}`,
          {title: 'Confirm free-up', tone: 'danger',
           confirmLabel: 'Delete oldest', cancelLabel: 'Cancel'})) return;
    const r = await api('/api/disk/prune_to_free', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({target_free_gb: gb, apply: true})});
    toast(`Removed ${r.removed} files · freed ${bytesHuman(r.bytes_freed)}`, 'success');
    loadDisk(true); loadHistory();
  } catch(e) { toast('Error: '+e.message, 'error'); }
}

// ── Professional confirm / prompt modal ─────────────────────────────────
// Replaces native window.confirm / window.prompt with a themed dialog.
// Returns a Promise that resolves to bool (confirm) or string|null (prompt).
const _dialogIcons = {
  danger: `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
  warn:   `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
  info:   `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
};
let _dialogResolver = null;

function showDialog(opts) {
  // opts: {title, message, tone: 'danger'|'warn'|'info', confirmLabel, cancelLabel,
  //        prompt: bool, defaultValue: str}
  const tone = opts.tone || 'info';
  const head = document.getElementById('dialog-head');
  head.className = 'dialog-head ' + tone;
  document.getElementById('dialog-icon').innerHTML = _dialogIcons[tone] || _dialogIcons.info;
  document.getElementById('dialog-title').textContent = opts.title || 'Confirm';
  document.getElementById('dialog-msg').innerHTML = opts.message || '';

  const inputWrap = document.getElementById('dialog-input-wrap');
  const input = document.getElementById('dialog-input');
  if (opts.prompt) {
    inputWrap.hidden = false;
    input.value = opts.defaultValue || '';
    input.placeholder = opts.placeholder || '';
    input.type = opts.inputType || 'text';
  } else {
    inputWrap.hidden = true;
  }

  const ok = document.getElementById('dialog-ok-btn');
  const cancel = document.getElementById('dialog-cancel-btn');
  ok.textContent = opts.confirmLabel || 'OK';
  cancel.textContent = opts.cancelLabel || 'Cancel';
  cancel.style.display = opts.hideCancel ? 'none' : '';
  ok.className = tone === 'danger' ? 'danger'
                : tone === 'warn' ? 'warn' : 'primary';

  document.getElementById('dialog-modal').classList.add('show');
  setTimeout(() => (opts.prompt ? input : ok).focus(), 60);

  return new Promise((resolve) => { _dialogResolver = resolve; });
}

function dialogConfirm() {
  const inputWrap = document.getElementById('dialog-input-wrap');
  const value = inputWrap.hidden ? true : document.getElementById('dialog-input').value;
  // Snapshot + clear resolver FIRST, so closeDialog doesn't see it as a
  // backdrop-cancel and resolve the promise with `false` instead of our value.
  const resolver = _dialogResolver;
  _dialogResolver = null;
  document.getElementById('dialog-modal').classList.remove('show');
  if (resolver) resolver(value);
}
function dialogCancel() {
  const inputWrap = document.getElementById('dialog-input-wrap');
  const value = inputWrap.hidden ? false : null;
  const resolver = _dialogResolver;
  _dialogResolver = null;
  document.getElementById('dialog-modal').classList.remove('show');
  if (resolver) resolver(value);
}
function closeDialog(e) {
  // Called by backdrop click or Escape key. Ignore clicks that landed on
  // the dialog body itself (only the outer #dialog-modal backdrop counts).
  if (e && e.target && e.target.id !== 'dialog-modal') return;
  document.getElementById('dialog-modal').classList.remove('show');
  if (_dialogResolver) {
    // Backdrop/Escape close counts as cancel
    const inputWrap = document.getElementById('dialog-input-wrap');
    const resolver = _dialogResolver;
    _dialogResolver = null;
    resolver(inputWrap.hidden ? false : null);
  }
}

// Convenience shims
async function confirmDialog(message, {title='Confirm', tone='info', confirmLabel='Confirm', cancelLabel='Cancel', hideCancel=false} = {}) {
  return !!(await showDialog({title, message, tone, confirmLabel, cancelLabel, hideCancel}));
}
async function promptDialog(message, {title='Enter value', defaultValue='', tone='info', inputType='text', placeholder='', confirmLabel='OK'} = {}) {
  return showDialog({title, message, tone, prompt:true, defaultValue, inputType, placeholder, confirmLabel});
}

// ── Bulk add / import modal (shared Archive + Live) ─────────────────────
let _bulkaddMode = 'archive';   // 'archive' | 'live'

function openBulkAdd() {
  _bulkaddMode = 'archive';
  document.getElementById('bulkadd-title').textContent = 'Bulk add performers';
  document.getElementById('bulkadd-sub').innerHTML =
    'One username per line.<br>' +
    'Or upload a <code>config.json</code> export — we merge <code>performers</code> + enabled sites.';
  document.getElementById('bulkadd-text').placeholder =
    'alice_example\nbob_example\n# lines starting with # are comments';
  document.getElementById('bulkadd-modal').classList.add('show');
  setTimeout(() => document.getElementById('bulkadd-text').focus(), 30);
}

function openLiveBulkAdd() {
  _bulkaddMode = 'live';
  document.getElementById('bulkadd-title').textContent = 'Bulk add live models';
  document.getElementById('bulkadd-sub').innerHTML =
    'One per line: <code>username Site</code> — room IDs auto-resolved.<br>' +
    'Or upload a JSON array like <code>[{"username":"alice","site":"Chaturbate"}]</code>.';
  document.getElementById('bulkadd-text').placeholder =
    'alice Chaturbate\nbob StripChat\n# username site (one per line)';
  document.getElementById('bulkadd-modal').classList.add('show');
  setTimeout(() => document.getElementById('bulkadd-text').focus(), 30);
}

function closeBulkAdd(e) {
  if (e && e.target && e.target.id !== 'bulkadd-modal') return;
  document.getElementById('bulkadd-modal').classList.remove('show');
  document.getElementById('bulkadd-text').value = '';
}

function bulkaddLoadFile(e) {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = (ev) => {
    const text = ev.target.result;
    try {
      const parsed = JSON.parse(text);
      // If it's the full config with performers[], pretty-fill
      if (parsed && typeof parsed === 'object') {
        if (_bulkaddMode === 'archive') {
          const perfs = Array.isArray(parsed.performers) ? parsed.performers
                         : Array.isArray(parsed) ? parsed : [];
          document.getElementById('bulkadd-text').value = perfs.join('\n');
          // Stash the full parsed object for submit to use (merge mode)
          window._bulkaddImportedConfig = parsed.performers ? parsed : null;
        } else {
          const entries = Array.isArray(parsed) ? parsed
                         : Array.isArray(parsed.models) ? parsed.models : [];
          // Render as textarea-editable lines
          document.getElementById('bulkadd-text').value =
            entries.map(e => `${e.username||''} ${e.site||''}${e.room_id ? ' '+e.room_id : ''}`).join('\n');
          window._bulkaddImportedEntries = entries;
        }
        toast(`Loaded ${f.name}`, 'success');
      }
    } catch (err) {
      // Not JSON — treat as plaintext
      document.getElementById('bulkadd-text').value = text;
      toast(`Loaded ${f.name} (plaintext)`);
    }
  };
  reader.readAsText(f);
}

async function bulkaddSubmit() {
  const text = document.getElementById('bulkadd-text').value.trim();
  if (!text) { toast('Nothing to add', 'error'); return; }
  try {
    if (_bulkaddMode === 'archive') {
      // If we loaded a full config object, use the /import endpoint for
      // merge semantics. Otherwise just bulk-add the names.
      if (window._bulkaddImportedConfig) {
        const r = await api('/api/config/import', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({config: window._bulkaddImportedConfig})});
        toast(`Imported config: +${r.performers_added} performers, updated ${r.fields_updated.length} fields`, 'success');
        window._bulkaddImportedConfig = null;
      } else {
        const r = await api('/api/config/performer/bulk_add', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({text})});
        toast(`Added ${r.added.length} performers (total ${r.total})`, 'success');
      }
      loadConfig();
    } else {
      // Live: prefer explicit entries if a JSON file was loaded
      const body = (window._bulkaddImportedEntries && window._bulkaddImportedEntries.length)
        ? {entries: window._bulkaddImportedEntries}
        : {text};
      const r = await api('/api/live/bulk_add', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body)});
      let msg = `Added ${r.added} live models (total ${r.total})`;
      if (r.errors && r.errors.length) msg += ` · ${r.errors.length} errors`;
      toast(msg, r.errors && r.errors.length ? 'info' : 'success');
      if (r.errors && r.errors.length) console.warn('bulk-add errors:', r.errors);
      window._bulkaddImportedEntries = null;
      liveRefresh();
    }
    closeBulkAdd();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

// ── Command palette (Ctrl+K) ─────────────────────────────────────────────
const _paletteCmds = [
  {label:'Refresh everything',  kbd:'F5',     run:() => refreshAll()},
  {label:'Start all downloads', kbd:'',       run:() => startDownload()},
  {label:'Stop download',       kbd:'',       run:() => stopDownload()},
  {label:'Run dedup',           kbd:'',       run:() => runDedup()},
  {label:'Refresh storage',     kbd:'',       run:() => { switchTab('archive'); loadDisk(true); }},
  {label:'Prune old videos…',   kbd:'',       run:() => { switchTab('archive'); diskPruneOlder(); }},
  {label:'Free up disk space…', kbd:'',       run:() => { switchTab('archive'); diskPruneToFree(); }},
  {label:'Open Archive tab',    kbd:'1',      run:() => switchTab('archive')},
  {label:'Open Live tab',       kbd:'2',      run:() => switchTab('live')},
  {label:'Focus add-performer',  kbd:'',      run:() => {switchTab('archive'); document.getElementById('new-perf').focus();}},
  {label:'Bulk add performers…', kbd:'',      run:() => {switchTab('archive'); openBulkAdd();}},
  {label:'Focus add-live-model', kbd:'',      run:() => {switchTab('live'); document.getElementById('live-new-username').focus();}},
  {label:'Bulk add live models…',kbd:'',      run:() => {switchTab('live'); openLiveBulkAdd();}},
  {label:'Import JSON config…',  kbd:'',      run:() => {switchTab('archive'); openBulkAdd();}},
  {label:'Start all live models', kbd:'',     run:() => liveToggleAll(true)},
  {label:'Stop all live models',  kbd:'',     run:() => liveToggleAll(false)},
  {label:'Enable Tor',           kbd:'',      run:() => enableTor()},
];
let _paletteSelected = 0;

function openPalette() {
  const m = document.getElementById('palette-modal');
  m.classList.add('show');
  const inp = document.getElementById('palette-input');
  inp.value = ''; inp.focus();
  paletteFilter();
}
function closePalette(e) {
  if (e && e.target && e.target.id !== 'palette-modal') return;
  document.getElementById('palette-modal').classList.remove('show');
}
function paletteFilter() {
  const q = document.getElementById('palette-input').value.trim().toLowerCase();
  const matches = _paletteCmds.filter(c =>
    !q || c.label.toLowerCase().includes(q) || (c.kbd && c.kbd.toLowerCase().includes(q)));
  _paletteSelected = 0;
  document.getElementById('palette-list').innerHTML = matches.map((c, i) => `
    <div class="pcmd ${i === 0 ? 'selected' : ''}" data-idx="${i}"
         onclick="paletteRun(${_paletteCmds.indexOf(c)})">
      <span>${escapeHtml(c.label)}</span>
      ${c.kbd ? `<span class="kbd">${escapeHtml(c.kbd)}</span>` : ''}
    </div>`).join('');
  window._paletteMatches = matches;
}
function paletteKey(e) {
  const matches = window._paletteMatches || [];
  if (e.key === 'Escape') { closePalette(); return; }
  if (e.key === 'ArrowDown') {
    e.preventDefault(); _paletteSelected = (_paletteSelected + 1) % matches.length;
  } else if (e.key === 'ArrowUp') {
    e.preventDefault(); _paletteSelected = (_paletteSelected - 1 + matches.length) % matches.length;
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const cmd = matches[_paletteSelected];
    if (cmd) { closePalette(); cmd.run(); }
    return;
  } else return;
  document.querySelectorAll('#palette-list .pcmd').forEach((el, i) => {
    el.classList.toggle('selected', i === _paletteSelected);
  });
}
function paletteRun(idx) {
  const cmd = _paletteCmds[idx];
  if (cmd) { closePalette(); cmd.run(); }
}
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault(); openPalette();
  } else if (e.key === 'Escape') {
    // Close whichever modal is open (dialog first — it returns a Promise)
    const dlg = document.getElementById('dialog-modal');
    if (dlg && dlg.classList.contains('show')) { dialogCancel(); return; }
    ['palette-modal','bulkadd-modal','preview-modal'].forEach(id => {
      const m = document.getElementById(id);
      if (m && m.classList.contains('show')) m.classList.remove('show');
    });
  } else if (!e.ctrlKey && !e.metaKey && !e.altKey
             && document.activeElement && document.activeElement.tagName !== 'INPUT'
             && document.activeElement.tagName !== 'TEXTAREA') {
    if (e.key === '1') switchTab('archive');
    else if (e.key === '2') switchTab('live');
  }
});

// ── Initial load + polling ───────────────────────────────────────────────
(async () => {
  await loadSites();
  await loadAuth();
  await loadConfig();
  await loadHistory();
  await refreshStatus();
  await refreshProgress();
  await loadDisk();
  setInterval(refreshStatus, 2000);
  setInterval(refreshProgress, 700);   // fast cadence for progress bar
  setInterval(loadHistory, 15000);
  setInterval(loadAuth, 30000);
  setInterval(loadDisk, 20000);        // disk snapshot every 20s
  // Live polling — only every 3s and only when Live tab visible
  setInterval(() => { if (_currentPage === 'live') liveRefresh(); }, 3000);
  // Always pull ONCE so the "Live" tab badge can update even from Archive tab
  setInterval(async () => {
    if (_currentPage !== 'live') {
      try {
        const d = await api('/api/live/status');
        const rec = (d.summary||{}).recording || 0;
        const badge = document.getElementById('tab-live-badge');
        if (rec > 0) { badge.textContent = rec; badge.hidden = false; }
        else { badge.hidden = true; }
      } catch(e) { /* silent */ }
    }
  }, 8000);

  // Respect URL hash (#live → open Live tab)
  const initial = (location.hash || '').replace('#', '').toLowerCase();
  if (initial === 'live') switchTab('live');
})();
</script>
</body>
</html>
"""


# ── Background runner ────────────────────────────────────────────────────────
def _tail_log():
    """Continuously read tail of universal.log into _state['log_tail']."""
    last_size = 0
    while True:
        try:
            if LOG_PATH.exists():
                size = LOG_PATH.stat().st_size
                if size < last_size:
                    last_size = 0  # log rotated
                if size > last_size:
                    with open(LOG_PATH, "rb") as f:
                        f.seek(last_size)
                        new_data = f.read()
                        last_size = size
                    for line in new_data.decode("utf-8", errors="replace").splitlines():
                        with _state_lock:
                            _state["log_tail"].append(line)
                            # Try to extract current performer
                            if "Searching for: " in line or "───" in line:
                                # ─── performer ───
                                m = line.strip().replace("─", "").strip()
                                if m and len(m) < 50:
                                    _state["current_performer"] = m
        except Exception:
            pass
        time.sleep(1.5)


_tail_thread = threading.Thread(target=_tail_log, daemon=True)
_tail_thread.start()


def _monitor_subprocess():
    """Monitor the download subprocess and update _state when done."""
    global _runner_thread
    while True:
        with _state_lock:
            proc = _runner_thread
        if proc is not None:
            proc.wait()
            with _state_lock:
                _state["running"] = False
                _state["pid"] = None
                _runner_thread = None
        time.sleep(0.5)


threading.Thread(target=_monitor_subprocess, daemon=True).start()


# ── API routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify({
            "running": _state["running"],
            "pid": _state["pid"],
            "started_at": _state["started_at"],
            "current_performer": _state["current_performer"],
            "log_tail": list(_state["log_tail"])[-200:],
        })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        new_cfg = request.get_json(force=True)
        # Merge (don't wipe fields we don't know about)
        cur = load_config()
        cur.update(new_cfg)
        save_config(cur)
        return jsonify({"ok": True})
    return jsonify(load_config())


@app.route("/api/config/performer/add", methods=["POST"])
def api_add_performer():
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify({"error": "empty name"}), 400
    cfg = load_config()
    perfs = cfg.setdefault("performers", [])
    if name not in perfs:
        perfs.append(name)
        save_config(cfg)
    return jsonify({"ok": True, "performers": perfs})


@app.route("/api/config/performer/remove", methods=["POST"])
def api_remove_performer():
    name = (request.get_json(force=True).get("name") or "").strip()
    cfg = load_config()
    perfs = cfg.get("performers", [])
    cfg["performers"] = [p for p in perfs if p != name]
    save_config(cfg)
    return jsonify({"ok": True, "performers": cfg["performers"]})


@app.route("/api/config/performer/bulk_add", methods=["POST"])
def api_bulk_add_performers():
    """Add many performers at once. Accepts:
       - names: ["alice", "bob", ...]     (explicit list)
       - text: "alice\nbob\ncharlie"      (whitespace/comma separated)
    Duplicates (case-insensitive) are silently skipped."""
    body = request.get_json(force=True) or {}
    names: list[str] = []
    if isinstance(body.get("names"), list):
        names = [str(n).strip() for n in body["names"]]
    if body.get("text"):
        for chunk in str(body["text"]).replace(",", "\n").splitlines():
            names.append(chunk.strip())
    # Clean + dedup
    cfg = load_config()
    existing = {p.lower() for p in (cfg.get("performers") or [])}
    added = []
    for n in names:
        if not n or n.startswith("#"):
            continue
        if n.lower() in existing:
            continue
        existing.add(n.lower())
        cfg.setdefault("performers", []).append(n)
        added.append(n)
    if added:
        save_config(cfg)
    return jsonify({"ok": True, "added": added, "total": len(cfg.get("performers", []))})


@app.route("/api/config/import", methods=["POST"])
def api_config_import():
    """Merge a JSON config blob into the current config.json.

    Accepts the same schema as config.json itself, OR a simpler shape:
      {"performers": ["a", "b"], "enabled_sites": [...], "max_videos_per_site": 100}
    Overrides top-level scalars; unions list fields (performers + enabled_sites).
    """
    body = request.get_json(force=True) or {}
    data = body.get("config") or body
    if not isinstance(data, dict):
        return jsonify({"error": "config must be a JSON object"}), 400
    cfg = load_config()
    merged_changes = {"performers_added": 0, "fields_updated": []}

    # Union-merge list fields
    for field in ("performers", "enabled_sites"):
        if field in data and isinstance(data[field], list):
            current = list(cfg.get(field) or [])
            existing = {p.lower() if isinstance(p, str) else p for p in current}
            for n in data[field]:
                if isinstance(n, str):
                    if n.strip() and n.strip().lower() not in existing:
                        existing.add(n.strip().lower())
                        current.append(n.strip())
                        if field == "performers":
                            merged_changes["performers_added"] += 1
            cfg[field] = current

    # Scalar overrides
    for k, v in data.items():
        if k in ("performers", "enabled_sites"):
            continue
        if k in cfg or isinstance(v, (str, int, float, bool)):
            cfg[k] = v
            merged_changes["fields_updated"].append(k)

    save_config(cfg)
    return jsonify({"ok": True, **merged_changes,
                    "total_performers": len(cfg.get("performers", []))})


@app.route("/api/sites")
def api_sites():
    return jsonify({"sites": load_sites()})


@app.route("/api/sites/detailed")
def api_sites_detailed():
    return jsonify({"sites": load_sites_detailed()})


@app.route("/api/auth")
def api_auth():
    return jsonify(cookies_diagnostics())


@app.route("/api/progress")
def api_progress():
    return jsonify(read_progress())


@app.route("/api/progress/cancel", methods=["POST"])
def api_progress_cancel():
    """Cancel a specific active download by its slot id. Kills the
    underlying aria2c / curl / ffmpeg subprocess; the download loop
    catches the failure and treats the video as skip (not fail),
    then continues to the next queued item.

    Note: the download subprocess runs in a DIFFERENT process than
    the webui (python universal_downloader.py), so we can't call
    tracker.cancel_slot() directly. Instead we write the cancel
    request into the shared progress JSON file, and the downloader
    watches for it on each update cycle.
    """
    body = request.get_json(silent=True) or {}
    try:
        slot = int(body.get("slot", -1))
    except Exception:
        return jsonify({"error": "slot must be an integer"}), 400
    if slot < 0:
        return jsonify({"error": "slot required"}), 400

    # Persist cancel request in _progress.json so the downloader sees it
    progress_path = DOWNLOADS_DIR / "_progress.json"
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
    except Exception:
        data = {}
    cancelled = set(data.get("cancelled_slots") or [])
    cancelled.add(slot)
    data["cancelled_slots"] = sorted(cancelled)
    # Atomic write
    tmp = progress_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        import os as _os
        _os.replace(tmp, progress_path)
    except Exception as e:
        return jsonify({"error": f"write: {e}"}), 500
    return jsonify({"ok": True, "slot": slot})


@app.route("/api/history")
def api_history():
    return jsonify(load_json(HISTORY_PATH))


@app.route("/api/failed")
def api_failed():
    return jsonify(load_json(FAILED_PATH))


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """Open a folder (or the parent of a file) in the native file explorer.

    Body: {"path": "<absolute or relative-to-downloads path>"}

    Security: we refuse to open anything outside DOWNLOADS_DIR or SCRIPT_DIR
    so a malicious request can't navigate your whole filesystem."""
    body = request.get_json(silent=True) or {}
    raw = (body.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "path required"}), 400
    try:
        p = Path(raw)
        if not p.is_absolute():
            # Relative → interpret under downloads/
            p = DOWNLOADS_DIR / raw
        p = p.resolve()
    except Exception as e:
        return jsonify({"error": f"bad path: {e}"}), 400

    # Security: stay within our downloads tree (or script dir)
    roots = [DOWNLOADS_DIR.resolve(), SCRIPT_DIR.resolve()]
    if not any(str(p).lower().startswith(str(r).lower()) for r in roots):
        return jsonify({"error": "path outside downloads directory"}), 403

    # If the path doesn't exist but a parent does, open the nearest parent
    target = p if p.exists() else (p.parent if p.parent.exists() else None)
    if target is None:
        return jsonify({"error": f"not found: {p}"}), 404

    # If target is a file, open its parent folder (and highlight it on Windows)
    try:
        if sys.platform == "win32":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target if target.is_dir() else target.parent)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
    except Exception as e:
        return jsonify({"error": f"open: {e}"}), 500
    return jsonify({"ok": True, "opened": str(target)})


@app.route("/api/history/reset", methods=["POST"])
def api_history_reset():
    """Reset the download-history ledger so videos get re-downloaded even
    if files were previously recorded as OK.

    Body:
      {"performer": "<name>"}  → clear only this performer's entries
      {"performer": "<name>", "sites": ["youtube", ...]} → only on these sites
      {"all": true}            → clear every performer (requires explicit flag)
      {"include_failed": true} → also clear failed.json for this performer

    Returns counts of entries removed."""
    body = request.get_json(silent=True) or {}
    performer = (body.get("performer") or "").strip()
    sites_filter = body.get("sites") or []
    clear_all = bool(body.get("all", False))
    include_failed = bool(body.get("include_failed", True))

    if not performer and not clear_all:
        return jsonify({"error": "performer required (or pass {all:true})"}), 400

    # Load
    history = load_json(HISTORY_PATH) or {}
    failed = load_json(FAILED_PATH) or {} if include_failed else {}

    removed_history = 0
    removed_failed = 0

    def _clear_performer(perf_key: str) -> None:
        nonlocal removed_history, removed_failed
        entries = history.get(perf_key) or {}
        if isinstance(entries, dict):
            if sites_filter:
                keep = {}
                for k, v in entries.items():
                    site = (v or {}).get("site") or (k.split("|")[0] if "|" in k else "")
                    if site in sites_filter:
                        removed_history += 1
                    else:
                        keep[k] = v
                history[perf_key] = keep
            else:
                removed_history += len(entries)
                history.pop(perf_key, None)
        if include_failed and isinstance(failed, dict):
            f_entries = failed.get(perf_key) or {}
            if isinstance(f_entries, dict):
                if sites_filter:
                    keep_f = {}
                    for k, v in f_entries.items():
                        site = (v or {}).get("site") or (k.split("|")[0] if "|" in k else "")
                        if site in sites_filter:
                            removed_failed += 1
                        else:
                            keep_f[k] = v
                    failed[perf_key] = keep_f
                else:
                    removed_failed += len(f_entries)
                    failed.pop(perf_key, None)

    if clear_all:
        for perf_key in list(history.keys()):
            _clear_performer(perf_key)
    else:
        # Case-insensitive match across known keys
        target_lower = performer.lower()
        matched = [k for k in history.keys() if k.lower() == target_lower]
        for perf_key in matched:
            _clear_performer(perf_key)
        # Also clear in failed.json even if no history row existed
        if include_failed and not matched:
            for f_key in list(failed.keys()):
                if f_key.lower() == target_lower:
                    _clear_performer(f_key)

    # Persist atomically
    try:
        save_json(HISTORY_PATH, history)
        if include_failed:
            save_json(FAILED_PATH, failed)
    except Exception as e:
        return jsonify({"error": f"write: {e}"}), 500

    return jsonify({
        "ok": True,
        "performer": performer or "(all)",
        "removed_history": removed_history,
        "removed_failed": removed_failed,
        "sites_filter": sites_filter,
    })


@app.route("/api/site-health")
def api_site_health():
    """Per-site success/fail history + drift classification. The UI uses
    this to flag sites that used to work but now fail every download."""
    path = DOWNLOADS_DIR / "_site_health.json"
    if not path.exists():
        return jsonify({"sites": {}, "updated_at": ""})
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        return jsonify({"error": f"read: {e}", "sites": {}}), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    global _runner_thread
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "already running"}), 400

    # Optional: specific performer
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    performer = body.get("performer")

    cmd = [sys.executable, str(SCRIPT_DIR / "universal_downloader.py")]
    if performer:
        cmd.append(performer)
    else:
        cmd.append("--all")

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(SCRIPT_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with _state_lock:
        _state["running"] = True
        _state["pid"] = proc.pid
        _state["started_at"] = datetime.now().isoformat()
        _state["current_performer"] = performer or "(all)"
        _runner_thread = proc
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _runner_thread
    with _state_lock:
        proc = _runner_thread
    if not proc:
        return jsonify({"error": "not running"}), 400
    try:
        # On Windows, terminate kills the process tree
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                          capture_output=True, timeout=10)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with _state_lock:
        _state["running"] = False
        _state["pid"] = None
        _runner_thread = None
    return jsonify({"ok": True})


@app.route("/api/disk")
def api_disk():
    """Snapshot: per-performer bytes, per-site totals, free space on drive.
    Cached for 3 s on the backend — safe to poll aggressively."""
    if not _disk:
        return jsonify({"error": "disk manager unavailable"}), 500
    force = request.args.get("force") == "1"
    return jsonify(_disk.snapshot(force=force))


@app.route("/api/disk/wipe", methods=["POST"])
def api_disk_wipe():
    """Delete every video for one performer. Hard op — requires confirm."""
    if not _disk:
        return jsonify({"error": "disk manager unavailable"}), 500
    body = request.get_json(force=True) or {}
    performer = (body.get("performer") or "").strip()
    if not performer:
        return jsonify({"error": "performer required"}), 400
    if not body.get("confirm"):
        return jsonify({"error": "confirm=true required for destructive op"}), 400
    return jsonify(_disk.wipe_performer(performer))


@app.route("/api/disk/delete", methods=["POST"])
def api_disk_delete():
    """Delete a specific list of paths."""
    if not _disk:
        return jsonify({"error": "disk manager unavailable"}), 500
    body = request.get_json(force=True) or {}
    paths = body.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return jsonify({"error": "paths[] required"}), 400
    if not body.get("confirm"):
        return jsonify({"error": "confirm=true required"}), 400
    return jsonify(_disk.delete_files(paths))


@app.route("/api/disk/prune_older", methods=["POST"])
def api_disk_prune_older():
    if not _disk:
        return jsonify({"error": "disk manager unavailable"}), 500
    body = request.get_json(force=True) or {}
    days = int(body.get("days") or 0)
    if days < 1:
        return jsonify({"error": "days must be >= 1"}), 400
    return jsonify(_disk.prune_older_than(days, apply=bool(body.get("apply"))))


@app.route("/api/disk/prune_to_free", methods=["POST"])
def api_disk_prune_to_free():
    if not _disk:
        return jsonify({"error": "disk manager unavailable"}), 500
    body = request.get_json(force=True) or {}
    target = float(body.get("target_free_gb") or 0.0)
    if target <= 0:
        return jsonify({"error": "target_free_gb must be > 0"}), 400
    return jsonify(_disk.prune_to_free(target, apply=bool(body.get("apply"))))


@app.route("/api/disk/enforce_cap", methods=["POST"])
def api_disk_enforce_cap():
    if not _disk:
        return jsonify({"error": "disk manager unavailable"}), 500
    body = request.get_json(force=True) or {}
    performer = (body.get("performer") or "").strip()
    cap = float(body.get("max_gb") or 0.0)
    if not performer or cap <= 0:
        return jsonify({"error": "performer + max_gb required"}), 400
    return jsonify(_disk.enforce_performer_cap(performer, cap,
                                               apply=bool(body.get("apply"))))


@app.route("/api/live/sites")
def api_live_sites():
    """Supported cam sites from StreaMonitor."""
    if not _live:
        return jsonify({"available": False, "sites": []})
    return jsonify({"available": _live_available, "sites": _live.list_sites()})


@app.route("/api/live/status")
def api_live_status():
    """Snapshot of every live model and its current state."""
    if not _live:
        return jsonify({"available": False, "models": [], "summary": {}})
    return jsonify(_live.get_snapshot())


@app.route("/api/live/bulk_add", methods=["POST"])
def api_live_bulk_add():
    """Bulk-add live models. Accepts:
      - entries: [{username, site, room_id?}, ...]
      - text: "alice Chaturbate\nbob StripChat 12345\n# comments ok"
             (whitespace-separated: username, site, optional room_id)
    Unknown sites skipped with an error, dupes skipped silently."""
    if not _live:
        return jsonify({"error": "live recording unavailable"}), 503
    body = request.get_json(force=True) or {}
    entries: list[dict] = []
    # Explicit list
    if isinstance(body.get("entries"), list):
        entries.extend(body["entries"])
    # Free-text paste
    if body.get("text"):
        available_sites = {s["name"].lower(): s["name"] for s in (_live.list_sites() or [])}
        for line in str(body["text"]).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            user = parts[0]
            site_raw = parts[1]
            # Case-insensitive site match
            site = available_sites.get(site_raw.lower(), site_raw)
            room_id = parts[2] if len(parts) >= 3 else None
            entries.append({"username": user, "site": site, "room_id": room_id})
    try:
        return jsonify(_live.bulk_add(entries))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/live/add", methods=["POST"])
def api_live_add():
    if not _live:
        return jsonify({"error": "live recording unavailable (StreaMonitor not installed)"}), 503
    body = request.get_json(force=True) or {}
    try:
        r = _live.add_model(body.get("username", ""), body.get("site", ""),
                             room_id=body.get("room_id") or None)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(r)


@app.route("/api/live/remove", methods=["POST"])
def api_live_remove():
    if not _live:
        return jsonify({"error": "live recording unavailable"}), 503
    body = request.get_json(force=True) or {}
    try:
        r = _live.remove_model(body.get("username", ""), body.get("site", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(r)


@app.route("/api/live/start", methods=["POST"])
def api_live_start():
    if not _live:
        return jsonify({"error": "live recording unavailable"}), 503
    body = request.get_json(force=True) or {}
    try:
        r = _live.start_model(body.get("username", ""), body.get("site", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(r)


@app.route("/api/live/stop", methods=["POST"])
def api_live_stop():
    if not _live:
        return jsonify({"error": "live recording unavailable"}), 503
    body = request.get_json(force=True) or {}
    try:
        r = _live.stop_model(body.get("username", ""), body.get("site", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(r)


@app.route("/api/live/pause", methods=["POST"])
def api_live_pause():
    """Pause is a soft-stop: model stays in the list, thread stops, the UI
    shows a 'PAUSED' chip and a Start button to resume. Internally the
    same as stop_model() — the UI just presents it differently."""
    if not _live:
        return jsonify({"error": "live recording unavailable"}), 503
    body = request.get_json(force=True) or {}
    try:
        r = _live.stop_model(body.get("username", ""), body.get("site", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(r)


@app.route("/api/live/toggle_all", methods=["POST"])
def api_live_toggle_all():
    if not _live:
        return jsonify({"error": "live recording unavailable"}), 503
    body = request.get_json(force=True) or {}
    return jsonify(_live.toggle_all(bool(body.get("running", True))))


@app.route("/api/tor", methods=["POST"])
def api_tor():
    """Start the embedded Tor helper, wait for bootstrap, return the SOCKS
    URL. Called by the UI when user clicks "Use Tor" in the auth panel —
    the returned URL is auto-filled into config.download_proxy.

    Also supports action=stop to kill tor.exe and action=status for a
    cheap ping. Long-running on first --start (20-60 s bootstrap)."""
    body = request.get_json(silent=True) or {}
    action = body.get("action", "start")
    try:
        if action == "stop":
            r = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "tor_helper.py"), "--stop"],
                capture_output=True, text=True, timeout=10,
            )
            return jsonify({"ok": True, "stopped": True, "stdout": (r.stdout or r.stderr)[-500:]})

        if action == "status":
            r = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "tor_helper.py"), "--status"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip().startswith("socks5://"):
                return jsonify({"ok": True, "running": True, "proxy": r.stdout.strip()})
            return jsonify({"ok": True, "running": False})

        # action == "start"
        r = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "tor_helper.py"), "--start"],
            capture_output=True, text=True, timeout=180,
        )
        proxy = r.stdout.strip()
        if r.returncode != 0 or not proxy.startswith("socks5://"):
            return jsonify({
                "error": "Tor failed to start",
                "stderr": (r.stderr or r.stdout)[-800:],
            }), 500

        # Auto-save to config so subsequent runs use it
        cfg = load_config()
        cfg["download_proxy"] = proxy
        save_config(cfg)
        return jsonify({"ok": True, "proxy": proxy,
                        "message": f"Tor running at {proxy}; saved to config.download_proxy"})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Tor bootstrap timed out after 3 minutes"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dedup", methods=["POST"])
def api_dedup():
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "dedupe.py"), "--apply"],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR), timeout=600,
        )
        # Parse last "GRAND TOTAL" line
        out = r.stdout or ""
        message = "Dedup complete."
        for line in out.splitlines():
            if "GRAND TOTAL" in line or "freed" in line:
                message = line.strip()
        return jsonify({"ok": True, "message": message, "stdout": out[-2000:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/file")
def api_file():
    """Serve a video file from disk (for inline playback)."""
    path = request.args.get("path", "")
    if not path:
        return "path required", 400
    # Safety: only allow files inside downloads/
    p = Path(path).resolve()
    if not str(p).startswith(str(DOWNLOADS_DIR.resolve())):
        return "access denied", 403
    if not p.exists() or not p.is_file():
        return "not found", 404
    return send_file(str(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args()
    # Force UTF-8 output on Windows cp1252 consoles
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    msg = f"\n==  Harvestr UI running at http://{args.host}:{args.port}  ==\n"
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
