#!/usr/bin/env python3
"""Focused suite for issue #64 implementation package **P1** -- the owner-lease
store and its liveness classifier -- on the accredited base
`4aa89e78a509e28077ca67e6f6f148c892e089b4`.

P1 is pure store plus pure classification: no wiring into `cowork.py`, no
dispatch, no send, no spawn. So everything proven here is proven against the
DURABLE ARTIFACTS and REAL OS PROCESSES, never against an in-memory flag:

  - **G1c / F1 (concurrency).** Eight REAL concurrent OS processes race the
    first acquisition of one session. Exactly one wins at `epoch = 1`; the
    other seven are refused with `live_owner`. This is what proves the flock
    boundary is genuinely cross-process, which is the guarantee it takes over
    from the `O_EXCL` primitive an owner lease cannot use (a claim is
    create-once; a lease must support read-modify-write takeover).

  - **G3d (the structural proof).** A REPO-WIDE AST sweep over every module in
    `scripts/`: every reference to `owner_lease_path_for(...)` appears either
    as an argument of `state_store._locked_json_transaction` or as the direct
    argument of a provably pure, fully spelled `os.path.exists` / `isfile` /
    `isdir` predicate whose result is consumed as a condition; the only durable
    write to an owner path outside that seam is
    `mark_owner_terminal_unlocked`'s single sidecar write, and its path
    argument is `owner_terminal_mark_path_for(...)`; and neither
    `owner_lease_path_for` nor the literal `"owner/lease.json"` reaches an
    `open(...)`, `write_json_atomic_durable(...)`, `os.replace(...)` or
    `shutil.*` call anywhere. This is what makes "at most one live lease per
    session, and `epoch` never repeats" a structural fact rather than a
    convention: `epoch` is derived only from a record no unlocked path can
    overwrite.

    That read-only predicate carve-out AMENDS the accepted plan's G3d
    STRUCTURAL TEST RECIPE (accepted plan line 2162), which was written
    reference-scoped. It does NOT amend the guarantee that recipe protects:
    the single-writer invariant (Rule W1, plan.md:573-588; section 10
    guarantee 1, plan.md:2747-2751) is WRITE-scoped and is left exactly as
    published. An existence predicate writes nothing, so admitting it takes
    nothing away from the invariant, while every write shape, every bare
    reference and every receiver the sweep cannot resolve stays refused.
    `LeaseWriterConfinementTests` states the carve-out's exact limits and
    pins each refusal with an adversarial control.

  - **G4a / G4c / G4d (the acquire gate and takeover).** One assertion per
    verdict that plain `acquire_owner_lease` REFUSES -- including
    `stale_dead_owner`, because a crashed owner is never implicitly reclaimed
    -- and acquires only for `unowned`; that a mismatched mode writes nothing;
    that `terminate_prior` never signals a pid whose start time differs from
    the lease's (the whole PID-reuse defence); and that
    `acquire_owner_lease.__doc__`'s verdict list is parsed out and asserted
    EQUAL to `OWNER_VERDICTS`, so the published contract cannot drift from the
    classifier.

  - **G6 / G14 (scope confinement).** The working tree changes exactly the
    three paths P1 is authorized to touch; every excluded production file is
    byte-identical to the accredited base; every pre-existing `cowork_state.py`
    symbol is AST- and docstring-identical to it; `save_role_session` keeps its
    exact five-parameter signature; and `scripts/test_cowork_state_m3.py` is
    byte-identical and unedited.

  - **G11a / G13f / N11 (the closed exception surface).** The declared
    hierarchy is exactly one base and five subclasses, all deriving from
    `Exception`; there is no bare `except:` and no `except Exception` anywhere
    in `cowork_owner.py`; and each of the three binding functions is TOTAL over
    its failure modes -- every `TimeoutError`, `OSError`, `CorruptRecordError`
    and `ValueError`, injected at the real reuse boundary (including a genuine
    cross-process lock timeout), comes back as `ProviderBindingUnavailable`
    with `__cause__` naming the real failure, and nothing raw escapes.

  - **The P1 halves of F2, F3, F5, F9, F10, F13 and G10.** Clean restart;
    a REALLY SIGKILLed owner (before the deadline: refuse; after an INJECTED
    -clock deadline: `stale_dead_owner`, still refused by plain acquire, and
    recoverable only by an explicit `proved_dead` takeover); pid reuse
    classified dead and never signalled; a corrupt lease refused, never
    silently acquired; a foreign-host lease `stale_unproven` with no probe and
    no signal and BOTH takeover modes refused; the SIGTERM-versus-takeover
    race leaving the successor's lease BYTE-IDENTICAL; and the
    compare-and-swap that makes a straggling heartbeat harmless.

Every fixture redirects `COWORK_SESSIONS_ROOT` into `tempfile.mkdtemp()` (so
nothing here can touch the real home dir, and nothing is written inside the
worktree), injects `now=` rather than sleeping toward a deadline, and spawns
no provider and no network client.

Run standalone:

    python3 -m unittest scripts.test_m55_owner_store -v
"""

import ast
import datetime
import hashlib
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_REPO_ROOT = os.path.dirname(_HERE)

import cowork_owner as owner  # noqa: E402
import cowork_state as state_store  # noqa: E402

# The accredited base this package is bound to.
BASE_SHA = "4aa89e78a509e28077ca67e6f6f148c892e089b4"

# P1's OWN signed candidate commit -- the other end of this package's interval.
# `ScopeConfinementTests` below measures the CLOSED interval
# BASE_SHA..CANDIDATE_SHA, never the live working tree, so what it asserts is a
# permanent fact about a finished piece of history that no later commit can
# enter or turn red.
CANDIDATE_SHA = "325d2cf5e6545c1d29e69dbb86c9fe7196868924"

# P1's write authority, exactly.
ALLOWED_CHANGED_PATHS = frozenset({
    "scripts/cowork_owner.py",
    "scripts/cowork_state.py",
    "scripts/test_m55_owner_store.py",
})

# Production paths P1 must leave byte-identical to the accredited base. Two
# are named by the plan for their own reasons: `cowork_eval.py`, whose
# `drain`'s "never raises" invariant no package may disturb, and
# `test_cowork_state_m3.py`, whose exact-signature characterization of
# `save_role_session` is what froze that function.
EXCLUDED_PATHS = (
    "scripts/cowork.py",
    "scripts/cowork_bridge.py",
    "scripts/cowork_dispatch.py",
    "scripts/cowork_eval.py",
    "scripts/cowork_report.py",
    "scripts/cowork_verification.py",
    "scripts/cowork_verification_evidence.py",
    "scripts/cowork_control_plane.py",
    "scripts/cowork_measure.py",
    "scripts/cowork_trace.py",
    "scripts/test_cowork_state_m3.py",
    "scripts/test_cowork.py",
)

# The five path helpers P1 may add to `cowork_state.py`, and nothing else.
ALLOWED_NEW_STATE_SYMBOLS = frozenset({
    "owner_dir_for",
    "owner_lease_path_for",
    "owner_terminal_mark_path_for",
    "owner_history_path_for",
    "provider_session_binding_path_for",
})

# The exception surface the plan declares: one base, five subclasses.
DECLARED_SUBCLASSES = frozenset({
    "OwnerLeaseConflict",
    "OwnerLeaseCorrupt",
    "OwnerLeaseLost",
    "ProviderSessionConflict",
    "ProviderBindingUnavailable",
})

# The translation set the three binding functions must be total over.
TRANSLATED_FAILURE_NAMES = ("TimeoutError", "OSError", "CorruptRecordError",
                            "ValueError")


# --------------------------------------------------------------------------- #
# Helpers.                                                                     #
# --------------------------------------------------------------------------- #


def _git_show_bytes(rev, rel_path):
    return subprocess.run(
        ["git", "show", "%s:%s" % (rev, rel_path)],
        cwd=_REPO_ROOT, capture_output=True, check=True).stdout


def _read_local_bytes(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), "rb") as fh:
        return fh.read()


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _scripts_modules():
    """Every Python module under `scripts/` -- the repo-wide sweep's domain."""
    out = []
    for name in sorted(os.listdir(_HERE)):
        if name.endswith(".py"):
            out.append(os.path.join(_HERE, name))
    return out


