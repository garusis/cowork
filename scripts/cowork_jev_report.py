#!/usr/bin/env python3
"""Reports for the Jev observational pilot: metrics M1-M10, decision cascade,
immutable closed reports and dated supplements (protocol s8 and s9).

Everything here is a pure function of the pilot registry plus the ``now`` the
caller passes; no clock is read inside ``compute_report``. Metrics are labelled
``jev_obs M1..M10`` and are never added to ``cowork_measure`` cost classes or
its M1-M5 milestone vocabulary. An undefined quantity (division by zero, an
unknown charge) is the string ``not_computable`` and any cascade clause that
contains it is false.
"""

import datetime
import hashlib
import json
import math
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_jev_capture as cap  # noqa: E402
import cowork_jev_client as jc  # noqa: E402
import cowork_jev_observer as obs  # noqa: E402

NOT_COMPUTABLE = "not_computable"
REPORT_SCHEMA = "jev_obs_report.v1"
SUPPLEMENT_SCHEMA = "jev_obs_supplement.v1"
LABEL = "jev_obs"
ADJ_UNKNOWN_RESERVATION_TOKENS = obs.ADJ_ENVELOPE["tokens"]


def _frac(n, d):
    return {"n": n, "d": d, "value": (n / d) if d else NOT_COMPUTABLE}


def _val(frac):
    return frac["value"] if isinstance(frac, dict) else frac


def _le(x, limit):
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and x <= limit


def _ge(x, limit):
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and x >= limit


def _lt(x, limit):
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and x < limit


def _p95(values):
    if not values:
        return NOT_COMPUTABLE
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


# --------------------------------------------------------------------------- #
# Close.                                                                      #
# --------------------------------------------------------------------------- #


def effective_close(pilot, view, now):
    """The earliest close trigger known at ``now``: {trigger, close_seq,
    event_at} or None while the cohort is still open."""
    accepted = obs._accepted_lines(pilot)

    def seq_before(moment):
        seqs = [ln["acceptance_seq"] for ln in accepted
                if cap._parse_utc(ln.get("accepted_at")) is not None
                and cap._parse_utc(ln["accepted_at"]) < moment]
        return max(seqs) if seqs else 0

    events = []
    for stamp in view["stamps"]:
        when = cap._parse_utc(stamp.get("event_at"))
        if when is not None:
            events.append((when, stamp["trigger"], stamp["close_seq"],
                           stamp["event_at"]))
    for stop in view["hard_stops"]:
        # A stop with a recorded close_seq is already represented by its
        # serialized close stamp above; only legacy unstamped records fall
        # back to a conservative timestamp-derived boundary (strictly-before,
        # which can drop an equal-timestamp acceptance).
        if stop.get("close_seq") is not None:
            continue
        when = cap._parse_utc(stop.get("event_at"))
        if when is not None:
            events.append((when, "hard_stop", seq_before(when),
                           stop["event_at"]))
    deadline = pilot.deadline
    now_dt = cap._parse_utc(now)
    if now_dt is not None and now_dt >= deadline:
        events.append((deadline, "time_30d", seq_before(deadline),
                       deadline.strftime("%Y-%m-%dT%H:%M:%SZ")))
    if not events:
        return None
    events.sort(key=lambda e: (e[0], e[1]))
    _when, trigger, close_seq, event_at = events[0]
    return {"trigger": trigger, "close_seq": close_seq,
            "event_at": event_at}


# --------------------------------------------------------------------------- #
# Metrics.                                                                    #
# --------------------------------------------------------------------------- #


