#!/usr/bin/env python3
"""Durable single-writer session ownership: the owner-lease store, its
liveness classifier, and the global provider-session exclusivity index
(garusis/cowork-internal#64, implementation package **P1**).

This module is PURE STORE plus PURE CLASSIFICATION. It is deliberately not
wired into `cowork.py` by this package: P2 adds the ownership gate and the
crash-safe lifecycle, P3 the provider/controller exclusivity call sites, P4
the status/report surface. Nothing here imports `cowork.py` or
`cowork_control_plane.py`, and nothing here spawns, dispatches, or sends.

WHAT THE LEASE IS FOR. Two cowork processes driving one session corrupt it:
both write `session.json`, both advance phases, both resume the same provider
conversation. The lease makes exactly one process the writer, and makes that
fact DURABLE -- a fencing token (`owner_id`) plus a strictly monotonic `epoch`
that every governed write is checked against, rather than an in-process flag a
crash silently loses.

THE ATOMICITY BOUNDARY (plan §3.3, rules W1-W5), which is the whole design:

  W1  `owner/lease.json` has exactly ONE writer shape. Every transition --
      acquire, renew, release, mark-terminal-under-the-lock, take over -- runs
      as one `mutate(existing)` closure inside
      `state_store._locked_json_transaction(state_store.owner_lease_path_for(
      session_uuid), mutate)`, which holds a real cross-process
      `fcntl.flock(LOCK_EX)`, reads the record FRESH under that lock, and
      persists through `write_json_atomic_durable` (fsync of bytes AND parent
      dir). NO reference to `owner_lease_path_for(...)` appears anywhere in
      this module except as that call's first argument -- including the
      READ-ONLY paths, which pass a `lambda existing: None` mutate and so read
      under the lock without writing. That is what makes `epoch`'s
      monotonicity a structural fact rather than a hope.

  W2  Every locked transition is a COMPARE-AND-SWAP. `renew`, `release` and the
      locked terminal path each refuse -- by returning `None` from `mutate`, so
      the decision and the not-writing are both inside the held lock -- when
      the durable record does not name this `(owner_id, epoch)` or is no longer
      `state == "live"`. A predecessor can therefore never overwrite a
      successor's lease, and a straggling heartbeat can never resurrect a
      released or terminal one. See `_cas_mutate`.

  W3  The SIGNAL path never touches `lease.json`. `_locked_json_transaction`
      must not be re-entered from a signal handler that may have interrupted
      the same lock (its own docstring says so). `mark_owner_terminal_unlocked`
      therefore takes no lock, reads nothing, and performs exactly one
      `write_json_atomic_durable` to the PER-OWNER sidecar
      `owner/terminal.<owner_id>.json`. Because `owner_id` is minted fresh on
      every acquisition and never reused, that filename is unique to the
      marking owner for all time.

  W4  Sidecars are CONSULTED, never trusted blindly. `classify_owner_lease`
      folds a sidecar only when it names the CURRENT lease's `(owner_id,
      epoch)`; a predecessor's orphan is ignored entirely, and so is a corrupt
      one (a sidecar can only ever RELEASE a lease, so ignoring it fails toward
      refusing -- the safe direction).

  W5  Folding and sweeping happen UNDER the lock: `acquire_owner_lease` and
      `take_over` fold a matching sidecar into their in-lock view of the
      existing record, and, after the successor is written, unlink every
      sidecar naming a different owner. Sweeping is best-effort -- an unlink
      failure never fails an acquisition, because an orphan sidecar is inert
      by W4.

FAIL-SAFE DIRECTION, fixed and uniform: every uncertainty resolves toward
REFUSING. Death is never assumed, a live owner is never declared dead, and a
crashed owner's lease is NEVER implicitly reclaimed -- `acquire_owner_lease`
raises on `stale_dead_owner`, and recovery requires an explicit
`take_over(..., "proved_dead")` with in-lock proof. This mirrors
`cowork_verification_evidence._worker_pid_start_corroborated`'s own stated
safety direction, and it is why an unreadable lease classifies `corrupt`
(refuse) rather than `unowned` (acquire).

PID IDENTITY. `pid` alone cannot fence anything -- the OS recycles pids. Every
lease therefore records the owning process's OWN start time from
`ps -o lstart=`, converted through
`cowork_verification_evidence._local_naive_to_aware` (DST-ambiguity-safe,
never a naive-local-as-UTC guess), and liveness compares it for EQUALITY TO
THE SECOND. A recycled pid characteristically carries a different start time,
so it classifies `stale_dead_owner` and is never signalled. Where no start
time can be read at all, `pid_start_source` records `"unavailable"` honestly
and every later verdict downgrades to `stale_unproven` -- never to a false
"dead".

THE CLOSED BINDING-EXCEPTION SET (plan §4 P1, `SW64V-M02`). The three provider
-binding functions are TOTAL over their failure modes: every `TimeoutError`,
`OSError`, `state_store.CorruptRecordError` and `ValueError` reaching their
boundary is re-raised as `ProviderBindingUnavailable` FROM the original, so
`__cause__` always names the real failure and the published surface stays
`ProviderSessionConflict` (from `bind_provider_session` only) plus
`ProviderBindingUnavailable`, and nothing else. That closure is what lets the
eventual caller keep an explicitly-named, non-anonymous handler list. There is
NO bare `except:` and NO `except Exception` anywhere in this module, on
purpose.

A PROVEN COLLISION AND AN UNVERIFIABLE INDEX ARE DIFFERENT FACTS, and this
module never conflates them: `ProviderSessionConflict` means a different LIVE
session owns that provider conversation; `ProviderBindingUnavailable` means
the index could not be consulted or written and proves nothing at all.

IMPLEMENTATION STATE, declared rather than left to be discovered: `_BIND_LOG`
is this module's in-process record of the binding keys THIS process bound, so
`release_provider_bindings` can iterate exactly those keys and never the whole
global directory (plan §3.6, "bounded"). It is ordinary implementation state,
not part of the published contract, and nothing reads it to make a
correctness decision -- a lost log only means fewer records are cleaned up,
which rule 2 of §3.6 already makes self-healing.

Python 3.9+, stdlib only -- matches `cowork_state.py` and
`cowork_verification_evidence.py`. Import direction is
`cowork_owner -> cowork_verification_evidence -> cowork_state`; no cycle.
"""

import datetime
import hashlib
import os
import platform
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_state as state_store  # noqa: E402
import cowork_verification_evidence as evidence  # noqa: E402


# --------------------------------------------------------------------------- #
# Schema and policy constants.                                                 #
# --------------------------------------------------------------------------- #

OWNER_LEASE_SCHEMA_VERSION = 1
OWNER_LEASE_RECORD = "SessionOwnerLease"
OWNER_TERMINAL_MARK_RECORD = "OwnerTerminalMark"

