#!/usr/bin/env python3
"""Focused tests for M2 Package D: the durable recovery circuit breaker.

Implementer evidence only — non-authoritative. The controller runs its own
focused breaker tests, repeated-identical/changed-cause fixtures, and
crash/restart durability gate separately.

Run standalone:

    python3 -m unittest scripts/test_cowork_recovery_breaker.py -v
"""

import multiprocessing
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_control_plane as control_plane  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_recovery_breaker as breaker  # noqa: E402

_CAUSE = dict(
    role="engineer",
    config_digest="a" * 64,
    provider="claude-sonnet-5",
    candidate="candidate-1",
    reason="turn_timeout",
)


def _attempt(ledger_path, cause=None, **overrides):
    fields = dict(cause or _CAUSE)
    fields.update(overrides)
    return breaker.attempt(ledger_path, **fields)


def _mp_attempt(ledger_path, result_path):
    """Module-level (picklable / fork-safe) worker target: one genuinely
    separate-process breaker attempt for the SAME cause, used to prove the
    trip decision cannot be raced past `TRIP_THRESHOLD` by two concurrent
    processes."""
    outcome = _attempt(ledger_path)
    with open(result_path, "w") as fh:
        fh.write("tripped" if outcome["tripped"] else "recorded")


class AttemptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.jsonl")

    def test_fingerprint_is_derived_only_from_control_plane_fingerprint(self):
        expected = control_plane.fingerprint(**_CAUSE)
        outcome = _attempt(self.ledger_path)
        self.assertEqual(outcome["fingerprint"], expected)

    def test_first_attempt_under_threshold_is_recorded_not_tripped(self):
        outcome = _attempt(self.ledger_path)
        self.assertFalse(outcome["tripped"])
        self.assertEqual(outcome["attempt_count"], 0)
        self.assertIsNotNone(outcome["record"])
        self.assertEqual(outcome["record"]["fingerprint"], outcome["fingerprint"])

    def test_repeated_identical_cause_trips_at_documented_threshold(self):
        outcomes = [_attempt(self.ledger_path) for _ in range(breaker.TRIP_THRESHOLD)]
        for i, outcome in enumerate(outcomes):
            self.assertFalse(outcome["tripped"], "attempt %d unexpectedly tripped" % i)
            self.assertEqual(outcome["attempt_count"], i)

        # attempt threshold+1 is refused BEFORE dispatch.
        refused = _attempt(self.ledger_path)
        self.assertTrue(refused["tripped"])
        self.assertIsNone(refused["record"])
        self.assertEqual(refused["attempt_count"], breaker.TRIP_THRESHOLD)

        # The refusal itself must not have been appended: durable history
        # still holds exactly TRIP_THRESHOLD records, and asking again gives
        # the identical refusal, not a growing count.
        history = breaker.history(self.ledger_path, **_CAUSE)
        self.assertEqual(len(history), breaker.TRIP_THRESHOLD)
        refused_again = _attempt(self.ledger_path)
        self.assertTrue(refused_again["tripped"])
        self.assertEqual(refused_again["attempt_count"], breaker.TRIP_THRESHOLD)

    def test_differing_role_starts_independent_history(self):
        for _ in range(breaker.TRIP_THRESHOLD):
            self.assertFalse(_attempt(self.ledger_path)["tripped"])
        self.assertTrue(_attempt(self.ledger_path)["tripped"])

        other = _attempt(self.ledger_path, role="reviewer")
        self.assertFalse(other["tripped"])
        self.assertEqual(other["attempt_count"], 0)
        self.assertNotEqual(other["fingerprint"], control_plane.fingerprint(**_CAUSE))

    def test_differing_config_digest_starts_independent_history(self):
        for _ in range(breaker.TRIP_THRESHOLD):
            self.assertFalse(_attempt(self.ledger_path)["tripped"])
        other = _attempt(self.ledger_path, config_digest="b" * 64)
        self.assertFalse(other["tripped"])
        self.assertEqual(other["attempt_count"], 0)

    def test_differing_provider_starts_independent_history(self):
        for _ in range(breaker.TRIP_THRESHOLD):
            self.assertFalse(_attempt(self.ledger_path)["tripped"])
        other = _attempt(self.ledger_path, provider="opencode")
        self.assertFalse(other["tripped"])
        self.assertEqual(other["attempt_count"], 0)

    def test_differing_candidate_starts_independent_history(self):
        for _ in range(breaker.TRIP_THRESHOLD):
            self.assertFalse(_attempt(self.ledger_path)["tripped"])
        other = _attempt(self.ledger_path, candidate="candidate-2")
        self.assertFalse(other["tripped"])
        self.assertEqual(other["attempt_count"], 0)

    def test_differing_reason_starts_independent_history(self):
        for _ in range(breaker.TRIP_THRESHOLD):
            self.assertFalse(_attempt(self.ledger_path)["tripped"])
        other = _attempt(self.ledger_path, reason="preflight_rejected")
        self.assertFalse(other["tripped"])
        self.assertEqual(other["attempt_count"], 0)

    def test_none_candidate_is_a_distinct_cause_from_a_string_candidate(self):
        for _ in range(breaker.TRIP_THRESHOLD):
            self.assertFalse(_attempt(self.ledger_path)["tripped"])
        other = _attempt(self.ledger_path, candidate=None)
        self.assertFalse(other["tripped"])
        self.assertEqual(other["attempt_count"], 0)

    def test_history_is_pure_read_and_never_appends(self):
        _attempt(self.ledger_path)
        _attempt(self.ledger_path)
        before = breaker.history(self.ledger_path, **_CAUSE)
        for _ in range(5):
            breaker.history(self.ledger_path, **_CAUSE)
        after = breaker.history(self.ledger_path, **_CAUSE)
        self.assertEqual(before, after)
        self.assertEqual(len(after), 2)

    def test_history_empty_for_a_cause_never_attempted(self):
        self.assertEqual(breaker.history(self.ledger_path, **_CAUSE), [])

    def test_malformed_ledger_fails_closed_as_tripped(self):
        with open(self.ledger_path, "w") as fh:
            fh.write("{not valid json\n")
        outcome = _attempt(self.ledger_path)
        self.assertTrue(outcome["tripped"])
        self.assertIsNone(outcome["record"])

    def test_invalid_cause_input_raises_not_a_parallel_fingerprint(self):
        with self.assertRaises(ValueError):
            _attempt(self.ledger_path, role="")


