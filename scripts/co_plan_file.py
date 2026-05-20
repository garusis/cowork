#!/usr/bin/env python3
"""File helper for the co-plan Markdown chat protocol."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Iterator


POLL_ROLES = {"planner", "advisor"}
LEDGER_RAISE_ROLES = {"planner", "advisor"}
HEADING_RE = re.compile(r"^### \[([^\]]+)\] ([^\n]+)$", re.MULTILINE)
CONSENSUS_RE = re.compile(r"^### \[consensus\] [^\n]+$", re.MULTILINE)
GOAL_HEADING_RE = re.compile(r"^### \[goal\] [^\n]+$", re.MULTILINE)
PROPOSAL_MARKER = "--- proposing consensus ---"
CONFIRM_MARKER = "--- consensus confirmed ---"
ESCALATION_MARKER = "--- escalating to marcos ---"
SIGNOFF_RECAP_READY_RE = re.compile(r"\bready\s+for\s+sign[-\s]?off\b", re.IGNORECASE)
SIGNOFF_RECAP_LEDGER_RE = re.compile(r"\b(?:ledger|Q\d+|questions?)\b", re.IGNORECASE)
PLAN_REVIEW_RECEIPT_RE = re.compile(r"^##+\s*Plan Review Receipt\s*$", re.MULTILINE)
PLAN_HEADING_RE = re.compile(r"^(?:##|###)\s+(.+?)\s*#*\s*$", re.MULTILINE)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
STATE_SCHEMA_VERSION = 2
QUESTION_ID_RE = re.compile(r"^Q(\d+)$")
REQUIRED_PLAN_HEADINGS = [
    "summary",
    "goal coverage",
    "decisions",
    "approach",
    "implementation changes",
    "tests",
    "risks and verification",
    "assumptions",
]
PLACEHOLDER_RE = re.compile(
    r"\b(?:TBD|TODO|FIXME|XXX)\b|"
    r"\b(?:open|unresolved|pending)\s+questions?\b|"
    r"\bneeds\s+(?:marcos|decision|answer|clarification)\b",
    re.IGNORECASE,
)
STALE_PLAN_HEADING_RE = re.compile(
    r"\b(?:previous|prior|old|earlier|superseded)\s+(?:plan|version)s?\b|"
    r"\b(?:revision|plan|change)\s+history\b|"
    r"\bchangelog\b",
    re.IGNORECASE,
)


def _normalize_heading_text(text: str) -> str:
    """Lowercase, strip simple markdown emphasis, collapse whitespace."""
    stripped = re.sub(r"[*_`~]", "", text)
    return re.sub(r"\s+", " ", stripped).strip().lower()


def extract_plan_headings(plan_text: str) -> list[str]:
    """Return normalized `##` / `###` headings from plan text (code blocks ignored)."""
    cleaned = FENCED_CODE_RE.sub("", plan_text)
    return [_normalize_heading_text(m.group(1)) for m in PLAN_HEADING_RE.finditer(cleaned)]


def validate_plan_text(plan_text: str, state: dict[str, object]) -> list[str]:
    """Return human-readable reasons the plan is not ready for consensus."""
    reasons: list[str] = []
    plain = FENCED_CODE_RE.sub("", plan_text)
    headings = extract_plan_headings(plan_text)
    heading_set = set(headings)

    missing = [
        heading for heading in REQUIRED_PLAN_HEADINGS if heading not in heading_set
    ]
    if missing:
        reasons.append(
            "missing required plan headings: " + ", ".join(missing)
        )

    stale_headings = [
        heading for heading in headings if STALE_PLAN_HEADING_RE.search(heading)
    ]
    if stale_headings:
        reasons.append(
            "plan appears to contain stale history/version headings: "
            + ", ".join(stale_headings[:5])
        )

    placeholders = sorted({match.group(0) for match in PLACEHOLDER_RE.finditer(plain)})
    if placeholders:
        reasons.append(
            "plan contains unresolved placeholder text: "
            + ", ".join(placeholders[:8])
        )

    questions = state.get("questions", [])
    question_by_id = {
        question.get("id"): question
        for question in questions
        if isinstance(question, dict)
    }
    open_ids = [
        question["id"]
        for question in questions
        if isinstance(question, dict) and question.get("state") == "open"
    ]
    if open_ids:
        reasons.append("open ledger questions remain: " + ", ".join(open_ids))

    referenced_ids = sorted(
        {match.group(0) for match in re.finditer(r"\bQ\d+\b", plain)},
        key=lambda value: int(value[1:]),
    )
    unresolved_refs: list[str] = []
    unknown_refs: list[str] = []
    for question_id in referenced_ids:
        question = question_by_id.get(question_id)
        if question is None:
            unknown_refs.append(question_id)
        elif question.get("state") != "answered":
            unresolved_refs.append(question_id)
    if unknown_refs:
        reasons.append(
            "plan references unknown ledger questions: " + ", ".join(unknown_refs)
        )
    if unresolved_refs:
        reasons.append(
            "plan references unanswered ledger questions: "
            + ", ".join(unresolved_refs)
        )

    return reasons


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expand_path(path: str) -> Path:
    return Path(path).expanduser()


def lock_path(chat_file: Path) -> Path:
    return Path(f"{chat_file}.lock")


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def acquire_lock(chat_file: Path, timeout: float = 10.0) -> Iterator[None]:
    lock = lock_path(chat_file)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {lock}")
            time.sleep(0.1)

    try:
        yield
    finally:
        try:
            os.rmdir(lock)
        except FileNotFoundError:
            pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _existing_paths(paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    existing: list[str] = []
    for path in paths:
        expanded = path.expanduser()
        if expanded.exists():
            resolved = str(expanded)
            if resolved not in seen:
                seen.add(resolved)
                existing.append(resolved)
    return existing


def _caveman_candidate_paths() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".claude" / "skills" / "caveman" / "SKILL.md",
        home / ".claude" / "skills" / "cavecrew" / "SKILL.md",
        home / ".claude" / "plugins" / "caveman" / "SKILL.md",
        home / ".claude" / "plugins" / "caveman" / ".claude-plugin" / "plugin.json",
        home / ".codex" / "skills" / "caveman" / "SKILL.md",
        home / ".codex" / "skills" / "cavecrew" / "SKILL.md",
        home / ".codex" / "plugins" / "caveman" / "SKILL.md",
        home / ".codex" / "plugins" / "caveman" / ".codex-plugin" / "plugin.json",
        home / ".agents" / "skills" / "caveman" / "SKILL.md",
        home / ".agents" / "skills" / "cavecrew" / "SKILL.md",
        home / ".config" / "caveman",
    ]

    extra = os.environ.get("COPLAN_CAVEMAN_PATHS", "")
    for value in extra.split(os.pathsep):
        value = value.strip()
        if value:
            candidates.append(Path(value))

    for base, pattern in (
        (home / ".claude" / "skills", "*caveman*/SKILL.md"),
        (home / ".claude" / "skills", "*cavecrew*/SKILL.md"),
        (home / ".codex" / "skills", "*caveman*/SKILL.md"),
        (home / ".codex" / "skills", "*cavecrew*/SKILL.md"),
        (home / ".agents" / "skills", "*caveman*/SKILL.md"),
        (home / ".agents" / "skills", "*cavecrew*/SKILL.md"),
    ):
        if base.exists():
            candidates.extend(base.glob(pattern))

    return candidates


def dependency_status() -> dict[str, object]:
    """Return optional compression dependency status without installing anything."""
    rtk_path = shutil.which("rtk")
    caveman_commands = [
        command
        for command in ("caveman", "caveman-compress", "caveman-shrink")
        if shutil.which(command) is not None
    ]
    caveman_paths = _existing_paths(_caveman_candidate_paths())
    caveman_available = bool(caveman_commands or caveman_paths)

    return {
        "policy": {
            "auto_install": False,
            "missing_tools_block": False,
            "required_when_available": True,
            "human_facing_messages": "normal",
            "agent_to_agent_messages": "use installed optional tools",
        },
        "rtk": {
            "available": rtk_path is not None,
            "path": rtk_path,
            "required_when_available": True,
            "usage": "Use rtk-wrapped shell exploration commands where practical.",
        },
        "caveman": {
            "available": caveman_available,
            "commands": caveman_commands,
            "paths": caveman_paths,
            "required_when_available": True,
            "usage": "Use terse caveman style for planner/advisor back-and-forth only.",
        },
    }


def read_body_file(path: Path) -> tuple[str | None, int | None]:
    if not path.exists():
        print(f"body file does not exist: {path}", file=sys.stderr)
        return None, 1
    body = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if not body:
        print("body must not be empty", file=sys.stderr)
        return None, 2
    return body, None


def has_consensus(text: str) -> bool:
    return bool(CONSENSUS_RE.search(text))


def parse_messages(text: str) -> list[dict[str, str]]:
    matches = list(HEADING_RE.finditer(text))
    messages: list[dict[str, str]] = []

    for index, match in enumerate(matches):
        body_start = match.end()
        if body_start < len(text) and text[body_start] == "\n":
            body_start += 1
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        messages.append(
            {
                "role": match.group(1),
                "timestamp": match.group(2),
                "body": body,
            }
        )

    return messages


def has_both_consensus_proposals(messages: list[dict[str, str]]) -> bool:
    """Whether both planner and advisor have posted a `--- proposing consensus ---` message."""
    seen: set[str] = set()
    for message in messages:
        if message["role"] in POLL_ROLES and PROPOSAL_MARKER in message["body"]:
            seen.add(message["role"])
    return seen == POLL_ROLES


def open_questions(state: dict[str, object]) -> list[dict[str, object]]:
    return [
        question
        for question in state.get("questions", [])
        if isinstance(question, dict) and question.get("state") == "open"
    ]


def open_question_ids(state: dict[str, object]) -> list[str]:
    return [str(question["id"]) for question in open_questions(state)]


def mentions_question_id(body: str, question_id: str) -> bool:
    return re.search(rf"\b{re.escape(question_id)}\b", body) is not None


def signoff_recap_refusal_reason(body: str) -> str | None:
    if not SIGNOFF_RECAP_READY_RE.search(body):
        return "body must state that the plan is ready for sign-off"
    if not SIGNOFF_RECAP_LEDGER_RE.search(body):
        return "body must include a Question Ledger recap or explicit Q-id references"
    return None


def clear_all_proposals_unsafe(state: dict[str, object]) -> bool:
    if state.get("proposed_plan") is None:
        return False
    state["proposed_plan"] = None
    return True


def withdraw_role_proposal_unsafe(state: dict[str, object], role: str) -> bool:
    proposed = state.get("proposed_plan")
    if not isinstance(proposed, dict):
        return False

    hashes = proposed.get("hashes")
    if not isinstance(hashes, dict):
        state["proposed_plan"] = None
        return True

    changed = False
    if hashes.get(role) is not None:
        hashes[role] = None
        changed = True

    if hashes.get("planner") is None and hashes.get("advisor") is None:
        state["proposed_plan"] = None

    return changed


def decide_next_action(chat_file: Path, self_role: str) -> dict[str, object]:
    """Return the deterministic next action for a planner/advisor loop."""
    text = read_text(chat_file)
    messages = parse_messages(text)
    last = messages[-1] if messages else None
    state = StateFile.load(chat_file) or {}
    StateFile._recompute_derived(state)
    ids = open_question_ids(state)
    derived = state.get("derived", {})
    plan_file_intact = bool(derived.get("plan_file_intact"))
    signoff_present = bool(derived.get("signoff_present"))

    base: dict[str, object] = {
        "file": str(chat_file),
        "role": self_role,
        "last_role": last["role"] if last else None,
        "open_question_ids": ids,
        "open_question_count": len(ids),
        "should_poll": False,
    }

    if has_consensus(text):
        return {
            **base,
            "action": "summarize_exit",
            "reason": "consensus_exists",
        }

    if ids:
        if self_role == "planner":
            return {
                **base,
                "action": "escalate_open_questions",
                "reason": "open_questions_require_planner_escalation",
            }
        return {
            **base,
            "action": "wait",
            "reason": "open_questions_wait_for_planner",
            "should_poll": True,
        }

    last_role = last["role"] if last else None
    last_body = last["body"] if last else ""
    last_has_escalation = ESCALATION_MARKER in last_body
    both_proposed = has_both_consensus_proposals(messages)

    proposals_ready_for_signoff = (
        both_proposed
        and plan_file_intact
        and not signoff_present
        and (last_role == "advisor" or (last_role == "planner" and PROPOSAL_MARKER in last_body))
    )
    if proposals_ready_for_signoff:
        action = "post_signoff_recap" if self_role == "planner" else "wait"
        return {
            **base,
            "action": action,
            "reason": "proposals_ready_for_signoff",
            "should_poll": action == "wait",
        }

    if last_role == "goal":
        action = "post" if self_role == "planner" else "wait"
    elif last_role == "planner":
        action = "wait" if last_has_escalation or self_role == "planner" else "post"
    elif last_role == "advisor":
        action = "post" if self_role == "planner" else "wait"
    elif last_role == "marcos":
        action = "post" if self_role == "planner" else "wait"
    elif last_role == "marcos-signoff":
        action = "post_consensus" if self_role == "planner" else "summarize_exit"
    elif last_role == "consensus":
        action = "summarize_exit"
    else:
        action = "wait"

    should_poll = action == "wait"
    return {
        **base,
        "action": action,
        "reason": "turn_matrix",
        "should_poll": should_poll,
    }


def default_plan_file(chat_file: Path) -> Path:
    return chat_file.with_name(f"{chat_file.stem}.plan.md")


def initial_plan_refusal_reasons(plan_file: Path) -> list[str]:
    if not plan_file.exists():
        return [f"initial planner turn must create plan file first: {plan_file}"]
    if plan_file.stat().st_size == 0:
        return [f"initial planner turn must not use an empty plan file: {plan_file}"]

    plan_text = plan_file.read_text(encoding="utf-8")
    headings = set(extract_plan_headings(plan_text))
    required = {"goal coverage", "decisions", "approach", "risks and verification"}
    missing = sorted(required - headings)
    if missing:
        return [
            "initial planner turn plan file is missing required draft headings: "
            + ", ".join(missing)
        ]
    return []


def turn_payload(chat_file: Path, self_role: str, next_action: dict[str, object]) -> dict[str, object]:
    """Map low-level next-action output to the small agent-facing protocol."""
    low_action = str(next_action["action"])
    payload: dict[str, object] = {
        "file": str(chat_file),
        "role": self_role,
        "low_level_action": low_action,
        "last_role": next_action.get("last_role"),
        "open_question_ids": next_action.get("open_question_ids", []),
        "open_question_count": next_action.get("open_question_count", 0),
        "plan_file": str(default_plan_file(chat_file)),
        "optional_dependencies": dependency_status(),
    }

    if low_action == "post":
        initial = self_role == "planner" and next_action.get("last_role") == "goal"
        return {
            **payload,
            "action": "compose_initial_plan" if initial else "compose_turn",
            "post_command": "post",
            "must_create_plan_file": initial,
        }
    if low_action == "escalate_open_questions":
        initial = self_role == "planner" and next_action.get("last_role") == "goal"
        return {
            **payload,
            "action": "compose_escalation",
            "post_command": "post",
            "stop_after_post": True,
            "must_create_plan_file": initial,
        }
    if low_action == "post_signoff_recap":
        return {
            **payload,
            "action": "compose_signoff_recap",
            "post_command": "post",
            "stop_after_post": True,
        }
    if low_action == "summarize_exit":
        return {
            **payload,
            "action": "closed",
            "reason": next_action.get("reason", "summarize_exit"),
        }
    if low_action == "wait":
        return {
            **payload,
            "action": "wait",
            "reason": next_action.get("reason", "turn_matrix"),
        }
    return {
        **payload,
        "action": low_action,
        "reason": next_action.get("reason", "turn_matrix"),
    }


def await_turn(
    chat_file: Path,
    self_role: str,
    timeout: float,
    poll_interval: float,
    lock_timeout: float = 10.0,
) -> tuple[int, dict[str, object]]:
    """Wait until this role has an actionable high-level turn or a stop state."""
    timeout = max(timeout, 0.0)
    poll_interval = max(poll_interval, 0.1)
    deadline = time.monotonic() + timeout

    while True:
        next_action = decide_next_action(chat_file, self_role)
        low_action = next_action["action"]
        if low_action == "post_consensus" and self_role == "planner":
            return write_consensus_for_turn(chat_file, lock_timeout)
        if low_action != "wait":
            return 0, turn_payload(chat_file, self_role, next_action)

        initial = inspect_chat(chat_file)
        initial_count = initial["message_count"]
        initial_timestamp = initial["last_timestamp"]

        while True:
            now = time.monotonic()
            if now >= deadline:
                return 2, {
                    "action": "timeout",
                    "file": str(chat_file),
                    "role": self_role,
                    "waiting_self": self_role,
                    "last_role": initial["last_role"],
                    "next_action": next_action,
                }

            time.sleep(min(poll_interval, deadline - now))
            current = inspect_chat(chat_file)
            if current["consensus_exists"]:
                return 0, {
                    "action": "closed",
                    "file": str(chat_file),
                    "role": self_role,
                    "reason": "consensus_exists",
                }

            appended = (
                current["message_count"] > initial_count
                or current["last_timestamp"] != initial_timestamp
            )
            if appended and current["last_role"] != self_role:
                break


def command_turn(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    self_role = args.self_role.strip()

    if self_role not in POLL_ROLES:
        print(f"--self must be one of {sorted(POLL_ROLES)}, got: {self_role}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    code, payload = await_turn(
        chat_file,
        self_role,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        lock_timeout=args.lock_timeout,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


class StateFile:
    """Sidecar JSON state for the Question Ledger, plan-file proposals, and sign-off.

    Layout, written pretty-printed alongside the chat file as
    `<chat>.state.json`:

        {
          "version": 2,
          "chat_file": "agent-chat.md",
          "created_at": "<iso>",
          "questions": [{"id": "Q1", "state": "open"|"answered", ...}, ...],
          "proposed_plan": null | {
            "path": "<absolute path to the plan file>",
            "hashes": {
              "planner": "<sha256>" | null,
              "advisor": "<sha256>" | null
            }
          },
          "signoff": null | {"signed_at": "<iso>"},
          "derived": {
            "open_question_count": 0,
            "answered_question_count": 0,
            "signoff_present": false,
            "proposed_plan_path": null | "<path>",
            "proposed_plan_hashes_match": false
          }
        }

    A valid co-plan chat always has both the `.md` and the `.state.json`.
    Commands that touch the sidecar refuse if it is missing; `init` is the
    only command that creates one.
    """

    @staticmethod
    def path(chat_file: Path) -> Path:
        return Path(f"{chat_file}.state.json")

    @classmethod
    def exists(cls, chat_file: Path) -> bool:
        return cls.path(chat_file).exists()

    @classmethod
    def fresh(cls, chat_file: Path, created_at: str) -> dict[str, object]:
        return cls._recompute_derived(
            {
                "version": STATE_SCHEMA_VERSION,
                "chat_file": chat_file.name,
                "created_at": created_at,
                "questions": [],
                "proposed_plan": None,
                "signoff": None,
                "derived": {},
            }
        )

    @classmethod
    def load(cls, chat_file: Path) -> dict[str, object] | None:
        sidecar = cls.path(chat_file)
        if not sidecar.exists():
            return None
        return json.loads(sidecar.read_text(encoding="utf-8"))

    @classmethod
    def save(cls, chat_file: Path, state: dict[str, object]) -> None:
        sidecar = cls.path(chat_file)
        state = cls._recompute_derived(state)
        payload = json.dumps(state, indent=2, sort_keys=False) + "\n"
        tmp = Path(f"{sidecar}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, sidecar)

    @staticmethod
    def next_question_id(state: dict[str, object]) -> str:
        max_id = 0
        for question in state.get("questions", []):
            match = QUESTION_ID_RE.match(question["id"])
            if match:
                max_id = max(max_id, int(match.group(1)))
        return f"Q{max_id + 1}"

    @staticmethod
    def find_question(state: dict[str, object], question_id: str) -> dict[str, object] | None:
        for question in state.get("questions", []):
            if question["id"] == question_id:
                return question
        return None

    @staticmethod
    def _recompute_derived(state: dict[str, object]) -> dict[str, object]:
        questions = state.get("questions", [])
        open_count = sum(1 for q in questions if q["state"] == "open")
        answered_count = sum(1 for q in questions if q["state"] == "answered")
        proposed = state.get("proposed_plan") or {}
        hashes = proposed.get("hashes", {}) if isinstance(proposed, dict) else {}
        plan_path = proposed.get("path") if isinstance(proposed, dict) else None
        planner_hash = hashes.get("planner") if isinstance(hashes, dict) else None
        advisor_hash = hashes.get("advisor") if isinstance(hashes, dict) else None
        both_present = bool(planner_hash) and bool(advisor_hash)
        hashes_equal = both_present and planner_hash == advisor_hash
        plan_file_current_hash = sha256_file(Path(plan_path)) if plan_path else None
        plan_file_intact = (
            hashes_equal
            and plan_file_current_hash is not None
            and plan_file_current_hash == planner_hash
        )
        state["derived"] = {
            "open_question_count": open_count,
            "answered_question_count": answered_count,
            "signoff_present": state.get("signoff") is not None,
            "proposed_plan_path": plan_path,
            "proposed_plan_hashes_equal": hashes_equal,
            "proposed_plan_hashes_match": plan_file_intact,
            "plan_file_current_hash": plan_file_current_hash,
            "plan_file_intact": plan_file_intact,
        }
        return state


def inspect_chat(chat_file: Path) -> dict[str, object]:
    text = read_text(chat_file)
    messages = parse_messages(text)
    last = messages[-1] if messages else None
    stat = chat_file.stat() if chat_file.exists() else None
    last_body = last["body"] if last else ""

    state = StateFile.load(chat_file) or {}
    StateFile._recompute_derived(state)
    derived = state.get("derived", {})
    proposed = state.get("proposed_plan") or {}
    proposed_path = proposed.get("path") if isinstance(proposed, dict) else None
    proposed_hashes = (
        proposed.get("hashes", {}) if isinstance(proposed, dict) else {}
    )

    plan_file_current_hash = derived.get("plan_file_current_hash")

    planner_hash = proposed_hashes.get("planner") if isinstance(proposed_hashes, dict) else None
    advisor_hash = proposed_hashes.get("advisor") if isinstance(proposed_hashes, dict) else None
    plan_file_intact = (
        plan_file_current_hash is not None
        and bool(planner_hash)
        and bool(advisor_hash)
        and planner_hash == advisor_hash == plan_file_current_hash
    )

    return {
        "file": str(chat_file),
        "exists": chat_file.exists(),
        "consensus_exists": has_consensus(text),
        "message_count": len(messages),
        "last_role": last["role"] if last else None,
        "last_timestamp": last["timestamp"] if last else None,
        "last_body": last_body,
        "last_message_contains_proposal": PROPOSAL_MARKER in last_body,
        "last_message_contains_confirmation": CONFIRM_MARKER in last_body,
        "last_message_contains_escalation": ESCALATION_MARKER in last_body,
        "both_proposals_present": has_both_consensus_proposals(messages),
        "open_question_count": derived.get("open_question_count", 0),
        "answered_question_count": derived.get("answered_question_count", 0),
        "signoff_present": derived.get("signoff_present", False),
        "proposed_plan_path": proposed_path,
        "proposed_plan_hashes": {
            "planner": planner_hash,
            "advisor": advisor_hash,
        },
        "proposed_plan_hashes_equal": derived.get("proposed_plan_hashes_equal", False),
        "proposed_plan_hashes_match": derived.get("proposed_plan_hashes_match", False),
        "plan_file_current_hash": plan_file_current_hash,
        "plan_file_intact": derived.get("plan_file_intact", plan_file_intact),
        "mtime_ns": stat.st_mtime_ns if stat else None,
        "sha256": sha256_file(chat_file),
    }


def consensus_refusal_reasons(chat_file: Path, state: dict[str, object]) -> tuple[list[str], str | None, str | None]:
    StateFile._recompute_derived(state)
    open_count = state["derived"]["open_question_count"]
    signoff_present = state["derived"]["signoff_present"]
    proposed = state.get("proposed_plan") or {}
    plan_path = proposed.get("path") if isinstance(proposed, dict) else None
    hashes = proposed.get("hashes", {}) if isinstance(proposed, dict) else {}
    planner_hash = hashes.get("planner") if isinstance(hashes, dict) else None
    advisor_hash = hashes.get("advisor") if isinstance(hashes, dict) else None
    reasons: list[str] = []

    if open_count > 0:
        reasons.append("open questions: " + ", ".join(open_question_ids(state)))
    if not signoff_present:
        reasons.append("marcos sign-off missing")
    if not plan_path:
        reasons.append("no plan file registered")
    elif not planner_hash or not advisor_hash:
        reasons.append("both planner and advisor must run propose-consensus")
    elif planner_hash != advisor_hash:
        reasons.append("planner/advisor proposed against different plan-file contents")
    else:
        current_plan_hash = sha256_file(Path(plan_path))
        if current_plan_hash is None:
            reasons.append(f"plan file missing at {plan_path}")
        elif current_plan_hash != planner_hash:
            reasons.append("plan file has changed since proposal (sha mismatch)")

    return reasons, plan_path, planner_hash


def _check_chat_and_sidecar(chat_file: Path) -> int | None:
    """Print a friendly stderr message and return an exit code if either file is missing."""
    if not chat_file.exists():
        print(f"chat file does not exist: {chat_file}", file=sys.stderr)
        return 1
    if not StateFile.exists(chat_file):
        print(
            f"sidecar missing for chat: {chat_file} "
            "(this chat was not created via `init` or its sidecar was deleted)",
            file=sys.stderr,
        )
        return 1
    return None


def command_init(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    if args.body_file is not None:
        body_file = expand_path(args.body_file)
        if not body_file.exists():
            print(f"body file does not exist: {body_file}", file=sys.stderr)
            return 1
        goal = body_file.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    else:
        goal = args.goal.strip()
    if not goal:
        print("goal must not be empty", file=sys.stderr)
        return 2

    chat_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            if chat_file.exists():
                print(json.dumps({"created": False, "file": str(chat_file)}, indent=2))
                return 0

            timestamp = utc_timestamp()
            content = (
                "# Co-plan chat\n\n"
                "This file is the shared planning record for a two-agent planning chat. "
                "Messages are append-only. The planner writes `### [consensus]` when "
                "the plan is agreed.\n\n"
                "---\n\n"
                f"### [goal] {timestamp}\n\n"
                f"{goal}\n\n"
            )
            chat_file.write_text(content, encoding="utf-8")
            StateFile.save(chat_file, StateFile.fresh(chat_file, timestamp))
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "created": True,
                "file": str(chat_file),
                "sidecar": str(StateFile.path(chat_file)),
            },
            indent=2,
        )
    )
    return 0


def command_update_goal(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    body_file = expand_path(args.body_file)

    if not chat_file.exists():
        print(f"chat file does not exist: {chat_file}", file=sys.stderr)
        return 1
    if not body_file.exists():
        print(f"body file does not exist: {body_file}", file=sys.stderr)
        return 1

    body = body_file.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if not body:
        print("body must not be empty", file=sys.stderr)
        return 2

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            messages = parse_messages(current)
            if not messages or messages[0]["role"] != "goal":
                print("chat file has no goal section", file=sys.stderr)
                return 1
            if any(message["role"] != "goal" for message in messages):
                print(
                    "refusing to update goal after non-goal messages exist",
                    file=sys.stderr,
                )
                return 1

            goal_match = GOAL_HEADING_RE.search(current)
            if not goal_match:
                print("goal heading not found", file=sys.stderr)
                return 1

            insertion = current.rstrip("\n")
            if not insertion.endswith("\n"):
                insertion += "\n"
            insertion += f"\n{body}\n"
            chat_file.write_text(insertion, encoding="utf-8")
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({"updated": True, "file": str(chat_file)}, indent=2))
    return 0


def command_read(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    if not chat_file.exists():
        print(f"chat file does not exist: {chat_file}", file=sys.stderr)
        return 1
    print(chat_file.read_text(encoding="utf-8"), end="")
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    print(json.dumps(inspect_chat(chat_file), indent=2, sort_keys=True))
    return 0


def command_deps_status(args: argparse.Namespace) -> int:
    print(json.dumps(dependency_status(), indent=2, sort_keys=True))
    return 0


def command_next_action(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    self_role = args.self_role.strip()

    if self_role not in POLL_ROLES:
        print(f"--self must be one of {sorted(POLL_ROLES)}, got: {self_role}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    print(json.dumps(decide_next_action(chat_file, self_role), indent=2, sort_keys=True))
    return 0


def command_post_turn(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    body_file = expand_path(args.body_file)
    role = args.self_role.strip()

    if role not in POLL_ROLES:
        print(f"--self must be one of {sorted(POLL_ROLES)}, got: {role}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    body, body_error = read_body_file(body_file)
    if body_error is not None:
        return body_error
    if ESCALATION_MARKER in body:
        print(
            "refusing to post turn: use escalate-open-questions for escalations",
            file=sys.stderr,
        )
        return 3
    if PROPOSAL_MARKER in body:
        print(
            "refusing to post turn: use propose-consensus --body-file for proposal receipts",
            file=sys.stderr,
        )
        return 3

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                print("refusing to post turn after consensus exists", file=sys.stderr)
                return 3

            next_action = decide_next_action(chat_file, role)
            if next_action["action"] != "post":
                print(
                    "refusing to post turn: next action for "
                    f"{role} is {next_action['action']}",
                    file=sys.stderr,
                )
                print(json.dumps(next_action, indent=2, sort_keys=True))
                return 3

            state = StateFile.load(chat_file)
            _write_message_unsafe(chat_file, role, body, utc_timestamp())
            withdraw_role_proposal_unsafe(state, role)
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    next_action_after = decide_next_action(chat_file, role)
    print(
        json.dumps(
            {
                "posted": True,
                "file": str(chat_file),
                "role": role,
                "next_action": next_action_after,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_post_signoff_recap(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    body_file = expand_path(args.body_file)

    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    body, body_error = read_body_file(body_file)
    if body_error is not None:
        return body_error
    recap_error = signoff_recap_refusal_reason(body)
    if recap_error is not None:
        print(f"refusing to post sign-off recap: {recap_error}", file=sys.stderr)
        return 3

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                print("refusing to post sign-off recap after consensus exists", file=sys.stderr)
                return 3

            next_action = decide_next_action(chat_file, "planner")
            if next_action["action"] != "post_signoff_recap":
                print(
                    "refusing to post sign-off recap: next action for planner "
                    f"is {next_action['action']}",
                    file=sys.stderr,
                )
                print(json.dumps(next_action, indent=2, sort_keys=True))
                return 3

            _write_message_unsafe(chat_file, "planner", body, utc_timestamp())
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "posted": True,
                "file": str(chat_file),
                "role": "planner",
                "kind": "signoff_recap",
                "next_action": decide_next_action(chat_file, "planner"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _write_message_unsafe(chat_file: Path, role: str, body: str, timestamp: str) -> None:
    """Append a `### [role] timestamp` block. Caller must hold the lock."""
    current = read_text(chat_file)
    separator = "" if current.endswith("\n\n") else "\n" if current.endswith("\n") else "\n\n"
    message = f"{separator}### [{role}] {timestamp}\n\n{body}\n\n"
    with chat_file.open("a", encoding="utf-8") as handle:
        handle.write(message)


def _record_proposal_from_body_unsafe(
    chat_file: Path,
    role: str,
    body: str,
    plan_file: Path,
    state: dict[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    """Append a proposal body and store its plan hash. Caller must hold the lock."""
    if not plan_file.exists():
        return None, f"plan file does not exist: {plan_file}"
    if plan_file.stat().st_size == 0:
        return None, f"plan file is empty: {plan_file}"
    if PROPOSAL_MARKER not in body:
        body = f"{body.rstrip()}\n\n{PROPOSAL_MARKER}"
    receipt_header_match = PLAN_REVIEW_RECEIPT_RE.search(body)
    if receipt_header_match is None:
        return (
            None,
            "proposal body has no `## Plan Review Receipt` section. "
            "The receipt is the agent's attestation that it reviewed the "
            "full current plan file end-to-end.",
        )

    plan_text = plan_file.read_text(encoding="utf-8")
    validation_reasons = validate_plan_text(plan_text, state)
    if validation_reasons:
        return None, "plan validation failed: " + "; ".join(validation_reasons)

    plan_headings = extract_plan_headings(plan_text)
    if plan_headings:
        receipt_section = body[receipt_header_match.end() :].lower()
        missing = [
            heading for heading in plan_headings
            if heading and heading not in receipt_section
        ]
        if missing:
            listed = "; ".join(f'"{h}"' for h in missing[:5])
            suffix = "" if len(missing) <= 5 else f" (+ {len(missing) - 5} more)"
            return (
                None,
                "Plan Review Receipt does not enumerate every plan-file "
                f"`##` / `###` heading. Missing: {listed}{suffix}.",
            )

    timestamp = utc_timestamp()
    _write_message_unsafe(chat_file, role, body, timestamp)

    plan_hash = sha256_file(plan_file)
    plan_path_abs = str(plan_file.resolve())
    proposed = state.get("proposed_plan")
    if not isinstance(proposed, dict) or proposed.get("path") != plan_path_abs:
        proposed = {
            "path": plan_path_abs,
            "hashes": {"planner": None, "advisor": None},
        }
    else:
        proposed.setdefault("hashes", {})
        proposed["hashes"].setdefault("planner", None)
        proposed["hashes"].setdefault("advisor", None)

    proposed["hashes"][role] = plan_hash
    state["proposed_plan"] = proposed
    StateFile.save(chat_file, state)

    other = "advisor" if role == "planner" else "planner"
    other_hash = proposed["hashes"].get(other)
    return (
        {
            "kind": "proposal",
            "role": role,
            "plan_file": plan_path_abs,
            "plan_hash": plan_hash,
            "other_role_hash": other_hash,
            "hashes_match": bool(other_hash) and other_hash == plan_hash,
            "both_proposed": bool(other_hash),
        },
        None,
    )


def command_post(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    body_file = expand_path(args.body_file)
    plan_file = expand_path(args.plan_file) if args.plan_file else default_plan_file(chat_file)
    role = args.self_role.strip()

    if role not in POLL_ROLES:
        print(f"--self must be one of {sorted(POLL_ROLES)}, got: {role}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    body, body_error = read_body_file(body_file)
    if body_error is not None:
        return body_error

    posted: dict[str, object]
    follow_turn = True
    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                print("refusing to post after consensus exists", file=sys.stderr)
                return 3

            next_action = decide_next_action(chat_file, role)
            low_action = next_action["action"]
            state = StateFile.load(chat_file)

            if low_action == "post":
                is_proposal = (
                    PROPOSAL_MARKER in body
                    or PLAN_REVIEW_RECEIPT_RE.search(body) is not None
                )
                if is_proposal:
                    posted_or_none, proposal_error = _record_proposal_from_body_unsafe(
                        chat_file,
                        role,
                        body,
                        plan_file,
                        state,
                    )
                    if proposal_error is not None:
                        print(f"refusing to post proposal: {proposal_error}", file=sys.stderr)
                        return 3
                    posted = {"posted": True, **posted_or_none}
                else:
                    if ESCALATION_MARKER in body:
                        print(
                            "refusing to post turn: current action is a normal turn, "
                            "not an escalation",
                            file=sys.stderr,
                        )
                        return 3
                    if role == "planner" and next_action.get("last_role") == "goal":
                        reasons = initial_plan_refusal_reasons(plan_file)
                        if reasons:
                            print("refusing to post initial planner turn: " + "; ".join(reasons), file=sys.stderr)
                            return 3
                    _write_message_unsafe(chat_file, role, body, utc_timestamp())
                    withdraw_role_proposal_unsafe(state, role)
                    StateFile.save(chat_file, state)
                    posted = {"posted": True, "kind": "turn", "role": role}
            elif low_action == "escalate_open_questions" and role == "planner":
                if next_action.get("last_role") == "goal":
                    reasons = initial_plan_refusal_reasons(plan_file)
                    if reasons:
                        print("refusing to post initial planner escalation: " + "; ".join(reasons), file=sys.stderr)
                        return 3
                ids = open_question_ids(state)
                missing = [
                    question_id for question_id in ids
                    if not mentions_question_id(body, question_id)
                ]
                if missing:
                    print(
                        "refusing to post escalation: body does not mention all "
                        "open questions; missing: " + ", ".join(missing),
                        file=sys.stderr,
                    )
                    return 3
                if ESCALATION_MARKER not in body:
                    body = f"{body.rstrip()}\n\n{ESCALATION_MARKER}"
                _write_message_unsafe(chat_file, "planner", body, utc_timestamp())
                clear_all_proposals_unsafe(state)
                StateFile.save(chat_file, state)
                posted = {
                    "posted": True,
                    "kind": "escalation",
                    "role": "planner",
                    "open_question_ids": ids,
                    "stop": "waiting_for_marcos",
                }
                follow_turn = False
            elif low_action == "post_signoff_recap" and role == "planner":
                recap_error = signoff_recap_refusal_reason(body)
                if recap_error is not None:
                    print(
                        f"refusing to post sign-off recap: {recap_error}",
                        file=sys.stderr,
                    )
                    return 3
                _write_message_unsafe(chat_file, "planner", body, utc_timestamp())
                posted = {
                    "posted": True,
                    "kind": "signoff_recap",
                    "role": "planner",
                    "stop": "waiting_for_marcos_signoff",
                }
                follow_turn = False
            else:
                print(
                    "refusing to post: next action for "
                    f"{role} is {low_action}; run `turn` first",
                    file=sys.stderr,
                )
                print(json.dumps(turn_payload(chat_file, role, next_action), indent=2, sort_keys=True))
                return 3
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not follow_turn:
        print(json.dumps({"file": str(chat_file), **posted}, indent=2, sort_keys=True))
        return 0

    code, payload = await_turn(
        chat_file,
        role,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        lock_timeout=args.lock_timeout,
    )
    print(
        json.dumps(
            {
                "file": str(chat_file),
                **posted,
                "turn": payload,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return code


def command_question(args: argparse.Namespace) -> int:
    args.role = args.self_role
    return command_add_question(args)


def command_add_question(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    role = args.role.strip()
    question_text = args.question.strip()

    if role not in LEDGER_RAISE_ROLES:
        print(f"--role must be one of {sorted(LEDGER_RAISE_ROLES)}, got: {role}", file=sys.stderr)
        return 2
    if not question_text:
        print("--question must not be empty", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            state = StateFile.load(chat_file)
            if has_consensus(read_text(chat_file)):
                print("refusing to add a question after consensus exists", file=sys.stderr)
                return 3
            if state.get("signoff") is not None:
                print("refusing to add a question after sign-off", file=sys.stderr)
                return 3

            question_id = StateFile.next_question_id(state)
            state.setdefault("questions", []).append(
                {
                    "id": question_id,
                    "state": "open",
                    "question": question_text,
                    "raised_by": role,
                    "raised_at": utc_timestamp(),
                    "answered_at": None,
                    "answer": None,
                }
            )
            clear_all_proposals_unsafe(state)
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "added": True,
                "id": question_id,
                "raised_by": role,
                "file": str(chat_file),
            },
            indent=2,
        )
    )
    return 0


def command_escalate_open_questions(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    body_file = expand_path(args.body_file)

    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    if not body_file.exists():
        print(f"body file does not exist: {body_file}", file=sys.stderr)
        return 1

    body = body_file.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if not body:
        print("body must not be empty", file=sys.stderr)
        return 2

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                print("refusing to escalate after consensus exists", file=sys.stderr)
                return 3
            state = StateFile.load(chat_file)
            if state.get("signoff") is not None:
                print("refusing to escalate after sign-off", file=sys.stderr)
                return 3

            ids = open_question_ids(state)
            if not ids:
                print("refusing to escalate: no open questions", file=sys.stderr)
                return 3

            missing = [
                question_id
                for question_id in ids
                if not mentions_question_id(body, question_id)
            ]
            if missing:
                print(
                    "refusing to escalate: body does not mention all open questions; "
                    "missing: " + ", ".join(missing),
                    file=sys.stderr,
                )
                return 3

            if ESCALATION_MARKER not in body:
                body = f"{body.rstrip()}\n\n{ESCALATION_MARKER}"

            _write_message_unsafe(chat_file, "planner", body, utc_timestamp())
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "escalated": True,
                "file": str(chat_file),
                "role": "planner",
                "open_question_ids": ids,
            },
            indent=2,
        )
    )
    return 0


def command_answer_question(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    question_id = args.id.strip()
    answer_text = args.answer.strip()

    if not QUESTION_ID_RE.match(question_id):
        print(f"--id must look like Q<n>, got: {question_id}", file=sys.stderr)
        return 2
    if not answer_text:
        print("--answer must not be empty", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            state = StateFile.load(chat_file)
            if has_consensus(read_text(chat_file)):
                print("refusing to answer a question after consensus exists", file=sys.stderr)
                return 3

            question = StateFile.find_question(state, question_id)
            if question is None:
                print(f"no such question: {question_id}", file=sys.stderr)
                return 3

            previously_answered = question["state"] == "answered"
            timestamp = utc_timestamp()
            question["state"] = "answered"
            question["answer"] = answer_text
            question["answered_at"] = timestamp

            body = f"**{question_id}**: {answer_text}"
            _write_message_unsafe(chat_file, "marcos", body, timestamp)
            clear_all_proposals_unsafe(state)
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "answered": True,
                "id": question_id,
                "previously_answered": previously_answered,
                "file": str(chat_file),
            },
            indent=2,
        )
    )
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    if args.body_file is not None:
        body_file = expand_path(args.body_file)
        body, body_error = read_body_file(body_file)
        if body_error is not None:
            return body_error
        decision = body
    else:
        decision = args.decision.strip()
        if not decision:
            print("--decision must not be empty", file=sys.stderr)
            return 2

    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            state = StateFile.load(chat_file)
            if has_consensus(read_text(chat_file)):
                print("refusing to resolve after consensus exists", file=sys.stderr)
                return 3

            timestamp = utc_timestamp()
            _write_message_unsafe(chat_file, "marcos", decision, timestamp)
            cleared_proposals = clear_all_proposals_unsafe(state)
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "resolved": True,
                "file": str(chat_file),
                "role": "marcos",
                "timestamp": timestamp,
                "cleared_proposals": cleared_proposals,
            },
            indent=2,
        )
    )
    return 0


def command_list_questions(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    state_filter = args.state

    if state_filter not in {"open", "answered", "all"}:
        print(f"--state must be open|answered|all, got: {state_filter}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    state = StateFile.load(chat_file)
    questions = state.get("questions", [])
    if state_filter != "all":
        questions = [q for q in questions if q["state"] == state_filter]

    print(json.dumps({"questions": questions}, indent=2))
    return 0


def command_validate_plan(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    plan_file = expand_path(args.plan_file)

    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    if not plan_file.exists():
        print(f"plan file does not exist: {plan_file}", file=sys.stderr)
        return 1
    if plan_file.stat().st_size == 0:
        print(f"plan file is empty: {plan_file}", file=sys.stderr)
        return 2

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            state = StateFile.load(chat_file)
            StateFile._recompute_derived(state)
            plan_text = plan_file.read_text(encoding="utf-8")
            reasons = validate_plan_text(plan_text, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if reasons:
        print(
            "plan validation failed: " + "; ".join(reasons),
            file=sys.stderr,
        )
        print(
            json.dumps(
                {
                    "valid": False,
                    "file": str(chat_file),
                    "plan_file": str(plan_file),
                    "reasons": reasons,
                },
                indent=2,
            )
        )
        return 3

    print(
        json.dumps(
            {
                "valid": True,
                "file": str(chat_file),
                "plan_file": str(plan_file),
            },
            indent=2,
        )
    )
    return 0


def command_propose_consensus(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    plan_file = expand_path(args.plan_file)
    body_file = expand_path(args.body_file) if args.body_file is not None else None
    role = args.self_role.strip()

    if role not in POLL_ROLES:
        print(f"--self must be one of {sorted(POLL_ROLES)}, got: {role}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error
    if not plan_file.exists():
        print(f"plan file does not exist: {plan_file}", file=sys.stderr)
        return 1
    if plan_file.stat().st_size == 0:
        print(f"plan file is empty: {plan_file}", file=sys.stderr)
        return 2
    body: str | None = None
    if body_file is not None:
        body, body_error = read_body_file(body_file)
        if body_error is not None:
            return body_error

    plan_path_abs = str(plan_file.resolve())

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                print("refusing to propose: consensus already written", file=sys.stderr)
                return 3

            state = StateFile.load(chat_file)
            if state.get("signoff") is not None:
                print(
                    "refusing to propose: sign-off already recorded; plan is locked",
                    file=sys.stderr,
                )
                return 3

            if body is not None:
                next_action = decide_next_action(chat_file, role)
                if next_action["action"] != "post":
                    print(
                        "refusing to propose: next action for "
                        f"{role} is {next_action['action']}",
                        file=sys.stderr,
                    )
                    print(json.dumps(next_action, indent=2, sort_keys=True))
                    return 3
                if PROPOSAL_MARKER not in body:
                    body = f"{body.rstrip()}\n\n{PROPOSAL_MARKER}"
                if PLAN_REVIEW_RECEIPT_RE.search(body) is None:
                    print(
                        f"refusing to propose: role '{role}' proposal body has no "
                        "`## Plan Review Receipt` section. The receipt is the "
                        "agent's attestation that they reviewed the full current "
                        "plan file end-to-end.",
                        file=sys.stderr,
                    )
                    return 3
                _write_message_unsafe(chat_file, role, body, utc_timestamp())
                current = read_text(chat_file)

            messages = parse_messages(current)
            latest_self_message = None
            for message in reversed(messages):
                if message["role"] == role:
                    latest_self_message = message
                    break
            if latest_self_message is None:
                print(
                    f"refusing to propose: role '{role}' has not posted any chat "
                    "message yet; the message containing the `## Plan Review "
                    "Receipt` and `--- proposing consensus ---` marker must be "
                    "posted before calling propose-consensus, or passed via "
                    "`--body-file`",
                    file=sys.stderr,
                )
                return 3
            receipt_header_match = PLAN_REVIEW_RECEIPT_RE.search(
                latest_self_message["body"]
            )
            if PROPOSAL_MARKER not in latest_self_message["body"]:
                print(
                    f"refusing to propose: role '{role}' most recent chat message "
                    f"does not contain `{PROPOSAL_MARKER}`.",
                    file=sys.stderr,
                )
                return 3
            if receipt_header_match is None:
                print(
                    f"refusing to propose: role '{role}' most recent chat message "
                    "has no `## Plan Review Receipt` section. The receipt is the "
                    "agent's attestation that they reviewed the full current plan "
                    "file end-to-end. Add a `## Plan Review Receipt` section to "
                    "the message and re-run propose-consensus.",
                    file=sys.stderr,
                )
                return 3

            plan_text = plan_file.read_text(encoding="utf-8")
            validation_reasons = validate_plan_text(plan_text, state)
            if validation_reasons:
                print(
                    "refusing to propose: plan validation failed: "
                    + "; ".join(validation_reasons),
                    file=sys.stderr,
                )
                return 3

            plan_hash = sha256_file(plan_file)
            plan_headings = extract_plan_headings(plan_text)
            if plan_headings:
                receipt_section = latest_self_message["body"][
                    receipt_header_match.end() :
                ].lower()
                missing = [
                    heading for heading in plan_headings
                    if heading and heading not in receipt_section
                ]
                if missing:
                    listed = "; ".join(f'"{h}"' for h in missing[:5])
                    suffix = (
                        ""
                        if len(missing) <= 5
                        else f" (+ {len(missing) - 5} more)"
                    )
                    print(
                        "refusing to propose: Plan Review Receipt does not "
                        "enumerate every plan-file `##` / `###` heading. "
                        f"Missing: {listed}{suffix}. The receipt must mention "
                        "each heading by name (case-insensitive substring "
                        "match within the section that follows "
                        "`## Plan Review Receipt`).",
                        file=sys.stderr,
                    )
                    return 3

            proposed = state.get("proposed_plan")
            if not isinstance(proposed, dict) or proposed.get("path") != plan_path_abs:
                proposed = {
                    "path": plan_path_abs,
                    "hashes": {"planner": None, "advisor": None},
                }
            else:
                proposed.setdefault("hashes", {})
                proposed["hashes"].setdefault("planner", None)
                proposed["hashes"].setdefault("advisor", None)

            proposed["hashes"][role] = plan_hash
            state["proposed_plan"] = proposed
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    other = "advisor" if role == "planner" else "planner"
    other_hash = proposed["hashes"].get(other)
    hashes_match = bool(other_hash) and other_hash == plan_hash

    print(
        json.dumps(
            {
                "proposed": True,
                "role": role,
                "plan_file": plan_path_abs,
                "plan_hash": plan_hash,
                "other_role_hash": other_hash,
                "hashes_match": hashes_match,
                "both_proposed": bool(other_hash),
            },
            indent=2,
        )
    )
    return 0


def command_signoff(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            state = StateFile.load(chat_file)
            current = read_text(chat_file)
            if has_consensus(current):
                print("consensus already written; sign-off has no effect", file=sys.stderr)
                return 3
            if state.get("signoff") is not None:
                print("sign-off already recorded", file=sys.stderr)
                return 3

            StateFile._recompute_derived(state)
            open_count = state["derived"]["open_question_count"]
            if open_count > 0:
                open_ids = [q["id"] for q in state.get("questions", []) if q["state"] == "open"]
                print(
                    "refusing to sign off with open questions: " + ", ".join(open_ids),
                    file=sys.stderr,
                )
                return 3

            messages = parse_messages(current)
            if not has_both_consensus_proposals(messages):
                print(
                    "refusing to sign off: both planner and advisor must post "
                    f"'{PROPOSAL_MARKER}' first",
                    file=sys.stderr,
                )
                return 3

            proposed = state.get("proposed_plan") or {}
            plan_path = proposed.get("path") if isinstance(proposed, dict) else None
            hashes = proposed.get("hashes", {}) if isinstance(proposed, dict) else {}
            planner_hash = hashes.get("planner") if isinstance(hashes, dict) else None
            advisor_hash = hashes.get("advisor") if isinstance(hashes, dict) else None

            if not plan_path or not planner_hash or not advisor_hash:
                missing = []
                if not plan_path:
                    missing.append("no plan file registered")
                if not planner_hash:
                    missing.append("planner has not run propose-consensus")
                if not advisor_hash:
                    missing.append("advisor has not run propose-consensus")
                print(
                    "refusing to sign off: " + "; ".join(missing),
                    file=sys.stderr,
                )
                return 3

            if planner_hash != advisor_hash:
                print(
                    "refusing to sign off: planner and advisor proposed against "
                    "different plan file contents (hashes differ — the proposing "
                    "agent must re-run propose-consensus against the current file)",
                    file=sys.stderr,
                )
                return 3

            current_hash = sha256_file(Path(plan_path))
            if current_hash is None:
                print(
                    f"refusing to sign off: plan file missing at {plan_path}",
                    file=sys.stderr,
                )
                return 3
            if current_hash != planner_hash:
                print(
                    "refusing to sign off: plan file has changed since proposal "
                    f"(stored {planner_hash[:12]}…, current {current_hash[:12]}…); "
                    "both agents must re-run propose-consensus against the current file",
                    file=sys.stderr,
                )
                return 3

            timestamp = utc_timestamp()
            state["signoff"] = {"signed_at": timestamp}
            body = (
                "Open questions resolved. Plan file reviewed and approved. "
                "Ready for consensus."
            )
            _write_message_unsafe(chat_file, "marcos-signoff", body, timestamp)
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "signed_off": True,
                "signed_at": timestamp,
                "file": str(chat_file),
                "plan_file": plan_path,
                "plan_hash": planner_hash,
            },
            indent=2,
        )
    )
    return 0


def command_write_consensus(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    try:
        with acquire_lock(chat_file, timeout=args.lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                print("consensus already written", file=sys.stderr)
                return 3

            next_action = decide_next_action(chat_file, "planner")
            if next_action["action"] != "post_consensus":
                print(
                    "refusing to write consensus: next action for planner "
                    f"is {next_action['action']}",
                    file=sys.stderr,
                )
                print(json.dumps(next_action, indent=2, sort_keys=True))
                return 3

            state = StateFile.load(chat_file)
            reasons, plan_path, _planner_hash = consensus_refusal_reasons(chat_file, state)
            if reasons:
                print(
                    "refusing to write consensus: " + "; ".join(reasons),
                    file=sys.stderr,
                )
                return 3

            body = f"Final agreed implementation plan lives at `{plan_path}`."
            _write_message_unsafe(chat_file, "consensus", body, utc_timestamp())
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "consensus_written": True,
                "file": str(chat_file),
                "plan_file": plan_path,
            },
            indent=2,
        )
    )
    return 0


def write_consensus_for_turn(
    chat_file: Path,
    lock_timeout: float = 10.0,
) -> tuple[int, dict[str, object]]:
    try:
        with acquire_lock(chat_file, timeout=lock_timeout):
            current = read_text(chat_file)
            if has_consensus(current):
                return 0, {
                    "action": "closed",
                    "file": str(chat_file),
                    "role": "planner",
                    "reason": "consensus_exists",
                }

            next_action = decide_next_action(chat_file, "planner")
            if next_action["action"] != "post_consensus":
                return 3, {
                    "action": "error",
                    "file": str(chat_file),
                    "role": "planner",
                    "reason": "next_action_not_post_consensus",
                    "next_action": next_action,
                }

            state = StateFile.load(chat_file)
            reasons, plan_path, _planner_hash = consensus_refusal_reasons(chat_file, state)
            if reasons:
                return 3, {
                    "action": "error",
                    "file": str(chat_file),
                    "role": "planner",
                    "reason": "consensus_refused",
                    "reasons": reasons,
                }

            body = f"Final agreed implementation plan lives at `{plan_path}`."
            _write_message_unsafe(chat_file, "consensus", body, utc_timestamp())
            StateFile.save(chat_file, state)
    except TimeoutError as exc:
        return 1, {
            "action": "error",
            "file": str(chat_file),
            "role": "planner",
            "reason": str(exc),
        }

    return 0, {
        "action": "consensus_written",
        "consensus_written": True,
        "file": str(chat_file),
        "role": "planner",
        "plan_file": plan_path,
    }


def command_poll_for_other(args: argparse.Namespace) -> int:
    chat_file = expand_path(args.file)
    self_role = args.self_role.strip()

    if self_role not in POLL_ROLES:
        print(f"--self must be one of {sorted(POLL_ROLES)}, got: {self_role}", file=sys.stderr)
        return 2
    error = _check_chat_and_sidecar(chat_file)
    if error is not None:
        return error

    next_action = decide_next_action(chat_file, self_role)
    if next_action["action"] == "summarize_exit":
        print(json.dumps(next_action, indent=2, sort_keys=True))
        return 0
    if next_action["action"] != "wait":
        print(
            "refusing to poll: next action for "
            f"{self_role} is {next_action['action']}",
            file=sys.stderr,
        )
        print(json.dumps(next_action, indent=2, sort_keys=True))
        return 3

    timeout = max(args.timeout, 0.0)
    poll_interval = max(args.poll_interval, 0.1)

    initial = inspect_chat(chat_file)
    initial_count = initial["message_count"]
    initial_timestamp = initial["last_timestamp"]
    deadline = time.monotonic() + timeout

    while True:
        now = time.monotonic()
        if now >= deadline:
            payload = {
                "timeout": True,
                "file": str(chat_file),
                "waiting_self": self_role,
                "last_role": initial["last_role"],
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 2

        time.sleep(min(poll_interval, deadline - now))

        current = inspect_chat(chat_file)
        if current["consensus_exists"]:
            print(json.dumps(current, indent=2, sort_keys=True))
            return 0

        appended = (
            current["message_count"] > initial_count
            or current["last_timestamp"] != initial_timestamp
        )
        if appended and current["last_role"] != self_role:
            print(json.dumps(current, indent=2, sort_keys=True))
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a co-plan Markdown chat file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a chat file with a goal message")
    init_parser.add_argument("--file", required=True)
    init_goal_group = init_parser.add_mutually_exclusive_group(required=True)
    init_goal_group.add_argument("--goal")
    init_goal_group.add_argument("--body-file")
    init_parser.add_argument("--lock-timeout", type=float, default=10.0)
    init_parser.set_defaults(func=command_init)

    update_goal_parser = subparsers.add_parser(
        "update-goal",
        help="append a paragraph to the goal section before planning starts",
    )
    update_goal_parser.add_argument("--file", required=True)
    update_goal_parser.add_argument("--body-file", required=True)
    update_goal_parser.add_argument("--lock-timeout", type=float, default=10.0)
    update_goal_parser.set_defaults(func=command_update_goal)

    read_parser = subparsers.add_parser("read", help="print the chat file")
    read_parser.add_argument("--file", required=True)
    read_parser.set_defaults(func=command_read)

    inspect_parser = subparsers.add_parser("inspect", help="print chat state as JSON")
    inspect_parser.add_argument("--file", required=True)
    inspect_parser.set_defaults(func=command_inspect)

    deps_parser = subparsers.add_parser(
        "deps-status",
        help="print optional rtk/caveman dependency status as JSON",
    )
    deps_parser.set_defaults(func=command_deps_status)

    turn_parser = subparsers.add_parser(
        "turn",
        help="wait until this role has an actionable co-plan turn or a stop state",
    )
    turn_parser.add_argument("--file", required=True)
    turn_parser.add_argument("--self", dest="self_role", required=True)
    turn_parser.add_argument("--timeout", type=float, default=900.0)
    turn_parser.add_argument("--poll-interval", type=float, default=3.0)
    turn_parser.add_argument("--lock-timeout", type=float, default=10.0)
    turn_parser.set_defaults(func=command_turn)

    post_parser = subparsers.add_parser(
        "post",
        help="append the current role's expected turn, then wait when the protocol says wait",
    )
    post_parser.add_argument("--file", required=True)
    post_parser.add_argument("--self", dest="self_role", required=True)
    post_parser.add_argument("--body-file", required=True)
    post_parser.add_argument("--plan-file")
    post_parser.add_argument("--timeout", type=float, default=900.0)
    post_parser.add_argument("--poll-interval", type=float, default=3.0)
    post_parser.add_argument("--lock-timeout", type=float, default=10.0)
    post_parser.set_defaults(func=command_post)

    question_parser = subparsers.add_parser(
        "question",
        help="add an open ledger question for this role",
    )
    question_parser.add_argument("--file", required=True)
    question_parser.add_argument("--self", dest="self_role", required=True)
    question_parser.add_argument("--question", required=True)
    question_parser.add_argument("--lock-timeout", type=float, default=10.0)
    question_parser.set_defaults(func=command_question)

    next_action_parser = subparsers.add_parser(
        "next-action",
        help="print the deterministic next action for a planner/advisor loop",
    )
    next_action_parser.add_argument("--file", required=True)
    next_action_parser.add_argument("--self", dest="self_role", required=True)
    next_action_parser.set_defaults(func=command_next_action)

    post_turn_parser = subparsers.add_parser(
        "post-turn",
        help="append a planner/advisor turn only when next-action allows posting",
    )
    post_turn_parser.add_argument("--file", required=True)
    post_turn_parser.add_argument("--self", dest="self_role", required=True)
    post_turn_parser.add_argument("--body-file", required=True)
    post_turn_parser.add_argument("--lock-timeout", type=float, default=10.0)
    post_turn_parser.set_defaults(func=command_post_turn)

    signoff_recap_parser = subparsers.add_parser(
        "post-signoff-recap",
        help="append the planner sign-off recap only when next-action allows it",
    )
    signoff_recap_parser.add_argument("--file", required=True)
    signoff_recap_parser.add_argument("--body-file", required=True)
    signoff_recap_parser.add_argument("--lock-timeout", type=float, default=10.0)
    signoff_recap_parser.set_defaults(func=command_post_signoff_recap)

    poll_parser = subparsers.add_parser(
        "poll-for-other",
        help="block until a new message arrives from a role other than --self",
    )
    poll_parser.add_argument("--file", required=True)
    poll_parser.add_argument("--self", dest="self_role", required=True)
    poll_parser.add_argument("--timeout", type=float, default=900.0)
    poll_parser.add_argument("--poll-interval", type=float, default=3.0)
    poll_parser.set_defaults(func=command_poll_for_other)

    add_q_parser = subparsers.add_parser(
        "add-question",
        help="add an open question to the ledger (planner or advisor)",
    )
    add_q_parser.add_argument("--file", required=True)
    add_q_parser.add_argument("--role", required=True)
    add_q_parser.add_argument("--question", required=True)
    add_q_parser.add_argument("--lock-timeout", type=float, default=10.0)
    add_q_parser.set_defaults(func=command_add_question)

    escalate_parser = subparsers.add_parser(
        "escalate-open-questions",
        help="append a planner escalation covering every open ledger question",
    )
    escalate_parser.add_argument("--file", required=True)
    escalate_parser.add_argument("--body-file", required=True)
    escalate_parser.add_argument("--lock-timeout", type=float, default=10.0)
    escalate_parser.set_defaults(func=command_escalate_open_questions)

    answer_parser = subparsers.add_parser(
        "answer-question",
        help="record marcos's answer to a ledger question and append a `### [marcos]` block",
    )
    answer_parser.add_argument("--file", required=True)
    answer_parser.add_argument("--id", required=True)
    answer_parser.add_argument("--answer", required=True)
    answer_parser.add_argument("--lock-timeout", type=float, default=10.0)
    answer_parser.set_defaults(func=command_answer_question)

    resolve_parser = subparsers.add_parser(
        "resolve",
        help="append a non-ledger marcos decision block and invalidate proposals",
    )
    resolve_parser.add_argument("--file", required=True)
    resolve_body_group = resolve_parser.add_mutually_exclusive_group(required=True)
    resolve_body_group.add_argument("--decision")
    resolve_body_group.add_argument("--body-file")
    resolve_parser.add_argument("--lock-timeout", type=float, default=10.0)
    resolve_parser.set_defaults(func=command_resolve)

    list_q_parser = subparsers.add_parser(
        "list-questions",
        help="print ledger questions as JSON",
    )
    list_q_parser.add_argument("--file", required=True)
    list_q_parser.add_argument("--state", default="all")
    list_q_parser.set_defaults(func=command_list_questions)

    validate_parser = subparsers.add_parser(
        "validate-plan",
        help="validate the current plan file before consensus proposal",
    )
    validate_parser.add_argument("--file", required=True)
    validate_parser.add_argument("--plan-file", required=True)
    validate_parser.add_argument("--lock-timeout", type=float, default=10.0)
    validate_parser.set_defaults(func=command_validate_plan)

    propose_parser = subparsers.add_parser(
        "propose-consensus",
        help=(
            "record that a role has proposed consensus against a plan file "
            "(stores the file's SHA256 in the sidecar for the given role)"
        ),
    )
    propose_parser.add_argument("--file", required=True)
    propose_parser.add_argument("--self", dest="self_role", required=True)
    propose_parser.add_argument("--plan-file", required=True)
    propose_parser.add_argument("--body-file")
    propose_parser.add_argument("--lock-timeout", type=float, default=10.0)
    propose_parser.set_defaults(func=command_propose_consensus)

    signoff_parser = subparsers.add_parser(
        "signoff",
        help=(
            "record marcos sign-off (refused with open questions, missing "
            "proposals, plan-file hash mismatch, or modified plan file)"
        ),
    )
    signoff_parser.add_argument("--file", required=True)
    signoff_parser.add_argument("--lock-timeout", type=float, default=10.0)
    signoff_parser.set_defaults(func=command_signoff)

    consensus_parser = subparsers.add_parser(
        "write-consensus",
        help="write final consensus only when sign-off and plan hash gates pass",
    )
    consensus_parser.add_argument("--file", required=True)
    consensus_parser.add_argument("--lock-timeout", type=float, default=10.0)
    consensus_parser.set_defaults(func=command_write_consensus)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
