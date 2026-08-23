#!/usr/bin/env python3
"""Focused tests for M2 Package C: the atomic controller policy/config
transition primitive (issue #13), proven in isolation against a fake caller.

Covers the two invariants the frozen plan names explicitly:

  * a rejected transition leaves both the persisted and active policy
    record byte-identical to their pre-attempt values;
  * zero dispatch is possible while a transition is pending (proven by
    holding the SAME per-session lock externally and observing a
    concurrent dispatch guard block until it is released).

Run standalone:

    python3 -m unittest scripts/test_cowork_policy_atomic.py -v
"""

import fcntl
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_policy as policy  # noqa: E402
import cowork_state as state_store  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _M2EnvMixin:
    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)
        self.addCleanup(policy.deactivate)

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root

    def _raw_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()


class DecideControllerPolicyTransitionTest(_M2EnvMixin, unittest.TestCase):
    def test_first_commit_activates_process_policy(self):
        session_id = _uuid()
        policy.deactivate()
        result = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(policy.active_allowed(), ("claude",))

    def test_committed_state_matches_durable_read(self):
        session_id = _uuid()
        result = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude", "codex"]})
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(
            state_store.read_controller_transition(session_id), result["state"])

    def test_stale_revision_rejected_bytes_and_active_unchanged(self):
        session_id = _uuid()
        first = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        self.assertEqual(first["outcome"], "committed")
        active_before = policy.active_allowed()
        path = state_store.controller_transition_path_for(session_id)
        bytes_before = self._raw_bytes(path)

        second = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["codex"]})
        self.assertEqual(second["outcome"], "rejected")
        self.assertEqual(second["reason"], "stale_revision")

        self.assertEqual(self._raw_bytes(path), bytes_before)
        self.assertEqual(
            state_store.read_controller_transition(session_id), first["state"])
        # Active in-process policy is untouched by a rejected attempt.
        self.assertEqual(policy.active_allowed(), active_before)

    def test_malformed_policy_shape_rejected_bytes_unchanged(self):
        session_id = _uuid()
        first = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        path = state_store.controller_transition_path_for(session_id)
        bytes_before = self._raw_bytes(path)

        result = policy.decide_controller_policy_transition(
            session_id, first["state"]["revision"], policy=["claude"])
        self.assertEqual(result["outcome"], "rejected")
        self.assertTrue(result["reason"].startswith("invalid:"))
        self.assertEqual(self._raw_bytes(path), bytes_before)

    def test_empty_allowed_list_rejected_bytes_unchanged(self):
        session_id = _uuid()
        first = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        path = state_store.controller_transition_path_for(session_id)
        bytes_before = self._raw_bytes(path)

        result = policy.decide_controller_policy_transition(
            session_id, first["state"]["revision"], policy={"allowed": []})
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(self._raw_bytes(path), bytes_before)

    def test_unknown_controller_name_rejected_bytes_unchanged(self):
        session_id = _uuid()
        path = state_store.controller_transition_path_for(session_id)
        self.assertFalse(os.path.exists(path))

        result = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["not-a-real-controller"]})
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(os.path.exists(path))

    def test_caller_validate_hook_chains_and_rejects(self):
        """Negative control: a conflicting policy transition (fresh
        team/config combined with --allow-controllers, per issue #13's
        evidence) is rejected atomically with zero partial state."""
        session_id = _uuid()

        def reject_conflicting_config(current, proposed_policy, proposed_config):
            if proposed_config is not None and proposed_policy is not None:
                raise ValueError("fresh config cannot combine with a policy change")

        result = policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]},
            config={"builder": {"controller": "claude"}},
            validate=reject_conflicting_config)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(
            state_store.read_controller_transition(session_id),
            {"policy": None, "config": None, "revision": 0})

    def test_preserve_default_commit_never_erases_active_allowlist(self):
        """B1 regression: a config-only (PRESERVE) transition -- the
        default when `policy` is omitted, and the shape a lone role/mapping
        switch would use -- must never collapse into 'unrestricted' and
        erase an in-force controller allowlist, even though nothing was
        ever explicitly committed to the durable transition store (so
        Package B's own PRESERVE carries forward a null durable policy)."""
        session_id = _uuid()
        policy.activate(("claude",))
        result = policy.decide_controller_policy_transition(
            session_id, 0, config={"builder": {"controller": "codex"}})
        self.assertEqual(result["outcome"], "committed")
        self.assertIsNone(result["state"]["policy"])
        self.assertEqual(policy.active_allowed(), ("claude",))
        with self.assertRaises(policy.DispatchBlocked):
            policy.guard("codex")

    def test_preserve_explicit_none_commit_never_erases_active_allowlist(self):
        """Same as above but with `policy=None` passed explicitly (the
        PRESERVE alias), matching B1's exact reproduction."""
        session_id = _uuid()
        policy.activate(("claude",))
        result = policy.decide_controller_policy_transition(
            session_id, 0, config={"builder": {"controller": "codex"}},
            policy=None)
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(policy.active_allowed(), ("claude",))

    def test_preserve_commit_never_clears_invalid_mode(self):
        """B1 regression: a PRESERVE-shaped commit must never clear the
        fail-closed `activate_invalid` state, which guard() must keep
        blocking EVERY controller under."""
        session_id = _uuid()
        policy.activate_invalid("{{garbage")
        result = policy.decide_controller_policy_transition(
            session_id, 0, config={"builder": {"controller": "codex"}})
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(policy.active_meta()["mode"], "invalid")
        with self.assertRaises(policy.DispatchBlocked):
            policy.guard("codex")

    def test_explicit_all_commit_activates_unrestricted(self):
        """Explicit unrestricted remains a real, distinct, provable event --
        never confused with PRESERVE."""
        session_id = _uuid()
        policy.activate(("claude",))
        result = policy.decide_controller_policy_transition(
            session_id, 0, policy=policy.ALL)
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["state"]["policy"], {"allowed": None})
        self.assertIsNone(policy.active_allowed())
        self.assertIsNone(policy.guard("codex"))

    def test_preserve_after_explicit_all_still_preserves_unrestricted(self):
        """A PRESERVE commit that follows an explicit ALL commit correctly
        carries the durable 'explicitly unrestricted' state forward (via
        Package B's own PRESERVE semantics) without this layer touching the
        active policy holder at all."""
        session_id = _uuid()
        first = policy.decide_controller_policy_transition(
            session_id, 0, policy=policy.ALL)
        policy.activate(("claude",))  # simulate a later, unrelated commit
        result = policy.decide_controller_policy_transition(
            session_id, first["state"]["revision"],
            config={"builder": {"controller": "codex"}})
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["state"]["policy"], {"allowed": None})
        # PRESERVE never calls activate(): the process's active policy is
        # whatever it was before this call, untouched by this commit.
        self.assertEqual(policy.active_allowed(), ("claude",))


