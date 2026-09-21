#!/usr/bin/env python3
"""cowork agent transcript output: role replies, progress notices, activity.

Everything here writes plain text to the invocation's output stream. Output is
identical whether that stream is a pipe, a file, a terminal or a StringIO:
nothing branches on the stream, nothing animates, nothing reads input. The
machine contract of a run is its JSON run-result line and its durable
artifacts; this transcript is the human-readable log beside them.

Python 3.9+.
"""

import os

# --------------------------------------------------------------------------- #
# Labels and paths.                                                           #
# --------------------------------------------------------------------------- #

# The label that precedes the orchestrator-supplied context when it is echoed
# into the transcript.
CONTEXT_LABEL = "context › "


def speaker_label(name):
    """The transcript label for a role reply: 'name › '."""
    return "%s › " % name


def display_path(path):
    """Collapse a leading $HOME prefix to '~'. A path that is exactly home
    becomes '~'; a path not under home is returned unchanged."""
    if not path:
        return path
    home = os.path.expanduser("~")
    if not home or home == "~":
        return path
    if path == home:
        return "~"
    prefix = home + os.sep
    if path.startswith(prefix):
        return "~" + os.sep + path[len(prefix):]
    return path


def render_path(path, cwd=None):
    """The short display form of a path for the transcript: cwd-relative when
    it sits under cwd, else '~/…' when under home, else '…/<basename>'."""
    if not path:
        return path
    cwd = cwd or os.getcwd()
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:  # different drive on Windows, etc.
        rel = None
    if rel is not None and not rel.startswith(".."):
        return rel
    home = display_path(path)
    if home != path:
        return home
    return "…/" + os.path.basename(path)


shorten_path = render_path


# --------------------------------------------------------------------------- #
# Channel markers.                                                            #
#                                                                             #
# A lead role may wrap self-narration in sentinel lines, each ALONE on its    #
# own line: `[[internal]]` opens a block, `[[/internal]]` closes it. Marker   #
# lines are channel control and are never written to the transcript; the     #
# enclosed text is written plain. For marker-free content the transcript is   #
# byte-identical to the reply.                                                #
# --------------------------------------------------------------------------- #

INTERNAL_OPEN = "[[internal]]"
INTERNAL_CLOSE = "[[/internal]]"


def split_channel_segments(text, internal_start=False):
    """Split `text` into ordered (channel, segment_text) runs, channel in
    {'user','internal'}, and return (segments, internal_end).

    A control line is recognized ONLY when a full line's stripped content
    equals exactly `[[internal]]` or `[[/internal]]`. Channel state is
    depth-1: a second open while internal, or a close with no open, is a
    no-op. Marker lines are always stripped. For marker-free text the single
    segment is byte-identical to the input."""
    segments = []
    internal = bool(internal_start)
    channel = "internal" if internal else "user"
    buf = []

    def flush():
        if buf:
            segments.append((channel, "".join(buf)))
            buf.clear()

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == INTERNAL_OPEN:
            if not internal:
                flush()
                internal = True
                channel = "internal"
            continue
        if stripped == INTERNAL_CLOSE:
            if internal:
                flush()
                internal = False
                channel = "user"
            continue
        buf.append(line)
    flush()
    return segments, internal


def write_reply(io_out, text):
    """Write one whole (non-streamed) reply, marker lines stripped."""
    segments, _ = split_channel_segments(text)
    plain = "".join(seg for _channel, seg in segments)
    io_out.write(plain + ("\n" if not plain.endswith("\n") else ""))
    io_out.flush()


class TranscriptStream:
    """A streamed role reply: the label once, then chunks as they arrive with
    complete marker lines stripped (a partial trailing line is held until it
    completes or the turn ends). Content-free trace events record the stream's
    shape."""

    def __init__(self, io_out, label_text, trace=None, trace_fields=None):
        self.io_out = io_out
        self.label_text = label_text
        self.trace = trace
        self.trace_fields = trace_fields or {}
        self.buf = []
        self._pending = ""
        self._started = False
        self._chunks = 0
        self._chars = 0

    def _trace(self, name, **fields):
        if not self.trace:
            return
        data = dict(self.trace_fields)
        data.update(fields)
        self.trace.event(name, **data)

    def __enter__(self):
        self._trace("transcript.stream.start",
                    label_bytes=len(self.label_text.encode("utf-8")))
        return self

    def feed(self, chunk):
        self.buf.append(chunk)
        self._chunks += 1
        self._chars += len(chunk)
        if not self._started:
            self.io_out.write("\n" + self.label_text)
            self._started = True
        self._pending += chunk
        out = []
        while True:
            nl = self._pending.find("\n")
            if nl == -1:
                break
            line = self._pending[:nl + 1]
            self._pending = self._pending[nl + 1:]
            if line.strip() in (INTERNAL_OPEN, INTERNAL_CLOSE):
                continue
            out.append(line)
        if out:
            self.io_out.write("".join(out))
            self.io_out.flush()

    def __exit__(self, *exc):
        tail = self._pending
        self._pending = ""
        if tail and tail.strip() not in (INTERNAL_OPEN, INTERNAL_CLOSE):
            self.io_out.write(tail)
        self.io_out.write("\n")
        full = "".join(self.buf)
        self._trace("transcript.stream.end", chunks=self._chunks,
                    chars=self._chars,
                    lines=full.count("\n") + (1 if full else 0))
        self.io_out.flush()


# --------------------------------------------------------------------------- #
# Notices.                                                                    #
# --------------------------------------------------------------------------- #


def notice(io_out, text):
    """One transcript notice: a blank line, then `text`."""
    if io_out is None:
        return
    try:
        io_out.write("\n" + text + "\n")
        io_out.flush()
    except (OSError, ValueError):
        pass


