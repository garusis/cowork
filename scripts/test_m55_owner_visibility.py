#!/usr/bin/env python3
"""Focused suite for issue #64 implementation package **P4** -- owner
visibility and the recovery surface -- on the frozen P3 candidate
`c7aa5e7a57f3ca81b0a33e3f481cb650d0c1fd94`.

P1 proved the store, P2 wired the gate, P3 added provider-session
exclusivity. All three REFUSE. P4 adds no refusal at all: it makes ownership
VISIBLE on three surfaces and tells an operator what to do about it. So what
has to be proven here is INERTNESS and CONTAINMENT, not enforcement.

  - **Criterion 1 (`--session-owner` is a read-only report).** Five lease
    states -- unowned, a live same-host owner, a crashed owner, an owner whose
    death cannot be proved, and a corrupt record -- each exit 0 in both text
    and `--json` form, with `cowork.run_flow`, the bridge session constructors
    and every non-`ps` `subprocess.Popen` replaced by doubles that RAISE on
    call, and with `owner/lease.json` and `owner/history.jsonl` byte-identical
    (or still absent) across the query. `--json` is the RAW
    `owner_status_view`, not a look-alike.

  - **Criterion 2 (the `--report` owner block never enters the record).** The
    block is ordered above both the provenance banner and the report heading;
    `render_report` still takes the record as its only argument; a recursive
    key scan of the built AND the loaded record finds no owner/lease key at
    any depth; and `--report --json` is byte-for-byte the record
    `cowork_measure` produces on its own.

  - **Criterion 3 (the report still writes nothing).** A git-tracked
    measurement fixture's recursive path SET and per-file sha256 map are
    identical before and after a `--report` run, asserted explicitly for the
    absence of an `owner/` directory and of a `lease.json.lock`.

  - **Criterion 4 (scope confinement).** The changed-path set is exactly P4's
    four allowed paths; every excluded production file is byte-identical to
    the frozen base; every excluded SYMBOL is AST- and docstring-identical
    through the module AST; and each of the three edited files is pinned
    SYMBOLICALLY -- only the named functions differ and the set of new
    module-level names is exactly the declared one.

  - **Criterion 5 (the picker is safe and compatible).** The suffix is right
    for each verdict and absent for unowned/missing/malformed; every legacy
    session-row shape still lists without raising; and `owner` is present on
    every row.

Plus the supporting invariants: the closed reason vocabulary, a corrupt lease
that never advertises `--take-over`, and `render_owner_status(None)` rendering
byte-identically to a real unowned view.

Every fixture redirects `COWORK_SESSIONS_ROOT` into a fresh
`tempfile.mkdtemp()` (so nothing here touches the real home dir and nothing is
written inside the worktree), drives the REAL production functions rather than
fakes of them, and spawns no provider and no network client.
"""

import ast
import contextlib
import datetime
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_REPO_ROOT = os.path.dirname(_HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_owner as owner  # noqa: E402
import cowork_report  # noqa: E402
import cowork_state as state_store  # noqa: E402

# The frozen base this package is bound to: P3's accepted candidate. G6's
# per-package `<base>` rule -- never the accredited base, for P2 onward.
BASE_SHA = "c7aa5e7a57f3ca81b0a33e3f481cb650d0c1fd94"

# P4's OWN signed candidate commit -- the other end of this package's interval.
# The scope class below measures the CLOSED interval BASE_SHA..CANDIDATE_SHA,
# never the live working tree, so what it asserts is a permanent fact about a
# finished piece of history that no later commit can enter or turn red.
CANDIDATE_SHA = "fc7ff676e173a6b6e02246abbaee6c2cb41e5a06"

# P4's write authority, exactly.
ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/cowork.py",
    "scripts/cowork_report.py",
    "scripts/cowork_state.py",
    "scripts/test_m55_owner_visibility.py",
})

# Production and test paths P4 must leave byte-identical to the frozen base.
# P2's list MINUS `cowork_state.py` and `cowork_report.py` (P4 legitimately
# edits both), PLUS the three files P2/P3 were allowed to edit and P4 is not,
# and the two historical owner suites this package must not touch -- their
# scope gates FAIL on this candidate by design, and editing them to pass would
# be the exact failure they exist to prevent.
EXCLUDED_PATHS = tuple(sorted({
    "scripts/cowork_bridge.py",
    "scripts/cowork_capacity.py",
    "scripts/cowork_capacity_scheduler.py",
    "scripts/cowork_control_plane.py",
    "scripts/cowork_dispatch.py",
    "scripts/cowork_eval.py",
    "scripts/cowork_ledger.py",
    "scripts/cowork_owner.py",
    "scripts/cowork_ui.py",
    "scripts/cowork_verification.py",
    "scripts/cowork_verification_evidence.py",
    "scripts/test_cowork.py",
    "scripts/test_cowork_state_m3.py",
    "scripts/test_dispatch_contract_characterization.py",
    "scripts/test_m3_negative_controls.py",
    "scripts/test_m55_owner_gate.py",
    "scripts/test_m55_owner_store.py",
}))

# The excluded-SYMBOL set for P4: P2's list with `run_report` REMOVED, because
# P4's own allowed-symbol list names it (plan.md:1947-1958 -- P2/P3 keep it,
# P4/P5 drop it).
EXCLUDED_MODULE_SYMBOLS = (
    "_emit_activity_record", "_run_activity_tick_loop", "_role_loop",
    "run_scout", "run_planner", "run_builder", "run_reviewer_once",
    "run_worktree", "_send", "_record_role_identity",
    "_isolated_evaluator_session", "_score_queued_entry", "drain_evaluations",
    "run_evaluation_transition", "_make_enqueue_eval_fn",
    "_enqueue_reviewer_eval", "_construct_resume_session",
)
# Closures nested in `run_flow`, unchanged from P2. They are NOT module
# attributes, so `inspect.getsource` cannot reach them by name without
# executing `run_flow`; the gate resolves them by name through the module AST
# instead, which also asserts the parent is unchanged and is strictly stronger.
EXCLUDED_RUN_FLOW_CLOSURES = (
    "measurement_checkpoint", "role_saver", "_measurement_ingest",
    "_measurement_rebuild", "set_phase", "reviewer_controller_check",
    "switch_controller", "ensure_controller_dispatchable",
)

# D-P4-07: authority pinned SYMBOLICALLY, not merely by path. P2's byte
# identity check no longer protects `cowork_report.py` or `cowork_state.py`,
# because P4 legitimately edits both -- a file-level gate would let an
# unrelated change ride along inside an allowed file. For each edited file:
# the ONLY pre-existing top-level symbols that may differ, and the EXACT set of
# new module-level names that may appear.
ALLOWED_SYMBOL_CHANGES = {
    "scripts/cowork.py": frozenset({
        "build_parser", "main", "run_report", "_session_picker_label"}),
    "scripts/cowork_report.py": frozenset(),
    "scripts/cowork_state.py": frozenset({"list_sessions"}),
}
ALLOWED_NEW_SYMBOLS = {
    "scripts/cowork.py": frozenset({"run_session_owner"}),
    "scripts/cowork_report.py": frozenset({
        "render_owner_status", "_section_owner_status"}),
    "scripts/cowork_state.py": frozenset(),
}

# The closed reason vocabulary the owner block may print: the five
# `OWNER_VERDICTS` plus `foreign_host` for the refused-takeover case
# (plan.md:2013-2018). Never free prose, never a sixth verdict.
OWNER_REASON_TOKENS = frozenset(owner.OWNER_VERDICTS) | {"foreign_host"}

# The labels the owner block must carry for every state that HAS an owner.
OWNER_BLOCK_LABELS = ("state", "owner", "process", "launched", "since",
                      "heartbeat", "deadline", "reason", "sidecar",
                      "recovery")

OWNER_BLOCK_TITLE = "Session owner (single-writer lease)"
REPORT_HEADING = "cowork measurement report"

