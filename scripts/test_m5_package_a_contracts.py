#!/usr/bin/env python3
"""Focused suite for M5 Package A: checkpoint request/result/receipt/claim-
lease contracts, the additive `cowork_state.py` path helpers, and the two
frozen extraction seams (`cowork_verification_worker.py`,
`cowork_verification_evidence.py`) -- garusis/cowork-internal#60, foundation
#24, and the Package-A extraction prerequisites for #44/#51.

Never invokes a real Claude, Codex, or OpenCode session; every fixture that
needs a real subprocess spawns a bare `python3 -c ...` inside a throwaway
git repo, exactly like `scripts/test_cowork.py`'s own owned-verification
fixtures.

Run standalone:

    python3 -m unittest scripts/test_m5_package_a_contracts.py -v
"""

import ast
import hashlib
import inspect
import json
import os
import py_compile
import re
import shutil
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
_REPO_ROOT = os.path.dirname(_HERE)

import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402
import cowork_verification_worker as worker_module  # noqa: E402
import cowork_verification_evidence as evidence_module  # noqa: E402

# The signed base this package's brief is bound to
# (m5-supervisor-checkpoints-plan-v2.json's own `base_commit`).
BASE_SHA = "729c1750907151345c7326e49c2aef2d815bb5e3"

# This package's own signed commit -- pins the changed-paths allowlist
# proof to Package A's own committed diff (BASE_SHA..CANDIDATE_SHA) rather
# than to whatever happens to be dirty in a worktree that also carries
# later, unrelated packages' own uncommitted test-only edits.
CANDIDATE_SHA = "eae4276d07a887a041177817221bf1b0bcdf99f0"

# The exact, frozen five-path allowlist this package may change.
ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/cowork_verification.py",
    "scripts/cowork_state.py",
    "scripts/cowork_verification_worker.py",
    "scripts/cowork_verification_evidence.py",
    "scripts/test_m5_package_a_contracts.py",
})

# Every other path the frozen brief explicitly names as excluded (read-only)
# for this package.
EXCLUDED_PATHS = (
    "scripts/cowork.py",
    "scripts/cowork_handoff.py",
    "scripts/cowork_ledger.py",
    "scripts/cowork_measure.py",
    "scripts/test_cowork.py",
)

# The frozen 28-module M1-M4 regression command
# (integration_policy.regression_command).
REGRESSION_MODULES = (
    "scripts.test_cowork", "scripts.test_cowork_activity_contracts",
    "scripts.test_cowork_activity_cross_surface",
    "scripts.test_cowork_bridge_activity", "scripts.test_cowork_bridge_capacity",
    "scripts.test_cowork_capacity", "scripts.test_cowork_capacity_scheduler",
    "scripts.test_cowork_control_plane", "scripts.test_cowork_control_plane_m3",
    "scripts.test_cowork_dispatch_identity", "scripts.test_cowork_policy_atomic",
    "scripts.test_cowork_recovery_breaker", "scripts.test_cowork_report_activity",
    "scripts.test_cowork_state_m2", "scripts.test_cowork_state_m3",
    "scripts.test_cowork_state_m4", "scripts.test_cowork_ui_activity",
    "scripts.test_cowork_wake_macos", "scripts.test_cowork_wake_manual",
    "scripts.test_cowork_watchdog", "scripts.test_cowork_workunit",
    "scripts.test_dispatch_contract_characterization",
    "scripts.test_m2_crash_resume", "scripts.test_m2_negative_controls",
    "scripts.test_m3_crash_resume", "scripts.test_m3_negative_controls",
    "scripts.test_m4_crash_resume", "scripts.test_m4_negative_controls",
)


