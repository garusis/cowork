#!/usr/bin/env python3
"""Focused suite for M5 Package B: immutable worker capture and startup
identity -- garusis/cowork-internal#44, filling the frozen
`resolve_worker_source`/`spawn_worker` seam Package A reserved in
`cowork_verification_worker.py`.

M5B v3 (bounded successor to v2, garusis/cowork-internal#44 review round 2):
mechanically adopts v2's worker-capture bytes unchanged, then closes the two
independent-review majors v2's own two-path allowlist authority could not
reach -- M5B-R-M1 (the legacy worker-crash fixture, `scripts/test_cowork.py`)
and M5B-R-M2 (reclaiming the per-transaction captured tool-snapshot checkout,
`scripts/cowork_verification.py`'s worker-seam import block and
`run_transaction` lifecycle). This file's own allowlist/exclusion/named-region
tests below are widened accordingly, from v2's two-path scope to v3's frozen
four-path scope -- see `ALLOWED_CHANGED_PATHS` and the named-region tests.

MINOR DISPOSITIONS (M5B-R-m1 through m8, from the v2 review, SHA-256
9865b9d07f051e84d7bd51991b85e34b7272876ac0dbb6deb4b094822088e9b7):

  m1 FIXED -- worker_identity_mismatch now carries exit_code/log_tail too
     (cowork_verification_worker._classify_worker_startup); proven by
     IdentityMismatchTests.

  m2 FIXED (documentation only) -- _classify_worker_startup's own
     docstring now states plainly that an OSError from
     resolve_worker_source/_materialize_tool_snapshot, not only from
     Popen, also surfaces as worker_spawn_failed via the spine's except
     OSError handler; behavior is intentionally UNCHANGED (widening it
     would be a second, independent production behavior change this
     package does not own).

  m3 CARRIED, nonblocking -- _build_installation_manifest still silently
     skips an unreadable non-entry *.py file. A full fix would require
     introspecting the worker's actual runtime import graph (which module
     it would fail on, if any) purely to pick a more specific failure
     reason; the transaction still correctly, safely lands on UNVERIFIED
     either way (via worker_exited_before_identity_report rather than
     worker_source_missing), so this is a diagnostic-precision gap, not a
     correctness gap, and out of proportion to fix here.

  m4 FIXED -- symlinked *.py files (entry point or not) are now excluded
     from the captured manifest rather than silently mis-captured via
     os.path.isfile's symlink-following (_iter_installation_source_files);
     proven by MinorDispositionTests.

  m5 FIXED -- _materialize_tool_snapshot now starts from a clean checkout
     directory (rmtree first), exactly like the spine's own
     materialize_command_checkout; proven by MinorDispositionTests.

  m6 CARRIED, nonblocking -- all installation *.py files are still hashed
     and copied twice per transaction (object store, then checkout) even
     though only cowork_verification.py participates in the identity
     check. This is the design's own deliberate provenance/evidence
     tradeoff (the whole installation is captured, not merely the one
     file checked), not an oversight; a partial-capture optimization is a
     larger, riskier redesign out of proportion to a minor.

  m7 CARRIED, not applicable to this candidate's writable scope -- the gate
     PASS/FAIL ledger durability gap (standalone controller-gates.json vs.
     the package's own state.json/events.jsonl) is orchestration-state
     plumbing, not a scripts/ path in this candidate's four-path
     allowlist; it is the supervisor's concern, not this candidate's.

  m8 CARRIED, correctly time-scoped -- test_head_is_bound_to_the_signed_
     post_package_a_base's hardcoded HEAD==BASE_SHA assertion is exactly
     right for THIS candidate's own gate-time verification (proving the
     worktree sits at the exact signed base before integration); its
     self-invalidation once/if ever committed onto a different HEAD is a
     concern for that later, out-of-scope integration step, not for this
     bounded candidate.

Never invokes a real Claude, Codex, or OpenCode session; every fixture that
needs a real subprocess spawns a bare `python3 -c ...` (or the real worker
entry point) inside a throwaway git repo / session root, exactly like
`scripts/test_cowork.py` and `scripts/test_m5_package_a_contracts.py`'s own
owned-verification fixtures.

Run standalone:

    python3 -m unittest scripts/test_m5_package_b_worker_capture.py -v
"""

import ast
import hashlib
import os
import py_compile
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

# The exact post-Package-A signed base this package's frozen brief is bound
# to (the commit this worktree's HEAD already sits at, uncommitted changes
# on top).
BASE_SHA = "eae4276d07a887a041177817221bf1b0bcdf99f0"

# This package's own exact, frozen v3 four-path allowlist (widened from v2's
# two-path allowlist by the v3 frozen brief, to close M5B-R-M1/M5B-R-M2).
# Two of these four are only PARTIALLY writable -- see
# `NamedRegionScopeTests` below for the named-region check that enforces the
# narrower, sub-file scope the brief actually grants for each.
ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/cowork_verification_worker.py",
    "scripts/cowork_verification.py",
    "scripts/test_m5_package_b_worker_capture.py",
    "scripts/test_cowork.py",
})

# Every path the frozen brief explicitly excludes (read-only for this
# package) -- must remain byte-identical to the signed base. NOTE: v2's own
# EXCLUDED_PATHS also listed `scripts/cowork_verification.py` and
# `scripts/test_cowork.py` -- both are removed here because the v3 frozen
# brief explicitly, narrowly re-authorizes each (see NamedRegionScopeTests),
# closing M5B-R-M1/M5B-R-M2. This is a disclosed, brief-authorized widening
# of what v2's own candidate-local structural assertions rejected, not a
# silent relaxation.
EXCLUDED_PATHS = (
    "scripts/cowork_verification_evidence.py",
    "scripts/cowork_state.py",
    "scripts/cowork.py",
    "scripts/cowork_handoff.py",
    "scripts/cowork_ledger.py",
    "scripts/cowork_measure.py",
)


def _git_changed_paths():
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "."],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    untracked = [line[3:] for line in status if line.startswith("?? ")]
    return {p.strip() for p in (tracked + untracked) if p.strip()}


def _git_show(rev, rel_path):
    result = subprocess.run(
        ["git", "show", "%s:%s" % (rev, rel_path)],
        cwd=_REPO_ROOT, capture_output=True, check=True)
    return result.stdout


