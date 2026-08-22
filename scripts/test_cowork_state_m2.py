#!/usr/bin/env python3
"""Focused tests for M2 Package B: crash-safe WorkUnit / dependency-graph /
PhaseState persistence, atomic controller-policy transitions, and the
legacy read/migration shim added to cowork_state.py.

Run standalone:

    python3 -m unittest scripts/test_cowork_state_m2.py -v
"""

import fcntl
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_control_plane as control_plane  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_workunit as workunit  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


def _mp_append_running(session_id, work_id):
    """Module-level (picklable / fork-safe) worker target: append one
    'running' PhaseState record from a genuinely separate OS process -- see
    `PhaseStateHistoryTest.test_concurrent_appends_across_processes_
    serialize_without_corruption`."""
    state_store.append_phase_state_entry(session_id, work_id, "running", None)


def _wait_for_marker(marker_path, timeout=10.0):
    deadline = time.time() + timeout
    while not os.path.exists(marker_path) and time.time() < deadline:
        time.sleep(0.01)
    return os.path.exists(marker_path)


def _mp_append_victim(session_id, work_id, frozen_recorded_at, own_marker_path,
                      peer_marker_path, result_path):
    """Module-level (picklable / fork-safe) worker target: the 'victim' of a
    genuine cross-process duplicate-`recorded_at` race, deterministically
    ordered via marker files rather than hoped-for real timing (mirrors
    this suite's own no-flake philosophy for the same-process signal
    interleaving tests). `_utc_now` is frozen so this process's own durable
    record and the peer's share the identical `recorded_at`.

    `_reconciled_phase_state_entry` -- which runs strictly AFTER
    `append_phase_state_entry` releases its own `fcntl.flock` (see that
    function's body) -- is hooked to publish `own_marker_path` the instant
    it starts, then BLOCK until `peer_marker_path` exists: this guarantees
    the peer process's own same-`recorded_at` record is durably written and
    on disk before this process's own reconciliation re-read runs, making
    the race deterministic instead of racing real OS scheduling."""
    state_store._utc_now = lambda: frozen_recorded_at
    real_reconcile = state_store._reconciled_phase_state_entry

    def hooked_reconcile(cp, sid, wid, written):
        with open(own_marker_path, "w") as fh:
            fh.write("done")
        if not _wait_for_marker(peer_marker_path):
            raise AssertionError(
                "peer process never published %r -- test setup bug"
                % peer_marker_path)
        return real_reconcile(cp, sid, wid, written)

    state_store._reconciled_phase_state_entry = hooked_reconcile
    entry = state_store.append_phase_state_entry(session_id, work_id, "running", None)
    with open(result_path, "w") as fh:
        json.dump(entry, fh)


def _mp_append_peer(session_id, work_id, frozen_recorded_at, victim_marker_path,
                    own_marker_path, result_path):
    """Module-level (picklable / fork-safe) worker target: the 'peer' of the
    deterministic cross-process race above -- waits for the victim's own
    record to already be durable, then appends its OWN record (same frozen
    `recorded_at`, a genuinely different `append_id`), then publishes its
    own marker so the waiting victim's reconciliation proceeds."""
    state_store._utc_now = lambda: frozen_recorded_at
    if not _wait_for_marker(victim_marker_path):
        raise AssertionError(
            "victim process never published %r -- test setup bug"
            % victim_marker_path)
    entry = state_store.append_phase_state_entry(
        session_id, work_id, "preflighting", None)
    with open(result_path, "w") as fh:
        json.dump(entry, fh)
    with open(own_marker_path, "w") as fh:
        fh.write("done")


def _make_work_unit(**overrides):
    base = dict(
        schema_version=1, record="WorkUnit",
        work_id=_uuid(), session_id=_uuid(), phase="building",
        role="builder", seat=0, round=1, attempt=1,
        controller="claude", provider="anthropic",
        requested_model="sonnet", effective_model="sonnet", effort="high",
        candidate_manifest_digest=None, candidate_index=None,
        prompt_digest="b" * 64, pending_turn_digest=None,
        parent_work_id=None, governed_child_policy="inherit",
        graph_revision=1, predecessor_work_ids=[], fan_join_id=None,
        lifecycle_state="pending", terminal_reason=None,
    )
    base.update(overrides)
    return base


_UNSET = object()


def _gate_evidence(digest=_UNSET, index=None, verdict="pass"):
    return {"gate_validation": {
        "candidate_manifest_digest": "c" * 64 if digest is _UNSET else digest,
        "candidate_index": index,
        "verdict": verdict,
    }}


def _node(work_id, predecessors=(), candidate=None, index=None, policy="inherit"):
    return {
        "work_id": work_id,
        "candidate_manifest_digest": candidate,
        "candidate_index": index,
        "governed_child_policy": policy,
        "predecessor_work_ids": list(predecessors),
    }


class _M2EnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test, so nothing ever touches the
    real home dir (mirrors test_cowork.py's _EvalEnvMixin)."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root

    def _raw_lines(self, path):
        with open(path, "r") as fh:
            return fh.readlines()

    def _raw_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()


# --------------------------------------------------------------------------- #
# WorkUnit store.                                                             #
# --------------------------------------------------------------------------- #


class WorkUnitStoreTest(_M2EnvMixin, unittest.TestCase):
    def test_mint_persists_first_record(self):
        w = _make_work_unit()
        stored = state_store.mint_work_unit(w)
        self.assertEqual(stored["work_id"], w["work_id"])
        self.assertEqual(stored["transition_index"], 0)
        history = state_store.read_work_unit_history(w["session_id"], w["work_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["lifecycle_state"], "pending")
        self.assertEqual(
            state_store.current_work_unit_state(w["session_id"], w["work_id"]),
            history[0])

    def test_mint_normalizes_uuid_casing(self):
        w = _make_work_unit()
        w["work_id"] = w["work_id"].upper()
        w["session_id"] = w["session_id"].upper()
        stored = state_store.mint_work_unit(w)
        self.assertEqual(stored["work_id"], stored["work_id"].lower())
        history = state_store.read_work_unit_history(
            w["session_id"].lower(), w["work_id"].lower())
        self.assertEqual(len(history), 1)

    def test_mint_rejects_invalid_record_and_writes_nothing(self):
        w = _make_work_unit()
        del w["role"]
        with self.assertRaises(ValueError):
            state_store.mint_work_unit(w)
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        self.assertFalse(os.path.exists(path))

    def test_mint_twice_raises_and_history_unchanged(self):
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        before = self._raw_bytes(path)
        with self.assertRaises(ValueError):
            state_store.mint_work_unit(w)
        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        self.assertEqual(
            len(state_store.read_work_unit_history(
                w["session_id"], w["work_id"])), 1)

    def test_transition_before_mint_raises_and_writes_nothing(self):
        w = _make_work_unit(lifecycle_state="preflighting")
        with self.assertRaises(ValueError):
            state_store.append_work_unit_transition(w)
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        self.assertFalse(os.path.exists(path))

    def test_transition_appends_and_preserves_prior_line_bytes(self):
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        first_line_before = self._raw_lines(path)[0]

        running = _make_work_unit(
            work_id=w["work_id"], session_id=w["session_id"],
            lifecycle_state="preflighting")
        state_store.append_work_unit_transition(running)

        lines = self._raw_lines(path)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], first_line_before)
        history = state_store.read_work_unit_history(
            w["session_id"], w["work_id"])
        self.assertEqual([h["lifecycle_state"] for h in history],
                         ["pending", "preflighting"])
        self.assertEqual([h["transition_index"] for h in history], [0, 1])
        self.assertEqual(
            state_store.current_work_unit_state(
                w["session_id"], w["work_id"])["lifecycle_state"],
            "preflighting")

    def test_transition_validates_before_write(self):
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        bad = dict(w)
        bad["lifecycle_state"] = "not_a_real_state"
        with self.assertRaises(ValueError):
            state_store.append_work_unit_transition(bad)
        self.assertEqual(
            len(state_store.read_work_unit_history(
                w["session_id"], w["work_id"])), 1)

    def test_read_history_empty_for_unminted_work_id(self):
        self.assertEqual(
            state_store.read_work_unit_history(_uuid(), _uuid()), [])
        self.assertIsNone(
            state_store.current_work_unit_state(_uuid(), _uuid()))

    def test_persisted_record_fails_raw_revalidation(self):
        """B-04 baseline: proves the hazard is real before proving the fix
        -- a persisted history record carries `transition_index` and
        `recorded_at`, which `validate_work_unit`'s exact-key-set check
        rejects verbatim."""
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        stored = state_store.current_work_unit_state(
            w["session_id"], w["work_id"])
        with self.assertRaises(ValueError):
            workunit.validate_work_unit(stored)

    def test_work_unit_from_history_record_round_trips_through_validation(self):
        w = _make_work_unit()
        minted = state_store.mint_work_unit(w)
        projected = state_store.work_unit_from_history_record(minted)
        self.assertNotIn("transition_index", projected)
        self.assertNotIn("recorded_at", projected)
        revalidated = workunit.validate_work_unit(projected)
        self.assertEqual(revalidated["work_id"], w["work_id"])
        self.assertEqual(revalidated["lifecycle_state"], "pending")

        running = _make_work_unit(
            work_id=w["work_id"], session_id=w["session_id"],
            lifecycle_state="preflighting")
        state_store.append_work_unit_transition(running)
        current = state_store.current_work_unit_state(
            w["session_id"], w["work_id"])
        revalidated_current = workunit.validate_work_unit(
            state_store.work_unit_from_history_record(current))
        self.assertEqual(revalidated_current["lifecycle_state"], "preflighting")

    def test_work_unit_from_history_record_projects_every_history_entry(self):
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        running = _make_work_unit(
            work_id=w["work_id"], session_id=w["session_id"],
            lifecycle_state="preflighting")
        state_store.append_work_unit_transition(running)
        history = state_store.read_work_unit_history(
            w["session_id"], w["work_id"])
        for record in history:
            revalidated = workunit.validate_work_unit(
                state_store.work_unit_from_history_record(record))
            self.assertEqual(revalidated["work_id"], w["work_id"])

    def test_work_unit_from_history_record_none_projects_to_none(self):
        self.assertIsNone(state_store.work_unit_from_history_record(None))

    def test_concurrent_mint_only_one_winner(self):
        w = _make_work_unit()
        results = []
        errors = []

        def attempt():
            try:
                results.append(state_store.mint_work_unit(dict(w)))
            except ValueError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 7)
        self.assertEqual(
            len(state_store.read_work_unit_history(
                w["session_id"], w["work_id"])), 1)

    def test_concurrent_transitions_serialize_without_loss(self):
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        n = 20

        def attempt(i):
            rec = _make_work_unit(
                work_id=w["work_id"], session_id=w["session_id"],
                lifecycle_state="preflighting")
            state_store.append_work_unit_transition(rec)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        history = state_store.read_work_unit_history(
            w["session_id"], w["work_id"])
        self.assertEqual(len(history), n + 1)
        self.assertEqual(
            [h["transition_index"] for h in history], list(range(n + 1)))


# --------------------------------------------------------------------------- #
# Dependency-graph store.                                                     #
# --------------------------------------------------------------------------- #


class DependencyGraphStoreTest(_M2EnvMixin, unittest.TestCase):
    def test_append_first_and_second_revision(self):
        session_id = _uuid()
        a, b = _uuid(), _uuid()
        rev1 = state_store.append_graph_revision(session_id, [_node(a)])
        self.assertEqual(rev1["graph_revision"], 1)

        rev2 = state_store.append_graph_revision(
            session_id, [_node(a), _node(b, predecessors=[a])])
        self.assertEqual(rev2["graph_revision"], 2)

        revisions = state_store.read_graph_revisions(session_id)
        self.assertIsInstance(revisions, tuple)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(
            state_store.current_graph_revision(session_id), revisions[-1])
        self.assertEqual(state_store.current_graph_revision(session_id)["graph_revision"], 2)

    def test_invalid_revision_rejected_before_write(self):
        session_id = _uuid()
        a = _uuid()
        with self.assertRaises(workunit.GraphValidationError):
            state_store.append_graph_revision(
                session_id, [_node(a, predecessors=[a])])  # self-edge
        path = state_store.graph_revisions_path_for(session_id)
        self.assertFalse(os.path.exists(path))

    def test_immutable_prior_revision_bytes_unchanged(self):
        session_id = _uuid()
        a, b = _uuid(), _uuid()
        state_store.append_graph_revision(session_id, [_node(a)])
        path = state_store.graph_revisions_path_for(session_id)
        first_line_before = self._raw_lines(path)[0]

        state_store.append_graph_revision(
            session_id, [_node(a), _node(b, predecessors=[a])])
        lines = self._raw_lines(path)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], first_line_before)

        # A subsequent invalid revision attempt must not disturb either prior
        # line, and must not append a third.
        with self.assertRaises(workunit.GraphValidationError):
            state_store.append_graph_revision(
                session_id, [_node(a, predecessors=[_uuid()])])  # dangling
        lines_after = self._raw_lines(path)
        self.assertEqual(lines_after, lines)

    def test_read_graph_revisions_empty_for_fresh_session(self):
        session_id = _uuid()
        self.assertEqual(state_store.read_graph_revisions(session_id), ())
        self.assertIsNone(state_store.current_graph_revision(session_id))

    def test_each_revision_validated_in_isolation(self):
        # Revision 2 may reuse a work_id from revision 1 -- validation never
        # consults prior revisions (mirrors cowork_workunit.append_revision).
        session_id = _uuid()
        a = _uuid()
        state_store.append_graph_revision(session_id, [_node(a)])
        rev2 = state_store.append_graph_revision(session_id, [_node(a)])
        self.assertEqual(rev2["graph_revision"], 2)


# --------------------------------------------------------------------------- #
# B-CRASH-ATOMICITY-1: append-only JSONL persistence must be TRUTHFULLY       #
# crash-reconstructible for short/partial regular-file writes -- a prior      #
# version of this module's docstrings falsely invoked PIPE_BUF (a PIPE-only   #
# POSIX guarantee) to claim regular-file write completeness, and never        #
# called fsync at all, so a short write could report success and a torn       #
# tail could be silently concatenated with the next append's own bytes,       #
# corrupting both into one unparseable line. Every hazard below is a          #
# directly INJECTED short write / fsync failure / torn tail -- never a        #
# hoped-for real crash timing -- matching this suite's own no-flake style.    #
# --------------------------------------------------------------------------- #


def _truncate_tail_bytes(path, n):
    """Drop the last `n` bytes of `path`, simulating a short/torn write a
    crash left mid-append (never a full, valid trailing record)."""
    with open(path, "r+b") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.truncate(max(0, size - n))


