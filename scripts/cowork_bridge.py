#!/usr/bin/env python3
"""cowork controller bridges.

Two controller back-ends spin up a role's CLI and bridge its conversation to the
user:

- claude: a persistent duplex process driven by stream-json on stdin/stdout.
- codex: one-shot `codex exec --json` plus `codex exec resume <thread_id>` for
  each follow-up turn (codex exec has no persistent duplex stdin).

The command-assembly, message-framing, event-parsing, and probe logic are pure
functions so they can be unit-tested with fakes; only the thin `*_spawn` drivers
touch real subprocesses.

The `--input-format stream-json` stdin schema is officially undocumented
(anthropics/claude-code issue #24594). `probe_claude_stream_json` confirms the
installed claude accepts our shape before any real turn, so no unverified shape
is baked in silently.

Python 3.9+, stdlib only.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_ui as ui  # noqa: E402
import cowork_trace as trace_store  # noqa: E402
import cowork_probe_cache as probe_cache  # noqa: E402
# Re-exported so existing callers/tests keep using bridge.USER_LABEL /
# bridge.speaker_label; the canonical definitions live in cowork_ui.
from cowork_ui import USER_LABEL, speaker_label  # noqa: E402,F401

DEFAULT_ROLE_PROMPT = "roles/scout.md"

# The spinner moved to cowork_ui (both bridges + the loop share it). Alias kept
# for back-compat.
_Spinner = ui.Spinner


def turn_result(ok=True, result="ok", **fields):
    """Small structured send result consumed by the cowork orchestrator."""
    out = {"ok": bool(ok), "result": result}
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def _terminate(proc):
    """Best-effort: stop a spawned CLI so it is not left running after an
    interrupt. Tries SIGTERM, then SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:  # noqa: BLE001 - never raise from cleanup
        pass

# --------------------------------------------------------------------------- #
# Command assembly (verified flags, see the signed-off plan D3/D4 + Mode map). #
# --------------------------------------------------------------------------- #


def claude_mode_flags(mode, yolo):
    """Permission/mode flags for claude given (mode, yolo)."""
    if mode == "plan":
        # Plan mode is read-only regardless of the yolo toggle.
        return ["--permission-mode", "plan"]
    # implement
    if yolo:
        return ["--dangerously-skip-permissions"]
    # yolo off: auto-approve edits + common fs commands; anything else is denied
    # and surfaced as an error (no interactive approval relay in v1).
    return ["--permission-mode", "acceptEdits"]


def codex_mode_flags(mode, yolo):
    """Sandbox flags for codex given (mode, yolo).

    `codex exec` is already non-interactive (it never prompts for approval), so
    approval policy is governed entirely by the sandbox — there is no
    `--ask-for-approval` flag on the exec subcommand (verified against codex-cli
    0.133.0; passing it errors).
    """
    if mode == "plan":
        return ["--sandbox", "read-only"]
    # implement
    if yolo:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["--sandbox", "workspace-write"]


def build_claude_command(role_prompt_file, mode, yolo, session_id=None,
                         resume_id=None, extra_writable_dir=None, model=None):
    """Full argv for a persistent duplex claude scout process.

    Pass `session_id` to pin a known UUID on a fresh session (so it can be saved
    and resumed later), or `resume_id` to continue a saved session.
    `extra_writable_dir`, when set, is granted as an additional writable root
    via `--add-dir` so a no-yolo (acceptEdits) role can write its session
    artifacts even though they live outside cwd. Re-applied on every spawn
    (fresh AND resume), so resumed Claude roles keep the grant.
    `model`, when set, pins the role to a specific model via `--model`
    (re-applied on resume too, so a resumed role keeps its pinned model)."""
    cmd = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",  # stream tokens as they are generated
        "--replay-user-messages",
        # Interactive question/plan tools auto-return "skipped" in headless -p and
        # break the clarify loop; the role asks via text + status=needs_input.
        "--disallowedTools",
        "AskUserQuestion",
        "ExitPlanMode",
        "--append-system-prompt-file",
        role_prompt_file,
    ] + claude_mode_flags(mode, yolo)
    if model:
        cmd += ["--model", model]
    if extra_writable_dir:
        cmd += ["--add-dir", extra_writable_dir]
    if resume_id:
        cmd += ["--resume", resume_id]
    elif session_id:
        cmd += ["--session-id", session_id]
    return cmd


