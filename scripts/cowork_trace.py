#!/usr/bin/env python3
"""Private cowork orchestration trace.

The trace complements Claude/Codex controller logs. It records cowork's own
decisions and controller invocation metadata, but never raw prompts, replies, or
terminal transcript text. UI render diagnostics are limited to metadata such as
renderer mode, terminal dimensions, byte/line counts, and status counters.
"""

import contextlib
import datetime
import hashlib
import json
import os
import re
import time
import uuid


# Work classes (P1). Exclusive: every unit of controller cost belongs to exactly
# one of these. `in_flight`, `failed` and `cancelled` are terminal states of a
# turn rather than purposes, and are assigned by the record builder from the
# turn's observed end path; the purpose classes below are stamped at start.
WORK_CLASSES = (
    "productive", "review", "evaluation", "verification", "recovery",
    "probe", "in_flight", "failed", "cancelled",
)

# Usage scopes (P1/C1): how a turn's `usage` block relates to the provider's
# own counters. `turn_native` — the provider reports per turn (Claude).
# `turn_delta` — cowork derived this turn's share from a cumulative counter
# (Codex). `incomparable` — the cumulative reading moved backwards, so no
# honest per-turn figure exists and `usage` is absent.
USAGE_SCOPES = ("turn_native", "turn_delta", "incomparable", "unknown")

MODEL_SOURCES = ("live_event", "config_pinned", "unknown")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def new_work_id():
    """Mint the id joining a unit of work's start event to its end event."""
    return str(uuid.uuid4())


def strip_ansi(value):
    """Remove ANSI escape sequences from a string (CV-002: a model name read
    off a styled terminal line can carry a `[1m` fragment; it must never reach
    the record)."""
    if not isinstance(value, str):
        return value
    return _ANSI_RE.sub("", value).replace("\x1b", "").strip()


def work_meta(work_id, work_class, usage_scope=None, identity=None,
              duration_ms=None):
    """Build the stamped work-record fields shared by every emission site (P1).

    `duration_ms` is required on end events (P14) — an end event that omits it
    leaves the turn's cost unattributable in time, so every end path computes
    elapsed BEFORE emitting. A turn with a start and no end is `in_flight` and
    the record reports its duration as `unknown`, never 0.
    """
    meta = {"work_id": work_id, "work_class": work_class}
    if usage_scope is not None:
        meta["usage_scope"] = usage_scope
    if identity is not None:
        meta["identity"] = identity
    if duration_ms is not None:
        meta["duration_ms"] = duration_ms
    return meta


def identity_meta(controller=None, provider=None, model=None,
                  model_source=None, controller_session_id=None):
    """The canonical identity block stamped on a unit of work.

    `model_source` says how the model was learned: `live_event` (the controller
    named it in its own event stream), `config_pinned` (cowork's configured
    value, used when the controller never names one) or `unknown`. Values are
    ANSI-stripped so a styled fragment can never enter the record (CV-002)."""
    source = model_source if model_source in MODEL_SOURCES else "unknown"
    return {
        "controller": strip_ansi(controller),
        "provider": strip_ansi(provider),
        "model": strip_ansi(model),
        "model_source": source,
        "controller_session_id": strip_ansi(controller_session_id),
    }


# --------------------------------------------------------------------------- #
# Process-global active trace (P15).                                          #
#                                                                             #
# One cowork process serves exactly one session, which is why cowork_policy    #
# already uses and documents this pattern. `user_wait()` needs an emitter at   #
# six blocking call sites whose signatures carry no trace parameter; threading #
# one through all of them and their call sites would be invasive and easy to   #
# miss a site. Tests inject their own ask callables and never call set_active, #
# so a non-interactive call emits nothing and cannot manufacture wait time in  #
# the very figure being validated.                                            #
# --------------------------------------------------------------------------- #

_ACTIVE = {"trace": None, "wait_depth": 0}


def set_active(trace):
    """Install the process-global trace used by `user_wait`."""
    _ACTIVE["trace"] = trace
    _ACTIVE["wait_depth"] = 0
    return trace


def active():
    return _ACTIVE["trace"]


def clear_active():
    _ACTIVE["trace"] = None
    _ACTIVE["wait_depth"] = 0


