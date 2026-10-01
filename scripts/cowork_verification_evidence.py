#!/usr/bin/env python3
"""Evidence-reconciliation extraction seam (M5 Package A foundation, M5
Package C implementation; garusis/cowork-internal#51).

Owns bounded polling for one command's terminal evidence, the ledger
revision that records whatever was observed, the `should_defer_teardown`
decision point the spine's `finally` block and its loop-control conditions
consult before tearing down the worker or aborting the inventory, and (new
in this candidate) `reconcile_pending_evidence`, the resume-time
reconciliation entry point that eventually records a deferred attempt's
true terminal outcome and owns its deferred teardown. Every function below
is invoked by `cowork_verification._run_owned_transaction` at its EXISTING
bare module-level name (re-exported into that module's own namespace at
import time -- see its `from cowork_verification_evidence import ...`
line).

`should_defer_teardown(session_uuid, transaction_id, active_label) -> bool`
is the truthful reconciliation-pending predicate Package A froze the
signature of and stubbed to always return `False`. PACKAGE C: still
returns `False` for every case Package A's stub already covered (nothing
evidenced possibly-alive), so the base commit's unconditional teardown/
abort behavior is unchanged for the ordinary green/red/timeout paths --
`True` only once this candidate has DURABLY recorded, in this
transaction's `verification_deferred_reconciliation_path_for` marker, that
`active_label` (or an earlier label from the SAME transaction) is still
possibly alive per the worker's own published active-command-group
evidence (`verification_active_pgid_path_for` + `_pgid_alive`). Deferred
labels are tracked as a SET on that marker, not a single overwritten
field, so a later consult for a DIFFERENT (or `None`) `active_label` still
correctly reports the deferral of an EARLIER one the spine's own local
`active_label` variable has since overwritten (carried finding
M5A-R-m6 -- see `_read_deferred_marker`/`_write_deferred_marker`).

`reconcile_pending_evidence(session_uuid, transaction_id, active_label)` is
the resume-time reconciliation entry point PACKAGE C implements: it reads
the SAME durable marker (never relying on `active_label` alone, so a
supervisor crash that lost every in-memory reference still resolves every
deferred transaction from disk), re-checks each deferred label's terminal
evidence with the SAME bounded, no-relaunch machinery `bounded_evidence_
wait` already uses, revises that label's ledger attempt from `unresolved`
to its TRUE terminal outcome once evidence lands (or to `EVIDENCE_ABSENT`
with bounded diagnostics once the process is confirmed genuinely gone with
none), and -- only once nothing for the transaction is left possibly
alive -- owns tearing down the active command's process group and the
worker's own, exactly mirroring `cleanup_active_command_group`/
`terminate_worker`'s idempotent TERM-then-KILL escalation but sourced
entirely from durable pgid/pid evidence rather than the original `proc`/
liveness-pipe handles a resumed process never has. Idempotent: a
transaction with nothing deferred, or already fully reconciled, is a
no-op; teardown is skipped (never double-fired) once the marker is gone.

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
`EVIDENCE_UNRESOLVED`/`EVIDENCE_ABSENT`, used in function bodies here too)
are therefore small, frozen, local DUPLICATES of `cowork_verification`'s
own module-level constants of the same name and value --
`scripts/test_checkpoint_contracts.py` asserts the shared subset stays
equal, so any future drift is caught immediately rather than silently
producing two different poll bounds. `_execution_wait_budget_s`,
`_pgid_alive`, and `cleanup_active_command_group`, by contrast, are only
ever used inside a function BODY here (never a default parameter value),
so they are reached via a lazy `_spine()` back-reference, exactly like
`cowork_verification_worker.py` does for its own constants -- one source
of truth, no duplication risk.

BOUNDED SUCCESSOR (M5C-V2), three residual findings closed on top of the
exact adopted v1 correction: (B1) `test_should_defer_teardown_engages_
end_to_end_through_the_real_spine` now sizes its real command's duration
to comfortably outlast every real delay `run_transaction` can still incur
once teardown is genuinely deferred (in particular the pre-existing
`finally` block's own bounded `startup_capture_thread.join`, which cannot
return early while the worker is deliberately kept alive) and additionally
proves a `reconcile_pending_evidence` pass consulted WHILE the command is
still genuinely alive leaves the durable deferred marker byte-for-byte
unchanged -- never a contradictory removal of truthful marker state for
work that is not actually done. (B2) The still-alive-work assertions in
`test_still_alive_stays_deferred_never_torn_down_or_fabricated` and
`test_defensively_included_still_alive_label_is_persisted_not_fabricated`
now compare against the legitimate pre-existing PENDING mint record
instead of an empty ledger, which a genuine mint always contradicts --
the corrected proof is that reconciliation appends no `unresolved`/
`terminal` revision on top of it, not that no record exists at all. (B3)
`_teardown_worker_process_group`'s own `os.getpgid`/`os.killpg` calls, and
`reconcile_pending_evidence`'s call into the frozen spine's
`cleanup_active_command_group`, now tolerate `PermissionError` (EPERM)
exactly like the existing `ProcessLookupError` tolerance -- a signal the
OS refuses is proof this candidate may not touch that process group
further, never grounds to crash reconciliation or escalate to a stronger
signal against it.

BOUNDED SUCCESSOR (M5C-V3), one independent-review blocker and its
supporting major closed on top of the exact adopted v2 correction, with no
narrowing of any prior closure: (B1) `_worker_pid_start_corroborated` no
longer treats `ps -o lstart=`'s naive LOCAL wall-clock output as if it were
already UTC -- doing so silently widened (or narrowed, on a UTC-positive
host) the pid-reuse acceptance window by exactly this host's UTC offset,
letting a genuinely reused pid that started HOURS after the real identity
report be falsely corroborated. `_local_naive_to_aware` now converts it
truthfully via `astimezone()` (which assumes a naive value is local and
attaches the correct system offset without shifting the wall-clock
reading), and fails SAFE -- refusing corroboration, never guessing -- for
the one case that conversion cannot resolve on its own: a local timestamp
that lands in a DST fall-back's repeated hour, genuinely ambiguous between
two different real instants.

Python 3.9+, stdlib only -- matches `cowork_verification.py`.
"""

