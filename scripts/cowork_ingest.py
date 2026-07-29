#!/usr/bin/env python3
"""D1: read-only, fallible ingestion of the CONTROLLERS' own session logs.

Cowork does not run the agents' commands, so it cannot observe tool work
directly. It has two choices: believe what an agent says it did, or read what
the controller recorded. An agent that omits a failure erases it from the first
source and cannot erase it from the second, so verification and tool facts are
taken from here and agent prose is only ever a claim to be checked against it.

THREE HARD RULES, all asserted rather than assumed:

1. READ-ONLY. Nothing here writes, moves or renames a controller file. Every
   ingested file's content digest is taken before and after the read and
   returned, so the property is evidence in the record rather than a promise in
   a docstring.
2. NEVER RAISES. Every public function returns a result object carrying a
   `state` in {ok, missing, unreadable, truncated, unrecognised}. A log cowork
   cannot parse yields `unknown` figures — never a guess, and never a broken
   run. A run with every controller log deleted mid-flight still completes.
3. MINTS NOTHING. Ingestion emits id-free OBSERVATIONS keyed by their natural
   key `(controller_session_id, tool_call_id)`. `cowork_ledger.reconcile_
   attempts` is the sole place that turns an observation into an identified,
   durable ledger record (P3) — so replaying a log cannot renumber history.

CONTENT-FREE. Command text is reduced to a sanitized identity (the program and
its subcommand-shaped arguments); outputs become counters, flags and digests.
No prompt, reply, file body or command output is ever carried out of here.

We are reading someone else's private log format. Claude's and Codex's layouts
can change without notice, so every reader shape-CHECKS before it parses and an
unrecognised layout degrades to `unrecognised` rather than to a wrong number.

Python 3.9+, stdlib only.
"""

import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex

# Ingestion states. `ok` is the only one that yields figures; every other state
# yields `unknown` and names itself, so a caller can report WHY a figure is
# missing instead of printing a confident zero.
STATES = ("ok", "missing", "unreadable", "truncated", "unrecognised")

# Tool intents. A tool call is classified by what it does to the world, which is
# what makes "this run mutated the tree and never restored it" reportable.
INTENTS = ("read", "mutate", "verify", "search", "network", "other")

# Verification adjudications (criterion 3). `unknown` is a real outcome: a
# command whose exit status cannot be read is not a pass.
ADJUDICATIONS = ("pass", "fail", "unresolved", "unknown")

# What KIND of failure it was. Reporting "12 failures" without these makes a
# missing system dependency indistinguishable from a real product regression,
# so the same environment mistake can be repeated across roles unnoticed.
FAILURE_CLASSES = ("product_regression", "new_product_defect", "test_harness",
                   "environment_dependency", "flaky", "mutation", "unknown")

_ENVIRONMENT_SIGNALS = (
    "command not found", "no such file or directory", "permission denied",
    "connection refused", "could not resolve host", "network is unreachable",
    "modulenotfounderror", "importerror", "cannot find module",
    "unable to locate package", "address already in use", "agent returned an "
    "error", "no space left on device",
)
_HARNESS_SIGNALS = ("collection error", "conftest", "fixture", "usage:",
                    "unrecognized arguments", "error: unrecognized")
_FLAKY_SIGNALS = ("timed out waiting", "connection reset", "temporarily "
                  "unavailable", "resource temporarily")


def classify_failure(text, adjudication=None, intent=None):
    """Name the KIND of failure from what the log actually says.

    Deliberately conservative: `unknown` when the output gives no signal, rather
    than guessing `product_regression` and putting a real environment problem on
    the builder's account.
    """
    if adjudication == "pass":
        return None
    lowered = (text or "").lower()
    if adjudication == "unresolved":
        return "flaky" if any(sig in lowered for sig in _FLAKY_SIGNALS) \
            else "unknown"
    for signal in _ENVIRONMENT_SIGNALS:
        if signal in lowered:
            return "environment_dependency"
    for signal in _HARNESS_SIGNALS:
        if signal in lowered:
            return "test_harness"
    for signal in _FLAKY_SIGNALS:
        if signal in lowered:
            return "flaky"
    if re.search(r"\b(ran|collected)\s+0\s+(tests?|items?)\b", lowered):
        # It exited cleanly having verified nothing. That is a harness or
        # selection problem, not evidence of a product defect.
        return "test_harness"
    if intent == "mutate":
        return "mutation"
    if re.search(r"\b(assertionerror|failed|failures?=\d+|expected)\b",
                 lowered):
        return "product_regression"
    return "unknown"

# Commands whose PURPOSE is verification. Matching is on the resolved program
# plus its first argument, never on a substring of free text, so a command that
# merely mentions "test" in a path is not counted as a test run.
_VERIFY_PROGRAMS = {
    "pytest", "unittest", "nose", "tox", "jest", "vitest", "mocha", "phpunit",
    "rspec", "ctest", "gradlew", "mypy", "ruff", "flake8", "pylint", "eslint",
    "tsc", "shellcheck", "cargo", "go", "npm", "yarn", "pnpm", "make",
}
_VERIFY_SUBCOMMANDS = {
    ("cargo", "test"), ("cargo", "clippy"), ("cargo", "check"),
    ("go", "test"), ("go", "vet"), ("npm", "test"), ("npm", "run"),
    ("yarn", "test"), ("pnpm", "test"), ("make", "test"), ("make", "check"),
    ("python", "-m"), ("python3", "-m"),
}

_MUTATING_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
_READING_TOOLS = {"Read", "NotebookRead"}
_SEARCH_TOOLS = {"Grep", "Glob", "Search"}
_NETWORK_TOOLS = {"WebFetch", "WebSearch"}

_EXIT_RE = re.compile(
    r"\bexit(?:ed)?(?:\s+with)?(?:\s+|_)?(?:code|status)?"
    r"\s*[:=]?\s*(\d{1,3})\b",
    re.IGNORECASE,
)
_TEST_COUNT_RE = re.compile(
    r"\b(?:ran|collected)\s+(\d+)\s+(?:tests?|items?)\b", re.IGNORECASE)
