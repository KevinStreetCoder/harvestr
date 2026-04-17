#!/usr/bin/env python3
"""
Embedded Tor helper — auto-discovers tor.exe (Tor Browser bundle or
standalone) and runs it as a detached SOCKS5 proxy on port 9055 (custom,
to avoid conflicts with Tor Browser on 9150 / standalone tor on 9050).

Exit codes:
  0 on success — prints "socks5://127.0.0.1:9055"
  1 on failure

Harvestr / webui.py uses this via the "Enable Tor" button in Settings:
it runs `python tor_helper.py --start` and drops the returned URL into
config.download_proxy.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SOCKS_PORT = 9055      # custom, avoids clashing with default 9050/9150
CONTROL_PORT = 9056

CANDIDATE_PATHS = [
    r"C:\Users\{user}\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"C:\Users\{user}\AppData\Local\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"C:\Program Files (x86)\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"C:\Tor\tor.exe",
]
DATA_DIR = Path(__file__).resolve().parent / "_tor_data"


def find_tor() -> str | None:
    user = os.environ.get("USERNAME", "")
    for p in CANDIDATE_PATHS:
        resolved = p.format(user=user)
        if Path(resolved).is_file():
            return resolved
    # Search PATH as a fallback
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / "tor.exe"
        if cand.is_file():
            return str(cand)
    return None


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_tor(tor_exe: str) -> subprocess.Popen | None:
    """Launch tor.exe in the background. Returns the Popen handle."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    torrc = DATA_DIR / "torrc"
    torrc.write_text(
        f"SocksPort 127.0.0.1:{SOCKS_PORT}\n"
        f"ControlPort 127.0.0.1:{CONTROL_PORT}\n"
        f"DataDirectory {DATA_DIR}\n"
        f"Log notice stderr\n"
        f"AvoidDiskWrites 1\n"
        # A few exit countries known to be unfiltered for adult/hosting IPs:
        # No hard ExitNodes pin — let Tor's circuit builder choose, which is
        # the whole reason Tor routes around geographic filtering.
        ,
        encoding="utf-8",
    )

    # Hide the console window on Windows
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

    log_path = DATA_DIR / "tor.log"
    log_fh = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [tor_exe, "-f", str(torrc)],
            stdout=log_fh, stderr=log_fh,
            cwd=str(DATA_DIR),
            creationflags=flags,
            close_fds=True,
        )
        return proc
    except Exception as e:
        print(f"ERROR starting tor: {e}", file=sys.stderr)
        return None


def wait_bootstrapped(timeout: int = 120) -> bool:
    """Block until tor reports Bootstrapped 100% or timeout."""
    deadline = time.time() + timeout
    log_path = DATA_DIR / "tor.log"
    last_pct = -1
    while time.time() < deadline:
        if log_path.exists():
            try:
                txt = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                txt = ""
            import re
            last = 0
            for m in re.finditer(r"Bootstrapped\s+(\d+)%", txt):
                last = int(m.group(1))
            if last != last_pct:
                print(f"  bootstrapped {last}%", file=sys.stderr)
                last_pct = last
            if last >= 100 and is_port_open(SOCKS_PORT):
                return True
        time.sleep(1.5)
    return False


def stop_tor() -> int:
    """Kill any tor.exe we started (by data-dir match)."""
    if sys.platform == "win32":
        r = subprocess.run(["taskkill", "/F", "/IM", "tor.exe"],
                           capture_output=True, text=True)
        return 0 if "SUCCESS" in (r.stdout or "") else r.returncode
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true", help="Start Tor + print proxy URL")
    ap.add_argument("--stop", action="store_true", help="Kill any running tor.exe")
    ap.add_argument("--status", action="store_true", help="Print proxy URL if Tor is reachable")
    ap.add_argument("--test", action="store_true", help="Start Tor + test Coomer reachability")
    args = ap.parse_args()

    if args.stop:
        print("stopping tor...", file=sys.stderr)
        rc = stop_tor()
        return rc

    if args.status:
        if is_port_open(SOCKS_PORT):
            print(f"socks5://127.0.0.1:{SOCKS_PORT}")
            return 0
        print("not running", file=sys.stderr)
        return 1

    # --start or --test
    if is_port_open(SOCKS_PORT):
        print(f"  already running on {SOCKS_PORT}", file=sys.stderr)
    else:
        tor_exe = find_tor()
        if not tor_exe:
            print("ERROR: tor.exe not found. Install Tor Browser from "
                  "https://www.torproject.org/download/ "
                  "or run: winget install TorProject.TorBrowser",
                  file=sys.stderr)
            return 1
        print(f"  found: {tor_exe}", file=sys.stderr)
        proc = start_tor(tor_exe)
        if not proc:
            return 1
        print(f"  started pid={proc.pid}, bootstrapping...", file=sys.stderr)
        if not wait_bootstrapped():
            print("ERROR: Tor failed to bootstrap in 120s", file=sys.stderr)
            return 1

    proxy = f"socks5://127.0.0.1:{SOCKS_PORT}"
    print(proxy)

    if args.test:
        print("testing coomer.st via Tor...", file=sys.stderr)
        try:
            r = subprocess.run(
                ["curl", "-sS", "-o", os.devnull,
                 "--socks5-hostname", f"127.0.0.1:{SOCKS_PORT}",
                 "--connect-timeout", "30", "--max-time", "90",
                 "-w", "HTTP %{http_code} · %{time_total}s\n",
                 "-H", "Accept: text/css",
                 "https://coomer.st/api/v1/onlyfans/user/blondie_254/profile"],
                capture_output=True, text=True, timeout=100,
            )
            print("  " + (r.stdout or r.stderr).strip(), file=sys.stderr)
        except Exception as e:
            print(f"  test failed: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
