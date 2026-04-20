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

    # Class-level subprocess registry: {slot: (Popen, cancel_event)}
    # Used by cancel_slot() to terminate a running download cleanly.
    _slot_procs: Dict[int, Any] = {}
    _cancelled_slots: set = set()

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
            # Phase of the pipeline: probing | enumerating | downloading | done
            "phase": "idle",
            "phase_label": "",            # human-readable, e.g. "Probing 50 sites..."
            "probe_done": 0,              # sites probed so far
            "probe_total": 0,             # total sites to probe
            "current_site": "",           # site currently being worked
            "videos_found": 0,            # running total of video refs enumerated
            "sites_hit": [],              # list of {site, count} for sites that returned videos
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
                "phase": "probing", "phase_label": "Probing sites...",
                "probe_done": 0, "probe_total": 0,
                "current_site": "", "videos_found": 0,
                "sites_hit": [],
            })
            self._active.clear()
        self._flush()

    def set_phase(self, phase: str, label: str = "") -> None:
        """One of: probing | enumerating | downloading | done"""
        with self._lock:
            self.session["phase"] = phase
            self.session["phase_label"] = label or phase.capitalize() + "..."
        self._flush()

    def note_probe(self, site: str, done: int, total: int) -> None:
        with self._lock:
            self.session["current_site"] = site
            self.session["probe_done"] = done
            self.session["probe_total"] = total
        self._flush()

    def note_hit(self, site: str, count: int, url: str = "") -> None:
        """Record a successful site probe. The URL (optional) is stored so the
        web UI can make each site-pill clickable → open the performer's page
        on that site in a new tab for manual verification."""
        with self._lock:
            hits = self.session.get("sites_hit", [])
            # Replace existing entry for this site if present (dedup)
            hits = [h for h in hits if h.get("site") != site]
            entry = {"site": site, "count": int(count)}
            if url:
                entry["url"] = url
            hits.append(entry)
            self.session["sites_hit"] = hits
            self.session["videos_found"] = sum(h.get("count", 0) for h in hits)
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
                    backend: str, video_url: str = "") -> int:
        """Register a new active download. Returns a slot id for updates.
        The video_url (optional) is exposed to the webui so the user can
        click the active-row title to open the source page and verify
        the download belongs to the right performer."""
        with self._lock:
            slot = self._slot_counter
            self._slot_counter += 1
            self._active[slot] = {
                "slot": slot,
                "video_id": video_id,
                "site": site,
                "title": title[:150],
                "backend": backend,
                "video_url": video_url,
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
            self._slot_procs.pop(slot, None)
            self._cancelled_slots.discard(slot)
            if status in ("ok", "fail", "skip"):
                self.session[status] = int(self.session.get(status, 0)) + 1
        self._flush()

    def register_subprocess(self, slot: int, proc: Any) -> None:
        """Bind a running Popen to a slot so cancel_slot can terminate it."""
        with self._lock:
            self._slot_procs[slot] = proc
            if slot in self._cancelled_slots:
                # Cancel arrived before we registered — kill immediately
                self._kill_proc_locked(slot, proc)

    def cancel_slot(self, slot: int) -> bool:
        """Terminate the running download at `slot`. Returns True if a
        process was actually killed; False if slot wasn't active.

        The download thread will see the subprocess exit non-zero, log
        a cancellation, and continue to the next queued item."""
        with self._lock:
            proc = self._slot_procs.get(slot)
            self._cancelled_slots.add(slot)   # in case proc registers later
            if not proc:
                # Maybe in yt-dlp path (no registered subprocess) — mark for
                # the download thread to notice via `is_cancelled(slot)`.
                return False
            return self._kill_proc_locked(slot, proc)

    def is_cancelled(self, slot: int) -> bool:
        # Opportunistic cross-process sync: the webui (different process)
        # writes cancel requests into _progress.json. Pick them up here so
        # the downloader sees them even though it has its own tracker.
        self._ingest_external_cancels()
        with self._lock:
            return slot in self._cancelled_slots

    def _ingest_external_cancels(self) -> None:
        """Read cancelled_slots written by another process (the webui) out of
        our own progress JSON file, and merge them into our in-memory set.

        Called from is_cancelled() (hot path on every downloader tick) and
        on each flush so kills are applied promptly."""
        try:
            if not self.path.exists():
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        ext = data.get("cancelled_slots") or []
        if not ext:
            return
        with self._lock:
            newly_cancelled = []
            for s in ext:
                try:
                    s = int(s)
                except Exception:
                    continue
                if s not in self._cancelled_slots:
                    self._cancelled_slots.add(s)
                    newly_cancelled.append(s)
            # Kill any already-running subprocesses for the new cancels
            for s in newly_cancelled:
                proc = self._slot_procs.get(s)
                if proc:
                    self._kill_proc_locked(s, proc)

    def _kill_proc_locked(self, slot: int, proc: Any) -> bool:
        """Terminate a subprocess. Caller must hold self._lock."""
        try:
            if proc.poll() is None:  # still running
                if os.name == "nt":
                    # On Windows, taskkill /T /F to nuke the process tree
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True, timeout=5,
                    )
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
                return True
        except Exception:
            pass
        return False

    # ── flush ────────────────────────────────────────────────────────

    def _flush(self) -> None:
        """Atomic-rename write. Swallows transient Windows lock errors.

        Also merges any externally-written cancelled_slots (from the webui
        process) into our in-memory set before writing — this keeps the
        cross-process cancel pipe alive across our atomic-rename writes."""
        self._ingest_external_cancels()
        with self._lock:
            cancelled_list = sorted(self._cancelled_slots)
        snapshot = {
            "updated_at": _now_iso(),
            "session": dict(self.session),
            "active": list(self._active.values()),
            "cancelled_slots": cancelled_list,
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


class CancelledBySlot(Exception):
    """Raised from a yt-dlp progress hook when the user clicks Skip.
    yt-dlp will unwind and surface this as a DownloadError, which we catch
    in the download loop and convert to a skip (not fail)."""
    pass


def make_yt_dlp_hook(tracker: ProgressTracker, slot: int):
    """Return a progress_hooks callable for yt-dlp. Updates tracker on each
    status=downloading event, and aborts the download if the user cancelled
    the slot via the web UI."""
    def hook(d: dict) -> None:
        status = d.get("status", "")
        # Cancel check happens on every tick — if the webui flipped this
        # slot to cancelled, raise out of the hook to abort yt-dlp.
        if tracker.is_cancelled(slot):
            raise CancelledBySlot(f"slot {slot} cancelled by user")
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