# Derived from the mechanism that actually renews (plan §3.5): a dedicated
# heartbeat daemon that fires independently of turns and of in-flight sends,
# so a healthy owner parked for hours at a human gate keeps renewing. The
# grace is sized for one missed tick plus a slow fsync -- NOT for a human gate,
# which no longer interacts with the deadline at all. 30 + 120 = a 150s
# deadline a healthy owner refreshes five times over.
DEFAULT_HEARTBEAT_INTERVAL_S = 30
DEFAULT_LEASE_GRACE_S = 120

# TERM-then-KILL escalation for an operator-authorized `terminate_prior`
# takeover, mirroring `cowork_verification.terminate_worker`'s shape and its
# `DEFAULT_TERM_GRACE_S = 10` / `wait(timeout=5)` budget.
TAKEOVER_TERM_GRACE_S = 10
TAKEOVER_KILL_GRACE_S = 5

# The closed verdict vocabulary. `acquire_owner_lease`'s docstring names an
# outcome for every one of these, and gate G4d asserts the two agree.
OWNER_VERDICTS = frozenset({
    "unowned",
    "live_owner",
    "stale_dead_owner",
    "stale_unproven",
    "corrupt",
})

_LEASE_STATES = frozenset({"live", "released", "terminal"})
_ENTRY_POINTS = frozenset({"run_flow", "resume_trigger"})
_TAKEOVER_MODES = frozenset({"proved_dead", "terminate_prior"})

# `ps -o lstart=` prints this host's LOCAL wall clock with no offset field.
_PS_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
_PS_TIMEOUT_S = 5
_TAKEOVER_POLL_S = 0.1

# Every binding key this process bound, grouped by its owning (session_uuid,
# owner_id) -- see the module docstring's IMPLEMENTATION STATE note.
_BIND_LOG = {}


# --------------------------------------------------------------------------- #
# Exception hierarchy -- DECLARED, not inferred (plan §3.7 rule E1).           #
#                                                                              #
# One base and five subclasses, all deriving from `Exception` and none from    #
# `BaseException`. That choice is decided rather than incidental: it matches   #
# `state_store.PauseLeaseConflict(Exception)`, it lets one `except            #
# OwnerLeaseError` backstop cover every subclass (catch the base, never a      #
# name tuple, which is exactly the omission SW64S-M02 recorded), and a         #
# `BaseException` base would escape `cowork_eval.drain`'s `except Exception`   #
# and silently break that function's pinned "never raises" invariant in a file #
# no package is authorized to modify.                                          #
#                                                                              #
# The consequence, stated so callers design around it rather than discover it: #
# a plain `except Exception:` DOES absorb an owner refusal.                    #
# --------------------------------------------------------------------------- #


class OwnerLeaseError(Exception):
    """Base of every owner-lease and provider-binding refusal this module
    raises. Catch THIS, never a tuple of subclass names: a subclass added
    later is then covered by construction instead of silently falling out of
    a stale handler list."""


class OwnerLeaseConflict(OwnerLeaseError):
    """Another process's lease stands in the way of an acquisition. `reason`
    is one of the closed `OWNER_VERDICTS` (`live_owner`, `stale_dead_owner`,
    `stale_unproven`) or `foreign_host` for a refused takeover -- always a
    stable machine-readable string, never free prose."""

    def __init__(self, session_uuid, reason):
        super().__init__("session %s: owner lease refuses acquisition (%s)"
                         % (session_uuid, reason))
        self._session_uuid = session_uuid
        self._reason = reason

    @property
    def session_uuid(self):
        return self._session_uuid

    @property
    def reason(self):
        return self._reason


class OwnerLeaseCorrupt(OwnerLeaseError):
    """The durable lease record exists but could not be read as a
    `SessionOwnerLease` of a known schema version -- or could not be read at
    all. Damaged or unreadable state conflicts EXPLICITLY (the same rule
    `state_store.CorruptRecordError` exists for): it never collapses to
    "absent" and lets a caller acquire over whatever the damaged file was
    still holding. `__cause__` names the underlying failure whenever there was
    one."""

    def __init__(self, session_uuid, detail=None):
        super().__init__("session %s: owner lease is unreadable or corrupt%s"
                         % (session_uuid, ": %s" % detail if detail else ""))
        self._session_uuid = session_uuid
        self._detail = detail

    @property
    def session_uuid(self):
        return self._session_uuid

    @property
    def detail(self):
        return self._detail


class OwnerLeaseLost(OwnerLeaseError):
    """The durable record no longer names this holder -- the fencing check
    every governed write must pass has failed. Raised by `assert_owner`."""

    def __init__(self, session_uuid, owner_id=None, epoch=None,
                 reason="not_owner"):
        super().__init__(
            "session %s: owner lease no longer names owner %s epoch %s (%s)"
            % (session_uuid, owner_id, epoch, reason))
        self._session_uuid = session_uuid
        self._owner_id = owner_id
        self._epoch = epoch
        self._reason = reason

    @property
    def session_uuid(self):
        return self._session_uuid

    @property
    def owner_id(self):
        return self._owner_id

    @property
    def epoch(self):
        return self._epoch

    @property
    def reason(self):
        return self._reason


class ProviderSessionConflict(OwnerLeaseError):
    """PROOF that a different, currently LIVE-leased cowork session owns this
    `(controller, provider_session_id)` provider conversation. Raised by
    `bind_provider_session` and by nothing else.

    Carries the four typed attributes an operator message and a typed run-end
    reason need, so a refusal can be reported without a second index
    lookup."""

    def __init__(self, controller, provider_session_id, owner_session_uuid,
                 role=None):
        super().__init__(
            "%s session %s is already bound to live cowork session %s"
            % (controller, provider_session_id, owner_session_uuid))
        self._controller = controller
        self._provider_session_id = provider_session_id
        self._owner_session_uuid = owner_session_uuid
        self._role = role

    @property
    def controller(self):
        return self._controller

    @property
    def provider_session_id(self):
        return self._provider_session_id

    @property
    def owner_session_uuid(self):
        return self._owner_session_uuid

    @property
    def role(self):
        return self._role


class ProviderBindingUnavailable(OwnerLeaseError):
    """The exclusivity index could not be CONSULTED or WRITTEN -- a lock
    timeout, an I/O failure, a corrupt record, or a rejected identifier.

    This is NOT a collision and must never be reported to an operator as one.
    It proves nothing about who owns the provider conversation, which is why
    its caller's obligations are the mirror image of a conflict's: a paid
    provider session id observed alongside this exception must still be
    persisted (stranding a live conversation outside the durable record that
    names it is the very defect #64 exists to remove), while the failure is
    still reported at the next governed seam.

    Always raised `from` the underlying exception, so `__cause__` names the
    real `TimeoutError` / `OSError` / `CorruptRecordError` / `ValueError`."""

    def __init__(self, controller, provider_session_id, role=None,
                 detail=None):
        super().__init__(
            "could not verify provider-session exclusivity for %s session %s%s"
            % (controller, provider_session_id,
               ": %s" % detail if detail else ""))
        self._controller = controller
        self._provider_session_id = provider_session_id
        self._role = role
        self._detail = detail

    @property
    def controller(self):
        return self._controller

    @property
    def provider_session_id(self):
        return self._provider_session_id

    @property
    def role(self):
        return self._role

    @property
    def detail(self):
        return self._detail


