#!/usr/bin/env python3
"""cowork shared UX layer: one isatty gate behind a Python rich-UI stack.

On a real interactive terminal, conversation input is a prompt_toolkit editor
(Enter submits, Shift+Enter/Ctrl+J/Alt+Enter insert a newline), markdown streams
live via Rich, banners are Rich panels, and menus/confirms are questionary. When
the stream is NOT a terminal (piped, or a StringIO under test, or the
non-interactive --team/--config/--context args path), every helper falls back to
the plain readline()/print behavior cowork had before, byte-for-byte — so the
scripted and test paths are unchanged.

rich / prompt_toolkit / questionary are imported lazily inside the TTY branches, so
importing this module (and running the fallback-path tests) never requires them
installed. `cowork --check` verifies them for interactive use.

Python 3.9+.
"""

import array
import errno
import fcntl
import os
# NOT `import select`: this module defines a public `select()` gate helper,
# which would shadow the stdlib module and break the readability fallback.
import select as select_mod
import sys
import termios
import threading

# cowork_trace imports nothing of ours, so this cannot cycle. Used only for the
# user-wait instrumentation seam (P15): with no active trace it is a no-op, so
# an injected/non-interactive prompt emits nothing.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cowork_trace as trace_store  # noqa: E402

# ANSI foreground colors, used only when color is enabled (a real terminal).
RESET = "\033[0m"
CYAN = "\033[36m"     # the user
GREEN = "\033[32m"    # the speaking role (scout, …)
MAGENTA = "\033[35m"  # the planner (distinct from the scout's green)
YELLOW = "\033[33m"   # the builder (distinct from the planner's magenta)
RED = "\033[31m"      # errors
DIM = "\033[2m"       # turn separators / hints

# Per-role label colors; any role not listed falls back to green.
ROLE_COLORS = {"you": CYAN, "planner": MAGENTA, "builder": YELLOW}

# Labels. The plain forms MUST stay byte-identical to the historical constants;
# cowork_bridge re-exports these so `bridge.USER_LABEL` / `bridge.speaker_label`
# keep working.
USER_LABEL = "you › "


def speaker_label(name):
    return "%s › " % name


def is_tty(stream):
    """True only for a real terminal. A FakeTTY test stream overrides isatty() to
    return True; StringIO returns False, so every rich path falls back to plain."""
    return bool(getattr(stream, "isatty", lambda: False)())


def is_real_terminal(stream):
    """True only for a stream backed by an ACTUAL terminal file descriptor.

    Stricter than `is_tty`, deliberately. A FakeTTY test stream overrides
    isatty() to return True so the rich rendering paths get exercised, which is
    exactly right for rendering — and exactly wrong for deciding whether a
    human can answer a blocking question. A gate that opens on a FakeTTY waits
    on a terminal that does not exist.
    """
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    try:
        return bool(os.isatty(fd))
    except (OSError, ValueError):
        return False


def colorize(text, code, enabled):
    """Wrap text in an ANSI color when enabled; return it untouched otherwise."""
    if not enabled:
        return text
    return "%s%s%s" % (code, text, RESET)


def label(name, enabled):
    """Speaker label, colored on a TTY and plain ('name › ') otherwise. The user
    is cyan, the planner magenta; any other role is green. Plain output is
    byte-identical to the old labels."""
    plain = USER_LABEL if name == "you" else speaker_label(name)
    return colorize(plain, ROLE_COLORS.get(name, GREEN), enabled)


def display_path(path):
    """Collapse a leading $HOME prefix to '~' so a home-rooted path renders short
    and scannable (e.g. '~/.cowork/sessions/<id>/planner.plan.md'). A path that
    is exactly home becomes '~'; a path not under home is returned unchanged."""
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


def hyperlink(text, target_abspath, enabled):
    """Layer an OSC 8 hyperlink to file://<abs> over `text` when enabled (a TTY).
    The visible characters are unchanged in both cases — capable terminals make
    `text` clickable; everywhere else (and off a TTY) `text` is returned plain so
    the short path stays copy-pasteable. Width-correct: terminals and Rich size
    on the visible text, not the escape."""
    if not enabled or not target_abspath:
        return text
    target = os.path.abspath(target_abspath)
    return "\033]8;;file://%s\033\\%s\033]8;;\033\\" % (target, text)


def _path_display(path, cwd=None):
    """The short display string for a path: cwd-relative when it sits under cwd,
    else '~/…' when under home, else '…/<basename>'. (No linking — see
    `render_path`.)"""
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


def render_path(path, enabled=False, cwd=None):
    """The user-facing rendering of a filesystem path: the short display form
    (`_path_display`) wrapped in an OSC 8 hyperlink to the absolute file on a TTY.
    The single helper every banner/notice path should use so the '~' form and the
    clickable link are consistent everywhere."""
    if not path:
        return path
    return hyperlink(_path_display(path, cwd), path, enabled)


def shorten_path(path, cwd=None):
    """A short, scannable form of a path: relative to cwd when it sits under it,
    else '~/…' for a home-rooted path, else '…/<basename>'. Linking-free; callers
    that also want a clickable target use `render_path`."""
    return _path_display(path, cwd)


def turn_separator(io_out, enabled=None):
    """A faint rule between turns. No-op when not a TTY (keeps test output clean)."""
    enabled = is_tty(io_out) if enabled is None else enabled
    if not enabled:
        return
    io_out.write("\n" + colorize("─" * 48, DIM, True) + "\n")
    io_out.flush()


def internal_lead_in(io_out, enabled=None):
    """A faint lead-in (blank line + dim rule) printed just above a surfaced
    internal block — the reviewer/advisor's dim channel — so it gets breathing
    room from the agent text above instead of crowding it. No-op when not a TTY
    (byte-identical output, exactly like turn_separator), so the scripted/test
    paths are unchanged."""
    enabled = is_tty(io_out) if enabled is None else enabled
    if not enabled:
        return
    io_out.write("\n" + colorize("─" * 48, DIM, True) + "\n")
    io_out.flush()


class Spinner:
    """Minimal TTY spinner. No-op when the output is not a real terminal.

    Usable as a context manager (`with Spinner(out):`) or imperatively via
    start()/stop(). Used for turn-based controllers (codex) that don't stream."""

    FRAMES = "|/-\\"

    def __init__(self, out, label="working"):
        self.out = out
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self._tty = is_tty(out)

    def start(self):
        if self._tty and self._thread is None:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    __enter__ = start

    def set_label(self, text):
        """Swap the label while spinning (e.g. 'scout working' -> 'scout using
        Bash'). The spin thread re-reads the label every frame; off a TTY this
        is a pure attribute write (no thread, no bytes)."""
        self.label = text

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            # \033[K clears to end-of-line so a shrinking label leaves no residue.
            self.out.write(
                "\r\033[K%s %s…" % (self.FRAMES[i % len(self.FRAMES)], self.label))
            self.out.flush()
            i += 1
            self._stop.wait(0.1)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        if self._tty:
            self.out.write("\r\033[K")  # clear the spinner line
            self.out.flush()

    def __exit__(self, *exc):
        self.stop()