import datetime
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_state as state_store  # noqa: E402
import cowork_ledger as ledger  # noqa: E402


def _spine():
    """Lazy back-reference to `cowork_verification` for
    `_execution_wait_budget_s`, `_pgid_alive`, and
    `cleanup_active_command_group` -- see the module docstring's
    CROSS-MODULE CONSTANTS AND HELPERS note for why this is a function-body
    import, and why it is NOT used for `bounded_evidence_wait`'s own default
    parameter values."""
    import cowork_verification
    return cowork_verification


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


# Mirrors cowork_verification.EVIDENCE_PRESENT/EVIDENCE_UNRESOLVED/
# EVIDENCE_ABSENT and DEFAULT_EVIDENCE_POLL_ATTEMPTS/
# DEFAULT_EVIDENCE_POLL_DELAY_S -- duplicated, not imported; see the module
# docstring's CROSS-MODULE CONSTANTS note.
EVIDENCE_PRESENT = "present"
EVIDENCE_UNRESOLVED = "unresolved"
EVIDENCE_ABSENT = "absent"
DEFAULT_EVIDENCE_POLL_ATTEMPTS = 10
DEFAULT_EVIDENCE_POLL_DELAY_S = 1.0


def _poll_attempt_events(events_path, seen_count):
    """Read new lines from the attempt-events stream past `seen_count`.
    Returns `(events, new_seen_count)`. Relocated verbatim from the base
    commit."""
    events = state_store.read_jsonl_tolerant(events_path)
    return events[seen_count:], len(events)


def _label_possibly_alive_pgid(session_uuid, transaction_id, label):
    """The pgid of `label`'s command IF the worker's own durable
    active-command-group publication (`verification_active_pgid_path_for`)
    still names `label` AND `_pgid_alive` confirms something remains in
    that process group right now -- `None` otherwise (no publication, a
    different label is the one actually active, or the group is
    genuinely empty). This is the SAME evidence
    `cleanup_active_command_group` already trusts to find what to
    TERM/KILL -- reused here, never re-derived from a second source, and
    never a reason to relaunch anything."""
    active = state_store.read_json_tolerant(
        state_store.verification_active_pgid_path_for(
            session_uuid, transaction_id))
    if not isinstance(active, dict):
        return None
    pgid = active.get("pgid")
    if not pgid or active.get("label") != label:
        return None
    return pgid if _spine()._pgid_alive(pgid) else None


