#!/usr/bin/env python3
"""
Live-model history tracker for Harvestr.

Records per-model online/offline transitions across StreaMonitor polls
so the UI can surface:

  * last_online_ts      — ISO timestamp of most recent PUBLIC/PRIVATE
  * last_offline_ts     — ISO timestamp when they went idle/off
  * online_sessions     — how many distinct online periods in the last 7d
  * online_hours_7d     — total hours online in the last 7 days
  * avg_session_minutes — mean duration of an online period
  * next_predicted_ts   — best-guess "when will this model be online next"
                          based on historical hour-of-day + day-of-week
                          frequency pattern (simple histogram)

Persists to downloads/_live_history.json.
Uses an append-only event log per model, with derived metrics computed
on read (snapshot()).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()

# How many days of history to keep
KEEP_DAYS = 45

# Status values that count as "online" for session aggregation
ONLINE_STATUSES = {"PUBLIC", "PRIVATE", "ONLINE"}
OFFLINE_STATUSES = {"OFFLINE", "LONG_OFFLINE", "NOTRUNNING"}


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.isoformat()


def _parse(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class LiveHistory:
    """Tracks per-model state transitions + derived frequency metrics."""

    def __init__(self, downloads_dir: Path):
        self.path = Path(downloads_dir) / "_live_history.json"
        self._data: Dict[str, Any] = {"models": {}, "updated_at": _iso(_now())}
        self._last_status: Dict[str, str] = {}   # key -> last recorded status
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "models" not in self._data:
                self._data["models"] = {}
            # Warm last-status cache from newest transition
            for key, entry in self._data["models"].items():
                txs = entry.get("transitions") or []
                if txs:
                    self._last_status[key] = txs[-1].get("to", "")
        except Exception:
            self._data = {"models": {}, "updated_at": _iso(_now())}

    def _flush(self) -> None:
        self._data["updated_at"] = _iso(_now())
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix="._live_history.", suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _trim_old(self, entry: Dict[str, Any]) -> None:
        """Drop transitions older than KEEP_DAYS days."""
        cutoff = _now() - timedelta(days=KEEP_DAYS)
        txs = entry.get("transitions") or []
        entry["transitions"] = [t for t in txs
                                 if _parse(t.get("ts", "")) and _parse(t["ts"]) >= cutoff]

    # ── recording ────────────────────────────────────────────────────

    def record(self, key: str, status: str,
                meta: Optional[Dict[str, Any]] = None) -> None:
        """Record a poll result for `key` (username|site). Only writes
        to disk on actual state transitions — a steady stream of
        "same as last" polls is free."""
        with self._lock:
            prev = self._last_status.get(key)
            # No change: still update meta sidecar but don't append event
            if prev == status:
                if meta:
                    self._update_meta_locked(key, meta)
                return
            # State transition — append an event
            entry = self._data["models"].setdefault(key, {
                "transitions": [],
                "meta": {},
            })
            entry["transitions"].append({
                "ts": _iso(_now()),
                "from": prev or "",
                "to": status,
            })
            self._trim_old(entry)
            if meta:
                entry["meta"].update({k: v for k, v in meta.items() if v not in (None, "")})
            self._last_status[key] = status
        self._flush()

    def _update_meta_locked(self, key: str, meta: Dict[str, Any]) -> None:
        entry = self._data["models"].setdefault(key, {"transitions": [], "meta": {}})
        entry["meta"].update({k: v for k, v in meta.items() if v not in (None, "")})

    # ── queries ──────────────────────────────────────────────────────

    def snapshot(self, key: str) -> Dict[str, Any]:
        """Return derived metrics for one model. O(|transitions|) each call."""
        with self._lock:
            entry = self._data["models"].get(key)
            if not entry:
                return {}
            txs = entry.get("transitions") or []
            meta = dict(entry.get("meta") or {})
        return _compute_metrics(txs, meta)

    def snapshot_all(self) -> Dict[str, Dict[str, Any]]:
        """Return derived metrics for every tracked model."""
        with self._lock:
            raw = dict(self._data.get("models") or {})
        out: Dict[str, Dict[str, Any]] = {}
        for key, entry in raw.items():
            out[key] = _compute_metrics(entry.get("transitions") or [],
                                         dict(entry.get("meta") or {}))
        return out


# ──────────────────────────────────────────────────────────────────────

def _compute_metrics(transitions: List[Dict[str, Any]],
                      meta: Dict[str, Any]) -> Dict[str, Any]:
    """Crunch the transition log into display-friendly metrics.

    Computes:
      last_online_ts, last_offline_ts,
      online_sessions_7d, online_hours_7d, avg_session_minutes,
      next_predicted_ts (best hour-of-day/day-of-week pick)
    """
    if not transitions:
        return {"meta": meta}

    now = _now()
    cutoff_7d = now - timedelta(days=7)

    last_online_ts = ""
    last_offline_ts = ""
    # Walk transitions in order, aggregate sessions
    session_hours = 0.0
    sessions_7d = 0
    online_start: Optional[datetime] = None
    hour_of_day_counter: Counter = Counter()
    dayhour_counter: Counter = Counter()
    recent_session_durations: List[float] = []

    for t in transitions:
        ts = _parse(t.get("ts", ""))
        if not ts:
            continue
        to = t.get("to", "")
        if to in ONLINE_STATUSES:
            if online_start is None:
                online_start = ts
                hour_of_day_counter[ts.hour] += 1
                dayhour_counter[(ts.weekday(), ts.hour)] += 1
            last_online_ts = _iso(ts)
        else:
            # Closing an online session
            if online_start is not None:
                dur = (ts - online_start).total_seconds() / 60.0  # minutes
                if ts >= cutoff_7d:
                    session_hours += dur / 60.0
                    sessions_7d += 1
                recent_session_durations.append(dur)
                online_start = None
            if to in OFFLINE_STATUSES:
                last_offline_ts = _iso(ts)

    # Open session? Count time-so-far as still ongoing
    currently_online = online_start is not None
    if currently_online and online_start >= cutoff_7d:
        session_hours += (now - online_start).total_seconds() / 3600.0
        sessions_7d += 1

    avg_session_minutes = (sum(recent_session_durations) / len(recent_session_durations)
                            if recent_session_durations else 0.0)

    # Simple prediction: pick the most-common (day-of-week, hour-of-day)
    # slot from the last 30 days of online-start events, project to the
    # NEXT occurrence of that slot. If no data, fall back to most-common
    # hour-of-day across the week.
    next_predicted_ts = ""
    if currently_online:
        # They're online right now — no prediction needed
        pass
    elif dayhour_counter:
        top = dayhour_counter.most_common(1)[0][0]
        target_dow, target_hour = top
        # Compute next datetime matching this (dow, hour)
        days_ahead = (target_dow - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
        next_predicted_ts = _iso(target)
    elif hour_of_day_counter:
        top_hour = hour_of_day_counter.most_common(1)[0][0]
        target = now.replace(hour=top_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        next_predicted_ts = _iso(target)

    return {
        "last_online_ts": last_online_ts,
        "last_offline_ts": last_offline_ts,
        "online_sessions_7d": sessions_7d,
        "online_hours_7d": round(session_hours, 1),
        "avg_session_minutes": round(avg_session_minutes, 0),
        "next_predicted_ts": next_predicted_ts,
        "currently_online": currently_online,
        "peak_hour_utc": (hour_of_day_counter.most_common(1)[0][0]
                           if hour_of_day_counter else -1),
        "meta": meta,
    }