_CODEX_WALL_RE = re.compile(r"Wall time\s+([0-9.]+)\s*seconds",
                            re.IGNORECASE)


class Result:
    """What every public function here returns.

    `state` is always present and is the first thing a caller should read: only
    `ok` carries figures. `digest_before`/`digest_after` are the read-only
    assertion — identical values are the evidence that ingestion did not touch
    the file it read.
    """

    def __init__(self, state, path=None, controller=None, **fields):
        self.state = state if state in STATES else "unrecognised"
        self.path = path
        self.controller = controller
        self.turns = fields.get("turns") or []
        self.tool_activity = fields.get("tool_activity") or []
        self.verification_attempts = fields.get("verification_attempts") or []
        self.mutations = fields.get("mutations") or []
        self.digest_before = fields.get("digest_before")
        self.digest_after = fields.get("digest_after")
        self.lines_read = fields.get("lines_read", 0)
        self.lines_unparsed = fields.get("lines_unparsed", 0)
        self.evidence_lost = fields.get("evidence_lost", False)
        self.controller_session_id = fields.get("controller_session_id")
        self.detail = fields.get("detail")

    @property
    def ok(self):
        return self.state == "ok"

    @property
    def read_only_verified(self):
        """True only when the file's content is provably unchanged across the
        read. `None` when there is nothing to compare (a missing file)."""
        if self.digest_before is None and self.digest_after is None:
            return None
        return self.digest_before == self.digest_after

    def as_dict(self):
        return {
            "state": self.state,
            "path": self.path,
            "controller": self.controller,
            "controller_session_id": self.controller_session_id,
            "turns": self.turns,
            "tool_activity": self.tool_activity,
            "verification_attempts": self.verification_attempts,
            "mutations": self.mutations,
            "digest_before": self.digest_before,
            "digest_after": self.digest_after,
            "read_only_verified": self.read_only_verified,
            "lines_read": self.lines_read,
            "lines_unparsed": self.lines_unparsed,
            "evidence_lost": self.evidence_lost,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------- #
# Locating a controller's log.                                                #
#                                                                             #
# Roots are env-overridable (mirroring COWORK_SESSIONS_ROOT) so fixtures and   #
# tests never read, or even open, a real log.                                 #
# --------------------------------------------------------------------------- #


def claude_projects_root():
    return (os.environ.get("COWORK_CLAUDE_PROJECTS_ROOT")
            or os.path.expanduser(os.path.join("~", ".claude", "projects")))


def codex_sessions_root():
    return (os.environ.get("COWORK_CODEX_SESSIONS_ROOT")
            or os.path.expanduser(os.path.join("~", ".codex")))


def claude_project_slug(cwd):
    """Claude names a project directory after the cwd with every non-alnum run
    collapsed to '-' (e.g. /Users/x/code/cowork -> -Users-x-code-cowork)."""
    return re.sub(r"[^A-Za-z0-9]+", "-", cwd or os.getcwd())


def locate_claude_log(session_id, cwd=None, root=None):
    """Path of a Claude session JSONL, or None. The slug is derived from cwd,
    but a session recorded under a different cwd is still found by scanning the
    project dirs for the id — a worktree run changes cwd mid-session, so
    trusting the slug alone would lose exactly those logs."""
    if not session_id:
        return None
    root = root or claude_projects_root()
    direct = os.path.join(root, claude_project_slug(cwd or os.getcwd()),
                          "%s.jsonl" % session_id)
    if os.path.exists(direct):
        return direct
    try:
        matches = sorted(glob.glob(os.path.join(root, "*",
                                                "%s.jsonl" % session_id)))
    except OSError:
        return None
    return matches[0] if matches else None


def locate_codex_log(thread_id, root=None):
    """Path of a Codex rollout for `thread_id`, or None. Live sessions are
    searched first, then archived_sessions — an archived thread's evidence is
    no less real for having been filed away."""
    if not thread_id:
        return None
    root = root or codex_sessions_root()
    for subdir in ("sessions", "archived_sessions"):
        base = os.path.join(root, subdir)
        pattern = "rollout-*-%s.jsonl" % thread_id
        try:
            matches = [p for p in _walk_files(base)
                       if fnmatch.fnmatch(os.path.basename(p), pattern)]
        except OSError:
            matches = []
        if matches:
            return sorted(matches)[0]
    return None


def _walk_files(base):
    out = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            out.append(os.path.join(dirpath, name))
    return out


# --------------------------------------------------------------------------- #
# Reading.                                                                    #
# --------------------------------------------------------------------------- #


def _digest(path):
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, TypeError, ValueError):
        return None


def _read_records(path):
    """Read a JSONL log into `(records, state, stats)`.

    A final unparseable line is TRUNCATION, not corruption: an append-only log
    read while it is being written ends mid-line routinely. The records before
    it are retained and the tail is flagged `evidence_lost`, because the honest
    statement is "everything up to here, and something after it we cannot see"
    — not "unreadable", and certainly not "this is all there was".
    """
    stats = {"lines_read": 0, "lines_unparsed": 0, "evidence_lost": False}
    if not path or not os.path.exists(path):
        return [], "missing", stats
    try:
        with open(path, "r", errors="replace") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return [], "unreadable", stats
    records = []
    total = len(raw_lines)
    for index, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        stats["lines_read"] += 1
        try:
            obj = json.loads(line)
        except ValueError:
            stats["lines_unparsed"] += 1
            if index == total - 1:
                stats["evidence_lost"] = True
                return records, "truncated", stats
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, "ok", stats


# --------------------------------------------------------------------------- #
# Command sanitization (content-free).                                        #
# --------------------------------------------------------------------------- #


_OPERATORS = ("&&", "||", ";", "|", "&")