def _sha256_file(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _init_git_repo(seed_cowork_source=False, garbage_worker_source=False):
    """A throwaway committed git repo -- by default containing only a
    single unrelated tracked file, deliberately NOT `scripts/
    cowork_verification.py` or any other Cowork source at all, so a green
    transaction against it is only possible if the worker never needed
    anything from this repo's own tree in the first place."""
    d = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "config", "commit.gpgsign", "false"],
                   check=True)
    with open(os.path.join(d, "f.txt"), "w") as fh:
        fh.write("x")
    if garbage_worker_source:
        scripts_dir = os.path.join(d, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        with open(os.path.join(scripts_dir, "cowork_verification.py"),
                 "w") as fh:
            fh.write("raise SystemExit("
                     "'PACKAGE_B_NEGATIVE_CONTROL: this target-repo copy "
                     "must never be executed')\n")
    elif seed_cowork_source:
        scripts_dir = os.path.join(d, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        for name in ("cowork_verification.py", "cowork_state.py",
                    "cowork_policy.py", "cowork_ledger.py"):
            shutil.copyfile(os.path.join(_HERE, name),
                            os.path.join(scripts_dir, name))
    subprocess.run(["git", "-C", d, "add", "-A"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


class _SessionFixture(unittest.TestCase):
    """Shared fixture: an isolated `COWORK_SESSIONS_ROOT` and a fresh
    `session_uuid` -- nothing else. Individual tests build whatever
    request.json/target-repo state they need."""

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
        self.sessions_root = root
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]

    def _write_request(self, transaction_id, protocol_version=None,
                       inventory=None):
        request = {
            "protocol_version": (verification.PROTOCOL_VERSION
                                if protocol_version is None
                                else protocol_version),
            "session_uuid": self.session_uuid,
            "transaction_id": transaction_id,
            "inventory": inventory if inventory is not None else [],
            "timeout_policy": {},
        }
        request_path = state_store.verification_request_path_for(
            self.session_uuid, transaction_id)
        state_store.write_json_atomic(request_path, request)
        return request_path

    def _close_result(self, result):
        proc, write_fd, capture_thread = result
        try:
            os.close(write_fd)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if capture_thread is not None:
            capture_thread.join(timeout=5)


# =========================================================================== #
# Allowlist, py_compile, hash, and integrity gates.                           #
# =========================================================================== #


class AllowlistAndHashTests(unittest.TestCase):

    def test_changed_paths_are_within_the_four_path_allowlist(self):
        offenders = _git_changed_paths() - ALLOWED_CHANGED_PATHS
        self.assertFalse(
            offenders,
            "paths changed outside the frozen v3 four-path allowlist: %s"
            % sorted(offenders))

    def test_all_owned_paths_py_compile(self):
        for rel in sorted(ALLOWED_CHANGED_PATHS):
            path = os.path.join(_REPO_ROOT, rel)
            self.assertTrue(os.path.exists(path), "missing owned path: %s"
                            % rel)
            py_compile.compile(path, doraise=True)

    def test_candidate_hashes_are_well_formed_sha256(self):
        for rel in sorted(ALLOWED_CHANGED_PATHS):
            digest = _sha256_file(rel)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_head_is_bound_to_the_signed_post_package_a_base(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(
            head, BASE_SHA,
            "this worktree's HEAD must still be the exact signed "
            "post-Package-A base -- Package B works uncommitted on top of "
            "it, never on a different commit")


class ExcludedPathsUntouchedTests(unittest.TestCase):
    """Every path the frozen brief marks read-only for this package --
    including Package A's own owned files and its test suite -- must be
    byte-identical to the signed base commit (main/m5/Package-A
    integrity)."""

    def test_excluded_paths_are_byte_identical_to_base(self):
        for rel in EXCLUDED_PATHS:
            base_bytes = _git_show(BASE_SHA, rel)
            with open(os.path.join(_REPO_ROOT, rel), "rb") as fh:
                current_bytes = fh.read()
            self.assertEqual(
                current_bytes, base_bytes,
                "%s must be byte-identical to the signed base commit "
                "(read-only for this package)" % rel)

    def test_package_a_test_suite_still_passes_except_its_now_stale_stub_assertions(self):
        # `test_m5_package_a_contracts.py` is unmodified (asserted above)
        # and describes PACKAGE A's OWN candidate -- three of its
        # assertions are, by that file's own explicit comments, exactly
        # what Package B (#44) is supposed to change:
        # `resolve_worker_source` no longer raises NotImplementedError, it
        # is now referenced by `spawn_worker`'s own flow, and
        # `worker_source_missing`/`worker_identity_mismatch` are no longer
        # absent literals. This is run here, not merely asserted, so this
        # candidate packet carries direct, current evidence of exactly
        # which (and only which) of Package A's own tests are affected by
        # completing #44, rather than an unverified claim.
        result = subprocess.run(
            [sys.executable, "-m", "unittest",
             "scripts.test_m5_package_a_contracts", "-v"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=300)
        stderr = result.stderr
        # Exactly the three assertions that file's own comments identify as
        # describing PACKAGE A's (not Package B's) candidate must now fail --
        # unchanged, byte-for-byte-adopted from v2 (v2 already made this
        # exact disposition; v3 does not revisit it).
        expected_failures = {
            "test_extension_stubs_are_not_referenced_by_this_candidates_own_flow",
            "test_resolve_worker_source_raises_not_implemented",
            "test_worker_source_missing_and_identity_mismatch_are_absent",
        }
        # v3-NEW, disclosed and dispositioned here (not silently green):
        # Package A's OWN `ExcludedPathsUntouchedTests`/
        # `ExtractionReexportStructuralTests` assert `scripts/test_cowork.py`
        # byte-identical to the signed base -- true for Package A's own
        # candidate, but this v3 candidate's frozen brief explicitly,
        # narrowly re-authorizes editing exactly one test method in that
        # file (M5B-R-M1's repair) plus its local fixture. Package A's own
        # suite has no way to know about that v3-specific re-authorization
        # (it predates #44's second review round entirely), so both of its
        # byte-identity assertions on `test_cowork.py` now fail -- correctly
        # and expectedly, not a regression in either candidate. Touching
        # `scripts/cowork_verification.py` does NOT trip either of these two
        # tests: Package A's own `EXCLUDED_PATHS` never listed that file
        # (Package A owns it too), so no third failure is expected from
        # that edit.
        expected_failures |= {
            "test_excluded_paths_are_byte_identical_to_base",
            "test_test_cowork_py_is_byte_identical_to_base",
        }
        for name in expected_failures:
            self.assertIn(
                "FAIL: %s " % name, stderr,
                "expected exactly this Package-A stub assertion to now "
                "fail (by that file's own documented design once #44 is "
                "implemented, or by this v3 candidate's disclosed, "
                "brief-authorized widening onto test_cowork.py): %s" % name)
        # `test_changed_paths_are_within_the_five_path_allowlist` may ALSO
        # now fail, purely as a byproduct of this candidate's own new test
        # file existing on disk (Package A's five-path allowlist predates
        # Package B and has no way to know about it) -- not a behavioral
        # regression. No OTHER test may newly fail.
        allowed_extra_failures = {
            "test_changed_paths_are_within_the_five_path_allowlist"}
        for line in stderr.splitlines():
            if line.startswith("FAIL: ") or line.startswith("ERROR: "):
                name = line.split(" ", 2)[1]
                self.assertIn(
                    name, expected_failures | allowed_extra_failures,
                    "an UNEXPECTED Package-A contract test failed/errored: "
                    "%s" % line)


# =========================================================================== #
# Named-region scope: the two PARTIALLY-writable v3 paths.                    #
# =========================================================================== #


def _top_level_diffs(base_source, current_source):
    """Diff `base_source`/`current_source` (two versions of the SAME file)
    at exactly two levels -- every top-level statement, and (one level
    deeper) every member of any top-level `class` whose own text changed --
    zipped BY POSITION, not by name. Position, not name, is what remains
    stable across a functionally-scoped edit: this candidate adds or
    removes no top-level statement, class, or method anywhere in either of
    the two files this checks, so base and current always have the exact
    same COUNT and ORDER of top-level (and, inside a changed class,
    member-level) statements -- only specific bodies differ internally.

    Returns a dict `label -> (kind, base_text, current_text)` for every
    region whose source text differs, where `label` is the bare
    function/class name, `"ClassName.method_name"` one level inside a
    changed class, or `"TOP_LEVEL_STMT_AT_LINE_<n>"` (`<n>` from the BASE
    file) for a changed anonymous top-level statement (import/assign/try/
    if) -- literal and specific, so a genuine change to something with no
    def/class name can never be silently absent from the returned dict. A
    positional COUNT or KIND/NAME mismatch at any level -- a real
    structural change (added/removed/reordered top-level statement or
    class member) this candidate must never make -- is itself returned
    under a `"__STRUCTURE__..."` label, deliberately never matched by any
    caller's own allowed-label set, so it always fails any check built on
    top of this."""

    def segments(body, lines):
        out = []
        for node in body:
            start, end = node.lineno, node.end_lineno
            name = getattr(node, "name", None)
            kind = type(node).__name__
            text = "".join(lines[start - 1:end])
            out.append((kind, name, text, node))
        return out

    base_lines = base_source.splitlines(keepends=True)
    cur_lines = current_source.splitlines(keepends=True)
    base_segs = segments(ast.parse(base_source).body, base_lines)
    cur_segs = segments(ast.parse(current_source).body, cur_lines)

    diffs = {}
    if len(base_segs) != len(cur_segs):
        diffs["__STRUCTURE__"] = (
            "TOP_LEVEL", "%d statements" % len(base_segs),
            "%d statements" % len(cur_segs))
        return diffs

    for i, (base_seg, cur_seg) in enumerate(zip(base_segs, cur_segs)):
        bkind, bname, btext, bnode = base_seg
        ckind, cname, ctext, _cnode = cur_seg
        if bkind != ckind or bname != cname:
            diffs["__STRUCTURE__@%d" % i] = (
                bkind, "%s %r" % (bkind, bname), "%s %r" % (ckind, cname))
            continue
        if btext == ctext:
            continue
        label = bname or ("TOP_LEVEL_STMT_AT_LINE_%d" % bnode.lineno)
        if bkind != "ClassDef":
            diffs[label] = (bkind, btext, ctext)
            continue
        # A changed class: recurse ONE level into its own members, by the
        # exact same positional-zip contract, so the diff is attributed to
        # the SPECIFIC method(s) that changed, not the whole class.
        b_inner = segments(bnode.body, base_lines)
        c_inner = segments(_cnode.body, cur_lines)
        if len(b_inner) != len(c_inner):
            diffs["%s.__STRUCTURE__" % label] = (
                "ClassDef", "%d members" % len(b_inner),
                "%d members" % len(c_inner))
            continue
        for j, (b_mem, c_mem) in enumerate(zip(b_inner, c_inner)):
            bmkind, bmname, bmtext, bmnode = b_mem
            cmkind, cmname, cmtext, _cmnode = c_mem
            if bmkind != cmkind or bmname != cmname:
                diffs["%s.__STRUCTURE__@%d" % (label, j)] = (
                    bmkind, "%s %r" % (bmkind, bmname),
                    "%s %r" % (cmkind, cmname))
                continue
            if bmtext == cmtext:
                continue
            inner_label = bmname or (
                "TOP_LEVEL_STMT_AT_LINE_%d" % bmnode.lineno)
            diffs["%s.%s" % (label, inner_label)] = (
                bmkind, bmtext, cmtext)
    return diffs


class NamedRegionScopeTests(unittest.TestCase):
    """Enforces the NARROWER, sub-file scope the v3 frozen brief actually
    grants for the two partially-writable paths -- `git diff` alone (the
    path-level `AllowlistAndHashTests` above) cannot express "only this
    function changed inside this file". Any region outside the allowed set
    below is disclosed HERE, by name, never silently passed through a
    path-level check that cannot see inside the file."""

    def test_cowork_verification_changes_are_confined_to_the_authorized_named_regions(self):
        # Brief: "only the worker-seam import/fallback and run_transaction
        # worker lifecycle/cleanup sites needed to retain and reclaim the
        # captured checkout". The seam import/fallback block is the file's
        # one top-level `try`/`except` statement (adding
        # `reclaim_tool_snapshot_checkout` to the import list and its
        # fallback assignment); the lifecycle/cleanup sites are inside
        # `run_transaction`'s own three-function family.
        base_source = _git_show(
            BASE_SHA, "scripts/cowork_verification.py").decode("utf-8")
        with open(os.path.join(_REPO_ROOT, "scripts",
                               "cowork_verification.py")) as fh:
            current_source = fh.read()
        diffs = _top_level_diffs(base_source, current_source)
        allowed_functions = {
            "run_transaction", "_run_transaction_body",
            "_run_owned_transaction"}
        offenders = []
        for label, (kind, _base_text, _cur_text) in diffs.items():
            if kind == "Try":
                continue  # the seam import/fallback block itself
            if label in allowed_functions:
                continue
            offenders.append(label)
        self.assertFalse(
            offenders,
            "scripts/cowork_verification.py changed outside the "
            "authorized worker-seam import/fallback and run_transaction "
            "lifecycle/cleanup regions: %s" % sorted(offenders))
        # Non-vacuous: something in each authorized region actually did
        # change (otherwise this test would trivially pass on an unmodified
        # file and prove nothing).
        self.assertTrue(
            any(kind == "Try" for kind, _b, _c in diffs.values()),
            "expected the worker-seam import/fallback try block to have "
            "actually changed (reclaim_tool_snapshot_checkout added)")
        self.assertTrue(
            diffs.keys() & allowed_functions,
            "expected at least one of run_transaction/"
            "_run_transaction_body/_run_owned_transaction to have "
            "actually changed (checkout reclaim added)")

    def test_test_cowork_changes_are_confined_to_the_authorized_named_regions(self):
        # Brief (M5 Package B bounded successor v4): exactly three named
        # methods and their directly local fixture setup/cleanup --
        # `test_worker_crash_before_identity_completes_promptly_with_
        # evidence` (v3's own authorized region, carried forward
        # unchanged) plus the two v4-authorized regions this candidate
        # closes (M5B-V4-B1/B2): `test_startup_log_tail_sentinel_survives_
        # through_the_real_worker_path` and `test_deliberately_older_
        # parent_executes_captured_newer_worker`, both outside v3's own
        # authority and left broken by it.
        base_source = _git_show(
            BASE_SHA, "scripts/test_cowork.py").decode("utf-8")
        with open(os.path.join(_REPO_ROOT, "scripts", "test_cowork.py")) as fh:
            current_source = fh.read()
        diffs = _top_level_diffs(base_source, current_source)
        allowed_labels = {
            "OwnedVerificationLedgerIntegrationTests."
            "test_worker_crash_before_identity_completes_promptly_with_evidence",
            "OwnedVerificationLedgerIntegrationTests."
            "test_startup_log_tail_sentinel_survives_through_the_real_worker_path",
            "OwnedVerificationWorkerBoundaryTests."
            "test_deliberately_older_parent_executes_captured_newer_worker",
        }
        offenders = [label for label in diffs if label not in allowed_labels]
        self.assertFalse(
            offenders,
            "scripts/test_cowork.py changed outside the three authorized "
            "test methods (and their directly local fixture setup/"
            "cleanup, which this candidate kept fully inline in those "
            "same methods rather than touching any shared fixture): %s"
            % sorted(offenders))
        missing = allowed_labels - diffs.keys()
        self.assertFalse(
            missing,
            "expected every one of the three authorized test methods to "
            "have actually changed: %s" % sorted(missing))


# =========================================================================== #
# Frozen signature + three-item iterable/proc-stashed compatibility channel.  #
# =========================================================================== #


class SignatureAndCompatibilityTests(unittest.TestCase):

    def test_spawn_worker_signature_is_unchanged(self):
        import inspect
        sig = inspect.signature(worker_module.spawn_worker)
        self.assertEqual(
            list(sig.parameters),
            ["python_executable", "checkout_root", "request_path",
             "session_uuid", "transaction_id"])
        self.assertIsNone(sig.parameters["session_uuid"].default)
        self.assertIsNone(sig.parameters["transaction_id"].default)

    def test_worker_startup_result_is_a_four_field_named_record(self):
        self.assertEqual(
            worker_module.WorkerStartupResult.__slots__,
            ("proc", "liveness_write_fd", "capture_thread", "classification"))

    def test_iterates_as_the_original_three_item_handle_bundle(self):
        classification = {"identity": None, "worker_verified": False,
                          "startup_failure": {"reason": "worker_source_missing"}}
        wr = worker_module.WorkerStartupResult(
            "PROC", "FD", "THREAD", classification)
        proc, liveness_write_fd, capture_thread = wr
        self.assertEqual((proc, liveness_write_fd, capture_thread),
                         ("PROC", "FD", "THREAD"))
        self.assertEqual(len(wr), 3)
        self.assertEqual((wr[0], wr[1], wr[2]), ("PROC", "FD", "THREAD"))

    def test_classification_reachable_via_attribute_and_proc_stash(self):
        transaction_id = "T-compat-1"
        with tempfile.TemporaryDirectory() as sessions_root:
            prior = os.environ.get("COWORK_SESSIONS_ROOT")
            os.environ["COWORK_SESSIONS_ROOT"] = sessions_root
            try:
                session_uuid = "S-" + uuid.uuid4().hex[:8]
                request = {
                    "protocol_version": verification.PROTOCOL_VERSION,
                    "session_uuid": session_uuid,
                    "transaction_id": transaction_id, "inventory": [],
                    "timeout_policy": {}}
                request_path = state_store.verification_request_path_for(
                    session_uuid, transaction_id)
                state_store.write_json_atomic(request_path, request)
                result = worker_module.spawn_worker(
                    sys.executable, "/irrelevant-checkout-root",
                    request_path, session_uuid=session_uuid,
                    transaction_id=transaction_id)
                proc, write_fd, capture_thread = result
                self.assertIs(result.classification,
                              proc._cowork_startup_classification)
                self.assertTrue(result.classification["worker_verified"])
                os.close(write_fd)
                proc.wait(timeout=5)
                if capture_thread is not None:
                    capture_thread.join(timeout=5)
            finally:
                if prior is None:
                    os.environ.pop("COWORK_SESSIONS_ROOT", None)
                else:
                    os.environ["COWORK_SESSIONS_ROOT"] = prior


# =========================================================================== #
# Non-vacuous source resolution + snapshot-before-launch + expected-hash.     #
# =========================================================================== #


class SourceResolutionTests(_SessionFixture):

    def test_resolve_worker_source_captures_the_real_running_installation(self):
        transaction_id = "T-resolve-1"
        resolution = worker_module.resolve_worker_source(
            self.session_uuid, transaction_id)
        self.assertIsNotNone(resolution)
        manifest_files = resolution["manifest_files"]
        # Non-vacuous: the running installation has dozens of *.py files
        # directly under scripts/, not merely a single stubbed entry.
        self.assertGreater(len(manifest_files), 10)
        worker_rel_path = resolution["worker_rel_path"]
        self.assertEqual(worker_rel_path,
                         os.path.join("scripts", "cowork_verification.py"))
        self.assertIn(worker_rel_path, manifest_files)

        with open(os.path.join(_HERE, "cowork_verification.py"), "rb") as fh:
            real_bytes = fh.read()
        real_hash = hashlib.sha256(real_bytes).hexdigest()
        self.assertEqual(resolution["expected_hash"], real_hash)
        self.assertEqual(manifest_files[worker_rel_path]["sha256"], real_hash)

        # The materialized checkout holds a byte-for-byte copy of the real
        # running installation's file -- not a placeholder, not empty.
        captured_path = os.path.join(resolution["checkout_root"],
                                     worker_rel_path)
        with open(captured_path, "rb") as fh:
            captured_bytes = fh.read()
        self.assertEqual(captured_bytes, real_bytes)

        # Durably persisted where `_classify_worker_startup` reads it back
        # from, independent of the in-memory `resolution` dict.
        manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, transaction_id)
        manifest_doc = state_store.read_json_tolerant(manifest_path)
        self.assertEqual(
            manifest_doc["files"][worker_rel_path]["sha256"], real_hash)

    def test_resolve_worker_source_never_reads_checkout_root(self):
        # `resolve_worker_source` takes no `checkout_root` parameter at all
        # -- it cannot possibly consult the target-repo snapshot, by
        # construction, not merely by convention.
        import inspect
        sig = inspect.signature(worker_module.resolve_worker_source)
        self.assertNotIn("checkout_root", sig.parameters)

    def test_manifest_and_checkout_exist_before_popen_is_ever_called(self):
        transaction_id = "T-order-1"
        request_path = self._write_request(transaction_id)
        manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, transaction_id)
        tool_snapshot_root = state_store.verification_tool_snapshot_root_for(
            self.session_uuid)
        observed = {}

        def spy_popen(*args, **kwargs):
            observed["manifest_exists"] = os.path.exists(manifest_path)
            observed["argv"] = args[0]
            observed["cwd"] = kwargs.get("cwd")
            raise OSError("intentional: stop before a real process exists")

        bogus_checkout_root = "/this-target-repo-checkout-must-never-be-used"
        with mock.patch.object(worker_module.subprocess, "Popen",
                               side_effect=spy_popen):
            with self.assertRaises(OSError):
                worker_module.spawn_worker(
                    sys.executable, bogus_checkout_root, request_path,
                    session_uuid=self.session_uuid,
                    transaction_id=transaction_id)

        self.assertTrue(
            observed.get("manifest_exists"),
            "the captured tool-snapshot manifest must already be durably "
            "written to disk before spawn_worker ever calls Popen")
        self.assertTrue(observed["argv"][1].startswith(tool_snapshot_root),
                        "the launched script path must come from the "
                        "captured tool snapshot, not checkout_root: %r"
                        % (observed["argv"],))
        self.assertTrue(observed["cwd"].startswith(tool_snapshot_root))
        self.assertNotIn(bogus_checkout_root, observed["argv"][1])
        self.assertNotEqual(observed["cwd"], bogus_checkout_root)


# =========================================================================== #
# Captured-manifest identity: real, non-mocked end-to-end verification.       #
# =========================================================================== #


class CapturedManifestIdentityTests(_SessionFixture):

    def test_real_worker_verifies_against_the_captured_installation_manifest(self):
        transaction_id = "T-identity-1"
        request_path = self._write_request(transaction_id)
        result = worker_module.spawn_worker(
            sys.executable, "/irrelevant-checkout-root", request_path,
            session_uuid=self.session_uuid, transaction_id=transaction_id)
        self.assertTrue(result.classification["worker_verified"])
        self.assertIsNone(result.classification["startup_failure"])
        identity = result.classification["identity"]
        self.assertIsNotNone(identity)
        self.assertEqual(identity["source_hash"], verification.self_source_hash())
        self.assertEqual(identity["protocol_version"],
                         verification.PROTOCOL_VERSION)
        self._close_result(result)

    def test_run_transaction_end_to_end_goes_green_via_the_captured_manifest(self):
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        result = verification.run_transaction(repo, self.session_uuid, entries)
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertTrue(result["worker_identity_verified"])
        self.assertIsNone(result["startup_failure"])


# =========================================================================== #
# worker_source_missing: distinct, detected before any real launch.          #
# =========================================================================== #


class MissingSourceTests(_SessionFixture):

    def test_resolve_worker_source_returns_none_when_entry_point_absent(self):
        empty_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(empty_dir, ignore_errors=True))
        with mock.patch.object(worker_module, "_installation_scripts_dir",
                               return_value=empty_dir):
            resolution = worker_module.resolve_worker_source(
                self.session_uuid, "T-missing-resolve-1")
        self.assertIsNone(resolution)

    def test_spawn_worker_reports_worker_source_missing_without_launching_the_worker(self):
        empty_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(empty_dir, ignore_errors=True))
        transaction_id = "T-missing-1"
        request_path = self._write_request(transaction_id)
        with mock.patch.object(worker_module, "_installation_scripts_dir",
                               return_value=empty_dir):
            result = worker_module.spawn_worker(
                sys.executable, "/irrelevant-checkout-root", request_path,
                session_uuid=self.session_uuid, transaction_id=transaction_id)
        self.assertIsNone(result.classification["identity"])
        self.assertFalse(result.classification["worker_verified"])
        self.assertEqual(
            result.classification["startup_failure"]["reason"],
            "worker_source_missing")
        # A real, terminable process still backs the frozen handle bundle.
        proc, write_fd, capture_thread = result
        self.assertIsNotNone(proc)
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(
            proc.poll(),
            "a real, terminable placeholder process must still back the "
            "WorkerStartupResult contract even when source is missing")
        # Nothing was ever captured for this transaction -- no manifest.
        manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, transaction_id)
        self.assertFalse(os.path.exists(manifest_path))
        self._close_result(result)