def cascade(inputs):
    """Decision cascade of protocol s9 over the decision inputs."""
    if inputs["H"]:
        return {"outcome": "STOP_OBSERVATION", "step": 1, "reason":
                "hard_stop", "S": None, "F": None, "X": None, "gaps": []}
    S = (_ge(inputs["N_comp"], 10) and _ge(inputs["A_det"], 8)
         and _ge(inputs["NA_det"], 8) and _le(inputs["U"], 0.40)
         and _le(inputs["SF"], 0.25))
    if not S:
        gaps = []
        for name, need in (("N_comp", 10), ("A_det", 8), ("NA_det", 8)):
            if inputs[name] < need:
                gaps.append({"quantity": name, "have": inputs[name],
                             "need": need, "missing": need - inputs[name]})
        for name, limit in (("U", 0.40), ("SF", 0.25)):
            if not isinstance(inputs[name], (int, float)):
                gaps.append({"quantity": name, "have": inputs[name],
                             "limit": limit, "missing": "not_computable"})
            elif inputs[name] > limit:
                gaps.append({"quantity": name, "have": inputs[name],
                             "limit": limit, "missing": "above_limit"})
        return {"outcome": "INSUFFICIENT_EVIDENCE", "step": 2, "reason":
                "sufficiency_not_met", "S": False, "F": None, "X": None,
                "gaps": gaps}
    F = (_ge(inputs["P"], 0.50) and inputs["D_add"] >= 3
         and _le(inputs["M4"], 0.20) and _le(inputs["SF"], 0.10)
         and _le(inputs["p95"], 60) and _le(inputs["JC"], 0.10))
    X = _lt(inputs["P"], 0.25) or inputs["D_add"] == 0
    if F:
        outcome, step = "RECOMMEND_ASSISTANCE", 3
    elif X:
        outcome, step = "STOP_OBSERVATION", 4
    else:
        outcome, step = "NO_RECOMMENDATION_CONTINUE", 5
    return {"outcome": outcome, "step": step, "reason": None, "S": True,
            "F": F, "X": X, "gaps": []}


def _attempt_costs(view):
    known, unknown, latencies = Decimal(0), [], []
    known_n = 0
    for attempt_id in sorted(view["attempts"]):
        parts = view["attempts"][attempt_id]
        outcome = parts.get("outcome")
        started = parts.get("started") or {}
        if outcome is None:
            unknown.append({"attempt_id": attempt_id,
                            "unit_id": started.get("unit_id"), "usd": None,
                            "reason": "pending"})
            continue
        cost = outcome["cost"]
        latency = outcome["response"].get("latency_ms")
        if isinstance(latency, int) and not isinstance(latency, bool):
            latencies.append(latency / 1000.0)
        if cost.get("charge_status") == "known" and cost.get("usd") is not \
                None:
            known += Decimal(str(cost["usd"]))
            known_n += 1
        else:
            unknown.append({"attempt_id": attempt_id,
                            "unit_id": outcome["unit_id"], "usd": None,
                            "reason": cost.get("unknown_reason")})
    return known, known_n, unknown, latencies


