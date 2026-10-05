#!/usr/bin/env python3
"""Pure helpers for session lineage: replacement-import planning, the source
finding reader, content-addressed staging, cost reconciliation, cohort
comparability and closure linking.

READ-ONLY BY CONSTRUCTION. A source session is opened with plain `json`/`open`
for reading only. Nothing here goes through `state_store.load`/`save`, a ledger
append helper or a measurement builder, so a source's session file, ledger,
measurement and artifacts are byte-identical after any call. The one place a
file is written is the content-addressed pair `stage_content`/`place_content`,
and only inside the explicit `store_dir` a caller hands in. Nothing computes a
session asset location: every path is an argument. No provider, network or
clock is touched.

WHAT EACH HELPER RETURNS. Plain dicts and lists, never formatted text: a later
stage formats the correction packet, delivers it, writes `lineage.json` and the
closure rows, and builds reports. These helpers only decide what is true about
the source.

THE UNRESOLVED-FINDING RULE. The reviewer's LATEST usable verdict decides what
a replacement imports, never the highest round that happens to hold typed
ledger rows. The verdict body comes from the paired reviewer's live verdict file
or, when that is absent or unusable, the newest usable frozen copy. It is bound
to ledger rows by CONTENT and APPEND POSITION, not by round number: the live
loop counter, the lead seat counter and the reviewer seat counter all differ and
the loop counter restarts after a resume. A latest verdict with no typed
findings, or one whose findings were never recorded in the ledger, imports zero
and says why in `unresolved_basis.reason`.

Python 3.9+, stdlib only.
"""

import collections
import hashlib
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_ledger as ledger  # noqa: E402
import cowork_state as state_store  # noqa: E402

LINEAGE_VERSION = 1
RULE_VERSION = 1
UNKNOWN = "unknown"

START_ROLES = ("scout", "planner", "builder")

# The phase being corrected, its lead role and its paired reviewer.
PHASE_LEAD = {
    "scouting": "scout",
    "planning": "planner",
    "building": "builder",
}
PHASE_REVIEWER = {
    "scouting": "scout-reviewer",
    "planning": "planning-advisor",
    "building": "build-reviewer",
}
# Names a frozen verdict file carries. `<seat>-reviewed` files freeze the
# ARTIFACT under review and are never verdicts, so they are not in this set.
VERDICT_LABELS = tuple(sorted(
    set(PHASE_LEAD.values()) | set(PHASE_REVIEWER.values())))
# Basename of the live verdict file per phase; frozen copies keep it as `<base>`.
_VERDICT_BASENAME = {
    "scouting": os.path.basename(state_store.review_path_for("", None)),
    "planning": os.path.basename(state_store.planner_review_path_for("", None)),
    "building": os.path.basename(state_store.build_review_path_for("", None)),
}

LINEAGE_SCHEMA = {
    "version": LINEAGE_VERSION,
    "fields": ("source_session", "replacement_session", "reason", "start_role",
               "imported_artifacts", "unresolved_finding_ids",
               "unresolved_basis", "finding_packet"),
    "imported_artifact_fields": ("role", "logical_path", "source_path",
                                 "sha256", "approval_baseline"),
    "approval_baseline_fields": ("reviewer", "hash", "epoch",
                                 "context_revision"),
    "unresolved_basis_fields": ("phase", "discoverer", "round", "rule_version",
                                "reason", "verdict_source", "verdict_ref"),
    "verdict_ref_fields": ("source", "label", "round", "sha256"),
    "finding_fields": ("source_finding_id", "severity", "criterion", "summary",
                       "evidence_path", "evidence_sha256", "evidence_state",
                       "evidence_pinned", "discoverer", "round", "phase",
                       "source_record_sha256"),
    "finding_packet_fields": ("path", "sha256"),
}

REFUSAL_CODES = ("source_unreadable", "artifact_missing", "baseline_missing",
                 "baseline_malformed", "baseline_stale", "hash_mismatch",
                 "role_unreachable", "unknown_role")

UNRESOLVED_REASONS = ("none", "latest_verdict_findings",
                      "latest_verdict_all_resolved", "latest_verdict_approve",
                      "latest_verdict_no_typed_findings", "no_verdict_evidence",
                      "latest_verdict_unrecorded")
