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

import cowork_workunit as _work_unit_mod


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
# Compound proof (#39).  Commands are split only at unquoted, unescaped runs
# of operator characters; an operator character inside quotes or after a
# backslash is denied outright, so no stage text can ever contain one.  '~'
# joins the expansion class because _resolve does not expand it.
_OPERATOR_CHARS = "|;&"
_SUPPORTED_OPERATORS = ("&&", ";", "|")
_KNOWN_OPERATORS = ("&&", ";", "|", "||", "&", "|&", ";;")
_EXPANSION = re.compile(r"[$`*?\[\]{}()!~]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
UNPROVABLE_CONSTRUCTS = frozenset((
    "control_character", "expansion", "quoted_operator", "escaped_operator",
    "unsupported_operator", "empty_stage", "unbalanced_quote", "redirect",
    "mutating_stage", "unknown_command", "unsafe_flag", "interpreter_inline",
    "find_action", "script_path", "unresolved_target", "extra_operand"))
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}")
_SAFE_FLAG_TOKEN = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9-]{0,39}")
_FRAGMENT_MESSAGE_LIMIT = 160
# Flags whose value is a separate argument; that argument is not a file.
_VALUE_FLAGS = {
    "head": ("-n", "-c", "--lines", "--bytes"),
    "tail": ("-n", "-c", "--lines", "--bytes", "--pid", "--sleep-interval"),
    "sort": ("-k", "-t", "--key", "--field-separator"),
    "uniq": ("-f", "-s", "-w", "--skip-fields", "--skip-chars",
             "--check-chars"),
}
# Stdout-only helpers accepted by the proof path.  Deliberately separate from
# INERT_COMMANDS: readonly_bash_commands() feeds a prefix-injectable
# controller allowlist and must not grow with this table.
SINK_COMMANDS = frozenset(("cat", "sort", "uniq", "echo", "which"))
SAFE_SINK_FLAGS = {
    "cat": frozenset((
        "-b", "-e", "-n", "-s", "-t", "-u", "-v", "-A", "-E", "-T",
        "--number", "--number-nonblank", "--squeeze-blank", "--show-all",
        "--show-ends", "--show-tabs", "--show-nonprinting")),
    "sort": frozenset((
        "-b", "-c", "-C", "-d", "-f", "-g", "-h", "-k", "-M", "-n", "-r",
        "-s", "-t", "-u", "-V", "--ignore-leading-blanks", "--check",
        "--dictionary-order", "--ignore-case", "--general-numeric-sort",
        "--human-numeric-sort", "--key", "--month-sort", "--numeric-sort",
        "--reverse", "--stable", "--field-separator", "--unique",
        "--version-sort")),
    "uniq": frozenset((
        "-c", "-d", "-D", "-i", "-u", "-f", "-s", "-w", "--count",
        "--repeated", "--unique", "--ignore-case", "--skip-fields",
        "--skip-chars", "--check-chars")),
    "which": frozenset(("-a", "-s")),
}
_SINK_NUMERIC_VALUES = {"sort": ("-k",), "uniq": ("-f", "-s", "-w")}
# Verbs whose file operands become read targets (protected-path check).
_TARGET_SINKS = frozenset(("cat", "sort", "uniq", "head", "tail", "wc"))
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
        "--only-matching", "--quiet", "--recursive",
        "-A", "-B", "-C", "--after-context", "--before-context",
        "--context")),
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
        "kernel_boundary": "opportunistic",
    },
    ("opencode", "implement"): {
        "delegation": "proven_absent",
        "child_correlation": "unavailable",
        "mutation_gate": "controller_permissions",
        "kernel_boundary": "opportunistic",
    },
}


def readonly_bash_commands():
    """Ordered read-only shell commands vetted by this module's own tables:
    one `git <subcommand>` per SAFE_GIT_FLAGS entry, then the non-git inert
    commands. This is the single source of truth for any controller-facing
    read-only bash allowlist — never a hand-maintained copy."""
    git_forms = tuple("git %s" % sub for sub in SAFE_GIT_FLAGS)
    inert = tuple(sorted(INERT_COMMANDS - {"git"}))
    return git_forms + inert


def readonly_bash_glob_patterns():
    """Ordered, de-duplicated glob patterns for `readonly_bash_commands()`:
    the bare command and the command-with-arguments form. Callers pairing
    this with a permission map MUST back it with a kernel write boundary —
    glob prefix matching is injectable (`git status; <anything>` matches
    `git status *`)."""
    patterns = []
    for command in readonly_bash_commands():
        patterns.append(command)
        patterns.append(command + " *")
    return tuple(dict.fromkeys(patterns))


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


