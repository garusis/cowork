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
    # M4 Package D: durable activity/watchdog facts -- see `_section_activity`.
    ("activity.work_id", "activity.work_id"),
    ("activity.class", "activity.activity_class"),
    ("activity.original_classification", "activity.original_classification"),
    ("activity.reconciled", "activity.reconciled"),
    ("activity.source", "activity.source"),
    ("activity.age_seconds", "activity.age_seconds"),
    ("activity.artifact_delta", "activity.artifact_delta"),
    ("activity.provider_health", "activity.provider_health"),
    ("activity.watchdog_verdict", "activity.watchdog_verdict"),
    ("activity.durable_evidence_ref", "activity.durable_evidence_ref"),
    ("activity.process_probe_ref", "activity.process_probe_ref"),
    ("activity.next_inspection_at", "activity.next_inspection_at"),
    ("activity.interval_seconds", "activity.interval_seconds"),
    # M7 Package B: universal context views, rendered by the trailing
    # `_section_*` functions after the activity section.
    ("context.limits_basis", "context.limits_basis"),
    ("context.dispatch_count", "context.dispatch_count"),
    ("context.unknown_metric_count", "context.unknown_metric_count"),
    ("context.by_role", "context.by_role"),
    ("context.incomplete", "context.incomplete"),
    ("profile_attribution.state", "profile_attribution.state"),
    ("profile_attribution.initial", "profile_attribution.initial"),
    ("profile_attribution.by_profile", "profile_attribution.by_profile"),
    ("profile_attribution.unknown_turns",
     "profile_attribution.unknown_turns"),
    ("profile_attribution.incomplete", "profile_attribution.incomplete"),
    ("repeated_context.deliveries", "repeated_context.deliveries"),
    ("repeated_context.repeated", "repeated_context.repeated"),
    ("repeated_context.repeated_bytes", "repeated_context.repeated_bytes"),
    ("repeated_context.commands", "repeated_context.commands"),
    ("repeated_context.reread_state", "repeated_context.reread_state"),
    ("repeated_context.incomplete", "repeated_context.incomplete"),
    ("cost_split.buckets", "cost_split.buckets"),
    ("cost_split.verification", "cost_split.verification"),
    ("cost_split.rework", "cost_split.rework"),
    ("cost_split.unmapped_classes", "cost_split.unmapped_classes"),
    ("cost_split.incomplete", "cost_split.incomplete"),
    ("recovery.state", "recovery.state"),
    ("recovery.episode_count", "recovery.episode_count"),
    ("recovery.invalid_episode_count", "recovery.invalid_episode_count"),
    ("recovery.recovery_turn_count", "recovery.recovery_turn_count"),
    ("recovery.episodes", "recovery.episodes"),
    ("recovery.value_by_state", "recovery.value_by_state"),
    ("recovery.unattributed_recovery_work_ids",
     "recovery.unattributed_recovery_work_ids"),
    ("recovery.incomplete", "recovery.incomplete"),
    ("lineage.state", "lineage.state"),
    ("lineage.source_session", "lineage.source_session"),
    ("lineage.replacement_session", "lineage.replacement_session"),
    ("lineage.reason", "lineage.reason"),
    ("lineage.start_role", "lineage.start_role"),
    ("lineage.imported_artifact_count", "lineage.imported_artifact_count"),
    ("lineage.unresolved_finding_count",
     "lineage.unresolved_finding_count"),
    ("lineage.unresolved_basis_reason", "lineage.unresolved_basis_reason"),
    ("lineage.reconciliation", "lineage.reconciliation"),
    ("lineage.closure", "lineage.closure"),
    ("lineage.cohort", "lineage.cohort"),
    ("lineage.incomplete", "lineage.incomplete"),
    ("owned.bound_reuse", "owned_verification.bound_reuse"),
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
    lines.extend(_section_execution_profile(record))
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
    lines.extend(_section_activity(record))
    lines.extend(_section_context(record))
    lines.extend(_section_profile_attribution(record))
    lines.extend(_section_repeated_context(record))
    lines.extend(_section_cost_split(record))
    lines.extend(_section_recovery(record))
    lines.extend(_section_lineage(record))
    lines.extend(_section_bound_reuse(record))
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
        lines.append("  user_wait comes from %s timed prompt span(s) recorded "
                     "by a legacy interactive session and is never inferred "
                     "from gaps between events." % _fmt(span_count))
    else:
        lines.append("  user_wait is UNKNOWN: this session recorded no wait "
                     "spans (agent-only runs have no interactive prompts; only "
                     "legacy sessions carry them). It is not 0 — inferring it "
                     "from gaps between events is forbidden.")
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
    suite = latest.get("suite")
    if isinstance(suite, dict):
        universe = suite.get("universe") or {}
        lines.append("  composed suite %s: components=%s members=%s "
                     "universe=%s granularity=%s"
                     % (_fmt(suite.get("suite_id")),
                        _fmt(cost.get("final_suite_component_count")),
                        _fmt(suite.get("member_count")),
                        _fmt(str(suite.get("universe_digest"))[:12]),
                        _fmt(suite.get("granularity"))))
        lines.append("    universe tests_dir=%s include=%s exclude=%s "
                     "split=%s"
                     % (_fmt(suite.get("tests_dir")),
                        ",".join(universe.get("include") or []) or "-",
                        ",".join(universe.get("exclude") or []) or "-",
                        ",".join(suite.get("split_modules") or []) or "-"))
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


