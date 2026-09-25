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
        self._flush_lock = threading.Lock()   # one rewrite at a time
        # Memoized snapshot() results. _compute_metrics is O(|transitions|) and
        # get_snapshot() calls snapshot() per-model across 1000+ models on every
        # /api/live/status poll -- an un-cached recompute that timed out the
        # endpoint at scale. Invalidated when a transition is appended (the
        # (len, last_ts) key changes) and by a short TTL for the few
        # wall-clock-relative fields. key -> (n, last_ts, mono_ts, result).
        self._snap_cache: Dict[str, tuple] = {}
        self._last_flush = 0.0
        self._dirty = False                   # transitions not yet on disk
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
                    to = txs[-1].get("to", "")
                    self._last_status[key] = "OFFLINE" if to == "LONG_OFFLINE" else to
        except Exception:
            self._data = {"models": {}, "updated_at": _iso(_now())}

    def flush_now(self) -> bool:
        """Write everything recorded so far, waiting for an in-flight rewrite
        instead of skipping. Called from the shutdown drain: the process is
        then killed with taskkill /F, so an atexit handler would never run.
        Returns False if the file could not be written."""
        return self._flush(wait=True)

    def _flush(self, wait: bool = False) -> bool:
        # Single-flight. A full rewrite took 12-16 s under load, and with the
        # old 2 s throttle 1-3 of them were always running: ~6 MB/s written to
        # C: (180 GB in one 8.7 h run) for a file that changes by a few KB.
        if not self._flush_lock.acquire(blocking=wait):
            return False
        tmp = None
        ok = False
        try:
            # Consistent snapshot in ~20 ms. Transition dicts are never changed
            # after they are appended, so copying the lists and meta dicts is
            # enough. The old dump ran outside the lock; a record() on another
            # thread could change a dict mid-dump and abort it, leaving a
            # truncated temp file behind.
            with self._lock:
                self._dirty = False
                self._data["updated_at"] = _iso(_now())
                models = {k: {**e,
                              "transitions": list(e.get("transitions") or []),
                              "meta": dict(e.get("meta") or {})}
                          for k, e in (self._data.get("models") or {}).items()}
                top = {k: v for k, v in self._data.items() if k != "models"}
            # Encode model by model with the C encoder: ~0.3 s CPU for 25 MB and
            # the GIL is released between entries (json.dump(fp) is the
            # pure-Python encoder, ~2.7 s; one json.dumps of the whole file holds
            # the GIL ~0.5 s and freezes every capture thread meanwhile).
            enc = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix="._live_history.", suffix=".tmp",
            )
            # errors="replace": a lone surrogate in some meta string must not make
            # every future flush fail.
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace",
                           buffering=1 << 20) as f:
                f.write('{"models":{')
                n = 0
                for k, e in models.items():
                    try:
                        blob = enc.encode(k) + ":" + enc.encode(e)
                    except (TypeError, ValueError, OverflowError):
                        # One unencodable meta value (e.g. a set) used to fail
                        # every flush from then on; skip just that model on disk
                        # (it stays in memory).
                        continue
                    if n:
                        f.write(",")
                    f.write(blob)
                    n += 1
                f.write("}")
                for k, v in top.items():
                    f.write(",")
                    f.write(enc.encode(k))
                    f.write(":")
                    f.write(enc.encode(v))
                f.write("}")
            os.replace(tmp, self.path)
            tmp = None          # published; nothing left to clean up
            ok = True
        except Exception:
            self._dirty = True  # retry on the next due record()
        finally:
            # A failed rewrite (os.replace -> WinError 5 while another process
            # has the history file open, or an encode error) would otherwise
            # leave a full-size ._live_history.*.tmp in downloads\ every time.
            # This is this call's own scratch file on C:, not a recording.
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            self._flush_lock.release()
        return ok

    def _trim_old(self, entry: Dict[str, Any]) -> None:
        """Drop transitions older than KEEP_DAYS days."""
        cutoff = _now() - timedelta(days=KEEP_DAYS)
        txs = entry.get("transitions") or []
        entry["transitions"] = [t for t in txs
                                 if _parse(t.get("ts", "")) and _parse(t["ts"]) >= cutoff]

    # ── recording ────────────────────────────────────────────────────

    def record(self, key: str, status: str,
                meta: Optional[Dict[str, Any]] = None) -> None:
        """Record a poll result for `key` (username|site). Only a state
        transition is appended; the file is rewritten at most every 30 s, and
        only while transitions are pending (a no-change poll can trigger that
        pending write)."""
        # OFFLINE vs LONG_OFFLINE is poll cadence, not an online/offline change.
        # Bulk polls flipped between them every cycle: 317k of 382k stored
        # transitions, and last_offline_ts kept pointing at the latest flip.
        if status == "LONG_OFFLINE":
            status = "OFFLINE"
        with self._lock:
            prev = self._last_status.get(key)
            if prev == status:
                # No change: still update the meta sidecar, append nothing.
                if meta:
                    self._update_meta_locked(key, meta)
            else:
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
                self._dirty = True
            if not self._dirty:
                return
        # Throttle disk flushes. get_snapshot() calls record() per model on
        # every poll; during a status storm (boot, when 1000+ models transition
        # NOTRUNNING->online at once) an fsync of the whole JSON per transition
        # serialized get_snapshot into a >45s /api/live/status timeout. The
        # in-memory log stays current; only the on-disk copy lags.
        # (30 s, not 2 s: see _flush.) Any record() call -- a no-change poll
        # included -- persists pending transitions once 30 s have passed, so a
        # hard kill loses at most ~30 s plus one snapshot-build interval.
        import time as _t
        now = _t.monotonic()
        if now - self._last_flush >= 30.0:
            self._last_flush = now
            self._flush()

    def _update_meta_locked(self, key: str, meta: Dict[str, Any]) -> None:
        entry = self._data["models"].setdefault(key, {"transitions": [], "meta": {}})
        entry["meta"].update({k: v for k, v in meta.items() if v not in (None, "")})

    # ── queries ──────────────────────────────────────────────────────

    def snapshot(self, key: str) -> Dict[str, Any]:
        """Return derived metrics for one model. _compute_metrics is
        O(|transitions|); memoize it. The result only changes when record()
        appends a transition (so the (len, last_ts) cache key changes) or, for
        the wall-clock-relative fields, after a short TTL. Without this the
        per-model recompute across 1000+ models timed out /api/live/status."""
        import time as _t
        with self._lock:
            entry = self._data["models"].get(key)
            if not entry:
                return {}
            txs = entry.get("transitions") or []
            meta = dict(entry.get("meta") or {})
            n = len(txs)
            last_ts = txs[-1].get("ts", "") if txs else ""
            now = _t.monotonic()
            cached = self._snap_cache.get(key)
            if (cached and cached[0] == n and cached[1] == last_ts
                    and (now - cached[2]) < 30.0):
                return cached[3]
            result = _compute_metrics(txs, meta)
            self._snap_cache[key] = (n, last_ts, now, result)
            return result

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