# =========================================================================== #
# worker_identity_mismatch: a real report that disagrees with the manifest.  #
# =========================================================================== #


class IdentityMismatchTests(_SessionFixture):

    def test_worker_identity_mismatch_when_reported_hash_disagrees_with_manifest(self):
        transaction_id = "T-mismatch-1"
        request_path = self._write_request(transaction_id)
        real_resolve = worker_module.resolve_worker_source

        def corrupted_resolve(session_uuid, transaction_id_):
            resolution = real_resolve(session_uuid, transaction_id_)
            if resolution is None:
                return resolution
            worker_rel_path = resolution["worker_rel_path"]
            manifest_path = (
                state_store.verification_tool_snapshot_manifest_path_for(
                    session_uuid, transaction_id_))
            manifest_doc = state_store.read_json_tolerant(manifest_path)
            manifest_doc["files"][worker_rel_path]["sha256"] = "0" * 64
            state_store.write_json_atomic(manifest_path, manifest_doc)
            resolution = dict(resolution)
            resolution["manifest_files"] = manifest_doc["files"]
            return resolution

        with mock.patch.object(worker_module, "resolve_worker_source",
                               side_effect=corrupted_resolve):
            result = worker_module.spawn_worker(
                sys.executable, "/irrelevant-checkout-root", request_path,
                session_uuid=self.session_uuid, transaction_id=transaction_id)

        identity = result.classification["identity"]
        self.assertIsNotNone(
            identity, "the real worker must have actually reported an "
            "identity for this to be a genuine mismatch, not a missing "
            "report")
        self.assertFalse(result.classification["worker_verified"])
        failure = result.classification["startup_failure"]
        self.assertEqual(failure["reason"], "worker_identity_mismatch")
        self.assertEqual(failure["expected_source_hash"], "0" * 64)
        self.assertEqual(failure["reported_source_hash"],
                         verification.self_source_hash())
        self.assertNotEqual(failure["reported_source_hash"],
                            failure["expected_source_hash"])
        # M5B-R-m1: every reason in the taxonomy now carries exit_code/
        # log_tail, including this one -- `proc.poll()` is non-blocking
        # (`None` while the worker is still alive, its real exit code once
        # it has already exited -- either way, present and never omitted).
        self.assertIn("exit_code", failure)
        self.assertIn("log_tail", failure)
        self._close_result(result)