def _section_execution_profile(record):
    """Execution profile (`execution_profile.*`): the selected and effective
    profile, every promotion with its reason codes, the deferred minor note
    count, and executed versus reused verification entries, for cohort
    comparison. A PURE lookup section: it returns `[]` when the record carries
    no such key, so a report for an unprofiled session is byte-identical to a
    pre-feature one, and every figure is read straight from the record."""
    profile = _at(record, "execution_profile", None)
    if not isinstance(profile, dict):
        return []
    lines = ["Execution profile", "-" * 56]
    lines.append("  selected=%s  effective=%s  policy_version=%s"
                 % (_fmt(_at(record, "execution_profile.selected")),
                    _fmt(_at(record, "execution_profile.effective")),
                    _fmt(_at(record, "execution_profile.policy_version"))))
    lines.append("  promotions: %s"
                 % _fmt(_at(record, "execution_profile.promotion_count")))
    history = profile.get("promotion_history")
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            codes = entry.get("reason_codes")
            lines.append("    #%s %s -> %s  reasons=%s  seam=%s"
                         % (_fmt(entry.get("seq")), _fmt(entry.get("from")),
                            _fmt(entry.get("to")),
                            ",".join(str(c) for c in codes)
                            if isinstance(codes, list) else _fmt(codes),
                            _fmt(entry.get("seam"))))
    lines.append("  deferred minor notes: %s"
                 % _fmt(_at(record,
                            "execution_profile.deferred_minor_note_count")))
    lines.append("  batch artifacts: %s"
                 % _fmt(_at(record,
                            "execution_profile.batch_artifact_count")))
    lines.append("  verification entries: executed=%s  reused=%s"
                 % (_fmt(_at(record,
                             "execution_profile.verification.executed")),
                    _fmt(_at(record,
                             "execution_profile.verification.reused"))))
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


# --------------------------------------------------------------------------- #
# M4 Package D: durable activity/watchdog facts (`record.activity`).          #
#                                                                              #
# `cowork_measure.build_record` (Package D's own additive grant there) always #
# sets `record["activity"]` -- either real, durably-sourced compact facts, or #
# a fixed all-UNKNOWN shape when no durable ActivityRecord exists for this    #
# session -- so this section, and every figure it prints, ALWAYS resolves     #
# (never a missing-key report line): the fixed-shape guarantee on the BUILD   #
# side is exactly what keeps this PURE lookup section total. Every value is   #
# read verbatim via `_at`, same as every other section in this module; this   #
# function computes nothing and reclassifies nothing.                        #
# --------------------------------------------------------------------------- #

