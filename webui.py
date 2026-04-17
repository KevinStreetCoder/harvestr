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
    position: sticky; top: 0; z-index: 20;
    backdrop-filter: blur(8px);
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
                background: #0b1320; border: 1px solid #1c2438; border-radius: 8px; }
  .dl-active .top { display: flex; align-items: baseline; gap: 10px; font-size: 12.5px; margin-bottom: 6px; }
  .dl-active .top .title { flex: 1; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dl-active .top .meta { color: var(--text-3); font-size: 11.5px; font-family: "JetBrains Mono", monospace; }
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

  /* Tooltips */
  [data-tip] { position: relative; }
  [data-tip]:hover::after {
    content: attr(data-tip); position: absolute; z-index: 100;
    bottom: 100%; left: 50%; transform: translateX(-50%);
    background: #0a0e18; color: var(--text); padding: 6px 10px;
    border-radius: 6px; font-size: 11.5px; white-space: nowrap;
    border: 1px solid var(--border-2); margin-bottom: 5px;
    box-shadow: 0 4px 12px #00000080;
  }

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

  /* Toast */
  .toast {
    position: fixed; top: 74px; right: 24px;
    padding: 12px 16px; background: linear-gradient(180deg, #2f8ae0, #2a6cb3);
    color: white; border-radius: 8px; font-size: 13px; font-weight: 500;
    opacity: 0; transform: translateY(-8px); transition: all .3s;
    box-shadow: 0 6px 20px #00000080; z-index: 200;
    border: 1px solid #3b8ce6;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.error { background: linear-gradient(180deg, #e85656, #a8381b); border-color: #e85656; }
  .toast.success { background: linear-gradient(180deg, #34c26e, #1b7d3b); border-color: #34c26e; }

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

  <span class="status-pill">
    <span class="status-dot" id="status-dot"></span>
    <span id="status-text">idle</span>
  </span>

  <div style="flex:1"></div>

  <button class="primary" id="start-btn" onclick="startDownload()"
          data-tip="Run every performer in config">▶&nbsp; Start all</button>
  <button class="danger" id="stop-btn" onclick="stopDownload()" disabled
          data-tip="Kill the running subprocess">■&nbsp; Stop</button>
  <button onclick="runDedup()" data-tip="Scan + delete duplicate files">⌥&nbsp; Dedup</button>
  <button class="ghost" onclick="refreshAll()" data-tip="Reload config / sites / history">↻</button>
</header>

<div id="toast" class="toast"></div>

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
      <div class="flex mb">
        <input id="new-perf" type="text" placeholder="username (e.g. blondie_254)"
               onkeydown="if(event.key==='Enter'){addPerformer();}" />
        <button class="primary" onclick="addPerformer()">+ Add</button>
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
        <tr><td>Download proxy</td><td><input id="cfg-proxy" type="text" placeholder="http://host:port, socks5://127.0.0.1:9150 (Tor), blank = none"/></td></tr>
        <tr><td>CamSmut user</td><td><input id="cfg-cs-user" type="text" placeholder="(empty = skip camsmut)"/></td></tr>
        <tr><td>CamSmut password</td><td><input id="cfg-cs-pass" type="password" placeholder=""/></td></tr>
      </table>
      <div style="margin-top: 12px;">
        <button class="primary" onclick="saveSettings()">Save settings</button>
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
function toast(msg, type='') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + type;
  setTimeout(() => t.className = 'toast ' + type, 3000);
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
          .map(h => `<span class="pill hit-pill">${escapeHtml(h.site)} · ${h.count}</span>`)
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
        return `<div class="dl-active">
          <div class="top">
            <span class="pill ${a.backend === 'yt-dlp' ? 'ytdlp' : 'custom'}">${escapeHtml(a.site || '?')}</span>
            <span class="title" title="${escapeHtml(a.title || '')}">${escapeHtml(a.title || a.video_id || '')}</span>
            <span class="meta">${done} / ${total} · ${speed} · ETA ${eta} · ${pct.toFixed(1)}%</span>
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
  renderPerformers();
  renderSites();
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
  if (!confirm('Remove ' + name + '?')) return;
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
  if (!confirm(`Start download for ${perfs.length} performers?`)) return;
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
  if (!confirm('Stop running download?')) return;
  try {
    await api('/api/stop', {method:'POST'});
    toast('Stopped');
    setTimeout(refreshStatus, 800);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function runDedup() {
  if (!confirm('Run content-based dedup? Deletes duplicate video files (keeps the most descriptive copy).')) return;
  try {
    const r = await api('/api/dedup', {method:'POST'});
    toast(r.message || 'Dedup complete', 'success');
    loadHistory();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}
async function refreshAll() {
  await Promise.all([loadSites(), loadAuth(), loadConfig(), loadHistory(), refreshStatus(), refreshProgress()]);
  toast('Refreshed');
}

// ── Initial load + polling ───────────────────────────────────────────────
(async () => {
  await loadSites();
  await loadAuth();
  await loadConfig();
  await loadHistory();
  await refreshStatus();
  await refreshProgress();
  setInterval(refreshStatus, 2000);
  setInterval(refreshProgress, 700);   // fast cadence for progress bar
  setInterval(loadHistory, 15000);
  setInterval(loadAuth, 30000);
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


@app.route("/api/history")
def api_history():
    return jsonify(load_json(HISTORY_PATH))


@app.route("/api/failed")
def api_failed():
    return jsonify(load_json(FAILED_PATH))


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
