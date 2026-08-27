#!/usr/bin/env python3
"""Evidence-reconciliation extraction seam (M5 Package A;
garusis/cowork-internal#51).

Owns bounded polling for one command's terminal evidence, the ledger
revision that records whatever was observed, and (new in this candidate) the
`should_defer_teardown` decision point the spine's `finally` block and its
loop-control conditions consult before tearing down the worker or aborting
the inventory. Every function below is invoked by
`cowork_verification._run_owned_transaction` at its EXISTING bare
module-level name (re-exported into that module's own namespace at import
time -- see its `from cowork_verification_evidence import ...` line).

`should_defer_teardown(session_uuid, transaction_id, active_label) -> bool`
is a NEW decision point this candidate FREEZES the signature of and defines
a stub for, returning `False` UNCONDITIONALLY -- a concrete, frozen return
value, never `NotImplementedError` -- so the spine's teardown/abort behavior
is preserved EXACTLY as the base commit's (which always tore down /
aborted). Only Package C's later implementation may return `True`, once it
actually tracks a reconciliation-pending attempt as possibly alive.
`reconcile_pending_evidence` below is a SEPARATE, wholly new extension
point (the resume-time reconciliation entry point Package C adds for #51)
that raises `NotImplementedError` until Package C fills it in -- Package
A's own `should_defer_teardown` never calls it, since it always returns
`False`.

CROSS-MODULE CONSTANTS AND HELPERS. `bounded_evidence_wait`'s own signature
uses `DEFAULT_EVIDENCE_POLL_ATTEMPTS`/`DEFAULT_EVIDENCE_POLL_DELAY_S` as
DEFAULT PARAMETER VALUES -- resolved once, at module-IMPORT time, not at
call time. A lazy (function-body) back-reference to `cowork_verification`
(the pattern `cowork_verification_worker.py` uses for its own constants)
CANNOT satisfy a default-parameter-value use: the value must already exist
when this module's `def bounded_evidence_wait(...)` line itself executes,
and `cowork_verification` imports THIS module's symbols at its own top
level, so any module-level `import cowork_verification` here is a genuine
circular import. These two constants (plus `EVIDENCE_PRESENT`/
`EVIDENCE_UNRESOLVED`, used in function bodies here too) are therefore
small, frozen, local DUPLICATES of `cowork_verification`'s own
module-level constants of the same name and value --
`scripts/test_m5_package_a_contracts.py` asserts the two modules' values
stay equal, so any future drift is caught immediately rather than silently
producing two different poll bounds. `_execution_wait_budget_s`, by
contrast, is only ever used inside a function BODY here (never a default
parameter value), so it is reached via a lazy `_spine()` back-reference,
exactly like `cowork_verification_worker.py` does for its own constants --
one source of truth, no duplication risk.

Python 3.9+, stdlib only -- matches `cowork_verification.py`.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_state as state_store  # noqa: E402
import cowork_ledger as ledger  # noqa: E402


def _spine():
    """Lazy back-reference to `cowork_verification` for
    `_execution_wait_budget_s` -- see the module docstring's CROSS-MODULE
    CONSTANTS AND HELPERS note for why this is a function-body import, and
    why it is NOT used for `bounded_evidence_wait`'s own default parameter
    values."""
    import cowork_verification
    return cowork_verification


# Mirrors cowork_verification.EVIDENCE_PRESENT/EVIDENCE_UNRESOLVED and
# DEFAULT_EVIDENCE_POLL_ATTEMPTS/DEFAULT_EVIDENCE_POLL_DELAY_S -- duplicated,
# not imported; see the module docstring's CROSS-MODULE CONSTANTS note.
EVIDENCE_PRESENT = "present"
EVIDENCE_UNRESOLVED = "unresolved"
DEFAULT_EVIDENCE_POLL_ATTEMPTS = 10
DEFAULT_EVIDENCE_POLL_DELAY_S = 1.0


def _poll_attempt_events(events_path, seen_count):
    """Read new lines from the attempt-events stream past `seen_count`.
    Returns `(events, new_seen_count)`. Relocated verbatim from the base
    commit."""
    events = state_store.read_jsonl_tolerant(events_path)
    return events[seen_count:], len(events)


def bounded_evidence_wait(session_uuid, transaction_id, expected_labels,
                          poll_attempts=DEFAULT_EVIDENCE_POLL_ATTEMPTS,
                          poll_delay_s=DEFAULT_EVIDENCE_POLL_DELAY_S,
                          sleep=time.sleep):
    """Poll the worker's attempt-events stream for terminal events for every
    label in `expected_labels`, for a BOUNDED number of attempts. Relocated
    verbatim from the base commit. Past the bound, writes an explicit
    `unresolved`/`absent` terminal state for whatever is still missing and
    STOPS POLLING -- it never re-launches a command.

    Returns `{label: terminal_event_or_synthetic_unresolved}`.
    """
    events_path = state_store.verification_attempt_events_path_for(
        session_uuid, transaction_id)
    terminal_by_label = {}
    attempt = 0
    while attempt < poll_attempts and len(terminal_by_label) < len(
            expected_labels):
        events = state_store.read_jsonl_tolerant(events_path)
        for ev in events:
            if ev.get("event") == "terminal" and ev.get("label") in (
                    expected_labels or ()):
                terminal_by_label[ev["label"]] = ev
        if len(terminal_by_label) >= len(expected_labels):
            break
        attempt += 1
        if attempt < poll_attempts:
            sleep(poll_delay_s)
    for label in expected_labels or ():
        if label not in terminal_by_label:
            terminal_by_label[label] = {
                "event": "terminal", "label": label,
                "evidence_state": EVIDENCE_UNRESOLVED,
                "exit_code": None, "note": "evidence not observed within "
                "the bounded poll; the underlying command was never "
                "re-launched",
            }
        else:
            terminal_by_label[label].setdefault(
                "evidence_state", EVIDENCE_PRESENT)
    return terminal_by_label


def _revise_attempt_ledger(ledger_path, transaction_id, label, fields,
                           attempt_state):
    """The one call site every ledger revision in `_run_owned_transaction`
    goes through. Relocated verbatim from the base commit. Returns
    `(record_or_None, ok)`."""
    record = ledger.revise_owned_attempt(
        ledger_path, transaction_id, label, fields,
        attempt_state=attempt_state)
    return record, record is not None


def _revise_attempt_ledger_with_retry(ledger_path, transaction_id, label,
                                      fields, attempt_state, attempts=2,
                                      delay_s=0.05, sleep=time.sleep):
    """`_revise_attempt_ledger` with a small BOUNDED retry. Relocated
    verbatim from the base commit."""
    record = None
    ok = False
    for i in range(max(1, attempts)):
        record, ok = _revise_attempt_ledger(
            ledger_path, transaction_id, label, fields, attempt_state)
        if ok:
            return record, ok
        if i < attempts - 1:
            sleep(delay_s)
    return record, ok


def _wait_for_attempt_and_revise_ledger(
        session_uuid, transaction_id, entry, request, ledger_path,
        overall_deadline, timeout_policy, snapshot_manifest_digest,
        bounded_evidence_wait_fn=None):
    """Wait for one entry's terminal evidence, then revise the SAME
    pre-minted ledger id with whatever was observed. Relocated verbatim from
    the base commit -- including its call into `_execution_wait_budget_s`,
    which stays spine-owned (see `_spine()` above) since it is not part of
    the frozen evidence_reconciliation_seam's own symbol list.

    `bounded_evidence_wait_fn` is injectable (defaults to the real
    `bounded_evidence_wait`) so tests can control exactly what each wait
    phase observes without racing a real subprocess's timing.

    Returns `(attempt_dict, ledger_ok)`.
    """
    wait_fn = bounded_evidence_wait_fn or bounded_evidence_wait
    label = entry["label"]
    execution_budget_s = _spine()._execution_wait_budget_s(timeout_policy)
    remaining_overall = max(0.0, overall_deadline - time.time())
    primary_wait_s = min(execution_budget_s, remaining_overall)
    primary_poll_delay_s = 1.0
    primary_poll_attempts = (
        int(primary_wait_s // primary_poll_delay_s) + 1
        if primary_wait_s > 0 else 0)
    terminal = wait_fn(
        session_uuid, transaction_id, [label],
        poll_attempts=primary_poll_attempts,
        poll_delay_s=primary_poll_delay_s)
    attempt = terminal.get(label, {})
    if attempt.get("evidence_state") != EVIDENCE_PRESENT:
        # Execution should have ended by now -- evidence is still missing.
        # THIS is where the plan's short evidence_retry_policy applies: one
        # more bounded poll for evidence that is merely slow to land, before
        # concluding it is genuinely absent.
        retry_policy = request.get("evidence_retry_policy") or {}
        terminal = wait_fn(
            session_uuid, transaction_id, [label],
            poll_attempts=retry_policy.get(
                "poll_attempts", DEFAULT_EVIDENCE_POLL_ATTEMPTS),
            poll_delay_s=retry_policy.get(
                "poll_delay_s", DEFAULT_EVIDENCE_POLL_DELAY_S))
        attempt = terminal.get(label, {})
    attempt["kind"] = entry.get("kind")
    attempt["ledger_attempt_id"] = entry.get("ledger_attempt_id")
    for meta_key in ("invalidation_reason", "reuse_decision",
                    "triggering_finding", "marginal_cost"):
        if meta_key in entry:
            attempt[meta_key] = entry[meta_key]

    evidence_state = attempt.get("evidence_state")
    exit_code = attempt.get("exit_code")
    timed_out = bool(attempt.get("timed_out"))
    if evidence_state == EVIDENCE_PRESENT:
        if timed_out:
            exit_status, adjudication = "timeout", "fail"
        elif exit_code == 0:
            exit_status, adjudication = "pass", "pass"
        else:
            exit_status, adjudication = "fail", "fail"
        revise_state = "terminal"
    else:
        exit_status, adjudication = "unknown", (
            "unresolved" if evidence_state == EVIDENCE_UNRESOLVED
            else "unknown")
        revise_state = "unresolved"
    record, ledger_ok = _revise_attempt_ledger(
        ledger_path, transaction_id, label,
        fields={
            "exit_code": exit_code,
            "evidence_state": evidence_state,
            "timed_out": timed_out,
            "wall_time_s": attempt.get("wall_time_s"),
            "verification_kind": entry.get("kind"),
            "exit_status": exit_status,
            "adjudication": adjudication,
            "command_fingerprint": " ".join(entry.get("command") or []),
            "started_at": attempt.get("started_at"),
            "ended_at": attempt.get("ended_at"),
            "observed_source_digest": snapshot_manifest_digest,
        },
        attempt_state=revise_state)
    if not ledger_ok:
        attempt["evidence_state"] = EVIDENCE_UNRESOLVED
        attempt["ledger_revision_failed"] = True
    elif record.get("id") != entry.get("ledger_attempt_id"):
        ledger_ok = False
        attempt["evidence_state"] = EVIDENCE_UNRESOLVED
        attempt["ledger_revision_mismatch"] = True
    return attempt, ledger_ok


def should_defer_teardown(session_uuid, transaction_id, active_label):
    """Decision point the spine's `finally` block (before calling
    `cleanup_active_command_group`/`terminate_worker`) and its deadline- and
    evidence-abort loop-control conditions consult before tearing down the
    worker or aborting the inventory for `active_label` (the label of the
    entry currently believed possibly still in flight, or `None` when
    nothing is).

    PACKAGE A'S OWN IMPLEMENTATION: returns `False` UNCONDITIONALLY -- a
    concrete, frozen value, not `NotImplementedError` -- so every consulting
    call site behaves EXACTLY as the base commit's unconditional teardown/
    abort did (M5R-C2). `session_uuid`/`transaction_id`/`active_label` are
    accepted (the frozen signature) but never consulted by this stub.

    Only Package C's later implementation may return `True` -- once it
    actually tracks a reconciliation-pending attempt (one whose evidence
    poll expired but whose process might still be alive) as possibly alive,
    deferring `cleanup_active_command_group`/`terminate_worker` for that
    transaction and leaving eventual teardown to `reconcile_pending_evidence`
    below once the process is confirmed genuinely terminal."""
    return False


def reconcile_pending_evidence(session_uuid, transaction_id, active_label):
    """Extension point for Package C (garusis/cowork-internal#51): the
    resume-time reconciliation entry point that determines whether a
    reconciliation-pending attempt (one `should_defer_teardown` chose not to
    tear down) is genuinely terminal -- checking process/process-group
    liveness via `_pgid_alive` before declaring `evidence_state=absent` --
    and performs the deferred ledger revision plus eventual teardown once
    that is confirmed, including after a supervisor crash (using
    `cowork_state.verification_deferred_reconciliation_path_for`'s durable
    marker to find every such transaction, not just the ones a still-running
    process happens to remember).

    Package A's own `should_defer_teardown` always returns `False`, so
    nothing in this candidate ever reaches this stub. Raises
    NotImplementedError until Package C fills it in."""
    raise NotImplementedError(
        "Package C implements resume-time evidence reconciliation here "
        "(garusis/cowork-internal#51); Package A's should_defer_teardown "
        "always returns False, so this stub is never reached in this "
        "candidate.")