def _section_activity(record):
    """The durable activity/watchdog facts for this session's most recently
    classified work engagement (`record.activity`) -- the SAME compact-fact
    vocabulary the run transcript shows (`cowork_transcript.
    render_activity`), built here purely
    from durable evidence: no live process probe exists once a session has
    ended, so a populated `watchdog_verdict` here is always `no_action` with
    null evidence refs -- never a fabricated post-hoc stall/progress claim.
    """
    lines = ["Activity (durable, cross-surface)", "-" * 56]
    activity = _at(record, "activity", {})
    if not isinstance(activity, dict) or not activity:
        lines.extend(["  (no durable activity recorded for this session)", ""])
        return lines
    lines.append("  work_id            %s" % _fmt(_at(record, "activity.work_id")))
    lines.append("  activity_class     %s" % _fmt(_at(record, "activity.activity_class")))
    lines.append("  original_class     %s" % _fmt(
        _at(record, "activity.original_classification")))
    lines.append("  reconciled         %s" % _fmt(_at(record, "activity.reconciled")))
    lines.append("  source             %s" % _fmt(_at(record, "activity.source")))
    lines.append("  age_seconds        %s" % _fmt(_at(record, "activity.age_seconds")))
    lines.append("  artifact_delta     %s" % _fmt(_at(record, "activity.artifact_delta")))
    lines.append("  provider_health    %s" % _fmt(_at(record, "activity.provider_health")))
    lines.append("  watchdog_verdict   %s" % _fmt(_at(record, "activity.watchdog_verdict")))
    lines.append("  durable_evidence   %s" % _fmt(
        _at(record, "activity.durable_evidence_ref")))
    lines.append("  process_probe      %s" % _fmt(
        _at(record, "activity.process_probe_ref")))
    lines.append("  next_inspection_at %s" % _fmt(
        _at(record, "activity.next_inspection_at")))
    lines.append("  interval_seconds   %s" % _fmt(
        _at(record, "activity.interval_seconds")))
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# M7 Package B: the universal context views. Each section is a pure lookup    #
# that returns `[]` when the record lacks its key, so a record built before    #
# these views existed renders exactly as it always did. Every count and total  #
# was computed in `cowork_measure`; nothing here adds anything up.             #
# --------------------------------------------------------------------------- #

_LIMITS_BASIS_TEXT = {"unprofiled": "none (unprofiled)",
                      "profiled": "profile envelope",
                      UNKNOWN: UNKNOWN}


def _fmt_metric(metric, value):
    if metric in ("prompt_bytes", "artifact_bytes"):
        return _fmt_bytes(value)
    if metric == "elapsed_ms":
        return _fmt_ms(value)
    return _fmt(value)


def _fmt_usage(value):
    if isinstance(value, dict):
        return ", ".join("%s=%s" % (key, _fmt(amount))
                         for key, amount in sorted(value.items()))
    return _fmt(value)


def _rollup_text(rollup):
    if not isinstance(rollup, dict):
        return UNKNOWN
    return ("turns=%s  usage=%s  duration=%s"
            % (_fmt(rollup.get("turns")), _fmt_usage(rollup.get("usage")),
               _fmt_ms(rollup.get("duration_ms"))))


def _note_lines(view):
    """One `unknown:` line per note a view recorded in its own `incomplete`."""
    notes = view.get("incomplete")
    out = []
    if isinstance(notes, list):
        for note in notes:
            if isinstance(note, dict):
                out.append("  unknown: %s - %s"
                           % (_fmt(note.get("field")),
                              _fmt(note.get("reason"))))
    return out


def _section_context(record):
    """Context envelope and consumption (`context.*`): per-dispatch size and
    consumption by role. Limits print as `none (unprofiled)` for a session with
    no profile; a metric with no data prints `unknown`, never 0."""
    view = _at(record, "context", None)
    if not isinstance(view, dict):
        return []
    lines = ["Context envelope and consumption", "-" * 56]
    basis = _at(record, "context.limits_basis")
    lines.append("  limits: %s" % (_LIMITS_BASIS_TEXT.get(basis, _fmt(basis))
                                   if isinstance(basis, str) else _fmt(basis)))
    lines.append("  dispatches: %s  metrics with no data: %s"
                 % (_fmt(_at(record, "context.dispatch_count")),
                    _fmt(_at(record, "context.unknown_metric_count"))))
    roles = _at(record, "context.by_role")
    if isinstance(roles, dict):
        for role, bucket in sorted(roles.items()):
            if not isinstance(bucket, dict):
                continue
            lines.append("  %s  dispatches=%s"
                         % (_fmt(role), _fmt(bucket.get("dispatches"))))
            maxima = bucket.get("max")
            unknowns = bucket.get("unknown_count")
            unknowns = unknowns if isinstance(unknowns, dict) else {}
            if isinstance(maxima, dict):
                for metric, value in maxima.items():
                    lines.append("    %-22s max=%s  no data=%s"
                                 % (metric, _fmt_metric(metric, value),
                                    _fmt(unknowns.get(metric))))
    lines.extend(_note_lines(view))
    lines.append("")
    return lines


