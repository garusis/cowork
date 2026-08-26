#!/usr/bin/env python3
"""Dual-evidence watchdog decisions and scheduled-review reconciliation --
M4 Package D.

This module is the SOLE place a `cowork_activity.WatchdogDecision` is
constructed. It never certifies `soft_warning`/`hard_stall_eligible` off
elapsed time or an event tail alone: every terminal-leaning verdict is
reached ONLY by combining (a) validated durable evidence -- the CURRENT
effective `ActivityClass`, taken from `cowork_state.latest_activity`'s
`(activity_record, reconciliation_record)` pair -- with (b) a live
process/controller-health probe, reusing Package C's pinned
`cowork_bridge.live_child_handle(session)` UNCHANGED. Both legs are always
consulted; `decide()` has no code path that reaches a verdict from only one
of them, and every `soft_warning`/`hard_stall_eligible` decision this module
returns is passed through `cowork_activity.validate_watchdog_decision`
before being handed back, so a one-leg decision is rejected identically to
how a hand-built dict would be.

A live child (Claude's session-lifetime child included -- non-null the
entire time it is alive, torn down only by `close()`, never by a per-turn
`no_first_token` deadline) is real, positive evidence of progress: while
`live_child_handle` reports the child alive, this module NEVER returns a
terminal verdict, no matter how large `age_seconds`/how overdue the
scheduled review is. A quiet, alive, long-running turn is never mistaken
for a hard stall -- only a genuinely evidenced terminal `ActivityClass`
(`process_crash`, `hung_descendant`, `no_evidence_silence`) with a DEAD
probe can ever escalate.

`hung_descendant` additionally requires INDEPENDENT `ps`-based orphan/
zombie evidence (`independent_hung_descendant_evidence`), cross-referenced
against Package C's own reap evidence (the classified `hung_descendant`
ActivityClass itself, derived from the bounded first-token-deadline SIGTERM/
SIGKILL/reap sequence in `cowork_bridge.py`). Package C's classification
alone, with no independent OS-level confirmation, degrades to
`soft_warning` -- never a silent promotion to a hard-stall certification.

`next_inspection_at` (`cowork_activity.ScheduledReviewRecord`, minted and
owned by Package B) is the sole authoritative source for whether a
scheduled review is due (`review_due`); this module never recomputes
"elapsed since last event" as a substitute.

Python 3.9+, stdlib only, plus the sibling `cowork_activity`/`cowork_bridge`
modules.
"""

import subprocess

import cowork_activity as activity_contracts
import cowork_bridge as bridge

# The three ActivityClass members that may ever justify a terminal
# (soft_warning/hard_stall_eligible) verdict -- every other class (including
# `provider_wait`, `productive_model_work`, `local_tool_work`,
# `owned_verification`, `policy_denial`) is real, positive or benign
# evidence and is always `no_action`, regardless of age or probe state.
TERMINAL_ACTIVITY_CLASSES = frozenset({
    "process_crash", "hung_descendant", "no_evidence_silence"})


def process_probe(session):
    """Truthful live process/controller-health probe for `session`.

    Reuses Package C's pinned `cowork_bridge.live_child_handle(session)`
    UNCHANGED -- non-null whenever ANY controller child is alive, including
    Claude's session-lifetime child and Codex's/OpenCode's per-turn
    handles; this module never re-derives process liveness any other way,
    so Codex and OpenCode are probed exactly as truthfully as Claude.

    Returns `(alive, process_probe_ref)`: `alive` is a plain bool;
    `process_probe_ref` is always a nonempty, non-null descriptive string
    (`"pid:<n>"` when alive, `"dead:no_live_child"` when not) -- the
    dual-evidence law requires a non-null `process_probe_ref` on every
    terminal verdict, and this function's return is honest either way: a
    dead probe is still a probe that was genuinely consulted, never a
    silently-omitted one.
    """
    proc = bridge.live_child_handle(session)
    if proc is None:
        return False, "dead:no_live_child"
    pid = getattr(proc, "pid", None)
    return True, ("pid:%s" % pid if pid is not None else "pid:unknown")


def _real_ps(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    return result.stdout


def independent_hung_descendant_evidence(pid, ps_runner=None):
    """Independent OS-level (`ps`) confirmation that `pid` is a genuine
    orphan/zombie descendant, cross-referenced against -- never substituted
    for -- Package C's own reap evidence (the `hung_descendant`
    ActivityClass).

    Returns a nonempty evidence string when `ps` reports `pid` alive with
    `ppid == 1` (reparented -- its original parent is gone) or a
    zombie/stopped state (`Z`/`T` in `stat`), else `None` -- "no
    independent evidence", never an exception. `ps_runner(argv) -> str`
    defaults to a real, bounded (`timeout=5`) `ps` invocation; tests inject
    a fake so this module never actually shells out under test.

    Total: any `OSError`/`subprocess.SubprocessError`/malformed-output
    case is `None`, never a raised exception reaching the caller -- a
    watchdog probe must never crash the turn it is observing.
    """
    if not pid:
        return None
    runner = ps_runner or _real_ps
    try:
        output = runner(["ps", "-o", "pid=,ppid=,stat=", "-p", str(pid)])
    except Exception:  # noqa: BLE001 -- total: a probe failure is "no evidence".
        return None
    if not isinstance(output, str):
        return None
    line = output.strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) < 3:
        return None
    found_pid, ppid, stat = parts[0], parts[1], parts[2]
    if found_pid != str(pid):
        return None
    if ppid == "1" or "Z" in stat or "T" in stat:
        return "ps:pid=%s,ppid=%s,stat=%s" % (found_pid, ppid, stat)
    return None