# --------------------------------------------------------------------------- #
# Clock and host identity.                                                     #
# --------------------------------------------------------------------------- #


def _utc_now():
    """This module's single clock reading, in the `...Z` form every persisted
    timestamp in the repository uses (identical to
    `cowork_verification_evidence._utc_now`)."""
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _parse_instant(value):
    """Parse a persisted `...Z` timestamp back to an aware datetime, or None
    when it is absent or unparseable. Never guesses a timezone."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _resolve_now(now):
    """Accept an injected clock as an aware datetime or as a `...Z` string --
    fixtures inject `now=` rather than sleeping -- and fall back to the real
    clock. A naive datetime is refused rather than silently read as UTC."""
    if now is None:
        return _utc_now()
    if isinstance(now, datetime.datetime):
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware, not naive")
        return now
    parsed = _parse_instant(now)
    if parsed is None:
        raise ValueError("now %r is not an ISO-8601 instant with an offset"
                         % (now,))
    return parsed


def _boot_time_marker():
    """A stable per-boot string, so `host_id` changes when the machine
    reboots and the pid space is recycled wholesale.

    `sysctl -n kern.boottime` first (macOS/BSD, and the reading this host's
    own pids are numbered against); PID 1's start time second, which is the
    same fact by another route and works wherever `ps` does. Both failing,
    a fixed literal: `host_id` then still scopes a lease to this MACHINE,
    which is the property `pid`/`pid_start_at` actually need, and the pid
    -start equality check below remains the real fencing test."""
    try:
        result = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                capture_output=True, text=True,
                                timeout=_PS_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        text = result.stdout.strip()
        marker = text.split("sec = ", 1)[-1].split(",", 1)[0].strip()
        if marker:
            return "kern.boottime:%s" % marker
    init_start = read_process_start_time(1)
    if init_start:
        return "pid1.lstart:%s" % init_start
    return "boot.unavailable"


_BOOT_MARKER = None


def host_id():
    """Stable identity of the MACHINE this process runs on:
    `sha256(nodename + "\\x1f" + boot marker)`.

    It scopes `pid` and `pid_start_at` to the host that produced them. A lease
    carrying a different `host_id` is NEVER pid-probed and NEVER auto-taken-
    over -- its process table is simply not ours to read, so its owner's death
    is unprovable from here and the verdict must be `stale_unproven`. The boot
    marker is included so a reboot (which recycles the entire pid space) yields
    a different host identity rather than letting a pre-reboot pid appear to
    corroborate a post-reboot process."""
    global _BOOT_MARKER
    if _BOOT_MARKER is None:
        _BOOT_MARKER = _boot_time_marker()
    raw = "\x1f".join((platform.uname().node or "", _BOOT_MARKER))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Process-start identity -- the fencing companion to `pid`.                    #
# --------------------------------------------------------------------------- #


def _probe_pid_start(pid):
    """Probe the local process table for `pid`'s own start time.

    Returns one of three genuinely different answers, because collapsing them
    is exactly how a live owner gets declared dead:

      ("absent",  None)  -- `ps` positively reported no such process;
      ("started", iso )  -- the process exists and started at `iso` (UTC);
      ("unknown", None)  -- `ps` was unavailable, errored, printed something
                            unexpected, or printed a local wall clock that is
                            genuinely AMBIGUOUS (a DST fall-back's repeated
                            hour). NOT evidence of anything.

    The naive local reading is converted through
    `cowork_verification_evidence._local_naive_to_aware`, never treated as if
    it were already UTC -- on a timezone-shifted host that error is worth
    hours, which is more than enough to make a recycled pid look like the
    original owner."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ("unknown", None)
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if not out:
        # `ps` exits non-zero with no output for "no such process"; anything
        # written to stderr means `ps` itself failed, which proves nothing.
        return ("unknown", None) if err else ("absent", None)
    try:
        started_naive = datetime.datetime.strptime(out, _PS_LSTART_FORMAT)
    except ValueError:
        return ("unknown", None)
    started = evidence._local_naive_to_aware(started_naive)
    if started is None:
        return ("unknown", None)
    return ("started", _iso(started))


def read_process_start_time(pid):
    """This process's -- or any local pid's -- own start time as a `...Z`
    string, or None when it could not be read truthfully. None is honest
    ignorance, never a guess: a lease built on it records
    `pid_start_source: "unavailable"` and every later liveness verdict for it
    downgrades to `stale_unproven` rather than to a false "dead"."""
    status, started = _probe_pid_start(pid)
    return started if status == "started" else None


# --------------------------------------------------------------------------- #
# Records.                                                                     #
# --------------------------------------------------------------------------- #


def owner_identity(session_uuid, entry_point, launch_dir, session_file):
    """Mint this process's claim on `session_uuid` -- the `OwnerIdentity` a
    caller holds for the whole owned region and hands to `acquire_owner_lease`,
    `take_over` and `bind_provider_session`.

    `owner_id` is a fresh uuid4 and is THE FENCING TOKEN: minted on every
    acquisition and every takeover, never reused, which is what makes the
    per-owner terminal sidecar path unique for all time and what a governed
    write is checked against. Paths are realpath'd so the same anchor reached
    through a symlink or a worktree chdir is recorded identically."""
    state_store._assert_safe_identifier(session_uuid, "session_uuid")
    if entry_point not in _ENTRY_POINTS:
        raise ValueError("entry_point %r is not one of %s"
                         % (entry_point, sorted(_ENTRY_POINTS)))
    pid = os.getpid()
    pid_start_at = read_process_start_time(pid)
    return {
        "session_uuid": session_uuid,
        "owner_id": str(uuid.uuid4()),
        "host_id": host_id(),
        "pid": pid,
        "pid_start_at": pid_start_at,
        "pid_start_source": "ps_lstart" if pid_start_at else "unavailable",
        "launch_dir": os.path.realpath(launch_dir) if launch_dir else None,
        "session_file": (os.path.realpath(session_file)
                         if session_file else None),
        "entry_point": entry_point,
    }


def validate_owner_lease(record):
    """Return `record` when it is a well-formed `SessionOwnerLease` v1, else
    raise `OwnerLeaseCorrupt`.

    An UNKNOWN `schema_version` is corrupt, not tolerable: a record written by
    a future cowork carries invariants this code cannot honour, and guessing
    at them is how one silently acquires over a live owner."""
    if not isinstance(record, dict):
        raise OwnerLeaseCorrupt(None, "record is %s, not an object"
                                % type(record).__name__)
    session_uuid = record.get("session_uuid")
    if record.get("record") != OWNER_LEASE_RECORD:
        raise OwnerLeaseCorrupt(session_uuid, "record is %r, expected %r"
                                % (record.get("record"), OWNER_LEASE_RECORD))
    if record.get("schema_version") != OWNER_LEASE_SCHEMA_VERSION:
        raise OwnerLeaseCorrupt(
            session_uuid, "unknown schema_version %r"
            % (record.get("schema_version"),))
    if not isinstance(session_uuid, str) or not session_uuid:
        raise OwnerLeaseCorrupt(None, "session_uuid is missing or not a string")
    if not isinstance(record.get("owner_id"), str) or not record.get("owner_id"):
        raise OwnerLeaseCorrupt(session_uuid,
                                "owner_id is missing or not a string")
    epoch = record.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise OwnerLeaseCorrupt(session_uuid, "epoch %r is not >= 1" % (epoch,))
    if record.get("state") not in _LEASE_STATES:
        raise OwnerLeaseCorrupt(session_uuid, "state %r is not one of %s"
                                % (record.get("state"), sorted(_LEASE_STATES)))
    return record


