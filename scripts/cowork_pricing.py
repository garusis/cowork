#!/usr/bin/env python3
"""Versioned token-normalization and pricing schema (P8).

The schema ships with an EMPTY snapshot, so by default every model resolves to
`unpriced` and no cost figure in the record claims to be money. That is
deliberate. Commercial prices baked into a repository are stale by
construction — they change without a commit — and a confidently wrong dollar
figure is worse than an honest "unpriced", because it gets quoted.

What IS delivered is the machinery: a versioned schema, a snapshot loader, and
provenance stamps (`pricing_schema_version`, `pricing_snapshot_id`) on every
cost field, so the day a real snapshot is supplied every figure says which
prices produced it.

Python 3.9+, stdlib only.
"""

import json
import os

SCHEMA_VERSION = 1

# The canonical token axes. Providers name these differently — Claude reports
# `cache_creation_input_tokens` / `cache_read_input_tokens`, Codex reports
# `cache_write_input_tokens` / `cached_input_tokens` — and the two are NOT
# renamed away when they are reconciled. Normalization maps onto these axes and
# keeps the provider's own field names alongside, so nothing is lost in
# translation.
AXES = ("input", "cached_input", "cache_write", "output", "reasoning_output")

# provider field name -> canonical axis.
_FIELD_MAP = {
    "input_tokens": "input",
    "prompt_tokens": "input",
    "cache_read_input_tokens": "cached_input",
    "cached_input_tokens": "cached_input",
    "cache_creation_input_tokens": "cache_write",
    "cache_write_input_tokens": "cache_write",
    "output_tokens": "output",
    "completion_tokens": "output",
    "reasoning_output_tokens": "reasoning_output",
}

DEFAULT_SNAPSHOT = {
    "schema_version": SCHEMA_VERSION,
    "snapshot_id": "empty",
    "captured_at": None,
    "models": {},
}


def snapshot_path(root=None):
    """Path of the pricing snapshot. Overridable via COWORK_PRICING_SNAPSHOT so
    an operator can point at their own captured prices without editing the
    repository copy."""
    override = os.environ.get("COWORK_PRICING_SNAPSHOT")
    if override:
        return override
    base = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "pricing", "snapshot.json")


def load_snapshot(path=None):
    """Load the pricing snapshot, falling back to the empty default.

    Tolerant: a missing or malformed snapshot yields the empty default rather
    than an error, because a broken price file must leave figures `unpriced`,
    not stop a report from rendering.
    """
    path = path or snapshot_path()
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return dict(DEFAULT_SNAPSHOT)
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return dict(DEFAULT_SNAPSHOT)
    out = dict(DEFAULT_SNAPSHOT)
    out.update({k: data.get(k, out.get(k))
                for k in ("schema_version", "snapshot_id", "captured_at",
                          "models")})
    return out


def normalize_usage(usage):
    """Map one provider usage dict onto the canonical axes.

    Returns `{"axes": {...}, "native": {...}, "unmapped": [...]}`. `native` is
    the provider's own counters kept verbatim; `unmapped` names any field with
    no canonical axis, so a provider adding a counter shows up as an explicit
    gap rather than vanishing into a total that no longer adds up.
    """
    axes = {axis: 0 for axis in AXES}
    native = {}
    unmapped = []
    if not isinstance(usage, dict):
        return {"axes": axes, "native": {}, "unmapped": [], "state": "unknown"}
    for field, value in usage.items():
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        native[field] = value
        axis = _FIELD_MAP.get(field)
        if axis is None:
            # `total_tokens` is a sum of the others, not a new axis: counting it
            # would double every figure it appears in.
            if field not in ("total_tokens", "total"):
                unmapped.append(field)
            continue
        axes[axis] += value
    return {"axes": axes, "native": native, "unmapped": sorted(unmapped),
            "state": "ok"}


def price_usage(usage, model, snapshot=None):
    """Price one usage dict, or say honestly that it cannot be priced.

    Returns a dict that ALWAYS carries `pricing_schema_version` and
    `pricing_snapshot_id`, so a cost figure can be traced to the prices that
    produced it. A model absent from the snapshot resolves to
    `state='unpriced'` with `cost=None` — never 0, which would read as free.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else load_snapshot()
    stamp = {
        "pricing_schema_version": snapshot.get("schema_version",
                                               SCHEMA_VERSION),
        "pricing_snapshot_id": snapshot.get("snapshot_id", "empty"),
        "pricing_captured_at": snapshot.get("captured_at"),
        "model": model,
    }
    normalized = normalize_usage(usage)
    rates = (snapshot.get("models") or {}).get(model)
    if not isinstance(rates, dict):
        stamp.update({"state": "unpriced", "cost": None,
                      "axes": normalized["axes"],
                      "reason": "model not in pricing snapshot"})
        return stamp
    total = 0.0
    missing = []
    for axis, tokens in normalized["axes"].items():
        if not tokens:
            continue
        rate = rates.get(axis)
        if not isinstance(rate, (int, float)):
            missing.append(axis)
            continue
        total += (tokens / 1_000_000.0) * float(rate)
    if missing:
        # A partial price is not a price. Naming the axes that had no rate is
        # more useful than a total that silently omits them.
        stamp.update({"state": "unpriced", "cost": None,
                      "axes": normalized["axes"],
                      "reason": "no rate for axis: %s" % ", ".join(missing)})
        return stamp
    stamp.update({"state": "priced", "cost": round(total, 6),
                  "currency": snapshot.get("currency", "USD"),
                  "axes": normalized["axes"]})
    return stamp
