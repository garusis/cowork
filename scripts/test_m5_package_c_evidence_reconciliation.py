#!/usr/bin/env python3
"""The truthful `should_defer_teardown` reconciliation-pending predicate
and the `reconcile_pending_evidence` resume-time reconciliation entry point
in `cowork_verification_evidence.py`.

Never invokes a real Claude/Codex/opencode session. Real, short-lived
`python3` subprocesses (own process group via `start_new_session=True`) are
used wherever genuine process/process-group liveness evidence is under
test -- `_pgid_alive` is real OS-level evidence, not something a fake can
stand in for without hollowing out the very thing this package must prove.

Run standalone:

    python3 -m unittest scripts/test_m5_package_c_evidence_reconciliation.py -v
"""

import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_state as state_store  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_verification as verification  # noqa: E402
import cowork_verification_evidence as evidence_module  # noqa: E402


def _spawn_sleeper(seconds):
    """A real, disposable subprocess in its OWN process group -- genuine
    `_pgid_alive` evidence, not a fake standing in for it. Returns
    `(proc, pgid)`."""
    proc = subprocess.Popen(
        [sys.executable or "python3", "-c",
         "import time; time.sleep(%r)" % seconds],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    # Give the child a moment to actually exec and settle into its own
    # session/pgid before any caller reads it as "the" pgid.
    time.sleep(0.05)
    return proc, os.getpgid(proc.pid)


def _reap(proc):
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001 - best-effort teardown in tests
        pass


def _spawn_and_reap_dead_pid():
    """A pid/pgid GUARANTEED gone: a trivial subprocess, waited out fully.
    Not vulnerable to PID reuse within a test's short lifetime."""
    proc = subprocess.Popen(
        [sys.executable or "python3", "-c", "pass"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    pgid = os.getpgid(proc.pid)
    proc.wait(timeout=5)
    deadline = time.time() + 2
    while verification._pgid_alive(pgid) and time.time() < deadline:
        time.sleep(0.05)
    return pgid


class _SessionFixture(unittest.TestCase):
    """Isolated COWORK_SESSIONS_ROOT, a fresh session/transaction id pair,
    and a registry of spawned real subprocesses this test owns -- always
    reaped in tearDown, even on failure."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.transaction_id = "T-" + uuid.uuid4().hex[:8]
        self._procs = []
        self.addCleanup(self._reap_all)

    def _reap_all(self):
        for proc in self._procs:
            _reap(proc)

    def spawn_sleeper(self, seconds=10):
        proc, pgid = _spawn_sleeper(seconds)
        self._procs.append(proc)
        return proc, pgid

    def write_active_pgid(self, pgid, label):
        state_store.write_json_atomic(
            state_store.verification_active_pgid_path_for(
                self.session_uuid, self.transaction_id),
            {"pgid": pgid, "label": label, "started_at": "2026-01-01T00:00:00Z"})

    def write_worker_identity(self, pid, reported_at=None):
        # A REALISTIC "now" timestamp by default -- not a stale fixed
        # literal -- so `_worker_pid_start_corroborated`'s real `ps`-based
        # start-time check (guarding against tearing down an unrelated,
        # unauthorized process group after a pid gets reused) actually
        # corroborates a genuinely just-spawned test fixture process.
        # Tests that specifically want to prove the corroboration check
        # REFUSES a stale/mismatched identity pass their own
        # `reported_at`.
        state_store.write_json_atomic(
            state_store.verification_worker_identity_path_for(
                self.session_uuid, self.transaction_id),
            {"source_hash": "deadbeef", "protocol_version": 3, "pid": pid,
             "reported_at": reported_at or evidence_module._utc_now()})

    def write_terminal_event(self, label, **fields):
        event = {"event": "terminal", "label": label, "at":
                "2026-01-01T00:00:00Z"}
        event.update(fields)
        state_store.append_jsonl_atomic(
            state_store.verification_attempt_events_path_for(
                self.session_uuid, self.transaction_id), event)

    def mint(self, label, command=None):
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        return ledger.mint_owned_attempt(
            ledger_path, self.transaction_id, label,
            fields={"command": command or ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot"})

    def latest_ledger_record(self, label):
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        return evidence_module._latest_ledger_record(
            ledger_path, self.transaction_id, label)

    def ledger_records(self, label):
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        key = ledger.owned_attempt_key(self.transaction_id, label)
        return [r for r in ledger.read_ledger(ledger_path)
               if r.get("attempt_key") == key]


# =========================================================================== #
# bounded_evidence_wait's liveness-informed UNRESOLVED-vs-ABSENT fallback.     #
# =========================================================================== #


class BoundedEvidenceWaitLivenessFallbackTests(_SessionFixture):

    def test_unresolved_when_process_group_still_alive_after_the_window(self):
        _proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "slow")
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["slow"],
            poll_attempts=1, poll_delay_s=0.01)
        attempt = terminal["slow"]
        self.assertEqual(attempt["evidence_state"],
                         evidence_module.EVIDENCE_UNRESOLVED)
        self.assertIn("still alive", attempt["note"])
        self.assertIsNone(attempt["exit_code"])

    def test_absent_when_no_active_pgid_publication_exists_at_all(self):
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["never"],
            poll_attempts=1, poll_delay_s=0.01)
        self.assertEqual(terminal["never"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)

    def test_absent_when_active_pgid_names_a_different_label(self):
        _proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "other-label")
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["slow"],
            poll_attempts=1, poll_delay_s=0.01)
        self.assertEqual(terminal["slow"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)

    def test_absent_when_published_pgid_is_confirmed_genuinely_dead(self):
        dead_pgid = _spawn_and_reap_dead_pid()
        self.write_active_pgid(dead_pgid, "gone")
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gone"],
            poll_attempts=1, poll_delay_s=0.01)
        self.assertEqual(terminal["gone"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)

    def test_a_landed_terminal_event_always_takes_precedence_over_liveness(self):
        self.write_terminal_event(
            "done", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["done"],
            poll_attempts=1, poll_delay_s=0.01)
        self.assertEqual(terminal["done"]["evidence_state"],
                         evidence_module.EVIDENCE_PRESENT)
        self.assertEqual(terminal["done"]["exit_code"], 0)

    def test_never_relaunches_the_underlying_command(self):
        self.write_active_pgid(None, "x")
        with mock.patch("subprocess.Popen", side_effect=AssertionError(
                "bounded_evidence_wait must never launch a subprocess")):
            evidence_module.bounded_evidence_wait(
                self.session_uuid, self.transaction_id, ["x", "never"],
                poll_attempts=2, poll_delay_s=0.01)


# =========================================================================== #
# should_defer_teardown: truthful predicate, durable label SET (M5A-R-m6).    #
# =========================================================================== #


class ShouldDeferTeardownPredicateTests(_SessionFixture):

    def test_false_when_nothing_ever_evidenced_possibly_alive(self):
        self.assertIs(evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "never-started"), False)
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.verification_deferred_reconciliation_path_for(
                self.session_uuid, self.transaction_id)))

    def test_false_for_none_session_or_transaction_never_raises(self):
        self.assertIs(
            evidence_module.should_defer_teardown(None, None, None), False)
        self.assertIs(evidence_module.should_defer_teardown(
            self.session_uuid, None, "x"), False)
        self.assertIs(evidence_module.should_defer_teardown(
            None, self.transaction_id, "x"), False)

    def test_true_and_marker_records_the_label_when_genuinely_alive(self):
        _proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "a")
        self.assertIs(evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "a"), True)
        marker = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        self.assertEqual(set(marker["labels"]), {"a"})
        self.assertEqual(marker["labels"]["a"]["pgid"], pgid)

    def test_earlier_deferred_label_survives_a_later_different_consult(self):
        # M5A-R-m6, proven directly: the spine's own local `active_label`
        # variable would have been overwritten to "b" (and later reset to
        # None) by this point -- this durable set must not lose "a".
        _proc_a, pgid_a = self.spawn_sleeper(5)
        self.write_active_pgid(pgid_a, "a")
        self.assertTrue(evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "a"))

        dead_pgid = _spawn_and_reap_dead_pid()
        self.write_active_pgid(dead_pgid, "b")
        # "b" itself is not evidenced alive -- but "a" must still be
        # remembered, so this must STILL return True.
        self.assertTrue(evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "b"))
        # The finally block's own consult, with active_label reset to None.
        self.assertTrue(evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, None))

        marker = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        self.assertEqual(set(marker["labels"]), {"a"},
                         "'b' was never evidenced alive and must never be "
                         "added; 'a' must never be dropped")

    def test_idempotent_second_consult_of_the_same_alive_label_does_not_rewrite(self):
        _proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "a")
        evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "a")
        before = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "a")
        after = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        self.assertEqual(before, after)

    def test_never_redefers_an_already_resolved_label(self):
        # M5 Package C correction: a label whose ledger attempt is ALREADY
        # terminal (resolved by an earlier consult in this run, or by a
        # prior reconcile_pending_evidence pass) must never be re-added to
        # the deferred set just because a caller still happens to be
        # holding its label and it happens to still show up as pgid-alive
        # (e.g. a reused pgid) -- otherwise the finally block could defer
        # teardown forever for something genuinely done.
        self.mint("x")
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "x",
            fields={"evidence_state": evidence_module.EVIDENCE_PRESENT,
                   "exit_code": 0}, attempt_state="terminal")
        proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "x")
        self.assertIs(evidence_module.should_defer_teardown(
            self.session_uuid, self.transaction_id, "x"), False)
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.verification_deferred_reconciliation_path_for(
                self.session_uuid, self.transaction_id)))

    def test_never_relaunches_and_never_tears_anything_down(self):
        _proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "a")
        with mock.patch("subprocess.Popen", side_effect=AssertionError(
                "should_defer_teardown must never launch a subprocess")), \
             mock.patch.object(verification, "cleanup_active_command_group",
                               side_effect=AssertionError(
                                   "must never tear down")), \
             mock.patch.object(verification, "terminate_worker",
                               side_effect=AssertionError(
                                   "must never tear down")):
            evidence_module.should_defer_teardown(
                self.session_uuid, self.transaction_id, "a")


# =========================================================================== #
# reconcile_pending_evidence: late-evidence revision, dead-without-evidence,   #
# eventual teardown, crash/resume, idempotence, negative double-cleanup.      #
# =========================================================================== #


class LateEvidenceReconciliationTests(_SessionFixture):

    def test_late_evidence_revises_unresolved_ledger_to_true_terminal_present(self):
        minted = self.mint("slow")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "slow",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"slow": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})

        def _write_late():
            time.sleep(0.2)
            self.write_terminal_event(
                "slow", evidence_state=evidence_module.EVIDENCE_PRESENT,
                exit_code=0, timed_out=False, wall_time_s=0.2)
        threading.Thread(target=_write_late, daemon=True).start()

        result = evidence_module.reconcile_pending_evidence(
            self.session_uuid, self.transaction_id, "slow")
        self.assertEqual(result["still_pending"], [])
        self.assertEqual(len(result["reconciled"]), 1)
        self.assertEqual(result["reconciled"][0]["evidence_state"],
                         evidence_module.EVIDENCE_PRESENT)
        self.assertTrue(result["reconciled"][0]["ledger_ok"])

        records = self.ledger_records("slow")
        self.assertEqual([r["id"] for r in records], [minted["id"]] * len(records))
        latest = records[-1]
        self.assertEqual(latest["attempt_state"], "terminal")
        self.assertEqual(latest["evidence_state"], evidence_module.EVIDENCE_PRESENT)
        self.assertEqual(latest["exit_status"], "pass")
        self.assertEqual(latest["adjudication"], "pass")
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.verification_deferred_reconciliation_path_for(
                self.session_uuid, self.transaction_id)))

    def test_eventual_process_group_teardown_once_everything_is_resolved(self):
        minted = self.mint("slow")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "slow",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")

        cmd_proc, cmd_pgid = self.spawn_sleeper(10)
        worker_proc, worker_pgid = self.spawn_sleeper(10)
        self.write_active_pgid(cmd_pgid, "slow")
        self.write_worker_identity(worker_proc.pid)
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"slow": {"pgid": cmd_pgid, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "slow", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)

        self.assertTrue(verification._pgid_alive(cmd_pgid))
        self.assertTrue(verification._pgid_alive(worker_pgid))

        result = evidence_module.reconcile_pending_evidence(
            self.session_uuid, self.transaction_id, "slow")
        self.assertEqual(result["still_pending"], [])

        deadline = time.time() + 3
        while (verification._pgid_alive(cmd_pgid)
              or verification._pgid_alive(worker_pgid)) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(verification._pgid_alive(cmd_pgid),
                         "the active command's own process group must be "
                         "torn down once reconciliation is complete")
        self.assertFalse(verification._pgid_alive(worker_pgid),
                         "the worker's own process group must be torn down "
                         "once nothing for the transaction is left possibly "
                         "alive")
        _reap(cmd_proc)
        _reap(worker_proc)

    def test_still_alive_stays_deferred_never_torn_down_or_fabricated(self):
        minted = self.mint("x")
        self.assertIsNotNone(minted)
        cmd_proc, cmd_pgid = self.spawn_sleeper(10)
        self.write_active_pgid(cmd_pgid, "x")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"x": {"pgid": cmd_pgid, "deferred_at": "2026-01-01T00:00:00Z"}})

        with mock.patch.object(verification, "cleanup_active_command_group",
                               side_effect=AssertionError(
                                   "must never tear down a still-alive "
                                   "process")):
            result = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, "x")

        self.assertEqual(result["still_pending"], ["x"])
        self.assertEqual(result["reconciled"], [])
        self.assertTrue(verification._pgid_alive(cmd_pgid))
        marker = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        self.assertEqual(set(marker["labels"]), {"x"})
        # No fabricated terminal outcome for something never actually
        # resolved (M5C-V2-B2): the only ledger record for "x" is the
        # ORIGINAL pending mint from `self.mint("x")` above -- asserting an
        # empty list here would also be satisfied by a bug that silently
        # discarded that legitimate mint, and would spuriously fail on the
        # legitimate record alone; the real property under test is that
        # reconciliation itself appended NOTHING (no "unresolved", no
        # "terminal") on top of it.
        records = self.ledger_records("x")
        self.assertEqual([r["id"] for r in records], [minted["id"]])
        self.assertEqual(records[-1]["attempt_state"], "pending")
        self.assertNotIn(records[-1]["attempt_state"],
                         ("terminal", "unresolved"))
        _reap(cmd_proc)


class DeadWithoutEvidenceAbsentTests(_SessionFixture):

    def test_confirmed_dead_process_persists_absent_with_bounded_diagnostics(self):
        minted = self.mint("gone")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "gone",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        dead_pgid = _spawn_and_reap_dead_pid()
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"gone": {"pgid": dead_pgid, "deferred_at": "2026-01-01T00:00:00Z"}})
        # No active_pgid.json publication at all, and no terminal event --
        # genuinely gone with nothing observed.

        with mock.patch("subprocess.Popen", side_effect=AssertionError(
                "must never relaunch the command")):
            result = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, "gone")

        self.assertEqual(result["still_pending"], [])
        self.assertEqual(result["reconciled"],
                         [{"label": "gone",
                           "evidence_state": evidence_module.EVIDENCE_ABSENT,
                           "ledger_ok": True}])
        records = self.ledger_records("gone")
        latest = records[-1]
        self.assertEqual(latest["attempt_state"], "terminal")
        self.assertEqual(latest["evidence_state"], evidence_module.EVIDENCE_ABSENT)
        self.assertIsNone(latest["exit_code"])
        diagnostics = latest["absent_diagnostics"]
        self.assertIn("checked_at", diagnostics)
        self.assertEqual(diagnostics["last_known_pgid"], dead_pgid)
        # Bounded: a small, fixed-shape diagnostics dict, never an
        # unbounded log/capture.
        self.assertLessEqual(len(diagnostics), 4)


# =========================================================================== #
# Crash/resume, idempotence, negative double-cleanup controls.                #
# =========================================================================== #


class CrashResumeIdempotenceTests(_SessionFixture):

    def test_resolves_purely_from_the_durable_marker_with_no_active_label(self):
        # Simulates a supervisor crash: the caller has NO in-memory
        # active_label at all, only the durable marker on disk.
        minted = self.mint("slow")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "slow",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"slow": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "slow", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=1, timed_out=False)

        result = evidence_module.reconcile_pending_evidence(
            self.session_uuid, self.transaction_id, None)
        self.assertEqual(result["still_pending"], [])
        self.assertEqual(len(result["reconciled"]), 1)
        latest = self.ledger_records("slow")[-1]
        self.assertEqual(latest["exit_status"], "fail")

    def test_no_op_when_nothing_was_ever_deferred(self):
        with mock.patch.object(verification, "cleanup_active_command_group",
                               side_effect=AssertionError(
                                   "no-op must touch no process group")):
            result = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, None)
        self.assertEqual(result, {"transaction_id": self.transaction_id,
                                  "reconciled": [], "still_pending": []})

    def test_idempotent_second_call_after_full_reconciliation_is_a_no_op(self):
        minted = self.mint("slow")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "slow",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"slow": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "slow", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)
        evidence_module.reconcile_pending_evidence(
            self.session_uuid, self.transaction_id, "slow")
        before_count = len(self.ledger_records("slow"))

        with mock.patch.object(verification, "cleanup_active_command_group",
                               side_effect=AssertionError(
                                   "no orphan double teardown")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError(
                 "no relaunch on a repeat call")):
            second = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, "slow")

        self.assertEqual(second, {"transaction_id": self.transaction_id,
                                  "reconciled": [], "still_pending": []})
        self.assertEqual(len(self.ledger_records("slow")), before_count,
                         "a second reconciliation pass must append no new "
                         "ledger revision")

    def test_double_teardown_of_an_already_dead_process_group_is_not_an_error(self):
        dead_pgid = _spawn_and_reap_dead_pid()
        self.write_active_pgid(dead_pgid, "gone")
        verification.cleanup_active_command_group(
            self.session_uuid, self.transaction_id)
        # Second, redundant teardown of the SAME already-empty group must
        # not raise.
        verification.cleanup_active_command_group(
            self.session_uuid, self.transaction_id)
        self.write_worker_identity(dead_pgid)
        evidence_module._teardown_worker_process_group(
            self.session_uuid, self.transaction_id)
        evidence_module._teardown_worker_process_group(
            self.session_uuid, self.transaction_id)

    def test_safe_to_teardown_worker_never_kills_an_unrelated_still_running_entry(self):
        minted = self.mint("done")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "done",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"done": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "done", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)

        # active_pgid.json currently names a DIFFERENT, never-deferred
        # label the spine has since moved on to -- this pass must never
        # reach in and kill it.
        other_proc, other_pgid = self.spawn_sleeper(10)
        worker_proc, worker_pgid = self.spawn_sleeper(10)
        self.write_active_pgid(other_pgid, "other-in-progress")
        self.write_worker_identity(worker_proc.pid)

        result = evidence_module.reconcile_pending_evidence(
            self.session_uuid, self.transaction_id, "done")
        self.assertEqual(result["still_pending"], [])

        time.sleep(0.2)
        self.assertTrue(verification._pgid_alive(other_pgid),
                        "an entry this pass never handled must never be "
                        "killed")
        self.assertTrue(verification._pgid_alive(worker_pgid),
                        "the worker must not be torn down while something "
                        "this pass never touched is still active")
        _reap(other_proc)
        _reap(worker_proc)

    def test_reconcile_never_clobbers_a_concurrently_added_deferred_label(self):
        # M5 Package C correction: a marker mutation concurrent with an
        # in-flight reconciliation pass (a live should_defer_teardown
        # consult for a DIFFERENT, still-live entry of the SAME
        # transaction, landing WHILE this pass is still bounded-polling an
        # earlier label) must never be silently dropped by this pass's own
        # final marker write.
        self.mint("b")
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "b",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"b": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "b", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)

        real_wait = evidence_module.bounded_evidence_wait
        injected = {"done": False}

        def wait_with_concurrent_write(*args, **kwargs):
            if not injected["done"]:
                injected["done"] = True
                evidence_module._update_deferred_marker(
                    self.session_uuid, self.transaction_id,
                    add={"c": {"pgid": 424242,
                              "deferred_at": "2026-01-01T00:00:00Z"}})
            return real_wait(*args, **kwargs)

        with mock.patch.object(evidence_module, "bounded_evidence_wait",
                               side_effect=wait_with_concurrent_write):
            result = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, "b")

        self.assertEqual(result["still_pending"], [])
        self.assertEqual([r["label"] for r in result["reconciled"]], ["b"])
        marker = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        self.assertEqual(
            set(marker["labels"]), {"c"},
            "the concurrently-added label 'c' must survive this "
            "reconciliation pass's own marker write; 'b' must be removed "
            "since it was genuinely resolved")

    def test_defensively_included_still_alive_label_is_persisted_not_fabricated(self):
        # A stale in-memory active_label the marker never durably recorded
        # (e.g. a crash between should_defer_teardown's own decision and
        # its write landing), when it turns out STILL genuinely alive,
        # must be durably added to the marker (so a LATER crash-recovery
        # call with no active_label hint at all can still find it) and
        # must never be given a fabricated terminal ledger outcome.
        minted = self.mint("x")
        proc, pgid = self.spawn_sleeper(10)
        self.write_active_pgid(pgid, "x")
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.verification_deferred_reconciliation_path_for(
                self.session_uuid, self.transaction_id)))

        result = evidence_module.reconcile_pending_evidence(
            self.session_uuid, self.transaction_id, "x")

        self.assertEqual(result["still_pending"], ["x"])
        self.assertEqual(result["reconciled"], [])
        # M5C-V2-B2: the only ledger record for "x" must still be the
        # ORIGINAL pending mint -- an empty-list assertion here would be
        # trivially (and wrongly) satisfied by a bug that dropped that
        # legitimate record too; the real property is that this defensive-
        # inclusion path appended no "unresolved"/"terminal" revision on
        # top of it for something still genuinely alive.
        records = self.ledger_records("x")
        self.assertEqual([r["id"] for r in records], [minted["id"]],
                         "no fabricated terminal outcome for something "
                         "still genuinely alive")
        self.assertEqual(records[-1]["attempt_state"], "pending")
        marker = evidence_module._read_deferred_marker(
            self.session_uuid, self.transaction_id)
        self.assertEqual(
            set(marker["labels"]), {"x"},
            "a defensively-included still-alive label must be durably "
            "persisted, not silently dropped")

    def test_worker_teardown_refuses_a_pid_that_postdates_the_identity_report(self):
        # PID-reuse guard: never target an unrelated/unauthorized process
        # group. A pid that EXISTS but started AFTER the recorded identity
        # report cannot be the same worker process this transaction
        # actually spawned.
        proc, pgid = self.spawn_sleeper(5)
        self.write_worker_identity(proc.pid,
                                   reported_at="2020-01-01T00:00:00Z")
        evidence_module._teardown_worker_process_group(
            self.session_uuid, self.transaction_id)
        time.sleep(0.2)
        self.assertTrue(
            verification._pgid_alive(pgid),
            "a pid that could not be corroborated against its recorded "
            "identity report must never be torn down")

    def test_worker_teardown_proceeds_for_a_corroborated_pid(self):
        proc, pgid = self.spawn_sleeper(5)
        self.write_worker_identity(proc.pid)  # realistic "now" reported_at
        evidence_module._teardown_worker_process_group(
            self.session_uuid, self.transaction_id)
        deadline = time.time() + 3
        while verification._pgid_alive(pgid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(verification._pgid_alive(pgid))


# =========================================================================== #
# C2-REV-B1/C2-REV-M2: timezone-safe pid-start corroboration, proven with     #
# REALISTIC minutes-scale boundaries on a genuinely non-UTC host -- the      #
# independent review's own six-year-gap vacuity closed, not merely noted.    #
# =========================================================================== #


class _NonUtcHostFixture(_SessionFixture):
    """Forces a REAL, fixed, non-UTC system timezone offset (`TZ=Etc/GMT+5`,
    i.e. UTC-5 with no DST -- the exact class of host, `TZ=-0500`, the
    independent review's own C2-REV-B1 evidence was collected on) for the
    duration of each test, restored in `tearDown` regardless of whatever
    timezone the machine actually running this suite happens to be in. `ps`
    (spawned by `_worker_pid_start_corroborated`/`_teardown_worker_process_
    group` as a genuine child process) inherits this process's environment,
    so it formats `lstart=` in the SAME forced non-UTC local time this
    test's own UTC-instant arithmetic below is computed against -- making
    the boundary genuinely non-UTC and deterministic on any CI host,
    UTC-based or not."""

    NON_UTC_TZ = "Etc/GMT+5"

    def setUp(self):
        super().setUp()
        self._prior_tz = os.environ.get("TZ")
        os.environ["TZ"] = self.NON_UTC_TZ
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._prior_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._prior_tz
        time.tzset()


class PidStartTimezoneSafeCorroborationBoundaryTests(_NonUtcHostFixture):
    """Realistic, MINUTES-scale near-boundary proof that
    `_worker_pid_start_corroborated` truthfully interprets `ps -o lstart=`'s
    naive LOCAL output rather than silently treating it as UTC (C2-REV-B1):
    on this fixed `UTC-5` host, the pre-fix bug widened the acceptance
    window by exactly five hours, which a five-MINUTE gap -- unlike the
    six-YEAR gap the independent review found insufficient (C2-REV-M2) --
    is far too small to survive."""

    def test_accepts_a_pid_that_started_a_few_minutes_before_a_realistic_report(self):
        # Realistic ordering: the worker starts, then reports its own
        # identity a couple of minutes later -- comfortably inside the
        # corroboration window regardless of this host's non-UTC offset.
        proc, _pgid = self.spawn_sleeper(3)
        real_started_dt = datetime.datetime.fromisoformat(
            evidence_module._utc_now().replace("Z", "+00:00"))
        reported_at = (real_started_dt + datetime.timedelta(
            minutes=2)).isoformat().replace("+00:00", "Z")
        self.assertTrue(
            evidence_module._worker_pid_start_corroborated(
                proc.pid, reported_at),
            "a pid that genuinely started minutes before a realistic "
            "identity report must be corroborated on a non-UTC host")

    def test_refuses_a_pid_that_started_minutes_after_a_stale_report(self):
        # A STALE identity report from a few minutes ago -- the pid under
        # test right now genuinely started AFTER it, exactly the reused-pid
        # shape this guard exists to catch. Under the pre-fix bug (naive
        # local forced to UTC), this host's five-hour offset would have
        # completely masked a five-MINUTE gap and falsely corroborated it
        # (see the module docstring's M5C-V3 note and the independent
        # review's own C2-REV-B1 evidence).
        proc, _pgid = self.spawn_sleeper(3)
        reported_at = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(minutes=5)).isoformat().replace(
                          "+00:00", "Z")
        self.assertFalse(
            evidence_module._worker_pid_start_corroborated(
                proc.pid, reported_at),
            "a pid that genuinely started minutes after a stale identity "
            "report must be refused on a non-UTC host")

    def test_teardown_end_to_end_refuses_a_pid_minutes_after_a_stale_report(self):
        # The same near-boundary refusal, proven through the real
        # `_teardown_worker_process_group` call site (the fixed gate's
        # actual guard), not merely the helper in isolation.
        proc, pgid = self.spawn_sleeper(5)
        reported_at = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(minutes=5)).isoformat().replace(
                          "+00:00", "Z")
        self.write_worker_identity(proc.pid, reported_at=reported_at)
        evidence_module._teardown_worker_process_group(
            self.session_uuid, self.transaction_id)
        time.sleep(0.2)
        self.assertTrue(
            verification._pgid_alive(pgid),
            "a pid that genuinely started minutes after a stale identity "
            "report must never be torn down, even on a non-UTC host")

    def test_teardown_end_to_end_proceeds_for_a_pid_reported_minutes_later(self):
        proc, pgid = self.spawn_sleeper(5)
        real_started_dt = datetime.datetime.fromisoformat(
            evidence_module._utc_now().replace("Z", "+00:00"))
        reported_at = (real_started_dt + datetime.timedelta(
            minutes=2)).isoformat().replace("+00:00", "Z")
        self.write_worker_identity(proc.pid, reported_at=reported_at)
        evidence_module._teardown_worker_process_group(
            self.session_uuid, self.transaction_id)
        deadline = time.time() + 3
        while verification._pgid_alive(pgid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(
            verification._pgid_alive(pgid),
            "a genuinely corroborated pid on a non-UTC host must still be "
            "torn down")


class LocalNaiveTimestampAmbiguityTests(unittest.TestCase):
    """Direct coverage of `_local_naive_to_aware`'s fail-safe handling of a
    genuinely AMBIGUOUS local wall-clock reading (a DST fall-back's repeated
    hour) -- required by C2-REV-B1's "malformed, ambiguous, or unavailable
    evidence" fail-closed contract now that this candidate performs a real
    local-to-aware conversion instead of a blind UTC label."""

    def setUp(self):
        self._prior_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._prior_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._prior_tz
        time.tzset()

    def test_refuses_an_ambiguous_dst_fallback_local_time(self):
        # 2025-11-02 01:30 local occurs TWICE in America/New_York (the DST
        # fall-back hour): two distinct real UTC instants share this exact
        # local wall-clock reading, so it can never be truthfully resolved
        # to a single instant -- refuse rather than guess.
        ambiguous_naive = datetime.datetime(2025, 11, 2, 1, 30, 0)
        self.assertIsNone(
            evidence_module._local_naive_to_aware(ambiguous_naive))

    def test_resolves_an_unambiguous_local_time_correctly(self):
        unambiguous_naive = datetime.datetime(2025, 6, 15, 12, 0, 0)
        resolved = evidence_module._local_naive_to_aware(unambiguous_naive)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.replace(tzinfo=None), unambiguous_naive)
        self.assertEqual(resolved.utcoffset(), datetime.timedelta(hours=-4))

    def test_worker_pid_start_corroborated_refuses_an_ambiguous_ps_reading(self):
        fake_result = mock.Mock(returncode=0,
                                stdout="Sun Nov  2 01:30:00 2025\n")
        with mock.patch("subprocess.run", return_value=fake_result):
            self.assertFalse(
                evidence_module._worker_pid_start_corroborated(
                    99999, "2025-11-02T06:00:00Z"))


# =========================================================================== #
# Ownership-safe cleanup tolerates EPERM/PermissionError (M5C-V2-B3): never   #
# crashes reconciliation, never escalates against a group it may not touch.  #
# =========================================================================== #


class OwnershipSafeCleanupToleratesPermissionDeniedTests(_SessionFixture):

    def test_teardown_worker_process_group_tolerates_epermission_on_getpgid(self):
        proc, pgid = self.spawn_sleeper(5)
        self.write_worker_identity(proc.pid)
        with mock.patch("os.getpgid", side_effect=PermissionError(1, "eperm")):
            # Must not raise -- a denied getpgid is exactly as inconclusive
            # as a ProcessLookupError, and must be tolerated the same way.
            evidence_module._teardown_worker_process_group(
                self.session_uuid, self.transaction_id)
        self.assertTrue(verification._pgid_alive(pgid),
                        "nothing may be signaled once ownership could not "
                        "even be determined")

    def test_teardown_worker_process_group_tolerates_epermission_on_sigterm(self):
        proc, pgid = self.spawn_sleeper(5)
        self.write_worker_identity(proc.pid)
        with mock.patch("os.killpg",
                        side_effect=PermissionError(1, "eperm")):
            evidence_module._teardown_worker_process_group(
                self.session_uuid, self.transaction_id)
        self.assertTrue(verification._pgid_alive(pgid),
                        "a permission-denied TERM must never be treated as "
                        "having torn anything down, and must never crash")

    def test_teardown_worker_process_group_tolerates_epermission_on_sigkill(self):
        proc, pgid = self.spawn_sleeper(5)
        self.write_worker_identity(proc.pid)
        calls = []

        def flaky_killpg(target_pgid, sig):
            calls.append(sig)
            if sig == signal.SIGTERM:
                return None
            raise PermissionError(1, "eperm")

        with mock.patch("os.killpg", side_effect=flaky_killpg), \
             mock.patch.object(verification, "_pgid_alive",
                               return_value=True), \
             mock.patch.object(verification, "DEFAULT_TERM_GRACE_S", 0.2):
            # `_pgid_alive` forced True so the grace-period loop always
            # escalates to the SIGKILL branch instead of exiting early
            # because the (unrelated, real) SIGTERM happened to work; the
            # grace period itself is shortened so this test does not have
            # to burn a real ~10s waiting it out.
            evidence_module._teardown_worker_process_group(
                self.session_uuid, self.transaction_id)
        self.assertIn(signal.SIGKILL, calls,
                     "escalation must still be attempted")
        _reap(proc)

    def test_reconcile_tolerates_permission_denied_active_command_cleanup(self):
        # Everything resolves genuinely (evidence PRESENT); the active-
        # command teardown this triggers hits the FROZEN
        # `cleanup_active_command_group`, which this candidate cannot edit
        # and which only tolerates `ProcessLookupError` itself -- the call
        # SITE here must still degrade gracefully rather than crash
        # reconciliation when it raises `PermissionError` instead.
        minted = self.mint("x")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "x",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"x": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "x", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)

        with mock.patch.object(
                verification, "cleanup_active_command_group",
                side_effect=PermissionError(1, "eperm")):
            result = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, "x")

        # No crash, and the genuinely-resolved evidence was still durably
        # recorded -- a permission denial on the (unrelated, frozen)
        # active-command teardown must never roll back or block an
        # already-true reconciliation result.
        self.assertEqual(result["still_pending"], [])
        self.assertEqual(len(result["reconciled"]), 1)
        self.assertTrue(result["reconciled"][0]["ledger_ok"])
        self.assertIsNone(state_store.read_json_tolerant(
            state_store.verification_deferred_reconciliation_path_for(
                self.session_uuid, self.transaction_id)))

    def test_reconcile_never_signals_a_pgid_reused_by_an_unrelated_process(self):
        # Ownership-safety, proven end-to-end through a full reconciliation
        # pass with BOTH the active-command and worker teardown paths
        # denied by the OS (PermissionError -- never ProcessLookupError,
        # which would honestly mean "already gone"): the pass must still
        # complete, the genuinely-resolved evidence must still be recorded,
        # and every signal attempt observed must target ONLY the two real
        # pgids this pass actually owns evidence for -- never a retry with
        # a stronger signal against a denied target, never a different or
        # wider one (e.g. pgid 0/-1).
        minted = self.mint("x")
        self.assertIsNotNone(minted)
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        ledger.revise_owned_attempt(
            ledger_path, self.transaction_id, "x",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        cmd_proc, cmd_pgid = self.spawn_sleeper(10)
        worker_proc, worker_pgid = self.spawn_sleeper(10)
        self.write_active_pgid(cmd_pgid, "x")
        self.write_worker_identity(worker_proc.pid)
        evidence_module._write_deferred_marker(
            self.session_uuid, self.transaction_id,
            {"x": {"pgid": cmd_pgid, "deferred_at": "2026-01-01T00:00:00Z"}})
        self.write_terminal_event(
            "x", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)

        killpg_calls = []

        def spy_killpg(target_pgid, sig):
            killpg_calls.append((target_pgid, sig))
            raise PermissionError(1, "eperm")

        with mock.patch("os.killpg", side_effect=spy_killpg):
            result = evidence_module.reconcile_pending_evidence(
                self.session_uuid, self.transaction_id, "x")

        self.assertEqual(result["still_pending"], [])
        self.assertEqual(len(result["reconciled"]), 1)
        self.assertTrue(result["reconciled"][0]["ledger_ok"])
        targeted_pgids = {pgid_ for pgid_, _sig in killpg_calls}
        self.assertTrue(targeted_pgids,
                        "this scenario must genuinely attempt at least one "
                        "signal for the test to prove anything")
        self.assertTrue(targeted_pgids.issubset({cmd_pgid, worker_pgid}),
                        "only the pgids this pass actually owns evidence "
                        "for may ever be signaled: %r" % targeted_pgids)
        # Neither real process was actually harmed -- every attempted
        # signal was denied (mocked), never delivered.
        self.assertTrue(verification._pgid_alive(cmd_pgid))
        self.assertTrue(verification._pgid_alive(worker_pgid))
        _reap(cmd_proc)
        _reap(worker_proc)


# =========================================================================== #
# Cancellation/deadline semantics preserved (end-to-end through the real      #
# spine, exactly the base commit's own frozen behavior).                     #
# =========================================================================== #


def _init_git_repo():
    d = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "config", "commit.gpgsign", "false"],
                   check=True)
    with open(os.path.join(d, "f.txt"), "w") as fh:
        fh.write("x")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


def _seed_worker_into_repo(repo):
    dest_dir = os.path.join(repo, "scripts")
    os.makedirs(dest_dir, exist_ok=True)
    for name in ("cowork_verification.py", "cowork_state.py",
                "cowork_policy.py", "cowork_ledger.py",
                "cowork_verification_worker.py",
                "cowork_verification_evidence.py"):
        shutil.copyfile(os.path.join(_HERE, name),
                        os.path.join(dest_dir, name))
    subprocess.run(["git", "-C", repo, "add", "scripts"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "seed worker"],
                   check=True)


class _RealWorkerFixture(unittest.TestCase):
    """A throwaway committed git repo seeded with the real, candidate
    worker modules and an isolated session root -- mirrors
    `scripts/test_m5_package_a_contracts.py`'s own fixture of the same
    shape, duplicated rather than imported so this suite stays
    self-contained (importing a sibling `test_*` module risks unittest's
    own discovery picking up ITS test classes a second time)."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        _seed_worker_into_repo(self.repo)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]


class CancellationDeadlineSemanticsPreservedTests(_RealWorkerFixture):

    def test_should_defer_teardown_is_the_exact_reexported_symbol(self):
        self.assertIs(verification.should_defer_teardown,
                      evidence_module.should_defer_teardown)

    def test_cancel_event_still_tears_down_immediately_regardless_of_deferral(self):
        calls = []
        real_cleanup = verification.cleanup_active_command_group
        real_terminate = verification.terminate_worker

        def spy_cleanup(*a, **k):
            calls.append("cleanup")
            return real_cleanup(*a, **k)

        def spy_terminate(*a, **k):
            calls.append("terminate")
            return real_terminate(*a, **k)

        signal_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(signal_dir, ignore_errors=True))
        started = os.path.join(signal_dir, "started")
        marker = os.path.join(signal_dir, "marker")
        cmd = ["python3", "-c",
              "import time\nopen(%r, 'w').write('started')\n"
              "time.sleep(2)\n"
              "open(%r, 'w').write('done')" % (started, marker)]
        entries = [{"label": "slow", "command": cmd,
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        cancel_event = threading.Event()
        observed = {}

        # Cancel only once the command itself reports it is running. A fixed
        # timer raced snapshot/worker startup: when it fired before entry 0
        # was dispatched, the pre-dispatch cancellation gate correctly
        # returned UNVERIFIED instead of exercising mid-flight teardown.
        def _cancel_once_command_started():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if os.path.exists(started):
                    observed["started_at"] = time.monotonic()
                    break
                time.sleep(0.02)
            cancel_event.set()
        threading.Thread(target=_cancel_once_command_started,
                         daemon=True).start()

        with mock.patch.object(verification, "cleanup_active_command_group",
                               side_effect=spy_cleanup), \
             mock.patch.object(verification, "terminate_worker",
                               side_effect=spy_terminate):
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries,
                cancel_event=cancel_event)

        # A cancel_event firing WHILE the only entry is already mid-flight
        # never sets `deadline_hit` (only the top-of-loop, before-a-NEW-
        # entry check does) -- this is the base commit's own,
        # Package-C-unchanged verdict shape: RED, exactly as an ordinary
        # non-zero/killed command would be, never fabricated as
        # UNVERIFIED. Asserted exactly (not merely "not GREEN") since this
        # is deterministic, identical to what Package A's own stub would
        # have produced for the same scenario -- should_defer_teardown
        # never returns True here (the process is already dead by the
        # time the bounded wait gives up), so this is unaffected by the
        # deferral seam at all.
        self.assertIn("started_at", observed,
                      "the command never reported it started: %r" % (
                          {k: result.get(k) for k in (
                              "verdict", "attempts", "startup_failure",
                              "worker_identity_verified",
                              "ledger_failure")},))
        self.assertEqual(result["verdict"], verification.VERDICT_RED)
        # Outlive the command's own 2s sleep so a surviving, un-torn-down
        # command would have written its marker by now.
        remaining = observed["started_at"] + 2.5 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.assertFalse(os.path.exists(marker),
                         "the command must have been torn down before it "
                         "could finish and write its marker")
        self.assertIn("cleanup", calls)
        self.assertIn("terminate", calls)

    def test_cancel_set_before_the_worker_even_starts_stays_unverified(self):
        # Companion to the mid-flight case above, mirroring Package A's own
        # established, deterministic pattern (no real-time race): cancel
        # fires before the worker is ever verified, so verdict is
        # UNVERIFIED via `worker_verified=False` -- this exact disposition
        # is untouched by Package C's deferral seam.
        cancel_event = threading.Event()
        cancel_event.set()
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        result = verification.run_transaction(
            self.repo, self.session_uuid, entries, cancel_event=cancel_event)
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)

    def test_should_defer_teardown_engages_end_to_end_through_the_real_spine(self):
        # A deterministic proof the seam is REALLY consulted by the live
        # spine (not merely unit-tested in isolation): a REAL, genuinely
        # still-running command (should_defer_teardown's own liveness
        # check, `_label_possibly_alive_pgid`, is never faked -- it reads
        # the worker's real active-command-group publication). Only the
        # EVIDENCE WAIT is faked, to a deterministic "not yet present"
        # without racing `command_timeout_s`'s own coupling to the
        # primary wait window -- everything downstream of that
        # (`should_defer_teardown`'s consult, the finally block's own
        # teardown gate) is the real, unmodified spine.
        #
        # M5C-V2-B1: the command's own duration must comfortably outlast
        # EVERY real delay `run_transaction` can still incur once teardown
        # is genuinely deferred -- in particular the `finally` block's own
        # (pre-existing, Package-C-unrelated) `startup_capture_thread.join(
        # timeout=5)`, which cannot return early while the worker's stdout
        # pipe stays open (the worker is deliberately being kept alive).
        # A command that could finish inside that up-to-5s window made this
        # test's own "still genuinely running" assertion below flaky --
        # true on a fast machine, silently FALSE (the command had already
        # finished) on a loaded one, which is exactly a truthful-marker-
        # state claim the test could not actually back up. 40s leaves a
        # wide, deterministic margin over that -- including the
        # `reconcile_pending_evidence` round-trip below, whose own default
        # bounded poll alone can take close to 10s.
        marker_path = os.path.join(tempfile.mkdtemp(), "marker")
        cmd = ["python3", "-c",
              "import time\ntime.sleep(40)\n"
              "open(%r, 'w').write('done')" % marker_path]
        entries = [{"label": "slow", "command": cmd,
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        calls = []
        real_cleanup = verification.cleanup_active_command_group
        real_terminate = verification.terminate_worker

        def spy_cleanup(*a, **k):
            calls.append("cleanup")
            return real_cleanup(*a, **k)

        def spy_terminate(*a, **k):
            calls.append("terminate")
            return real_terminate(*a, **k)

        def fake_wait(session_uuid, transaction_id, labels, poll_attempts,
                     poll_delay_s):
            # A short, REAL, deterministic delay -- not a race on the
            # worker's own subprocess-spawn variance -- so the worker has
            # genuinely started the command and published its pgid before
            # should_defer_teardown's own (real, unfaked) liveness check
            # runs, without waiting anywhere near the command's full
            # duration.
            time.sleep(0.3)
            return {"slow": {"evidence_state":
                             evidence_module.EVIDENCE_UNRESOLVED}}

        result = None
        try:
            with mock.patch.object(evidence_module, "bounded_evidence_wait",
                                   side_effect=fake_wait), \
                 mock.patch.object(verification,
                                   "cleanup_active_command_group",
                                   side_effect=spy_cleanup), \
                 mock.patch.object(verification, "terminate_worker",
                                   side_effect=spy_terminate):
                result = verification.run_transaction(
                    self.repo, self.session_uuid, entries)

            self.assertEqual(result["verdict"], verification.VERDICT_RED)
            self.assertEqual(
                calls, [],
                "should_defer_teardown must have genuinely deferred the "
                "finally block's own teardown calls for this real "
                "transaction")
            transaction_id = result["transaction_id"]
            marker = evidence_module._read_deferred_marker(
                self.session_uuid, transaction_id)
            self.assertIn("slow", marker["labels"])
            self.assertFalse(os.path.exists(marker_path),
                             "the command was genuinely still running, not "
                             "finished, at the moment of deferral")

            active = state_store.read_json_tolerant(
                state_store.verification_active_pgid_path_for(
                    self.session_uuid, transaction_id))
            self.assertIsNotNone(active)
            self.assertTrue(verification._pgid_alive(active["pgid"]),
                            "the deferred command must never have been "
                            "torn down or relaunched")

            # M5C-V2-B1: a resume-time reconciliation pass consulted WHILE
            # the command is STILL genuinely alive must retain the SAME
            # truthful durable marker state -- never contradicting the
            # spine's own deferral by removing "slow" (or tearing anything
            # down) for something reconcile_pending_evidence's own,
            # unfaked, `_pgid_alive`-backed re-check confirms is still
            # running.
            with mock.patch.object(verification,
                                   "cleanup_active_command_group",
                                   side_effect=spy_cleanup), \
                 mock.patch.object(verification, "terminate_worker",
                                   side_effect=spy_terminate):
                recon = evidence_module.reconcile_pending_evidence(
                    self.session_uuid, transaction_id, None)
            self.assertEqual(recon["still_pending"], ["slow"])
            self.assertEqual(recon["reconciled"], [])
            self.assertEqual(
                calls, [],
                "a still-genuinely-alive reconciliation pass must not "
                "tear anything down either")
            marker_after_reconcile = evidence_module._read_deferred_marker(
                self.session_uuid, transaction_id)
            self.assertEqual(
                marker_after_reconcile, marker,
                "the durable deferred marker must be byte-for-byte "
                "unchanged by a reconciliation pass that found the "
                "command still genuinely alive -- no contradictory "
                "removal of truthful marker state")
            self.assertFalse(os.path.exists(marker_path),
                             "still genuinely running after the "
                             "reconciliation pass too")
        finally:
            # Explicit cleanup: teardown was deliberately skipped above, so
            # this test owns tearing the real background processes down
            # itself rather than leaking them across the suite.
            self._kill_transaction_processes(
                result.get("transaction_id") if result else None)

    def _kill_transaction_processes(self, transaction_id):
        if not transaction_id:
            return
        import signal
        active = state_store.read_json_tolerant(
            state_store.verification_active_pgid_path_for(
                self.session_uuid, transaction_id))
        if isinstance(active, dict) and active.get("pgid"):
            try:
                os.killpg(active["pgid"], signal.SIGKILL)
            except OSError:
                pass
        identity = state_store.read_json_tolerant(
            state_store.verification_worker_identity_path_for(
                self.session_uuid, transaction_id))
        if isinstance(identity, dict) and identity.get("pid"):
            try:
                os.killpg(os.getpgid(identity["pid"]), signal.SIGKILL)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
