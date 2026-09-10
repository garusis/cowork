"""Cowork-only routing tests.

Two things are covered here. `SelectBackendTests` covers the selector's routing
semantics: an elapsed-but-coherent validity window is reported and still selects
Cowork, and every integrity failure lands on the `blocked` outcome. The
`RoutingSurfaceTests` class covers the routing surfaces of the two orchestration
skills as text: no selectable direct-Claude route or fallback vocabulary
survives, no receipt-currency claim survives, both skill frontmatters satisfy
the skill-authoring rules, and the three statements this migration must make are
present and quotable.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
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

REPO_ROOT = Path(__file__).resolve().parents[3]

# Exactly the routing surfaces of the two authorized skill trees. The frozen
# historical controller (scripts/orchestrate.py, tests/test_orchestrate.py) is
# deliberately excluded, as is this test file, which necessarily carries the
# forbidden tokens as pattern literals.
ROUTING_SURFACES = (
    "skills/cowork-orchestrate/SKILL.md",
    "skills/cowork-orchestrate/references/backend-gate.md",
    "skills/cowork-orchestrate/references/backend-gate-pointer.md",
    "skills/cowork-orchestrate/agents/openai.yaml",
    "skills/cowork-orchestrate/scripts/select_backend.py",
    "skills/cowork-refactor-orchestrator/SKILL.md",
    "skills/cowork-refactor-orchestrator/references/artifact-contract-schema.md",
    "skills/cowork-refactor-orchestrator/references/bootstrap-backend.md",
    "skills/cowork-refactor-orchestrator/references/cowork-backend-gate.md",
    "skills/cowork-refactor-orchestrator/agents/openai.yaml",
)

# A line carrying one of these is a surviving route to the removed backend,
# unless the line explicitly marks itself as describing the historical
# controller.
FORBIDDEN_ROUTING_TOKENS = (
    "direct-claude",
    "direct claude",
    "direct_claude",
    "claude_direct",
    "fallback",
    "fall back",
)
HISTORICAL_MARKER = "historical"

# Receipt-currency phrasing: the age requirement this migration retires. These
# carry no escape - they must simply be absent.
FORBIDDEN_CURRENCY_PHRASES = (
    "current release-bound receipt",
    "current, release-bound receipt",
)

FRONTMATTER_FILES = (
    "skills/cowork-orchestrate/SKILL.md",
    "skills/cowork-refactor-orchestrator/SKILL.md",
)
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
    "compatibility",
}
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The user's authority amendment: Cowork is the default, and the one permitted
# way out is a supervisor-reviewed repair of a real Cowork blocker through the
# invoke-claude-agent skill, returning to Cowork afterwards. Calendar expiry and
# self-hosting must never reach it. Every surface that states the exception must
# state all of its limits, so a partial restatement cannot widen it.
EXCEPTION_SURFACES = (
    "skills/cowork-orchestrate/SKILL.md",
    "skills/cowork-orchestrate/references/backend-gate.md",
    "skills/cowork-refactor-orchestrator/SKILL.md",
    "skills/cowork-refactor-orchestrator/references/cowork-backend-gate.md",
)
EXCEPTION_REQUIRED_TOKENS = (
    "invoke-claude-agent",
    "real cowork blocker",
    "supervisor",
)

# The self-hosting runner must be resolved and frozen per package, never pinned
# as a commit literal in prose - a literal goes stale silently and then names a
# runner nobody validated.
SELF_HOSTING_RUNNER_DOC = (
    "skills/cowork-refactor-orchestrator/references/bootstrap-backend.md"
)
COMMIT_LITERAL = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
RUNNER_FREEZE_TOKENS = (
    "per package",
    "frozen for the whole run",
    "historical receipts",
    "global accreditation",
)

# UI metadata contract for the agent cards.
AGENT_METADATA_FILES = {
    "skills/cowork-orchestrate/agents/openai.yaml": "$cowork-orchestrate",
    "skills/cowork-refactor-orchestrator/agents/openai.yaml": (
        "$cowork-refactor-orchestrator"
    ),
}
AGENT_FIELD_PATTERN = re.compile(r'^\s*([A-Za-z_]+):\s*"(.*)"\s*$')
SHORT_DESCRIPTION_MIN = 25
SHORT_DESCRIPTION_MAX = 64


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


def read_surface(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse a simple one-line `key: value` frontmatter block without PyYAML."""
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---"
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, separator, value = line.partition(":")
        if not separator:
            raise AssertionError(f"frontmatter line is not `key: value`: {line!r}")
        fields[key.strip()] = value.strip()
    raise AssertionError("frontmatter block is not terminated by ---")