# --------------------------------------------------------------------------- #
# Markdown rendering (Rich).                                                   #
# --------------------------------------------------------------------------- #


def _terminal_size():
    import shutil
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


def _rich_console(io_out, size=None):
    from rich.console import Console
    # force_terminal so Rich emits styling even when io_out is a FakeTTY/pipe we
    # have already decided is interactive. Pin the real terminal size: io_out is a
    # wrapped stream Rich can't always size from, so it would fall back to 80x25 —
    # which makes Live think short replies overflow the viewport and replay lines.
    cols, rows = size or _terminal_size()
    return Console(file=io_out, force_terminal=True,
                   width=cols, height=rows)


# --------------------------------------------------------------------------- #
# Channel rendering: user-facing vs. internal (self-narration / reviewer loop). #
#                                                                             #
# A user-facing role may wrap internal self-narration in sentinel lines, each #
# ALONE on its own line: `[[internal]]` opens a block, `[[/internal]]` closes  #
# it. Everything outside such a block is user-facing. Reviewer/advisor         #
# sessions render WHOLLY internal by construction (internal=True), so their    #
# robustness never depends on the model emitting markers.                      #
#                                                                             #
# The same parser (`split_channel_segments`) backs both render paths — the     #
# streaming claude path (StreamingMarkdown) and the one-shot codex path        #
# (render_markdown) — so behavior is identical across controllers. On a TTY an #
# internal segment is de-emphasized (Rich dim) under a small sub-label; off a  #
# TTY only the marker lines are stripped and the enclosed text is emitted      #
# plain, so marker-FREE content is byte-identical to the historical output.    #
# --------------------------------------------------------------------------- #

INTERNAL_OPEN = "[[internal]]"
INTERNAL_CLOSE = "[[/internal]]"
# Shown dim ahead of an internal block on a TTY so the user can tell internal
# self/peer chatter from content addressed to them.
INTERNAL_SUBLABEL = "· internal"


def split_channel_segments(text, internal_start=False):
    """Split `text` into ordered (channel, segment_text) runs, channel in
    {'user','internal'}, and return (segments, internal_end).

    A control line is recognized ONLY when a full line's stripped content equals
    exactly `[[internal]]` or `[[/internal]]`; text that merely contains the
    literal mid-line renders verbatim. Channel state is a BOOLEAN (depth-1): a
    second open while already internal, or a close with no open, is a no-op.
    Marker lines are always stripped. `internal_start` seeds the state so a
    block can span multiple calls (the streaming commit cursor); `internal_end`
    reports the state after this text so the caller can carry it forward.

    For marker-FREE text the single returned segment's text is byte-identical to
    the input (``"".join(seg for _c, seg in segments) == text``)."""
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
            continue  # marker line is channel control, never displayed
        if stripped == INTERNAL_CLOSE:
            if internal:
                flush()
                internal = False
                channel = "user"
            continue
        buf.append(line)
    flush()
    return segments, internal


def _hold_marker_prefix(text):
    """Split off a trailing partial line (no terminating newline) that COULD be
    the start of a control marker, so the live TTY tail never flashes a partial
    sentinel like `[[intern` before the line completes. Returns the text safe to
    render now; the held remainder stays in the region buffer and renders once
    the line completes (or, at end of turn, as ordinary content — an incomplete
    sentinel is never a marker). Complete marker lines are handled by the parser;
    this only guards the still-growing last line."""
    nl = text.rfind("\n")
    last = text[nl + 1:]
    stripped = last.strip()
    if stripped and (INTERNAL_OPEN.startswith(stripped)
                     or INTERNAL_CLOSE.startswith(stripped)):
        return text[:nl + 1]  # hold the ambiguous trailing line
    return text


def _segment_renderables(text, internal_start=False, whole_internal=False):
    """Build the Rich renderables for `text`'s channel segments (TTY only), and
    return (renderables, internal_end). A 'user' segment renders as Markdown; an
    'internal' segment renders as a dim sub-label followed by dim Markdown.
    `whole_internal` treats the entire text as one internal segment (the
    reviewer/advisor channel), bypassing marker parsing."""
    from rich.markdown import Markdown
    from rich.styled import Styled
    from rich.text import Text
    if whole_internal:
        # Strip control lines even for a wholly-internal region (the contract:
        # marker lines are NEVER emitted literally), then render every remaining
        # line on the internal channel regardless of any stray markers within.
        stripped, _end = split_channel_segments(text)
        segments = [("internal", "".join(s for _channel, s in stripped))]
        internal_end = True
    else:
        segments, internal_end = split_channel_segments(text, internal_start)
    renderables = []
    for channel, seg in segments:
        body = seg.strip("\n")
        if not body:
            continue
        if channel == "internal":
            renderables.append(Text(INTERNAL_SUBLABEL, style="dim"))
            renderables.append(Styled(Markdown(body), "dim"))
        else:
            renderables.append(Markdown(body))
    return renderables, internal_end


def render_markdown(io_out, text, enabled=None, internal=False):
    """Render markdown on a TTY (Rich); write the raw text otherwise. Used for
    whole, non-streamed replies (codex) and any one-shot markdown.

    Channel-aware: inline `[[internal]]` blocks render dim with a sub-label, and
    `internal=True` renders the WHOLE text on the internal channel (the codex
    reviewer/advisor path). Off a TTY only the marker lines are stripped — for
    marker-free content the output is byte-identical to the historical raw
    write."""
    enabled = is_tty(io_out) if enabled is None else enabled
    if not enabled:
        # Off a TTY there is no styling, so the internal flag only governs which
        # lines are stripped: marker lines go, enclosed text stays plain.
        segments, _ = split_channel_segments(text)
        plain = "".join(seg for _channel, seg in segments)
        io_out.write(plain + ("\n" if not plain.endswith("\n") else ""))
        io_out.flush()
        return
    console = _rich_console(io_out)
    renderables, _ = _segment_renderables(text, whole_internal=internal)
    for renderable in renderables:
        console.print(renderable)


def _safe_commit_point(text, start):
    """Largest index > start such that text[start:index] is a self-contained
    markdown prefix safe to render permanently and drop from the live region.

    "Safe" = ends on a paragraph boundary (a blank line) that is NOT inside an
    open ``` code fence (a fence can contain blank lines, so a naive split would
    cut it mid-block and render garbage). Returns the index just past the blank
    line, or None if nothing can be committed yet."""
    best = None
    fences = 0
    i = start
    n = len(text)
    while True:
        nl = text.find("\n\n", i)
        if nl == -1:
            break
        # count fence lines in the candidate prefix (cheap; prefixes are short).
        seg = text[i:nl]
        fences += sum(1 for ln in seg.split("\n") if ln.lstrip().startswith("```"))
        if fences % 2 == 0:
            best = nl + 2  # commit through the blank line; remainder starts clean
        i = nl + 2
        if i >= n:
            break
    return best