class DurabilityTest(unittest.TestCase):
    """Trip truth survives a crash/restart and is read from durable ledger
    storage rather than recomputed from anything held in process memory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.jsonl")

    def _run_in_fresh_interpreter(self, code):
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            import cowork_recovery_breaker as breaker
            %s
        """) % (_HERE, code)
        subprocess.run([sys.executable, "-c", script], check=True,
                       capture_output=True, text=True)

    def test_history_written_by_one_process_is_read_by_another(self):
        cause_kwargs = ", ".join("%s=%r" % (k, v) for k, v in _CAUSE.items())
        self._run_in_fresh_interpreter(
            "breaker.attempt(%r, %s)" % (self.ledger_path, cause_kwargs))
        self._run_in_fresh_interpreter(
            "breaker.attempt(%r, %s)" % (self.ledger_path, cause_kwargs))

        # A brand-new process (this test process never called `attempt`
        # itself) sees exactly two durable attempts -- nothing about the
        # trip count lived only in the writer processes' memory.
        history = breaker.history(self.ledger_path, **_CAUSE)
        self.assertEqual(len(history), 2)

        outcome = _attempt(self.ledger_path)
        self.assertFalse(outcome["tripped"])
        self.assertEqual(outcome["attempt_count"], 2)

    def test_trip_reached_across_process_restarts_is_still_honored(self):
        cause_kwargs = ", ".join("%s=%r" % (k, v) for k, v in _CAUSE.items())
        for _ in range(breaker.TRIP_THRESHOLD):
            self._run_in_fresh_interpreter(
                "breaker.attempt(%r, %s)" % (self.ledger_path, cause_kwargs))

        # A fourth, freshly-started process must independently observe the
        # trip -- it never shared memory with any prior writer.
        refused_script = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            import cowork_recovery_breaker as breaker
            outcome = breaker.attempt(%r, %s)
            assert outcome["tripped"] is True, outcome
            assert outcome["record"] is None, outcome
        """) % (_HERE, self.ledger_path, cause_kwargs)
        subprocess.run([sys.executable, "-c", refused_script], check=True,
                       capture_output=True, text=True)

        history = breaker.history(self.ledger_path, **_CAUSE)
        self.assertEqual(len(history), breaker.TRIP_THRESHOLD)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.jsonl")

    def test_concurrent_attempts_for_identical_cause_never_exceed_threshold(self):
        n_workers = 8
        result_paths = [
            os.path.join(self._tmp.name, "result-%d" % i)
            for i in range(n_workers)
        ]
        procs = [
            multiprocessing.Process(target=_mp_attempt,
                                    args=(self.ledger_path, result_path))
            for result_path in result_paths
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            self.assertEqual(p.exitcode, 0)

        recorded = 0
        for result_path in result_paths:
            with open(result_path) as fh:
                if fh.read().strip() == "recorded":
                    recorded += 1
        self.assertEqual(recorded, breaker.TRIP_THRESHOLD)

        history = breaker.history(self.ledger_path, **_CAUSE)
        self.assertEqual(len(history), breaker.TRIP_THRESHOLD)


class LedgerAdditivePrimitivesTest(unittest.TestCase):
    """Package D's only writable surface in cowork_ledger.py: additive
    append/read functions for breaker history, reusing the ledger's
    existing locking/atomic-batch primitives."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = os.path.join(self._tmp.name, "ledger.jsonl")

    def test_append_breaker_attempt_reuses_atomic_commit_and_lock(self):
        outcome = ledger.append_breaker_attempt(
            self.ledger_path, "fp-1", 3, {"role": "engineer"})
        self.assertFalse(outcome["tripped"])
        self.assertEqual(outcome["record"]["fingerprint"], "fp-1")
        raw = ledger.read_ledger(self.ledger_path)
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["kind"], "breaker_attempt")

    def test_append_breaker_attempt_missing_path_is_tripped_not_a_crash(self):
        outcome = ledger.append_breaker_attempt(None, "fp-1", 3)
        self.assertTrue(outcome["tripped"])
        self.assertIsNone(outcome["record"])

    def test_read_breaker_history_ignores_other_ledger_kinds(self):
        ledger.append_record(self.ledger_path, "finding", {"summary": "x"})
        ledger.append_breaker_attempt(self.ledger_path, "fp-2", 3)
        history = ledger.read_breaker_history(self.ledger_path, "fp-2")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["fingerprint"], "fp-2")

    def test_pre_existing_public_functions_still_present(self):
        # Non-authoritative spot check -- the controller runs the
        # exhaustive public-signature stability gate separately.
        for name in (
            "read_ledger", "next_id", "append_record", "withdraw",
            "current_state", "collapse", "active_attempts", "attempt_key",
            "reconcile_attempts", "owned_attempt_key",
            "mint_owned_attempts_batch", "mint_owned_attempt",
            "revise_owned_attempt", "materialize_attempts",
            "append_finding", "append_decision", "append_amendment",
            "append_escape", "validate_citations",
        ):
            self.assertTrue(hasattr(ledger, name), "missing %s" % name)


if __name__ == "__main__":
    unittest.main()