class SelectBackendTests(unittest.TestCase):
    def test_complete_current_evidence_selects_cowork(self) -> None:
        result = SELECT_BACKEND.select(valid_evidence(), NOW)
        self.assertEqual("cowork", result["backend"])
        self.assertTrue(result["cowork_eligible"])
        self.assertEqual([], result["failures"])
        self.assertEqual(2, result["schema_version"])
        self.assertFalse(result["validity_elapsed"])

    def test_elapsed_but_coherent_window_selects_cowork(self) -> None:
        evidence = valid_evidence()
        evidence["expires_at"] = "2026-09-01T20:00:00Z"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("cowork", result["backend"])
        self.assertTrue(result["cowork_eligible"])
        self.assertEqual([], result["failures"])
        self.assertTrue(result["validity_elapsed"])

    def test_failed_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["5"]["status"] = "FAIL"  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("criterion_5_not_pass", result["failures"])

    def test_missing_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        del evidence["criteria"]["6"]  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("missing_criteria:6", result["failures"])

    def test_extra_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["7"] = {  # type: ignore[index]
            "status": "PASS", "receipt_sha256": HEX}
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("unexpected_criteria:7", result["failures"])

    def test_future_evidence_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["issued_at"] = "2026-09-02T00:00:00Z"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("evidence_issued_in_future", result["failures"])

    def test_naive_timestamp_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["issued_at"] = "2026-09-01T00:00:00"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("invalid_validity_window", result["failures"])

    def test_malformed_receipt_hash_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["2"]["receipt_sha256"] = "not-a-hash"  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("criterion_2_invalid_receipt", result["failures"])

    def test_invalid_release_digest_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["release_digest"] = "not-a-hash"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("invalid_release_digest", result["failures"])

    def test_non_object_evidence_fails_closed(self) -> None:
        result = SELECT_BACKEND.select([valid_evidence()], NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("evidence_not_object", result["failures"])
        self.assertIsNone(result["validity_elapsed"])

    def test_unsupported_input_schema_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["schema_version"] = 2
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("unsupported_schema", result["failures"])

    def test_incoherent_validity_window_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["issued_at"] = "2026-09-01T00:00:00Z"
        evidence["expires_at"] = "2026-08-31T00:00:00Z"
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("incoherent_validity_window", result["failures"])

    def test_non_object_criterion_fails_closed(self) -> None:
        evidence = valid_evidence()
        evidence["criteria"]["3"] = "PASS"  # type: ignore[index]
        result = SELECT_BACKEND.select(evidence, NOW)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertIn("criterion_3_not_object", result["failures"])

    def test_cli_missing_file_reports_absent(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--evidence", "/no/such/file"],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
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
        self.assertEqual("blocked", result["backend"])
        self.assertFalse(result["cowork_eligible"])
        self.assertTrue(result["reason"].startswith("evidence_unreadable"))


class RoutingSurfaceTests(unittest.TestCase):
    def test_no_selectable_direct_claude_route_in_authorized_trees(self) -> None:
        missing = [
            relative
            for relative in ROUTING_SURFACES
            if not (REPO_ROOT / relative).is_file()
        ]
        self.assertEqual(
            [], missing,
            f"routing surfaces missing - a rename would silently empty the scan: {missing}",
        )

        offenders: list[str] = []
        for relative in ROUTING_SURFACES:
            for number, line in enumerate(read_surface(relative).splitlines(), start=1):
                lowered = line.lower()
                routing_hits = [
                    token for token in FORBIDDEN_ROUTING_TOKENS if token in lowered
                ]
                if routing_hits and HISTORICAL_MARKER not in lowered:
                    offenders.append(
                        f"{relative}:{number}: {sorted(routing_hits)}: {line.strip()}"
                    )
                currency_hits = [
                    phrase for phrase in FORBIDDEN_CURRENCY_PHRASES if phrase in lowered
                ]
                if currency_hits:
                    offenders.append(
                        f"{relative}:{number}: {sorted(currency_hits)}: {line.strip()}"
                    )
        self.assertEqual(
            [], offenders,
            "surviving fallback or receipt-currency language:\n" + "\n".join(offenders),
        )

    def test_skill_frontmatter_meets_authoring_rules(self) -> None:
        for relative in FRONTMATTER_FILES:
            with self.subTest(skill=relative):
                text = read_surface(relative)
                self.assertTrue(
                    text.startswith("---"), f"{relative} has no frontmatter block")
                fields = parse_frontmatter(text)

                unexpected = sorted(set(fields) - ALLOWED_FRONTMATTER_KEYS)
                self.assertEqual([], unexpected, f"{relative} has unknown keys")
                self.assertIn("name", fields, f"{relative} is missing name")
                self.assertIn(
                    "description", fields, f"{relative} is missing description")

                name = fields["name"]
                self.assertRegex(name, SKILL_NAME_PATTERN, f"{relative} name shape")
                self.assertLessEqual(len(name), 64, f"{relative} name length")

                description = fields["description"]
                self.assertNotEqual("", description, f"{relative} empty description")
                self.assertNotIn("<", description, f"{relative} description angle")
                self.assertNotIn(">", description, f"{relative} description angle")
                self.assertLessEqual(
                    len(description), 1024, f"{relative} description length")

    def test_required_routing_statements_present(self) -> None:
        orchestrate_skill = read_surface("skills/cowork-orchestrate/SKILL.md").lower()
        self.assertTrue(
            any(
                "preflight" in line and "blocked" in line
                for line in orchestrate_skill.splitlines()
            ),
            "cowork-orchestrate/SKILL.md must state that a failed preflight is blocked",
        )

        refactor_skill = read_surface(
            "skills/cowork-refactor-orchestrator/SKILL.md").lower()
        for needle in ("orchestrate.py", "historical", "dispatch"):
            self.assertIn(
                needle, refactor_skill,
                "cowork-refactor-orchestrator/SKILL.md must name orchestrate.py as a "
                "historical controller that must not dispatch new work",
            )

        runner = read_surface(
            "skills/cowork-refactor-orchestrator/references/bootstrap-backend.md").lower()
        self.assertTrue(
            any(
                "target" in line and "runner" in line and "changed" in line
                for line in runner.splitlines()
            ),
            "bootstrap-backend.md must state that a diff in the target does not mean "
            "the runner changed",
        )
        self.assertIn("frozen", runner)
        self.assertIn("isolated target worktree", runner)

    def test_authorized_exception_is_stated_and_bounded(self) -> None:
        for relative in EXCEPTION_SURFACES:
            with self.subTest(surface=relative):
                text = read_surface(relative).lower()
                lines = text.splitlines()
                for needle in EXCEPTION_REQUIRED_TOKENS:
                    self.assertIn(
                        needle, text,
                        f"{relative} must name the {needle!r} limit on the exception",
                    )
                self.assertTrue(
                    any("return" in line and "cowork" in line for line in lines),
                    f"{relative} must state the return to Cowork after repair",
                )
                self.assertTrue(
                    any(
                        "elapsed" in line
                        and "self-hosting" in line
                        and "never" in line
                        for line in lines
                    ),
                    f"{relative} must state that an elapsed window or a self-hosting "
                    "shape never reaches the exception",
                )

        runner = read_surface(
            "skills/cowork-refactor-orchestrator/references/bootstrap-backend.md").lower()
        self.assertIn("invoke-claude-agent", runner)
        self.assertTrue(
            any(
                "self-hosting" in line and "never" in line
                for line in runner.splitlines()
            ),
            "bootstrap-backend.md must state that a self-hosting shape is never "
            "itself a reason to leave Cowork",
        )

    def test_self_hosting_runner_is_resolved_per_package_not_pinned(self) -> None:
        text = read_surface(SELF_HOSTING_RUNNER_DOC)
        pinned = COMMIT_LITERAL.findall(text)
        self.assertEqual(
            [], pinned,
            f"{SELF_HOSTING_RUNNER_DOC} pins a commit literal {pinned}; the runner "
            "must be resolved and recorded per package instead",
        )

        lowered = text.lower()
        for needle in RUNNER_FREEZE_TOKENS:
            self.assertIn(
                needle, lowered,
                f"{SELF_HOSTING_RUNNER_DOC} must state the {needle!r} rule",
            )
        self.assertTrue(
            any(
                "record" in line and "head" in line and "tree" in line
                for line in lowered.splitlines()
            ),
            f"{SELF_HOSTING_RUNNER_DOC} must state that the selected runner's "
            "HEAD/tree is recorded and frozen for the run",
        )

    def test_agent_metadata_meets_ui_contract(self) -> None:
        for relative, mention in AGENT_METADATA_FILES.items():
            with self.subTest(agent=relative):
                fields: dict[str, str] = {}
                for line in read_surface(relative).splitlines():
                    matched = AGENT_FIELD_PATTERN.match(line)
                    if matched:
                        fields[matched.group(1)] = matched.group(2)

                short = fields.get("short_description")
                self.assertIsNotNone(
                    short, f"{relative} has no short_description")
                assert short is not None
                self.assertGreaterEqual(
                    len(short), SHORT_DESCRIPTION_MIN,
                    f"{relative} short_description is {len(short)} chars, "
                    f"minimum {SHORT_DESCRIPTION_MIN}",
                )
                self.assertLessEqual(
                    len(short), SHORT_DESCRIPTION_MAX,
                    f"{relative} short_description is {len(short)} chars, "
                    f"maximum {SHORT_DESCRIPTION_MAX}",
                )

                prompt = fields.get("default_prompt")
                self.assertIsNotNone(prompt, f"{relative} has no default_prompt")
                assert prompt is not None
                self.assertIn(
                    mention, prompt,
                    f"{relative} default_prompt must mention {mention}",
                )


if __name__ == "__main__":
    unittest.main()
