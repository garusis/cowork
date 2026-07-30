#!/usr/bin/env python3
"""Pure policy for controller-native delegation and filesystem actions.

This module deliberately performs no I/O.  Callers resolve installed schemas,
git ownership and durable ledgers before invoking it; the broker is the only
component allowed to turn a decision into a durable record.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import shlex


KNOWN_IDENTITY_SOURCES = frozenset(("live_event", "config_pinned"))
CHILD_TOOLS = frozenset(("Agent", "Task"))
READ_TOOLS = frozenset((
    "Read", "Glob", "Grep", "ToolSearch", "WebFetch", "WebSearch"))
MUTATION_TOOLS = frozenset(("Write", "Edit", "MultiEdit", "NotebookEdit"))
INERT_COMMANDS = frozenset(("git", "ls", "rg", "grep", "find", "pwd",
                            "wc", "head", "tail"))
MUTATING_COMMANDS = frozenset(("rm", "mv", "cp", "install", "tee", "dd",
                               "touch", "mkdir", "rmdir", "chmod", "chown",
                               "truncate", "ln"))
# Shell parsing is deny-first.  These are syntax characters whose runtime
# meaning can change the argv or execute another command; none may reach the
# small proof-producing command adapters below.  Quotes and backslash remain
# available so shlex can prove ordinary single-command argv.
SHELL_META = re.compile(r"[\x00-\x1f\x7f$`*?\[\]{}()|;&!]")
REDIRECT = re.compile(r"(?:^|[\s;|&])(?:>>?|[0-9]+>>?)\s*(\S+)")
SAFE_GIT_FLAGS = {
    "status": frozenset((
        "-s", "-b", "-u", "--short", "--porcelain", "--branch", "--show-stash",
        "--untracked-files", "--ignored", "--no-renames")),
    "diff": frozenset((
        "--stat", "--shortstat", "--numstat", "--name-only",
        "--name-status", "--cached", "--staged", "--check", "--quiet",
        "--exit-code", "--no-ext-diff", "--no-textconv", "--color",
        "--no-color", "--binary", "--full-index", "--compact-summary")),
    "log": frozenset((
        "-n", "--oneline", "--decorate", "--no-decorate", "--stat",
        "--shortstat", "--name-only", "--name-status", "--graph",
        "--all", "--branches", "--tags", "--remotes")),
    "show": frozenset((
        "-s", "--stat", "--shortstat", "--name-only", "--name-status",
        "--format", "--pretty", "--no-patch", "--color", "--no-color")),
    "ls-files": frozenset((
        "-c", "-d", "-m", "-o", "-i", "-s", "-u", "-k",
        "--cached", "--deleted", "--modified", "--others",
        "--ignored", "--stage", "--unmerged", "--killed",
        "--exclude-standard", "--error-unmatch")),
    "rev-parse": frozenset((
        "-q", "--verify", "--short", "--abbrev-ref", "--show-toplevel",
        "--show-prefix", "--show-cdup", "--git-dir", "--is-inside-work-tree")),
}
SAFE_FIND_FLAGS = frozenset((
    "-name", "-iname", "-path", "-ipath", "-type", "-maxdepth",
    "-mindepth", "-size", "-mtime", "-mmin", "-newer", "-user", "-group",
    "-perm", "-empty", "-readable", "-print", "-print0", "-prune",
    "-quit", "-true", "-false", "-not", "-a", "-and", "-o", "-or"))
SAFE_INERT_FLAGS = {
    "ls": frozenset((
        "-a", "-A", "-l", "-h", "-R", "-d", "-1", "-F", "-p", "-t",
        "-r", "-S", "-U", "--all", "--almost-all", "--long",
        "--human-readable", "--recursive", "--directory", "--color",
        "--classify", "--file-type", "--sort", "--reverse")),
    "rg": frozenset((
        "-n", "-N", "-l", "-L", "-c", "-i", "-s", "-S", "-F", "-w",
        "-x", "-g", "-t", "-T", "--files", "--hidden", "--glob",
        "--type", "--type-not", "--fixed-strings", "--ignore-case",
        "--case-sensitive", "--smart-case", "--word-regexp",
        "--line-regexp", "--count", "--count-matches",
        "--files-with-matches", "--files-without-match", "--json",
        "--stats", "--heading", "--no-heading", "--line-number",
        "--no-line-number")),
    "grep": frozenset((
        "-E", "-F", "-G", "-P", "-e", "-f", "-i", "-v", "-w", "-x",
        "-n", "-H", "-h", "-l", "-L", "-c", "-o", "-q", "-R", "-r",
        "--extended-regexp", "--fixed-strings", "--basic-regexp",
        "--perl-regexp", "--regexp", "--file", "--ignore-case",
        "--invert-match", "--word-regexp", "--line-regexp",
        "--line-number", "--with-filename", "--no-filename",
        "--files-with-matches", "--files-without-match", "--count",
        "--only-matching", "--quiet", "--recursive")),
    "pwd": frozenset(("-L", "-P", "--logical", "--physical")),
    "wc": frozenset(("-c", "-m", "-l", "-w", "-L", "--bytes", "--chars",
                     "--lines", "--words", "--max-line-length")),
    "head": frozenset(("-n", "-c", "-q", "-v", "--lines", "--bytes",
                       "--quiet", "--verbose")),
    "tail": frozenset(("-n", "-c", "-q", "-v", "-f", "-F", "--lines",
                       "--bytes", "--quiet", "--verbose", "--follow",
                       "--retry", "--pid", "--sleep-interval")),
}

CONTROLLER_CAPABILITY_MATRIX = {
    ("claude", "plan"): {
        "delegation": "enforceably_disabled",
        "child_correlation": "unavailable",
        "mutation_gate": "pre_execution_record",
        "kernel_boundary": "required",
    },
    ("claude", "implement"): {
        "delegation": "enforceably_disabled",
        "child_correlation": "unavailable",
        "mutation_gate": "pre_execution_record",
        "kernel_boundary": "required",
    },
    ("codex", "plan"): {
        "delegation": "enforceably_disabled",
        "child_correlation": "unavailable",
        "mutation_gate": "pre_execution_record",
        "kernel_boundary": "required",
    },
    ("codex", "implement"): {
        "delegation": "enforceably_disabled",
        "child_correlation": "unavailable",
        "mutation_gate": "pre_execution_record",
        "kernel_boundary": "required",
    },
    ("opencode", "plan"): {
        "delegation": "proven_absent",
        "child_correlation": "unavailable",
        "mutation_gate": "controller_permissions",
        "kernel_boundary": "not_required",
    },
    ("opencode", "implement"): {
        "delegation": "proven_absent",
        "child_correlation": "unavailable",
        "mutation_gate": "controller_permissions",
        "kernel_boundary": "not_required",
    },
}


def _real(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _inside(path, root):
    try:
        return os.path.commonpath((_real(path), _real(root))) == _real(root)
    except (ValueError, TypeError):
        return False


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def child_request_metadata(requested):
    """Content-free durable metadata for a delegated child request."""
    requested = requested if isinstance(requested, dict) else {}
    raw = json.dumps(
        requested, sort_keys=True, separators=(",", ":")).encode()
    identity = {}
    for key in ("controller", "model", "effort"):
        if requested.get(key) is not None:
            identity[key] = requested[key]
        source = requested.get(key + "_source")
        if source is not None:
            identity[key + "_source"] = source
    return {
        "requested_identity": identity,
        "requested_input_digest": hashlib.sha256(raw).hexdigest(),
        "requested_input_bytes": len(raw),
    }


@dataclass(frozen=True)
class OwnedScope:
    repo_roots: tuple = field(default_factory=tuple)
    declared_outputs: tuple = field(default_factory=tuple)
    role_temp_dir: str = None
    controller_state_dir: str = None
    session_assets_dir: str = None
    sibling_worktrees: tuple = field(default_factory=tuple)
    protected_paths: tuple = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "repo_roots",
                           tuple(_real(p) for p in self.repo_roots if p))
        object.__setattr__(self, "declared_outputs",
                           tuple(_real(p) for p in self.declared_outputs if p))
        object.__setattr__(self, "sibling_worktrees",
                           tuple(_real(p) for p in self.sibling_worktrees if p))
        object.__setattr__(self, "protected_paths",
                           tuple(_real(p) for p in self.protected_paths if p))
        for name in ("role_temp_dir", "controller_state_dir",
                     "session_assets_dir"):
            value = getattr(self, name)
            object.__setattr__(self, name, _real(value) if value else None)

    @property
    def writable_roots(self):
        roots = list(self.repo_roots) + list(self.declared_outputs)
        roots += [self.role_temp_dir, self.controller_state_dir]
        return tuple(dict.fromkeys(p for p in roots if p))

    def owns(self, path):
        path = _real(path)
        return any(_inside(path, root) for root in self.writable_roots)

    def is_declared_output(self, path):
        path = _real(path)
        return any(path == root or _inside(path, root)
                   for root in self.declared_outputs)

    def is_role_temp(self, path):
        return bool(self.role_temp_dir and _inside(path, self.role_temp_dir))

    def is_controller_state(self, path):
        return bool(self.controller_state_dir
                    and _inside(path, self.controller_state_dir))

    def is_protected(self, path):
        path = _real(path)
        return any(path == protected or _inside(path, protected)
                   for protected in self.protected_paths)


def load_capability_allowlist(entries):
    """Validate schema-pinned read-only capabilities.

    Configuration is intentionally incapable of granting mutation authority.
    """
    result = {}
    for entry in entries or ():
        if not isinstance(entry, dict):
            raise ValueError("capability entry must be an object")
        tool = entry.get("tool") or entry.get("identity")
        digest = entry.get("schema_digest")
        if not tool or not digest:
            raise ValueError("capability pin required")
        if entry.get("classification") != "read_only":
            raise ValueError("only read_only capabilities may be allowlisted")
        if entry.get("mutation_capable") or tool in MUTATION_TOOLS:
            raise ValueError("mutation-capable tools require an adapter")
        if not entry.get("evidence"):
            raise ValueError("capability evidence required")
        result[tool] = dict(entry)
    return result


def capability_decision(controller, mode, delegation="unknown",
                        mutation_gate="none", kernel_boundary=False):
    """Evaluate the fail-closed controller capability matrix."""
    row = CONTROLLER_CAPABILITY_MATRIX.get((controller, mode))
    if not row or row.get("refused"):
        return {"allow": False, "reason": "controller_capability_missing",
                "missing_capability": ((row or {}).get("refused")
                                       or "unknown_capability_row")}
    if (delegation == "governed"
            and row.get("child_correlation") != "documented"):
        return {"allow": False, "reason": "controller_capability_missing",
                "missing_capability": "child_agent_correlation"}
    delegation_ok = delegation in ("governed", "enforceably_disabled",
                                   "proven_absent")
    mutation_ok = mutation_gate == row.get("mutation_gate")
    if not delegation_ok:
        return {"allow": False, "reason": "delegation_capability_unknown"}
    if not mutation_ok:
        return {"allow": False, "reason": "mutation_gate_missing"}
    if row.get("kernel_boundary") == "required" and not kernel_boundary:
        return {"allow": False, "reason": "kernel_boundary_missing"}
    return {"allow": True, "reason": "capabilities_governed",
            "controller": controller, "mode": mode}


def decide_child(requested_child, parent_effective, allowed_controllers,
                 pin_capability=True):
    """Decide and pin a child before dispatch.

    Missing child values mean inheritance only when the gateway can replace the
    complete tool input. Explicit values must exactly match the concrete parent.
    """
    requested_child = dict(requested_child or {})
    parent = dict(parent_effective or {})
    required = ("controller", "model", "effort")
    for key in required:
        source = parent.get(key + "_source")
        if not parent.get(key) or source not in KNOWN_IDENTITY_SOURCES:
            return {"allow": False, "reason": "parent_identity_unresolved",
                    "requested": requested_child, "effective": parent}
    controller = requested_child.get("controller") or parent["controller"]
    if controller not in set(allowed_controllers or ()):
        return {"allow": False, "reason": "child_controller_not_permitted",
                "requested": requested_child, "effective": parent}
    for key in required:
        requested = requested_child.get(key)
        if requested is not None and requested != parent[key]:
            reason = ("child_identity_override_attempted"
                      if key in ("model", "effort")
                      else "child_identity_mismatch")
            return {"allow": False, "reason": reason,
                    "requested": requested_child, "effective": parent}
    if not pin_capability:
        return {"allow": False, "reason": "child_pin_not_enforceable",
                "requested": requested_child, "effective": parent}
    pinned = dict(requested_child)
    # Claude's subagent tool exposes `model`; controller is fixed by the
    # transport and effort is fixed by the session-level --effort pin. Do not
    # invent unsupported tool-input fields.
    pinned["model"] = parent["model"]
    effective = {key: parent[key] for key in required}
    effective.update({key + "_source": parent[key + "_source"]
                      for key in required})
    return {"allow": True, "reason": "child_identity_pinned",
            "requested": requested_child, "effective": effective,
            "updated_input": pinned, "pinned_input_digest": _digest(pinned)}


def _resolve(value, cwd):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    return _real(value if os.path.isabs(value) else os.path.join(cwd, value))


def _safe_flag(argument, allowed, numeric=False, numeric_values=()):
    """Match one option without letting an unknown bundled flag slip through."""
    if argument == "--":
        return True
    if argument.startswith("--"):
        return argument.split("=", 1)[0] in allowed
    if not argument.startswith("-") or argument == "-":
        return True
    if numeric and re.fullmatch(r"-[0-9]+", argument):
        return True
    for flag in numeric_values:
        if argument.startswith(flag) and re.fullmatch(
                r"[0-9]+", argument[len(flag):]):
            return True
    if argument in allowed:
        return True
    # A short-option bundle is safe only when every constituent short flag is
    # independently allowlisted for this exact verb/subcommand.
    return len(argument) > 2 and all(
        ("-" + letter) in allowed for letter in argument[1:])


def _bash_action(command, cwd):
    if not isinstance(command, str) or not command.strip():
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "target_unresolved"}
    if SHELL_META.search(command):
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable"}
    try:
        parts = shlex.split(command)
    except ValueError:
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable"}
    if not parts or any(p.startswith("./") or p.endswith((".sh", ".py"))
                        for p in parts[:1]):
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable"}
    verb = os.path.basename(parts[0])
    if verb in ("python", "python3", "perl", "ruby", "node") and any(
            p in ("-c", "-e") for p in parts[1:]):
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable"}
    if verb == "find" and any(p in ("-exec", "-execdir", "-delete")
                              for p in parts):
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable"}
    redirects = [_resolve(m.group(1), cwd) for m in REDIRECT.finditer(command)]
    if any(p is None for p in redirects):
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "target_unresolved"}
    if redirects:
        return {"class": "write", "targets": redirects,
                "resolution_complete": True, "proof": "shell_redirect"}
    if verb in MUTATING_COMMANDS:
        if verb in ("mv", "dd"):
            return {"class": "unknown", "targets": [],
                    "resolution_complete": False, "reason": "shell_unprovable"}
        candidates = [p for p in parts[1:] if not p.startswith("-")]
        # Source operands are harmless for cp/mv/install; destination is last.
        if verb in ("cp", "mv", "install", "ln") and candidates:
            candidates = candidates[-1:]
        targets = [_resolve(p, cwd) for p in candidates]
        if not targets or any(p is None for p in targets):
            return {"class": "unknown", "targets": [],
                    "resolution_complete": False,
                    "reason": "target_unresolved"}
        return {"class": "delete" if verb in ("rm", "rmdir") else "write",
                "targets": targets, "resolution_complete": True,
                "proof": "shell_argv"}
    if verb in INERT_COMMANDS:
        if verb == "git":
            subcommand = parts[1] if len(parts) > 1 else None
            safe_flags = SAFE_GIT_FLAGS.get(subcommand)
            if safe_flags is None:
                return {"class": "unknown", "targets": [],
                        "resolution_complete": False,
                        "reason": "shell_unprovable"}
            for argument in parts[2:]:
                if not _safe_flag(
                        argument, safe_flags,
                        numeric=subcommand == "log",
                        numeric_values=("-n",) if subcommand == "log" else ()):
                    return {"class": "unknown", "targets": [],
                            "resolution_complete": False,
                            "reason": "shell_unprovable"}
        if verb == "find":
            for argument in parts[1:]:
                if argument.startswith("-") and argument not in SAFE_FIND_FLAGS:
                    return {"class": "unknown", "targets": [],
                            "resolution_complete": False,
                            "reason": "shell_unprovable"}
        if verb in SAFE_INERT_FLAGS:
            for argument in parts[1:]:
                if not _safe_flag(
                        argument, SAFE_INERT_FLAGS[verb],
                        numeric=verb in ("head", "tail"),
                        numeric_values=("-n", "-c")
                        if verb in ("head", "tail") else ()):
                    return {"class": "unknown", "targets": [],
                            "resolution_complete": False,
                            "reason": "shell_unprovable"}
        return {"class": "read", "targets": [],
                "resolution_complete": True, "proof": "inert_verb"}
    return {"class": "unknown", "targets": [],
            "resolution_complete": False, "reason": "shell_unprovable"}


def classify_action(tool_name, tool_input, cwd=None, installed_schema=None,
                    capability_allowlist=None):
    """Classify one catch-all tool call and resolve every mutation target."""
    cwd = _real(cwd or os.getcwd())
    tool_input = dict(tool_input or {})
    if tool_name in CHILD_TOOLS:
        return {"class": "child", "targets": [],
                "resolution_complete": True, "input": tool_input}
    if tool_name in READ_TOOLS:
        raw = (tool_input.get("file_path") or tool_input.get("path"))
        target = _resolve(raw, cwd) if raw else None
        return {"class": "read", "targets": [target] if target else [],
                "resolution_complete": True, "proof": "builtin_read"}
    if tool_name in ("Bash", "Shell", "exec_command"):
        return _bash_action(tool_input.get("command") or tool_input.get("cmd"),
                            cwd)
    if tool_name in MUTATION_TOOLS:
        raw = (tool_input.get("file_path") or tool_input.get("path")
               or tool_input.get("notebook_path"))
        target = _resolve(raw, cwd)
        return {"class": "write", "targets": [target] if target else [],
                "resolution_complete": bool(target),
                "reason": None if target else "target_unresolved",
                "proof": "builtin_adapter"}
    allow = (capability_allowlist or {}).get(tool_name)
    if allow:
        if installed_schema is None:
            return {"class": "unknown", "targets": [],
                    "resolution_complete": False, "reason": "tool_unpinnable"}
        if _digest(installed_schema) != allow.get("schema_digest"):
            return {"class": "unknown", "targets": [],
                    "resolution_complete": False, "reason": "tool_schema_drift"}
        return {"class": "read", "targets": [],
                "resolution_complete": True, "proof": "schema_pin"}
    return {"class": "unknown", "targets": [],
            "resolution_complete": False, "reason": "unknown_tool_class"}


def decide(action, scope, created_paths=(), clean_tracked_paths=()):
    """Apply ownership and recoverability rules to a classified action."""
    action = dict(action or {})
    kind = action.get("class")
    targets = action.get("targets") or ()
    if kind == "read":
        if any(target and scope.is_protected(target) for target in targets):
            return {"allow": False, "reason": "protected_controller_state"}
        return {"allow": True, "reason": "read_only"}
    if kind in ("unknown", None) or not action.get("resolution_complete"):
        return {"allow": False,
                "reason": action.get("reason") or "target_unresolved"}
    if kind == "child":
        return {"allow": False, "reason": "child_requires_identity_policy"}
    created = {_real(p) for p in created_paths}
    clean = {_real(p) for p in clean_tracked_paths}
    for target in targets:
        if not target:
            return {"allow": False, "reason": "target_unresolved"}
        target = _real(target)
        owned_by_repo = any(
            target == root or _inside(target, root)
            for root in scope.repo_roots)
        for sibling in scope.sibling_worktrees:
            if not (target == sibling or _inside(target, sibling)):
                continue
            sibling_inside_owned = any(
                sibling == root or _inside(sibling, root)
                for root in scope.repo_roots)
            # A selected worktree may intentionally live below the main
            # worktree. Its ancestor is registered as a sibling, but must not
            # shadow the more-specific active root. Siblings at or below an
            # owned root still override that broader writable scope.
            if sibling_inside_owned or not owned_by_repo:
                return {"allow": False, "reason": "sibling_worktree"}
        if (scope.session_assets_dir and _inside(
                target, scope.session_assets_dir)
                and not (scope.is_declared_output(target)
                         or scope.is_role_temp(target)
                         or scope.is_controller_state(target))):
            return {"allow": False,
                    "reason": "session_asset_not_declared_output"}
        if not scope.owns(target):
            return {"allow": False, "reason": "target_outside_owned_scope"}
        if kind == "delete" and not (
                scope.is_role_temp(target) or target in created
                or target in clean):
            return {"allow": False, "reason": "delete_not_recoverable"}
    return {"allow": True, "reason": "owned_target",
            "target_count": len(targets)}


def sanitize(decision, action=None, work_id=None, parent_work_id=None,
             guard_attempt_id=None):
    """Return the content-free durable representation of a decision."""
    action = action or {}
    targets = action.get("targets") or ()
    return {
        "guard_attempt_id": guard_attempt_id,
        "work_id": work_id,
        "parent_work_id": parent_work_id,
        "allow": bool((decision or {}).get("allow")),
        "reason": (decision or {}).get("reason") or "unknown",
        "action_class": action.get("class") or "unknown",
        "target_count": len(targets),
        "path_digests": [_digest(_real(p)) for p in targets if p],
        "command_fingerprint": _digest({
            "proof": action.get("proof"), "class": action.get("class"),
            "target_count": len(targets),
        }),
    }
