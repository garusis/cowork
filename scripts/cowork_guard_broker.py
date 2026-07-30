#!/usr/bin/env python3
"""Privilege-separated guard broker owned by the cowork supervisor."""

import json
import os
import socket
import struct
import threading
import uuid
import datetime
try:
    import fcntl
except ImportError:  # Windows: the process-local lock still serializes writes.
    fcntl = None

import cowork_action_policy as action_policy
import cowork_delta


MAX_REQUEST_BYTES = 256 * 1024
_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()


def append_once(path, record, key="guard_attempt_id", sync=True):
    """Atomically deduplicate and append one JSONL object.

    The in-process lock serializes threads; flock extends the same critical
    section to any other authorized process using this writer.
    """
    value = record.get(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    real = os.path.realpath(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.setdefault(real, threading.Lock())
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":")) + "\n"
    with lock, open(path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        if value:
            fh.seek(0)
            for line in fh:
                try:
                    if json.loads(line).get(key) == value:
                        return False
                except ValueError:
                    continue
        fh.seek(0, os.SEEK_END)
        fh.write(encoded)
        fh.flush()
        if sync:
            os.fsync(fh.fileno())
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return True


class GuardBroker:
    def __init__(self, socket_path, token, scope, actions_path,
                 children_path, trace_path, parent_identity,
                 allowed_controllers=("claude",), capability_allowlist=None,
                 installed_schemas=None, max_request_bytes=MAX_REQUEST_BYTES):
        self.socket_path = socket_path
        self.token = token
        self.scope = scope
        self.actions_path = actions_path
        self.children_path = children_path
        self.trace_path = trace_path
        self.parent_identity = parent_identity
        self.allowed_controllers = tuple(allowed_controllers)
        self.capability_allowlist = capability_allowlist or {}
        self.installed_schemas = installed_schemas or {}
        self.max_request_bytes = max_request_bytes
        self._snapshots = {}
        self._children_by_tool = {}
        self._children_by_agent = {}
        self._child_started_at = {}
        self._finalized = set()
        self._child_usage = {}
        self._seen_child_usage = set()
        self._attempts = set()
        self._stop = threading.Event()
        self._server = None
        self._rehydrate_children()

    def _rehydrate_children(self):
        open_by_work = {}
        try:
            with open(self.children_path, "r") as fh:
                records = []
                for line in fh:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            records = []
        for record in records:
            work_id = record.get("work_id")
            if not work_id:
                continue
            if record.get("state") == "started":
                open_by_work[work_id] = record
            elif record.get("state") in ("ended", "blocked"):
                open_by_work.pop(work_id, None)
                if record.get("state") == "ended":
                    self._finalized.add(work_id)
            if record.get("agent_id"):
                self._children_by_agent[record["agent_id"]] = work_id
        for work_id, record in open_by_work.items():
            tool_use_id = record.get("tool_use_id")
            if tool_use_id:
                self._children_by_tool[tool_use_id] = work_id
            if record.get("ts"):
                self._child_started_at[work_id] = record["ts"]
            before_snapshot = record.get("before_snapshot")
            if isinstance(before_snapshot, dict):
                self._snapshots[work_id] = before_snapshot

    def _record(self, path, record):
        append_once(path, record)

    def _trace(self, event, attempt_id, **fields):
        record = {
            "event": event, "event_id": str(uuid.uuid4()),
            "guard_attempt_id": attempt_id,
            "ts": datetime.datetime.now(
                datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        append_once(self.trace_path, record)

    def _peer_allowed(self, conn):
        try:
            if hasattr(socket, "SO_PEERCRED"):
                raw = conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED,
                    struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", raw)
            elif hasattr(socket, "LOCAL_PEERCRED"):
                # Darwin exposes struct xucred through LOCAL_PEERCRED:
                # uint version, uid_t uid, then group metadata. The uid is the
                # second native 32-bit unsigned field.
                raw = conn.getsockopt(
                    getattr(socket, "SOL_LOCAL", 0),
                    socket.LOCAL_PEERCRED, 256)
                if len(raw) < struct.calcsize("=II"):
                    return False
                version, uid = struct.unpack_from("=II", raw)
                if version != getattr(socket, "XUCRED_VERSION", 0):
                    return False
            else:
                # A platform without a peer-credential mechanism cannot meet
                # the broker authentication contract.
                return False
            return uid == os.getuid()
        except (OSError, struct.error):
            return False

    def handle(self, request, peer_allowed=True):
        attempt_id = request.get("guard_attempt_id")
        if not attempt_id:
            attempt_id = str(uuid.uuid4())
        self._attempts.add(attempt_id)
        if (not peer_allowed or request.get("token") != self.token):
            decision = {"allow": False, "reason": "guard_unavailable"}
            self._record(self.actions_path, action_policy.sanitize(
                decision, guard_attempt_id=attempt_id))
            self._trace("action.policy.decision", attempt_id,
                        allow=False, reason="guard_unavailable")
            return self._hook_response(decision, attempt_id)
        payload = request.get("payload") or {}
        event_name = payload.get("hook_event_name") or "PreToolUse"
        if event_name == "SubagentStop":
            work_id = self._children_by_agent.get(payload.get("agent_id"))
            if work_id:
                self.finalize_child(
                    work_id, agent_id=payload.get("agent_id"),
                    terminal_source="subagent_stop")
            elif payload.get("agent_id"):
                self._trace(
                    "child.ungoverned", attempt_id,
                    reason="child_agent_correlation_unavailable")
            return {}
        if event_name == "SubagentStart":
            agent_id = payload.get("agent_id")
            if agent_id:
                # Documented SubagentStart has no Agent tool_use_id, so no
                # concurrency-safe pre-dispatch join exists. Agent/Task is
                # disabled and denied before launch; observing this event means
                # that boundary was bypassed.
                self._trace(
                    "child.ungoverned", attempt_id,
                    reason="child_agent_correlation_unavailable")
            return {
                "continue": False,
                "stopReason": "child_agent_correlation_unavailable",
            }
        agent_id = payload.get("agent_id")
        if agent_id and agent_id not in self._children_by_agent:
            tool_name = payload.get("tool_name") or payload.get("tool")
            tool_input = payload.get("tool_input") or {}
            action = action_policy.classify_action(
                tool_name, tool_input, cwd=payload.get("cwd"),
                installed_schema=self.installed_schemas.get(tool_name),
                capability_allowlist=self.capability_allowlist)
            decision = {
                "allow": False,
                "reason": "child_agent_correlation_unavailable",
            }
            self._record(self.actions_path, action_policy.sanitize(
                decision, action,
                parent_work_id=payload.get("parent_work_id"),
                guard_attempt_id=attempt_id))
            self._trace(
                "child.ungoverned", attempt_id,
                parent_work_id=payload.get("parent_work_id"),
                reason="child_agent_correlation_unavailable")
            if event_name == "PreToolUse":
                return self._hook_response(decision, attempt_id)
            return {}
        if event_name == "PostToolUse":
            tool_name = payload.get("tool_name") or payload.get("tool")
            tool_input = payload.get("tool_input") or {}
            if tool_name in action_policy.CHILD_TOOLS:
                child_work_id = self._children_by_tool.get(
                    payload.get("tool_use_id"))
                if child_work_id:
                    if child_work_id in self._finalized:
                        self._record(self.children_path, {
                            "guard_attempt_id":
                                "terminal-precedence-" + child_work_id,
                            "work_id": child_work_id,
                            "state": "terminal_precedence",
                            "terminal_source": "agent_tool_result",
                            "ts": _utc_now(),
                        })
                    else:
                        self.finalize_child(
                            child_work_id,
                            terminal_source="agent_tool_result")
                return {}
            acting_work_id = (payload.get("work_id")
                              or self._children_by_agent.get(
                                  payload.get("agent_id"))
                              or payload.get("parent_work_id"))
            action = action_policy.classify_action(
                tool_name, tool_input, cwd=payload.get("cwd"),
                installed_schema=self.installed_schemas.get(tool_name),
                capability_allowlist=self.capability_allowlist)
            if acting_work_id and self._is_child_work(acting_work_id):
                self._record(self.children_path, {
                    "guard_attempt_id": "tool-%s-%s" % (
                        acting_work_id, attempt_id),
                    "work_id": acting_work_id, "state": "tool",
                    "tool_name": tool_name, "tool_use_id": payload.get(
                        "tool_use_id"), "ts": _utc_now(),
                })
            if action.get("class") in ("write", "delete"):
                evidence = action_policy.sanitize(
                    {"allow": True, "reason": "post_tool_evidence"}, action,
                    work_id=acting_work_id,
                    parent_work_id=payload.get("parent_work_id"),
                    guard_attempt_id=attempt_id)
                evidence["evidence_kind"] = "mutation_effect"
                self._record(self.actions_path, evidence)
            return {}
        tool_name = payload.get("tool_name") or payload.get("tool")
        tool_input = payload.get("tool_input") or {}
        parent_work_id = payload.get("parent_work_id")
        acting_work_id = payload.get("work_id") or parent_work_id
        if tool_name in action_policy.CHILD_TOOLS:
            child_work_id = str(uuid.uuid4())
            requested = dict(tool_input)
            decision = action_policy.decide_child(
                requested, self.parent_identity, self.allowed_controllers,
                pin_capability=True)
            if decision.get("allow"):
                decision = dict(
                    decision, allow=False,
                    reason="child_agent_correlation_unavailable")
            before_snapshot = (cowork_delta.snapshot(self.scope)
                               if decision.get("allow") else None)
            request_meta = action_policy.child_request_metadata(requested)
            child = {
                "guard_attempt_id": attempt_id,
                "work_id": child_work_id,
                "parent_work_id": parent_work_id,
                "tool_use_id": payload.get("tool_use_id"),
                "effective_identity": decision.get("effective"),
                "pinned_input_digest": decision.get("pinned_input_digest"),
                "decision": "allow" if decision.get("allow") else "deny",
                "state": "started" if decision.get("allow") else "blocked",
                "reason": decision.get("reason"),
                "effective_policy": decision.get("reason"),
                "ts": _utc_now(),
            }
            child.update(request_meta)
            if before_snapshot is not None:
                child["before_snapshot"] = before_snapshot
            self._record(self.children_path, child)
            self._trace(
                "child.work.start" if decision.get("allow")
                else "child.dispatch.blocked",
                attempt_id, work_id=child_work_id,
                parent_work_id=parent_work_id, reason=decision.get("reason"),
                work_kind=("child" if decision.get("allow")
                           else "child_attempt"))
            if decision.get("allow"):
                self._snapshots[child_work_id] = before_snapshot
                self._child_started_at[child_work_id] = child["ts"]
                if payload.get("tool_use_id"):
                    self._children_by_tool[payload["tool_use_id"]] = (
                        child_work_id)
            return self._hook_response(decision, attempt_id,
                                       child_work_id=child_work_id)
        action = action_policy.classify_action(
            tool_name, tool_input, cwd=payload.get("cwd"),
            installed_schema=self.installed_schemas.get(tool_name),
            capability_allowlist=self.capability_allowlist)
        decision = action_policy.decide(
            action, self.scope,
            created_paths=payload.get("created_paths") or (),
            clean_tracked_paths=payload.get("clean_tracked_paths") or ())
        record = action_policy.sanitize(
            decision, action, work_id=acting_work_id,
            parent_work_id=parent_work_id, guard_attempt_id=attempt_id)
        self._record(self.actions_path, record)
        self._trace("action.policy.decision", attempt_id,
                    work_id=acting_work_id, parent_work_id=parent_work_id,
                    allow=decision.get("allow"),
                    reason=decision.get("reason"),
                    action_class=action.get("class"))
        return self._hook_response(decision, attempt_id)

    @staticmethod
    def _hook_response(decision, attempt_id, child_work_id=None):
        output = {"hookEventName": "PreToolUse"}
        if decision.get("allow"):
            output["permissionDecision"] = "allow"
            if decision.get("updated_input") is not None:
                output["updatedInput"] = decision["updated_input"]
        else:
            output["permissionDecision"] = "deny"
            output["permissionDecisionReason"] = (
                "%s guard_attempt_id=%s" %
                (decision.get("reason") or "denied", attempt_id))
        response = {"hookSpecificOutput": output,
                    "guard_attempt_id": attempt_id}
        if child_work_id:
            response["child_work_id"] = child_work_id
        return response

    def finalize_child(self, work_id, agent_id=None,
                       terminal_source="agent_tool_result"):
        if work_id in self._finalized:
            return {"state": "duplicate_terminal", "added": [],
                    "modified": [], "deleted": []}
        if agent_id is None:
            agent_id = next(
                (candidate for candidate, candidate_work_id
                 in self._children_by_agent.items()
                 if candidate_work_id == work_id),
                None)
        before = self._snapshots.pop(work_id, None)
        delta = cowork_delta.diff(before, cowork_delta.snapshot(self.scope))
        usage = self._child_usage.pop(work_id, None)
        ended_at = _utc_now()
        duration_ms = _duration_ms(
            self._child_started_at.get(work_id), ended_at)
        self._record(self.children_path, {
            "guard_attempt_id": "delta-" + work_id,
            "work_id": work_id, "state": "ended", "delta": delta,
            "agent_id": agent_id, "terminal_source": terminal_source,
            "ts": ended_at, "duration_ms": duration_ms,
            "usage": usage, "usage_scope": (
                "child_native_sum" if usage else "unknown"),
        })
        self._finalized.add(work_id)
        self._trace("child.work.end", "delta-" + work_id,
                    work_id=work_id, work_kind="child")
        return delta

    def work_id_for_tool(self, tool_use_id):
        return self._children_by_tool.get(tool_use_id)

    def _is_child_work(self, work_id):
        return bool(
            work_id
            and (work_id in self._snapshots
                 or work_id in self._finalized
                 or work_id in self._children_by_tool.values()
                 or work_id in self._children_by_agent.values()))

    def has_attempt(self, attempt_id):
        return attempt_id in self._attempts

    def record_child_usage(self, work_id, usage, event_id=None,
                           replayed=False):
        if replayed or not work_id or not isinstance(usage, dict):
            return
        key = (work_id, event_id)
        if event_id and key in self._seen_child_usage:
            return
        if event_id:
            self._seen_child_usage.add(key)
        bucket = self._child_usage.setdefault(work_id, {})
        for key, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                bucket[key] = bucket.get(key, 0) + value

    def serve_forever(self):
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(self.socket_path)
        bound_stat = os.stat(self.socket_path)
        bound_identity = (bound_stat.st_dev, bound_stat.st_ino)
        os.chmod(self.socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.25)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    peer_allowed = self._peer_allowed(conn)
                    raw = b""
                    while (b"\n" not in raw
                           and len(raw) <= self.max_request_bytes):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    try:
                        if len(raw) > self.max_request_bytes:
                            raise ValueError("oversize")
                        request = json.loads(raw.split(b"\n", 1)[0])
                        response = self.handle(
                            request, peer_allowed=peer_allowed)
                    except (ValueError, TypeError):
                        attempt_id = str(uuid.uuid4())
                        decision = {
                            "allow": False, "reason": "guard_unavailable"}
                        self._record(
                            self.actions_path, action_policy.sanitize(
                                decision, guard_attempt_id=attempt_id))
                        response = self._hook_response(decision, attempt_id)
                    conn.sendall(
                        (json.dumps(response, sort_keys=True) + "\n").encode())
        finally:
            server.close()
            # A newer broker may already have replaced this pathname. Remove
            # only the socket inode this server actually bound.
            try:
                current = os.stat(self.socket_path)
                if (current.st_dev, current.st_ino) == bound_identity:
                    os.unlink(self.socket_path)
            except FileNotFoundError:
                pass

    def stop(self):
        self._stop.set()


def _utc_now():
    return datetime.datetime.now(
        datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(started, ended):
    try:
        left = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
        right = datetime.datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return max(0, int((right - left).total_seconds() * 1000))
    except (AttributeError, TypeError, ValueError):
        return "unknown"