# --------------------------------------------------------------------------- #
# M2 Package C: governed-child-policy inheritance.                            #
#                                                                              #
# `decide_child` proves three of the four required inheritance fields —      #
# controller, model, effort. This section proves the fourth,                 #
# `governed_child_policy` (`cowork_workunit.GOVERNED_CHILD_POLICIES`), the    #
# same way: an absent or unknown policy is never silently read as `inherit`  #
# or `denied`, exactly like a missing controller/model/effort is never       #
# silently read as inherited. `decide_child_governed` composes both checks   #
# into the single WorkUnit-typed child-dispatch decision function this       #
# package exposes for E to call (frozen-plan invariant: "a child missing     #
# any one of the four fails closed").                                        #
# --------------------------------------------------------------------------- #


def parent_effective_from_work_unit(work_unit, controller_source=None,
                                    model_source=None, effort_source=None):
    """Project a validated parent WorkUnit dict (`cowork_workunit.
    validate_work_unit`) down to the `parent_effective` shape `decide_child`/
    `decide_child_governed` consume — mirrors `cowork_workunit.
    graph_node_from_work_unit`'s own projection pattern, so a child-dispatch
    decision reads its parent's identity FROM a real WorkUnit record rather
    than a hand-built shadow copy of the same fields. Never mutates input.

    Frozen-plan M3 fix: `WorkUnit` carries NO `*_source` provenance fields
    (see `cowork_workunit._WORK_UNIT_KEYS`), so this projection can never
    honestly derive `controller_source`/`model_source`/`effort_source` from
    the WorkUnit itself — doing so from mere presence (the pre-fix
    behavior: `"config_pinned" if controller else None`) fabricates
    provenance and degrades `decide_child`'s `source not in
    KNOWN_IDENTITY_SOURCES` gate to a non-null check, mislabelling a value
    genuinely learned from `live_event` as `config_pinned`.

    Each `*_source` argument is therefore an EXPLICIT, caller-supplied
    identity source — the caller's own honest account of how it resolved
    `controller`/`effective_model`/`effort` for THIS WorkUnit (e.g.
    `'config_pinned'` when session configuration pinned it, `'live_event'`
    when it was observed from the controller's own output). A source not
    named in `KNOWN_IDENTITY_SOURCES` (including the default `None`, i.e.
    no source supplied) projects to `None`, which `decide_child`'s existing
    gate already fails closed on — never silently accepted. This function
    performs no Package A expansion and wires no live call site: a caller
    with no honest source to supply gets a fail-closed projection, not a
    new WorkUnit field.
    """
    work_unit = dict(work_unit or {})

    def _known_source(source):
        return source if source in KNOWN_IDENTITY_SOURCES else None

    return {
        "controller": work_unit.get("controller"),
        "controller_source": _known_source(controller_source),
        "model": work_unit.get("effective_model"),
        "model_source": _known_source(model_source),
        "effort": work_unit.get("effort"),
        "effort_source": _known_source(effort_source),
        "governed_child_policy": work_unit.get("governed_child_policy"),
    }


def decide_child_policy_inheritance(parent_effective, allowed_child_policies=None):
    """Fail-closed governed-child-policy inheritance check.

    `parent_effective` must carry a non-null `governed_child_policy` naming a
    member of `cowork_workunit.GOVERNED_CHILD_POLICIES` — an absent or
    unrecognized value is refused (`parent_policy_unresolved`), never read as
    permission. `denied` always refuses (`governed_child_denied`): this
    parent may not spawn governed children at all. `inherit` and `isolated`
    both permit spawning (they differ only in how the CHILD is subsequently
    governed, a distinction later stages own) unless the caller supplies
    `allowed_child_policies` and the parent's policy is not among them.
    """
    parent = dict(parent_effective or {})
    child_policy = parent.get("governed_child_policy")
    if child_policy not in _work_unit_mod.GOVERNED_CHILD_POLICIES:
        return {"allow": False, "reason": "parent_policy_unresolved",
                "governed_child_policy": child_policy}
    if child_policy == "denied":
        return {"allow": False, "reason": "governed_child_denied",
                "governed_child_policy": child_policy}
    if (allowed_child_policies is not None
            and child_policy not in set(allowed_child_policies)):
        return {"allow": False, "reason": "child_policy_not_permitted",
                "governed_child_policy": child_policy}
    return {"allow": True, "reason": "governed_child_policy_inherited",
            "governed_child_policy": child_policy}