EVIDENCE_STATES = ("verified", "missing", "sha_mismatch")
VERDICT_SOURCES = ("live", "frozen", "none")

COHORT_REFUSAL_CODES = ("recovery_flag_differs", "recovery_reason_differs",
                        "evaluation_policy_differs", "descriptor_missing")

# role -> (reviewer, artifact basenames hashed together, epoch reader)
_APPROVAL_SPECS = {
    "scout": ("scout-reviewer", ("scout.intel.json", "scout.intel.md"),
              state_store.get_scouting_epoch),
    "planner": ("planning-advisor", ("planner.plan.json", "planner.plan.md"),
                state_store.get_planning_epoch),
}
# Approved artifacts each start role needs, in import order.
_REQUIRED_APPROVALS = {
    "scout": (),
    "planner": ("scout",),
    "builder": ("scout", "planner"),
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FROZEN_PREFIX = re.compile(r"^(scouting|planning|building)-r(\d+)-(.+)$")
_FROZEN_TAIL = re.compile(r"^([0-9a-f]{12})-(.+)$")


# --------------------------------------------------------------------------- #
# Hashing and tolerant reads.                                                 #
# --------------------------------------------------------------------------- #


def sha256_file(path):
    """Hex sha256 of a file's bytes, or None when it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, TypeError, ValueError):
        return None


def canonical_sha256(obj):
    """sha256 of the canonical JSON encoding of `obj` (sorted keys, no
    whitespace), so equal data hashes equally whatever its key order."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _read_json(path):
    """Parsed JSON at `path`, or None. Never writes."""
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return None


def load_source_state(session_path):
    """The source session file as a plain dict, or None when it is missing,
    unreadable or not an object. Deliberately not `state_store.load`."""
    data = _read_json(session_path)
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# Lineage record.                                                             #
# --------------------------------------------------------------------------- #


def empty_basis(phase=None, discoverer=None, reason="none"):
    """An `unresolved_basis` carrying no verdict evidence."""
    return {
        "phase": phase, "discoverer": discoverer, "round": None,
        "rule_version": RULE_VERSION, "reason": reason,
        "verdict_source": "none",
        "verdict_ref": {"source": "none", "label": None, "round": None,
                        "sha256": None},
    }


def new_lineage_record(**fields):
    """Validate and return one lineage record shaped by LINEAGE_SCHEMA.

    Unknown fields, a missing source/replacement/reason/start_role, an unknown
    start role or a malformed nested value raise ValueError, so a malformed
    record is never written by a later stage."""
    allowed = LINEAGE_SCHEMA["fields"]
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        raise ValueError("unknown lineage fields: %s" % ", ".join(unknown))
    for name in ("source_session", "replacement_session", "reason",
                 "start_role"):
        if not isinstance(fields.get(name), str) or not fields.get(name):
            raise ValueError("lineage field %s must be a non-empty string"
                             % name)
    if fields["start_role"] not in START_ROLES:
        raise ValueError("unknown start_role %r" % (fields["start_role"],))
    artifacts = fields.get("imported_artifacts") or []
    if not isinstance(artifacts, (list, tuple)):
        raise ValueError("imported_artifacts must be a list")
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("imported_artifacts entries must be objects")
        missing = [k for k in LINEAGE_SCHEMA["imported_artifact_fields"]
                   if k not in item]
        if missing:
            raise ValueError("imported artifact lacks: %s"
                             % ", ".join(missing))
    ids = fields.get("unresolved_finding_ids") or []
    if not isinstance(ids, (list, tuple)) or not all(
            isinstance(i, str) and i for i in ids):
        raise ValueError("unresolved_finding_ids must be a list of ids")
    basis = fields.get("unresolved_basis")
    if basis is None:
        basis = empty_basis()
    if not isinstance(basis, dict):
        raise ValueError("unresolved_basis must be an object")
    missing = [k for k in LINEAGE_SCHEMA["unresolved_basis_fields"]
               if k not in basis]
    if missing:
        raise ValueError("unresolved_basis lacks: %s" % ", ".join(missing))
    packet = fields.get("finding_packet")
    if packet is not None:
        if not isinstance(packet, dict) or any(
                k not in packet for k in LINEAGE_SCHEMA["finding_packet_fields"]):
            raise ValueError("finding_packet must carry path and sha256")
    return {
        "schema": LINEAGE_VERSION,
        "source_session": fields["source_session"],
        "replacement_session": fields["replacement_session"],
        "reason": fields["reason"],
        "start_role": fields["start_role"],
        "imported_artifacts": [dict(a) for a in artifacts],
        "unresolved_finding_ids": list(ids),
        "unresolved_basis": dict(basis),
        "finding_packet": dict(packet) if packet is not None else None,
    }


# --------------------------------------------------------------------------- #
# Import planning.                                                            #
# --------------------------------------------------------------------------- #


def _refusal(start_role, source_session, code, detail):
    return {"ok": False, "start_role": start_role,
            "source_session": source_session, "refusal": code,
            "detail": detail, "imported_artifacts": []}


def _verify_approval(state, artifact_dir, approval_role):
    """Verify one approved artifact set read-only. Returns
    `(entries, None)` or `(None, (code, detail))`.

    An approval is current only when the recorded baseline hash equals the
    composite hash of the artifacts as they are now AND the baseline's epoch is
    the source's current epoch for that phase: a hand-back bumps the epoch
    without touching the hash, so a handed-back artifact is never imported."""
    reviewer, names, epoch_of = _APPROVAL_SPECS[approval_role]
    sessions = state.get("sessions")
    entry = sessions.get(reviewer) if isinstance(sessions, dict) else None
    raw = entry.get("last_approved_baseline") if isinstance(entry, dict) \
        else None
    paths = [os.path.join(artifact_dir, name) for name in names]
    if raw is None:
        if not any(os.path.exists(p) for p in paths):
            return None, ("role_unreachable",
                          "the source never produced approved %s artifacts"
                          % approval_role)
        return None, ("baseline_missing",
                      "no approval baseline recorded for %s" % reviewer)
    if not isinstance(raw, dict) or not (
            isinstance(raw.get("hash"), str) and _HEX64.match(raw["hash"])):
        return None, ("baseline_malformed",
                      "approval baseline for %s is malformed" % reviewer)
    epoch = raw.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return None, ("baseline_malformed",
                      "approval baseline epoch for %s is not an integer"
                      % reviewer)
    for path in paths:
        if not os.path.isfile(path):
            return None, ("artifact_missing",
                          "approved artifact is missing: %s"
                          % os.path.basename(path))
    if state_store.composite_artifact_hash(paths) != raw["hash"]:
        return None, ("hash_mismatch",
                      "artifacts differ from what %s approved" % reviewer)
    if epoch != epoch_of(state):
        return None, ("baseline_stale",
                      "approval for %s predates a phase re-entry" % reviewer)
    baseline = {"reviewer": reviewer, "hash": raw["hash"], "epoch": epoch,
                "context_revision": raw.get("context_revision")}
    entries = []
    for name, path in zip(names, paths):
        entries.append({
            "role": approval_role,
            "logical_path": name,
            "source_path": path,
            "sha256": sha256_file(path),
            "approval_baseline": dict(baseline),
        })
    return entries, None


def plan_import(session_path, artifact_dir, start_role):
    """Plan a replacement session's start at `start_role`, read-only.

    `artifact_dir` is the source session folder that holds the lead artifacts.
    Returns `{ok, start_role, source_session, refusal, detail,
    imported_artifacts}`. Any unknown role, unreadable source, missing or
    malformed baseline, stale epoch, missing artifact or hash mismatch returns a
    closed code from REFUSAL_CODES and plans nothing."""
    if start_role not in START_ROLES:
        return _refusal(start_role, None, "unknown_role",
                        "start role %r is not one of %s"
                        % (start_role, ", ".join(START_ROLES)))
    state = load_source_state(session_path)
    if state is None:
        return _refusal(start_role, None, "source_unreadable",
                        "the source session file cannot be read")
    source_session = state_store.get_session_uuid(state)
    imported = []
    for approval_role in _REQUIRED_APPROVALS[start_role]:
        entries, failure = _verify_approval(state, artifact_dir, approval_role)
        if failure:
            return _refusal(start_role, source_session, failure[0], failure[1])
        imported.extend(entries)
    return {"ok": True, "start_role": start_role,
            "source_session": source_session, "refusal": None, "detail": None,
            "imported_artifacts": imported}


def reachable_start_roles(session_path, artifact_dir):
    """Start roles whose prerequisites verify, in START_ROLES order. Never
    raises: an unverifiable prerequisite drops the later roles."""
    roles = []
    for role in START_ROLES:
        try:
            plan = plan_import(session_path, artifact_dir, role)
        except Exception:  # noqa: BLE001 - reachability is advisory
            break
        if not plan["ok"]:
            break
        roles.append(role)
    return roles


# --------------------------------------------------------------------------- #
# Latest usable verdict.                                                      #
# --------------------------------------------------------------------------- #


def _usable_verdict_body(raw_bytes):
    """The parsed verdict when the bytes are a usable verdict body, else None.

    Usable means: a JSON object whose `verdict` is a valid verdict, and a
    `needs_user` verdict carries a non-empty `user_question` (the same test the
    runtime applies when it reads a verdict, without its in-memory rewrite)."""
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in state_store.VALID_VERDICTS:
        return None
    if verdict == "needs_user" and not str(
            data.get("user_question") or "").strip():
        return None
    return data


def _parse_frozen_name(name):
    """Parse `<phase>-r<digits>-<label>-<sha12>-<base>` into its parts, or None.

    `<label>` is matched longest-first against the closed verdict label set and
    must be followed by `-` plus exactly twelve lowercase hex digits, so a
    `<seat>-reviewed` artifact file never parses as a verdict."""
    m = _FROZEN_PREFIX.match(name or "")
    if not m:
        return None
    phase, counter, rest = m.group(1), int(m.group(2)), m.group(3)
    for label in sorted(VERDICT_LABELS, key=len, reverse=True):
        if not rest.startswith(label + "-"):
            continue
        tail = _FROZEN_TAIL.match(rest[len(label) + 1:])
        if tail:
            return {"phase": phase, "round": counter, "label": label,
                    "digest": tail.group(1), "base": tail.group(2)}
    return None


def _read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (OSError, TypeError, ValueError):
        return None


def _latest_verdict(phase, review_path, evidence_dir):
    """`(body, ref)` for the paired reviewer's latest usable verdict of `phase`.

    Order: the live verdict file, then the newest usable frozen verdict file
    ordered by (mtime, name) descending across the lead and reviewer labels.
    Round numbers are never used to order or compare: the freeze paths use
    different counters. `(None, none-ref)` when neither source is usable."""
    reviewer = PHASE_REVIEWER[phase]
    none_ref = {"source": "none", "label": None, "round": None, "sha256": None}
    raw = _read_bytes(review_path) if review_path else None
    if raw is not None:
        body = _usable_verdict_body(raw)
        if body is not None:
            return body, {"source": "live", "label": reviewer, "round": None,
                          "sha256": hashlib.sha256(raw).hexdigest()}
    if not evidence_dir:
        return None, none_ref
    try:
        names = os.listdir(evidence_dir)
    except OSError:
        return None, none_ref
    wanted = {PHASE_LEAD[phase], reviewer}
    candidates = []
    for name in names:
        parsed = _parse_frozen_name(name)
        if not parsed or parsed["phase"] != phase \
                or parsed["label"] not in wanted \
                or parsed["base"] != _VERDICT_BASENAME[phase]:
            continue
        path = os.path.join(evidence_dir, name)
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            continue
        candidates.append((mtime, name, parsed, path))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    for _mtime, _name, parsed, path in candidates:
        raw = _read_bytes(path)
        body = _usable_verdict_body(raw) if raw is not None else None
        if body is not None:
            return body, {"source": "frozen", "label": parsed["label"],
                          "round": parsed["round"],
                          "sha256": hashlib.sha256(raw).hexdigest()}
    return None, none_ref


# --------------------------------------------------------------------------- #
# Source finding reader.                                                      #
# --------------------------------------------------------------------------- #


def _norm_value(value):
    """One finding field in comparable form: None and an absent key are equal
    (the ledger drops None), an empty string stays distinct."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _normalise_finding(entry):
    return (_norm_value(entry.get("summary")),
            _norm_value(entry.get("severity")),
            _norm_value(entry.get("criterion")))


def _bind_window(rows, typed):
    """The LAST window of consecutive raw `rows` that share one stored round and
    whose normalised multiset equals `typed`'s, or None.

    Rows are the reviewer's raw finding rows of the phase in append order, taken
    before any state filtering so a partly withdrawn round still binds. Binding
    is by content and position only."""
    size = len(typed)
    if size == 0 or len(rows) < size:
        return None
    wanted = collections.Counter(typed)
    for start in range(len(rows) - size, -1, -1):
        window = rows[start:start + size]
        if len({json.dumps(r.get("round"), sort_keys=True, default=str)
                for r in window}) != 1:
            continue
        if collections.Counter(_normalise_finding(r) for r in window) == wanted:
            return window
    return None


def _is_resolved(record, closed_ids):
    state = record.get("state") or "open"
    return (state in ("withdrawn", "superseded", "closed")
            or record.get("closure") == "superseded"
            or record.get("disposition") == "withdrawn"
            or record.get("id") in closed_ids)


def _evidence_fields(record):
    """`(evidence_sha256, evidence_state, evidence_pinned)` for one finding.

    A finding is never dropped for evidence reasons: a missing file or a
    differing digest only changes the state. With no recorded digest the
    observed one is reported and `evidence_pinned` is False."""
    path = record.get("evidence_path")
    recorded = record.get("evidence_sha256")
    observed = sha256_file(path) if isinstance(path, str) and path else None
    if recorded:
        pinned = True
        if observed is None:
            return recorded, "missing", pinned
        if str(recorded).lower() != observed:
            return recorded, "sha_mismatch", pinned
        return recorded, "verified", pinned
    if observed is None:
        return None, "missing", False
    return observed, "verified", False


def _finding_row(record, discoverer, phase):
    sha, state, pinned = _evidence_fields(record)
    return {
        "source_finding_id": record.get("id"),
        "severity": record.get("severity"),
        "criterion": record.get("criterion"),
        "summary": record.get("summary"),
        "evidence_path": record.get("evidence_path"),
        "evidence_sha256": sha,
        "evidence_state": state,
        "evidence_pinned": pinned,
        "discoverer": record.get("discoverer") or discoverer,
        "round": record.get("round"),
        "phase": record.get("phase") or phase,
        "source_record_sha256": canonical_sha256(record),
    }


def _result(findings, basis):
    return {"findings": findings, "unresolved_basis": basis}


def read_unresolved_findings(ledger_path, session_state, review_path=None,
                             evidence_dir=None):
    """The source findings a replacement should start from, read-only.

    `session_state` is the source session as a plain dict; its phase is the one
    being corrected and its paired reviewer's latest usable verdict decides what
    is imported. `review_path` is that reviewer's live verdict file and
    `evidence_dir` the folder of frozen verdict copies; with neither, nothing is
    imported (`no_verdict_evidence`).

    Returns `{findings, unresolved_basis}`. Never writes. `unresolved_basis.
    round` is the LEDGER round of the matched rows (None when nothing matched);
    `verdict_ref.round` is a frozen file's own counter. They are different
    counters and must not be compared. An unknown `reason` is to be treated as
    zero findings imported."""
    if not isinstance(session_state, dict) or (
            "phase" in session_state
            and session_state["phase"] not in state_store.PHASES):
        return _result([], empty_basis())
    phase = state_store.get_phase(session_state)
    reviewer = PHASE_REVIEWER[phase]

    body, ref = _latest_verdict(phase, review_path, evidence_dir)

    def basis(reason, round_value=None):
        out = empty_basis(phase, reviewer, reason)
        out.update({"round": round_value, "verdict_source": ref["source"],
                    "verdict_ref": ref})
        return out

    if body is None:
        return _result([], basis("no_verdict_evidence"))
    if body.get("verdict") == "approve":
        return _result([], basis("latest_verdict_approve"))
    typed_raw = body.get("corrective_findings")
    typed_entries = [f for f in typed_raw if isinstance(f, dict)] \
        if isinstance(typed_raw, list) else []
    if not typed_entries:
        return _result([], basis("latest_verdict_no_typed_findings"))

    records = ledger.read_ledger(ledger_path)
    raw_rows = [r for r in records
                if r.get("kind") == "finding" and not r.get("marker")
                and r.get("id") and r.get("phase") == phase
                and r.get("discoverer") == reviewer]
    window = _bind_window(raw_rows, [_normalise_finding(f)
                                     for f in typed_entries])
    if window is None:
        return _result([], basis("latest_verdict_unrecorded"))

    collapsed = ledger.collapse(records)
    closed_ids = {r.get("closes") for r in records
                  if r.get("kind") == "closure" and not r.get("marker")}
    findings = []
    for row in window:
        current = collapsed.get(row["id"], row)
        if not _is_resolved(current, closed_ids):
            findings.append(_finding_row(current, reviewer, phase))
    reason = "latest_verdict_findings" if findings \
        else "latest_verdict_all_resolved"
    return _result(findings, basis(reason, window[0].get("round")))


# --------------------------------------------------------------------------- #
# Content-addressed store helpers.                                            #
# --------------------------------------------------------------------------- #


def content_address(path):
    """The content address (sha256 hex) of a file's bytes."""
    digest = sha256_file(path)
    if digest is None:
        raise OSError("cannot read %r" % (path,))
    return digest


def stage_content(store_dir, source_path):
    """Copy `source_path` to a unique temporary name inside `store_dir`.

    Returns `(staged_path, sha256)`. The temporary name starts with `.stage-`
    and can never equal a content address, so a crash after this call leaves no
    final file."""
    os.makedirs(store_dir, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".stage-", dir=store_dir)
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as dst, open(source_path, "rb") as src:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    return staged, h.hexdigest()


def place_content(store_dir, staged_path):
    """Move a staged file to `<store_dir>/<sha256>` and return that path.

    Idempotent: when the final file already exists the staged copy is removed
    and the existing path returned. The staged file must live in `store_dir`."""
    if os.path.dirname(os.path.abspath(staged_path)) != os.path.abspath(
            store_dir):
        raise ValueError("staged file is not inside the store directory")
    final = os.path.join(store_dir, content_address(staged_path))
    if os.path.exists(final):
        os.unlink(staged_path)
        return final
    os.replace(staged_path, final)
    return final


# --------------------------------------------------------------------------- #
# Lineage reconciliation.                                                     #
# --------------------------------------------------------------------------- #


def _int_usage(entry):
    """The entry's usage as `{field: int}`, or None when it has none we can
    trust (missing, empty, or marked incomparable)."""
    if entry.get("usage_scope") == "incomparable":
        return None
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return None
    ints = {k: v for k, v in usage.items()
            if isinstance(v, int) and not isinstance(v, bool)}
    return ints or None


def _empty_total():
    return {"turns": 0, "usage": {}, "unknown_count": 0}


def reconcile_lineage(source, replacement):
    """Reconcile a source and a replacement session into one lineage.

    Each argument is a plain dict `{session_uuid, work: {work_id: entry}}`
    (an entry may carry its own `session_uuid`, `usage`, `usage_scope` and
    `repeated_context`). Every `(session_uuid, work_id)` is counted once, first
    occurrence wins, so source cost listed again on the replacement side is not
    re-added. Entries of the source session are `preserved`; the rest are
    `repeated` when flagged `repeated_context`, else `new`. Usage that is
    missing or incomparable is `unknown`, never 0.

    Returns `{preserved, repeated, new, totals, unknown, duplicates_skipped}`.
    Pure: no measurement is built and nothing is written."""
    source = source if isinstance(source, dict) else {}
    replacement = replacement if isinstance(replacement, dict) else {}
    source_uuid = source.get("session_uuid")
    buckets = {"preserved": [], "repeated": [], "new": []}
    totals = {name: _empty_total() for name in buckets}
    unknown = []
    skipped = []
    seen = set()
    for side in (source, replacement):
        work = side.get("work")
        for work_id, entry in (work.items() if isinstance(work, dict) else ()):
            entry = entry if isinstance(entry, dict) else {}
            session_uuid = entry.get("session_uuid") or side.get("session_uuid")
            key = (session_uuid, work_id)
            if key in seen:
                skipped.append({"session_uuid": session_uuid,
                                "work_id": work_id})
                continue
            seen.add(key)
            if side is source or (source_uuid and session_uuid == source_uuid):
                name = "preserved"
            elif entry.get("repeated_context"):
                name = "repeated"
            else:
                name = "new"
            buckets[name].append({"session_uuid": session_uuid,
                                  "work_id": work_id})
            total = totals[name]
            total["turns"] += 1
            usage = _int_usage(entry)
            if usage is None:
                total["unknown_count"] += 1
                unknown.append({
                    "session_uuid": session_uuid, "work_id": work_id,
                    "bucket": name,
                    "reason": "usage_incomparable"
                    if entry.get("usage_scope") == "incomparable"
                    else "usage_missing"})
                continue
            for field, value in usage.items():
                total["usage"][field] = total["usage"].get(field, 0) + value
    for total in totals.values():
        if not total["usage"] and total["unknown_count"]:
            total["usage"] = UNKNOWN
    out = dict(buckets)
    out.update({"totals": totals, "unknown": unknown,
                "duplicates_skipped": skipped})
    return out


# --------------------------------------------------------------------------- #
# Cohort comparability.                                                       #
# --------------------------------------------------------------------------- #


def cohort_comparable(a, b):
    """Whether two cohort descriptors may be compared like for like.

    Each descriptor is `{recovery, recovery_reason, evaluation_policy}`; a
    missing evaluation policy resolves to the default the way the session state
    does. Returns `{comparable, code}` with the first differing field's closed
    code from COHORT_REFUSAL_CODES, never a silent comparison."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return {"comparable": False, "code": "descriptor_missing"}

    def policy(descriptor):
        return state_store.get_evaluation_policy(
            {"evaluation_policy": descriptor.get("evaluation_policy")})

    if bool(a.get("recovery")) != bool(b.get("recovery")):
        return {"comparable": False, "code": "recovery_flag_differs"}
    if a.get("recovery_reason") != b.get("recovery_reason"):
        return {"comparable": False, "code": "recovery_reason_differs"}
    if policy(a) != policy(b):
        return {"comparable": False, "code": "evaluation_policy_differs"}
    return {"comparable": True, "code": None}


# --------------------------------------------------------------------------- #
# Closure link.                                                               #
# --------------------------------------------------------------------------- #


def closure_link(imported, cited, replacement_findings=(),
                 existing_closures=()):
    """Link a replacement's closures to the source findings it imported.

    `imported` is the rows from `read_unresolved_findings`; `cited` is source
    finding ids (strings, or `{source_finding_id, closes}` dicts). Only ids that
    were imported are accepted; anything else is rejected and never recorded,
    even when the string equals a replacement finding id. An accepted link keeps
    the source id verbatim and carries `closes` (a replacement id or None) as a
    separate field. A source id is attributed once: a repeated citation or one
    already in `existing_closures` is `already_attributed` and worth zero.

    Each replacement finding is `new` or `replay` by normalised (summary,
    severity, criterion) against an imported row, never by id equality: ids
    restart per ledger. A replay earns nothing.

    Returns `{links, rejected, replacement, value}`."""
    imported_rows = {row.get("source_finding_id"): row
                     for row in imported or [] if isinstance(row, dict)}
    done = set()
    for item in existing_closures or ():
        sid = item.get("source_finding_id") if isinstance(item, dict) else item
        if isinstance(sid, str):
            done.add(sid)
    links = []
    rejected = []
    for item in cited or ():
        if isinstance(item, dict):
            sid, closes = item.get("source_finding_id"), item.get("closes")
        else:
            sid, closes = item, None
        if not isinstance(sid, str) or sid not in imported_rows:
            rejected.append({"source_finding_id": sid,
                             "reason": "not_in_imported_set"})
            continue
        if sid in done:
            links.append({"source_finding_id": sid, "closes": closes,
                          "status": "already_attributed", "value": 0})
            continue
        done.add(sid)
        links.append({"source_finding_id": sid, "closes": closes,
                      "status": "attributed", "value": 1})
    imported_shapes = {_normalise_finding(row) for row in imported_rows.values()}
    classified = []
    for finding in replacement_findings or ():
        if not isinstance(finding, dict):
            continue
        shape = _normalise_finding(finding)
        classified.append({"id": finding.get("id"),
                           "class": "replay" if shape in imported_shapes
                           else "new"})
    return {
        "links": links,
        "rejected": rejected,
        "replacement": classified,
        "value": {
            "closures": sum(1 for link in links if link["value"]),
            "new_findings": sum(1 for c in classified if c["class"] == "new"),
            "replay_earned": 0,
        },
    }
