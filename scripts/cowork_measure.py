#!/usr/bin/env python3
"""The AUTHORITATIVE measurement record, and the only thing that builds it (D3).

One file is the truth about a run: `measurement.json`. The text report is a
rendering of it, `--report --json` prints it, and `builder.summary.md` derives
its completion account from it. Nothing downstream recomputes anything.

THE HARD BOUNDARY, and why it is drawn here rather than at render time:

    BUILD   — `build_record(session_uuid)` is the ONLY reader of raw sources
              (trace, scores, identities, ledger, controller logs). It writes
              `measurement.json` and stamps `built_from`: the digest and line
              count of every raw source it consumed.

    RENDER  — `cowork_report.render_report(record)` takes the record and
              nothing else.

    CHECK   — `check_provenance(session_uuid, record)` re-hashes the raw sources
              and reports whether they have moved on. It produces NO figure and
              its result is never passed into the renderer.

An earlier draft had `--report` rebuild from raw sources every time. That is not
testable: criterion 5's fixture ships a record that deliberately DISAGREES with
its own trace, and a report that rebuilds would reproduce the divergence and
print the raw value while appearing to pass. Separating build from render is
what makes that fixture decide renderer purity instead of merely re-exercising
recomputation.

PURITY. This module reads the ledger; it never writes to it and never mints an
id. Verification attempts are already identified by `cowork_ledger.reconcile_
attempts` before a build runs (P3), so building or rendering a report ten times
leaves `ledger.jsonl` byte-identical.

HONEST UNKNOWNS. Nothing missing becomes 0 and nothing unknown is ranked. A
figure with no source is `unknown` and its absence is listed in `incomplete[]`
with the reason — which is what lets the two legacy sessions on disk report
cleanly while saying plainly which records they predate.

Python 3.9+, stdlib only.
"""

import datetime
import glob
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_ingest as ingest  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_delta as delta_store  # noqa: E402
import cowork_pricing as pricing  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

SCHEMA_VERSION = 1

# The exclusive cost classes. Every unit of controller cost lands in exactly one
# of them, and whatever will not classify is reported as a NAMED remainder
# rather than hidden inside a total (criterion 1).
COST_CLASSES = ("productive", "review", "evaluation", "verification",
                "recovery", "probe", "in_flight", "failed", "cancelled")

UNKNOWN = "unknown"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


# --------------------------------------------------------------------------- #
# Raw sources.                                                                #
# --------------------------------------------------------------------------- #


def _source_paths(session_uuid):
    return {
        "trace": trace_store.trace_path_for(session_uuid),
        "scores": state_store.scores_path_for(session_uuid),
        "identities": state_store.identities_path_for(session_uuid),
        "ledger": state_store.ledger_path_for(session_uuid),
        "evaluation_queue": state_store.evaluation_queue_path_for(
            session_uuid),
        "children": state_store.children_path_for(session_uuid),
        "actions": state_store.actions_path_for(session_uuid),
    }