def _build_lease(claimant, epoch, now, predecessor=None,
                 heartbeat_interval_s=DEFAULT_HEARTBEAT_INTERVAL_S,
                 lease_grace_s=DEFAULT_LEASE_GRACE_S):
    """The one place a `live` lease record is constructed, so every acquisition
    and every takeover produce exactly the same shape and the same derived
    deadline."""
    stamp = _iso(now)
    deadline = now + datetime.timedelta(
        seconds=heartbeat_interval_s + lease_grace_s)
    return {
        "schema_version": OWNER_LEASE_SCHEMA_VERSION,
        "record": OWNER_LEASE_RECORD,
        "session_uuid": claimant["session_uuid"],
        "owner_id": claimant["owner_id"],
        "epoch": epoch,
        "state": "live",
        "host_id": claimant.get("host_id"),
        "pid": claimant.get("pid"),
        "pid_start_at": claimant.get("pid_start_at"),
        "pid_start_source": claimant.get("pid_start_source", "unavailable"),
        "launch_dir": claimant.get("launch_dir"),
        "session_file": claimant.get("session_file"),
        "entry_point": claimant.get("entry_point"),
        "acquired_at": stamp,
        "heartbeat_at": stamp,
        "heartbeat_interval_s": heartbeat_interval_s,
        "lease_grace_s": lease_grace_s,
        "lease_deadline_at": _iso(deadline),
        "released_at": None,
        "terminal_reason": None,
        "predecessor": predecessor,
        "taken_over_by": None,
    }


def _deadline_of(record):
    """The lease's own deadline, recomputed from `heartbeat_at` when the
    persisted `lease_deadline_at` is missing or unparseable so a slightly
    damaged-but-valid record still expires rather than living forever."""
    deadline = _parse_instant(record.get("lease_deadline_at"))
    if deadline is not None:
        return deadline
    heartbeat = _parse_instant(record.get("heartbeat_at"))
    if heartbeat is None:
        return None
    interval = record.get("heartbeat_interval_s") or DEFAULT_HEARTBEAT_INTERVAL_S
    grace = record.get("lease_grace_s") or DEFAULT_LEASE_GRACE_S
    return heartbeat + datetime.timedelta(seconds=interval + grace)


# --------------------------------------------------------------------------- #
# Terminal sidecars -- the signal-safe path (rules W3/W4/W5).                  #
# --------------------------------------------------------------------------- #