def bounded_evidence_wait(session_uuid, transaction_id, expected_labels,
                          poll_attempts=DEFAULT_EVIDENCE_POLL_ATTEMPTS,
                          poll_delay_s=DEFAULT_EVIDENCE_POLL_DELAY_S,
                          sleep=time.sleep, deadline=None,
                          is_worker_alive=None, now=time.time):
    """Poll the worker's attempt-events stream for terminal evidence for every
    label in `expected_labels`, for a BOUNDED number of attempts -- the SAME
    command-budget window every caller already sizes (the primary wait, or
    the short `evidence_retry_policy` second pass). Past the bound, this
    NEVER re-launches the command; instead it consults the same durable
    process/process-group liveness evidence `cleanup_active_command_group`
    uses (`_label_possibly_alive_pgid`, M5 Package C) to decide, for
    whatever is still missing, between two honest terminal states: still
    possibly alive -> `EVIDENCE_UNRESOLVED` (genuinely unknown; the caller's
    `should_defer_teardown` consult is what may defer teardown for it), or
    confirmed genuinely gone with nothing observed -> `EVIDENCE_ABSENT`
    (reusing the existing constant, never inventing a fourth evidence
    state).

    SKIP EVENTS ARE TERMINAL, NOT SOMETHING TO WAIT FOR (blocker79). The
    worker already publishes a durable `skipped_no_permit` /
    `skipped_liveness_lost` / `skipped_mutation_detected` record for an
    entry it deliberately did NOT execute, and then moves on. Before this,
    only `event == "terminal"` was matched here, so that record was
    discarded and the parent kept polling its entire budget for evidence
    that provably could never arrive. Such a label now resolves AT ONCE as
    `EVIDENCE_ABSENT` with `exit_code=None`, `skipped=True` and
    `skip_reason` naming the event -- an UNEXECUTED gate, distinguishable
    from ran-but-evidence-lost, and unreachable from `pass` (which requires
    `EVIDENCE_PRESENT`). A genuine `terminal` event for the same label
    always takes precedence over a skip record: really observed evidence is
    never overridden by the recorded absence of it.

    `deadline` and `is_worker_alive` are OPTIONAL and default to `None`,
    which reproduces the previous behavior exactly:

      * `deadline` -- an absolute, `now()`-comparable wall-clock instant
        (the spine's own `overall_deadline` already is one). Re-checked on
        every iteration, so a poll whose nominal attempt-count budget was
        sized against a clock that later JUMPED (a host suspension is the
        motivating case) still stops at the deadline instead of spending
        every remaining attempt past it. `now` is injectable purely so a
        test can drive that clock deterministically.
      * `is_worker_alive` -- a zero-argument predicate (the spine passes
        one backed by `proc.poll()`, which also reaps the exited child). An
        exited worker can emit no further evidence, so there is nothing
        left to wait for. Liveness is sampled BEFORE the event stream is
        read and acted on only AFTER it, so everything the worker managed
        to write before exiting is still read and honored first.

    Neither can turn a missing label into a false PASS, and neither
    bypasses the liveness-informed fallback below: a dead WORKER does not
    prove the child command's own process group is gone, so a label whose
    published command group is still alive stays `EVIDENCE_UNRESOLVED`
    exactly as before.

    Returns `{label: terminal_event_or_synthetic_unresolved_or_absent}`.
    """
    # The worker's own deliberate-skip events: `worker_main` writes these
    # three event names, and only these, for an inventory entry it refuses
    # to execute (no permit, parent liveness lost, candidate mutated). Kept
    # here as function-local literals, not a module constant: they are
    # recognition internal to THIS wait, and `cowork_verification` imports
    # this module at its own top level, so reading them back off the spine
    # would be a genuine circular import.
    # `scripts/test_verification_permit_exit.py` reads this tuple out of
    # this function's own source and asserts it still matches every skip
    # event the worker actually emits, so drift is caught immediately
    # rather than silently re-introducing the unbounded wait this
    # recognition exists to end.
    skip_events = ("skipped_no_permit", "skipped_liveness_lost",
                   "skipped_mutation_detected")
    events_path = state_store.verification_attempt_events_path_for(
        session_uuid, transaction_id)
    expected = list(expected_labels or ())
    expected_set = set(expected)
    needed = len(expected)
    terminal_by_label = {}
    skipped_by_label = {}
    resolved = set()
    stopped_by = None
    attempt = 0
    while attempt < poll_attempts and len(resolved) < needed:
        # Sampled BEFORE the read below and acted on only after it: a
        # worker that exits between these two points still has everything
        # it wrote observed on this very iteration.
        worker_gone = (is_worker_alive is not None
                       and not is_worker_alive())
        events = state_store.read_jsonl_tolerant(events_path)
        for ev in events:
            label = ev.get("label")
            if label not in expected_set:
                continue
            if ev.get("event") == "terminal":
                terminal_by_label[label] = ev
                resolved.add(label)
            elif (ev.get("event") in skip_events
                    and label not in skipped_by_label):
                skipped_by_label[label] = ev
                resolved.add(label)
        if len(resolved) >= needed:
            break
        if worker_gone:
            stopped_by = "worker_exited"
            break
        if deadline is not None and now() >= deadline:
            stopped_by = "deadline_reached"
            break
        attempt += 1
        if attempt < poll_attempts:
            sleep(poll_delay_s)
    terminal_by_expected_label = {}
    for label in expected:
        observed = terminal_by_label.get(label)
        if observed is not None:
            observed.setdefault("evidence_state", EVIDENCE_PRESENT)
            terminal_by_expected_label[label] = observed
            continue
        skip = skipped_by_label.get(label)
        if skip is not None:
            terminal_by_expected_label[label] = {
                "event": "terminal", "label": label,
                "evidence_state": EVIDENCE_ABSENT,
                "exit_code": None,
                "skipped": True,
                "skip_reason": skip.get("event"),
                "skipped_at": skip.get("at"),
                "note": "the worker recorded %r for this label: the "
                "command was never executed, so no evidence for it can "
                "ever arrive -- it was never re-launched and is never "
                "adjudicated pass" % (skip.get("event"),),
            }
            continue
        pgid = _label_possibly_alive_pgid(
            session_uuid, transaction_id, label)
        if pgid is not None:
            terminal_by_expected_label[label] = {
                "event": "terminal", "label": label,
                "evidence_state": EVIDENCE_UNRESOLVED,
                "exit_code": None,
                "note": "evidence not observed within the bounded "
                "poll; process group %s is still alive -- the "
                "underlying command was never re-launched" % pgid,
            }
        else:
            terminal_by_expected_label[label] = {
                "event": "terminal", "label": label,
                "evidence_state": EVIDENCE_ABSENT,
                "exit_code": None,
                "note": "evidence not observed within the bounded "
                "poll and no live process/process-group evidence "
                "remains for it -- the underlying command was never "
                "re-launched",
            }
        if stopped_by is not None:
            # Why the poll ended before its attempt budget was spent --
            # diagnostics only; it never changes WHICH evidence state was
            # concluded just above.
            terminal_by_expected_label[label]["wait_stopped_by"] = stopped_by
    return terminal_by_expected_label


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
        bounded_evidence_wait_fn=None, is_worker_alive=None):
    """Wait for one entry's terminal evidence, then revise the SAME
    pre-minted ledger id with whatever was observed. Relocated verbatim from
    the base commit -- including its call into `_execution_wait_budget_s`,
    which stays spine-owned (see `_spine()` above) since it is not part of
    the frozen evidence_reconciliation_seam's own symbol list.

    `bounded_evidence_wait_fn` is injectable (defaults to the real
    `bounded_evidence_wait`) so tests can control exactly what each wait
    phase observes without racing a real subprocess's timing.

    The `overall_deadline` this function ALREADY receives (and, when the
    caller supplies one, the optional `is_worker_alive` predicate) is now
    forwarded into BOTH wait phases -- the primary wait and the short
    `evidence_retry_policy` retry -- so neither can outlive the deadline
    the caller already bounded this transaction by (blocker79: the primary
    wait's attempt COUNT was sized from the remaining deadline exactly
    once, up front, and then never re-checked, so a clock that jumped
    mid-wait spent every remaining attempt past it). Forwarded only to a
    wait callable that can actually accept them -- decided just below by
    INSPECTING the resolved callable, never by calling it and catching the
    failure, which would also swallow a genuine `TypeError` raised from
    inside a wait that really did run. So a narrower injected double is
    called with the exact pre-existing argument shape and behaves exactly
    as it did before.

    Returns `(attempt_dict, ledger_ok)`.
    """
    # Function-local, like `_spine()` above: needed only for the one
    # signature probe below, and never as a module-level dependency of the
    # frozen evidence seam.
    import inspect

    wait_fn = bounded_evidence_wait_fn or bounded_evidence_wait
    bound_kwargs = {"deadline": overall_deadline}
    if is_worker_alive is not None:
        bound_kwargs["is_worker_alive"] = is_worker_alive
    # A `unittest.mock` replacement accepts anything at its own boundary
    # while forwarding to a target that may not, so the delegate
    # (`side_effect`, else `_mock_wraps`) is what gets inspected in that
    # case. A callable that cannot be introspected at all is offered
    # nothing -- the conservative choice, since it reproduces the exact
    # pre-existing call shape.
    probe_target = wait_fn
    for probe_attr in ("side_effect", "_mock_wraps"):
        delegate = getattr(probe_target, probe_attr, None)
        if callable(delegate):
            probe_target = delegate
            break
    try:
        accepted = inspect.signature(probe_target).parameters
    except (TypeError, ValueError):
        bound_kwargs = {}
    else:
        if not any(p.kind is p.VAR_KEYWORD for p in accepted.values()):
            bound_kwargs = {name: value
                            for name, value in bound_kwargs.items()
                            if name in accepted}
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
        poll_delay_s=primary_poll_delay_s, **bound_kwargs)
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
                "poll_delay_s", DEFAULT_EVIDENCE_POLL_DELAY_S),
            **bound_kwargs)
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