def _git_changed_paths():
    return set(subprocess.run(
        ["git", "diff", "--name-only", BASE_SHA, CANDIDATE_SHA],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines())


def _git_show(rev, rel_path):
    result = subprocess.run(
        ["git", "show", "%s:%s" % (rev, rel_path)],
        cwd=_REPO_ROOT, capture_output=True, check=True)
    return result.stdout


def _sha256_file(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


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
    """Copy the REAL, candidate `cowork_verification.py` and every module it
    now imports at load time (`cowork_state`, `cowork_ledger`,
    `cowork_verification_worker`, `cowork_verification_evidence`) into a
    throwaway repo's `scripts/` dir and commit them, so a spawned `--worker`
    subprocess is fully self-sufficient -- unlike
    `scripts/test_cowork.py`'s own `_seed_worker_into_repo`, which seeds
    only the base commit's four dependencies and is exactly the fixture the
    resilient-import fallback near the top of `cowork_verification.py`
    exists for (see `WorkerSubprocessMissingSeamSiblingsTests` below)."""
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
    """Shared fixture: an isolated COWORK_SESSIONS_ROOT and a throwaway
    committed git repo seeded with the real, candidate worker modules --
    mirrors `scripts/test_cowork.py`'s own `_OwnedVerificationTestBase`, but
    self-contained (no cross-import from that file) and seeded with the two
    new seam modules so a spawned worker never needs the import fallback."""

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


# =========================================================================== #
# Allowlist, py_compile, and candidate-hash gates.                            #
# =========================================================================== #


class AllowlistAndHashTests(unittest.TestCase):

    def test_changed_paths_are_within_the_five_path_allowlist(self):
        offenders = _git_changed_paths() - ALLOWED_CHANGED_PATHS
        self.assertFalse(
            offenders,
            "paths changed outside the frozen five-path allowlist: %s"
            % sorted(offenders))

    def test_candidate_sha_is_exactly_one_commit_on_base_sha(self):
        result = subprocess.run(
            ["git", "rev-parse", "%s^" % CANDIDATE_SHA],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(
            result.stdout.strip(), BASE_SHA,
            "CANDIDATE_SHA must be exactly one commit on top of BASE_SHA")

    def test_all_five_owned_paths_py_compile(self):
        for rel in sorted(ALLOWED_CHANGED_PATHS):
            path = os.path.join(_REPO_ROOT, rel)
            self.assertTrue(os.path.exists(path), "missing owned path: %s"
                            % rel)
            py_compile.compile(path, doraise=True)

    def test_candidate_hashes_are_well_formed_sha256(self):
        hashes = {}
        for rel in sorted(ALLOWED_CHANGED_PATHS):
            digest = _sha256_file(rel)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            hashes[rel] = digest
        # The two seam modules and the spine must never coincide -- a real
        # relocation, not a byte-identical copy-paste.
        self.assertNotEqual(hashes["scripts/cowork_verification.py"],
                            hashes["scripts/cowork_verification_worker.py"])
        self.assertNotEqual(hashes["scripts/cowork_verification.py"],
                            hashes["scripts/cowork_verification_evidence.py"])
        self.assertNotEqual(hashes["scripts/cowork_verification_worker.py"],
                            hashes["scripts/cowork_verification_evidence.py"])


class ExcludedPathsUntouchedTests(unittest.TestCase):
    """Every path the frozen brief marks read-only for this package must be
    byte-identical to the signed base commit."""

    def test_excluded_paths_are_byte_identical_to_base(self):
        # Commit-pinned, not live-working-tree: this package's own
        # read-only claim over each excluded path is a property of ITS OWN
        # committed diff (BASE_SHA..CANDIDATE_SHA) -- the live-tree form is
        # inherently stale once this package is itself historical and a
        # LATER, unrelated package's own uncommitted test-only edits share
        # the same worktree.
        for rel in EXCLUDED_PATHS:
            base_bytes = _git_show(BASE_SHA, rel)
            current_bytes = _git_show(CANDIDATE_SHA, rel)
            self.assertEqual(
                current_bytes, base_bytes,
                "%s must be byte-identical to the signed base commit "
                "(read-only for this package)" % rel)


# =========================================================================== #
# self_source_hash: candidate-identity equality (not a base-commit value).    #
# =========================================================================== #


class SelfSourceHashIdentityTests(unittest.TestCase):

    def test_matches_the_candidates_own_current_bytes(self):
        with open(os.path.join(_REPO_ROOT,
                               "scripts", "cowork_verification.py"), "rb") as fh:
            candidate_bytes = fh.read()
        expected = hashlib.sha256(candidate_bytes).hexdigest()
        self.assertEqual(verification.self_source_hash(), expected)

    def test_does_not_equal_the_base_commit_hash(self):
        base_bytes = _git_show(BASE_SHA, "scripts/cowork_verification.py")
        base_hash = hashlib.sha256(base_bytes).hexdigest()
        # Package A necessarily edits this file, so its current hash must
        # differ from the frozen base-commit value -- asserting EQUALITY to
        # a base-commit hash here would be exactly the defect M5R2-B1 closed.
        self.assertNotEqual(verification.self_source_hash(), base_hash)

    def test_differs_from_both_seam_modules_own_hashes(self):
        worker_hash = _sha256_file("scripts/cowork_verification_worker.py")
        evidence_hash = _sha256_file("scripts/cowork_verification_evidence.py")
        current = verification.self_source_hash()
        self.assertNotEqual(current, worker_hash)
        self.assertNotEqual(current, evidence_hash)

    def test_is_a_locally_defined_function_not_an_import(self):
        self.assertEqual(verification.self_source_hash.__module__,
                         "cowork_verification")
        self.assertNotIn("self_source_hash",
                         vars(worker_module))
        self.assertNotIn("self_source_hash",
                         vars(evidence_module))


# =========================================================================== #
# Extraction/re-export structural compatibility (indirection_contract).       #
# =========================================================================== #


class ExtractionReexportStructuralTests(unittest.TestCase):
    """The frozen indirection_contract's exact symbol set: every name is a
    bare module attribute of `cowork_verification`, bound to the seam
    module's own function object -- never a copy -- so
    `mock.patch.object(verification, name, ...)` and every bare-name call
    site inside `cowork_verification.py` intercept identically to the base
    commit's own, undivided implementation."""

    _WORKER_REEXPORTS = ("spawn_worker", "verify_worker_identity",
                        "_read_worker_startup_log")
    _EVIDENCE_REEXPORTS = ("bounded_evidence_wait", "_poll_attempt_events",
                          "_revise_attempt_ledger",
                          "_revise_attempt_ledger_with_retry",
                          "_wait_for_attempt_and_revise_ledger",
                          "should_defer_teardown")

    def test_worker_reexports_are_identity_bound_to_the_worker_module(self):
        for name in self._WORKER_REEXPORTS:
            self.assertIs(getattr(verification, name),
                          getattr(worker_module, name),
                          "verification.%s is not the same object as "
                          "cowork_verification_worker.%s" % (name, name))

    def test_evidence_reexports_are_identity_bound_to_the_evidence_module(self):
        for name in self._EVIDENCE_REEXPORTS:
            self.assertIs(getattr(verification, name),
                          getattr(evidence_module, name),
                          "verification.%s is not the same object as "
                          "cowork_verification_evidence.%s" % (name, name))

    def test_mock_patch_object_intercepts_the_spawn_worker_call_site(self):
        # scripts/test_cowork.py's one real mock.patch.object(verification,
        # "spawn_worker", ...) call site (:25133) relies on exactly this
        # module-attribute-lookup-at-call-time mechanism.
        sentinel = object()
        with mock.patch.object(verification, "spawn_worker",
                               side_effect=lambda *a, **k: sentinel):
            self.assertIs(verification.spawn_worker(), sentinel)
        self.assertIsNot(verification.spawn_worker, sentinel)
        self.assertIs(verification.spawn_worker, worker_module.spawn_worker)

    def test_run_owned_transaction_references_bare_names_not_qualified(self):
        # Python resolves an unqualified name at CALL time against the
        # enclosing function's __globals__ (== cowork_verification's own
        # module dict) -- this is what co_names records for every global
        # name a function's bytecode actually references.
        names = set(verification._run_owned_transaction.__code__.co_names)
        for expected in self._WORKER_REEXPORTS[:1] + (
                "cleanup_active_command_group", "terminate_worker",
                "_issue_permit", "detect_mutation") + self._EVIDENCE_REEXPORTS[2:]:
            self.assertIn(expected, names,
                         "_run_owned_transaction no longer references the "
                         "bare name %r" % expected)

    def test_five_direct_attribute_call_sites_resolve_to_seam_functions(self):
        # The exact five direct (non-patch) call sites named by the frozen
        # brief: verification._read_worker_startup_log (:24941),
        # verification.bounded_evidence_wait (:23747, :23760),
        # verification._wait_for_attempt_and_revise_ledger (:24090, :24120).
        self.assertIs(verification._read_worker_startup_log,
                      worker_module._read_worker_startup_log)
        self.assertIs(verification.bounded_evidence_wait,
                      evidence_module.bounded_evidence_wait)
        self.assertIs(verification._wait_for_attempt_and_revise_ledger,
                      evidence_module._wait_for_attempt_and_revise_ledger)

    def test_test_cowork_py_is_byte_identical_to_base(self):
        # Commit-pinned, not live-working-tree: this package's own
        # read-only claim over test_cowork.py is a property of ITS OWN
        # committed diff (BASE_SHA..CANDIDATE_SHA), exactly like
        # `test_excluded_paths_are_byte_identical_to_base` above -- never
        # whatever a later, unrelated package's own uncommitted test-only
        # edits also happen to add to the same file in the same worktree.
        base_bytes = _git_show(BASE_SHA, "scripts/test_cowork.py")
        current_bytes = _git_show(CANDIDATE_SHA, "scripts/test_cowork.py")
        self.assertEqual(current_bytes, base_bytes)


# =========================================================================== #
# Widened worker-startup return channel (WorkerStartupResult).                #
# =========================================================================== #


class WorkerStartupResultChannelTests(unittest.TestCase):

    def _make(self, classification=None):
        classification = classification or {
            "identity": None, "worker_verified": False,
            "startup_failure": None}
        return worker_module.WorkerStartupResult(
            "PROC", "FD", "THREAD", classification)

    def test_iterates_as_the_original_three_item_handle_bundle(self):
        proc, liveness_write_fd, capture_thread = self._make()
        self.assertEqual((proc, liveness_write_fd, capture_thread),
                         ("PROC", "FD", "THREAD"))

    def test_len_and_getitem_match_the_three_item_bundle(self):
        wr = self._make()
        self.assertEqual(len(wr), 3)
        self.assertEqual((wr[0], wr[1], wr[2]), ("PROC", "FD", "THREAD"))

    def test_classification_carries_the_widened_fourth_field(self):
        classification = {"identity": {"source_hash": "abc"},
                          "worker_verified": True, "startup_failure": None}
        wr = self._make(classification)
        self.assertEqual(wr.classification, classification)
        self.assertEqual(set(wr.classification.keys()),
                         {"identity", "worker_verified", "startup_failure"})

    def test_channel_is_lossless_for_every_transactionresult_field(self):
        # The three TransactionResult fields the base spine computed itself
        # (worker_identity, worker_identity_verified, startup_failure) are
        # exactly classification's three keys -- no channel is lossy.
        classification = {"identity": {"x": 1}, "worker_verified": True,
                          "startup_failure": {"reason": "y"}}
        wr = self._make(classification)
        self.assertEqual(wr.classification["identity"], {"x": 1})
        self.assertIs(wr.classification["worker_verified"], True)
        self.assertEqual(wr.classification["startup_failure"], {"reason": "y"})


# =========================================================================== #
# startup_failure reason-set ownership (M5R2-M1).                             #
# =========================================================================== #


def _string_literals_excluding_docstrings(source):
    """Every string constant a module/function's CODE actually produces,
    excluding docstrings (and, since `ast` never records them at all,
    comments) -- so a prose reference inside a docstring/comment (e.g. this
    test file's own explanations of what Package B/C will add) can never be
    mistaken for a string this candidate's code itself constructs."""
    tree = ast.parse(source)
    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(
                        value.value, str):
                    docstring_ids.add(id(value))
    return [node.value for node in ast.walk(tree)
           if isinstance(node, ast.Constant) and isinstance(node.value, str)
           and id(node) not in docstring_ids]


class StartupFailureOwnershipTests(unittest.TestCase):

    def test_request_rejected_and_worker_exited_reasons_are_worker_owned(self):
        literals = _string_literals_excluding_docstrings(
            inspect.getsource(worker_module))
        self.assertIn("request_rejected", literals)
        self.assertIn("worker_exited_before_identity_report", literals)

    def test_worker_spawn_failed_is_absent_from_the_worker_module(self):
        literals = _string_literals_excluding_docstrings(
            inspect.getsource(worker_module))
        self.assertNotIn("worker_spawn_failed", literals)

    def test_worker_source_missing_and_identity_mismatch_are_worker_owned(self):
        # Package B (#44) has since added its two NEW reasons. They joined
        # the taxonomy exactly where the two reasons above already live --
        # inside this module, minted by this module's own code -- so the
        # ownership boundary this class exists to pin is now stated
        # positively for all four, at the specific seam that produces each.
        literals = _string_literals_excluding_docstrings(
            inspect.getsource(worker_module))
        self.assertIn("worker_source_missing", literals)
        self.assertIn("worker_identity_mismatch", literals)
        # `worker_source_missing` is decided BEFORE any identity report can
        # arrive (spawn_worker, when resolve_worker_source returns None);
        # `worker_identity_mismatch` only AFTER one did (the startup
        # classifier). Neither is produced by the other's seam.
        spawn_literals = _string_literals_excluding_docstrings(
            inspect.getsource(worker_module.spawn_worker))
        self.assertIn("worker_source_missing", spawn_literals)
        self.assertNotIn("worker_identity_mismatch", spawn_literals)
        classify_literals = _string_literals_excluding_docstrings(
            inspect.getsource(worker_module._classify_worker_startup))
        self.assertIn("worker_identity_mismatch", classify_literals)
        self.assertNotIn("worker_source_missing", classify_literals)
        # Worker-owned means the spine mints neither -- the same direction
        # of ownership `worker_spawn_failed` has in reverse below.
        spine_literals = _string_literals_excluding_docstrings(
            inspect.getsource(verification))
        self.assertNotIn("worker_source_missing", spine_literals)
        self.assertNotIn("worker_identity_mismatch", spine_literals)

    def test_worker_spawn_failed_is_produced_only_by_run_transactions_oserror_handler(self):
        spine_literals = _string_literals_excluding_docstrings(
            inspect.getsource(verification))
        self.assertIn("worker_spawn_failed", spine_literals)
        # Scoped to run_transaction's own except OSError handler, not
        # _run_owned_transaction.
        run_owned_literals = _string_literals_excluding_docstrings(
            inspect.getsource(verification._run_owned_transaction))
        self.assertNotIn("worker_spawn_failed", run_owned_literals)
        run_transaction_body_src = inspect.getsource(
            verification._run_transaction_body)
        self.assertIn("worker_spawn_failed", run_transaction_body_src)
        self.assertIn("except OSError", run_transaction_body_src)


# =========================================================================== #
# Structural: no inlined worker/evidence logic remains in the spine.          #
# =========================================================================== #


class RunOwnedTransactionStructuralTests(unittest.TestCase):

    def test_no_inlined_identity_verification_or_worker_spawn_logic(self):
        names = set(verification._run_owned_transaction.__code__.co_names)
        for forbidden in ("verify_worker_identity", "_read_worker_identity",
                          "_read_worker_startup_log",
                          "WORKER_EXIT_REQUEST_REJECTED", "subprocess"):
            self.assertNotIn(
                forbidden, names,
                "_run_owned_transaction still inlines %r -- worker-spawn/"
                "startup-classification/identity-verification logic must "
                "be fully relocated" % forbidden)

    def test_no_inlined_evidence_poll_internals(self):
        names = set(verification._run_owned_transaction.__code__.co_names)
        for forbidden in ("bounded_evidence_wait", "_poll_attempt_events"):
            self.assertNotIn(
                forbidden, names,
                "_run_owned_transaction directly references %r -- these "
                "live one level down, only inside "
                "_wait_for_attempt_and_revise_ledger" % forbidden)

    def test_should_defer_teardown_is_the_only_new_decision_point_referenced(self):
        names = set(verification._run_owned_transaction.__code__.co_names)
        self.assertIn("should_defer_teardown", names)

    def test_mint_failure_and_oserror_literals_are_exempt_and_untouched(self):
        # M5R2-M1: the mint-failure TransactionResult literal (:2217 at
        # base) and run_transaction's OSError handler (:2255-2277 at base)
        # are spine-retained and explicitly OUTSIDE this extraction's scope
        # -- this structural test is scoped to _run_owned_transaction only.
        body_src = inspect.getsource(verification._run_transaction_body)
        self.assertIn("mint_failed_label", body_src)
        self.assertIn("except OSError", body_src)


# =========================================================================== #
# Definition-site-dependency audit (M5R-C1, M5R2-m1, M5R2-m2).                #
# =========================================================================== #


class DefinitionSiteDependencyAuditTests(unittest.TestCase):

    _RELOCATED = (
        (worker_module, "spawn_worker"),
        (worker_module, "verify_worker_identity"),
        (worker_module, "_read_worker_startup_log"),
        (worker_module, "_read_worker_identity"),
        (evidence_module, "bounded_evidence_wait"),
        (evidence_module, "_wait_for_attempt_and_revise_ledger"),
        (evidence_module, "_poll_attempt_events"),
        (evidence_module, "_revise_attempt_ledger"),
        (evidence_module, "_revise_attempt_ledger_with_retry"),
    )

    def test_nine_relocated_symbols_have_no_definition_site_dependence(self):
        self.assertEqual(len(self._RELOCATED), 9)
        for module, name in self._RELOCATED:
            src = inspect.getsource(getattr(module, name))
            for token in ("__file__", "__name__", "__module__"):
                self.assertNotIn(
                    token, src,
                    "%s.%s unexpectedly depends on %s -- audit finding "
                    "should have been 'no such dependency found'"
                    % (module.__name__, name, token))

    def test_should_defer_teardown_newly_defined_no_dependence_possible(self):
        src = inspect.getsource(evidence_module.should_defer_teardown)
        for token in ("__file__", "__name__", "__module__"):
            self.assertNotIn(token, src)

    def test_self_source_hash_is_the_sole_file_dependent_exclusion(self):
        src = inspect.getsource(verification.self_source_hash)
        self.assertIn("__file__", src)


# =========================================================================== #
# Extension stubs for Package B (#44) / Package C (#51).                     #
# =========================================================================== #


class ExtensionStubTests(unittest.TestCase):

    def test_resolve_worker_source_captures_the_running_installation(self):
        # Package B (#44) filled this seam in. Its current contract, all of
        # it exercised here: no session/transaction identity at all is the
        # documented `worker_source_missing` condition, returned as `None`
        # rather than raised; with both identities it captures THIS running
        # Cowork installation's own `scripts/` tree, records the manifest
        # durably, and materializes the checkout `spawn_worker` would exec
        # from. No worker binary is assumed and no process is launched --
        # only file capture into an isolated, throwaway sessions root.
        self.assertIsNone(worker_module.resolve_worker_source())
        self.assertIsNone(worker_module.resolve_worker_source("S-x", None))
        self.assertIsNone(worker_module.resolve_worker_source(None, "T-x"))

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
        session_uuid = "S-" + uuid.uuid4().hex[:8]
        transaction_id = "T-" + uuid.uuid4().hex[:8]

        resolution = worker_module.resolve_worker_source(
            session_uuid, transaction_id)
        self.assertEqual(
            set(resolution),
            {"checkout_root", "manifest_files", "worker_rel_path",
             "expected_hash"})
        worker_rel = os.path.join("scripts", "cowork_verification.py")
        self.assertEqual(resolution["worker_rel_path"], worker_rel)
        # The captured source is the RUNNING installation's own, never a
        # target-repo copy: the expected hash the worker's self-report must
        # equal is this very process's own `cowork_verification.py` bytes.
        self.assertEqual(resolution["expected_hash"],
                         _sha256_file("scripts/cowork_verification.py"))
        self.assertEqual(
            resolution["manifest_files"][worker_rel]["sha256"],
            resolution["expected_hash"])
        # The checkout is a real, nameable, materialized tree under this
        # session's own tool-snapshot root -- not merely a computed path.
        self.assertEqual(
            resolution["checkout_root"],
            worker_module.tool_snapshot_checkout_dir(
                session_uuid, transaction_id))
        captured = os.path.join(resolution["checkout_root"], worker_rel)
        self.assertTrue(os.path.isfile(captured))
        with open(captured, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(),
                             resolution["expected_hash"])
        # ...and the manifest `_classify_worker_startup` reads back after
        # the worker reports identity is already durable at this point,
        # i.e. written BEFORE any process could have been spawned.
        manifest_doc = state_store.read_json_tolerant(
            state_store.verification_tool_snapshot_manifest_path_for(
                session_uuid, transaction_id))
        self.assertEqual(manifest_doc["files"], resolution["manifest_files"])
        self.assertEqual(manifest_doc["root"], _HERE)

    def test_reconcile_pending_evidence_resolves_a_deferred_label_durably(self):
        # Package C (#51) filled this seam in. Exercised here is its actual
        # durable behavior, not merely that it stopped raising: a deferred
        # label whose terminal evidence has since landed is revised in the
        # ledger to its TRUE terminal outcome under the SAME minted id,
        # dropped from the transaction's durable deferred marker, and its
        # teardown owned exactly once -- then a second pass finds nothing
        # left (idempotent, no double teardown). Driven entirely from
        # bounded local fixtures: an isolated sessions root and one
        # hand-written terminal event already on disk, so the bounded poll
        # resolves on its first attempt and nothing real is ever launched.
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
        session_uuid = "S-" + uuid.uuid4().hex[:8]
        transaction_id = "T-" + uuid.uuid4().hex[:8]

        ledger_path = state_store.ledger_path_for(session_uuid)
        minted = evidence_module.ledger.mint_owned_attempt(
            ledger_path, transaction_id, "slow",
            fields={"command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot"})
        self.assertIsNotNone(minted)
        evidence_module.ledger.revise_owned_attempt(
            ledger_path, transaction_id, "slow",
            fields={"evidence_state": evidence_module.EVIDENCE_UNRESOLVED},
            attempt_state="unresolved")
        evidence_module._write_deferred_marker(
            session_uuid, transaction_id,
            {"slow": {"pgid": 999999, "deferred_at": "2026-01-01T00:00:00Z"}})
        state_store.append_jsonl_atomic(
            state_store.verification_attempt_events_path_for(
                session_uuid, transaction_id),
            {"event": "terminal", "label": "slow",
             "at": "2026-01-01T00:00:00Z",
             "evidence_state": evidence_module.EVIDENCE_PRESENT,
             "exit_code": 0, "timed_out": False, "wall_time_s": 0.2})

        with mock.patch.object(
                verification, "cleanup_active_command_group") as cleanup:
            result = evidence_module.reconcile_pending_evidence(
                session_uuid, transaction_id, "slow")
        self.assertEqual(result["transaction_id"], transaction_id)
        self.assertEqual(result["still_pending"], [])
        self.assertEqual(
            result["reconciled"],
            [{"label": "slow",
              "evidence_state": evidence_module.EVIDENCE_PRESENT,
              "ledger_ok": True}])
        self.assertEqual(cleanup.call_count, 1)

        key = evidence_module.ledger.owned_attempt_key(transaction_id, "slow")
        records = [rec for rec in evidence_module.ledger.read_ledger(
            ledger_path) if rec.get("attempt_key") == key]
        self.assertEqual({rec["id"] for rec in records}, {minted["id"]})
        latest = records[-1]
        self.assertEqual(latest["attempt_state"], "terminal")
        self.assertEqual(latest["evidence_state"],
                         evidence_module.EVIDENCE_PRESENT)
        self.assertEqual(latest["exit_status"], "pass")
        self.assertEqual(latest["adjudication"], "pass")
        self.assertEqual(
            evidence_module._read_deferred_marker(
                session_uuid, transaction_id)["labels"], {})

        with mock.patch.object(
                verification, "cleanup_active_command_group") as cleanup_again:
            second = evidence_module.reconcile_pending_evidence(
                session_uuid, transaction_id, "slow")
        self.assertEqual(second["reconciled"], [])
        self.assertEqual(second["still_pending"], [])
        self.assertEqual(cleanup_again.call_count, 0)

    def test_should_defer_teardown_predicate_itself_never_raises(self):
        # M5R-C2: NotImplementedError is scoped only to the reconciliation
        # body Package C fills in -- never to this predicate, which must
        # return a concrete False.
        self.assertIs(
            evidence_module.should_defer_teardown("s", "t", "label"), False)

    def test_completed_seams_are_referenced_exactly_where_intended(self):
        # Checked against compiled global-name references (co_names), never
        # raw source text -- a prose mention inside a docstring explaining
        # WHY these seams exist (as these modules' own docstrings do) must
        # never be mistaken for an actual call.
        #
        # Both seams are now implemented, so this states where each one is
        # reached from. `spawn_worker` is the one caller of
        # `resolve_worker_source`: source is captured there, before any
        # pipe or process exists.
        self.assertIn("resolve_worker_source",
                      worker_module.spawn_worker.__code__.co_names)
        # The boundary this test has always drawn is unchanged for
        # everything else. The startup classifier verifies against the
        # manifest `resolve_worker_source` already wrote; it never
        # re-resolves the source itself...
        self.assertNotIn(
            "resolve_worker_source",
            worker_module._classify_worker_startup.__code__.co_names)
        # ...and `should_defer_teardown` remains a predicate only: it never
        # reaches into the reconciliation that owns ledger revision and
        # teardown (M5R-C2's own scoping, still intact after #51).
        self.assertNotIn(
            "reconcile_pending_evidence",
            evidence_module.should_defer_teardown.__code__.co_names)
        # Neither seam is inlined into the spine's own owned-transaction
        # flow: it reaches them only through the two relocated entry points
        # it already calls (`spawn_worker`, `should_defer_teardown`).
        for forbidden in ("resolve_worker_source",
                         "reconcile_pending_evidence"):
            self.assertNotIn(
                forbidden,
                verification._run_owned_transaction.__code__.co_names)


# =========================================================================== #
# Cross-module shared-constant consistency (no silent drift).                 #
# =========================================================================== #


class CrossModuleConstantConsistencyTests(unittest.TestCase):

    def test_evidence_state_constants_match(self):
        self.assertEqual(verification.EVIDENCE_PRESENT,
                         evidence_module.EVIDENCE_PRESENT)
        self.assertEqual(verification.EVIDENCE_UNRESOLVED,
                         evidence_module.EVIDENCE_UNRESOLVED)

    def test_evidence_poll_defaults_match(self):
        self.assertEqual(verification.DEFAULT_EVIDENCE_POLL_ATTEMPTS,
                         evidence_module.DEFAULT_EVIDENCE_POLL_ATTEMPTS)
        self.assertEqual(verification.DEFAULT_EVIDENCE_POLL_DELAY_S,
                         evidence_module.DEFAULT_EVIDENCE_POLL_DELAY_S)

    def test_bounded_evidence_wait_default_params_match_the_constants(self):
        sig = inspect.signature(evidence_module.bounded_evidence_wait)
        self.assertEqual(sig.parameters["poll_attempts"].default,
                         verification.DEFAULT_EVIDENCE_POLL_ATTEMPTS)
        self.assertEqual(sig.parameters["poll_delay_s"].default,
                         verification.DEFAULT_EVIDENCE_POLL_DELAY_S)

    def test_max_startup_log_bytes_matches(self):
        self.assertEqual(verification.MAX_STARTUP_LOG_BYTES,
                         worker_module.MAX_STARTUP_LOG_BYTES)


# =========================================================================== #
# Timeout sourcing (M5R3-m1) and cancellation-ordering disposition (M5R3-m2). #
# =========================================================================== #


class TimeoutAndCancellationDispositionTests(unittest.TestCase):

    def test_startup_allowance_s_reads_the_request_from_request_path(self):
        with tempfile.TemporaryDirectory() as d:
            request_path = os.path.join(d, "request.json")
            state_store.write_json_atomic(
                request_path,
                {"timeout_policy": {"startup_allowance_s": 7}})
            self.assertEqual(
                worker_module._startup_allowance_s(request_path), 7)

    def test_startup_allowance_s_falls_back_exactly_like_the_base_spine(self):
        with tempfile.TemporaryDirectory() as d:
            request_path = os.path.join(d, "request.json")
            state_store.write_json_atomic(
                request_path, {"timeout_policy": {}})
            self.assertEqual(
                worker_module._startup_allowance_s(request_path),
                verification.DEFAULT_STARTUP_ALLOWANCE_S)

    def test_startup_allowance_s_falls_back_when_request_is_unreadable(self):
        missing_path = "/does/not/exist/request.json"
        self.assertEqual(
            worker_module._startup_allowance_s(missing_path),
            verification.DEFAULT_STARTUP_ALLOWANCE_S)

    def test_no_pre_existing_m1_m4_test_module_references_cancel_event(self):
        # M5R3-m2's evidence claim, verified directly rather than merely
        # asserted: a grep for `cancel_event` across all 28 pre-existing
        # regression modules returns zero matches, so the relocated
        # identity read's ordering change relative to the cancel watcher is
        # provably undetectable by the mandatory regression command.
        for module_name in REGRESSION_MODULES:
            rel_path = "scripts/%s.py" % module_name.split(".")[-1]
            path = os.path.join(_REPO_ROOT, rel_path)
            with open(path, "r") as fh:
                text = fh.read()
            self.assertNotIn(
                "cancel_event", text,
                "%s references cancel_event -- M5R3-m2's 'undetectable by "
                "any M1-M4 test' evidence no longer holds" % rel_path)

    def test_cancellation_delay_is_bounded_by_the_startup_allowance(self):
        # M5R3-m2's disposition: the identity read's relocation inside
        # spawn_worker bounds any cancellation delay during the startup
        # window by exactly the same startup_allowance_s/
        # DEFAULT_STARTUP_ALLOWANCE_S this module already honors -- never
        # unbounded. Proven directly: _read_worker_identity (the relocated
        # poll) never blocks past its own timeout_s, with a real clock.
        import time as time_mod
        start = time_mod.time()
        identity = worker_module._read_worker_identity(
            "S-bound-check", "T-bound-check", timeout_s=0.3,
            poll_delay_s=0.05)
        elapsed = time_mod.time() - start
        self.assertIsNone(identity)
        self.assertLess(elapsed, 2.0,
                        "the relocated identity read must remain bounded "
                        "by its own timeout_s, not block indefinitely")

    def test_spawn_worker_signature_is_not_widened_with_cancel_event(self):
        sig = inspect.signature(worker_module.spawn_worker)
        self.assertNotIn("cancel_event", sig.parameters)
        self.assertEqual(
            list(sig.parameters),
            ["python_executable", "checkout_root", "request_path",
             "session_uuid", "transaction_id"])


# =========================================================================== #
# should_defer_teardown: frozen False stub + real teardown-gating wiring.     #
# =========================================================================== #


class ZeroBehaviorChangeSeamTests(_RealWorkerFixture):

    def test_should_defer_teardown_stub_returns_false_unconditionally(self):
        self.assertIs(verification.should_defer_teardown(
            "any-session", "any-transaction", "any-label"), False)
        self.assertIs(verification.should_defer_teardown(None, None, None),
                      False)

    def _run_green_transaction_with_teardown_spies(self):
        calls = []
        real_cleanup = verification.cleanup_active_command_group
        real_terminate = verification.terminate_worker

        def spy_cleanup(*a, **k):
            calls.append("cleanup")
            return real_cleanup(*a, **k)

        def spy_terminate(*a, **k):
            calls.append("terminate")
            return real_terminate(*a, **k)

        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        with mock.patch.object(verification, "cleanup_active_command_group",
                               side_effect=spy_cleanup), \
             mock.patch.object(verification, "terminate_worker",
                               side_effect=spy_terminate):
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries)
        return result, calls

    def test_false_default_still_tears_down_every_time_zero_behavior_change(self):
        result, calls = self._run_green_transaction_with_teardown_spies()
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(calls, ["cleanup", "terminate"],
                         "the base commit's own unconditional teardown "
                         "must be unchanged while should_defer_teardown "
                         "returns False")

    def test_true_skips_the_finally_blocks_teardown_calls(self):
        with mock.patch.object(verification, "should_defer_teardown",
                               return_value=True):
            result, calls = self._run_green_transaction_with_teardown_spies()
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(
            calls, [],
            "cleanup_active_command_group/terminate_worker must be "
            "skipped for the whole transaction while should_defer_teardown "
            "returns True -- proving the seam's wiring is real, not dead "
            "code, even though Package A's own stub never returns True")


# =========================================================================== #
# overall_deadline computed before spawn_worker (M5A-R-M1).                   #
# =========================================================================== #


class OverallDeadlineOrderingTests(_RealWorkerFixture):
    """M5A-R-M1: `overall_deadline` is computed BEFORE `spawn_worker` is
    called, restoring the base commit's own ordering -- proven
    behaviorally, not merely by inspecting source order, since a purely
    textual/co_names check cannot distinguish "computed before" from
    "computed after" two adjacent statements."""

    def test_overall_deadline_clock_starts_before_spawn_worker_not_after(self):
        real_spawn_worker = verification.spawn_worker

        def slow_spawn_worker(*args, **kwargs):
            time.sleep(1.2)
            return real_spawn_worker(*args, **kwargs)

        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        with mock.patch.object(verification, "spawn_worker",
                              side_effect=slow_spawn_worker), \
             mock.patch.object(verification, "_overall_deadline_s",
                              return_value=1.0):
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries)
        # A 1-second overall budget, if computed BEFORE a spawn_worker call
        # that itself takes 1.2s, is already exhausted by the time the
        # entry loop's first deadline check runs immediately afterward --
        # this can ONLY happen if `time.time()` for `overall_deadline` was
        # sampled before the slow spawn, not after it (computing it AFTER
        # the 1.2s delay would instead leave the full 1-second budget
        # still fresh, and the trivial "pass" command would easily
        # complete within it, going GREEN).
        self.assertEqual(
            result["verdict"], verification.VERDICT_UNVERIFIED,
            "overall_deadline must be computed before spawn_worker's own "
            "delay is incurred, exactly like the base commit")

    def test_overall_deadline_s_is_referenced_before_spawn_worker_in_source(self):
        # Companion structural check: the source ORDER also matches (not a
        # substitute for the behavioral proof above, since source order
        # alone cannot prove which `time.time()` call actually executed
        # first at a control-flow level, but a useful, cheap regression
        # trip-wire against someone reordering the lines back).
        src = inspect.getsource(verification._run_owned_transaction)
        overall_deadline_pos = src.index("overall_deadline = time.time()")
        spawn_worker_pos = src.index("spawn_worker(\n")
        self.assertLess(
            overall_deadline_pos, spawn_worker_pos,
            "overall_deadline must be computed before the spawn_worker "
            "call site in _run_owned_transaction's source")


# =========================================================================== #
# Cancellation-ordering bound, end-to-end (M5R3-m2, strengthened).            #
# =========================================================================== #


class CancellationOrderingEndToEndTests(_RealWorkerFixture):
    """M5R3-m2's disposition, proven end-to-end against a REAL spawned
    worker -- not merely `_read_worker_identity` tested in isolation."""

    def test_cancel_set_before_launch_still_completes_bounded_and_unverified(self):
        cancel_event = threading.Event()
        cancel_event.set()
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        started = time.time()
        result = verification.run_transaction(
            self.repo, self.session_uuid, entries, cancel_event=cancel_event)
        elapsed = time.time() - started
        self.assertLess(
            elapsed, 20,
            "a cancel_event already set before the transaction starts must "
            "not block for anywhere near the full startup allowance")
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)

    def test_cancel_during_the_relocated_identity_read_is_bounded_and_torn_down(self):
        # A cancel_event set WHILE spawn_worker's own (relocated) identity
        # read is still in flight is observed once spawn_worker returns
        # and the entry loop's own check runs -- not mid-read (M5R3-m2) --
        # but the total delay stays bounded by the identity read's own
        # timeout_s, and teardown still completes: no orphaned process.
        cancel_event = threading.Event()
        real_read_identity = worker_module._read_worker_identity
        proc_holder = {}

        def slow_read_identity(session_uuid, transaction_id, timeout_s=10,
                              poll_delay_s=0.2, sleep=time.sleep,
                              now=time.time, proc=None):
            proc_holder["proc"] = proc
            cancel_event.set()
            time.sleep(0.5)
            return real_read_identity(
                session_uuid, transaction_id, timeout_s=timeout_s,
                poll_delay_s=poll_delay_s, sleep=sleep, now=now, proc=proc)

        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        started = time.time()
        with mock.patch.object(worker_module, "_read_worker_identity",
                              side_effect=slow_read_identity):
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries,
                cancel_event=cancel_event)
        elapsed = time.time() - started
        self.assertLess(
            elapsed, 10,
            "cancellation during the relocated identity read must remain "
            "bounded, never block for the full unrelated command's own "
            "timeout")
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)
        proc = proc_holder.get("proc")
        self.assertIsNotNone(proc, "the slow identity-read spy was never "
                             "invoked with a real proc handle")
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(
            proc.poll(), "the worker process must be torn down, never left "
            "orphaned, even though the cancel fired mid-identity-read")