class StreamingMarkdown:
    """A live-rendered markdown region on a TTY; a raw passthrough otherwise.

    TTY: open a Rich Live region. feed(chunk) grows a buffer, permanently prints
    any complete leading paragraphs above the region, and keeps only the still-
    growing tail inside Live. Holding just the tail bounds the Live region height,
    so it never overflows the viewport and replays lines as the reply gets long.
    Non-TTY: write the label once, then stream raw chunks live — byte-identical to
    the historical raw stream, so the StringIO/mocked-subprocess tests are
    unchanged."""

    def __init__(self, io_out, label_text, trace=None, trace_fields=None,
                 internal=False):
        self.io_out = io_out
        self.label_text = label_text
        self.trace = trace
        self.trace_fields = trace_fields or {}
        self.tty = is_tty(io_out)
        # internal=True renders the WHOLE region on the internal (dim) channel —
        # the reviewer/advisor session. Otherwise inline `[[internal]]` blocks
        # opt individual runs onto the internal channel.
        self.internal = internal
        # Channel state at the TTY commit cursor, carried across commits so a
        # block that opens in one committed paragraph and closes in a later one
        # stays dim throughout. _render() reads it without mutating it.
        self._channel_internal = internal
        # Non-TTY marker stripping is line-oriented: a marker is acted on only
        # once a complete line is available, so a partial trailing line is held
        # here until the next chunk (or the turn end) completes it.
        self._nontty_pending = ""
        self._nontty_internal = internal
        self.buf = []
        self._committed = 0  # chars of the buffer already printed permanently
        self._console = None
        self._live = None
        self._status = None  # transient activity line ('scout using Bash…')
        self._started = False  # non-TTY: have we written the label yet
        self._chunks = 0
        self._chars = 0
        self._status_sets = 0
        self._status_clears = 0

    def _trace(self, name, **fields):
        if not self.trace:
            return
        data = dict(self.trace_fields)
        data.update(fields)
        self.trace.event(name, **data)

    def __enter__(self):
        renderer = "rich_live" if self.tty else "raw"
        size = _terminal_size() if self.tty else (None, None)
        term = os.environ.get("TERM") if self.tty else None
        self._trace(
            "ui.markdown.start",
            renderer=renderer,
            tty=self.tty,
            terminal_width=size[0],
            terminal_height=size[1],
            label_bytes=len(self.label_text.encode("utf-8")),
            vertical_overflow="visible" if self.tty else None,
            term_present=bool(term) if self.tty else None,
            term_dumb=(term == "dumb") if self.tty else None,
        )
        if self.tty:
            from rich.live import Live
            from rich.markdown import Markdown
            # A surfaced internal region (reviewer/advisor) gets a faint lead-in
            # gap above its label so the dim channel doesn't crowd the agent text
            # before it. Rendered once, here, above the label (no-op off a TTY,
            # which never reaches this branch).
            if self.internal:
                internal_lead_in(self.io_out, True)
            self.io_out.write("\n" + self.label_text + "\n")
            self.io_out.flush()
            self._console = _rich_console(self.io_out, size=size)
            self._live = Live(Markdown(""), console=self._console,
                              refresh_per_second=10, vertical_overflow="visible")
            self._live.__enter__()
        return self

    def _tail(self):
        return "".join(self.buf)[self._committed:]

    def _commit_complete_paragraphs(self):
        """Print finalized leading paragraphs above the Live region and advance the
        commit cursor, so Live only ever holds the unfinished tail. Rich routes
        console.print on the Live console above the live region automatically."""
        full = "".join(self.buf)
        point = _safe_commit_point(full, self._committed)
        if point is None:
            return
        raw = full[self._committed:point]
        renderables, self._channel_internal = _segment_renderables(
            raw, internal_start=self._channel_internal,
            whole_internal=self.internal)
        if renderables:
            for renderable in renderables:
                self._console.print(renderable)
            chunk = raw.strip("\n")
            self._trace(
                "ui.markdown.commit",
                renderer="rich_live",
                chunk_chars=len(chunk),
                chunk_lines=chunk.count("\n") + 1,
                committed_chars=point,
                tail_chars=len(full) - point,
            )
        self._committed = point

    def _render(self):
        """The Live renderable: the still-growing tail, plus an animated dim status
        row while the agent is busy between text blocks (tool calls). The spinner
        is built fresh per render — Live's auto-refresh animates it against
        console time, and not storing it keeps the state surface minimal."""
        from rich.markdown import Markdown
        # The tail is not yet committed, so render it from a COPY of the channel
        # state (discard the returned end-state — only a commit advances it).
        # Hold back a trailing partial line that could be a marker prefix, so a
        # marker split across chunks never flashes half-matched in the live tail
        # (the held text renders next frame once the line completes).
        renderables, _ = _segment_renderables(
            _hold_marker_prefix(self._tail()),
            internal_start=self._channel_internal,
            whole_internal=self.internal)
        items = list(renderables)
        if self._status is not None:
            from rich.spinner import Spinner as RichSpinner
            from rich.text import Text
            items.append(RichSpinner("dots", text=Text(self._status, style="dim")))
        if not items:
            return Markdown("")
        if len(items) == 1:
            return items[0]
        from rich.console import Group
        return Group(*items)

    def set_status(self, text):
        """Show an activity row under the markdown (TTY only; no-op otherwise so
        the non-TTY byte contract is untouched)."""
        if not self.tty or self._live is None:
            return
        self._status_sets += 1
        self._status = text
        self._live.update(self._render())

    def clear_status(self):
        if self._status is None:
            return
        self._status = None
        self._status_clears += 1
        if self.tty and self._live is not None:
            self._live.update(self._render())

    def feed(self, chunk):
        self.buf.append(chunk)
        self._chunks += 1
        self._chars += len(chunk)
        if self.tty:
            self._commit_complete_paragraphs()
            self._live.update(self._render())
        else:
            if not self._started:
                self.io_out.write("\n" + self.label_text)
                self._started = True
            self._feed_nontty(chunk)

    def _feed_nontty(self, chunk):
        """Stream a chunk off a TTY, stripping whole marker lines as they
        complete. A complete line ending in '\\n' is classified now; a partial
        trailing line is held in self._nontty_pending until completed (markers
        act on COMPLETE lines only). For marker-free content the emitted bytes
        are identical to the historical raw passthrough."""
        self._nontty_pending += chunk
        out = []
        while True:
            nl = self._nontty_pending.find("\n")
            if nl == -1:
                break
            line = self._nontty_pending[:nl + 1]
            self._nontty_pending = self._nontty_pending[nl + 1:]
            stripped = line.strip()
            if stripped == INTERNAL_OPEN:
                self._nontty_internal = True
                continue  # marker line stripped from output
            if stripped == INTERNAL_CLOSE:
                self._nontty_internal = False
                continue
            out.append(line)
        if out:
            self.io_out.write("".join(out))
            self.io_out.flush()

    def __exit__(self, *exc):
        if self.tty and self._live is not None:
            self._status = None  # never leave a tool label in the final render
            # Empty the Live region, tear it down, then print the remaining tail
            # permanently — once. Rendering the tail in the final Live frame AND
            # printing it would duplicate it; clearing first avoids that.
            tail = self._tail()
            self._committed = len("".join(self.buf))  # _tail() now empty
            self._live.update(self._render())
            self._live.__exit__(*exc)
            # Render the tail's segments — this force-closes any unclosed
            # internal block (it just renders dim through end of turn); channel
            # state never carries into the next turn (a fresh region per send).
            renderables, self._channel_internal = _segment_renderables(
                tail, internal_start=self._channel_internal,
                whole_internal=self.internal)
            for renderable in renderables:
                self._console.print(renderable)
        else:
            # Flush any held partial line. A trailing marker line (no newline)
            # is force-closed: classified and stripped rather than leaked.
            tail = self._nontty_pending
            self._nontty_pending = ""
            if tail.strip() not in (INTERNAL_OPEN, INTERNAL_CLOSE) and tail:
                self.io_out.write(tail)
            self.io_out.write("\n")
        full = "".join(self.buf)
        self._trace(
            "ui.markdown.end",
            renderer="rich_live" if self.tty else "raw",
            tty=self.tty,
            chunks=self._chunks,
            chars=self._chars,
            lines=full.count("\n") + (1 if full else 0),
            committed_chars=self._committed,
            final_tail_chars=len(self._tail()),
            status_sets=self._status_sets,
            status_clears=self._status_clears,
        )
        self.io_out.flush()