def build_codex_command(prompt_text, mode, yolo, extra_writable_dir=None,
                        model=None):
    """argv for the first one-shot codex exec turn. The role spec is prepended
    into prompt_text by the caller (no AGENTS.md is written into the repo).

    `--skip-git-repo-check` lets cowork run outside a trusted/git directory
    (codex exec otherwise refuses with "Not inside a trusted directory").
    `extra_writable_dir`, when set, is granted as an additional writable root
    via `--add-dir` so a no-yolo (workspace-write) role can write its session
    artifacts outside cwd. The grant is re-applied on every codex resume too
    (see `codex_resume_mode_args` / `build_codex_resume_command`), so resumed
    roles keep the same effective permissions as this fresh turn."""
    return (
        ["codex", "exec", "--json", "--skip-git-repo-check"]
        + codex_mode_flags(mode, yolo)
        + (["--model", model] if model else [])
        + (["--add-dir", extra_writable_dir] if extra_writable_dir else [])
        + [prompt_text]
    )


def codex_resume_mode_args(mode, yolo, extra_writable_dir=None):
    """Resume-compatible permission args mirroring `codex_mode_flags` for a
    `codex exec resume` turn (verified against codex-cli 0.139.0).

    `codex exec resume` rejects `--sandbox`/`--add-dir`, but accepts
    `--dangerously-bypass-approvals-and-sandbox` and `-c <dotted.toml.path>`.
    So the sandboxed modes are re-applied through `-c` config keys instead:

    - plan -> `sandbox_mode="read-only"` (mirrors fresh `--sandbox read-only`).
    - implement + yolo -> `--dangerously-bypass-approvals-and-sandbox`.
    - implement + no-yolo -> `sandbox_mode="workspace-write"`, plus, when
      `extra_writable_dir` is set, `sandbox_workspace_write.writable_roots`
      granting that dir (mirrors fresh `--sandbox workspace-write` + `--add-dir`;
      the root is ADDED to the default roots, verified live).

    The writable-root path is encoded as a TOML basic string via `json.dumps`
    (a valid TOML basic string for filesystem paths, escaping any quotes/
    backslashes); the array value is `[` + json.dumps(dir) + `]`.
    """
    if mode == "plan":
        return ["-c", 'sandbox_mode="read-only"']
    # implement
    if yolo:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    args = ["-c", 'sandbox_mode="workspace-write"']
    if extra_writable_dir:
        roots = "[" + json.dumps(extra_writable_dir) + "]"
        args += ["-c", "sandbox_workspace_write.writable_roots=" + roots]
    return args


def build_codex_resume_command(thread_id, prompt_text, mode, yolo,
                               extra_writable_dir=None, model=None):
    """argv for a codex follow-up turn against an explicit thread id (never
    --last, which could grab a concurrent session in the same cwd).

    On codex-cli 0.139.0 `codex exec resume` does NOT inherit the original
    session's sandbox, so each resume must re-apply the role's permissions.
    `--sandbox`/`--add-dir` are rejected on resume, but
    `--dangerously-bypass-approvals-and-sandbox` and `-c` config keys are
    accepted — see `codex_resume_mode_args`, which mirrors the fresh-launch
    permissions for every (mode, yolo) combo. The permission args go after
    `--skip-git-repo-check` and before the thread id; prompt_text stays the
    final positional arg (never --last)."""
    return (
        ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
        + codex_resume_mode_args(mode, yolo, extra_writable_dir)
        # `codex exec resume` rejects `--model` but accepts the `-c model=...`
        # config key (same mechanism as the re-applied sandbox), so a pinned
        # model survives resume turns too. TOML basic string via json.dumps.
        + (["-c", "model=" + json.dumps(model)] if model else [])
        + [thread_id, prompt_text]
    )


# --------------------------------------------------------------------------- #
# Message framing / event parsing.                                            #
# --------------------------------------------------------------------------- #


def encode_user_message(text):
    """Newline-delimited stream-json user message for claude stdin."""
    obj = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(obj) + "\n"


def _text_from_content(content):
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
    return "".join(parts)


def _looks_like_permission_denial(text):
    if not text:
        return False
    low = text.lower()
    return (
        "permission" in low
        or "requires approval" in low
        or "not allowed" in low
        or "denied" in low
    )