def _deferred_marker_path(session_uuid, transaction_id):
    return state_store.verification_deferred_reconciliation_path_for(
        session_uuid, transaction_id)


def _read_deferred_marker(session_uuid, transaction_id):
    """This transaction's durable deferred-reconciliation marker, normalized
    to always carry a `labels` dict (`{label: {pgid, deferred_at}}`) even
    when the file is absent/corrupt -- the marker's `labels` KEYS are the
    SET of labels currently believed reconciliation-pending, tracked as a
    set rather than a single overwritten field (carried finding
    M5A-R-m6): the spine's own local `active_label` variable is
    overwritten on every loop iteration and reset to `None` once an entry
    resolves, so it alone cannot tell a later consult (in the `finally`
    block, or for a subsequent entry) that an EARLIER label is still
    deferred -- this durable, additive set is the one source of truth
    `should_defer_teardown`/`reconcile_pending_evidence` both read."""
    marker = state_store.read_json_tolerant(
        _deferred_marker_path(session_uuid, transaction_id))
    labels = marker.get("labels") if isinstance(marker, dict) else None
    if not isinstance(labels, dict):
        labels = {}
    return {"transaction_id": transaction_id, "labels": dict(labels)}


def _write_deferred_marker(session_uuid, transaction_id, labels):
    """Persist `labels` (`{label: {pgid, deferred_at}}`) as this
    transaction's deferred-reconciliation marker, or remove the marker
    entirely once `labels` is empty -- absent means nothing is deferred,
    matching `verification_deferred_reconciliation_path_for`'s own
    contract. Best-effort, like every other marker write in this seam."""
    path = _deferred_marker_path(session_uuid, transaction_id)
    if not labels:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    state_store.write_json_atomic(path, {
        "transaction_id": transaction_id,
        "labels": labels,
        "updated_at": _utc_now(),
    })


def _update_deferred_marker(session_uuid, transaction_id, add=None,
                            remove=None):
    """The ONE mutation path for this transaction's deferred marker: reads
    the CURRENT on-disk state immediately before writing and applies `add`
    ({label: info}, never overwriting an already-recorded label's own
    diagnostics) and `remove` (an iterable of labels now fully resolved) on
    top of THAT fresh read -- never a stale snapshot-then-blind-replace.
    Without this, a `reconcile_pending_evidence` pass that read the marker
    at the START of a (potentially many-seconds-long, one-bounded-poll-
    per-label) run and then unconditionally overwrote it at the END could
    silently discard a label a CONCURRENT `should_defer_teardown` consult
    -- for a different, still-live entry of the SAME transaction -- added
    in the meantime, contradicting a live marker. A no-op write (nothing
    actually changed) never touches disk. Returns the resulting `labels`
    dict."""
    current = _read_deferred_marker(session_uuid, transaction_id)["labels"]
    changed = False
    for label, info in (add or {}).items():
        if label not in current:
            current[label] = info
            changed = True
    for label in (remove or ()):
        if label in current:
            del current[label]
            changed = True
    if changed:
        _write_deferred_marker(session_uuid, transaction_id, current)
    return current


