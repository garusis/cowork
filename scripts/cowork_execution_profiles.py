#!/usr/bin/env python3
"""Execution profiles: explicit, versioned light / standard / assurance policy.

This module is the ONLY owner of execution-profile policy. The runtime
(`cowork.py`, `cowork_state.py`, `cowork_verification.py`,
`cowork_measure.py`, `cowork_report.py`) consults it through thin hooks and
never re-derives a decision. It is deliberately distinct from
`cowork_profiles.py`, which owns controller authentication profiles.

A profile jointly controls: which paired phases run, the batch boundary, the
focused/cumulative/full check cadence and dependency-bound evidence reuse, the
build-review thresholds and deferred minor notes, the invalidation rule,
deterministic one-way promotion, and a frozen serial concurrency contract
that a later scheduler consumes (`resolved_vertex_policy`).

Pure and stdlib-only: nothing here performs I/O except through the
caller-supplied `save_fn`/`trace_fn` of `ProfileSession`. The only sibling
import is lazy (`_default_inventory_validator`).

Python 3.9+.
"""

import fnmatch
import hashlib
import json
import re

POLICY_VERSION = 1
RECORD_SCHEMA = 1
PREVIEW_SCHEMA = 1

PROFILE_LIGHT = "light"
PROFILE_STANDARD = "standard"
PROFILE_ASSURANCE = "assurance"
PROFILES = (PROFILE_LIGHT, PROFILE_STANDARD, PROFILE_ASSURANCE)
RANK = {PROFILE_LIGHT: 0, PROFILE_STANDARD: 1, PROFILE_ASSURANCE: 2}

# Checks no profile may weaken.
REQUIRED_CHECKS = ("owned_verification_final_suite",
                   "paired_reviewer_approval", "user_declared_checks")

PLAN_SOURCE_PLAN = "plan"
PLAN_SOURCE_INTEL = "intel"

LIGHT_BATCH_CAP = 8
MAX_BATCH_ARTIFACTS = 1024

DOC_EXTENSIONS = (".md", ".markdown", ".rst", ".txt", ".adoc")
CHECK_CLASSES = ("lint", "format")

CONCURRENCY_CONTRACT_VERSION = 1

# Closed reason codes a verdict screen may return (stop kind
# `review_profile_rejected` carries one as `profile_rejected`).
REJECT_CORRECTIVE_ON_APPROVE = "corrective_findings_on_approve"
REJECT_NOTES_REFUSED = "deferred_notes_refused"
REJECT_NOTES_MALFORMED = "deferred_notes_malformed"
REJECT_CODES = (REJECT_CORRECTIVE_ON_APPROVE, REJECT_NOTES_REFUSED,
                REJECT_NOTES_MALFORMED)

# Closed record-validation reason codes.
RECORD_REASONS = ("record_missing", "record_unparseable", "record_schema",
                  "binding_mismatch", "policy_digest_mismatch",
                  "effective_below_selected", "history_inconsistent")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UnknownProfile(ValueError):
    def __init__(self, name):
        self.name = name
        super().__init__("unknown execution profile %r" % (name,))


# --------------------------------------------------------------------------- #
# Frozen policy tables.                                                       #
# --------------------------------------------------------------------------- #


class _FrozenDict(dict):
    def _blocked(self, *_a, **_k):
        raise TypeError("execution profile policy is frozen")

    __setitem__ = __delitem__ = clear = pop = popitem = _blocked
    setdefault = update = _blocked


