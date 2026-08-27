#!/usr/bin/env python3
"""Worker-capture extraction seam (M5 Package A; garusis/cowork-internal#44).

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
`session_uuid`/`transaction_id` alone, via the exact same deterministic
`cowork_state.verification_snapshot_manifest_path_for` path the spine's own
snapshot step already wrote) is what lets a LATER package (B, #44) change
WHICH manifest is captured and consulted -- the Cowork-installation
tool-snapshot manifest, instead of this target-repo one -- entirely inside
this file, without any spine edit. Package A's own candidate keeps the
base's exact target-repo-manifest behavior; only Package B changes the
source (see `resolve_worker_source` below, its documented extension point).

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

import os
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
    (cowork_verification.py:2467-2468, :2510-2541) -- the manifest_files/
    worker_rel_path derivation, the identity read, the verification call,
    and the request_rejected/worker_exited_before_identity_report
    distinction are all bound-for-bound identical to the base, just moved
    here and computed before `spawn_worker` returns instead of after.
    `worker_spawn_failed` is explicitly NOT one of the reasons this can
    produce -- that reason is minted by `run_transaction`'s own `except
    OSError` handler in the spine, for the case where `Popen` itself raises
    before this function (or `spawn_worker`) is ever reached."""
    if not session_uuid or not transaction_id:
        return {"identity": None, "worker_verified": False,
               "startup_failure": None}
    verification = _spine()
    timeout_s = _startup_allowance_s(request_path)
    identity = _read_worker_identity(
        session_uuid, transaction_id, timeout_s=timeout_s, proc=proc)
    manifest_path = state_store.verification_snapshot_manifest_path_for(
        session_uuid, transaction_id)
    manifest_doc = state_store.read_json_tolerant(manifest_path)
    manifest_files = (manifest_doc or {}).get("files", {})
    worker_rel_path = os.path.join("scripts", "cowork_verification.py")
    worker_verified = verify_worker_identity(
        identity, manifest_files, worker_rel_path)
    startup_failure = None
    if not worker_verified and identity is None:
        # ORCH-030: distinguish "the worker process already exited without
        # ever reporting identity" (a real startup failure, with a captured
        # reason) from "still running, gave up waiting".
        exit_code = proc.poll()
        if exit_code is not None:
            # The process has exited, so its stdout pipe has already
            # delivered EOF to `_capture_startup_log` -- join it (bounded)
            # BEFORE reading, so `log_tail` is never read from a
            # partially-written file.
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
    return {"identity": identity, "worker_verified": worker_verified,
           "startup_failure": startup_failure}


def spawn_worker(python_executable, checkout_root, request_path,
                 session_uuid=None, transaction_id=None):
    """Spawn `python3 <checkout_root>/scripts/cowork_verification.py --worker
    <request_path>` with DEVNULL stdin, in a new process group/session
    (`start_new_session=True`), and a liveness pipe whose write end the
    parent holds and the worker's read end it inherits.

    Returns a `WorkerStartupResult` (see its own docstring): the base
    commit's three-item handle bundle `(proc, liveness_write_fd,
    capture_thread)`, widened by exactly one field, `classification`,
    computed by `_classify_worker_startup` before this function returns.
    The caller closes `liveness_write_fd` to signal shutdown/cancel and the
    worker's watchdog thread observes EOF. `capture_thread` is `None` when
    no `session_uuid`/`transaction_id` was given (nothing to capture into);
    otherwise the caller MUST join it (bounded) before reading the startup
    log again -- see `_read_worker_startup_log` -- since the log is only
    flushed to disk once, when this thread finishes.

    `PYTHONDONTWRITEBYTECODE=1` is set so the bootstrap checkout never gets
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
    worker_script = os.path.join(checkout_root, "scripts",
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
    try:
        proc = subprocess.Popen(
            [python_executable, worker_script, "--worker", request_path,
             "--liveness-fd", str(read_fd)],
            cwd=checkout_root, stdin=subprocess.DEVNULL,
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


def resolve_worker_source(session_uuid=None, transaction_id=None):
    """Extension point for Package B (garusis/cowork-internal#44): capture
    the worker's SOURCE from the running orchestrator's own
    Cowork-installation root (not the target-repo snapshot `spawn_worker`
    uses today), build the dedicated tool snapshot
    (`cowork_state.verification_tool_snapshot_*`), and verify the worker's
    identity report against THAT manifest instead of the target-repo one --
    adding the two new startup_failure reasons (worker_source_missing,
    worker_identity_mismatch) into the `classification` dict `spawn_worker`'s
    widened `WorkerStartupResult` already carries back to the spine.

    Package A's own `spawn_worker`/`_classify_worker_startup` never call
    this: the base's two spawn-adjacent startup_failure reasons
    (request_rejected, worker_exited_before_identity_report) and the
    target-repo manifest_files/worker_rel_path derivation are fully
    relocated and working as of this candidate, exercised by the base's own
    `scripts/test_cowork.py` fixtures. This stub exists purely so #44's
    remaining work has a named, documented seam to fill in -- entirely
    inside this file -- without any further spine edit. Raises
    NotImplementedError until Package B fills it in."""
    raise NotImplementedError(
        "Package B implements Cowork-installation tool-snapshot worker-"
        "source resolution here (garusis/cowork-internal#44); Package A's "
        "own spawn_worker/classification never calls this stub.")