def _section_profile_attribution(record):
    """Usage and duration per execution profile in force at each turn."""
    view = _at(record, "profile_attribution", None)
    if not isinstance(view, dict):
        return []
    lines = ["Profile attribution", "-" * 56]
    lines.append("  state=%s  initial profile=%s  turns with no clear profile=%s"
                 % (_fmt(_at(record, "profile_attribution.state")),
                    _fmt(_at(record, "profile_attribution.initial")),
                    _fmt(_at(record, "profile_attribution.unknown_turns"))))
    by_profile = _at(record, "profile_attribution.by_profile")
    if isinstance(by_profile, dict):
        for label, rollup in sorted(by_profile.items()):
            lines.append("  %-12s %s" % (_fmt(label), _rollup_text(rollup)))
    lines.extend(_note_lines(view))
    lines.append("")
    return lines


def _section_repeated_context(record):
    """Artifact bytes delivered again unchanged, and repeated command
    identities. Reread state is `detected` (read from controller logs after the
    fact) or `unknown`; nothing here claims a repeat was prevented."""
    view = _at(record, "repeated_context", None)
    if not isinstance(view, dict):
        return []
    lines = ["Repeated context", "-" * 56]
    deliveries = _at(record, "repeated_context.deliveries")
    if isinstance(deliveries, dict):
        lines.append("  deliveries: total=%s  first=%s  changed=%s  renewed=%s"
                     "  repeated=%s  unknown=%s"
                     % (_fmt(deliveries.get("total")),
                        _fmt(deliveries.get("first")),
                        _fmt(deliveries.get("changed")),
                        _fmt(deliveries.get("renewed")),
                        _fmt(deliveries.get("repeated")),
                        _fmt(deliveries.get("unknown"))))
    lines.append("  repeated bytes: %s"
                 % _fmt_bytes(_at(record, "repeated_context.repeated_bytes")))
    repeated = _at(record, "repeated_context.repeated")
    if isinstance(repeated, list):
        for item in repeated:
            if isinstance(item, dict):
                lines.append("    %s  %s  %s (was %s)"
                             % (_fmt(item.get("role")), _fmt(item.get("path")),
                                _fmt_bytes(item.get("bytes")),
                                _fmt(item.get("previous_work_id"))))
    lines.append("  reread state: %s"
                 % _fmt(_at(record, "repeated_context.reread_state")))
    commands = _at(record, "repeated_context.commands.by_role")
    if isinstance(commands, dict):
        for role, entry in sorted(commands.items()):
            if isinstance(entry, dict):
                lines.append("    %s  repeated commands=%s  (%s)"
                             % (_fmt(role), _fmt(entry.get("repeated_targets")),
                                _fmt(entry.get("state"))))
    lines.extend(_note_lines(view))
    lines.append("")
    return lines