def decide_child_governed(requested_child, parent_effective, allowed_controllers,
                          allowed_child_policies=None, pin_capability=True):
    """WorkUnit-typed child-dispatch decision function (frozen-plan C
    deliverable): the single decision proving ALL FOUR required inheritance
    fields before a child may dispatch — controller, model, effort (via
    `decide_child`) AND `governed_child_policy` (via
    `decide_child_policy_inheritance`). A child missing any one of the four
    fails closed. `decide_child` runs first since it is already the
    production correlation-blocking primitive; the policy check only
    NARROWS an already-allowed decision and can never loosen a `decide_child`
    denial.
    """
    decision = decide_child(requested_child, parent_effective,
                            allowed_controllers, pin_capability=pin_capability)
    if not decision.get("allow"):
        return decision
    policy_decision = decide_child_policy_inheritance(
        parent_effective, allowed_child_policies=allowed_child_policies)
    if not policy_decision.get("allow"):
        return {"allow": False, "reason": policy_decision["reason"],
                "requested": requested_child, "effective": decision.get("effective"),
                "governed_child_policy": policy_decision.get("governed_child_policy")}
    result = dict(decision)
    result["governed_child_policy"] = policy_decision["governed_child_policy"]
    return result


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


def _unprovable(construct, stage_index, stage_count, fragment,
                operator=None, verb=None, flag=None):
    """In-memory stage detail; sanitize() reduces it to content-free form."""
    return {"construct": construct, "stage_index": stage_index,
            "stage_count": stage_count, "fragment": fragment,
            "operator": operator, "verb": verb, "flag": flag}


def _split_compound(command):
    """Split at unquoted `&&`, `;` and `|`; fail closed on anything else.

    Returns ({"stages": [...], "operators": [...]}, None) or (None, detail).
    """
    stages, operators = [], []
    stage_flags = {}
    current = []
    quote = None
    i, n = 0, len(command)
    while i < n:
        char = command[i]
        stage = len(stages)
        if quote == "'":
            if char in _OPERATOR_CHARS:
                stage_flags.setdefault(stage, "quoted_operator")
            elif char == "'":
                quote = None
            current.append(char)
            i += 1
            continue
        if quote == '"':
            if char == "\\" and i + 1 < n:
                if command[i + 1] in _OPERATOR_CHARS:
                    stage_flags.setdefault(stage, "quoted_operator")
                current.append(command[i:i + 2])
                i += 2
                continue
            if char in _OPERATOR_CHARS:
                stage_flags.setdefault(stage, "quoted_operator")
            elif char == '"':
                quote = None
            current.append(char)
            i += 1
            continue
        if char == "\\":
            if i + 1 < n and command[i + 1] in _OPERATOR_CHARS:
                stage_flags.setdefault(stage, "escaped_operator")
            current.append(command[i:i + 2])
            i += 2
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            i += 1
            continue
        if char in _OPERATOR_CHARS:
            end = i
            while end < n and command[end] in _OPERATOR_CHARS:
                end += 1
            run = command[i:end]
            before = command[i - 1] if i else ""
            after = command[end] if end < n else ""
            if before in ("<", ">") or (set(run) == {"&"} and after == ">"):
                # fd duplication / clobber forms such as 2>&1, >| and &>:
                # a redirect, kept inside the stage rather than split on.
                stage_flags.setdefault(stage, ("redirect", run))
                current.append(run)
                i = end
                continue
            stages.append("".join(current))
            operators.append(run)
            current = []
            i = end
            continue
        current.append(char)
        i += 1
    stages.append("".join(current))
    if quote is not None:
        stage_flags[len(stages) - 1] = "unbalanced_quote"
    count = len(stages)
    for index, text in enumerate(stages):
        fragment = text.strip()
        joining = operators[index - 1] if index else None
        flagged = stage_flags.get(index)
        if flagged is not None:
            if isinstance(flagged, tuple):
                return None, _unprovable(flagged[0], index + 1, count,
                                         flagged[1], operator=flagged[1])
            return None, _unprovable(flagged, index + 1, count, fragment,
                                     operator=joining)
        if not fragment:
            return None, _unprovable("empty_stage", index + 1, count,
                                     fragment, operator=joining)
        if _CONTROL.search(text):
            return None, _unprovable("control_character", index + 1, count,
                                     fragment, operator=joining)
        if _EXPANSION.search(text):
            return None, _unprovable("expansion", index + 1, count,
                                     fragment, operator=joining)
        if index < len(operators) and (
                operators[index] not in _SUPPORTED_OPERATORS):
            return None, _unprovable("unsupported_operator", index + 1,
                                     count, operators[index],
                                     operator=operators[index])
    return {"stages": stages, "operators": operators}, None