def render_drain_state(io_out, policy, summary):
    """The evaluation drain's own visible state.

    EVERY FIGURE IS ONE PRECOMPUTED FIELD: `preview`/`drain` compute the
    buckets; nothing is re-derived here. The four counts are whole-queue and
    mutually exclusive:

      Pending/running   `pending_running`  — waiting, or being scored now
      Completed         `drained_total`    — successfully scored, whole queue
      Held/skipped      `held`             — held, deferred or retired
      Terminal/failed   `terminal_total`   — finished failing

    `unverifiable` and `superseded` are breakdowns of the line they belong to,
    never extra buckets. The wording never contains phase-approval phrasing."""
    if io_out is None:
        return
    summary = summary or {}
    completed = "Completed: %s" % summary.get("drained_total", 0)
    if summary.get("unverifiable_total", 0):
        completed += " (%s unverifiable)" % summary["unverifiable_total"]
    held = "Held/skipped: %s" % summary.get("held", 0)
    if summary.get("superseded_total", 0):
        held += " (%s superseded)" % summary["superseded_total"]
    lines = [
        "Evaluation drain",
        "Governing policy: %s" % (policy or "unknown"),
        "Pending/running: %s" % summary.get("pending_running", 0),
        completed,
        held,
        "Terminal/failed: %s" % summary.get("terminal_total", 0),
        "The run continues; nothing waits on this drain.",
    ]
    notice(io_out, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Activity snapshot (M4).                                                     #
#                                                                             #
# Consumes ONLY an already-produced `cowork_activity.project_compact_state()` #
# dict. Nothing here reads persistence, inspects a process, computes an age   #
# or reclassifies: every value is read verbatim off `compact_state` through   #
# the closed `_ACTIVITY_FACT_KEYS` allowlist, and an absent key renders as    #
# the literal `_ACTIVITY_UNREPORTED` marker. A snapshot, never continuous.    #
# --------------------------------------------------------------------------- #

_ACTIVITY_CLASS_LABELS = {
    "productive_model_work": "productive model work",
    "local_tool_work": "local tool work",
    "owned_verification": "owned verification",
    "provider_wait": "waiting on provider",
    "policy_denial": "policy denial",
    "process_crash": "process crash",
    "hung_descendant": "hung descendant",
    "no_evidence_silence": "no evidence (silence)",
}

_WATCHDOG_VERDICT_LABELS = {
    "no_action": "no action",
    "soft_warning": "soft warning",
    "hard_stall_eligible": "hard-stall eligible",
}

_ACTIVITY_UNREPORTED = "(not reported)"

_ACTIVITY_FACT_KEYS = (
    "activity_class", "original_classification", "reconciled", "source",
    "age_seconds", "artifact_delta", "provider_health", "watchdog_verdict",
    "durable_evidence_ref", "process_probe_ref", "next_inspection_at",
    "interval_seconds",
)


def _activity_fact(compact_state, key):
    if key not in compact_state:
        return _ACTIVITY_UNREPORTED
    return compact_state[key]


def _activity_facts(compact_state):
    """The pure, ordered fact set, read from the closed allowlist only."""
    return {key: _activity_fact(compact_state, key)
            for key in _ACTIVITY_FACT_KEYS}


def _activity_class_label(value):
    if not isinstance(value, str):
        return _ACTIVITY_UNREPORTED
    return _ACTIVITY_CLASS_LABELS.get(value, value)


def _watchdog_verdict_label(value):
    if not isinstance(value, str):
        return _ACTIVITY_UNREPORTED
    return _WATCHDOG_VERDICT_LABELS.get(value, value)


def _activity_display(value, none_text):
    if value is _ACTIVITY_UNREPORTED:
        return _ACTIVITY_UNREPORTED
    if value is None:
        return none_text
    return value


def _activity_text_lines(facts):
    """The plain fact lines. A `None` or absent value renders truthfully
    ('none', 'not scheduled', `_ACTIVITY_UNREPORTED`), never invented."""
    class_text = _activity_class_label(facts["activity_class"])
    if facts["reconciled"] is True:
        original_text = _activity_class_label(facts["original_classification"])
        activity_line = "activity: %s (reconciled from %s)" % (
            class_text, original_text)
    else:
        activity_line = "activity: %s" % class_text

    age = facts["age_seconds"]
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        age_line = "age: %ss" % age
    else:
        age_line = "age: %s" % _activity_display(age, "not reported")

    source_line = "source: %s" % _activity_display(
        facts["source"], "not reported")
    provider_line = "provider health: %s" % _activity_display(
        facts["provider_health"], "not reported")
    watchdog_line = "watchdog: %s" % _watchdog_verdict_label(
        facts["watchdog_verdict"])
    evidence_line = "  evidence: durable=%s process=%s" % (
        _activity_display(facts["durable_evidence_ref"], "none"),
        _activity_display(facts["process_probe_ref"], "none"))
    next_line = "next inspection: %s (every %ss)" % (
        _activity_display(facts["next_inspection_at"], "not scheduled"),
        _activity_display(facts["interval_seconds"], "?"))

    delta = facts["artifact_delta"]
    if delta is _ACTIVITY_UNREPORTED:
        delta_text = _ACTIVITY_UNREPORTED
    elif not delta:
        delta_text = "none"
    else:
        delta_text = ", ".join(delta)
    artifact_line = "artifact changes: %s" % delta_text

    return [activity_line, source_line, age_line, provider_line,
            watchdog_line, evidence_line, next_line, artifact_line]


def render_activity(io_out, compact_state):
    """Write the activity snapshot for one turn boundary as plain lines."""
    lines = _activity_text_lines(_activity_facts(compact_state))
    io_out.write("\n".join(lines) + "\n")
    io_out.flush()
