#!/usr/bin/env python3
"""Recovery-episode evidence: what a recovery turn changed, and what it earned.

A recovery turn is a turn that re-runs work after a failure (schema repair,
orchestration debugging, an unchanged replay). This module is the pure
vocabulary and arithmetic for asking "did that turn earn anything?":

- a closed set of reason classes (`REASON_CLASSES`); an unknown reason code is
  refused with `UnknownReasonCode`, never mapped to a class by guess;
- `artifact_delta` and `finding_delta`: before/after comparisons that answer
  `unknown` — never `unchanged` or zero — whenever an input is missing,
  malformed or inconsistent;
- `attribute_value`: a verdict of `zero`, `positive` or `unknown`. Zero is
  attributed only when the artifacts are provably unchanged and nothing
  creditable happened; the whole turn is then recovery overhead. A finding
  earns value once: the caller passes the finding keys already credited;
- `lead_reopen_decision` / `inventory_reason_state`: plain records that flag a
  reopen or a repeated inventory that cites no durable reason. They never
  dispatch, block or prevent anything, and "prevented" cannot be represented;
- an episode record and an idempotent, torn-tail-tolerant `episodes.jsonl`
  append/read inside a directory the caller names.

INPUT CONTRACT for ledger records: `before_records` and `after_records` are the
APPEND-ONLY HISTORY as a list of plain dicts (the shape `read_ledger` returns),
and `after_records` is a superset of `before_records`. A dict (the shape
`collapse` returns), a round-filtered subset or any non-list is not supported:
a non-list side reads as `unknown`, and so does a finding open before that is
missing from the after history. This module never selects verdict rounds; the
caller passes the whole history it is comparing.

Finding identity: a finding is keyed `src:<source_session>:<source_finding_id>`
when any of its records carries `source_finding_id`, else by its plain id, so a
re-minted id is never mistaken for a source id. A `closure` record (plain dict
with `closes` = a finding id and/or `source_session` + `source_finding_id`)
closes the finding it resolves to; a closure that resolves to no finding is
ignored. This module only reads closures; it never writes one.

Callers (later stages) import this module; it imports no sibling module.

Python 3.9+, stdlib only. `fcntl` is POSIX-only, like the ledger writer.
"""

import fcntl
import hashlib
import json
import os
import re

SCHEMA_VERSION = 1
EPISODES_FILENAME = "episodes.jsonl"

REASON_CLASSES = ("schema_repair", "orchestration_debugging", "unchanged_replay")
COST_CLASS = "recovery"
DELTA_STATES = ("unchanged", "changed", "added", "removed", "unknown")
FINDING_DELTA_STATES = ("unchanged", "changed", "unknown")
VALUE_STATES = ("zero", "positive", "unknown")
OVERHEAD_STATES = ("full", "partial", "unknown")
REASON_REF_STATES = ("durable", "absent", "unknown", "unverified")
REREAD_STATES = ("instructed", "detected")
FLAG_REOPEN_WITHOUT_REASON = "reopen_without_durable_reason"
FLAG_REPEATED_INVENTORY = "repeated_inventory_without_reason"
OUTCOMES = ("recorded", "flagged")

EPISODE_KEYS = (
    "schema", "episode_id", "failed_role", "failed_work_id",
    "recovery_work_id", "reason_class", "reason_ref", "before", "after",
    "artifact_delta", "finding_delta", "value", "cost_class",
)

_LEDGER_STATES = ("open", "superseded", "withdrawn", "closed")
_RETIRED_STATES = ("withdrawn", "superseded")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class UnknownReasonCode(ValueError):
    """The reason code is not one of `REASON_CLASSES`."""


def _is_str(value):
    return isinstance(value, str) and bool(value)


def classify_recovery_turn(reason_code):
    """Map a reason code to `{'reason_class', 'cost_class'}`.

    The match is exact (no strip, no case folding). Anything outside
    `REASON_CLASSES`, including None and non-strings, raises
    `UnknownReasonCode`: there is no default class.
    """
    if not isinstance(reason_code, str) or reason_code not in REASON_CLASSES:
        raise UnknownReasonCode(
            "unknown recovery reason code %r (expected one of %s)"
            % (reason_code, ", ".join(REASON_CLASSES)))
    return {"reason_class": reason_code, "cost_class": COST_CLASS}