def compute_report(pilot, now, as_of=None):
    """Deterministic report for the cohort as recorded up to ``as_of``."""
    view = obs.build_view(pilot, as_of)
    close = effective_close(pilot, view, now)
    close_seq = close["close_seq"] if close else None

    in_window, outside = [], []
    for cand in view["candidates"]:
        if close_seq is not None and cand["acceptance_seq"] > close_seq:
            outside.append(cand)
        else:
            in_window.append(cand)
    eligible = [c for c in in_window if not c["excluded"]]
    excluded = [c for c in in_window if c["excluded"]]
    intervened = [c for c in eligible if c["intervened"]]
    included = [c for c in eligible if not c["intervened"]]
    included_ids = {c["candidate_id"] for c in included}

    # Candidate accounting (M10).
    captured_ids = {c["capture_id"] for c in view["candidates"]}
    excluded_reasons = {}
    for sess in view["sessions"]:
        if sess["state"] == "excluded" and \
                sess.get("capture_id") not in captured_ids:
            reason = sess.get("exclusion_reason") or "capture_error"
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
    for cand in excluded:
        excluded_reasons[cand["excluded"]] = excluded_reasons.get(
            cand["excluded"], 0) + 1
    duplicates = sum(1 for s in view["sessions"]
                     if s["state"] == "duplicate_ticket")
    outside_count = len(outside) + sum(
        1 for s in view["sessions"] if s["state"] == "outside_cohort_window")

    # Units (M7, M10).
    unit_states, queried_n = {}, 0
    abstain_reasons, failures, alerts_n, no_alert_n = {}, 0, 0, 0
    outside_denominator = {"dropped_cap": 0, "not_queried_cap": 0,
                           "not_applicable": 0,
                           obs.NOT_QUERIED_INTERRUPTED: 0, "pending": 0}
    for cand in included:
        for unit in cand["units"]:
            status = unit["status"] or "pending"
            if status == "abstain":
                status = "abstain:%s" % unit["abstain_reason"]
            unit_states[status] = unit_states.get(status, 0) + 1
            if unit["queried"]:
                queried_n += 1
                if unit["status"] == "service_failure":
                    failures += 1
                elif unit["status"] == "abstain":
                    abstain_reasons[unit["abstain_reason"]] = \
                        abstain_reasons.get(unit["abstain_reason"], 0) + 1
                elif unit["status"] == "alert":
                    alerts_n += 1
                elif unit["status"] == "no_alert":
                    no_alert_n += 1
            elif unit["status"] is None:
                outside_denominator["pending"] += 1
            elif unit["status"] == "abstain":
                outside_denominator["not_applicable"] += 1
            elif unit["status"] in outside_denominator:
                outside_denominator[unit["status"]] += 1

    # Adjudications (M1-M4).
    adjudicated = []
    for cand in included:
        for unit in cand["units"]:
            out = view["adjudications"].get(unit["unit_id"], {}).get(
                "outcome")
            if out:
                adjudicated.append((cand, unit, out["outcome"]))

    def count(stratum, outcome):
        return sum(1 for _c, u, o in adjudicated
                   if u["status"] == stratum and o == outcome)

    a_conf, a_ref = count("alert", "confirmed"), count("alert", "refuted")
    n_conf, n_ref = (count("no_alert", "confirmed"),
                     count("no_alert", "refuted"))
    a_unk, n_unk = count("alert", "unknown"), count("no_alert", "unknown")
    alert_cands = {}
    for cand, unit, outcome in adjudicated:
        if unit["status"] == "alert" and outcome in ("confirmed", "refuted"):
            alert_cands.setdefault(cand["candidate_id"], False)
            if outcome == "confirmed":
                alert_cands[cand["candidate_id"]] = True
    m1 = _frac(a_conf, a_conf + a_ref)
    m2 = _frac(sum(1 for v in alert_cands.values() if v), len(alert_cands))
    m3 = {"overall": _frac(a_unk + n_unk, len(adjudicated)),
          "alert": _frac(a_unk, a_conf + a_ref + a_unk),
          "no_alert": _frac(n_unk, n_conf + n_ref + n_unk)}
    m4 = _frac(n_conf, n_conf + n_ref)

    # Defects (M5, M6).
    defects = [d for d in obs.build_defect_records(pilot, view)
               if d["candidate_id"] in included_ids]
    comp = [c for c in included if c["comparator"] == "verifiable"]
    comp_ids = {c["candidate_id"] for c in comp}
    added = sorted({d["defect_key"] for d in defects
                    if d["origin"] == "introduced" and d["detected_by_jev"]
                    and d["additional_signal"]
                    and d["candidate_id"] in comp_ids})
    added_cands = {d["candidate_id"] for d in defects
                   if d["defect_key"] in added}
    m5 = {"D_add": len(added), "defect_keys": added,
          "candidates": _frac(len(added_cands), len(comp))}
    m6 = {origin: {"detected_by_jev": 0, "not_detected": 0}
          for origin in obs.ORIGINS}
    for d in defects:
        if d["additional_signal"] and d["candidate_id"] in comp_ids:
            m6[d["origin"]]["detected_by_jev" if d["detected_by_jev"]
                            else "not_detected"] += 1

    # Cost and wait (M8).
    known, known_n, unknown, latencies = _attempt_costs(view)
    queried_cands = [c for c in included
                     if any(u["queried"] for u in c["units"])]
    reservation = jc.RESERVATION_USD
    conservative = known + reservation * len(unknown)
    jc_value = (float(conservative) / len(queried_cands)
                if queried_cands else NOT_COMPUTABLE)
    adj_usage = {"tokens": 0, "tool_calls": 0, "minutes": 0}
    adj_known, adj_unknown, adj_pending = 0, [], []
    for unit_id in sorted(view["adjudications"]):
        adj = view["adjudications"][unit_id]
        out = adj.get("outcome")
        if not out:
            if "started" in adj:
                # In flight: the envelope is reserved, usage is not known.
                adj_pending.append({
                    "unit_id": unit_id, "usage": None,
                    "reserved_tokens": ADJ_UNKNOWN_RESERVATION_TOKENS,
                    "reason": "in_flight"})
            continue
        if out.get("charge_status") == "known":
            adj_known += 1
            for key in adj_usage:
                adj_usage[key] += out["usage"][key]
        else:
            adj_unknown.append({"unit_id": unit_id, "usage": None,
                                "reserved_tokens":
                                ADJ_UNKNOWN_RESERVATION_TOKENS,
                                "reason": "interrupted_unknown"})
    d_add = len(added)
    adj_exact = not adj_unknown and not adj_pending
    exact = not unknown and adj_exact
    m8 = {
        "jev_usd_known": float(known), "jev_attempts_known": known_n,
        "jev_attempts_unknown": unknown,
        "jev_usd_conservative": float(conservative),
        "jev_usd_per_candidate_queried": jc_value,
        "latency_p95_s": _p95(latencies),
        "cost_exact": exact,
        "adjudication": {"units_known": adj_known,
                         "units_unknown": adj_unknown,
                         "units_pending": adj_pending,
                         "retained_reservation_tokens":
                         ADJ_UNKNOWN_RESERVATION_TOKENS
                         * (len(adj_unknown) + len(adj_pending)),
                         "exact": adj_exact, "usage": adj_usage,
                         "per_unit": {k: (v / adj_known if adj_known
                                          else NOT_COMPUTABLE)
                                      for k, v in adj_usage.items()}},
        "cost_per_additional_defect": {
            "jev_usd": (float(known) / d_add
                        if d_add and exact else NOT_COMPUTABLE),
            "adjudication_tokens": (adj_usage["tokens"] / d_add
                                    if d_add and adj_exact
                                    else NOT_COMPUTABLE)},
        "instrumentation_ms": {
            "capture_total": sum(c["instrumentation_ms"] for c in eligible),
            "capture_max": max([c["instrumentation_ms"] for c in eligible]
                               or [0]),
            "review_total": sum(c["review_instrumentation_ms"]
                                for c in eligible),
            "review_max": max([c["review_instrumentation_ms"]
                               for c in eligible] or [0])}}

    m9 = _frac(len(comp), len(included))
    m7_den = queried_n
    m7 = {"queried": queried_n,
          "service_failure": _frac(failures, m7_den),
          "abstain": {r: _frac(n, m7_den)
                      for r, n in sorted(abstain_reasons.items())},
          "outside_denominator": dict(sorted(outside_denominator.items()))}

    inputs = {"N_comp": len(comp), "A_det": a_conf + a_ref,
              "NA_det": n_conf + n_ref, "U": _val(m3["overall"]),
              "SF": _val(m7["service_failure"]), "P": _val(m1),
              "M4": _val(m4), "D_add": d_add,
              "p95": m8["latency_p95_s"], "JC": jc_value,
              "H": bool(view["hard_stops"])}
    decision = cascade(inputs)

    pending = {
        "units": sorted(u["unit_id"] for c in eligible for u in c["units"]
                        if u["status"] is None),
        "attempts": sorted(u["attempt_id"] for u in unknown
                           if u["reason"] == "pending"),
        "candidates_awaiting_selection": sorted(
            c["candidate_id"] for c in included
            if c["candidate_id"] not in view["selections"]),
        "adjudications": sorted(
            uid for uid, adj in view["adjudications"].items()
            if "started" in adj and "outcome" not in adj),
        "selected_not_started": sorted(
            uid for sel in view["selections"].values()
            for uid in sel.get("selected", [])
            if "started" not in view["adjudications"].get(uid, {}))}

    return {
        "schema": REPORT_SCHEMA, "label": LABEL, "now": now,
        "as_of": as_of,
        "cohort": {"cohort_id": pilot.cohort_id,
                   "cohort_number": pilot.activation["cohort_number"],
                   "model": pilot.activation["model"],
                   "question_set_digest":
                   pilot.activation["question_set_digest"],
                   "thresholds": pilot.activation["thresholds"],
                   "activated_at": pilot.activation["activated_at"]},
        "trigger": close,
        "status": "closed" if close else "open",
        "case_ids": sorted(c["candidate_id"] for c in in_window),
        "metrics": {
            "M1": m1, "M2": m2, "M3": m3, "M4": m4, "M5": m5, "M6": m6,
            "M7": m7, "M8": m8, "M9": m9,
            "M10": {
                "candidates_eligible": len(eligible),
                "candidates_excluded": dict(sorted(
                    excluded_reasons.items())),
                "candidates_intervened": [
                    {"candidate_id": c["candidate_id"],
                     "reasons": c["intervention_reasons"]}
                    for c in intervened],
                "sessions_duplicate_ticket": duplicates,
                "candidates_outside_cohort_window": outside_count,
                "units_by_state": dict(sorted(unit_states.items())),
                "findings_unkeyed": sum(c["findings_unkeyed"]
                                        for c in included),
                "capture_verification_failed": sorted(
                    c["candidate_id"] for c in excluded
                    if c["excluded"] == "capture_verification_failed")}},
        "counts": {
            "candidates": {"eligible": len(eligible),
                           "excluded": sum(excluded_reasons.values()),
                           "intervened": len(intervened),
                           "duplicate": duplicates,
                           "queried": len(queried_cands)},
            "units_queried": queried_n, "alerts_investigated": a_conf
            + a_ref + a_unk, "adjudicated": len(adjudicated),
            "confirmed_defects": len({d["defect_key"] for d in defects}),
            "refutations": a_ref + n_ref, "unknowns": a_unk + n_unk,
            "failures": failures,
            "exclusions": sum(excluded_reasons.values())},
        "inputs": inputs, "decision": decision, "pending": pending,
        "defects": defects}


