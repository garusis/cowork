#!/usr/bin/env python3
"""Capability manifest schema and persistence — M1 P1.

A manifest captures what a dispatch *can* do (the capability section) and what
it is *bound to* for a specific work item (the binding section). The digest
covers the binding, so any mutation is detectable without re-reading every
field. Persistence is atomic (same-directory temp + os.replace).

No dispatch integration or capability checks belong in P1; this module is
pure schema + storage.

Public API:
    compile_manifest(work_id, capability, binding, status=None) -> dict
    manifest_digest(manifest) -> str (sha256 hex)
    manifest_stale_reasons(manifest, new_binding) -> list[str]
    manifest_is_stale(manifest, new_binding) -> bool
    persist_manifest(path, manifest) -> None  (raises OSError on failure)
    load_manifest(path) -> dict | None  (tolerant: never raises)

Python 3.9+, stdlib only.
"""

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from cowork_action_policy import (
    SHELL_META, REDIRECT, SAFE_GIT_FLAGS, _safe_flag, _real, _inside,
)

# v2 (M1 production repair): binding.effort joined controller/model/mode as
# dispatch identity. A v1 manifest on disk lacks binding.effort, so
# _validate_manifest's exact-key check on binding rejects it outright —
# load_manifest returns None, which the compiler treats as "no manifest" and
# recompiles fresh rather than silently reusing stale v1 facts.
SCHEMA_VERSION = 2

# Allowed phases for the status block.
_STATUS_PHASES = frozenset({"compiling", "proven", "refused"})

# Allowed action class names.
ACTION_CLASSES = frozenset({
    "read", "write", "exec", "network", "ui", "git", "tool",
})

# Required top-level keys in a v1 manifest.
_MANIFEST_KEYS = frozenset({
    "schema_version", "work_id", "capability", "binding", "status", "digest",
})

# Required capability sub-keys.
_CAPABILITY_KEYS = frozenset({
    "inputs", "outputs", "runtime_roots", "private_paths",
    "guard_required", "socket",
    "kernel_boundary", "artifact_writes", "action_classes", "command_adapters",
})

# Required binding sub-keys.
_BINDING_KEYS = frozenset({
    "work_id", "controller", "model", "effort", "config_digest",
    "instruction_digests", "policy_snapshot",
    "worktree", "candidate_snapshot", "guard_snapshot",
})

# Required status sub-keys.
_STATUS_KEYS = frozenset({"phase", "preflight", "refusal"})

# Required refusal sub-keys when present.
_REFUSAL_KEYS = frozenset({"code", "message", "source"})


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def _canonical_binding_bytes(binding):
    """Deterministic UTF-8 bytes over the binding dict (sorted keys, no indent)."""
    return json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_digest(manifest):
    """SHA-256 hex digest of the canonical (sorted-key, compact) binding JSON.

    Used as the canonical identifier of a binding snapshot. Any mutation of
    any binding field changes the digest."""
    binding = (manifest or {}).get("binding") or {}
    return hashlib.sha256(_canonical_binding_bytes(binding)).hexdigest()


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------

def compile_manifest(work_id, capability, binding, status=None):
    """Assemble and return a validated v1 manifest dict.

    Computes the binding digest and stores it in `manifest["digest"]`.
    Raises ValueError if required keys are absent or types are wrong.
    Does not write to disk — call persist_manifest separately."""
    if not isinstance(work_id, str) or not work_id:
        raise ValueError("work_id must be a nonempty string")
    capability = _validate_capability(capability)
    binding = _validate_binding(binding)
    if work_id != binding["work_id"]:
        raise ValueError("work_id must match binding.work_id")
    if status is None:
        status = {"phase": "compiling", "preflight": [], "refusal": None}
    status = _validate_status(status)
    digest = hashlib.sha256(_canonical_binding_bytes(binding)).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "work_id": work_id,
        "capability": capability,
        "binding": binding,
        "status": status,
        "digest": digest,
    }


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def manifest_stale_reasons(manifest, new_binding):
    """Return deterministic paths for every divergent binding value.

    A missing manifest has no binding to compare, so it returns the explicit
    ``manifest_missing`` reason.  Dict keys are traversed in sorted order;
    lists and scalar values are reported at their containing binding path.
    This helper is pure and does not require either value to be schema-valid.
    """
    if not manifest:
        return ["manifest_missing"]
    old_binding = manifest.get("binding") if isinstance(manifest, dict) else None
    if not isinstance(old_binding, dict):
        return ["binding"]
    comparison_binding = new_binding if isinstance(new_binding, dict) else {}
    reasons = []
    _binding_difference_paths(old_binding, comparison_binding, "", reasons)
    return reasons


