#!/usr/bin/env python3
"""
Content-based dedup for the universal downloader.

Finds and removes duplicate video files based on byte-level content signature
(first 64 KB hash + last 64 KB hash + file size). Same video downloaded from
multiple sites = same fingerprint → keep only one.

Why not just file size? KVS mirrors re-encode or wrap files slightly, so sizes
can differ by a few bytes. The middle of the file may differ (re-encoded
headers/trailers) but head+tail usually match byte-for-byte for true duplicates.

Why not full hash? Hashing a 1 GB file takes minutes per file. Head+tail is
fast (<50 ms per file) and catches >99% of real duplicates.

Usage:
  python dedupe.py                  # scan & report, no deletion
  python dedupe.py --apply          # actually delete duplicates (keeps first)
  python dedupe.py --performer NAME # only for one performer

Dedup policy:
  - Within a performer folder, group files by (size, head_hash, tail_hash).
  - Groups with 2+ files = duplicates.
  - Keep the file with the longest name (most descriptive title).
  - If tied, keep the oldest (earliest mtime) — presumably the first download.
  - Update history.json to remove references to deleted files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
HISTORY_PATH = DOWNLOADS_DIR / "history.json"

HEAD_BYTES = 64 * 1024    # 64 KB head
TAIL_BYTES = 64 * 1024    # 64 KB tail
MIN_SIZE = 1_000_000       # skip files < 1 MB (noise)


def file_fingerprint(path: Path) -> str | None:
    """(size, head_sha1, tail_sha1) compressed into a single string key."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size < MIN_SIZE:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
            if size > HEAD_BYTES + TAIL_BYTES:
                f.seek(-TAIL_BYTES, os.SEEK_END)
                tail = f.read(TAIL_BYTES)
            else:
                tail = b""
    except OSError:
        return None
    h1 = hashlib.sha1(head).hexdigest()[:16]
    h2 = hashlib.sha1(tail).hexdigest()[:16] if tail else "0"
    return f"{size}:{h1}:{h2}"


def scan_performer(perf_dir: Path) -> dict[str, list[Path]]:
    """Group video files in a performer folder by fingerprint."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in perf_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in (".mp4", ".mkv", ".webm", ".mov"):
            continue
        fp = file_fingerprint(f)
        if fp is None:
            continue
        groups[fp].append(f)
    return groups


def pick_keeper(files: list[Path]) -> Path:
    """Choose which file to keep from a duplicate group."""
    # Prefer longest filename (most descriptive title)
    files.sort(key=lambda p: (-len(p.name), p.stat().st_mtime))
    return files[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete duplicates (default: dry-run report only)")
    ap.add_argument("--performer", help="Only process one performer folder")
    ap.add_argument("--output-dir", default=str(DOWNLOADS_DIR),
                    help="Downloads folder to scan")
    args = ap.parse_args()

    downloads = Path(args.output_dir)
    if not downloads.exists():
        print(f"Downloads folder not found: {downloads}")
        return 1

    # Load history so we can update it
    history_path = downloads / "history.json"
    history: dict = {}
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    total_files = 0
    total_dupes = 0
    total_bytes_saved = 0
    perf_dirs = sorted(p for p in downloads.iterdir() if p.is_dir() and not p.name.startswith("_"))
    if args.performer:
        perf_dirs = [p for p in perf_dirs if p.name.lower() == args.performer.lower()]

    for perf_dir in perf_dirs:
        print(f"\n{'='*70}")
        print(f"Performer: {perf_dir.name}")
        print("=" * 70)
        groups = scan_performer(perf_dir)
        perf_files = sum(len(v) for v in groups.values())
        dupes_in_perf = 0
        for fp, files in groups.items():
            if len(files) < 2:
                continue
            keeper = pick_keeper(files)
            removers = [f for f in files if f != keeper]
            size_mb = files[0].stat().st_size / (1024 * 1024)
            print(f"\n  Duplicate group ({size_mb:.1f} MB each, {len(files)} copies):")
            print(f"    KEEP: {keeper.name}")
            for r in removers:
                print(f"    DROP: {r.name}")
            if args.apply:
                for r in removers:
                    try:
                        total_bytes_saved += r.stat().st_size
                        r.unlink()
                        # Remove from history
                        perf_lower = perf_dir.name.lower()
                        entries = history.get(perf_lower, {})
                        to_remove = []
                        for gid, info in entries.items():
                            if info.get("output") == str(r):
                                to_remove.append(gid)
                        for gid in to_remove:
                            del entries[gid]
                        dupes_in_perf += 1
                    except Exception as e:
                        print(f"      ERROR removing {r}: {e}")
            else:
                for r in removers:
                    total_bytes_saved += r.stat().st_size
                    dupes_in_perf += 1
        total_files += perf_files
        total_dupes += dupes_in_perf
        print(f"\n  Summary: {perf_files} files, {dupes_in_perf} duplicates "
              f"{'removed' if args.apply else '(would be removed)'}")

    if args.apply and total_dupes:
        # Save updated history
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False, sort_keys=True)

    print(f"\n{'='*70}")
    print(f"GRAND TOTAL: {total_files} files scanned, {total_dupes} duplicates "
          f"{'removed' if args.apply else 'found'}")
    print(f"Disk space {'freed' if args.apply else 'savable'}: "
          f"{total_bytes_saved / 1024 / 1024 / 1024:.2f} GB")
    if not args.apply and total_dupes:
        print("\nRe-run with --apply to actually delete duplicate files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