def _usage_from_result(obj):
    """Best-effort extraction of token usage from a claude stream-json `result`
    event (#1/D2). Returns a small content-free dict of int counts when the CLI
    exposes them, else None. Tolerant: any unexpected shape yields None and never
    raises. Byte+hash accounting is the load-bearing data; usage is optional."""
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    out = {}
    for key in keys:
        val = usage.get(key)
        if isinstance(val, bool):  # bool is an int subclass; never a token count
            continue
        if isinstance(val, int):
            out[key] = val
    return out or None


def parse_claude_event(obj):
    """Classify one claude stream-json output event.

    Returns a dict with at least {"kind": ...}. Kinds: assistant, result,
    system, partial, user_replay, denied, other.
    """
    etype = obj.get("type")
    if etype == "assistant":
        msg = obj.get("message", {}) or {}
        text = _text_from_content(msg.get("content"))
        # A denied/blocked tool surfaces as an error tool_result in the stream.
        for part in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                if part.get("is_error") and _looks_like_permission_denial(
                    _text_from_content(part.get("content"))
                ):
                    return {"kind": "denied", "text": _text_from_content(part.get("content"))}
        return {"kind": "assistant", "text": text}
    if etype == "result":
        subtype = obj.get("subtype", "")
        is_error = "error" in (subtype or "")
        return {
            "kind": "result",
            "subtype": subtype,
            "is_error": is_error,
            "text": obj.get("result", ""),
            "session_id": obj.get("session_id"),
            # Best-effort controller-reported usage (#1/D2): present only when the
            # CLI's result event exposes token counts. Never load-bearing.
            "usage": _usage_from_result(obj),
        }
    if etype == "system":
        # The init system event names the live model (traceability: which
        # model actually served this session, not just which was requested).
        return {"kind": "system", "subtype": obj.get("subtype", ""),
                "model": obj.get("model")}
    if etype == "stream_event":
        event = obj.get("event") or {}
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return {"kind": "partial", "text": delta.get("text", "")}
        block = event.get("content_block") or {}
        if (event.get("type") == "content_block_start"
                and block.get("type") == "tool_use"):
            # Fallback to 'tool' so the activity line never reads 'using …'.
            return {"kind": "tool", "name": block.get("name") or "tool"}
        return {"kind": "partial", "text": ""}
    if etype == "user":
        return {"kind": "user_replay"}
    return {"kind": "other", "type": etype}


# Codex item types that mean "the agent is using a tool", with the spinner
# label each one shows. Only these flip the activity label; unknown item types
# stay "other" so future codex events can't reset the label incorrectly.
_CODEX_TOOL_LABELS = {
    "command_execution": "running a command",
    "mcp_tool_call": "calling a tool",
    "file_change": "editing files",
    "patch_apply": "editing files",
    "web_search": "searching the web",
}


def _codex_tool_label(itype, item):
    if itype == "mcp_tool_call" and item.get("tool"):
        return "calling %s" % item["tool"]
    return _CODEX_TOOL_LABELS[itype]


def parse_codex_event(obj):
    """Classify one codex --json (JSONL) event.

    Kinds: thread_started (thread_id), turn_started, turn_completed, message
    (text), denied, error, tool (label), tool_done, other.
    """
    etype = obj.get("type")
    if etype == "thread.started":
        # `model` is best-effort: newer codex CLIs may name the live model on
        # the thread event; absent on older versions and dropped downstream.
        return {"kind": "thread_started", "thread_id": obj.get("thread_id"),
                "model": obj.get("model")}
    if etype == "turn.started":
        return {"kind": "turn_started"}
    if etype == "turn.completed":
        return {"kind": "turn_completed", "usage": obj.get("usage"),
                "model": obj.get("model")}
    if etype == "error":
        return {"kind": "error", "text": obj.get("message", "")}
    if etype in ("item.started", "item.completed"):
        item = obj.get("item", {}) or {}
        itype = item.get("type")
        status = item.get("status")
        if status in ("rejected", "declined", "denied"):
            return {"kind": "denied", "text": item.get("text", "") or itype or ""}
        if itype in ("agent_message", "message", "assistant_message"):
            return {"kind": "message", "text": item.get("text", "")}
        if itype in _CODEX_TOOL_LABELS:
            if etype == "item.started":
                return {"kind": "tool", "item_type": itype,
                        "label": _codex_tool_label(itype, item)}
            return {"kind": "tool_done", "item_type": itype}
        return {"kind": "other", "item_type": itype}
    return {"kind": "other", "type": etype}