def _binding_difference_paths(old_value, new_value, path, reasons):
    """Append deterministic, leaf-level differences between binding values."""
    if isinstance(old_value, dict) and isinstance(new_value, dict):
        for key in sorted(set(old_value) | set(new_value)):
            child_path = "%s.%s" % (path, key) if path else key
            if key not in old_value or key not in new_value:
                reasons.append(child_path)
            else:
                _binding_difference_paths(old_value[key], new_value[key],
                                          child_path, reasons)
        return
    if old_value != new_value:
        reasons.append(path or "binding")


def manifest_is_stale(manifest, new_binding):
    """Return True when manifest_stale_reasons() finds a binding divergence."""
    return bool(manifest_stale_reasons(manifest, new_binding))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist_manifest(path, manifest):
    """Write manifest atomically to path (same-directory temp + os.replace).

    Creates parent directories as needed. Raises OSError on failure."""
    dirname = os.path.dirname(os.path.abspath(path))
    os.makedirs(dirname, exist_ok=True)
    tmp = path + ".tmp.%d.%d" % (os.getpid(), int(time.monotonic() * 1e9) & 0xFFFFFFFF)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_manifest(path):
    """Return the manifest dict, or None for missing/unreadable/malformed/invalid.

    Never raises. Schema-invalid manifests (wrong schema_version, missing keys,
    wrong types) also return None."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return _validate_manifest_or_none(data)


# ---------------------------------------------------------------------------
# Validation helpers (internal)
# ---------------------------------------------------------------------------

def _validate_manifest_or_none(data):
    """Return the manifest dict if it passes schema checks, else None."""
    try:
        return _validate_manifest(data)
    except (ValueError, TypeError, KeyError):
        return None


def _validate_manifest(data):
    if not isinstance(data, dict):
        raise ValueError("manifest must be a dict")
    _check_exact_keys(data, _MANIFEST_KEYS, "manifest")
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ValueError("schema_version must be %d, got %r" % (SCHEMA_VERSION, sv))
    if not isinstance(data.get("work_id"), str) or not data["work_id"]:
        raise ValueError("work_id must be a nonempty string")
    cap = _validate_capability(data.get("capability"))
    bind = _validate_binding(data.get("binding"))
    if data["work_id"] != bind["work_id"]:
        raise ValueError("work_id must match binding.work_id")
    status = _validate_status(data.get("status"))
    digest = data.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("digest must be a 64-char hex string")
    # Verify the stored digest matches the binding (JSON round-trip invariant).
    expected = hashlib.sha256(_canonical_binding_bytes(bind)).hexdigest()
    if digest != expected:
        raise ValueError("digest mismatch: stored %s != computed %s" % (digest, expected))
    return {
        "schema_version": SCHEMA_VERSION,
        "work_id": data["work_id"],
        "capability": cap,
        "binding": bind,
        "status": status,
        "digest": digest,
    }


def _validate_capability(cap):
    if not isinstance(cap, dict):
        raise ValueError("capability must be a dict")
    _check_exact_keys(cap, _CAPABILITY_KEYS, "capability")
    if not isinstance(cap.get("inputs"), list):
        raise ValueError("capability.inputs must be a list")
    if not isinstance(cap.get("outputs"), list):
        raise ValueError("capability.outputs must be a list")
    if not isinstance(cap.get("runtime_roots"), list):
        raise ValueError("capability.runtime_roots must be a list")
    if not isinstance(cap.get("private_paths"), list):
        raise ValueError("capability.private_paths must be a list")
    if not isinstance(cap.get("guard_required"), bool):
        raise ValueError("capability.guard_required must be a bool")
    socket = cap.get("socket")
    if socket is not None and not isinstance(socket, str):
        raise ValueError("capability.socket must be a string or null")
    if not isinstance(cap.get("kernel_boundary"), dict):
        raise ValueError("capability.kernel_boundary must be a dict")
    if not isinstance(cap.get("artifact_writes"), list):
        raise ValueError("capability.artifact_writes must be a list")
    ac = cap.get("action_classes")
    if not isinstance(ac, list):
        raise ValueError("capability.action_classes must be a list")
    for item in ac:
        if not isinstance(item, str):
            raise ValueError("capability.action_classes entries must be strings")
    if not isinstance(cap.get("command_adapters"), dict):
        raise ValueError("capability.command_adapters must be a dict")
    return dict(cap)


def _validate_binding(bind):
    if not isinstance(bind, dict):
        raise ValueError("binding must be a dict")
    _check_exact_keys(bind, _BINDING_KEYS, "binding")
    if not isinstance(bind.get("work_id"), str) or not bind["work_id"]:
        raise ValueError("binding.work_id must be a nonempty string")
    if not isinstance(bind.get("controller"), str) or not bind["controller"]:
        raise ValueError("binding.controller must be a nonempty string")
    model = bind.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("binding.model must be a string or null")
    effort = bind.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ValueError("binding.effort must be a string or null")
    if not isinstance(bind.get("config_digest"), str):
        raise ValueError("binding.config_digest must be a string")
    if not isinstance(bind.get("instruction_digests"), dict):
        raise ValueError("binding.instruction_digests must be a dict")
    if not isinstance(bind.get("policy_snapshot"), dict):
        raise ValueError("binding.policy_snapshot must be a dict")
    worktree = bind.get("worktree")
    if worktree is not None and not isinstance(worktree, str):
        raise ValueError("binding.worktree must be a string or null")
    cand = bind.get("candidate_snapshot")
    if cand is not None and not isinstance(cand, dict):
        raise ValueError("binding.candidate_snapshot must be a dict or null")
    guard = bind.get("guard_snapshot")
    if guard is not None and not isinstance(guard, dict):
        raise ValueError("binding.guard_snapshot must be a dict or null")
    return dict(bind)


def _validate_status(status):
    if not isinstance(status, dict):
        raise ValueError("status must be a dict")
    _check_exact_keys(status, _STATUS_KEYS, "status")
    phase = status.get("phase")
    if phase not in _STATUS_PHASES:
        raise ValueError("status.phase must be one of %s, got %r"
                         % (sorted(_STATUS_PHASES), phase))
    if not isinstance(status.get("preflight"), list):
        raise ValueError("status.preflight must be a list")
    refusal = status.get("refusal")
    if refusal is not None:
        if not isinstance(refusal, dict):
            raise ValueError("status.refusal must be a dict or null")
        _check_exact_keys(refusal, _REFUSAL_KEYS, "status.refusal")
        for key in ("code", "message", "source"):
            if not isinstance(refusal.get(key), str) or not refusal[key]:
                raise ValueError("status.refusal.%s must be a nonempty string" % key)
    return dict(status)


def _check_exact_keys(record, expected_keys, record_name):
    extra = set(record) - expected_keys
    missing = expected_keys - set(record)
    if missing:
        raise ValueError("%s missing keys: %s" % (record_name, sorted(missing)))
    if extra:
        raise ValueError("%s has extra keys: %s" % (record_name, sorted(extra)))


# ---------------------------------------------------------------------------
# Preflight status transitions (P2)
# ---------------------------------------------------------------------------

def manifest_proven(manifest, preflight_checks):
    """Return a new manifest with status.phase='proven'.

    Does not mutate the input. preflight_checks is the ordered list of
    CapabilityCheckResult dicts collected during the preflight run."""
    result = copy.deepcopy(manifest)
    result["status"] = {
        "phase": "proven",
        "preflight": list(preflight_checks),
        "refusal": None,
    }
    return result


def manifest_refused(manifest, preflight_checks, refusal_code, refusal_message):
    """Return a new manifest with status.phase='refused' and a refusal block.

    Does not mutate the input. refusal_code and refusal_message must be
    nonempty strings; source is always 'preflight'."""
    result = copy.deepcopy(manifest)
    result["status"] = {
        "phase": "refused",
        "preflight": list(preflight_checks),
        "refusal": {
            "code": refusal_code,
            "message": refusal_message,
            "source": "preflight",
        },
    }
    return result


# ---------------------------------------------------------------------------
# P3: Command-contract layer
# ---------------------------------------------------------------------------

_REFUSED_GIT_SUBCOMMANDS = frozenset({"push", "merge", "checkout"})
_ALLOWED_WORKTREE_OPERATIONS = frozenset({
    "list", "add", "remove", "lock", "unlock", "prune", "repair", "move",
})


@dataclass(frozen=True)
class ContractDecision:
    allow: bool
    capability: str
    reason: str
    repair_hint: str


def validate_rtk_present(rtk_path, stat_fn=os.stat):
    """Verify rtk exists and is executable. Pure apart from stat_fn seam."""
    try:
        st = stat_fn(rtk_path)
    except OSError:
        return ContractDecision(
            allow=False, capability="rtk_present",
            reason="rtk not found at path",
            repair_hint="install rtk and ensure it is on PATH",
        )
    if st.st_mode & 0o111 == 0:
        return ContractDecision(
            allow=False, capability="rtk_present",
            reason="rtk is not executable",
            repair_hint="chmod +x the rtk binary",
        )
    return ContractDecision(
        allow=True, capability="rtk_present",
        reason="rtk present and executable", repair_hint="",
    )


def validate_argv_form(argv):
    """Reject shell metacharacters and redirect operators. Executes nothing."""
    joined = " ".join(argv) if isinstance(argv, (list, tuple)) else str(argv)
    if SHELL_META.search(joined):
        return ContractDecision(
            allow=False, capability="argv_form",
            reason="shell metacharacter in argv",
            repair_hint="remove shell metacharacters from argv",
        )
    if REDIRECT.search(joined):
        return ContractDecision(
            allow=False, capability="argv_form",
            reason="redirect operator in argv",
            repair_hint="remove redirect operators from argv",
        )
    return ContractDecision(
        allow=True, capability="argv_form", reason="argv form is safe", repair_hint="",
    )


def validate_cwd(cwd, runtime_roots, stat_fn=os.stat):
    """Fail-closed cwd validator. Pure apart from stat_fn seam."""
    real_cwd = _real(cwd)
    real_tmp = _real("/tmp")
    if real_cwd == real_tmp or real_cwd.startswith(real_tmp + os.sep):
        return ContractDecision(
            allow=False, capability="cwd",
            reason="cwd is under /tmp and is unconditionally refused",
            repair_hint="use a non-tmp working directory",
        )
    try:
        st = stat_fn(cwd)
        if st.st_mode & 0o1002 == 0o1002:
            return ContractDecision(
                allow=False, capability="cwd",
                reason="cwd is a world-writable sticky path",
                repair_hint="use a non-sticky working directory",
            )
    except OSError:
        return ContractDecision(
            allow=False, capability="cwd",
            reason="cwd is not accessible",
            repair_hint="use an accessible working directory",
        )
    for root in runtime_roots:
        real_root = _real(root)
        if _inside(real_cwd, real_root) and real_cwd != real_root:
            return ContractDecision(
                allow=True, capability="cwd",
                reason="cwd is a strict descendant of a declared runtime root",
                repair_hint="",
            )
    return ContractDecision(
        allow=False, capability="cwd",
        reason="cwd is not a strict descendant of any declared runtime root",
        repair_hint="use a subdirectory of a declared runtime root",
    )


def validate_git_operation(subcommand, flags=()):
    """Allow only SAFE_GIT_FLAGS subcommands/flags; push/merge/checkout are unconditionally refused."""
    if subcommand in _REFUSED_GIT_SUBCOMMANDS:
        return ContractDecision(
            allow=False, capability="git_operation",
            reason="git %s is unconditionally refused" % subcommand,
            repair_hint="use a read-only git subcommand",
        )
    if subcommand not in SAFE_GIT_FLAGS:
        return ContractDecision(
            allow=False, capability="git_operation",
            reason="git subcommand not in safe allowlist: %s" % subcommand,
            repair_hint="use an allowlisted git subcommand",
        )
    allowed = SAFE_GIT_FLAGS[subcommand]
    for flag in flags:
        if not _safe_flag(flag, allowed):
            return ContractDecision(
                allow=False, capability="git_operation",
                reason="unsafe flag for git %s: %s" % (subcommand, flag),
                repair_hint="remove unsafe flags",
            )
    return ContractDecision(
        allow=True, capability="git_operation", reason="git operation allowed", repair_hint="",
    )


def validate_plan_verification_command(adapter_name, artifact_writes, manifest_capability):
    """Verify adapter membership and that artifact writes are a declared subset."""
    adapters = manifest_capability.get("command_adapters", {})
    if adapter_name not in adapters:
        return ContractDecision(
            allow=False, capability="plan_verification",
            reason="adapter %s not declared in manifest capability" % adapter_name,
            repair_hint="declare the adapter in the manifest capability",
        )
    allowed_writes = set(manifest_capability.get("artifact_writes", []))
    excess = set(artifact_writes) - allowed_writes
    if excess:
        return ContractDecision(
            allow=False, capability="plan_verification",
            reason="artifact writes not allowed: %s" % sorted(excess),
            repair_hint="restrict artifact writes to declared paths",
        )
    return ContractDecision(
        allow=True, capability="plan_verification",
        reason="plan verification command allowed", repair_hint="",
    )


def validate_worktree_operation(path, operation, manifest_binding, isdir=os.path.isdir):
    """Pure worktree validator apart from isdir seam."""
    if operation not in _ALLOWED_WORKTREE_OPERATIONS:
        return ContractDecision(
            allow=False, capability="worktree_operation",
            reason="operation not recognized: %s" % operation,
            repair_hint="use a recognized worktree operation",
        )
    if "worktree" not in manifest_binding or manifest_binding.get("worktree") is None:
        return ContractDecision(
            allow=False, capability="worktree_operation",
            reason="no worktree declared in manifest binding",
            repair_hint="declare a worktree in the manifest binding",
        )
    declared = manifest_binding["worktree"]
    if not isdir(declared):
        return ContractDecision(
            allow=False, capability="worktree_operation",
            reason="declared worktree is not an existing directory: %s" % declared,
            repair_hint="ensure the worktree directory exists",
        )
    real_declared = _real(declared)
    real_path = _real(path)
    if not _inside(real_path, real_declared):
        return ContractDecision(
            allow=False, capability="worktree_operation",
            reason="path outside declared worktree: %s" % path,
            repair_hint="use a path inside or equal to the declared worktree",
        )
    return ContractDecision(
        allow=True, capability="worktree_operation", reason="worktree operation allowed", repair_hint="",
    )
