#!/usr/bin/env python3
"""Test enumeration + stream extraction on camcaps_io and camcaps_tv."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_scrapers import CamCapsIO, CamCapsTV, ProbeHit

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("test")

USER = sys.argv[1] if len(sys.argv) > 1 else "example_user"

for cls in (CamCapsIO, CamCapsTV):
    print(f"\n{'='*60}\n{cls.NAME}\n{'='*60}")
    s = cls(log)
    hit = s.probe(USER)
    if not hit:
        print(f"  NO HIT for {USER}")
        continue
    print(f"  HIT: {hit.entry_count} videos @ {hit.url}")
    videos = s.enumerate(hit, USER, limit=3)
    print(f"  Enumerated {len(videos)} videos")
    for v in videos:
        print(f"    - {v.video_id}: {v.video_url}")
    for v in videos[:2]:
        print(f"\n  Extracting stream for {v.video_id}...")
        ok = s.extract_stream(v)
        if ok:
            print(f"    title: {v.title[:70]}")
            print(f"    kind:  {v.stream_kind}")
            print(f"    url:   {v.stream_url[:120]}")
            print(f"    hdrs:  {v.stream_headers}")
        else:
            print(f"    FAIL")