def _label_already_terminal(ledger_path, transaction_id, label):
    prior = _latest_ledger_record(ledger_path, transaction_id, label)
    return bool(prior) and prior.get("attempt_state") == "terminal"


def should_defer_teardown(session_uuid, transaction_id, active_label):
    """The truthful reconciliation-pending predicate the spine's `finally`
    block (before calling `cleanup_active_command_group`/`terminate_worker`)
    and its deadline- and evidence-abort loop-control conditions consult
    before tearing down the worker or aborting the inventory for
    `active_label` (the label of the entry currently believed possibly
    still in flight, or `None` when nothing new is).

    PACKAGE C (garusis/cowork-internal#51): if `active_label` is not
    already tracked as deferred and this transaction's durable
    active-command-group publication shows it still genuinely possibly
    alive (`_label_possibly_alive_pgid` -- the SAME evidence
    `bounded_evidence_wait`'s own `EVIDENCE_UNRESOLVED`-vs-`EVIDENCE_ABSENT`
    fallback just used to decide it could not yet call this genuinely
    terminal), it is added to this transaction's durable deferred-label SET
    (`_read_deferred_marker`/`_write_deferred_marker`) -- never a single
    overwritten field, so an earlier deferred label from a prior loop
    iteration is never silently dropped just because THIS call's
    `active_label` is different or `None` (carried finding M5A-R-m6).
    Returns `True` iff that set is non-empty afterward -- for every case
    Package A's stub already covered (nothing ever evidenced possibly
    alive: an ordinary green/red/timeout transaction, or a genuinely dead
    process with no evidence), that set stays empty and this returns
    `False`, UNCHANGED from the base commit's unconditional teardown/abort
    (M5R-C2).

    Never tears anything down and never relaunches the underlying command
    itself -- this is a predicate only; `reconcile_pending_evidence` below
    owns the actual deferred ledger revision and eventual teardown."""
    if not session_uuid or not transaction_id:
        return False
    current = _read_deferred_marker(session_uuid, transaction_id)["labels"]
    if not active_label or active_label in current:
        return bool(current)
    ledger_path = state_store.ledger_path_for(session_uuid)
    if _label_already_terminal(ledger_path, transaction_id, active_label):
        # Already durably resolved (by an earlier consult in THIS run, or
        # by a prior reconcile_pending_evidence pass) -- never re-defer
        # something that is genuinely done just because a caller still
        # happens to be holding its label.
        return bool(current)
    pgid = _label_possibly_alive_pgid(
        session_uuid, transaction_id, active_label)
    if pgid is None:
        return bool(current)
    updated = _update_deferred_marker(
        session_uuid, transaction_id,
        add={active_label: {"pgid": pgid, "deferred_at": _utc_now()}})
    return bool(updated)


def _latest_ledger_record(ledger_path, transaction_id, label):
    key = ledger.owned_attempt_key(transaction_id, label)
    if not key:
        return None
    latest = None
    for rec in ledger.read_ledger(ledger_path):
        if (rec.get("kind") == "attempt" and not rec.get("marker")
                and rec.get("attempt_key") == key):
            latest = rec
    return latest


def _local_naive_to_aware(naive_dt):
    """Interpret `naive_dt` -- a naive wall-clock timestamp already known to
    be in THIS system's local timezone, exactly what `ps -o lstart=` prints
    -- as a truthful aware instant, never as if it were already UTC (which
    silently widens or narrows the corroboration window below by exactly
    this host's UTC offset -- M5C-V3-B1). `datetime.astimezone()` on a
    naive value assumes it represents local time and attaches the correct
    system-local offset without shifting the wall-clock reading, which is
    exactly the semantics `ps -o lstart=` output needs.

    Fails SAFE (returns `None`, never a guess) when the local wall-clock
    reading is genuinely AMBIGUOUS -- a DST fall-back repeated hour, where
    two distinct real instants share the identical local clock string --
    by resolving both possible interpretations (`fold=0`/`fold=1`) and
    refusing whenever they disagree on UTC offset."""
    try:
        as_fold0 = naive_dt.replace(fold=0).astimezone()
        as_fold1 = naive_dt.replace(fold=1).astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    if as_fold0.utcoffset() != as_fold1.utcoffset():
        return None
    return as_fold0