class AppendJsonlAtomicCrashSafetyTest(_M2EnvMixin, unittest.TestCase):
    """Direct, primitive-level proofs against `append_jsonl_atomic` itself --
    the one shared write path every M2 Package B store (PhaseState, WorkUnit,
    graph revision) funnels through."""

    def _some_record(self, session_id, work_id, state="running"):
        return {"session_id": session_id, "work_id": work_id, "state": state,
                "reason_code": None, "event": None, "evidence": None,
                "source": None, "transition_index": 1,
                "recorded_at": "2024-01-01T00:00:00Z",
                "append_id": "deadbeefdeadbeefdeadbeefdeadbeef"}

    def test_short_write_never_reports_success_and_leaves_no_torn_tail(self):
        """A `_write_all_fd` that is interrupted partway through -- exactly
        what a real short `os.write` return, or a crash mid-syscall, would
        produce -- must never let `append_jsonl_atomic` report success, and
        must never leave the partial bytes it itself wrote behind."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)
        before = self._raw_bytes(path)

        real_write_all = state_store._write_all_fd

        def failing_write_all(fd, data):
            os.write(fd, data[:4])  # a genuine partial write actually lands
            raise OSError("simulated short/interrupted write")

        state_store._write_all_fd = failing_write_all
        try:
            ok = state_store.append_jsonl_atomic(
                path, self._some_record(session_id, work_id))
        finally:
            state_store._write_all_fd = real_write_all

        self.assertFalse(ok)
        # Rolled back to exactly the pre-attempt bytes -- no torn fragment
        # left behind by this failed attempt.
        self.assertEqual(self._raw_bytes(path), before)
        self.assertEqual(
            [h["state"] for h in state_store.read_phase_state_history(
                session_id, work_id)],
            ["pending"])

    def test_fsync_failure_never_reports_success_and_rolls_back(self):
        """A `write()` returning the full byte count only means the bytes
        reached the page cache, not stable storage -- `fsync` failing (or
        never being called at all, the pre-fix behavior) must not let this
        function claim a durable, complete success."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)
        before = self._raw_bytes(path)

        real_fsync = state_store.os.fsync

        def failing_fsync(fd):
            raise OSError("simulated fsync failure")

        state_store.os.fsync = failing_fsync
        try:
            ok = state_store.append_jsonl_atomic(
                path, self._some_record(session_id, work_id))
        finally:
            state_store.os.fsync = real_fsync

        self.assertFalse(ok)
        self.assertEqual(self._raw_bytes(path), before)

    def test_torn_tail_without_trailing_newline_repaired_before_append(self):
        """The exact hazard the pre-fix code had: a torn tail with NO
        trailing newline, appended onto blindly, would concatenate the next
        record's bytes directly onto the old fragment -- corrupting BOTH
        into one jointly unparseable line and silently losing a
        legitimately new, fully-written record to a wholly unrelated prior
        crash. Proves the repair instead, via a fresh 'reopen' of the file
        (a direct `append_jsonl_atomic` call, no in-memory state carried
        over) -- the exact post-restart shape."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)

        with open(path, "ab") as fh:
            fh.write(b'{"state": "runni')  # deliberately incomplete, no \n
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))

        # A reader (no repair) tolerates the torn tail, seeing only prior
        # valid history -- never a corrupted or partial "running" entry.
        self.assertEqual(
            [h["state"] for h in state_store.read_phase_state_history(
                session_id, work_id)],
            ["pending"])

        ok = state_store.append_jsonl_atomic(
            path, self._some_record(session_id, work_id, state="running"))
        self.assertTrue(ok)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # every line independently, fully parses
        self.assertEqual(json.loads(lines[0])["state"], "pending")
        self.assertEqual(json.loads(lines[1])["state"], "running")

    def test_torn_tail_with_newline_but_invalid_json_also_repaired(self):
        """Hardened against non-crash corruption too: a final line that IS
        newline-terminated but does not parse as a JSON object (never
        producible by this module's own writes, but a defensive backstop)
        is treated the same as a torn tail and repaired before the next
        append lands."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)

        with open(path, "ab") as fh:
            fh.write(b"not valid json at all\n")

        ok = state_store.append_jsonl_atomic(
            path, self._some_record(session_id, work_id, state="running"))
        self.assertTrue(ok)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)
        self.assertEqual(json.loads(lines[0])["state"], "pending")
        self.assertEqual(json.loads(lines[1])["state"], "running")

    def test_truncated_trailing_record_repaired_before_next_append(self):
        """A record that WAS fully valid but got physically cut short by a
        crash mid-write of ITS OWN bytes -- chopping off its trailing
        newline and some of its own content, rather than a fresh append
        never reaching a newline at all -- must be dropped by the repair,
        never kept as a corrupted record and never left to corrupt the
        next append onto it."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        state_store.append_phase_state_entry(session_id, work_id, "preflighting", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)
        _truncate_tail_bytes(path, 10)  # chop the tail off the 2nd record
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))
        self.assertEqual(
            [h["state"] for h in state_store.read_phase_state_history(
                session_id, work_id)],
            ["pending"])

        ok = state_store.append_jsonl_atomic(
            path, self._some_record(session_id, work_id, state="running"))
        self.assertTrue(ok)
        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)
        self.assertEqual(json.loads(lines[0])["state"], "pending")
        self.assertEqual(json.loads(lines[1])["state"], "running")

    def test_empty_file_and_healthy_file_are_repair_no_ops(self):
        """The common case -- nothing torn -- must not be disturbed: a
        brand-new (nonexistent) path and an already-healthy file both
        append cleanly with zero bytes dropped."""
        session_id, work_id = _uuid(), _uuid()
        path = state_store.phase_state_history_path_for(session_id, work_id)
        self.assertFalse(os.path.exists(path))
        ok = state_store.append_jsonl_atomic(
            path, self._some_record(session_id, work_id, state="pending"))
        self.assertTrue(ok)
        self.assertEqual(len(self._raw_lines(path)), 1)

        ok2 = state_store.append_jsonl_atomic(
            path, self._some_record(session_id, work_id, state="running"))
        self.assertTrue(ok2)
        lines = self._raw_lines(path)
        self.assertEqual(len(lines), 2)
        json.loads(lines[0])
        json.loads(lines[1])


# --------------------------------------------------------------------------- #
# B-CRASH-ROLLBACK-1 / B-CRASH-REENTRANT-2: a REAL signal delivered strictly  #
# INSIDE a single `append_jsonl_atomic` call -- not merely between two        #
# separate calls, which is all the pre-existing B-11/B-14 suite above         #
# exercises -- must never let this call's rollback erase a record a           #
# reentrant signal-handler call already durably committed in the interim,     #
# and must never let this call itself report success for a record that       #
# landed corrupted by that same interleaving. Every test below uses a REAL    #
# `os.kill` + a REAL `signal.signal` handler fired from inside the actual     #
# primitive (via a targeted monkeypatch of the ONE helper call that marks     #
# the exact injection point -- observation, mid-partial-write, or the        #
# write-to-fsync gap), never a simulated/hoped-for timing race.               #
# --------------------------------------------------------------------------- #


class AppendJsonlAtomicReentrantSignalTest(_M2EnvMixin, unittest.TestCase):
    def _some_record(self, session_id, work_id, marker):
        return {"session_id": session_id, "work_id": work_id,
                "state": "running", "reason_code": None, "event": None,
                "evidence": None, "source": marker, "transition_index": 1,
                "recorded_at": "2024-01-01T00:00:00Z",
                "append_id": state_store._mint_append_id()}

    def _install_sigusr1_reentrant_handler(self, path, marker):
        """Register a REAL SIGUSR1 handler that durably commits its OWN,
        genuinely different record to `path` via a fresh, independent
        `append_jsonl_atomic` call -- exactly the documented reentrant
        signal-handler shape (a separate fd on the same path) -- the FIRST
        time it fires. Returns (old_handler, handler_ran, reentrant_record)
        for the caller to restore/assert against."""
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("SIGUSR1 unavailable on this platform")
        handler_ran = []
        session_id, work_id = _uuid(), _uuid()
        reentrant_record = self._some_record(session_id, work_id, marker)

        def handler(signum, frame):
            handler_ran.append(True)
            ok = state_store.append_jsonl_atomic(path, reentrant_record)
            if not ok:
                raise AssertionError(
                    "reentrant handler's own append_jsonl_atomic call "
                    "failed -- test setup bug")

        old_handler = signal.signal(signal.SIGUSR1, handler)
        return old_handler, handler_ran, reentrant_record

    def test_signal_during_observation_cannot_erase_concurrently_committed_record(self):
        """Signal delivered strictly between this call's own observation
        (the fresh read `_repair_torn_tail_now` performs) and its repair
        `ftruncate` -- with a genuine PRE-EXISTING torn tail present, so a
        truncate is actually attempted, not skipped as a no-op. The
        reentrant handler durably commits its own record in that exact
        gap; this call's repair must re-observe the file fresh (via the
        byte-for-byte recheck read) rather than truncating against its
        now-stale first read, or it would erase the handler's
        already-durable record -- exactly the B-CRASH-ROLLBACK-1 hazard,
        but at the repair-before-write site rather than the
        failure-rollback site."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b'{"state": "pending"}\n')
            fh.write(b'{"state": "runni')  # torn -- no trailing newline

        old_handler, handler_ran, reentrant_record = (
            self._install_sigusr1_reentrant_handler(path, "reentrant"))
        real_read_all_fd = state_store._read_all_fd
        fired = {"n": 0}

        def hooked_read_all_fd(fd):
            result = real_read_all_fd(fd)
            fired["n"] += 1
            if fired["n"] == 1:
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True],
                                  "handler did not run synchronously -- "
                                  "test setup bug")
            return result

        state_store._read_all_fd = hooked_read_all_fd
        own_record = {"session_id": _uuid(), "work_id": _uuid(),
                      "state": "running", "reason_code": None,
                      "event": None, "evidence": None, "source": "outer",
                      "transition_index": 1,
                      "recorded_at": "2024-01-01T00:00:01Z",
                      "append_id": state_store._mint_append_id()}
        try:
            ok = state_store.append_jsonl_atomic(path, own_record)
        finally:
            state_store._read_all_fd = real_read_all_fd
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertTrue(handler_ran, [True])
        self.assertTrue(ok)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        # The original healthy "pending" record, the reentrant handler's
        # own durably-committed record (never erased), and this call's own
        # -- all three intact, none merged, none lost.
        self.assertEqual(len(lines), 3)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["state"], "pending")
        self.assertEqual(parsed[1]["source"], "reentrant")
        self.assertEqual(parsed[2]["source"], "outer")

    def test_signal_mid_partial_write_rolls_back_without_erasing_reentrant_record(self):
        """Signal delivered strictly INSIDE `_write_all_fd`'s own short-write
        loop -- after a genuine partial `os.write` of THIS call's own bytes
        has already landed, before the rest. The reentrant handler commits
        its own complete record in that exact gap; this call's own
        remaining bytes then land, via `O_APPEND`, AFTER the handler's
        record -- a real, non-simulated instance of the B-CRASH-REENTRANT-2
        hazard. This call must detect its own record did not land intact
        (never report false success), and its rollback must remove ONLY
        its own now-torn fragment -- never the handler's already-durable,
        independently valid record."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())

        old_handler, handler_ran, reentrant_record = (
            self._install_sigusr1_reentrant_handler(path, "reentrant"))
        real_write_all_fd = state_store._write_all_fd
        fired = {"n": 0}

        def hooked_write_all_fd(fd, data):
            fired["n"] += 1
            if fired["n"] == 1:
                os.write(fd, data[:6])  # a genuine partial write lands
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True],
                                  "handler did not run synchronously -- "
                                  "test setup bug")
                real_write_all_fd(fd, data[6:])  # this call resumes for real
            else:
                real_write_all_fd(fd, data)

        state_store._write_all_fd = hooked_write_all_fd
        own_record = {"session_id": _uuid(), "work_id": _uuid(),
                      "state": "running", "reason_code": None,
                      "event": None, "evidence": None, "source": "outer",
                      "transition_index": 1,
                      "recorded_at": "2024-01-01T00:00:01Z",
                      "append_id": state_store._mint_append_id()}
        try:
            ok = state_store.append_jsonl_atomic(path, own_record)
        finally:
            state_store._write_all_fd = real_write_all_fd
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])
        # This call's own interleaved, now-corrupted write must never be
        # reported as success.
        self.assertFalse(ok)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        # Exactly the reentrant handler's own record survives -- durably
        # committed, never erased by this call's rollback -- and this
        # call's own torn fragment is fully gone, never left half-written
        # or corrupting the handler's record.
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["source"], "reentrant")
        self.assertEqual(
            state_store.read_jsonl_tolerant(path), [reentrant_record])

    def test_signal_between_write_and_fsync_lets_both_records_land_intact(self):
        """Signal delivered strictly AFTER this call's own write has fully
        and contiguously landed but BEFORE `fsync` -- the reentrant
        handler's own record durably lands right after this call's own,
        entirely benignly (this call's own bytes were never split, so
        nothing of its own is corrupted). This call must still correctly
        report TRUE -- a legitimate concurrent append landing after an
        already-complete, untouched write is not corruption, and must
        never be misreported as this call's own failure."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())

        old_handler, handler_ran, reentrant_record = (
            self._install_sigusr1_reentrant_handler(path, "reentrant"))
        real_fsync = state_store.os.fsync
        fired = {"n": 0}

        def hooked_fsync(fd):
            fired["n"] += 1
            if fired["n"] == 1:
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True],
                                  "handler did not run synchronously -- "
                                  "test setup bug")
            return real_fsync(fd)

        state_store.os.fsync = hooked_fsync
        own_record = {"session_id": _uuid(), "work_id": _uuid(),
                      "state": "running", "reason_code": None,
                      "event": None, "evidence": None, "source": "outer",
                      "transition_index": 1,
                      "recorded_at": "2024-01-01T00:00:01Z",
                      "append_id": state_store._mint_append_id()}
        try:
            ok = state_store.append_jsonl_atomic(path, own_record)
        finally:
            state_store.os.fsync = real_fsync
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])
        self.assertTrue(ok)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["source"], "outer")
        self.assertEqual(parsed[1]["source"], "reentrant")