def _freeze(value):
    if isinstance(value, dict):
        return _FrozenDict((k, _freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, dict):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


_ALL_ROLES = ("scout", "scout-reviewer", "planner", "planning-advisor",
              "builder", "build-reviewer")
_LIGHT_ROLES = ("scout", "scout-reviewer", "builder", "build-reviewer")

_PHASE_SCOUTING = {"phase": "scouting", "lead": "scout",
                   "reviewer": "scout-reviewer"}
_PHASE_PLANNING = {"phase": "planning", "lead": "planner",
                   "reviewer": "planning-advisor"}
_PHASE_BUILDING = {"phase": "building", "lead": "builder",
                   "reviewer": "build-reviewer"}

CONCURRENCY = {"contract_version": CONCURRENCY_CONTRACT_VERSION,
               "mode": "serial", "max_parallel_vertices": 1,
               "fan_out_shapes": []}


def _definition(roles, phases, boundary, max_artifacts, declared_in,
                reuse, deferred, deferable, revise_severities,
                invalidation_rule):
    return {
        "roles": list(roles),
        "phases": [dict(p) for p in phases],
        "batch": {"boundary": boundary, "max_artifacts": max_artifacts,
                  "declared_in": declared_in, "derivatives": "direct"},
        "checks": {
            "focused": "rerun_when_declared_dependencies_change",
            "cumulative": "one_owned_transaction_per_ready_for_review",
            "full": "final_suite_required_every_transaction",
            "reuse": reuse,
        },
        "review": {
            "cadence": "one_build_review_per_ready_for_review",
            "deferred_minor_notes": deferred,
            "approve_requires_zero_corrective_findings": True,
        },
        "thresholds": {"revise_severities": list(revise_severities),
                       "deferable_severities": list(deferable)},
        "invalidation": {"rule": invalidation_rule,
                         "undeclared_dependencies": "rerun",
                         "executable_change": "invalidate_all"},
        "required_checks": list(REQUIRED_CHECKS),
        "concurrency": dict(CONCURRENCY),
    }


PROFILE_DEFINITIONS = _freeze({
    PROFILE_LIGHT: _definition(
        _LIGHT_ROLES, (_PHASE_SCOUTING, _PHASE_BUILDING),
        "documentation_family", LIGHT_BATCH_CAP, "intel",
        "dependency_digest", "permitted", ("minor",),
        ("blocking", "major"), "changed_artifact_plus_direct_derivatives"),
    PROFILE_STANDARD: _definition(
        _ALL_ROLES, (_PHASE_SCOUTING, _PHASE_PLANNING, _PHASE_BUILDING),
        "behavior_change", None, "plan",
        "dependency_digest", "permitted", ("minor",),
        ("blocking", "major"), "changed_artifact_plus_direct_derivatives"),
    PROFILE_ASSURANCE: _definition(
        _ALL_ROLES, (_PHASE_SCOUTING, _PHASE_PLANNING, _PHASE_BUILDING),
        "invariant_boundary", None, "plan",
        "none", "refused", (), ("blocking", "major", "minor"),
        "all_on_any_change"),
})

TRIGGER_SCOPE_EXPANSION = "scope_expansion"
TRIGGER_BATCH_UNDECLARED = "batch_undeclared"
TRIGGER_EXECUTABLE_CHANGE = "executable_change"
TRIGGER_SOURCE_CONFLICT = "source_conflict"
TRIGGER_EVIDENCE_FAILED = "evidence_failed"
TRIGGER_MATERIAL_FINDING = "material_finding"
TRIGGER_ARCHITECTURAL_RISK = "architectural_risk"
TRIGGER_REPEATED_REJECTION = "repeated_rejection"
TRIGGER_EXPLICIT_REQUEST = "explicit_request"
TRIGGER_SIGNAL_MALFORMED = "signal_malformed"

_TARGET_NEXT_RANK = "next_rank"
_TARGET_REQUESTED = "requested"


def _trigger(trigger, signal, seam, target):
    return {"trigger": trigger, "signal": signal, "seam": seam,
            "target": target, "reason_code": trigger}


# Evaluated, and recorded, in this order.
PROMOTION_TRIGGERS = _freeze([
    _trigger(TRIGGER_SCOPE_EXPANSION,
             "builder changed paths outside the declared batch artifacts "
             "and derivatives, or a declared batch exceeds the profile cap",
             "intel approval (cap) / builder ready_for_review "
             "(changed paths)", _TARGET_NEXT_RANK),
    _trigger(TRIGGER_BATCH_UNDECLARED,
             "light intel has no result.batch or no verification inventory",
             "intel approval", PROFILE_STANDARD),
    _trigger(TRIGGER_EXECUTABLE_CHANGE,
             "a declared batch path or a builder changed path is classified "
             "executable/generator by the closed classification rule",
             "intel approval / builder ready_for_review", PROFILE_STANDARD),
    _trigger(TRIGGER_SOURCE_CONFLICT,
             "non-empty result.source_conflicts in scout intel or builder "
             "status", "intel approval / builder ready_for_review",
             PROFILE_STANDARD),
    _trigger(TRIGGER_EVIDENCE_FAILED,
             "owned verification verdict red or unverified, except failures "
             "confined to declared lint/format check classes",
             "builder ready_for_review gate", PROFILE_STANDARD),
    _trigger(TRIGGER_MATERIAL_FINDING,
             "build-reviewer corrective finding with severity blocking or "
             "major", "build-review verdict", PROFILE_STANDARD),
    _trigger(TRIGGER_ARCHITECTURAL_RISK,
             "any reviewer corrective finding with risk_class "
             "'architectural', or scout intel result.risk_class "
             "'architectural'", "any review verdict / intel approval",
             PROFILE_ASSURANCE),
    _trigger(TRIGGER_REPEATED_REJECTION,
             "review round cap reached in any phase", "review loop",
             PROFILE_ASSURANCE),
    _trigger(TRIGGER_EXPLICIT_REQUEST,
             "--profile X on resume with rank(X) above the current profile",
             "invocation", _TARGET_REQUESTED),
    _trigger(TRIGGER_SIGNAL_MALFORMED,
             "any promotion signal field present but not matching its closed "
             "schema", "wherever the field is read", PROFILE_ASSURANCE),
])

TRIGGER_ORDER = tuple(t["reason_code"] for t in PROMOTION_TRIGGERS)
_TARGET_BY_CODE = {t["reason_code"]: t["target"] for t in PROMOTION_TRIGGERS}

ROUND_CAP_TRIGGERS = (TRIGGER_REPEATED_REJECTION,)


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def policy_digest():
    return hashlib.sha256(_canonical({
        "policy_version": POLICY_VERSION,
        "definitions": _thaw(PROFILE_DEFINITIONS),
        "triggers": _thaw(PROMOTION_TRIGGERS)}).encode("utf-8")).hexdigest()


def known_profiles():
    return list(PROFILES)


def team_for(profile):
    if profile not in RANK:
        raise UnknownProfile(profile)
    return list(PROFILE_DEFINITIONS[profile]["roles"])


def rank(profile):
    if profile not in RANK:
        raise UnknownProfile(profile)
    return RANK[profile]


def concurrency_contract(profile):
    if profile not in RANK:
        raise UnknownProfile(profile)
    return _thaw(PROFILE_DEFINITIONS[profile]["concurrency"])


def resolved_vertex_policy(profile, role):
    """The per-vertex policy a later scheduler consumes. Serial contract only;
    nothing here schedules."""
    if profile not in RANK:
        raise UnknownProfile(profile)
    definition = PROFILE_DEFINITIONS[profile]
    phase = None
    for p in definition["phases"]:
        if role in (p["lead"], p["reviewer"]):
            phase = p["phase"]
            break
    return {
        "profile": profile,
        "phase": phase,
        "concurrency": _thaw(definition["concurrency"]),
        "required_checks": list(definition["required_checks"]),
        "thresholds": _thaw(definition["thresholds"]),
        "reuse_mode": definition["checks"]["reuse"],
    }


def preview(name):
    """The complete effective policy of one profile, as a JSON-able dict.
    Raises UnknownProfile for any name not in PROFILES."""
    if name not in RANK:
        raise UnknownProfile(name)
    out = {"preview_schema": PREVIEW_SCHEMA, "profile": name,
           "policy_version": POLICY_VERSION, "policy_digest": policy_digest()}
    out.update(_thaw(PROFILE_DEFINITIONS[name]))
    out["promotion"] = {"rank": RANK[name],
                        "triggers": _thaw(PROMOTION_TRIGGERS)}
    return out


def non_weakening_violations(profile):
    """Reasons a profile definition would weaken a required check. Empty for
    every shipped profile."""
    if profile not in RANK:
        raise UnknownProfile(profile)
    d = PROFILE_DEFINITIONS[profile]
    problems = []
    if tuple(d["required_checks"]) != REQUIRED_CHECKS:
        problems.append("required_checks_differ")
    if d["checks"]["full"] != "final_suite_required_every_transaction":
        problems.append("final_suite_not_required")
    if not d["review"]["approve_requires_zero_corrective_findings"]:
        problems.append("approve_allows_corrective_findings")
    for phase in d["phases"]:
        if not phase.get("reviewer"):
            problems.append("unpaired_phase:%s" % phase.get("phase"))
        elif phase["lead"] not in d["roles"] or (
                phase["reviewer"] not in d["roles"]):
            problems.append("phase_role_missing:%s" % phase.get("phase"))
    if d["concurrency"]["mode"] != "serial":
        problems.append("concurrency_not_serial")
    return problems


# --------------------------------------------------------------------------- #
# Context envelopes.                                                          #
# --------------------------------------------------------------------------- #
#
# A context envelope is the per-(profile, role) LIMITS a later stage compares
# an observation against. It is a separate frozen table, not part of
# PROFILE_DEFINITIONS: folding it into the definitions would change
# `policy_digest`, which `validate_record` checks, and invalidate every saved
# profiled session. The table holds limits only. It carries no required check,
# review cadence or threshold, so no entry can weaken a profile's policy; an
# unknown or missing limit is advisory and never stops anything.

CONTEXT_ENVELOPE_VERSION = 1

# Literal copies of the metric classification owned by `cowork_context`; the
# two modules do not import each other and a test keeps the copies equal.
_ENVELOPE_METRICS = ("prompt_bytes", "artifact_bytes", "repository_reads",
                     "elapsed_ms", "reported_input_tokens",
                     "cache_read_tokens")
_ENVELOPE_SESSION_WINDOW_METRICS = ("repository_reads", "elapsed_ms",
                                    "reported_input_tokens",
                                    "cache_read_tokens")

# Substrings that mark a policy key. Matched against lowercased key names
# inside a role entry, where no legitimate envelope key contains any of them.
_ENVELOPE_POLICY_KEY_PARTS = ("required_checks", "review", "cadence",
                              "threshold")

_MISSING = object()


def _envelope(limits, step_pct, max_per_chain, rotation_trigger_metrics):
    return {
        "limits": {metric: {"warn": warn, "hard": hard}
                   for metric, (warn, hard) in limits.items()},
        "expansion": {"step_pct": step_pct, "max_per_chain": max_per_chain},
        "rotation_trigger_metrics": list(rotation_trigger_metrics),
    }


def _default_envelope():
    # Conservative defaults, not provider limits. cache_read_tokens has no
    # defensible number (providers expose it unevenly), so it stays None.
    return _envelope(
        {"prompt_bytes": (60000, 120000),
         "artifact_bytes": (200000, 400000),
         "repository_reads": (150, 300),
         "elapsed_ms": (1800000, 3600000),
         "reported_input_tokens": (120000, 180000),
         "cache_read_tokens": (None, None)},
        25, 2, ("reported_input_tokens", "repository_reads", "elapsed_ms"))


CONTEXT_ENVELOPES = _freeze({
    PROFILE_LIGHT: {role: _default_envelope() for role in _LIGHT_ROLES},
    PROFILE_STANDARD: {role: _default_envelope() for role in _ALL_ROLES},
    PROFILE_ASSURANCE: {role: _default_envelope() for role in _ALL_ROLES},
})


def resolved_context_envelope(profile, role):
    """The context envelope of one (profile, role) as an independent copy, or
    None when the role is not on the profile's team (an unprofiled caller then
    gets no limits, which a later stage reads as advisory). Raises
    UnknownProfile for a profile that does not exist."""
    if profile not in RANK:
        raise UnknownProfile(profile)
    if role not in team_for(profile):
        return None
    entry = CONTEXT_ENVELOPES[profile].get(role)
    if entry is None:
        return None
    out = {"envelope_version": CONTEXT_ENVELOPE_VERSION,
           "profile": profile, "role": role}
    out.update(_thaw(entry))
    return out


def context_envelope_digest():
    """Digest of the whole envelope table. Independent of `policy_digest`: the
    table is read at call time and never feeds the policy digest."""
    return hashlib.sha256(_canonical({
        "envelope_version": CONTEXT_ENVELOPE_VERSION,
        "envelopes": _thaw(CONTEXT_ENVELOPES)}).encode("utf-8")).hexdigest()


def _is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _policy_key_paths(value, prefix=""):
    """Dotted paths of every key, at any depth, naming a policy concept."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = prefix + str(key)
            lowered = str(key).lower()
            if any(part in lowered for part in _ENVELOPE_POLICY_KEY_PARTS):
                found.append(path)
            found.extend(_policy_key_paths(child, path + "."))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_policy_key_paths(child, prefix))
    return found


def envelope_non_weakening_violations(profile):
    """Reasons a profile's context envelopes would stop being limits-only.
    Empty for every shipped profile. Role-level keys are checked by team
    membership alone (the legitimate role names scout-reviewer and
    build-reviewer contain 'review'); the policy-key walk starts inside each
    role entry. An entry that is not a mapping with a mapping of limits is
    reported as missing, since it offers no usable envelope."""
    if profile not in RANK:
        raise UnknownProfile(profile)
    table = CONTEXT_ENVELOPES[profile]
    team = team_for(profile)
    problems = []
    for role in table:
        if role not in team:
            problems.append("envelope_role_not_in_team:%s" % (role,))
    for role in team:
        entry = table.get(role)
        if not isinstance(entry, dict) or not isinstance(
                entry.get("limits"), dict):
            problems.append("envelope_missing_for_role:%s" % (role,))
            continue
        for path in _policy_key_paths(entry):
            problems.append("policy_key_in_envelope:%s:%s" % (role, path))
        for metric, limit in entry["limits"].items():
            if metric not in _ENVELOPE_METRICS:
                problems.append("unknown_metric:%s:%s" % (role, metric))
                continue
            # A limit that is not a mapping, or lacks a bound, is invalid: an
            # absent bound must be spelled None.
            limit = limit if isinstance(limit, dict) else {}
            bounds = {}
            for name in ("warn", "hard"):
                value = limit.get(name, _MISSING)
                if value is not None and not (
                        _is_plain_int(value) and value >= 0):
                    problems.append("limit_invalid:%s:%s:%s" % (
                        role, metric, name))
                else:
                    bounds[name] = value
            if (_is_plain_int(bounds.get("warn"))
                    and _is_plain_int(bounds.get("hard"))
                    and bounds["hard"] < bounds["warn"]):
                problems.append("hard_below_warn:%s:%s" % (role, metric))
        rotation = entry.get("rotation_trigger_metrics")
        if not isinstance(rotation, (list, tuple)):
            problems.append("rotation_metric_not_session_window:%s:%s" % (
                role, "rotation_trigger_metrics"))
        else:
            for metric in rotation:
                if metric not in _ENVELOPE_SESSION_WINDOW_METRICS:
                    problems.append(
                        "rotation_metric_not_session_window:%s:%s" % (
                            role, metric))
        expansion = entry.get("expansion")
        if not (isinstance(expansion, dict)
                and all(_is_plain_int(expansion.get(k))
                        and expansion[k] > 0
                        for k in ("step_pct", "max_per_chain"))):
            problems.append("expansion_invalid:%s" % (role,))
    return problems


def role_brief_note(record, record_path="<execution profile record>"):
    """Plain text appended to the scout's first-message brief for a profiled
    session."""
    selected = record.get("selected")
    effective = record.get("effective")
    lines = [
        "Execution profile: this session runs under the %r profile "
        "(effective %r). Read its policy from %s." % (
            selected, effective, record_path)]
    if effective == PROFILE_LIGHT:
        lines.append(
            "Under light the approved intel is the plan: write result.batch "
            "{artifacts, derivatives} (at most %d documentation artifacts) "
            "and a schema-2 result.verification (verification_schema 2) "
            "whose entries may carry depends_on and check_class. A missing "
            "batch or inventory, an executable path or a source conflict "
            "promotes the session to planning." % LIGHT_BATCH_CAP)
    return " ".join(lines)


# --------------------------------------------------------------------------- #
# Path rules and classification.                                              #
# --------------------------------------------------------------------------- #


def _valid_rel_path(path):
    return (isinstance(path, str) and path != "" and "\x00" not in path
            and "\\" not in path and not path.startswith("/")
            and ".." not in path.split("/") and path.strip() == path)


def is_bytecode_byproduct(path):
    if not isinstance(path, str):
        return False
    return ("__pycache__" in path.split("/")
            or path.endswith(".pyc") or path.endswith(".pyo"))


def classify_path(path, mode=None):
    """`documentation` iff the lowercase extension is a documentation
    extension and (when a mode is known) the file is not executable;
    everything else is `executable` (closed, fails toward promotion)."""
    lowered = str(path).lower()
    if (lowered.endswith(DOC_EXTENSIONS)
            and mode not in ("755", 0o755, "0755")):
        return "documentation"
    return "executable"


def dependency_matches(path, patterns):
    for pattern in patterns:
        if path == pattern:
            return True
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(
                path, pattern):
            return True
    return False


def validate_batch(batch):
    """(normalized, None) or (None, reason). A batch is {artifacts: 1..N
    unique relative paths, derivatives: {artifact: [relative paths]}} with
    every derivative key a listed artifact."""
    if not isinstance(batch, dict):
        return None, "batch_not_object"
    if set(batch) - {"artifacts", "derivatives"}:
        return None, "batch_unknown_keys"
    artifacts = batch.get("artifacts")
    if (not isinstance(artifacts, list)
            or not 1 <= len(artifacts) <= MAX_BATCH_ARTIFACTS):
        return None, "batch_artifacts_invalid"
    if not all(_valid_rel_path(a) for a in artifacts):
        return None, "batch_artifact_path_invalid"
    if len(set(artifacts)) != len(artifacts):
        return None, "batch_artifacts_duplicate"
    derivatives = batch.get("derivatives", {})
    if derivatives is None:
        derivatives = {}
    if not isinstance(derivatives, dict):
        return None, "batch_derivatives_invalid"
    for key, values in derivatives.items():
        if key not in artifacts:
            return None, "batch_derivative_key_unlisted"
        if not isinstance(values, list) or not all(
                _valid_rel_path(v) for v in values):
            return None, "batch_derivative_path_invalid"
    return {"artifacts": list(artifacts),
            "derivatives": {k: list(v) for k, v in derivatives.items()}
            }, None


def _batch_digest(artifacts, derivatives):
    return hashlib.sha256(_canonical({
        "artifacts": sorted(artifacts),
        "derivatives": {k: sorted(v) for k, v in derivatives.items()}
    }).encode("utf-8")).hexdigest()


def _batch_record(source, artifacts, derivatives, cap=None):
    record = {"source": source, "artifacts": list(artifacts),
              "derivatives": {k: list(v) for k, v in derivatives.items()},
              "digest": _batch_digest(artifacts, derivatives)}
    if cap is not None:
        record["cap"] = cap
    return record


def batch_paths(batch):
    """Every path a batch boundary admits: artifacts plus derivatives."""
    if not isinstance(batch, dict):
        return set()
    paths = set(batch.get("artifacts") or ())
    for values in (batch.get("derivatives") or {}).values():
        paths.update(values)
    return paths


def _relativize(path, repo_root):
    if isinstance(path, str) and path.startswith("/") and repo_root:
        root = repo_root.rstrip("/") + "/"
        if path.startswith(root):
            return path[len(root):]
    return path


def resolve_plan_batch(plan_result, repo_root=None):
    """(batch_record, triggers) for an approved plan, per the deterministic
    boundary rule: the plan's result.batch; else the per-file implementation
    list; else an unresolved, fail-closed boundary and signal_malformed."""
    result = plan_result if isinstance(plan_result, dict) else {}
    if result.get("batch") is not None:
        normalized, _reason = validate_batch(result.get("batch"))
        if normalized is None:
            return (_batch_record("plan_unresolved", [], {}),
                    [TRIGGER_SIGNAL_MALFORMED])
        return (_batch_record("plan", normalized["artifacts"],
                              normalized["derivatives"]), [])
    implementation = result.get("implementation")
    pieces = []
    unresolved = not isinstance(implementation, list) or not implementation
    if not unresolved:
        for item in implementation:
            value = item.get("file") if isinstance(item, dict) else None
            if not isinstance(value, str):
                unresolved = True
                continue
            for piece in value.split(", "):
                piece = _relativize(piece.strip(), repo_root)
                if _valid_rel_path(piece):
                    if piece not in pieces:
                        pieces.append(piece)
                else:
                    unresolved = True
    if unresolved or not pieces:
        return (_batch_record("plan_unresolved", pieces, {}),
                [TRIGGER_SIGNAL_MALFORMED])
    return _batch_record("plan_implementation_files", pieces, {}), []


# --------------------------------------------------------------------------- #
# Signal extractors. Each returns reason codes in table order.                #
# --------------------------------------------------------------------------- #


def _ordered(codes):
    wanted = set(codes)
    return [c for c in TRIGGER_ORDER if c in wanted]


def _default_inventory_validator(raw_verification, declared_schema):
    import cowork_verification
    cowork_verification.normalize_inventory(raw_verification, declared_schema)


def _inventory_valid(result, validator):
    raw = result.get("verification")
    if not isinstance(raw, list) or not raw:
        return False
    try:
        (validator or _default_inventory_validator)(
            raw, result.get("verification_schema"))
    except Exception:  # InventoryError and anything else: fail closed.
        return False
    return True


def _source_conflicts_state(value):
    """'none' | 'conflict' | 'malformed'."""
    if value is None:
        return "none"
    if not isinstance(value, list):
        return "malformed"
    if not value:
        return "none"
    for item in value:
        if not (isinstance(item, dict)
                and isinstance(item.get("summary"), str)
                and item["summary"].strip()
                and isinstance(item.get("sources"), list)
                and item["sources"]
                and all(isinstance(s, str) and s.strip()
                        for s in item["sources"])):
            return "malformed"
    return "conflict"


def _risk_class_state(value):
    if value is None:
        return "none"
    return "architectural" if value == "architectural" else "malformed"


def intel_triggers(effective, intel_result, inventory_validator=None):
    """Triggers raised by an approved scout intel. Light needs a declared
    batch plus a valid schema-2 inventory (the intel is the plan); standard
    and assurance validate a batch only when one is present."""
    result = intel_result if isinstance(intel_result, dict) else {}
    codes = set()
    batch_raw = result.get("batch")
    normalized = None
    if batch_raw is not None:
        normalized, _reason = validate_batch(batch_raw)
        if normalized is None:
            codes.add(TRIGGER_SIGNAL_MALFORMED)
    if effective == PROFILE_LIGHT:
        if batch_raw is None or result.get("verification") is None:
            codes.add(TRIGGER_BATCH_UNDECLARED)
        elif result.get("verification") is not None and not _inventory_valid(
                result, inventory_validator):
            codes.add(TRIGGER_SIGNAL_MALFORMED)
        cap = PROFILE_DEFINITIONS[PROFILE_LIGHT]["batch"]["max_artifacts"]
        if normalized is not None and len(normalized["artifacts"]) > cap:
            codes.add(TRIGGER_SCOPE_EXPANSION)
    if normalized is not None:
        for path in normalized["artifacts"]:
            if classify_path(path) == "executable":
                codes.add(TRIGGER_EXECUTABLE_CHANGE)
    state = _source_conflicts_state(result.get("source_conflicts"))
    if state == "conflict":
        codes.add(TRIGGER_SOURCE_CONFLICT)
    elif state == "malformed":
        codes.add(TRIGGER_SIGNAL_MALFORMED)
    risk = _risk_class_state(result.get("risk_class"))
    if risk == "architectural":
        codes.add(TRIGGER_ARCHITECTURAL_RISK)
    elif risk == "malformed":
        codes.add(TRIGGER_SIGNAL_MALFORMED)
    return _ordered(codes)


def plan_triggers(effective, plan_result, repo_root=None):
    _batch, triggers = resolve_plan_batch(plan_result, repo_root)
    return _ordered(triggers)


def _attempt_failed(attempt):
    return (not isinstance(attempt, dict)
            or attempt.get("exit_code") not in (0, None)
            or bool(attempt.get("timed_out")))


def evidence_failed(txn_result, inventory):
    """True when the owned transaction verdict is red, or unverified and not
    deferred (issue #51 deferred-pending is excluded). Exception: a red
    verdict whose only failing attempts belong to lint/format check classes
    with no mutation, ledger failure or startup failure."""
    if not isinstance(txn_result, dict):
        return True
    verdict = txn_result.get("verdict")
    if verdict == "green":
        return False
    stamp = txn_result.get("deferred_reconciliation")
    if verdict == "unverified" and isinstance(stamp, dict) and (
            stamp.get("state") == "pending"):
        return False
    if verdict != "red":
        return True
    if (txn_result.get("mutation") is not None
            or txn_result.get("ledger_failure") is not None
            or txn_result.get("startup_failure") is not None):
        return True
    classes = {}
    for entry in inventory or ():
        if isinstance(entry, dict):
            classes[entry.get("label")] = entry.get("check_class")
    failing = []
    for attempt in txn_result.get("attempts") or ():
        if not isinstance(attempt, dict):
            return True
        if attempt.get("evidence_state") not in (None, "present"):
            return True
        if _attempt_failed(attempt):
            failing.append(attempt.get("label"))
    if not failing:
        return True
    return not all(classes.get(label) in CHECK_CLASSES for label in failing)


def builder_ready_triggers(effective, batch, changed_paths, executable_paths,
                           txn_result, inventory, status_result):
    """Triggers at a builder ready_for_review. `changed_paths` None means the
    change set could not be measured: that is signal_malformed, never a
    silent pass."""
    codes = set()
    if changed_paths is None:
        codes.add(TRIGGER_SIGNAL_MALFORMED)
    else:
        changed = [p for p in changed_paths if not is_bytecode_byproduct(p)]
        if changed:
            if not isinstance(batch, dict):
                codes.add(TRIGGER_SIGNAL_MALFORMED)
            elif set(changed) - batch_paths(batch):
                codes.add(TRIGGER_SCOPE_EXPANSION)
        if executable_paths:
            codes.add(TRIGGER_EXECUTABLE_CHANGE)
    status = status_result if isinstance(status_result, dict) else {}
    state = _source_conflicts_state(status.get("source_conflicts"))
    if state == "conflict":
        codes.add(TRIGGER_SOURCE_CONFLICT)
    elif state == "malformed":
        codes.add(TRIGGER_SIGNAL_MALFORMED)
    if evidence_failed(txn_result, inventory):
        codes.add(TRIGGER_EVIDENCE_FAILED)
    return _ordered(codes)


_BUILD_ROLES = ("builder", "build-reviewer")


def verdict_triggers(reviewer_role, verdict):
    """Triggers raised by one reviewer verdict. material_finding is only for
    the build-reviewer; architectural_risk applies to any reviewer."""
    codes = set()
    findings = verdict.get("corrective_findings") if isinstance(
        verdict, dict) else None
    if not isinstance(findings, list):
        return []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if reviewer_role in _BUILD_ROLES and finding.get("severity") in (
                "blocking", "major"):
            codes.add(TRIGGER_MATERIAL_FINDING)
        risk = _risk_class_state(finding.get("risk_class"))
        if risk == "architectural":
            codes.add(TRIGGER_ARCHITECTURAL_RISK)
        elif risk == "malformed":
            codes.add(TRIGGER_SIGNAL_MALFORMED)
    return _ordered(codes)


def classify_build_verdict(effective, verdict):
    """('approve', notes) | ('reject', profile_rejected_code) | ('pass',
    None). A revise always passes (it reopens the builder)."""
    if not isinstance(verdict, dict) or verdict.get("verdict") != "approve":
        return "pass", None
    findings = verdict.get("corrective_findings")
    if findings not in (None, []):
        return "reject", REJECT_CORRECTIVE_ON_APPROVE
    notes = verdict.get("deferred_minor_notes")
    if notes is None or notes == []:
        return "approve", []
    if not isinstance(notes, list):
        return "reject", REJECT_NOTES_MALFORMED
    if effective == PROFILE_ASSURANCE:
        return "reject", REJECT_NOTES_REFUSED
    cleaned = []
    for note in notes:
        if not (isinstance(note, dict)
                and isinstance(note.get("summary"), str)
                and note["summary"].strip()):
            return "reject", REJECT_NOTES_MALFORMED
        sha = note.get("evidence_sha256")
        if sha is not None and not (isinstance(sha, str)
                                    and _SHA256_RE.match(sha)):
            return "reject", REJECT_NOTES_MALFORMED
        path = note.get("evidence_path")
        if path is not None and not isinstance(path, str):
            return "reject", REJECT_NOTES_MALFORMED
        cleaned.append({"summary": note["summary"],
                        "evidence_path": path, "evidence_sha256": sha})
    return "approve", cleaned


# --------------------------------------------------------------------------- #
# Promotion.                                                                  #
# --------------------------------------------------------------------------- #


def promote(current, reason_codes, requested=None):
    """(effective, matched codes in table order). Effective is the maximum,
    by rank, of `current` and every matched target; nothing ever lowers it."""
    if current not in RANK:
        raise UnknownProfile(current)
    codes = list(reason_codes)
    for code in codes:
        if code not in _TARGET_BY_CODE:
            raise ValueError("unknown promotion reason code %r" % (code,))
    matched = _ordered(codes)
    best = RANK[current]
    for code in matched:
        target = _TARGET_BY_CODE[code]
        if target == _TARGET_NEXT_RANK:
            value = min(RANK[current] + 1, RANK[PROFILE_ASSURANCE])
        elif target == _TARGET_REQUESTED:
            if requested not in RANK:
                raise UnknownProfile(requested)
            value = RANK[requested]
        else:
            value = RANK[target]
        best = max(best, value)
    return PROFILES[best], matched


def apply_promotion(record, reason_codes, seam, now, requested=None):
    """(new_record, changed). Appends one history entry only when the
    effective profile changes; every other field is carried untouched."""
    effective, matched = promote(record["effective"], reason_codes,
                                 requested)
    if effective == record["effective"]:
        return record, False
    updated = dict(record)
    history = list(record.get("promotion_history") or ())
    history.append({"seq": len(history) + 1, "from": record["effective"],
                    "to": effective, "reason_codes": matched,
                    "seam": seam, "at": now})
    updated["effective"] = effective
    updated["promotion_history"] = history
    return updated, True


# --------------------------------------------------------------------------- #
# Invalidation and reuse.                                                     #
# --------------------------------------------------------------------------- #


def _affected_paths(changed_paths, artifact_derivatives):
    affected = set(changed_paths)
    for path in changed_paths:
        affected.update((artifact_derivatives or {}).get(path) or ())
    return affected


def invalidated_evidence(changed_paths, accepted_evidence,
                         artifact_derivatives, executable_changed):
    """Sorted labels whose accepted evidence must be redone: all of them on
    an executable change; otherwise every label with no declared
    dependencies, or whose dependencies match a changed path or a direct
    derivative of a changed artifact."""
    labels = sorted(accepted_evidence or ())
    if executable_changed:
        return labels
    affected = _affected_paths(
        [p for p in changed_paths or () if not is_bytecode_byproduct(p)],
        artifact_derivatives)
    out = []
    for label in labels:
        depends_on = (accepted_evidence[label] or {}).get("depends_on") or []
        if not depends_on or any(
                dependency_matches(p, depends_on) for p in affected):
            out.append(label)
    return out


class ReusePolicy(object):
    """Decides, per inventory entry, whether prior accepted evidence may
    stand. Assurance never gets one (`reuse_policy_for` returns None)."""

    def __init__(self, effective, accepted_evidence, artifact_derivatives):
        self.effective = effective
        self.accepted_evidence = dict(accepted_evidence or {})
        self.artifact_derivatives = dict(artifact_derivatives or {})

    def prior_for(self, entry):
        prior = self.accepted_evidence.get(entry.get("label"))
        return prior if isinstance(prior, dict) else None

    def may_reuse(self, entry, prior_record, changed_paths,
                  executable_changed, current_digest):
        if self.effective == PROFILE_ASSURANCE:
            return False
        if entry.get("kind") == "preflight":
            return False
        depends_on = entry.get("depends_on")
        if not depends_on or current_digest is None:
            return False
        if not isinstance(prior_record, dict):
            return False
        for key in ("label", "command", "execution_mode", "kind",
                    "depends_on"):
            if prior_record.get(key) != entry.get(key):
                return False
        if prior_record.get("dependency_digest") != current_digest:
            return False
        if executable_changed:
            return False
        affected = _affected_paths(
            [p for p in changed_paths or () if not is_bytecode_byproduct(p)],
            self.artifact_derivatives)
        return not any(dependency_matches(p, depends_on) for p in affected)


def reuse_policy_for(record):
    if record.get("effective") == PROFILE_ASSURANCE:
        return None
    return ReusePolicy(
        record["effective"], record.get("accepted_evidence"),
        (record.get("invalidation_graph") or {}).get("artifact_derivatives"))


def diff_manifests(base_files, new_files):
    """(changed_paths, executable_paths) between two manifest `files`
    mappings, compared on (type, sha256, mode, symlink_target). Added and
    removed paths count; bytecode byproducts are dropped from both outputs."""
    base_files = base_files or {}
    new_files = new_files or {}

    def ident(entry):
        if not isinstance(entry, dict):
            return None
        return (entry.get("type"), entry.get("sha256"), entry.get("mode"),
                entry.get("symlink_target"))

    changed = []
    executable = []
    for path in sorted(set(base_files) | set(new_files)):
        if is_bytecode_byproduct(path):
            continue
        before = base_files.get(path)
        after = new_files.get(path)
        if ident(before) == ident(after):
            continue
        changed.append(path)
        modes = [e.get("mode") if isinstance(e, dict) else None
                 for e in (before, after)]
        types = [e.get("type") if isinstance(e, dict) else None
                 for e in (before, after)]
        if "symlink" in types or any(
                classify_path(path, m) == "executable" for m in modes):
            executable.append(path)
    return changed, executable


# --------------------------------------------------------------------------- #
# Record.                                                                     #
# --------------------------------------------------------------------------- #

_RECORD_KEYS = ("schema", "policy_version", "policy_digest", "selected",
                "effective", "rationale", "plan_source", "batch",
                "accepted_evidence", "invalidation_graph", "last_invalidated",
                "deferred_minor_notes", "promotion_history", "counters",
                "building_baseline")
_EVIDENCE_KEYS = ("label", "command", "execution_mode", "kind", "depends_on",
                  "dependency_digest", "source_transaction_id",
                  "manifest_digest")


def new_record(selected, rationale_note, now):
    if selected not in RANK:
        raise UnknownProfile(selected)
    return {
        "schema": RECORD_SCHEMA, "policy_version": POLICY_VERSION,
        "policy_digest": policy_digest(), "selected": selected,
        "effective": selected,
        "rationale": {"selected_by": "cli", "note": rationale_note,
                      "at": now},
        "plan_source": PLAN_SOURCE_PLAN, "batch": None,
        "accepted_evidence": {},
        "invalidation_graph": {"artifact_derivatives": {},
                               "entry_dependencies": {}},
        "last_invalidated": [], "deferred_minor_notes": [],
        "promotion_history": [],
        "counters": {"verification_executed": 0, "verification_reused": 0},
        "building_baseline": None,
    }


def binding_for(record):
    return {"selected": record["selected"],
            "policy_version": record["policy_version"],
            "policy_digest": record["policy_digest"]}


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _shape_ok(raw):
    if set(raw) != set(_RECORD_KEYS):
        return False
    if raw["schema"] != RECORD_SCHEMA:
        return False
    if not (isinstance(raw["policy_version"], int)
            and isinstance(raw["policy_digest"], str)):
        return False
    if raw["selected"] not in RANK or raw["effective"] not in RANK:
        return False
    rationale = raw["rationale"]
    if not (isinstance(rationale, dict)
            and isinstance(rationale.get("selected_by"), str)
            and isinstance(rationale.get("note"), (str, type(None)))):
        return False
    if raw["plan_source"] not in (PLAN_SOURCE_PLAN, PLAN_SOURCE_INTEL):
        return False
    batch = raw["batch"]
    if batch is not None:
        if not (isinstance(batch, dict)
                and isinstance(batch.get("source"), str)
                and _is_str_list(batch.get("artifacts"))
                and isinstance(batch.get("derivatives"), dict)
                and all(_is_str_list(v)
                        for v in batch["derivatives"].values())
                and isinstance(batch.get("digest"), str)):
            return False
    evidence = raw["accepted_evidence"]
    if not isinstance(evidence, dict):
        return False
    for label, entry in evidence.items():
        if not (isinstance(entry, dict)
                and set(entry) == set(_EVIDENCE_KEYS)
                and entry.get("label") == label
                and isinstance(entry.get("command"), list)
                and _is_str_list(entry.get("depends_on"))
                and isinstance(entry.get("source_transaction_id"), str)):
            return False
    graph = raw["invalidation_graph"]
    if not (isinstance(graph, dict)
            and set(graph) == {"artifact_derivatives", "entry_dependencies"}
            and isinstance(graph["artifact_derivatives"], dict)
            and all(_is_str_list(v)
                    for v in graph["artifact_derivatives"].values())
            and isinstance(graph["entry_dependencies"], dict)
            and all(_is_str_list(v)
                    for v in graph["entry_dependencies"].values())):
        return False
    if not _is_str_list(raw["last_invalidated"]):
        return False
    notes = raw["deferred_minor_notes"]
    if not (isinstance(notes, list)
            and all(isinstance(n, dict) and isinstance(n.get("summary"), str)
                    for n in notes)):
        return False
    history = raw["promotion_history"]
    if not (isinstance(history, list) and all(
            isinstance(h, dict)
            and {"seq", "from", "to", "reason_codes", "seam", "at"}
            <= set(h) for h in history)):
        return False
    counters = raw["counters"]
    if not (isinstance(counters, dict)
            and set(counters) == {"verification_executed",
                                  "verification_reused"}
            and all(isinstance(v, int) and not isinstance(v, bool)
                    for v in counters.values())):
        return False
    baseline = raw["building_baseline"]
    if baseline is not None and not (
            isinstance(baseline, dict)
            and isinstance(baseline.get("building_epoch"), int)
            and isinstance(baseline.get("manifest_fingerprint"), str)):
        return False
    return True


def _history_ok(raw):
    previous = raw["selected"]
    for index, entry in enumerate(raw["promotion_history"], start=1):
        if (entry.get("seq") != index or entry.get("from") != previous
                or entry.get("to") not in RANK
                or RANK[entry["to"]] <= RANK[previous]
                or not _is_str_list(entry.get("reason_codes"))):
            return False
        previous = entry["to"]
    return raw["effective"] == previous


def validate_record(raw, binding):
    """(record, None) when the record is intact, else (None, closed reason
    code). Never collapses damage into 'absent'."""
    if raw is None:
        return None, "record_missing"
    if not isinstance(raw, dict):
        return None, "record_unparseable"
    if not (isinstance(binding, dict)
            and set(binding) == {"selected", "policy_version",
                                 "policy_digest"}
            and binding.get("selected") in RANK
            and isinstance(binding.get("policy_version"), int)
            and isinstance(binding.get("policy_digest"), str)):
        return None, "binding_mismatch"
    if not _shape_ok(raw):
        return None, "record_schema"
    if (raw["selected"] != binding["selected"]
            or raw["policy_version"] != binding["policy_version"]
            or raw["policy_digest"] != binding["policy_digest"]):
        return None, "binding_mismatch"
    if (raw["policy_version"] != POLICY_VERSION
            or raw["policy_digest"] != policy_digest()):
        return None, "policy_digest_mismatch"
    if RANK[raw["effective"]] < RANK[raw["selected"]]:
        return None, "effective_below_selected"
    if not _history_ok(raw):
        return None, "history_inconsistent"
    return raw, None


def add_deferred_notes(record, notes, review_round, manifest_digest):
    updated = dict(record)
    stored = list(record.get("deferred_minor_notes") or ())
    for note in notes:
        stored.append({"summary": note["summary"],
                       "evidence_path": note.get("evidence_path"),
                       "evidence_sha256": note.get("evidence_sha256"),
                       "review_round": review_round,
                       "manifest_digest": manifest_digest})
    updated["deferred_minor_notes"] = stored
    return updated


def record_transaction(record, txn_result, normalized_inventory):
    """New record after one owned transaction: green executed entries become
    accepted evidence; reused entries keep their record; the dependency graph,
    last invalidated labels and executed/reused counters are refreshed on
    every transaction."""
    updated = dict(record)
    evidence = dict(record.get("accepted_evidence") or {})
    graph = {
        "artifact_derivatives": dict(
            (record.get("invalidation_graph") or {}).get(
                "artifact_derivatives") or {}),
        "entry_dependencies": dict(
            (record.get("invalidation_graph") or {}).get(
                "entry_dependencies") or {})}
    by_label = {e["label"]: e for e in normalized_inventory or ()}
    for label, entry in by_label.items():
        graph["entry_dependencies"][label] = list(
            entry.get("depends_on") or [])
    attempts = [a for a in (txn_result.get("attempts") or ())
                if isinstance(a, dict)]
    reused = [r for r in (txn_result.get("evidence_reuse") or ())
              if isinstance(r, dict)]
    digests = txn_result.get("dependency_digests") or {}
    manifest = (txn_result.get("snapshot") or {}).get("manifest_digest")
    if txn_result.get("verdict") == "green":
        for attempt in attempts:
            label = attempt.get("label")
            entry = by_label.get(label)
            if (entry is None or not entry.get("depends_on")
                    or attempt.get("exit_code") != 0
                    or attempt.get("evidence_state") != "present"
                    or digests.get(label) is None):
                continue
            evidence[label] = {
                "label": label, "command": list(entry["command"]),
                "execution_mode": entry["execution_mode"],
                "kind": entry["kind"],
                "depends_on": list(entry["depends_on"]),
                "dependency_digest": digests[label],
                "source_transaction_id": txn_result.get("transaction_id"),
                "manifest_digest": manifest}
    updated["accepted_evidence"] = evidence
    updated["invalidation_graph"] = graph
    reused_labels = {r.get("label") for r in reused}
    updated["last_invalidated"] = sorted(
        label for label in by_label if label not in reused_labels)
    counters = dict(record.get("counters") or {})
    counters["verification_executed"] = (
        counters.get("verification_executed", 0) + len(attempts))
    counters["verification_reused"] = (
        counters.get("verification_reused", 0) + len(reused))
    updated["counters"] = counters
    return updated


# --------------------------------------------------------------------------- #
# Runtime session handle.                                                     #
# --------------------------------------------------------------------------- #


class ProfileSession(object):
    """Mutable runtime handle over one persisted profile record. Every
    mutation saves before it returns. Hooks never lower the effective
    profile and never touch batch, evidence, graph, plan source or baseline
    during a promotion."""

    def __init__(self, record, save_fn, trace_fn=None, now_fn=None):
        self._record = record
        self._save = save_fn
        self._trace = trace_fn
        self._now = now_fn or utc_now

    # -- reads -------------------------------------------------------------

    @property
    def record(self):
        return self._record

    @property
    def effective(self):
        return self._record["effective"]

    @property
    def selected(self):
        return self._record["selected"]

    @property
    def plan_source(self):
        return self._record["plan_source"]

    def reuse_policy(self):
        return reuse_policy_for(self._record)

    # -- internals ---------------------------------------------------------

    def _emit(self, name, **fields):
        if self._trace is not None:
            self._trace(name, **fields)

    def _commit(self, record):
        self._record = record
        self._save(record)

    def _promote(self, codes, seam, requested=None):
        updated, changed = apply_promotion(
            self._record, codes, seam, self._now(), requested)
        if changed:
            entry = updated["promotion_history"][-1]
            self._commit(updated)
            self._emit("profile.promotion", **{
                "from": entry["from"], "to": entry["to"],
                "reason_codes": entry["reason_codes"], "seam": seam})
        return changed

    # -- screening and verdicts -------------------------------------------

    def screen_verdict(self, role, verdict):
        """Profile-rejected code for a build-reviewer verdict, else None.
        Applies only to the builder phase."""
        if role not in _BUILD_ROLES:
            return None
        action, detail = classify_build_verdict(self.effective, verdict)
        return detail if action == "reject" else None

    def on_verdict(self, role, verdict):
        return self._promote(verdict_triggers(role, verdict), "verdict")

    def on_round_cap(self, role):
        return self._promote(list(ROUND_CAP_TRIGGERS), "round_cap")

    def on_build_approved(self, verdict, review_round, manifest_digest):
        action, notes = classify_build_verdict(self.effective, verdict)
        if action != "approve" or not notes:
            return 0
        self._commit(add_deferred_notes(
            self._record, notes, review_round, manifest_digest))
        self._emit("profile.deferred_notes", count=len(notes))
        return len(notes)

    # -- seams -------------------------------------------------------------

    def on_intel_approved(self, intel_result, inventory_validator=None):
        """Records the declared batch, evaluates the intel triggers and
        returns the next phase: `building` (light, clean) or `planning`."""
        result = intel_result if isinstance(intel_result, dict) else {}
        normalized = None
        if result.get("batch") is not None:
            normalized, _reason = validate_batch(result.get("batch"))
        updated = dict(self._record)
        if normalized is not None:
            cap = PROFILE_DEFINITIONS[self.effective]["batch"][
                "max_artifacts"]
            updated["batch"] = _batch_record(
                PLAN_SOURCE_INTEL, normalized["artifacts"],
                normalized["derivatives"], cap)
            graph = dict(updated["invalidation_graph"])
            graph["artifact_derivatives"] = {
                k: list(v) for k, v in normalized["derivatives"].items()}
            updated["invalidation_graph"] = graph
            self._commit(updated)
        self._promote(intel_triggers(
            self.effective, result, inventory_validator), "intel_approval")
        return "building" if self.effective == PROFILE_LIGHT else "planning"

    def on_plan_approved(self, plan_result, repo_root=None):
        batch, triggers = resolve_plan_batch(plan_result, repo_root)
        updated = dict(self._record)
        updated["batch"] = batch
        graph = dict(updated["invalidation_graph"])
        graph["artifact_derivatives"] = {
            k: list(v) for k, v in batch["derivatives"].items()}
        updated["invalidation_graph"] = graph
        self._commit(updated)
        self._promote(_ordered(triggers), "plan_approval")

    def set_plan_source(self, source):
        if source not in (PLAN_SOURCE_PLAN, PLAN_SOURCE_INTEL):
            raise ValueError("unknown plan source %r" % (source,))
        if self._record["plan_source"] != source:
            updated = dict(self._record)
            updated["plan_source"] = source
            self._commit(updated)

    def needs_building_baseline(self, epoch):
        baseline = self._record.get("building_baseline")
        return not (isinstance(baseline, dict)
                    and baseline.get("building_epoch") == epoch)

    def set_building_baseline(self, epoch, fingerprint):
        updated = dict(self._record)
        updated["building_baseline"] = {"building_epoch": epoch,
                                        "manifest_fingerprint": fingerprint}
        self._commit(updated)

    def on_transaction(self, txn_result, normalized_inventory):
        updated = record_transaction(
            self._record, txn_result, normalized_inventory)
        self._commit(updated)
        self._emit(
            "profile.evidence",
            executed=len(txn_result.get("attempts") or ()),
            reused=len(txn_result.get("evidence_reuse") or ()),
            invalidated_count=len(updated["last_invalidated"]))

    def on_builder_ready(self, changed_paths, executable_paths, txn_result,
                         inventory, status_result):
        return self._promote(builder_ready_triggers(
            self.effective, self._record.get("batch"), changed_paths,
            executable_paths, txn_result, inventory, status_result),
            "builder_ready")

    def promote_explicit(self, requested):
        if requested not in RANK:
            raise UnknownProfile(requested)
        return self._promote([TRIGGER_EXPLICIT_REQUEST], "invocation",
                             requested)


def utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")