# --------------------------------------------------------------------------- #
# Banners (Rich Panel).                                                        #
# --------------------------------------------------------------------------- #

# border styles per banner kind (Rich color names).
_BANNER_STYLE = {"start": "blue", "review": "green", "done": "green",
                 "needs_input": "yellow", "dissent": "yellow", "info": "white"}


def banner(io_out, text, kind="info", enabled=None):
    """A bordered, colored Rich panel on a TTY; plain text otherwise.

    The plain fallback writes `text` verbatim, so callers can rely on their
    keyword substrings ('ready for review', 'needs your input', …) surviving in
    the non-TTY/test path."""
    enabled = is_tty(io_out) if enabled is None else enabled
    if not enabled:
        io_out.write("\n" + text + "\n")
        io_out.flush()
        return
    from rich.panel import Panel
    style = _BANNER_STYLE.get(kind, "white")
    _rich_console(io_out).print(Panel(text, border_style=style, expand=False))


# --------------------------------------------------------------------------- #
# Gate input activation (the UX-011 approval-integrity boundary).              #
#                                                                              #
# A consequential gate must consume only input produced AFTER it has finished  #
# painting. Two independent channels feed a freshly opened gate stale bytes:   #
#                                                                              #
#   1. the kernel tty input queue — while a role turn runs there is no         #
#      prompt_toolkit application attached, so anything typed sits in the fd's  #
#      queue and the next application reads it as ordinary key presses; and     #
#   2. prompt_toolkit's PROCESS-GLOBAL typeahead buffer                        #
#      (prompt_toolkit/input/typeahead.py `_buffer`, keyed by                  #
#      input.typeahead_hash() == "fd-<fileno>", i.e. the same key for every    #
#      application on stdin). Application.run_async() stores every unprocessed  #
#      key press at exit and feeds it into the NEXT application — before that  #
#      application's first render. That is the mechanism behind the recorded    #
#      incident: the tail of a multi-line answer, left unprocessed when the     #
#      answer editor exited, replayed into a review menu minutes later.        #
#                                                                              #
# So the boundary is TWO drains around one activation: `begin_gate` clears both #
# channels before the widget's application starts (the typeahead replay happens #
# before the first paint, so a render hook cannot catch it), and               #
# `arm_activation` drains the fd again on the first `after_render` (keys read  #
# from the fd can only be read by the event loop's reader callback, which       #
# cannot run until that first synchronous redraw returns, so a render hook IS   #
# the right place for them). Neither drain alone is sufficient.                #
#                                                                              #
# Contract:                                                                    #
#   * DISCARD is absolute. Every pre-activation byte is dropped and can never  #
#     select anything.                                                         #
#   * NOTICE is observation-bound. Sampling the queue (FIONREAD/select) and     #
#     flushing it (tcflush) cannot be one atomic operation, so a byte landing  #
#     in the microseconds between them is still discarded but was never seen,  #
#     and is not reported. cowork never warns speculatively, so a notice        #
#     always refers to input that really was queued.                           #
#   * FAIL CLOSED. If the queue cannot be cleared the stale bytes are still    #
#     there, so the gate is NOT run: the wrapper returns DRAIN_FAILED and each  #
#     reader maps it to its own safe, non-approving outcome.                   #
#   * There is NO environment variable and NO flag that disables any of this.  #
# --------------------------------------------------------------------------- #

# Clamp on any reported count. Never user-facing policy — just a bound so a
# runaway paste cannot put an unbounded number in a notice or a trace record.
GATE_DISCARD_CAP = 999

# tcflush attempts within a single drain before it is called a failure.
GATE_DRAIN_RETRIES = 3

# How many times ONE gate may be re-opened after a post-render discard before it
# fails closed. The re-open loop is driven by external input, so a stuck key or a
# continuous paste could otherwise re-open the gate forever. Exhausting this is
# NOT a drain failure — the boundary worked every time — so it reports its own
# `reopen_limit` reason while taking the same safe, non-approving exit.
GATE_REOPEN_LIMIT = 3

# Returned from a widget's application when the first-render drain found queued
# input: the wrapper prints the notice and re-opens the gate. Private to this
# module's wrapper loop; it never escapes to a caller.
STALE = object()


class _DrainFailed:
    """The value the gate wrappers return when the input boundary could not be
    established. `__bool__` RAISES so an unmapped call site fails loudly instead
    of evaluating truthy and approving — a plain object() would have been truthy
    at `if ui.confirm(...)`. It also has no `.strip()`, so the free-form feedback
    sites raise AttributeError rather than mis-branching."""

    __slots__ = ()

    def __bool__(self):
        raise TypeError(
            "gate input boundary failed; caller must map ui.DRAIN_FAILED")

    def __repr__(self):
        return "<ui.DRAIN_FAILED>"