class PhaseStateMidWriteSignalTest(_M2EnvMixin, unittest.TestCase):
    """The same B-CRASH-REENTRANT-2 hazard, proven through the PUBLIC
    PhaseState API rather than the raw primitive: a real SIGUSR1 fires
    strictly inside `append_jsonl_atomic`'s own write loop while the main
    flow is durably recording 'running', not merely between two separate
    `_jsonl_append_unlocked` calls (that residual is B-14, already
    covered above). The main flow's own attempt must fail loudly rather
    than report false success or corrupt the file, and Package E's
    external-kill 'aborted' record -- committed inside the signal handler
    -- must survive completely intact, with truthful, non-duplicate
    `transition_index` and undiminished terminal currency."""

    def test_mid_write_signal_during_running_append_preserves_terminal_record(self):
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("SIGUSR1 unavailable on this platform")
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)

        handler_ran = []

        def handler(signum, frame):
            handler_ran.append(True)
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "aborted", "external_kill",
                event="sigterm", source="package_e")

        old_handler = signal.signal(signal.SIGUSR1, handler)
        real_write_all_fd = state_store._write_all_fd
        fired = {"n": 0}

        def hooked_write_all_fd(fd, data):
            fired["n"] += 1
            if fired["n"] == 1:
                os.write(fd, data[:6])
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True],
                                  "handler did not run synchronously -- "
                                  "test setup bug")
                real_write_all_fd(fd, data[6:])
            else:
                real_write_all_fd(fd, data)

        state_store._write_all_fd = hooked_write_all_fd
        try:
            with self.assertRaises(OSError):
                state_store.append_phase_state_entry_unlocked(
                    session_id, work_id, "running", None,
                    source="main_flow")
        finally:
            state_store._write_all_fd = real_write_all_fd
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])

        # The terminal record survives completely intact -- never erased
        # by the interrupted main flow's own rollback -- and is durable,
        # dominant current truth.
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual([h["state"] for h in history], ["pending", "aborted"])
        self.assertEqual(
            [h["transition_index"] for h in history], [0, 1])
        self.assertEqual([h["superseded"] for h in history], [False, False])
        current = state_store.current_phase_state(session_id, work_id)
        self.assertEqual(current["state"], "aborted")

        # No corruption anywhere in the raw file: every line parses.
        path = state_store.phase_state_history_path_for(session_id, work_id)
        raw = self._raw_bytes(path)
        for line in raw.splitlines():
            json.loads(line)

        # And the taxonomy-level guarantee still holds afterward.
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "running", None, source="late_writer")


# --------------------------------------------------------------------------- #
# v7 correction: B1 (rollback truncates via an unconfirmed absolute offset),  #
# M1 (the repair freshness guard was SIZE-only, so a length-colliding         #
# reentrant record could still be erased), M2 (`os.close` escaping the        #
# never-raise contract), and M3 (no parent-directory fsync for a newly        #
# created history, so its first record was not actually crash-durable        #
# despite a truthful-looking True). Every proof below is non-vacuous: each    #
# was confirmed to FAIL against the pre-correction code before being kept.    #
# --------------------------------------------------------------------------- #


class AppendJsonlAtomicRollbackAndRepairFreshnessTest(_M2EnvMixin, unittest.TestCase):
    def _some_record(self, session_id, work_id, marker):
        return {"session_id": session_id, "work_id": work_id,
                "state": "running", "reason_code": None, "event": None,
                "evidence": None, "source": marker, "transition_index": 1,
                "recorded_at": "2024-01-01T00:00:00Z",
                "append_id": state_store._mint_append_id()}

    def _padded_record(self, session_id, work_id, marker, target_len):
        """Build a record whose serialized `json.dumps(..., sort_keys=True)
        + '\\n'` byte length is EXACTLY `target_len`, by growing a `pad`
        field -- lets a test FORCE a byte-length collision against an
        unrelated fragment (M1) rather than merely hoping for one."""
        record = self._some_record(session_id, work_id, marker)
        record["pad"] = ""
        base_len = len(
            json.dumps(record, sort_keys=True).encode("utf-8")) + 1
        pad_needed = target_len - base_len
        if pad_needed < 0:
            raise AssertionError(
                "target_len %d too small for base record shape (needs "
                "at least %d) -- test setup bug" % (target_len, base_len))
        record["pad"] = "x" * pad_needed
        actual_len = len(
            json.dumps(record, sort_keys=True).encode("utf-8")) + 1
        assert actual_len == target_len, (actual_len, target_len)
        return record

    def test_signal_during_rollback_cannot_erase_concurrently_committed_record(self):
        """B1: `append_jsonl_atomic`'s own `fsync` fails for THIS call
        after its write fully, contiguously landed -- so `_rollback_
        failed_append`'s first read sees exactly this call's own complete
        `line` at the tail and decides to remove it. A REAL SIGUSR1
        handler fires the instant that first read returns (injected at
        the ROLLBACK site's own `_read_all_fd` call, not the repair
        site's, which the pre-existing suite already covers), durably
        committing its own record H via a fresh, independent
        `append_jsonl_atomic` call in the exact gap between rollback's
        decision and its own recheck. Proves the recheck detects this and
        retries instead of truncating H away -- the precise B1 failure
        scenario ('the outer frame resumes and truncates to len(prefix),
        erasing H')."""
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("SIGUSR1 unavailable on this platform")
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())
        reentrant_record = self._some_record(_uuid(), _uuid(), "reentrant")
        handler_ran = []

        def handler(signum, frame):
            handler_ran.append(True)
            ok = state_store.append_jsonl_atomic(path, reentrant_record)
            if not ok:
                raise AssertionError(
                    "reentrant handler's own append_jsonl_atomic call "
                    "failed -- test setup bug")

        old_handler = signal.signal(signal.SIGUSR1, handler)
        real_read_all_fd = state_store._read_all_fd
        real_fsync = state_store.os.fsync
        read_calls = {"n": 0}
        fsync_calls = {"n": 0}

        def hooked_read_all_fd(fd):
            result = real_read_all_fd(fd)
            read_calls["n"] += 1
            # Call 1 is `_repair_torn_tail_now`'s only read (a fresh,
            # empty file needs no repair, so it returns without a second
            # read). Call 2 is `_rollback_failed_append`'s own first
            # (decision) read -- inject immediately after THAT one, so
            # the reentrant's commit lands strictly between rollback's
            # decision and its own recheck read.
            if read_calls["n"] == 2:
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True],
                                  "handler did not run synchronously -- "
                                  "test setup bug")
            return result

        def hooked_fsync(fd):
            fsync_calls["n"] += 1
            if fsync_calls["n"] == 1:
                raise OSError("simulated fsync failure for THIS call only")
            return real_fsync(fd)

        state_store._read_all_fd = hooked_read_all_fd
        state_store.os.fsync = hooked_fsync
        own_record = self._some_record(_uuid(), _uuid(), "outer")
        try:
            ok = state_store.append_jsonl_atomic(path, own_record)
        finally:
            state_store._read_all_fd = real_read_all_fd
            state_store.os.fsync = real_fsync
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])
        # This call's own fsync genuinely failed -- correctly False.
        self.assertFalse(ok)

        # The reentrant handler's own durably committed record must
        # survive completely intact -- never erased by this call's
        # rollback, even though rollback's own FIRST (now-stale) decision
        # was "remove exactly my own complete line": once the recheck
        # sees H now sitting after this call's own line, `line` is no
        # longer the file's tail, so this call's own (already fully
        # written, merely unsynced) bytes are structurally unremovable by
        # a tail-only `ftruncate` without ALSO destroying H -- and this
        # rollback correctly refuses to do that, leaving both intact
        # rather than erasing H to fully clean up after itself (a
        # pre-existing, documented, accepted residual: a false FAILURE
        # that leaves a durable record behind, never a false SUCCESS or a
        # lost foreign record). The one non-negotiable property is H
        # itself: present, complete, and byte-for-byte unmodified.
        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        parsed = [json.loads(line) for line in lines]
        self.assertEqual([p["source"] for p in parsed], ["outer", "reentrant"])
        history = state_store.read_jsonl_tolerant(path)
        self.assertIn(reentrant_record, history)

    def test_length_colliding_reentrant_record_survives_repair_recheck(self):
        """M1: a prior version of `_repair_torn_tail_now`'s freshness
        guard compared ONLY `os.fstat(...).st_size` against the stale
        read's own length. A reentrant record whose serialized byte
        length happens to exactly equal the torn fragment it replaced
        leaves `st_size` numerically unchanged from that stale
        observation -- passing a size-only guard while being entirely
        different bytes -- and would be erased anyway. This FORCES that
        exact length collision (via `_padded_record`, not a hoped-for
        coincidence) and proves the content-based (byte-for-byte) recheck
        catches it where a size-only check could not."""
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("SIGUSR1 unavailable on this platform")
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Long enough that `_padded_record`'s base record shape (session_id
        # + work_id UUIDs, a 128-bit hex append_id, etc.) always fits under
        # it with room for padding -- the exact byte value doesn't matter,
        # only that it is torn (no trailing newline) and long.
        torn = b'{"state": "running", "pad": "' + b"z" * 500  # torn, no \n
        with open(path, "wb") as fh:
            fh.write(b'{"state": "pending"}\n')
            fh.write(torn)

        reentrant_record = self._padded_record(
            _uuid(), _uuid(), "reentrant", len(torn))
        handler_ran = []

        def handler(signum, frame):
            handler_ran.append(True)
            ok = state_store.append_jsonl_atomic(path, reentrant_record)
            if not ok:
                raise AssertionError(
                    "reentrant handler's own append_jsonl_atomic call "
                    "failed -- test setup bug")

        old_handler = signal.signal(signal.SIGUSR1, handler)
        real_read_all_fd = state_store._read_all_fd
        fired = {"n": 0}

        def hooked_read_all_fd(fd):
            result = real_read_all_fd(fd)
            fired["n"] += 1
            if fired["n"] == 1:
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True],
                                  "handler did not run synchronously -- "
                                  "test setup bug")
            return result

        state_store._read_all_fd = hooked_read_all_fd
        own_record = self._some_record(_uuid(), _uuid(), "outer")
        try:
            ok = state_store.append_jsonl_atomic(path, own_record)
        finally:
            state_store._read_all_fd = real_read_all_fd
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])
        self.assertTrue(ok)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 3)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["state"], "pending")
        self.assertEqual(parsed[1]["source"], "reentrant")
        self.assertEqual(parsed[2]["source"], "outer")