def capture_thread_id(events):
    """Return the thread_id from the first thread.started event, or None."""
    for obj in events:
        parsed = parse_codex_event(obj)
        if parsed["kind"] == "thread_started" and parsed.get("thread_id"):
            return parsed["thread_id"]
    return None


def denial_message():
    return "denied: enable yolo or rerun this role with implement access"


# --------------------------------------------------------------------------- #
# Probe: confirm the installed claude accepts our stdin schema.               #
# --------------------------------------------------------------------------- #


def probe_claude_stream_json(spawn, mode="plan", yolo=True,
                             role_prompt_file=DEFAULT_ROLE_PROMPT, trace=None,
                             role="scout", extra_writable_dir=None,
                             cache_enabled=False, version_fn=None,
                             cache_path=None):
    """Send one minimal user message to claude and confirm an assistant/result
    event comes back.

    spawn(command, stdin_text) -> iterable of raw event dicts (json objects).
    Returns (ok, alert_or_None). On an unsupported shape, ok is False and alert
    explains the failure rather than proceeding on a guessed schema.
    `extra_writable_dir` mirrors the live session's writable-root grant so the
    pre-first-token probe runs with the same sandbox.

    #3 probe cache: when `cache_enabled` (the live launch call sites pass True;
    tests and existing callers default to False, keeping the always-live-probe
    behavior), a conservative cache key is computed over the resolved CLI path,
    `claude --version`, the role-prompt hash, mode, yolo, and writable-dir
    presence. On a HIT the live probe is skipped entirely (no spawn) and
    (True, None) is returned. On a MISS the live probe runs and, on success, the
    key is stored. A version-resolution failure forces always-live (never
    cached). `version_fn`/`cache_path` are injectable for tests.
    """
    command = build_claude_command(role_prompt_file, mode, yolo,
                                   extra_writable_dir=extra_writable_dir)
    cache_key = None
    if cache_enabled:
        claude_path = probe_cache.resolve_claude_path(command)
        resolver = version_fn or probe_cache.claude_version
        version = resolver(claude_path)
        cache_key = probe_cache.probe_cache_key(
            claude_path, version, role_prompt_file, mode, yolo,
            bool(extra_writable_dir))
        if cache_key and probe_cache.cache_hit(cache_key, path=cache_path):
            if trace:
                trace.event("controller.probe.cache_hit", controller="claude",
                            role=role, prompt_kind="probe", mode=mode, yolo=yolo,
                            role_prompt_file=role_prompt_file)
            return True, None
    stdin_text = encode_user_message("ping")
    if trace:
        data = trace_store.command_meta(command)
        data.update(trace_store.prompt_meta(stdin_text, prefix="stdin"))
        trace.event("controller.probe.start", controller="claude", role=role,
                    prompt_kind="probe", mode=mode, yolo=yolo, cwd=os.getcwd(),
                    role_prompt_file=role_prompt_file, **data)
    try:
        events = spawn(command, stdin_text)
        seen_ok = False
        probe_usage = None
        for obj in events:
            parsed = parse_claude_event(obj)
            kind = parsed.get("kind")
            if kind == "result":
                # The result is terminal AND the only event carrying usage —
                # capture it even when an assistant event preceded it (#1/D2),
                # then stop.
                probe_usage = parsed.get("usage")
                seen_ok = True
                break
            if kind == "assistant":
                # A valid shape, but keep scanning so a following result's usage
                # is not dropped.
                seen_ok = True
        if seen_ok:
            if cache_enabled and cache_key:
                probe_cache.cache_store(cache_key, path=cache_path)
                if trace:
                    trace.event("controller.probe.cache_store",
                                controller="claude", role=role,
                                prompt_kind="probe")
            if trace:
                trace.event("controller.probe.end", controller="claude",
                            role=role, prompt_kind="probe", result="ok",
                            usage=probe_usage)
            return True, None
    except Exception as exc:  # noqa: BLE001 - surface any spawn failure as an alert
        if trace:
            trace.event("controller.probe.end", controller="claude", role=role,
                        prompt_kind="probe", result="error",
                        error_type=type(exc).__name__)
        return False, (
            "Could not probe `claude` stream-json input (%s).\n"
            "    Confirm `claude` is installed and supports "
            "`--input-format stream-json`." % exc
        )
    if trace:
        trace.event("controller.probe.end", controller="claude", role=role,
                    prompt_kind="probe", result="unsupported")
    return False, (
        "`claude` did not accept the cowork stream-json stdin message shape.\n"
        "    The stdin schema is undocumented (anthropics/claude-code #24594); "
        "your claude version may differ. Update claude or report the schema."
    )