DRAIN_FAILED = _DrainFailed()

_gate_epoch_lock = threading.Lock()
_gate_epoch = 0


def _next_gate_epoch():
    """A monotonically increasing id for one gate opening, so a trace reader can
    tell two drains of the same gate apart."""
    global _gate_epoch
    with _gate_epoch_lock:
        _gate_epoch += 1
        return _gate_epoch


def _clamp_count(n):
    if n is None:
        return None
    return min(int(n), GATE_DISCARD_CAP)


def _gate_fd(stream):
    """The file descriptor of `stream` when it is a REAL terminal, else None.

    A FakeTTY (a StringIO claiming isatty()) has no usable fileno, so every
    existing test path and every piped/scripted path yields None and the whole
    boundary no-ops."""
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return None
    try:
        return fd if os.isatty(fd) else None
    except (OSError, ValueError):
        return None


class DrainResult:
    """Outcome of one attempt to clear the terminal input queue.

    `ok` is True ONLY when tcflush actually returned. On False the stale bytes
    are STILL QUEUED and the caller must not proceed as if they were gone."""

    __slots__ = ("ok", "pending", "count", "errno_name")

    def __init__(self, ok, pending=False, count=None, errno_name=None):
        self.ok = ok
        self.pending = pending
        self.count = count
        self.errno_name = errno_name


def _pending_input(fd):
    """(pending, count) for the terminal input queue, WITHOUT reading a byte.

    FIONREAD reports the exact pending byte count without transferring anything
    into the process. When it is unavailable the fallback is select(), which
    answers only 'is something readable' — hence (True, None). There is
    deliberately NO os.read anywhere on this path: reading the bytes even to
    measure them would materialize discarded content in Python."""
    if fd is None:
        return (False, 0)
    try:
        buf = array.array("i", [0])
        fcntl.ioctl(fd, termios.FIONREAD, buf, True)
        n = int(buf[0])
        return (n > 0, n)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        readable, _w, _x = select_mod.select([fd], [], [], 0)
        return (bool(readable), None)
    except (OSError, ValueError):
        return (False, None)


def _errno_name(exc):
    """The symbolic errno ('ENOTTY', …) of an OSError — never a message, which
    could carry user data."""
    return errno.errorcode.get(getattr(exc, "errno", None))


def _drop_terminal_input(fd):
    """Sample and then discard the terminal input queue. The bytes are dropped
    in the kernel by tcflush(TCIFLUSH) and never enter this process."""
    if fd is None:
        return DrainResult(True, False, 0, None)
    pending, count = _pending_input(fd)
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except OSError as exc:
        # The queue was NOT cleared. Report the failure rather than swallowing
        # it — the caller fails closed.
        return DrainResult(False, pending, count, _errno_name(exc))
    return DrainResult(True, pending, count, None)


def _drop_typeahead():
    """Clear prompt_toolkit's process-global typeahead bucket for the session's
    input. Returns (ok, count) with a clamped character count.

    Unlike the terminal queue, prompt_toolkit has ALREADY materialized these key
    presses before cowork is involved, so they cannot be dropped without being
    touched. The guarantee here is narrower and explicit: they are cleared
    without ever being logged, displayed, replayed or otherwise exposed, and
    only a capped length escapes as metadata.

    FAILS CLOSED. `ok` is False only when a bucket that EXISTS could not be
    emptied — that leaves replayable key presses armed for the next application,
    which is the exact incident mechanism, so the caller must not treat the
    boundary as established. Two cases are NOT failures because there is nothing
    to clear: prompt_toolkit not being importable, and a session that has not
    resolved an input yet (the bucket is keyed by input.typeahead_hash(), so it
    can only be non-empty once an Application has run)."""
    try:
        from prompt_toolkit.application.current import get_app_session
        from prompt_toolkit.input import typeahead as pt_typeahead
    except ImportError:
        return (True, 0)
    # Read the session's input WITHOUT creating one: asking for one would
    # needlessly construct a terminal input object at the very first gate.
    try:
        session = get_app_session()
    except Exception:
        return (False, 0)
    if getattr(session, "_input", None) is None:
        return (True, 0)
    try:
        inp = session.input
        presses = pt_typeahead.get_typeahead(inp)   # returns AND resets
        count = 0
        for press in presses:
            count += len(getattr(press, "data", "") or "")
        pt_typeahead.clear_typeahead(inp)
        del presses
    except Exception:
        # The bucket exists and could not be emptied. Do NOT swallow this: its
        # contents would be replayed into the next application before it draws.
        return (False, 0)
    return (True, _clamp_count(count))


class Activation:
    """The record of one gate opening. `begin_gate` and `arm_activation` only
    INSPECT and MUTATE this — they render nothing and call no callback, so the
    ui wrapper stays the single reporting layer."""

    __slots__ = ("gate", "epoch", "pending", "count", "safe", "errno_name",
                 "typeahead_cleared", "active", "reason")

    def __init__(self, gate=None, epoch=0):
        self.gate = gate
        self.epoch = epoch
        self.pending = False
        self.count = 0
        self.safe = True
        self.errno_name = None
        self.typeahead_cleared = False
        self.active = False
        # Which stale-input channel could not be cleared, when safe is False:
        # 'tcflush' (the terminal queue), 'typeahead' (prompt_toolkit's
        # cross-application replay bucket) or 'key_queue' (the live
        # application's own buffers). None while the boundary holds.
        self.reason = None


def begin_gate(io_in, io_out, gate=None):
    """Drain both stale-input channels BEFORE a gate's widget opens.

    The typeahead buffer must be cleared here, not from a render hook:
    Application.run_async() feeds it into the key processor before the first
    redraw, so by first render the key has already been processed and the
    highlighted choice already accepted.

    Off a real terminal this is a no-op (safe, nothing pending)."""
    act = Activation(gate=gate, epoch=_next_gate_epoch())
    fd = _gate_fd(io_in)
    if fd is None:
        return act
    result = DrainResult(False)
    for _ in range(GATE_DRAIN_RETRIES):
        result = _drop_terminal_input(fd)
        if result.ok:
            break
    if not result.ok:
        # Fail closed. Still try the typeahead bucket — it is an independent
        # stale channel and clearing it is strictly better than not — but leave
        # pending/count empty so nothing downstream can report a discard that
        # did not happen.
        cleared, _count = _drop_typeahead()
        act.safe = False
        act.reason = "tcflush"
        act.errno_name = result.errno_name
        act.typeahead_cleared = cleared
        return act
    cleared, typeahead = _drop_typeahead()
    if not cleared:
        # The terminal queue is clean but prompt_toolkit's replay bucket is not,
        # and its contents are fed to the next application BEFORE it draws — the
        # exact incident mechanism. Fail closed rather than open a gate that can
        # be driven by keys we could not drop.
        act.safe = False
        act.reason = "typeahead"
        act.typeahead_cleared = False
        return act
    act.pending = bool(result.pending or typeahead)
    if result.count is None:
        act.count = None                      # FIONREAD unavailable
    else:
        act.count = _clamp_count(result.count + typeahead)
    return act


