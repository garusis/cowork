#!/usr/bin/env python3
"""cowork session controller policy: the single fail-closed dispatch decision.

A cowork session may declare which controllers it is ALLOWED to use. The
declaration is persisted in the session file (`controller_policy`, see
`cowork_state`); this module owns the in-process decision built from it.

Two ideas carry the whole feature:

1. **Decide once, enforce twice.** `guard()` is the ONLY place that decides
   whether a controller may start. It is called at the cowork.py launch sites
   (so a block reads as a clear user-facing message) and again as the first
   statement of every cowork_bridge entry point that creates a process or writes
   provider-specific setup (so a forgotten call site cannot become a hole).

2. **Three-way proposals, never two-way.** A controller update names its policy
   component as one of three DISTINCT things: `PRESERVE` (no policy supplied —
   the saved policy is left byte-for-byte untouched), `ALL` (an explicit reset —
   the restriction is removed), or a normalized tuple (an explicit restricted
   set). `PRESERVE` and `ALL` are sentinels and neither is `None`, so "not
   supplied" can never be silently read as "unrestricted" — which is what stops
   a lone role switch from deleting the restriction it is being checked against.

The active policy is process-global module state, activated once per run right
after the session state is loaded and re-activated after an accepted transition.
One cowork process serves exactly one session, so this is semantically correct,
and it makes every spawn guarded by construction rather than by remembering to
thread a parameter through five run_* functions.

Python 3.9+, stdlib only. This module imports nothing from cowork.py or
cowork_bridge.py, so it can be a leaf dependency of both.
"""

import collections
import contextlib

# Canonical controller set. `cowork.CONTROLLERS` re-exports this name so there
# is exactly one definition.
CONTROLLERS = ("claude", "codex", "opencode")

# Kinds of guarded action, recorded on the blocked-dispatch trace event.
KINDS = ("dispatch", "probe", "setup")


class _Sentinel:
    """A named, non-None singleton. `PRESERVE is not ALL is not None` is the
    whole point: the three proposal states must never collapse into two."""

    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name

    def __bool__(self):
        # Deliberately truthy: `if proposal.policy:` must not read PRESERVE as
        # "nothing supplied".
        return True


#: No policy component supplied — leave the saved `controller_policy` untouched.
PRESERVE = _Sentinel("PRESERVE")
#: An explicit reset — remove the restriction entirely (`--allow-controllers all`).
ALL = _Sentinel("ALL")


#: One controller update. `policy` is PRESERVE, ALL, or a normalized tuple;
#: `mappings` is a sequence of (role, controller) pairs; `source` is one of
#: cli / interactive / gate.
ControllerProposal = collections.namedtuple(
    "ControllerProposal", ["policy", "mappings", "source"])
ControllerProposal.__new__.__defaults__ = (PRESERVE, (), None)


def normalize(allowed):
    """Return `allowed` as a de-duplicated tuple in CONTROLLERS order.

    Raises ValueError on an empty result or an unknown controller name — an
    empty allowed set is never a valid policy (`all` is how a restriction is
    removed)."""
    if allowed is None:
        raise ValueError("an allowed controller set cannot be empty; use "
                         "'all' to remove the restriction.")
    seen = set()
    for item in allowed:
        name = str(item).strip().lower()
        if name not in CONTROLLERS:
            raise ValueError(
                "unknown controller %r (expected one of: %s)"
                % (item, ", ".join(CONTROLLERS)))
        seen.add(name)
    if not seen:
        raise ValueError("an allowed controller set cannot be empty; use "
                         "'all' to remove the restriction.")
    return tuple(c for c in CONTROLLERS if c in seen)