# --------------------------------------------------------------------------- #
# Rendering and immutable close.                                              #
# --------------------------------------------------------------------------- #


def to_json(report):
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False,
                      default=str) + "\n"


def _fmt(value):
    if isinstance(value, dict) and "n" in value and "d" in value:
        shown = value["value"]
        shown = "%.4f" % shown if isinstance(shown, float) else shown
        return "%s/%s = %s" % (value["n"], value["d"], shown)
    return json.dumps(value, sort_keys=True, default=str)


def render_text(report):
    metrics = report["metrics"]
    lines = ["%s report cohort %s (%s)" % (
        LABEL, report["cohort"]["cohort_number"], report["status"]),
        "model %s, question set %s" % (
            report["cohort"]["model"],
            report["cohort"]["question_set_digest"])]
    for name in ("M1", "M2", "M4", "M9"):
        lines.append("%s %s: %s" % (LABEL, name, _fmt(metrics[name])))
    lines.append("%s M3: overall %s, alert %s, no_alert %s" % (
        LABEL, _fmt(metrics["M3"]["overall"]), _fmt(metrics["M3"]["alert"]),
        _fmt(metrics["M3"]["no_alert"])))
    lines.append("%s M5: D_add %s, candidates %s" % (
        LABEL, metrics["M5"]["D_add"], _fmt(metrics["M5"]["candidates"])))
    lines.append("%s M6: %s" % (LABEL, _fmt(metrics["M6"])))
    lines.append("%s M7: queried %s, service_failure %s, abstain %s, "
                 "outside %s" % (
                     LABEL, metrics["M7"]["queried"],
                     _fmt(metrics["M7"]["service_failure"]),
                     _fmt(metrics["M7"]["abstain"]),
                     _fmt(metrics["M7"]["outside_denominator"])))
    m8 = metrics["M8"]
    lines.append("%s M8: Jev known usd %s plus %d unknown-charge attempt(s) "
                 "(usd null), conservative %s, p95 %s s, adjudication %s" % (
                     LABEL, m8["jev_usd_known"],
                     len(m8["jev_attempts_unknown"]),
                     m8["jev_usd_conservative"], m8["latency_p95_s"],
                     _fmt(m8["adjudication"]["usage"])))
    lines.append("%s M10: %s" % (LABEL, _fmt(metrics["M10"])))
    lines.append("inputs: %s" % _fmt(report["inputs"]))
    decision = report["decision"]
    lines.append("decision: %s (step %s)" % (decision["outcome"],
                                             decision["step"]))
    for gap in decision["gaps"]:
        lines.append("  gap: %s" % _fmt(gap))
    lines.append("pending: %s" % _fmt(report["pending"]))
    return "\n".join(lines) + "\n"


