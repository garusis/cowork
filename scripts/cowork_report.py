#!/usr/bin/env python3
"""The PURE renderer of the authoritative measurement record (D3).

This module computes nothing. `render_report(record)` takes the record and
nothing else — no path, no trace, no raw source — and every figure it prints is
LOOKED UP at a declared field path in that record. `rendered_lineage(record)`
exposes those paths, so a test can assert that each printed figure resolves to a
real field rather than to arithmetic hidden in a format string.

That is the whole point. "The report says only what the record knows" is not a
style preference: as long as the printer can compute, a number can appear in a
report that appears nowhere in the authoritative record, and the two can then
disagree without anything detecting it. Removing the printer's ability to
compute removes the possibility.

Aggregation lives in `cowork_measure` (including `summarize_trace`, which moved
there because it reads a raw source). Staleness lives in
`cowork_measure.check_provenance`, whose banner `cowork.py` prints ABOVE this
output and whose result is never passed in here.

Python 3.9+, stdlib only.
"""

import json

UNKNOWN = "unknown"


class RawSourceRejected(TypeError):
    """Raised when the renderer is handed a path or a trace instead of a record.

    A renderer that quietly accepted a path could load it, and then it would be
    computing again. Refusing loudly is what keeps the boundary real.
    """


def _require_record(record):
    if isinstance(record, str):
        raise RawSourceRejected(
            "render_report takes the measurement RECORD, not a path (%r). "
            "Build it with cowork_measure.build_record / load it with "
            "cowork_measure.load_record; the renderer never reads a file."
            % (record,))
    if not isinstance(record, dict):
        raise RawSourceRejected(
            "render_report takes the measurement record (a dict), got %s"
            % type(record).__name__)
    return record


def _at(record, path, default=UNKNOWN):
    """Look up one declared field path. The ONLY way a figure reaches output."""
    node = record
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _fmt(value):
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "%.2f" % value
    return str(value)


def _fmt_ms(value):
    if not isinstance(value, int) or isinstance(value, bool):
        return UNKNOWN
    if value >= 60000:
        return "%.1f min" % (value / 60000.0)
    return "%.1f s" % (value / 1000.0)