# =========================================================================== #
# Minor dispositions M5B-R-m4/m5: symlinks excluded, checkout starts clean.   #
# =========================================================================== #


class MinorDispositionTests(_SessionFixture):

    def test_m4_a_symlinked_py_file_is_excluded_rather_than_mis_captured(self):
        # M5B-R-m4: `os.path.isfile` follows symlinks, so a bare isfile
        # check would silently capture a symlink's TARGET content under
        # the symlink's own name. A symlinked `cowork_verification.py`
        # (the entry point) must therefore be treated as ABSENT --
        # worker_source_missing, fail closed -- never mis-captured as a
        # real file.
        real_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(real_dir, ignore_errors=True))
        target = os.path.join(_HERE, "cowork_verification.py")
        link = os.path.join(real_dir, "cowork_verification.py")
        os.symlink(target, link)
        self.assertTrue(os.path.isfile(link), "fixture sanity: the symlink "
                                              "must resolve to a real file")
        names = worker_module._iter_installation_source_files(real_dir)
        self.assertNotIn(
            "cowork_verification.py", names,
            "a symlinked entry-point file must be excluded from the "
            "captured manifest, not silently mis-captured as a plain file")
        manifest_files, _raw = worker_module._build_installation_manifest(
            real_dir)
        self.assertIsNone(
            manifest_files,
            "with the entry point excluded as a symlink, the manifest "
            "build must report the same worker_source_missing condition "
            "as a directory with no entry point at all")

    def test_m4_a_symlinked_non_entry_file_is_also_excluded(self):
        real_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(real_dir, ignore_errors=True))
        shutil.copyfile(os.path.join(_HERE, "cowork_verification.py"),
                        os.path.join(real_dir, "cowork_verification.py"))
        os.symlink(os.path.join(_HERE, "cowork_state.py"),
                  os.path.join(real_dir, "cowork_state.py"))
        names = worker_module._iter_installation_source_files(real_dir)
        self.assertIn("cowork_verification.py", names)
        self.assertNotIn(
            "cowork_state.py", names,
            "a symlinked non-entry-point file must also be excluded, not "
            "mis-captured under its own name")

    def test_m5_materialize_starts_from_a_clean_checkout_directory(self):
        transaction_id = "T-clean-checkout-1"
        checkout_root = worker_module.tool_snapshot_checkout_dir(
            self.session_uuid, transaction_id)
        stale_leftover = os.path.join(checkout_root, "scripts",
                                      "leftover_from_a_crashed_attempt.py")
        os.makedirs(os.path.dirname(stale_leftover))
        with open(stale_leftover, "w") as fh:
            fh.write("# stale leftover that must not survive\n")

        resolution = worker_module.resolve_worker_source(
            self.session_uuid, transaction_id)

        self.assertIsNotNone(resolution)
        self.assertFalse(
            os.path.exists(stale_leftover),
            "a stale leftover from a prior, crashed materialization "
            "attempt at this same path must never silently survive into "
            "a fresh materialization")