class WorkerSubprocessMissingSeamSiblingsTests(unittest.TestCase):
    """A `--worker` subprocess spawned into a target repo checkout that
    lacks the two new seam-module siblings (exactly what
    `scripts/test_cowork.py`'s own `_seed_worker_into_repo` fixture seeds)
    must still load and run its approved inventory -- the resilient
    try/except import fallback near the top of `cowork_verification.py`."""

    def test_run_transaction_still_goes_green_without_seam_siblings(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        if prior is None:
            self.addCleanup(lambda: os.environ.pop(
                "COWORK_SESSIONS_ROOT", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__(
                "COWORK_SESSIONS_ROOT", prior))
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        dest_dir = os.path.join(repo, "scripts")
        os.makedirs(dest_dir, exist_ok=True)
        # Deliberately only the base commit's four dependencies -- no
        # cowork_verification_worker.py/cowork_verification_evidence.py.
        for name in ("cowork_verification.py", "cowork_state.py",
                    "cowork_policy.py", "cowork_ledger.py"):
            shutil.copyfile(os.path.join(_HERE, name),
                            os.path.join(dest_dir, name))
        subprocess.run(["git", "-C", repo, "add", "scripts"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "seed worker"],
                       check=True)
        session_uuid = "S-" + uuid.uuid4().hex[:8]
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        result = verification.run_transaction(repo, session_uuid, entries)
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertTrue(result["worker_identity_verified"])

    def test_seam_unavailable_fallback_actually_raises_when_called(self):
        # Behavioral, not structural: a genuinely isolated interpreter,
        # `sys.path`-rooted at a directory that holds ONLY the base
        # commit's four dependencies (no seam siblings), actually CALLS
        # `spawn_worker` (the `_seam_unavailable` fallback binding) and
        # must observe a clear, loud ImportError -- never a silent no-op,
        # and never some unrelated exception (NameError/AttributeError)
        # that would mean the fallback bound the wrong kind of object.
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        for name in ("cowork_verification.py", "cowork_state.py",
                    "cowork_policy.py", "cowork_ledger.py"):
            shutil.copyfile(os.path.join(_HERE, name), os.path.join(d, name))
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import cowork_verification as v\n"
            "try:\n"
            "    v.spawn_worker('python3', '/tmp', '/tmp/request.json')\n"
            "except ImportError as exc:\n"
            "    print('RAISED:' + str(exc))\n"
            "except Exception as exc:\n"
            "    print('WRONG_EXCEPTION_TYPE:' + type(exc).__name__)\n"
            "else:\n"
            "    print('DID_NOT_RAISE')\n"
        ) % d
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=30)
        self.assertIn("RAISED:", result.stdout,
                      "stdout=%r stderr=%r" % (result.stdout, result.stderr))
        self.assertIn(
            "cowork_verification_worker.py/cowork_verification_evidence.py",
            result.stdout)


