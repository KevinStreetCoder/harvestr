#!/usr/bin/env python3
"""
Video integrity check + repair pipeline for Harvestr.

Problem: live HLS recordings (StripChat, Chaturbate, etc.) sometimes end
with truncated frames, audio/video desync, missing moov atoms, or dropped
segments when the CDN cuts out mid-stream. These files often play "mostly
fine" but freeze at the end, show no duration, or have broken audio.

Strategy — three tiers, cheapest first:

  1. probe          — ffprobe to confirm the file is parseable and has
                      at least one valid video stream + duration > 0.
                      Fast (<200 ms), no re-encoding.

  2. remux          — `ffmpeg -i in -c copy -movflags +faststart out.mp4`
                      fixes missing moov atoms, reshuffles packet order,
                      and converts ts→mp4 cheaply. No quality loss.
                      Usually fixes HLS-recording corruption.

  3. re-encode      — full re-encode with `-c:v libx264 -c:a aac`. Last
                      resort for truly broken streams where copy-remux
                      fails to decode. Slow but forgiving.

If all three fail, we optionally delete the file (depending on policy).

Entry points:
    check_playable(path) -> (ok, reason, metadata)
    repair_file(path, delete_if_unfixable=False) -> (new_path, result)
    sweep_folder(folder) -> list of results
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# Detected once at module load
_FFMPEG_BIN: Optional[str] = None
_FFPROBE_BIN: Optional[str] = None


def _find(name: str) -> Optional[str]:
    p = shutil.which(name)
    if p:
        return p
    # Windows common paths
    for c in (rf"C:\ffmpeg\bin\{name}.exe",
              rf"C:\Program Files\ffmpeg\bin\{name}.exe"):
        if os.path.exists(c):
            return c
    return None


def _ffmpeg() -> Optional[str]:
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = _find("ffmpeg") or ""
    return _FFMPEG_BIN or None


def _ffprobe() -> Optional[str]:
    global _FFPROBE_BIN
    if _FFPROBE_BIN is None:
        _FFPROBE_BIN = _find("ffprobe") or ""
    return _FFPROBE_BIN or None


# Common video extensions we'll bother checking
VIDEO_EXTS = {".mp4", ".mkv", ".m4v", ".mov", ".webm", ".ts", ".flv",
              ".avi", ".m4a"}


@dataclass
class RepairResult:
    path: str
    action: str = ""            # "ok" | "remuxed" | "reencoded" | "deleted" | "failed"
    reason: str = ""            # human-readable explanation
    duration_s: float = 0.0
    before_size: int = 0
    after_size: int = 0
    elapsed_s: float = 0.0


# ──────────────────────────────────────────────────────────────────────
# Tier 1: probe

def check_playable(path: str, log: Optional[logging.Logger] = None
                    ) -> Tuple[bool, str, dict]:
    """Return (ok, reason, metadata). Runs ffprobe to validate the file.

    ok=False means the file is broken (no video stream, zero duration,
    or ffprobe couldn't parse the container). metadata includes
    duration_s, streams, size_bytes."""
    probe = _ffprobe()
    if not probe:
        return False, "ffprobe not found", {}
    if not os.path.exists(path):
        return False, "file missing", {}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"stat: {e}", {}
    if size < 10_000:   # <10 KB = probably header-only garbage
        return False, f"too small ({size} B)", {"size_bytes": size}

    cmd = [probe, "-v", "error",
           "-show_streams", "-show_format",
           "-of", "json",
           "-analyzeduration", "5000000",
           "-probesize", "5000000",
           path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "ffprobe timeout", {"size_bytes": size}
    except Exception as e:
        return False, f"ffprobe error: {e}", {"size_bytes": size}
    if r.returncode != 0:
        err = (r.stderr.decode(errors="replace") or "")
        one_line = " | ".join(l.strip() for l in err.strip().splitlines() if l.strip())[-140:]
        return False, f"ffprobe exit {r.returncode}: {one_line}", {"size_bytes": size}

    try:
        data = json.loads(r.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        return False, f"ffprobe JSON parse: {e}", {"size_bytes": size}

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (ValueError, TypeError):
        pass

    meta = {
        "size_bytes": size,
        "duration_s": duration,
        "video_streams": len(video_streams),
        "audio_streams": len(audio_streams),
        "format": fmt.get("format_name", ""),
    }

    # Audio-only files (soundcloud m4a etc.) are OK with just audio
    is_audio_only = path.lower().endswith(".m4a") or path.lower().endswith(".mp3")

    if duration <= 0:
        return False, "zero duration (truncated / missing moov)", meta
    if not is_audio_only and not video_streams:
        return False, "no video stream", meta
    if is_audio_only and not audio_streams:
        return False, "no audio stream (audio-only file)", meta
    # Extremely short files (<0.5s) with tiny byte count are almost
    # certainly header-only fragments; anything longer we accept.
    if duration < 0.5 and size < 50_000:
        return False, f"near-empty ({duration:.1f}s, {size}B)", meta

    return True, "ok", meta


# ──────────────────────────────────────────────────────────────────────
# Tier 2 + 3: repair

def _run_ffmpeg(cmd: List[str], timeout: int = 600) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)
    # Compress multi-line stderr to one line for readable logs
    err = (r.stderr.decode(errors="replace") if r.stderr else "")
    one_line = " | ".join(l.strip() for l in err.strip().splitlines() if l.strip())[-220:]
    return r.returncode, one_line


def _remux(src: str, dst: str) -> Tuple[int, str]:
    """Copy-codec remux: fixes container-level corruption without re-encoding."""
    ff = _ffmpeg()
    if not ff:
        return -1, "ffmpeg not found"
    # -fflags +genpts regenerates missing timestamps; -avoid_negative_ts fixes desync
    cmd = [ff, "-y", "-fflags", "+genpts", "-i", src,
           "-map", "0", "-c", "copy",
           "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart",
           dst]
    return _run_ffmpeg(cmd, timeout=300)


def _reencode(src: str, dst: str) -> Tuple[int, str]:
    """Full re-encode: slow but forgives any decoder error."""
    ff = _ffmpeg()
    if not ff:
        return -1, "ffmpeg not found"
    cmd = [ff, "-y", "-err_detect", "ignore_err",
           "-i", src,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart",
           dst]
    return _run_ffmpeg(cmd, timeout=900)


def repair_file(path: str, *,
                 delete_if_unfixable: bool = False,
                 log: Optional[logging.Logger] = None) -> RepairResult:
    """Run the full 3-tier repair pipeline on one file.

    Returns RepairResult with:
      action = "ok"         → already playable, no-op
               "remuxed"    → fixed by stream-copy remux (no quality loss)
               "reencoded"  → fixed by full re-encode
               "deleted"    → all tiers failed, file removed
               "failed"     → all tiers failed, file kept
    """
    t0 = time.time()
    p = Path(path)
    result = RepairResult(path=str(p))
    if not p.exists():
        result.action = "failed"
        result.reason = "file not found"
        return result

    result.before_size = p.stat().st_size

    # Tier 1: check
    ok, reason, meta = check_playable(str(p), log)
    result.duration_s = float(meta.get("duration_s") or 0.0)
    if ok:
        result.action = "ok"
        result.reason = "playable"
        result.after_size = result.before_size
        result.elapsed_s = round(time.time() - t0, 2)
        return result

    # Determine output container — prefer mp4 for playability
    # Audio-only files stay audio-only
    is_audio_only = p.suffix.lower() in (".m4a", ".mp3", ".aac", ".opus")
    out_ext = ".m4a" if is_audio_only else ".mp4"
    fixed_path = p.with_suffix(".repaired" + out_ext)

    # Tier 2: remux
    if log:
        log.info(f"  [repair] {p.name}: remux attempt ({reason})")
    rc, err = _remux(str(p), str(fixed_path))
    if rc == 0 and fixed_path.exists():
        ok2, reason2, meta2 = check_playable(str(fixed_path), log)
        if ok2:
            # Replace original
            try:
                p.unlink()
                final = p.with_suffix(out_ext)
                fixed_path.rename(final)
                result.action = "remuxed"
                result.reason = f"fixed via remux (was: {reason})"
                result.after_size = final.stat().st_size
                result.duration_s = float(meta2.get("duration_s") or 0.0)
                result.elapsed_s = round(time.time() - t0, 2)
                if log:
                    log.info(f"  [repair] {p.name}: remuxed OK, "
                             f"duration={result.duration_s:.1f}s")
                return result
            except OSError as e:
                if log:
                    log.warning(f"  [repair] {p.name}: rename after remux: {e}")
        else:
            try: fixed_path.unlink()
            except OSError: pass
    else:
        try:
            if fixed_path.exists():
                fixed_path.unlink()
        except OSError:
            pass

    # Tier 3: re-encode
    if log:
        log.info(f"  [repair] {p.name}: re-encode attempt")
    rc, err = _reencode(str(p), str(fixed_path))
    if rc == 0 and fixed_path.exists():
        ok3, reason3, meta3 = check_playable(str(fixed_path), log)
        if ok3:
            try:
                p.unlink()
                final = p.with_suffix(out_ext)
                fixed_path.rename(final)
                result.action = "reencoded"
                result.reason = f"fixed via re-encode (was: {reason})"
                result.after_size = final.stat().st_size
                result.duration_s = float(meta3.get("duration_s") or 0.0)
                result.elapsed_s = round(time.time() - t0, 2)
                if log:
                    log.info(f"  [repair] {p.name}: re-encoded OK")
                return result
            except OSError as e:
                if log:
                    log.warning(f"  [repair] {p.name}: rename after re-encode: {e}")
        else:
            try: fixed_path.unlink()
            except OSError: pass

    # All tiers failed
    if log:
        log.warning(f"  [repair] {p.name}: all tiers failed ({reason} / {err[:80]})")
    if delete_if_unfixable:
        try:
            p.unlink()
            result.action = "deleted"
            result.reason = f"unfixable ({reason}); deleted"
        except OSError as e:
            result.action = "failed"
            result.reason = f"unfixable, delete failed: {e}"
    else:
        result.action = "failed"
        result.reason = f"unfixable ({reason}); kept on disk"
    result.elapsed_s = round(time.time() - t0, 2)
    return result


# ──────────────────────────────────────────────────────────────────────
# Folder sweep

def sweep_folder(folder: str, *,
                  recursive: bool = True,
                  delete_if_unfixable: bool = False,
                  only_recent_seconds: float = 0.0,
                  skip_if_locked: bool = True,
                  log: Optional[logging.Logger] = None,
                  progress_cb=None) -> List[RepairResult]:
    """Run repair_file() over every video in `folder`.

    progress_cb(stage, current_index, total, current_path, partial_result)
        Optional callback called at each stage:
          stage="listing"  — scanning the folder for files
          stage="start"    — about to process file `current_path`
          stage="done"     — finished processing file; partial_result is
                             the RepairResult just added
          stage="finished" — all files processed
    """
    root = Path(folder)
    results: List[RepairResult] = []
    if not root.exists():
        if progress_cb:
            progress_cb("finished", 0, 0, "", None)
        return results

    if progress_cb:
        progress_cb("listing", 0, 0, str(root), None)

    paths: List[Path] = []
    iter_ = root.rglob("*") if recursive else root.iterdir()
    now = time.time()
    for p in iter_:
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        # Skip temporary / partial files
        name = p.name.lower()
        if ".part" in name or name.endswith(".tmp.ts") or name.endswith(".tmp"):
            continue
        if only_recent_seconds > 0:
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if (now - mtime) > only_recent_seconds:
                continue
        # Skip locked files (recording in progress on Windows)
        if skip_if_locked and _is_locked(p):
            if log:
                log.debug(f"  [repair] {p.name}: file is locked (recording?), skipping")
            continue
        paths.append(p)

    total = len(paths)
    if log:
        log.info(f"  [repair] sweeping {total} files in {root}")

    for i, p in enumerate(paths, 1):
        if progress_cb:
            progress_cb("start", i, total, str(p), None)
        try:
            r = repair_file(str(p),
                             delete_if_unfixable=delete_if_unfixable,
                             log=log)
            results.append(r)
        except Exception as e:
            if log:
                log.error(f"  [repair] {p.name} raised: {e}")
            r = RepairResult(path=str(p), action="failed",
                              reason=f"exception: {e}")
            results.append(r)
        if progress_cb:
            progress_cb("done", i, total, str(p), r)

    if progress_cb:
        progress_cb("finished", total, total, "", None)
    return results


def _is_locked(p: Path) -> bool:
    """Best-effort: try to open in append mode. If that fails on Windows,
    the file is locked by another process."""
    try:
        with open(p, "ab"):
            pass
        return False
    except (PermissionError, OSError):
        return True


# ──────────────────────────────────────────────────────────────────────
# Convenience

def summarize(results: List[RepairResult]) -> dict:
    """Build a count summary for the UI / log output."""
    counts = {"ok": 0, "remuxed": 0, "reencoded": 0, "deleted": 0, "failed": 0}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    total = sum(counts.values())
    return {
        "total": total,
        "counts": counts,
        "results": [vars(r) for r in results],
    }