def arm_activation(app, activation, fd):
    """Install a ONE-SHOT drain on `app`'s first render.

    after_render fires at the end of the first _redraw(), which happens before
    the event loop can run its fd reader — so anything this finds arrived while
    the gate was drawing. It records the outcome on the SAME Activation and
    exits the application with STALE so the wrapper can report once and re-open
    a live gate. One-shot, so later redraws (cursor moves, typing) never drain
    and a genuine in-box paste is never over-discarded."""
    if app is None or fd is None:
        return
    try:
        after_render = app.after_render
    except AttributeError:
        return                                # stub session in a test
    state = {"fired": False}

    def _on_first_render(_sender=None):
        if state["fired"]:
            return                            # the render_as_done redraw
        state["fired"] = True
        result = DrainResult(True, False, 0, None)
        for _ in range(GATE_DRAIN_RETRIES):
            result = _drop_terminal_input(fd)
            if result.ok:
                break
        # Drop the live application's OWN queues too, without inspecting them.
        # (These are the running app's buffers, deliberately NOT what the
        # typeahead_cleared flag reports.)
        #
        # A MISSING attribute is tolerated — stub sessions in tests have no
        # key_processor or input, and there is then nothing to drop. A present
        # one that RAISES is a boundary failure and fails closed: those buffers
        # can hold keys read before activation, so leaving them is exactly the
        # hazard this drain exists to remove.
        queues_ok = True
        processor = getattr(app, "key_processor", None)
        if processor is not None:
            try:
                processor.empty_queue()
            except Exception:
                queues_ok = False
        app_input = getattr(app, "input", None)
        if app_input is not None and hasattr(app_input, "flush_keys"):
            try:
                app_input.flush_keys()
            except Exception:
                queues_ok = False
        activation.active = True
        stale = False
        if not result.ok:
            activation.safe = False
            activation.reason = "tcflush"
            activation.errno_name = result.errno_name
            activation.typeahead_cleared = False
            stale = True
        elif not queues_ok:
            activation.safe = False
            activation.reason = "key_queue"
            activation.typeahead_cleared = False
            stale = True
        elif result.pending:
            activation.pending = True
            activation.count = _clamp_count(result.count)
            stale = True
        if stale:
            try:
                app.exit(STALE)
            except Exception:                 # pragma: no cover - not running
                pass

    after_render += _on_first_render


def _notice(io_out, text):
    """Notices are plain writes, never Rich: they must survive verbatim into a
    captured pty transcript and into the non-color path."""
    if io_out is None:
        return
    try:
        io_out.write("\n" + text + "\n")
        io_out.flush()
    except (OSError, ValueError):
        pass


def discard_notice(io_out, count=None):
    """Content-free heads-up that queued input was thrown away. Only a clamped
    count is ever rendered — never any payload text."""
    if count is None:
        detail = "Input typed before this gate was ready"
    else:
        shown = ("%d+" % GATE_DISCARD_CAP if count >= GATE_DISCARD_CAP
                 else str(count))
        detail = "Input typed before this gate was ready (%s characters)" % shown
    _notice(io_out, detail + " was ignored — please enter it again.")


def drain_failed_notice(io_out):
    """One wording for every channel that can fail (the terminal queue,
    prompt_toolkit's replay bucket, the live application's own buffers): what
    matters to the user is identical in all three — leftover input could not be
    cleared, so old and new keystrokes can no longer be told apart."""
    _notice(io_out,
            "cowork could not clear input left over from before this gate, so "
            "it cannot tell keystrokes typed before this gate from keystrokes "
            "typed now. This gate will not be run.")


def gate_abandoned_notice(io_out):
    _notice(io_out,
            "Input kept arriving before this gate was ready, so the gate was "
            "not run and this phase is ending without approving.")


def render_drain_state(io_out, policy, summary, blocking=False):
    """The evaluation drain's own visible state.

    Draining used to happen silently inside a phase transition, so time the run
    spent waiting on scoring looked exactly like the previous phase having been
    signed off. It is its own thing now, and it says so: the wording below never
    contains any phase-approval phrasing.

    EVERY FIGURE IS ONE PRECOMPUTED FIELD. There is no arithmetic here, and none
    is allowed: `preview`/`drain` compute the buckets, and a renderer that
    re-derived one could disagree with the durable state it claims to describe.

    The four counts are whole-queue and mutually exclusive, and together they
    account for every entry in the queue:

      Pending/running   `pending_running`  — waiting, or being scored now
      Completed         `drained_total`    — successfully scored, whole queue
      Held/skipped      `held`             — held, deferred or retired
      Terminal/failed   `terminal_total`   — finished failing; needs a retry

    Completed is pinned to the WHOLE-QUEUE `drained_total`, never to the
    per-drain `drained`: this screen is drawn from a preview, which by
    definition has scored nothing, so the per-drain figure would read 0 on every
    screen no matter how much work the queue had actually finished.

    `unverifiable` and `superseded` are printed as BREAKDOWNS OF the line they
    belong to, never as extra buckets. Superseded work sits inside held/skipped
    precisely because retiring a superseded candidate is not completing it —
    nothing was scored — and reporting it under Completed is the specific lie
    this layout exists to prevent. An unverifiable entry did drain successfully,
    so it is counted in Completed, but never silently: its own count is printed
    beside it.

    Plain writes only, so the block is byte-identical on and off a TTY and can
    be asserted verbatim against a StringIO.
    """
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
    ]
    if blocking:
        lines.append("This drain is holding the run until you choose below.")
    else:
        lines.append("No evaluation work is running; the run continues.")
    _notice(io_out, "\n".join(lines))


def drain_gate(io_in, io_out, policy, summary, ask_fn=None):
    """Render the drain state and ask what evaluation should do.

    Four bounded, safe actions. NONE of them invents a success: holding, ending
    and walking away all leave every durable entry exactly where it is, and none
    of them writes a completion marker for work that did not complete.

    `retry` is offered only when there is already-terminal work to retry —
    `terminal_existing`, the retryable set, and NOT `terminal`, which counts
    what became terminal during a drain that has not happened yet.

    Dismissal and a failed input boundary both fall back to `end`, which is the
    safe direction: the queue is preserved and nothing is scored.
    """
    render_drain_state(io_out, policy, summary, blocking=True)
    choices = [("continue", "Continue eligible evaluation work"),
               ("hold", "Hold remaining optional work")]
    if (summary or {}).get("terminal_existing", 0):
        choices.append(("retry", "Retry eligible failed work"))
    choices.append(("end", "End without evaluating (queue preserved)"))
    picked = select("What should evaluation do?", choices, ask_fn=ask_fn,
                    io_in=io_in, io_out=io_out, gate="evaluation_drain")
    if picked is DRAIN_FAILED or picked is None:
        return "end"
    return picked