def _sink_operands(verb, args):
    """File operands of a sink verb: skip flags and separate flag values."""
    value_flags = _VALUE_FLAGS.get(verb, ())
    operands = []
    skip_next = False
    after_dashes = False
    for argument in args:
        if after_dashes:
            operands.append(argument)
        elif skip_next:
            skip_next = False
        elif argument == "--":
            after_dashes = True
        elif argument in value_flags:
            skip_next = True
        elif not argument.startswith("-"):
            operands.append(argument)
    return operands


def _stage_fail(construct, reason="shell_unprovable", verb=None, flag=None):
    return {"class": "unknown", "targets": [], "resolution_complete": False,
            "reason": reason, "construct": construct, "verb": verb,
            "flag": flag}


def _check_flags(verb, args, allowed, numeric=False, numeric_values=()):
    for argument in args:
        if not _safe_flag(argument, allowed, numeric=numeric,
                          numeric_values=numeric_values):
            return _stage_fail("unsafe_flag", verb=verb, flag=argument)
    return None


def _bash_stage(fragment, cwd, compound):
    """Prove one stage.  Single-stage results keep the pre-#39 classes."""
    if "<" in fragment:
        return _stage_fail("redirect")
    if compound and ">" in fragment:
        return _stage_fail("redirect")
    try:
        parts = shlex.split(fragment)
    except ValueError:
        return _stage_fail("unbalanced_quote")
    if not parts:
        return _stage_fail("empty_stage")
    if any(p.startswith("=") for p in parts):
        return _stage_fail("expansion")
    verb = os.path.basename(parts[0])
    if parts[0].startswith("./") or parts[0].endswith((".sh", ".py")):
        return _stage_fail("script_path", verb=verb)
    if verb in ("python", "python3", "perl", "ruby", "node") and any(
            p in ("-c", "-e") for p in parts[1:]):
        return _stage_fail("interpreter_inline", verb=verb)
    if verb == "find":
        for p in parts:
            if p in ("-exec", "-execdir", "-delete"):
                return _stage_fail("find_action", verb=verb, flag=p)
    redirects = [_resolve(m.group(1), cwd) for m in REDIRECT.finditer(fragment)]
    if any(p is None for p in redirects):
        return _stage_fail("unresolved_target", reason="target_unresolved",
                           verb=verb)
    if redirects:
        return {"class": "write", "targets": redirects,
                "resolution_complete": True, "proof": "shell_redirect",
                "verb": verb}
    if verb in MUTATING_COMMANDS:
        if verb in ("mv", "dd"):
            return _stage_fail("mutating_stage", verb=verb)
        candidates = [p for p in parts[1:] if not p.startswith("-")]
        # Source operands are harmless for cp/mv/install; destination is last.
        if verb in ("cp", "mv", "install", "ln") and candidates:
            candidates = candidates[-1:]
        targets = [_resolve(p, cwd) for p in candidates]
        if not targets or any(p is None for p in targets):
            return _stage_fail("unresolved_target",
                               reason="target_unresolved", verb=verb)
        return {"class": "delete" if verb in ("rm", "rmdir") else "write",
                "targets": targets, "resolution_complete": True,
                "proof": "shell_argv", "verb": verb}
    if verb not in INERT_COMMANDS and verb not in SINK_COMMANDS:
        return _stage_fail("unknown_command", verb=verb)
    if verb == "git":
        subcommand = parts[1] if len(parts) > 1 else None
        safe_flags = SAFE_GIT_FLAGS.get(subcommand)
        if safe_flags is None:
            return _stage_fail("unknown_command", verb=verb)
        failed = _check_flags(
            verb, parts[2:], safe_flags, numeric=subcommand == "log",
            numeric_values=("-n",) if subcommand == "log" else ())
        if failed:
            return failed
    if verb == "find":
        for argument in parts[1:]:
            if argument.startswith("-") and argument not in SAFE_FIND_FLAGS:
                return _stage_fail("unsafe_flag", verb=verb, flag=argument)
    if verb in SAFE_INERT_FLAGS:
        if verb in ("head", "tail"):
            numeric_values = ("-n", "-c")
        elif verb == "grep":
            numeric_values = ("-A", "-B", "-C")
        else:
            numeric_values = ()
        failed = _check_flags(verb, parts[1:], SAFE_INERT_FLAGS[verb],
                              numeric=verb in ("head", "tail"),
                              numeric_values=numeric_values)
        if failed:
            return failed
    if verb in SAFE_SINK_FLAGS:
        if verb == "uniq" and "--" in parts[1:]:
            return _stage_fail("unsafe_flag", verb=verb, flag="--")
        failed = _check_flags(
            verb, parts[1:], SAFE_SINK_FLAGS[verb],
            numeric_values=_SINK_NUMERIC_VALUES.get(verb, ()))
        if failed:
            return failed
    targets = []
    if verb in _TARGET_SINKS:
        operands = _sink_operands(verb, parts[1:])
        if verb == "uniq" and len(operands) > 1:
            return _stage_fail("extra_operand", verb=verb)
        targets = [_resolve(p, cwd) for p in operands]
        if any(p is None for p in targets):
            return _stage_fail("unresolved_target",
                               reason="target_unresolved", verb=verb)
    return {"class": "read", "targets": targets,
            "resolution_complete": True, "proof": "inert_verb", "verb": verb}


