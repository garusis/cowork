#!/usr/bin/env python3
"""Worker-capture extraction seam and immutable worker capture (M5 Package A
+ Package B; garusis/cowork-internal#44).

Owns everything about SPAWNING the owned-verification worker subprocess and
CLASSIFYING its startup outcome (identity report, verified/unverified,
startup_failure) before `cowork_verification._run_owned_transaction` ever
sees a result. `spawn_worker` is the one frozen entry point the spine calls,
by its bare module-level name (re-exported into `cowork_verification`'s own
namespace at import time -- see that module's `from cowork_verification_worker
import ...` line): `_run_owned_transaction` never separately calls
`verify_worker_identity` or `_read_worker_identity` itself.

`spawn_worker` returns a `WorkerStartupResult`, a frozen named record
widening the base commit's three-item handle bundle (`proc`,
`liveness_write_fd`, `capture_thread`) by exactly one field, `classification`
-- `{"identity", "worker_verified", "startup_failure"}`, the same three
values `TransactionResult` already carries. No channel is lossy: every field
the spine populates `TransactionResult` with (`worker_identity`,
`worker_identity_verified`, `startup_failure`) is fully computed in here,
before `spawn_worker` returns.

WHY THIS MODULE ALSO OWNS `manifest_files`/`worker_rel_path` DERIVATION: the
base commit computed these once in the spine, from the ALREADY-BUILT
target-repo snapshot manifest, then reused them for the one identity check
that followed. Relocating this derivation here (recomputed from
`session_uuid`/`transaction_id` alone) is what lets Package B (#44) change
WHICH manifest is captured and consulted -- entirely inside this file,
without any spine edit.

PACKAGE B (#44): `resolve_worker_source` captures a dedicated,
content-addressed snapshot of the RUNNING Cowork installation's own
`scripts/` tree (never the target-repo snapshot `checkout_root` points at),
using Package A's reserved `cowork_state.verification_tool_snapshot_*`
helpers, and materializes it into a fresh checkout -- BEFORE `spawn_worker`
ever calls `Popen`, so the expected hash is recorded before the worker
process is launched. `spawn_worker` execs the worker ONLY from that
captured checkout; `_classify_worker_startup` verifies the worker's
self-reported identity against that SAME captured manifest (read back from
`verification_tool_snapshot_manifest_path_for`), not the target-repo one.
Two new, distinct `startup_failure` reasons result:
`worker_source_missing` (the installation's own `cowork_verification.py`
could not be found/read -- detected before any process is spawned) and
`worker_identity_mismatch` (a real identity WAS reported, but its hash or
protocol_version disagrees with the captured manifest). Neither reason
changes the meaning of `request_rejected` or
`worker_exited_before_identity_report`, both still produced exactly as
before, only now checked against the installation manifest rather than the
target-repo one.

TIMEOUT SOURCING (M5R3-m1). The base commit read `_read_worker_identity`'s
`timeout_s` from `timeout_policy.get("startup_allowance_s") or
DEFAULT_STARTUP_ALLOWANCE_S`, where `timeout_policy = request.get(
"timeout_policy") or {}` was computed from the spine's OWN in-memory
`request` dict, AFTER `spawn_worker`'s call site (cowork_verification.py
:2473-2477/:2510-2514 at base). The CANDIDATE spine now computes
`timeout_policy` (for its own, separate `overall_deadline` use) BEFORE
calling `spawn_worker`, restoring the base's own deadline-timing semantics
(M5A-R-M1) -- but `spawn_worker`'s frozen signature still carries no
`request`/`timeout_policy` parameter either way, in base or candidate
ordering, so this module has never been able to receive that in-memory
value directly and always reads the identical value back off disk instead:
`request_path` is already a frozen `spawn_worker` parameter, and the full
request document (including `timeout_policy`) is already durably written
there by `_run_transaction_body`, strictly BEFORE `_run_owned_transaction`
-- and therefore `spawn_worker` -- is ever called (cowork_verification.py
:2230-2232 at base). `_startup_allowance_s` below reads it back with the
exact same fallback, bound-for-bound identical to the base, without
widening `spawn_worker`'s parameter list.

CANCELLATION ORDERING (M5R3-m2). At the base, the identity read (:2510) ran
AFTER the parent's `_cancel_watcher` thread was already started (:2504-2506),
so a `cancel_event` firing during the startup-allowance window was acted on
immediately. Relocating the identity read inside `spawn_worker` moves it
BEFORE the spine can start that watcher (the watcher needs `proc`/
`liveness_write_fd`, only available once `spawn_worker` returns) -- so a
cancel that fires DURING this module's own bounded identity-read window is
now only observed once `spawn_worker` returns and the spine's entry loop
reaches its own `cancel_event.is_set()` check, not mid-read. This is
disposed of here explicitly, not left implicit: the delay is BOUNDED by the
same `startup_allowance_s`/`DEFAULT_STARTUP_ALLOWANCE_S` this module already
honors (never unbounded), teardown afterward still runs through the same
idempotent `terminate_worker`/`cleanup_active_command_group` path regardless
of which branch (immediate watcher vs. entry-loop check) ultimately fires
it, and `scripts/test_m5_package_a_contracts.py` proves both the bound and
that no pre-existing M1-M4 test module references `cancel_event` (so this
ordering change is invisible to, and does not regress, any of them). Package
A's frozen `spawn_worker` signature is not widened with a `cancel_event`
parameter to close this gap -- doing so would be a second, independent
production behavior change (mid-read cancellation) this package does not
own; recording the bound is the disposition this candidate makes.

CROSS-MODULE CONSTANTS. This module needs a handful of `cowork_verification`
module-level constants (`PROTOCOL_VERSION`, `WORKER_EXIT_REQUEST_REJECTED`,
`DEFAULT_STARTUP_ALLOWANCE_S`). `cowork_verification` itself imports THIS
module's symbols at its own top level (the re-export/indirection contract),
so a plain top-level `import cowork_verification` here would be a genuine
circular import that only resolves in ONE of the two possible load orders
(it breaks the moment anything imports this module before
`cowork_verification`, which `scripts/test_m5_package_a_contracts.py`'s own
structural tests deliberately do). Every reference to those constants is
therefore inside a function BODY, `import cowork_verification` done lazily
at call time via `_spine()` -- by the time any function here is actually
CALLED, both modules have always finished loading, in either order, so the
lookup is trivially safe. `self_source_hash` is NOT among these constants:
it stays defined AND called directly in `cowork_verification.py` (see that
module's own docstring), never touched by this module at all.

Python 3.9+, stdlib only -- matches `cowork_verification.py`.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_state as state_store  # noqa: E402


def _spine():
    """Lazy back-reference to `cowork_verification` for its shared
    protocol-level constants -- see the module docstring's CROSS-MODULE
    CONSTANTS note for why this is a function-body import, never a
    module-level one."""
    import cowork_verification
    return cowork_verification


# Hard cap on the worker's captured startup stdout+stderr -- BOTH what gets
# written to disk (the WRITE side, `_capture_startup_log`) and what a reader
# will ever pull into memory afterward (the READ side,
# `_read_worker_startup_log`). Relocated verbatim from the base commit
# alongside `spawn_worker`, its sole owner; re-exported by
# `cowork_verification` (beyond the extraction seam's own minimum list)
# because `scripts/test_cowork.py` reads `verification.MAX_STARTUP_LOG_BYTES`
# directly.
MAX_STARTUP_LOG_BYTES = 64 * 1024  # 64 KiB


class WorkerStartupResult(object):
    """Frozen, widened return of `spawn_worker` (worker_capture_seam): the
    base commit's three-item handle bundle `(proc, liveness_write_fd,
    capture_thread)` plus exactly one more field, `classification` -- a dict
    `{"identity", "worker_verified", "startup_failure"}` matching
    `TransactionResult`'s own field names one-for-one, computed entirely
    inside this module before `spawn_worker` returns.

    ITERATES AS THE ORIGINAL THREE-ITEM TUPLE ON PURPOSE: `a, b, c =
    spawn_worker(...)` -- the base commit's calling convention, still used
    by a wrapper in `scripts/test_cowork.py` this candidate may not edit --
    keeps working unchanged. `classification` is reachable via
    `.classification` attribute access (what the spine's own updated call
    site uses) or via the `proc._cowork_startup_classification` fallback
    `spawn_worker` also stashes on the returned `proc` -- see
    `spawn_worker`'s own docstring for why: a caller that destructures this
    object into a bare 3-tuple and hands that plain tuple onward (exactly
    what `scripts/test_cowork.py`'s `spy_spawn_worker` wrapper at
    :25121-25128 does when wrapping the real `spawn_worker` under
    `mock.patch.object`) would otherwise silently lose `classification` even
    though the real `proc` object it forwards is the exact same one.
    """

    __slots__ = ("proc", "liveness_write_fd", "capture_thread",
                "classification")

    def __init__(self, proc, liveness_write_fd, capture_thread,
                classification):
        self.proc = proc
        self.liveness_write_fd = liveness_write_fd
        self.capture_thread = capture_thread
        self.classification = classification

    def __iter__(self):
        return iter((self.proc, self.liveness_write_fd, self.capture_thread))

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return (self.proc, self.liveness_write_fd, self.capture_thread)[index]

    def __repr__(self):
        return ("WorkerStartupResult(proc=%r, liveness_write_fd=%r, "
                "capture_thread=%r, classification=%r)"
                % (self.proc, self.liveness_write_fd, self.capture_thread,
                   self.classification))


def _capture_startup_log(pipe_fh, log_path, max_bytes):
    """Read `pipe_fh` (the worker's merged stdout+stderr) to completion,
    retaining a bounded TAIL/RING of AT MOST `max_bytes` -- the LAST bytes
    the child wrote, not the first. Relocated verbatim from the base commit;
    see `cowork_verification.spawn_worker`'s (now this module's) own
    docstring for the full rationale. Runs in a daemon thread; the pipe is
    closed when the child exits and this function returns."""
    buf = bytearray()
    try:
        while True:
            chunk = pipe_fh.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                del buf[:len(buf) - max_bytes]
        with open(log_path, "wb") as out:
            out.write(bytes(buf))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe_fh.close()
        except OSError:
            pass


def _read_worker_identity(session_uuid, transaction_id, timeout_s=10,
                          poll_delay_s=0.2, sleep=time.sleep, now=time.time,
                          proc=None):
    """Poll for the worker's self-reported identity, up to `timeout_s`.
    Relocated verbatim from the base commit (ORCH-030). Not re-exported by
    `cowork_verification` (M5R2-m2): no spine call site or test references
    it directly -- only this module's own `spawn_worker` calls it, to build
    `classification` before returning.

    Returns the identity dict, or `None` on timeout/no-report (the caller
    distinguishes "still running, gave up" from "already exited" via
    `proc.poll()` itself after this returns)."""
    identity_path = state_store.verification_worker_identity_path_for(
        session_uuid, transaction_id)
    deadline = now() + timeout_s
    while now() < deadline:
        identity = state_store.read_json_tolerant(identity_path)
        if identity:
            return identity
        if proc is not None and proc.poll() is not None:
            sleep(min(poll_delay_s, 0.05))
            return state_store.read_json_tolerant(identity_path)
        sleep(poll_delay_s)
    return None


def verify_worker_identity(identity, snapshot_manifest, worker_file_rel):
    """True only when the worker's self-reported source hash matches the
    snapshot manifest's entry for its own file AND its protocol version
    matches ours. Any mismatch (including a missing report) means the
    transaction is UNVERIFIED -- never accepted on faith. Relocated
    verbatim from the base commit; re-exported by `cowork_verification` for
    direct testability from Package B's own test module, even though the
    spine's call graph no longer references it separately (it is now only
    called internally, by this module's own `spawn_worker`)."""
    if not isinstance(identity, dict):
        return False
    if identity.get("protocol_version") != _spine().PROTOCOL_VERSION:
        return False
    entry = (snapshot_manifest or {}).get(worker_file_rel)
    if not entry or entry.get("type") != "file":
        return False
    return identity.get("source_hash") == entry.get("sha256")


def _read_worker_startup_log(session_uuid, transaction_id, max_bytes=4096):
    """Bounded tail of the worker's captured startup stdout/stderr, for a
    structured `startup_failure` reason. Relocated verbatim from the base
    commit. Never raises -- a log that cannot be read yields an explicit
    note rather than blocking failure reporting."""
    log_path = state_store.verification_worker_startup_log_path_for(
        session_uuid, transaction_id)
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                data = b"...(truncated)...\n" + fh.read(max_bytes)
            else:
                data = fh.read(max_bytes)
    except OSError:
        return "(startup log unavailable)"
    return data.decode("utf-8", "replace")


def _startup_allowance_s(request_path):
    """M5R3-m1: obtain `_read_worker_identity`'s `timeout_s` the same way
    the base spine did -- `timeout_policy.startup_allowance_s`, falling back
    to `DEFAULT_STARTUP_ALLOWANCE_S` -- by reading it from the request
    already durably written at `request_path` before `spawn_worker` was
    ever called. See the module docstring's TIMEOUT SOURCING note."""
    request = state_store.read_json_tolerant(request_path)
    timeout_policy = (request or {}).get("timeout_policy") or {}
    return (timeout_policy.get("startup_allowance_s")
           or _spine().DEFAULT_STARTUP_ALLOWANCE_S)


def _classify_worker_startup(proc, capture_thread, request_path,
                             session_uuid, transaction_id):
    """Build the `classification` dict `spawn_worker` returns:
    `{"identity", "worker_verified", "startup_failure"}`. Relocated
    verbatim from the base spine's own inline logic
    (cowork_verification.py:2467-2468, :2510-2541) -- the identity read and
    the request_rejected/worker_exited_before_identity_report distinction
    are bound-for-bound identical to the base, just moved here and computed
    before `spawn_worker` returns instead of after.

    PACKAGE B (#44): `manifest_files`/`worker_rel_path` are now read from
    the captured Cowork-INSTALLATION tool-snapshot manifest
    (`verification_tool_snapshot_manifest_path_for`, written by
    `resolve_worker_source` before `spawn_worker` ever calls `Popen` -- see
    that function's own docstring) instead of the base's target-repo
    snapshot manifest. A worker that DID report an identity, but whose
    reported hash/protocol does not match this manifest, now yields a new,
    distinct `worker_identity_mismatch` startup_failure -- the base only
    ever produced a startup_failure when identity was `None` entirely; this
    is the one behavioral widening this package adds, not a change to
    either pre-existing reason's own meaning.

    `worker_spawn_failed` is explicitly NOT one of the reasons THIS
    function can produce -- it is minted only by `run_transaction`'s own
    `except OSError` handler in the spine, for any `OSError` that reaches
    it having propagated out of `spawn_worker` before this function's own
    return value was ever available to classify. M5B-R-m2 (documentation):
    that includes not just `Popen` raising, but also an `OSError` from
    `resolve_worker_source`/`_materialize_tool_snapshot` -- reading,
    hashing, or copying the installation's own `scripts/*.py` -- since
    `spawn_worker` calls `resolve_worker_source` before ever reaching this
    function; either origin surfaces identically as `worker_spawn_failed`,
    by the same spine handler, for the same reason: no worker process
    could be confirmed to exist for this transaction at all."""
    if not session_uuid or not transaction_id:
        return {"identity": None, "worker_verified": False,
               "startup_failure": None}
    verification = _spine()
    timeout_s = _startup_allowance_s(request_path)
    identity = _read_worker_identity(
        session_uuid, transaction_id, timeout_s=timeout_s, proc=proc)
    manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
        session_uuid, transaction_id)
    manifest_doc = state_store.read_json_tolerant(manifest_path)
    manifest_files = (manifest_doc or {}).get("files", {})
    worker_rel_path = os.path.join("scripts", "cowork_verification.py")
    worker_verified = verify_worker_identity(
        identity, manifest_files, worker_rel_path)
    startup_failure = None
    if identity is None:
        if not worker_verified:
            # ORCH-030: distinguish "the worker process already exited
            # without ever reporting identity" (a real startup failure,
            # with a captured reason) from "still running, gave up
            # waiting".
            exit_code = proc.poll()
            if exit_code is not None:
                # The process has exited, so its stdout pipe has already
                # delivered EOF to `_capture_startup_log` -- join it
                # (bounded) BEFORE reading, so `log_tail` is never read
                # from a partially-written file.
                if capture_thread is not None:
                    capture_thread.join(timeout=5)
                startup_failure = {
                    "reason": ("request_rejected"
                              if exit_code == verification.WORKER_EXIT_REQUEST_REJECTED
                              else "worker_exited_before_identity_report"),
                    "exit_code": exit_code,
                    "log_tail": _read_worker_startup_log(
                        session_uuid, transaction_id),
                }
    elif not worker_verified:
        # PACKAGE B (#44): the worker DID report an identity -- it is not
        # silently missing -- but that report does not match the captured
        # Cowork-installation manifest (a different source hash, or a
        # different protocol_version). Distinct from both reasons above:
        # this is neither "rejected before publishing identity" nor "exited
        # without ever publishing identity" -- a report WAS published, and
        # it disagrees with what this transaction captured and expected.
        reported = identity if isinstance(identity, dict) else {}
        expected_entry = manifest_files.get(worker_rel_path) or {}
        startup_failure = {
            "reason": "worker_identity_mismatch",
            "reported_source_hash": reported.get("source_hash"),
            "expected_source_hash": expected_entry.get("sha256"),
            "reported_protocol_version": reported.get("protocol_version"),
            "expected_protocol_version": verification.PROTOCOL_VERSION,
            # M5B-R-m1: every other reason in this taxonomy carries
            # `exit_code`/`log_tail`; a mismatched-but-published identity
            # means the worker is typically still running (never blocked on
            # here -- `proc.poll()` is non-blocking, `None` while alive,
            # exactly like a genuinely still-running worker would report),
            # so the diagnostic payload stays shaped the same across all
            # four reasons instead of silently varying by reason.
            "exit_code": proc.poll(),
            "log_tail": _read_worker_startup_log(
                session_uuid, transaction_id),
        }
    return {"identity": identity, "worker_verified": worker_verified,
           "startup_failure": startup_failure}


def _installation_scripts_dir():
    """The RUNNING Cowork installation's own `scripts/` directory -- this
    module's own directory, at call time (never cached at import time,
    though in practice a single process only ever has one). This is the
    ONLY source `resolve_worker_source` ever reads from -- never
    `checkout_root` (the TARGET repo's own materialized snapshot
    `spawn_worker` received as a parameter): a target repo may not track
    Cowork's tool source at all, or may track a stale/divergent copy, and
    #44 requires the worker to run the orchestrator's OWN code either way."""
    return os.path.dirname(os.path.abspath(__file__))


def _iter_installation_source_files(scripts_dir):
    """Every top-level `*.py` FILE (never a symlink) directly inside
    `scripts_dir`, sorted for determinism. Deliberately NOT recursive:
    `data/`/`fixtures/` are non-code test assets, and `__pycache__` is a
    build artifact -- neither is part of the installation's own source
    identity a worker self-reports against.

    M5B-R-m4: `os.path.isfile` FOLLOWS symlinks, so a bare `isfile` check
    would silently capture a symlink's TARGET content under the symlink's
    own name, misrepresenting it as a real, standalone file -- the
    manifest has no `symlink_target` field to record what actually
    happened instead (unlike `cowork_state`'s own generic snapshot-manifest
    shape). Rather than mis-capture, a symlink is excluded outright: if it
    happens to be `cowork_verification.py` itself, `_build_installation_
    manifest`'s own entry-point check then correctly reports
    `worker_source_missing` -- fail closed, never fail silently-wrong."""
    try:
        names = os.listdir(scripts_dir)
    except OSError:
        return []
    return sorted(
        name for name in names
        if name.endswith(".py")
        and not os.path.islink(os.path.join(scripts_dir, name))
        and os.path.isfile(os.path.join(scripts_dir, name)))


def _build_installation_manifest(scripts_dir):
    """Content-addressed manifest of the running installation's own
    `scripts/*.py`: `{"scripts/<name>": {"type": "file", "sha256": ...,
    "mode": ...}}`, keyed and shaped exactly as `verify_worker_identity`
    expects. Returns `(manifest_files, raw_bytes_by_rel)` on success.

    Returns `(None, None)` -- the `worker_source_missing` condition
    `resolve_worker_source` checks for -- when the installation's own
    worker entry point (`cowork_verification.py`) cannot even be listed or
    read at `scripts_dir`: with no readable copy of the file the worker
    itself execs, there is nothing to capture, launch, or verify against,
    so this is detected and reported BEFORE any process is ever spawned."""
    entry_name = "cowork_verification.py"
    names = _iter_installation_source_files(scripts_dir)
    if entry_name not in names:
        return None, None
    manifest = {}
    raw_by_rel = {}
    for name in names:
        full = os.path.join(scripts_dir, name)
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            if name == entry_name:
                return None, None
            continue
        rel = os.path.join("scripts", name)
        manifest[rel] = {
            "type": "file",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mode": "755" if os.access(full, os.X_OK) else "644",
        }
        raw_by_rel[rel] = raw
    if os.path.join("scripts", entry_name) not in manifest:
        return None, None
    return manifest, raw_by_rel


def _write_tool_snapshot_object(session_uuid, sha256, raw_bytes):
    """Copy one file's bytes into the content-addressed tool-snapshot
    object store (`cowork_state.verification_tool_snapshot_object_path`),
    atomically and idempotently -- a no-op if the object is already
    present (two files with identical content, or two transactions in the
    same session capturing an unchanged installation, share one blob)."""
    obj_path = state_store.verification_tool_snapshot_object_path(
        session_uuid, sha256)
    if os.path.exists(obj_path):
        return
    dirname = os.path.dirname(obj_path)
    os.makedirs(dirname, exist_ok=True)
    tmp = obj_path + ".tmp.%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(raw_bytes)
    os.replace(tmp, obj_path)


def tool_snapshot_checkout_dir(session_uuid, transaction_id):
    """The deterministic, explicitly NAMEABLE root of one transaction's
    materialized tool-snapshot checkout (M5B-R-M2) -- the directory
    `spawn_worker` launches the worker process from, once `resolve_worker_
    source` succeeds. Derived from exactly the two identities that already
    scope every other per-transaction tool-snapshot artifact --
    `session_uuid` (via `cowork_state.verification_tool_snapshot_root_for`,
    Package A's own reserved root: which store) and `transaction_id`
    (which transaction within it) -- colocated with (never overlapping)
    the `objects/` and `manifests/` subdirectories that root already owns.
    Two different transactions, even across two different sessions, can
    never collide on this path.

    A public, importable path helper (not a leading-underscore module
    private) is the fix M5B-R-M2 makes to the predecessor's own structural
    complaint: no cleanup owner outside this module could previously even
    NAME this directory in order to remove it, since it was computed only
    as a private join inside `_materialize_tool_snapshot`. Not a new
    `cowork_state.py` path helper -- this module's writable scope excludes
    that file -- just a deterministic join composed here, exactly like
    `worker_rel_path` is derived elsewhere in this module rather than
    stored as its own state-store constant."""
    return os.path.join(
        state_store.verification_tool_snapshot_root_for(session_uuid),
        "checkout", transaction_id)


def reclaim_tool_snapshot_checkout(session_uuid, transaction_id):
    """Remove exactly ONE transaction's materialized tool-snapshot checkout
    (M5B-R-M2) -- `tool_snapshot_checkout_dir(session_uuid, transaction_id)`
    -- and nothing else: never the shared, content-addressed `objects/`
    store (checkouts are built by COPYING out of it, never by moving or
    linking it away, so removing one checkout can never touch the objects
    other checkouts, or the object store, still need) and never the
    durable per-transaction manifest at `verification_tool_snapshot_
    manifest_path_for` (evidence a caller may still need to read after
    this reclaim runs). Scoped by `transaction_id` in the checkout path
    itself, exactly like `cowork_verification.remove_command_checkout` is
    scoped by `index` -- reclaiming transaction A's checkout can never
    touch transaction B's checkout, which lives at a disjoint path keyed
    by B's own, different `transaction_id`.

    Idempotent and exception-safe, matching `remove_command_checkout`'s own
    contract: a checkout that was never materialized (`resolve_worker_
    source` returned `None`) or was already reclaimed is a silent no-op,
    never a raised error -- the checkout is disposable scratch space, not
    evidence, so a removal failure must never itself invalidate an
    otherwise-complete transaction. Safe to call from every parent-side
    terminal path (normal completion, startup failure, cancellation,
    timeout, exception) and safe to call more than once for the same
    transaction."""
    shutil.rmtree(tool_snapshot_checkout_dir(session_uuid, transaction_id),
                  ignore_errors=True)


def _materialize_tool_snapshot(session_uuid, transaction_id, manifest_files):
    """Build a FRESH, real directory tree at `tool_snapshot_checkout_dir`
    from the captured tool-snapshot manifest + content-addressed objects --
    a byte-for-byte copy out of the object store, never a hard link, so the
    checkout can never be mutated out from under the object store (or vice
    versa). This is the "captured path" `spawn_worker` execs the worker
    from; nothing else is ever launched as the worker.

    M5B-R-m5: starts from a CLEAN directory, exactly like the spine's own
    `materialize_command_checkout` -- a stale leftover at this same path
    (e.g. from a prior process that materialized but crashed before
    `reclaim_tool_snapshot_checkout` ever ran) must never silently merge
    into this fresh materialization."""
    dest_root = tool_snapshot_checkout_dir(session_uuid, transaction_id)
    if os.path.exists(dest_root):
        shutil.rmtree(dest_root)
    for rel, entry in manifest_files.items():
        dest = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        obj_path = state_store.verification_tool_snapshot_object_path(
            session_uuid, entry["sha256"])
        with open(obj_path, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
        os.chmod(dest, 0o755 if entry.get("mode") == "755" else 0o644)
    return dest_root


def resolve_worker_source(session_uuid=None, transaction_id=None):
    """Capture the worker's SOURCE from the running orchestrator's own
    Cowork-installation root (never the target-repo snapshot
    `spawn_worker`'s `checkout_root` parameter points at -- see
    `_installation_scripts_dir`), build a dedicated content-addressed tool
    snapshot (`cowork_state.verification_tool_snapshot_*`, Package A's own
    reserved state helpers), and materialize it into a fresh checkout --
    all BEFORE `spawn_worker` ever calls `Popen`, so the expected hash this
    returns is recorded before the worker process is launched, not after.

    Returns `None` -- the `worker_source_missing` condition -- when
    `session_uuid`/`transaction_id` are not both given, or when
    `_build_installation_manifest` cannot even find/read the installation's
    own `cowork_verification.py`. Never raises for either of those two
    conditions: a missing source is a reportable startup_failure, not an
    exception `spawn_worker`'s caller would have to newly handle.

    On success, returns `{"checkout_root", "manifest_files",
    "worker_rel_path", "expected_hash"}`: `checkout_root` is the captured
    path `spawn_worker` execs `scripts/cowork_verification.py` from and
    sets as the worker process's `cwd`; `manifest_files`/`worker_rel_path`
    match `verify_worker_identity`'s own parameter shapes one-for-one (the
    same manifest is also durably written to
    `verification_tool_snapshot_manifest_path_for` for
    `_classify_worker_startup` to read back after the worker reports its
    identity); `expected_hash` is `manifest_files[worker_rel_path]`'s own
    `sha256` -- the exact value the worker's self-report must equal."""
    if not session_uuid or not transaction_id:
        return None
    scripts_dir = _installation_scripts_dir()
    manifest_files, raw_by_rel = _build_installation_manifest(scripts_dir)
    if manifest_files is None:
        return None
    for rel, entry in manifest_files.items():
        _write_tool_snapshot_object(
            session_uuid, entry["sha256"], raw_by_rel[rel])
    manifest_doc = {
        "generated_at": state_store._utc_now(),
        "root": scripts_dir,
        "files": manifest_files,
    }
    manifest_path = state_store.verification_tool_snapshot_manifest_path_for(
        session_uuid, transaction_id)
    state_store.write_json_atomic(manifest_path, manifest_doc)
    checkout_root = _materialize_tool_snapshot(
        session_uuid, transaction_id, manifest_files)
    worker_rel_path = os.path.join("scripts", "cowork_verification.py")
    return {
        "checkout_root": checkout_root,
        "manifest_files": manifest_files,
        "worker_rel_path": worker_rel_path,
        "expected_hash": manifest_files[worker_rel_path]["sha256"],
    }


def spawn_worker(python_executable, checkout_root, request_path,
                 session_uuid=None, transaction_id=None):
    """Spawn the worker process with DEVNULL stdin, in a new process
    group/session (`start_new_session=True`), and a liveness pipe whose
    write end the parent holds and the worker's read end it inherits.

    PACKAGE B (#44): when `session_uuid`/`transaction_id` are both given,
    `resolve_worker_source` is called FIRST, before any pipe or process
    exists, to capture a dedicated, content-addressed snapshot of the
    RUNNING Cowork installation's own `scripts/` tree and materialize it
    into a fresh checkout. On success, `python3
    <captured-checkout>/scripts/cowork_verification.py --worker
    <request_path>` is what actually gets launched, with that captured
    checkout as `cwd` too -- `checkout_root` (the TARGET repo's own
    snapshot) is never consulted for source in this path. Only the
    degenerate case of no `session_uuid`/`transaction_id` at all (nothing
    could ever be captured into or verified against) falls back to
    `checkout_root`, exactly matching the base commit's own behavior for
    that case -- `_classify_worker_startup` already returns an empty,
    unverified classification whenever session/transaction are absent, so
    this fallback exists only to keep the process launch itself
    well-defined, never to satisfy the identity contract.

    When `resolve_worker_source` instead returns `None` (a real
    `session_uuid`/`transaction_id` were given, but the installation's own
    `cowork_verification.py` could not be found/read) this function never
    execs the missing source: a real, terminable placeholder process is
    still spawned (preserving the frozen three-item handle-bundle contract
    -- a genuine `proc` a caller can `poll()`/`terminate_worker()`), but it
    is NOT the worker, and `classification` is set directly to a
    `worker_source_missing` startup_failure without ever waiting on an
    identity report that could not possibly arrive.

    Returns a `WorkerStartupResult` (see its own docstring): the base
    commit's three-item handle bundle `(proc, liveness_write_fd,
    capture_thread)`, widened by exactly one field, `classification`.
    The caller closes `liveness_write_fd` to signal shutdown/cancel and the
    worker's watchdog thread observes EOF. `capture_thread` is `None` when
    no `session_uuid`/`transaction_id` was given (nothing to capture into);
    otherwise the caller MUST join it (bounded) before reading the startup
    log again -- see `_read_worker_startup_log` -- since the log is only
    flushed to disk once, when this thread finishes.

    `PYTHONDONTWRITEBYTECODE=1` is set so the captured checkout never gets
    `__pycache__` written into it (which would itself be a mutation of an
    "immutable" snapshot directory) -- the checkout is writable at the
    OS-permission level (cleanup must still be able to remove it), so this
    env var is the actual guard, not filesystem read-only enforcement.

    stdout/stderr are captured via a PIPE and a background thread
    (`_capture_startup_log`), bounded to `MAX_STARTUP_LOG_BYTES` on disk --
    NOT connected directly to an unbounded on-disk file -- so a worker that
    crashes before ever reporting identity leaves the parent a genuinely
    bounded, structured diagnostic instead of an unbounded disk write.
    """
    resolution = None
    if session_uuid and transaction_id:
        resolution = resolve_worker_source(session_uuid, transaction_id)
    source_missing = bool(session_uuid and transaction_id
                          and resolution is None)

    worker_root = resolution["checkout_root"] if resolution else checkout_root
    worker_script = os.path.join(worker_root, "scripts",
                                 "cowork_verification.py")

    read_fd, write_fd = os.pipe()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    capture_log_path = None
    if session_uuid and transaction_id:
        capture_log_path = state_store.verification_worker_startup_log_path_for(
            session_uuid, transaction_id)
        os.makedirs(os.path.dirname(capture_log_path), exist_ok=True)
    stdout_target = subprocess.PIPE if capture_log_path else subprocess.DEVNULL
    stderr_target = (subprocess.STDOUT if capture_log_path
                     else subprocess.DEVNULL)
    if source_missing:
        # Never exec the missing/uncaptured source: a trivial, immediately-
        # exiting REAL command instead -- distinguishable, bounded, and
        # deliberately not "the worker" in any sense (its own exit code
        # never collides with 0/None/WORKER_EXIT_REQUEST_REJECTED). `cwd`
        # is left at the parent's own (always-valid) working directory,
        # never `worker_root` -- with no captured checkout to launch from,
        # `worker_root` here just echoes the caller's `checkout_root`
        # argument, which this placeholder must not depend on existing.
        argv = [python_executable, "-c", "import sys; sys.exit(97)"]
        popen_cwd = None
    else:
        argv = [python_executable, worker_script, "--worker", request_path,
                "--liveness-fd", str(read_fd)]
        popen_cwd = worker_root
    try:
        proc = subprocess.Popen(
            argv, cwd=popen_cwd, stdin=subprocess.DEVNULL,
            stdout=stdout_target, stderr=stderr_target,
            start_new_session=True, env=env,
            pass_fds=(read_fd,), close_fds=True)
    except BaseException:
        # `Popen` raising means no worker process exists to ever hold or
        # observe the write end either -- close BOTH ends on this path
        # before propagating.
        os.close(read_fd)
        os.close(write_fd)
        raise
    else:
        os.close(read_fd)
    capture_thread = None
    if capture_log_path and proc.stdout is not None:
        capture_thread = threading.Thread(
            target=_capture_startup_log,
            args=(proc.stdout, capture_log_path, MAX_STARTUP_LOG_BYTES),
            daemon=True)
        capture_thread.start()

    if source_missing:
        try:
            exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            exit_code = proc.poll()
        if capture_thread is not None:
            capture_thread.join(timeout=5)
        classification = {
            "identity": None, "worker_verified": False,
            "startup_failure": {
                "reason": "worker_source_missing",
                "exit_code": exit_code,
                "log_tail": _read_worker_startup_log(
                    session_uuid, transaction_id),
            },
        }
    else:
        classification = _classify_worker_startup(
            proc, capture_thread, request_path, session_uuid, transaction_id)
    # Fallback carrier for `classification`: a caller that destructures this
    # function's return into a bare 3-tuple and forwards THAT onward (see
    # `WorkerStartupResult`'s own docstring) still forwards the same `proc`
    # object, so stashing `classification` on it lets the spine recover it
    # even through such a wrapper, with zero cooperation required from the
    # wrapper itself.
    proc._cowork_startup_classification = classification
    return WorkerStartupResult(proc, write_fd, capture_thread, classification)