def _candidate_scripts_names():
    """Sorted basenames of the `.py` files under `scripts/` AT the candidate
    commit -- the frozen-interval counterpart of `_scripts_modules`.

    A SEPARATE reader rather than a change to `_scripts_modules`: that helper
    also feeds the three G3d repo-wide lease sweeps, which are claims about the
    production tree as it stands RIGHT NOW, and re-pointing it would silently
    convert three live behavioural gates into historical ones.

    `git ls-tree --name-only` emits paths carrying the `scripts/` prefix and
    includes non-`.py` entries, so both are handled here."""
    out = subprocess.run(
        ["git", "--no-optional-locks", "ls-tree", "--name-only",
         CANDIDATE_SHA, "scripts/"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True).stdout
    names = []
    for line in out.split():
        name = line[len("scripts/"):] if line.startswith("scripts/") else line
        if name.endswith(".py"):
            names.append(name)
    return sorted(names)


def _git_merge_base_is_ancestor(ancestor, descendant):
    """True when `ancestor` really is an ancestor of `descendant`.

    Guards the two frozen endpoint literals against a typo: a mistyped hash
    would otherwise point every claim in `ScopeConfinementTests` at an
    unrelated piece of history and go on reporting green. `merge-base
    --is-ancestor` only -- deliberately NOT the adjacency half of the
    precedent at test_m5_package_e_integration.py, since a package need not be
    exactly one commit above its base. `--no-optional-locks` so this cannot
    refresh `.git/index` while the suite runs as a live-candidate
    preflight."""
    return subprocess.run(
        ["git", "--no-optional-locks", "merge-base", "--is-ancestor",
         ancestor, descendant],
        cwd=_REPO_ROOT, capture_output=True).returncode == 0


def _module_tree(path):
    with open(path, "r") as fh:
        return ast.parse(fh.read(), filename=path)


def _called_name(node):
    """The bare callable name of a `Call`, whether written `f(...)` or
    `mod.f(...)`."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _lease_file(session_uuid):
    """The lease file, spelled WITHOUT `owner_lease_path_for` and without the
    literal `"owner/lease.json"`, on purpose.

    G3d's repo-wide sweep asserts that neither of those two spellings ever
    reaches an `open(...)` call anywhere in the repository -- including this
    file. A fixture that needs to fabricate a damaged or foreign lease
    therefore composes the path from `owner_dir_for`, which is a plain
    directory helper carrying none of the lease's single-writer contract."""
    return os.path.join(state_store.owner_dir_for(session_uuid), "lease.json")


def _write_raw_lease(session_uuid, payload):
    """Fabricate a durable lease record directly, bypassing the store, so a
    fixture can put the classifier in front of a state a healthy run could
    only reach by crashing (a foreign host, a dead pid, damaged bytes)."""
    os.makedirs(state_store.owner_dir_for(session_uuid), exist_ok=True)
    with open(_lease_file(session_uuid), "w") as fh:
        if isinstance(payload, (bytes, str)):
            fh.write(payload if isinstance(payload, str)
                     else payload.decode("utf-8"))
        else:
            json.dump(payload, fh)


def _read_raw_lease_bytes(session_uuid):
    with open(_lease_file(session_uuid), "rb") as fh:
        return fh.read()


def _child_env(root):
    env = dict(os.environ)
    env["COWORK_SESSIONS_ROOT"] = root
    env["PYTHONPATH"] = _HERE + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _dead_pid():
    """A pid that has genuinely exited -- a real crashed owner's pid, obtained
    by letting a real child run and reaping it, never a guessed number."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class OwnerStoreTestCase(unittest.TestCase):
    """Redirects `COWORK_SESSIONS_ROOT` into a fresh temp dir for every test,
    so no fixture can touch the real home dir or write inside the worktree."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cowork-owner-p1-")
        self.addCleanup(shutil.rmtree, self.root, True)
        patcher = mock.patch.dict(os.environ,
                                  {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session_uuid = str(uuid.uuid4())

    def claimant(self, session_uuid=None, entry_point="run_flow"):
        return owner.owner_identity(session_uuid or self.session_uuid,
                                    entry_point, self.root, None)

    def acquire(self, session_uuid=None, **kwargs):
        return owner.acquire_owner_lease(
            session_uuid or self.session_uuid,
            self.claimant(session_uuid), **kwargs)


# --------------------------------------------------------------------------- #
# G1c / F1 -- eight REAL processes race one first acquisition.                  #
# --------------------------------------------------------------------------- #

_RACER = r"""
import json, os, sys, time
sys.path.insert(0, os.environ["COWORK_SCRIPTS"])
import cowork_owner as owner
session_uuid = sys.argv[1]
gate = sys.argv[2]
deadline = time.time() + 30
while not os.path.exists(gate) and time.time() < deadline:
    time.sleep(0.005)
try:
    rec = owner.acquire_owner_lease(
        session_uuid,
        owner.owner_identity(session_uuid, "run_flow", os.getcwd(), None))
    print(json.dumps({"outcome": "acquired", "epoch": rec["epoch"],
                      "owner_id": rec["owner_id"]}))
except owner.OwnerLeaseConflict as exc:
    print(json.dumps({"outcome": "refused", "reason": exc.reason}))
except owner.OwnerLeaseCorrupt as exc:
    print(json.dumps({"outcome": "corrupt", "detail": str(exc)}))
"""


class ConcurrentAcquisitionTests(OwnerStoreTestCase):
    """G1c / F1."""

    def test_eight_real_processes_yield_exactly_one_epoch_one_winner(self):
        gate = os.path.join(self.root, "go")
        env = _child_env(self.root)
        env["COWORK_SCRIPTS"] = _HERE
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _RACER, self.session_uuid, gate],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=env)
            for _ in range(8)
        ]
        # Every racer is already spinning on the gate file, so the acquisition
        # attempts genuinely overlap rather than being serialized by process
        # startup cost.
        time.sleep(0.5)
        with open(gate, "w") as fh:
            fh.write("go")
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out.strip()))

        winners = [r for r in results if r["outcome"] == "acquired"]
        refused = [r for r in results if r["outcome"] == "refused"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(refused), 7, results)
        self.assertEqual(winners[0]["epoch"], 1)
        for entry in refused:
            self.assertEqual(entry["reason"], "live_owner", results)

        # The durable record names the single winner, at epoch 1.
        record = owner.read_owner_lease(self.session_uuid)
        self.assertEqual(record["owner_id"], winners[0]["owner_id"])
        self.assertEqual(record["epoch"], 1)
        self.assertEqual(record["state"], "live")


# --------------------------------------------------------------------------- #
# G3d -- the blocker's structural proof, swept repo-wide.                      #
# --------------------------------------------------------------------------- #