def _stage_result(result):
    """Drop the stage-internal diagnostic keys from a proven stage."""
    return {key: value for key, value in result.items()
            if key not in ("construct", "verb", "flag")}


def _bash_action(command, cwd):
    if not isinstance(command, str) or not command.strip():
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "target_unresolved"}
    split, detail = _split_compound(command)
    if detail is not None:
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable",
                "unprovable": detail}
    stages, operators = split["stages"], split["operators"]
    count = len(stages)
    if count == 1:
        fragment = stages[0].strip()
        result = _bash_stage(fragment, cwd, compound=False)
        if result["class"] == "unknown":
            failed = _stage_result(result)
            failed["unprovable"] = _unprovable(
                result["construct"], 1, 1, fragment, verb=result.get("verb"),
                flag=result.get("flag"))
            return failed
        return _stage_result(result)
    targets = []
    for index, text in enumerate(stages):
        fragment = text.strip()
        result = _bash_stage(fragment, cwd, compound=True)
        if result["class"] == "read":
            targets.extend(result["targets"])
            continue
        construct = (result.get("construct")
                     if result["class"] == "unknown" else "mutating_stage")
        return {"class": "unknown", "targets": [],
                "resolution_complete": False, "reason": "shell_unprovable",
                "unprovable": _unprovable(
                    construct, index + 1, count, fragment,
                    operator=operators[index - 1] if index else None,
                    verb=result.get("verb"), flag=result.get("flag"))}
    return {"class": "read", "targets": targets, "resolution_complete": True,
            "proof": "compound_read", "stage_count": count}


def unprovable_message(detail):
    """Bounded, human-readable stage detail for a hook deny reason."""
    if not detail:
        return ""
    construct = detail.get("construct") or "unknown"
    flag = detail.get("flag")
    if isinstance(flag, str) and _SAFE_FLAG_TOKEN.fullmatch(
            flag.split("=", 1)[0]):
        construct = "%s %s" % (construct, flag.split("=", 1)[0])
    fragment = detail.get("fragment") or ""
    text = json.dumps(fragment[:_FRAGMENT_MESSAGE_LIMIT])
    if len(fragment) > _FRAGMENT_MESSAGE_LIMIT:
        text += "…"
    return "(stage %s of %s, %s: %s)" % (
        detail.get("stage_index"), detail.get("stage_count"), construct, text)


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
    record = {
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
    if action.get("stage_count") is not None:
        record["stage_count"] = action["stage_count"]
    detail = action.get("unprovable")
    if detail:
        record["unprovable"] = _sanitize_unprovable(detail)
    return record


def _sanitize_unprovable(detail):
    """Closed enums, regex-gated tokens and a digest; never raw text."""
    operator = detail.get("operator")
    construct = detail.get("construct")
    fragment = detail.get("fragment")
    raw = (fragment if isinstance(fragment, str) else "").encode(
        "utf-8", "surrogatepass")
    result = {
        "stage_index": detail.get("stage_index"),
        "stage_count": detail.get("stage_count"),
        "operator": (None if operator is None else
                     operator if operator in _KNOWN_OPERATORS else "other"),
        "construct": (construct if construct in UNPROVABLE_CONSTRUCTS
                      else "other"),
        "fragment_sha256": hashlib.sha256(raw).hexdigest(),
        "fragment_bytes": len(raw),
    }
    verb = detail.get("verb")
    if isinstance(verb, str) and _SAFE_TOKEN.fullmatch(verb):
        result["verb"] = verb
    flag = detail.get("flag")
    if isinstance(flag, str):
        name = flag.split("=", 1)[0]
        if _SAFE_FLAG_TOKEN.fullmatch(name):
            result["flag"] = name
    return result


def rtk_command_forms():
    """Closed tuple of every allowed rtk command form — pure derivation of readonly_bash_commands()."""
    return tuple("rtk " + cmd for cmd in readonly_bash_commands())