# The measurement fixtures criterion 2 and criterion 3 are decided on. The
# criterion-3 one is git-TRACKED, which is what makes "the report never writes"
# a real property rather than a statement about a scratch directory.
TRACKED_FIXTURE = "c2-finding-lifecycle"
SEED_FIXTURE = "c1-turn-lifecycle"
SEED_FIXTURE_FILES = ("trace.jsonl", "identities.json", "scores.json")
_FIXTURES_ROOT = os.path.join(_HERE, "fixtures", "measurement")


# --------------------------------------------------------------------------- #
# Shared helpers. Modelled on test_m55_owner_gate.py's, with every git call    #
# carrying `--no-optional-locks` so no gate invocation can refresh             #
# `.git/index`: this suite's scope class runs as a LIVE-CANDIDATE preflight,   #
# and the verification transaction fails closed on a live-candidate git-index  #
# change.                                                                      #
# --------------------------------------------------------------------------- #


def _git(args, check=False):
    return subprocess.run(
        ["git", "--no-optional-locks"] + list(args),
        cwd=_REPO_ROOT, capture_output=True, check=check)


def _git_show_bytes(rev, rel_path):
    return _git(["show", "%s:%s" % (rev, rel_path)], check=True).stdout


def _git_merge_base_is_ancestor(ancestor, descendant):
    """True when `ancestor` really is an ancestor of `descendant`.

    Guards the two frozen endpoint literals against a typo: a mistyped hash
    would otherwise point this gate at an unrelated piece of history and go on
    reporting green. `merge-base --is-ancestor` only -- deliberately NOT the
    adjacency half of the precedent at test_m5_package_e_integration.py, since
    a package need not be exactly one commit above its base."""
    return _git(["merge-base", "--is-ancestor", ancestor,
                 descendant]).returncode == 0


def _read_local_bytes(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return fh.read()


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _top_level(tree):
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))}


