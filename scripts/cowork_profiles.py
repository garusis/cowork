#!/usr/bin/env python3
"""Private controller state that reuses existing CLI authentication safely.

Claude Code ties its macOS Keychain login to the active configuration profile.
Cowork therefore keeps the authenticated profile read-only and redirects only
the preselected session transcript through a symlink into role-private state.

Codex file-based authentication lives in auth.json and contains live tokens.
Cowork never copies it. A private CODEX_HOME references the existing file, and
the outer kernel boundary makes the reference and target read-only.
"""

import atexit
import json
import os
import re
import stat
import threading
import uuid


_SAFE_CLAUDE_AUTH_METHODS = frozenset((
    "claude.ai", "api_key", "bedrock", "vertex", "foundry"))
_CLAUDE_REFERENCE_LOCK = threading.RLock()
_CLAUDE_REFERENCES = {}


class ProfileBootstrapError(RuntimeError):
    """Private state cannot safely reuse the existing CLI login."""


def default_claude_config_dir(environ=None, home=None):
    environ = os.environ if environ is None else environ
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    home = os.path.expanduser("~") if home is None else home
    return os.path.join(os.path.abspath(home), ".claude")


def claude_project_key(cwd):
    """Claude Code's on-disk project key for an absolute working directory."""
    return re.sub(r"[^A-Za-z0-9-]", "-", os.path.abspath(cwd))


def reference_claude_session(target_dir, session_id, cwd, environ=None,
                             home=None, resume=False):
    """Route one authenticated-profile session into private role state.

    Claude writes both a project transcript and a ``session-env`` directory
    under its authenticated configuration root. Reference both exact
    session-id paths into role-private state so the kernel boundary never needs
    to make the authenticated profile writable.
    """
    try:
        canonical_id = str(uuid.UUID(str(session_id)))
    except (ValueError, TypeError, AttributeError):
        raise ProfileBootstrapError("claude_session_id_required")
    config_dir = default_claude_config_dir(environ=environ, home=home)
    project_key = claude_project_key(cwd)
    source_dir = os.path.join(config_dir, "projects", project_key)
    private_dir = os.path.join(
        os.path.abspath(os.path.expanduser(target_dir)),
        "projects", project_key)
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(private_dir, exist_ok=True)
    private_file = os.path.join(private_dir, canonical_id + ".jsonl")
    source_file = os.path.join(source_dir, canonical_id + ".jsonl")
    source_env = os.path.join(config_dir, "session-env", canonical_id)
    private_env = os.path.join(
        os.path.abspath(os.path.expanduser(target_dir)),
        "session-env", canonical_id)

    private_exists = os.path.isfile(private_file)
    if resume and not private_exists:
        raise ProfileBootstrapError("claude_private_session_missing")
    if not resume and os.path.lexists(private_file):
        raise ProfileBootstrapError("claude_private_session_collision")
    references = (
        (source_file, private_file, "claude_session_reference"),
        (source_env, private_env, "claude_session_env_reference"),
    )
    for source, target, error_prefix in references:
        if not os.path.lexists(source):
            continue
        if not os.path.islink(source):
            raise ProfileBootstrapError(error_prefix + "_collision")
        if os.path.realpath(source) != os.path.realpath(target):
            raise ProfileBootstrapError(error_prefix + "_mismatch")

    os.makedirs(os.path.dirname(source_env), exist_ok=True)
    os.makedirs(private_env, exist_ok=True)
    created = []
    try:
        for source, target, _error_prefix in references:
            if not os.path.lexists(source):
                os.symlink(target, source)
                created.append(source)
    except OSError:
        for source in reversed(created):
            try:
                os.unlink(source)
            except OSError:
                pass
        raise
    with _CLAUDE_REFERENCE_LOCK:
        for source, target, _error_prefix in references:
            _CLAUDE_REFERENCES[source] = target
    return {
        "source_file": source_file,
        "target_file": private_file,
        "session_env_source": source_env,
        "session_env_target": private_env,
        "changed": bool(created),
        "credential_copied": False,
        "cleanup_kind": "claude_session_reference",
        "protected_paths": (),
    }