def _section_cost_split(record):
    """Model cost split by purpose. The buckets are exclusive; verification is
    a separate unit (wall time, not model usage) and rework is an overlay that
    is not added into any total."""
    view = _at(record, "cost_split", None)
    if not isinstance(view, dict):
        return []
    lines = ["Cost split", "-" * 56]
    buckets = _at(record, "cost_split.buckets")
    if isinstance(buckets, dict):
        for name, rollup in buckets.items():
            lines.append("  %-15s %s" % (_fmt(name), _rollup_text(rollup)))
    lines.append("  verification (wall time, not model usage): items=%s  "
                 "wall=%s"
                 % (_fmt(_at(record, "cost_split.verification.work_items")),
                    _fmt_ms(_secs_to_ms(_at(
                        record,
                        "cost_split.verification.subprocess_wall_time_s")))))
    lines.append("  rework (overlay, not added to the buckets above): "
                 "turns=%s  usage=%s  duration=%s"
                 % (_fmt(_at(record, "cost_split.rework.turns")),
                    _fmt_usage(_at(record, "cost_split.rework.usage")),
                    _fmt_ms(_at(record, "cost_split.rework.duration_ms"))))
    unmapped = _at(record, "cost_split.unmapped_classes")
    if isinstance(unmapped, dict):
        for work_class, count in sorted(unmapped.items()):
            lines.append("  unmapped class %s: %s turn(s)"
                         % (_fmt(work_class), _fmt(count)))
    lines.extend(_note_lines(view))
    lines.append("")
    return lines


def _section_recovery(record):
    """Recovery episodes: what each recovery turn changed and earned. A missing
    record of episodes is `unknown`, never zero."""
    view = _at(record, "recovery", None)
    if not isinstance(view, dict):
        return []
    lines = ["Recovery episodes", "-" * 56]
    lines.append("  state=%s  episodes=%s  invalid=%s  recovery turns=%s"
                 % (_fmt(_at(record, "recovery.state")),
                    _fmt(_at(record, "recovery.episode_count")),
                    _fmt(_at(record, "recovery.invalid_episode_count")),
                    _fmt(_at(record, "recovery.recovery_turn_count"))))
    episodes = _at(record, "recovery.episodes")
    if isinstance(episodes, list):
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            lines.append("    %s -> %s  reason=%s  artifacts=%s  findings=%s"
                         "  value=%s  overhead=%s"
                         % (_fmt(episode.get("failed_work_id")),
                            _fmt(episode.get("recovery_work_id")),
                            _fmt(episode.get("reason_class")),
                            _fmt(episode.get("artifact_delta_state")),
                            _fmt(episode.get("finding_delta_state")),
                            _fmt(episode.get("value_state")),
                            _fmt(episode.get("recovery_overhead"))))
            lines.append("      findings: new=%s  closed=%s  retired=%s"
                         "  reread=%s"
                         % (_fmt(episode.get("new_finding_count")),
                            _fmt(episode.get("closed_finding_count")),
                            _fmt(episode.get("retired_finding_count")),
                            _fmt(episode.get("reread_state"))))
    by_value = _at(record, "recovery.value_by_state")
    if isinstance(by_value, dict):
        lines.append("  value: zero=%s  positive=%s  unknown=%s"
                     % (_fmt(by_value.get("zero")),
                        _fmt(by_value.get("positive")),
                        _fmt(by_value.get("unknown"))))
    unattributed = _at(record, "recovery.unattributed_recovery_work_ids")
    if isinstance(unattributed, list):
        for work_id in unattributed:
            lines.append("  recovery turn with no episode: %s" % _fmt(work_id))
    lines.extend(_note_lines(view))
    lines.append("")
    return lines