def read_terminal_mark(session_uuid, owner_id):
    """Read ONE owner's terminal sidecar, or None.

    Deliberately TOLERANT, and that direction is the safe one: a sidecar can
    only ever RELEASE a lease, so a corrupt or unreadable one being ignored
    means the lease keeps standing and the next acquirer is refused. Failing
    the other way -- treating an unreadable sidecar as a release -- would let a
    second writer in on nothing but damaged bytes."""
    try:
        record = state_store._read_json_or_raise_if_corrupt(
            state_store.owner_terminal_mark_path_for(session_uuid, owner_id))
    except (state_store.CorruptRecordError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("record") != OWNER_TERMINAL_MARK_RECORD:
        return None
    if record.get("schema_version") != OWNER_LEASE_SCHEMA_VERSION:
        return None
    return record


def mark_owner_terminal_unlocked(session_uuid, owner_id, epoch, reason,
                                 owner_matched=True, now=None):
    """Mark THIS owner terminal from a signal handler, and return the sidecar
    path written.

    Takes NO lock, reads nothing, appends to no JSONL, and NEVER writes
    `owner/lease.json` (rule W3): `_locked_json_transaction` must not be
    re-entered from a handler that may have interrupted the very same lock, and
    the original design's unconditional whole-file rewrite of the lease was the
    blocker this replaces. It performs exactly ONE
    `write_json_atomic_durable` to the per-owner path
    `owner/terminal.<owner_id>.json`.

    It cannot name any other owner's file. `owner_id` is a fresh uuid4 per
    acquisition, never reused, so this path is unique to the marking owner for
    all time -- which is precisely why a SIGTERM delivered to a predecessor
    AFTER a successor has taken over leaves the successor's lease
    byte-identical, and why `epoch` can never repeat.

    Idempotent under a repeated signal: same path, same content shape.
    `owner_matched` records, for the audit only, whether this process's
    in-memory `(owner_id, epoch)` still matched what it had acquired; it never
    suppresses the mark or any other handler effect."""
    path = state_store.owner_terminal_mark_path_for(session_uuid, owner_id)
    # The path argument is spelled out INLINE rather than passed as `path`:
    # this is the one place in the repository where a durable write touches an
    # owner path outside `_locked_json_transaction`, and the structural sweep
    # that proves it reads the call's argument, not a variable bound earlier.
    state_store.write_json_atomic_durable(
        state_store.owner_terminal_mark_path_for(session_uuid, owner_id), {
        "schema_version": OWNER_LEASE_SCHEMA_VERSION,
        "record": OWNER_TERMINAL_MARK_RECORD,
        "session_uuid": session_uuid,
        "owner_id": owner_id,
        "epoch": epoch,
        "terminal_reason": reason,
        "marked_at": _iso(_resolve_now(now)),
        "owner_matched": bool(owner_matched),
        })
    return path


def _fold_terminal_mark(session_uuid, record):
    """Fold a MATCHING terminal sidecar into an in-lock view of the lease
    (rule W4), returning `(folded_record, mark_or_None)`.

    A sidecar counts only when it names the CURRENT lease's `(owner_id,
    epoch)`. A predecessor's orphan -- the whole SIGTERM-versus-takeover race
    -- names a different pair and is ignored entirely, so it can never release
    a successor's lease. The fold is a projection: `record` itself is never
    mutated, and nothing is written here."""
    if not isinstance(record, dict) or record.get("state") != "live":
        return (record, None)
    mark = read_terminal_mark(session_uuid, record.get("owner_id"))
    if not mark:
        return (record, None)
    if (mark.get("owner_id") != record.get("owner_id")
            or mark.get("epoch") != record.get("epoch")):
        return (record, None)
    folded = dict(record)
    folded["state"] = "terminal"
    folded["terminal_reason"] = mark.get("terminal_reason")
    return (folded, mark)


def _sweep_terminal_marks(session_uuid, keep_owner_id):
    """Unlink every terminal sidecar naming an owner other than
    `keep_owner_id`, after a successor record has been written (rule W5).

    BEST-EFFORT by design: an unlink failure never fails an acquisition,
    because an orphan sidecar is already inert under W4's matching rule. This
    exists to bound growth -- to "one file per owner that was SIGTERMed and
    whose session was never re-acquired" -- not to establish correctness.
    Returns the number of sidecars removed."""
    removed = 0
    try:
        entries = os.listdir(state_store.owner_dir_for(session_uuid))
    except (OSError, ValueError):
        return removed
    keep_name = "terminal.%s.json" % keep_owner_id
    for name in entries:
        if not name.startswith("terminal.") or not name.endswith(".json"):
            continue
        if name == keep_name:
            continue
        try:
            os.remove(os.path.join(
                state_store.owner_dir_for(session_uuid), name))
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- #
# History -- append-only audit, observational only.                            #
# --------------------------------------------------------------------------- #


def append_owner_history(session_uuid, entry):
    """Append one audit record to `owner/history.jsonl` and report whether it
    landed.

    Purely observational: no decision anywhere is taken from this log, so a
    failed append is returned as False rather than raised. Reuses
    `state_store.append_jsonl_atomic`, whose durability contract (full write
    plus fsync, or nothing) is the one this audit needs."""
    record = dict(entry or {})
    record.setdefault("schema_version", OWNER_LEASE_SCHEMA_VERSION)
    record.setdefault("record", "SessionOwnerLeaseHistory")
    record.setdefault("session_uuid", session_uuid)
    record.setdefault("at", _iso(_utc_now()))
    try:
        return bool(state_store.append_jsonl_atomic(
            state_store.owner_history_path_for(session_uuid), record))
    except (TimeoutError, OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Liveness classification (plan §3.4) -- the single normative acquire gate.    #
# --------------------------------------------------------------------------- #


def _classify_record(record, now, probe=True):
    """Classify an ALREADY-READ lease record. Pure: no lease-path I/O at all,
    which is what lets `acquire_owner_lease` and `take_over` re-evaluate the
    verdict INSIDE their own held lock without re-entering it."""
    if record is None:
        return "unowned"
    try:
        validate_owner_lease(record)
    except OwnerLeaseCorrupt:
        return "corrupt"
    if record.get("state") != "live":
        return "unowned"

    deadline = _deadline_of(record)
    if deadline is not None and now < deadline:
        return "live_owner"

    # Past the deadline. Death is still not assumed -- it must be PROVED, on
    # this host, against this pid's own start time.
    if record.get("host_id") != host_id():
        return "stale_unproven"
    if record.get("pid_start_source") != "ps_lstart" \
            or not record.get("pid_start_at"):
        return "stale_unproven"
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "stale_unproven"
    if not probe:
        return "stale_unproven"

    status, started = _probe_pid_start(pid)
    if status == "absent":
        return "stale_dead_owner"
    if status == "started":
        if started == record.get("pid_start_at"):
            # Expired on the clock, but the very same process is demonstrably
            # still running: the OR-clause of `live_owner`.
            return "live_owner"
        # The pid exists but is a DIFFERENT process -- pid reuse. The original
        # owner is gone, and this innocent process must never be signalled.
        return "stale_dead_owner"
    return "stale_unproven"


def classify_owner_lease(session_uuid, now=None, probe=True):
    """Classify a session's owner lease into exactly one of `OWNER_VERDICTS`.

    Decided from the durable record (plus any MATCHING terminal sidecar, rule
    W4) and, only when `host_id` matches, the local process table. It never
    writes, never signals, and never reclaims -- reclamation is always an
    explicit `take_over`.

    The record is read UNDER the lease's own lock, through a `mutate` closure
    that returns None and therefore writes nothing: rule W1 admits no other
    access to that path, and reading fresh under the lock is also what keeps a
    verdict from being decided against a snapshot a concurrent writer has
    already superseded.

    `probe=False` suppresses the process-table probe; an expired lease then
    classifies `stale_unproven`, because without a probe death is precisely
    what cannot be proved.

    Every uncertainty resolves toward REFUSING: an unreadable record, a lock
    that could not be taken, and an I/O failure all classify `corrupt`, which
    refuses, rather than `unowned`, which would acquire."""
    moment = _resolve_now(now)
    try:
        record = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid),
            lambda existing: None)
    except (state_store.CorruptRecordError, TimeoutError, OSError):
        return "corrupt"
    if record is None:
        return "unowned"
    folded, _mark = _fold_terminal_mark(session_uuid, record)
    return _classify_record(folded, moment, probe=probe)


def select_takeover_mode(session_uuid, now=None):
    """The mode `--take-over` selects for this session, or None to refuse.

      `stale_dead_owner`            -> "proved_dead"
      `live_owner`, same host_id    -> "terminate_prior"
      `live_owner`, foreign host    -> None  (refuse: "foreign_host")
      `stale_unproven` | `corrupt`  -> None  (refuse)
      `unowned`                     -> None  (nothing to take over)

    It NEVER falls back from one mode to the other. `stale_unproven` refuses
    both, and that is the point: `proved_dead` has no proof, and
    `terminate_prior` cannot verify the identity of the pid it would signal."""
    verdict = classify_owner_lease(session_uuid, now=now)
    if verdict == "stale_dead_owner":
        return "proved_dead"
    if verdict != "live_owner":
        return None
    try:
        record = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid),
            lambda existing: None)
    except (state_store.CorruptRecordError, TimeoutError, OSError):
        return None
    if not isinstance(record, dict) or record.get("host_id") != host_id():
        return None
    return "terminate_prior"


# --------------------------------------------------------------------------- #
# The five locked transitions (rules W1/W2/W5).                                #
# --------------------------------------------------------------------------- #


def _cas_mutate(owner_id, epoch, apply_fn, applied):
    """Build the `mutate` closure that makes a transition a genuine
    compare-and-swap (rule W2).

    It returns None -- `_locked_json_transaction`'s documented "no write, and
    the caller is handed the value captured under this same lock" outcome --
    whenever the durable record is absent, names a different `(owner_id,
    epoch)`, or is no longer `live`. Both the decision and the not-writing
    happen inside the held lock, so there is no read-then-compare window: a
    predecessor cannot terminally overwrite a successor's lease, and a
    straggling heartbeat cannot resurrect a released or terminal one.

    `applied` is a one-element list the closure sets to True only when it
    actually produced a successor record, so the caller can distinguish "I
    wrote" from "the CAS refused and returned the current value"."""
    def mutate(existing):
        if existing is None:
            return None
        if (existing.get("owner_id") != owner_id
                or existing.get("epoch") != epoch):
            return None
        if existing.get("state") != "live":
            return None
        successor = apply_fn(existing)
        applied[0] = True
        return successor
    return mutate


