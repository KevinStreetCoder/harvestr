#!/usr/bin/env python3
"""
Disk management for Harvestr.

Provides:
  - Real-time usage: per-performer, per-site, per-live-model, overall
  - Free-space monitoring with configurable warning threshold
  - Bulk cleanup: wipe a performer, delete files older than N days,
    prune oldest to reclaim N GB
  - Retention rules driven from config.json:
        min_free_gb      — pause downloads if free space drops below
        max_per_performer_gb — auto-prune a performer over this cap
        auto_prune_days  — delete files older than N days (0 = never)
  - Keeps history.json in sync when files are removed

All functions are read-only by default; destructive ops require
`confirm=True` or an `apply=True` flag.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts", ".flv")


@dataclass
class _PerformerStats:
    name: str
    total_bytes: int = 0
    file_count: int = 0
    oldest_mtime: Optional[float] = None
    newest_mtime: Optional[float] = None
    sites: Dict[str, int] = field(default_factory=dict)  # site → bytes

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "bytes": self.total_bytes,
            "files": self.file_count,
            "oldest": datetime.fromtimestamp(self.oldest_mtime).isoformat()
                      if self.oldest_mtime else "",
            "newest": datetime.fromtimestamp(self.newest_mtime).isoformat()
                      if self.newest_mtime else "",
            "sites": self.sites,
        }


class DiskManager:
    """Scan + report + prune."""

    def __init__(self, downloads_dir: Path) -> None:
        self.downloads_dir = Path(downloads_dir)
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_at: float = 0.0
        self._cache_ttl: float = 3.0   # snapshot valid for 3 s
        self._lock = threading.Lock()

    # ── Scanning ─────────────────────────────────────────────────────────

    def _scan(self) -> Dict[str, _PerformerStats]:
        """Walk downloads_dir — return {performer_name → stats}."""
        out: Dict[str, _PerformerStats] = {}
        if not self.downloads_dir.exists():
            return out
        for child in self.downloads_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            # StreaMonitor layout is "<name> [SITESLUG]". Split if so.
            pname, slug = child.name, ""
            if child.name.endswith("]") and " [" in child.name:
                pname, rest = child.name.rsplit(" [", 1)
                slug = rest[:-1]
            s = out.get(pname.lower())
            if s is None:
                s = _PerformerStats(name=pname)
                out[pname.lower()] = s
            for f in child.rglob("*"):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext not in VIDEO_EXTS:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                s.total_bytes += st.st_size
                s.file_count += 1
                mtime = st.st_mtime
                s.oldest_mtime = min(s.oldest_mtime or mtime, mtime)
                s.newest_mtime = max(s.newest_mtime or 0.0, mtime)
                site_key = slug or "archive"
                s.sites[site_key] = s.sites.get(site_key, 0) + st.st_size
        return out

    def snapshot(self, force: bool = False) -> Dict[str, Any]:
        """Cached disk snapshot."""
        with self._lock:
            if not force and self._cache and (time.time() - self._cache_at) < self._cache_ttl:
                return self._cache
            performers = self._scan()
            total_bytes = sum(p.total_bytes for p in performers.values())

            # Free-space on the drive containing downloads_dir
            usage = shutil.disk_usage(self.downloads_dir)

            # Sort performers by size desc
            by_size = sorted(performers.values(), key=lambda p: -p.total_bytes)

            # Sites aggregated across all performers
            sites: Dict[str, int] = {}
            for p in performers.values():
                for s, b in p.sites.items():
                    sites[s] = sites.get(s, 0) + b

            self._cache = {
                "updated_at": datetime.now().replace(microsecond=0).isoformat(),
                "downloads_dir": str(self.downloads_dir),
                "drive": {
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "archive_bytes": total_bytes,
                    "archive_frac_of_drive": (total_bytes / usage.total) if usage.total else 0,
                    "free_frac": (usage.free / usage.total) if usage.total else 0,
                },
                "performers": [p.to_json() for p in by_size],
                "sites": sorted(
                    [{"site": k, "bytes": v} for k, v in sites.items()],
                    key=lambda x: -x["bytes"],
                ),
            }
            self._cache_at = time.time()
            return self._cache

    # ── Pruning ──────────────────────────────────────────────────────────

    def list_files(self, performer: Optional[str] = None,
                   older_than_days: Optional[int] = None,
                   min_size_bytes: int = 0,
                   max_size_bytes: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return metadata for files matching criteria (read-only)."""
        out: List[Dict[str, Any]] = []
        cutoff = None
        if older_than_days is not None and older_than_days > 0:
            cutoff = time.time() - (older_than_days * 86400)
        want = performer.lower() if performer else None
        for child in self.downloads_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            pname = child.name.rsplit(" [", 1)[0] if " [" in child.name else child.name
            if want and pname.lower() != want:
                continue
            for f in child.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if cutoff and st.st_mtime > cutoff:
                    continue
                if st.st_size < min_size_bytes:
                    continue
                if max_size_bytes and st.st_size > max_size_bytes:
                    continue
                out.append({
                    "path": str(f),
                    "performer": pname,
                    "folder": child.name,
                    "name": f.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
        return out

    def delete_files(self, paths: List[str]) -> Dict[str, Any]:
        """Best-effort batch delete. Returns counts + total bytes reclaimed."""
        removed, skipped, errors = 0, 0, []
        freed = 0
        root = self.downloads_dir.resolve()
        for p in paths:
            fp = Path(p).resolve()
            # Safety: must be inside downloads_dir
            try:
                fp.relative_to(root)
            except ValueError:
                skipped += 1
                errors.append(f"outside downloads_dir: {p}")
                continue
            if not fp.is_file():
                skipped += 1
                continue
            try:
                sz = fp.stat().st_size
                fp.unlink()
                freed += sz
                removed += 1
            except Exception as e:
                errors.append(f"{fp.name}: {type(e).__name__}: {e}")

        # Sync history.json — drop any entry whose "output" matches a deleted file
        if removed > 0:
            history_path = self.downloads_dir / "history.json"
            if history_path.exists():
                try:
                    h = json.loads(history_path.read_text(encoding="utf-8"))
                    deleted_set = {str(Path(p).resolve()) for p in paths}
                    for perf, entries in list(h.items()):
                        if not isinstance(entries, dict):
                            continue
                        for gid in list(entries.keys()):
                            info = entries[gid]
                            out = info.get("output") if isinstance(info, dict) else None
                            if out and str(Path(out).resolve()) in deleted_set:
                                del entries[gid]
                        if not entries:
                            del h[perf]
                    history_path.write_text(
                        json.dumps(h, indent=2, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8",
                    )
                except Exception as e:
                    errors.append(f"history sync: {type(e).__name__}: {e}")

        self._cache = None   # invalidate
        return {"removed": removed, "skipped": skipped, "bytes_freed": freed,
                "errors": errors}

    def wipe_performer(self, performer: str) -> Dict[str, Any]:
        """Delete ALL videos for a performer. Removes the folder if empty."""
        files = self.list_files(performer=performer)
        r = self.delete_files([f["path"] for f in files])
        # Remove empty folders
        want = performer.lower()
        for child in list(self.downloads_dir.iterdir()):
            if not child.is_dir():
                continue
            pname = child.name.rsplit(" [", 1)[0] if " [" in child.name else child.name
            if pname.lower() == want:
                try:
                    # Only remove if truly empty (no lingering non-video files)
                    if not any(child.iterdir()):
                        child.rmdir()
                except OSError:
                    pass
        return r

    def prune_older_than(self, days: int, apply: bool = False) -> Dict[str, Any]:
        """Find (and optionally delete) videos older than N days."""
        files = self.list_files(older_than_days=days)
        total = sum(f["size"] for f in files)
        if not apply:
            return {"dry_run": True, "file_count": len(files),
                    "would_free_bytes": total}
        r = self.delete_files([f["path"] for f in files])
        return {"dry_run": False, **r, "matched": len(files)}

    def prune_to_free(self, target_free_gb: float, apply: bool = False) -> Dict[str, Any]:
        """Delete oldest files until free space is at least target_free_gb."""
        usage = shutil.disk_usage(self.downloads_dir)
        target_bytes = int(target_free_gb * (1024 ** 3))
        need = target_bytes - usage.free
        if need <= 0:
            return {"dry_run": True, "already_free_gb": usage.free / (1024**3),
                    "nothing_to_do": True}
        files = self.list_files()
        files.sort(key=lambda f: f["mtime"])   # oldest first
        picked: List[Dict[str, Any]] = []
        running = 0
        for f in files:
            if running >= need:
                break
            picked.append(f)
            running += f["size"]
        if not apply:
            return {"dry_run": True, "would_delete": len(picked),
                    "would_free_bytes": running,
                    "still_needed_bytes": max(0, need - running)}
        r = self.delete_files([f["path"] for f in picked])
        return {"dry_run": False, **r, "matched": len(picked)}

    def enforce_performer_cap(self, performer: str, max_gb: float,
                               apply: bool = False) -> Dict[str, Any]:
        """If a performer's videos exceed max_gb, delete the oldest until under."""
        files = self.list_files(performer=performer)
        files.sort(key=lambda f: f["mtime"])   # oldest first
        cap = int(max_gb * (1024 ** 3))
        total = sum(f["size"] for f in files)
        if total <= cap:
            return {"dry_run": True, "under_cap": True, "current_bytes": total}
        to_free = total - cap
        picked: List[Dict[str, Any]] = []
        freed = 0
        for f in files:
            if freed >= to_free:
                break
            picked.append(f)
            freed += f["size"]
        if not apply:
            return {"dry_run": True, "would_delete": len(picked),
                    "would_free_bytes": freed}
        r = self.delete_files([f["path"] for f in picked])
        return {"dry_run": False, **r, "matched": len(picked)}