def _tokenize(text):
    """Quote-aware tokens for one command line, or None when it will not parse.

    Splitting on operators with a regex is wrong the moment a quoted argument
    contains one: `rg 'write|patch|apply'` is ONE search, not four commands, and
    naive splitting turns its alternation into a list of fictitious programs
    (one of which then looks like a test run). shlex respects the quotes.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quotes: no honest tokenization exists.
        return None


def split_segments(text):
    """Split a command line into its separately-executed segments.

    Real commands are compound: `cd repo && python -m unittest x | tail`. A
    reader that looks only at the first program sees `cd` and concludes nothing
    was verified, which is how a real test run becomes invisible. Every segment
    is classified, and the line's intent is the strongest one found.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    tokens = _tokenize(text)
    if tokens is None:
        # Fall back to the whole line as one segment rather than guessing at
        # boundaries we cannot see.
        return [text.strip()]
    segments = []
    current = []
    for token in tokens:
        if token in _OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def sanitize_command(text):
    """Reduce a command line to a content-free IDENTITY.

    Keeps each segment's program and its leading option/subcommand tokens;
    replaces everything that could carry content (paths, quoted strings, URLs,
    values) with a placeholder. `pytest -q tests/test_secret_name.py` becomes
    `pytest -q <arg>`: enough to say two runs are the same command, never enough
    to leak what was run against.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    parts = [_sanitize_segment(seg) for seg in split_segments(text)]
    parts = [p for p in parts if p]
    return " && ".join(parts) if parts else None


# Shell noise that is not part of a command's IDENTITY: redirections, and the
# fragments sanitization leaves behind when it splits `2>&1`.
_REDIRECTION = re.compile(r"^(?:\d?>{1,2}|<|&\d?|\d)$")


def command_fingerprint(text):
    """A stable identity for "the same command", across shell wrapping.

    Exact sanitized-identity matching could not join a builder's claim to its
    own log entry: the log records what actually ran —
    `python -m unittest suite 2>&1 | tail -3`, which sanitizes to
    `python -m unittest suite 2 <arg> && tail -3` — while the status records the
    bare `python -m unittest suite`. The two never compared equal, so every
    claim came back `self_reported` even though its run was right there in the
    log. That is a matching failure reported as an absence of evidence, which is
    worse than either.

    The fingerprint keeps the segment that DOES the work — the verification one
    when there is one, else the first — and drops redirections, placeholders and
    pipeline tails, all of which change how output is plumbed and not what ran.
    """
    identity = sanitize_command(text) if text else None
    if not identity:
        return None
    segments = [seg for seg in identity.split(" && ") if seg.strip()]
    if not segments:
        return None
    chosen = None
    for segment in segments:
        intent, is_verify = _classify_segment(segment)
        if is_verify:
            chosen = segment
            break
    if chosen is None:
        # No verification segment: the first real program is the identity, so a
        # `cd` prefix does not become the command.
        for segment in segments:
            if _classify_segment(segment)[0] != "read":
                chosen = segment
                break
        chosen = chosen or segments[0]
    # Drop placeholders and redirections. The placeholder is matched in both
    # forms because fingerprinting an ALREADY-sanitized identity re-sanitizes
    # `<arg>` into a bare `arg`, which would otherwise survive as a token and
    # stop a claim matching its own attempt.
    tokens = [t for t in chosen.split()
              if t.strip("<>") not in ("arg", "")
              and not _REDIRECTION.match(t)]
    if not tokens:
        return None
    fingerprint = " ".join(tokens)
    # An INLINE SCRIPT is the command. `python -c "<script>"` sanitizes to
    # `python -c`, so every inline-script command in an inventory collapsed to
    # one fingerprint and one attempt could corroborate a different assertion
    # that never ran. The script body is what distinguishes them, so a short
    # digest of it joins the fingerprint. Content-free: a digest, never the
    # body.
    if "-c" in tokens or "--command" in tokens:
        body = _inline_script_body(text)
        if body:
            fingerprint += " #" + hashlib.sha256(
                body.encode("utf-8")).hexdigest()[:10]
    return fingerprint


def _inline_script_body(text):
    """The script passed to `-c`, or None."""
    tokens = _tokenize(text) if isinstance(text, str) else None
    if not tokens:
        return None
    for index, token in enumerate(tokens):
        if token in ("-c", "--command") and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def has_pipeline(text):
    """Whether a command pipes, which MASKS the producer's exit status: the
    shell reports the last stage's. `pytest | tail` exits 0 when pytest fails.
    Recorded on the attempt so a pass read off a piped command is qualified
    rather than trusted. Quote-aware, so a `|` inside a regex is not a pipe."""
    if not isinstance(text, str):
        return False
    tokens = _tokenize(text)
    if tokens is None:
        return False
    return "|" in tokens


_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _sanitize_segment(segment):
    tokens = segment if isinstance(segment, list) else segment.strip().split()
    # A leading `VAR=value` prefix sets the environment; it is not the program.
    # Reading it as one made `COWORK_SESSIONS_ROOT=x python …` and `python …`
    # different commands, so a claim could not be joined to its own run.
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    out = [os.path.basename(tokens[0])]
    kept = 0
    for token in tokens[1:]:
        # An OPTION or a short bare word is structure, not content, and is what
        # makes two runs of the same command comparable. Anything path-shaped or
        # long enough to carry a value is replaced.
        if (kept < 4 and re.fullmatch(r"-{0,2}[A-Za-z0-9][\w.:=-]*", token)
                and not _looks_like_path(token)):
            out.append(token)
            kept += 1
        else:
            out.append("<arg>")
    # Collapse a run of placeholders so `a <arg> <arg> <arg>` and `a <arg>`
    # compare equal as command identities.
    collapsed = []
    for token in out:
        if token == "<arg>" and collapsed and collapsed[-1] == "<arg>":
            continue
        collapsed.append(token)
    return " ".join(collapsed)


def _looks_like_path(token):
    return ("/" in token or token.startswith(".") or token.endswith(".py")
            or token.endswith(".js") or token.endswith(".ts"))


# Intent precedence when a compound line does several things at once. A line
# that mutates the tree AND runs tests is reported as verification, because
# that is the claim being made about it; the mutation is recorded separately.
_INTENT_RANK = {"other": 0, "read": 1, "search": 2, "network": 3,
                "mutate": 4, "verify": 5}


def classify_command(command_identity):
    """Whether a command's PURPOSE is verification, and its overall intent.

    Every segment is classified and the strongest result wins, so a test run
    hidden behind a `cd` is still a test run.
    """
    if not command_identity:
        return "other", False
    best = ("other", False)
    for segment in command_identity.split(" && "):
        intent, is_verify = _classify_segment(segment)
        if _INTENT_RANK.get(intent, 0) > _INTENT_RANK.get(best[0], 0):
            best = (intent, is_verify)
        elif is_verify:
            best = (intent, True)
    return best


def _classify_segment(segment):
    parts = segment.split()
    program = parts[0] if parts else ""
    program = program[:-3] if program.endswith(".sh") else program
    first_arg = next((p for p in parts[1:] if not p.startswith("-")), None)
    if program in ("python", "python3", "py") and "-m" in parts:
        index = parts.index("-m") + 1
        module = parts[index] if index < len(parts) else ""
        if module.startswith(("unittest", "pytest", "tox", "mypy", "ruff",
                              "flake8", "pylint")):
            return "verify", True
        return "other", False
    if (program, first_arg) in _VERIFY_SUBCOMMANDS:
        return "verify", True
    if program in _VERIFY_PROGRAMS:
        return "verify", True
    if program == "git":
        if first_arg in ("apply", "checkout", "restore", "reset", "clean",
                         "commit", "merge", "rebase"):
            return "mutate", False
        return "read", False
    if program in ("rm", "mv", "cp", "sed", "tee", "install", "mkdir",
                   "touch", "chmod"):
        return "mutate", False
    if program in ("cat", "head", "tail", "ls", "wc", "diff", "cd", "echo"):
        return "read", False
    if program in ("grep", "rg", "find", "ag", "rtk"):
        return "search", False
    if program in ("curl", "wget"):
        return "network", False
    return "other", False


def parse_exit_status(text):
    """The exit status a controller reported in its tool output, or None.

    None is NOT zero. A command whose status cannot be read has an unknown
    outcome, and calling that a pass is precisely the fabrication D1 forbids.
    """
    if not isinstance(text, str):
        return None
    match = _EXIT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_test_count(text):
    """The number of tests a run reported executing, or None when unstated.

    This is what makes "exit 0" insufficient: a suite that collected ZERO tests
    exits 0 and has verified nothing.
    """
    if not isinstance(text, str):
        return None
    match = _TEST_COUNT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def adjudicate(exit_status, executed_count=None, expected_count=None,
               expected_polarity=None, timed_out=False, interrupted=False):
    """Decide one verification attempt's outcome from LOG facts.

    Exit status alone never certifies quality:
      - a timeout or an interrupt is `unresolved`, a TERMINAL state. A later
        green run is a different attempt and never closes this one — otherwise
        "re-run until it passes" would launder a hang into a pass.
      - `expected_polarity='pass_on_nonzero'` inverts the test, so a negative
        assertion ("this must fail") is adjudicated on what it actually asserts.
      - an executed count of 0, or one short of `expected_count`, FAILS even on
        exit 0: a suite that ran nothing verified nothing.
      - an unreadable status is `unknown`, never a pass.
    """
    if timed_out or interrupted:
        return "unresolved"
    if exit_status is None:
        return "unknown"
    if expected_polarity == "pass_on_nonzero":
        passed = exit_status != 0
    else:
        passed = exit_status == 0
    if not passed:
        return "fail"
    if executed_count is not None:
        if executed_count == 0:
            return "fail"
        if expected_count is not None and executed_count < expected_count:
            return "fail"
    elif expected_count is not None:
        return "unknown"
    return "pass"


# --------------------------------------------------------------------------- #
# Claude.                                                                     #
# --------------------------------------------------------------------------- #


def _claude_shape_ok(records):
    """A Claude log is recognised when it carries at least one assistant/user
    record with a sessionId and a timestamp. Checked BEFORE parsing so a format
    change surfaces as `unrecognised` rather than as a wrong number."""
    for rec in records:
        if (rec.get("type") in ("assistant", "user")
                and rec.get("sessionId") and rec.get("timestamp")):
            return True
    return False


def ingest_claude(path):
    """Read one Claude session JSONL into content-free observations."""
    digest_before = _digest(path)
    records, state, stats = _read_records(path)
    if state in ("missing", "unreadable"):
        return Result(state, path=path, controller="claude",
                      digest_before=digest_before,
                      digest_after=_digest(path), **stats)
    if not _claude_shape_ok(records):
        return Result("unrecognised", path=path, controller="claude",
                      digest_before=digest_before, digest_after=_digest(path),
                      detail="no assistant/user record with sessionId",
                      **stats)

    session_id = next((r.get("sessionId") for r in records
                       if r.get("sessionId")), None)
    turns = []
    calls = {}      # tool_use_id -> pending call
    activity = []
    attempts = []
    mutations = []

    for rec in records:
        rtype = rec.get("type")
        message = rec.get("message")
        message = message if isinstance(message, dict) else {}
        if rtype == "assistant":
            usage = message.get("usage")
            if isinstance(usage, dict):
                turns.append({
                    "controller": "claude",
                    "controller_session_id": rec.get("sessionId"),
                    "timestamp": rec.get("timestamp"),
                    # Claude reports usage PER assistant message, so this is
                    # already a per-turn figure and needs no differencing.
                    "usage_scope": "turn_native",
                    "usage": _int_fields(usage),
                    "model": message.get("model"),
                })
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                command = None
                piped = False
                # Initialized BEFORE the branch. Reading it unconditionally
                # while only assigning it inside the isinstance check raised
                # UnboundLocalError on a Bash tool_use whose input is null or
                # not a dict — an ingester that promises never to raise, raising
                # on malformed input.
                fingerprint = None
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    raw = tool_input.get("command")
                    command = sanitize_command(raw)
                    piped = has_pipeline(raw)
                    # Computed from the RAW text and PERSISTED. Deriving it
                    # later from `command_identity` was too late: sanitization
                    # has already replaced the inline script with a placeholder,
                    # so `python -c "<script>"` and a different `-c` script
                    # produced different digests than the same commands
                    # fingerprinted raw — and an attempt could never match its
                    # own claim.
                    fingerprint = command_fingerprint(raw)
                intent, is_verify = _claude_intent(name, command)
                calls[block.get("id")] = {
                    # The controller's OWN turn boundary. Without it the record
                    # fell back to each call's timestamp, which made every call
                    # its own "turn" — 519 calls became 519 turns, so a turn
                    # that used sixty tools was invisible, which is the only
                    # thing per-turn aggregation is for.
                    "turn_id": rec.get("requestId") or rec.get("parentUuid"),
                    "tool_call_id": block.get("id"),
                    "controller_session_id": rec.get("sessionId"),
                    "tool_name": name,
                    "command_identity": command,
                    "command_fingerprint": fingerprint,
                    "intent": intent,
                    "is_verification": is_verify,
                    "pipeline": piped,
                    "started_at": rec.get("timestamp"),
                }
        elif rtype == "user":
            tool_result = rec.get("toolUseResult")
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                call = calls.pop(block.get("tool_use_id"), None)
                if call is None:
                    continue
                observation = _claude_finish(call, block, tool_result,
                                             rec.get("timestamp"))
                activity.append(observation)
                if call["is_verification"]:
                    attempts.append(observation)
                if call["intent"] == "mutate":
                    mutations.append({
                        "controller_session_id": call["controller_session_id"],
                        "tool_call_id": call["tool_call_id"],
                        "tool_name": call["tool_name"],
                        "timestamp": rec.get("timestamp"),
                        "restored": None,
                    })

    # A call with no result never completed. It is recorded as unresolved
    # rather than dropped: an interrupted command is exactly the kind of thing
    # a summary would quietly omit.
    for call in calls.values():
        observation = dict(call)
        observation.update({
            "exit_status": None, "adjudication": "unresolved",
            "interrupted": True, "timed_out": False, "ended_at": None,
            "executed_count": None, "tty_stdin_mode": "unknown",
            # No output to read, so no signal to class it by. `unknown` is the
            # honest answer for a command that never came back.
            "failure_class": "unknown",
        })
        activity.append(observation)
        if call["is_verification"]:
            attempts.append(observation)

    return Result("ok" if state == "ok" else state, path=path,
                  controller="claude", controller_session_id=session_id,
                  turns=turns, tool_activity=activity,
                  verification_attempts=attempts, mutations=mutations,
                  digest_before=digest_before, digest_after=_digest(path),
                  **stats)


def _claude_intent(name, command):
    if name in _MUTATING_TOOLS:
        return "mutate", False
    if name in _READING_TOOLS:
        return "read", False
    if name in _SEARCH_TOOLS:
        return "search", False
    if name in _NETWORK_TOOLS:
        return "network", False
    if name == "Bash":
        return classify_command(command)
    return "other", False


def _claude_finish(call, block, tool_result, timestamp):
    observation = dict(call)
    text = _claude_result_text(block)
    interrupted = False
    if isinstance(tool_result, dict):
        interrupted = bool(tool_result.get("interrupted"))
        stdout = tool_result.get("stdout")
        if isinstance(stdout, str) and not text:
            text = stdout
    timed_out = bool(text and re.search(
        r"\btimed?\s?out\b|\btimeout\b", text, re.IGNORECASE))
    piped = bool(call.get("pipeline"))
    exit_status = parse_exit_status(text)
    if exit_status is None and block.get("is_error") is False and not piped:
        # An explicitly non-error result with no status line: the controller
        # says it succeeded, and that is a weaker claim than a read status —
        # recorded, but as the controller's word, not as a measured 0.
        exit_status = 0
    if block.get("is_error") is True and exit_status in (None, 0):
        exit_status = exit_status if exit_status else 1
    if piped and not _EXIT_RE.search(text or ""):
        # The shell reports the LAST stage's status, so `pytest | tail` exits 0
        # when pytest fails. Recording that as the producer's result is how a
        # red run becomes a green claim. Without an explicit producer status in
        # the output there is nothing to read, and `unknown` is the only honest
        # answer — the report already annotated PIPED but still let the pass
        # through, which annotated the problem instead of preventing it.
        exit_status = None
    observation.update({
        "ended_at": timestamp,
        "exit_status": exit_status,
        "is_error": bool(block.get("is_error")),
        "interrupted": interrupted,
        "timed_out": timed_out,
        "executed_count": parse_test_count(text),
        # Neither controller exposes whether the command had a TTY or an open
        # stdin (P17): Claude's toolUseResult carries only interrupted /
        # isImage / noOutputExpected / stderr / stdout. The field is present and
        # honestly `unknown` rather than invented.
        "tty_stdin_mode": "unknown",
        "output_bytes": len(text.encode("utf-8")) if text else 0,
    })
    observation["adjudication"] = adjudicate(
        observation["exit_status"],
        executed_count=observation["executed_count"],
        interrupted=interrupted, timed_out=timed_out)
    observation["failure_class"] = classify_failure(
        text, observation["adjudication"], call.get("intent"))
    return observation


def _claude_result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Codex.                                                                      #
# --------------------------------------------------------------------------- #


def _codex_shape_ok(records):
    """A Codex rollout is recognised when it carries a session_meta record AND
    at least one event_msg/response_item record."""
    has_meta = any(r.get("type") == "session_meta" for r in records)
    has_body = any(r.get("type") in ("event_msg", "response_item")
                   for r in records)
    return has_meta and has_body


def ingest_codex(path):
    """Read one Codex rollout into content-free observations."""
    digest_before = _digest(path)
    records, state, stats = _read_records(path)
    if state in ("missing", "unreadable"):
        return Result(state, path=path, controller="codex",
                      digest_before=digest_before,
                      digest_after=_digest(path), **stats)
    if not _codex_shape_ok(records):
        return Result("unrecognised", path=path, controller="codex",
                      digest_before=digest_before, digest_after=_digest(path),
                      detail="no session_meta plus event body",
                      **stats)

    session_id = None
    turns = []
    calls = {}
    activity = []
    attempts = []
    mutations = []
    pending_exec_cells = {}
    open_turn = None
    cumulative_before = None
    last_totals = None

    for rec in records:
        payload = rec.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        ptype = payload.get("type")
        timestamp = rec.get("timestamp")
        if rec.get("type") == "session_meta":
            session_id = session_id or payload.get("session_id") or payload.get("id")
        elif ptype == "task_started":
            open_turn = {"turn_id": payload.get("turn_id"),
                         "started_at": timestamp, "last_sum": {}}
            cumulative_before = last_totals
        elif ptype == "token_count":
            info = payload.get("info")
            info = info if isinstance(info, dict) else {}
            last = _int_fields(info.get("last_token_usage"))
            totals = _int_fields(info.get("total_token_usage"))
            if totals:
                last_totals = totals
            if open_turn is not None and last:
                for field, value in last.items():
                    open_turn["last_sum"][field] = (
                        open_turn["last_sum"].get(field, 0) + value)
        elif ptype == "task_complete":
            if open_turn is not None:
                turns.append(_codex_turn(open_turn, session_id, timestamp,
                                         cumulative_before, last_totals))
                open_turn = None
        elif ptype in ("custom_tool_call", "function_call"):
            raw = _codex_call_command(payload)
            command = sanitize_command(raw)
            fingerprint = command_fingerprint(raw)
            name = payload.get("name")
            # A long outer `exec` call yields a cell id, then Codex records the
            # completed result under a separate `wait` function call. Preserve
            # the original command identity across that continuation instead
            # of turning one verification into an unresolved exec plus an
            # unrelated wait.
            if name == "wait":
                cell_id = _codex_cell_id(payload)
                continued = pending_exec_cells.pop(cell_id, None)
                if continued is not None:
                    calls[payload.get("call_id")] = continued
                    continue
            intent, is_verify = (classify_command(command) if command
                                 else _codex_tool_intent(name))
            calls[payload.get("call_id")] = {
                # Codex names the turn on the payload; the open task's id is
                # the fallback when a call omits it.
                "turn_id": (payload.get("turn_id")
                            or (payload.get(
                                "internal_chat_message_metadata_passthrough")
                                or {}).get("turn_id")
                            or (open_turn or {}).get("turn_id")),
                "tool_call_id": payload.get("call_id"),
                "controller_session_id": session_id,
                "tool_name": name,
                "command_identity": command,
                "command_fingerprint": fingerprint,
                "intent": intent,
                "is_verification": is_verify,
                "pipeline": has_pipeline(raw),
                "started_at": timestamp,
            }
        elif ptype in ("custom_tool_call_output", "function_call_output"):
            call = calls.pop(payload.get("call_id"), None)
            if call is None:
                continue
            yielded = _codex_yielded_cell_id(_codex_output_text(payload))
            if yielded is not None:
                pending_exec_cells[yielded] = call
                continue
            observation = _codex_finish(call, payload, timestamp)
            activity.append(observation)
            if call["is_verification"]:
                attempts.append(observation)
        elif ptype == "patch_apply_end":
            # A patch application is a MUTATION of the working tree, recorded
            # whether or not it succeeded — an unrestored mutation is what makes
            # later evidence from that tree unsafe to count.
            changes = payload.get("changes")
            mutations.append({
                "controller_session_id": session_id,
                "tool_call_id": payload.get("call_id"),
                "tool_name": "patch_apply",
                "timestamp": timestamp,
                "success": bool(payload.get("success")),
                "file_count": len(changes) if isinstance(changes, dict) else None,
                "restored": None,
            })

    if open_turn is not None:
        # A turn that started and never completed: in flight or killed. Its
        # duration is unknown, never 0.
        turns.append(_codex_turn(open_turn, session_id, None,
                                 cumulative_before, last_totals,
                                 incomplete=True))
    # A yielded exec is removed from `calls` while it waits for a later
    # `wait` call/result. If the rollout ends in that window, it is still an
    # in-flight command and must not disappear. Merge both pending stores by
    # the ORIGINAL tool-call id so a malformed/repeated cell reference cannot
    # emit the same attempt twice; when completion arrives on a later ingest,
    # the wait path preserves this id and ledger reconciliation revises the
    # same natural key.
    unfinished = {}
    for call in list(calls.values()) + list(pending_exec_cells.values()):
        key = call.get("tool_call_id")
        if key is not None:
            unfinished.setdefault(key, call)
    for call in unfinished.values():
        observation = dict(call)
        observation.update({
            "exit_status": None, "adjudication": "unresolved",
            "interrupted": True, "timed_out": False, "ended_at": None,
            "executed_count": None, "tty_stdin_mode": "unknown",
            # No output to read, so no signal to class it by. `unknown` is the
            # honest answer for a command that never came back.
            "failure_class": "unknown",
        })
        activity.append(observation)
        if call["is_verification"]:
            attempts.append(observation)

    return Result("ok" if state == "ok" else state, path=path,
                  controller="codex", controller_session_id=session_id,
                  turns=turns, tool_activity=activity,
                  verification_attempts=attempts, mutations=mutations,
                  digest_before=digest_before, digest_after=_digest(path),
                  **stats)


def _codex_turn(open_turn, session_id, ended_at, cumulative_before,
                cumulative_after, incomplete=False):
    """One Codex turn's own usage.

    The per-call `last_token_usage` values summed across the turn ARE the turn's
    cost. `total_token_usage` is the thread's running total, so its delta across
    the turn is the independent cross-check — and a disagreement between the two
    is reported rather than silently resolved in favour of one.
    """
    summed = dict(open_turn.get("last_sum") or {})
    delta = None
    if isinstance(cumulative_after, dict):
        before = cumulative_before if isinstance(cumulative_before, dict) else {}
        delta = {}
        for field, value in cumulative_after.items():
            prior = before.get(field, 0)
            if value - prior < 0:
                delta = None
                break
            delta[field] = value - prior
    scope = "turn_delta"
    if not summed and delta is None:
        scope = "unknown"
    elif delta is not None and summed and delta != summed:
        # Both readings exist and disagree: neither is silently preferred.
        scope = "incomparable"
    return {
        "controller": "codex",
        "controller_session_id": session_id,
        "turn_id": open_turn.get("turn_id"),
        "timestamp": open_turn.get("started_at"),
        "ended_at": ended_at,
        "usage_scope": scope,
        "usage": summed or None,
        "usage_cross_check": delta,
        "work_state": "in_flight" if incomplete else "complete",
    }


def _codex_tool_intent(name):
    """Classify a Codex tool call that carries no shell command, by tool name.

    Never `verify`: nothing here executed a command, so nothing here can
    certify anything."""
    name = (name or "").lower()
    if "patch" in name or "write" in name or "edit" in name:
        return "mutate", False
    if "read" in name or "view" in name or "cat" in name:
        return "read", False
    if "search" in name or "grep" in name or "find" in name:
        return "search", False
    if "web" in name or "fetch" in name or "browser" in name:
        return "network", False
    return "other", False


_CODEX_CMD_RE = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _codex_call_command(payload):
    """The shell command inside a Codex tool call, or None.

    Codex's `exec` tool does not pass a command directly: its input is a
    JavaScript snippet calling `tools.exec_command({"cmd": "..."})`. Reading the
    snippet as the command would classify every shell run as JavaScript, so the
    embedded `cmd` is extracted. When there is no such field the raw input is
    returned and sanitization reduces it as usual.
    """
    for key in ("input", "arguments"):
        raw = payload.get(key)
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            found = parsed.get("cmd") or parsed.get("command")
            if found:
                return found
        match = _CODEX_CMD_RE.search(raw)
        if match:
            try:
                return json.loads('"%s"' % match.group(1))
            except ValueError:
                return match.group(1)
    # No command field. The input is something else entirely — patch text for
    # apply_patch, a file path for a read — and reading it AS a shell line makes
    # patch prose look like a passing test run. There is no command here, and
    # saying so is the honest answer; the call is still recorded, classified by
    # its tool name.
    return None


def _codex_cell_id(payload):
    """Cell id named by a Codex wait call, normalized for dictionary use."""
    raw = payload.get("arguments")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("cell_id") is None:
        return None
    return str(parsed["cell_id"])


def _codex_yielded_cell_id(text):
    """Cell id from an outer exec result that yielded before completion."""
    if not isinstance(text, str):
        return None
    match = re.search(r"\bScript running with cell ID\s+([^\s]+)", text)
    return match.group(1) if match else None


def _codex_finish(call, payload, timestamp):
    observation = dict(call)
    text = _codex_output_text(payload)
    # Codex output has shipped in two forms: custom tools lead with
    # "Script completed", while function_call/exec_command reports
    # "Process exited with code N". Read either explicit signal and infer
    # nothing where neither is present.
    piped = bool(call.get("pipeline"))
    exit_status = parse_exit_status(text)
    if exit_status is None and text:
        if text.lstrip().startswith("Script completed") and not piped:
            # "Script completed" describes the PIPELINE, not the producer.
            exit_status = 0
        elif re.match(r"\s*(Script failed|Error|error:|Command failed)", text):
            exit_status = 1
    # A successful report may DISCUSS a timed-out attempt in its body. Treating
    # any occurrence of those words as the current shell call timing out turns
    # a green `cowork --report c3-controller-log` into `unresolved`. Timeout is
    # a controller-result envelope signal, so accept it only at the beginning
    # of the tool result, never from arbitrary command output.
    timed_out = bool(text and re.match(
        r"\s*(?:(?:Script|Command|Process)\s+)?timed? ?out\b"
        r"|\s*(?:Error|error:).*?\btimed? ?out\b",
        text,
        re.IGNORECASE,
    ))
    wall = _CODEX_WALL_RE.search(text or "")
    observation.update({
        "ended_at": timestamp,
        "exit_status": exit_status,
        "is_error": exit_status not in (None, 0),
        "interrupted": False,
        "timed_out": timed_out,
        "executed_count": parse_test_count(text),
        "wall_time_s": float(wall.group(1)) if wall else None,
        # See P17: Codex's custom_tool_call_output carries an output text and a
        # wall time, and nothing about the terminal the command ran under.
        "tty_stdin_mode": "unknown",
        "output_bytes": len(text.encode("utf-8")) if text else 0,
    })
    observation["adjudication"] = adjudicate(
        exit_status, executed_count=observation["executed_count"],
        timed_out=timed_out)
    observation["failure_class"] = classify_failure(
        text, observation["adjudication"], call.get("intent"))
    return observation


def _codex_output_text(payload):
    output = payload.get("output")
    if isinstance(output, str):
        return output
    parts = []
    for item in output or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def annotate_attempts(attempts, mutations=None):
    """Add the cross-attempt facts no single attempt can know about itself.

    Three of criterion 3's requirements only exist in relation to other
    attempts:

    - RETRY LINEAGE. Re-running the same command identity is a retry, and its
      `attempt_number` plus `retries` is what makes "we ran it eight times until
      it passed" visible instead of reading as one clean pass.
    - OVERLAP. Two runs whose windows intersect were in flight together, so
      neither's result cleanly describes the tree — reported, not hidden.
    - RECURRENCE. The same environment failure hit repeatedly, possibly by
      different roles, is avoidable orchestration cost rather than N unrelated
      accidents.
    - UNSAFE EVIDENCE. An attempt whose tree was MUTATED WHILE IT RAN tested
      something that no longer exists, so it is REFUSED rather than counted.
      Deliberately not "any attempt after any mutation": a builder editing
      files is the entire point of a build, and refusing every check that
      follows an edit would refuse them all. The unsafe case is the tree moving
      *underneath* a running command.
    """
    ordered = sorted(attempts or [],
                     key=lambda a: (a.get("started_at") or "", 
                                    a.get("tool_call_id") or ""))
    seen = {}
    recurrence = {}
    mutation_times = sorted(
        m.get("timestamp") for m in (mutations or [])
        if isinstance(m, dict) and not m.get("restored") and m.get("timestamp"))
    for attempt in ordered:
        identity = attempt.get("command_identity") or "(unknown)"
        prior = seen.get(identity, [])
        attempt["attempt_number"] = len(prior) + 1
        attempt["retries"] = len(prior)
        if prior:
            attempt["retry_of"] = prior[-1].get("tool_call_id")
            attempt["retry_state"] = "retry"
        else:
            attempt["retry_state"] = "fresh"
        seen.setdefault(identity, []).append(attempt)
        if attempt.get("failure_class") == "environment_dependency":
            key = (identity, attempt.get("failure_class"))
            recurrence[key] = recurrence.get(key, 0) + 1
            attempt["environment_recurrence"] = recurrence[key]
        overlapping = []
        for other in ordered:
            if other is attempt:
                continue
            if _windows_overlap(attempt, other):
                overlapping.append(other.get("tool_call_id"))
        if overlapping:
            attempt["overlaps"] = overlapping
            attempt["overlap_state"] = "overlapping"
        else:
            attempt["overlap_state"] = "exclusive"
        started, ended = attempt.get("started_at"), attempt.get("ended_at")
        during = [t for t in mutation_times
                  if started and ended and started < t < ended]
        if during:
            attempt["evidence_safety"] = "refused"
            attempt["mutations_during_run"] = len(during)
            attempt["refusal_reason"] = (
                "the tree was mutated %d time(s) while this ran, so its result "
                "describes a state that no longer exists" % len(during))
        else:
            attempt["evidence_safety"] = "accepted"
    return ordered


def _windows_overlap(a, b):
    """Whether two attempts were in flight at the same time."""
    a_start, a_end = a.get("started_at"), a.get("ended_at")
    b_start, b_end = b.get("started_at"), b.get("ended_at")
    if not (a_start and a_end and b_start and b_end):
        return False
    # `<=` on both sides, because a controller that stamps whole seconds gives
    # two genuinely concurrent runs identical timestamps. Requiring strict
    # inequality reported them as exclusive, which is the opposite of the truth.
    return a_start <= b_end and b_start <= a_end


def _int_fields(usage):
    """Keep only integer counters from a usage dict; drop nested structures and
    non-numeric annotations so what survives can be summed honestly."""
    if not isinstance(usage, dict):
        return {}
    return {k: v for k, v in usage.items()
            if isinstance(v, int) and not isinstance(v, bool)}


# --------------------------------------------------------------------------- #
# The one entry point the orchestrator calls.                                 #
# --------------------------------------------------------------------------- #


def ingest_session(identities, cwd=None, claude_root=None, codex_root=None):
    """Ingest every controller log a session's roles ran against.

    `identities` is the identities.json mapping (role -> {tool, session_id}).
    Returns `{role: Result}`. Never raises: a role whose log cannot be located
    yields a `missing` Result, so the caller can report the gap by name.
    """
    out = {}
    if not isinstance(identities, dict):
        return out
    for role, identity in identities.items():
        if role == "observations" or not isinstance(identity, dict):
            continue
        tool = identity.get("tool") or identity.get("controller")
        session_id = identity.get("session_id") or identity.get("thread_id")
        try:
            if tool == "claude":
                path = locate_claude_log(session_id, cwd=cwd, root=claude_root)
                out[role] = ingest_claude(path)
            elif tool == "codex":
                path = locate_codex_log(session_id, root=codex_root)
                out[role] = ingest_codex(path)
            else:
                out[role] = Result("unrecognised", controller=tool,
                                   detail="no ingester for controller %r"
                                          % (tool,))
        except Exception as exc:  # noqa: BLE001 - ingestion never breaks a run
            out[role] = Result("unreadable", controller=tool,
                               detail="%s: %s" % (type(exc).__name__, exc))
    return out


def attempt_predates_tree(attempt, newest_source_mtime):
    """Whether an attempt ran BEFORE the tree reached its current state.

    The only provenance an attempt genuinely carries is WHEN it ran. Comparing
    that against the newest source-file mtime says, independently of anything
    the builder claims, whether the tree changed after the command executed.
    An attempt with no readable start time cannot be placed at all, which is
    itself a reason to refuse it rather than to assume it is current.
    """
    started = attempt.get("started_at") if isinstance(attempt, dict) else None
    if not started or newest_source_mtime is None:
        return None
    try:
        import datetime
        stamp = datetime.datetime.fromisoformat(
            str(started).replace("Z", "+00:00"))
        return stamp.timestamp() < newest_source_mtime
    except (ValueError, TypeError, OverflowError):
        return None


def observations_for(results, verification_only=True):
    """Flatten per-role Results into the id-free observation list that
    `cowork_ledger.reconcile_attempts` identifies (P3). Each observation keeps
    its natural key `(controller_session_id, tool_call_id)` and the role that
    produced it; ingestion mints nothing."""
    out = []
    for role, result in sorted((results or {}).items()):
        # A TRUNCATED log still holds real evidence before its cut tail, and
        # dropping it loses every attempt the log did record. The retained
        # records are kept and marked, so the loss is reported rather than
        # silently widened to the whole file.
        state = getattr(result, "state", None)
        if state not in ("ok", "truncated"):
            continue
        # A CLAIM is about a command that ran, and whether ingestion happened to
        # classify that command as `verify` has nothing to do with whether the
        # log corroborates it. `cowork --check` and `cowork --report` are not
        # verify-classified, so restricting the join to verification attempts
        # made those claims permanently uncorroborable no matter how cleanly
        # they ran.
        source = (result.verification_attempts if verification_only
                  else result.tool_activity)
        annotated = annotate_attempts(
            [dict(a) for a in source], result.mutations)
        for attempt in annotated:
            entry = dict(attempt)
            entry["role"] = role
            entry["controller"] = result.controller
            if state == "truncated":
                entry["source_state"] = "truncated"
                entry["evidence_lost_after"] = True
            out.append(entry)
    return out