def read_owner_lease(session_uuid):
    """Read and validate a session's lease record, or None when no lease has
    ever been written.

    Raises `OwnerLeaseCorrupt` when the record exists but is unreadable,
    malformed, or of an unknown schema version -- "written, then damaged" is
    never allowed to collapse into "genuinely absent", which is the
    distinction `state_store.CorruptRecordError` exists to preserve. Read
    under the lease lock, per rule W1."""
    try:
        record = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid),
            lambda existing: None)
    except state_store.CorruptRecordError as exc:
        raise OwnerLeaseCorrupt(session_uuid, str(exc)) from exc
    except (TimeoutError, OSError) as exc:
        raise OwnerLeaseCorrupt(session_uuid, str(exc)) from exc
    if record is None:
        return None
    return validate_owner_lease(record)


def acquire_owner_lease(session_uuid, claimant, now=None):
    """Acquire the single-writer lease for `session_uuid`, or refuse.

    Acquires ONLY when `classify_owner_lease(...)` is `unowned` -- which
    includes a lease whose MATCHING terminal sidecar has been folded under the
    lock. Returns the durable live record (`state="live"`, the claimant's fresh
    `owner_id`, `epoch = prior.epoch + 1` or 1), fsynced (bytes and parent
    directory) before this returns.

    Verdict -> outcome, complete and normative:

      "unowned"           -> acquires
      "live_owner"        -> raises OwnerLeaseConflict(session_uuid, "live_owner")
      "stale_dead_owner"  -> raises OwnerLeaseConflict(session_uuid, "stale_dead_owner")
                             -- a crashed owner is NEVER implicitly reclaimed;
                                recovery requires an explicit
                                take_over(..., "proved_dead")
      "stale_unproven"    -> raises OwnerLeaseConflict(session_uuid, "stale_unproven")
      "corrupt"           -> raises OwnerLeaseCorrupt(session_uuid)

    Everything -- folding, classifying, deriving the epoch and writing the
    successor -- happens inside ONE `_locked_json_transaction`, so the verdict
    can never be decided against a record another process has already
    replaced. `epoch` is derived only from that in-lock read, which is the
    single structural fact its monotonicity rests on.

    A store-level failure fails LOUD rather than quietly: a lock that could not
    be taken within `state_store._M3_LOCK_TIMEOUT_SECONDS` raises
    `TimeoutError` and a failed write raises `OSError`, both untranslated,
    because an acquisition that did not happen must never look like one that
    did."""
    moment = _resolve_now(now)

    def mutate(existing):
        folded, mark = _fold_terminal_mark(session_uuid, existing)
        verdict = _classify_record(folded, moment, probe=True)
        if verdict == "corrupt":
            raise OwnerLeaseCorrupt(session_uuid)
        if verdict != "unowned":
            raise OwnerLeaseConflict(session_uuid, verdict)
        if mark is not None:
            append_owner_history(session_uuid, {
                "event": "terminal_mark_folded",
                "owner_id": mark.get("owner_id"),
                "epoch": mark.get("epoch"),
                "terminal_reason": mark.get("terminal_reason"),
                "owner_matched": mark.get("owner_matched"),
            })
        prior_epoch = folded.get("epoch") if isinstance(folded, dict) else None
        epoch = (prior_epoch + 1) if isinstance(prior_epoch, int) else 1
        return _build_lease(claimant, epoch, moment)

    try:
        result = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid), mutate)
    except state_store.CorruptRecordError as exc:
        raise OwnerLeaseCorrupt(session_uuid, str(exc)) from exc
    _sweep_terminal_marks(session_uuid, result["owner_id"])
    append_owner_history(session_uuid, {
        "event": "acquired",
        "owner_id": result["owner_id"],
        "epoch": result["epoch"],
        "pid": result.get("pid"),
        "host_id": result.get("host_id"),
        "entry_point": result.get("entry_point"),
    })
    return result


def renew_owner_lease(session_uuid, owner_id, epoch, now=None):
    """Refresh this owner's heartbeat, or return None having written nothing.

    Best-effort and safe to call from a daemon thread: it is a compare-and-swap
    (rule W2), so it writes NOTHING and returns None when the durable record
    does not name this `(owner_id, epoch)` or is no longer `live`. It can
    therefore never resurrect a released or terminal lease, which is exactly
    what makes a straggler renew landing after a release harmless.

    It never raises on a lost lease, and it never raises on a store failure
    either -- a heartbeat must not be able to fail a turn. Only `lease.json`
    is written, under the lock."""
    moment = _resolve_now(now)
    applied = [False]

    def apply_fn(existing):
        record = dict(existing)
        record["heartbeat_at"] = _iso(moment)
        interval = record.get("heartbeat_interval_s") \
            or DEFAULT_HEARTBEAT_INTERVAL_S
        grace = record.get("lease_grace_s") or DEFAULT_LEASE_GRACE_S
        record["lease_deadline_at"] = _iso(
            moment + datetime.timedelta(seconds=interval + grace))
        return record

    try:
        result = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid),
            _cas_mutate(owner_id, epoch, apply_fn, applied))
    except (state_store.CorruptRecordError, TimeoutError, OSError, ValueError):
        return None
    return result if applied[0] else None


def release_owner_lease(session_uuid, owner_id, epoch, reason, now=None):
    """Release this owner's lease, idempotently.

    A compare-and-swap (rule W2): it writes nothing when the durable record
    names a different owner or is already released or terminal, so it is safe
    to call twice and safe to call after a takeover -- a dying predecessor can
    never mark a successor's live lease released.

    Called from the release `finally` that wraps the entire owned region, so a
    store-level failure here is caught and reported as "not released" rather
    than raised: displacing a run's real exit code with a teardown error is the
    one thing this call must not do. Returns None, always."""
    moment = _resolve_now(now)
    applied = [False]

    def apply_fn(existing):
        record = dict(existing)
        record["state"] = "released"
        record["released_at"] = _iso(moment)
        record["terminal_reason"] = reason
        return record

    try:
        state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid),
            _cas_mutate(owner_id, epoch, apply_fn, applied))
    except (state_store.CorruptRecordError, TimeoutError, OSError, ValueError):
        return None
    if applied[0]:
        append_owner_history(session_uuid, {
            "event": "released",
            "owner_id": owner_id,
            "epoch": epoch,
            "reason": reason,
        })
    return None


def _await_pid_gone(pid, expected_start_at, grace_s):
    """Poll until `pid` is provably no longer the process the lease named, or
    the grace expires. True means proved gone -- absent, or present with a
    DIFFERENT start time, which is a recycled pid and therefore equally proof
    that the original process is dead. "unknown" is never proof and never
    returns True."""
    deadline = time.time() + grace_s
    while True:
        status, started = _probe_pid_start(pid)
        if status == "absent":
            return True
        if status == "started" and started != expected_start_at:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(_TAKEOVER_POLL_S)


def _terminate_prior_owner(record):
    """TERM-then-KILL the prior owner named by `record`, and report whether it
    is provably gone. Mirrors `cowork_verification.terminate_worker`'s
    escalation shape and budget.

    Step zero is the whole PID-reuse defence and is not skippable: the pid's
    CURRENT start time must equal the lease's EXACTLY, or nothing is signalled
    at all. An unrelated process that merely inherited the pid is never sent a
    signal, and the takeover aborts instead."""
    pid = record.get("pid")
    expected = record.get("pid_start_at")
    status, started = _probe_pid_start(pid)
    if status == "absent":
        return True
    if status != "started" or started != expected:
        # Either the pid could not be identified, or it is a different process
        # than the one that took the lease. Refuse to signal it.
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    if _await_pid_gone(pid, expected, TAKEOVER_TERM_GRACE_S):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    return _await_pid_gone(pid, expected, TAKEOVER_KILL_GRACE_S)