class NarrowedImportErrorHandlingTests(unittest.TestCase):
    """The extraction-seam import fallback catches ONLY a
    `ModuleNotFoundError` naming exactly one of the two seam modules --
    never a blanket `ImportError` -- so a genuine bug inside a PRESENT seam
    module can never be silently misreported as 'the siblings are merely
    absent'."""

    def test_module_not_found_error_name_check_is_exact(self):
        src = inspect.getsource(verification)
        self.assertIn("except ModuleNotFoundError as _seam_import_error:",
                     src)
        self.assertIn("_seam_import_error.name not in _SEAM_MODULE_NAMES",
                     src)

    def test_unrelated_import_error_inside_a_present_seam_module_propagates(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        for name in ("cowork_verification.py", "cowork_state.py",
                    "cowork_policy.py", "cowork_ledger.py",
                    "cowork_verification_evidence.py"):
            shutil.copyfile(os.path.join(_HERE, name), os.path.join(d, name))
        # A deliberately BROKEN worker module: present on sys.path (so
        # `ModuleNotFoundError.name` would never match either seam module's
        # OWN name), but itself fails to import something unrelated -- this
        # must propagate, never be silently swallowed as "the sibling
        # files are merely absent".
        with open(os.path.join(d, "cowork_verification_worker.py"), "w") as fh:
            fh.write("import this_module_does_not_exist_at_all_xyz\n")
        script = ("import sys\nsys.path.insert(0, %r)\n"
                  "import cowork_verification\n") % d
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(
            result.returncode, 0,
            "an unrelated ModuleNotFoundError inside a PRESENT seam module "
            "must propagate, not be silently swallowed by the narrowed "
            "fallback")
        self.assertIn("this_module_does_not_exist_at_all_xyz", result.stderr)
        self.assertNotIn("Cowork tool source", result.stderr,
                         "the unrelated error must not be misreported as "
                         "the seam-siblings-absent fallback message")


class DriftProtectedFallbackConstantTests(unittest.TestCase):
    """The `MAX_STARTUP_LOG_BYTES` fallback literal bound inside
    `cowork_verification.py`'s own except-branch (used only when the seam
    siblings are genuinely absent, so it cannot simply import the real
    constant) must never silently drift from
    `cowork_verification_worker.MAX_STARTUP_LOG_BYTES`'s own definition."""

    def test_fallback_literal_matches_the_real_constant(self):
        src = inspect.getsource(verification)
        match = re.search(
            r"^\s*MAX_STARTUP_LOG_BYTES = ([0-9 *]+)\s*$", src, re.MULTILINE)
        self.assertIsNotNone(
            match, "fallback MAX_STARTUP_LOG_BYTES literal not found in "
            "cowork_verification.py's except-branch")
        fallback_value = eval(match.group(1), {"__builtins__": {}}, {})
        self.assertEqual(
            fallback_value, worker_module.MAX_STARTUP_LOG_BYTES,
            "the except-branch fallback constant has drifted from "
            "cowork_verification_worker.MAX_STARTUP_LOG_BYTES's real value")


# =========================================================================== #
# CheckpointRequest/CheckpointResult/CheckpointReceipt: schema/version/       #
# unknown-key validators.                                                     #
# =========================================================================== #


def _valid_request(**overrides):
    base = {
        "checkpoint_schema_version": verification.CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": "cp-" + uuid.uuid4().hex[:8],
        "session_uuid": "S-" + uuid.uuid4().hex[:8],
        "phase": "RED",
        "candidate_digest": "deadbeef" * 8,
        "argv": ["python3", "-c", "pass"],
        "cwd": "/tmp/checkout",
        "mutation_class": verification.MUTATION_CLASS_READ_ONLY,
        "status": verification.CHECKPOINT_STATUS_REQUIRED,
    }
    base.update(overrides)
    return base


def _valid_result(**overrides):
    base = {
        "checkpoint_schema_version": verification.CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": "cp-1",
        "executor_identity": "builder-executor-1",
        "argv": ["python3", "-c", "pass"],
        "cwd": "/tmp/checkout",
        "exit_code": 0,
        "evidence_state": verification.EVIDENCE_PRESENT,
    }
    base.update(overrides)
    return base


def _valid_receipt(**overrides):
    base = {
        "checkpoint_schema_version": verification.CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": "cp-1",
        "session_uuid": "S-1",
        "phase": "RED",
        "candidate_digest": "deadbeef" * 8,
        "verdict": verification.CHECKPOINT_ACCEPTED,
        "terminal": True,
    }
    base.update(overrides)
    return base


class CheckpointRequestSchemaTests(unittest.TestCase):

    def test_accepts_a_well_formed_request(self):
        normalized = verification.normalize_checkpoint_request(
            _valid_request())
        self.assertEqual(normalized["mutation_class"],
                         verification.MUTATION_CLASS_READ_ONLY)
        self.assertEqual(normalized["status"],
                         verification.CHECKPOINT_STATUS_REQUIRED)
        self.assertEqual(normalized["declared_output_paths"], [])

    def test_rejects_wrong_schema_version(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(checkpoint_schema_version=999))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_schema_version_mismatch")

    def test_rejects_unknown_key(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(unexpected_field="surprise"))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_unknown_key")

    def test_rejects_missing_required_key(self):
        raw = _valid_request()
        del raw["phase"]
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(raw)
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_missing_key")

    def test_rejects_bad_mutation_class(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(mutation_class="destructive"))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_bad_mutation_class")

    def test_rejects_bad_status(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(status="whenever"))
        self.assertEqual(ctx.exception.code, "checkpoint_request_bad_status")

    def test_live_candidate_requires_declared_output_paths(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(_valid_request(
                mutation_class=verification.MUTATION_CLASS_LIVE_CANDIDATE))
        self.assertEqual(
            ctx.exception.code,
            "checkpoint_request_live_candidate_needs_output_paths")

    def test_live_candidate_with_declared_output_paths_is_accepted(self):
        normalized = verification.normalize_checkpoint_request(_valid_request(
            mutation_class=verification.MUTATION_CLASS_LIVE_CANDIDATE,
            declared_output_paths=["src/foo.py"]))
        self.assertEqual(normalized["declared_output_paths"], ["src/foo.py"])

    def test_read_only_rejects_declared_output_paths(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(_valid_request(
                declared_output_paths=["src/foo.py"]))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_unauthorized_output_paths")

    def test_rejects_relative_cwd(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(cwd="relative/path"))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_relative_cwd")

    def test_rejects_non_string_env_values(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(env={"FLAG": 1}))
        self.assertEqual(ctx.exception.code, "checkpoint_request_bad_env")

    def test_rejects_non_positive_timeout(self):
        with self.assertRaises(verification.CheckpointError):
            verification.normalize_checkpoint_request(
                _valid_request(timeout_s=0))


class CheckpointResultSchemaTests(unittest.TestCase):

    def test_accepts_a_well_formed_result(self):
        normalized = verification.normalize_checkpoint_result(
            _valid_result())
        self.assertEqual(normalized["evidence_state"],
                         verification.EVIDENCE_PRESENT)

    def test_rejects_wrong_schema_version(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_result(
                _valid_result(checkpoint_schema_version=0))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_schema_version_mismatch")

    def test_rejects_unknown_key(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_result(
                _valid_result(unexpected="x"))
        self.assertEqual(ctx.exception.code, "checkpoint_result_unknown_key")

    def test_rejects_bad_evidence_state(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_result(
                _valid_result(evidence_state="maybe"))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_bad_evidence_state")

    def test_rejects_non_int_exit_code(self):
        with self.assertRaises(verification.CheckpointError):
            verification.normalize_checkpoint_result(
                _valid_result(exit_code="0"))


class CheckpointReceiptSchemaTests(unittest.TestCase):

    def test_accepts_a_well_formed_accepted_receipt(self):
        normalized = verification.normalize_checkpoint_receipt(
            _valid_receipt())
        self.assertEqual(normalized["verdict"], verification.CHECKPOINT_ACCEPTED)
        self.assertTrue(normalized["terminal"])

    def test_rejects_unknown_key(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_receipt(
                _valid_receipt(unexpected="x"))
        self.assertEqual(ctx.exception.code, "checkpoint_receipt_unknown_key")

    def test_rejects_non_terminal_receipt(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_receipt(
                _valid_receipt(terminal=False))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_receipt_not_terminal")

    def test_rejected_verdict_requires_a_reason(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_receipt(_valid_receipt(
                verdict=verification.CHECKPOINT_REJECTED))
        self.assertEqual(
            ctx.exception.code,
            "checkpoint_receipt_missing_rejection_reason")

    def test_rejected_verdict_with_reason_is_accepted(self):
        normalized = verification.normalize_checkpoint_receipt(_valid_receipt(
            verdict=verification.CHECKPOINT_REJECTED,
            rejection_reason="wrong_argv"))
        self.assertEqual(normalized["rejection_reason"], "wrong_argv")


# =========================================================================== #
# Path traversal.                                                             #
# =========================================================================== #


class CheckpointPathTraversalTests(unittest.TestCase):

    def test_rejects_traversal_in_cwd(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(
                _valid_request(cwd="/tmp/checkout/../../etc"))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_path_traversal")

    def test_rejects_traversal_in_declared_output_paths(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_request(_valid_request(
                mutation_class=verification.MUTATION_CLASS_LIVE_CANDIDATE,
                declared_output_paths=["../outside.py"]))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_request_path_traversal")

    def test_rejects_traversal_in_result_generated_paths(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_result(
                _valid_result(generated_paths=["../../secrets.txt"]))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_path_traversal")

    def test_rejects_traversal_in_result_output_paths(self):
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.normalize_checkpoint_result(
                _valid_result(output_paths=["a/../../b.py"]))
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_path_traversal")

    def test_accepts_ordinary_relative_paths(self):
        normalized = verification.normalize_checkpoint_result(
            _valid_result(generated_paths=["build/out.txt"],
                         output_paths=["build/out.txt"]))
        self.assertEqual(normalized["generated_paths"], ["build/out.txt"])


# =========================================================================== #
# Cross-check: CheckpointResult against its CheckpointRequest.                #
# =========================================================================== #


class CheckpointResultCrossCheckTests(unittest.TestCase):

    def _request(self, **overrides):
        return verification.normalize_checkpoint_request(
            _valid_request(**overrides))

    def _result(self, **overrides):
        return verification.normalize_checkpoint_result(
            _valid_result(**overrides))

    def test_matching_result_passes(self):
        request = self._request(checkpoint_id="cp-x")
        result = self._result(checkpoint_id="cp-x")
        self.assertIsNone(
            verification.validate_checkpoint_result_against_request(
                result, request))

    def test_wrong_argv_is_rejected(self):
        request = self._request(checkpoint_id="cp-x")
        result = self._result(checkpoint_id="cp-x",
                              argv=["python3", "-c", "print(1)"])
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.validate_checkpoint_result_against_request(
                result, request)
        self.assertEqual(ctx.exception.code, "checkpoint_result_wrong_argv")

    def test_wrong_cwd_is_rejected(self):
        request = self._request(checkpoint_id="cp-x")
        result = self._result(checkpoint_id="cp-x", cwd="/tmp/somewhere-else")
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.validate_checkpoint_result_against_request(
                result, request)
        self.assertEqual(ctx.exception.code, "checkpoint_result_wrong_cwd")

    def test_over_broad_output_paths_are_rejected(self):
        request = self._request(
            checkpoint_id="cp-x",
            mutation_class=verification.MUTATION_CLASS_LIVE_CANDIDATE,
            declared_output_paths=["src/allowed.py"])
        result = self._result(checkpoint_id="cp-x",
                              output_paths=["src/allowed.py", "src/rogue.py"])
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.validate_checkpoint_result_against_request(
                result, request)
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_over_broad_output")

    def test_unauthorized_mutation_on_read_only_is_rejected(self):
        request = self._request(checkpoint_id="cp-x")
        result = self._result(checkpoint_id="cp-x", mutation_detected=True)
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.validate_checkpoint_result_against_request(
                result, request)
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_unauthorized_mutation")

    def test_unauthorized_mutation_on_isolated_is_rejected(self):
        request = self._request(
            checkpoint_id="cp-x", mutation_class=verification.MUTATION_CLASS_ISOLATED)
        result = self._result(checkpoint_id="cp-x", mutation_detected=True)
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.validate_checkpoint_result_against_request(
                result, request)
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_unauthorized_mutation")

    def test_declared_mutation_on_live_candidate_is_authorized(self):
        request = self._request(
            checkpoint_id="cp-x",
            mutation_class=verification.MUTATION_CLASS_LIVE_CANDIDATE,
            declared_output_paths=["src/allowed.py"])
        result = self._result(checkpoint_id="cp-x", mutation_detected=True,
                              output_paths=["src/allowed.py"])
        self.assertIsNone(
            verification.validate_checkpoint_result_against_request(
                result, request))

    def test_wrong_checkpoint_id_is_rejected(self):
        request = self._request(checkpoint_id="cp-x")
        result = self._result(checkpoint_id="cp-y")
        with self.assertRaises(verification.CheckpointError) as ctx:
            verification.validate_checkpoint_result_against_request(
                result, request)
        self.assertEqual(ctx.exception.code,
                         "checkpoint_result_wrong_checkpoint_id")


# =========================================================================== #
# Claim/lease: once-only claim, once-only terminal publication.               #
# =========================================================================== #


class CheckpointClaimLeaseTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self.root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.checkpoint_id = "cp-" + uuid.uuid4().hex[:8]

    def test_fresh_claim_succeeds(self):
        ok, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(ok)
        self.assertEqual(record["executor_identity"], "executor-1")
        self.assertEqual(record["state"], verification.CHECKPOINT_CLAIM_CLAIMED)

    def test_duplicate_claim_by_a_different_executor_is_rejected(self):
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        ok, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-2")
        self.assertFalse(ok)
        self.assertEqual(record["executor_identity"], "executor-1")

    def test_duplicate_claim_by_the_same_executor_is_also_rejected(self):
        # Once-only: even the SAME identity re-claiming is rejected, never
        # silently re-granted or double-executed.
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        ok, record = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertFalse(ok)

    def test_publish_receipt_marks_the_claim_terminal(self):
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        ok, published = verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(ok)
        self.assertEqual(published["checkpoint_id"], self.checkpoint_id)
        claim = state_store.read_json_tolerant(
            state_store.checkpoint_claim_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertEqual(claim["state"], verification.CHECKPOINT_CLAIM_TERMINAL)

    def test_second_publish_attempt_is_rejected_never_overwritten(self):
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        first = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid,
            verdict=verification.CHECKPOINT_ACCEPTED))
        verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, first)
        second = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid,
            verdict=verification.CHECKPOINT_REJECTED,
            rejection_reason="late_relaunch"))
        ok, existing = verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, second)
        self.assertFalse(ok)
        self.assertEqual(existing["verdict"], verification.CHECKPOINT_ACCEPTED)
        on_disk = state_store.read_json_tolerant(
            state_store.checkpoint_receipt_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertEqual(on_disk["verdict"], verification.CHECKPOINT_ACCEPTED)

    def test_publish_requires_a_normalized_terminal_receipt(self):
        with self.assertRaises(verification.CheckpointError):
            verification.publish_checkpoint_receipt(
                self.session_uuid, self.checkpoint_id, {"terminal": False})

    def test_concurrent_claimants_exactly_one_wins(self):
        # M5A-R-M3: a REAL concurrency race (threads racing the identical
        # checkpoint_id), not merely a sequential double-call -- proving the
        # kernel-exclusive `os.O_CREAT | os.O_EXCL` creation actually
        # serializes concurrent claimants, rather than a read-then-write
        # check-and-set that could let two winners through under real
        # contention.
        winners = []
        winners_lock = threading.Lock()
        claimant_count = 12
        barrier = threading.Barrier(claimant_count)

        def attempt(i):
            barrier.wait()
            ok, _record = verification.claim_checkpoint(
                self.session_uuid, self.checkpoint_id, "executor-%d" % i)
            if ok:
                with winners_lock:
                    winners.append(i)

        threads = [threading.Thread(target=attempt, args=(i,))
                  for i in range(claimant_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(winners), 1,
                         "exactly one concurrent claimant must win, got %r"
                         % winners)
        claim = state_store.read_json_tolerant(
            state_store.checkpoint_claim_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertEqual(claim["executor_identity"],
                         "executor-%d" % winners[0])

    def test_claim_creation_uses_a_kernel_exclusive_primitive(self):
        src = inspect.getsource(verification.claim_checkpoint)
        self.assertIn("_create_checkpoint_claim_exclusive", src)
        create_src = inspect.getsource(
            verification._create_checkpoint_claim_exclusive)
        self.assertIn("O_EXCL", create_src)
        self.assertIn("O_CREAT", create_src)

    def test_receipt_write_failure_leaves_the_claim_retryable(self):
        # M5A-R-M2: if the RECEIPT write itself fails, nothing durable has
        # changed -- the claim must still read back as "claimed", never
        # falsely "terminal", so a retry can still succeed.
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        receipt_path = state_store.checkpoint_receipt_path_for(
            self.session_uuid, self.checkpoint_id)
        real_write = state_store.write_json_atomic_durable

        def fail_only_receipt(path, data):
            if path == receipt_path:
                return False
            return real_write(path, data)

        with mock.patch.object(state_store, "write_json_atomic_durable",
                              side_effect=fail_only_receipt):
            ok, result = verification.publish_checkpoint_receipt(
                self.session_uuid, self.checkpoint_id, receipt)
        self.assertFalse(ok)
        self.assertIsNone(result)
        claim = state_store.read_json_tolerant(
            state_store.checkpoint_claim_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertEqual(claim["state"], verification.CHECKPOINT_CLAIM_CLAIMED,
                         "a failed receipt write must never leave the "
                         "claim falsely marked terminal")
        self.assertIsNone(state_store.read_json_tolerant(receipt_path))
        ok2, published = verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(ok2)
        self.assertEqual(published["checkpoint_id"], self.checkpoint_id)

    def test_claim_write_failure_after_receipt_success_is_retryable(self):
        # M5A-R-M2: if the receipt write succeeds but the CLAIM write then
        # fails, the receipt is already durably on disk -- a retry must not
        # lose or corrupt it, and must complete the terminal marker.
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        claim_path = state_store.checkpoint_claim_path_for(
            self.session_uuid, self.checkpoint_id)
        real_write = state_store.write_json_atomic_durable

        def fail_only_claim(path, data):
            if path == claim_path:
                return False
            return real_write(path, data)

        with mock.patch.object(state_store, "write_json_atomic_durable",
                              side_effect=fail_only_claim):
            ok, result = verification.publish_checkpoint_receipt(
                self.session_uuid, self.checkpoint_id, receipt)
        self.assertFalse(ok)
        self.assertIsNone(result)
        on_disk_receipt = state_store.read_json_tolerant(
            state_store.checkpoint_receipt_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertIsNotNone(on_disk_receipt,
                            "the receipt must already be durable even "
                            "though the claim write failed")
        self.assertEqual(on_disk_receipt["checkpoint_id"], self.checkpoint_id)
        claim = state_store.read_json_tolerant(claim_path)
        self.assertEqual(claim["state"], verification.CHECKPOINT_CLAIM_CLAIMED)
        ok2, published = verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(ok2)
        claim2 = state_store.read_json_tolerant(claim_path)
        self.assertEqual(claim2["state"], verification.CHECKPOINT_CLAIM_TERMINAL)

    def test_publish_writes_the_receipt_before_the_claim(self):
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        order = []
        real_write = state_store.write_json_atomic_durable
        claim_path = state_store.checkpoint_claim_path_for(
            self.session_uuid, self.checkpoint_id)
        receipt_path = state_store.checkpoint_receipt_path_for(
            self.session_uuid, self.checkpoint_id)

        def spy_write(path, data):
            if path == claim_path:
                order.append("claim")
            elif path == receipt_path:
                order.append("receipt")
            return real_write(path, data)

        with mock.patch.object(state_store, "write_json_atomic_durable",
                              side_effect=spy_write):
            ok, _published = verification.publish_checkpoint_receipt(
                self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(ok)
        self.assertEqual(order, ["receipt", "claim"])

    def test_publish_uses_the_durable_write_primitive_not_the_plain_one(self):
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        with mock.patch.object(
                state_store, "write_json_atomic") as plain_write, \
             mock.patch.object(
                state_store, "write_json_atomic_durable",
                side_effect=state_store.write_json_atomic_durable
                ) as durable_write:
            ok, _published = verification.publish_checkpoint_receipt(
                self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(ok)
        plain_write.assert_not_called()
        self.assertEqual(durable_write.call_count, 2)

    def test_simulated_crash_between_the_two_writes_is_recovered_by_retry(self):
        # M5A-R-M2 crash-safety, simulated directly at the artifact level:
        # a receipt is durably on disk, the claim is still "claimed" (as if
        # the process crashed after the first write and before the
        # second) -- resuming and calling publish again must complete
        # cleanly.
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        state_store.write_json_atomic_durable(
            state_store.checkpoint_receipt_path_for(
                self.session_uuid, self.checkpoint_id), receipt)
        claim = state_store.read_json_tolerant(
            state_store.checkpoint_claim_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertEqual(claim["state"], verification.CHECKPOINT_CLAIM_CLAIMED)
        ok, published = verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, receipt)
        self.assertTrue(ok)
        self.assertEqual(published, receipt)
        claim_after = state_store.read_json_tolerant(
            state_store.checkpoint_claim_path_for(
                self.session_uuid, self.checkpoint_id))
        self.assertEqual(claim_after["state"],
                         verification.CHECKPOINT_CLAIM_TERMINAL)


# =========================================================================== #
# Crash-safe state reconstruction (artifacts alone).                          #
# =========================================================================== #


class CheckpointCrashSafeStateTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self.root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.checkpoint_id = "cp-" + uuid.uuid4().hex[:8]

    def test_unknown_checkpoint_reconstructs_as_unknown(self):
        state = verification.reconstruct_checkpoint_state(
            self.session_uuid, "cp-never-existed")
        self.assertEqual(state["state"], "unknown")
        self.assertIsNone(state["request"])

    def test_request_only_reconstructs_as_pending(self):
        state_store.write_json_atomic(
            state_store.checkpoint_request_path_for(
                self.session_uuid, self.checkpoint_id),
            _valid_request(checkpoint_id=self.checkpoint_id,
                          session_uuid=self.session_uuid))
        state = verification.reconstruct_checkpoint_state(
            self.session_uuid, self.checkpoint_id)
        self.assertEqual(state["state"], "pending")
        self.assertIsNotNone(state["request"])
        self.assertIsNone(state["claim"])

    def test_crash_between_claim_and_publish_reconstructs_as_claimed(self):
        # The exact "crash between claim and terminal publish" negative
        # control this package's state machine must support: a claim exists
        # on disk, no receipt was ever published (the process crashed
        # mid-execution) -- resume must see "claimed", never "terminal" and
        # never silently lose the claim.
        state_store.write_json_atomic(
            state_store.checkpoint_request_path_for(
                self.session_uuid, self.checkpoint_id),
            _valid_request(checkpoint_id=self.checkpoint_id,
                          session_uuid=self.session_uuid))
        ok, _claim = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        self.assertTrue(ok)
        state = verification.reconstruct_checkpoint_state(
            self.session_uuid, self.checkpoint_id)
        self.assertEqual(state["state"], "claimed")
        self.assertIsNone(state["receipt"])

    def test_terminal_checkpoint_reconstructs_as_terminal_exactly_once(self):
        state_store.write_json_atomic(
            state_store.checkpoint_request_path_for(
                self.session_uuid, self.checkpoint_id),
            _valid_request(checkpoint_id=self.checkpoint_id,
                          session_uuid=self.session_uuid))
        verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-1")
        receipt = verification.normalize_checkpoint_receipt(_valid_receipt(
            checkpoint_id=self.checkpoint_id, session_uuid=self.session_uuid))
        verification.publish_checkpoint_receipt(
            self.session_uuid, self.checkpoint_id, receipt)
        state = verification.reconstruct_checkpoint_state(
            self.session_uuid, self.checkpoint_id)
        self.assertEqual(state["state"], "terminal")
        self.assertIsNotNone(state["receipt"])
        # Re-claiming after terminal is still rejected (once-only, forever).
        ok, existing = verification.claim_checkpoint(
            self.session_uuid, self.checkpoint_id, "executor-2")
        self.assertFalse(ok)
        self.assertEqual(existing["state"],
                         verification.CHECKPOINT_CLAIM_TERMINAL)

    def test_partially_written_json_never_raises_on_reconstruction(self):
        # Simulate a crash mid-write: a request file with truncated/corrupt
        # bytes must be treated as absent, never crash the reconstruction.
        path = state_store.checkpoint_request_path_for(
            self.session_uuid, self.checkpoint_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not valid json")
        state = verification.reconstruct_checkpoint_state(
            self.session_uuid, self.checkpoint_id)
        self.assertEqual(state["state"], "unknown")


# =========================================================================== #
# cowork_state.py additive path helpers.                                      #
# =========================================================================== #


class CheckpointStatePathHelperTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self.root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]

    def test_checkpoint_paths_are_distinct_per_checkpoint_id(self):
        p1 = state_store.checkpoint_request_path_for(self.session_uuid, "a")
        p2 = state_store.checkpoint_request_path_for(self.session_uuid, "b")
        self.assertNotEqual(p1, p2)

    def test_checkpoint_paths_are_distinct_per_artifact(self):
        cid = "cp-1"
        paths = {
            state_store.checkpoint_request_path_for(self.session_uuid, cid),
            state_store.checkpoint_claim_path_for(self.session_uuid, cid),
            state_store.checkpoint_result_path_for(self.session_uuid, cid),
            state_store.checkpoint_receipt_path_for(self.session_uuid, cid),
        }
        self.assertEqual(len(paths), 4)

    def test_list_checkpoint_ids_is_empty_for_a_fresh_session(self):
        self.assertEqual(
            state_store.list_checkpoint_ids(self.session_uuid), [])

    def test_list_checkpoint_ids_finds_every_checkpoint_with_a_request(self):
        for cid in ("cp-a", "cp-b", "cp-c"):
            state_store.write_json_atomic(
                state_store.checkpoint_request_path_for(
                    self.session_uuid, cid),
                {"checkpoint_id": cid})
        self.assertEqual(
            state_store.list_checkpoint_ids(self.session_uuid),
            ["cp-a", "cp-b", "cp-c"])

    def test_list_checkpoint_ids_ignores_a_dir_with_no_request(self):
        os.makedirs(os.path.join(
            state_store.checkpoint_root_for(self.session_uuid), "cp-empty"),
            exist_ok=True)
        self.assertEqual(
            state_store.list_checkpoint_ids(self.session_uuid), [])

    def test_current_checkpoint_pointer_path_keyed_by_work_id(self):
        p1 = state_store.current_checkpoint_pointer_path_for(
            self.session_uuid, "W-1")
        p2 = state_store.current_checkpoint_pointer_path_for(
            self.session_uuid, "W-2")
        self.assertNotEqual(p1, p2)

    def test_tool_snapshot_paths_are_distinct_from_target_repo_snapshot(self):
        self.assertNotEqual(
            state_store.verification_tool_snapshot_root_for(self.session_uuid),
            state_store.verification_snapshot_root_for(self.session_uuid))
        self.assertNotEqual(
            state_store.verification_tool_snapshot_manifest_path_for(
                self.session_uuid, "T-1"),
            state_store.verification_snapshot_manifest_path_for(
                self.session_uuid, "T-1"))

    def test_tool_snapshot_object_path_shards_by_prefix(self):
        digest = "ab" + "c" * 62
        path = state_store.verification_tool_snapshot_object_path(
            self.session_uuid, digest)
        self.assertTrue(path.endswith(os.path.join("ab", digest)))

    def test_deferred_reconciliation_path_is_per_transaction(self):
        p1 = state_store.verification_deferred_reconciliation_path_for(
            self.session_uuid, "T-1")
        p2 = state_store.verification_deferred_reconciliation_path_for(
            self.session_uuid, "T-2")
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.startswith(
            state_store.verification_transaction_dir(self.session_uuid, "T-1")))


if __name__ == "__main__":
    unittest.main()