# =========================================================================== #
# Preserved reason taxonomy: request_rejected / worker_exited_before_...      #
# =========================================================================== #


class ReasonTaxonomyPreservedTests(_SessionFixture):

    def test_request_rejected_reason_and_exit_code_preserved(self):
        transaction_id = "T-rejected-1"
        request_path = self._write_request(transaction_id, protocol_version=1)
        result = worker_module.spawn_worker(
            sys.executable, "/irrelevant-checkout-root", request_path,
            session_uuid=self.session_uuid, transaction_id=transaction_id)
        failure = result.classification["startup_failure"]
        self.assertIsNone(result.classification["identity"])
        self.assertFalse(result.classification["worker_verified"])
        self.assertEqual(failure["reason"], "request_rejected")
        self.assertEqual(failure["exit_code"],
                         verification.WORKER_EXIT_REQUEST_REJECTED)
        self._close_result(result)

    def test_worker_exited_before_identity_report_reason_preserved(self):
        transaction_id = "T-exited-1"
        missing_request_path = os.path.join(
            self.sessions_root, "does-not-exist-request.json")
        result = worker_module.spawn_worker(
            sys.executable, "/irrelevant-checkout-root",
            missing_request_path, session_uuid=self.session_uuid,
            transaction_id=transaction_id)
        failure = result.classification["startup_failure"]
        self.assertIsNone(result.classification["identity"])
        self.assertFalse(result.classification["worker_verified"])
        self.assertEqual(failure["reason"],
                         "worker_exited_before_identity_report")
        self.assertEqual(failure["exit_code"], 2)
        self._close_result(result)

    def test_worker_spawn_failed_is_absent_from_this_module(self):
        import ast
        import inspect

        def string_literals(source):
            tree = ast.parse(source)
            docstring_ids = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef,
                                    ast.AsyncFunctionDef, ast.ClassDef)):
                    body = getattr(node, "body", None)
                    if body and isinstance(body[0], ast.Expr):
                        value = body[0].value
                        if isinstance(value, ast.Constant) and isinstance(
                                value.value, str):
                            docstring_ids.add(id(value))
            return [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and id(n) not in docstring_ids]

        literals = string_literals(inspect.getsource(worker_module))
        self.assertNotIn("worker_spawn_failed", literals)
        self.assertIn("request_rejected", literals)
        self.assertIn("worker_exited_before_identity_report", literals)
        self.assertIn("worker_source_missing", literals)
        self.assertIn("worker_identity_mismatch", literals)

    def test_self_source_hash_stays_locally_defined_in_the_spine(self):
        self.assertEqual(verification.self_source_hash.__module__,
                         "cowork_verification")
        self.assertNotIn("self_source_hash", vars(worker_module))