# ---------------------------------------------------------------- artifacts

def _valid_sha(value):
    return isinstance(value, str) and bool(_SHA256.match(value))


def normalize_artifacts(descriptors):
    """Sorted `[{'path', 'sha256'}]` with sha256 lowercased, or None.

    None when `descriptors` is not a list, an entry has no usable path, or a
    path repeats. An entry whose sha256 is invalid is kept with
    `sha256: None` so the unknown survives into the delta.
    """
    if not isinstance(descriptors, list):
        return None
    out = {}
    for entry in descriptors:
        if not isinstance(entry, dict) or not _is_str(entry.get("path")):
            return None
        path = entry["path"]
        if path in out:
            return None
        sha = entry.get("sha256")
        out[path] = sha.lower() if _valid_sha(sha) else None
    return [{"path": p, "sha256": out[p]} for p in sorted(out)]


def _artifact_map(side):
    """`{path: lowercased sha or None}`, or None when structurally invalid."""
    if not isinstance(side, list):
        return None
    out = {}
    for entry in side:
        if not isinstance(entry, dict) or not _is_str(entry.get("path")):
            return None
        path = entry["path"]
        if path in out:
            return None
        sha = entry.get("sha256")
        out[path] = sha.lower() if _valid_sha(sha) else None
    return out


def artifact_delta(before, after):
    """Compare two artifact lists of `{'path', 'sha256'}`.

    Returns `{'state', 'paths', 'changed', 'added', 'removed'}`. A None or
    non-list side, a non-dict entry, an empty path or a duplicate path makes
    the state `unknown`. Otherwise each path is `unchanged`, `changed`,
    `added`, `removed`, or `unknown` (an invalid sha256 on either side). The
    aggregate is `changed` if any path changed, was added or was removed (a
    definite change stands beside an unknown path); else `unknown` if any path
    is unknown; else `unchanged`, including two empty lists.
    """
    left = _artifact_map(before)
    right = _artifact_map(after)
    if left is None or right is None:
        return {"state": "unknown", "paths": {}, "changed": [],
                "added": [], "removed": []}
    paths = {}
    for path in sorted(set(left) | set(right)):
        if path not in left:
            paths[path] = "unknown" if right[path] is None else "added"
        elif path not in right:
            paths[path] = "unknown" if left[path] is None else "removed"
        elif left[path] is None or right[path] is None:
            paths[path] = "unknown"
        elif left[path] == right[path]:
            paths[path] = "unchanged"
        else:
            paths[path] = "changed"
    changed = [p for p, s in paths.items() if s == "changed"]
    added = [p for p, s in paths.items() if s == "added"]
    removed = [p for p, s in paths.items() if s == "removed"]
    if changed or added or removed:
        state = "changed"
    elif any(s == "unknown" for s in paths.values()):
        state = "unknown"
    else:
        state = "unchanged"
    return {"state": state, "paths": paths, "changed": changed,
            "added": added, "removed": removed}


# ----------------------------------------------------------------- findings

def _source_key(session, finding_id):
    return "src:%s:%s" % (session, finding_id)


def _present(value):
    return value is not None and value != "" and not isinstance(
        value, (dict, list, bool))