@contextlib.contextmanager
def user_wait(reason, work_id=None):
    """Time one interactive blocker, emitting a paired user.wait span (P15).

    These spans are the ONLY source of user-wait time. Inference from gaps
    between unrelated events is forbidden: a gap is equally an ingestion stall,
    a controller hang or a suspended process, and is not evidence of a human.

    The span closes on every exit path and stamps an `outcome` — `answered`,
    `cancelled`, `eof` or `drain_failed`. Callers set a non-default outcome by
    assigning to the yielded span's `outcome` field. Nesting is refused: a gate
    invoked inside another gate contributes exactly one span, never two. With
    no active trace the whole thing is a no-op.
    """
    span = _WaitSpan(reason)
    tracer = active()
    if tracer is None or _ACTIVE["wait_depth"] > 0:
        # No emitter, or already inside a span: hand back an inert handle so
        # callers need no branch of their own.
        span.recording = False
        yield span
        return
    span.recording = True
    _ACTIVE["wait_depth"] += 1
    started = time.monotonic()
    work_id = work_id or new_work_id()
    span.work_id = work_id
    try:
        tracer.event("user.wait.start", work_id=work_id, reason=reason)
    except Exception:  # noqa: BLE001 - instrumentation never breaks a prompt
        pass
    try:
        yield span
    except KeyboardInterrupt:
        span.outcome = "cancelled"
        raise
    except BaseException:
        span.outcome = span.outcome or "cancelled"
        raise
    finally:
        _ACTIVE["wait_depth"] -= 1
        try:
            tracer.event("user.wait.end", work_id=work_id, reason=reason,
                         outcome=span.outcome or "answered",
                         duration_ms=int((time.monotonic() - started) * 1000))
        except Exception:  # noqa: BLE001
            pass


class _WaitSpan:
    """Handle yielded by `user_wait`; callers stamp the termination outcome."""

    def __init__(self, reason):
        self.reason = reason
        self.outcome = None
        self.work_id = None
        self.recording = False


def trace_path_for(session_uuid):
    """Path of the per-session trace file. Root is overridable via
    COWORK_SESSIONS_ROOT so tests never write to the real home dir
    (mirrors cowork_state.scores_path_for)."""
    root = (os.environ.get("COWORK_SESSIONS_ROOT")
            or os.path.expanduser(os.path.join("~", ".cowork", "sessions")))
    return os.path.join(root, session_uuid, "trace.jsonl")


def new_run_id():
    return str(uuid.uuid4())


def prompt_meta(text, prefix="prompt"):
    text = text or ""
    raw = text.encode("utf-8")
    return {
        "%s_sha256" % prefix: hashlib.sha256(raw).hexdigest(),
        "%s_bytes" % prefix: len(raw),
    }


def redacted_argv(argv, prompt_text=None):
    """Return argv with any prompt body replaced by '<prompt>'."""
    if argv is None:
        return None
    out = []
    for arg in argv:
        if prompt_text is not None and arg == prompt_text:
            out.append("<prompt>")
        else:
            out.append(arg)
    return out


def command_meta(argv, prompt_text=None):
    data = {"argv": redacted_argv(argv, prompt_text=prompt_text)}
    if prompt_text is not None:
        data.update(prompt_meta(prompt_text))
    return data


class Trace:
    def __init__(self, path, session_uuid=None, run_id=None, enabled=True):
        self.path = path
        self.session_uuid = session_uuid
        self.run_id = run_id or new_run_id()
        self.enabled = bool(enabled and path)

    def event(self, name, **fields):
        """Append one event. Returns its `event_id` (None when disabled) so a
        caller can record it as another event's `triggering_event_id` (P16)."""
        if not self.enabled:
            return None
        # Every event carries its own identity (P16) so a transition's
        # `triggering_event_id` has a referent. Stamped here rather than at each
        # emission site so it applies to all of them at once.
        event_id = str(uuid.uuid4())
        obj = {
            "ts": _now(),
            "event": name,
            "event_id": event_id,
            "run_id": self.run_id,
        }
        if self.session_uuid:
            obj["session_uuid"] = self.session_uuid
        obj.update({k: v for k, v in fields.items() if v is not None})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as fh:
                json.dump(_json_safe(obj), fh, sort_keys=True)
                fh.write("\n")
        except OSError:
            # Debug tracing must never break cowork.
            return event_id
        return event_id


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