def _section_lineage(record):
    """Lineage of an imported session: preserved, repeated and new work, the
    closure value of imported findings, and cohort comparability."""
    view = _at(record, "lineage", None)
    if not isinstance(view, dict):
        return []
    lines = ["Lineage", "-" * 56]
    state = _at(record, "lineage.state")
    lines.append("  state: %s" % _fmt(state))
    if state == "none":
        lines.append("  (not an imported session)")
        lines.append("")
        return lines
    lines.append("  source=%s  replacement=%s  start role=%s"
                 % (_fmt(_at(record, "lineage.source_session")),
                    _fmt(_at(record, "lineage.replacement_session")),
                    _fmt(_at(record, "lineage.start_role"))))
    lines.append("  reason=%s  imported artifacts=%s  unresolved findings=%s"
                 "  basis=%s"
                 % (_fmt(_at(record, "lineage.reason")),
                    _fmt(_at(record, "lineage.imported_artifact_count")),
                    _fmt(_at(record, "lineage.unresolved_finding_count")),
                    _fmt(_at(record, "lineage.unresolved_basis_reason"))))
    reconciliation = _at(record, "lineage.reconciliation")
    if isinstance(reconciliation, dict):
        lines.append("  work: preserved=%s  repeated=%s  new=%s  unknown=%s"
                     "  duplicates skipped=%s"
                     % (_fmt(reconciliation.get("preserved_count")),
                        _fmt(reconciliation.get("repeated_count")),
                        _fmt(reconciliation.get("new_count")),
                        _fmt(reconciliation.get("unknown_count")),
                        _fmt(reconciliation.get("duplicates_skipped_count"))))
        totals = reconciliation.get("totals")
        if isinstance(totals, dict):
            for name in ("preserved", "repeated", "new"):
                lines.append("    %-10s %s"
                             % (name, _rollup_text(totals.get(name))))
    closure = _at(record, "lineage.closure")
    if isinstance(closure, dict):
        lines.append("  closure: closed=%s  new findings=%s  replayed=%s"
                     "  replay earned=%s  rejected=%s"
                     % (_fmt(closure.get("closures")),
                        _fmt(closure.get("new_findings")),
                        _fmt(closure.get("replay_findings")),
                        _fmt(closure.get("replay_earned")),
                        _fmt(closure.get("rejected_count"))))
    cohort = _at(record, "lineage.cohort")
    if isinstance(cohort, dict):
        lines.append("  cohort comparable: %s  code=%s"
                     % (_fmt(cohort.get("comparable")),
                        _fmt(cohort.get("code"))))
    lines.extend(_note_lines(view))
    lines.append("")
    return lines


def _section_bound_reuse(record):
    """Bound verification reuse, kept apart from executed transactions."""
    reuse = _at(record, "owned_verification.bound_reuse", None)
    if not isinstance(reuse, dict):
        return []
    lines = ["Bound verification reuse", "-" * 56]
    lines.append("  bound reuses: %s  avoided wall time: %s"
                 % (_fmt(_at(record, "owned_verification.bound_reuse.count")),
                    _fmt_ms(_secs_to_ms(_at(
                        record, "owned_verification.bound_reuse."
                                "avoided_subprocess_wall_time_s")))))
    rows = _at(record, "owned_verification.bound_reuse.by_transaction")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                lines.append("    %s  reused=%s  wall=%s"
                             % (_fmt(row.get("transaction_id")),
                                _fmt(row.get("bound_count")),
                                _fmt_ms(_secs_to_ms(
                                    row.get("subprocess_wall_time_s")))))
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Issue #64 P4: the read-only SESSION OWNER block.                            #
#                                                                              #
# This renderer takes an `owner_status_view` PROJECTION -- never a measurement #
# record, never a session uuid it would have to read a lease for, and never a  #
# path. It imports nothing new: in particular it never imports cowork_owner or #
# cowork_state, so the "this module computes nothing and reads nothing"        #
# boundary the whole file rests on stays literally true for the owner block    #
# too.                                                                         #
#                                                                              #
# It is deliberately the ONE renderer both P4 surfaces use -- the always-on    #
# `cowork --session-owner` query and the existence-gated block `run_report`    #
# writes above the provenance banner. A second, gate-local "unowned" rendering #
# is exactly how the two surfaces would drift into disagreeing about what an   #
# unowned session looks like, so `view=None` renders what a real               #
# `verdict: unowned` view renders, byte for byte.                              #
#                                                                              #
# Nothing here appears in `LINEAGE` and nothing here enters the measurement    #
# record: an owner lease is LIVE state observed at print time, not a measured  #
# figure, and letting one into the record would make its "every figure below   #
# is a field in it" contract false.                                            #
# --------------------------------------------------------------------------- #


def render_owner_status(view, session_file=None):
    """Render the read-only single-writer OWNER block (issue #64 P4).

    `view` is a `cowork_owner.owner_status_view` projection, or None meaning
    "no lease record exists on disk". None renders exactly what a real
    `verdict: unowned` view renders — the two P4 surfaces cannot drift.

    `session_file` is used for ONE thing: printing the exact
    `cowork --session-file <path> --take-over` recovery command, which no view
    carries a path for on its own. With no path known it degrades to a bare
    `cowork --take-over`, matching `cowork_owner.refusal_message`. It is never
    a figure and never reaches `render_report`, whose signature is untouched.

    This function REPORTS. It acquires nothing, reads nothing, and never
    raises for any shape of view it is handed.
    """
    return "\n".join(_section_owner_status(view, session_file)) + "\n"


