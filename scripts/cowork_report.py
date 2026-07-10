#!/usr/bin/env python3
"""Read-only token/byte report over a cowork session trace (#2).

Aggregates the content-free accounting written by #1 into a plain-text summary
of where prompt bytes concentrate in a cowork session, so optimization is
data-driven. Pure stdlib, no side effects, and tolerant of malformed/partial
trace lines (they are skipped, never raised) — a trace is an append-only log
that may be read mid-write.

The authoritative per-turn prompt bytes live on `controller.turn.start` events
(one per controller turn, carrying `prompt_bytes` plus the #1 accounting fields:
prompt_kind, role, controller, fresh/resume, round, context_revision, and the
artifact descriptors). Controller-reported usage, when present, rides on
`controller.turn.end`. Review-skip savings come from `review.skipped` events.
"""

import json


def _iter_events(source):
    """Yield parsed event dicts from `source` (a path, a file-like, or an
    iterable of lines/dicts). Malformed JSON lines are skipped silently."""
    if isinstance(source, str):
        try:
            with open(source, "r") as fh:
                for line in fh:
                    obj = _parse_line(line)
                    if obj is not None:
                        yield obj
        except OSError:
            return
        return
    for item in source:
        if isinstance(item, dict):
            yield item
            continue
        obj = _parse_line(item)
        if obj is not None:
            yield obj


def _parse_line(line):
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _as_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def summarize_trace(source):
    """Aggregate a trace into the report structure. `source` is a trace path, a
    file-like, or an iterable of JSON lines / event dicts."""
    bytes_by_role_controller = {}
    bytes_by_kind = {}
    fresh_resume = {"fresh": 0, "resume": 0, "unknown": 0}
    largest = []  # list of (bytes, role, controller, kind, round)
    # path -> {"bytes": touched, "embedded": embedded, "turns": int}
    artifact_bytes = {}
    # delivery -> {"turns", "embedded", "touched"} (#3): how artifacts were sent
    delivery_breakdown = {}
    # (role, delivery) -> {"bytes", "launches"} (#4): static role/system prompt
    role_prompt_bytes = {}
    review_skips = []  # list of {role, reason}
    usage_by_controller = {}  # controller -> {token field -> sum}
    # (role, controller, model) -> {"turns": n, "usage": {field -> sum}}:
    # consumption per tool+model combo, joinable against the scores each
    # combo received (see summarize_scores).
    usage_by_role_model = {}
    turn_count = 0

    for obj in _iter_events(source):
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
                # "Bytes actually embedded in the prompt" (#3): explicit when the
                # descriptor carries it; else the full body for an embedded
                # delivery, 0 for a path/diff reference (body not inlined).
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
            # Item #4: static role/system-prompt cost, separate from per-turn
            # user-message prompt_bytes. Tagged by delivery (claude_system vs
            # codex_inline).
            rp_role = obj.get("role") or "(unknown)"
            rp_delivery = obj.get("delivery") or "(unknown)"
            rp_key = (rp_role, rp_delivery)
            rp = role_prompt_bytes.setdefault(
                rp_key, {"bytes": 0, "launches": 0})
            rp["bytes"] += _as_int(obj.get("bytes"))
            rp["launches"] += 1
        elif name in ("controller.turn.end", "controller.probe.end"):
            # Controller-reported usage rides turn ends and probe ends alike
            # (#1 adds best-effort usage to the probe result); aggregate both.
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
        "usage_by_controller": usage_by_controller,
        "usage_by_role_model": usage_by_role_model,
    }


