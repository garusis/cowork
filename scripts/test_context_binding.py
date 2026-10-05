#!/usr/bin/env python3
"""Focused permanent tests for the prior-green binding comparer
(`cowork_verified_binding`).

The positive cases run ONE real owned verification transaction in a throwaway
committed git repository (real snapshot, worker and result persistence, an
isolated COWORK_SESSIONS_ROOT), so the stored result and request shapes the
comparer reads are the real ones. The pointer is built from that stored result;
no orchestration writer is involved. Every other input is a neutral, synthetic
variation of those documents.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_binding
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_execution_profiles as profiles  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402
import cowork_verified_binding as binding  # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64

_WORKER_MODULES = ("cowork_verification.py", "cowork_state.py",
                   "cowork_policy.py", "cowork_ledger.py")


def _entry(label, depends_on=None, kind="baseline", command=None):
    entry = {"label": label, "command": command or ["python3", "-c", "pass"],
             "execution_mode": "isolated_snapshot", "kind": kind}
    if depends_on is not None:
        entry["depends_on"] = list(depends_on)
    return entry


class _Fixture:
    """One real green transaction and everything derived from it."""


_F = None
_CLEANUPS = []


def _git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _build_fixture():
    fixture = _Fixture()
    sessions_root = tempfile.mkdtemp()
    _CLEANUPS.append(lambda: shutil.rmtree(sessions_root, ignore_errors=True))
    prior = os.environ.get("COWORK_SESSIONS_ROOT")
    os.environ["COWORK_SESSIONS_ROOT"] = sessions_root

    def restore():
        if prior is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = prior
    _CLEANUPS.append(restore)

    repo = os.path.realpath(tempfile.mkdtemp())
    _CLEANUPS.append(lambda: shutil.rmtree(repo, ignore_errors=True))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, text in (("docs/a.md", "a\n"), ("src/core.py", "VALUE = 1\n")):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    scripts = os.path.join(repo, "scripts")
    os.makedirs(scripts)
    for name in _WORKER_MODULES:
        shutil.copyfile(os.path.join(_HERE, name), os.path.join(scripts, name))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    fixture.repo = repo
    fixture.session_uuid = "S-" + uuid.uuid4().hex[:8]
    fixture.raw = [_entry("doc a", ["docs/a.md"]),
                   _entry("unit", kind="final_suite")]
    # A reuse policy makes the stored request carry the profiled
    # (dependency-digest) fields a bound transaction is later compared with.
    fixture.result = verification.run_transaction(
        repo, fixture.session_uuid, fixture.raw,
        reuse_policy=profiles.ReusePolicy("standard", {}, {}))
    transaction_id = fixture.result["transaction_id"]
    fixture.stored_result = state_store.read_json_tolerant(
        state_store.verification_result_path_for(
            fixture.session_uuid, transaction_id))
    fixture.stored_request = state_store.read_json_tolerant(
        state_store.verification_request_path_for(
            fixture.session_uuid, transaction_id))
    schema, entries, _label = verification.normalize_inventory(fixture.raw)
    fixture.schema = schema
    fixture.entries = entries
    fixture.configuration = (fixture.stored_request or {}).get(
        "configuration")
    fixture.identity = binding.compute_inventory_identity(
        schema, entries, None, fixture.configuration)
    snapshot = (fixture.stored_result or {}).get("snapshot") or {}
    fixture.snapshot = (snapshot.get("manifest_digest"),
                        snapshot.get("index_digest"))
    fixture.pointer = {
        "transaction_id": transaction_id,
        "manifest_digest": fixture.snapshot[0],
        "index_digest": fixture.snapshot[1],
        "disposition": "pending_review",
        "inventory_identity": fixture.identity,
    }
    return fixture


def setUpModule():
    global _F
    try:
        _F = _build_fixture()
    except BaseException:
        tearDownModule()
        raise


def tearDownModule():
    global _F
    _F = None
    while _CLEANUPS:
        _CLEANUPS.pop()()


def _kwargs(**over):
    kwargs = dict(
        pointer=copy.deepcopy(_F.pointer),
        stored_result=copy.deepcopy(_F.stored_result),
        stored_request=copy.deepcopy(_F.stored_request),
        live_identity=_F.snapshot,
        current_inventory_identity=_F.identity,
        reuse_mode="dependency_digest",
        session_uuid=_F.session_uuid,
        disposition=None,
        open_finding_classes=())
    kwargs.update(over)
    return kwargs


def _bind(**over):
    return binding.bind_prior_green(**_kwargs(**over))


def _result_with(**over):
    result = copy.deepcopy(_F.stored_result)
    result.update(over)
    return result


def _pointer_with(**over):
    pointer = copy.deepcopy(_F.pointer)
    pointer.update(over)
    return pointer


class PriorGreenBindingTests(unittest.TestCase):
    def test_the_first_transaction_is_a_real_green_terminal_result(self):
        self.assertEqual(_F.stored_result["verdict"],
                         verification.VERDICT_GREEN)
        self.assertEqual(_F.stored_result["final_suite_binding"], "ran_once")
        self.assertEqual(len(_F.stored_result["attempts"]), 2)
        self.assertEqual(_F.stored_request["session_uuid"], _F.session_uuid)
        self.assertEqual(_F.stored_request["evidence_reuse"], [])

    def test_the_live_candidate_matches_after_an_artifact_only_change(self):
        outside = os.path.join(os.path.dirname(_F.repo),
                               "outside-" + uuid.uuid4().hex[:8] + ".txt")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        with open(outside, "w") as fh:
            fh.write("session artifact\n")
        self.assertEqual(verification.current_candidate_identity(_F.repo),
                         _F.snapshot)

    def test_the_exact_prior_green_transaction_binds_without_a_worker(self):
        before = copy.deepcopy(_F.stored_result)
        with mock.patch.object(
                verification, "spawn_worker",
                side_effect=AssertionError("a worker must not start")) as spy, \
                mock.patch.object(
                    verification, "run_transaction",
                    side_effect=AssertionError("no new transaction")) as run:
            bound, refusal = _bind()
        spy.assert_not_called()
        run.assert_not_called()
        self.assertIsNone(refusal)
        self.assertEqual(bound["transaction_id"],
                         _F.stored_result["transaction_id"])
        self.assertEqual(bound["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(bound["attempts"], before["attempts"])
        self.assertIs(bound["bound_reuse"], True)
        self.assertEqual(bound["bound_prior_transaction_id"],
                         _F.stored_result["transaction_id"])
        self.assertIs(bound["reused_lock_result"], False)

    def test_a_prior_superseded_by_a_finding_still_binds(self):
        bound, refusal = _bind(
            pointer=_pointer_with(disposition="superseded_by_finding"))
        self.assertIsNone(refusal)
        self.assertEqual(bound["transaction_id"],
                         _F.stored_result["transaction_id"])

    def test_the_explicit_disposition_input_wins_over_the_pointer(self):
        bound, refusal = _bind(
            pointer=_pointer_with(disposition="rejected"),
            disposition="pending_review")
        self.assertIsNone(refusal)
        self.assertIsNotNone(bound)
        bound, refusal = _bind(disposition="accepted")
        self.assertEqual(refusal, "disposition_not_bindable")

    def test_every_bindable_final_suite_binding_binds(self):
        for value in binding.BINDABLE_FINAL_BINDINGS:
            with self.subTest(final_suite_binding=value):
                bound, refusal = _bind(
                    stored_result=_result_with(final_suite_binding=value))
                self.assertIsNone(refusal)
                self.assertEqual(bound["final_suite_binding"], value)

    def test_no_open_finding_tokens_still_binds(self):
        for tokens in ((), [], set(), frozenset()):
            with self.subTest(tokens=repr(tokens)):
                bound, refusal = _bind(open_finding_classes=tokens)
                self.assertIsNone(refusal)
                self.assertIsNotNone(bound)

    def test_the_bound_result_is_a_copy_of_the_stored_one(self):
        stored = copy.deepcopy(_F.stored_result)
        bound, _refusal = _bind(stored_result=stored)
        bound["attempts"].append({"label": "extra"})
        bound["verdict"] = "red"
        self.assertEqual(stored, _F.stored_result)
        self.assertNotIn("bound_reuse", stored)
        self.assertNotIn("bound_reuse", _F.stored_result)


class FailClosedBindingTests(unittest.TestCase):
    def assertRefused(self, code, **over):
        bound, refusal = _bind(**over)
        self.assertIsNone(bound)
        self.assertEqual(refusal, code)
        self.assertIn(refusal, binding.REFUSAL_CODES)

    def test_no_pointer(self):
        for pointer in (None, {}, [], "pointer"):
            with self.subTest(pointer=repr(pointer)):
                self.assertRefused("no_pointer", pointer=pointer)

    def test_a_legacy_pointer_without_an_identity(self):
        for value in (None, "short", 5, SHA_A.upper()):
            with self.subTest(identity=repr(value)):
                pointer = _pointer_with(inventory_identity=value)
                self.assertRefused("legacy_pointer_no_identity",
                                   pointer=pointer)
        pointer = _pointer_with()
        del pointer["inventory_identity"]
        self.assertRefused("legacy_pointer_no_identity", pointer=pointer)

    def test_a_reuse_mode_that_may_not_bind(self):
        for mode in ("none", None, "assurance", "", 3):
            with self.subTest(mode=repr(mode)):
                self.assertRefused("reuse_mode_not_allowed", reuse_mode=mode)

    def test_an_unreadable_result(self):
        for result in (None, [], "result", {}):
            with self.subTest(result=repr(result)):
                self.assertRefused("result_unreadable", stored_result=result)
        self.assertRefused("result_unreadable",
                           stored_result=_result_with(transaction_id="other"))
        self.assertRefused("result_unreadable",
                           pointer=_pointer_with(transaction_id=None))

    def test_a_result_file_truncated_on_disk(self):
        path = state_store.verification_result_path_for(
            _F.session_uuid, _F.pointer["transaction_id"])
        with open(path, "rb") as fh:
            raw = fh.read()
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        torn = os.path.join(root, "result.json")
        with open(torn, "wb") as fh:
            fh.write(raw[:len(raw) // 2])
        read = state_store.read_json_tolerant(torn)
        self.assertRefused("result_unreadable", stored_result=read)

    def test_a_request_that_disagrees_with_the_result(self):
        request = copy.deepcopy(_F.stored_request)
        for name, bad in (
                ("none", None),
                ("list", []),
                ("other transaction",
                 dict(request, transaction_id="other")),
                ("other manifest",
                 dict(request, snapshot={
                     "manifest_digest": SHA_A,
                     "index_digest": _F.snapshot[1]})),
                ("other index",
                 dict(request, snapshot={
                     "manifest_digest": _F.snapshot[0],
                     "index_digest": SHA_B})),
                ("no snapshot", {k: v for k, v in request.items()
                                 if k != "snapshot"})):
            with self.subTest(case=name):
                self.assertRefused("request_mismatch", stored_request=bad)
        result = _result_with()
        del result["snapshot"]
        self.assertRefused("request_mismatch", stored_result=result)

    def test_a_foreign_session(self):
        for session in ("S-someone-else", None, ""):
            with self.subTest(session=repr(session)):
                self.assertRefused("session_mismatch", session_uuid=session)

    def test_a_prior_that_is_not_green(self):
        for name, over in (
                ("red", {"verdict": "red"}),
                ("unverified", {"verdict": "unverified"}),
                ("mutation", {"mutation": {"reason": "mutated"}}),
                ("ledger failure", {"ledger_failure": {"reason": "x"}}),
                ("startup failure", {"startup_failure": {"reason": "x"}}),
                ("persistence failure", {"result_persistence_failed": True})):
            with self.subTest(case=name):
                self.assertRefused("not_green",
                                   stored_result=_result_with(**over))

    def test_a_deferred_prior(self):
        self.assertRefused(
            "deferred",
            stored_result=_result_with(
                deferred_reconciliation={"state": "pending"}))

    def test_a_final_suite_that_did_not_run_to_completion(self):
        for value in ("not_reached", "legacy_unknown", None, "other"):
            with self.subTest(binding=repr(value)):
                self.assertRefused(
                    "binding_not_final",
                    stored_result=_result_with(final_suite_binding=value))

    def test_a_disposition_that_may_not_bind(self):
        for value in ("rejected", "accepted", None, "other"):
            with self.subTest(disposition=repr(value)):
                self.assertRefused(
                    "disposition_not_bindable",
                    pointer=_pointer_with(disposition=value))
        self.assertRefused("disposition_not_bindable",
                           disposition="rejected")

    def test_an_open_finding_that_targets_the_verification(self):
        for tokens in (["verification_challenge"], ["architectural"],
                       ["signal_malformed"], ["unknown_token"],
                       ("verification_challenge", "architectural"),
                       "verification_challenge", 5, object(), None,
                       iter(()), {"architectural": True}):
            with self.subTest(tokens=repr(tokens)[:40]):
                self.assertRefused("verification_finding_open",
                                   open_finding_classes=tokens)

    def test_a_moved_candidate_manifest(self):
        for live in ((SHA_A, _F.snapshot[1]), None, (), "pair",
                     (_F.snapshot[0],), (None, None)):
            with self.subTest(live=repr(live)):
                self.assertRefused("manifest_mismatch", live_identity=live)
        self.assertRefused("manifest_mismatch",
                           pointer=_pointer_with(manifest_digest=SHA_A))

    def test_a_moved_candidate_index(self):
        self.assertRefused("index_mismatch",
                           live_identity=(_F.snapshot[0], SHA_B))
        self.assertRefused("index_mismatch",
                           pointer=_pointer_with(index_digest=SHA_B))

    def test_a_different_inventory(self):
        changed = [dict(e, command=["python3", "-c", "print(1)"])
                   if e["label"] == "doc a" else e for e in _F.entries]
        other = binding.compute_inventory_identity(
            _F.schema, changed, None, _F.configuration)
        self.assertNotEqual(other, _F.identity)
        for value in (other, None, "", SHA_A):
            with self.subTest(identity=repr(value)):
                self.assertRefused("inventory_mismatch",
                                   current_inventory_identity=value)

    def test_every_refusal_code_has_a_negative_above(self):
        seen = set()
        for over in (
                {"pointer": None},
                {"pointer": _pointer_with(inventory_identity=None)},
                {"reuse_mode": "none"},
                {"stored_result": None},
                {"stored_request": None},
                {"session_uuid": "S-other"},
                {"stored_result": _result_with(verdict="red")},
                {"stored_result": _result_with(
                    deferred_reconciliation={"state": "pending"})},
                {"stored_result": _result_with(final_suite_binding=None)},
                {"disposition": "rejected"},
                {"open_finding_classes": ["architectural"]},
                {"live_identity": (SHA_A, _F.snapshot[1])},
                {"live_identity": (_F.snapshot[0], SHA_B)},
                {"current_inventory_identity": SHA_A}):
            seen.add(_bind(**over)[1])
        self.assertEqual(seen, set(binding.REFUSAL_CODES))

    def test_the_first_failing_check_names_the_refusal(self):
        self.assertRefused(
            "no_pointer", pointer=None, reuse_mode="none",
            stored_result=None, session_uuid=None)
        self.assertRefused(
            "legacy_pointer_no_identity",
            pointer=_pointer_with(inventory_identity=None),
            reuse_mode="none")
        self.assertRefused("reuse_mode_not_allowed", reuse_mode="none",
                           stored_result=None)
        self.assertRefused("not_green", stored_result=_result_with(
            verdict="red", deferred_reconciliation={"state": "pending"}))
        self.assertRefused(
            "disposition_not_bindable", disposition="rejected",
            open_finding_classes=["architectural"],
            live_identity=(SHA_A, SHA_B))

    def test_corrupt_inputs_are_refusals_and_never_raise(self):
        garbage = (None, 0, 1.5, "x", b"x", [], (), {}, [1, 2], {"a": []},
                   object(), float("nan"))
        for value in garbage:
            for name in ("pointer", "stored_result", "stored_request",
                         "live_identity", "current_inventory_identity",
                         "reuse_mode", "session_uuid", "disposition",
                         "open_finding_classes"):
                with self.subTest(arg=name, value=repr(value)[:30]):
                    bound, refusal = _bind(**{name: value})
                    if bound is None:
                        self.assertIn(refusal, binding.REFUSAL_CODES)
                    else:
                        self.assertIsNone(refusal)
        bound, refusal = binding.bind_prior_green(
            None, None, None, None, None, None)
        self.assertEqual((bound, refusal), (None, "no_pointer"))

    def test_the_inputs_are_not_mutated(self):
        kwargs = _kwargs()
        before = copy.deepcopy(kwargs)
        binding.bind_prior_green(**kwargs)
        self.assertEqual(kwargs, before)
        kwargs = _kwargs(stored_result=_result_with(verdict="red"))
        before = copy.deepcopy(kwargs)
        binding.bind_prior_green(**kwargs)
        self.assertEqual(kwargs, before)


class InventoryIdentityTests(unittest.TestCase):
    ENTRIES = [_entry("a", ["docs/a.md"]), _entry("b", kind="final_suite")]

    def _identity(self, **over):
        args = dict(schema=2, entries=copy.deepcopy(self.ENTRIES),
                    suite_decl=None, configuration={"team": ["builder"]})
        args.update(over)
        return binding.compute_inventory_identity(**args)

    def test_equal_input_gives_one_hex_identity(self):
        first, second = self._identity(), self._identity()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertTrue(set(first) <= set("0123456789abcdef"))

    def test_key_order_does_not_matter(self):
        reordered = [dict(reversed(list(e.items()))) for e in self.ENTRIES]
        self.assertEqual(self._identity(),
                         self._identity(entries=reordered))
        self.assertEqual(
            self._identity(configuration={"x": 1, "y": 2}),
            self._identity(configuration={"y": 2, "x": 1}))

    def test_any_semantic_change_changes_the_identity(self):
        base = self._identity()
        changed_entry = copy.deepcopy(self.ENTRIES)
        changed_entry[0]["command"] = ["python3", "-c", "print(1)"]
        changed_depends = copy.deepcopy(self.ENTRIES)
        changed_depends[0]["depends_on"] = ["docs/other.md"]
        for name, other in (
                ("entry", self._identity(entries=changed_entry)),
                ("dependency", self._identity(entries=changed_depends)),
                ("order", self._identity(entries=self.ENTRIES[::-1])),
                ("fewer entries", self._identity(entries=self.ENTRIES[:1])),
                ("suite", self._identity(suite_decl={"suite_id": "s"})),
                ("configuration", self._identity(configuration={})),
                ("schema", self._identity(schema=3))):
            with self.subTest(change=name):
                self.assertNotEqual(other, base)

    def test_input_that_cannot_be_serialized_has_no_identity(self):
        for over in ({"entries": [object()]},
                     {"configuration": {"x": float("nan")}},
                     {"suite_decl": {1, 2}}):
            with self.subTest(over=sorted(over)):
                self.assertIsNone(self._identity(**over))

    def test_a_missing_identity_never_binds(self):
        bound, refusal = _bind(current_inventory_identity=None)
        self.assertEqual((bound, refusal), (None, "inventory_mismatch"))

    def test_normalizing_the_same_inventory_twice_agrees(self):
        schema, entries, _label = verification.normalize_inventory(_F.raw)
        again = verification.normalize_inventory(copy.deepcopy(_F.raw))[1]
        self.assertEqual(
            binding.compute_inventory_identity(schema, entries, None, {}),
            binding.compute_inventory_identity(schema, again, None, {}))
        text = json.dumps(entries, sort_keys=True)
        self.assertIn("doc a", text)


if __name__ == "__main__":
    unittest.main()
