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


def shorten_path(path, cwd=None):
    """A short, scannable form of an intel path: relative to cwd when it sits
    under it, else '…/<basename>'. Used everywhere except the one full mention."""
    if not path:
        return path
    import os
    cwd = cwd or os.getcwd()
    try:
        rel = os.path.relpath(path, cwd)
    except ValueError:  # different drive on Windows, etc.
        return path
    if not rel.startswith(".."):
        return rel
    return "…/" + os.path.basename(path)


def turn_separator(io_out, enabled=None):
    """A faint rule between turns. No-op when not a TTY (keeps test output clean)."""
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


def render_markdown(io_out, text, enabled=None):
    """Render markdown on a TTY (Rich); write the raw text otherwise. Used for
    whole, non-streamed replies (codex) and any one-shot markdown."""
    enabled = is_tty(io_out) if enabled is None else enabled
    if not enabled:
        io_out.write(text + ("\n" if not text.endswith("\n") else ""))
        io_out.flush()
        return
    from rich.markdown import Markdown
    _rich_console(io_out).print(Markdown(text))


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

    def __init__(self, io_out, label_text, trace=None, trace_fields=None):
        self.io_out = io_out
        self.label_text = label_text
        self.trace = trace
        self.trace_fields = trace_fields or {}
        self.tty = is_tty(io_out)
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
        from rich.markdown import Markdown
        chunk = full[self._committed:point].strip("\n")
        if chunk:
            self._console.print(Markdown(chunk))
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
        md = Markdown(self._tail())
        if self._status is None:
            return md
        from rich.console import Group
        from rich.spinner import Spinner as RichSpinner
        from rich.text import Text
        return Group(md, RichSpinner("dots", text=Text(self._status, style="dim")))

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
            self.io_out.write(chunk)
            self.io_out.flush()

    def __exit__(self, *exc):
        if self.tty and self._live is not None:
            self._status = None  # never leave a tool label in the final render
            # Empty the Live region, tear it down, then print the remaining tail
            # permanently — once. Rendering the tail in the final Live frame AND
            # printing it would duplicate it; clearing first avoids that.
            tail = self._tail().strip("\n")
            self._committed = len("".join(self.buf))  # _tail() now empty
            self._live.update(self._render())
            self._live.__exit__(*exc)
            if tail:
                from rich.markdown import Markdown
                self._console.print(Markdown(tail))
        else:
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
