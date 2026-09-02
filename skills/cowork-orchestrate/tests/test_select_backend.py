from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "select_backend.py"
SPEC = importlib.util.spec_from_file_location("select_backend", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SELECT_BACKEND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECT_BACKEND)

NOW = dt.datetime(2026, 9, 1, 20, 0, tzinfo=dt.timezone.utc)
HEX = "a" * 64


def valid_evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "release_digest": HEX,
        "issued_at": "2026-09-01T00:00:00Z",
        "expires_at": "2026-09-08T00:00:00Z",
        "criteria": {
            str(number): {"status": "PASS", "receipt_sha256": HEX}
            for number in range(1, 7)
        },
    }


class SelectBackendTests(unittest.TestCase):
    def test_complete_current_evidence_selects_cowork(self) -> None:
        result = SELECT_BACKEND.select(valid_evidence(), NOW)
        self.assertEqual("cowork", result["backend"])
        self.assertTrue(result["cowork_eligible"])
        self.assertEqual([], result["failures"])

    def test_failed_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["5"]["status"] = "FAIL"  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("direct-claude", result["backend"])
        self.assertIn("criterion_5_not_pass", result["failures"])

    def test_missing_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        del evidence["criteria"]["6"]  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertIn("missing_criteria:6", result["failures"])

    def test_extra_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["7"] = {  # type: ignore[index]
            "status": "PASS", "receipt_sha256": HEX}
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertIn("unexpected_criteria:7", result["failures"])

    def test_expired_evidence_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["expires_at"] = "2026-09-01T20:00:00Z"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertIn("evidence_stale", result["failures"])

    def test_future_evidence_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["issued_at"] = "2026-09-02T00:00:00Z"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertIn("evidence_issued_in_future", result["failures"])

    def test_naive_timestamp_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["issued_at"] = "2026-09-01T00:00:00"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertIn("invalid_validity_window", result["failures"])

    def test_malformed_receipt_hash_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["2"]["receipt_sha256"] = "not-a-hash"  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertIn("criterion_2_invalid_receipt", result["failures"])

    def test_cli_missing_file_reports_absent(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--evidence", "/no/such/file"],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual("direct-claude", result["backend"])
        self.assertEqual(["evidence_absent"], result["failures"])

    def test_cli_unreadable_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text("{", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--evidence", str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        result = json.loads(completed.stdout)
        self.assertEqual("direct-claude", result["backend"])
        self.assertTrue(result["reason"].startswith("evidence_unreadable"))


if __name__ == "__main__":
    unittest.main()