# =========================================================================== #
# Negative controls: the target-repo snapshot is never the worker's source.   #
# =========================================================================== #


class NegativeTargetRepoSourceControlTests(_SessionFixture):

    def test_transaction_goes_green_even_when_target_repo_has_no_cowork_source_at_all(self):
        repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        self.assertFalse(
            os.path.exists(os.path.join(repo, "scripts")),
            "fixture sanity: this target repo must not track any Cowork "
            "source at all")
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        result = verification.run_transaction(repo, self.session_uuid, entries)
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertTrue(result["worker_identity_verified"])

    def test_transaction_goes_green_while_ignoring_a_garbage_target_repo_worker_copy(self):
        repo = _init_git_repo(garbage_worker_source=True)
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        with open(os.path.join(repo, "scripts",
                               "cowork_verification.py")) as fh:
            self.assertIn("PACKAGE_B_NEGATIVE_CONTROL", fh.read())
        entries = [{"label": "noop", "command": ["python3", "-c", "pass"],
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        result = verification.run_transaction(repo, self.session_uuid, entries)
        self.assertEqual(
            result["verdict"], verification.VERDICT_GREEN,
            "a garbage scripts/cowork_verification.py committed in the "
            "TARGET repo must never be what the worker is launched from")
        self.assertTrue(result["worker_identity_verified"])

    def test_spawn_worker_ignores_checkout_root_argument_entirely_for_source(self):
        transaction_id = "T-negctrl-argument-1"
        request_path = self._write_request(transaction_id)
        fake_checkout = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(fake_checkout, ignore_errors=True))
        fake_scripts = os.path.join(fake_checkout, "scripts")
        os.makedirs(fake_scripts)
        with open(os.path.join(fake_scripts, "cowork_verification.py"),
                 "w") as fh:
            fh.write("raise SystemExit('must never run')\n")
        result = worker_module.spawn_worker(
            sys.executable, fake_checkout, request_path,
            session_uuid=self.session_uuid, transaction_id=transaction_id)
        self.assertTrue(result.classification["worker_verified"])
        self.assertEqual(result.classification["identity"]["source_hash"],
                         verification.self_source_hash())
        self._close_result(result)


# =========================================================================== #
# M5B-R-M2: nameable checkout, reclaimed on every parent-side terminal path,  #
# no leak across repeated transactions, siblings never disturbed.            #
# =========================================================================== #


class CheckoutNamingAndDirectReclaimTests(_SessionFixture):
    """`tool_snapshot_checkout_dir`/`reclaim_tool_snapshot_checkout` tested
    directly, against `resolve_worker_source`'s own real materialization --
    no `run_transaction`/`spawn_worker` involved yet (that end-to-end
    lifecycle is `ParentSideTerminalPathReclaimTests`, below)."""

    def test_checkout_dir_is_deterministic_from_session_and_transaction(self):
        d1 = worker_module.tool_snapshot_checkout_dir(
            self.session_uuid, "T-a")
        d2 = worker_module.tool_snapshot_checkout_dir(
            self.session_uuid, "T-a")
        self.assertEqual(d1, d2, "same (session, transaction) must name "
                                 "the same checkout every time")
        d_other_txn = worker_module.tool_snapshot_checkout_dir(
            self.session_uuid, "T-b")
        self.assertNotEqual(d1, d_other_txn)
        d_other_session = worker_module.tool_snapshot_checkout_dir(
            "S-" + uuid.uuid4().hex[:8], "T-a")
        self.assertNotEqual(d1, d_other_session)

    def test_reclaim_is_idempotent_on_a_checkout_never_materialized(self):
        never = "T-never-materialized-" + uuid.uuid4().hex[:8]
        # Must not raise, whether called once or twice against a checkout
        # that was never created in the first place.
        worker_module.reclaim_tool_snapshot_checkout(self.session_uuid, never)
        worker_module.reclaim_tool_snapshot_checkout(self.session_uuid, never)

    def test_reclaim_removes_only_its_own_checkout_leaving_objects_and_manifest(self):
        t1, t2 = "T-reclaim-sib-1", "T-reclaim-sib-2"
        res1 = worker_module.resolve_worker_source(self.session_uuid, t1)
        res2 = worker_module.resolve_worker_source(self.session_uuid, t2)
        self.assertTrue(os.path.isdir(res1["checkout_root"]))
        self.assertTrue(os.path.isdir(res2["checkout_root"]))
        objects_dir = state_store.verification_tool_snapshot_objects_dir(
            self.session_uuid)
        manifest1 = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, t1)
        manifest2 = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, t2)
        self.assertTrue(os.path.isdir(objects_dir))
        self.assertTrue(os.path.exists(manifest1))
        self.assertTrue(os.path.exists(manifest2))

        worker_module.reclaim_tool_snapshot_checkout(self.session_uuid, t1)

        self.assertFalse(
            os.path.exists(res1["checkout_root"]),
            "the reclaimed transaction's own checkout must be gone")
        self.assertTrue(
            os.path.isdir(res2["checkout_root"]),
            "a SIBLING transaction's checkout must never be disturbed by "
            "reclaiming a different transaction's own checkout")
        self.assertTrue(
            os.path.isdir(objects_dir),
            "the shared content-addressed object store must never be "
            "deleted by reclaiming one transaction's checkout")
        self.assertTrue(
            os.path.exists(manifest1),
            "this transaction's own durable manifest is evidence a caller "
            "may still need to read -- reclaim must not delete it")
        self.assertTrue(os.path.exists(manifest2))

        # Idempotent: reclaiming the same (now-already-gone) checkout again
        # must not raise.
        worker_module.reclaim_tool_snapshot_checkout(self.session_uuid, t1)
        # A sibling's own checkout content is untouched byte-for-byte, not
        # merely still present as an empty/corrupted directory.
        worker_rel = res2["worker_rel_path"]
        with open(os.path.join(res2["checkout_root"], worker_rel), "rb") as fh:
            sibling_bytes = fh.read()
        with open(os.path.join(_HERE, "cowork_verification.py"), "rb") as fh:
            real_bytes = fh.read()
        self.assertEqual(sibling_bytes, real_bytes)
        worker_module.reclaim_tool_snapshot_checkout(self.session_uuid, t2)


