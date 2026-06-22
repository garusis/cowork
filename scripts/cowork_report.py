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
    artifact_bytes = {}  # path -> {"bytes": int, "turns": int}
    review_skips = []  # list of {role, reason}
    usage_by_controller = {}  # controller -> {token field -> sum}
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
                entry = artifact_bytes.setdefault(path, {"bytes": 0, "turns": 0})
                entry["bytes"] += _as_int(art.get("bytes"))
                entry["turns"] += 1
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
        "review_skips": review_skips,
        "usage_by_controller": usage_by_controller,
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
    rows = sorted(summary["artifact_bytes"].items(),
                  key=lambda kv: kv[1]["bytes"], reverse=True)
    if rows:
        for path, entry in rows:
            lines.append("  %-10s x%-3d %s"
                         % (_fmt_bytes(entry["bytes"]), entry["turns"], path))
    else:
        lines.append("  (no artifact descriptors recorded)")
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

    return "\n".join(lines) + "\n"


def report_for_trace(trace_path, session_uuid=None):
    """Convenience: summarize + render a trace file in one call."""
    return render_report(summarize_trace(trace_path), session_uuid=session_uuid)