def cleanup_claude_session_reference(profile):
    """Remove exact session symlinks; keep private transcript and env state."""
    if not isinstance(profile, dict):
        return False
    if profile.get("cleanup_kind") != "claude_session_reference":
        return False
    references = (
        (profile.get("source_file"), profile.get("target_file")),
        (profile.get("session_env_source"), profile.get("session_env_target")),
    )
    removed = False
    for source, target in references:
        if not source or not target:
            continue
        try:
            if (not os.path.islink(source)
                    or os.path.realpath(source) != os.path.realpath(target)):
                continue
            os.unlink(source)
            removed = True
        except OSError:
            continue
        finally:
            with _CLAUDE_REFERENCE_LOCK:
                if _CLAUDE_REFERENCES.get(source) == target:
                    _CLAUDE_REFERENCES.pop(source, None)
    return removed


def _cleanup_registered_claude_references():
    """Best-effort cleanup when an interrupt unwinds outside a role loop."""
    with _CLAUDE_REFERENCE_LOCK:
        references = tuple(_CLAUDE_REFERENCES.items())
    for source, target in references:
        try:
            if (os.path.islink(source)
                    and os.path.realpath(source) == os.path.realpath(target)):
                os.unlink(source)
        except OSError:
            pass
        finally:
            with _CLAUDE_REFERENCE_LOCK:
                if _CLAUDE_REFERENCES.get(source) == target:
                    _CLAUDE_REFERENCES.pop(source, None)


atexit.register(_cleanup_registered_claude_references)


def default_codex_auth_file(environ=None, home=None):
    environ = os.environ if environ is None else environ
    configured = environ.get("CODEX_HOME")
    if configured:
        base = os.path.abspath(os.path.expanduser(configured))
    else:
        home = os.path.expanduser("~") if home is None else home
        base = os.path.join(os.path.abspath(home), ".codex")
    return os.path.join(base, "auth.json")


def reference_codex_auth(target_dir, source_file=None, environ=None, home=None):
    """Reference, but never copy, an existing file-based Codex login."""
    source_file = source_file or default_codex_auth_file(
        environ=environ, home=home)
    source_file = os.path.realpath(source_file)
    try:
        source_stat = os.stat(source_file)
    except OSError as exc:
        raise ProfileBootstrapError(
            "codex_login_reference_unavailable:%s" % type(exc).__name__)
    if not stat.S_ISREG(source_stat.st_mode):
        raise ProfileBootstrapError("codex_login_reference_not_file")
    if stat.S_IMODE(source_stat.st_mode) & 0o077:
        raise ProfileBootstrapError("codex_login_reference_permissions")

    target_dir = os.path.abspath(os.path.expanduser(target_dir))
    os.makedirs(target_dir, exist_ok=True)
    target_file = os.path.join(target_dir, "auth.json")
    if os.path.lexists(target_file):
        if not os.path.islink(target_file):
            raise ProfileBootstrapError("codex_private_auth_must_be_reference")
        if os.path.realpath(target_file) != source_file:
            raise ProfileBootstrapError("codex_private_auth_reference_mismatch")
        changed = False
    else:
        os.symlink(source_file, target_file)
        changed = True
    return {
        "target_file": target_file,
        "source_file": source_file,
        "changed": changed,
        "credential_copied": False,
        "protected_paths": (target_file, source_file),
    }


def auth_command(controller):
    if controller == "claude":
        return ["claude", "auth", "status", "--json"]
    if controller == "codex":
        return ["codex", "login", "status"]
    raise ProfileBootstrapError("unsupported_controller_profile")


def parse_auth_status(controller, returncode, stdout):
    """Return content-free authentication status from a native CLI result."""
    if controller == "claude":
        try:
            value = json.loads(stdout or "")
        except ValueError:
            value = {}
        authenticated = returncode == 0 and value.get("loggedIn") is True
        raw_method = value.get("authMethod")
        method = (raw_method if authenticated
                  and raw_method in _SAFE_CLAUDE_AUTH_METHODS
                  else ("other" if authenticated else None))
    elif controller == "codex":
        normalized = (stdout or "").strip().lower()
        authenticated = returncode == 0 and normalized.startswith("logged in")
        method = "chatgpt" if "chatgpt" in normalized else (
            "api" if "api" in normalized else None)
    else:
        raise ProfileBootstrapError("unsupported_controller_profile")
    return {"authenticated": authenticated, "method": method}