def _section_owner_status(view, session_file=None):
    """The owner block's line list, in this module's `_section_*` house style.

    TOTAL by construction: every value goes through `_fmt`, so a field the
    projection could not compute prints `unknown` rather than raising. A status
    surface that crashed would be strictly worse than one that says "unknown",
    which is the same reason `owner_status_view` itself never raises for
    anything it finds on disk.

    Two wordings are decided rather than incidental:

      - `reason` is always a token from the closed vocabulary — one of the five
        `cowork_owner.OWNER_VERDICTS`, plus `foreign_host` appended when the
        lease is held on another host. Never free prose, never a sixth verdict.
      - the `corrupt` recovery line NEVER advertises a takeover. A takeover
        refuses an unreadable lease (`acquire_owner_lease` raises
        `OwnerLeaseCorrupt` before it can acquire), so pointing an operator at
        one would be a false recovery — the precise failure P4 exists to
        remove.
    """
    lines = ["Session owner (single-writer lease)", "-" * 56]
    verdict = (view or {}).get("verdict") if isinstance(view, dict) else None
    if not isinstance(view, dict) or verdict == "unowned" or not verdict:
        # The gated path (no lease record on disk) and the real `unowned`
        # verdict render identically, on purpose.
        lines.append("  state              unowned")
        lines.append("  reason             unowned")
        lines.append("  recovery           none needed — no cowork process "
                     "holds this session")
        lines.append("")
        return lines

    lease = view.get("lease") if isinstance(view.get("lease"), dict) else {}
    host_matches = view.get("host_matches")
    heartbeat = view.get("heartbeat_age_s")
    foreign = host_matches is False
    takeover = ("cowork%s --take-over"
                % (" --session-file %s" % session_file if session_file else ""))

    lines.append("  state              %s" % _fmt(verdict))
    lines.append("  owner              %s (epoch %s)"
                 % (_fmt(lease.get("owner_id")), _fmt(lease.get("epoch"))))
    lines.append("  process            pid %s on host %s (%s)"
                 % (_fmt(lease.get("pid")), _fmt(lease.get("host_id")),
                    "another host" if foreign
                    else ("this host" if host_matches else UNKNOWN)))
    lines.append("  launched           %s" % _fmt(lease.get("launch_dir")))
    lines.append("  since              %s" % _fmt(lease.get("acquired_at")))
    lines.append("  heartbeat          %s"
                 % ("%ds ago" % int(heartbeat)
                    if isinstance(heartbeat, (int, float))
                    and not isinstance(heartbeat, bool) else UNKNOWN))
    lines.append("  deadline           %s (expired %s)"
                 % (_fmt(view.get("lease_deadline_at")),
                    _fmt(view.get("expired"))))
    lines.append("  reason             %s%s"
                 % (verdict, " (foreign_host)" if foreign else ""))
    lines.append("  sidecar            %s"
                 % ("a terminal mark matches this lease"
                    if view.get("terminal_mark_matches")
                    else "no matching terminal mark"))
    if verdict == "corrupt":
        lines.append("  detail             %s" % _fmt(view.get("detail")))

    if verdict == "live_owner" and not foreign:
        recovery = ("stop that process, or take over with:  %s" % takeover)
    elif verdict == "live_owner":
        recovery = ("act on the owning host; a takeover from here is refused "
                    "(foreign_host)")
    elif verdict == "stale_dead_owner":
        recovery = ("the owner is provably dead — take over with:  %s"
                    % takeover)
    elif verdict == "stale_unproven":
        recovery = ("death is NOT proven — recover from the owning host, or "
                    "once the process is proved gone; recovery from here is "
                    "refused")
    else:
        recovery = ("the lease record for session %s is unreadable and a "
                    "takeover cannot repair it — inspect that record"
                    % _fmt(view.get("session_uuid")))
    lines.append("  recovery           %s" % recovery)
    lines.append("")
    return lines