def _fold_findings(records):
    """Fold a record list into `(key_states, closure_keys, unrecognised)`.

    `key_states` maps a finding key to its latest state; `closure_keys` is the
    set of keys a resolvable closure targets; `unrecognised` is True when a
    finding record carries a state outside the ledger vocabulary. Returns None
    when `records` is not a list of dicts.
    """
    if not isinstance(records, list) or not all(
            isinstance(r, dict) for r in records):
        return None
    id_states = {}
    id_source = {}
    order = []
    unrecognised = False
    closures = []
    for rec in records:
        kind = rec.get("kind")
        if kind == "closure":
            closures.append(rec)
            continue
        if kind != "finding" or not _is_str(rec.get("id")):
            continue
        fid = rec["id"]
        state = rec.get("state") or "open"
        if state not in _LEDGER_STATES:
            unrecognised = True
        if fid not in id_states:
            order.append(fid)
        id_states[fid] = state
        if fid not in id_source and _present(rec.get("source_finding_id")):
            id_source[fid] = _source_key(
                "" if rec.get("source_session") is None
                else rec.get("source_session"), rec["source_finding_id"])
    id_key = {fid: id_source.get(fid, fid) for fid in order}
    per_key = {}
    for fid in order:
        per_key.setdefault(id_key[fid], []).append(id_states[fid])
    key_states = {}
    for key, states in per_key.items():
        key_states[key] = "open" if "open" in states else states[-1]
    closure_keys = set()
    for rec in closures:
        by_source = None
        if _present(rec.get("source_session")) and _present(
                rec.get("source_finding_id")):
            by_source = _source_key(
                rec["source_session"], rec["source_finding_id"])
        by_closes = None
        closes = rec.get("closes")
        if _is_str(closes) and closes in id_key:
            by_closes = id_key[closes]
        if by_source is not None and _is_str(closes):
            if by_closes is None or by_closes != by_source:
                continue
            closure_keys.add(by_source)
        elif by_source is not None:
            closure_keys.add(by_source)
        elif by_closes is not None:
            closure_keys.add(by_closes)
    return key_states, closure_keys, unrecognised


def _open_keys(folded):
    key_states, closure_keys, _ = folded
    return {k for k, s in key_states.items()
            if s == "open" and k not in closure_keys}


def open_finding_keys(records):
    """Sorted keys of the findings open in `records`, or None if unreadable."""
    folded = _fold_findings(records)
    if folded is None:
        return None
    return sorted(_open_keys(folded))


def finding_delta(before_records, after_records):
    """Compare finding state across two ledger histories.

    Returns `{'state', 'new', 'closed', 'retired'}` (keys, sorted). `new` is
    open after and not before. `closed` is open before and closure-targeted (or
    state `closed`) after: value-bearing. `retired` is open before and
    withdrawn/superseded after without a closure: not value. State is
    `unknown` for an unreadable side, an unrecognised finding state, or a
    finding open before that vanished from the after history; otherwise
    `changed` when any list is non-empty, else `unchanged`.
    """
    unknown = {"state": "unknown", "new": [], "closed": [], "retired": []}
    before = _fold_findings(before_records)
    after = _fold_findings(after_records)
    if before is None or after is None or before[2] or after[2]:
        return unknown
    before_open = _open_keys(before)
    after_open = _open_keys(after)
    after_states, after_closures, _ = after
    closed, retired = [], []
    for key in sorted(before_open):
        if key in after_closures or after_states.get(key) == "closed":
            closed.append(key)
        elif key not in after_states:
            return unknown
        elif after_states[key] in _RETIRED_STATES:
            retired.append(key)
    new = sorted(after_open - before_open)
    state = "changed" if (new or closed or retired) else "unchanged"
    return {"state": state, "new": new, "closed": closed, "retired": retired}


def credited_finding_keys(episodes):
    """Union of `value.credited_finding_keys` over stored episodes."""
    out = set()
    for episode in episodes or []:
        if not isinstance(episode, dict):
            continue
        value = episode.get("value")
        keys = value.get("credited_finding_keys") if isinstance(
            value, dict) else None
        if isinstance(keys, list):
            out.update(k for k in keys if _is_str(k))
    return sorted(out)


# -------------------------------------------------------------------- value