class AppendJsonlAtomicCloseAndDirDurabilityTest(_M2EnvMixin, unittest.TestCase):
    """M2 (never-raise contract) and M3 (parent-directory durability)."""

    def _some_record(self, session_id, work_id, marker, transition_index=0):
        return {"session_id": session_id, "work_id": work_id,
                "state": "pending", "reason_code": None, "event": None,
                "evidence": None, "source": marker,
                "transition_index": transition_index,
                "recorded_at": "2024-01-01T00:00:00Z",
                "append_id": state_store._mint_append_id()}

    def test_close_failure_does_not_escape_never_raise_contract(self):
        """M2: `os.close(fd)` can itself raise `OSError` (a deferred
        write-back error is a real POSIX possibility), exactly like every
        other syscall this primitive uses. A prior version left it in an
        unguarded `finally`, letting that `OSError` escape past this
        function's own documented 'never crash the worker or parent'
        contract -- every one of `cowork_verification.py`'s bare,
        try/except-free call sites would abort mid-run. Proves
        `append_jsonl_atomic` still merely RETURNS (never raises) even
        when `os.close` itself fails, and that the record -- already
        fully written and fsynced before `close` ever runs -- stays
        durably visible regardless."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())
        # Pre-create the file (unhooked) so THIS call's own `created` is
        # False and `_fsync_parent_dir` -- which does its own independent
        # open/fsync/close of the directory fd -- is skipped, isolating
        # the single `os.close` call this test targets: this call's own
        # fd, in `append_jsonl_atomic`'s own outer `finally`.
        first = self._some_record(_uuid(), _uuid(), "first", 0)
        self.assertTrue(state_store.append_jsonl_atomic(path, first))

        real_close = state_store.os.close

        def failing_close(fd):
            real_close(fd)  # the fd IS actually released ...
            raise OSError("simulated deferred close failure")  # ... but
            # close() itself still reports a (deferred) error, exactly as
            # POSIX permits.

        state_store.os.close = failing_close
        record = self._some_record(_uuid(), _uuid(), "outer", 1)
        try:
            ok = state_store.append_jsonl_atomic(path, record)  # must not raise
        finally:
            state_store.os.close = real_close

        self.assertTrue(ok)
        self.assertEqual(
            state_store.read_jsonl_tolerant(path), [first, record])

    def test_parent_dir_fsynced_exactly_once_for_a_newly_created_file(self):
        """M3: a brand new file's own directory ENTRY is durable state
        belonging to the PARENT directory's inode, not the file's own
        fd -- fsyncing the file's fd alone never touches it. Proves
        `_fsync_parent_dir` runs for the append that actually creates
        `path`, and is not repeated for a later append to the
        now-already-existing file."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())
        self.assertFalse(os.path.exists(path))
        real_fsync_parent_dir = state_store._fsync_parent_dir
        calls = []

        def counting_fsync_parent_dir(p):
            calls.append(p)
            return real_fsync_parent_dir(p)

        state_store._fsync_parent_dir = counting_fsync_parent_dir
        try:
            ok1 = state_store.append_jsonl_atomic(
                path, self._some_record(_uuid(), _uuid(), "first", 0))
            self.assertEqual(len(calls), 1)
            ok2 = state_store.append_jsonl_atomic(
                path, self._some_record(_uuid(), _uuid(), "second", 1))
        finally:
            state_store._fsync_parent_dir = real_fsync_parent_dir

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        # Only the FIRST call (the one that created the file) fsyncs the
        # parent directory -- the second, appending to an already
        # existing file, never repeats it.
        self.assertEqual(calls, [path])

    def test_parent_dir_fsync_failure_prevents_false_success(self):
        """M3: a directory-fsync failure must be treated exactly like any
        other durability failure inside `append_jsonl_atomic` -- rolled
        back and reported False -- never a True that silently omits the
        newly created file's own directory-entry durability."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())

        def failing_fsync_parent_dir(p):
            raise OSError("simulated parent-directory fsync failure")

        real_fsync_parent_dir = state_store._fsync_parent_dir
        state_store._fsync_parent_dir = failing_fsync_parent_dir
        record = self._some_record(_uuid(), _uuid(), "outer")
        try:
            ok = state_store.append_jsonl_atomic(path, record)
        finally:
            state_store._fsync_parent_dir = real_fsync_parent_dir

        self.assertFalse(ok)
        # Rolled back -- this call's own record must not be left visible
        # despite its own file-level bytes having been fully written and
        # fsynced; only the directory's own durability was unconfirmed.
        self.assertEqual(state_store.read_jsonl_tolerant(path), [])

    def test_real_append_jsonl_atomic_survives_a_real_new_file_creation(self):
        """Non-mocked sanity companion to the two tests above: a genuine
        `append_jsonl_atomic` call creating a brand new file, with no
        hooks at all, still returns True and the parent directory is
        real (this would raise `NotADirectoryError`/`FileNotFoundError`
        loudly if `_fsync_parent_dir` ever computed the wrong directory
        for a nested session-assets path)."""
        path = state_store.phase_state_history_path_for(_uuid(), _uuid())
        self.assertFalse(os.path.exists(path))
        record = self._some_record(_uuid(), _uuid(), "solo")
        ok = state_store.append_jsonl_atomic(path, record)
        self.assertTrue(ok)
        self.assertEqual(state_store.read_jsonl_tolerant(path), [record])


class PhaseStateCrashReconstructionTest(_M2EnvMixin, unittest.TestCase):
    """Reopen/next-append reconstruction through the PUBLIC PhaseState API
    (not the raw primitive above): a torn tail left by a simulated crash
    must be transparently repaired by the next real
    `append_phase_state_entry` call, with `append_id` identity, ordering,
    and `transition_index` all staying truthful across the repair."""

    def test_next_append_repairs_torn_tail_and_preserves_identity(self):
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)

        # Simulate a crash mid-write of a SECOND record -- a short write
        # that landed some bytes but never reached its own trailing
        # newline, exactly the shape a killed process leaves behind.
        with open(path, "ab") as fh:
            fh.write(b'{"session_id": "%s", "state": "preflig' % session_id.encode())
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))

        # Reopen/reconstruct: a reader sees only the prior valid history.
        self.assertEqual(
            [h["state"] for h in state_store.read_phase_state_history(
                session_id, work_id)],
            ["pending"])

        # The next append (fresh call, no in-memory state) must repair the
        # torn tail before landing its own record -- never concatenate.
        entry = state_store.append_phase_state_entry(
            session_id, work_id, "running", None)
        self.assertEqual(entry["state"], "running")
        self.assertEqual(entry["transition_index"], 1)
        self.assertFalse(entry["superseded"])

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual([h["state"] for h in history], ["pending", "running"])
        self.assertEqual([h["transition_index"] for h in history], [0, 1])
        # append_id identity survives the repair intact: the returned
        # entry is still exactly the durable record it names, not a stale
        # or mismatched shape produced by the reconstruction.
        self.assertEqual(entry, history[-1])
        self.assertNotEqual(entry["append_id"], history[0]["append_id"])


class WorkUnitCrashReconstructionTest(_M2EnvMixin, unittest.TestCase):
    """Reopen/next-append reconstruction through the PUBLIC WorkUnit API,
    for both `mint_work_unit` (the FIRST record) and
    `append_work_unit_transition` (a later record)."""

    def test_mint_repairs_preexisting_torn_tail_from_a_dead_partial_file(self):
        """A crash that landed bytes for a work_id's FIRST record but never
        reached its own trailing newline -- e.g. a process killed between
        `os.makedirs` and completing that first `os.write` -- must not
        block a fresh `mint_work_unit` for the same work_id from landing a
        clean, complete first record."""
        w = _make_work_unit()
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b'{"work_id": "%s", "lifecycle_stat' % w["work_id"].encode())
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))
        self.assertEqual(
            state_store.read_work_unit_history(w["session_id"], w["work_id"]), [])

        stored = state_store.mint_work_unit(w)
        self.assertEqual(stored["transition_index"], 0)
        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])
        history = state_store.read_work_unit_history(
            w["session_id"], w["work_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["lifecycle_state"], "pending")

    def test_transition_repairs_torn_tail_before_appending(self):
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])

        with open(path, "ab") as fh:
            fh.write(b'{"lifecycle_state": "preflig')  # torn, no newline
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))
        self.assertEqual(
            [h["lifecycle_state"] for h in state_store.read_work_unit_history(
                w["session_id"], w["work_id"])],
            ["pending"])

        transitioning = _make_work_unit(
            work_id=w["work_id"], session_id=w["session_id"],
            lifecycle_state="preflighting")
        stored = state_store.append_work_unit_transition(transitioning)
        self.assertEqual(stored["transition_index"], 1)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)
        history = state_store.read_work_unit_history(
            w["session_id"], w["work_id"])
        self.assertEqual(
            [h["lifecycle_state"] for h in history], ["pending", "preflighting"])
        self.assertEqual([h["transition_index"] for h in history], [0, 1])


class GraphRevisionCrashReconstructionTest(_M2EnvMixin, unittest.TestCase):
    """Reopen/next-append reconstruction through the PUBLIC dependency-graph
    API: a torn tail left by a simulated crash must be repaired by the next
    `append_graph_revision` call, with revision numbering staying truthful
    (never reusing or skipping a revision number because of the repair)."""

    def test_append_repairs_torn_tail_before_appending(self):
        session_id = _uuid()
        a = _uuid()
        state_store.append_graph_revision(session_id, [_node(a)])
        path = state_store.graph_revisions_path_for(session_id)

        with open(path, "ab") as fh:
            fh.write(b'{"graph_revision": 2, "nod')  # torn, no newline
        self.assertFalse(self._raw_bytes(path).endswith(b"\n"))
        self.assertEqual(len(state_store.read_graph_revisions(session_id)), 1)

        b = _uuid()
        stored = state_store.append_graph_revision(
            session_id, [_node(a), _node(b, predecessors=[a])])
        self.assertEqual(stored["graph_revision"], 2)

        raw = self._raw_bytes(path)
        lines = raw.splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)
        revisions = state_store.read_graph_revisions(session_id)
        self.assertEqual([r["graph_revision"] for r in revisions], [1, 2])
        self.assertEqual(
            state_store.current_graph_revision(session_id), revisions[-1])


# --------------------------------------------------------------------------- #
# Durable, taxonomy-typed PhaseState history.                                 #
# --------------------------------------------------------------------------- #


class PhaseStateHistoryTest(_M2EnvMixin, unittest.TestCase):
    def test_rejects_state_outside_closed_taxonomy(self):
        session_id, work_id = _uuid(), _uuid()
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "made_up_state", "because")
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_terminal_state_requires_reason_code(self):
        session_id, work_id = _uuid(), _uuid()
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "aborted", None)
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "aborted", "")
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_terminal_kill_record_persists(self):
        session_id, work_id = _uuid(), _uuid()
        entry = state_store.append_phase_state_entry(
            session_id, work_id, "aborted", "external_kill",
            event="sigterm", source="package_e")
        self.assertEqual(entry["state"], "aborted")
        self.assertEqual(entry["reason_code"], "external_kill")
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id),
            dict(entry, superseded=False))

    def test_append_return_value_matches_readback_shape_exactly(self):
        """B-14-R2-SHAPE-OPEN, closed: the record `append_phase_state_entry`/
        `_unlocked` return must be identity-identical -- same
        `transition_index`, same `superseded` -- to what a fresh
        `read_phase_state_history`/`current_phase_state` call returns for
        that exact durable entry, for BOTH entry points and across a
        non-terminal, a terminal, and a rejected-further-append state, with
        no `dict(entry, superseded=False)` massaging needed by the caller."""
        session_id, work_id = _uuid(), _uuid()
        running = state_store.append_phase_state_entry(
            session_id, work_id, "running", None)
        self.assertIn("superseded", running)
        self.assertEqual(running["transition_index"], 0)
        self.assertFalse(running["superseded"])
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id)[-1],
            running)
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id), running)

        terminal = state_store.append_phase_state_entry_unlocked(
            session_id, work_id, "aborted", "external_kill",
            event="sigterm", source="package_e")
        self.assertEqual(terminal["transition_index"], 1)
        self.assertFalse(terminal["superseded"])
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id)[-1],
            terminal)
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id), terminal)

    def _adversarial_evidence(self):
        """Evidence shaped to trigger all three B-SHAPE-R1 failure modes at
        once: a non-string dict key (isolated in its own sub-dict so
        `json.dumps(..., sort_keys=True)` does not itself raise TypeError
        sorting mixed str/int keys), a tuple, and a NaN float -- none of
        which round-trips `==`-equal through JSON, by construction."""
        return {
            "gate_validation": {1: "non_string_key", 2: "b"},
            "weird_tuple": (1, 2, 3),
            "nan_value": float("nan"),
        }

    def test_locked_append_survives_tuple_nonstring_key_nan_evidence(self):
        """B-SHAPE-R1, closed: `_reconciled_phase_state_entry` must not
        raise `RuntimeError` merely because `evidence` contains a tuple
        (round-trips through JSON as a list, never `==` the original
        tuple), a non-string dict key (round-trips coerced to a string,
        never `==` the original key), or `NaN` (never `==` `NaN` even
        after an identical round-trip -- not a serialization-fidelity bug,
        IEEE-754 NaN is never self-equal). None of the three is a defect
        in the write path itself, and none may turn a genuinely successful,
        correctly-durable write into a spurious error. Locked entry point."""
        session_id, work_id = _uuid(), _uuid()
        evidence = self._adversarial_evidence()
        entry = state_store.append_phase_state_entry(
            session_id, work_id, "running", None, evidence=evidence)
        self.assertEqual(entry["state"], "running")
        self.assertEqual(entry["transition_index"], 0)
        self.assertFalse(entry["superseded"])
        readback = state_store.read_phase_state_history(session_id, work_id)[-1]
        # Compare via the canonical JSON encoding rather than `==` -- the
        # NaN inside `evidence` would make a direct dict `==` comparison
        # false even between two independently-read copies of the exact
        # same durable bytes, which is a Python float quirk, not a shape
        # mismatch this test is trying to catch.
        self.assertEqual(
            json.dumps(entry, sort_keys=True),
            json.dumps(readback, sort_keys=True))
        self.assertEqual(
            json.dumps(state_store.current_phase_state(session_id, work_id),
                       sort_keys=True),
            json.dumps(entry, sort_keys=True))

    def test_unlocked_append_survives_tuple_nonstring_key_nan_evidence(self):
        """B-SHAPE-R1, closed: identical proof against the UNLOCKED,
        reentrant-safe entry point -- Package E's external-kill handler
        goes through this one, and it must be equally immune to
        `evidence`'s shape causing a spurious `RuntimeError`."""
        session_id, work_id = _uuid(), _uuid()
        evidence = self._adversarial_evidence()
        entry = state_store.append_phase_state_entry_unlocked(
            session_id, work_id, "aborted", "external_kill",
            event="sigterm", source="package_e", evidence=evidence)
        self.assertEqual(entry["state"], "aborted")
        self.assertEqual(entry["transition_index"], 0)
        self.assertFalse(entry["superseded"])
        readback = state_store.read_phase_state_history(session_id, work_id)[-1]
        self.assertEqual(
            json.dumps(entry, sort_keys=True),
            json.dumps(readback, sort_keys=True))
        self.assertEqual(
            json.dumps(state_store.current_phase_state(session_id, work_id),
                       sort_keys=True),
            json.dumps(entry, sort_keys=True))

    def test_non_terminal_state_reason_code_optional(self):
        session_id, work_id = _uuid(), _uuid()
        entry = state_store.append_phase_state_entry(
            session_id, work_id, "running", None)
        self.assertEqual(entry["state"], "running")
        self.assertIsNone(entry["reason_code"])

    def test_history_append_only_and_ordered(self):
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        state_store.append_phase_state_entry(
            session_id, work_id, "preflighting", None)
        state_store.append_phase_state_entry(session_id, work_id, "running", None)
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history],
            ["pending", "preflighting", "running"])
        self.assertEqual([h["transition_index"] for h in history], [0, 1, 2])

    def test_additive_to_legacy_session_phase(self):
        """PhaseState history is a distinct namespace from the legacy
        session-level scouting/planning/building phase tracked by
        get_phase/save_phase; writing one must never affect the other."""
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, "session.json")
        state = state_store.save_phase(path, "planning")
        self.assertEqual(state_store.get_phase(state), "planning")

        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(
            session_id, work_id, "running", None)

        reloaded = state_store.load(path)
        self.assertEqual(state_store.get_phase(reloaded), "planning")
        self.assertNotIn("phase_state", reloaded)

    def test_concurrent_appends_across_processes_serialize_without_corruption(self):
        """`append_phase_state_entry`'s `fcntl.flock` serializes every
        genuinely independent appender -- proven here with real, separate OS
        processes, exactly what this module's own docs describe as the
        actually-supported concurrent case ("genuinely concurrent
        (different-process...) caller must still go through the locked
        entry point" -- `_jsonl_append_unlocked`'s docstring)."""
        session_id, work_id = _uuid(), _uuid()
        n = 8
        ctx = multiprocessing.get_context("fork")
        procs = [
            ctx.Process(target=_mp_append_running, args=(session_id, work_id))
            for _ in range(n)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            self.assertEqual(
                p.exitcode, 0,
                "worker process %s exited with %r" % (p.pid, p.exitcode))

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(len(history), n)
        self.assertEqual(
            sorted(h["transition_index"] for h in history), list(range(n)))
        # Every line must be independently parseable JSON -- no interleaving.
        path = state_store.phase_state_history_path_for(session_id, work_id)
        with open(path) as fh:
            for line in fh:
                json.loads(line)

    def test_appends_succeed_with_long_lived_foreign_threads_alive(self):
        """B-14-R1-A, closed: the real Cowork host is multi-threaded at
        exactly the moments PhaseState matters -- `cowork_bridge.py` starts a
        long-lived `cowork-guard-<role>` daemon thread running
        `broker.serve_forever` for the whole dispatched turn,
        `cowork_ui.py` starts a daemon spinner thread on a TTY, and
        `cowork_verification.py` starts watchdog/capture/cancel-watcher
        threads during verification -- none of which can be stopped while
        the turn an external kill would interrupt is running. This test
        keeps several such long-lived daemon threads alive for the ENTIRE
        span (never joined until after every assertion) and proves every
        `append_phase_state_entry` call still succeeds and is correctly
        serialized: `threading.active_count()` is deterministically >= 1 +
        len(guards) + 1 (this test's own main thread) for the whole test, so
        this is not probabilistic -- a prior version of this module would
        have raised `OSError` and written nothing for every single one of
        these attempts."""
        session_id, work_id = _uuid(), _uuid()
        stop = threading.Event()

        def long_lived_guard():
            # Mirrors cowork_bridge.py's cowork-guard-<role> daemon thread:
            # alive for the whole dispatched turn, never stopped by this
            # module.
            stop.wait(timeout=30)

        guards = [threading.Thread(target=long_lived_guard, daemon=True,
                                    name="cowork-guard-%d" % i)
                  for i in range(3)]
        for g in guards:
            g.start()
        try:
            self.assertGreaterEqual(threading.active_count(), 1 + len(guards))

            n = 6
            results = []
            results_lock = threading.Lock()

            def attempt():
                try:
                    state_store.append_phase_state_entry(
                        session_id, work_id, "running", None)
                    outcome = "ok"
                except OSError:
                    outcome = "os_error"
                with results_lock:
                    results.append(outcome)

            threads = [threading.Thread(target=attempt) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(
                results, ["ok"] * n,
                "every PhaseState append must succeed while long-lived "
                "foreign threads are alive -- this is the exact host "
                "shape B-14-R1-A found broken")

            history = state_store.read_phase_state_history(session_id, work_id)
            self.assertEqual(len(history), n)
            self.assertEqual(
                sorted(h["transition_index"] for h in history),
                list(range(n)))

            # The terminal record itself -- the one Package E's external-kill
            # handler must record -- also succeeds under the exact same
            # multi-thread conditions, still via the safe reentrant entry
            # point (never the locked one, per B-01).
            terminal = state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "aborted", "external_kill",
                event="sigterm", source="package_e")
            self.assertEqual(terminal["state"], "aborted")
            self.assertEqual(
                state_store.current_phase_state(session_id, work_id)["state"],
                "aborted")
        finally:
            stop.set()
            for g in guards:
                g.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# B-02: `completed` PhaseState fails closed without candidate-bound,          #
# gate-validation evidence valid under the frozen control-plane contract.     #
# --------------------------------------------------------------------------- #


def _mint_candidate_work_unit(session_id, work_id, digest, index=None):
    """Mint a WorkUnit for (session_id, work_id) already bound to a real
    candidate identity, so a 'completed' PhaseState attempt against it has
    something legitimate to bind to (B-10)."""
    w = _make_work_unit(
        session_id=session_id, work_id=work_id,
        candidate_manifest_digest=digest, candidate_index=index)
    return state_store.mint_work_unit(w)


class PhaseStateCompletedGateTest(_M2EnvMixin, unittest.TestCase):
    def test_completed_without_minted_work_unit_rejected(self):
        """B-10: there is no legitimate candidate to bind evidence to when
        this work_id was never minted at all -- fails closed even with
        otherwise-perfect evidence."""
        session_id, work_id = _uuid(), _uuid()
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "completed", "gate_validated",
                evidence=_gate_evidence())
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_completed_without_evidence_rejected_and_writes_nothing(self):
        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64)
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "completed", "gate_validated")
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_completed_with_failing_verdict_rejected(self):
        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64)
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "completed", "gate_validated",
                evidence=_gate_evidence(digest="a" * 64, verdict="fail"))
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_completed_with_null_candidate_digest_rejected(self):
        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64)
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "completed", "gate_validated",
                evidence=_gate_evidence(digest=None))
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_completed_with_malformed_evidence_shape_rejected(self):
        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64)
        for bad_evidence in (None, {}, {"gate_validation": "not-a-dict"}):
            with self.assertRaises(ValueError):
                state_store.append_phase_state_entry(
                    session_id, work_id, "completed", "gate_validated",
                    evidence=bad_evidence)
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])

    def test_completed_with_valid_candidate_bound_evidence_persists(self):
        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64, index=2)
        evidence = _gate_evidence(digest="a" * 64, index=2)
        entry = state_store.append_phase_state_entry(
            session_id, work_id, "completed", "gate_validated",
            evidence=evidence)
        self.assertEqual(entry["state"], "completed")
        self.assertEqual(entry["evidence"], evidence)
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id),
            dict(entry, superseded=False))

    def test_unlocked_entry_point_enforces_same_completed_gate(self):
        session_id, work_id = _uuid(), _uuid()
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "completed", "gate_validated")
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])
        _mint_candidate_work_unit(session_id, work_id, "a" * 64)
        entry = state_store.append_phase_state_entry_unlocked(
            session_id, work_id, "completed", "gate_validated",
            evidence=_gate_evidence(digest="a" * 64))
        self.assertEqual(entry["state"], "completed")

    def test_completed_with_evidence_naming_a_different_candidate_rejected(self):
        """B-10: gate evidence that is well-formed, passing, and even names
        a REAL candidate is still refused if it is not THIS work_id's own
        candidate -- proves the binding is to the specific WorkUnit, not
        merely to 'some' well-formed candidate."""
        session_id = _uuid()
        work_id_a, work_id_b = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id_a, "a" * 64)
        _mint_candidate_work_unit(session_id, work_id_b, "b" * 64)

        # Evidence genuinely naming work_id_b's candidate must not complete
        # work_id_a.
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id_a, "completed", "gate_validated",
                evidence=_gate_evidence(digest="b" * 64))
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id_a), [])

        # The genuinely matching evidence still works for each work_id.
        entry_a = state_store.append_phase_state_entry(
            session_id, work_id_a, "completed", "gate_validated",
            evidence=_gate_evidence(digest="a" * 64))
        entry_b = state_store.append_phase_state_entry(
            session_id, work_id_b, "completed", "gate_validated",
            evidence=_gate_evidence(digest="b" * 64))
        self.assertEqual(entry_a["state"], "completed")
        self.assertEqual(entry_b["state"], "completed")

    def test_completed_with_mismatched_candidate_index_rejected(self):
        """B-10: digest alone is not enough -- candidate_index must match
        too, exactly like cowork_control_plane._gate_evidence_matches_
        candidate defines candidate identity as the PAIR."""
        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64, index=0)
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "completed", "gate_validated",
                evidence=_gate_evidence(digest="a" * 64, index=1))
        self.assertEqual(
            state_store.read_phase_state_history(session_id, work_id), [])