def _fmt_bytes(n):
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def render_report(summary, session_uuid=None):
    """Render the aggregated summary as a plain-text report."""
    lines = []
    head = "cowork session report"
    if session_uuid:
        head += " — %s" % session_uuid
    lines.append(head)
    lines.append("=" * len(head))
    lines.append("")

    if not summary.get("turn_count"):
        lines.append("No controller turns recorded in this trace.")
        return "\n".join(lines) + "\n"

    lines.append("Controller turns: %d" % summary["turn_count"])
    fr = summary["fresh_resume"]
    lines.append("Fresh vs. resumed turns: %d fresh, %d resumed, %d unspecified"
                 % (fr["fresh"], fr["resume"], fr["unknown"]))
    lines.append("")

    lines.append("Prompt bytes by role + controller:")
    rows = sorted(summary["bytes_by_role_controller"].items(),
                  key=lambda kv: kv[1], reverse=True)
    for (role, controller), total in rows:
        lines.append("  %-18s %-8s %s" % (role, controller, _fmt_bytes(total)))
    lines.append("")

    lines.append("Prompt bytes by prompt kind:")
    rows = sorted(summary["bytes_by_kind"].items(),
                  key=lambda kv: kv[1], reverse=True)
    for kind, total in rows:
        lines.append("  %-22s %s" % (kind, _fmt_bytes(total)))
    lines.append("")

    lines.append("Largest single prompts:")
    for pbytes, role, controller, kind, rnd in summary["largest_prompts"]:
        rtxt = (" round %s" % rnd) if rnd is not None else ""
        lines.append("  %-10s %-18s %-8s %s%s"
                     % (_fmt_bytes(pbytes), role, controller, kind, rtxt))
    lines.append("")

    lines.append("Artifact contribution by file (summed over turns sent):")
    lines.append("  (touched = full artifact size; embedded = bytes actually "
                 "inlined in the prompt)")
    rows = sorted(summary["artifact_bytes"].items(),
                  key=lambda kv: kv[1]["bytes"], reverse=True)
    if rows:
        for path, entry in rows:
            lines.append(
                "  touched %-10s embedded %-10s x%-3d %s"
                % (_fmt_bytes(entry["bytes"]),
                   _fmt_bytes(entry.get("embedded", 0)),
                   entry["turns"], path))
    else:
        lines.append("  (no artifact descriptors recorded)")
    lines.append("")

    delivery = summary.get("delivery_breakdown") or {}
    lines.append("Artifact delivery breakdown (how artifacts reached prompts):")
    if delivery:
        rows = sorted(delivery.items(),
                      key=lambda kv: kv[1]["embedded"], reverse=True)
        for mode, d in rows:
            lines.append(
                "  %-9s %3d sends, touched %-10s embedded %s"
                % (mode, d["turns"], _fmt_bytes(d["touched"]),
                   _fmt_bytes(d["embedded"])))
    else:
        lines.append("  (no artifact descriptors recorded)")
    lines.append("")

    role_prompt = summary.get("role_prompt_bytes") or {}
    lines.append("Role/system-prompt bytes by role (static, separate from "
                 "user-message bytes):")
    if role_prompt:
        rows = sorted(role_prompt.items(),
                      key=lambda kv: kv[1]["bytes"], reverse=True)
        for (role, mode), rp in rows:
            lines.append("  %-18s %-14s %-10s x%d"
                         % (role, mode, _fmt_bytes(rp["bytes"]),
                            rp["launches"]))
    else:
        lines.append("  (no role-prompt bytes recorded)")
    lines.append("")

    skips = summary["review_skips"]
    lines.append("Review-skip hits (hash-gate savings): %d" % len(skips))
    for skip in skips:
        lines.append("  %-18s %s" % (skip["role"], skip["reason"]))
    lines.append("")

    usage = summary["usage_by_controller"]
    lines.append("Controller-reported usage (where exposed):")
    if usage:
        for controller, fields in sorted(usage.items()):
            parts = ", ".join("%s=%d" % (k, v)
                              for k, v in sorted(fields.items()))
            lines.append("  %-8s %s" % (controller, parts))
    else:
        lines.append("  (none reported by the CLIs this session)")

    by_role_model = summary.get("usage_by_role_model") or {}
    if by_role_model:
        lines.append("")
        lines.append("Turns + usage by role, tool, and model:")
        for (role, controller, model), b in sorted(by_role_model.items()):
            ident = controller if model == "(unknown)" else (
                "%s/%s" % (controller, model))
            parts = ", ".join("%s=%d" % (k, v)
                              for k, v in sorted(b["usage"].items()))
            lines.append("  %-18s %-28s %3d turns%s"
                         % (role, ident, b["turns"],
                            (", %s" % parts) if parts else ""))

    return "\n".join(lines) + "\n"