def _protected(io_in, io_out, ask_fn=None):
    """Whether a wrapper call is a protected gate: an injected ask_fn or a
    missing/non-terminal stream keeps today's behavior byte-identical."""
    if ask_fn is not None:
        return False
    if io_in is None or io_out is None:
        return False
    return is_tty(io_in) and is_tty(io_out)


def _run_gate(io_in, io_out, gate, on_discard, on_drain_fail, build):
    """THE wrapper loop, shared verbatim by confirm / select / prompt_user.

    `build()` returns (application, run) for ONE attempt — a fresh widget every
    time, so exactly one after_render handler exists per attempt and handlers
    can never accumulate on a reused application object.

    This is the ONLY layer that renders a notice or invokes a callback, and it
    does so exactly once per gate open."""
    fd = _gate_fd(io_in)
    reopens = 0
    while True:
        act = begin_gate(io_in, io_out, gate=gate)
        if not act.safe:
            drain_failed_notice(io_out)
            if on_drain_fail is not None:
                on_drain_fail(gate=act.gate, epoch=act.epoch,
                              phase="pre_render", reason=act.reason,
                              errno_name=act.errno_name,
                              typeahead_cleared=act.typeahead_cleared,
                              reopens=None)
            return DRAIN_FAILED
        if act.pending:
            discard_notice(io_out, act.count)
            if on_discard is not None:
                on_discard(gate=act.gate, epoch=act.epoch, phase="pre_render",
                           count=act.count)
        app, run = build()
        arm_activation(app, act, fd)
        result = run()
        if result is not STALE:
            return result
        if not act.safe:
            # A channel could not be cleared while the gate was drawing.
            drain_failed_notice(io_out)
            if on_drain_fail is not None:
                on_drain_fail(gate=act.gate, epoch=act.epoch,
                              phase="post_render", reason=act.reason,
                              errno_name=act.errno_name,
                              typeahead_cleared=act.typeahead_cleared,
                              reopens=None)
            return DRAIN_FAILED
        reopens += 1
        if reopens >= GATE_REOPEN_LIMIT:
            # Check the limit BEFORE any reporting, so exactly one notice and
            # one event fire for this final attempt.
            gate_abandoned_notice(io_out)
            if on_drain_fail is not None:
                on_drain_fail(gate=act.gate, epoch=act.epoch,
                              phase="post_render", reason="reopen_limit",
                              errno_name=None, typeahead_cleared=None,
                              reopens=GATE_REOPEN_LIMIT)
            return DRAIN_FAILED
        discard_notice(io_out, act.count)
        if on_discard is not None:
            on_discard(gate=act.gate, epoch=act.epoch, phase="post_render",
                       count=act.count)
        # Loop back to the TOP: every re-open is a full gate open — drain, a
        # new Activation, a fresh widget, exactly one handler.


# --------------------------------------------------------------------------- #
# Conversation input (prompt_toolkit).                                         #
# --------------------------------------------------------------------------- #

# Sentinels returned by prompt_user. EOF ends the conversation (Ctrl-D / exhausted
# input); CANCEL means the editor was dismissed and the caller re-prompts. Both are
# distinct from a blank line (""), which the caller also re-prompts on.
CANCEL = object()
EOF = object()

# Spelled out inline so the submit/newline keys are always visible (the bottom
# toolbar doesn't render in every terminal).
INPUT_HINT = "Enter to send · Ctrl+J or Alt+Enter for a new line"


def build_key_bindings():
    """prompt_toolkit bindings giving Claude/Codex-CLI parity: Enter submits;
    Ctrl+J and Alt+Enter insert a newline.

    prompt_toolkit has no Shift+Enter key constant (terminals send the same byte
    for Enter and Shift+Enter unless the Kitty protocol is active), so Shift+Enter
    can't be bound by name — Ctrl+J and Alt+Enter are the portable newline keys.
    A terminal can be configured to send Alt+Enter/ESC+Enter for Shift+Enter (e.g.
    VS Code / iTerm2 keymaps), which then newlines here, exactly like Claude Code's
    /terminal-setup."""
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("c-j")              # Ctrl+J (LF) — portable
    @kb.add("escape", "enter")  # Alt/Meta+Enter — portable
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return kb


def _default_prompt_session():
    from prompt_toolkit import PromptSession
    return PromptSession()


def prompt_user(io_in, io_out, header=None, session_factory=None, gate=None,
                on_discard=None, on_drain_fail=None):
    """Unified conversation input.

    On a real terminal: a prompt_toolkit multiline editor — Enter submits,
    Shift+Enter/Ctrl+J/Alt+Enter insert a newline, full line editing + history.
    Off a terminal (piped / tests): plain `io_in.readline()`.

    Returns the entered text (possibly '' for a blank line); EOF when input is
    exhausted / Ctrl-D (end of conversation); or CANCEL when the editor was
    dismissed. Ctrl-C propagates (the loop treats it as an abort).

    PROTECTION IS OPT-IN AND KEYED ON `gate`. Only a call that names a
    consequential gate runs the activation boundary; stale input is then
    discarded before and at first render, `on_discard`/`on_drain_fail` report
    it, and DRAIN_FAILED is returned when a stale-input channel could not be
    cleared. With `gate=None` — the ordinary context and turn prompts — the
    behavior is exactly what it has always been: no drain, no notice, no loop,
    and NEVER DRAIN_FAILED. That matters because those callers consume the
    result as text (`reply.strip()`), so a sentinel they never asked for would
    raise instead of re-prompting.

    `build_key_bindings` is deliberately untouched, so prompt_toolkit's default
    bracketed-paste handler keeps inserting a framed paste whole once the editor
    is open and activated."""
    with trace_store.user_wait("prompt_user") as _span:
        return _prompt_user_inner(io_in, io_out, header, session_factory, gate,
                                  on_discard, on_drain_fail, _span)