def parse_allowed(value):
    """Parse an `--allow-controllers` value into ALL or a normalized tuple.

    `'all'` (any case, on its own) returns the ALL sentinel — the explicit reset.
    Anything else is a comma-separated controller list. Raises ValueError with a
    message naming the valid controllers on an empty list, an unknown name, or
    `all` mixed with other names."""
    tokens = [t.strip().lower() for t in str(value).split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ValueError(
            "--allow-controllers needs at least one controller (%s), or 'all' "
            "to remove the restriction." % ", ".join(CONTROLLERS))
    if "all" in tokens:
        if len(tokens) > 1:
            raise ValueError(
                "--allow-controllers 'all' removes the restriction and cannot "
                "be combined with other controllers.")
        return ALL
    return normalize(tokens)


def effective_allowed(proposal_policy, saved_allowed):
    """The allowed set that a proposal will actually be judged against.

    PRESERVE -> the currently saved set (so a mapping-only update is still
    checked against the restriction it must not rewrite); ALL -> None
    (unrestricted); a tuple -> that tuple.

    Every validation, probe-window activation and post-write activation goes
    through this one function, so the three cannot drift apart."""
    if proposal_policy is PRESERVE:
        return tuple(saved_allowed) if saved_allowed else None
    if proposal_policy is ALL:
        return None
    return normalize(proposal_policy)


def is_allowed(allowed, controller):
    """Whether `controller` may run under `allowed` (None = unrestricted)."""
    if allowed is None:
        return True
    return controller in tuple(allowed)


def eligible(allowed, current):
    """The controllers a role on `current` may be switched TO, in CONTROLLERS
    order. Unrestricted (`allowed=None`) yields every controller but `current`;
    an empty allowed set yields an empty list."""
    pool = CONTROLLERS if allowed is None else tuple(allowed)
    return [c for c in CONTROLLERS if c != current and c in pool]


def format_allowed(allowed):
    """Human rendering of an allowed set for messages and gate text."""
    if allowed is None:
        return "any controller"
    names = [c for c in CONTROLLERS if c in tuple(allowed)]
    return ", ".join(names) if names else "none"


class DispatchBlocked(RuntimeError):
    """Raised by `guard` BEFORE any process is created or any provider-specific
    file is written, when the session's policy forbids the controller."""

    def __init__(self, controller, role=None, phase=None, allowed=None,
                 kind="dispatch", invalid=False):
        self.controller = controller
        self.role = role
        self.phase = phase
        self.allowed = allowed
        self.kind = kind
        self.invalid = bool(invalid)
        super().__init__(self._message())

    def _message(self):
        if self.invalid:
            return (
                "cowork: this session's saved controller policy is unreadable, "
                "so no controller can start (%s requested %s). Re-run with "
                "--allow-controllers to replace it."
                % (self.role or "a role", self.controller))
        allowed = format_allowed(self.allowed)
        if self.role:
            return (
                "cowork: %s is configured for %s, which this session does not "
                "allow (allowed: %s)." % (self.role, self.controller, allowed))
        return (
            "cowork: %s is not allowed in this session (allowed: %s)."
            % (self.controller, allowed))

    def __str__(self):
        return self._message()


class ChildDispatchBlocked(RuntimeError):
    """User-facing form of a pre-child fail-closed policy decision."""

    def __init__(self, decision):
        self.decision = dict(decision or {})
        super().__init__(
            "cowork: child dispatch blocked before launch (%s)"
            % (self.decision.get("reason") or "unknown"))


# --------------------------------------------------------------------------- #
# The active-policy holder.                                                    #
#                                                                              #
# Process-global by design (see the module docstring). `mode` is one of:        #
#   'unrestricted' — no policy in force (the default, and what every session    #
#                    saved before this feature loads as);                       #
#   'allowed'      — a restricted set is in force;                              #
#   'invalid'      — the saved policy is unreadable, so EVERY controller is     #
#                    blocked (fail closed, never fail open).                    #
# --------------------------------------------------------------------------- #

_ACTIVE = {"mode": "unrestricted", "allowed": None, "raw": None,
           "trace": None, "phase": None}


def activate(allowed, trace=None, phase=None):
    """Put `allowed` in force (None = unrestricted). Returns the allowed set."""
    _ACTIVE["mode"] = "unrestricted" if allowed is None else "allowed"
    _ACTIVE["allowed"] = None if allowed is None else normalize(allowed)
    _ACTIVE["raw"] = None
    _ACTIVE["trace"] = trace
    _ACTIVE["phase"] = phase
    return _ACTIVE["allowed"]


def activate_invalid(raw, trace=None, phase=None):
    """Enter the fail-closed state for an unreadable saved policy: while it is
    active `guard` blocks EVERY controller."""
    _ACTIVE["mode"] = "invalid"
    _ACTIVE["allowed"] = ()
    _ACTIVE["raw"] = raw
    _ACTIVE["trace"] = trace
    _ACTIVE["phase"] = phase


def deactivate():
    """Clear the active policy back to unrestricted. Tests call this in
    setUp/addCleanup so no policy leaks between them."""
    _ACTIVE.update({"mode": "unrestricted", "allowed": None, "raw": None,
                    "trace": None, "phase": None})


def active_allowed():
    """The allowed tuple in force, or None when unrestricted. An INVALID policy
    reports an empty tuple (nothing is allowed)."""
    return _ACTIVE["allowed"]


def active_meta():
    """A copy of the holder: {mode, allowed, raw, phase}. `trace` is omitted."""
    return {"mode": _ACTIVE["mode"], "allowed": _ACTIVE["allowed"],
            "raw": _ACTIVE["raw"], "phase": _ACTIVE["phase"]}


def is_active():
    return _ACTIVE["mode"] != "unrestricted"


@contextlib.contextmanager
def restricted(allowed, trace=None, phase=None):
    """Temporarily put `allowed` in force, restoring the previous holder on
    exit. Used by the probe window and by tests."""
    prior = dict(_ACTIVE)
    try:
        activate(allowed, trace=trace, phase=phase)
        yield active_allowed()
    finally:
        _ACTIVE.update(prior)


def guard(controller, role=None, kind="dispatch", phase=None, trace=None):
    """The single policy decision. Returns None when the controller may run;
    otherwise traces `controller.dispatch.blocked` and raises DispatchBlocked
    BEFORE any process is created.

    Called both at the cowork.py launch sites (for the clear message) and as the
    first statement of every cowork_bridge entry point (the guarantee)."""
    mode = _ACTIVE["mode"]
    if mode == "unrestricted":
        return None
    invalid = mode == "invalid"
    allowed = _ACTIVE["allowed"]
    if not invalid and is_allowed(allowed, controller):
        return None
    tracer = trace if trace is not None else _ACTIVE["trace"]
    if tracer is not None:
        try:
            tracer.event("controller.dispatch.blocked", role=role,
                         phase=phase if phase is not None else _ACTIVE["phase"],
                         requested_controller=controller,
                         allowed=("<invalid>" if invalid
                                  else list(allowed or ())),
                         kind=kind)
        except Exception:  # noqa: BLE001 - tracing must never break a block
            pass
    raise DispatchBlocked(controller, role=role,
                          phase=phase if phase is not None else _ACTIVE["phase"],
                          allowed=allowed, kind=kind, invalid=invalid)


def guard_child(requested_child, parent_effective, pin_capability=True):
    """Return the child decision; the broker owns ids and durable emission."""
    from cowork_action_policy import decide_child
    allowed = active_allowed()
    if _ACTIVE["mode"] == "invalid":
        allowed = ()
    elif allowed is None:
        allowed = (parent_effective or {}).get("controller"),
    return decide_child(requested_child, parent_effective, allowed,
                        pin_capability=pin_capability)