def summarize_scores(scores):
    """Aggregate a scores.json dict (or path) into the evaluation-analysis
    structure. Tolerant: missing/malformed input yields empty aggregates.

    Three views over the schema-2 traceability stamps:
    - received: scores each (evaluatee, tool, model) combo received — the
      "which tool+model does better work" view (per-criterion averages).
    - eval_cost: tokens/duration each (evaluator, tool, model) combo spent
      producing evaluations. A shared turn (a round-1 consumed-upstream
      bundle rides the same send) is counted ONCE via its eval_turn_id.
    - score_by_verdict: average score grouped by the round's reviewed
      verdict, correlating scores with outcomes.
    Entries written before schema 2 lack the stamps and fold into the
    "(unknown)" identity buckets."""
    if isinstance(scores, str):
        try:
            with open(scores, "r") as fh:
                scores = json.load(fh)
        except (OSError, ValueError):
            scores = None
    entries = (scores or {}).get("evaluations")
    entries = entries if isinstance(entries, list) else []
    received = {}
    eval_cost = {}
    score_by_verdict = {}
    seen_turns = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        criteria = [c for c in entry.get("criteria") or []
                    if isinstance(c, dict)
                    and isinstance(c.get("score"), int)]
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
            continue  # shared turn: usage/duration already counted once
        if turn_id:
            seen_turns.add(turn_id)
        ebucket["turns"] += 1
        ebucket["duration_ms"] += _as_int(entry.get("duration_ms"))
        for field, val in (entry.get("usage") or {}).items():
            iv = _as_int(val)
            if iv:
                ebucket["usage"][field] = ebucket["usage"].get(field, 0) + iv
    return {
        "entry_count": len(entries),
        "received": received,
        "eval_cost": eval_cost,
        "score_by_verdict": score_by_verdict,
    }


def _fmt_identity(key):
    role, tool, model = key
    ident = tool if model == "(unknown)" else "%s/%s" % (tool, model)
    return "%-18s %s" % (role, ident)


def render_scores_report(summary):
    """Render the summarize_scores structure as a plain-text section."""
    lines = []
    head = "Evaluation scores (scores.json)"
    lines.append(head)
    lines.append("-" * len(head))
    if not summary.get("entry_count"):
        lines.append("  (no evaluations recorded this session)")
        return "\n".join(lines) + "\n"
    lines.append("Scores received, by evaluatee tool+model:")
    rows = sorted(summary["received"].items(),
                  key=lambda kv: -(kv[1]["score_total"]
                                   / kv[1]["score_count"])
                  if kv[1]["score_count"] else 0)
    for key, b in rows:
        avg = (b["score_total"] / b["score_count"]
               if b["score_count"] else 0.0)
        lines.append("  %s  avg %.2f over %d criteria (%d evals)"
                     % (_fmt_identity(key), avg, b["score_count"],
                        b["entries"]))
        for name, cb in sorted(b["criteria"].items()):
            lines.append("      %-38s %.2f x%d"
                         % (name, cb["total"] / cb["count"], cb["count"]))
    lines.append("")
    lines.append("Evaluation cost, by evaluator tool+model "
                 "(shared turns counted once):")
    for key, b in sorted(summary["eval_cost"].items()):
        parts = ", ".join("%s=%d" % (k, v)
                          for k, v in sorted(b["usage"].items()))
        lines.append("  %s  %d turns, %d entries%s%s"
                     % (_fmt_identity(key), b["turns"], b["entries"],
                        (", %s" % parts) if parts else "",
                        (", %.1fs" % (b["duration_ms"] / 1000.0))
                        if b["duration_ms"] else ""))
    verdicts = summary.get("score_by_verdict") or {}
    if verdicts:
        lines.append("")
        lines.append("Average score by reviewed verdict:")
        for verdict, vb in sorted(verdicts.items()):
            lines.append("  %-12s %.2f x%d"
                         % (verdict, vb["total"] / vb["count"], vb["count"]))
    return "\n".join(lines) + "\n"


def report_for_trace(trace_path, session_uuid=None):
    """Convenience: summarize + render a trace file in one call."""
    return render_report(summarize_trace(trace_path), session_uuid=session_uuid)


def report_for_scores(scores_path):
    """Convenience: summarize + render a scores.json file in one call."""
    return render_scores_report(summarize_scores(scores_path))