class DispatchBlockedWhilePendingTest(_M2EnvMixin, unittest.TestCase):
    """Proves 'zero dispatch is possible while the transition is pending':
    a fake caller holds the SAME per-session transition lock externally
    (simulating an in-flight transition), and a concurrent dispatch guard
    must block until it is released, then observe the post-transition
    state -- never a torn, mid-transition read."""

    def test_dispatch_guard_blocks_until_lock_released(self):
        session_id = _uuid()
        lock_path = policy._transition_lock_path(session_id)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        holder = open(lock_path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

        observed = {}
        started = threading.Event()

        def attempt_dispatch():
            started.set()
            try:
                policy.guard_serialized_with_transition(session_id, "codex")
                observed["blocked"] = False
            except policy.DispatchBlocked:
                observed["blocked"] = True

        t = threading.Thread(target=attempt_dispatch)
        t.start()
        started.wait(timeout=2)
        # The dispatch guard must still be waiting on the lock: no
        # observation recorded yet.
        time.sleep(0.2)
        self.assertNotIn("blocked", observed)

        # Commit a restrictive transition while still holding the external
        # lock reference is impossible by construction (propose also needs
        # this lock) -- release it now to let both proceed in order.
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertIn("blocked", observed)

    def test_dispatch_guard_reflects_committed_transition_after_release(self):
        session_id = _uuid()
        policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        policy.deactivate()  # simulate a process that missed activation

        # Self-healing: guard_serialized_with_transition re-syncs from the
        # durable state before judging, even though `activate` was never
        # called in this process for this commit.
        with self.assertRaises(policy.DispatchBlocked):
            policy.guard_serialized_with_transition(session_id, "codex")
        self.assertIsNone(
            policy.guard_serialized_with_transition(session_id, "claude"))

    def test_guard_serialized_does_not_clear_invalid_on_preserve_commit(self):
        """B1 regression (second repro): a PRESERVE-shaped durable commit
        (revision >= 1 but policy still null) must not make
        `guard_serialized_with_transition` re-sync to 'unrestricted' and
        clobber an active `activate_invalid` fail-closed state."""
        session_id = _uuid()
        policy.decide_controller_policy_transition(
            session_id, 0, config={"builder": {"controller": "codex"}})
        self.assertEqual(
            state_store.read_controller_transition(session_id)["revision"], 1)
        policy.activate_invalid("{{garbage")
        with self.assertRaises(policy.DispatchBlocked) as ctx:
            policy.guard_serialized_with_transition(session_id, "codex")
        self.assertTrue(ctx.exception.invalid)
        self.assertEqual(policy.active_meta()["mode"], "invalid")

    def test_guard_serialized_resyncs_on_explicit_all_commit(self):
        """An explicit ALL commit IS a genuine resync signal: it correctly
        clears a stale in-memory allowlist for a process that missed the
        `activate` call."""
        session_id = _uuid()
        policy.decide_controller_policy_transition(
            session_id, 0, policy={"allowed": ["claude"]})
        policy.decide_controller_policy_transition(
            session_id, 1, policy=policy.ALL)
        policy.activate(("claude",))  # simulate a process that missed it
        self.assertIsNone(
            policy.guard_serialized_with_transition(session_id, "codex"))

    def test_propose_itself_serializes_against_a_held_lock(self):
        """The propose side (not just the dispatch-guard side) is also
        serialized against the same lock -- a transition attempt cannot
        observe or write during a window some other holder of the SAME
        lock file occupies."""
        session_id = _uuid()
        lock_path = policy._transition_lock_path(session_id)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        holder = open(lock_path, "a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

        committed = threading.Event()

        def attempt():
            policy.decide_controller_policy_transition(
                session_id, 0, policy={"allowed": ["claude"]})
            committed.set()

        t = threading.Thread(target=attempt)
        t.start()
        time.sleep(0.2)
        self.assertFalse(committed.is_set())

        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
        t.join(timeout=5)
        self.assertTrue(committed.is_set())
        self.assertEqual(
            state_store.read_controller_transition(session_id)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