# --------------------------------------------------------------------------- #
# Thin real-subprocess drivers (not unit-tested; exercised manually).         #
# --------------------------------------------------------------------------- #


def _real_claude_spawn(command, stdin_text):
    """Run a claude command with stdin_text and yield parsed json events.

    Used by the probe in a real run. One-shot: closes stdin after writing.
    """
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    proc.stdin.write(stdin_text)
    proc.stdin.close()
    events = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    proc.wait()
    return events



# --------------------------------------------------------------------------- #
# Synchronous session bridges.                                                #
#                                                                             #
# Each `send(text)` runs exactly one turn, streams the labeled reply, and     #
# returns when the turn completes — so the caller (cowork) can read the scout #
# intel `status` between turns and decide whether to prompt or finish.        #
# --------------------------------------------------------------------------- #


class ClaudeSession:
    """One persistent `claude -p` stream-json process; one turn per send()."""

    def __init__(self, role_prompt_file, mode, yolo, io_out=None, speaker="scout",
                 session_id=None, resume_id=None, on_session_id=None,
                 region_factory=None, trace=None, extra_writable_dir=None,
                 internal=False, model=None):
        self.io_out = io_out or sys.stdout
        self.speaker = speaker
        self.controller = "claude"
        self.label = speaker_label(speaker)
        # internal=True streams this whole session on the dim internal channel
        # (reviewer/advisor); role sessions stay False and mark inline blocks.
        self.internal = internal
        self.on_session_id = on_session_id
        self.trace = trace
        self.mode = mode
        self.yolo = yolo
        self.role_prompt_file = role_prompt_file
        self.session_id = session_id
        self.resume_id = resume_id
        # The live model, captured from the first system-init event that names
        # it (traceability: stamped on turn results and eval score entries).
        # `requested_model` is the config-pinned model (None = CLI default).
        self.model = None
        self.requested_model = model
        # Markdown render region; injectable for tests. TTY: Rich Live streaming.
        # Non-TTY: raw passthrough, byte-identical to the historical stream.
        self._region_factory = region_factory or ui.StreamingMarkdown
        self._seen_session = False
        self.extra_writable_dir = extra_writable_dir
        command = build_claude_command(role_prompt_file, mode, yolo,
                                       session_id=session_id, resume_id=resume_id,
                                       extra_writable_dir=extra_writable_dir,
                                       model=model)
        if self.trace:
            self.trace.event(
                "controller.spawn.start", controller="claude", role=speaker,
                fresh=not bool(resume_id), resume=bool(resume_id), mode=mode,
                yolo=yolo, cwd=os.getcwd(), role_prompt_file=role_prompt_file,
                session_id=session_id, resume_id=resume_id,
                **trace_store.command_meta(command))
            # Item #4 measurement: the static role markdown loaded via
            # --append-system-prompt-file is a SYSTEM prompt — outside the
            # per-turn user-message prompt_bytes. Record its size separately,
            # tagged role_prompt_delivery='claude_system', once per spawn. This
            # single emission covers BOTH lead and reviewer Claude launches
            # (every ClaudeSession passes role_prompt_file through here).
            try:
                rp_bytes = os.path.getsize(role_prompt_file)
            except OSError:
                rp_bytes = None
            if rp_bytes is not None:
                self.trace.event("role.prompt.bytes", role=speaker,
                                 bytes=rp_bytes, delivery="claude_system")
        try:
            self.proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            if self.trace:
                self.trace.event(
                    "controller.spawn.end", controller="claude", role=speaker,
                    result="error", error_type=type(exc).__name__)
            raise
        if self.trace:
            self.trace.event("controller.spawn.end", controller="claude",
                             role=speaker, result="ok")

    def send(self, text, meta=None):
        """Send one user message and surface the labeled reply for one turn.

        On a TTY a `scout working…` spinner fills the gap before the first token
        (#13), then the reply renders **live** as markdown in a Rich region (#5) —
        length-independent. Off a TTY the region is a raw passthrough, byte-for-byte
        the historical token stream (so the streaming/test contract is unchanged).

        `meta` is an optional per-turn accounting dict (#1/D11) merged into the
        controller.turn.start event (prompt_kind, role, controller, phase,
        fresh/resume, round, artifact descriptors, context_revision). It is
        content-free metadata only — Trace.event drops any None field."""
        if self.trace:
            fields = {"controller": "claude", "role": self.speaker}
            fields.update(trace_store.prompt_meta(text))
            if meta:
                fields.update({k: v for k, v in meta.items() if v is not None})
            self.trace.event("controller.turn.start", **fields)
        turn_started = time.monotonic()

        def _elapsed_ms():
            return int((time.monotonic() - turn_started) * 1000)

        self.proc.stdin.write(encode_user_message(text))
        self.proc.stdin.flush()
        tty = ui.is_tty(self.io_out)
        any_text = False
        denied = False
        region = None
        idle = "%s working" % self.speaker
        status_active = False  # the region currently shows a tool-activity row
        spinner = ui.Spinner(self.io_out, idle) if tty else None
        if spinner:
            spinner.start()

        def _set_status(text):
            # Show/refresh the activity row; guarded so injected/custom regions
            # without status support keep working.
            nonlocal status_active
            st = getattr(region, "set_status", None)
            if st:
                st(text)
                status_active = True

        def _clear_status():
            nonlocal status_active
            if not status_active:
                return
            cs = getattr(region, "clear_status", None)
            if cs:
                cs()
            status_active = False

        def _feed(chunk):
            # Open the render region on the first token (after stopping the
            # gap-filling spinner), then stream into it.
            nonlocal region
            if region is None:
                if spinner:
                    spinner.stop()
                label = ui.label(self.speaker, tty)
                if self._region_factory is ui.StreamingMarkdown:
                    region = self._region_factory(
                        self.io_out, label, trace=self.trace,
                        trace_fields={
                            "controller": "claude",
                            "role": self.speaker,
                        }, internal=self.internal)
                else:
                    region = self._region_factory(self.io_out, label)
                region.__enter__()
            else:
                _clear_status()  # text resumed: drop the tool-activity row
            region.feed(chunk)

        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A new text block after the model already produced text (e.g. it
            # resumed narration after a tool call) must be separated, else the
            # blocks abut with no space ("...off.Enough recon").
            if (obj.get("type") == "stream_event" and region is not None
                    and region.buf):
                ev = obj.get("event") or {}
                if (ev.get("type") == "content_block_start"
                        and (ev.get("content_block") or {}).get("type") == "text"):
                    region.feed("\n\n")
            parsed = parse_claude_event(obj)
            sid = parsed.get("session_id")
            if sid and not self._seen_session and self.on_session_id:
                self._seen_session = True
                if self.trace:
                    self.trace.event("controller.session_id",
                                     controller="claude", role=self.speaker,
                                     session_id=sid)
                self.on_session_id(sid)
            kind = parsed["kind"]
            if (kind == "system" and parsed.get("model")
                    and parsed["model"] != self.model):
                self.model = parsed["model"]
                if self.trace:
                    self.trace.event("controller.model", controller="claude",
                                     role=self.speaker, model=self.model)
            if kind == "partial" and parsed.get("text"):
                _feed(parsed["text"])
                any_text = True
            elif kind == "assistant" and parsed.get("text") and not any_text:
                _feed(parsed["text"])
                any_text = True
            elif kind == "tool":
                # The model is calling a tool — keep the UI alive (#loading-state).
                busy = "%s using %s" % (self.speaker, parsed.get("name") or "tool")
                if region is None:
                    if spinner:
                        spinner.set_label(busy)
                else:
                    _set_status(busy + "…")
            elif kind == "user_replay":
                # A tool_result came back; back to plain "working" until the
                # next text token or tool call.
                if region is None:
                    if spinner:
                        spinner.set_label(idle)
                elif status_active:
                    _set_status(idle + "…")
            elif kind == "denied":
                if spinner:
                    spinner.stop()
                _clear_status()  # never leave a tool label over the raw write
                denied = True
                if self.trace:
                    self.trace.event("controller.denied", controller="claude",
                                     role=self.speaker)
                self.io_out.write("\n" + ui.label(self.speaker, tty) + denial_message())
            elif kind == "result":
                if spinner:
                    spinner.stop()
                _clear_status()
                if region is not None:
                    region.__exit__(None, None, None)  # finalize the render
                elif denied:
                    self.io_out.write("\n")
                if parsed.get("is_error"):
                    if self.trace:
                        self.trace.event(
                            "controller.turn.end", controller="claude",
                            role=self.speaker, result="error",
                            subtype=parsed.get("subtype"),
                            model=self.model or self.requested_model, duration_ms=_elapsed_ms())
                    self.io_out.write(
                        ui.colorize("[error] " + (parsed.get("text") or ""),
                                    ui.RED, tty) + "\n")
                    self.io_out.flush()
                    return turn_result(
                        False, "error", subtype=parsed.get("subtype"),
                        session_id=sid or self.session_id,
                        model=self.model or self.requested_model, duration_ms=_elapsed_ms())
                elif self.trace:
                    self.trace.event("controller.turn.end", controller="claude",
                                     role=self.speaker,
                                     result="denied" if denied else "ok",
                                     subtype=parsed.get("subtype"),
                                     usage=parsed.get("usage"),
                                     model=self.model or self.requested_model,
                                     duration_ms=_elapsed_ms())
                self.io_out.flush()
                if denied:
                    return turn_result(
                        False, "denied", denied=True,
                        subtype=parsed.get("subtype"),
                        session_id=sid or self.session_id,
                        usage=parsed.get("usage"), model=self.model or self.requested_model,
                        duration_ms=_elapsed_ms())
                return turn_result(
                    True, "ok", subtype=parsed.get("subtype"),
                    session_id=sid or self.session_id,
                    usage=parsed.get("usage"), model=self.model or self.requested_model,
                    duration_ms=_elapsed_ms())
            self.io_out.flush()
        if spinner:
            spinner.stop()
        if region is not None:
            region.__exit__(None, None, None)
        if self.trace:
            self.trace.event("controller.turn.end", controller="claude",
                             role=self.speaker, result="error",
                             error_type="eof")
        return turn_result(False, "error", error_type="eof",
                           session_id=self.session_id)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        _terminate(self.proc)


class CodexSession:
    """Turn-based codex bridge: first `codex exec --json`, then
    `codex exec resume <thread_id>` per send(). A spinner runs during each turn."""

    def __init__(self, mode, yolo, io_out=None, speaker="scout",
                 resume_thread_id=None, on_thread_id=None, trace=None,
                 extra_writable_dir=None, internal=False, model=None):
        self.mode = mode
        self.yolo = yolo
        self.controller = "codex"
        self.io_out = io_out or sys.stdout
        self.speaker = speaker
        self.label = speaker_label(speaker)
        # internal=True renders this whole session's turns on the dim internal
        # channel (reviewer/advisor); role sessions stay False and mark inline.
        self.internal = internal
        self.thread_id = resume_thread_id
        self.on_thread_id = on_thread_id
        self.trace = trace
        # The live model, captured best-effort from codex events that name it
        # (traceability: stamped on turn results and eval score entries).
        # `requested_model` is the config-pinned model (None = CLI default).
        self.model = None
        self.requested_model = model
        # Granted as a writable root on the fresh exec turn AND re-granted on
        # every resume (via -c sandbox_workspace_write.writable_roots), so a
        # resumed no-yolo role keeps writing its session assets outside cwd.
        self.extra_writable_dir = extra_writable_dir
        self._notified = False
        self._resuming_first = resume_thread_id is not None
        self._started = False

    def _run(self, command):
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        events = []
        tty = ui.is_tty(self.io_out)
        wrote_label = {"done": False}
        try:
            with _Spinner(self.io_out, label="%s working" % self.speaker) as spin:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append(obj)
                    parsed = parse_codex_event(obj)

                    def _emit(text, render=True):
                        spin.stop()
                        if not wrote_label["done"]:
                            # A surfaced internal block (reviewer/advisor) gets a
                            # faint lead-in gap above its label so it doesn't
                            # crowd the agent text before it (no-op off a TTY).
                            if self.internal:
                                ui.internal_lead_in(self.io_out, tty)
                            self.io_out.write(ui.label(self.speaker, tty))
                            wrote_label["done"] = True
                        if render:
                            ui.render_markdown(self.io_out, text, enabled=tty,
                                               internal=self.internal)
                        else:
                            self.io_out.write(text + "\n")
                        self.io_out.flush()

                    if parsed["kind"] == "message" and parsed.get("text"):
                        _emit(parsed["text"])
                    elif parsed["kind"] == "denied":
                        _emit(denial_message(), render=False)
                    elif parsed["kind"] == "error":
                        _emit("[error] " + (parsed.get("text") or ""), render=False)
                    elif parsed["kind"] == "tool" and not wrote_label["done"]:
                        # Reflect tool activity in the spinner while it's live
                        # (it stops on the first emitted message and never
                        # restarts — codex emits its message at turn end).
                        spin.set_label("%s %s" % (self.speaker, parsed["label"]))
                    elif parsed["kind"] == "tool_done" and not wrote_label["done"]:
                        spin.set_label("%s working" % self.speaker)
            proc.wait()
        except KeyboardInterrupt:
            _terminate(proc)
            raise
        return events

    def send(self, text, meta=None):
        if not self._started and not self._resuming_first:
            command = build_codex_command(
                text, self.mode, self.yolo,
                extra_writable_dir=self.extra_writable_dir,
                model=self.requested_model)
            fresh = True
        else:
            if not self.thread_id:
                self.io_out.write("[error] no codex thread id; cannot continue\n")
                self.io_out.flush()
                if self.trace:
                    self.trace.event("controller.turn.end", controller="codex",
                                     role=self.speaker, result="error",
                                     error_type="missing_thread_id")
                return turn_result(False, "error",
                                   error_type="missing_thread_id")
            command = build_codex_resume_command(
                self.thread_id, text, self.mode, self.yolo,
                extra_writable_dir=self.extra_writable_dir,
                model=self.requested_model)
            fresh = False
        self._started = True
        if self.trace:
            fields = {
                "controller": "codex", "role": self.speaker,
                "fresh": fresh, "resume": not fresh, "mode": self.mode,
                "yolo": self.yolo, "cwd": os.getcwd(),
                "thread_id": self.thread_id,
            }
            fields.update(trace_store.command_meta(command, prompt_text=text))
            # Per-turn accounting (#1/D11): caller-supplied meta merges in last,
            # but never overrides the authoritative fresh/resume computed here.
            if meta:
                fields.update({k: v for k, v in meta.items()
                               if v is not None and k not in ("fresh", "resume")})
            self.trace.event("controller.turn.start", **fields)
        turn_started = time.monotonic()
        try:
            events = self._run(command)
        except Exception as exc:  # noqa: BLE001
            if self.trace:
                self.trace.event("controller.turn.end", controller="codex",
                                 role=self.speaker, result="error",
                                 error_type=type(exc).__name__)
            return turn_result(False, "error", error_type=type(exc).__name__)
        duration_ms = int((time.monotonic() - turn_started) * 1000)
        tid = capture_thread_id(events)
        if tid and not self.thread_id:
            self.thread_id = tid
            if self.trace:
                self.trace.event("controller.thread_id", controller="codex",
                                 role=self.speaker, thread_id=self.thread_id)
        if self.thread_id and self.on_thread_id and not self._notified:
            self._notified = True
            if self.trace:
                self.trace.event("controller.thread_id.notified",
                                 controller="codex", role=self.speaker,
                                 thread_id=self.thread_id)
            self.on_thread_id(self.thread_id)
        parsed_events = [parse_codex_event(obj) for obj in events]
        kinds = [p.get("kind") for p in parsed_events]
        result = "error" if "error" in kinds else "denied" if "denied" in kinds else "ok"
        # Best-effort controller-reported usage (#1/D2): the turn.completed event
        # carries it when codex exposes it; otherwise None and the field is dropped.
        usage = None
        for p in parsed_events:
            if p.get("kind") == "turn_completed" and isinstance(p.get("usage"), dict):
                usage = p["usage"]
            if (p.get("kind") in ("thread_started", "turn_completed")
                    and p.get("model") and p["model"] != self.model):
                self.model = p["model"]
                if self.trace:
                    self.trace.event("controller.model", controller="codex",
                                     role=self.speaker, model=self.model)
        # Older codex CLIs never name the live model in events; fall back to
        # the config-pinned model so the stamp is still meaningful.
        model = self.model or self.requested_model
        if self.trace:
            self.trace.event("controller.turn.end", controller="codex",
                             role=self.speaker, result=result,
                             thread_id=self.thread_id, event_count=len(events),
                             usage=usage, model=model,
                             duration_ms=duration_ms)
        return turn_result(
            result == "ok", result, denied=(result == "denied"),
            thread_id=self.thread_id, usage=usage, model=model,
            duration_ms=duration_ms)

    def close(self):
        pass