# --------------------------------------------------------------------------- #
# B-01: append_phase_state_entry_unlocked is the safe, explicit reentrant     #
# entry point for a caller (Package E's external-kill signal handler) that   #
# may run while this SAME process already holds the locked entry point's     #
# per-path flock. Proven non-vacuously against a REAL held flock, never by    #
# hanging the test process.                                                  #
# --------------------------------------------------------------------------- #


class PhaseStateReentrancyTest(_M2EnvMixin, unittest.TestCase):
    def _hold_external_lock(self, session_id, work_id):
        path = state_store.phase_state_history_path_for(session_id, work_id)
        lock_path = path + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def test_unlocked_entry_point_appends_while_lock_already_held(self):
        """Simulates the exact B-01 scenario: the main flow is inside the
        locked critical section for (session_id, work_id) -- represented
        here by directly holding the same per-path flock a real
        append_phase_state_entry call would be holding -- and a signal
        handler must still be able to durably record a terminal PhaseState.
        Non-vacuous: the lock is a REAL fcntl.flock on the REAL lock path,
        not a mock."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "running", None)
        held = self._hold_external_lock(session_id, work_id)
        try:
            entry = state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "aborted", "external_kill",
                event="sigterm", source="package_e")
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        self.assertEqual(entry["state"], "aborted")
        self.assertEqual(entry["reason_code"], "external_kill")
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual([h["state"] for h in history], ["running", "aborted"])
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id)["state"],
            "aborted")

    def test_locked_entry_point_reentrant_call_self_deadlocks(self):
        """Non-vacuous proof of the exact hazard B-01 closes: calling the
        LOCKED public entry point again while this process already holds
        the SAME path's lock blocks forever -- an open-file-description
        lock is not reentrant, even from the process that already holds it
        via a different fd -- which is exactly why
        append_phase_state_entry_unlocked exists. Run on a background
        daemon thread with a bounded wait so this test itself never hangs:
        the assertion is that the call has NOT completed within the bound,
        then the outer lock is released so the thread unblocks.

        Once unblocked, the reentrant call runs to completion on a SECOND
        thread while this test's own main thread is alive: this module makes
        no thread-count precondition (B-14-R1), so it completes and durably
        persists 'aborted' exactly as it would with no other thread alive."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "running", None)
        held = self._hold_external_lock(session_id, work_id)
        completed = threading.Event()
        outcome = {}

        def reentrant_attempt():
            try:
                state_store.append_phase_state_entry(
                    session_id, work_id, "aborted", "external_kill")
                outcome["result"] = "ok"
            except OSError as exc:
                outcome["result"] = "failed_closed"
                outcome["error"] = exc
            completed.set()

        t = threading.Thread(target=reentrant_attempt, daemon=True)
        t.start()
        try:
            finished_in_time = completed.wait(timeout=1.0)
            self.assertFalse(
                finished_in_time,
                "locked append_phase_state_entry unexpectedly returned "
                "while this process already held the same path's lock -- "
                "the self-deadlock this test guards against did not "
                "reproduce")
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        # Releasing the outer lock lets the blocked thread finally acquire
        # it and finish (one way or another); join with a bound so a
        # genuine regression (the thread staying stuck even after release)
        # fails loudly instead of hanging the suite.
        t.join(timeout=5.0)
        self.assertTrue(completed.is_set(), "reentrant attempt never "
                        "completed even after the outer lock was released")
        self.assertEqual(outcome.get("result"), "ok")
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id)["state"],
            "aborted")
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual([h["state"] for h in history], ["running", "aborted"])


# --------------------------------------------------------------------------- #
# B-11: the EXACT signal-reentry scenario the module banner sanctions must   #
# never let a handler's terminal record be overtaken by the interrupted,     #
# stale-derived append that resumes after it, and transition_index must      #
# stay a single reconstructible sequence (no duplicate/misordered index).    #
# --------------------------------------------------------------------------- #


