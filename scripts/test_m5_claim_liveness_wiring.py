#!/usr/bin/env python3
"""Production liveness wiring for checkpoint claims.

`reconstruct_checkpoint_state` branches solely on the claim's `state` field,
so on its own it reports `checkpoint_state="claimed"` for a checkpoint whose
claimant crashed past its own persisted lease, byte-for-byte identically to a
checkpoint still being worked. The production wake path must therefore reach
`classify_checkpoint_claim_liveness` through the liveness-aware surface.

What is proven here, from DURABLE ARTIFACTS ALONE (no terminal output, no
live process handle, no in-memory state):

  - `reconstruct_checkpoint_state_with_liveness` joins the per-checkpoint
    reconstruction to the existing classifier as
    ONE additive `claim_liveness` field, and `reconstruct_all_checkpoints`
    is wired through it;
  - a BOUNDED, EXPIRED, crash-stranded claim with no terminal output
    surfaces `process_crash` while its lifecycle `state` still truthfully
    reads `claimed`; a LIVE bounded claim stays `owned_verification`; an
    expired claim with durable `timed_out` corroboration surfaces
    `hung_descendant`;
  - the documented explicitly-unbounded conservative behaviour is
    UNCHANGED: no elapsed time, however absurd, ever indicts it;
  - the four-value checkpoint `state` vocabulary is preserved and the two
    vocabularies are DISJOINT, so no downstream reader can confuse a
    stranded claim with an ordinary `claimed` one, nor a liveness value with
    a lifecycle state;
  - `cowork.checkpoint_wake_block` -- the production wake path -- really
    does call the liveness-aware surface and no longer calls the bare one,
    exposes the verdict additively as `block.checkpoint_claim_liveness`, and
    renders the same handoff prose as the fact set without liveness;
  - classification is derived only from durable artifacts and deterministic
    time input: it is re-derived identically in a fresh process with no
    terminal at all, and arbitrary terminal-shaped noise in the durable
    artifacts never moves it.

Run standalone:

    python3 scripts/test_m5_claim_liveness_wiring.py -v
"""

import ast
import datetime
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402

# The four-value checkpoint lifecycle vocabulary. This suite
# asserts it is preserved EXACTLY -- liveness is additive, never a fifth
# member and never an overload of an existing one.
CHECKPOINT_STATES = frozenset({"pending", "claimed", "terminal", "unknown"})

# The classifier's own closed vocabulary (literals matching
# `cowork_activity.ACTIVITY_CLASSES`, which the production module
# deliberately never imports).
LIVENESS_CLASSES = frozenset({"owned_verification", "process_crash",
                              "hung_descendant", "no_evidence_silence"})

# The additive field/attribute names the liveness verdict travels under --
# deliberately NOT `state`/`checkpoint_state`.
LIVENESS_FIELD = "claim_liveness"
LIVENESS_ATTR = "checkpoint_claim_liveness"

GRACE_S = verification.CHECKPOINT_CLAIM_LEASE_GRACE_S

