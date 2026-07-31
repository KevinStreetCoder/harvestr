#!/usr/bin/env python3
"""Machine-local secret storage for per-site API credentials.

Deliberately NOT part of config.json. config.json is edited through the web UI
and read back by it, so anything stored there is one careless screenshot or
bug-report paste away from being public. Credentials live in their own
gitignored file (or environment variables) and are never returned to the UI in
full — callers get a masked form for display and the real value only at the
point of use.

Precedence (first hit wins):
  1. environment variable      HARVESTR_X_BEARER, HARVESTR_X_AUTH_TOKEN, ...
  2. secrets file              secrets.json next to this module
  3. empty

The file format is a flat namespace -> {key: value} map:

    {
      "x": {
        "bearer":     "AAAAAAAA...",
        "auth_token": "...",
        "ct0":        "..."
      }
    }
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict, Optional

SECRETS_PATH = Path(__file__).resolve().parent / "secrets.json"

# namespace -> {logical key: environment variable}
ENV_MAP: Dict[str, Dict[str, str]] = {
    "x": {
        "bearer":     "HARVESTR_X_BEARER",
        "auth_token": "HARVESTR_X_AUTH_TOKEN",
        "ct0":        "HARVESTR_X_CT0",
        "api_key":    "HARVESTR_X_API_KEY",
        "api_secret": "HARVESTR_X_API_SECRET",
    },
}

_cache: Optional[dict] = None


def _load_file() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    data: dict = {}
    try:
        if SECRETS_PATH.exists():
            data = json.loads(SECRETS_PATH.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
    except Exception:
        # A malformed secrets file must not take the downloader down; the
        # caller just behaves as if no credentials were configured.
        data = {}
    _cache = data
    return data


def reload() -> None:
    """Drop the cache so the next get() re-reads the file."""
    global _cache
    _cache = None


def get(namespace: str, key: str, default: str = "") -> str:
    """Return a secret. Environment wins over the file."""
    env_var = ENV_MAP.get(namespace, {}).get(key)
    if env_var:
        val = os.environ.get(env_var)
        if val:
            return val.strip()
    ns = _load_file().get(namespace) or {}
    val = ns.get(key)
    return str(val).strip() if val else default


def get_all(namespace: str) -> Dict[str, str]:
    """Every configured secret for a namespace (real values — use at call site
    only, never for display or logging)."""
    out: Dict[str, str] = {}
    for key in ENV_MAP.get(namespace, {}):
        v = get(namespace, key)
        if v:
            out[key] = v
    for key, v in (_load_file().get(namespace) or {}).items():
        if v and key not in out:
            out[key] = str(v).strip()
    return out


def mask(value: str) -> str:
    """Display form: enough to recognise which credential it is, not enough to
    use. Short values are hidden entirely rather than half-revealed."""
    if not value:
        return ""
    v = str(value)
    return "•" * 8 if len(v) < 12 else f"{'•' * 8}{v[-4:]}"


def status(namespace: str) -> Dict[str, object]:
    """UI-safe summary: which credentials exist and where they came from.
    Never includes a usable value."""
    out: Dict[str, object] = {}
    file_ns = _load_file().get(namespace) or {}
    for key, env_var in ENV_MAP.get(namespace, {}).items():
        from_env = bool(os.environ.get(env_var))
        val = get(namespace, key)
        out[key] = {
            "set": bool(val),
            "source": "env" if from_env else ("file" if file_ns.get(key) else None),
            "masked": mask(val),
        }
    return out


def set_many(namespace: str, values: Dict[str, str]) -> None:
    """Write secrets to the local file with owner-only permissions.

    An empty string CLEARS a key, so the UI can remove a credential. Values are
    written atomically; the file is chmod 0600 so it isn't world-readable on
    multi-user machines (a no-op on most Windows setups, harmless there).
    """
    data = dict(_load_file())
    ns = dict(data.get(namespace) or {})
    for k, v in (values or {}).items():
        v = (v or "").strip()
        if v:
            ns[k] = v
        else:
            ns.pop(k, None)
    if ns:
        data[namespace] = ns
    else:
        data.pop(namespace, None)

    tmp = SECRETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(tmp, SECRETS_PATH)
    reload()