class PhaseStateInterleavingTest(_M2EnvMixin, unittest.TestCase):
    def test_signal_interruption_cannot_overtake_terminal_record(self):
        """Reproduces exactly what the module banner sanctions: the main
        flow is inside append_phase_state_entry_unlocked for
        (session_id, work_id) -- it has just read the prior history but not
        yet durably written its own (non-terminal) record -- when a SIGTERM
        handler fires and durably records a terminal 'aborted' state via
        append_phase_state_entry_unlocked before returning control. This
        monkeypatches read_jsonl_tolerant (the exact read _jsonl_append_
        unlocked performs) to fire the 'handler' the first time it is
        called, deterministically landing the interruption at the precise
        point the finding describes, without any real OS signal or timing
        race -- so this test can never flake and never hangs.
        """
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        state_store.append_phase_state_entry(session_id, work_id, "running", None)

        real_read = state_store.read_jsonl_tolerant
        calls = {"n": 0}

        def interleaving_read(path):
            calls["n"] += 1
            result = real_read(path)
            if calls["n"] == 1:
                # The "signal handler": runs to completion, exactly as a
                # real signal.signal-delivered handler would, before the
                # interrupted frame (below) resumes.
                state_store.append_phase_state_entry_unlocked(
                    session_id, work_id, "aborted", "external_kill",
                    event="sigterm", source="package_e")
            return result

        state_store.read_jsonl_tolerant = interleaving_read
        try:
            # The "interrupted main flow": its own record was about to be
            # derived from the pre-interruption history (2 records); the
            # handler above lands durably first.
            with self.assertRaises(ValueError):
                state_store.append_phase_state_entry_unlocked(
                    session_id, work_id, "running", None, source="main_flow")
        finally:
            state_store.read_jsonl_tolerant = real_read

        # The terminal record is the durable "current" state -- never
        # overtaken by the interrupted frame's stale-derived append, which
        # was refused instead of silently landing after it.
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id)["state"],
            "aborted")
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history], ["pending", "running", "aborted"])
        # transition_index stays a single reconstructible sequence: no
        # duplicate, strictly increasing, exactly len(history) entries.
        self.assertEqual(
            [h["transition_index"] for h in history], list(range(len(history))))

    def test_unlocked_append_retries_against_fresh_state_when_non_terminal(self):
        """When the file changes underneath an in-flight unlocked append but
        the fresh latest state is still non-terminal (not the terminal-kill
        case above), the stale build is discarded and retried against the
        fresh history -- it still succeeds, with a correctly renumbered
        transition_index and no collision."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)

        real_read = state_store.read_jsonl_tolerant
        calls = {"n": 0}

        def interleaving_read(path):
            calls["n"] += 1
            result = real_read(path)
            if calls["n"] == 1:
                state_store.append_phase_state_entry_unlocked(
                    session_id, work_id, "preflighting", None,
                    source="other_writer")
            return result

        state_store.read_jsonl_tolerant = interleaving_read
        try:
            entry = state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "running", None, source="main_flow")
        finally:
            state_store.read_jsonl_tolerant = real_read

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history],
            ["pending", "preflighting", "running"])
        self.assertEqual(
            [h["transition_index"] for h in history], [0, 1, 2])
        self.assertEqual(entry["transition_index"], 2)

    def test_reconstruction_is_terminal_dominant_regardless_of_file_position(self):
        """Direct, non-vacuous proof of terminal-dominant reconstruction
        against a raw jsonl file -- no signal or thread timing needed. A
        stray non-terminal record positioned AFTER a terminal one (exactly
        what the one residual B-14 write-time sliver can produce -- see
        `PhaseStateFinalWindowSignalTest`) must never be reconstructed as
        "current", and every further legitimate append must still be
        refused, proving the whole-history terminal scan does not depend on
        the terminal record being last. B-14-R2-B: the raw history
        `read_phase_state_history` itself returns -- not merely what
        `current_phase_state` picks out -- must also be truthful and
        non-ambiguous: no duplicate `transition_index`, and the stray
        record explicitly labeled `superseded`, never silently
        indistinguishable from a real transition to a direct reader."""
        session_id, work_id = _uuid(), _uuid()
        path = state_store.phase_state_history_path_for(session_id, work_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        records = [
            {"session_id": session_id, "work_id": work_id, "state": "pending",
             "reason_code": None, "event": None, "evidence": None,
             "source": None, "transition_index": 0,
             "recorded_at": "2024-01-01T00:00:00Z"},
            {"session_id": session_id, "work_id": work_id, "state": "aborted",
             "reason_code": "external_kill", "event": "sigterm",
             "evidence": None, "source": "package_e", "transition_index": 1,
             "recorded_at": "2024-01-01T00:00:01Z"},
            # The race artifact: a stale, non-terminal record that landed
            # AFTER the terminal one, reusing its embedded transition_index
            # -- the exact ambiguity B-14-R2-B closes at the raw-history
            # level, not merely inside current_phase_state.
            {"session_id": session_id, "work_id": work_id, "state": "running",
             "reason_code": None, "event": None, "evidence": None,
             "source": "main_flow", "transition_index": 1,
             "recorded_at": "2024-01-01T00:00:02Z"},
        ]
        for record in records:
            self.assertTrue(state_store.append_jsonl_atomic(path, record))

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history], ["pending", "aborted", "running"])
        # Truthful, collision-free indices -- never trusting the possibly
        # duplicate embedded value -- and an explicit, truthful superseded
        # label visible to a direct reader of this raw list.
        self.assertEqual([h["transition_index"] for h in history], [0, 1, 2])
        self.assertEqual(
            [h["superseded"] for h in history], [False, False, True])

        current = state_store.current_phase_state(session_id, work_id)
        self.assertEqual(current["state"], "aborted")
        self.assertEqual(current["transition_index"], 1)
        self.assertFalse(current["superseded"])

        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry(
                session_id, work_id, "running", None)
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "running", None)


# --------------------------------------------------------------------------- #
# B-SHAPE-R2: append/read identity must stay exact even when two durable      #
# PhaseState records share the identical `recorded_at` value, or when wall    #
# time moves backward between two appends. Every collision below is FORCED   #
# by freezing (or sequencing) `_utc_now`, never merely hoped for from real    #
# clock precision -- a timestamp-uniqueness assumption is exactly the         #
# correctness gap this closes, not something a benchmark of how rarely it     #
# collides in practice could ever prove sound.                                #
# --------------------------------------------------------------------------- #


class PhaseStateDuplicateRecordedAtIdentityTest(_M2EnvMixin, unittest.TestCase):
    def _freeze_utc_now(self, value):
        real = state_store._utc_now
        state_store._utc_now = lambda: value
        self.addCleanup(setattr, state_store, "_utc_now", real)

    def _sequence_utc_now(self, values):
        real = state_store._utc_now
        it = iter(values)
        state_store._utc_now = lambda: next(it)
        self.addCleanup(setattr, state_store, "_utc_now", real)

    def _inject_colliding_duplicate_after_write(self):
        """Hook `append_jsonl_atomic` so that, immediately after THIS call's
        own record durably lands, a SECOND, genuinely different durable
        record -- its own fresh `append_id`, but the identical (frozen)
        `recorded_at` -- lands right behind it, BEFORE this call's own
        read-time reconciliation re-read. This is the exact ordering the
        pre-B-SHAPE-R2 `recorded_at`-only match (search from the end of the
        reconciled list) gets wrong: it would return the injected record,
        not this call's own. Returns the real function so the caller can
        restore it in a `finally`."""
        real_append = state_store.append_jsonl_atomic
        fired = {"n": 0}

        def hooked_append(path, record):
            ok = real_append(path, record)
            fired["n"] += 1
            if fired["n"] == 1:
                other = dict(record)
                other["state"] = "preflighting"
                other["source"] = "concurrent_writer"
                other["append_id"] = state_store._mint_append_id()
                real_append(path, other)
            return ok

        state_store.append_jsonl_atomic = hooked_append
        return real_append

    def test_locked_identity_survives_duplicate_recorded_at_after_own_write(self):
        """Locked entry point, non-vacuous duplicate-`recorded_at` proof: a
        second durable record sharing this call's own frozen `recorded_at`
        physically lands after this call's own write but before its own
        reconciliation re-read -- the returned entry must still be THIS
        call's own record, not the other one."""
        session_id, work_id = _uuid(), _uuid()
        frozen = "2024-01-01T00:00:00Z"
        self._freeze_utc_now(frozen)
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)

        real_append = self._inject_colliding_duplicate_after_write()
        try:
            entry = state_store.append_phase_state_entry(
                session_id, work_id, "running", None)
        finally:
            state_store.append_jsonl_atomic = real_append

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history],
            ["pending", "running", "preflighting"])
        # The collision is real and non-vacuous: every durable record here
        # shares the identical recorded_at.
        self.assertEqual({h["recorded_at"] for h in history}, {frozen})
        self.assertEqual([h["transition_index"] for h in history], [0, 1, 2])

        self.assertEqual(entry["state"], "running")
        self.assertEqual(entry["transition_index"], 1)
        self.assertFalse(entry["superseded"])
        self.assertEqual(entry, history[1])

    def test_unlocked_identity_survives_duplicate_recorded_at_after_own_write(self):
        """Identical proof against the UNLOCKED, reentrant-safe entry point
        -- Package E's external-kill handler goes through this one, and it
        must be equally immune to a same-`recorded_at` collision."""
        session_id, work_id = _uuid(), _uuid()
        frozen = "2024-01-01T00:00:00Z"
        self._freeze_utc_now(frozen)
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)

        real_append = self._inject_colliding_duplicate_after_write()
        try:
            entry = state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "running", None, source="main_flow")
        finally:
            state_store.append_jsonl_atomic = real_append

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history],
            ["pending", "running", "preflighting"])
        self.assertEqual({h["recorded_at"] for h in history}, {frozen})
        self.assertEqual([h["transition_index"] for h in history], [0, 1, 2])

        self.assertEqual(entry["state"], "running")
        self.assertEqual(entry["transition_index"], 1)
        self.assertFalse(entry["superseded"])
        self.assertEqual(entry, history[1])

    def test_concurrent_identity_survives_duplicate_recorded_at_across_processes(self):
        """Genuine cross-process duplicate-`recorded_at` proof, using REAL,
        separate OS processes -- the actually-supported concurrent case
        this module's own docs describe. Deterministically ordered via
        marker files (see `_mp_append_victim`/`_mp_append_peer`) rather than
        hoped-for real timing, so this can never flake: the 'victim'
        process's own record is durably written and its `fcntl.flock`
        released, then the 'peer' process durably writes its OWN record
        with the identical (frozen) `recorded_at`, and ONLY THEN does the
        victim's own reconciliation re-read run and see both -- the exact
        cross-process ordering the pre-B-SHAPE-R2 `recorded_at`-only match
        (search from the end) would get wrong, returning the peer's record
        instead of the victim's own."""
        session_id, work_id = _uuid(), _uuid()
        frozen = "2024-01-01T00:00:00Z"
        victim_marker = os.path.join(self._root, "victim.marker")
        peer_marker = os.path.join(self._root, "peer.marker")
        victim_result = os.path.join(self._root, "victim.json")
        peer_result = os.path.join(self._root, "peer.json")
        ctx = multiprocessing.get_context("fork")
        victim = ctx.Process(
            target=_mp_append_victim,
            args=(session_id, work_id, frozen, victim_marker, peer_marker,
                  victim_result))
        peer = ctx.Process(
            target=_mp_append_peer,
            args=(session_id, work_id, frozen, victim_marker, peer_marker,
                  peer_result))
        victim.start()
        peer.start()
        victim.join(timeout=30)
        peer.join(timeout=30)
        self.assertEqual(victim.exitcode, 0, "victim process failed")
        self.assertEqual(peer.exitcode, 0, "peer process failed")

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual([h["state"] for h in history], ["running", "preflighting"])
        # The collision is real and non-vacuous: both durable records share
        # the identical recorded_at.
        self.assertEqual({h["recorded_at"] for h in history}, {frozen})
        self.assertEqual([h["transition_index"] for h in history], [0, 1])

        with open(victim_result) as fh:
            victim_entry = json.load(fh)
        with open(peer_result) as fh:
            peer_entry = json.load(fh)

        # Each process's own returned entry must be ITS OWN durable record
        # -- never the other process's, even though the victim's own
        # reconciliation re-read happens strictly AFTER the peer's
        # colliding record already physically landed last in the file.
        self.assertEqual(victim_entry["state"], "running")
        self.assertEqual(victim_entry["transition_index"], 0)
        self.assertEqual(victim_entry, history[0])
        self.assertEqual(peer_entry["state"], "preflighting")
        self.assertEqual(peer_entry["transition_index"], 1)
        self.assertEqual(peer_entry, history[1])
        self.assertNotEqual(victim_entry["append_id"], peer_entry["append_id"])

    def test_identity_survives_backward_moving_wall_clock(self):
        """Wall time moving backward between two appends -- an NTP step, a
        clock reset, a resumed suspended host -- must never corrupt
        identity, append order, or `superseded`: none of those may be
        derived from `recorded_at` at all. The third append additionally
        repeats the second's (now-earlier) timestamp exactly, combining
        both hazards in one short history."""
        session_id, work_id = _uuid(), _uuid()
        later, earlier = "2024-06-01T12:00:00Z", "2024-01-01T00:00:00Z"
        self._sequence_utc_now([later, earlier, earlier])

        first = state_store.append_phase_state_entry(
            session_id, work_id, "pending", None)
        second = state_store.append_phase_state_entry(
            session_id, work_id, "preflighting", None)
        third = state_store.append_phase_state_entry(
            session_id, work_id, "running", None)

        self.assertEqual(first["recorded_at"], later)
        self.assertEqual(second["recorded_at"], earlier)
        self.assertEqual(third["recorded_at"], earlier)

        # Append order (and therefore transition_index/identity) tracks the
        # true order of the calls, never a clock that just moved backward.
        self.assertEqual(
            [first["state"], second["state"], third["state"]],
            ["pending", "preflighting", "running"])
        self.assertEqual(
            [first["transition_index"], second["transition_index"],
             third["transition_index"]], [0, 1, 2])
        self.assertEqual(
            len({first["append_id"], second["append_id"], third["append_id"]}),
            3)

        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history],
            ["pending", "preflighting", "running"])
        self.assertEqual([h["transition_index"] for h in history], [0, 1, 2])
        self.assertEqual(
            [h["superseded"] for h in history], [False, False, False])
        self.assertEqual(second["append_id"], history[1]["append_id"])
        self.assertEqual(third["append_id"], history[2]["append_id"])


# --------------------------------------------------------------------------- #
# B-14 / B-14-R1: the ONE-signal residual the corrected review found -- a     #
# signal landing strictly between `_jsonl_append_unlocked`'s freshness       #
# re-read and its `append_jsonl_atomic` call can still let a handler's       #
# terminal record be overtaken by the interrupted, stale-derived append that #
# resumes right after it, physically landing later in the file. This module  #
# no longer tries to block that signal (a prior version's                   #
# `threading.active_count() == 1` + `pthread_sigmask` attempt made every     #
# PhaseState write refuse outright in the real, always-multi-threaded host   #
# -- B-14-R1-A). Instead it tolerates the overtake and makes it harmless:    #
# `current_phase_state` is terminal-dominant, so the handler's terminal      #
# record is still the durable "current" truth no matter where it physically  #
# lands. Uses a REAL `os.kill` + a REAL `signal.signal` handler (not a       #
# simulated call) fired from inside the actual write path, so this proves    #
# genuine OS-level interleaving, not merely a favorable read ordering.       #
# --------------------------------------------------------------------------- #


class PhaseStateFinalWindowSignalTest(_M2EnvMixin, unittest.TestCase):
    def test_real_signal_in_final_window_overtakes_but_terminal_still_dominates(self):
        """Reproduces the exact B-14 residual with a real signal: the main
        flow's own `_jsonl_append_unlocked` call for 'running' has already
        passed its freshness re-read (confirming the file still matches what
        it read) when a real SIGUSR1 fires, synchronously running a handler
        that durably records a terminal 'aborted' PhaseState via
        `append_phase_state_entry_unlocked` -- entirely BEFORE the
        interrupted frame's own pending 'running' write executes. That
        stale 'running' record then lands in the file strictly AFTER
        'aborted', reusing its `transition_index` -- proving the race is
        real, not merely theoretical -- yet `current_phase_state` still
        durably reports 'aborted', not 'running', because it is
        terminal-dominant: zero false reconstruction, zero lost terminal
        currency, even though the write-time race was never prevented."""
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("SIGUSR1 unavailable on this platform")

        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(
            session_id, work_id, "pending", None)

        handler_ran = []

        def handler(signum, frame):
            handler_ran.append(True)
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "aborted", "external_kill",
                event="sigterm", source="package_e")

        old_handler = signal.signal(signal.SIGUSR1, handler)
        real_append = state_store.append_jsonl_atomic
        fired = {"n": 0}

        def signalling_append(path, record):
            fired["n"] += 1
            if fired["n"] == 1:
                # append_jsonl_atomic is only ever called from inside
                # _jsonl_append_unlocked, immediately after its freshness
                # re-read -- exactly the window B-14 found. Nothing defers
                # this signal now, so `handler` runs to completion here,
                # inline, durably writing 'aborted' BEFORE this call proceeds
                # to durably write its own stale 'running' record.
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(
                    handler_ran, [True],
                    "the handler did not run synchronously inside this "
                    "window -- test setup bug, this is meant to reproduce "
                    "the real interleaving")
            return real_append(path, record)

        state_store.append_jsonl_atomic = signalling_append
        try:
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "running", None, source="main_flow")
        finally:
            state_store.append_jsonl_atomic = real_append
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])

        # The race artifact is real and visible in the raw history: the
        # stale 'running' record durably landed AFTER 'aborted'. B-14-R2-B:
        # `read_phase_state_history` itself (not merely `current_phase_state`)
        # must present this truthfully and non-ambiguously -- a corrected,
        # collision-free transition_index and an explicit `superseded` label
        # on the race artifact, never a naive duplicate a raw-history reader
        # could mistake for two legitimate transitions.
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history], ["pending", "aborted", "running"])
        self.assertEqual(
            [h["transition_index"] for h in history], [0, 1, 2])
        self.assertEqual(
            [h["superseded"] for h in history], [False, False, True])

        # Yet the durable, dominant current state is still the terminal one
        # -- never lost, never overtaken -- because current_phase_state
        # scans for the first terminal record rather than trusting position.
        current = state_store.current_phase_state(session_id, work_id)
        self.assertEqual(current["state"], "aborted")
        self.assertFalse(current["superseded"])

        # Truthful to read_m2_state consumers too, not merely to a direct
        # current_phase_state/read_phase_state_history caller.
        m2 = state_store.read_m2_state(
            {"session_uuid": session_id}, work_id=work_id)
        self.assertEqual(m2["phase_state"]["state"], "aborted")
        self.assertEqual(
            [h["superseded"] for h in m2["phase_state_history"]],
            [False, False, True])

        # And the taxonomy-level guarantee still holds: any FURTHER attempt
        # to append is refused, because the builder scans the whole history
        # for a terminal record, not merely the physically-last entry.
        with self.assertRaises(ValueError):
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "running", None, source="late_writer")

    def test_real_signal_terminal_vs_terminal_race_retains_correct_currency(self):
        """B-14-R2-A: a genuine terminal-vs-terminal race, not merely
        terminal-vs-non-terminal. The main flow's own already
        candidate-bound, gate-valid 'completed' write is interrupted, in
        the exact same final write-time window, by Package E's
        external-kill handler durably recording 'aborted' FIRST. Both
        sides are terminal; this proves the fix genuinely resolves two
        racing terminal writes to the correct (first-durably-written) one
        -- not merely "a terminal record beats a non-terminal one" -- with
        truthful, non-colliding identity for both durable records."""
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("SIGUSR1 unavailable on this platform")

        session_id, work_id = _uuid(), _uuid()
        _mint_candidate_work_unit(session_id, work_id, "a" * 64)
        state_store.append_phase_state_entry(session_id, work_id, "running", None)
        evidence = _gate_evidence(digest="a" * 64)

        handler_ran = []

        def handler(signum, frame):
            handler_ran.append(True)
            state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "aborted", "external_kill",
                event="sigterm", source="package_e")

        old_handler = signal.signal(signal.SIGUSR1, handler)
        real_append = state_store.append_jsonl_atomic
        fired = {"n": 0}

        def signalling_append(path, record):
            fired["n"] += 1
            if fired["n"] == 1:
                os.kill(os.getpid(), signal.SIGUSR1)
                self.assertEqual(handler_ran, [True])
            return real_append(path, record)

        state_store.append_jsonl_atomic = signalling_append
        try:
            completed_entry = state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "completed", "gate_validated",
                evidence=evidence, source="main_flow")
        finally:
            state_store.append_jsonl_atomic = real_append
            signal.signal(signal.SIGUSR1, old_handler)

        self.assertEqual(handler_ran, [True])

        # Both terminal records durably landed -- the race is real -- with
        # truthful, collision-free identity for each.
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history], ["running", "aborted", "completed"])
        self.assertEqual(
            [h["transition_index"] for h in history], [0, 1, 2])
        self.assertEqual(
            [h["superseded"] for h in history], [False, False, True])

        # B-14-R2-SHAPE-OPEN, closed: even in this EXACT residual-sliver
        # race -- the one case where the build-time transition_index could
        # genuinely collide -- the record `append_phase_state_entry_unlocked`
        # itself returns for the losing 'completed' write is the SAME
        # canonical, read-time-reconciled shape (true position 2,
        # superseded=True) as what `read_phase_state_history` reports for
        # that identical durable entry, not the stale build-time dict.
        self.assertEqual(completed_entry, history[-1])
        self.assertEqual(completed_entry["transition_index"], 2)
        self.assertTrue(completed_entry["superseded"])

        # The genuinely first-durably-written terminal record -- the
        # external kill -- retains currency, even against a second,
        # later-written terminal record, not merely a non-terminal one.
        current = state_store.current_phase_state(session_id, work_id)
        self.assertEqual(current["state"], "aborted")
        self.assertFalse(current["superseded"])

        m2 = state_store.read_m2_state(
            {"session_uuid": session_id}, work_id=work_id)
        self.assertEqual(m2["phase_state"]["state"], "aborted")
        self.assertEqual(
            [h["state"] for h in m2["phase_state_history"]],
            ["running", "aborted", "completed"])