def _fingerprint(path):
    """`{sha256, lines, bytes}` for one raw source, or a `missing` marker."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {"state": "missing", "sha256": None, "lines": 0, "bytes": 0}
    return {
        "state": "ok",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "lines": raw.count(b"\n"),
        "bytes": len(raw),
    }


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return out
    return out


def _read_json(path):
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data


# --------------------------------------------------------------------------- #
# The work record: joining a turn's start to its end.                         #
# --------------------------------------------------------------------------- #


def build_work(events):
    """Join every start event to its end on `work_id` (P1).

    A turn with a start and NO end is `in_flight`, and its duration is
    `unknown` — never 0, which would read as instant and free. A turn with an
    end and no start is an ORPHAN and is reported as one rather than being
    quietly promoted to a complete turn.

    Legacy sessions predate `work_id` entirely. Their turns are still counted,
    under synthetic ids, with `work_id_source='synthesized'` so nothing claims
    an identity the trace never had.
    """
    work = {}
    orphans = []
    synthetic = 0
    # Pre-work_id traces have no join at all. The best available reconstruction
    # is positional: within one (role, controller) the next end belongs to the
    # most recent unclosed start. It is a RECONSTRUCTION, not a join, and is
    # labelled `synthesized` so nothing downstream reads it as an identity the
    # trace actually recorded. Without it a legacy session reports every turn
    # twice — once as a classless end and once as a durationless start.
    open_legacy = {}
    for event in events:
        name = event.get("event")
        if name not in ("controller.turn.start", "controller.turn.end",
                        "controller.probe.start", "controller.probe.end",
                        "controller.turn.rejected",
                        "eval.turn.start", "eval.turn.end",
                        "child.work.start", "child.work.end",
                        "child.dispatch.blocked"):
            continue
        work_id = event.get("work_id")
        is_start = name.endswith(".start")
        if not work_id:
            key = (name.split(".")[1], event.get("role"),
                   event.get("controller"))
            if is_start:
                synthetic += 1
                work_id = "legacy-%04d" % synthetic
                open_legacy.setdefault(key, []).append(work_id)
                entry = work.setdefault(work_id, {
                    "work_id": work_id, "work_id_source": "synthesized"})
                _merge_work(entry, event, True)
                entry["work_state"] = "in_flight"
                continue
            pending = open_legacy.get(key) or []
            if pending:
                work_id = pending.pop(0)
            else:
                # An end with no start before it: still counted, still an
                # orphan, never promoted to a complete turn.
                synthetic += 1
                work_id = "legacy-%04d" % synthetic
                work.setdefault(work_id, {
                    "work_id": work_id, "work_id_source": "synthesized"})
            entry = work[work_id]
            _merge_work(entry, event, False)
            entry["work_state"] = "complete"
            continue
        entry = work.setdefault(work_id, {
            "work_id": work_id, "work_id_source": "stamped"})
        _merge_work(entry, event, is_start)
        if name.startswith("child."):
            entry["parent_work_id"] = event.get("parent_work_id")
            entry["work_kind"] = (
                "child_attempt" if name == "child.dispatch.blocked"
                else "child")
            if name == "child.dispatch.blocked":
                entry["work_state"] = "blocked"
                entry["reason"] = event.get("reason")
                entry["duration_ms"] = event.get("duration_ms", 0)
                continue
        if name == "controller.turn.rejected":
            # A rejected turn never began: it has no start to join to, and
            # recording it as an end would leave an orphan in the record. It is
            # its own terminal state.
            entry["work_state"] = "rejected"
            entry["duration_ms"] = event.get("duration_ms", 0)
        elif is_start:
            entry.setdefault("work_state", "in_flight")
        else:
            entry["work_state"] = "complete"

    for entry in work.values():
        if entry.get("work_state") == "in_flight":
            # THE point of the in-flight class: an unfinished turn's cost is
            # real and its duration is not knowable. Both are stated. The class
            # is `in_flight` rather than the purpose it STARTED with — a turn
            # that never finished did not do productive work, and leaving it in
            # `productive` would silently pad that class with turns whose
            # duration is unknown.
            entry["intended_class"] = entry.get("work_class")
            entry["work_class"] = "in_flight"
            entry["duration_ms"] = UNKNOWN
        if (entry.get("started_at") is None and entry.get("ended_at")
                and entry.get("work_state") != "rejected"):
            # A REJECTED turn also has no start — by design, because it never
            # began. Overwriting it as an orphan would report a deliberate
            # terminal state as a defect in the record.
            orphans.append(entry["work_id"])
            entry["work_state"] = "orphan_end"
    return work, orphans


def child_work_from_ledger(records):
    """Build stable child/blocked-attempt work items from append-only records."""
    work = {}
    for record in records or ():
        if not isinstance(record, dict) or not record.get("work_id"):
            continue
        work_id = record["work_id"]
        item = work.setdefault(work_id, {
            "work_id": work_id, "work_id_source": "stamped",
            "work_kind": ("child_attempt"
                          if record.get("state") == "blocked" else "child"),
            "work_class": "productive",
            "parent_work_id": record.get("parent_work_id"),
            "identity": record.get("effective_identity"),
            "requested_identity": record.get("requested_identity"),
            "pinned_input_digest": record.get("pinned_input_digest"),
            "tool_use_id": record.get("tool_use_id"),
            "usage_scope": UNKNOWN,
            "tools": [],
        })
        for field in ("agent_id", "agent_type", "effective_policy",
                      "terminal_source"):
            if record.get(field) is not None:
                item[field] = record[field]
        state = record.get("state")
        if state == "blocked":
            item.update({"work_state": "blocked", "work_class": "productive",
                         "reason": record.get("reason"), "duration_ms": 0})
        elif state == "started":
            item.setdefault("work_state", "in_flight")
            item.setdefault("started_at", record.get("ts"))
        elif state == "ended":
            item["work_state"] = "complete"
            item["ended_at"] = record.get("ts")
            item["duration_ms"] = record.get("duration_ms", UNKNOWN)
            if record.get("usage") is not None:
                item["usage"] = record["usage"]
                item["usage_scope"] = (
                    record.get("usage_scope") or "child_native_sum")
            if record.get("delta") is not None:
                item["delta"] = record["delta"]
        elif state == "tool":
            tool = {
                "name": record.get("tool_name"),
                "tool_use_id": record.get("tool_use_id"),
                "ts": record.get("ts"),
            }
            if tool not in item["tools"]:
                item["tools"].append(tool)
        elif state == "terminal_precedence":
            item["terminal_source"] = record.get("terminal_source")
    for item in work.values():
        item["tool_count"] = len(item.get("tools") or [])
        if item.get("work_state") == "in_flight":
            item["duration_ms"] = UNKNOWN
    return work


def reconcile_guard_records(action_records, events):
    """One record per guard attempt: broker > supervisor > stream fallback."""
    by_id = {}
    for record in action_records or ():
        attempt_id = record.get("guard_attempt_id")
        if attempt_id:
            item = dict(record)
            item["evidence_channel"] = "broker"
            by_id[attempt_id] = item
    for event in events or ():
        attempt_id = event.get("guard_attempt_id")
        if not attempt_id or attempt_id in by_id:
            continue
        if event.get("event") == "guard.broker.unavailable":
            item = dict(event)
            item["evidence_channel"] = "supervisor"
            by_id[attempt_id] = item
        elif event.get("event") == "action.policy.denied_offline":
            item = dict(event)
            item["evidence_channel"] = "stream"
            by_id[attempt_id] = item
    return [by_id[key] for key in sorted(by_id)]


def reconcile_artifact_attribution(work, action_records):
    """Join content deltas to actor-specific PostToolUse path evidence."""
    deltas = {}
    for work_id, item in (work or {}).items():
        if item.get("work_kind") == "child" and isinstance(
                item.get("delta"), dict):
            deltas[work_id] = item["delta"]
    evidence = {}
    for record in action_records or ():
        if (record.get("evidence_kind") != "mutation_effect"
                or not record.get("work_id")):
            continue
        for digest in record.get("path_digests") or ():
            actors = evidence.setdefault(digest, [])
            if record["work_id"] not in actors:
                actors.append(record["work_id"])
    return delta_store.attribute(deltas, evidence)


def nested_contributions(work, artifact_attribution):
    """Build evidence-linked production and coordination contributions."""
    contributions = []
    for path, attribution in sorted((artifact_attribution or {}).items()):
        mode = attribution.get("attribution") or UNKNOWN
        actors = attribution.get("work_ids") or ()
        if not actors:
            contributions.append({
                "work_id": UNKNOWN, "mode": "unattributed",
                "artifact_path": path, "evidence": ["artifact_delta"],
            })
            continue
        for actor in actors:
            contributions.append({
                "work_id": actor,
                "mode": "contested" if mode == "contested" else "produced",
                "artifact_path": path, "evidence": ["artifact_delta"],
            })
    for item in (work or {}).values():
        if item.get("work_kind") != "child" or not item.get("parent_work_id"):
            continue
        evidence = [value for value in (
            item.get("tool_use_id"), item.get("pinned_input_digest"))
            if value]
        contributions.append({
            "work_id": item["parent_work_id"],
            "child_work_id": item.get("work_id"),
            "mode": "coordinated",
            "artifact_path": None,
            "evidence": evidence or [UNKNOWN],
        })
    return contributions


def reconcile_nested(work, provider_totals=None):
    """Exact-once nested usage with explicit per-axis arithmetic basis."""
    provider_totals = provider_totals if isinstance(provider_totals, dict) \
        else {}
    axes = set(provider_totals)
    for item in work.values():
        if isinstance(item.get("usage"), dict):
            axes.update(item["usage"])
    totals = {}
    basis = {}
    comparable = True
    reasons = {}
    if not axes:
        comparable = False
        reasons["overall"] = "provider total unavailable"
    for axis in sorted(axes):
        parent_direct = 0
        child_sum = 0
        known = True
        parent_bases = set()
        for item in work.values():
            usage = item.get("usage")
            if item.get("work_kind") == "child_attempt":
                continue
            if not isinstance(usage, dict) or not isinstance(
                    usage.get(axis), int):
                known = False
                continue
            if item.get("work_kind") == "child":
                child_sum += usage[axis]
            else:
                parent_direct += usage[axis]
                parent_bases.add(item.get("usage_basis"))
        provider = provider_totals.get(axis)
        additive = parent_direct + child_sum
        additive_allowed = bool(parent_bases) and parent_bases.issubset(
            {"parent_message_sum", "parent_direct"})
        inclusive_allowed = bool(parent_bases) and parent_bases.issubset(
            {"parent_message_sum", "parent_inclusive"})
        additive_match = (
            known and additive_allowed and isinstance(provider, int)
            and provider == additive)
        inclusive_match = (
            known and inclusive_allowed and isinstance(provider, int)
            and provider == parent_direct)
        # With no child usage the two arithmetic expressions are identical.
        # Prefer the direct/additive name unless the source explicitly says
        # inclusive; the numeric result remains exact either way.
        if additive_match and not (
                inclusive_match and parent_bases == {"parent_inclusive"}):
            totals[axis] = provider
            basis[axis] = "parent_direct_plus_children"
            reasons[axis] = "provider total equals direct parent plus children"
        elif inclusive_match:
            totals[axis] = provider
            basis[axis] = "parent_inclusive"
            reasons[axis] = "provider total equals evidenced inclusive parent"
        else:
            totals[axis] = UNKNOWN
            basis[axis] = UNKNOWN
            if provider is None:
                reasons[axis] = "provider total unavailable"
            elif not known:
                reasons[axis] = "native usage incomplete"
            else:
                reasons[axis] = "provider arithmetic or parent basis ambiguous"
            comparable = False
    contributions = []
    work_items = []
    for item in work.values():
        if item.get("work_kind") not in ("child", "child_attempt"):
            continue
        work_items.append(dict(item))
        contributions.append({
            "work_id": item.get("work_id"),
            "parent_work_id": item.get("parent_work_id"),
            "kind": item.get("work_kind"),
            "usage": item.get("usage", UNKNOWN),
            "delta": item.get("delta", UNKNOWN),
            "artifact_attribution": item.get(
                "artifact_attribution", UNKNOWN),
            "evidence": [v for v in (item.get("tool_use_id"),
                                     item.get("pinned_input_digest")) if v],
        })
    work_items.sort(key=lambda item: item.get("work_id") or "")
    return {"totals": totals, "basis": basis,
            "comparable": comparable,
            "comparability_reason": reasons,
            "provider_totals": provider_totals or UNKNOWN,
            "contributions": contributions,
            "contribution_count": len(contributions),
            "work_items": work_items}


def _merge_work(entry, event, is_start):
    if is_start:
        entry["started_at"] = event.get("ts")
        entry.setdefault("role", event.get("role"))
        entry.setdefault("phase", event.get("phase"))
        entry.setdefault("round", event.get("round"))
        entry.setdefault("prompt_bytes", event.get("prompt_bytes"))
        entry["work_class"] = event.get("work_class") or entry.get(
            "work_class") or "productive"
    else:
        entry["ended_at"] = event.get("ts")
        entry["result"] = event.get("result")
        entry["error_type"] = event.get("error_type")
        if event.get("duration_ms") is not None:
            entry["duration_ms"] = event.get("duration_ms")
        if event.get("usage") is not None:
            entry["usage"] = event.get("usage")
        if event.get("usage_native") is not None:
            entry["usage_native"] = event.get("usage_native")
        if event.get("provider_usage_total") is not None:
            entry["provider_usage_total"] = event.get(
                "provider_usage_total")
        if event.get("usage_basis") is not None:
            entry["usage_basis"] = event.get("usage_basis")
        # The END's class wins: a turn that started as `productive` and ended
        # cancelled IS cancelled. The purpose it was launched with does not
        # survive the way it actually finished.
        if event.get("work_class"):
            entry["work_class"] = event["work_class"]
    entry["usage_scope"] = event.get("usage_scope") or entry.get(
        "usage_scope") or UNKNOWN
    if event.get("identity"):
        entry.setdefault("identity", event["identity"])
    entry.setdefault("controller", event.get("controller"))
    entry.setdefault("role", event.get("role"))


# --------------------------------------------------------------------------- #
# Cost classification and reconciliation.                                     #
# --------------------------------------------------------------------------- #


def classify_costs(work):
    """Split usage across the exclusive cost classes, with a NAMED remainder.

    Reconciliation is the honesty check: the classes are summed, the
    controllers' own totals are summed independently, and any difference is
    reported as `unreconciled` naming why — never absorbed into a class to make
    the arithmetic look clean.

    A turn whose `usage_scope` is `incomparable` (a Codex cumulative counter
    that moved backwards) contributes to no class and is counted separately. It
    is not zero; it is unmeasurable, and averaging it in as zero would
    understate the run.
    """
    by_class = {name: {"turns": 0, "usage": {}, "duration_ms": 0,
                       "duration_unknown_turns": 0}
                for name in COST_CLASSES}
    incomparable = {"turns": 0, "work_ids": []}
    # The reconciliation basis is every turn's OWN usage, summed across all
    # turns. Codex's `usage_native` is the thread's running total, so summing
    # THAT across turns would count each turn once per subsequent turn — the
    # very error the per-turn delta exists to fix. Native counters are reported
    # for provenance and are never used as an additive total.
    turn_total = {}
    classified_total = {}
    unclassified = {"turns": 0, "work_ids": []}
    native_present = 0

    for work_id, entry in sorted(work.items()):
        work_class = entry.get("work_class")
        usage = entry.get("usage")
        if isinstance(entry.get("usage_native"), dict):
            native_present += 1
        if entry.get("usage_scope") == "incomparable":
            incomparable["turns"] += 1
            incomparable["work_ids"].append(work_id)
            continue
        if isinstance(usage, dict):
            for field, value in usage.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    turn_total[field] = turn_total.get(field, 0) + value
        if work_class not in by_class:
            unclassified["turns"] += 1
            unclassified["work_ids"].append(work_id)
            continue
        bucket = by_class[work_class]
        bucket["turns"] += 1
        duration = entry.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            bucket["duration_ms"] += duration
        else:
            bucket["duration_unknown_turns"] += 1
        if isinstance(usage, dict):
            for field, value in usage.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    bucket["usage"][field] = bucket["usage"].get(field, 0) + value
                    classified_total[field] = classified_total.get(
                        field, 0) + value

    remainder = {}
    for field in set(turn_total) | set(classified_total):
        diff = turn_total.get(field, 0) - classified_total.get(field, 0)
        if diff:
            remainder[field] = diff
    return {
        "by_class": by_class,
        "classified_total": classified_total,
        "turn_total": turn_total,
        "turns_with_native_counters": native_present,
        # WHAT THE RECONCILIATION ACTUALLY PROVES. `classified_total` and
        # `turn_total` are both derived from each turn's own `usage`, so their
        # agreement shows only that classification lost nothing — it is NOT a
        # check against the provider. Saying "reconciles with the controllers'
        # own totals" on the strength of it would be an overclaim, so the basis
        # is named and the independent check is reported separately below.
        "basis": "per-turn usage (classification completeness)",
        "independent_check": _native_cross_check(work),
        # Named, never hidden. A nonzero remainder means some cost a turn
        # actually reported did not land in a class, and that is a fact about
        # the measurement rather than something to round away.
        "unreconciled": remainder,
        "reconciled": not remainder,
        "incomparable": incomparable,
        "unclassified": unclassified,
    }


MILESTONE_PHASES = ("editing", "verification", "repair")


def build_milestones(events):
    """Builder milestones, and the turn spans they partition.

    Append-only and content-free: each is a timestamped marker naming what the
    builder moved on to. The spans between them are what turn "the build cost
    X" into "the build spent X editing and Y re-verifying after a failure".
    """
    out = {"events": [], "by_phase": {p: 0 for p in MILESTONE_PHASES},
           "state": "ok"}
    previous = None
    for event in events:
        if event.get("event") != "role.milestone":
            continue
        phase = event.get("milestone_phase")
        entry = {"phase": phase, "ts": event.get("ts"),
                 "role": event.get("role"), "round": event.get("round")}
        out["events"].append(entry)
        if previous and previous.get("phase") in out["by_phase"]:
            out["by_phase"][previous["phase"]] += 1
        previous = entry
    if not out["events"]:
        out["state"] = UNKNOWN
    return out


def readiness_claims(events):
    """Every `ready_for_review` promotion, and whether it was verified.

    A promotion is VERIFIED only when the manifest digest it was made against
    matches the digest verification actually ran against. Anything else is
    recorded as `unverified` — not rejected, because refusing the promotion
    would break the run, but never counted as a clean readiness either.
    """
    out = {"claims": [], "total": 0, "unverified": 0, "state": "ok"}
    for event in events:
        if event.get("event") != "role.readiness":
            continue
        claimed = event.get("claimed_manifest")
        verified = event.get("verified_manifest")
        state = ("verified" if claimed and verified and claimed == verified
                 else "unverified")
        out["claims"].append({
            "role": event.get("role"), "ts": event.get("ts"),
            "round": event.get("round"), "state": state,
            "reason": event.get("reason"),
        })
        out["total"] += 1
        if state == "unverified":
            out["unverified"] += 1
    if not out["claims"]:
        out["state"] = UNKNOWN
    return out


def verification_claim_summary(claims):
    """Scalar claim rollups stored in the authoritative record.

    The renderer must not count collection members: if it can aggregate, it
    can print a figure that exists nowhere in the record. Keep the list for
    detail and these scalars for every displayed total.
    """
    out = {"self_reported": 0, "contradicted": 0}
    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        state = claim.get("claim_state")
        if state in out:
            out[state] += 1
    return out


def tool_activity(ingest_results, work=None):
    """Content-free tool activity per role AND per cowork turn.

    The turn a call belongs to is decided by JOINING it to the work record's
    turn windows, not by the controller's own request id. A controller request
    is one model round-trip — Claude opens a new one per tool result — so
    grouping by it produced ~one bucket per call and could never show a turn
    that used sixty tools, which is the only reason to aggregate per turn.
    A call outside every window is `(unattributed)` and says so.
    """
    windows = []
    for work_id, entry in (work or {}).items():
        started, ended = entry.get("started_at"), entry.get("ended_at")
        if started and ended:
            windows.append((started, ended, work_id, entry.get("role")))
    windows.sort()

    def _turn_for(call, role):
        stamp = call.get("started_at")
        if not stamp:
            return "(unattributed)"
        for started, ended, work_id, work_role in windows:
            if started <= stamp <= ended and (work_role is None
                                              or work_role == role):
                return work_id
        return "(unattributed)"

    return _tool_activity(ingest_results, _turn_for)


def _tool_activity(ingest_results, turn_for):
    """Content-free tool activity per role: how many calls, of what intent, how
    many touched the same target twice, and how many mutations were never
    restored.

    An unrestored mutation matters beyond its own row: every later attempt in
    that session tested a tree nobody can reconstruct, which is why those
    attempts are refused rather than counted.
    """
    out = {}
    for role, result in (ingest_results or {}).items():
        if not hasattr(result, "tool_activity"):
            continue
        bucket = {"calls": 0, "by_intent": {}, "repeated_targets": 0,
                  "unrestored_mutations": 0, "state": result.state,
                  "by_turn": {}}
        targets = {}
        for call in result.tool_activity:
            bucket["calls"] += 1
            intent = call.get("intent") or "other"
            bucket["by_intent"][intent] = bucket["by_intent"].get(intent, 0) + 1
            # Per TURN as well as per role: "this role used 90 tools" hides a
            # single turn that used 60 of them.
            # `turn_id` only. Falling back to the per-call timestamp produced
            # one bucket per call, which is not an aggregation at all — a call
            # with no turn is grouped as `(unknown)` and says so.
            turn = turn_for(call, role)
            per_turn = bucket["by_turn"].setdefault(
                str(turn), {"calls": 0, "by_intent": {}})
            per_turn["calls"] += 1
            per_turn["by_intent"][intent] = per_turn["by_intent"].get(
                intent, 0) + 1
            identity = call.get("command_identity") or call.get("tool_name")
            if identity:
                targets[identity] = targets.get(identity, 0) + 1
        bucket["repeated_targets"] = sum(1 for n in targets.values() if n > 1)
        bucket["turns"] = len(bucket["by_turn"])
        bucket["busiest_turn_calls"] = max(
            (t["calls"] for t in bucket["by_turn"].values()), default=0)
        bucket["unrestored_mutations"] = sum(
            1 for m in result.mutations
            if isinstance(m, dict) and not m.get("restored"))
        out[role] = bucket
    return out


def environment_recurrences(attempts):
    """The same environment failure hit more than once.

    Two roles independently tripping over one missing dependency is not two
    accidents; it is the same avoidable orchestration cost paid twice, and it
    stays invisible unless failures are classed and counted across roles.
    """
    grouped = {}
    for attempt in attempts or []:
        if attempt.get("failure_class") != "environment_dependency":
            continue
        identity = attempt.get("command_identity") or "(unknown)"
        bucket = grouped.setdefault(identity, {"count": 0, "roles": set()})
        bucket["count"] += 1
        if attempt.get("role"):
            bucket["roles"].add(attempt["role"])
    return [{"command_identity": identity, "count": bucket["count"],
             "roles": sorted(bucket["roles"])}
            for identity, bucket in sorted(grouped.items())
            if bucket["count"] > 1]


def _native_cross_check(work):
    """Compare the derived per-turn usage against the provider's OWN counters.

    This is the only genuinely independent check available, and it is only
    available where a provider reports per-turn natively. Claude does, so its
    turns cross-check exactly. Codex reports a cumulative thread total, which
    cannot be summed across turns without counting each turn once per later
    turn — so those turns are reported as `not_comparable` rather than folded
    into a number that would look like agreement.
    """
    out = {"comparable": {}, "not_comparable_turns": 0, "state": "ok",
           "mismatches": {}}
    for entry in work.values():
        native = entry.get("usage_native")
        usage = entry.get("usage")
        if not isinstance(native, dict) or not isinstance(usage, dict):
            continue
        if entry.get("usage_scope") != "turn_native":
            # A cumulative counter is not an independent per-turn basis.
            out["not_comparable_turns"] += 1
            continue
        for field, value in native.items():
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            bucket = out["comparable"].setdefault(
                field, {"native": 0, "derived": 0})
            bucket["native"] += value
            derived = usage.get(field)
            bucket["derived"] += derived if isinstance(derived, int) else 0
    for field, bucket in out["comparable"].items():
        if bucket["native"] != bucket["derived"]:
            out["mismatches"][field] = bucket["native"] - bucket["derived"]
    if out["mismatches"]:
        out["state"] = "diverged"
    elif not out["comparable"]:
        out["state"] = UNKNOWN
    return out


def duration_by_class(work, user_wait_ms, user_wait_spans=None):
    """Per-class duration, plus user-wait time from its own spans ONLY.

    User-wait is never inferred from gaps between events (P15). A gap is equally
    an ingestion stall, a controller hang or a suspended process; calling it
    human thinking time would be a fabricated figure dressed as a measurement.
    """
    # Start every class UNKNOWN, not 0. A class with no measured turn has no
    # duration; rendering it as `0.0 s` states that it took no time, which is a
    # different and false claim, and it is the one "nothing missing becomes 0"
    # rule the report was breaking on its own output.
    out = {"%s_ms" % name: UNKNOWN for name in COST_CLASSES}
    measured = {name: 0 for name in COST_CLASSES}
    seen = set()
    unknown_turns = 0
    for entry in work.values():
        work_class = entry.get("work_class")
        duration = entry.get("duration_ms")
        if work_class not in COST_CLASSES:
            continue
        seen.add(work_class)
        if isinstance(duration, int) and not isinstance(duration, bool):
            measured[work_class] += duration
        else:
            unknown_turns += 1
    for name in seen:
        out["%s_ms" % name] = measured[name]
    # User-wait comes only from paired spans. With no spans at all the figure is
    # not zero waiting — it is a session that predates the instrumentation, and
    # `incomplete[]` says so.
    out["user_wait_ms"] = user_wait_ms if user_wait_spans else UNKNOWN
    out["turns_with_unknown_duration"] = unknown_turns
    return out


def user_wait_from_spans(events):
    """Sum the paired user.wait spans. Returns `(total_ms, spans, unresolved)`.

    An unpaired start survives only a process kill; it is `unresolved` and
    contributes nothing rather than being closed at an arbitrary time.
    """
    open_spans = {}
    spans = []
    unpaired_ends = []
    total = 0
    for event in events:
        name = event.get("event")
        if name == "user.wait.start":
            open_spans[event.get("work_id")] = event
        elif name == "user.wait.end":
            start = open_spans.pop(event.get("work_id"), None)
            duration = event.get("duration_ms")
            if not isinstance(duration, int) or isinstance(duration, bool):
                continue
            if start is None:
                # An end with no start in this trace. Its duration describes a
                # span we cannot see the beginning of, so counting it would put
                # unverifiable time into the one figure that is supposed to
                # come only from paired spans.
                unpaired_ends.append(event.get("work_id"))
                continue
            total += duration
            spans.append({
                "work_id": event.get("work_id"),
                "reason": event.get("reason"),
                "outcome": event.get("outcome"),
                "duration_ms": duration,
                "paired": True,
            })
    # Both halves of "unresolved": starts that never ended, and ends whose
    # start we never saw. Neither contributes time.
    return total, spans, [w for w in open_spans if w] + unpaired_ends


# --------------------------------------------------------------------------- #
# Input-source attribution.                                                   #
# --------------------------------------------------------------------------- #


def input_sources(events, work):
    """Break input tokens into the sources cowork can actually measure, plus an
    EXPLICIT remainder.

    The upheld CV-006 boundary: neither controller exposes a per-source
    decomposition of its input tokens, so the honest answer is the sources we
    measured, the total the provider reported, and `unattributed_input` for the
    difference. Presenting the measured part as the whole would be a precision
    we do not have.

    Byte figures are measured directly. Token figures are ESTIMATES and are
    labelled as such — bytes are not tokens, and pretending otherwise is the
    invented precision criterion 5 forbids.
    """
    measured = {"role_prompt_bytes": 0, "user_message_bytes": 0,
                "artifact_descriptor_bytes": 0}
    for event in events:
        name = event.get("event")
        if name == "role.prompt.bytes":
            value = event.get("bytes")
            if isinstance(value, int):
                measured["role_prompt_bytes"] += value
        elif name == "controller.turn.start":
            value = event.get("prompt_bytes")
            if isinstance(value, int):
                measured["user_message_bytes"] += value
            for art in event.get("artifacts") or []:
                if isinstance(art, dict):
                    emb = art.get("embedded_bytes")
                    if isinstance(emb, int):
                        measured["artifact_descriptor_bytes"] += emb
    # The provider's OWN decomposition of its input tokens. These axes are
    # measured, not inferred — they are what the controller reported.
    by_axis = {axis: 0 for axis in pricing.AXES}
    attributed_tokens = 0
    unattributed_tokens = 0
    incomparable_turns = 0
    for work_id, entry in work.items():
        if entry.get("usage_scope") == "incomparable":
            incomparable_turns += 1
            continue
        normalized = pricing.normalize_usage(entry.get("usage"))
        for axis, value in normalized["axes"].items():
            by_axis[axis] += value
        turn_input = (normalized["axes"]["input"]
                      + normalized["axes"]["cached_input"]
                      + normalized["axes"]["cache_write"])
        # A turn cowork can tie to a prompt IT built is attributable; a turn
        # with no recorded prompt bytes (a resumed turn replaying cached
        # context, a probe) is not, and is counted as the named remainder
        # rather than folded into the attributed side.
        if isinstance(entry.get("prompt_bytes"), int):
            attributed_tokens += turn_input
        else:
            unattributed_tokens += turn_input
    return {
        # Two axes that must NOT be subtracted from each other. Bytes are what
        # cowork put in a prompt; tokens are what the provider charged for. No
        # byte-to-token conversion is performed anywhere, because any such
        # estimate would be presented as a measurement and is not one (CV-006
        # upheld: the controllers do not expose a real decomposition).
        "measured_bytes": measured,
        "measured_bytes_total": sum(measured.values()),
        "provider_token_axes": by_axis,
        "attributed_input_tokens": attributed_tokens,
        # Named and explicit. Everything the provider counted on turns cowork
        # cannot tie to a prompt it built: system scaffolding, tool schemas,
        # replayed cached context.
        "unattributed_input_tokens": unattributed_tokens,
        "incomparable_turns": incomparable_turns,
        "note": ("Bytes and tokens are separate axes and are never converted "
                 "into one another. `unattributed_input_tokens` is the input "
                 "the provider counted on turns with no cowork-built prompt "
                 "recorded; it is reported, not closed."),
    }


# --------------------------------------------------------------------------- #
# Findings, scores and cohorts.                                               #
# --------------------------------------------------------------------------- #


def finding_lifecycle(records):
    """Count findings by their FINAL state across the whole history.

    A withdrawn finding is counted as withdrawn, not erased — the retraction is
    itself evidence about the reviewer. An approving round contributes ZERO
    corrective findings, which is the CV-030 fix: counting the raw length of a
    `findings` array made an approval's summary prose look like corrections.
    """
    collapsed = ledger.collapse(records)
    out = {"confirmed": 0, "withdrawn": 0, "superseded": 0, "open": 0,
           "by_severity": {}, "total": 0}
    for record in collapsed.values():
        if record.get("kind") != "finding":
            continue
        out["total"] += 1
        state = record.get("state") or "open"
        if state == "withdrawn":
            out["withdrawn"] += 1
        elif state == "superseded":
            out["superseded"] += 1
        elif record.get("disposition") == "confirmed":
            out["confirmed"] += 1
        else:
            out["open"] += 1
        severity = record.get("severity") or UNKNOWN
        out["by_severity"][severity] = out["by_severity"].get(severity, 0) + 1
    return out


# What a finding's disposition says about the review effort that produced it.
FINDING_DISPOSITIONS = ("confirmed", "withdrawn", "duplicate",
                        "context_missing")


def marginal_cost_per_finding(work, records):
    """What one finding of each disposition actually cost.

    The BASIS is stated rather than assumed: review-class turns are what
    produced findings, so their cost divided by the findings they produced is
    the per-finding cost. That is an average over a phase, not an attribution of
    specific tokens to a specific finding — cowork cannot see which tokens
    produced which finding, and pretending otherwise would be invented
    precision.

    A disposition with no findings yields `unknown`, never 0: a review that
    found no duplicates did not produce duplicates for free.
    """
    # Cost is grouped BY ROUND, because that is the only link between a review
    # turn and the findings it produced. An earlier version divided one pooled
    # total by `count/total` per disposition, which reduces algebraically to
    # `total/total_findings` — every disposition got an identical figure, so a
    # function named per-disposition could not vary by disposition at all.
    cost_by_round = {}
    review_turns = 0
    for entry in work.values():
        if entry.get("work_class") not in ("review", "evaluation"):
            continue
        review_turns += 1
        key = (entry.get("phase"), entry.get("round"))
        bucket = cost_by_round.setdefault(key, {"usage": {}, "duration_ms": 0})
        duration = entry.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool):
            bucket["duration_ms"] += duration
        usage = entry.get("usage")
        if isinstance(usage, dict):
            for field, value in usage.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    bucket["usage"][field] = bucket["usage"].get(
                        field, 0) + value

    findings_by_round = {}
    counts = {d: 0 for d in FINDING_DISPOSITIONS}
    collapsed = ledger.collapse(records)
    for record in collapsed.values():
        if record.get("kind") != "finding":
            continue
        state = record.get("state")
        disposition = ("withdrawn" if state == "withdrawn"
                       else record.get("disposition") or "confirmed")
        if disposition not in counts:
            continue
        counts[disposition] += 1
        key = (record.get("phase"), record.get("round"))
        findings_by_round.setdefault(key, {}).setdefault(disposition, 0)
        findings_by_round[key][disposition] += 1

    # Within each round, that round's review cost is split across the findings
    # that round produced. A disposition's cost is the sum of its shares across
    # rounds — so a disposition concentrated in expensive rounds costs more than
    # one concentrated in cheap rounds, which is the whole point.
    attributed = {d: {"usage": {}, "duration_ms": 0, "linked": 0}
                  for d in FINDING_DISPOSITIONS}
    unlinked = {d: 0 for d in FINDING_DISPOSITIONS}
    for key, dispositions in findings_by_round.items():
        cost = cost_by_round.get(key)
        round_total = sum(dispositions.values())
        if not cost or not round_total:
            for disposition, count in dispositions.items():
                unlinked[disposition] += count
            continue
        for disposition, count in dispositions.items():
            share = count / round_total
            target = attributed[disposition]
            target["linked"] += count
            target["duration_ms"] += cost["duration_ms"] * share
            for field, value in cost["usage"].items():
                target["usage"][field] = target["usage"].get(
                    field, 0) + value * share

    out = {
        "basis": ("each ROUND's review + evaluation cost is split across the "
                  "findings that round produced, and a disposition's cost is "
                  "the sum of its per-round shares. An average within a round, "
                  "NOT an attribution of specific tokens to a specific finding "
                  "- cowork cannot see which tokens produced which finding."),
        "review_turns": review_turns,
        "rounds_with_cost": len(cost_by_round),
        "findings_by_disposition": counts,
        "per_disposition": {},
    }
    for disposition, count in counts.items():
        bucket = attributed[disposition]
        if not count:
            out["per_disposition"][disposition] = {
                "count": 0, "usage": UNKNOWN, "duration_ms": UNKNOWN,
                "reason": "no findings with this disposition"}
            continue
        if not bucket["linked"]:
            out["per_disposition"][disposition] = {
                "count": count, "usage": UNKNOWN, "duration_ms": UNKNOWN,
                "unlinked_findings": unlinked[disposition],
                "reason": ("no review-class cost recorded for the round(s) "
                           "these findings came from")}
            continue
        per = bucket["linked"]
        out["per_disposition"][disposition] = {
            "count": count,
            "linked_findings": bucket["linked"],
            "unlinked_findings": unlinked[disposition],
            "usage": {field: round(value / per, 2)
                      for field, value in bucket["usage"].items()} or UNKNOWN,
            "duration_ms": (round(bucket["duration_ms"] / per)
                            if bucket["duration_ms"] else UNKNOWN),
        }
    return out


def outcome_adjusted_calibration(scores, records):
    """How well contemporaneous scores predicted what the findings turned out
    to be worth.

    CONTEMPORANEOUS SCORES ARE NEVER OVERWRITTEN. The adjustment is reported
    ALONGSIDE the original average as its own field, because a score is a record
    of what an evaluator believed at the time; rewriting it with hindsight
    destroys the only evidence of how good that judgement was, which is the
    thing being measured.
    """
    collapsed = ledger.collapse(records)
    by_round = {}
    for record in collapsed.values():
        if record.get("kind") != "finding":
            continue
        key = (record.get("phase"), record.get("round"))
        bucket = by_round.setdefault(key, {"confirmed": 0, "withdrawn": 0})
        if record.get("state") == "withdrawn":
            bucket["withdrawn"] += 1
        else:
            bucket["confirmed"] += 1
    entries = (scores or {}).get("evaluations")
    entries = entries if isinstance(entries, list) else []
    out = {"rounds": [], "state": "ok",
           "note": ("contemporaneous scores are preserved verbatim; the "
                    "adjustment is reported beside them, never in place of "
                    "them")}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        numeric = [c.get("score") for c in entry.get("criteria") or []
                   if isinstance(c, dict)
                   and state_store.is_numeric_score(c.get("score"))]
        if not numeric:
            continue
        outcome = by_round.get((entry.get("phase"), entry.get("round")))
        contemporaneous = sum(numeric) / len(numeric)
        row = {"phase": entry.get("phase"), "round": entry.get("round"),
               "evaluatee": entry.get("evaluatee"),
               "contemporaneous_average": round(contemporaneous, 2)}
        if not outcome or not (outcome["confirmed"] + outcome["withdrawn"]):
            row["outcome_adjusted"] = UNKNOWN
            row["reason"] = "no finding outcomes recorded for this round"
        else:
            total = outcome["confirmed"] + outcome["withdrawn"]
            row["confirmed"] = outcome["confirmed"]
            row["withdrawn"] = outcome["withdrawn"]
            row["precision"] = round(outcome["confirmed"] / total, 2)
            row["outcome_adjusted"] = round(
                contemporaneous * (outcome["confirmed"] / total), 2)
        out["rounds"].append(row)
    if not out["rounds"]:
        out["state"] = UNKNOWN
    return out


def score_cohorts(scores):
    """Group scores into HOMOGENEOUS cohorts instead of one pooled average.

    A single pooled average over every evaluation mixes phases, role pairs,
    directions, criteria, models and verdicts — so a difference between two
    controllers is indistinguishable from a difference in what they happened to
    be asked. Cohorts keyed on all six make a comparison mean something.

    Non-numeric scores (`not_applicable`, `insufficient_evidence`) are counted
    in their own buckets and excluded from every average. They are real values;
    they are just not numbers, and averaging them as zero would punish an
    evaluator for being honest about what it could not judge.
    """
    entries = (scores or {}).get("evaluations")
    entries = entries if isinstance(entries, list) else []
    cohorts = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for crit in entry.get("criteria") or []:
            if not isinstance(crit, dict):
                continue
            key = "|".join(str(entry.get(field) or UNKNOWN) for field in (
                "phase", "evaluator", "evaluatee", "evaluatee_tool",
                "evaluatee_model", "reviewed_verdict")) + "|" + str(
                crit.get("name") or UNKNOWN)
            bucket = cohorts.setdefault(key, {
                "phase": entry.get("phase"),
                "evaluator": entry.get("evaluator"),
                "evaluatee": entry.get("evaluatee"),
                "evaluatee_tool": entry.get("evaluatee_tool"),
                "evaluatee_model": entry.get("evaluatee_model"),
                "verdict": entry.get("reviewed_verdict"),
                "criterion": crit.get("name"),
                "total": 0, "count": 0,
                "not_applicable": 0, "insufficient_evidence": 0,
                "unverifiable": 0,
            })
            if entry.get("verification_state") == "changed":
                bucket["unverifiable"] += 1
                continue
            score = crit.get("score")
            if state_store.is_numeric_score(score):
                bucket["total"] += score
                bucket["count"] += 1
            elif score in ("not_applicable", "insufficient_evidence"):
                bucket[score] += 1
    for bucket in cohorts.values():
        # `average` stays UNKNOWN when a cohort has no numeric score. It is not
        # 0, and it is not ranked.
        bucket["average"] = (bucket["total"] / bucket["count"]
                             if bucket["count"] else UNKNOWN)
    return cohorts


def enhancement_digest(scores):
    """Deduplicate enhancement suggestions and count their recurrence.

    The same suggestion made in eight rounds is one idea that was ignored seven
    times, not eight ideas. Collapsing them with a recurrence count is what
    makes that visible.
    """
    entries = (scores or {}).get("evaluations")
    entries = entries if isinstance(entries, list) else []
    digest = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = (entry.get("enhancement_suggestions") or "").strip()
        if not text:
            continue
        key = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:16]
        bucket = digest.setdefault(key, {
            "digest": key, "recurrences": 0, "sources": [],
            "bytes": len(text.encode("utf-8")), "disposition": UNKNOWN})
        bucket["recurrences"] += 1
        source = "%s/%s" % (entry.get("evaluator") or UNKNOWN,
                            entry.get("phase") or UNKNOWN)
        if source not in bucket["sources"]:
            bucket["sources"].append(source)
    return digest


# --------------------------------------------------------------------------- #
# Owned verification transactions: authoritative when present, legacy        #
# controller-log-derived attempts remain the fallback for sessions with none. #
# --------------------------------------------------------------------------- #


def _owned_transaction_result_paths(session_uuid):
    """Every `result.json` an owned transaction wrote for this session, oldest
    first (by `finished_at`, falling back to path sort when that is absent).
    Discovery is a directory glob (mirroring `cowork_state`'s own approach)
    rather than an index file, since a transaction's home directory is fully
    deterministic from `(session_uuid, transaction_id)` alone."""
    root = state_store.verification_root_for(session_uuid)
    pattern = os.path.join(root, "transactions", "*", "result.json")
    try:
        paths = sorted(glob.glob(pattern))
    except OSError:
        return []
    def _key(path):
        doc = _read_json(path) or {}
        return doc.get("finished_at") or ""
    return sorted(paths, key=_key)


def owned_transactions_for(session_uuid):
    """Every owned `TransactionResult` persisted for this session, oldest
    first. `[]` when the session has none (a legacy session, or one that
    never reached the builder-readiness gate) — the caller's fallback to the
    controller-log-derived path is unconditional on this being empty."""
    if not session_uuid:
        return []
    out = []
    for path in _owned_transaction_result_paths(session_uuid):
        doc = _read_json(path)
        if isinstance(doc, dict):
            out.append(doc)
    return out


def latest_owned_transaction(session_uuid):
    """The most recently finished owned transaction for this session, or
    None. This is what a builder-readiness gate's decision was actually based
    on — later transactions (e.g. a subsequent green run after a red one)
    supersede earlier ones for "what does verification say about this
    session" purposes, while `owned_transactions_for` keeps every one for
    cost/attribution accounting."""
    transactions = owned_transactions_for(session_uuid)
    return transactions[-1] if transactions else None


# `kind`s that are the INITIAL inventory (baseline/preflight/final_suite, plus
# the whole-plan legacy_required bucket) — never misclassified as a
# reviewer-triggered focused check just because they happen to carry
# measurement metadata. Only `kind=focused` uses the focused-check contract
# (invalidation_reason/reuse_decision/triggering_finding/marginal_cost).
_INITIAL_INVENTORY_KINDS = (
    verification.KIND_BASELINE, verification.KIND_PREFLIGHT,
    verification.KIND_FINAL_SUITE, verification.KIND_LEGACY_REQUIRED)


def owned_transaction_cost_summary(result):
    """Subprocess duration/resource cost for one owned transaction, kept
    STRICTLY SEPARATE from model-inference cost (`cost.by_class` above): this
    is wall-clock time the worker's spawned commands spent, not anything a
    controller billed tokens for. One transaction — however many commands its
    inventory names after deduplication — counts as exactly ONE orchestration
    work item, per the plan's "19 command identities, one work item, one
    final suite" invariant.

    Returns a dict with `work_items=1`, per-kind counts, total/attempt wall
    time, final-suite identity/binding, mutation/cleanup outcome, and
    evidence retry/expiry counts. `None` when `result` is not a usable
    TransactionResult.
    """
    if not isinstance(result, dict):
        return None
    attempts = result.get("attempts") or []
    focused = []
    initial = []
    total_wall_s = 0.0
    evidence_unresolved = 0
    evidence_absent = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        wall = attempt.get("wall_time_s")
        if isinstance(wall, (int, float)):
            total_wall_s += wall
        state = attempt.get("evidence_state")
        if state == verification.EVIDENCE_UNRESOLVED:
            evidence_unresolved += 1
        elif state == verification.EVIDENCE_ABSENT:
            evidence_absent += 1
        kind = attempt.get("kind")
        if kind == verification.KIND_FOCUSED:
            focused.append(attempt)
        elif kind in _INITIAL_INVENTORY_KINDS or kind is None:
            initial.append(attempt)
        else:
            initial.append(attempt)
    mutation = result.get("mutation")
    return {
        "transaction_id": result.get("transaction_id"),
        "request_key": result.get("request_key"),
        "verdict": result.get("verdict"),
        "work_items": 1,
        "attempt_count": len(attempts),
        "initial_attempt_count": len(initial),
        "focused_attempt_count": len(focused),
        "final_suite_label": result.get("final_suite_label"),
        "final_suite_binding": result.get("final_suite_binding"),
        "subprocess_wall_time_s": total_wall_s,
        "mutation_detected": mutation is not None,
        "mutation": mutation,
        "worker_identity_verified": bool(
            result.get("worker_identity_verified")),
        "reused_lock_result": bool(result.get("reused_lock_result")),
        "evidence_unresolved_count": evidence_unresolved,
        "evidence_absent_count": evidence_absent,
        "snapshot": result.get("snapshot"),
        "created_at": result.get("created_at"),
        "finished_at": result.get("finished_at"),
    }


def owned_focused_check_attribution(result):
    """Focused-check (`kind=focused`) value attribution for one owned
    transaction: invalidation reason, reuse decision, triggering finding, and
    marginal cost per attempt — carried straight from the plan's inventory
    metadata onto the corresponding attempt, so a focused check's cost can be
    tied to the reviewer finding that triggered it WITHOUT double counting it
    against the initial baseline/preflight/final_suite inventory (which never
    populates these fields; see `_INITIAL_INVENTORY_KINDS`).

    Returns a list of `{label, invalidation_reason, reuse_decision,
    triggering_finding, marginal_cost, exit_code, wall_time_s}` dicts, one per
    `kind=focused` attempt. `[]` when `result` carries no focused attempts
    (an initial-only inventory, or a legacy/schema-1 transaction whose kind is
    always `legacy_required`)."""
    if not isinstance(result, dict):
        return []
    out = []
    for attempt in result.get("attempts") or []:
        if not isinstance(attempt, dict) or attempt.get("kind") != \
                verification.KIND_FOCUSED:
            continue
        out.append({
            "label": attempt.get("label"),
            "invalidation_reason": attempt.get("invalidation_reason"),
            "reuse_decision": attempt.get("reuse_decision"),
            "triggering_finding": attempt.get("triggering_finding"),
            "marginal_cost": attempt.get("marginal_cost"),
            "exit_code": attempt.get("exit_code"),
            "evidence_state": attempt.get("evidence_state"),
            "wall_time_s": attempt.get("wall_time_s"),
        })
    return out


# --------------------------------------------------------------------------- #
# Building the record.                                                        #
# --------------------------------------------------------------------------- #


def build_record(session_uuid, cwd=None, ingest_results=None):
    """Build the authoritative record from every raw source. Never raises.

    This is the ONLY function that reads raw sources for measurement purposes.
    It writes exactly one file — `measurement.json` — and mints no ids, so a
    build cannot mutate history.
    """
    paths = _source_paths(session_uuid)
    built_from = {name: _fingerprint(path) for name, path in paths.items()}
    incomplete = []

    events = _read_jsonl(paths["trace"])
    if not events:
        incomplete.append({
            "field": "record.work", "reason": "no trace events found",
            "path": paths["trace"]})
    scores = _read_json(paths["scores"]) or {}
    identities = _read_json(paths["identities"]) or {}
    ledger_records = ledger.read_ledger(paths["ledger"])

    work, orphans = build_work(events)
    child_records = _read_jsonl(paths["children"])
    action_records = _read_jsonl(paths["actions"])
    for work_id, entry in child_work_from_ledger(child_records).items():
        if work_id in work:
            work[work_id].update({k: v for k, v in entry.items()
                                  if v is not None})
        else:
            work[work_id] = entry
    artifact_attribution = reconcile_artifact_attribution(
        work, action_records)
    for work_id, entry in work.items():
        if entry.get("work_kind") not in ("child", "child_attempt"):
            continue
        entry["artifact_attribution"] = {
            path: item for path, item in artifact_attribution.items()
            if work_id in (item.get("work_ids") or ())
        }
    nested_governed = any(event.get("event") == "nested.guard.ready"
                          for event in events)
    if nested_governed:
        provider_totals = {}
        for entry in work.values():
            if entry.get("work_kind") in ("child", "child_attempt"):
                continue
            provider = entry.get("provider_usage_total")
            if not isinstance(provider, dict):
                continue
            for axis, value in provider.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    provider_totals[axis] = provider_totals.get(axis, 0) + value
        nested = reconcile_nested(work, provider_totals or None)
        nested["artifact_attribution"] = artifact_attribution
        nested["contributions"] = nested_contributions(
            work, artifact_attribution)
        nested["contribution_count"] = len(nested["contributions"])
        if not nested.get("comparable"):
            incomplete.append({
                "field": "record.nested.totals",
                "reason": "nested provider arithmetic is absent or ambiguous",
            })
        required_child_fields = (
            "identity", "effective_policy", "duration_ms", "usage",
            "tools", "tool_count", "work_state", "delta", "agent_id",
            "started_at")
        required_identity_fields = (
            "controller", "model", "model_source", "effort",
            "effort_source")
        for work_id, entry in work.items():
            if entry.get("work_kind") != "child":
                continue
            for field in required_child_fields:
                value = entry.get(field, UNKNOWN)
                if value in (None, UNKNOWN):
                    incomplete.append({
                        "field": "record.work[%s].%s" % (work_id, field),
                        "reason": "child telemetry field unavailable",
                    })
            if entry.get("work_state") == "complete":
                for field in ("ended_at", "terminal_source"):
                    if entry.get(field, UNKNOWN) in (None, UNKNOWN):
                        incomplete.append({
                            "field": "record.work[%s].%s" % (work_id, field),
                            "reason": "child terminal field unavailable",
                        })
            identity = entry.get("identity")
            for field in required_identity_fields:
                value = (identity.get(field, UNKNOWN)
                         if isinstance(identity, dict) else UNKNOWN)
                if value in (None, UNKNOWN):
                    incomplete.append({
                        "field": "record.work[%s].identity.%s"
                                 % (work_id, field),
                        "reason": "child identity source unavailable",
                    })
    else:
        nested = {
            "state": UNKNOWN, "totals": UNKNOWN, "basis": UNKNOWN,
            "comparable": False, "contributions": UNKNOWN,
            "contribution_count": UNKNOWN,
            "comparability_reason": UNKNOWN,
            "provider_totals": UNKNOWN,
            "artifact_attribution": UNKNOWN,
            "work_items": UNKNOWN,
        }
        incomplete.append({
            "field": "record.nested",
            "reason": "session predates governed child telemetry; nested "
                      "work and all-in cost are unknown"})
    if any(entry.get("work_id_source") == "synthesized"
           for entry in work.values()):
        incomplete.append({
            "field": "record.work[*].work_id",
            "reason": "this session predates the work record; turn ids are "
                      "synthesized and start/end joins are unavailable"})
    costs = classify_costs(work)
    wait_ms, wait_spans, wait_unresolved = user_wait_from_spans(events)
    if not wait_spans:
        incomplete.append({
            "field": "record.duration.by_class.user_wait_ms",
            "reason": "no user.wait spans recorded; this session predates "
                      "user-wait instrumentation and the figure is NOT "
                      "inferred from event gaps"})

    if ingest_results is None:
        # A session that ships its OWN controller logs (a fixture, or an
        # archived session moved between machines) uses them. Without this the
        # named fixture report reached past its own tree to the real
        # ~/.claude / ~/.codex roots, found nothing, and "passed" with no
        # ingested evidence at all — which is precisely what criterion 3 is
        # supposed to be deciding.
        bundled = os.path.join(state_store.session_assets_dir(session_uuid),
                               "controller_logs")
        claude_root = os.path.join(bundled, "claude")
        codex_root = os.path.join(bundled, "codex")
        ingest_results = ingest.ingest_session(
            identities, cwd=cwd,
            claude_root=claude_root if os.path.isdir(claude_root) else None,
            codex_root=codex_root if os.path.isdir(codex_root) else None)
    ingestion = {}
    for role, result in (ingest_results or {}).items():
        ingestion[role] = result.as_dict() if hasattr(result, "as_dict") else {}
        if not getattr(result, "ok", False):
            incomplete.append({
                "field": "record.tool_activity[%s]" % role,
                "reason": "controller log %s" % getattr(result, "state",
                                                        UNKNOWN)})

    # CLEAN-CHECKOUT REPRODUCIBILITY. `ledger.jsonl` is intentionally
    # gitignored for the checked-in fixtures (it is derived, mutable state,
    # not source truth) — a read-only checkout or one where the ignored file
    # was simply never generated must still reproduce the identical attempts
    # a persisted reconciliation would have. When the ledger already holds
    # attempt records, THOSE are used unchanged (the normal, writable-session
    # path is untouched). Only when it holds none do we materialize PURELY IN
    # MEMORY from the same tracked controller-log observations, via
    # `cowork_ledger.materialize_attempts` — never writing the ignored file
    # ourselves, and using the identical identity rules `reconcile_attempts`
    # would have applied.
    # OWNED transaction attempts are EXCLUDED here, not merely disjoint-by-
    # key: `record["owned_verification"]` (below) reads the authoritative
    # `result.json` for the same data directly, and is the tree that owns
    # it — showing the SAME command under BOTH `verification_attempts` and
    # `owned_verification` would represent one owned command twice, even
    # though the two trees can never disagree with each other about a
    # single command's outcome (`owned_verification` wins for anything
    # owned; this field stays legacy-controller-log-derived only, exactly
    # as its own comment below already promises).
    attempts = [a for a in ledger.active_attempts(ledger_records)
               if not (isinstance(a, dict) and a.get("owned"))]
    if not attempts:
        try:
            # `verification_only=True` (the default) matches exactly what the
            # normal writable-session path feeds `reconcile_attempts` (see
            # cowork.py's own `observations_for(results)` calls) — the whole
            # point of this fallback is to reproduce the identical attempts a
            # persisted reconciliation would have produced, not a wider set.
            observations = ingest.observations_for(ingest_results)
        except Exception:  # noqa: BLE001
            observations = []
        if observations:
            attempts = ledger.materialize_attempts(ledger_records,
                                                    observations)
    if not attempts:
        incomplete.append({
            "field": "record.verification_attempts",
            "reason": "no verification attempts reconciled into the ledger"})

    claims, attempts = join_claims_and_attempts(
        _builder_verification(session_uuid), attempts)
    for attempt in attempts:
        if isinstance(attempt, dict):
            attempt["overlap_count"] = len(attempt.get("overlaps") or [])
    unsupported = [c["label"] for c in claims
                   if c.get("claim_state") != "corroborated" and c.get("label")]
    if unsupported:
        incomplete.append({
            "field": "record.verification[*].claim_state",
            "reason": ("%d verification claim(s) are not corroborated by a "
                       "controller log: %s"
                       % (len(unsupported), ", ".join(unsupported[:4])))})

    queue_pending = []
    try:
        import cowork_eval as evaluation
        queue_pending = evaluation.pending_entries(paths["evaluation_queue"])
    except Exception:  # noqa: BLE001
        queue_pending = []

    # OWNED TRANSACTION EVIDENCE, when present, is authoritative: it is what
    # the builder-readiness gate itself decided on, not a rejoin of controller
    # logs after the fact. `latest_owned_transaction` is None for a legacy
    # session (no transaction ever ran) or one that never reached the
    # builder-readiness gate — the controller-log-derived `verification`/
    # `verification_attempts` above are computed UNCONDITIONALLY either way,
    # so a legacy session's report is unaffected by any of this.
    owned_transactions = owned_transactions_for(session_uuid)
    owned_txn = owned_transactions[-1] if owned_transactions else None
    owned_cost = owned_transaction_cost_summary(owned_txn)
    owned_focused = owned_focused_check_attribution(owned_txn)
    if owned_txn is None:
        incomplete.append({
            "field": "record.owned_verification",
            "reason": "no owned verification transaction was found for "
                      "this session; verification evidence above is "
                      "derived from controller logs (legacy path)"})

    record = {
        "schema_version": SCHEMA_VERSION,
        "session": session_uuid,
        "built_at": _utc_now(),
        # The digest and line count of every raw source consumed, so a later
        # provenance check can say whether they have moved on WITHOUT the report
        # ever recomputing a figure from them.
        "built_from": built_from,
        "work": work,
        "orphan_ends": orphans,
        "cost": costs,
        "nested": nested,
        "contribution": (nested.get("contributions", UNKNOWN)
                         if isinstance(nested, dict) else UNKNOWN),
        "guard_decisions": reconcile_guard_records(action_records, events),
        "duration": {"by_class": duration_by_class(work, wait_ms,
                                                   user_wait_spans=wait_spans),
                     "user_wait_spans": wait_spans,
                     "user_wait_span_count": len(wait_spans),
                     "user_wait_unresolved": wait_unresolved,
                     "user_wait_unresolved_count": len(wait_unresolved)},
        "input_sources": input_sources(events, work),
        "verification_attempts": attempts,
        "verification": claims,
        "verification_summary": verification_claim_summary(claims),
        # Owned-transaction verification (authoritative when present; see the
        # note above `owned_txn`). Kept as a SEPARATE tree from
        # `verification`/`verification_attempts` rather than merged into
        # them, so a legacy session's controller-log-derived fields are never
        # perturbed by this being absent, and so subprocess wall time here is
        # never confused with the model-inference cost in `cost.by_class`.
        "owned_verification": {
            "transactions": owned_transactions,
            "transaction_count": len(owned_transactions),
            "latest": owned_txn,
            "cost": owned_cost,
            "focused_attribution": owned_focused,
        },
        "tool_activity": tool_activity(ingest_results, work),
        "environment_recurrences": environment_recurrences(attempts),
        "findings": finding_lifecycle(ledger_records),
        "marginal_cost": marginal_cost_per_finding(work, ledger_records),
        "calibration": outcome_adjusted_calibration(scores, ledger_records),
        "ledger": ledger.collapse(ledger_records),
        "score_cohorts": score_cohorts(scores),
        "enhancements": enhancement_digest(scores),
        "ingestion": ingestion,
        "identities": identities,
        "pricing": _pricing_view(work),
        "evaluation_queue": {
            "pending": len(queue_pending),
            "pending_entries": [e.get("entry_id") for e in queue_pending],
        },
        "completion": [],
        # Milestones partition a builder turn's cost by what it was actually
        # doing (editing / verification / repair), so "how much did this build
        # spend fixing its own mistakes" is answerable.
        "milestones": build_milestones(events),
        # Readiness that was claimed before its verification finished is
        # recorded as unverified rather than accepted.
        "readiness": readiness_claims(events),
        # The prompt-byte concentration view. It lives in the RECORD rather
        # than being computed at print time, so the renderer can show it while
        # still computing nothing — and so a legacy session, whose trace has
        # little else in it, still reports something useful.
        "trace_summary": _jsonable_summary(summarize_trace(events)),
        # The legacy scores view is computed HERE, at build time, and stored in
        # the record. It used to be computed by the report from raw scores.json
        # after rendering — which meant the report printed figures that appeared
        # nowhere in the authoritative record, exactly the divergence D3 exists
        # to prevent.
        "scores_summary": _jsonable_scores(summarize_scores(scores)),
        "incomplete": incomplete,
    }
    record["replay"] = replay_rounds(record)
    record["completion"] = completion_account(
        record, session_uuid, seeds=_completion_seeds(session_uuid))
    return record


# How a builder's claim stands up against the controller's own log.
CLAIM_STATES = ("corroborated", "self_reported", "contradicted", "ambiguous")

# Claim states that BLOCK a readiness promotion.
#
# The line is between "the log cannot confirm this" and "the log says otherwise".
# `self_reported` (no matching attempt) and `unconfirmable` (the matching
# attempt's producer status was masked by a pipe) are both the former: they are
# recorded and surfaced, and they do not block, because blocking on them while
# tolerating `self_reported` — which is strictly LESS evidence — was incoherent.
#
# `latest_run_disagrees` and `ambiguous` are the latter: the log contradicts the
# claim, or the join cannot say which claim an attempt belongs to. Those block.
# `earlier_failures_retained` does not block either: the failures stay on the
# record, which is what "a failure is never erased" requires, but a green final
# run is still a green final run.
# ONLY POSITIVE CORROBORATION PROMOTES. Anything else — a contradiction, an
# unattributable join, an attempt whose producer status was masked by a pipe, or
# no attempt in the log at all — leaves the claim unproven, and an unproven
# claim does not promote readiness. `self_reported` is the weakest of these: it
# is a claim with no evidence whatsoever, and tolerating it would make the whole
# join decorative.
#
# Old failures are NOT erased by this rule. They stay on the record and in the
# report; they simply do not count as the fresh corroboration a promotion needs.
SUPPORTIVE_CLAIM_STATES = ("corroborated",)


class SourceClock:
    """The independent clock a promotion is judged against.

    `mtime` is the newest modification across the source files. `state` is `ok`
    only when every listed path was readable: a path that has been DELETED is a
    change to the tree that leaves no mtime behind, so skipping it silently
    would let a deletion pass unnoticed — the one mutation an mtime maximum
    cannot see.

    FAILS CLOSED. No paths, no readable clock, or any missing file yields a
    state other than `ok`, and a non-`ok` clock refuses every promotion rather
    than waving it through. An unavailable clock is not evidence of freshness.
    """

    def __init__(self, mtime=None, state="ok", missing=None, counted=0):
        self.mtime = mtime
        self.state = state
        self.missing = missing or []
        self.counted = counted

    @property
    def usable(self):
        return self.state == "ok" and self.mtime is not None

    def as_dict(self):
        return {"mtime": self.mtime, "state": self.state,
                "missing": self.missing[:8], "files_counted": self.counted}


def newest_source_mtime(root, paths):
    """Read the source clock. Returns a `SourceClock`, never a bare number."""
    if paths is None:
        return SourceClock(state="paths_unavailable")
    paths = list(paths)
    if not paths:
        return SourceClock(state="no_sources")
    newest = None
    missing = []
    counted = 0
    for rel in paths:
        try:
            stamp = os.path.getmtime(os.path.join(root, rel) if root else rel)
        except OSError:
            # DELETED or unreadable. Recorded, never skipped.
            missing.append(rel)
            continue
        counted += 1
        if newest is None or stamp > newest:
            newest = stamp
    if missing:
        return SourceClock(newest, "missing_sources", missing, counted)
    if newest is None:
        return SourceClock(state="unreadable")
    return SourceClock(newest, "ok", [], counted)


def blocks_readiness(claim, manifest=None, newest_mtime=None):
    """Whether one claim fails to support a promotion, and why.

    Returns None when the claim supports it, otherwise a short reason. A
    supporting claim must be ALL of:

    - `corroborated` — the log ran this command and agrees;
    - backed by an UNPIPED attempt — a piped producer's status is the
      pipeline's, so it certifies nothing about the command that was claimed;
    - and OBSERVED to have run against the tree being promoted — established
      from the attempt's own start time versus when the sources last changed,
      never from a manifest the claim supplied. Provenance the claim hands us
      is not provenance; it is the claim again.
    """
    if not isinstance(claim, dict):
        return "not a claim"
    state = claim.get("claim_state")
    if state not in SUPPORTIVE_CLAIM_STATES:
        return {
            "self_reported": "no controller-log attempt ran it",
            "ambiguous": "no attempt can be attributed to it specifically",
        }.get(state, "the log does not corroborate it (%s%s)" % (
            state, "/" + claim["contradiction_kind"]
            if claim.get("contradiction_kind") else ""))
    supporting = claim.get("corroborating_attempts") or []
    if not supporting:
        return "corroborated with no attributable attempt"
    fresh = [a for a in supporting if not a.get("pipeline")]
    if not fresh:
        return "every corroborating run was piped, so its exit status is the "\
               "pipeline's and certifies nothing"
    clock = newest_mtime
    if isinstance(clock, (int, float)):
        clock = SourceClock(float(clock))
    if clock is None:
        return "no source clock was supplied, so freshness cannot be decided"
    if not clock.usable:
        # FAIL CLOSED. A clock we cannot read is not permission to promote.
        return ("the source clock is unusable (%s%s), so no run can be shown "
                "to have happened after the tree reached this state"
                % (clock.state,
                   ": " + ", ".join(clock.missing[:3]) if clock.missing else ""))
    current = []
    for attempt in fresh:
        # An attempt bound to an ORCHESTRATOR-OBSERVED digest of the tree it ran
        # against needs no clock inference at all — that is the strongest form
        # of provenance, and it is preferred whenever it exists.
        observed = attempt.get("observed_source_digest")
        if observed:
            if manifest and observed == manifest:
                current.append(attempt)
            continue
        if ingest.attempt_predates_tree(attempt, clock.mtime) is False:
            current.append(attempt)
    if not current:
        if any(a.get("observed_source_digest") for a in fresh):
            return ("its corroborating run(s) were observed against a "
                    "different tree than the one being promoted")
        if any(not a.get("started_at") for a in fresh):
            return ("its corroborating run(s) carry no start time, so they "
                    "cannot be placed against this tree")
        return ("every corroborating run started before the sources last "
                "changed, so none of them ran against the promoted tree")
    return None


# How long after a command runs its attempt may still be missing from the log
# without that meaning it never happened. Controllers flush asynchronously.
LOG_FLUSH_GRACE_SECONDS = 120


def join_claims_and_attempts(entries, attempts, log_lag_seconds=None):
    """Join builder CLAIMS to log ATTEMPTS, in the order that makes the join
    truthful.

    ORDER MATTERS, and an earlier version had it backwards: it fixed each
    claim's state and only then re-adjudicated the attempts against the claim's
    declared expectations. An attempt could therefore flip to `fail` while the
    claim it came from still read `corroborated`. Expectations are applied
    FIRST, and claim truth is derived from the re-adjudicated result.

    Matching is on `command_fingerprint`, not on the raw sanitized identity.
    Exact-identity matching could not join a claim to its own run: the log
    records the wrapped form (`... 2>&1 | tail -3`) and the status records the
    bare command, so every claim came back `self_reported` while its evidence
    sat in the log. Reporting a matching failure as an absence of evidence is
    worse than either mistake alone.

    Returns `(claims, attempts)`; both carry the join.
    """
    entries = [dict(e) for e in (entries or []) if isinstance(e, dict)]
    # A status entry may already have been joined on an earlier pass. Treat
    # every join as a fresh projection: otherwise a claim that was previously
    # unconfirmable can later become corroborated while retaining the old
    # `claim_reason` / `contradiction_kind`, making one object assert both
    # outcomes at once.
    join_fields = (
        "claim_state", "claim_reason", "contradiction_kind",
        "evidence_state", "log_adjudications", "log_attempt_ids",
        "corroborating_attempts", "contradicting_attempts",
    )
    for claim in entries:
        for field in join_fields:
            claim.pop(field, None)
    attempts = attempts or []
    by_print = {}
    # The coarse, sanitized-derived key is a FALLBACK for attempts recorded
    # before fingerprints were persisted. It is kept apart from the precise key:
    # two claims whose raw fingerprints differ are not ambiguous just because
    # their sanitized forms collide — the coarse key is simply unusable for
    # them, and dropping it is the right response rather than refusing both.
    fallback_print = {}
    for claim in entries:
        claim["command_fingerprint"] = ingest.command_fingerprint(
            claim.get("command"))
        claim["command_identity"] = ingest.sanitize_command(
            claim.get("command"))
        # Two fingerprints, compared LIKE WITH LIKE. An attempt ingested with
        # this build carries a fingerprint taken from the RAW command; an older
        # attempt carries none and can only be fingerprinted from its sanitized
        # identity, which yields a different digest for the same command.
        # Matching a raw-derived print against a sanitized-derived one can never
        # succeed, so each is compared only against its own kind.
        claim["command_fingerprint_sanitized"] = ingest.command_fingerprint(
            claim["command_identity"])
        claim.setdefault("evidence_state", None)
        if claim["command_fingerprint"]:
            by_print.setdefault(claim["command_fingerprint"], []).append(claim)
        fallback = claim["command_fingerprint_sanitized"]
        if fallback and fallback != claim["command_fingerprint"]:
            fallback_print.setdefault(fallback, []).append(claim)
    # AMBIGUITY IS REFUSED, NOT RESOLVED. When two claims share a fingerprint,
    # `setdefault` used to keep the first and hand ITS attempts to both — so one
    # assertion could corroborate a different assertion that never ran. A join
    # that cannot say which claim an attempt belongs to has no business
    # corroborating either.
    ambiguous = {fp for fp, claims in by_print.items() if len(claims) > 1}
    by_print = {fp: claims[0] for fp, claims in by_print.items()
                if fp not in ambiguous}
    # A colliding fallback key is dropped, not escalated to ambiguity.
    by_print.update({fp: claims[0] for fp, claims in fallback_print.items()
                     if len(claims) == 1 and fp not in by_print
                     and fp not in ambiguous})

    # 1. Attach the declared purpose/expectations and RE-ADJUDICATE.
    matched = {}
    for attempt in attempts:
        # Prefer the fingerprint PERSISTED at ingestion (taken from the raw
        # command); fall back to deriving one from the sanitized identity only
        # for attempts recorded before that field existed.
        fingerprint = attempt.get("command_fingerprint")
        if not fingerprint:
            fingerprint = ingest.command_fingerprint(
                attempt.get("command_identity"))
            attempt["command_fingerprint"] = fingerprint
        claim = by_print.get(fingerprint)
        if not claim:
            attempt.setdefault("purpose", None)
            attempt["claim_state"] = "unclaimed"
            continue
        attempt["purpose"] = claim.get("purpose")
        # The claim's manifest is recorded as the CLAIMED one, never copied
        # onto the attempt as if the log had observed it. Copying it and then
        # reading it back as proof that the attempt ran against that tree was
        # circular: a stale attempt with no provenance inherited the current
        # digest and promoted itself.
        if claim.get("source_manifest"):
            attempt["claimed_source_manifest"] = claim["source_manifest"]
        for field in ("expected_test_count", "expected_polarity"):
            if claim.get(field) is not None:
                attempt[field] = claim[field]
        expected = attempt.get("expected_test_count")
        polarity = attempt.get("expected_polarity")
        if expected is not None or polarity:
            attempt["adjudication"] = ingest.adjudicate(
                attempt.get("exit_status"),
                executed_count=attempt.get("executed_count"),
                expected_count=expected, expected_polarity=polarity,
                timed_out=attempt.get("timed_out"),
                interrupted=attempt.get("interrupted"))
        matched.setdefault(fingerprint, []).append(attempt)

    # 2. NOW derive claim truth, from adjudications that already account for
    #    the expectations the claim itself declared.
    for claim in entries:
        if claim.get("command_fingerprint") in ambiguous:
            claim["claim_state"] = "ambiguous"
            claim["claim_reason"] = (
                "another claim in this inventory has the same command "
                "fingerprint, so no attempt can be attributed to this one "
                "specifically. Refused rather than guessed.")
            continue
        found = (matched.get(claim.get("command_fingerprint"))
                 or matched.get(claim.get("command_fingerprint_sanitized"))
                 or [])
        if not found:
            # ABSENT vs PENDING. A controller writes its log asynchronously, so
            # a command that ran seconds ago may not be on disk yet. Calling
            # that "no evidence" is wrong in a specific way: it is
            # indistinguishable from a command that never ran, and it invites
            # re-running work that already succeeded. Neither state promotes —
            # they simply say different true things.
            claim["claim_state"] = "self_reported"
            if log_lag_seconds is not None and claim.get("ran_at_seconds") \
                    and log_lag_seconds < LOG_FLUSH_GRACE_SECONDS:
                claim["evidence_state"] = "evidence_pending"
                claim["claim_reason"] = (
                    "no attempt on disk yet: the controller log is %ds behind "
                    "and writes asynchronously. Evidence PENDING, not absent."
                    % int(log_lag_seconds))
            else:
                claim["evidence_state"] = "evidence_absent"
                claim["claim_reason"] = (
                    "no controller-log attempt ran this command; the claim "
                    "stands on the builder's word alone")
            continue
        # ORDER BY TIME, not by position. `found` is built by walking the
        # attempts list, and `observations_for` concatenates them ROLE BY ROLE —
        # so the last element was the last attempt of the last role, not the
        # newest attempt. A command run by two roles therefore reported an old
        # failure as its "most recent run" while a newer passing run sat
        # earlier in the list.
        found = sorted(found, key=lambda a: (a.get("started_at") or "",
                                             a.get("ended_at") or ""))
        adjudications = [a.get("adjudication") for a in found]
        # The claim describes the run the builder made; the LATEST attempt is
        # the log's answer to it. Judging the claim against every attempt ever
        # made meant one development-time failure permanently contradicted a
        # command that now passes. Older failures are NOT erased — they stay in
        # `log_adjudications` and in `contradiction_kind` — they simply are not
        # the thing the claim is being compared against.
        log_ok = found[-1].get("adjudication") == "pass"
        claim["log_adjudications"] = adjudications
        claim["log_attempt_ids"] = [a.get("id") for a in found if a.get("id")]
        if bool(claim.get("ok")) == bool(log_ok):
            claim["claim_state"] = "corroborated"
            # The attempts that actually back it, so the gate can require a
            # fresh unpiped one rather than trusting the label.
            claim["corroborating_attempts"] = [
                {"id": a.get("id"), "pipeline": bool(a.get("pipeline")),
                 "started_at": a.get("started_at"),
                 "adjudication": a.get("adjudication"),
                 "source_manifest": a.get("source_manifest"),
                 "observed_source_digest": a.get("observed_source_digest")}
                for a in found if a.get("adjudication") == "pass"]
        else:
            claim["claim_state"] = "contradicted"
            # WHICH KIND of contradiction, because they mean different things.
            # A command that failed earlier in the session and passed on the
            # last run is the "a failure is never erased by a later pass" rule
            # working as specified — the claim describes the final run, the log
            # describes every run, and both stand. That is not the same as a
            # claim whose most recent run disagrees with it, which is a
            # straightforwardly wrong claim.
            latest = found[-1].get("adjudication")
            earlier_failures = sum(
                1 for a in adjudications[:-1] if a in ("fail", "unresolved"))
            if latest == "pass" and earlier_failures:
                claim["contradiction_kind"] = "earlier_failures_retained"
                claim["claim_reason"] = (
                    "the builder recorded ok=%s and the most recent run of "
                    "this command did pass, but %d earlier run(s) failed and "
                    "are retained — a failure is not erased by a later pass. "
                    "Adjudications: %s."
                    % (bool(claim.get("ok")), earlier_failures,
                       ", ".join(sorted(set(a for a in adjudications if a)))))
            elif latest == "unknown":
                # The status could not be read — typically a pipeline, where
                # the shell reports the LAST stage's exit. That is not the log
                # disagreeing; it is the log unable to confirm, which is a
                # weaker and different statement.
                claim["contradiction_kind"] = "unconfirmable"
                claim["claim_reason"] = (
                    "the builder recorded ok=%s, but the most recent logged "
                    "run of this command has an unreadable exit status (its "
                    "output was piped, so the shell reported the last stage). "
                    "The claim is not contradicted, it is UNCONFIRMED."
                    % bool(claim.get("ok")))
            else:
                claim["contradiction_kind"] = "latest_run_disagrees"
                claim["claim_reason"] = (
                    "the builder recorded ok=%s; after applying its own "
                    "declared expectations the controller log adjudicates the "
                    "most recent run as %s. Both are retained."
                    % (bool(claim.get("ok")), latest))
        claim["evidence_state"] = "evidence_present"
        for attempt in found:
            attempt["claim_state"] = claim["claim_state"]
    return entries, attempts


def log_lag(results):
    """How far behind wall-clock the newest ingested log entry is, in seconds.

    `None` when nothing datable was ingested. This is what separates "the log
    does not show it" from "the log has not caught up yet" — a distinction that
    decides whether re-running the work would tell us anything new.
    """
    import datetime
    newest = None
    for result in (results or {}).values():
        for call in getattr(result, "tool_activity", []) or []:
            stamp = call.get("ended_at") or call.get("started_at")
            if not stamp:
                continue
            try:
                when = datetime.datetime.fromisoformat(
                    str(stamp).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if newest is None or when > newest:
                newest = when
    if newest is None:
        return None
    import time
    return max(0.0, time.time() - newest)


def _builder_verification(session_uuid):
    """The builder's own verification log, read from its status file.

    Carried in the record so a completion item that cites a check is validated
    against whether that check PASSED, rather than against whether someone
    typed its name. A count mismatch is surfaced here rather than trusted: an
    entry claiming an expected test count that its own output contradicts is
    marked `ok: False` with the reason, because a self-reported pass that the
    output does not support is exactly what criterion 3 exists to catch.
    """
    directory = state_store.session_assets_dir(session_uuid)
    status = _read_json(os.path.join(directory, "builder.status.json"))
    entries = ((status or {}).get("result") or {}).get("verification")
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        checked = dict(entry)
        expected = entry.get("expected_test_count")
        observed = ingest.parse_test_count(entry.get("output_excerpt") or "")
        if isinstance(expected, int) and observed is not None:
            checked["observed_test_count"] = observed
            if observed != expected:
                checked["ok"] = False
                checked["count_mismatch"] = True
                checked["reason"] = (
                    "expected %d tests, output reports %d" % (expected,
                                                              observed))
        out.append(checked)
    return out


def completion_seeds_path_for(session_uuid):
    """Where a run declares what it committed to delivering.

    The account has to be seeded from something the run states up front,
    otherwise "what did we deliver" is answerable only in hindsight by whoever
    is writing the summary — which is the hand-authored claim the record exists
    to replace.
    """
    return os.path.join(state_store.session_assets_dir(session_uuid),
                        "completion_seeds.json")


def _completion_seeds(session_uuid):
    """The declared items, or None. Tolerant: a missing file leaves the account
    honestly unknown rather than silently empty."""
    data = _read_json(completion_seeds_path_for(session_uuid))
    if isinstance(data, dict):
        data = data.get("items")
    return data if isinstance(data, list) else None


# Completion states. `partially_delivered` and `still_open` exist because the
# alternative — forcing every item into delivered-or-not — is what produces a
# report that says "done" about work that is half done.
COMPLETION_STATES = ("delivered", "partially_delivered", "rejected",
                     "still_open")


def _evidence_is_substantive(reference, record):
    """Whether one evidence reference actually SUPPORTS a completion claim.

    Presence is not evidence. An earlier version resolved any existing
    `record.*` path regardless of its value, so `record.milestones` — an empty
    object whose own `state` was `unknown` — certified its item as delivered.
    That turned the completion account into a second place to assert things,
    which is the exact failure it exists to prevent.

    A reference counts only when it carries information:

    - `record.<path>` must resolve AND be non-empty AND not be self-declared
      unknown. A field that says `state: unknown` is the record reporting that
      it does not know; reading that as proof of delivery inverts its meaning.
    - a verification LABEL must name a check that actually passed.
    - anything else (a file or file#symbol citation) must exist on disk.
    """
    if not isinstance(reference, str) or not reference.strip():
        return False
    if reference.startswith("record."):
        sentinel = object()
        node = record
        for part in reference[len("record."):].split("."):
            node = node.get(part, sentinel) if isinstance(node, dict) else sentinel
            if node is sentinel:
                return False
        if isinstance(node, dict):
            if node.get("state") in (UNKNOWN, "missing", "unreadable"):
                return False
            # An empty container is a field that exists and holds nothing.
            return bool(node)
        if isinstance(node, (list, tuple, str)):
            return bool(node)
        if node is None:
            return False
        if node is False:
            return False
        return True
    # A `path#anchor` citation is a FILE reference; only the part before the
    # anchor names anything on disk. Testing the whole string treated
    # `README.md#Measurement` as a verification label, so a real file citation
    # resolved as a check that never ran.
    label = reference.split("#", 1)[0].strip()
    passing = _passing_verification_labels(record)
    if passing is not None and label in passing:
        return True
    if passing is not None and _looks_like_verification_label(label):
        # Named as a check, but not among the ones that passed.
        return False
    candidate = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), label)
    return os.path.exists(candidate) or os.path.exists(label)


def _looks_like_verification_label(reference):
    """A reference shaped like a verification label rather than a file path."""
    return not ("/" in reference or reference.endswith(".md")
                or reference.endswith(".py") or reference.endswith(".json"))


def _passing_verification_labels(record):
    """Labels of the verification commands that actually PASSED, or None when
    the record carries no verification log to check against."""
    entries = (record or {}).get("verification")
    if not isinstance(entries, list):
        return None
    return {e.get("label") for e in entries
            if isinstance(e, dict) and e.get("ok") and e.get("label")}


def resolve_completion_state(seed, record):
    """Resolve one declared item against the RECORD, not against a belief.

    Every evidence reference must resolve to a real field path or a passing
    verification label. An item whose evidence does not resolve is
    `still_open` no matter what it claims — which is what stops the account
    from being a second place to assert things.
    """
    evidence = [e for e in (seed.get("evidence") or []) if e]
    if seed.get("state") == "rejected":
        return "rejected", evidence, []
    resolved, unresolved = [], []
    for reference in evidence:
        if _evidence_is_substantive(reference, record):
            resolved.append(reference)
        else:
            unresolved.append(reference)
    if not resolved:
        return "still_open", evidence, unresolved
    if unresolved:
        return "partially_delivered", resolved, unresolved
    declared = seed.get("state")
    return (declared if declared in COMPLETION_STATES else "delivered",
            resolved, unresolved)


def completion_account(record, session_uuid=None, seeds=None):
    """The delivered / partially delivered / rejected / still-open account.

    AUTHORITATIVE HERE (P13). There is deliberately no second Markdown file:
    `builder.summary.md` renders this and is labelled a derived view, so the two
    cannot drift and there is never a question of which one is true.

    `seeds` is the list of items the run committed to, each resolved to a state
    with EVIDENCE — record field paths, verification labels, or file+symbol
    citations. An item with no evidence is `still_open` regardless of what
    anyone believes about it; "we did that" without a pointer is exactly the
    unevidenced claim the record exists to replace.
    """
    entries = []
    for seed in seeds or []:
        if not isinstance(seed, dict):
            continue
        state, evidence, unresolved = resolve_completion_state(seed, record)
        entry = {
            "item": seed.get("item"),
            "state": state,
            "evidence": evidence,
            "reason": seed.get("reason"),
        }
        if unresolved:
            entry["unresolved_evidence"] = unresolved
        entries.append(entry)
    if not entries:
        # Nothing was seeded. Say so plainly rather than emitting an empty list
        # that reads like "nothing was left open".
        entries.append({
            "item": "completion account",
            "state": "still_open",
            "evidence": [],
            "reason": ("no completion items were recorded for this session; "
                       "the account is unknown, not empty"),
        })
    return entries


def summarize_scores(scores):
    """Aggregate scores.json into the legacy evaluation view.

    Lives on the BUILD side because it reads a raw source. The authoritative
    view is `record.score_cohorts`, which groups scores into homogeneous
    cohorts rather than pooling them; this is retained so sessions whose scores
    predate cohorts still report something.
    """
    entries = (scores or {}).get("evaluations")
    entries = entries if isinstance(entries, list) else []
    received = {}
    eval_cost = {}
    score_by_verdict = {}
    seen_turns = set()

    def _as_int(value):
        return value if isinstance(value, int) and not isinstance(
            value, bool) else 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        criteria = [c for c in entry.get("criteria") or []
                    if isinstance(c, dict)
                    and state_store.is_numeric_score(c.get("score"))]
        rkey = (entry.get("evaluatee") or "(unknown)",
                entry.get("evaluatee_tool") or "(unknown)",
                entry.get("evaluatee_model") or "(unknown)")
        rbucket = received.setdefault(
            rkey, {"entries": 0, "score_total": 0, "score_count": 0,
                   "criteria": {}})
        rbucket["entries"] += 1
        for crit in criteria:
            score = crit["score"]
            rbucket["score_total"] += score
            rbucket["score_count"] += 1
            cb = rbucket["criteria"].setdefault(
                crit.get("name") or "(unnamed)", {"total": 0, "count": 0})
            cb["total"] += score
            cb["count"] += 1
            verdict = entry.get("reviewed_verdict")
            if verdict and entry.get("context") == "review-round":
                vb = score_by_verdict.setdefault(
                    verdict, {"total": 0, "count": 0})
                vb["total"] += score
                vb["count"] += 1
        ekey = (entry.get("evaluator") or "(unknown)",
                entry.get("evaluator_tool") or "(unknown)",
                entry.get("evaluator_model") or "(unknown)")
        ebucket = eval_cost.setdefault(
            ekey, {"entries": 0, "turns": 0, "duration_ms": 0, "usage": {}})
        ebucket["entries"] += 1
        turn_id = entry.get("eval_turn_id")
        if turn_id and turn_id in seen_turns:
            continue
        if turn_id:
            seen_turns.add(turn_id)
        ebucket["turns"] += 1
        ebucket["duration_ms"] += _as_int(entry.get("duration_ms"))
        for field, val in (entry.get("usage") or {}).items():
            iv = _as_int(val)
            if iv:
                ebucket["usage"][field] = ebucket["usage"].get(field, 0) + iv
    return {"entry_count": len(entries), "received": received,
            "eval_cost": eval_cost, "score_by_verdict": score_by_verdict}


def _jsonable_scores(summary):
    """Make the scores view JSON-round-trippable (its keys are tuples), and
    pre-compute every average so the renderer only looks values up."""
    out = {"entry_count": summary.get("entry_count", 0)}
    received = {}
    for key, bucket in (summary.get("received") or {}).items():
        entry = dict(bucket)
        entry["average"] = (bucket["score_total"] / bucket["score_count"]
                            if bucket["score_count"] else UNKNOWN)
        entry["criteria"] = {
            name: dict(cb, average=(cb["total"] / cb["count"]
                                    if cb["count"] else UNKNOWN))
            for name, cb in (bucket.get("criteria") or {}).items()}
        received[" | ".join(str(p) for p in key)] = entry
    out["received"] = received
    out["eval_cost"] = {" | ".join(str(p) for p in key): dict(bucket)
                        for key, bucket in
                        (summary.get("eval_cost") or {}).items()}
    out["score_by_verdict"] = {
        verdict: dict(bucket, average=(bucket["total"] / bucket["count"]
                                       if bucket["count"] else UNKNOWN))
        for verdict, bucket in (summary.get("score_by_verdict") or {}).items()}
    return out


def _jsonable_summary(summary):
    """Make the trace summary JSON-round-trippable.

    Its aggregates are keyed by tuples, which JSON cannot represent. Joining
    them with a separator keeps the record readable AND makes it survive a
    write/read cycle unchanged — a record that changed shape when persisted
    could not be the authority the report renders.
    """
    out = dict(summary)
    for key in ("bytes_by_role_controller", "role_prompt_bytes",
                "usage_by_role_model"):
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {" | ".join(str(p) for p in k) if isinstance(k, tuple)
                        else str(k): v for k, v in value.items()}
    largest = out.get("largest_prompts")
    if isinstance(largest, list):
        out["largest_prompts"] = [
            {"bytes": item[0], "role": item[1], "controller": item[2],
             "kind": item[3], "round": item[4]}
            for item in largest if isinstance(item, (list, tuple))
            and len(item) == 5]
    return out


def _pricing_view(work):
    """Price each turn, or say plainly that it is unpriced.

    With the default empty snapshot every model resolves to `unpriced`, so the
    report reads "unpriced" rather than "$0.00" — which would be a claim that
    the run was free.
    """
    snapshot = pricing.load_snapshot()
    priced = 0
    unpriced = 0
    models = {}
    for entry in work.values():
        identity = entry.get("identity") or {}
        model = identity.get("model")
        result = pricing.price_usage(entry.get("usage"), model,
                                     snapshot=snapshot)
        if result.get("state") == "priced":
            priced += 1
        else:
            unpriced += 1
        bucket = models.setdefault(model or UNKNOWN, {
            "turns": 0, "state": result.get("state"), "cost": None})
        bucket["turns"] += 1
    return {
        "schema_version": snapshot.get("schema_version"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "captured_at": snapshot.get("captured_at"),
        "priced_turns": priced,
        "unpriced_turns": unpriced,
        "by_model": models,
        "note": ("The default snapshot is EMPTY, so every model resolves to "
                 "unpriced. An unpriced turn is never rendered as 0 and never "
                 "ranked (CV-004 upheld: real prices in a repo are stale by "
                 "construction)."),
    }


def replay_rounds(record):
    """Rounds that redid unchanged work.

    A replay produces zero new findings and zero marginal value, and its cost is
    attributed to RECOVERY rather than to progress — because that is what it
    was. Reporting it as ordinary productive work would make repeated failure
    look like throughput.
    """
    by_round = {}
    for entry in (record.get("work") or {}).values():
        key = (entry.get("phase"), entry.get("round"))
        if key[1] is None:
            continue
        bucket = by_round.setdefault(str(key), {
            "phase": key[0], "round": key[1], "turns": 0,
            "recovery_turns": 0})
        bucket["turns"] += 1
        if entry.get("work_class") == "recovery":
            bucket["recovery_turns"] += 1
    out = []
    for bucket in by_round.values():
        if bucket["recovery_turns"]:
            out.append({
                "phase": bucket["phase"], "round": bucket["round"],
                "new_findings": 0, "marginal_value": 0,
                "attributed_to": "recovery",
                "recovery_turns": bucket["recovery_turns"],
            })
    return out


def write_record(session_uuid, record):
    """Persist the record. Returns the path, or None on failure."""
    path = state_store.measurement_path_for(session_uuid)
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(record, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        return None
    return path


def load_record(session_uuid):
    """The persisted record, or None. The report's ONLY input."""
    return _read_json(state_store.measurement_path_for(session_uuid))


def build_and_write(session_uuid, cwd=None, ingest_results=None):
    record = build_record(session_uuid, cwd=cwd, ingest_results=ingest_results)
    write_record(session_uuid, record)
    return record


# --------------------------------------------------------------------------- #
# Provenance: a separate step that produces no figure.                        #
# --------------------------------------------------------------------------- #


def check_provenance(session_uuid, record):
    """Has any raw source moved on since the record was built?

    Returns `{state: fresh|stale|unknown, diverged: [...], built_at}`. It hashes
    raw sources — and produces NO measurement figure, which is what keeps "the
    report computes nothing" literally true while still warning about staleness.
    Its result is printed as a banner and is never passed to the renderer.

    Best-effort: a failure yields `unknown` and never blocks a report.
    """
    if not isinstance(record, dict):
        return {"state": UNKNOWN, "diverged": [], "built_at": None,
                "detail": "no record to check"}
    built_from = record.get("built_from")
    if not isinstance(built_from, dict):
        return {"state": UNKNOWN, "diverged": [],
                "built_at": record.get("built_at"),
                "detail": "record predates provenance stamping"}
    diverged = []
    try:
        for name, path in _source_paths(session_uuid).items():
            recorded = built_from.get(name)
            if not isinstance(recorded, dict):
                continue
            current = _fingerprint(path)
            if current.get("sha256") != recorded.get("sha256"):
                diverged.append(name)
    except Exception:  # noqa: BLE001
        return {"state": UNKNOWN, "diverged": [],
                "built_at": record.get("built_at"),
                "detail": "provenance check failed"}
    return {
        "state": "stale" if diverged else "fresh",
        "diverged": sorted(diverged),
        "built_at": record.get("built_at"),
    }


# --------------------------------------------------------------------------- #
# Legacy trace summary (moved here from cowork_report, which is now pure).    #
# --------------------------------------------------------------------------- #


def _coerce_events(source):
    """Accept a path, an iterable of JSON lines, or an iterable of event dicts.

    A trace is an append-only log that may be read mid-write, so a malformed
    line is SKIPPED rather than raised — one truncated tail must not cost the
    whole run's accounting.
    """
    if isinstance(source, str):
        return _read_jsonl(source)
    out = []
    for item in source or []:
        if isinstance(item, dict):
            out.append(item)
            continue
        try:
            obj = json.loads(item)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def summarize_trace(source):
    """Aggregate a raw trace into the legacy report structure.

    Lives HERE rather than in cowork_report because it reads a raw source, and
    the renderer is not allowed to. Callers that want the old summary get it
    from the build side of the boundary.
    """
    events = _coerce_events(source)
    bytes_by_role_controller = {}
    bytes_by_kind = {}
    fresh_resume = {"fresh": 0, "resume": 0, "unknown": 0}
    largest = []
    artifact_bytes = {}
    delivery_breakdown = {}
    role_prompt_bytes = {}
    review_skips = []
    usage_by_controller = {}
    usage_by_role_model = {}
    turn_count = 0

    def _as_int(value):
        return value if isinstance(value, int) and not isinstance(
            value, bool) else 0

    for obj in events:
        name = obj.get("event")
        if name == "controller.turn.start":
            turn_count += 1
            role = obj.get("role") or "(unknown)"
            controller = obj.get("controller") or "(unknown)"
            kind = obj.get("prompt_kind") or "(unspecified)"
            pbytes = _as_int(obj.get("prompt_bytes"))
            key = (role, controller)
            bytes_by_role_controller[key] = (
                bytes_by_role_controller.get(key, 0) + pbytes)
            bytes_by_kind[kind] = bytes_by_kind.get(kind, 0) + pbytes
            if obj.get("fresh") is True:
                fresh_resume["fresh"] += 1
            elif obj.get("resume") is True:
                fresh_resume["resume"] += 1
            else:
                fresh_resume["unknown"] += 1
            largest.append((pbytes, role, controller, kind, obj.get("round")))
            for art in obj.get("artifacts") or []:
                if not isinstance(art, dict):
                    continue
                path = art.get("path")
                if not path:
                    continue
                touched = _as_int(art.get("bytes"))
                delivery = art.get("delivery") or "embedded"
                emb = art.get("embedded_bytes")
                if emb is None:
                    emb = touched if delivery == "embedded" else 0
                else:
                    emb = _as_int(emb)
                entry = artifact_bytes.setdefault(
                    path, {"bytes": 0, "embedded": 0, "turns": 0})
                entry["bytes"] += touched
                entry["embedded"] += emb
                entry["turns"] += 1
                db = delivery_breakdown.setdefault(
                    delivery, {"turns": 0, "embedded": 0, "touched": 0})
                db["turns"] += 1
                db["embedded"] += emb
                db["touched"] += touched
        elif name == "role.prompt.bytes":
            rp_key = (obj.get("role") or "(unknown)",
                      obj.get("delivery") or "(unknown)")
            rp = role_prompt_bytes.setdefault(
                rp_key, {"bytes": 0, "launches": 0})
            rp["bytes"] += _as_int(obj.get("bytes"))
            rp["launches"] += 1
        elif name in ("controller.turn.end", "controller.probe.end"):
            usage = obj.get("usage")
            if isinstance(usage, dict):
                controller = obj.get("controller") or "(unknown)"
                bucket = usage_by_controller.setdefault(controller, {})
                for field, val in usage.items():
                    iv = _as_int(val)
                    if iv:
                        bucket[field] = bucket.get(field, 0) + iv
            if name == "controller.turn.end":
                rkey = (obj.get("role") or "(unknown)",
                        obj.get("controller") or "(unknown)",
                        obj.get("model") or "(unknown)")
                rbucket = usage_by_role_model.setdefault(
                    rkey, {"turns": 0, "usage": {}})
                rbucket["turns"] += 1
                if isinstance(usage, dict):
                    for field, val in usage.items():
                        iv = _as_int(val)
                        if iv:
                            rbucket["usage"][field] = (
                                rbucket["usage"].get(field, 0) + iv)
        elif name == "review.skipped":
            review_skips.append({
                "role": obj.get("role") or "(unknown)",
                "reason": obj.get("reason") or "",
            })

    largest.sort(key=lambda t: t[0], reverse=True)
    return {
        "turn_count": turn_count,
        "bytes_by_role_controller": bytes_by_role_controller,
        "bytes_by_kind": bytes_by_kind,
        "fresh_resume": fresh_resume,
        "largest_prompts": largest[:10],
        "artifact_bytes": artifact_bytes,
        "delivery_breakdown": delivery_breakdown,
        "role_prompt_bytes": role_prompt_bytes,
        "review_skips": review_skips,
        "review_skip_count": len(review_skips),
        "usage_by_controller": usage_by_controller,
        "usage_by_role_model": usage_by_role_model,
    }