def attribute_value(artifact_delta, finding_delta, credited_keys=()):
    """Attribute value to one recovery turn.

    `positive` when an artifact changed or an uncredited new/closed finding key
    exists (positive wins over unknown). `zero` only when the artifact delta is
    `unchanged` and the finding delta is known with nothing creditable; the turn
    is then `full` recovery overhead. Anything else is `unknown`. A non-dict or
    unrecognised delta counts as unknown. Keys in `credited_keys` earn nothing
    a second time.
    """
    artifact_state = "unknown"
    if isinstance(artifact_delta, dict) and artifact_delta.get(
            "state") in DELTA_STATES:
        artifact_state = artifact_delta["state"]
    finding_state = "unknown"
    creditable = []
    if isinstance(finding_delta, dict) and finding_delta.get(
            "state") in FINDING_DELTA_STATES:
        finding_state = finding_delta["state"]
        credited = set(k for k in (credited_keys or ()) if _is_str(k))
        for field in ("new", "closed"):
            keys = finding_delta.get(field)
            if isinstance(keys, list):
                creditable.extend(
                    k for k in keys if _is_str(k) and k not in credited)
    creditable = sorted(set(creditable))
    if artifact_state in ("changed", "added", "removed") or creditable:
        state, overhead = "positive", "partial"
    elif artifact_state == "unchanged" and finding_state != "unknown":
        state, overhead = "zero", "full"
    else:
        state, overhead = "unknown", "unknown"
    return {"state": state, "artifact_state": artifact_state,
            "finding_state": finding_state,
            "credited_finding_keys": creditable,
            "recovery_overhead": overhead}


# --------------------------------------------------- flag-only reason checks

def _collection(value):
    return isinstance(value, (list, tuple, set, frozenset))


def reason_ref_state(reason_ref, known_reason_ids):
    """`absent`, `unknown`, `durable` or `unverified` for a cited reason.

    `unverified` when the caller could not supply the known ids: the claim
    that the reason is missing would then be untrue, so no flag follows.
    """
    if not _is_str(reason_ref):
        return "absent"
    if not _collection(known_reason_ids):
        return "unverified"
    return "durable" if reason_ref in known_reason_ids else "unknown"


def _clean_ref(reason_ref):
    return reason_ref if _is_str(reason_ref) else None


def lead_reopen_decision(reason_ref, known_reason_ids):
    """Record a lead reopen, flagging one with no durable reason.

    Returns `{'outcome', 'reason_ref', 'reason_state', 'flags'}`. Records and
    flags only; it never dispatches or blocks.
    """
    state = reason_ref_state(reason_ref, known_reason_ids)
    flags = [FLAG_REOPEN_WITHOUT_REASON] if state in (
        "absent", "unknown") else []
    return {"outcome": "flagged" if flags else "recorded",
            "reason_ref": _clean_ref(reason_ref),
            "reason_state": state, "flags": flags}


def inventory_reason_state(reason_ref, known_reason_ids, repeated,
                           reread_state=None):
    """Record the reason state of a possibly repeated inventory.

    Flags only a repeated inventory (`repeated is True`) whose reason is absent
    or unknown. `reread_state` must be None, `instructed` or `detected`.
    """
    if reread_state is not None and reread_state not in REREAD_STATES:
        raise ValueError(
            "reread_state must be None or one of %s" % (", ".join(
                REREAD_STATES),))
    state = reason_ref_state(reason_ref, known_reason_ids)
    flags = []
    if repeated is True and state in ("absent", "unknown"):
        flags.append(FLAG_REPEATED_INVENTORY)
    return {"reason_ref": _clean_ref(reason_ref), "reason_state": state,
            "repeated": repeated if isinstance(repeated, bool) else None,
            "reread_state": reread_state, "flags": flags}


# ----------------------------------------------------------------- episodes

def episode_key(failed_work_id, recovery_work_id):
    if not _is_str(failed_work_id) or not _is_str(recovery_work_id):
        raise ValueError("failed and recovery work ids must be non-empty text")
    return (failed_work_id, recovery_work_id)


def episode_id_for(failed_work_id, recovery_work_id):
    failed, recovery = episode_key(failed_work_id, recovery_work_id)
    digest = hashlib.sha256(
        (failed + "\n" + recovery).encode("utf-8")).hexdigest()
    return "RE-" + digest[:16]