# Re-derives one reconstruction in a FRESH process with no terminal at all:
# stdin/stdout/stderr are /dev/null (never a PTY), and the verdict comes back
# through a file, not through any stream a terminal could carry.
_HEADLESS_SRC = r'''
import json
import sys

sys.path.insert(0, sys.argv[1])
import cowork_verification as verification

_scripts, session_uuid, checkpoint_id, now, out_path = sys.argv[1:6]
built = verification.reconstruct_checkpoint_state_with_liveness(
    session_uuid, checkpoint_id, now=(now or None))
with open(out_path, "w") as fh:
    json.dump({"state": built["state"],
               "claim_liveness": built["claim_liveness"]}, fh)
'''


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _top_level_named(source):
    """`{top-level symbol name: exact source text}` for one module."""
    lines = source.splitlines(keepends=True)
    named = {}
    for node in ast.parse(source).body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and len(
                node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name is not None:
            named[name] = "".join(lines[node.lineno - 1:node.end_lineno])
    return named


def _referenced_names(func):
    """Every bare/attribute name mentioned in one function's own source."""
    tree = ast.parse(inspect.getsource(func).lstrip())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


class _ArtifactFixture(unittest.TestCase):
    """An isolated `COWORK_SESSIONS_ROOT` plus helpers that author checkpoint
    artifacts directly on disk. Every fixture below is DURABLE STATE ONLY --
    no claimant process is ever alive while a verdict is taken, which is the
    whole point: a crash-stranded claim must be classifiable on a machine
    where the claimant never ran."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.workdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.workdir, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self.root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]

    # -- helpers ---------------------------------------------------------- #

    def _request(self, checkpoint_id, timeout_s=30.0, work_id="W-live-1"):
        return verification.build_and_persist_checkpoint_request(
            self.session_uuid, checkpoint_id, work_id, "building",
            "candidate-digest-0", ["/bin/sh", "-c", "sleep 600"],
            self.workdir, verification.MUTATION_CLASS_READ_ONLY,
            timeout_s=timeout_s)

    def _claim(self, checkpoint_id, identity="host:4242"):
        claimed, record = verification.claim_checkpoint(
            self.session_uuid, checkpoint_id, identity)
        self.assertTrue(claimed, "fixture claim was refused")
        return record

    def _claim_path(self, checkpoint_id):
        return state_store.checkpoint_claim_path_for(
            self.session_uuid, checkpoint_id)

    def _age_claim(self, checkpoint_id, seconds_ago):
        """Rewrite the DURABLE claim so its lease began (and lapsed)
        `seconds_ago` in the past -- the on-disk residue a claimant that
        crashed that long ago would have left. Nothing else is touched."""
        path = self._claim_path(checkpoint_id)
        claim = state_store.read_json_tolerant(path)
        began = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
        claim["claimed_at"] = _iso(began)
        timeout_s = claim.get("lease_timeout_s")
        claim["lease_deadline_at"] = None if timeout_s is None else _iso(
            began + datetime.timedelta(seconds=timeout_s + GRACE_S))
        self.assertTrue(state_store.write_json_atomic_durable(path, claim))
        return claim

    def _write_result(self, checkpoint_id, **fields):
        path = state_store.checkpoint_result_path_for(
            self.session_uuid, checkpoint_id)
        self.assertTrue(state_store.write_json_atomic_durable(path, fields))

    def _make_terminal(self, checkpoint_id):
        path = self._claim_path(checkpoint_id)
        claim = state_store.read_json_tolerant(path)
        claim["state"] = verification.CHECKPOINT_CLAIM_TERMINAL
        self.assertTrue(state_store.write_json_atomic_durable(path, claim))

    def _bind_pointer(self, checkpoint_id, work_id="W-live-1"):
        self.assertTrue(state_store.write_json_atomic_durable(
            state_store.current_checkpoint_pointer_path_for(
                self.session_uuid, work_id),
            {"checkpoint_id": checkpoint_id, "work_id": work_id}))

    def _new_checkpoint(self):
        return "cp-" + uuid.uuid4().hex[:8]

    def _stranded(self, timeout_s=30.0, seconds_ago=3600.0, work_id="W-live-1"):
        """A BOUNDED claim whose lease lapsed long ago, with NO result and NO
        receipt behind it: the exact crash-strand shape."""
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id, timeout_s=timeout_s, work_id=work_id)
        self._claim(checkpoint_id)
        self._age_claim(checkpoint_id, seconds_ago)
        return checkpoint_id

    def _live(self, timeout_s=600.0, work_id="W-live-1"):
        """A BOUNDED claim comfortably inside its own lease."""
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id, timeout_s=timeout_s, work_id=work_id)
        self._claim(checkpoint_id)
        return checkpoint_id


# =========================================================================== #
# The additive accessor itself.                                               #
# =========================================================================== #


class LivenessAwareReconstructionTests(_ArtifactFixture):

    def test_the_accessor_exists_and_is_purely_additive(self):
        checkpoint_id = self._live()
        bare = verification.reconstruct_checkpoint_state(
            self.session_uuid, checkpoint_id)
        joined = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(set(joined) - set(bare), {LIVENESS_FIELD})
        for key, value in bare.items():
            self.assertEqual(joined[key], value,
                             "the accessor must not alter %r" % (key,))

    def test_a_bounded_expired_strand_surfaces_process_crash(self):
        checkpoint_id = self._stranded()
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        # Both truths at once: the lifecycle really is still `claimed` (no
        # terminal marker was ever published), and the claimant is gone.
        self.assertEqual(built["state"], "claimed")
        self.assertEqual(built[LIVENESS_FIELD], "process_crash")
        self.assertIsNone(built["result"])
        self.assertIsNone(built["receipt"])

    def test_a_live_bounded_claim_remains_owned_and_in_progress(self):
        checkpoint_id = self._live()
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built["state"], "claimed")
        self.assertEqual(built[LIVENESS_FIELD], "owned_verification")

    def test_an_expired_claim_with_durable_timeout_is_hung_descendant(self):
        checkpoint_id = self._stranded()
        self._write_result(checkpoint_id, timed_out=True, exit_code=None)
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built["state"], "claimed")
        self.assertEqual(built[LIVENESS_FIELD], "hung_descendant")

    def test_an_expired_claim_whose_result_did_not_time_out_is_a_crash(self):
        # Corroboration must be the durable `timed_out` fact specifically --
        # any old result must not upgrade a strand to `hung_descendant`.
        checkpoint_id = self._stranded()
        self._write_result(checkpoint_id, timed_out=False, exit_code=0)
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built[LIVENESS_FIELD], "process_crash")

    def test_the_explicitly_unbounded_conservative_behaviour_is_unchanged(self):
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id, timeout_s=None)
        record = self._claim(checkpoint_id)
        self.assertIsNone(record["lease_timeout_s"])
        self.assertIsNone(record["lease_deadline_at"])
        far_future = _iso(datetime.datetime.now(datetime.timezone.utc)
                          + datetime.timedelta(days=365 * 100))
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id, now=far_future)
        self.assertEqual(built["state"], "claimed")
        self.assertEqual(built[LIVENESS_FIELD], "owned_verification")

    def test_an_unbounded_claim_with_a_timed_out_result_stays_conservative(
            self):
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id, timeout_s=None)
        self._claim(checkpoint_id)
        self._write_result(checkpoint_id, timed_out=True)
        far_future = _iso(datetime.datetime.now(datetime.timezone.utc)
                          + datetime.timedelta(days=365 * 100))
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id, now=far_future)
        self.assertEqual(built[LIVENESS_FIELD], "owned_verification")

    def test_a_terminal_checkpoint_is_owned_verification(self):
        checkpoint_id = self._stranded()
        self._make_terminal(checkpoint_id)
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built["state"], "terminal")
        self.assertEqual(built[LIVENESS_FIELD], "owned_verification")

    def test_a_checkpoint_nothing_was_persisted_for_is_silence(self):
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, "cp-never-existed")
        self.assertEqual(built["state"], "unknown")
        self.assertEqual(built[LIVENESS_FIELD], "no_evidence_silence")

    def test_a_pending_checkpoint_has_no_claimant_to_indict(self):
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id)
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built["state"], "pending")
        self.assertEqual(built[LIVENESS_FIELD], "no_evidence_silence")

    def test_the_lease_grace_still_protects_a_claimant_past_its_timeout(self):
        # A claimant PAST its raw command timeout but still inside its total
        # lease is not a crash: expiry only bites once the grace is spent.
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id, timeout_s=30.0)
        self._claim(checkpoint_id)
        self._age_claim(checkpoint_id, 30.0 + (GRACE_S / 2.0))
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built[LIVENESS_FIELD], "owned_verification")

    def test_now_accepts_a_datetime_as_well_as_rfc3339(self):
        checkpoint_id = self._live(timeout_s=600.0)
        later = datetime.datetime.now(
            datetime.timezone.utc) + datetime.timedelta(
                seconds=600.0 + GRACE_S + 60.0)
        self.assertEqual(
            verification.reconstruct_checkpoint_state_with_liveness(
                self.session_uuid, checkpoint_id, now=later)[LIVENESS_FIELD],
            "process_crash")
        self.assertEqual(
            verification.reconstruct_checkpoint_state_with_liveness(
                self.session_uuid, checkpoint_id,
                now=_iso(later))[LIVENESS_FIELD],
            "process_crash")


# =========================================================================== #
# Vocabulary preservation: additive, disjoint, unconfusable.                   #
# =========================================================================== #


class VocabularyPreservationTests(_ArtifactFixture):

    def test_the_two_vocabularies_share_no_member(self):
        self.assertFalse(CHECKPOINT_STATES & LIVENESS_CLASSES)

    def test_state_stays_within_the_four_values_for_every_shape(self):
        shapes = [self._stranded(), self._live(), self._new_checkpoint()]
        pending = self._new_checkpoint()
        self._request(pending)
        shapes.append(pending)
        terminal = self._stranded()
        self._make_terminal(terminal)
        shapes.append(terminal)
        for checkpoint_id in shapes:
            built = verification.reconstruct_checkpoint_state_with_liveness(
                self.session_uuid, checkpoint_id)
            self.assertIn(built["state"], CHECKPOINT_STATES)
            self.assertIn(built[LIVENESS_FIELD], LIVENESS_CLASSES)
            self.assertNotIn(built[LIVENESS_FIELD], CHECKPOINT_STATES)

    def test_the_liveness_field_is_not_named_like_a_lifecycle_state(self):
        checkpoint_id = self._stranded()
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertIn(LIVENESS_FIELD, built)
        self.assertNotEqual(LIVENESS_FIELD, "state")
        # A reader that only knows `state` is unaffected; a reader that wants
        # the strand must ask for it by its own explicit name.
        self.assertNotEqual(built["state"], built[LIVENESS_FIELD])


# =========================================================================== #
# `reconstruct_all_checkpoints` is wired through the accessor.                #
# =========================================================================== #


class ReconstructAllCheckpointsWiringTests(_ArtifactFixture):

    def test_every_entry_carries_the_liveness_field(self):
        stranded = self._stranded(work_id="W-a")
        live = self._live(work_id="W-b")
        built = verification.reconstruct_all_checkpoints(self.session_uuid)
        self.assertEqual(set(built), {stranded, live})
        for entry in built.values():
            self.assertIn(LIVENESS_FIELD, entry)
            self.assertIn(entry[LIVENESS_FIELD], LIVENESS_CLASSES)
        self.assertEqual(built[stranded]["state"], "claimed")
        self.assertEqual(built[stranded][LIVENESS_FIELD], "process_crash")
        self.assertEqual(built[live][LIVENESS_FIELD], "owned_verification")

    def test_now_is_threaded_through_to_the_classifier(self):
        checkpoint_id = self._live(timeout_s=600.0)
        later = _iso(datetime.datetime.now(datetime.timezone.utc)
                     + datetime.timedelta(seconds=600.0 + GRACE_S + 60.0))
        self.assertEqual(
            verification.reconstruct_all_checkpoints(
                self.session_uuid)[checkpoint_id][LIVENESS_FIELD],
            "owned_verification")
        self.assertEqual(
            verification.reconstruct_all_checkpoints(
                self.session_uuid, now=later)[checkpoint_id][LIVENESS_FIELD],
            "process_crash")

    def test_the_original_per_id_payload_is_unchanged(self):
        checkpoint_id = self._stranded()
        entry = verification.reconstruct_all_checkpoints(
            self.session_uuid)[checkpoint_id]
        bare = verification.reconstruct_checkpoint_state(
            self.session_uuid, checkpoint_id)
        for key, value in bare.items():
            self.assertEqual(entry[key], value)

    def test_a_session_with_no_checkpoints_still_yields_an_empty_map(self):
        self.assertEqual(
            verification.reconstruct_all_checkpoints(self.session_uuid), {})


# =========================================================================== #
# The production wake path (`cowork.checkpoint_wake_block`).                   #
# =========================================================================== #


class ProductionWakePathTests(_ArtifactFixture):

    def test_the_wake_path_calls_the_liveness_aware_surface(self):
        names = _referenced_names(cowork.checkpoint_wake_block)
        self.assertIn("reconstruct_checkpoint_state_with_liveness", names)
        self.assertNotIn("reconstruct_checkpoint_state", names,
                         "the wake path must not still call the bare, "
                         "liveness-blind reconstruction surface")

    def test_the_classifier_now_has_a_real_production_call_site(self):
        # The accessor is the call site, and it is reached from production
        # (cowork.py), not only from tests.
        accessor = _top_level_named(inspect.getsource(verification))[
            "reconstruct_checkpoint_state_with_liveness"]
        self.assertIn("classify_checkpoint_claim_liveness", accessor)
        production = inspect.getsource(cowork)
        self.assertIn("reconstruct_checkpoint_state_with_liveness", production)

    def test_a_stranded_claim_wakes_with_process_crash_not_bare_claimed(self):
        checkpoint_id = self._stranded(work_id="W-wake")
        self._bind_pointer(checkpoint_id, work_id="W-wake")
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-wake", "builder")
        self.assertIsNotNone(block)
        self.assertEqual(getattr(block, LIVENESS_ATTR), "process_crash")
        # The lifecycle fact is untouched and still truthful.
        self.assertIn("claimed", str(block).lower())

    def test_a_live_claim_wakes_as_owned_verification(self):
        checkpoint_id = self._live(work_id="W-wake")
        self._bind_pointer(checkpoint_id, work_id="W-wake")
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-wake", "builder")
        self.assertEqual(getattr(block, LIVENESS_ATTR), "owned_verification")

    def test_a_hung_descendant_strand_wakes_with_its_own_class(self):
        checkpoint_id = self._stranded(work_id="W-wake")
        self._write_result(checkpoint_id, timed_out=True)
        self._bind_pointer(checkpoint_id, work_id="W-wake")
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-wake", "builder")
        self.assertEqual(getattr(block, LIVENESS_ATTR), "hung_descendant")

    def test_the_two_stranded_shapes_are_distinguishable_at_the_wake_path(
            self):
        crashed = self._stranded(work_id="W-crash")
        hung = self._stranded(work_id="W-hung")
        self._write_result(hung, timed_out=True)
        self._bind_pointer(crashed, work_id="W-crash")
        self._bind_pointer(hung, work_id="W-hung")
        crashed_block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-crash", "builder")
        hung_block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-hung", "builder")
        # Identical lifecycle prose (the per-checkpoint descriptor lines
        # naturally differ by id/digest), different durable liveness
        # verdicts -- exactly the distinction that did not exist before.
        lede = lambda block: str(block).split(
            "Read its current status from disk:")[0]
        self.assertEqual(lede(crashed_block), lede(hung_block))
        self.assertIn("claimed", lede(crashed_block).lower())
        self.assertEqual(getattr(crashed_block, LIVENESS_ATTR),
                         "process_crash")
        self.assertEqual(getattr(hung_block, LIVENESS_ATTR),
                         "hung_descendant")

    def test_pending_and_terminal_wakes_still_carry_the_field(self):
        pending = self._new_checkpoint()
        self._request(pending, work_id="W-pending")
        self._bind_pointer(pending, work_id="W-pending")
        pending_block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-pending", "builder")
        self.assertIn("pending", str(pending_block).lower())
        self.assertEqual(getattr(pending_block, LIVENESS_ATTR),
                         "no_evidence_silence")

        terminal = self._stranded(work_id="W-terminal")
        self._make_terminal(terminal)
        self._bind_pointer(terminal, work_id="W-terminal")
        terminal_block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-terminal", "builder")
        self.assertIn("terminal", str(terminal_block).lower())
        self.assertEqual(getattr(terminal_block, LIVENESS_ATTR),
                         "owned_verification")

    def test_the_rendered_prose_is_unchanged_by_the_wiring(self):
        checkpoint_id = self._stranded(work_id="W-wake")
        self._bind_pointer(checkpoint_id, work_id="W-wake")
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-wake", "builder")
        expected = handoff.render_handoff(
            "cowork->role:checkpoint_wake",
            artifacts=[{"label": "checkpoint status",
                        "path": self._claim_path(checkpoint_id),
                        "kind": "json", "source": "checkpoint_status"}],
            facts={"role": "builder", "checkpoint_id": checkpoint_id,
                   "checkpoint_phase": "building",
                   "checkpoint_state": "claimed"},
            ctx={})
        self.assertEqual(str(block), str(expected))

    def test_liveness_is_not_smuggled_into_the_handoff_fact_vocabulary(self):
        # The wake edge's fact vocabulary is CLOSED and stays closed: the
        # verdict must not have been injected as an undeclared fact, nor
        # overloaded onto `checkpoint_state`.
        declared = set(handoff.EDGES["cowork->role:checkpoint_wake"]["facts"])
        self.assertNotIn(LIVENESS_ATTR, declared)
        checkpoint_id = self._stranded(work_id="W-wake")
        self._bind_pointer(checkpoint_id, work_id="W-wake")
        block = cowork.checkpoint_wake_block(
            self.session_uuid, "W-wake", "builder")
        for liveness_class in LIVENESS_CLASSES:
            self.assertNotIn(liveness_class, str(block))

    def test_none_is_still_returned_when_nothing_is_bound(self):
        self.assertIsNone(cowork.checkpoint_wake_block(
            self.session_uuid, "W-unbound", "builder"))

    def test_an_unknown_checkpoint_still_wakes_nothing(self):
        self._bind_pointer("cp-never-existed", work_id="W-ghost")
        self.assertIsNone(cowork.checkpoint_wake_block(
            self.session_uuid, "W-ghost", "builder"))


# =========================================================================== #
# Durable-artifact-only derivation.                                            #
# =========================================================================== #


class DurableOnlyDerivationTests(_ArtifactFixture):

    def test_the_verdict_is_reproduced_in_a_fresh_terminal_free_process(self):
        checkpoint_id = self._stranded()
        out_path = os.path.join(self.workdir, "verdict.json")
        script = os.path.join(self.workdir, "headless.py")
        with open(script, "w") as fh:
            fh.write(_HEADLESS_SRC)
        env = dict(os.environ)
        env["COWORK_SESSIONS_ROOT"] = self.root
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with open(os.devnull, "rb") as devnull_in, \
                open(os.devnull, "wb") as devnull_out:
            proc = subprocess.run(
                [sys.executable, script, _HERE, self.session_uuid,
                 checkpoint_id, "", out_path],
                stdin=devnull_in, stdout=devnull_out, stderr=devnull_out,
                env=env)
        self.assertEqual(proc.returncode, 0)
        with open(out_path) as fh:
            verdict = json.load(fh)
        self.assertEqual(verdict, {"state": "claimed",
                                   "claim_liveness": "process_crash"})

    def test_terminal_shaped_noise_in_the_artifacts_never_moves_the_verdict(
            self):
        checkpoint_id = self._stranded()
        before = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)[LIVENESS_FIELD]
        # Digests/prose that look like a healthy run must not rescue a
        # strand: only the durable temporal basis and `timed_out` matter.
        self._write_result(
            checkpoint_id, exit_code=0, timed_out=False,
            stdout_digest="a" * 64,
            note="All tests passed. Executor is alive and well.")
        after = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)[LIVENESS_FIELD]
        self.assertEqual(before, "process_crash")
        self.assertEqual(after, "process_crash")

    def test_the_verdict_does_not_depend_on_this_process_having_claimed(self):
        # Hand-author the whole claim: no `claim_checkpoint` call, no
        # in-memory residue, nothing this process ever owned.
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id, timeout_s=30.0)
        began = datetime.datetime.now(
            datetime.timezone.utc) - datetime.timedelta(seconds=7200)
        self.assertTrue(state_store.write_json_atomic_durable(
            self._claim_path(checkpoint_id),
            {"checkpoint_id": checkpoint_id,
             "executor_identity": "some-other-host:1",
             "claimed_at": _iso(began),
             "state": verification.CHECKPOINT_CLAIM_CLAIMED,
             "lease_timeout_s": 30.0,
             "lease_grace_s": GRACE_S,
             "lease_deadline_at": _iso(began + datetime.timedelta(
                 seconds=30.0 + GRACE_S))}))
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        self.assertEqual(built["state"], "claimed")
        self.assertEqual(built[LIVENESS_FIELD], "process_crash")

    def test_the_accessor_never_writes_or_deletes_anything(self):
        checkpoint_id = self._stranded()
        root_before = _tree_snapshot(self.root)
        for _ in range(3):
            verification.reconstruct_checkpoint_state_with_liveness(
                self.session_uuid, checkpoint_id)
            verification.reconstruct_all_checkpoints(self.session_uuid)
        self.assertEqual(_tree_snapshot(self.root), root_before)

    def test_the_accessor_never_raises_on_a_corrupt_claim(self):
        checkpoint_id = self._new_checkpoint()
        self._request(checkpoint_id)
        with open(self._claim_path(checkpoint_id), "wb") as fh:
            fh.write(b'{"checkpoint_id": "trunc')
        built = verification.reconstruct_checkpoint_state_with_liveness(
            self.session_uuid, checkpoint_id)
        # Durable evidence of a claimant exists, so this is never collapsed
        # into silence -- and `state` falls back to the bare reconstruction's answer.
        self.assertEqual(built[LIVENESS_FIELD], "process_crash")
        self.assertIn(built["state"], CHECKPOINT_STATES)


def _tree_snapshot(root):
    """`{relative path: (size, sha256)}` for every file under `root`."""
    snapshot = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                blob = fh.read()
            snapshot[os.path.relpath(full, root)] = (
                len(blob), hashlib.sha256(blob).hexdigest())
    return snapshot


if __name__ == "__main__":
    unittest.main(verbosity=2)