# --------------------------------------------------------------------------- #
# B-14-R1: a prior version blocked signals on the calling thread alone and    #
# documented that as a process-wide guarantee -- false whenever a second,     #
# `threading`-visible thread exists with the signal still unblocked, since    #
# the OS may deliver a process-directed signal to THAT thread instead, and    #
# Python schedules the resulting Python-level handler call on the main        #
# thread's next eval-loop check regardless of this thread's own mask. This    #
# module no longer attempts that thread-local block at all (see the          #
# module-level TERMINAL-DOMINANT RECONSTRUCTION banner in cowork_state.py),   #
# so a foreign thread being alive can never cause a PhaseState append to be   #
# refused.                                                                    #
# --------------------------------------------------------------------------- #


class PhaseStateForeignThreadSignalTest(_M2EnvMixin, unittest.TestCase):
    def test_foreign_unblocked_thread_can_run_handler_despite_this_threads_block(self):
        """The B-14-R1 premise, proven independently of cowork_state.py: a
        `signal.signal`-registered handler can still run WHILE this (main)
        thread has the signal blocked via `pthread_sigmask`, if some OTHER,
        foreign thread that has NOT blocked it receives the process-directed
        signal instead. Real `os.kill`, real `signal.signal`, real
        `threading.Thread` -- no simulation, no mocking."""
        if not hasattr(signal, "SIGUSR2"):
            self.skipTest("SIGUSR2 unavailable on this platform")

        ran = threading.Event()

        def handler(signum, frame):
            ran.set()

        old_handler = signal.signal(signal.SIGUSR2, handler)

        release = threading.Event()

        def foreign_thread():
            # Deliberately does NOT block SIGUSR2 -- an ordinary, unrelated
            # thread elsewhere in the process that knows nothing about this
            # module's signal-safety story.
            release.wait(timeout=5.0)
            os.kill(os.getpid(), signal.SIGUSR2)

        # Started BEFORE this thread blocks, so it inherits the
        # still-unblocked mask at creation time (POSIX threads inherit
        # their creator's mask only at the moment of creation) -- exactly
        # the "some thread predates or never joins this thread's block"
        # case a real application cannot rule out.
        t = threading.Thread(target=foreign_thread)
        t.start()

        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR2})
        try:
            release.set()
            t.join(timeout=2.0)
            # Give the main thread's eval loop a bounded moment to service
            # the pending call the foreign thread's trampoline scheduled --
            # still while THIS thread's own mask remains blocked.
            deadline = time.time() + 2.0
            while not ran.is_set() and time.time() < deadline:
                pass
            self.assertTrue(
                ran.is_set(),
                "the foreign thread's signal never ran its handler while "
                "this thread had SIGUSR2 blocked -- test setup bug, this is "
                "meant to reproduce the real hazard B-14-R1 closes")
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            signal.signal(signal.SIGUSR2, old_handler)

    def test_phase_state_append_succeeds_while_foreign_thread_active(self):
        """B-14-R1-A, closed: Package E's external-kill handler calls
        exactly `append_phase_state_entry_unlocked` while a foreign,
        `threading`-visible thread is alive and has NOT blocked the
        external-kill signal (the real-host shape the test above proves is
        genuinely unsafe for thread-local `pthread_sigmask` alone) -- this
        module makes no attempt at that thread-local block and no
        thread-count precondition, so the terminal record is durably
        recorded regardless."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(
            session_id, work_id, "pending", None)

        foreign_alive = threading.Event()
        release_foreign = threading.Event()

        def foreign_thread():
            foreign_alive.set()
            release_foreign.wait(timeout=5.0)

        t = threading.Thread(target=foreign_thread)
        t.start()
        try:
            self.assertTrue(
                foreign_alive.wait(timeout=2.0),
                "foreign thread never started -- test setup bug")
            entry = state_store.append_phase_state_entry_unlocked(
                session_id, work_id, "aborted", "external_kill",
                event="sigterm", source="package_e")
        finally:
            release_foreign.set()
            t.join(timeout=5.0)

        self.assertEqual(entry["state"], "aborted")
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual(
            [h["state"] for h in history], ["pending", "aborted"])
        self.assertEqual(
            [h["transition_index"] for h in history],
            list(range(len(history))))
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id)["state"],
            "aborted")


# --------------------------------------------------------------------------- #
# Atomic controller policy/config transition record.                         #
# --------------------------------------------------------------------------- #


class ControllerTransitionTest(_M2EnvMixin, unittest.TestCase):
    def test_default_state_is_zero_revision(self):
        session_id = _uuid()
        default = state_store.read_controller_transition(session_id)
        self.assertEqual(
            default, {"policy": None, "config": None, "revision": 0})

    def test_first_proposal_commits(self):
        session_id = _uuid()
        result = state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]},
            config={"builder": {"controller": "claude"}})
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["state"]["revision"], 1)
        self.assertEqual(
            state_store.read_controller_transition(session_id), result["state"])

    def test_stale_revision_rejected_and_bytes_unchanged(self):
        session_id = _uuid()
        first = state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        path = state_store.controller_transition_path_for(session_id)
        before = self._raw_bytes(path)

        second = state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["codex"]})
        self.assertEqual(second["outcome"], "rejected")
        self.assertEqual(second["reason"], "stale_revision")
        self.assertEqual(second["state"], first["state"])

        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        self.assertEqual(
            state_store.read_controller_transition(session_id), first["state"])

    def test_correct_next_revision_commits_after_prior_commit(self):
        session_id = _uuid()
        first = state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        second = state_store.propose_controller_transition(
            session_id, first["state"]["revision"], policy={"allowed": ["codex"]})
        self.assertEqual(second["outcome"], "committed")
        self.assertEqual(second["state"]["revision"], 2)
        self.assertEqual(second["state"]["policy"], {"allowed": ["codex"]})

    def test_validate_hook_rejects_and_bytes_unchanged(self):
        session_id = _uuid()
        state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        path = state_store.controller_transition_path_for(session_id)
        before = self._raw_bytes(path)

        def reject_everything(current, policy, config):
            raise ValueError("policy must name at least one controller")

        result = state_store.propose_controller_transition(
            session_id, 1, policy={"allowed": []},
            validate=reject_everything)
        self.assertEqual(result["outcome"], "rejected")
        self.assertTrue(result["reason"].startswith("invalid:"))
        after = self._raw_bytes(path)
        self.assertEqual(before, after)

    def test_no_session_id_rejected(self):
        result = state_store.propose_controller_transition(None, 0, policy={})
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["reason"], "no_session")

    def test_every_attempt_logged(self):
        session_id = _uuid()
        state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["codex"]})  # stale -> rejected
        log = state_store.read_controller_transition_log(session_id)
        self.assertEqual(len(log), 2)
        self.assertEqual([e["outcome"] for e in log], ["committed", "rejected"])

    def test_racing_callers_cannot_both_commit(self):
        session_id = _uuid()
        outcomes = []
        lock = threading.Lock()

        def attempt():
            r = state_store.propose_controller_transition(
                session_id, 0, policy={"allowed": ["claude"]})
            with lock:
                outcomes.append(r["outcome"])

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("committed"), 1)
        self.assertEqual(outcomes.count("rejected"), 7)
        self.assertEqual(
            state_store.read_controller_transition(session_id)["revision"], 1)


# --------------------------------------------------------------------------- #
# Legacy read/migration shim.                                                 #
# --------------------------------------------------------------------------- #


class LegacyMigrationShimTest(_M2EnvMixin, unittest.TestCase):
    def _legacy_v1_fixture(self):
        """A version-1 session anchor captured from base, before M2 existed:
        no controller_policy, no context, no M2 fields whatsoever."""
        return {
            "version": 1,
            "team": ["scout", "builder"],
            "config": {
                "scout": {"controller": "claude", "model": None,
                          "effort": None, "yolo": True, "mode": "plan"},
                "builder": {"controller": "codex", "model": None,
                           "effort": None, "yolo": True, "mode": "plan"},
            },
            "sessions": {
                "scout": {"controller": "claude", "id": "sess-abc"},
            },
            "session_uuid": _uuid(),
            "created": 1750000000.0,
            "phase": "building",
        }

    def test_legacy_fixture_round_trips_unchanged(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        path = os.path.join(d, "session.json")
        fixture = self._legacy_v1_fixture()
        state_store.save(path, dict(fixture))
        loaded = state_store.load(path)
        self.assertEqual(loaded, dict(fixture, version=1))
        # Prior externally observable behavior is unaffected by M2's presence.
        self.assertEqual(state_store.get_phase(loaded), "building")
        self.assertEqual(
            state_store.read_controller_policy(loaded), ("unrestricted", None))
        self.assertIsNone(state_store.get_worktree(loaded))

    def test_legacy_session_synthesizes_m2_state_as_absent(self):
        fixture = self._legacy_v1_fixture()
        result = state_store.read_m2_state(fixture, work_id=_uuid())
        self.assertEqual(result["session_uuid"], fixture["session_uuid"])
        self.assertIsNone(result["work_unit_state"])
        self.assertEqual(result["work_unit_history"], [])
        self.assertIsNone(result["graph_revision"])
        self.assertEqual(result["graph_revisions"], ())
        self.assertIsNone(result["phase_state"])
        self.assertEqual(result["phase_state_history"], [])
        self.assertIsNone(result["controller_transition"])

    def test_legacy_session_with_no_session_uuid_never_touches_disk(self):
        fixture = self._legacy_v1_fixture()
        del fixture["session_uuid"]
        result = state_store.read_m2_state(fixture, work_id=_uuid())
        self.assertIsNone(result["session_uuid"])
        self.assertIsNone(result["work_unit_state"])
        self.assertEqual(result["graph_revisions"], ())

    def test_read_m2_state_never_raises_on_malformed_state(self):
        for bad in (None, {}, {"session_uuid": None}, "not-a-dict"):
            try:
                result = state_store.read_m2_state(bad)
            except Exception as exc:  # noqa: BLE001
                self.fail("read_m2_state raised on %r: %r" % (bad, exc))
            self.assertIsNone(result["session_uuid"])

    def test_read_m2_state_never_raises_on_truthy_malformed_session_uuid(self):
        """B-03 regression: every case above (None, {}, {'session_uuid':
        None}) has a FALSY session_uuid and returns at the early guard
        without ever reaching a path-building helper. These cases are
        TRUTHY and, without the fix, reach `_lower_safe_identifier` via
        `read_controller_transition` and raise ValueError -- a non-str
        session_uuid (12345, True) or one that fails the path-safety check
        (a traversal attempt) -- turning a corrupt legacy anchor into an
        unhandled crash for any caller of this documented 'Never raises'
        shim."""
        for bad_session_uuid in (12345, True, "../../etc", "not/a-safe-id"):
            fixture = self._legacy_v1_fixture()
            fixture["session_uuid"] = bad_session_uuid
            try:
                result = state_store.read_m2_state(fixture, work_id=_uuid())
            except Exception as exc:  # noqa: BLE001
                self.fail("read_m2_state raised on session_uuid=%r: %r"
                          % (bad_session_uuid, exc))
            self.assertEqual(result["session_uuid"], bad_session_uuid)
            self.assertIsNone(result["work_unit_state"])
            self.assertEqual(result["work_unit_history"], [])
            self.assertIsNone(result["graph_revision"])
            self.assertEqual(result["graph_revisions"], ())
            self.assertIsNone(result["phase_state"])
            self.assertEqual(result["phase_state_history"], [])
            self.assertIsNone(result["controller_transition"])

    def test_read_m2_state_never_raises_on_truthy_malformed_work_id(self):
        """Same hazard, reached via a valid session_uuid but a truthy
        malformed/unsafe work_id, deep inside the `if work_id:` branch's own
        path-building helpers."""
        session_id = _uuid()
        for bad_work_id in (12345, "../../etc"):
            fixture = self._legacy_v1_fixture()
            fixture["session_uuid"] = session_id
            try:
                result = state_store.read_m2_state(
                    fixture, work_id=bad_work_id)
            except Exception as exc:  # noqa: BLE001
                self.fail("read_m2_state raised on work_id=%r: %r"
                          % (bad_work_id, exc))
            self.assertEqual(result["session_uuid"], session_id)
            self.assertIsNone(result["work_unit_state"])
            self.assertEqual(result["work_unit_history"], [])
            self.assertIsNone(result["graph_revision"])
            self.assertEqual(result["graph_revisions"], ())

    def test_read_m2_state_reflects_written_m2_artifacts(self):
        session_id = _uuid()
        work_id = _uuid()
        w = _make_work_unit(session_id=session_id, work_id=work_id)
        state_store.mint_work_unit(w)
        state_store.append_graph_revision(session_id, [_node(work_id)])
        state_store.append_phase_state_entry(
            session_id, work_id, "running", None)
        state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]})

        fixture = self._legacy_v1_fixture()
        fixture["session_uuid"] = session_id
        result = state_store.read_m2_state(fixture, work_id=work_id)

        self.assertIsNotNone(result["work_unit_state"])
        self.assertEqual(result["work_unit_state"]["work_id"], work_id)
        self.assertEqual(len(result["work_unit_history"]), 1)
        self.assertEqual(result["graph_revision"]["graph_revision"], 1)
        self.assertEqual(len(result["graph_revisions"]), 1)
        self.assertEqual(result["phase_state"]["state"], "running")
        self.assertEqual(len(result["phase_state_history"]), 1)
        self.assertEqual(result["controller_transition"]["revision"], 1)


# --------------------------------------------------------------------------- #
# Crash-injection: simulated failures at each new write boundary must leave  #
# exactly the prior durable state, never a torn or falsely-completed one.    #
# --------------------------------------------------------------------------- #


class CrashInjectionTest(_M2EnvMixin, unittest.TestCase):
    def test_locked_append_failure_leaves_no_partial_line(self):
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "pending", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)
        before = self._raw_bytes(path)

        original = state_store.append_jsonl_atomic
        try:
            state_store.append_jsonl_atomic = lambda *a, **k: False
            with self.assertRaises(OSError):
                state_store.append_phase_state_entry(
                    session_id, work_id, "running", None)
        finally:
            state_store.append_jsonl_atomic = original

        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        self.assertEqual(
            len(state_store.read_phase_state_history(session_id, work_id)), 1)

    def test_unlocked_terminal_append_failure_leaves_no_partial_record(self):
        """Directly coupled to the exact durable boundary Package E's
        external-kill handler depends on -- the case above only exercises
        the LOCKED, non-terminal write path. If `append_jsonl_atomic`
        itself fails (disk full, permission race, ...) while durably
        writing the TERMINAL record via the unlocked, reentrant-safe entry
        point, this must raise and leave nothing partially written, and
        `current_phase_state` must still report the prior, non-terminal
        state -- a crash or retry right at this boundary can never
        fabricate an ambiguous or falsely-recorded terminal record."""
        session_id, work_id = _uuid(), _uuid()
        state_store.append_phase_state_entry(session_id, work_id, "running", None)
        path = state_store.phase_state_history_path_for(session_id, work_id)
        before = self._raw_bytes(path)

        original = state_store.append_jsonl_atomic
        try:
            state_store.append_jsonl_atomic = lambda *a, **k: False
            with self.assertRaises(OSError):
                state_store.append_phase_state_entry_unlocked(
                    session_id, work_id, "aborted", "external_kill",
                    event="sigterm", source="package_e")
        finally:
            state_store.append_jsonl_atomic = original

        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        history = state_store.read_phase_state_history(session_id, work_id)
        self.assertEqual([h["state"] for h in history], ["running"])
        self.assertEqual(
            state_store.current_phase_state(session_id, work_id)["state"],
            "running")

    def test_controller_transition_write_failure_rejects_cleanly(self):
        session_id = _uuid()
        state_store.propose_controller_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        path = state_store.controller_transition_path_for(session_id)
        before = self._raw_bytes(path)

        original = state_store.write_json_atomic
        try:
            state_store.write_json_atomic = lambda *a, **k: False
            result = state_store.propose_controller_transition(
                session_id, 1, policy={"allowed": ["codex"]})
        finally:
            state_store.write_json_atomic = original

        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["reason"], "write_failed")
        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        self.assertEqual(
            state_store.read_controller_transition(session_id)["revision"], 1)

    def test_work_unit_mint_failure_leaves_no_file(self):
        w = _make_work_unit()
        original = state_store.append_jsonl_atomic
        try:
            state_store.append_jsonl_atomic = lambda *a, **k: False
            with self.assertRaises(OSError):
                state_store.mint_work_unit(w)
        finally:
            state_store.append_jsonl_atomic = original
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        self.assertFalse(os.path.exists(path))

    def test_work_unit_transition_failure_leaves_no_partial_record(self):
        """B-COV-1: crash injection at the `append_work_unit_transition`
        durable boundary -- the plan's completion condition is
        crash-injection coverage at 100% of the new write boundaries, and
        this one (unlike `mint_work_unit` above) appends to an
        ALREADY-NON-EMPTY history, so it separately proves a failed
        transition append never corrupts or truncates the prior mint line.
        If `append_jsonl_atomic` itself fails (disk full, permission race,
        ...) while durably appending a transition for an already-minted
        work_id, this must raise and leave the file byte-identical to its
        pre-attempt state, with the prior mint record still the current
        state."""
        w = _make_work_unit()
        state_store.mint_work_unit(w)
        path = state_store.work_unit_history_path_for(
            w["session_id"], w["work_id"])
        before = self._raw_bytes(path)

        running = _make_work_unit(
            work_id=w["work_id"], session_id=w["session_id"],
            lifecycle_state="preflighting")
        original = state_store.append_jsonl_atomic
        try:
            state_store.append_jsonl_atomic = lambda *a, **k: False
            with self.assertRaises(OSError):
                state_store.append_work_unit_transition(running)
        finally:
            state_store.append_jsonl_atomic = original

        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        history = state_store.read_work_unit_history(
            w["session_id"], w["work_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(
            state_store.current_work_unit_state(
                w["session_id"], w["work_id"])["lifecycle_state"],
            "pending")

    def test_graph_revision_failure_leaves_no_partial_record(self):
        """B-COV-1: crash injection at the `append_graph_revision` durable
        boundary -- the last of the plan's six required crash-boundary
        proofs. A first revision commits normally; a simulated
        `append_jsonl_atomic` failure on a SECOND revision attempt must
        raise, leave the file byte-identical (the first revision's line
        untouched, no second line ever appears), and
        `current_graph_revision` must still report revision 1, never a
        torn or falsely-advanced revision."""
        session_id = _uuid()
        a = _uuid()
        state_store.append_graph_revision(session_id, [_node(a)])
        path = state_store.graph_revisions_path_for(session_id)
        before = self._raw_bytes(path)

        original = state_store.append_jsonl_atomic
        try:
            state_store.append_jsonl_atomic = lambda *a, **k: False
            with self.assertRaises(OSError):
                state_store.append_graph_revision(session_id, [_node(a)])
        finally:
            state_store.append_jsonl_atomic = original

        after = self._raw_bytes(path)
        self.assertEqual(before, after)
        revisions = state_store.read_graph_revisions(session_id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(
            state_store.current_graph_revision(session_id)["graph_revision"],
            1)


# --------------------------------------------------------------------------- #
# Isolated-snapshot compatibility: cowork_state.py must import and run every  #
# pre-M2 export exactly as before even when its M2 Package A siblings        #
# (cowork_control_plane.py, cowork_workunit.py) are entirely ABSENT from the  #
# captured environment. This is verified in a real subprocess, on a real     #
# isolated sys.path, rather than by monkeypatching sys.modules in-process --  #
# only a genuinely separate interpreter proves the module-level import        #
# graph never pulls the Package A siblings in eagerly.                       #
# --------------------------------------------------------------------------- #

_ISOLATED_SNAPSHOT_SCRIPT = textwrap.dedent("""
    import os
    import sys
    import tempfile
    import uuid

    sys.path.insert(0, %(isolated_dir)r)
    os.environ["COWORK_SESSIONS_ROOT"] = tempfile.mkdtemp()

    # cowork_control_plane / cowork_workunit are NOT importable at all from
    # this interpreter's sys.path -- only cowork_state.py and its genuine
    # pre-M2 dependency cowork_policy.py were captured.
    for missing in ("cowork_control_plane", "cowork_workunit"):
        try:
            __import__(missing)
        except ImportError:
            pass
        else:
            print("FAIL: %%s importable in the isolated snapshot -- test "
                  "setup is not isolated" %% missing)
            sys.exit(1)

    # Importing cowork_state itself must succeed with no M2 siblings present.
    import cowork_state as state_store

    # Every pre-M2 export must execute exactly as before.
    d = tempfile.mkdtemp()
    path = os.path.join(d, "session.json")
    state = state_store.save_phase(path, "planning")
    assert state_store.get_phase(state) == "planning"
    reloaded = state_store.load(path)
    assert state_store.get_phase(reloaded) == "planning"
    assert state_store.read_controller_policy(reloaded) == (
        "unrestricted", None)
    assert state_store.get_worktree(reloaded) is None

    def mk_work_unit():
        return dict(
            schema_version=1, record="WorkUnit",
            work_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()),
            phase="building", role="builder", seat=0, round=1, attempt=1,
            controller="claude", provider="anthropic",
            requested_model="sonnet", effective_model="sonnet",
            effort="high", candidate_manifest_digest=None,
            candidate_index=None, prompt_digest="b" * 64,
            pending_turn_digest=None, parent_work_id=None,
            governed_child_policy="inherit", graph_revision=1,
            predecessor_work_ids=[], fan_join_id=None,
            lifecycle_state="pending", terminal_reason=None,
        )

    def expect_import_error(label, fn):
        try:
            fn()
        except ImportError:
            return
        except Exception as exc:
            print("FAIL: %%s raised %%r, expected ImportError"
                  %% (label, exc))
            sys.exit(1)
        else:
            print("FAIL: %%s did not raise" %% label)
            sys.exit(1)

    # M2 functions that genuinely need the missing Package A siblings raise a
    # plain ImportError -- never AttributeError/NameError -- and only when
    # actually CALLED, never merely by importing cowork_state.
    expect_import_error(
        "mint_work_unit", lambda: state_store.mint_work_unit(mk_work_unit()))
    expect_import_error(
        "append_phase_state_entry",
        lambda: state_store.append_phase_state_entry(
            str(uuid.uuid4()), str(uuid.uuid4()), "pending", None))
    expect_import_error(
        "append_graph_revision",
        lambda: state_store.append_graph_revision(str(uuid.uuid4()), []))

    # M2 functions that do NOT need Package A (pure reads, and the
    # control-transition CAS, which validates nothing beyond its own
    # revision counter) keep working normally.
    result = state_store.propose_controller_transition(
        str(uuid.uuid4()), 0, policy={"allowed": ["claude"]})
    assert result["outcome"] == "committed", result

    m2 = state_store.read_m2_state(reloaded, work_id=str(uuid.uuid4()))
    assert m2["session_uuid"] is None  # the legacy fixture has none
    assert m2["work_unit_state"] is None
    assert m2["graph_revisions"] == ()

    # B-03-R1: the case above is VACUOUS for the actual hazard -- the
    # legacy fixture carries no session_uuid at all, so read_m2_state
    # short-circuits before ever reaching current_phase_state/
    # current_work_unit_state, and never even attempts the Package-A
    # import that could raise. A real, TRUTHY session_uuid AND work_id,
    # naming a session that genuinely HAS durable PhaseState history on
    # disk (written here directly, exactly as a full deployment WITH
    # Package A would have durably written it), is what actually exercises
    # current_phase_state's Package-A import inside read_m2_state's own
    # call chain -- this must still never raise.
    real_session_uuid = str(uuid.uuid4())
    real_work_id = str(uuid.uuid4())
    phase_path = state_store.phase_state_history_path_for(
        real_session_uuid, real_work_id)
    os.makedirs(os.path.dirname(phase_path), exist_ok=True)
    for phase_record in (
        {"session_id": real_session_uuid, "work_id": real_work_id,
         "state": "running", "reason_code": None, "event": None,
         "evidence": None, "source": None, "transition_index": 0,
         "recorded_at": "2024-01-01T00:00:00Z"},
        {"session_id": real_session_uuid, "work_id": real_work_id,
         "state": "aborted", "reason_code": "external_kill",
         "event": "sigterm", "evidence": None, "source": "package_e",
         "transition_index": 1, "recorded_at": "2024-01-01T00:00:01Z"},
    ):
        assert state_store.append_jsonl_atomic(phase_path, phase_record)

    m2_with_data = state_store.read_m2_state(
        {"session_uuid": real_session_uuid}, work_id=real_work_id)
    assert m2_with_data["session_uuid"] == real_session_uuid
    # Package A is genuinely absent from this interpreter, so the
    # taxonomy-aware fields cannot be interpreted -- must degrade to the
    # tolerant absent shape, never raise ImportError out of read_m2_state.
    assert m2_with_data["phase_state"] is None, m2_with_data
    assert m2_with_data["phase_state_history"] == [], m2_with_data
    assert m2_with_data["work_unit_state"] is None, m2_with_data

    # And the same call with a session_uuid that has NO durable data at
    # all needs no Package A import in the first place, and also never
    # raises.
    m2_empty = state_store.read_m2_state(
        {"session_uuid": str(uuid.uuid4())}, work_id=str(uuid.uuid4()))
    assert m2_empty["phase_state"] is None
    assert m2_empty["phase_state_history"] == []

    print("ISOLATED_SNAPSHOT_OK")
    """)


class IsolatedSnapshotCompatibilityTest(unittest.TestCase):
    def test_import_and_run_without_package_a_siblings(self):
        isolated_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(isolated_dir, ignore_errors=True))
        for name in ("cowork_state.py", "cowork_policy.py"):
            shutil.copy(os.path.join(_HERE, name),
                       os.path.join(isolated_dir, name))
        for name in ("cowork_control_plane.py", "cowork_workunit.py"):
            self.assertFalse(
                os.path.exists(os.path.join(isolated_dir, name)),
                "test setup bug: %s must be absent from the isolated "
                "snapshot" % name)

        script = _ISOLATED_SNAPSHOT_SCRIPT % {"isolated_dir": isolated_dir}
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=isolated_dir, capture_output=True, text=True, timeout=60)
        self.assertEqual(
            result.returncode, 0,
            "isolated-snapshot subprocess failed:\nstdout:\n%s\nstderr:\n%s"
            % (result.stdout, result.stderr))
        self.assertIn("ISOLATED_SNAPSHOT_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
