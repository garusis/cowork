#!/usr/bin/env python3
"""Select Cowork only from complete, integrity-verified backend-gate receipts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED = {str(number) for number in range(1, 7)}


def _timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_not_string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    return parsed.astimezone(dt.timezone.utc)


def select(evidence: Any, now: dt.datetime) -> dict[str, Any]:
    reasons: list[str] = []
    elapsed: bool | None = None
    if not isinstance(evidence, dict):
        reasons.append("evidence_not_object")
    else:
        if evidence.get("schema_version") != 1:
            reasons.append("unsupported_schema")
        if not HEX64.fullmatch(str(evidence.get("release_digest", ""))):
            reasons.append("invalid_release_digest")
        try:
            issued = _timestamp(evidence.get("issued_at"))
            expires = _timestamp(evidence.get("expires_at"))
            if issued > now:
                reasons.append("evidence_issued_in_future")
            if expires <= issued:
                reasons.append("incoherent_validity_window")
            # An elapsed window is reported, never disqualifying: a verified
            # immutable release does not stop being verified with the calendar.
            elapsed = expires <= now
        except (TypeError, ValueError):
            reasons.append("invalid_validity_window")

        criteria = evidence.get("criteria")
        if not isinstance(criteria, dict):
            reasons.append("criteria_not_object")
        else:
            keys = set(criteria)
            missing = sorted(EXPECTED - keys)
            extra = sorted(keys - EXPECTED)
            if missing:
                reasons.append("missing_criteria:" + ",".join(missing))
            if extra:
                reasons.append("unexpected_criteria:" + ",".join(extra))
            for key in sorted(EXPECTED & keys):
                item = criteria[key]
                if not isinstance(item, dict):
                    reasons.append(f"criterion_{key}_not_object")
                    continue
                if item.get("status") != "PASS":
                    reasons.append(f"criterion_{key}_not_pass")
                if not HEX64.fullmatch(str(item.get("receipt_sha256", ""))):
                    reasons.append(f"criterion_{key}_invalid_receipt")

    eligible = not reasons
    return {
        "schema_version": 2,
        "backend": "cowork" if eligible else "blocked",
        "cowork_eligible": eligible,
        "validity_elapsed": elapsed,
        "reason": "all_backend_gate_criteria_verified" if eligible else reasons[0],
        "failures": reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--now", help="ISO-8601 test/verification clock; defaults to UTC now")
    args = parser.parse_args()
    now = _timestamp(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    path = Path(args.evidence)
    if not path.is_file():
        result = {
            "schema_version": 2,
            "backend": "blocked",
            "cowork_eligible": False,
            "validity_elapsed": None,
            "reason": "evidence_absent",
            "failures": ["evidence_absent"],
        }
    else:
        try:
            result = select(json.loads(path.read_text(encoding="utf-8")), now)
        except (OSError, json.JSONDecodeError) as exc:
            result = {
                "schema_version": 2,
                "backend": "blocked",
                "cowork_eligible": False,
                "validity_elapsed": None,
                "reason": "evidence_unreadable",
                "failures": [f"evidence_unreadable:{type(exc).__name__}"],
            }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