def build_episode(failed_role, failed_work_id, recovery_work_id, reason_code,
                  reason_ref=None, before_artifacts=None,
                  after_artifacts=None, before_records=None,
                  after_records=None, credited_keys=()):
    """Assemble one episode record (a plain dict, JSON-serialisable).

    Raises `ValueError` for an empty role or id and `UnknownReasonCode` for a
    bad reason code. `reason_ref` is stored verbatim and not checked here.
    """
    if not _is_str(failed_role):
        raise ValueError("failed_role must be non-empty text")
    episode_key(failed_work_id, recovery_work_id)
    classified = classify_recovery_turn(reason_code)
    if reason_ref is not None and not _is_str(reason_ref):
        raise ValueError("reason_ref must be None or non-empty text")
    before = normalize_artifacts(before_artifacts)
    after = normalize_artifacts(after_artifacts)
    a_delta = artifact_delta(before, after)
    f_delta = finding_delta(before_records, after_records)
    return {
        "schema": SCHEMA_VERSION,
        "episode_id": episode_id_for(failed_work_id, recovery_work_id),
        "failed_role": failed_role,
        "failed_work_id": failed_work_id,
        "recovery_work_id": recovery_work_id,
        "reason_class": classified["reason_class"],
        "reason_ref": reason_ref,
        "before": {"artifacts": before,
                   "open_finding_ids": open_finding_keys(before_records)},
        "after": {"artifacts": after,
                  "open_finding_ids": open_finding_keys(after_records)},
        "artifact_delta": a_delta,
        "finding_delta": f_delta,
        "value": attribute_value(a_delta, f_delta, credited_keys),
        "cost_class": classified["cost_class"],
    }


def validate_episode(episode):
    """Problems with an episode, as strings; empty when it is valid."""
    if not isinstance(episode, dict):
        return ["episode is not an object"]
    problems = ["missing key %s" % k for k in EPISODE_KEYS if k not in episode]
    for key in ("failed_role", "failed_work_id", "recovery_work_id"):
        if key in episode and not _is_str(episode[key]):
            problems.append("%s is not non-empty text" % key)
    if (_is_str(episode.get("failed_work_id"))
            and _is_str(episode.get("recovery_work_id"))
            and episode.get("episode_id") != episode_id_for(
                episode["failed_work_id"], episode["recovery_work_id"])):
        problems.append("episode_id does not match the work ids")
    if "reason_class" in episode and episode["reason_class"] not in (
            REASON_CLASSES):
        problems.append("reason_class is not a known class")
    if "cost_class" in episode and episode["cost_class"] != COST_CLASS:
        problems.append("cost_class is not %s" % COST_CLASS)
    if "value" in episode:
        value = episode["value"]
        if not isinstance(value, dict) or value.get("state") not in (
                VALUE_STATES):
            problems.append("value.state is not a known state")
    return problems


def _parse_episodes(data):
    """Episodes in file order from raw bytes; first line per key wins."""
    text = data.decode("utf-8", errors="replace")
    seen = set()
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        failed = obj.get("failed_work_id")
        recovery = obj.get("recovery_work_id")
        if not _is_str(failed) or not _is_str(recovery):
            continue
        key = (failed, recovery)
        if key in seen:
            continue
        seen.add(key)
        out.append(obj)
    return out


def read_episodes(directory):
    """Episodes stored in `<directory>/episodes.jsonl`, in file order.

    Never raises and never creates anything: a missing or unreadable file reads
    as `[]`; unparseable, non-object and key-less lines are skipped.
    """
    if not _is_str(directory):
        return []
    try:
        with open(os.path.join(directory, EPISODES_FILENAME), "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    return _parse_episodes(data)


def append_episode(directory, episode):
    """Append an episode once per (failed work id, recovery work id).

    Returns `(stored_episode, created)`: the existing episode and False when
    the key is already stored, else the episode and True. Raises `ValueError`
    for an invalid directory or episode; `OSError` propagates. The only file
    touched is `<directory>/episodes.jsonl`, locked with `flock` on itself; an
    unterminated tail is terminated before the new line is written.
    """
    if not _is_str(directory):
        raise ValueError("directory must be non-empty text")
    problems = validate_episode(episode)
    if problems:
        raise ValueError("invalid episode: " + "; ".join(problems))
    try:
        line = json.dumps(episode, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("episode is not serialisable: %s" % exc)
    key = (episode["failed_work_id"], episode["recovery_work_id"])
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, EPISODES_FILENAME)
    with open(path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            data = fh.read()
            for existing in _parse_episodes(data):
                if (existing["failed_work_id"],
                        existing["recovery_work_id"]) == key:
                    return existing, False
            prefix = b"\n" if data and not data.endswith(b"\n") else b""
            fh.write(prefix + line + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return episode, True
