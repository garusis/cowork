#!/usr/bin/env python3
"""Controller hook transport for the orchestrator-owned guard broker.

The hook has no policy authority and writes no durable files.  Its sole minted
value is the correlation id needed when the broker is unavailable.
"""

import json
import argparse
import socket
import sys
import uuid


MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 3.0


def enrich_payload(payload, context):
    """Attach only the orchestrator-known parent work correlation."""
    payload = dict(payload or {})
    parent_work_id = (context or {}).get("current_parent_work_id")
    if parent_work_id:
        payload.setdefault("parent_work_id", parent_work_id)
        if not payload.get("agent_id"):
            payload.setdefault("work_id", parent_work_id)
    return payload


def _deny(attempt_id, reason="guard_unavailable"):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "%s guard_attempt_id=%s" % (reason, attempt_id),
        }
    }


def forward(payload, socket_path, token, timeout=DEFAULT_TIMEOUT,
            max_bytes=MAX_REQUEST_BYTES, attempt_id=None):
    attempt_id = attempt_id or str(uuid.uuid4())
    request = {"guard_attempt_id": attempt_id, "token": token,
               "payload": payload}
    raw = (json.dumps(request, sort_keys=True, separators=(",", ":"))
           + "\n").encode()
    if len(raw) > max_bytes:
        return _deny(attempt_id)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(raw)
        chunks = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return _deny(attempt_id)
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    except (OSError, ValueError, socket.timeout):
        return _deny(attempt_id)
    finally:
        try:
            client.close()
        except (NameError, OSError):
            pass
    if not isinstance(response, dict):
        return _deny(attempt_id)
    return response


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--context")
    args, _ = parser.parse_known_args()
    attempt_id = str(uuid.uuid4())
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            result = _deny(attempt_id)
        else:
            payload = json.loads(raw.decode())
            with open(args.context, "r") as fh:
                context = json.load(fh)
            payload = enrich_payload(payload, context)
            result = forward(
                payload, context["socket_path"], context["token"],
                attempt_id=attempt_id)
    except (OSError, ValueError, KeyError, TypeError):
        result = _deny(attempt_id)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