def take_over(session_uuid, claimant, mode, now=None):
    """Take a session over EXPLICITLY. Never implicit, never a fallback.

    `mode` is one of `"proved_dead"` or `"terminate_prior"`, and the verdict is
    re-evaluated INSIDE the lock: `proved_dead` requires `stale_dead_owner`,
    `terminate_prior` requires a same-host `live_owner`. A mode that does not
    match the in-lock verdict refuses and writes nothing, so an operator's
    intent can never be quietly upgraded into the other mode.

    `terminate_prior` is ordered and re-verified at every step: re-read the
    lease under the lock, re-read the prior pid's start time and refuse unless
    it matches EXACTLY, SIGTERM and poll `TAKEOVER_TERM_GRACE_S`, SIGKILL and
    poll `TAKEOVER_KILL_GRACE_S`, re-verify death, and only then write. If the
    prior owner is still provably alive, the takeover ABORTS and the lease is
    left byte-identical.

    The successor carries `epoch + 1`, a fresh `owner_id` and a `predecessor`
    block recording the evidence of death. Takeover touches `owner/lease.json`,
    appends to `owner/history.jsonl`, and sweeps non-matching sidecars -- and
    NOTHING else: pending turns, candidate bindings, work units, manifests,
    pause leases, phase state, the evaluation queue and `provider-bindings/`
    records are all left untouched, so a successor inherits the session it took
    over rather than a reset one."""
    if mode not in _TAKEOVER_MODES:
        raise ValueError("mode %r is not one of %s"
                         % (mode, sorted(_TAKEOVER_MODES)))
    moment = _resolve_now(now)

    def mutate(existing):
        folded, mark = _fold_terminal_mark(session_uuid, existing)
        verdict = _classify_record(folded, moment, probe=True)
        if verdict == "corrupt":
            raise OwnerLeaseCorrupt(session_uuid)
        if mode == "proved_dead":
            if verdict != "stale_dead_owner":
                raise OwnerLeaseConflict(session_uuid, verdict)
        else:
            if verdict != "live_owner":
                raise OwnerLeaseConflict(session_uuid, verdict)
            if folded.get("host_id") != host_id():
                raise OwnerLeaseConflict(session_uuid, "foreign_host")
            if not _terminate_prior_owner(folded):
                # Step 5: still provably alive (or unidentifiable). Abort --
                # the lease stays byte-identical.
                raise OwnerLeaseConflict(session_uuid, "live_owner")
        if mark is not None:
            append_owner_history(session_uuid, {
                "event": "terminal_mark_folded",
                "owner_id": mark.get("owner_id"),
                "epoch": mark.get("epoch"),
                "terminal_reason": mark.get("terminal_reason"),
                "owner_matched": mark.get("owner_matched"),
            })
        status, started = _probe_pid_start(folded.get("pid"))
        predecessor = {
            "owner_id": folded.get("owner_id"),
            "epoch": folded.get("epoch"),
            "pid": folded.get("pid"),
            "pid_start_at": folded.get("pid_start_at"),
            "evidence": ("pid_absent" if status == "absent"
                         else "pid_start_mismatch"),
            "observed_at": _iso(moment),
        }
        return _build_lease(claimant, (folded.get("epoch") or 0) + 1, moment,
                            predecessor=predecessor)

    try:
        result = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid), mutate)
    except state_store.CorruptRecordError as exc:
        raise OwnerLeaseCorrupt(session_uuid, str(exc)) from exc
    _sweep_terminal_marks(session_uuid, result["owner_id"])
    append_owner_history(session_uuid, {
        "event": "taken_over",
        "mode": mode,
        "owner_id": result["owner_id"],
        "epoch": result["epoch"],
        "predecessor": result.get("predecessor"),
    })
    return result


def assert_owner(session_uuid, owner_id, epoch):
    """The fencing check every governed write must pass. Returns None, or
    raises.

    `OwnerLeaseLost` when the durable record no longer names this
    `(owner_id, epoch)` as `live` -- taken over, released, or never written.
    `OwnerLeaseCorrupt` when the record could not be read at all: an
    unverifiable fence refuses, because proceeding on an unread record is
    exactly the multi-writer case the lease exists to prevent. `__cause__`
    names the real failure in that case, so "the disk failed" is never
    reported as "you lost the lease"."""
    record = read_owner_lease(session_uuid)
    if record is None:
        raise OwnerLeaseLost(session_uuid, owner_id, epoch, "no_lease")
    if record.get("state") != "live":
        raise OwnerLeaseLost(session_uuid, owner_id, epoch, record.get("state"))
    if record.get("owner_id") != owner_id or record.get("epoch") != epoch:
        raise OwnerLeaseLost(session_uuid, owner_id, epoch, "taken_over")
    return None


# --------------------------------------------------------------------------- #
# Read-only status projection.                                                 #
# --------------------------------------------------------------------------- #


def owner_status_view(session_uuid, now=None):
    """A read-only projection of a session's ownership, for UI, `--report` and
    `--session-owner`.

    It REPORTS, it does not gate: it acquires nothing, writes nothing, and --
    unlike every other entry point here -- never raises, because a status
    surface that fails is worse than one that says "corrupt". Every field a
    caller might otherwise re-derive (the verdict, the recommended takeover
    mode, heartbeat age, whether a sidecar matches) is computed here once.

    It never raises for anything it finds on disk -- a missing, damaged,
    unreadable or foreign lease all come back as a `verdict` field. (An
    invalid ARGUMENT, such as an unsafe `session_uuid` or a naive `now`, is a
    caller error and still raises `ValueError`.)"""
    moment = _resolve_now(now)
    view = {
        "schema_version": OWNER_LEASE_SCHEMA_VERSION,
        "record": "OwnerStatusView",
        "session_uuid": session_uuid,
        "observed_at": _iso(moment),
        "verdict": "corrupt",
        "lease": None,
        "terminal_mark": None,
        "terminal_mark_matches": False,
        "heartbeat_age_s": None,
        "lease_deadline_at": None,
        "expired": None,
        "host_matches": None,
        "takeover_mode": None,
    }
    try:
        record = read_owner_lease(session_uuid)
    except OwnerLeaseCorrupt as exc:
        view["detail"] = str(exc)
        return view
    if record is None:
        view["verdict"] = "unowned"
        return view

    folded, mark = _fold_terminal_mark(session_uuid, record)
    view["lease"] = record
    view["terminal_mark"] = mark
    view["terminal_mark_matches"] = mark is not None
    view["verdict"] = _classify_record(folded, moment, probe=True)
    view["host_matches"] = record.get("host_id") == host_id()
    heartbeat = _parse_instant(record.get("heartbeat_at"))
    if heartbeat is not None:
        view["heartbeat_age_s"] = (moment - heartbeat).total_seconds()
    deadline = _deadline_of(record)
    if deadline is not None:
        view["lease_deadline_at"] = _iso(deadline)
        view["expired"] = moment >= deadline
    if view["verdict"] == "stale_dead_owner":
        view["takeover_mode"] = "proved_dead"
    elif view["verdict"] == "live_owner" and view["host_matches"]:
        view["takeover_mode"] = "terminate_prior"
    return view


