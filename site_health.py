#!/usr/bin/env python3
"""
Site-drift detection for Harvestr.

Tracks per-site success/fail history across runs so we can surface:

  * Sites that USED to work but now all downloads fail (extractor broke,
    site redesigned, CDN moved)
  * Sites that have been silently dead for N runs (probes return nothing)
  * Sites that regress from working → contaminated (403s, redirects)

Writes a JSON ledger at downloads/_site_health.json:
  {
    "sites": {
      "camsmut": {
        "runs": [
          {"ts": "2026-04-20T17:20", "ok": 14, "fail": 0, "skip": 20, "probed": true, "hit": true},
          {"ts": "2026-04-19T14:30", "ok": 3,  "fail": 1, "skip": 0,  "probed": true, "hit": true}
        ],
        "status": "ok" | "degraded" | "broken" | "dead",
        "last_ok_run": "2026-04-20T17:20",
        "consec_failures": 0,
        "consec_no_hits": 0
      },
      ...
    }
  }

The webui's /api/site-health endpoint surfaces this for display.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# How many runs to keep in per-site history
MAX_HISTORY = 30

# Status thresholds
DEGRADED_FAILURE_RATE = 0.5   # >= 50% of recent runs failed → degraded
BROKEN_CONSEC_FAILURES = 3    # 3+ runs in a row with zero success → broken
DEAD_CONSEC_NO_HITS = 5       # 5+ runs with no probe hit + no downloads → dead


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class SiteHealth:
    """Tracks per-site success history across downloader runs."""

    def __init__(self, downloads_dir: Path):
        self.path = Path(downloads_dir) / "_site_health.json"
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"sites": {}, "updated_at": _now_iso()}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "sites" not in self._data:
                self._data["sites"] = {}
        except Exception:
            self._data = {"sites": {}, "updated_at": _now_iso()}

    def _flush(self) -> None:
        """Atomic-rename write with Windows-lock retry."""
        self._data["updated_at"] = _now_iso()
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix="._site_health.", suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
        except Exception:
            # Best-effort; drift detection should never block the downloader
            pass

    # ── recording ────────────────────────────────────────────────────

    def record_site_run(self, site: str, *, probed: bool, hit: bool,
                        ok: int = 0, fail: int = 0, skip: int = 0) -> None:
        """Record one site's outcome from the current run.

          probed: True if we actually attempted a probe for this site
          hit:    True if the probe returned any entries
          ok/fail/skip: download outcome counts (per-video)
        """
        with self._lock:
            sites = self._data.setdefault("sites", {})
            entry = sites.setdefault(site, {"runs": []})
            entry["runs"].append({
                "ts": _now_iso(),
                "ok": int(ok), "fail": int(fail), "skip": int(skip),
                "probed": bool(probed), "hit": bool(hit),
            })
            # Trim history
            if len(entry["runs"]) > MAX_HISTORY:
                entry["runs"] = entry["runs"][-MAX_HISTORY:]
            # Update computed status
            self._recompute_status_locked(site)
        self._flush()

    def _recompute_status_locked(self, site: str) -> None:
        """Classify the site's current health. Caller must hold self._lock."""
        entry = self._data["sites"].get(site)
        if not entry:
            return
        runs = entry.get("runs", [])
        if not runs:
            entry["status"] = "unknown"
            return
        # Find last successful run (>=1 OK download)
        last_ok = ""
        for r in reversed(runs):
            if r.get("ok", 0) > 0:
                last_ok = r.get("ts", "")
                break
        entry["last_ok_run"] = last_ok

        # Count consecutive failures (from the tail)
        consec_fail = 0
        for r in reversed(runs):
            if r.get("ok", 0) == 0 and (r.get("fail", 0) > 0 or r.get("hit")):
                consec_fail += 1
            else:
                break
        entry["consec_failures"] = consec_fail

        # Count consecutive "no hits" (probe ran, returned nothing)
        consec_no_hits = 0
        for r in reversed(runs):
            if r.get("probed") and not r.get("hit"):
                consec_no_hits += 1
            else:
                break
        entry["consec_no_hits"] = consec_no_hits

        # Classify
        recent = runs[-10:]
        total_hits = sum(1 for r in recent if r.get("hit"))
        total_oks = sum(1 for r in recent if r.get("ok", 0) > 0)
        if total_oks == 0 and total_hits >= BROKEN_CONSEC_FAILURES:
            entry["status"] = "broken"        # probes hit, downloads never succeed
        elif consec_fail >= BROKEN_CONSEC_FAILURES:
            entry["status"] = "broken"
        elif consec_no_hits >= DEAD_CONSEC_NO_HITS:
            entry["status"] = "dead"          # site gone dark entirely
        elif recent and total_oks / max(1, len(recent)) < (1 - DEGRADED_FAILURE_RATE):
            entry["status"] = "degraded"
        else:
            entry["status"] = "ok"

    # ── queries ──────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Return a dict suitable for the web UI."""
        with self._lock:
            return json.loads(json.dumps(self._data))  # deep copy

    def sites_with_status(self, status: str) -> List[str]:
        """Return site names currently in the given status."""
        with self._lock:
            return sorted([s for s, e in self._data.get("sites", {}).items()
                           if e.get("status") == status])

    def drift_report(self) -> Dict[str, List[str]]:
        """Summary for logging at end of a run:
          { 'broken': [...], 'degraded': [...], 'dead': [...] }
        """
        with self._lock:
            out: Dict[str, List[str]] = {"broken": [], "degraded": [], "dead": []}
            for site, entry in self._data.get("sites", {}).items():
                s = entry.get("status")
                if s in out:
                    out[s].append(site)
            for k in out:
                out[k].sort()
            return out


# ──────────────────────────────────────────────────────────────────────

def record_run_outcomes(health: SiteHealth,
                         site_outcomes: Dict[str, Dict[str, int]],
                         hit_sites: Iterable[str],
                         probed_sites: Iterable[str]) -> None:
    """Convenience: record all sites for a completed run at once.

    site_outcomes: {site_name: {"ok": N, "fail": N, "skip": N}}
    hit_sites:     set of sites that actually returned videos
    probed_sites:  set of all sites that were attempted (may be superset of hits)
    """
    hit_set = set(hit_sites or [])
    probed_set = set(probed_sites or [])
    all_sites = set(site_outcomes.keys()) | probed_set
    for site in all_sites:
        o = site_outcomes.get(site, {})
        health.record_site_run(
            site,
            probed=(site in probed_set),
            hit=(site in hit_set),
            ok=o.get("ok", 0),
            fail=o.get("fail", 0),
            skip=o.get("skip", 0),
        )