def _sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _report_paths(pilot):
    base = os.path.join(pilot.reports_dir, "cohort_%d_report"
                        % pilot.activation["cohort_number"])
    return base + ".json", base + ".txt", base + ".sha256"


def _create_exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def close_cohort(pilot, now):
    """Write the immutable closed report. A second call returns the existing
    paths without touching them."""
    os.makedirs(pilot.reports_dir, exist_ok=True)
    json_path, txt_path, sha_path = _report_paths(pilot)
    if os.path.exists(json_path):
        return {"created": False, "json": json_path, "text": txt_path}
    report = compute_report(pilot, now, as_of=now)
    report["closed_at"] = now
    if report["trigger"] is None:
        report["trigger"] = {"trigger": "manual", "close_seq": None,
                             "event_at": now}
    body = to_json(report)
    try:
        _create_exclusive(json_path, body)
    except FileExistsError:
        return {"created": False, "json": json_path, "text": txt_path}
    _create_exclusive(txt_path, render_text(report))
    _create_exclusive(sha_path, _sha(body) + "\n")
    return {"created": True, "json": json_path, "text": txt_path,
            "sha256": _sha(body)}


def load_closed_report(pilot):
    json_path = _report_paths(pilot)[0]
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_sidecar(pilot):
    try:
        with open(_report_paths(pilot)[2], "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _supplement_files(pilot):
    prefix = "cohort_%d_supplement_" % pilot.activation["cohort_number"]
    try:
        names = sorted(n for n in os.listdir(pilot.reports_dir)
                       if n.startswith(prefix) and n.endswith(".json"))
    except OSError:
        return []
    return [os.path.join(pilot.reports_dir, n) for n in names]


def write_supplement(pilot, now, regression_notes=()):
    """Dated supplement with only results that arrived after the close and are
    not in an earlier supplement. Never creates a case, never touches the
    closed report; writes nothing when there is nothing new."""
    closed = load_closed_report(pilot)
    if closed is None:
        return {"created": False, "reason": "not_closed"}
    closed_at = cap._parse_utc(closed["closed_at"])
    cases = set(closed["case_ids"])
    seen = set()
    for path in _supplement_files(pilot):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                seen.update(json.load(fh).get("item_keys", []))
        except (OSError, ValueError):
            continue
    view = obs.build_view(pilot)

    def late(stamp):
        when = cap._parse_utc(stamp)
        return when is not None and when > closed_at

    items = {}
    for cand in view["candidates"]:
        if cand["candidate_id"] not in cases:
            continue
        if cand["abandoned"] and late(cand["abandoned"].get("at")):
            items["abandoned:%s" % cand["candidate_id"]] = {
                "kind": "query_abandoned",
                "candidate_id": cand["candidate_id"]}
        for unit in cand["units"]:
            if unit["status"] is not None and late(unit.get("sealed_at")):
                items["unit:%s" % unit["unit_id"]] = {
                    "kind": "jev_unit_result", "unit_id": unit["unit_id"],
                    "candidate_id": cand["candidate_id"],
                    "status": unit["status"]}
    for unit_id, adj in view["adjudications"].items():
        out = adj.get("outcome")
        if out and out["candidate_id"] in cases and late(out["at"]):
            items["adjudication:%s" % unit_id] = {
                "kind": "adjudication", "unit_id": unit_id,
                "candidate_id": out["candidate_id"],
                "outcome": out["outcome"],
                "charge_status": out["charge_status"]}
    for attempt_id, parts in view["attempts"].items():
        outcome = parts.get("outcome")
        started = parts.get("started") or {}
        if outcome and late(outcome["at"]) and \
                started.get("candidate_id") in cases:
            items["attempt:%s" % attempt_id] = {
                "kind": "jev_attempt", "attempt_id": attempt_id,
                "candidate_id": started["candidate_id"],
                "unit_id": outcome["unit_id"],
                "charge_status": outcome["cost"]["charge_status"],
                "usd": outcome["cost"]["usd"]}
    observed_on = now[:10]
    for note in regression_notes:
        key = "regression:%s|%s" % (note["candidate_id"],
                                    _sha(note["note"]))
        if note["candidate_id"] in cases:
            items[key] = {"kind": "later_regression",
                          "candidate_id": note["candidate_id"],
                          "note": note["note"], "observed_on": observed_on}
    fresh = {k: v for k, v in items.items() if k not in seen}
    if not fresh:
        return {"created": False, "reason": "nothing_new"}
    base = "cohort_%d_supplement_%s" % (
        pilot.activation["cohort_number"], observed_on)
    path = os.path.join(pilot.reports_dir, base + ".json")
    suffix = 1
    while os.path.exists(path):
        suffix += 1
        path = os.path.join(pilot.reports_dir, "%s_%d.json" % (base, suffix))
    body = to_json({
        "schema": SUPPLEMENT_SCHEMA, "label": LABEL,
        "cohort_number": pilot.activation["cohort_number"],
        "closed_report_sha256": _read_sidecar(pilot),
        "written_at": now, "observed_on": observed_on,
        "item_keys": sorted(fresh),
        "items": [fresh[k] for k in sorted(fresh)]})
    _create_exclusive(path, body)
    return {"created": True, "path": path, "item_keys": sorted(fresh)}