def _worker_pid_start_corroborated(pid, reported_at):
    """Best-effort corroboration that `pid` is still the SAME process that
    reported this identity, not an unrelated process the OS has since
    reused that pid for. This risk is real HERE in a way it is not for
    `cleanup_active_command_group`/`terminate_worker`'s own immediate,
    same-live-transaction use: `reconcile_pending_evidence` is explicitly a
    RESUME-time entry point that may run an unbounded time after a
    supervisor crash, giving the OS ample opportunity to recycle a pid
    between the crash and this reconciliation pass. Compares the process's
    OWN start time (`ps -o lstart=`, macOS-available) against `reported_at`
    -- the genuine worker always started AT OR BEFORE it reported this
    identity; an unrelated process that later reused the pid characteristically
    started well AFTER. `ps -o lstart=` prints this host's LOCAL wall-clock
    time with NO timezone/offset field, while `reported_at` is always UTC
    (`_utc_now`) -- treating that naive local string as if it were already
    UTC (M5C-V3-B1) silently widens or narrows the acceptance window by
    exactly this host's UTC offset, on a sufficiently timezone-shifted host
    wide enough to falsely corroborate a genuinely-reused pid that started
    HOURS after the real report (see `_local_naive_to_aware`, which this
    truthfully converts through instead). Fails SAFE: any lookup/parsing/
    timezone-conversion uncertainty (no `reported_at` recorded, `ps`
    unavailable/erroring, unexpected output, a genuinely AMBIGUOUS local
    timestamp during a DST fall-back) treats the pid as NOT corroborated --
    refusing to tear it down rather than risking killing an unrelated,
    unauthorized process group."""
    if not reported_at:
        return False
    try:
        reported_dt = datetime.datetime.fromisoformat(
            str(reported_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    try:
        result = _spine().subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 - any ps failure is "not corroborated"
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        started_naive = datetime.datetime.strptime(
            result.stdout.strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return False
    started_dt = _local_naive_to_aware(started_naive)
    if started_dt is None:
        return False
    return started_dt <= reported_dt + datetime.timedelta(minutes=1)


def _reap_if_own_child(pid):
    """Best-effort, NEVER a signal: reap `pid` with a non-blocking
    `os.waitpid` if (and only if) it is a direct child of THIS process.
    `os.waitpid` can only ever succeed against an actual child of the
    calling process -- it raises `ChildProcessError` (never a permission
    error, never any effect on anything else) for a pid that is not, which
    covers both a resumed-after-crash worker/command this process never
    itself spawned (reparented to init/launchd, which reaps it on its own
    once genuinely dead) and a pid this process already reaped once.

    M5C-V2-B3/B1 (teardown truthfulness across zombie/reaping): a process
    this SAME run already successfully TERM/KILL'd can otherwise sit as an
    unreaped zombie -- `_pgid_alive`'s own `os.killpg(pgid, 0)` (see its
    docstring) still finds a zombie, because a pid/pgid slot is not
    released until something reaps it -- making "eventually torn down"
    impossible to truthfully observe even though the command is
    genuinely, unambiguously dead. Calling this before re-checking
    `_pgid_alive` is what makes that check truthful whenever THIS process
    is the one that did the killing."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass


def _pgid_truthfully_alive(spine, pgid):
    """`spine._pgid_alive(pgid)`, but only after giving THIS process a
    chance to reap `pgid` first (see `_reap_if_own_child`) -- `pgid` is
    always the LEADER pid of a group this seam ever publishes or targets
    (`run_command_in_group`'s `on_start` publishes `os.getpgid(proc.pid)`
    for a `start_new_session=True` child, whose pgid IS its own pid by
    POSIX `setsid` semantics; `_teardown_worker_process_group`'s own
    `pgid = os.getpgid(pid)` is exactly that same worker pid). Reaping is
    never a signal and can never affect an unrelated process, so this adds
    no ownership risk on top of `_pgid_alive` itself."""
    _reap_if_own_child(pgid)
    return spine._pgid_alive(pgid)


class _ConcurrentReaper(object):
    """A short-lived daemon thread that repeatedly, best-effort reaps
    `pgid` (see `_reap_if_own_child`) for as long as this context manager
    is open -- used to race the FROZEN `cleanup_active_command_group`'s
    own zombie-blind `_pgid_alive` polling loop, which this candidate
    cannot edit to add reaping to directly.

    M5C-V2 (teardown determinism across zombie/reaping): without this,
    a process `cleanup_active_command_group` has already genuinely
    SIGTERM'd can sit as an unreaped zombie for that frozen function's
    ENTIRE `term_grace_s` (its own `_pgid_alive(pgid)` check never
    becomes False on its own, since nothing in that function ever reaps),
    non-deterministically stalling reconciliation for seconds even though
    the command is already unambiguously dead. Python releases the GIL
    across both `time.sleep` and the `os.waitpid`/`os.killpg` syscalls
    involved, so this thread genuinely interleaves with that loop's own
    blocking waits and reaps the zombie within one short poll interval of
    it actually dying -- letting the frozen loop's very next `_pgid_alive`
    check observe the truth and return early, instead of only ever being
    reaped after that function gives up and returns.

    Reaping is never a signal, so this adds no ownership risk: it can
    never touch, let alone signal, a process that is not this process's
    own child (see `_reap_if_own_child`)."""

    def __init__(self, pgid, poll_s=0.02):
        self._pgid = pgid
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if self._pgid:
            def _run():
                while not self._stop.is_set():
                    _reap_if_own_child(self._pgid)
                    self._stop.wait(self._poll_s)
            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._pgid:
            _reap_if_own_child(self._pgid)
        return False


def _teardown_worker_process_group(session_uuid, transaction_id):
    """Own the worker's OWN process-group teardown from a resume-time
    reconciliation pass -- mirroring `terminate_worker`'s TERM-then-KILL
    escalation, but sourced entirely from the worker's durably self-
    reported pid (`verification_worker_identity_path_for`) rather than the
    original `proc`/liveness-pipe-fd handles, which are process-local and
    do not survive a supervisor crash (there is no fd number left to close
    after one; killing the process group achieves the same practical
    effect the liveness pipe's EOF was only ever a signal FOR). Idempotent:
    a pid that no longer exists (already exited, already reaped, or the
    worker never even reached identity report) is a silent no-op, exactly
    like `cleanup_active_command_group`'s own tolerance of an
    already-empty process group. Never targets an unrelated/unauthorized
    process group: `_worker_pid_start_corroborated` must positively confirm
    the pid still names the SAME process before anything is signaled.

    OWNERSHIP-SAFE (M5C-V2-B3): every `os.getpgid`/`os.killpg` call is also
    guarded against `PermissionError` (EPERM), not just `ProcessLookupError`
    -- a signal the OS refuses because this process does not own the
    target is, by definition, a group this candidate must never touch
    further; the SAME conservative "stop here, do not escalate, never
    crash reconciliation" response applies whether the denial arrives on
    the initial TERM or the eventual KILL."""
    identity = state_store.read_json_tolerant(
        state_store.verification_worker_identity_path_for(
            session_uuid, transaction_id))
    pid = identity.get("pid") if isinstance(identity, dict) else None
    if not pid:
        return
    if not _worker_pid_start_corroborated(
            pid, identity.get("reported_at")):
        return
    spine = _spine()
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    if not _pgid_truthfully_alive(spine, pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + spine.DEFAULT_TERM_GRACE_S
    while time.time() < deadline and _pgid_truthfully_alive(spine, pgid):
        time.sleep(0.1)
    if _pgid_truthfully_alive(spine, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # One final reap attempt: the SIGKILL just sent (or the SIGTERM that
    # already finished it inside the grace loop above) still leaves an
    # unreaped zombie in THIS process's own child table until something
    # collects it -- a caller's own truthfulness check right after this
    # function returns must never see a zombie misreported as "alive".
    _reap_if_own_child(pgid)


def _safe_to_teardown_worker(session_uuid, transaction_id, handled_labels):
    """Whether tearing the worker down now cannot possibly kill LEGITIMATE,
    never-deferred work: true when nothing is currently published as
    active, or when whatever is currently active is one of the labels this
    reconciliation pass just finished handling. False when some OTHER
    label -- one this pass never touched -- is the one currently active,
    so a resume-time reconciliation pass for an EARLIER deferred label can
    never reach in and kill a later, healthy, still-running entry it has
    no business touching."""
    active = state_store.read_json_tolerant(
        state_store.verification_active_pgid_path_for(
            session_uuid, transaction_id))
    if not isinstance(active, dict) or not active.get("pgid"):
        return True
    return active.get("label") in handled_labels


def reconcile_pending_evidence(session_uuid, transaction_id, active_label):
    """The resume-time reconciliation entry point for Package C
    (garusis/cowork-internal#51): resolves every label this transaction's
    durable deferred-reconciliation marker tracks (never just
    `active_label` alone -- the marker is what makes this survive a
    supervisor crash, per `_read_deferred_marker`'s own docstring), revises
    each one's ledger attempt from `unresolved` to its TRUE terminal
    outcome once evidence lands (or to `EVIDENCE_ABSENT` plus bounded
    diagnostics once the process is confirmed genuinely gone with none),
    and -- only once nothing for this transaction is left possibly alive --
    owns tearing down the active command's process group and the worker's
    own (`_safe_to_teardown_worker` bounds this to never touch a label this
    pass never handled).

    Reuses `bounded_evidence_wait` (the SAME command-budget window, the
    SAME `EVIDENCE_UNRESOLVED`-vs-`EVIDENCE_ABSENT` liveness-informed
    fallback) for each label's re-check -- one bounded poll, never an
    unbounded wait, and NEVER a relaunch of the underlying command.
    A label whose bounded re-check still comes back `EVIDENCE_UNRESOLVED`
    (still genuinely alive right now) stays deferred for a later
    reconciliation pass rather than being torn down or fabricated a
    terminal outcome it does not yet have.

    Idempotent and crash-safe: a transaction with nothing deferred (no
    marker, and no evidence for `active_label` either) is a silent no-op
    that touches no process/process-group at all; once every label is
    resolved the marker is removed, so a second call finds nothing left to
    reconcile or tear down (no orphan, no double teardown).

    Returns `{"transaction_id", "reconciled": [...], "still_pending": [...]}`.
    """
    if not session_uuid or not transaction_id:
        return {"transaction_id": transaction_id, "reconciled": [],
               "still_pending": []}
    marker = _read_deferred_marker(session_uuid, transaction_id)
    originally_deferred = marker["labels"]
    labels = dict(originally_deferred)
    ledger_path = state_store.ledger_path_for(session_uuid)
    if active_label and active_label not in labels:
        # Defensive inclusion only: an in-memory active_label the marker
        # itself never durably recorded (a crash between should_defer_
        # teardown deciding to defer and its own write landing) is still
        # reconciled here -- never silently dropped -- but this is NEVER a
        # substitute for the marker, which is the sole source a fresh
        # resume with no in-memory active_label at all (crash recovery)
        # relies on. Guarded against re-processing something ALREADY
        # durably resolved (a repeat call with a stale in-memory label a
        # caller kept around after a prior reconcile already finished it)
        # -- without this guard, a second call would re-derive the same
        # true evidence but append a REDUNDANT ledger revision and
        # needlessly repeat teardown, breaking idempotence.
        if not _label_already_terminal(ledger_path, transaction_id,
                                       active_label):
            labels[active_label] = {}

    if not labels:
        return {"transaction_id": transaction_id, "reconciled": [],
               "still_pending": []}

    reconciled = []
    still_pending = []
    resolved_labels = []
    newly_deferred = {}

    for label in sorted(labels):
        prior = _latest_ledger_record(ledger_path, transaction_id, label)
        terminal = bounded_evidence_wait(session_uuid, transaction_id,
                                         [label])
        attempt = terminal.get(label, {})
        evidence_state = attempt.get("evidence_state")

        if evidence_state == EVIDENCE_UNRESOLVED:
            # Still genuinely alive as of THIS check -- never torn down
            # while it might still be producing real evidence, never
            # fabricated a terminal outcome it does not yet have, and
            # never relaunched either. Stays deferred for a later pass --
            # durably recorded if it was only defensively included above
            # (never on the marker to begin with), so a LATER crash-
            # recovery call with no active_label hint at all can still
            # find it.
            still_pending.append(label)
            if label not in originally_deferred:
                newly_deferred[label] = labels.get(label) or {}
            continue

        exit_code = attempt.get("exit_code")
        timed_out = bool(attempt.get("timed_out"))
        if evidence_state == EVIDENCE_PRESENT:
            if timed_out:
                exit_status, adjudication = "timeout", "fail"
            elif exit_code == 0:
                exit_status, adjudication = "pass", "pass"
            else:
                exit_status, adjudication = "fail", "fail"
        else:
            # EVIDENCE_ABSENT: genuinely gone, nothing observed -- persisted
            # with bounded diagnostics, never a fabricated pass/fail.
            exit_status, adjudication = "unknown", "unknown"
        fields = {
            "exit_code": exit_code,
            "evidence_state": evidence_state,
            "timed_out": timed_out,
            "wall_time_s": attempt.get("wall_time_s"),
            "exit_status": exit_status,
            "adjudication": adjudication,
            "reconciled_at": _utc_now(),
            "reconciliation_note": attempt.get("note"),
        }
        if evidence_state == EVIDENCE_ABSENT:
            fields["absent_diagnostics"] = {
                "checked_at": _utc_now(),
                "last_known_pgid": (labels.get(label) or {}).get("pgid"),
            }
        if prior:
            for carry in ("verification_kind", "command_fingerprint",
                         "observed_source_digest"):
                if carry in prior:
                    fields[carry] = prior[carry]
        _record, ledger_ok = _revise_attempt_ledger_with_retry(
            ledger_path, transaction_id, label, fields=fields,
            attempt_state="terminal")
        reconciled.append({"label": label, "evidence_state": evidence_state,
                           "ledger_ok": ledger_ok})
        if ledger_ok:
            resolved_labels.append(label)
        # else: the ledger revision itself did not durably land -- this
        # label stays on the marker for a later reconciliation pass to
        # retry the WRITE (the evidence itself has already been
        # determined and will simply be re-derived identically next time,
        # never re-fabricated).

    _update_deferred_marker(session_uuid, transaction_id, add=newly_deferred,
                            remove=resolved_labels)

    if not still_pending and _safe_to_teardown_worker(
            session_uuid, transaction_id, set(labels)):
        # OWNERSHIP-SAFE (M5C-V2-B3): `cleanup_active_command_group` is
        # frozen spine code this candidate cannot edit -- it TERM/KILLs the
        # published pgid directly and only tolerates `ProcessLookupError`
        # itself. A pid/pgid the OS has since reused for a process this
        # supervisor does not own signals `PermissionError` (EPERM) instead
        # -- exactly the "not ours, never touch it" case `_pgid_alive`
        # already treats as inconclusive (see its own docstring) rather
        # than proof of anything. Tolerated here at the call site so a
        # permission denial degrades to "left for a later pass", never a
        # crashed reconciliation and never a second, more aggressive signal
        # attempt against a group this process was just told it may not
        # touch.
        active_before_cleanup = state_store.read_json_tolerant(
            state_store.verification_active_pgid_path_for(
                session_uuid, transaction_id))
        active_pgid = (active_before_cleanup.get("pgid")
                      if isinstance(active_before_cleanup, dict) else None)
        # M5C-V2 (teardown determinism across zombie/reaping): the active
        # command's pgid IS its own leader pid (every command this seam
        # ever publishes is `start_new_session=True`, whose pgid equals
        # its own pid by POSIX `setsid` semantics -- see
        # `_pgid_truthfully_alive`'s own docstring). `cleanup_active_
        # command_group` above is frozen spine code that cannot reap what
        # it just killed, so its OWN `_pgid_alive` polling loop would
        # otherwise see an unreaped zombie for its entire grace period
        # even though the command is already unambiguously dead.
        # `_ConcurrentReaper` races that loop with a never-a-signal
        # background reap so it converges as soon as the process actually
        # exits, not merely once that frozen function gives up waiting.
        with _ConcurrentReaper(active_pgid):
            try:
                _spine().cleanup_active_command_group(
                    session_uuid, transaction_id)
            except PermissionError:
                pass
        _teardown_worker_process_group(session_uuid, transaction_id)
        try:
            os.remove(state_store.verification_active_pgid_path_for(
                session_uuid, transaction_id))
        except OSError:
            pass

    return {"transaction_id": transaction_id, "reconciled": reconciled,
           "still_pending": still_pending}