def _prompt_user_inner(io_in, io_out, header, session_factory, gate,
                       on_discard, on_drain_fail, span):
    """`prompt_user`'s body, wrapped by the user-wait span (P15). `span` carries
    the termination outcome so a dismissed or EOF'd prompt closes its span with
    the truth rather than defaulting to `answered`."""
    if not (is_tty(io_in) and is_tty(io_out)):
        line = io_in.readline()
        if line == "":
            span.outcome = "eof"
            return EOF  # no trailing newline => genuine end of input
        return line.rstrip("\n")  # a blank line is "\n" => "" (re-prompt, not EOF)
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.styles import Style
    # Build a clear, multi-line prompt: the question, a dim key hint, then the
    # input marker — all visible right at the cursor (no reliance on a toolbar).
    head = colorize(header, CYAN, True) if header else ""
    hint = colorize(INPUT_HINT, DIM, True)
    message = ANSI((head + "\n" if head else "") + hint + "\n" + GREEN + "› " + RESET)
    # A blank bottom toolbar reserves the terminal's last row, so the input line
    # never sits flush against the bottom edge (margin to read/type). Styled with
    # 'noreverse' so it's invisible margin, not a dark status bar.
    pad_style = Style.from_dict({"bottom-toolbar": "noreverse",
                                 "bottom-toolbar.text": "noreverse"})

    def _open():
        """One editor attempt: (application, run). A FRESH PromptSession every
        time, so a re-opened gate is armed exactly once."""
        session = (session_factory or _default_prompt_session)()

        def _run():
            try:
                return session.prompt(
                    message,
                    multiline=True,
                    key_bindings=build_key_bindings(),
                    prompt_continuation=lambda width, line_number, soft: "  ",
                    bottom_toolbar=lambda: " ",
                    style=pad_style,
                )
            except EOFError:  # Ctrl-D on an empty buffer
                return EOF
            # KeyboardInterrupt (Ctrl-C) intentionally propagates -> abort.

        return getattr(session, "app", None), _run

    if gate is None:
        # Legacy path, byte-identical to before the boundary existed: the
        # ordinary context and turn prompts are not gates, have no caller that
        # maps DRAIN_FAILED, and must never be handed one.
        _app, run = _open()
        text = run()
    else:
        text = _run_gate(io_in, io_out, gate, on_discard, on_drain_fail, _open)
        if text is DRAIN_FAILED:
            return text
    if text is EOF or text is CANCEL:
        return text
    return (text or "").rstrip("\n")


def format_relative_time(epoch, now):
    """Compact relative timestamp for picker rows: 'just now', '5m ago',
    '3h ago', '2d ago', '3w ago', '5mo ago', '2y ago'. ALWAYS relative — no
    absolute date for old sessions (the approved picker contract). `now` is
    passed in (not read from the clock) so picker labels are deterministic under
    test. A missing/zero epoch yields 'unknown'."""
    if not epoch:
        return "unknown"
    delta = now - epoch
    if delta < 0:
        delta = 0
    if delta < 45:
        return "just now"
    minutes = int(delta // 60)
    if minutes < 60:
        return "%dm ago" % max(1, minutes)
    hours = int(delta // 3600)
    if hours < 24:
        return "%dh ago" % hours
    days = int(delta // 86400)
    if days < 7:
        return "%dd ago" % days
    weeks = int(delta // 604800)
    if weeks < 5:
        return "%dw ago" % weeks
    months = int(delta // 2592000)  # 30-day months
    if months < 12:
        return "%dmo ago" % max(1, months)
    years = int(delta // 31536000)  # 365-day years
    return "%dy ago" % max(1, years)


def confirm(prompt, ask_fn=None, default=True, io_in=None, io_out=None,
            gate=None, on_discard=None, on_drain_fail=None):
    """Yes/No gate. On a TTY: questionary.confirm. `ask_fn` is injectable for tests
    and returns a bool (or None, treated as False). `default` is the accept-on-
    Return answer and stays True so every existing caller is unchanged; the
    review gate passes default=False so a stray Return can never approve.

    Passing real terminal `io_in`/`io_out` opts the call into the activation
    boundary and can return DRAIN_FAILED, which the caller MUST map. With an
    injected `ask_fn` or a non-terminal stream the behavior is exactly what it
    has always been — no drain, no notice, no loop."""
    with trace_store.user_wait("confirm") as span:
        if not _protected(io_in, io_out, ask_fn):
            if ask_fn is None:
                import questionary
                ask_fn = lambda: questionary.confirm(
                    prompt, default=default).ask()
            raw = ask_fn()
            if raw is None:
                span.outcome = "cancelled"
            return bool(raw)
        import questionary

        def _open():
            question = questionary.confirm(prompt, default=default)
            return question.application, question.ask

        answer = _run_gate(io_in, io_out, gate, on_discard, on_drain_fail,
                           _open)
        if answer is DRAIN_FAILED:
            span.outcome = "drain_failed"
            return answer
        if answer is None:
            span.outcome = "cancelled"
        return bool(answer)


def select(prompt, choices, ask_fn=None, io_in=None, io_out=None, gate=None,
           on_discard=None, on_drain_fail=None):
    """Single-choice gate. `choices` is a list of (key, label) pairs; the first
    choice is the highlighted default (questionary points at the first
    non-disabled choice when no `default` is given — verified against
    questionary 2.1.1's InquirerControl._init_choices). On a TTY:
    questionary.select. `ask_fn` is injectable for tests and returns a key.
    Returns the chosen key, or None when the prompt was dismissed (callers pick
    their own safe fallback).

    Passing real terminal `io_in`/`io_out` opts the call into the activation
    boundary and can return DRAIN_FAILED, which the caller MUST map. With an
    injected `ask_fn` or a non-terminal stream the behavior is unchanged."""
    with trace_store.user_wait("select") as span:
        if not _protected(io_in, io_out, ask_fn):
            if ask_fn is None:
                import questionary
                ask_fn = lambda: questionary.select(
                    prompt,
                    choices=[questionary.Choice(label, value=key)
                             for key, label in choices]).ask()
            picked = ask_fn()
            if picked is None:
                span.outcome = "cancelled"
            return picked
        import questionary

        def _open():
            question = questionary.select(
                prompt,
                choices=[questionary.Choice(label, value=key)
                         for key, label in choices])
            return question.application, question.ask

        picked = _run_gate(io_in, io_out, gate, on_discard, on_drain_fail,
                           _open)
        if picked is DRAIN_FAILED:
            span.outcome = "drain_failed"
        elif picked is None:
            span.outcome = "cancelled"
        return picked


def multiselect(prompt, choices, selected=(), ask_fn=None):
    """Multi-choice gate (mirrors `select`). `choices` is a list of (key, label)
    pairs; `selected` is the set of keys pre-checked when the prompt opens. On a
    TTY: questionary.checkbox. `ask_fn` is injectable for tests and returns the
    list of chosen keys. Returns that list, or None when the prompt was
    dismissed (callers pick their own safe fallback — never an empty set)."""
    if ask_fn is None:
        import questionary
        checked = set(selected or ())
        ask_fn = lambda: questionary.checkbox(
            prompt,
            choices=[questionary.Choice(label, value=key,
                                        checked=key in checked)
                     for key, label in choices]).ask()
    return ask_fn()
