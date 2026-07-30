#!/usr/bin/env python3
"""Content snapshots and child-boundary delta attribution."""

import hashlib
import json
import os
import subprocess


UNKNOWN = "unknown"


def path_digest(path):
    real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    raw = json.dumps(real, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _hash_file(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def snapshot(scope, max_paths=10000):
    """Snapshot repo tracked/untracked state and declared external outputs."""
    entries = {}
    partial = False

    def record(path):
        nonlocal partial
        if len(entries) >= max_paths:
            partial = True
            return False
        entries[path] = _hash_file(path)
        return True

    for root in scope.repo_roots:
        if len(entries) >= max_paths:
            partial = True
            break
        try:
            proc = subprocess.run(
                ["git", "-C", root, "ls-files", "-co", "--exclude-standard",
                 "-z"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=True)
            names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
        except (OSError, subprocess.CalledProcessError):
            names = []
        for name in filter(None, names):
            path = os.path.realpath(os.path.join(root, name))
            if not record(path):
                break
    for output in scope.declared_outputs:
        if len(entries) >= max_paths:
            partial = True
            break
        if os.path.isdir(output):
            for base, _, names in os.walk(output):
                if len(entries) >= max_paths:
                    partial = True
                    break
                for name in names:
                    path = os.path.realpath(os.path.join(base, name))
                    if not record(path):
                        break
        else:
            record(os.path.realpath(output))
    return {"entries": entries, "state": "partial" if partial else "complete"}


def diff(before, after):
    if not before or not after:
        return {"state": UNKNOWN, "added": UNKNOWN, "modified": UNKNOWN,
                "deleted": UNKNOWN}
    left = before.get("entries") or {}
    right = after.get("entries") or {}
    added = sorted(p for p in right
                   if (p not in left or left[p] is None)
                   and right[p] is not None)
    deleted = sorted(p for p in left if p not in right or right[p] is None)
    modified = sorted(p for p in left.keys() & right.keys()
                      if left[p] is not None and left[p] != right[p]
                      and right[p] is not None)
    state = ("partial" if "partial" in (before.get("state"),
                                        after.get("state")) else "complete")
    return {"state": state, "added": added, "modified": modified,
            "deleted": deleted}


def attribute(deltas, mutation_evidence):
    """Credit changed paths to evidenced actors.

    A child boundary snapshot may contain a mutation performed by its parent
    while the child was running.  The actor evidence, not the enclosing
    snapshot owner, decides credit.  This also prevents an ancestor from
    inheriting credit for a descendant mutation merely because its observation
    window contains the same path.
    """
    result = {}
    for _work_id, delta in (deltas or {}).items():
        paths = []
        for key in ("added", "modified", "deleted"):
            value = delta.get(key)
            if isinstance(value, list):
                paths.extend(value)
        for path in paths:
            actors = sorted(set(
                mutation_evidence.get(
                    path, mutation_evidence.get(path_digest(path), ()))))
            if not actors:
                result.setdefault(path, {
                    "work_ids": [], "attribution": "unattributed"})
                continue
            current = result.setdefault(path, {"work_ids": []})
            current["work_ids"] = sorted(set(current["work_ids"] + actors))
    for path, item in result.items():
        if item.get("attribution") == "unattributed":
            continue
        item["attribution"] = ("credited" if len(item["work_ids"]) == 1
                               else "contested")
    return result
