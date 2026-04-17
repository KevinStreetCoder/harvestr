#!/usr/bin/env python3
"""
Shared live-progress tracker for the Harvestr downloader.

Writes a small JSON file (`downloads/_progress.json`) that the web UI polls
on a 500 ms cadence. The downloader calls `start_video` when a download kicks
off, `update_video` as bytes flow, and `finish_video` when it's done.

Format of _progress.json:
  {
    "updated_at": "2026-04-17T13:45:00",
    "session": {
      "running": true,
      "performer": "alice_example",
      "pid": 12345,
      "started_at": "2026-04-17T13:44:58",
      "ok": 3, "fail": 1, "skip": 0,
      "total_queued": 12
    },
    "active": [
      {
        "slot": 0,
        "video_id": "xyz",
        "site": "coomer",
        "title": "Video title",
        "backend": "yt-dlp" | "aria2c" | "curl" | "ffmpeg",
        "bytes_done": 12345678,
        "bytes_total": 100000000,
        "percent": 12.3,
        "speed_bps": 2500000,
        "eta_seconds": 35,
        "started_at": "2026-04-17T13:44:59"
      }
    ]
  }

Thread-safe via a lock + atomic rename. Tolerant of Windows file-lock races.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class ProgressTracker:
    """Holds in-memory state; flushes to disk after each update.

    A single instance lives in the UniversalDownloader. The webui reads the
    JSON file directly — no shared-memory link needed.
    """

    def __init__(self, downloads_dir: Path):
        self.path = Path(downloads_dir) / "_progress.json"
        self.session: Dict[str, Any] = {
            "running": False,
            "performer": "",
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "ok": 0,
            "fail": 0,
            "skip": 0,
            "total_queued": 0,
        }
        self._active: Dict[int, Dict[str, Any]] = {}  # slot_id -> video entry
        self._slot_counter = 0
        self._lock = threading.Lock()
        # Clean any stale progress from a previous crashed run
        self._flush()

    # ── session-level ────────────────────────────────────────────────

    def session_start(self, performer: str, total_queued: int = 0) -> None:
        with self._lock:
            self.session.update({
                "running": True,
                "performer": performer,
                "started_at": _now_iso(),
                "total_queued": total_queued,
                "ok": 0, "fail": 0, "skip": 0,
            })
            self._active.clear()
        self._flush()

    def session_update(self, **kw) -> None:
        with self._lock:
            self.session.update(kw)
        self._flush()

    def session_increment(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.session[key] = int(self.session.get(key, 0)) + n
        self._flush()

    def session_end(self) -> None:
        with self._lock:
            self.session["running"] = False
            self._active.clear()
        self._flush()

    # ── per-download ─────────────────────────────────────────────────

    def start_video(self, *, site: str, video_id: str, title: str,
                    backend: str) -> int:
        """Register a new active download. Returns a slot id for updates."""
        with self._lock:
            slot = self._slot_counter
            self._slot_counter += 1
            self._active[slot] = {
                "slot": slot,
                "video_id": video_id,
                "site": site,
                "title": title[:150],
                "backend": backend,
                "bytes_done": 0,
                "bytes_total": 0,
                "percent": 0.0,
                "speed_bps": 0,
                "eta_seconds": 0,
                "started_at": _now_iso(),
            }
        self._flush()
        return slot

    def update_video(self, slot: int, *, bytes_done: Optional[int] = None,
                     bytes_total: Optional[int] = None,
                     percent: Optional[float] = None,
                     speed_bps: Optional[int] = None,
                     eta_seconds: Optional[int] = None) -> None:
        with self._lock:
            entry = self._active.get(slot)
            if not entry:
                return
            if bytes_done is not None:
                entry["bytes_done"] = int(bytes_done)
            if bytes_total is not None and bytes_total > 0:
                entry["bytes_total"] = int(bytes_total)
            if percent is not None:
                entry["percent"] = round(float(percent), 1)
            elif entry["bytes_total"] > 0 and entry["bytes_done"] > 0:
                entry["percent"] = round(100.0 * entry["bytes_done"] / entry["bytes_total"], 1)
            if speed_bps is not None:
                entry["speed_bps"] = int(speed_bps)
            if eta_seconds is not None:
                entry["eta_seconds"] = int(eta_seconds)
        self._flush()

    def finish_video(self, slot: int, *, status: str = "ok") -> None:
        """Mark the slot finished. Removes from active list."""
        with self._lock:
            self._active.pop(slot, None)
            if status in ("ok", "fail", "skip"):
                self.session[status] = int(self.session.get(status, 0)) + 1
        self._flush()

    # ── flush ────────────────────────────────────────────────────────

    def _flush(self) -> None:
        """Atomic-rename write. Swallows transient Windows lock errors."""
        snapshot = {
            "updated_at": _now_iso(),
            "session": dict(self.session),
            "active": list(self._active.values()),
        }
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.path.parent), prefix="._progress.", suffix=".tmp",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
            # Retry os.replace on Windows
            for attempt in range(5):
                try:
                    os.replace(tmp_path, self.path)
                    return
                except PermissionError:
                    time.sleep(0.05 * (2 ** attempt))
                except OSError:
                    time.sleep(0.05 * (2 ** attempt))
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        except Exception:
            # Never let a progress-write bug kill the downloader
            pass


def make_yt_dlp_hook(tracker: ProgressTracker, slot: int):
    """Return a progress_hooks callable for yt-dlp. Updates tracker on each
    status=downloading event. Finishes slot on status=finished/error."""
    def hook(d: dict) -> None:
        status = d.get("status", "")
        if status == "downloading":
            bytes_done = d.get("downloaded_bytes") or 0
            bytes_total = (d.get("total_bytes")
                           or d.get("total_bytes_estimate")
                           or 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            tracker.update_video(slot,
                                 bytes_done=bytes_done,
                                 bytes_total=bytes_total,
                                 speed_bps=speed,
                                 eta_seconds=eta)
        elif status == "finished":
            tracker.update_video(slot, percent=100.0)
    return hook