class LeaseWriterConfinementTests(unittest.TestCase):
    """G3d. Not scoped to `cowork_owner.py`: the guarantee published to M5.5
    is that NO code path in the repository WRITES `owner/lease.json` outside
    `_locked_json_transaction`, so the sweep's scope has to equal that
    wording.

    That guarantee is WRITE-scoped (Rule W1; section 10 guarantee 1). The G3d
    structural test recipe (accepted plan line 2162) over-approximated it by
    forbidding every *mention* of `owner_lease_path_for` outside the locked
    seam -- which reports two legitimate read-only existence probes in
    shipping code as violations. `cowork_state.py`'s session listing and
    `cowork.py`'s report path both have to ask "is there a lease at all?"
    WITHOUT taking the lock, because the locked read creates the session's
    `owner/` directory before it reads, and a listing or a report must not
    write into the tree it is describing. This class AMENDS THE RECIPE, not
    the invariant, with one fail-closed carve-out.

    A reference is admissible only if it is a direct argument of a
    `_locked_json_transaction` call (unchanged), or ALL FIVE of these hold:

      1. the predicate is one of exactly `exists`, `isfile`, `isdir` -- not
         `os.stat`, not `os.access`, not `os.path.getsize`, not `pathlib`;
      2. it is spelled as the full `os.path.<name>` attribute chain, so an
         arbitrary object's `.exists()` is never blessed -- basename matching,
         which is what the module-level `_called_name` does, would bless it,
         which is why that helper is deliberately not reused here;
      3. the module really binds the standard library `os`, by a module-scope
         `import os` / `import os.path` carrying no `as`, and never rebinds
         that name anywhere in the module;
      4. the `owner_lease_path_for(...)` call is a DIRECT argument of the
         predicate, so a path laundered through another call is refused; and
      5. the predicate's result is consumed as a condition -- an `if`, `while`,
         `assert`, `not`, boolean operand or `return` -- so a result that is
         stored, forwarded, or otherwise flows out of view is refused.

    Anything the sweep cannot decide structurally stays a violation. That is
    deliberate rather than lazy: deciding the remaining shapes needs dataflow
    analysis, and the same relaxation that would admit a read-only alias would
    also admit a genuine unlocked WRITE through an alias.

    `_lease_path_offenders` is the SINGLE recognizer: the repo-wide sweep and
    every control test in this class drive that same method, so the controls
    prove something about the gate that actually runs rather than about a
    lookalike. Every control fixture is an inert string constant handed to
    `ast.parse`; no real reference to the lease helper is introduced here.
    """

    # The exact predicate set the carve-out admits. Fail-closed: widening it
    # is one name here plus one control below, never an inference.
    _READ_ONLY_OS_PATH_PROBES = ("exists", "isfile", "isdir")

    # -- the recognizer, shared by the repo sweep and the controls ---------- #

    def _parent_map(self, tree):
        """One child -> parent map per tree, for the direct-argument and
        consumption-context tests. `ast` keeps no parent links itself."""
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        return parents

    def _module_binds_real_os(self, tree):
        """True only when `os` in this module IS the standard library module:
        bound at module scope by an unaliased `import os` / `import os.path`,
        and never rebound. Any doubt returns False, which keeps the site an
        offender."""
        bound_at_module_scope = False
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.name in ("os", "os.path") and alias.asname is None:
                        bound_at_module_scope = True
        if not bound_at_module_scope:
            return False
        for node in ast.walk(tree):
            # One check covers assignment, augmented assignment, walrus,
            # for-target, `with ... as`, comprehension target and `del`.
            if isinstance(node, ast.Name) and node.id == "os" and \
                    isinstance(node.ctx, (ast.Store, ast.Del)):
                return False
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.asname == "os":
                        return False
                    if isinstance(node, ast.ImportFrom) and \
                            alias.asname is None and alias.name == "os":
                        return False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and node.name == "os":
                return False
            if isinstance(node, ast.arg) and node.arg == "os":
                return False
            if isinstance(node, ast.ExceptHandler) and node.name == "os":
                return False
            if isinstance(node, (ast.Global, ast.Nonlocal)) and \
                    "os" in node.names:
                return False
        return True

    def _is_os_path_read_only_probe(self, func):
        """True only for the fully spelled `os.path.<exists|isfile|isdir>`
        chain. A from-imported `exists`, any `obj.exists`, `os.stat` and every
        `pathlib` spelling are all False."""
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr not in self._READ_ONLY_OS_PATH_PROBES:
            return False
        middle = func.value
        if not isinstance(middle, ast.Attribute) or middle.attr != "path":
            return False
        base = middle.value
        return isinstance(base, ast.Name) and base.id == "os"

    def _is_predicate_position(self, node, parents):
        """True when `node`'s value is consumed as a condition. Walks up
        through `not` and boolean operands only; an assignment target, a call
        argument, a subscript, an f-string, a comprehension, a missing parent
        and an exhausted budget are all False."""
        current = node
        for _ in range(8):
            parent = parents.get(id(current))
            if parent is None:
                return False
            if isinstance(parent, (ast.If, ast.IfExp, ast.While)) and \
                    parent.test is current:
                return True
            if isinstance(parent, ast.Assert) and parent.test is current:
                return True
            if isinstance(parent, ast.Return) and parent.value is current:
                return True
            if isinstance(parent, ast.UnaryOp) and \
                    isinstance(parent.op, ast.Not) and \
                    parent.operand is current:
                current = parent
                continue
            if isinstance(parent, ast.BoolOp) and \
                    any(value is current for value in parent.values):
                current = parent
                continue
            return False
        return False

    def _is_read_only_probe_site(self, call, parents, binds_real_os):
        """True only when the whole carve-out holds for this lease-path call:
        real `os`, fully spelled predicate, direct argument, predicate
        consumption."""
        if not binds_real_os:
            return False
        probe = parents.get(id(call))
        if not isinstance(probe, ast.Call):
            return False
        if not self._is_os_path_read_only_probe(probe.func):
            return False
        direct = any(arg is call for arg in probe.args) or \
            any(kw.value is call for kw in probe.keywords)
        if not direct:
            return False
        return self._is_predicate_position(probe, parents)

    def _lease_path_offenders(self, tree, label):
        """The one recognizer. Returns the `(label, name, reason)` tuples for
        every inadmissible mention of `owner_lease_path_for` in `tree`."""
        offenders = []
        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and \
                    _called_name(node) == "_locked_json_transaction":
                for arg in list(node.args) + [kw.value
                                              for kw in node.keywords]:
                    guarded.add(id(arg))
        parents = self._parent_map(tree)
        binds_real_os = self._module_binds_real_os(tree)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name != "owner_lease_path_for":
                continue
            # Admissible shapes, and only these two: the `func` of a Call that
            # is itself a direct argument of a `_locked_json_transaction`
            # call, or the direct argument of a pure `os.path` predicate.
            enclosing = [c for c in ast.walk(tree)
                         if isinstance(c, ast.Call) and c.func is node]
            if not enclosing:
                offenders.append((label, name, "bare ref"))
                continue
            for call in enclosing:
                if id(call) in guarded:
                    continue
                if self._is_read_only_probe_site(call, parents, binds_real_os):
                    continue
                offenders.append((label, name, "unguarded call"))
        return offenders

    def _offenders_for(self, source):
        """Drive the recognizer over an inert fixture. The fixture text is a
        string constant and reaches the recognizer only through `ast.parse`,
        so nothing here introduces a real reference to the lease helper."""
        return self._lease_path_offenders(ast.parse(source), "fixture.py")

    _UNGUARDED = ("fixture.py", "owner_lease_path_for", "unguarded call")
    _BARE_REF = ("fixture.py", "owner_lease_path_for", "bare ref")

    def test_owner_lease_path_referenced_only_inside_locked_transaction(self):
        offenders = []
        for path in _scripts_modules():
            offenders += self._lease_path_offenders(
                _module_tree(path), os.path.basename(path))
        self.assertEqual(offenders, [], offenders)

    def test_durable_owner_write_lives_only_in_the_terminal_mark_seam(self):
        offenders = []
        for path in _scripts_modules():
            tree = _module_tree(path)
            enclosing = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for child in ast.walk(node):
                        enclosing.setdefault(id(child), node.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _called_name(node) != "write_json_atomic_durable":
                    continue
                arg = node.args[0] if node.args else None
                arg_name = (_called_name(arg)
                            if isinstance(arg, ast.Call) else None)
                if arg_name not in ("owner_lease_path_for",
                                    "owner_terminal_mark_path_for",
                                    "owner_history_path_for"):
                    continue
                fn = enclosing.get(id(node))
                if (fn, arg_name) != ("mark_owner_terminal_unlocked",
                                      "owner_terminal_mark_path_for"):
                    offenders.append((os.path.basename(path), fn, arg_name))
        self.assertEqual(offenders, [], offenders)

        # And there is EXACTLY ONE such write in the whole seam -- a handler
        # that wrote twice would no longer be idempotent under a repeated
        # signal.
        source = inspect.getsource(owner.mark_owner_terminal_unlocked)
        writes = [n for n in ast.walk(ast.parse(textwrap.dedent(source)))
                  if isinstance(n, ast.Call)
                  and _called_name(n) == "write_json_atomic_durable"]
        self.assertEqual(len(writes), 1)

    def test_no_raw_file_operation_names_the_lease(self):
        """The textual half: neither spelling of the lease path may reach an
        `open`, `write_json_atomic_durable`, `os.replace` or `shutil.*`
        call -- anywhere, including this test module."""
        raw_ops = ("open", "replace", "write_json_atomic_durable",
                   "copy", "copy2", "copyfile", "move", "rmtree")
        offenders = []
        for path in _scripts_modules():
            tree = _module_tree(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _called_name(node) not in raw_ops:
                    continue
                for arg in ast.walk(node):
                    if isinstance(arg, ast.Constant) and \
                            arg.value == "owner/lease.json":
                        offenders.append((os.path.basename(path), "literal"))
                    named = None
                    if isinstance(arg, ast.Attribute):
                        named = arg.attr
                    elif isinstance(arg, ast.Name):
                        named = arg.id
                    if named == "owner_lease_path_for":
                        offenders.append((os.path.basename(path), "helper"))
        self.assertEqual(offenders, [], offenders)

    def test_signal_safe_mark_takes_no_lock_and_appends_nothing(self):
        """Rule W3, asserted structurally: the handler seam must not call any
        locking or appending primitive at all."""
        source = textwrap.dedent(inspect.getsource(
            owner.mark_owner_terminal_unlocked))
        called = {_called_name(n) for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.Call)}
        for forbidden in ("_locked_json_transaction", "_locked_jsonl_append",
                          "append_jsonl_atomic", "_flock_exclusive_with_timeout",
                          "flock", "_read_json_or_raise_if_corrupt",
                          "append_owner_history"):
            self.assertNotIn(forbidden, called)

    # ------------------------------------------------------------------ #
    # Controls for the carve-out. Each one asserts the EXACT offender     #
    # list the recognizer returns for its fixture, which is what makes    #
    # them adversarial rather than decorative.                            #
    # ------------------------------------------------------------------ #

    def test_control_admits_probe_with_bare_name_callee(self):
        """P-a -- the `cowork_state.py` session-listing shape."""
        source = (
            "import os\n"
            "def rows(sid):\n"
            "    if os.path.exists(owner_lease_path_for(sid)):\n"
            "        return 1\n"
            "    return 0\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_probe_with_attribute_callee(self):
        """P-b -- the `cowork.py` report-path shape."""
        source = (
            "import os\n"
            "def report(sid):\n"
            "    if os.path.exists(state_store.owner_lease_path_for(sid)):\n"
            "        view = 1\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_isfile_probe(self):
        """P-c."""
        source = (
            "import os\n"
            "if os.path.isfile(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_isdir_probe(self):
        """P-d."""
        source = (
            "import os\n"
            "if os.path.isdir(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_negated_probe_under_plain_os_path_import(self):
        """P-e -- `import os.path` binds the real `os` too, and `not` is a
        condition."""
        source = (
            "import os.path\n"
            "if not os.path.exists(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_probe_returned_as_a_predicate(self):
        """P-f."""
        source = (
            "import os\n"
            "def has_lease(sid):\n"
            "    return os.path.exists(owner_lease_path_for(sid))\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_probe_in_while_assert_and_boolean_operand(self):
        """P-g. The boolean-operand shape is folded in here rather than given
        its own method: it is the same consumption rule as `not`."""
        source = (
            "import os\n"
            "while os.path.exists(owner_lease_path_for(sid)):\n"
            "    pass\n"
            "assert os.path.exists(owner_lease_path_for(sid))\n"
            "if os.path.exists(owner_lease_path_for(sid)) and ready:\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_admits_the_unchanged_locked_transaction_shape(self):
        """P-h -- the regression guard on the admission path this package did
        NOT touch."""
        source = (
            "def read(sid):\n"
            "    return state_store._locked_json_transaction(\n"
            "        owner_lease_path_for(sid), fn)\n"
        )
        self.assertEqual(self._offenders_for(source), [])

    def test_control_refuses_probe_on_an_unknown_receiver(self):
        """N-a -- `os` is bound and real here, so the refusal isolates the
        callee-spelling rule: an arbitrary object's `.exists` is not blessed."""
        source = (
            "import os\n"
            "if p.exists(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_from_imported_exists(self):
        """N-b."""
        source = (
            "from os.path import exists\n"
            "if exists(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_probe_when_os_is_an_alias_for_something_else(self):
        """N-c -- `import fakeos as os` makes the chain a lookalike."""
        source = (
            "import fakeos as os\n"
            "if os.path.exists(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_probe_when_os_is_rebound(self):
        """N-d -- a real `import os` that is later shadowed by assignment."""
        source = (
            "import os\n"
            "os = shim\n"
            "if os.path.exists(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_bare_reference(self):
        """N-e -- no call at all, so nothing about the use site is decidable."""
        source = (
            "import os\n"
            "handler = owner_lease_path_for\n"
        )
        self.assertEqual(self._offenders_for(source), [self._BARE_REF])

    def test_control_refuses_a_path_stashed_in_a_variable(self):
        """N-f -- the shape that would launder a genuine unlocked write. This
        is exactly why the carve-out refuses what it cannot decide."""
        source = (
            "import os\n"
            "path = owner_lease_path_for(sid)\n"
            "with open(path, \"w\") as fh:\n"
            "    fh.write(payload)\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_probe_result_that_is_stored(self):
        """N-g -- a stored result flows out of the recognizer's view."""
        source = (
            "import os\n"
            "ok = os.path.exists(owner_lease_path_for(sid))\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_probe_result_forwarded_to_a_call(self):
        """N-h."""
        source = (
            "import os\n"
            "log(os.path.exists(owner_lease_path_for(sid)))\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_lease_path_nested_below_a_direct_argument(self):
        """N-i -- laundered through `os.path.dirname`, so the predicate is no
        longer being applied to the lease path itself."""
        source = (
            "import os\n"
            "if os.path.exists(os.path.dirname(owner_lease_path_for(sid))):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_predicate_outside_the_admitted_three(self):
        """N-j -- pins the fail-closed predicate set (AS-2): `os.stat` is not
        admitted even though it is read-only in fact."""
        source = (
            "import os\n"
            "if os.stat(owner_lease_path_for(sid)):\n"
            "    pass\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_write_through_open(self):
        """N-k."""
        source = (
            "import os\n"
            "fh = open(owner_lease_path_for(sid), \"w\")\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_durable_write(self):
        """N-l."""
        source = (
            "import os\n"
            "write_json_atomic_durable(owner_lease_path_for(sid), payload)\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_a_rename_onto_the_lease(self):
        """N-m -- `os.replace` is an `os` attribute call, which is precisely
        the shape a basename-only matcher would confuse with a probe."""
        source = (
            "import os\n"
            "os.replace(tmp, owner_lease_path_for(sid))\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])

    def test_control_refuses_every_shutil_move_of_the_lease(self):
        """N-n -- four call sites, four offenders."""
        source = (
            "import os\n"
            "import shutil\n"
            "shutil.copy(owner_lease_path_for(sid), dst)\n"
            "shutil.copy2(owner_lease_path_for(sid), dst)\n"
            "shutil.copyfile(owner_lease_path_for(sid), dst)\n"
            "shutil.move(owner_lease_path_for(sid), dst)\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED] * 4)

    def test_control_refuses_a_locked_transaction_lookalike(self):
        """N-o -- the callee is the real name, but the lease path is not a
        DIRECT argument of it, so `guarded` does not admit it (AS-7)."""
        source = (
            "import os\n"
            "_locked_json_transaction(wrap(owner_lease_path_for(sid)), fn)\n"
        )
        self.assertEqual(self._offenders_for(source), [self._UNGUARDED])


# --------------------------------------------------------------------------- #
# G4a -- the acquire gate, one assertion per verdict.                          #
# --------------------------------------------------------------------------- #


class AcquireGateTests(OwnerStoreTestCase):
    """G4a, plus the P1 halves of F2, F3, F9 and F10."""

    def _expire(self, record, seconds_past=1):
        deadline = datetime.datetime.fromisoformat(
            record["lease_deadline_at"].replace("Z", "+00:00"))
        return deadline + datetime.timedelta(seconds=seconds_past)

    def test_unowned_acquires_at_epoch_one(self):
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "unowned")
        record = self.acquire()
        self.assertEqual(record["epoch"], 1)
        self.assertEqual(record["state"], "live")
        self.assertEqual(record["record"], owner.OWNER_LEASE_RECORD)
        self.assertEqual(record["schema_version"],
                         owner.OWNER_LEASE_SCHEMA_VERSION)

    def test_live_owner_refuses(self):
        self.acquire()
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "live_owner")
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            self.acquire()
        self.assertEqual(caught.exception.reason, "live_owner")

    def test_clean_restart_acquires_epoch_two_without_takeover(self):
        """F2."""
        first = self.acquire()
        owner.release_owner_lease(self.session_uuid, first["owner_id"],
                                  first["epoch"], "normal_exit")
        released = owner.read_owner_lease(self.session_uuid)
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["terminal_reason"], "normal_exit")
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "unowned")
        second = self.acquire()
        self.assertEqual(second["epoch"], 2)
        self.assertIsNone(second["predecessor"])

    def test_stale_dead_owner_refuses_and_is_never_implicitly_reclaimed(self):
        """F3's P1 half: a really-dead owner is proved dead, and STILL
        refused -- reclamation is only ever explicit."""
        record = self.acquire()
        dead = _dead_pid()
        fabricated = dict(record)
        fabricated["pid"] = dead
        _write_raw_lease(self.session_uuid, fabricated)
        # Before the deadline the lease still stands, dead pid or not.
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "live_owner")
        after = self._expire(record)
        self.assertEqual(
            owner.classify_owner_lease(self.session_uuid, now=after),
            "stale_dead_owner")
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            owner.acquire_owner_lease(self.session_uuid, self.claimant(),
                                      now=after)
        self.assertEqual(caught.exception.reason, "stale_dead_owner")
        # Nothing was written by the refusal.
        self.assertEqual(owner.read_owner_lease(self.session_uuid)["owner_id"],
                         record["owner_id"])

    def test_pid_reuse_classifies_dead_never_live(self):
        """F5's P1 half: the pid is alive, but it is a DIFFERENT process."""
        record = self.acquire()
        fabricated = dict(record)
        fabricated["pid_start_at"] = "1999-01-01T00:00:00Z"
        _write_raw_lease(self.session_uuid, fabricated)
        after = self._expire(record)
        self.assertEqual(
            owner.classify_owner_lease(self.session_uuid, now=after),
            "stale_dead_owner")

    def test_stale_unproven_refuses_without_probing_or_signalling(self):
        """F10's P1 half: a foreign host is never pid-probed, never
        signalled, and refuses both takeover modes."""
        record = self.acquire()
        fabricated = dict(record)
        fabricated["host_id"] = "f" * 64
        _write_raw_lease(self.session_uuid, fabricated)
        after = self._expire(record)
        with mock.patch.object(owner, "_probe_pid_start") as probe, \
                mock.patch("os.kill") as killer:
            verdict = owner.classify_owner_lease(self.session_uuid, now=after)
            mode = owner.select_takeover_mode(self.session_uuid, now=after)
            probe.assert_not_called()
            killer.assert_not_called()
        self.assertEqual(verdict, "stale_unproven")
        self.assertIsNone(mode)
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            owner.acquire_owner_lease(self.session_uuid, self.claimant(),
                                      now=after)
        self.assertEqual(caught.exception.reason, "stale_unproven")

    def test_unavailable_pid_start_downgrades_to_unproven_never_dead(self):
        record = self.acquire()
        fabricated = dict(record)
        fabricated["pid"] = _dead_pid()
        fabricated["pid_start_source"] = "unavailable"
        fabricated["pid_start_at"] = None
        _write_raw_lease(self.session_uuid, fabricated)
        self.assertEqual(
            owner.classify_owner_lease(self.session_uuid,
                                       now=self._expire(record)),
            "stale_unproven")

    def test_probe_disabled_never_reports_death(self):
        record = self.acquire()
        fabricated = dict(record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        self.assertEqual(
            owner.classify_owner_lease(self.session_uuid,
                                       now=self._expire(record), probe=False),
            "stale_unproven")

    def test_corrupt_lease_refuses_and_never_silently_acquires(self):
        """F9."""
        self.acquire()
        _write_raw_lease(self.session_uuid, '{"schema_ver')
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "corrupt")
        with self.assertRaises(owner.OwnerLeaseCorrupt):
            self.acquire()
        with self.assertRaises(owner.OwnerLeaseCorrupt):
            owner.read_owner_lease(self.session_uuid)
        self.assertIsNone(owner.select_takeover_mode(self.session_uuid))

    def test_unknown_schema_version_is_corrupt_not_tolerable(self):
        record = self.acquire()
        fabricated = dict(record)
        fabricated["schema_version"] = 99
        _write_raw_lease(self.session_uuid, fabricated)
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "corrupt")
        with self.assertRaises(owner.OwnerLeaseCorrupt):
            self.acquire()

    def test_every_verdict_has_exactly_one_acquire_outcome(self):
        """G4a's completeness half: the five verdicts are exhausted, and only
        `unowned` acquires."""
        self.assertEqual(owner.OWNER_VERDICTS, frozenset({
            "unowned", "live_owner", "stale_dead_owner", "stale_unproven",
            "corrupt"}))


# --------------------------------------------------------------------------- #
# G4c / G4d -- takeover mode selection, verification, and contract agreement.  #
# --------------------------------------------------------------------------- #


class TakeoverTests(OwnerStoreTestCase):
    """G4c, G4d, and F3/F12's P1 halves."""

    def _expire(self, record, seconds_past=1):
        deadline = datetime.datetime.fromisoformat(
            record["lease_deadline_at"].replace("Z", "+00:00"))
        return deadline + datetime.timedelta(seconds=seconds_past)

    def test_select_mode_from_each_verdict(self):
        """G4d's first half."""
        self.assertIsNone(owner.select_takeover_mode(self.session_uuid))

        record = self.acquire()
        self.assertEqual(owner.select_takeover_mode(self.session_uuid),
                         "terminate_prior")

        fabricated = dict(record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        after = self._expire(record)
        self.assertEqual(
            owner.select_takeover_mode(self.session_uuid, now=after),
            "proved_dead")

        fabricated = dict(record)
        fabricated["host_id"] = "f" * 64
        _write_raw_lease(self.session_uuid, fabricated)
        self.assertIsNone(
            owner.select_takeover_mode(self.session_uuid, now=after))
        # A foreign-host LIVE owner also refuses -- it is not ours to signal.
        self.assertIsNone(owner.select_takeover_mode(self.session_uuid))

        _write_raw_lease(self.session_uuid, "{not json")
        self.assertIsNone(owner.select_takeover_mode(self.session_uuid))

    def test_acquire_docstring_verdicts_equal_the_closed_set(self):
        """G4d's second half: the published contract is generated from the
        classifier's own closed vocabulary, so §10 cannot drift from §3.4."""
        doc = owner.acquire_owner_lease.__doc__
        named = set()
        for line in doc.splitlines():
            match = re.match(r'^\s*"([a-z_]+)"\s*->', line)
            if match:
                named.add(match.group(1))
        self.assertEqual(named, set(owner.OWNER_VERDICTS))

    def test_proved_dead_requires_the_verdict_it_names(self):
        """G4a's takeover half: a mismatched mode writes nothing."""
        record = self.acquire()
        before = _read_raw_lease_bytes(self.session_uuid)
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            owner.take_over(self.session_uuid, self.claimant(), "proved_dead")
        self.assertEqual(caught.exception.reason, "live_owner")
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)
        self.assertEqual(owner.read_owner_lease(self.session_uuid)["epoch"],
                         record["epoch"])

    def test_terminate_prior_requires_a_live_owner(self):
        record = self.acquire()
        fabricated = dict(record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        before = _read_raw_lease_bytes(self.session_uuid)
        after = self._expire(record)
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            owner.take_over(self.session_uuid, self.claimant(),
                            "terminate_prior", now=after)
        self.assertEqual(caught.exception.reason, "stale_dead_owner")
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)

    def test_proved_dead_takeover_succeeds_and_bumps_the_epoch(self):
        """F3's recovery half."""
        record = self.acquire()
        fabricated = dict(record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        after = self._expire(record)
        successor = owner.take_over(self.session_uuid, self.claimant(),
                                    "proved_dead", now=after)
        self.assertEqual(successor["epoch"], record["epoch"] + 1)
        self.assertNotEqual(successor["owner_id"], record["owner_id"])
        self.assertEqual(successor["state"], "live")
        self.assertEqual(successor["predecessor"]["owner_id"],
                         record["owner_id"])
        self.assertEqual(successor["predecessor"]["evidence"], "pid_absent")

    def test_terminate_prior_never_signals_a_mismatched_pid_start(self):
        """G4c / F5: the PID-reuse defence. The lease's pid is alive (it is
        this very process), but its recorded start time does not match, so the
        takeover aborts having signalled NOTHING."""
        record = self.acquire()
        fabricated = dict(record)
        fabricated["pid_start_at"] = "1999-01-01T00:00:00Z"
        _write_raw_lease(self.session_uuid, fabricated)
        before = _read_raw_lease_bytes(self.session_uuid)
        with mock.patch("os.kill") as killer:
            with self.assertRaises(owner.OwnerLeaseConflict) as caught:
                owner.take_over(self.session_uuid, self.claimant(),
                                "terminate_prior")
            self.assertEqual(killer.call_args_list, [])
        self.assertEqual(caught.exception.reason, "live_owner")
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)

    def test_terminate_prior_aborts_and_leaves_the_lease_byte_identical(self):
        """F12: a prior owner that survives TERM and KILL keeps its lease."""
        record = self.acquire()
        before = _read_raw_lease_bytes(self.session_uuid)
        with mock.patch("os.kill") as killer, \
                mock.patch.object(owner, "TAKEOVER_TERM_GRACE_S", 0), \
                mock.patch.object(owner, "TAKEOVER_KILL_GRACE_S", 0):
            with self.assertRaises(owner.OwnerLeaseConflict):
                owner.take_over(self.session_uuid, self.claimant(),
                                "terminate_prior")
            # It did try, in the declared order, and then gave up.
            self.assertEqual([call.args[1] for call in killer.call_args_list],
                             [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)
        self.assertEqual(owner.read_owner_lease(self.session_uuid)["owner_id"],
                         record["owner_id"])

    def test_unknown_mode_is_refused_before_any_io(self):
        self.acquire()
        with self.assertRaises(ValueError):
            owner.take_over(self.session_uuid, self.claimant(), "just_take_it")


# --------------------------------------------------------------------------- #
# Rules W2/W3/W4/W5 -- the CAS, the sidecar, and the SIGTERM x takeover race.  #
# --------------------------------------------------------------------------- #


class CompareAndSwapTests(OwnerStoreTestCase):
    """W2, and G10's store half."""

    def test_renew_refreshes_the_deadline_for_the_true_owner(self):
        record = self.acquire()
        later = datetime.datetime.fromisoformat(
            record["heartbeat_at"].replace("Z", "+00:00")) \
            + datetime.timedelta(seconds=45)
        renewed = owner.renew_owner_lease(self.session_uuid,
                                          record["owner_id"],
                                          record["epoch"], now=later)
        self.assertIsNotNone(renewed)
        self.assertGreater(renewed["lease_deadline_at"],
                           record["lease_deadline_at"])
        self.assertEqual(renewed["epoch"], record["epoch"])
        self.assertEqual(renewed["owner_id"], record["owner_id"])

    def test_renew_writes_nothing_for_a_mismatched_holder(self):
        record = self.acquire()
        before = _read_raw_lease_bytes(self.session_uuid)
        self.assertIsNone(owner.renew_owner_lease(
            self.session_uuid, "someone-else", record["epoch"]))
        self.assertIsNone(owner.renew_owner_lease(
            self.session_uuid, record["owner_id"], record["epoch"] + 1))
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)

    def test_renew_never_resurrects_a_released_or_terminal_lease(self):
        """The property that makes a straggling heartbeat harmless."""
        record = self.acquire()
        owner.release_owner_lease(self.session_uuid, record["owner_id"],
                                  record["epoch"], "normal_exit")
        before = _read_raw_lease_bytes(self.session_uuid)
        self.assertIsNone(owner.renew_owner_lease(
            self.session_uuid, record["owner_id"], record["epoch"]))
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)
        self.assertEqual(owner.read_owner_lease(self.session_uuid)["state"],
                         "released")

    def test_renew_on_a_missing_lease_returns_none(self):
        self.assertIsNone(
            owner.renew_owner_lease(self.session_uuid, "nobody", 1))

    def test_release_is_idempotent_and_never_touches_a_successor(self):
        record = self.acquire()
        owner.release_owner_lease(self.session_uuid, record["owner_id"],
                                  record["epoch"], "normal_exit")
        successor = self.acquire()
        before = _read_raw_lease_bytes(self.session_uuid)
        # The predecessor releases a second time, after a successor exists.
        owner.release_owner_lease(self.session_uuid, record["owner_id"],
                                  record["epoch"], "crash")
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)
        self.assertEqual(owner.read_owner_lease(self.session_uuid)["owner_id"],
                         successor["owner_id"])

    def test_assert_owner_is_the_fence(self):
        record = self.acquire()
        self.assertIsNone(owner.assert_owner(self.session_uuid,
                                             record["owner_id"],
                                             record["epoch"]))
        with self.assertRaises(owner.OwnerLeaseLost):
            owner.assert_owner(self.session_uuid, "not-me", record["epoch"])
        with self.assertRaises(owner.OwnerLeaseLost):
            owner.assert_owner(self.session_uuid, record["owner_id"],
                               record["epoch"] + 1)
        owner.release_owner_lease(self.session_uuid, record["owner_id"],
                                  record["epoch"], "normal_exit")
        with self.assertRaises(owner.OwnerLeaseLost) as caught:
            owner.assert_owner(self.session_uuid, record["owner_id"],
                               record["epoch"])
        self.assertEqual(caught.exception.reason, "released")

    def test_assert_owner_on_a_corrupt_record_fails_closed(self):
        record = self.acquire()
        _write_raw_lease(self.session_uuid, "{broken")
        with self.assertRaises(owner.OwnerLeaseCorrupt):
            owner.assert_owner(self.session_uuid, record["owner_id"],
                               record["epoch"])


class TerminalMarkTests(OwnerStoreTestCase):
    """W3/W4/W5, and F4/F13's P1 halves."""

    def test_a_matching_mark_releases_the_lease(self):
        record = self.acquire()
        path = owner.mark_owner_terminal_unlocked(
            self.session_uuid, record["owner_id"], record["epoch"], "sigterm")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "unowned")
        mark = owner.read_terminal_mark(self.session_uuid, record["owner_id"])
        self.assertEqual(mark["record"], owner.OWNER_TERMINAL_MARK_RECORD)
        self.assertEqual(mark["terminal_reason"], "sigterm")
        self.assertTrue(mark["owner_matched"])

    def test_the_mark_never_touches_the_lease_record(self):
        record = self.acquire()
        before = _read_raw_lease_bytes(self.session_uuid)
        owner.mark_owner_terminal_unlocked(
            self.session_uuid, record["owner_id"], record["epoch"], "sigterm")
        self.assertEqual(_read_raw_lease_bytes(self.session_uuid), before)

    def test_the_mark_is_idempotent_under_a_repeated_signal(self):
        record = self.acquire()
        first = owner.mark_owner_terminal_unlocked(
            self.session_uuid, record["owner_id"], record["epoch"], "sigterm")
        with open(first, "rb") as fh:
            payload = fh.read()
        second = owner.mark_owner_terminal_unlocked(
            self.session_uuid, record["owner_id"], record["epoch"], "sigterm",
            now=datetime.datetime.fromisoformat(
                json.loads(payload)["marked_at"].replace("Z", "+00:00")))
        self.assertEqual(first, second)
        with open(second, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_a_predecessors_mark_after_a_takeover_is_ignored_entirely(self):
        """F13/SW64-B01's P1 half. Owner A's lease is taken over by B; A is
        THEN SIGTERMed and marks itself terminal. B's lease must be
        byte-identical, the verdict must stay `live_owner`, a third process
        must still be refused, and no epoch may repeat."""
        first = self.acquire()
        fabricated = dict(first)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        deadline = datetime.datetime.fromisoformat(
            first["lease_deadline_at"].replace("Z", "+00:00"))
        after = deadline + datetime.timedelta(seconds=1)
        second = owner.take_over(self.session_uuid, self.claimant(),
                                 "proved_dead", now=after)
        successor_bytes = _read_raw_lease_bytes(self.session_uuid)

        # A, long since superseded, now runs its own SIGTERM handler.
        owner.mark_owner_terminal_unlocked(
            self.session_uuid, first["owner_id"], first["epoch"], "sigterm",
            owner_matched=False)

        self.assertEqual(_read_raw_lease_bytes(self.session_uuid),
                         successor_bytes)
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "live_owner")
        with self.assertRaises(owner.OwnerLeaseConflict) as caught:
            self.acquire()
        self.assertEqual(caught.exception.reason, "live_owner")
        self.assertEqual(second["epoch"], first["epoch"] + 1)

    def test_a_corrupt_sidecar_is_ignored_so_the_lease_still_stands(self):
        record = self.acquire()
        path = state_store.owner_terminal_mark_path_for(self.session_uuid,
                                                        record["owner_id"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(owner.read_terminal_mark(self.session_uuid,
                                                   record["owner_id"]))
        self.assertEqual(owner.classify_owner_lease(self.session_uuid),
                         "live_owner")

    def test_acquisition_sweeps_every_non_matching_sidecar(self):
        """W5: growth is bounded, and the sweep is best-effort."""
        record = self.acquire()
        owner.mark_owner_terminal_unlocked(
            self.session_uuid, record["owner_id"], record["epoch"], "sigterm")
        stale = str(uuid.uuid4())
        owner.mark_owner_terminal_unlocked(self.session_uuid, stale, 1,
                                           "sigterm")
        successor = self.acquire()
        remaining = [n for n in os.listdir(
            state_store.owner_dir_for(self.session_uuid))
            if n.startswith("terminal.")]
        self.assertEqual(remaining, [])
        self.assertEqual(successor["epoch"], record["epoch"] + 1)

    def test_history_records_the_fold_and_the_acquisitions(self):
        record = self.acquire()
        owner.mark_owner_terminal_unlocked(
            self.session_uuid, record["owner_id"], record["epoch"], "sigterm")
        self.acquire()
        events = []
        with open(state_store.owner_history_path_for(self.session_uuid)) as fh:
            for line in fh:
                events.append(json.loads(line)["event"])
        self.assertEqual(events.count("acquired"), 2)
        self.assertIn("terminal_mark_folded", events)


# --------------------------------------------------------------------------- #
# Status projection.                                                           #
# --------------------------------------------------------------------------- #


class StatusViewTests(OwnerStoreTestCase):

    def test_view_reports_and_never_gates(self):
        self.assertEqual(
            owner.owner_status_view(self.session_uuid)["verdict"], "unowned")
        record = self.acquire()
        view = owner.owner_status_view(self.session_uuid)
        self.assertEqual(view["verdict"], "live_owner")
        self.assertEqual(view["takeover_mode"], "terminate_prior")
        self.assertTrue(view["host_matches"])
        self.assertFalse(view["expired"])
        self.assertIsNotNone(view["heartbeat_age_s"])
        self.assertEqual(view["lease"]["owner_id"], record["owner_id"])

    def test_view_reports_corruption_instead_of_raising(self):
        self.acquire()
        _write_raw_lease(self.session_uuid, "{broken")
        view = owner.owner_status_view(self.session_uuid)
        self.assertEqual(view["verdict"], "corrupt")
        self.assertIn("detail", view)


# --------------------------------------------------------------------------- #
# G11a -- the declared exception hierarchy.                                    #
# --------------------------------------------------------------------------- #


class ExceptionHierarchyTests(unittest.TestCase):
    """G11a's P1 half."""

    def test_declared_bases(self):
        self.assertEqual(owner.OwnerLeaseError.__bases__, (Exception,))
        for name in sorted(DECLARED_SUBCLASSES):
            klass = getattr(owner, name)
            self.assertIn(owner.OwnerLeaseError, klass.__bases__, name)

    def test_the_subclass_set_is_closed_at_exactly_five(self):
        defined = {
            name for name, value in vars(owner).items()
            if isinstance(value, type) and value is not owner.OwnerLeaseError
            and issubclass(value, owner.OwnerLeaseError)
        }
        self.assertEqual(defined, set(DECLARED_SUBCLASSES))

    def test_a_plain_except_exception_absorbs_them(self):
        """The consequence callers must design around, asserted rather than
        assumed: this is why the fenced seams must stay clear of anonymous
        handlers."""
        for exc in (owner.OwnerLeaseLost("s"),
                    owner.ProviderSessionConflict("claude", "sid", "other"),
                    owner.ProviderBindingUnavailable("claude", "sid")):
            try:
                raise exc
            except Exception as caught:  # noqa: BLE001 - that IS the claim
                self.assertIs(caught, exc)

    def test_typed_attributes_are_carried_for_reporting(self):
        conflict = owner.ProviderSessionConflict("claude", "sid", "other-uuid",
                                                 "builder")
        self.assertEqual(
            (conflict.controller, conflict.provider_session_id,
             conflict.owner_session_uuid, conflict.role),
            ("claude", "sid", "other-uuid", "builder"))
        unavailable = owner.ProviderBindingUnavailable("codex", "sid2",
                                                       "scout", "boom")
        self.assertEqual(
            (unavailable.controller, unavailable.provider_session_id,
             unavailable.role), ("codex", "sid2", "scout"))


# --------------------------------------------------------------------------- #
# G13f / N11 -- the binding surface is total and closed.                       #
# --------------------------------------------------------------------------- #

_LOCK_HOLDER = r"""
import fcntl, os, sys, time
path = sys.argv[1] + ".lock"
os.makedirs(os.path.dirname(path), exist_ok=True)
fh = open(path, "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
sys.stdout.write("held\n")
sys.stdout.flush()
time.sleep(float(sys.argv[2]))
"""


class BindingSurfaceStaticTests(unittest.TestCase):
    """G13f: the translation rule as a checked property of the module."""

    def setUp(self):
        with open(owner.__file__, "r") as fh:
            self.tree = ast.parse(fh.read(), filename=owner.__file__)

    def test_no_anonymous_handler_anywhere_in_the_module(self):
        offenders = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                offenders.append("bare except at line %d" % node.lineno)
                continue
            names = []
            targets = (node.type.elts if isinstance(node.type, ast.Tuple)
                       else [node.type])
            for target in targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
                elif isinstance(target, ast.Attribute):
                    names.append(target.attr)
            for banned in ("Exception", "BaseException"):
                if banned in names:
                    offenders.append("%s at line %d" % (banned, node.lineno))
        self.assertEqual(offenders, [], offenders)

    def _function(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError("no top-level function %r" % name)

    def test_each_binding_function_translates_the_whole_failure_set(self):
        for name in ("bind_provider_session", "read_provider_binding",
                     "release_provider_bindings"):
            node = self._function(name)
            translating = []
            for handler in ast.walk(node):
                if not isinstance(handler, ast.ExceptHandler):
                    continue
                targets = (handler.type.elts
                           if isinstance(handler.type, ast.Tuple)
                           else [handler.type])
                caught = []
                for target in targets:
                    if isinstance(target, ast.Name):
                        caught.append(target.id)
                    elif isinstance(target, ast.Attribute):
                        caught.append(target.attr)
                if tuple(caught) != TRANSLATED_FAILURE_NAMES:
                    continue
                raises = [
                    n for n in ast.walk(handler)
                    if isinstance(n, ast.Raise) and n.cause is not None
                    and isinstance(n.exc, ast.Call)
                    and _called_name(n.exc) == "ProviderBindingUnavailable"
                ]
                if raises:
                    translating.append(handler)
            self.assertTrue(translating,
                            "%s has no total translating handler" % name)

    def test_provider_session_conflict_has_exactly_one_producer(self):
        producers = set()
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Raise) and isinstance(child.exc,
                                                               ast.Call):
                    if _called_name(child.exc) == "ProviderSessionConflict":
                        producers.add(node.name)
        self.assertEqual(producers, {"bind_provider_session"})

    def test_the_module_never_imports_the_orchestrator(self):
        """G6's import-direction half: P1 must stay independently
        reviewable, so the dependency runs one way only."""
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertNotIn("cowork", imported)
        self.assertNotIn("cowork_control_plane", imported)


class BindingSurfaceBehaviourTests(OwnerStoreTestCase):
    """N11's P1 half, by injection at the real reuse boundary."""

    def setUp(self):
        super().setUp()
        self.record = self.acquire()
        self.owner_ref = {"session_uuid": self.session_uuid,
                          "owner_id": self.record["owner_id"]}

    def test_bind_read_and_release_round_trip(self):
        bound = owner.bind_provider_session("claude", "sid-1", self.owner_ref,
                                            "builder")
        self.assertEqual(bound["owner_session_uuid"], self.session_uuid)
        self.assertEqual(bound["bound_by_owner_id"], self.record["owner_id"])
        self.assertEqual(
            owner.read_provider_binding("claude", "sid-1")["role"], "builder")
        self.assertEqual(
            owner.release_provider_bindings(self.session_uuid,
                                            self.record["owner_id"]), 1)
        self.assertIsNone(owner.read_provider_binding("claude", "sid-1"))

    def test_absent_binding_reads_as_none(self):
        self.assertIsNone(owner.read_provider_binding("claude", "never-bound"))

    def test_conflict_only_against_a_live_foreign_session(self):
        owner.bind_provider_session("claude", "sid-2", self.owner_ref,
                                    "builder")
        other = str(uuid.uuid4())
        other_record = owner.acquire_owner_lease(
            other, owner.owner_identity(other, "run_flow", self.root, None))
        other_ref = {"session_uuid": other,
                     "owner_id": other_record["owner_id"]}
        with self.assertRaises(owner.ProviderSessionConflict) as caught:
            owner.bind_provider_session("claude", "sid-2", other_ref, "scout")
        self.assertEqual(caught.exception.owner_session_uuid,
                         self.session_uuid)
        self.assertEqual(caught.exception.role, "scout")
        # Nothing was overwritten by the refusal.
        self.assertEqual(
            owner.read_provider_binding("claude", "sid-2")["owner_session_uuid"],
            self.session_uuid)

    def test_the_same_session_rebinds_idempotently_across_a_takeover(self):
        """Exclusivity is per SESSION, not per owner: a successor keeps its
        predecessor's provider conversations."""
        owner.bind_provider_session("claude", "sid-3", self.owner_ref,
                                    "builder")
        fabricated = dict(self.record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        deadline = datetime.datetime.fromisoformat(
            self.record["lease_deadline_at"].replace("Z", "+00:00"))
        successor = owner.take_over(
            self.session_uuid, self.claimant(), "proved_dead",
            now=deadline + datetime.timedelta(seconds=1))
        rebound = owner.bind_provider_session(
            "claude", "sid-3",
            {"session_uuid": self.session_uuid,
             "owner_id": successor["owner_id"]}, "builder")
        self.assertEqual(rebound["bound_by_owner_id"], successor["owner_id"])

    def test_an_orphan_binding_self_heals_once_its_owner_is_not_live(self):
        owner.bind_provider_session("claude", "sid-4", self.owner_ref,
                                    "builder")
        owner.release_owner_lease(self.session_uuid, self.record["owner_id"],
                                  self.record["epoch"], "normal_exit")
        other = str(uuid.uuid4())
        other_record = owner.acquire_owner_lease(
            other, owner.owner_identity(other, "run_flow", self.root, None))
        adopted = owner.bind_provider_session(
            "claude", "sid-4",
            {"session_uuid": other, "owner_id": other_record["owner_id"]},
            "scout")
        self.assertEqual(adopted["owner_session_uuid"], other)

    def test_release_deletes_nothing_once_a_successor_holds_the_lease(self):
        """F8(d): a dying predecessor must not delete what a successor
        inherited but has not yet re-bound."""
        owner.bind_provider_session("claude", "sid-5", self.owner_ref,
                                    "builder")
        fabricated = dict(self.record)
        fabricated["pid"] = _dead_pid()
        _write_raw_lease(self.session_uuid, fabricated)
        deadline = datetime.datetime.fromisoformat(
            self.record["lease_deadline_at"].replace("Z", "+00:00"))
        owner.take_over(self.session_uuid, self.claimant(), "proved_dead",
                        now=deadline + datetime.timedelta(seconds=1))
        path = state_store.provider_session_binding_path_for("claude", "sid-5")
        with open(path, "rb") as fh:
            before = fh.read()
        self.assertEqual(
            owner.release_provider_bindings(self.session_uuid,
                                            self.record["owner_id"]), 0)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_release_is_idempotent(self):
        owner.bind_provider_session("claude", "sid-6", self.owner_ref,
                                    "builder")
        self.assertEqual(
            owner.release_provider_bindings(self.session_uuid,
                                            self.record["owner_id"]), 1)
        self.assertEqual(
            owner.release_provider_bindings(self.session_uuid,
                                            self.record["owner_id"]), 0)

    def test_unsafe_identifiers_translate_rather_than_leak_value_error(self):
        for controller, sid in (("claude", "bad/id"), ("../etc", "sid")):
            with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
                owner.bind_provider_session(controller, sid, self.owner_ref,
                                            "builder")
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
                owner.read_provider_binding(controller, sid)
            self.assertIsInstance(caught.exception.__cause__, ValueError)
        with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
            owner.release_provider_bindings("bad/uuid", "owner")
        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_a_corrupt_record_never_reads_as_absent(self):
        path = state_store.provider_session_binding_path_for("claude", "sid-7")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("[not an object]")
        with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
            owner.read_provider_binding("claude", "sid-7")
        self.assertIsInstance(caught.exception.__cause__,
                              state_store.CorruptRecordError)
        with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
            owner.bind_provider_session("claude", "sid-7", self.owner_ref,
                                        "builder")
        self.assertIsInstance(caught.exception.__cause__,
                              state_store.CorruptRecordError)

    def test_a_write_failure_translates_to_unavailable(self):
        with mock.patch.object(state_store, "write_json_atomic_durable",
                               return_value=False):
            with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
                owner.bind_provider_session("claude", "sid-8", self.owner_ref,
                                            "builder")
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_every_injected_failure_mode_translates_on_every_reader(self):
        injected = (TimeoutError("lock"), OSError("io"),
                    state_store.CorruptRecordError("damaged"),
                    ValueError("identifier"))
        for exc in injected:
            with mock.patch.object(state_store,
                                   "_read_json_or_raise_if_corrupt",
                                   side_effect=exc):
                with self.assertRaises(
                        owner.ProviderBindingUnavailable) as caught:
                    owner.read_provider_binding("claude", "sid-9")
                self.assertIs(caught.exception.__cause__, exc)
            with mock.patch.object(state_store, "_locked_json_transaction",
                                   side_effect=exc):
                with self.assertRaises(
                        owner.ProviderBindingUnavailable) as caught:
                    owner.bind_provider_session("claude", "sid-9",
                                                self.owner_ref, "builder")
                self.assertIs(caught.exception.__cause__, exc)
                with self.assertRaises(
                        owner.ProviderBindingUnavailable) as caught:
                    owner.release_provider_bindings(self.session_uuid,
                                                    self.record["owner_id"])
                self.assertIs(caught.exception.__cause__, exc)

    def test_a_real_cross_process_lock_timeout_translates(self):
        """The lock-timeout arm, injected at the real boundary: a SECOND OS
        process genuinely holds the binding's flock, so
        `_flock_exclusive_with_timeout` really does exhaust."""
        path = state_store.provider_session_binding_path_for("claude",
                                                             "sid-locked")
        holder = subprocess.Popen(
            [sys.executable, "-c", _LOCK_HOLDER, path, "20"],
            stdout=subprocess.PIPE, text=True, env=_child_env(self.root))
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with mock.patch.object(state_store, "_M3_LOCK_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(owner.ProviderBindingUnavailable) as caught:
                owner.bind_provider_session("claude", "sid-locked",
                                            self.owner_ref, "builder")
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)


# --------------------------------------------------------------------------- #
# G6 / G14 -- scope confinement.                                               #
# --------------------------------------------------------------------------- #


class ScopeConfinementTests(unittest.TestCase):
    """G6 and G14, both bound to the accredited base.

    Measures the CLOSED interval `BASE_SHA..CANDIDATE_SHA` -- P1's own finished
    history -- rather than the live working tree, so every claim below is a
    permanent fact about that interval that no later commit can enter. It needs
    a repository where BOTH endpoint objects exist, which is why the
    verification inventory measures it through a `candidate_read_only`
    preflight entry rather than inside the isolated snapshot.
    """

    def _changed_paths(self):
        """The change set of the CLOSED interval `BASE_SHA..CANDIDATE_SHA`.

        Two frozen endpoints, named explicitly, so this is neither the
        working-tree form (`git diff --name-only <BASE>`, which measured
        whatever happened to be on disk at run time) nor the `<BASE>..` form
        (which means `<BASE>..HEAD` and follows a moving ref).

        The `git ls-files --others --exclude-standard` branch that used to be
        unioned in here is GONE, and that is not a loosening: it existed solely
        to catch files P1 had added but not yet committed while P1 was the live
        candidate. `CANDIDATE_SHA` is P1's finished commit, so it already
        CONTAINS every file P1 added -- including this module -- and the
        commit-to-commit diff reports them anyway.
        """
        tracked = subprocess.run(
            ["git", "--no-optional-locks", "diff", "--name-only",
             BASE_SHA, CANDIDATE_SHA],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            check=True).stdout.split()
        return {p for p in tracked if "__pycache__" not in p}

    def test_changed_paths_are_exactly_the_three_allowed(self):
        self.assertEqual(self._changed_paths(), set(ALLOWED_CHANGED_PATHS))

    def test_the_base_is_an_ancestor_of_the_candidate(self):
        """Both endpoints are frozen literals, so a single mistyped character
        would silently point every claim in this class at an unrelated piece of
        history. This fails loudly instead."""
        self.assertTrue(
            _git_merge_base_is_ancestor(BASE_SHA, CANDIDATE_SHA),
            "%s is not an ancestor of %s"
            % (BASE_SHA[:12], CANDIDATE_SHA[:12]))

    def test_every_excluded_production_path_is_byte_identical(self):
        for rel in EXCLUDED_PATHS:
            self.assertEqual(_sha256(_git_show_bytes(CANDIDATE_SHA, rel)),
                             _sha256(_git_show_bytes(BASE_SHA, rel)), rel)

    def test_cowork_state_gains_exactly_the_five_path_helpers(self):
        """The excluded-symbol half, AST/semantic rather than a byte digest:
        every pre-existing symbol must be unchanged, and the only additions
        may be the five named helpers."""
        base_tree = ast.parse(_git_show_bytes(BASE_SHA,
                                              "scripts/cowork_state.py"))
        cand_tree = ast.parse(_git_show_bytes(CANDIDATE_SHA,
                                              "scripts/cowork_state.py"))

        def symbols(tree):
            out = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    out[node.name] = ast.dump(node, include_attributes=False)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            out[target.id] = ast.dump(
                                node, include_attributes=False)
            return out

        base, cand = symbols(base_tree), symbols(cand_tree)
        self.assertEqual(set(base) - set(cand), set())
        self.assertEqual(set(cand) - set(base), set(ALLOWED_NEW_STATE_SYMBOLS))
        for name, dumped in base.items():
            self.assertEqual(cand[name], dumped, name)

    def test_pre_existing_state_docstrings_are_unchanged(self):
        def docstrings(source):
            tree = ast.parse(source)
            return {node.name: ast.get_docstring(node) for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))}

        base = docstrings(_git_show_bytes(BASE_SHA, "scripts/cowork_state.py"))
        cand = docstrings(_git_show_bytes(CANDIDATE_SHA,
                                          "scripts/cowork_state.py"))
        for name, doc in base.items():
            self.assertEqual(cand[name], doc, name)

    def test_save_role_session_is_frozen(self):
        """G14: the seam the predecessor plan tried to widen stays exactly as
        the accredited base left it."""
        self.assertEqual(
            list(inspect.signature(
                state_store.save_role_session).parameters.keys()),
            ["path", "role", "controller", "session_id", "prior"])
        base_tree = ast.parse(_git_show_bytes(BASE_SHA,
                                              "scripts/cowork_state.py"))
        base_node = next(n for n in base_tree.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "save_role_session")
        live_node = ast.parse(textwrap.dedent(
            inspect.getsource(state_store.save_role_session))).body[0]
        self.assertEqual(ast.dump(live_node, include_attributes=False),
                         ast.dump(base_node, include_attributes=False))
        # `clean=False`: `__doc__` keeps its source indentation, and the
        # claim being pinned is the docstring's exact bytes, not a dedented
        # projection of them.
        self.assertEqual(state_store.save_role_session.__doc__,
                         ast.get_docstring(base_node, clean=False))

    def test_the_state_characterization_file_is_untouched(self):
        rel = "scripts/test_cowork_state_m3.py"
        self.assertEqual(_sha256(_git_show_bytes(CANDIDATE_SHA, rel)),
                         _sha256(_git_show_bytes(BASE_SHA, rel)))
        self.assertNotIn(rel, ALLOWED_CHANGED_PATHS)

    def test_the_new_module_is_only_reachable_from_this_package(self):
        """P1 is pure: nothing in the accredited production tree may import
        `cowork_owner` yet, so P2-P5's wiring stays reviewable as wiring.

        Swept over P1's OWN candidate tree, not the live one -- P2 onward wire
        `cowork_owner` in deliberately, so a live sweep turns this negative
        control red the moment the very wiring it was written to sequence
        lands. Re-pointed, never deleted: the claim is still that nothing
        imported the module DURING P1."""
        importers = []
        for name in _candidate_scripts_names():
            if name in ("cowork_owner.py", "test_m55_owner_store.py"):
                continue
            text = _git_show_bytes(
                CANDIDATE_SHA, "scripts/" + name).decode("utf-8", "replace")
            if "cowork_owner" in text:
                importers.append(name)
        self.assertEqual(importers, [])


if __name__ == "__main__":
    unittest.main()