# --------------------------------------------------------------------------- #
# Global provider/controller session exclusivity (plan §3.6).                  #
#                                                                              #
# Keyed `(controller, provider_session_id) -> owner_session_uuid`, GLOBAL and  #
# outside any one session's directory, so it spans launch directories and      #
# session anchors -- a `.cowork/`-local index would not.                       #
#                                                                              #
# EXCLUSIVITY LIFETIME, one rule: a binding blocks a DIFFERENT session only    #
# while the session that holds it has a LIVE owner lease. Three consequences,  #
# and they are mutually consistent: the same session re-binds idempotently     #
# (including after a takeover changed `owner_id` -- exclusivity is per SESSION,#
# not per owner, so a successor keeps its predecessor's provider              #
# conversations); a SIGTERMed owner's orphan records self-heal rather than     #
# blocking forever; and clean-exit deletion is a bounded optimisation, never   #
# the mechanism correctness rests on.                                          #
#                                                                              #
# All three functions below are TOTAL over their failure modes: every          #
# TimeoutError, OSError, CorruptRecordError and ValueError is re-raised as     #
# `ProviderBindingUnavailable` FROM the original, at that function's own       #
# boundary, so their published exception set is closed.                        #
# --------------------------------------------------------------------------- #


def read_provider_binding(controller, provider_session_id):
    """Read the binding record for one `(controller, provider_session_id)`, or
    None.

    The reader the pre-dispatch checks use: it NEVER raises
    `ProviderSessionConflict`, and it returns None ONLY for a genuinely absent
    record -- never for one that exists but cannot be read, which raises
    `ProviderBindingUnavailable` instead. Collapsing "unreadable" into
    "absent" here would silently authorize the very resume the index exists to
    refuse."""
    try:
        return state_store._read_json_or_raise_if_corrupt(
            state_store.provider_session_binding_path_for(
                controller, provider_session_id))
    except (TimeoutError, OSError, state_store.CorruptRecordError,
            ValueError) as exc:
        raise ProviderBindingUnavailable(
            controller, provider_session_id, detail=str(exc)) from exc


def bind_provider_session(controller, provider_session_id, owner, role):
    """Bind one provider conversation to the acquired session, and return the
    durable record.

    Raises `ProviderSessionConflict` -- and this is the ONLY function that
    does -- iff an existing record names a DIFFERENT `owner_session_uuid` AND
    that session currently holds a `state == "live"` owner lease, evaluated
    inside this binding transaction. Otherwise the requester overwrites the
    record: the same session re-binds idempotently, including after a takeover
    changed `owner_id`, and a record whose owning session is no longer live is
    adoptable, which is what makes an abandoned binding self-healing instead of
    a permanent block.

    Every store-level failure -- a lock timeout, a write/fsync failure, a
    corrupt record, a rejected identifier -- is re-raised as
    `ProviderBindingUnavailable` from the original. A conflict and an
    unverifiable index are deliberately different exceptions carrying different
    obligations: the first is proof of a collision, the second proves nothing
    at all."""
    session_uuid = owner["session_uuid"]
    owner_id = owner["owner_id"]

    def mutate(existing):
        if isinstance(existing, dict):
            bound_to = existing.get("owner_session_uuid")
            if bound_to and bound_to != session_uuid:
                if classify_owner_lease(bound_to) == "live_owner":
                    raise ProviderSessionConflict(
                        controller, provider_session_id, bound_to, role)
        return {
            "schema_version": OWNER_LEASE_SCHEMA_VERSION,
            "record": "ProviderSessionBinding",
            "controller": controller,
            "provider_session_id": provider_session_id,
            "owner_session_uuid": session_uuid,
            "role": role,
            "bound_at": _iso(_utc_now()),
            "bound_by_owner_id": owner_id,
        }

    try:
        record = state_store._locked_json_transaction(
            state_store.provider_session_binding_path_for(
                controller, provider_session_id), mutate)
    except (TimeoutError, OSError, state_store.CorruptRecordError,
            ValueError) as exc:
        raise ProviderBindingUnavailable(
            controller, provider_session_id, role, str(exc)) from exc
    _BIND_LOG.setdefault((session_uuid, owner_id), []).append(
        (controller, provider_session_id))
    return record


def release_provider_bindings(session_uuid, owner_id):
    """Delete the binding records THIS owner bound for THIS session, and
    return how many were removed.

    SUCCESSOR GUARD FIRST: it reads `owner/lease.json` under the lock, and if
    that record is `live` under a DIFFERENT `owner_id`, a successor has taken
    the session over -- it then deletes NOTHING and returns 0. A dying
    predecessor must not be able to delete the bindings a successor inherited
    but has not yet re-bound.

    Otherwise it deletes only records for which BOTH
    `owner_session_uuid == session_uuid` AND `bound_by_owner_id == owner_id`.
    Idempotent -- an absent record is not an error -- and BOUNDED: it iterates
    only the keys this process itself bound, never the whole global directory.

    Called from the release `finally`, and NOT from the SIGTERM path (it takes
    locks, which the signal path forbids). Its translated
    `ProviderBindingUnavailable` is meant to be caught and traced at that call
    site: teardown must never displace a run's real exit code."""
    try:
        lease = state_store._locked_json_transaction(
            state_store.owner_lease_path_for(session_uuid),
            lambda existing: None)
    except (TimeoutError, OSError, state_store.CorruptRecordError,
            ValueError) as exc:
        raise ProviderBindingUnavailable(
            None, None, detail=str(exc)) from exc
    if (isinstance(lease, dict) and lease.get("state") == "live"
            and lease.get("owner_id") != owner_id):
        return 0

    deleted = 0
    for controller, provider_session_id in list(
            _BIND_LOG.get((session_uuid, owner_id), ())):
        try:
            path = state_store.provider_session_binding_path_for(
                controller, provider_session_id)
            record = state_store._read_json_or_raise_if_corrupt(path)
            if not isinstance(record, dict):
                continue
            if (record.get("owner_session_uuid") != session_uuid
                    or record.get("bound_by_owner_id") != owner_id):
                continue
            os.remove(path)
            deleted += 1
        except (TimeoutError, OSError, state_store.CorruptRecordError,
                ValueError) as exc:
            if isinstance(exc, FileNotFoundError):
                # An absent record is not an error -- this is idempotent, and
                # a record another process already removed is exactly the
                # outcome being asked for.
                continue
            raise ProviderBindingUnavailable(
                controller, provider_session_id, detail=str(exc)) from exc
    _BIND_LOG.pop((session_uuid, owner_id), None)
    return deleted
