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

Python 3.9+. Does not import co_plan_file.py.
"""

import os
import sys
import threading

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


def prompt_user(io_in, io_out, header=None, session_factory=None):
    """Unified conversation input.

    On a real terminal: a prompt_toolkit multiline editor — Enter submits,
    Shift+Enter/Ctrl+J/Alt+Enter insert a newline, full line editing + history.
    Off a terminal (piped / tests): plain `io_in.readline()`.

    Returns the entered text (possibly '' for a blank line); EOF when input is
    exhausted / Ctrl-D (end of conversation); or CANCEL when the editor was
    dismissed. Ctrl-C propagates (the loop treats it as an abort)."""
    if not (is_tty(io_in) and is_tty(io_out)):
        line = io_in.readline()
        if line == "":
            return EOF  # no trailing newline => genuine end of input
        return line.rstrip("\n")  # a blank line is "\n" => "" (re-prompt, not EOF)
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.styles import Style
    session = (session_factory or _default_prompt_session)()
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
    try:
        text = session.prompt(
            message,
            multiline=True,
            key_bindings=build_key_bindings(),
            prompt_continuation=lambda width, line_number, soft: "  ",
            bottom_toolbar=lambda: " ",
            style=pad_style,
        )
    except EOFError:          # Ctrl-D on an empty buffer
        return EOF
    # KeyboardInterrupt (Ctrl-C) intentionally propagates -> loop aborts cleanly.
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


def confirm(prompt, ask_fn=None):
    """Yes/No gate. On a TTY: questionary.confirm. `ask_fn` is injectable for tests
    and returns a bool (or None, treated as False)."""
    if ask_fn is None:
        import questionary
        ask_fn = lambda: questionary.confirm(prompt, default=True).ask()
    return bool(ask_fn())


def select(prompt, choices, ask_fn=None):
    """Single-choice gate. `choices` is a list of (key, label) pairs; the first
    choice is the highlighted default. On a TTY: questionary.select. `ask_fn` is
    injectable for tests and returns a key. Returns the chosen key, or None when
    the prompt was dismissed (callers pick their own safe fallback)."""
    if ask_fn is None:
        import questionary
        ask_fn = lambda: questionary.select(
            prompt,
            choices=[questionary.Choice(label, value=key)
                     for key, label in choices]).ask()
    return ask_fn()
