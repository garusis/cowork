#!/usr/bin/env python3
"""Path-first, diff-based review packets (#4).

Reviewer/advisor prompts no longer embed whole JSON/Markdown artifacts by
default. The current files on disk are the source of truth (the CLIs have
filesystem access), so a packet carries:

- the current artifact paths, each with its byte size and sha256;
- on a repeat review round in the same reviewer session — same phase epoch and
  same context revision — a deterministic unified diff from the version that
  reviewer last saw to the current version, so it can review incrementally
  without re-reading whole files.

Every other case sends a PATH-FIRST FULL-REREAD packet (paths + hashes + sizes +
an instruction to read the files from disk): the first pass, a fresh reviewer
session, a changed context revision (the snapshot key includes it, so a changed
revision has no prior snapshot), no prior snapshot, a JSON canonicalization
failure, a diff that fails or exceeds the size cap, or an explicit
`force_full_reread` (the malformed/weak-verdict retry, D8).

cowork generates the diff deterministically from its own on-disk snapshot — it
never asks the lead role to summarize its own changes. The snapshot is written
from the on-disk file at every packet build (fresh and resume), so the next
round's diff is available and deterministic regardless of conversation history.

Pure stdlib; tolerant (a read/parse failure degrades to full-reread, never
raises).
"""

import difflib
import hashlib
import json
import os


# Default cap on unified-diff lines before we abandon the diff and fall back to
# a full reread (doc: "the diff is too large/noisy to judge safely").
DEFAULT_DIFF_LINE_CAP = 400


FULL_REREAD_INSTRUCTION = (
    "Read the FULL current files from disk at the paths above. They are the "
    "authoritative current source of truth for your review."
)

DIFF_INSTRUCTION = (
    "You previously reviewed these artifacts. The current files are at the "
    "paths above. The diff below is deterministic and authoritative for what "
    "changed since your last review. Review the diff against your prior context "
    "and prior findings. Read the full current files from disk only if you need "
    "surrounding context, need to verify whole-artifact consistency, or the "
    "diff is too large/noisy to judge safely."
)


def _read_raw(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _canonicalize(raw_text, kind):
    """Return (canonical_text, ok). For JSON, parse + sort_keys + indent so
    key-order/whitespace churn does not dominate the diff; ok is False when the
    text is not valid JSON. For markdown (and anything else) the text is its own
    canonical form."""
    if raw_text is None:
        return "", False
    if kind == "json":
        try:
            obj = json.loads(raw_text)
        except (ValueError, TypeError):
            return raw_text, False
        return json.dumps(obj, sort_keys=True, indent=2), True
    return raw_text, True


def _snapshot_path(snapshot_dir, reviewer_role):
    safe = "".join(c if c.isalnum() or c in "-_" else "_"
                   for c in (reviewer_role or "reviewer"))
    return os.path.join(snapshot_dir, "review_snapshot.%s.json" % safe)


def _snapshot_key(epoch, context_revision, paths):
    """Snapshot identity = (epoch, context_revision, ordered artifact paths).
    Folding the ordered path set into the key means a changed or reordered
    artifact list under the same epoch/revision yields no prior snapshot, which
    forces the full-reread fallback (the reviewer never gets an incremental diff
    for a newly added/reordered artifact it lacks prior context for)."""
    joined = "\x1f".join(paths or [])
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return "%s|%s|%s" % (epoch, context_revision, digest)


def _load_snapshot(snapshot_dir, reviewer_role, key):
    """Return the prior {path: canonical_text} map for `key`, or None when no
    snapshot for this (role, epoch, context_revision) exists / is readable."""
    if not snapshot_dir:
        return None
    path = _snapshot_path(snapshot_dir, reviewer_role)
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    files = data.get("files")
    return files if isinstance(files, dict) else None


def _store_snapshot(snapshot_dir, reviewer_role, key, files):
    """Persist the current {path: canonical_text} map under `key`, overwriting
    any prior key (we only ever diff against the same key, so a single-key file
    stays bounded). Tolerant: a write failure is swallowed."""
    if not snapshot_dir:
        return
    path = _snapshot_path(snapshot_dir, reviewer_role)
    try:
        os.makedirs(snapshot_dir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"key": key, "files": files}, fh, sort_keys=True)
    except OSError:
        return


def _descriptor_lines(entries):
    lines = []
    for e in entries:
        present = "" if e["present"] else " (missing on disk)"
        lines.append("  - %s: %s  [%d bytes, sha256 %s]%s"
                     % (e["label"], e["path"], e["bytes"], e["sha256"][:12],
                        present))
    return "\n".join(lines)


def build_review_packet(reviewer_role, epoch, context_revision, artifacts,
                        snapshot_dir, *, force_full_reread=False,
                        diff_line_cap=DEFAULT_DIFF_LINE_CAP):
    """Build the embedded-artifact block for a reviewer prompt and (re)write the
    current snapshot.

    `artifacts` is an ordered list of dicts, each ``{"label", "path", "kind"}``
    where kind is "json" or "markdown". Returns the block string to splice into
    the reviewer's context where full artifact bodies used to live.
    """
    key = _snapshot_key(epoch, context_revision,
                        [a["path"] for a in artifacts])
    entries = []
    current_canon = {}
    canon_failed = False
    for art in artifacts:
        path = art["path"]
        raw = _read_raw(path)
        present = raw is not None
        raw_bytes = raw if raw is not None else b""
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        canon, ok = _canonicalize(raw_text if present else None, art.get("kind"))
        if present and not ok:
            canon_failed = True
        entries.append({
            "label": art.get("label") or os.path.basename(path),
            "path": path,
            "kind": art.get("kind"),
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "present": present,
            "canonical": canon,
        })
        current_canon[path] = canon

    prior = _load_snapshot(snapshot_dir, reviewer_role, key)

    # Always (re)write the snapshot from the on-disk files so the next round can
    # diff against it — done before any early return.
    _store_snapshot(snapshot_dir, reviewer_role, key, current_canon)

    use_diff = (not force_full_reread and prior is not None and not canon_failed)
    # Defensive: the key already folds in the ordered path set, so a matching
    # prior snapshot covers exactly these paths; but if any current path is
    # absent from the prior map, the reviewer lacks prior context for it — fall
    # back to a full reread rather than diff a new artifact against nothing.
    if use_diff and any(e["path"] not in prior for e in entries):
        use_diff = False
    diff_text = None
    if use_diff:
        chunks = []
        for e in entries:
            path = e["path"]
            before = (prior.get(path) or "").splitlines(keepends=True)
            after = (e["canonical"] or "").splitlines(keepends=True)
            d = list(difflib.unified_diff(
                before, after, fromfile="a/%s" % path, tofile="b/%s" % path))
            chunks.extend(d)
        if len(chunks) > diff_line_cap:
            use_diff = False  # too large/noisy → full reread
        else:
            diff_text = "".join(chunks)

    header = ("Reviewed artifacts (current files on disk — the authoritative "
              "source of truth):\n%s" % _descriptor_lines(entries))

    if use_diff:
        body = diff_text if diff_text.strip() else "(no changes since your last review)"
        return ("%s\n\n%s\n\nDeterministic unified diff since your last review:\n"
                "%s" % (header, DIFF_INSTRUCTION, body))
    return "%s\n\n%s" % (header, FULL_REREAD_INSTRUCTION)