class ParentSideTerminalPathReclaimTests(_SessionFixture):
    """Non-vacuous proof that `run_transaction` reclaims the captured
    checkout on every parent-side terminal path the frozen brief names:
    normal completion, startup failure, cancellation, timeout, and an
    exception propagating out of the entry loop -- plus repeated real
    transactions in the same session leaving nothing leaked behind."""

    def setUp(self):
        super().setUp()
        self.repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))

    def _checkout_dir(self, transaction_id):
        return worker_module.tool_snapshot_checkout_dir(
            self.session_uuid, transaction_id)

    def _noop_entries(self):
        return [{"label": "noop", "command": ["python3", "-c", "pass"],
                 "execution_mode": "isolated_snapshot",
                 "kind": verification.KIND_FINAL_SUITE}]

    def test_reclaimed_on_normal_completion(self):
        result = verification.run_transaction(
            self.repo, self.session_uuid, self._noop_entries())
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertFalse(
            os.path.exists(self._checkout_dir(result["transaction_id"])),
            "checkout must be reclaimed after a normal green completion")

    def test_reclaimed_on_startup_failure_when_popen_itself_raises(self):
        # `resolve_worker_source` succeeds (a real checkout IS materialized)
        # and only then does `Popen` raise -- the one parent-side terminal
        # path `_run_owned_transaction`'s own `finally` can never cover,
        # since the exception fires before that function is ever entered
        # (see `_run_transaction_body`'s own `except OSError` handler).
        #
        # `subprocess` is one shared module object -- patching `Popen` on it
        # affects every caller process-wide, including `build_snapshot`'s
        # own `git` invocations earlier in this same `run_transaction` call.
        # This wrapper raises ONLY for the actual worker-launch argv (it
        # carries `--worker`), and delegates every other call (git, etc.)
        # to the real `Popen` untouched.
        real_popen = subprocess.Popen

        def selective_popen(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            if isinstance(argv, list) and "--worker" in argv:
                raise OSError("intentional: no real worker process")
            return real_popen(*args, **kwargs)

        with mock.patch.object(worker_module.subprocess, "Popen",
                               side_effect=selective_popen):
            result = verification.run_transaction(
                self.repo, self.session_uuid, self._noop_entries())
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)
        self.assertEqual(result["startup_failure"]["reason"],
                         "worker_spawn_failed")
        transaction_id = result["transaction_id"]
        manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, transaction_id)
        self.assertTrue(
            os.path.exists(manifest_path),
            "non-vacuous: the manifest's existence proves a checkout was "
            "genuinely materialized before Popen raised, not skipped "
            "entirely -- evidence, so it must survive reclaim")
        self.assertFalse(
            os.path.exists(self._checkout_dir(transaction_id)),
            "checkout must still be reclaimed even though the OSError "
            "fired before _run_owned_transaction's own finally block "
            "could ever run")

    def test_reclaimed_on_cancellation(self):
        cancel_event = threading.Event()
        cancel_event.set()
        result = verification.run_transaction(
            self.repo, self.session_uuid, self._noop_entries(),
            cancel_event=cancel_event)
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)
        self.assertFalse(
            os.path.exists(self._checkout_dir(result["transaction_id"])),
            "checkout must be reclaimed on the cancellation path")

    def test_reclaimed_on_command_timeout(self):
        hang = ["python3", "-c", "import time\ntime.sleep(30)\n"]
        entries = [{"label": "hang", "command": hang,
                   "execution_mode": "isolated_snapshot",
                   "kind": verification.KIND_FINAL_SUITE}]
        result = verification.run_transaction(
            self.repo, self.session_uuid, entries,
            command_timeout_s=1, term_grace_s=1)
        # A hanging command bounded to a 1s budget never completes: the
        # parent's own bounded evidence poll gives up and the entry is
        # never terminalized as a clean success -- the exact "timeout"
        # terminal path, whether it surfaces as this attempt's own
        # `timed_out` flag or as unresolved evidence backstopped by the
        # overall deadline.
        attempt = result["attempts"][0]
        self.assertTrue(
            attempt.get("timed_out")
            or attempt.get("evidence_state") != verification.EVIDENCE_PRESENT,
            "expected the hanging command to never cleanly complete: %r"
            % (attempt,))
        self.assertIn(result["verdict"],
                      (verification.VERDICT_RED, verification.VERDICT_UNVERIFIED))
        self.assertFalse(
            os.path.exists(self._checkout_dir(result["transaction_id"])),
            "checkout must be reclaimed on the command-timeout path")

    def test_reclaimed_when_an_exception_propagates_from_the_entry_loop(self):
        fixed_transaction_id = "T-exc-path-" + uuid.uuid4().hex[:8]
        manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
            self.session_uuid, fixed_transaction_id)
        with mock.patch.object(verification, "new_transaction_id",
                               return_value=fixed_transaction_id):
            with mock.patch.object(verification, "detect_mutation",
                                   side_effect=RuntimeError(
                                       "intentional: exercise the "
                                       "exception-propagation path")):
                with self.assertRaises(RuntimeError):
                    verification.run_transaction(
                        self.repo, self.session_uuid, self._noop_entries())
        self.assertTrue(
            os.path.exists(manifest_path),
            "non-vacuous: the manifest's existence proves the checkout was "
            "genuinely materialized before detect_mutation raised")
        self.assertFalse(
            os.path.exists(self._checkout_dir(fixed_transaction_id)),
            "checkout must be reclaimed even when an exception propagates "
            "out of _run_owned_transaction's own try block, past its "
            "finally, all the way out of run_transaction")

    def test_repeated_real_transactions_in_one_session_leave_no_checkout_leaked(self):
        transaction_ids = []
        for i in range(3):
            # Each iteration's command is distinct so `build_request`'s own
            # `request_key` differs every time -- an IDENTICAL request
            # would instead hit single-flight/dedup reuse
            # (`acquire_single_flight`'s "reuse" path) and return the SAME
            # prior transaction_id, making this leak check vacuous.
            entries = [{"label": "noop", "kind": verification.KIND_FINAL_SUITE,
                       "execution_mode": "isolated_snapshot",
                       "command": ["python3", "-c",
                                  "pass  # iteration %d" % i]}]
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries)
            self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
            transaction_ids.append(result["transaction_id"])
        self.assertEqual(
            len(set(transaction_ids)), 3,
            "fixture sanity: three distinct transactions, not one reused "
            "id that would make this check vacuous")
        for transaction_id in transaction_ids:
            self.assertFalse(
                os.path.exists(self._checkout_dir(transaction_id)),
                "checkout leaked after a repeated transaction: %s"
                % transaction_id)
        checkout_parent = os.path.join(
            state_store.verification_tool_snapshot_root_for(
                self.session_uuid), "checkout")
        if os.path.isdir(checkout_parent):
            self.assertEqual(
                os.listdir(checkout_parent), [],
                "no stray per-transaction checkout directories should "
                "remain under the shared checkout/ parent after repeated "
                "transactions in the same session")
        # The shared, content-addressed object store is NOT expected to be
        # empty -- unlike the disposable checkouts, it is retained
        # provenance, by design (M5B-R-m6 disposition).
        objects_dir = state_store.verification_tool_snapshot_objects_dir(
            self.session_uuid)
        self.assertTrue(os.path.isdir(objects_dir))


if __name__ == "__main__":
    unittest.main()