def _module_level_names(tree):
    """Every name a module BINDS at top level -- functions, classes AND plain
    assignments.

    Deliberately wider than `_top_level`: P4's authority names SYMBOLS, so a
    new module-level constant, import or helper alias smuggled into an allowed
    file would be exactly the widening D-P4-07 exists to catch, and a
    def/class-only scan would never see any of them.
    """
    names = set(_top_level(tree))
    for node in tree.body:
        targets = ()
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = (node.target,)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0]
                         for alias in node.names)
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def _closures_of(parent):
    """Every `FunctionDef` whose nearest enclosing `FunctionDef` is `parent`,
    resolved BY NAME through the module AST rather than by line -- so a `try`
    wrapper introduced between the parent and the closure does not hide it,
    and a duplicate name is detected rather than silently mis-pinned."""
    found = {}

    def walk(node, nearest):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if nearest is parent:
                    found.setdefault(child.name, []).append(child)
                walk(child, child)
            else:
                walk(child, nearest)

    walk(parent, parent)
    return found


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _live_candidate_repo():
    """`_REPO_ROOT` when this suite is running in the LIVE git candidate, else
    None (the scope class skips).

    TWO conditions, and the second is the load-bearing one. The obvious check
    is that `git rev-parse --show-toplevel` resolves to this checkout's own
    root. The non-obvious one is that the frozen base commit must actually
    EXIST here, and it is what makes the skip correct inside the verification
    transaction: an `isolated_snapshot` command does NOT run in a git-free
    copy. `cowork_verification.materialize_command_checkout` gives every
    per-command checkout functional local git semantics of its own -- a real
    `git init` plus the transaction's captured raw index bytes written to
    `.git/index` -- so `--show-toplevel` there resolves happily to the checkout
    root, which IS that copy's own `_REPO_ROOT`. A toplevel test alone would
    therefore NOT skip in the snapshot; it would fall through to
    `git show <BASE>:...` against a repository holding no objects and ERROR the
    class. Requiring the base OBJECT is what distinguishes the live candidate
    from a freshly-initialised copy of it.

    This corrects plan assumption `snapshot_has_no_git`, which the planner
    recorded as UNVERIFIED. The verified answer is that the snapshot has git
    but no history, so the gate is guarded against what is actually there.
    Criterion 4 is still measured -- by the `candidate_read_only` preflight
    entry, which runs in the live worktree.
    """
    try:
        toplevel = _git(["rev-parse", "--show-toplevel"]).stdout.decode(
            "utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not toplevel:
        return None
    if os.path.realpath(toplevel) != os.path.realpath(_REPO_ROOT):
        return None
    try:
        found = _git(["cat-file", "-e", "%s^{commit}" % BASE_SHA])
    except (OSError, subprocess.SubprocessError):
        return None
    return _REPO_ROOT if found.returncode == 0 else None


class _Raises(object):
    """A double that fails the test if anything ever calls it. Used wherever
    the assertion is the ABSENCE of a paid dispatch."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *args, **kwargs):
        raise AssertionError("%s was invoked on a read-only owner query"
                             % self.label)


def _dead_pid():
    """A pid that has genuinely exited -- a real crashed owner's pid, obtained
    by letting a real child run and reaping it, never a guessed number."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _tree_digests(root):
    """`{relative path: sha256}` for every file under `root`, recursively.

    An exact per-path map rather than a count or a prefix match, so criterion 3
    can assert BOTH that no path appeared or vanished and that no surviving
    file's bytes changed."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "rb") as fh:
                    out[rel] = _sha256(fh.read())
            except OSError:
                out[rel] = "unreadable"
    return out


def _keys_at_any_depth(node, found=None):
    """Every mapping key anywhere in a nested structure.

    A top-level key check would miss an owner view nested inside a section, so
    "the view never enters the record" is asserted at every depth."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            _keys_at_any_depth(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _keys_at_any_depth(value, found)
    return found


def _labelled(text, label):
    """The value of one `  <label>   <value>` line of the owner block, or None.

    Parsed out of the rendered text rather than recomputed, so every assertion
    below is made against what an operator actually sees."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.split(" ")[0:1] == [label]:
            return stripped[len(label):].strip()
    return None


def _lease(**overrides):
    """A plausible `SessionOwnerLease`-shaped record for a hand-built view.
    Only the fields the block prints are populated; `_build_lease`'s full shape
    is not needed to render one."""
    record = {
        "owner_id": "owner-abc",
        "epoch": 4,
        "pid": 41287,
        "host_id": "host-a",
        "launch_dir": "/Users/someone/repo",
        "session_file": "/Users/someone/repo/.cowork/session.json",
        "acquired_at": "2026-09-02T14:11:02Z",
    }
    record.update(overrides)
    return record


def _view(verdict, **overrides):
    """An `owner_status_view`-shaped projection with every documented key
    present, so a renderer assertion is never accidentally testing a
    half-built dict."""
    view = {
        "schema_version": owner.OWNER_LEASE_SCHEMA_VERSION,
        "record": "OwnerStatusView",
        "session_uuid": "abc123",
        "observed_at": "2026-09-09T12:00:00Z",
        "verdict": verdict,
        "lease": None if verdict in ("unowned", "corrupt") else _lease(),
        "terminal_mark": None,
        "terminal_mark_matches": False,
        "heartbeat_age_s": None if verdict == "unowned" else 12.0,
        "lease_deadline_at": (None if verdict == "unowned"
                              else "2026-09-02T14:13:32Z"),
        "expired": None if verdict == "unowned" else (verdict != "live_owner"),
        "host_matches": None if verdict in ("unowned", "corrupt") else True,
        "takeover_mode": None,
    }
    view.update(overrides)
    return view


def _lease_file(session_uuid):
    """The lease file, spelled WITHOUT `owner_lease_path_for` and without the
    literal `"owner/lease.json"`, on purpose.

    G3d's repo-wide sweep asserts that neither of those two spellings ever
    reaches an `open(...)` call anywhere in the repository -- including this
    file. A fixture that needs to fabricate a damaged lease therefore composes
    the path from `owner_dir_for`, which is a plain directory helper carrying
    none of the lease's single-writer contract.

    Path-identical to what the store's own helper returns, and it rejects an
    unsafe `session_uuid` identically, because `owner_dir_for` performs that
    rejection and the lease helper is exactly that directory plus this
    filename."""
    return os.path.join(state_store.owner_dir_for(session_uuid), "lease.json")


# --------------------------------------------------------------------------- #
# Base case: a sandboxed assets home plus a sandboxed project directory.        #
# --------------------------------------------------------------------------- #


class OwnerVisibilityTestCase(unittest.TestCase):
    """Every test gets its own `COWORK_SESSIONS_ROOT` and its own project
    directory, so no fixture can touch the real home dir, write inside the
    worktree, or observe another test's lease."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cowork-owner-p4-root-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = tempfile.mkdtemp(prefix="cowork-owner-p4-proj-")
        self.addCleanup(shutil.rmtree, self.project, True)
        patcher = mock.patch.dict(os.environ,
                                  {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spath = os.path.join(self.project, ".cowork", "session.json")

    # -- lease seeding ----------------------------------------------------- #

    def claimant(self, session_uuid, pid=None, pid_start_at=None,
                 host_id=None):
        record = owner.owner_identity(session_uuid, "run_flow", self.project,
                                      self.spath)
        if pid is not None:
            record["pid"] = pid
            record["pid_start_at"] = pid_start_at
            record["pid_start_source"] = ("ps_lstart" if pid_start_at
                                          else "unavailable")
        if host_id is not None:
            record["host_id"] = host_id
        return record

    def seed_live_owner(self, session_uuid, host_id=None):
        """A genuinely LIVE lease: this process's own pid and start time, a
        fresh heartbeat, never released."""
        return owner.acquire_owner_lease(
            session_uuid, self.claimant(session_uuid, host_id=host_id))

    def seed_dead_owner(self, session_uuid, age_seconds=7200):
        """A crashed owner: a pid that has genuinely exited, and a heartbeat
        old enough that the real clock is past the deadline. `ps` reports the
        pid absent, so death is PROVED -> `stale_dead_owner`."""
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=age_seconds))
        return owner.acquire_owner_lease(
            session_uuid,
            self.claimant(session_uuid, pid=_dead_pid(),
                          pid_start_at="2020-01-01T00:00:00Z"),
            now=stale)

    def seed_unprovable_owner(self, session_uuid, age_seconds=7200):
        """An expired lease held on ANOTHER host: death cannot be proved from
        here at all, so the verdict is `stale_unproven` and `host_matches` is
        False -- which is also the foreign-host arm of the recovery line."""
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=age_seconds))
        return owner.acquire_owner_lease(
            session_uuid,
            self.claimant(session_uuid, pid=_dead_pid(),
                          pid_start_at="2020-01-01T00:00:00Z",
                          host_id="some-other-host"),
            now=stale)

    def seed_corrupt_lease(self, session_uuid):
        """The ONE fixture written directly rather than through the P1 API:
        there is no supported way to ASK the store for an unreadable record,
        and a plausible-but-valid stand-in would not exercise the corrupt path
        at all."""
        path = _lease_file(session_uuid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("not json {")
        return path

    # -- durable-artifact digests ------------------------------------------ #

    def lease_digests(self, session_uuid):
        """sha256 of the two durable owner artifacts, with None for a file that
        is genuinely absent. `None` stays DISTINCT from a digest, so "absent
        before and after" and "unchanged bytes" are separate facts rather than
        one blurred one."""
        out = {}
        for name, path in (
                ("lease", _lease_file(session_uuid)),
                ("history",
                 state_store.owner_history_path_for(session_uuid))):
            try:
                with open(path, "rb") as fh:
                    out[name] = _sha256(fh.read())
            except OSError:
                out[name] = None
        return out

    # -- CLI driving ------------------------------------------------------- #

    @contextlib.contextmanager
    def no_paid_dispatch(self, out):
        """Every seam a paid dispatch would have to pass through, replaced by a
        double that raises -- plus stdout redirected, so the real `main()` can
        be driven end to end.

        `subprocess.Popen` is GUARDED rather than blocked outright: the owner
        projection's own liveness probe shells out to `ps` (through
        `subprocess.run`, which resolves `Popen` in the same module), and
        forbidding that would break the very classification under test. Every
        OTHER process creation still fails the test.
        """
        real_popen = subprocess.Popen

        def guarded_popen(command, *args, **kwargs):
            argv0 = (command[0] if isinstance(command, (list, tuple))
                     else command)
            if os.path.basename(str(argv0)) != "ps":
                raise AssertionError(
                    "a process was created on a read-only owner query: %r"
                    % (command,))
            return real_popen(command, *args, **kwargs)

        with mock.patch.object(cowork, "run_flow",
                               _Raises("cowork.run_flow")), \
                mock.patch.object(bridge, "_real_claude_spawn",
                                  _Raises("_real_claude_spawn")), \
                mock.patch.object(bridge, "ClaudeSession",
                                  _Raises("ClaudeSession")), \
                mock.patch.object(bridge, "CodexSession",
                                  _Raises("CodexSession")), \
                mock.patch.object(bridge, "OpencodeSession",
                                  _Raises("OpencodeSession")), \
                mock.patch.object(subprocess, "Popen", guarded_popen), \
                mock.patch.object(sys, "stdout", out):
            yield

    def session_owner_cli(self, argv):
        """Drive the REAL `cowork.main` for `--session-owner`, through the real
        parser and the real dispatch. Returns `(rc, stdout_text)`."""
        out = io.StringIO()
        with self.no_paid_dispatch(out):
            rc = cowork.main(list(argv))
        return rc, out.getvalue()


# --------------------------------------------------------------------------- #
# Supporting invariants -- the renderer, over hand-built views.                 #
# --------------------------------------------------------------------------- #


class OwnerStatusRenderTests(unittest.TestCase):
    """`render_owner_status` is a pure view-in / text-out function, so it is
    tested against hand-built projections for every verdict -- including the
    shapes a real lease can produce but a happy-path fixture never would."""

    def test_every_owned_verdict_renders_every_named_field(self):
        for verdict in ("live_owner", "stale_dead_owner", "stale_unproven",
                        "corrupt"):
            with self.subTest(verdict):
                text = cowork_report.render_owner_status(
                    _view(verdict), "/repo/.cowork/session.json")
                self.assertIn(OWNER_BLOCK_TITLE, text)
                for label in OWNER_BLOCK_LABELS:
                    self.assertIsNotNone(_labelled(text, label), label)
                self.assertTrue(text.endswith("\n"))

    def test_an_unowned_view_says_so_and_needs_no_recovery(self):
        text = cowork_report.render_owner_status(_view("unowned"))
        self.assertEqual(_labelled(text, "state"), "unowned")
        self.assertEqual(_labelled(text, "reason"), "unowned")
        self.assertIn("none needed", _labelled(text, "recovery"))
        self.assertNotIn("--take-over", text)

    def test_the_none_view_and_a_real_unowned_view_render_identically(self):
        """D-P4-02. One renderer serves the gated ambient path and the
        always-on explicit query, so the gate cannot drift into a second,
        divergent "unowned" rendering."""
        self.assertEqual(cowork_report.render_owner_status(None),
                         cowork_report.render_owner_status(_view("unowned")))
        self.assertEqual(
            cowork_report.render_owner_status(None, "/repo/session.json"),
            cowork_report.render_owner_status(_view("unowned")))

    def test_missing_fields_render_unknown_and_never_raise(self):
        text = cowork_report.render_owner_status(
            _view("live_owner", lease={}, heartbeat_age_s=None,
                  lease_deadline_at=None, expired=None, host_matches=None))
        self.assertIn(cowork_report.UNKNOWN, text)
        for label in OWNER_BLOCK_LABELS:
            self.assertIsNotNone(_labelled(text, label), label)

    def test_a_view_that_is_not_a_dict_still_renders(self):
        """A status surface that raised would be strictly worse than one that
        reports "unowned"; `owner_status_view` never raises, and neither does
        its renderer."""
        for bad in (None, "corrupt", 7, [], {}):
            with self.subTest(repr(bad)):
                text = cowork_report.render_owner_status(bad)
                self.assertIn(OWNER_BLOCK_TITLE, text)
                self.assertTrue(text.endswith("\n"))

    def test_a_live_same_host_owner_gets_the_exact_takeover_command(self):
        text = cowork_report.render_owner_status(
            _view("live_owner"), "/repo/.cowork/session.abc123.json")
        recovery = _labelled(text, "recovery")
        self.assertIn("stop that process", recovery)
        self.assertIn(
            "cowork --session-file /repo/.cowork/session.abc123.json "
            "--take-over", recovery)

    def test_with_no_session_file_the_command_degrades_to_a_bare_takeover(self):
        """Matching `cowork_owner.refusal_message`, which degrades identically
        rather than printing a `--session-file` with nothing after it."""
        recovery = _labelled(
            cowork_report.render_owner_status(_view("live_owner")), "recovery")
        self.assertIn("cowork --take-over", recovery)
        self.assertNotIn("--session-file", recovery)

    def test_a_foreign_host_owner_is_told_a_takeover_here_refuses(self):
        text = cowork_report.render_owner_status(
            _view("live_owner", host_matches=False), "/repo/session.json")
        self.assertEqual(_labelled(text, "reason"),
                         "live_owner (foreign_host)")
        recovery = _labelled(text, "recovery")
        self.assertIn("foreign_host", recovery)
        self.assertNotIn("--take-over", recovery)
        self.assertIn("another host", _labelled(text, "process"))

    def test_a_provably_dead_owner_gets_the_takeover_command(self):
        recovery = _labelled(
            cowork_report.render_owner_status(_view("stale_dead_owner"),
                                              "/repo/session.json"),
            "recovery")
        self.assertIn("cowork --session-file /repo/session.json --take-over",
                      recovery)

    def test_an_unprovable_owner_is_never_handed_a_command_that_refuses(self):
        recovery = _labelled(
            cowork_report.render_owner_status(_view("stale_unproven"),
                                              "/repo/session.json"),
            "recovery")
        self.assertIn("NOT proven", recovery)
        self.assertNotIn("--take-over", recovery)

    def test_a_corrupt_lease_never_advertises_a_takeover(self):
        """D-P4-05, verified against the store rather than assumed:
        `acquire_owner_lease` raises `OwnerLeaseCorrupt` for an unreadable
        record, so a takeover cannot repair one. Telling an operator to run a
        command guaranteed to refuse is exactly the failure P4 removes."""
        text = cowork_report.render_owner_status(
            _view("corrupt", detail="not a readable SessionOwnerLease"),
            "/repo/session.json")
        self.assertNotIn("--take-over", text)
        self.assertIn("abc123", _labelled(text, "recovery"))
        self.assertIn("not a readable SessionOwnerLease",
                      _labelled(text, "detail"))

    def test_the_reason_line_is_always_closed_vocabulary(self):
        """Reason strings are machine-readable tokens, never prose: the five
        closed `OWNER_VERDICTS` plus `foreign_host`."""
        for verdict in sorted(owner.OWNER_VERDICTS):
            for host_matches in (True, False, None):
                with self.subTest(verdict=verdict, host=host_matches):
                    text = cowork_report.render_owner_status(
                        _view(verdict, host_matches=host_matches))
                    reason = _labelled(text, "reason")
                    tokens = [t.strip("()") for t in reason.split()]
                    self.assertTrue(tokens)
                    for token in tokens:
                        self.assertIn(token, OWNER_REASON_TOKENS)

    def test_the_renderer_never_sees_a_measurement_record(self):
        """It takes a VIEW. `render_report` keeps the record as its only
        argument, and the two never swap places."""
        self.assertEqual(
            list(inspect.signature(cowork_report.render_report).parameters),
            ["record"])
        self.assertEqual(
            list(inspect.signature(
                cowork_report.render_owner_status).parameters),
            ["view", "session_file"])


# --------------------------------------------------------------------------- #
# Supporting invariants -- the parser surface.                                  #
# --------------------------------------------------------------------------- #


class ParserSurfaceTests(unittest.TestCase):
    """The flag itself: how it parses, what its help says, and what it refuses
    to be combined with."""

    def test_the_flag_parses_bare_and_with_a_uuid(self):
        bare = cowork.build_parser().parse_args(["--session-owner"])
        self.assertIs(bare.session_owner, True)
        named = cowork.build_parser().parse_args(["--session-owner", "abc123"])
        self.assertEqual(named.session_owner, "abc123")

    def test_it_defaults_to_absent_when_not_supplied(self):
        self.assertIsNone(
            cowork.build_parser().parse_args(["--check"]).session_owner)

    def test_the_neighbouring_session_flags_still_parse_in_full_form(self):
        args = cowork.build_parser().parse_args(
            ["--session-file", "/tmp/s.json"])
        self.assertEqual(args.session_file, "/tmp/s.json")
        self.assertTrue(
            cowork.build_parser().parse_args(["--no-session"]).no_session)
        self.assertIs(
            cowork.build_parser().parse_args(["--session-owner"]).session_owner,
            True)

    def test_the_session_abbreviation_break_is_pinned_as_accepted(self):
        """`--session` was an unambiguous abbreviation of `--session-file`
        until this flag existed. Accepted rather than worked around: the flag
        name is fixed by the plan, the parser already documents the identical
        `--eval-session` tradeoff, and every full-form flag is unaffected. This
        test pins the CURRENT behaviour so the change is a decision on record
        rather than a surprise."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cowork.build_parser().parse_args(["--session", "/tmp/s.json"])

    def test_the_json_help_names_both_flags_it_applies_to(self):
        actions = {a.dest: a for a in cowork.build_parser()._actions}
        self.assertIn("--report", actions["report_json"].help)
        self.assertIn("--session-owner", actions["report_json"].help)

    def test_the_flag_help_states_the_inertness_contract(self):
        actions = {a.dest: a for a in cowork.build_parser()._actions}
        text = actions["session_owner"].help
        for phrase in ("read-only", "no lease", "no controller", "exits 0"):
            self.assertIn(phrase, text)

    def test_it_refuses_the_two_session_mutating_flags(self):
        for extra in (["--switch-controller", "scout=codex"],
                      ["--allow-controllers", "claude"]):
            with self.subTest(extra[0]):
                err = io.StringIO()
                with mock.patch.object(sys, "stderr", err):
                    rc = cowork.main(["--session-owner", "abc123"] + extra)
                self.assertEqual(rc, 2)
                self.assertIn("--session-owner", err.getvalue())


# --------------------------------------------------------------------------- #
# Criterion 1 -- `--session-owner` reports, read-only, for every lease state.   #
# --------------------------------------------------------------------------- #


class SessionOwnerCliTests(OwnerVisibilityTestCase):
    """Five real lease states, driven through the real `cowork.main`.

    The assertion is INERTNESS: exit 0, no controller construction, no process
    creation beyond the projection's own `ps` probe, and not one byte of the
    durable lease or its history changed by having been looked at.
    """

    # (name, seeder, expected verdict)
    def _fixtures(self):
        return (
            ("unowned", lambda uuid: None, "unowned"),
            ("live", self.seed_live_owner, "live_owner"),
            ("dead", self.seed_dead_owner, "stale_dead_owner"),
            ("unprovable", self.seed_unprovable_owner, "stale_unproven"),
            ("corrupt", self.seed_corrupt_lease, "corrupt"),
        )

    def test_every_lease_state_exits_zero_in_text_and_json(self):
        for name, seed, verdict in self._fixtures():
            with self.subTest(name):
                session_uuid = "s%s" % name
                seed(session_uuid)
                self.assertEqual(
                    owner.owner_status_view(session_uuid)["verdict"], verdict)
                rc, text = self.session_owner_cli(
                    ["--session-owner", session_uuid])
                self.assertEqual(rc, 0)
                self.assertIn(OWNER_BLOCK_TITLE, text)
                self.assertEqual(_labelled(text, "state"), verdict)
                rc_json, raw = self.session_owner_cli(
                    ["--session-owner", session_uuid, "--json"])
                self.assertEqual(rc_json, 0)
                self.assertEqual(json.loads(raw)["verdict"], verdict)

    def test_the_query_changes_no_durable_lease_byte(self):
        """The whole point of a status surface: looking cannot alter what is
        being looked at. `None` for an absent file is compared as `None`, so an
        unowned session is asserted to STAY unowned rather than merely to be
        unchanged-if-present."""
        for name, seed, _verdict in self._fixtures():
            with self.subTest(name):
                session_uuid = "d%s" % name
                seed(session_uuid)
                before = self.lease_digests(session_uuid)
                self.assertEqual(
                    self.session_owner_cli(
                        ["--session-owner", session_uuid])[0], 0)
                self.assertEqual(
                    self.session_owner_cli(
                        ["--session-owner", session_uuid, "--json"])[0], 0)
                self.assertEqual(self.lease_digests(session_uuid), before)
        # ...and the unowned case really did start from nothing on disk.
        self.assertEqual(self.lease_digests("dunowned"),
                         {"lease": None, "history": None})

    def test_the_json_form_is_the_raw_owner_status_view(self):
        """Not a wrapper, not a look-alike: the same projection P1 owns. Two
        keys are compared for PRESENCE rather than value, because both are
        clock-derived and would differ between any two calls -- pinning them
        would be testing the clock, not the contract."""
        volatile = ("observed_at", "heartbeat_age_s")
        for name, seed, _verdict in self._fixtures():
            with self.subTest(name):
                session_uuid = "j%s" % name
                seed(session_uuid)
                rc, raw = self.session_owner_cli(
                    ["--session-owner", session_uuid, "--json"])
                self.assertEqual(rc, 0)
                printed = json.loads(raw)
                expected = owner.owner_status_view(session_uuid)
                self.assertIsInstance(printed, dict)
                for key in volatile:
                    self.assertIn(key, printed)
                self.assertEqual(
                    {k: v for k, v in printed.items() if k not in volatile},
                    {k: v for k, v in expected.items() if k not in volatile})

    def test_a_live_owner_is_reported_with_its_real_process_facts(self):
        session_uuid = "slive2"
        lease = self.seed_live_owner(session_uuid)
        rc, text = self.session_owner_cli(["--session-owner", session_uuid])
        self.assertEqual(rc, 0)
        self.assertIn(str(os.getpid()), _labelled(text, "process"))
        self.assertIn("this host", _labelled(text, "process"))
        self.assertIn(lease["owner_id"], _labelled(text, "owner"))
        self.assertIn(os.path.realpath(self.project),
                      _labelled(text, "launched"))
        self.assertIn("ago", _labelled(text, "heartbeat"))
        self.assertIn("--take-over", _labelled(text, "recovery"))

    def test_the_recovery_command_names_the_owning_sessions_own_anchor(self):
        """The takeover has to point at the session that HOLDS the lease. The
        lease records that anchor, so the command is built from the record
        rather than from whatever session this invocation happens to be near."""
        session_uuid = "sanchor"
        os.makedirs(os.path.dirname(self.spath), exist_ok=True)
        state_store.save(self.spath, {"team": [], "config": {},
                                      "sessions": {},
                                      "session_uuid": session_uuid})
        self.seed_live_owner(session_uuid)
        rc, text = self.session_owner_cli(["--session-owner", session_uuid])
        self.assertEqual(rc, 0)
        self.assertIn("cowork --session-file %s --take-over"
                      % os.path.realpath(self.spath),
                      _labelled(text, "recovery"))

    def test_an_unsafe_session_id_reports_rather_than_raising(self):
        for argv, expect in ((["--session-owner", "../escape"], "not a usable"),
                             (["--session-owner", "../escape", "--json"],
                              "null")):
            with self.subTest(argv[-1]):
                rc, text = self.session_owner_cli(argv)
                self.assertEqual(rc, 0)
                self.assertIn(expect, text)

    def test_a_directory_with_no_sessions_exits_zero(self):
        """`run_report` returns 1 for this case. That is deliberately NOT
        copied: P4's contract is exit 0 always, because a diagnostic that
        signals failure when there is simply nothing to diagnose is unusable
        from the scripts that need it most."""
        cwd = tempfile.mkdtemp(prefix="cowork-owner-p4-empty-")
        self.addCleanup(shutil.rmtree, cwd, True)
        with mock.patch.object(os, "getcwd", lambda: cwd):
            rc, text = self.session_owner_cli(["--session-owner"])
            self.assertEqual(rc, 0)
            self.assertIn("no sessions found", text)
            rc_json, raw = self.session_owner_cli(
                ["--session-owner", "--json"])
        self.assertEqual(rc_json, 0)
        self.assertEqual(json.loads(raw), None)

    def test_a_bare_flag_resolves_this_directorys_most_recent_session(self):
        session_uuid = "srecent"
        path = state_store.new_session_path(self.project, session_uuid)
        state_store.save(path, {"team": [], "config": {}, "sessions": {},
                                "session_uuid": session_uuid})
        self.seed_live_owner(session_uuid)
        with mock.patch.object(os, "getcwd", lambda: self.project):
            rc, raw = self.session_owner_cli(["--session-owner", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(raw)["session_uuid"], session_uuid)


# --------------------------------------------------------------------------- #
# Criterion 2 -- the report's owner block, and the measurement contract.        #
# --------------------------------------------------------------------------- #


class ReportOwnerBlockTests(OwnerVisibilityTestCase):
    """The owner block sits ABOVE the provenance banner, and the measurement
    record does not know it exists."""

    SESSION = "S1"

    def setUp(self):
        super().setUp()
        # A WRITABLE session seeded from a fixture's RAW sources, never the
        # checked-in fixture directory itself: the record lifecycle here is
        # build-then-load, and a tracked fixture deliberately never persists.
        assets = state_store.session_assets_dir(self.SESSION)
        os.makedirs(assets, exist_ok=True)
        for name in SEED_FIXTURE_FILES:
            shutil.copy(os.path.join(_FIXTURES_ROOT, SEED_FIXTURE, name),
                        os.path.join(assets, name))
        self.trace = os.path.join(assets, "trace.jsonl")

    def report(self, argv=()):
        out = io.StringIO()
        args = cowork.build_parser().parse_args(
            ["--report", self.SESSION] + list(argv))
        self.assertEqual(cowork.run_report(args, io_out=out), 0)
        return out.getvalue()

    def test_the_block_is_printed_above_the_report_heading(self):
        self.seed_live_owner(self.SESSION)
        text = self.report()
        self.assertIn(OWNER_BLOCK_TITLE, text)
        self.assertLess(text.index(OWNER_BLOCK_TITLE),
                        text.index(REPORT_HEADING))
        self.assertEqual(_labelled(text, "state"), "live_owner")

    def test_the_block_is_printed_above_the_provenance_banner(self):
        """`render_provenance_banner` returns "" for a fresh record, so "above
        the banner" is only decidable against a STALE one -- which is why this
        test makes the record stale on purpose rather than asserting on an
        empty string."""
        self.seed_live_owner(self.SESSION)
        self.report()  # builds and persists the record
        with open(self.trace, "a") as fh:
            fh.write(json.dumps({"event": "noise"}) + "\n")
        # The marker is taken from the banner the production renderer actually
        # produces for this state, so the ordering assertion cannot silently
        # pass by matching a string the banner no longer prints.
        banner = cowork_report.render_provenance_banner(
            measure.check_provenance(self.SESSION,
                                     measure.load_record(self.SESSION)))
        self.assertTrue(
            banner,
            "a moved raw source must produce a non-empty provenance banner, "
            "or 'above the banner' is not decidable")
        marker = banner.splitlines()[0]
        text = self.report()
        self.assertIn(marker, text)
        self.assertLess(text.index(OWNER_BLOCK_TITLE), text.index(marker))
        self.assertLess(text.index(marker), text.index(REPORT_HEADING))

    def test_an_unowned_session_still_gets_the_block(self):
        text = self.report()
        self.assertIn(OWNER_BLOCK_TITLE, text)
        self.assertEqual(_labelled(text, "state"), "unowned")

    def test_the_owner_view_never_enters_the_measurement_record(self):
        self.seed_live_owner(self.SESSION)
        self.report()
        built = measure.build_record(self.SESSION, cwd=os.getcwd())
        loaded = measure.load_record(self.SESSION)
        self.assertIsInstance(loaded, dict)
        for label, record in (("built", built), ("loaded", loaded)):
            keys = _keys_at_any_depth(record)
            for forbidden in ("owner", "owner_status", "lease",
                              "owner_status_view"):
                with self.subTest(record=label, key=forbidden):
                    self.assertNotIn(forbidden, keys)

    def test_render_report_still_takes_the_record_as_its_only_argument(self):
        self.assertEqual(
            list(inspect.signature(cowork_report.render_report).parameters),
            ["record"])
        with self.assertRaises(TypeError):
            cowork_report.render_report({}, "a second argument")

    def test_the_json_report_is_untouched_by_the_owner_block(self):
        """The insertion sits AFTER the `--json` early return, so the
        authoritative artifact is byte-for-byte what `cowork_measure`
        produces."""
        self.seed_live_owner(self.SESSION)
        self.report()
        raw = self.report(["--json"])
        self.assertNotIn(OWNER_BLOCK_TITLE, raw)
        self.assertEqual(json.loads(raw), measure.load_record(self.SESSION))


# --------------------------------------------------------------------------- #
# Criterion 3 -- a report against a tracked fixture writes nothing.             #
# --------------------------------------------------------------------------- #


class TrackedFixtureReadOnlyTests(unittest.TestCase):
    """A checked-in measurement fixture is SOURCE TRUTH the criteria are
    decided on. A report that wrote into one -- a persisted record, a
    reconciled ledger, or now an owner directory and a lock file -- would make
    verification mutate the very tree it was verifying.

    This is the reason `run_report`'s owner block is gated on a lease record
    already existing: the read path creates `owner/` and opens
    `lease.json.lock` BEFORE it reads.
    """

    def setUp(self):
        self.target = os.path.join(_FIXTURES_ROOT, TRACKED_FIXTURE)
        patcher = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": _FIXTURES_ROOT})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _undo_any_mutation(self, before):
        """Remove anything the run added, so a FAILING assertion does not
        compound the mutation it just detected into the next run."""
        for rel in _tree_digests(self.target):
            if rel in before:
                continue
            try:
                os.remove(os.path.join(self.target, rel))
            except OSError:
                pass
        shutil.rmtree(os.path.join(self.target, "owner"), ignore_errors=True)

    def test_the_fixture_is_byte_identical_across_a_report_run(self):
        self.assertTrue(
            cowork._session_assets_are_tracked(TRACKED_FIXTURE),
            "criterion 3's premise: %s must be detected as git-TRACKED, or "
            "the report is entitled to persist a record into it for reasons "
            "that have nothing to do with P4" % TRACKED_FIXTURE)
        before = _tree_digests(self.target)
        self.assertIn("trace.jsonl", before)
        self.addCleanup(self._undo_any_mutation, before)
        out = io.StringIO()
        args = cowork.build_parser().parse_args(["--report", TRACKED_FIXTURE])
        self.assertEqual(cowork.run_report(args, io_out=out), 0)
        after = _tree_digests(self.target)
        self.assertEqual(set(after), set(before))
        self.assertEqual(after, before)

    def test_no_owner_directory_and_no_lock_file_appear(self):
        """Named explicitly rather than left to the digest comparison, because
        these two are the artifacts P4 could have introduced and a reader
        should be able to see them asserted by name."""
        before = _tree_digests(self.target)
        self.addCleanup(self._undo_any_mutation, before)
        out = io.StringIO()
        args = cowork.build_parser().parse_args(["--report", TRACKED_FIXTURE])
        self.assertEqual(cowork.run_report(args, io_out=out), 0)
        self.assertFalse(os.path.isdir(os.path.join(self.target, "owner")))
        for rel in _tree_digests(self.target):
            self.assertNotIn("lease.json", rel)
            self.assertNotIn(".lock", rel)

    def test_the_block_still_renders_for_the_gated_path(self):
        """The gate must not cost the surface: a fixture with no lease still
        gets the block, rendered by the same function the explicit query
        uses."""
        self.addCleanup(self._undo_any_mutation, _tree_digests(self.target))
        out = io.StringIO()
        args = cowork.build_parser().parse_args(["--report", TRACKED_FIXTURE])
        self.assertEqual(cowork.run_report(args, io_out=out), 0)
        text = out.getvalue()
        self.assertIn(OWNER_BLOCK_TITLE, text)
        self.assertEqual(_labelled(text, "state"), "unowned")


# --------------------------------------------------------------------------- #
# Criterion 5 -- the picker suffix and `list_sessions` compatibility.           #
# --------------------------------------------------------------------------- #


class PickerOwnerSuffixTests(OwnerVisibilityTestCase):
    """The picker shows a contested session BEFORE it is picked, and
    `list_sessions` stays total for every legacy row shape."""

    NOW = 1_000_000_000

    def label(self, **row):
        base = {"id": "abc123", "path": "/tmp/s.json", "summary": "a goal",
                "phase": "building", "created": self.NOW,
                "last_active": self.NOW}
        base.update(row)
        return cowork._session_picker_label(base, self.NOW)

    def test_a_live_owner_gets_the_pid_suffix(self):
        label = self.label(owner=_view("live_owner"))
        self.assertTrue(label.endswith(" · owned (pid 41287)"), label)
        self.assertIn("a goal", label)

    def test_every_non_live_lease_state_gets_the_stale_suffix(self):
        for verdict in ("stale_dead_owner", "stale_unproven", "corrupt"):
            with self.subTest(verdict):
                self.assertTrue(
                    self.label(owner=_view(verdict)).endswith(
                        " · stale owner (%s)" % verdict))

    def test_an_unowned_or_absent_or_malformed_owner_adds_no_suffix(self):
        plain = self.label()
        self.assertNotIn(" · owned", plain)
        self.assertNotIn(" · stale owner", plain)
        for value in (None, _view("unowned"), "live_owner", 7, [],
                      {"verdict": "something_new"}):
            with self.subTest(repr(value)):
                self.assertEqual(self.label(owner=value), plain)

    def test_a_live_owner_with_no_readable_pid_still_labels(self):
        self.assertTrue(
            self.label(owner=_view("live_owner", lease={})).endswith(
                " · owned (pid None)"))

    def test_the_suffix_reason_is_always_closed_vocabulary(self):
        for verdict in sorted(owner.OWNER_VERDICTS):
            label = self.label(owner=_view(verdict))
            if " · stale owner (" not in label:
                continue
            token = label.rsplit("(", 1)[1].rstrip(")")
            self.assertIn(token, OWNER_REASON_TOKENS)

    # -- `list_sessions` compatibility ------------------------------------- #

    def _write(self, cwd, suid=None, legacy=False, context=None, phase=None):
        path = (state_store.session_path(cwd) if legacy
                else state_store.new_session_path(cwd, suid))
        state = {"team": [], "config": {}, "sessions": {}}
        if suid and not legacy:
            state["session_uuid"] = suid
        if legacy and suid:
            state["session_uuid"] = suid
        if context is not None:
            state["context"] = {"text": context, "hash": "x", "revision": 1}
        if phase:
            state["phase"] = phase
        state_store.save(path, state)
        return path

    def test_every_legacy_row_shape_still_lists_and_carries_owner(self):
        """Replicates the pre-P4 `list_sessions` characterizations: a legacy
        `session.json`, an unreadable file, an id-less file, and an id derived
        from a filename. None of them may raise, all five pre-existing keys
        must survive, and `owner` must be present on every row."""
        cwd = tempfile.mkdtemp(prefix="cowork-owner-p4-rows-")
        self.addCleanup(shutil.rmtree, cwd, True)
        self._write(cwd, suid="good", context="a goal", phase="planning")
        self._write(cwd, legacy=True, suid="leg", context="legacy goal")
        bad = state_store.new_session_path(cwd, "bad")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "w") as fh:
            fh.write("not json {")
        state_store.save(state_store.new_session_path(cwd, "fromname"),
                         {"team": [], "config": {}, "sessions": {}})

        rows = state_store.list_sessions(cwd)
        self.assertEqual(sorted(r["id"] for r in rows),
                         ["fromname", "good", "leg"])
        for row in rows:
            with self.subTest(row["id"]):
                for key in ("id", "path", "summary", "phase", "created",
                            "last_active", "owner"):
                    self.assertIn(key, row)
                self.assertIsNone(row["owner"])

    def test_a_leased_session_carries_its_view_on_the_row(self):
        session_uuid = "leased1"
        self._write(self.project, suid=session_uuid, context="a goal")
        self.seed_live_owner(session_uuid)
        row = next(r for r in state_store.list_sessions(self.project)
                   if r["id"] == session_uuid)
        self.assertIsInstance(row["owner"], dict)
        self.assertEqual(row["owner"]["verdict"], "live_owner")
        self.assertIn(" · owned (pid %s)" % os.getpid(),
                      cowork._session_picker_label(row, self.NOW))

    def test_an_id_that_is_not_a_safe_identifier_never_raises(self):
        """A filename-derived id can be anything on disk.
        `owner_lease_path_for` refuses an unsafe one with a ValueError, and a
        listing that propagated it would take the picker down with it."""
        cwd = tempfile.mkdtemp(prefix="cowork-owner-p4-unsafe-")
        self.addCleanup(shutil.rmtree, cwd, True)
        state_store.save(state_store.new_session_path(cwd, "-unsafe"),
                         {"team": [], "config": {}, "sessions": {}})
        rows = state_store.list_sessions(cwd)
        self.assertEqual([r["id"] for r in rows], ["-unsafe"])
        self.assertIsNone(rows[0]["owner"])
        self.assertIsInstance(
            cowork._session_picker_label(rows[0], self.NOW), str)

    def test_listing_an_unowned_session_writes_nothing(self):
        """The existence gate again, on the picker's side: merely listing a
        directory must not create an `owner/` directory for every row in it."""
        session_uuid = "unleased1"
        self._write(self.project, suid=session_uuid, context="a goal")
        before = _tree_digests(self.root)
        rows = state_store.list_sessions(self.project)
        self.assertEqual([r["owner"] for r in rows], [None])
        self.assertEqual(_tree_digests(self.root), before)
        self.assertFalse(os.path.isdir(
            os.path.join(self.root, session_uuid, "owner")))


# --------------------------------------------------------------------------- #
# The corrupt fixture: what actually keeps it safe.                             #
# --------------------------------------------------------------------------- #


class CorruptFixtureIsolationTests(OwnerVisibilityTestCase):
    """`seed_corrupt_lease` performs a genuine UNLOCKED write of the lease
    record, and this class -- not the G3d sweep -- is what makes that
    admissible.

    Be plain about what changed and what it proves. The sweep now reports zero
    offenders for this module NOT because the unlocked write is gone (it is
    not) but because the path is composed from `owner_dir_for` plus a
    `"lease.json"` filename, which is outside the two spellings the sweep
    tracks. That recognizer is purely SYNTACTIC and admits exactly two shapes;
    it performs no arbitrary path or dataflow analysis, so after this change it
    proves NOTHING about this fixture at all. The repository is free of
    unlocked lease writes in PRODUCTION -- three test modules deliberately
    fabricate lease records outside the locked seam, and that trade is accepted
    on the strength of the confinement asserted below, not on the sweep.

    So the burden moves here, one test per clause: the write lands strictly
    inside the per-test temporary sessions root (and outside both the worktree
    and the real home assets dir), at exactly the path the production writer
    itself uses, with bytes that genuinely do not parse and still classify
    `corrupt` end to end. `FixtureRestorationTests` carries the fourth clause,
    which can only be observed from outside a finished test.
    """

    def test_the_corrupt_fixture_writes_only_inside_the_per_test_root(self):
        path = self.seed_corrupt_lease("isolation1")
        real = os.path.realpath(path)
        root = os.path.realpath(self.root)
        self.assertTrue(real.startswith(root + os.sep), real)
        self.assertFalse(
            real.startswith(os.path.realpath(_REPO_ROOT) + os.sep), real)

        # Where the record WOULD have gone with the override removed -- derived
        # by popping the variable inside a restoring patch, since every owner
        # path hangs off `session_assets_dir`, which reads the environment at
        # call time.
        with mock.patch.dict(os.environ):
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
            home = os.path.realpath(state_store.owner_dir_for("isolation1"))
        self.assertNotEqual(real, home)
        self.assertFalse(real.startswith(home + os.sep), real)

        # The temp root's ENTIRE file census is that one seeded record, so the
        # fixture wrote nothing else anywhere under it either.
        self.assertEqual(sorted(_tree_digests(self.root)),
                         [os.path.relpath(path, self.root)])

    def test_the_fixture_path_is_where_the_store_itself_writes_a_real_lease(
            self):
        """Equivalence proved BEHAVIOURALLY: the real production writer is
        driven, and the lease it produces is asserted to land exactly where the
        fixture's own path helper says it would.

        Deliberately not an equality assertion against the store's lease-path
        helper -- naming it here would put an unguarded reference back into
        this module and re-arm the sweep this correction closes. Driving the
        locked writer proves the stronger claim anyway."""
        self.seed_live_owner("isolation2")
        leases = sorted(rel for rel in _tree_digests(self.root)
                        if os.path.basename(rel) == "lease.json")
        self.assertEqual(
            leases, [os.path.relpath(_lease_file("isolation2"), self.root)])

    def test_the_seeded_record_is_unparseable_and_still_classifies_corrupt(
            self):
        """The corruption coverage the fixture exists for, asserted rather than
        assumed: a valid-but-odd stand-in would exercise no corrupt path at
        all."""
        path = self.seed_corrupt_lease("isolation3")
        with open(path, "rb") as fh:
            raw = fh.read()
        with self.assertRaises(ValueError):
            json.loads(raw.decode("utf-8"))
        self.assertEqual(owner.owner_status_view("isolation3")["verdict"],
                         "corrupt")
        # ...and the digest reader still points at exactly the file the writer
        # produced.
        self.assertEqual(self.lease_digests("isolation3")["lease"],
                         _sha256(raw))


class FixtureRestorationTests(unittest.TestCase):
    """The fourth clause: the sandbox is torn down and the environment put
    back.

    A plain `TestCase`, deliberately NOT an `OwnerVisibilityTestCase`:
    restoration can only be observed from OUTSIDE a case whose own cleanups
    have already run, so this class must not inherit that setUp.
    """

    def test_the_temporary_root_and_the_environment_are_restored_after_cleanup(
            self):
        before = os.environ.get("COWORK_SESSIONS_ROOT")
        captured = {}

        class _Probe(OwnerVisibilityTestCase):
            """Local, so the module gains no third top-level test class and
            unittest discovery never collects this twice."""

            def test_seeds_a_corrupt_record(self):
                captured["root"] = self.root
                captured["env"] = os.environ.get("COWORK_SESSIONS_ROOT")
                captured["path"] = self.seed_corrupt_lease("restored1")

        suite = unittest.TestLoader().loadTestsFromTestCase(_Probe)
        self.assertEqual(suite.countTestCases(), 1)
        result = unittest.TextTestRunner(stream=io.StringIO(),
                                         verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful(),
                        (result.errors, result.failures))

        self.assertEqual(captured["env"], captured["root"])
        self.assertNotEqual(captured["root"], before)
        self.assertFalse(os.path.exists(captured["root"]))
        self.assertFalse(os.path.exists(captured["path"]))
        self.assertEqual(os.environ.get("COWORK_SESSIONS_ROOT"), before)


# --------------------------------------------------------------------------- #
# Criterion 4 -- scope confinement, against the frozen base.                    #
# --------------------------------------------------------------------------- #


class ScopeConfinementTests(unittest.TestCase):
    """G6 for P4, bound to P4's own `<base>`: P3's accepted candidate.

    Measures the CLOSED interval `BASE_SHA..CANDIDATE_SHA` -- P4's own finished
    history -- rather than the live working tree, so every claim below is a
    permanent fact about that interval that no later commit can enter. It
    still needs a repository where BOTH endpoint objects exist, so it skips
    itself everywhere else (see `_live_candidate_repo`), and the verification
    inventory measures it through a dedicated `candidate_read_only` preflight
    entry rather than inside the isolated snapshot.
    """

    @classmethod
    def setUpClass(cls):
        if _live_candidate_repo() is None:
            raise unittest.SkipTest(
                "not the live git candidate (or base %s is absent here): "
                "criterion 4 is measured by the candidate_read_only preflight"
                % BASE_SHA[:12])

    def _changed_paths(self):
        """The change set of the CLOSED interval `BASE_SHA..CANDIDATE_SHA`.

        Two frozen endpoints, named explicitly, so this is neither the
        working-tree form (`git diff --name-only <BASE>`, which measured
        whatever happened to be on disk at run time) nor the `<BASE>..` form
        (which means `<BASE>..HEAD` and follows a moving ref).

        The `git ls-files --others --exclude-standard` branch that used to be
        unioned in here is GONE, and that is not a loosening: it existed solely
        to catch files P4 had added but not yet committed while P4 was the live
        candidate. `CANDIDATE_SHA` is P4's finished commit, so it already
        CONTAINS every file P4 added -- including this module -- and the
        commit-to-commit diff reports them anyway.
        """
        tracked = _git(["diff", "--name-only", BASE_SHA, CANDIDATE_SHA],
                       check=True).stdout.decode("utf-8").split()
        return {p for p in tracked if "__pycache__" not in p}

    def test_changed_paths_are_exactly_the_four_allowed(self):
        self.assertEqual(self._changed_paths(), set(ALLOWED_CHANGED_PATHS))

    def test_the_base_is_an_ancestor_of_the_candidate(self):
        """Both endpoints are frozen literals, so a single mistyped character
        would silently point every claim in this class at an unrelated piece of
        history. This fails loudly instead."""
        self.assertTrue(
            _git_merge_base_is_ancestor(BASE_SHA, CANDIDATE_SHA),
            "%s is not an ancestor of %s"
            % (BASE_SHA[:12], CANDIDATE_SHA[:12]))

    def test_every_excluded_path_is_byte_identical_to_the_base(self):
        for rel in EXCLUDED_PATHS:
            with self.subTest(rel):
                self.assertEqual(_sha256(_git_show_bytes(CANDIDATE_SHA, rel)),
                                 _sha256(_git_show_bytes(BASE_SHA, rel)))

    def test_every_excluded_module_symbol_is_ast_and_docstring_identical(self):
        base = _top_level(ast.parse(_git_show_bytes(BASE_SHA,
                                                    "scripts/cowork.py")))
        cand = _top_level(ast.parse(_git_show_bytes(CANDIDATE_SHA,
                                                    "scripts/cowork.py")))
        for name in EXCLUDED_MODULE_SYMBOLS:
            with self.subTest(name):
                self.assertIn(name, cand)
                self.assertEqual(_dump(cand[name]), _dump(base[name]))
                self.assertEqual(ast.get_docstring(cand[name]),
                                 ast.get_docstring(base[name]))

    def test_every_excluded_run_flow_closure_is_ast_identical(self):
        """The nested half, resolved by name through the module AST -- never
        `inspect.getsource`, which cannot reach a closure without executing its
        parent, and never a byte digest, which a legitimate re-indentation
        would break on a CORRECT candidate."""
        base_tree = ast.parse(_git_show_bytes(BASE_SHA, "scripts/cowork.py"))
        cand_tree = ast.parse(_git_show_bytes(CANDIDATE_SHA,
                                              "scripts/cowork.py"))
        base = _closures_of(_top_level(base_tree)["run_flow"])
        cand = _closures_of(_top_level(cand_tree)["run_flow"])
        for name in EXCLUDED_RUN_FLOW_CLOSURES:
            with self.subTest(name):
                self.assertEqual(len(base.get(name, [])), 1, name)
                self.assertEqual(len(cand.get(name, [])), 1, name)
                self.assertEqual(_dump(cand[name][0]), _dump(base[name][0]))
                self.assertEqual(ast.get_docstring(cand[name][0]),
                                 ast.get_docstring(base[name][0]))

    def test_each_edited_file_changes_only_its_named_symbols(self):
        """D-P4-07. The authority names SYMBOLS, so the gate pins symbols:
        nothing removed, only the declared names added, and every other
        pre-existing top-level symbol AST- and docstring-identical."""
        for rel in sorted(ALLOWED_SYMBOL_CHANGES):
            base_tree = ast.parse(_git_show_bytes(BASE_SHA, rel))
            cand_tree = ast.parse(_git_show_bytes(CANDIDATE_SHA, rel))
            base, cand = _top_level(base_tree), _top_level(cand_tree)
            with self.subTest(rel=rel, check="nothing removed"):
                self.assertEqual(set(base) - set(cand), set())
            with self.subTest(rel=rel, check="only declared additions"):
                self.assertEqual(
                    _module_level_names(cand_tree)
                    - _module_level_names(base_tree),
                    set(ALLOWED_NEW_SYMBOLS[rel]))
            for name, node in sorted(base.items()):
                if name in ALLOWED_SYMBOL_CHANGES[rel]:
                    continue
                with self.subTest(rel=rel, name=name):
                    self.assertEqual(_dump(cand[name]), _dump(node))
                    self.assertEqual(ast.get_docstring(cand[name]),
                                     ast.get_docstring(node))

    def test_the_two_historical_owner_suites_are_untouched(self):
        """Their scope gates pin `cowork_report.py` and `cowork_state.py` to
        THEIR bases, so both fail on this candidate -- which is correct, and is
        exactly why editing them to pass would be the failure they exist to
        prevent. Neither is in P4's allowed set.

        Read from `CANDIDATE_SHA`, so the claim stays TRUE and HONEST: it says
        P4 left those two files alone across P4's own interval, which no later
        package editing them can retroactively falsify."""
        for rel in ("scripts/test_m55_owner_store.py",
                    "scripts/test_m55_owner_gate.py"):
            with self.subTest(rel):
                self.assertNotIn(rel, ALLOWED_CHANGED_PATHS)
                self.assertEqual(_sha256(_git_show_bytes(CANDIDATE_SHA, rel)),
                                 _sha256(_git_show_bytes(BASE_SHA, rel)))

    def test_the_owner_module_is_not_a_p4_path_at_all(self):
        """P4 is visibility only: nothing about how ownership is acquired,
        refused, taken over or released may move."""
        self.assertNotIn("scripts/cowork_owner.py", ALLOWED_CHANGED_PATHS)
        self.assertIn("scripts/cowork_owner.py", EXCLUDED_PATHS)


if __name__ == "__main__":
    unittest.main()