def review_due(schedule_record, now):
    """Whether a scheduled review is due, per Package B's sole authoritative
    `next_inspection_at` -- NEVER a recomputed "elapsed since last event".

    Returns `None` (genuinely unknown -- no schedule exists yet) when
    `schedule_record` is `None`; otherwise a plain bool, `now >=
    next_inspection_at`, both RFC3339 strings compared lexicographically
    (safe for same-timezone/UTC-normalized RFC3339 strings, exactly like
    every other timestamp comparison already made in this repository's
    JSON-native records)."""
    if schedule_record is None:
        return None
    return now >= schedule_record["next_inspection_at"]


def _durable_evidence_ref(work_id, activity_record, reconciliation_record):
    if reconciliation_record is not None:
        return "reconciliation:%s@%s" % (work_id, reconciliation_record["time"])
    return "activity:%s@%s" % (work_id, activity_record["time"])


def decide(work_id, now, activity_record, reconciliation_record,
           schedule_record, session=None, hung_ps_evidence=None):
    """The sole dual-evidence `WatchdogDecision` constructor.

    `activity_record`/`reconciliation_record` are the exact `(activity_
    record, reconciliation_record)` pair `cowork_state.latest_activity`
    returns (`reconciliation_record` may be `None`); `schedule_record` is
    `cowork_state.read_next_inspection`'s return (`None` if never
    scheduled). `session`, when given, is probed via `process_probe`
    above; omitting it (`session=None`) is truthfully treated as "no live
    child" (`alive=False`), never as "unknown" -- a caller with no session
    object genuinely has no live-process evidence to offer.

    `hung_ps_evidence`, when given, is the caller's own already-collected
    `independent_hung_descendant_evidence(...)` result for the live/last-
    known descendant pid -- this function never shells out itself (no I/O
    beyond `process_probe`'s already-cheap `proc.poll()`), keeping `decide`
    a pure decision function over already-gathered evidence.

    Verdict rule (see the module docstring for the full rationale):
      - a genuinely ALIVE probe -> always `no_action` (silence while alive
        is never terminal evidence);
      - a dead probe but a non-terminal effective ActivityClass (
        `provider_wait`/`productive_model_work`/`local_tool_work`/
        `owned_verification`/`policy_denial`) -> `no_action` (a policy
        denial or a completed tool call is not a stall signal);
      - a dead probe, effective class `hung_descendant`, WITH independent
        `ps` evidence -> `hard_stall_eligible`;
      - a dead probe, effective class `hung_descendant`, with NO
        independent `ps` evidence -> `soft_warning` (Package C's
        classification alone is a real signal, but not yet cross-
        referenced);
      - a dead probe, effective class `process_crash`/`no_evidence_
        silence` -> `hard_stall_eligible` when the scheduled review is
        overdue (`review_due`), else `soft_warning`.

    Returns the `cowork_activity.validate_watchdog_decision`-normalized
    dict; raises `ValueError` only if the constructed record itself is
    somehow schema-invalid (never reachable via the branches above, since
    every terminal branch supplies both evidence refs)."""
    effective_class = (
        reconciliation_record["reconciled_classification"]
        if reconciliation_record is not None
        else activity_record["activity_class"])
    if session is not None:
        alive, process_probe_ref = process_probe(session)
    else:
        alive, process_probe_ref = False, "dead:no_live_child"

    durable_evidence_ref = _durable_evidence_ref(
        work_id, activity_record, reconciliation_record)

    if alive or effective_class not in TERMINAL_ACTIVITY_CLASSES:
        verdict = "no_action"
    elif effective_class == "hung_descendant":
        if hung_ps_evidence:
            process_probe_ref = "%s;%s" % (process_probe_ref, hung_ps_evidence)
            verdict = "hard_stall_eligible"
        else:
            verdict = "soft_warning"
    else:
        verdict = ("hard_stall_eligible"
                   if review_due(schedule_record, now)
                   else "soft_warning")

    return activity_contracts.validate_watchdog_decision({
        "schema_version": activity_contracts.SCHEMA_VERSION,
        "record": "WatchdogDecision",
        "work_id": work_id,
        "time": now,
        "verdict": verdict,
        "durable_evidence_ref": durable_evidence_ref,
        "process_probe_ref": process_probe_ref,
    })