def _secs_to_ms(seconds):
    """Convert a float seconds figure (as owned-transaction wall times are
    recorded) to the `int` milliseconds `_fmt_ms` expects, or None when
    `seconds` is not a plain number. A pure formatting-boundary conversion,
    not a recomputation of anything the record does not already state."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return int(seconds * 1000)


def _fmt_bytes(n):
    if not isinstance(n, int) or isinstance(n, bool):
        return UNKNOWN
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


# Every figure the report prints, and the record field path it comes from.
# This IS the contract criterion 5 is decided by: a figure with no entry here
# would be one the renderer computed.
LINEAGE = (
    ("turns.total", "cost.by_class"),
    ("cost.classes", "cost.by_class"),
    ("cost.reconciled", "cost.reconciled"),
    ("cost.basis", "cost.basis"),
    ("cost.independent_check", "cost.independent_check"),
    ("cost.unreconciled", "cost.unreconciled"),
    ("cost.incomparable_turns", "cost.incomparable.turns"),
    ("cost.unclassified_turns", "cost.unclassified.turns"),
    ("nested.work", "nested.work_items"),
    ("nested.totals", "nested.totals"),
    ("nested.basis", "nested.basis"),
    ("nested.comparable", "nested.comparable"),
    ("nested.comparability_reason", "nested.comparability_reason"),
    ("nested.provider_totals", "nested.provider_totals"),
    ("nested.artifact_attribution", "nested.artifact_attribution"),
    ("nested.contributions", "nested.contributions"),
    ("nested.contribution_count", "nested.contribution_count"),
    ("duration.by_class", "duration.by_class"),
    ("duration.user_wait_ms", "duration.by_class.user_wait_ms"),
    ("duration.user_wait_span_count", "duration.user_wait_span_count"),
    ("duration.user_wait_unresolved_count",
     "duration.user_wait_unresolved_count"),
    ("input.measured_bytes", "input_sources.measured_bytes"),
    ("input.attributed_tokens", "input_sources.attributed_input_tokens"),
    ("input.unattributed_tokens", "input_sources.unattributed_input_tokens"),
    ("input.axes", "input_sources.provider_token_axes"),
    ("findings.total", "findings.total"),
    ("findings.confirmed", "findings.confirmed"),
    ("findings.withdrawn", "findings.withdrawn"),
    ("findings.by_severity", "findings.by_severity"),
    ("verification.attempts", "verification_attempts"),
    ("verification.commands", "verification"),
    ("verification.self_reported", "verification_summary.self_reported"),
    ("verification.contradicted", "verification_summary.contradicted"),
    ("owned.incurred_cost", "owned_verification.incurred_cost"),
    ("owned.accepted_cost", "owned_verification.accepted_cost"),
    ("owned.avoided_cost", "owned_verification.avoided_cost"),
    ("tool_activity.by_role", "tool_activity"),
    ("verification.environment_recurrences", "environment_recurrences"),
    ("pricing.snapshot_id", "pricing.snapshot_id"),
    ("pricing.unpriced_turns", "pricing.unpriced_turns"),
    ("scores.cohorts", "score_cohorts"),
    ("marginal.per_disposition", "marginal_cost.per_disposition"),
    ("marginal.basis", "marginal_cost.basis"),
    ("calibration.rounds", "calibration.rounds"),
    ("enhancements.digest", "enhancements"),
    ("replay.rounds", "replay"),
    ("completion.entries", "completion"),
    ("milestones.by_phase", "milestones.by_phase"),
    ("readiness.claims", "readiness.claims"),
    ("readiness.total", "readiness.total"),
    ("readiness.unverified", "readiness.unverified"),
    ("queue.pending", "evaluation_queue.pending"),
    ("queue.by_state.pending", "evaluation_queue.by_state.pending"),
    ("queue.by_state.held", "evaluation_queue.by_state.held"),
    ("queue.by_state.attempting", "evaluation_queue.by_state.attempting"),
    ("queue.by_state.terminal", "evaluation_queue.by_state.terminal"),
    ("queue.by_state.retired", "evaluation_queue.by_state.retired"),
    ("queue.by_state.drained", "evaluation_queue.by_state.drained"),
    ("queue.read_state", "evaluation_queue.read_state"),
    ("trace.turn_count", "trace_summary.turn_count"),
    ("trace.bytes_by_kind", "trace_summary.bytes_by_kind"),
    ("trace.artifact_bytes", "trace_summary.artifact_bytes"),
    ("trace.usage_by_controller", "trace_summary.usage_by_controller"),
    ("trace.review_skips", "trace_summary.review_skips"),
    ("trace.review_skip_count", "trace_summary.review_skip_count"),
    ("trace.usage_by_role_model", "trace_summary.usage_by_role_model"),
    ("trace.role_prompt_bytes", "trace_summary.role_prompt_bytes"),
    ("trace.largest_prompts", "trace_summary.largest_prompts"),
    ("trace.delivery_breakdown", "trace_summary.delivery_breakdown"),
    ("trace.fresh_resume", "trace_summary.fresh_resume"),
    ("scores.legacy_received", "scores_summary.received"),
    ("scores.legacy_by_verdict", "scores_summary.score_by_verdict"),
    ("scores.legacy_eval_cost", "scores_summary.eval_cost"),
    ("incomplete", "incomplete"),
    ("built_at", "built_at"),
    ("schema_version", "schema_version"),
)


def rendered_lineage(record):
    """`{figure: {path, value, resolved}}` for every figure the report prints.

    `resolved` is False when a path is absent from the record — which is how a
    field renamed on the build side surfaces as a broken lineage rather than as
    a silently missing line in the output.
    """
    record = _require_record(record)
    out = {}
    for figure, path in LINEAGE:
        sentinel = object()
        value = _at(record, path, default=sentinel)
        out[figure] = {
            "path": path,
            "resolved": value is not sentinel,
            "value": None if value is sentinel else value,
        }
    return out


def render_report(record):
    """Render the authoritative record as plain text.

    `record` is the ONLY input. Passing a path raises rather than being loaded.
    """
    record = _require_record(record)
    lines = []
    head = "cowork measurement report"
    if record.get("session"):
        head += " — %s" % record.get("session")
    lines.append(head)
    lines.append("=" * 56)
    lines.append("")
    lines.append("Record schema %s, built %s"
                 % (_fmt(_at(record, "schema_version")),
                    _fmt(_at(record, "built_at"))))
    lines.append("This report is a rendering of that record. Every figure "
                 "below is a field in it.")
    lines.append("")

    lines.extend(_section_cost(record))
    lines.extend(_section_nested_work(record))
    lines.extend(_section_nested_cost(record))
    lines.extend(_section_duration(record))
    lines.extend(_section_input(record))
    lines.extend(_section_verification(record))
    lines.extend(_section_owned_verification(record))
    lines.extend(_section_claims(record))
    lines.extend(_section_findings(record))
    lines.extend(_section_marginal(record))
    lines.extend(_section_scores(record))
    lines.extend(_section_orchestrator_evaluations(record))
    lines.extend(_section_enhancements(record))
    lines.extend(_section_replay(record))
    lines.extend(_section_pricing(record))
    lines.extend(_section_trace(record))
    lines.extend(_section_scores_legacy(record))
    lines.extend(_section_readiness(record))
    lines.extend(_section_evaluation_queue(record))
    lines.extend(_section_completion(record))
    lines.extend(_section_incomplete(record))
    return "\n".join(lines) + "\n"


def _section_nested_work(record):
    lines = ["Nested work", "-" * 56]
    nested = _at(record, "nested.work_items", [])
    if not isinstance(nested, list):
        lines.extend(["  (nested work unknown for this record)", ""])
        return lines
    if not nested:
        lines.extend(["  (no governed child work recorded)", ""])
        return lines
    for item in nested:
        identity = item.get("identity") or {}
        lines.append("  %s  %s  %s/%s" % (
            _fmt(item.get("work_kind")), _fmt(item.get("work_state")),
            _fmt(identity.get("controller")), _fmt(identity.get("model"))))
        lines.append("      id=%s parent=%s duration=%s usage=%s" % (
            _fmt(item.get("work_id")), _fmt(item.get("parent_work_id")),
            _fmt(item.get("duration_ms")), _fmt(item.get("usage"))))
        lines.append(
            "      identity_sources=model:%s effort:%s policy=%s" % (
                _fmt(identity.get("model_source")),
                _fmt(identity.get("effort_source")),
                _fmt(item.get("effective_policy"))))
        lines.append("      agent=%s tools=%s terminal=%s" % (
            _fmt(item.get("agent_id")), _fmt(item.get("tool_count")),
            _fmt(item.get("terminal_source"))))
        if item.get("reason"):
            lines.append("      blocked: %s" % item["reason"])
        if item.get("delta") is not None:
            lines.append("      delta: %s" % _fmt(item["delta"]))
        if item.get("artifact_attribution") is not None:
            lines.append("      attribution: %s" %
                         _fmt(item["artifact_attribution"]))
    lines.append("")
    return lines


def _section_nested_cost(record):
    nested = _at(record, "nested", {})
    lines = ["Nested all-in cost", "-" * 56]
    if not isinstance(nested, dict) or not nested:
        lines.extend(["  (nested accounting unavailable)", ""])
        return lines
    lines.append("  comparable: %s" % _fmt(nested.get("comparable")))
    lines.append("  totals: %s" % _fmt(nested.get("totals")))
    lines.append("  basis: %s" % _fmt(nested.get("basis")))
    lines.append("  reason: %s" %
                 _fmt(nested.get("comparability_reason")))
    lines.append("  provider totals: %s" %
                 _fmt(nested.get("provider_totals")))
    lines.append("  artifact attribution: %s" %
                 _fmt(nested.get("artifact_attribution")))
    lines.append("  contributions: %s" %
                 _fmt(nested.get("contribution_count")))
    contributions = nested.get("contributions")
    if isinstance(contributions, list):
        for item in contributions:
            if not isinstance(item, dict):
                continue
            lines.append(
                "    actor=%s mode=%s child=%s artifact=%s evidence=%s" % (
                    _fmt(item.get("work_id")), _fmt(item.get("mode")),
                    _fmt(item.get("child_work_id")),
                    _fmt(item.get("artifact_path")),
                    _fmt(item.get("evidence"))))
    lines.append("")
    return lines


def _section_cost(record):
    lines = ["Cost by class (exclusive — every turn is in exactly one)",
             "-" * 56]
    by_class = _at(record, "cost.by_class", {})
    if not isinstance(by_class, dict) or not by_class:
        lines.append("  (no classified turns in this record)")
        lines.append("")
        return lines
    for name in sorted(by_class):
        bucket = by_class[name] or {}
        usage = bucket.get("usage") or {}
        parts = ", ".join("%s=%s" % (k, v) for k, v in sorted(usage.items()))
        unknown_note = ""
        if bucket.get("duration_unknown_turns"):
            unknown_note = (", %d with unknown duration"
                            % bucket["duration_unknown_turns"])
        lines.append("  %-12s %3s turns  %-10s%s%s"
                     % (name, _fmt(bucket.get("turns")),
                        _fmt_ms(bucket.get("duration_ms")),
                        (", " + parts) if parts else "", unknown_note))
    lines.append("")
    reconciled = _at(record, "cost.reconciled")
    basis = _at(record, "cost.basis", None)
    if reconciled is True:
        # Deliberately NOT "reconciles with the controllers' own totals" — this
        # check compares two figures derived from the same per-turn usage, so it
        # proves classification lost nothing and nothing more. The independent
        # provider comparison is reported on its own line below.
        lines.append("  Classification is complete: every turn's usage lands "
                     "in exactly one class.")
        if basis:
            lines.append("  Basis: %s." % basis)
    else:
        lines.append("  Classification is INCOMPLETE — usage a turn reported "
                     "did not land in any class:")
        for field, diff in sorted((_at(record, "cost.unreconciled", {})
                                   or {}).items()):
            lines.append("    unreconciled %-28s %s" % (field, diff))
    incomparable = _at(record, "cost.incomparable.turns", 0)
    if incomparable:
        lines.append("  %s turn(s) INCOMPARABLE: the provider's cumulative "
                     "counters moved backwards, so no honest per-turn figure "
                     "exists. Not counted as zero." % _fmt(incomparable))
    unclassified = _at(record, "cost.unclassified.turns", 0)
    if unclassified:
        lines.append("  %s turn(s) carry no recognised class."
                     % _fmt(unclassified))
    check = _at(record, "cost.independent_check", {})
    if isinstance(check, dict) and check:
        state = check.get("state")
        if state == "ok":
            lines.append("  Cross-checked against the providers' OWN per-turn "
                         "counters: agrees.")
        elif state == "diverged":
            lines.append("  Cross-check against the providers' own counters "
                         "DIVERGES:")
            for field, diff in sorted((check.get("mismatches") or {}).items()):
                lines.append("    %-28s %s" % (field, diff))
        else:
            lines.append("  No independent provider cross-check available for "
                         "this session.")
        if check.get("not_comparable_turns"):
            lines.append("    %s turn(s) report a cumulative thread counter, "
                         "which cannot be summed per turn and is excluded "
                         "from the cross-check."
                         % _fmt(check["not_comparable_turns"]))
    lines.append("")
    return lines


def _section_duration(record):
    lines = ["Time by class", "-" * 56]
    by_class = _at(record, "duration.by_class", {})
    if not isinstance(by_class, dict):
        lines.extend(["  (unknown)", ""])
        return lines
    for key in sorted(k for k in by_class if k.endswith("_ms")):
        value = by_class[key]
        rendered = _fmt_ms(value)
        if rendered == UNKNOWN:
            # Not "0.0 s". No turn of this class was measured, so its duration
            # is unknown — a different statement from "it took no time".
            rendered = "unknown (no measured turn of this class)"
        lines.append("  %-14s %s" % (key[:-3], rendered))
    unknown_turns = by_class.get("turns_with_unknown_duration")
    if unknown_turns:
        lines.append("  %s turn(s) report duration `unknown` (in flight, or "
                     "an end path that recorded none)." % _fmt(unknown_turns))
    span_count = _at(record, "duration.user_wait_span_count")
    if isinstance(span_count, int) and not isinstance(span_count, bool) \
            and span_count > 0:
        lines.append("  user_wait comes from %s timed prompt span(s) and is "
                     "never inferred from gaps between events."
                     % _fmt(span_count))
    else:
        lines.append("  user_wait is UNKNOWN: no timed prompt spans exist in "
                     "this session. It is not 0 — inferring it from gaps "
                     "between events is forbidden, because a gap is equally an "
                     "ingestion stall or a suspended process.")
    unresolved_count = _at(record, "duration.user_wait_unresolved_count")
    if isinstance(unresolved_count, int) \
            and not isinstance(unresolved_count, bool) \
            and unresolved_count > 0:
        lines.append("  %s wait span(s) unresolved (a killed process); they "
                     "contribute nothing." % _fmt(unresolved_count))
    lines.append("")
    return lines


def _section_input(record):
    lines = ["Input sources", "-" * 56]
    measured = _at(record, "input_sources.measured_bytes", {})
    if isinstance(measured, dict):
        for key in sorted(measured):
            lines.append("  %-28s %s" % (key, _fmt_bytes(measured[key])))
    axes = _at(record, "input_sources.provider_token_axes", {})
    if isinstance(axes, dict) and axes:
        lines.append("  provider token axes:")
        for key in sorted(axes):
            lines.append("    %-26s %s" % (key, _fmt(axes[key])))
    lines.append("  %-28s %s" % ("attributed input tokens",
                                 _fmt(_at(record,
                                          "input_sources."
                                          "attributed_input_tokens"))))
    lines.append("  %-28s %s" % ("UNATTRIBUTED input tokens",
                                 _fmt(_at(record,
                                          "input_sources."
                                          "unattributed_input_tokens"))))
    note = _at(record, "input_sources.note", None)
    if note:
        lines.append("  note: %s" % note)
    lines.append("")
    return lines


def _section_verification(record):
    lines = ["Verification attempts (derived from the controllers' own logs)",
             "-" * 56]
    attempts = _at(record, "verification_attempts", [])
    if not isinstance(attempts, list) or not attempts:
        lines.extend(["  (none reconciled into the ledger)", ""])
        return lines
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        lines.append(
            "  %-8s %-10s exit %-5s %-10s %s"
            % (_fmt(attempt.get("id")),
               _fmt(attempt.get("adjudication")),
               _fmt(attempt.get("exit_status")),
               _fmt(attempt.get("state")),
               _fmt(attempt.get("command_identity"))))
        detail = []
        if attempt.get("executed_count") is not None:
            detail.append("ran %s" % attempt["executed_count"])
        if attempt.get("expected_test_count") is not None:
            detail.append("expected %s" % attempt["expected_test_count"])
        if attempt.get("expected_polarity"):
            detail.append("polarity %s" % attempt["expected_polarity"])
        if attempt.get("claim_state"):
            detail.append("claim: %s" % attempt["claim_state"])
        if attempt.get("purpose"):
            detail.append("purpose: %s" % attempt["purpose"])
        if attempt.get("failure_class"):
            detail.append("failure: %s" % attempt["failure_class"])
        if attempt.get("retries"):
            detail.append("attempt %s of this command (%s retr%s)"
                          % (attempt.get("attempt_number"),
                             attempt["retries"],
                             "y" if attempt["retries"] == 1 else "ies"))
        if attempt.get("overlap_state") == "overlapping":
            detail.append("OVERLAPPING with %s other run(s) — neither result "
                          "cleanly describes the tree"
                          % _fmt(attempt.get("overlap_count")))
        if attempt.get("environment_recurrence", 0) > 1:
            detail.append("environment failure seen %sx — avoidable "
                          "orchestration cost"
                          % attempt["environment_recurrence"])
        if attempt.get("evidence_safety") == "refused":
            detail.append("EVIDENCE REFUSED: %s"
                          % attempt.get("refusal_reason"))
        if attempt.get("pipeline"):
            detail.append("PIPED (exit status is the last stage's)")
        if attempt.get("timed_out"):
            detail.append("timed out — terminal, never closed by a later run")
        if attempt.get("source_state") == "truncated":
            detail.append("from a TRUNCATED log; evidence after this is lost")
        detail.append("source manifest %s"
                      % (str(attempt.get("source_manifest"))[:12]
                         if attempt.get("source_manifest")
                         else "not_applicable (outside a building phase)"))
        if attempt.get("tty_stdin_mode"):
            detail.append("tty/stdin %s" % attempt["tty_stdin_mode"])
        if detail:
            lines.append("           %s" % "; ".join(detail))
    lines.extend(_attempt_rollups(record))
    lines.append("")
    return lines


def _section_owned_verification(record):
    """Owned verification transaction evidence (`owned_verification.*`) —
    the orchestrator's own hermetic, manifest-bound run of the plan's
    approved inventory, when one exists for this session. Distinct from the
    section above (`verification`/`verification_attempts`), which is always
    the controller-log-derived view; a session with an owned transaction
    carries BOTH, and this section is what makes clear which one the
    builder-readiness gate actually decided on. Tolerant of a legacy session
    that has none — every lookup below is a plain `_at` with a default, so a
    record built before this field existed renders the same "no owned
    transaction" note rather than failing.
    """
    lines = ["Owned verification transaction "
             "(orchestrator-run, manifest-bound)", "-" * 56]
    latest = _at(record, "owned_verification.latest", None)
    if not isinstance(latest, dict):
        lines.extend(["  (no owned verification transaction for this "
                      "session — controller-log-derived verification above "
                      "is the only evidence)", ""])
        return lines
    cost = _at(record, "owned_verification.cost", {}) or {}
    lines.append("  transaction %s  verdict=%s  final_suite=%s (%s)"
                 % (_fmt(latest.get("transaction_id")),
                    _fmt(latest.get("verdict")),
                    _fmt(latest.get("final_suite_label")),
                    _fmt(latest.get("final_suite_binding"))))
    lines.append("  work_items=%s  attempts=%s (initial=%s focused=%s)  "
                 "subprocess_wall_time=%s"
                 % (_fmt(cost.get("work_items")),
                    _fmt(cost.get("attempt_count")),
                    _fmt(cost.get("initial_attempt_count")),
                    _fmt(cost.get("focused_attempt_count")),
                    _fmt_ms(_secs_to_ms(cost.get("subprocess_wall_time_s")))))
    lines.append("  worker_identity_verified=%s  reused_lock_result=%s  "
                 "mutation_detected=%s"
                 % (_fmt(cost.get("worker_identity_verified")),
                    _fmt(cost.get("reused_lock_result")),
                    _fmt(cost.get("mutation_detected"))))
    if cost.get("mutation_detected"):
        mutation = cost.get("mutation") or {}
        lines.append("    MUTATION during verification: %s (changed: %s)"
                     % (_fmt(mutation.get("reason")),
                        ", ".join(mutation.get("changed_paths") or [])[:200]
                        or "(none listed)"))
    if cost.get("evidence_unresolved_count") or cost.get(
            "evidence_absent_count"):
        lines.append("  evidence retry/expiry: %s unresolved, %s absent "
                     "(bounded poll exhausted, never re-launched)"
                     % (_fmt(cost.get("evidence_unresolved_count")),
                        _fmt(cost.get("evidence_absent_count"))))
    snapshot = cost.get("snapshot") or {}
    if snapshot:
        lines.append("  snapshot manifest=%s index=%s"
                     % (str(snapshot.get("manifest_digest"))[:12],
                        str(snapshot.get("index_digest"))[:12]))
    for attempt in latest.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        lines.append(
            "    %-20s kind=%-16s exit=%-5s evidence=%-10s wall=%s"
            % (_fmt(attempt.get("label")), _fmt(attempt.get("kind")),
               _fmt(attempt.get("exit_code")),
               _fmt(attempt.get("evidence_state")),
               _fmt_ms(_secs_to_ms(attempt.get("wall_time_s")))))
    focused = _at(record, "owned_verification.focused_attribution", [])
    if isinstance(focused, list) and focused:
        lines.append("")
        lines.append("  Focused-check attribution (reviewer-triggered only "
                     "— never the initial baseline/preflight/final_suite):")
        for item in focused:
            if not isinstance(item, dict):
                continue
            lines.append(
                "    %-20s finding=%-10s reuse=%-10s marginal_cost=%s"
                % (_fmt(item.get("label")),
                   _fmt(item.get("triggering_finding")),
                   _fmt(item.get("reuse_decision")),
                   _fmt(item.get("marginal_cost"))))
            if item.get("invalidation_reason"):
                lines.append("      invalidated because: %s"
                             % item.get("invalidation_reason"))
    # ORCH-050/CV-050: EVERY transaction with its review disposition, then the
    # incurred-vs-accepted cost split — all figures read from the record.
    transactions = _at(record, "owned_verification.transactions", [])
    if isinstance(transactions, list) and transactions:
        lines.append("")
        lines.append("  Transactions (all, oldest first):")
        for transaction in transactions:
            if not isinstance(transaction, dict):
                continue
            line = ("    %-14s verdict=%-11s disposition=%s"
                    % (str(transaction.get("transaction_id"))[:14],
                       _fmt(transaction.get("verdict")),
                       _fmt(transaction.get("disposition"))))
            if transaction.get("review_round") is not None:
                line += "  round=%s" % _fmt(transaction.get("review_round"))
            reviewed = transaction.get("reviewed_manifest_digest")
            if reviewed:
                line += "  reviewed_manifest=%s" % str(reviewed)[:12]
            lines.append(line)
    incurred = _at(record, "owned_verification.incurred_cost", {}) or {}
    accepted = _at(record, "owned_verification.accepted_cost", {}) or {}
    lines.append("  incurred verification cost (ALL transactions): %s work "
                 "item(s), subprocess wall %s"
                 % (_fmt(incurred.get("work_items")),
                    _fmt_ms(_secs_to_ms(
                        incurred.get("subprocess_wall_time_s")))))
    lines.append("  accepted verification cost (accepted dispositions only): "
                 "%s work item(s), subprocess wall %s"
                 % (_fmt(accepted.get("work_items")),
                    _fmt_ms(_secs_to_ms(
                        accepted.get("subprocess_wall_time_s")))))
    avoided = _at(record, "owned_verification.avoided_cost", {}) or {}
    if isinstance(avoided, dict) and avoided.get("reuse_count"):
        reused_bits = []
        for item in avoided.get("reused") or []:
            if isinstance(item, dict):
                reused_bits.append("%s x%s"
                                   % (str(item.get("transaction_id"))[:12],
                                      _fmt(item.get("reuse_count"))))
        lines.append("  avoided cost via single-flight reuse: %s reuse(s), "
                     "~subprocess wall %s avoided (reused: %s)"
                     % (_fmt(avoided.get("reuse_count")),
                        _fmt_ms(_secs_to_ms(
                            avoided.get("subprocess_wall_time_s"))),
                        ", ".join(reused_bits) or "(unattributed)"))
    transaction_count = _at(record, "owned_verification.transaction_count", 0)
    if isinstance(transaction_count, int) and transaction_count > 1:
        lines.append("")
        lines.append("  %s owned transaction(s) recorded this session "
                     "(the latest is detailed above)." % _fmt(transaction_count))
    lines.append("")
    return lines


def _section_claims(record):
    """The builder's claims, joined to the controllers' logs."""
    claims = _at(record, "verification", [])
    if not isinstance(claims, list) or not claims:
        return []
    lines = ["Builder claims vs the controllers' logs", "-" * 56]
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        state = claim.get("claim_state") or UNKNOWN
        lines.append("  %-14s %-13s %s"
                     % (state, "ok" if claim.get("ok") else "FAILED",
                        _fmt(claim.get("label"))))
        if claim.get("purpose"):
            lines.append("        purpose: %s" % claim["purpose"])
        if claim.get("claim_reason"):
            lines.append("        %s" % claim["claim_reason"])
    self_reported = _at(record, "verification_summary.self_reported")
    contradicted = _at(record, "verification_summary.contradicted")
    lines.append("  %s self-reported (no log evidence), %s contradicted by "
                 "the log (both sides retained)."
                 % (_fmt(self_reported), _fmt(contradicted)))
    lines.append("")
    return lines


def _attempt_rollups(record):
    """Cross-attempt facts: tool activity, refusals, and repeated environment
    failures. These answer questions no single attempt can."""
    lines = []
    activity = _at(record, "tool_activity", {})
    if isinstance(activity, dict) and activity:
        lines.append("")
        lines.append("  Tool activity by role (content-free counts):")
        for role in sorted(activity):
            bucket = activity[role] or {}
            by_intent = bucket.get("by_intent") or {}
            lines.append("    %-16s %s call(s): %s"
                         % (role, _fmt(bucket.get("calls")),
                            ", ".join("%s=%s" % (k, v)
                                      for k, v in sorted(by_intent.items()))))
            if bucket.get("unrestored_mutations"):
                lines.append("      %s UNRESTORED mutation(s) — evidence "
                             "taken from this tree afterwards is refused"
                             % _fmt(bucket["unrestored_mutations"]))
            if bucket.get("repeated_targets"):
                lines.append("      %s target(s) touched more than once"
                             % _fmt(bucket["repeated_targets"]))
    recurrences = _at(record, "environment_recurrences", [])
    if isinstance(recurrences, list) and recurrences:
        lines.append("")
        lines.append("  Repeated environment failures (avoidable cost):")
        for item in recurrences:
            if isinstance(item, dict):
                lines.append("    %-40s %sx across %s role(s)"
                             % (_fmt(item.get("command_identity")),
                                _fmt(item.get("count")),
                                _fmt(item.get("roles"))))
    return lines


def _section_findings(record):
    lines = ["Findings", "-" * 56]
    lines.append("  total      %s" % _fmt(_at(record, "findings.total")))
    lines.append("  confirmed  %s" % _fmt(_at(record, "findings.confirmed")))
    lines.append("  withdrawn  %s  (kept as withdrawn, not erased)"
                 % _fmt(_at(record, "findings.withdrawn")))
    lines.append("  superseded %s" % _fmt(_at(record, "findings.superseded")))
    by_severity = _at(record, "findings.by_severity", {})
    if isinstance(by_severity, dict) and by_severity:
        lines.append("  by severity:")
        for key in sorted(by_severity):
            lines.append("    %-16s %s" % (key, by_severity[key]))
    lines.append("")
    return lines


def _section_marginal(record):
    """Marginal cost per finding, and outcome-adjusted calibration."""
    marginal = _at(record, "marginal_cost", {})
    lines = ["Marginal cost per finding", "-" * 56]
    if not isinstance(marginal, dict) or not marginal:
        lines.extend(["  (not computed for this session)", ""])
        return lines
    lines.append("  Basis: %s." % _fmt(marginal.get("basis")))
    lines.append("  From %s review/evaluation turn(s), %s."
                 % (_fmt(marginal.get("review_turns")),
                    _fmt_ms(marginal.get("review_duration_ms"))))
    per = marginal.get("per_disposition") or {}
    for disposition in sorted(per):
        bucket = per[disposition] or {}
        usage = bucket.get("usage")
        if isinstance(usage, dict) and usage:
            detail = ", ".join("%s=%s" % (k, v)
                               for k, v in sorted(usage.items()))
        else:
            detail = "unknown (%s)" % _fmt(bucket.get("reason"))
        lines.append("    %-18s n=%-4s %s"
                     % (disposition, _fmt(bucket.get("count")), detail))
    calibration = _at(record, "calibration", {})
    rounds = (calibration or {}).get("rounds") if isinstance(
        calibration, dict) else None
    lines.append("")
    lines.append("  Outcome-adjusted calibration (contemporaneous scores are "
                 "preserved, never overwritten):")
    if not rounds:
        lines.append("    (no rounds with both scores and finding outcomes)")
    else:
        for row in rounds:
            if not isinstance(row, dict):
                continue
            lines.append("    %-10s round %-3s %-14s scored %-6s -> adjusted "
                         "%-6s%s"
                         % (_fmt(row.get("phase")), _fmt(row.get("round")),
                            _fmt(row.get("evaluatee")),
                            _fmt(row.get("contemporaneous_average")),
                            _fmt(row.get("outcome_adjusted")),
                            ("  (%s)" % row["reason"]) if row.get("reason")
                            else ""))
    lines.append("")
    return lines


def _section_scores(record):
    lines = ["Score cohorts (homogeneous — never one pooled average)",
             "-" * 56]
    cohorts = _at(record, "score_cohorts", {})
    if not isinstance(cohorts, dict) or not cohorts:
        lines.extend(["  (no evaluations recorded)", ""])
        return lines
    for key in sorted(cohorts):
        bucket = cohorts[key] or {}
        average = bucket.get("average")
        extras = []
        for label in ("not_applicable", "insufficient_evidence",
                      "unverifiable"):
            if bucket.get(label):
                extras.append("%s=%s" % (label, bucket[label]))
        lines.append(
            "  %-10s %-14s %-14s %-22s avg %-7s n=%s%s"
            % (_fmt(bucket.get("phase")), _fmt(bucket.get("evaluatee")),
               _fmt(bucket.get("evaluatee_tool")),
               _fmt(bucket.get("criterion")), _fmt(average),
               _fmt(bucket.get("count")),
               ("  " + ", ".join(extras)) if extras else ""))
    lines.append("  Cohorts whose average is `unknown` have no numeric score "
                 "and are not ranked.")
    lines.append("")
    return lines


def _section_orchestrator_evaluations(record):
    """Targeted orchestrator-owned evaluations (`orchestrator_evaluations.*`).

    A PURE lookup section, kept clearly distinct from the peer score cohorts
    above. Returns `[]` when the record carries no such key (so a report for a
    session without this file is byte-identical to a pre-feature one). When the
    file was malformed the record carries `state=='malformed'`, and this renders
    a warning instead of scores. All averages are read straight from the record;
    no arithmetic happens here."""
    evaluations = _at(record, "orchestrator_evaluations", None)
    if not isinstance(evaluations, dict):
        return []
    state = evaluations.get("state")
    lines = ["Orchestrator evaluations (driver-owned — separate from peer "
             "scores)", "-" * 56]
    if state == "malformed":
        lines.append("  FILE MALFORMED — cannot render scores; the existing "
                     "orchestrator-evaluations.json is preserved for manual "
                     "inspection.")
        lines.append("")
        return lines
    if state != "ok":
        return []
    lines.append("  %s unique target(s) scored, %s total entr%s "
                 "(re-evaluations retained for audit; averages use the latest "
                 "entry per target)"
                 % (_fmt(_at(record,
                             "orchestrator_evaluations.current_target_count")),
                    _fmt(_at(record,
                             "orchestrator_evaluations.history_entry_count")),
                    "y" if _at(record, "orchestrator_evaluations."
                               "history_entry_count") == 1 else "ies"))
    for title, key in (("by role", "by_role"),
                       ("by controller", "by_controller"),
                       ("by model", "by_model")):
        bucket = evaluations.get(key)
        if not isinstance(bucket, dict) or not bucket:
            lines.append("  %s: (none recorded)" % title)
            continue
        lines.append("  %s:" % title)
        for name in sorted(bucket, key=lambda k: str(k)):
            row = bucket[name] or {}
            scores = ", ".join(
                "%s=%s" % (dimension, _fmt(row.get(dimension)))
                for dimension in ("output_quality", "intent_alignment",
                                  "evidence_quality", "self_sufficiency",
                                  "cost_worthiness"))
            lines.append("    %-16s n=%-4s %s"
                         % (name, _fmt(row.get("count")), scores))
    lines.append("")
    return lines


def _section_enhancements(record):
    lines = ["Enhancement suggestions (deduplicated)", "-" * 56]
    digest = _at(record, "enhancements", {})
    if not isinstance(digest, dict) or not digest:
        lines.extend(["  (none recorded)", ""])
        return lines
    for key in sorted(digest, key=lambda k: -(digest[k] or {}).get(
            "recurrences", 0)):
        bucket = digest[key] or {}
        lines.append("  %s  x%-3s %-12s from %s"
                     % (_fmt(bucket.get("digest")),
                        _fmt(bucket.get("recurrences")),
                        _fmt(bucket.get("disposition")),
                        ", ".join(bucket.get("sources") or [])))
    lines.append("")
    return lines


def _section_replay(record):
    rounds = _at(record, "replay", [])
    if not isinstance(rounds, list) or not rounds:
        return []
    lines = ["Replayed work (zero marginal value)", "-" * 56]
    for entry in rounds:
        if not isinstance(entry, dict):
            continue
        lines.append("  %-10s round %-3s  new findings %s, marginal value %s, "
                     "attributed to %s"
                     % (_fmt(entry.get("phase")), _fmt(entry.get("round")),
                        _fmt(entry.get("new_findings")),
                        _fmt(entry.get("marginal_value")),
                        _fmt(entry.get("attributed_to"))))
    lines.append("")
    return lines


def _section_pricing(record):
    lines = ["Pricing", "-" * 56]
    lines.append("  snapshot   %s (schema %s, captured %s)"
                 % (_fmt(_at(record, "pricing.snapshot_id")),
                    _fmt(_at(record, "pricing.schema_version")),
                    _fmt(_at(record, "pricing.captured_at"))))
    lines.append("  priced     %s turn(s)"
                 % _fmt(_at(record, "pricing.priced_turns")))
    lines.append("  unpriced   %s turn(s) — never rendered as 0, never ranked"
                 % _fmt(_at(record, "pricing.unpriced_turns")))
    lines.append("")
    return lines


def _section_trace(record):
    """Where prompt bytes concentrated. Read from `record.trace_summary`, which
    the BUILD side computed — the renderer only looks it up."""
    summary = _at(record, "trace_summary", {})
    lines = ["Prompt bytes", "-" * 56]
    if not isinstance(summary, dict) or not summary.get("turn_count"):
        lines.append("  No controller turns recorded in this trace.")
        lines.append("")
        return lines
    lines.append("  controller turns %s" % _fmt(summary.get("turn_count")))
    fresh = summary.get("fresh_resume") or {}
    lines.append("  fresh %s, resumed %s, unspecified %s"
                 % (_fmt(fresh.get("fresh")), _fmt(fresh.get("resume")),
                    _fmt(fresh.get("unknown"))))
    for title, key in (("by role + controller", "bytes_by_role_controller"),
                       ("by prompt kind", "bytes_by_kind"),
                       ("role/system prompts", "role_prompt_bytes")):
        bucket = summary.get(key)
        if not isinstance(bucket, dict) or not bucket:
            lines.append("  %s: (none recorded)" % title)
            continue
        lines.append("  %s:" % title)
        for name in sorted(bucket, key=lambda k: str(k)):
            value = bucket[name]
            if isinstance(value, dict):
                lines.append("    %-34s %s x%s"
                             % (name, _fmt_bytes(value.get("bytes")),
                                _fmt(value.get("launches"))))
            else:
                lines.append("    %-34s %s" % (name, _fmt_bytes(value)))
    largest = summary.get("largest_prompts")
    if isinstance(largest, list) and largest:
        lines.append("  largest single prompts:")
        for item in largest:
            if not isinstance(item, dict):
                continue
            round_note = ("" if item.get("round") is None
                          else " round %s" % item["round"])
            lines.append("    %-10s %-18s %-8s %s%s"
                         % (_fmt_bytes(item.get("bytes")),
                            _fmt(item.get("role")),
                            _fmt(item.get("controller")),
                            _fmt(item.get("kind")), round_note))
    artifacts = summary.get("artifact_bytes")
    if not isinstance(artifacts, dict) or not artifacts:
        lines.append("  artifact contribution: (no artifact descriptors "
                     "recorded)")
    else:
        lines.append("  artifact contribution (touched / embedded):")
        for path in sorted(artifacts, key=lambda k: -(
                artifacts[k] or {}).get("bytes", 0)):
            entry = artifacts[path] or {}
            lines.append("    %-10s %-10s x%-3s %s"
                         % (_fmt_bytes(entry.get("bytes")),
                            _fmt_bytes(entry.get("embedded")),
                            _fmt(entry.get("turns")), path))
    delivery = summary.get("delivery_breakdown")
    if not isinstance(delivery, dict) or not delivery:
        lines.append("  artifact delivery: (no artifact descriptors recorded)")
    else:
        lines.append("  artifact delivery:")
        for mode in sorted(delivery):
            entry = delivery[mode] or {}
            lines.append("    %-9s %3s sends, touched %-10s embedded %s"
                         % (mode, _fmt(entry.get("turns")),
                            _fmt_bytes(entry.get("touched")),
                            _fmt_bytes(entry.get("embedded"))))
    usage = summary.get("usage_by_controller")
    if not isinstance(usage, dict) or not usage:
        lines.append("  controller-reported usage: (none reported by the CLIs "
                     "this session)")
    else:
        lines.append("  controller-reported usage:")
        for controller in sorted(usage):
            fields = usage[controller] or {}
            lines.append("    %-9s %s"
                         % (controller,
                            ", ".join("%s=%s" % (k, v)
                                      for k, v in sorted(fields.items()))))
    by_role_model = summary.get("usage_by_role_model")
    if isinstance(by_role_model, dict) and by_role_model:
        lines.append("  turns + usage by role, tool and model:")
        for key in sorted(by_role_model, key=lambda k: str(k)):
            entry = by_role_model[key] or {}
            usage = entry.get("usage") or {}
            parts = ", ".join("%s=%s" % (k, v)
                              for k, v in sorted(usage.items()))
            # The record stores the composite key joined; render it as
            # `controller/model` so a tool+model combo reads as one identity.
            name = str(key).replace(" | ", "/")
            lines.append("    %-40s %3s turns%s"
                         % (name, _fmt(entry.get("turns")),
                            (", " + parts) if parts else ""))
    skips = summary.get("review_skips")
    if isinstance(skips, list):
        lines.append("  review-skip hits (hash-gate savings): %s"
                     % _fmt(summary.get("review_skip_count")))
        for skip in skips:
            if isinstance(skip, dict):
                lines.append("    %-18s %s" % (_fmt(skip.get("role")),
                                               _fmt(skip.get("reason"))))
    lines.append("")
    return lines


def _section_readiness(record):
    """Builder milestones and readiness honesty."""
    milestones = _at(record, "milestones", {})
    readiness = _at(record, "readiness", {})
    lines = ["Build telemetry", "-" * 56]
    if isinstance(milestones, dict) and milestones.get("events"):
        counts = milestones.get("by_phase") or {}
        lines.append("  milestones: %s"
                     % ", ".join("%s=%s" % (k, counts.get(k, 0))
                                 for k in sorted(counts)))
    else:
        lines.append("  milestones: (none recorded for this session)")
    claims = (readiness or {}).get("claims") if isinstance(
        readiness, dict) else None
    if claims:
        unverified = readiness.get("unverified") or 0
        lines.append("  readiness claims: %s, of which %s UNVERIFIED "
                     "(claimed against a tree that moved since verification)"
                     % (_fmt(readiness.get("total")), _fmt(unverified)))
        for claim in claims:
            if claim.get("state") == "unverified":
                lines.append("    %-12s round %-3s %s"
                             % (_fmt(claim.get("role")),
                                _fmt(claim.get("round")),
                                _fmt(claim.get("reason"))))
    else:
        lines.append("  readiness claims: (none recorded for this session)")
    lines.append("")
    return lines


def _section_evaluation_queue(record):
    """Each evaluation entry's FINAL DISPOSITION.

    Every figure is a scalar looked up at a declared path — the renderer does
    not count the members of `entries`, because a figure it derived is a figure
    that can disagree with the record it claims to be rendering.

    Held is deliberately not a failure and retired is deliberately not a
    completion: both are states work can legitimately end a session in, and
    collapsing either into "done" is what made a queue's real condition
    impossible to read.
    """
    by_state = _at(record, "evaluation_queue.by_state", {})
    lines = ["Evaluation queue dispositions", "-" * 56]
    if not isinstance(by_state, dict) or not by_state:
        lines.extend(["  (no evaluation queue recorded for this session)", ""])
        return lines
    read_state = _at(record, "evaluation_queue.read_state", None)
    if read_state == "unreadable":
        # The figures below are UNKNOWN, not zero. Saying so above them is what
        # stops a reader taking an unreadable queue for an empty one.
        lines.append("  The queue could not be read: these are UNKNOWN, not 0.")
    elif read_state == "missing":
        lines.append("  No queue file for this session — nothing was enqueued.")
    for state, note in (("pending", "awaiting scoring"),
                        ("attempting", "an attempt was recorded, no outcome"),
                        ("held", "held by policy or by you; not scored"),
                        ("drained", "scored successfully"),
                        ("retired", "superseded; never scored, not a failure"),
                        ("terminal", "budget spent; needs an explicit retry")):
        lines.append("  %-11s %-4s %s"
                     % (state, _fmt(_at(record,
                                        "evaluation_queue.by_state." + state)),
                        note))
    lines.append("")
    return lines


def _section_completion(record):
    entries = _at(record, "completion", [])
    lines = ["Completion account (authoritative — the build summary derives "
             "from this)", "-" * 56]
    if not isinstance(entries, list) or not entries:
        lines.extend(["  (not recorded for this session)", ""])
        return lines
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lines.append("  [%s] %s" % (_fmt(entry.get("state")),
                                    _fmt(entry.get("item"))))
        if entry.get("reason"):
            lines.append("        %s" % entry["reason"])
        for evidence in entry.get("evidence") or []:
            lines.append("        evidence: %s" % evidence)
    lines.append("")
    return lines


def _section_incomplete(record):
    entries = _at(record, "incomplete", [])
    pending = _at(record, "evaluation_queue.pending", 0)
    lines = ["What this record does NOT know", "-" * 56]
    if isinstance(pending, int) and pending:
        lines.append("  %d evaluation(s) still queued — reported as pending, "
                     "not as scored." % pending)
    if not isinstance(entries, list) or not entries:
        if not pending:
            lines.append("  (nothing flagged incomplete)")
        lines.append("")
        return lines
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lines.append("  %-42s %s" % (_fmt(entry.get("field")),
                                     _fmt(entry.get("reason"))))
    lines.append("")
    return lines


def render_provenance_banner(provenance):
    """Render the staleness banner printed ABOVE the report.

    Deliberately figure-free: source names, divergence state and a build
    timestamp only. Keeping every measurement figure out of this banner is what
    lets "the report computes nothing" stay literally true while a stale record
    still warns you.
    """
    if not isinstance(provenance, dict):
        return ""
    state = provenance.get("state") or UNKNOWN
    built_at = provenance.get("built_at") or UNKNOWN
    if state == "fresh":
        return ""
    if state == "stale":
        diverged = ", ".join(provenance.get("diverged") or []) or UNKNOWN
        return (
            "[stale record] Built %s. These raw sources have changed since: "
            "%s.\n             The figures below are the RECORD's, not "
            "recomputed ones. Use --rebuild to refresh.\n\n"
            % (built_at, diverged))
    detail = provenance.get("detail")
    return ("[provenance unknown] Built %s.%s\n\n"
            % (built_at, (" %s." % detail) if detail else ""))


# --------------------------------------------------------------------------- #
# Legacy score rendering, kept for the scores.json section.                   #
# --------------------------------------------------------------------------- #


def _section_scores_legacy(record):
    """The scores.json view, rendered from `record.scores_summary`.

    Every average here was computed at BUILD time and stored; this function
    looks values up and formats them. It used to read scores.json and compute
    after `render_report` had run, which put figures in the output that appeared
    nowhere in the authoritative record — the exact divergence D3 exists to
    prevent.
    """
    summary = _at(record, "scores_summary", {})
    if not isinstance(summary, dict) or not summary.get("entry_count"):
        return []
    lines = ["Evaluation scores (scores.json, legacy pooled view)", "-" * 56]
    lines.append("  The authoritative view is the score cohorts above; this "
                 "pooled view is kept for sessions that predate them.")
    received = summary.get("received") or {}
    if received:
        lines.append("  scores received, by evaluatee tool+model:")
        for key in sorted(received):
            bucket = received[key] or {}
            lines.append("    %-44s avg %-7s over %s criteria (%s evals)"
                         % (str(key).replace(" | ", " "),
                            _fmt(bucket.get("average")),
                            _fmt(bucket.get("score_count")),
                            _fmt(bucket.get("entries"))))
            for name in sorted(bucket.get("criteria") or {}):
                cb = bucket["criteria"][name]
                lines.append("        %-38s %-7s x%s"
                             % (name, _fmt(cb.get("average")),
                                _fmt(cb.get("count"))))
    cost = summary.get("eval_cost") or {}
    if cost:
        lines.append("  evaluation cost, by evaluator tool+model "
                     "(shared turns counted once):")
        for key in sorted(cost):
            bucket = cost[key] or {}
            usage = bucket.get("usage") or {}
            parts = ", ".join("%s=%s" % (k, v)
                              for k, v in sorted(usage.items()))
            lines.append("    %-44s %s turns, %s entries%s"
                         % (str(key).replace(" | ", " "),
                            _fmt(bucket.get("turns")),
                            _fmt(bucket.get("entries")),
                            (", " + parts) if parts else ""))
    verdicts = summary.get("score_by_verdict") or {}
    if verdicts:
        lines.append("  average score by reviewed verdict:")
        for verdict in sorted(verdicts):
            bucket = verdicts[verdict] or {}
            lines.append("    %-12s %-7s x%s"
                         % (verdict, _fmt(bucket.get("average")),
                            _fmt(bucket.get("count"))))
    lines.append("")
    return lines
