#!/usr/bin/env python3
"""
Live cam recording integration for Harvestr.

Bridges Harvestr's web UI to the StreaMonitor project
(https://github.com/lossless1/StreaMonitor or its fork at ./StreaMonitor).
StreaMonitor has 19 mature site modules (Chaturbate, StripChat, CamSoda,
Cam4, BongaCams, Flirt4Free, Cherry.tv, Streamate, MyFreeCams, ManyVids,
FanslyLive, AmateurTV, CamsCom, DreamCam, SexChatHu, XloveCam, ...) that
each implement a uniform Bot API with a clean Status enum.

This module:
  - Discovers a local StreaMonitor install via env var HARVESTR_STREAMONITOR
    or the known path C:\\F\\StreaMonitor
  - Lazily imports Bot, Status, and the 19 site classes
  - Provides a LiveManager class that the web UI can talk to:
        add_model, remove_model, start_model, stop_model, list_models,
        get_status_snapshot, get_sites
  - Persists the model list to downloads/live_models.json (separate from
    Harvestr's regular config.json — different purpose, different cadence)
  - Runs each model as a daemon thread (StreaMonitor's built-in pattern)

Design notes:
  - We do NOT re-implement the 19 site extractors. They're 200-500 lines
    each of carefully-tuned reverse-engineering. Instead we import them.
  - If StreaMonitor isn't available, this module exposes available=False
    and the UI shows a friendly "install StreaMonitor to enable live"
    banner instead of crashing.
  - Recording output goes to <downloads>/<performer> [SITE]/N.mkv,
    matching StreaMonitor's layout exactly so StreaMonitor's own tools
    (untrunc, move, etc.) keep working.
  - Config file is a flat JSON array of
        {"username": "alice", "site": "Chaturbate", "running": true,
         "room_id": "12345" (optional)}
    objects. Identical schema to StreaMonitor's config.json so you can
    copy yours over.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("harvestr.live")

# ── Discovery ────────────────────────────────────────────────────────────────

# Default candidate paths (Windows-centric since the project is Windows-first).
_CANDIDATES = [
    os.environ.get("HARVESTR_STREAMONITOR", ""),
    r"C:\F\StreaMonitor",
    r"D:\F\StreaMonitor",
    str(Path.home() / "StreaMonitor"),
    str(Path.home() / "Documents" / "StreaMonitor"),
    str(Path(__file__).resolve().parent / "StreaMonitor"),
]

_STREAMONITOR_PATH: Optional[str] = None
for _cand in _CANDIDATES:
    if _cand and (Path(_cand) / "streamonitor" / "bot.py").exists():
        _STREAMONITOR_PATH = _cand
        break


# ── Try to import the Bot framework ──────────────────────────────────────────

available = False
import_error: Optional[str] = None
Bot = None            # type: ignore
RoomIdBot = None      # type: ignore
Status = None         # type: ignore
SITES: Dict[str, type] = {}   # "Chaturbate" -> Chaturbate class

if _STREAMONITOR_PATH:
    try:
        if _STREAMONITOR_PATH not in sys.path:
            sys.path.insert(0, _STREAMONITOR_PATH)
        # Some StreaMonitor site modules import `parameters` and
        # `Controller` which expect the repo cwd. Give them an env hint.
        os.environ.setdefault("STRMNTR_DOWNLOAD_DIR",
                               str(Path(__file__).resolve().parent / "downloads"))
        from streamonitor.bot import Bot as _Bot, RoomIdBot as _RoomIdBot   # noqa
        from streamonitor.enums.status import Status as _Status             # noqa
        Bot = _Bot
        RoomIdBot = _RoomIdBot
        Status = _Status

        # Import all site classes by walking the package.
        import pkgutil
        import importlib
        import streamonitor.sites as _sites_pkg
        for mod_info in pkgutil.iter_modules(_sites_pkg.__path__):
            try:
                mod = importlib.import_module(f"streamonitor.sites.{mod_info.name}")
            except Exception as e:
                log.debug(f"  [live] skip site {mod_info.name}: {e}")
                continue
            # Every site module defines exactly one Bot subclass with
            # class attribute `site` (str).
            for attr in dir(mod):
                obj = getattr(mod, attr)
                try:
                    if (isinstance(obj, type) and issubclass(obj, Bot)
                            and obj is not Bot and obj is not RoomIdBot
                            and getattr(obj, "site", None)):
                        SITES[obj.site] = obj
                except Exception:
                    pass
        available = True
        log.info(f"  [live] StreaMonitor found at {_STREAMONITOR_PATH} "
                 f"— {len(SITES)} site modules loaded")
    except Exception as e:
        import_error = f"{type(e).__name__}: {e}"
        log.warning(f"  [live] StreaMonitor import failed ({import_error}); "
                    f"live features disabled")
else:
    import_error = "StreaMonitor not found at any candidate path"
    log.info(f"  [live] {import_error}. Set HARVESTR_STREAMONITOR env var "
             f"or place StreaMonitor at C:\\F\\StreaMonitor.")


# ── Status mapping (StreaMonitor Status enum → UI-friendly strings) ──────────

# Human-readable + UI-color for the status pill. These mirror the semantics
# used in StreaMonitor's own truck-kun skin but with a cleaner palette.
STATUS_UI: Dict[str, Tuple[str, str]] = {
    "UNKNOWN":      ("unknown",    "text-3"),
    "NOTRUNNING":   ("stopped",    "text-3"),
    "ERROR":        ("error",      "bad"),
    "RESTRICTED":   ("restricted", "warn"),
    "ONLINE":       ("connecting", "accent"),
    "PUBLIC":       ("recording",  "good"),
    "NOTEXIST":     ("not found",  "bad"),
    "PRIVATE":      ("private",    "purple"),
    "OFFLINE":      ("offline",    "text-3"),
    "LONG_OFFLINE": ("long offline", "text-3"),
    "DELETED":      ("deleted",    "bad"),
    "RATELIMIT":    ("rate-limited", "warn"),
    "CLOUDFLARE":   ("cloudflare", "warn"),
}


def status_ui(status_name: str) -> Tuple[str, str]:
    return STATUS_UI.get(status_name, (status_name.lower(), "text-3"))


# ── LiveManager — glue layer for the UI ──────────────────────────────────────

@dataclass
class _RunningModel:
    """Thread-safe wrapper around a StreaMonitor Bot instance plus its thread."""
    bot: Any                # streamonitor.bot.Bot
    site: str
    username: str
    room_id: Optional[str] = None
    created_at: str = ""


class LiveManager:
    """Single global coordinator for all running Bots.

    The web UI calls into this with plain strings / dicts; we translate to
    Bot API calls. All methods are thread-safe and fail-gracefully when
    StreaMonitor isn't available.
    """

    def __init__(self, downloads_dir: Path) -> None:
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.downloads_dir / "live_models.json"
        self._lock = threading.RLock()
        self._models: Dict[str, _RunningModel] = {}   # key = "username|site"
        # On startup, reconstruct from config (do NOT auto-start — user clicks)
        self._restore()

    @staticmethod
    def key_of(username: str, site: str) -> str:
        return f"{username.strip().lower()}|{site.strip()}"

    def _restore(self) -> None:
        """Read the saved model list. Does NOT start any bots."""
        if not self.config_path.exists():
            return
        try:
            entries = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"  [live] config read: {e}")
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            username = (entry.get("username") or "").strip()
            site = (entry.get("site") or "").strip()
            if not username or not site:
                continue
            # We create the Bot instance but don't start its thread unless
            # the saved entry says running=True
            was_running = bool(entry.get("running", False))
            room_id = entry.get("room_id")
            try:
                self._create_bot(username, site, room_id=room_id,
                                  autostart=was_running, _save=False)
            except Exception as e:
                log.warning(f"  [live] restore {username} [{site}]: {e}")
        log.info(f"  [live] restored {len(self._models)} models from config")

    def _save(self) -> None:
        """Persist current model list atomically."""
        entries = []
        with self._lock:
            for _, rm in self._models.items():
                bot = rm.bot
                e: Dict[str, Any] = {
                    "username": rm.username,
                    "site": rm.site,
                    "running": bool(getattr(bot, "running", False)),
                }
                if rm.room_id:
                    e["room_id"] = rm.room_id
                entries.append(e)
        tmp = self.config_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.config_path)
        except Exception as e:
            log.warning(f"  [live] save: {e}")

    def _create_bot(self, username: str, site: str,
                    *, room_id: Optional[str] = None,
                    autostart: bool = False, _save: bool = True) -> Any:
        if not available:
            raise RuntimeError("StreaMonitor not available. "
                               "Set HARVESTR_STREAMONITOR env var or install "
                               "StreaMonitor at C:\\F\\StreaMonitor.")
        site_cls = SITES.get(site)
        if site_cls is None:
            raise ValueError(f"unsupported site {site!r}; supported: "
                             f"{sorted(SITES.keys())}")
        key = self.key_of(username, site)
        with self._lock:
            if key in self._models:
                return self._models[key].bot
            # RoomIdBot subclasses take an extra room_id arg
            try:
                if RoomIdBot and issubclass(site_cls, RoomIdBot):
                    bot = site_cls(username, room_id=room_id)
                else:
                    bot = site_cls(username)
            except TypeError:
                # Older site modules may not accept room_id kw; fall back
                bot = site_cls(username)
            rm = _RunningModel(
                bot=bot, site=site, username=username,
                room_id=room_id,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            self._models[key] = rm
            if autostart:
                try:
                    bot.restart()   # StreaMonitor's entry — sets running=True, starts thread
                except Exception as e:
                    log.warning(f"  [live] autostart {key}: {e}")
        if _save:
            self._save()
        return rm.bot

    # ── Public API ───────────────────────────────────────────────────────

    def list_sites(self) -> List[Dict[str, Any]]:
        if not available:
            return []
        out = []
        for name, cls in sorted(SITES.items()):
            out.append({
                "name": name,
                "slug": getattr(cls, "siteslug", ""),
                "needs_room_id": bool(RoomIdBot and issubclass(cls, RoomIdBot)),
                "bulk": bool(getattr(cls, "bulk_update", False)),
            })
        return out

    def add_model(self, username: str, site: str,
                  room_id: Optional[str] = None) -> Dict[str, Any]:
        username = (username or "").strip()
        site = (site or "").strip()
        if not username:
            raise ValueError("username required")
        self._create_bot(username, site, room_id=room_id, autostart=False)
        return {"ok": True, "key": self.key_of(username, site)}

    def remove_model(self, username: str, site: str) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.pop(key, None)
        if rm:
            try:
                if getattr(rm.bot, "running", False):
                    rm.bot.stop(thread_too=True)
            except Exception as e:
                log.debug(f"  [live] remove {key}: {e}")
        self._save()
        return {"ok": True, "removed": bool(rm)}

    def start_model(self, username: str, site: str) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.get(key)
            if not rm:
                raise LookupError(f"no such model {key}")
            bot = rm.bot
            # Fresh-instantiate if the previous thread has already exited —
            # Thread objects in Python can only be started once.
            if not bot.is_alive() and getattr(bot, "running", False) is False:
                site_cls = SITES.get(rm.site)
                if site_cls:
                    try:
                        if RoomIdBot and issubclass(site_cls, RoomIdBot):
                            bot = site_cls(rm.username, room_id=rm.room_id)
                        else:
                            bot = site_cls(rm.username)
                        rm.bot = bot
                    except Exception as e:
                        log.debug(f"  [live] re-instantiate {key}: {e}")
            try:
                bot.restart()    # StreaMonitor convention: sets self.running=True,
                                 # spawns or resumes thread
            except Exception as e:
                log.warning(f"  [live] start {key}: {e}")
        self._save()
        return {"ok": True}

    def stop_model(self, username: str, site: str) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.get(key)
            if not rm:
                raise LookupError(f"no such model {key}")
            try:
                rm.bot.stop(thread_too=False)
            except Exception as e:
                log.debug(f"  [live] stop {key}: {e}")
        self._save()
        return {"ok": True}

    def toggle_all(self, running: bool) -> Dict[str, Any]:
        n = 0
        for key in list(self._models.keys()):
            try:
                user, site = key.split("|", 1)
                (self.start_model if running else self.stop_model)(user, site)
                n += 1
            except Exception as e:
                log.debug(f"  [live] bulk toggle {key}: {e}")
        return {"ok": True, "count": n}

    def get_snapshot(self) -> Dict[str, Any]:
        """Build the full UI-facing state snapshot for the Live tab."""
        models: List[Dict[str, Any]] = []
        recording_count = 0
        total_sessions_bytes = 0
        status_hist: Dict[str, int] = {}

        with self._lock:
            for _, rm in sorted(self._models.items(),
                                key=lambda kv: (kv[1].site, kv[1].username.lower())):
                bot = rm.bot
                status_name = getattr(getattr(bot, "sc", None), "name", "UNKNOWN")
                status_hist[status_name] = status_hist.get(status_name, 0) + 1
                label, color = status_ui(status_name)
                is_running = bool(getattr(bot, "running", False))
                is_recording = bool(getattr(bot, "recording", False))
                if is_recording:
                    recording_count += 1
                # Total file size for this model (StreaMonitor caches in
                # video_files_total_size on the Bot)
                size_bytes = int(getattr(bot, "video_files_total_size", 0) or 0)
                total_sessions_bytes += size_bytes
                models.append({
                    "key": self.key_of(rm.username, rm.site),
                    "username": rm.username,
                    "site": rm.site,
                    "site_slug": getattr(bot, "siteslug", ""),
                    "room_id": rm.room_id or "",
                    "running": is_running,
                    "recording": is_recording,
                    "status": status_name,
                    "status_label": label,
                    "status_color": color,
                    "size_bytes": size_bytes,
                    "gender": getattr(getattr(bot, "gender", None), "value", "") or "",
                    "country": getattr(bot, "country", "") or "",
                    "last_info": self._scrub_last_info(getattr(bot, "lastInfo", {})),
                })

        return {
            "available": available,
            "import_error": import_error,
            "streamonitor_path": _STREAMONITOR_PATH or "",
            "summary": {
                "total": len(models),
                "running": sum(1 for m in models if m["running"]),
                "recording": recording_count,
                "total_bytes": total_sessions_bytes,
                "status_hist": status_hist,
            },
            "models": models,
        }

    @staticmethod
    def _scrub_last_info(info: Dict[str, Any]) -> Dict[str, Any]:
        """Strip huge / binary values from bot.lastInfo so it's JSON-safe
        and small enough to transit on every /api/live/status poll."""
        if not isinstance(info, dict):
            return {}
        safe = {}
        for k, v in info.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                if isinstance(v, str) and len(v) > 200:
                    v = v[:200] + "..."
                safe[k] = v
            elif isinstance(v, (list, tuple)):
                safe[k] = len(v)
        return safe
