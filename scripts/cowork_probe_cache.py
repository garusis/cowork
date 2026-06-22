#!/usr/bin/env python3
"""Global, persistent cache of successful `claude` stream-json probes (#3).

Each fresh Claude-backed role pings `claude -p` with a minimal message before
opening its real session, to guard against stream-json schema drift. That ping
spends an extra live controller call per fresh role. Probe success is a property
of the *installed CLI + command shape*, not of any one session, so a successful
probe is cached GLOBALLY and a matching launch skips the live ping across every
session and role.

The cache key is deliberately conservative (doc guardrail): any change to the
resolved CLI, its `--version` string, the role-prompt file, the mode/yolo
settings, or whether an extra writable dir is granted re-probes. There is no TTL
— key invalidation alone handles CLI drift.

Storage is a single JSON file. Its root is overridable via COWORK_SESSIONS_ROOT
(matching `cowork_state.scores_path_for` / `cowork_trace.trace_path_for`) so
tests never touch the real home dir. A corrupt or unreadable cache file is
treated as a miss and never raises — caching must never break a launch.
"""

import hashlib
import json
import os
import shutil
import subprocess


_MISSING = "\x00cowork-missing\x00"


def probe_cache_path():
    """Path of the GLOBAL probe-success cache: `~/.cowork/probe_cache.json`
    (D4/Q2). The `.cowork` home is derived from COWORK_SESSIONS_ROOT when set
    (its parent dir — the sessions root is `<home>/sessions`), so tests that
    relocate the sessions root keep the cache off the real home; a direct
    COWORK_PROBE_CACHE override pins it exactly."""
    override = os.environ.get("COWORK_PROBE_CACHE")
    if override:
        return override
    sessions_root = os.environ.get("COWORK_SESSIONS_ROOT")
    if sessions_root:
        home = os.path.dirname(os.path.normpath(sessions_root)) or sessions_root
    else:
        home = os.path.expanduser(os.path.join("~", ".cowork"))
    return os.path.join(home, "probe_cache.json")


def resolve_claude_path(command=None):
    """Resolve the claude executable to a stable, absolute real path so the
    cache key changes when a *different* binary (same version string) is picked
    up after a PATH change (D4 conservatism). When `command` (a built argv list)
    is given its first element is the executable name/path; it is run through
    `shutil.which` (if not already absolute) and `os.path.realpath`. Returns None
    when it cannot be resolved — the caller then never caches."""
    exe = command[0] if command else "claude"
    if not os.path.isabs(exe):
        resolved = shutil.which(exe)
        if not resolved:
            return None
        exe = resolved
    return os.path.realpath(exe)


def claude_version(claude_path):
    """Return `claude --version` stdout (stripped), or None on any failure.

    A cheap LOCAL exec — not a billed controller turn. Guarded so that any
    failure (missing binary, non-zero exit, timeout) yields None, which forces
    the caller to always live-probe and never cache (doc guardrail)."""
    if not claude_path:
        return None
    try:
        out = subprocess.run(
            [claude_path, "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10)
    except Exception:  # noqa: BLE001 - any failure → no version → never cache
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _role_prompt_sha(role_prompt_file):
    try:
        with open(role_prompt_file, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return _MISSING


def probe_cache_key(claude_path, version, role_prompt_file, mode, yolo,
                    has_extra_writable_dir):
    """Conservative sha256 key over the inputs that define probe success:
    resolved CLI path, `--version` string, role-prompt file hash, mode, yolo,
    and whether an extra writable dir is granted (it changes the command shape).
    Returns None when the version is unknown — an unknown version must never be
    cached (it would mask a CLI upgrade)."""
    if not version:
        return None
    parts = [
        "v1",
        claude_path or _MISSING,
        version,
        _role_prompt_sha(role_prompt_file),
        str(mode),
        "yolo" if yolo else "no-yolo",
        "extradir" if has_extra_writable_dir else "no-extradir",
    ]
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load(path):
    """Return the cache dict, or {} on any missing/corrupt/unreadable file."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    keys = data.get("keys")
    if not isinstance(keys, dict):
        return {}
    return data


def cache_hit(key, path=None):
    """Whether `key` is a stored successful probe. False on a None key or any
    cache-read failure (treated as a miss)."""
    if not key:
        return False
    path = path or probe_cache_path()
    return bool(_load(path).get("keys", {}).get(key))


def cache_store(key, path=None):
    """Record `key` as a successful probe. No-op on a None key. Tolerant: any
    write failure is swallowed (caching must never break a launch)."""
    if not key:
        return
    path = path or probe_cache_path()
    data = _load(path)
    keys = dict(data.get("keys") or {})
    keys[key] = True
    data = {"version": 1, "keys": keys}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh, sort_keys=True)
    except OSError:
        return
