#!/usr/bin/env python3
"""The guard actions ledger's `command_fingerprint` identifies WHICH shell
command was attempted.

Run: python3 scripts/cowork_offline_tests.py test_command_fingerprint

The value is `cmd1:` plus a SHA-256 over the neutrally normalized command text
(`cowork_action_policy.command_identity`), or null when no command text exists.
It is content-free and independent of targets, proof and class, which stay
carried by `path_digests`, `target_count` and `action_class`.  Every input here
is synthetic and neutral; nothing spawns a provider.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_action_policy as action_policy  # noqa: E402
import cowork_guard_broker as guard_broker  # noqa: E402
import cowork_measure as measure  # noqa: E402

FORMAT = re.compile(r"^cmd1:[0-9a-f]{64}$")
RECORD_KEYS = {
    "guard_attempt_id", "work_id", "parent_work_id", "allow", "reason",
    "action_class", "target_count", "path_digests", "command_fingerprint"}
OPTIONAL_KEYS = {"stage_count", "authorities", "unprovable"}


class CommandIdentityTests(unittest.TestCase):

    def identity(self, text):
        return action_policy.command_identity(text)

    def assertSame(self, left, right):
        self.assertIsNotNone(self.identity(left))
        self.assertEqual(self.identity(left), self.identity(right),
                         (left, right))

    def assertDifferent(self, left, right):
        self.assertIsNotNone(self.identity(left))
        self.assertIsNotNone(self.identity(right))
        self.assertNotEqual(self.identity(left), self.identity(right),
                            (left, right))

    def test_value_is_versioned_digest(self):
        self.assertRegex(self.identity("frobnicate alpha"), FORMAT)

    def test_identical_text_is_equal(self):
        self.assertSame("frobnicate alpha --beta", "frobnicate alpha --beta")

    def test_outer_whitespace_and_unquoted_runs_collapse(self):
        self.assertSame("frobnicate alpha", "  frobnicate alpha \t\n ")
        self.assertSame("frobnicate alpha beta",
                        "frobnicate   alpha \t beta")
        self.assertSame("frobnicate alpha", "frobnicate\talpha")
        self.assertSame("echo  'a  b'", "echo 'a  b'")

    def test_whitespace_inside_quotes_is_preserved(self):
        self.assertDifferent("echo 'a  b'", "echo 'a b'")
        self.assertDifferent('echo "a  b"', 'echo "a b"')
        self.assertDifferent("echo 'a b'", "echo 'a\tb'")

    def test_different_text_differs(self):
        self.assertDifferent("frobnicate alpha", "frobnicate beta")
        self.assertDifferent("frobnicate alpha beta",
                             "frobnicate beta alpha")
        self.assertDifferent("frobnicate alpha", "Frobnicate alpha")
        self.assertDifferent("frobnicate -n 5", "frobnicate -n5")
        self.assertDifferent("frobnicate alpha\nquuxify",
                             "frobnicate alpha quuxify")
        self.assertDifferent("frobnicate && quuxify", "frobnicate ; quuxify")
        self.assertDifferent("frobnicate", "quuxify")

    def test_escaped_quote_does_not_open_a_quote(self):
        self.assertSame("echo \\' a  b", "echo \\' a b")
        self.assertDifferent("echo ' a  b", "echo ' a b")
        self.assertDifferent('echo "a\\"  b"', 'echo "a\\" b"')

    def test_single_quotes_have_no_escapes(self):
        self.assertSame("echo 'a\\'  b", "echo 'a\\' b")

    def test_unterminated_quote_still_has_a_stable_identity(self):
        text = 'echo "abc  def'
        self.assertIsNotNone(self.identity(text))
        self.assertEqual(self.identity(text), self.identity(text))
        self.assertDifferent(text, 'echo "abc def')
        self.assertIsNotNone(self.identity("echo abc\\"))

    def test_no_command_text_has_no_identity(self):
        for value in ("", "   ", "\n\t ", None, 5, ["frobnicate"],
                      b"frobnicate", {"command": "frobnicate"}):
            with self.subTest(value=value):
                self.assertIsNone(self.identity(value))

    def test_unicode_and_lone_surrogates_do_not_raise(self):
        for value in ("echo héllo ☃", "echo \ud800 x"):
            with self.subTest(value=value):
                self.assertRegex(self.identity(value), FORMAT)

    def test_identity_never_contains_the_input(self):
        text = "frobnicate  sentinelzq91   /opt/neutral/area"
        value = self.identity(text)
        for fragment in ("frobnicate", "sentinelzq91", "/opt/neutral",
                         "frobnicate sentinelzq91 /opt/neutral/area"):
            self.assertNotIn(fragment, value)


class _PolicyFixture(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.owned = os.path.join(self.root, "owned")
        os.mkdir(self.owned)
        self.sub = os.path.join(self.owned, "sub")
        os.mkdir(self.sub)
        self.protected = os.path.join(self.root, "state", "auth.json")
        os.makedirs(os.path.dirname(self.protected))
        Path(self.protected).write_text("{}\n")
        self.scope = action_policy.OwnedScope(
            repo_roots=(self.owned,), protected_paths=(self.protected,))

    def record(self, command, cwd=None, tool="Bash", key="command"):
        action = action_policy.classify_action(
            tool, {key: command}, cwd=cwd or self.owned)
        decision = action_policy.decide(action, self.scope)
        return action_policy.sanitize(decision, action)


class ClassifyAndSanitizeTests(_PolicyFixture):

    def test_shell_tools_take_identity_from_command_or_cmd(self):
        expected = action_policy.command_identity("ls")
        for tool in ("Bash", "Shell", "exec_command"):
            for tool_input in ({"command": "ls"}, {"cmd": "ls"},
                               {"command": "", "cmd": "ls"},
                               {"command": None, "cmd": "ls"}):
                with self.subTest(tool=tool, tool_input=tool_input):
                    action = action_policy.classify_action(
                        tool, tool_input, cwd=self.owned)
                    self.assertEqual(action["command_identity"], expected)
                    record = action_policy.sanitize(
                        action_policy.decide(action, self.scope), action)
                    self.assertEqual(record["command_fingerprint"], expected)

    def test_missing_or_non_command_input_is_null(self):
        for tool_input in ({}, {"command": ""}, {"command": "   "},
                           {"command": None}, {"command": 5},
                           {"command": ["ls"]}, {"cmd": "\n"}):
            with self.subTest(tool_input=tool_input):
                action = action_policy.classify_action(
                    "Bash", tool_input, cwd=self.owned)
                record = action_policy.sanitize(
                    action_policy.decide(action, self.scope), action)
                self.assertIsNone(record["command_fingerprint"])

    def test_non_shell_tools_are_null(self):
        target = os.path.join(self.owned, "item.txt")
        digest = action_policy._digest({"a": 1})
        cases = (
            ("Write", {"file_path": target}, {}),
            ("Edit", {"file_path": target}, {}),
            ("Read", {"file_path": target}, {}),
            ("Agent", {"prompt": "frobnicate"}, {}),
            ("MysteryTool", {"command": "frobnicate"}, {}),
            ("CapTool", {"command": "frobnicate"},
             {"capability_allowlist": {"CapTool": {"schema_digest": digest}},
              "installed_schema": {"a": 1}}),
        )
        for tool, tool_input, extra in cases:
            with self.subTest(tool=tool):
                action = action_policy.classify_action(
                    tool, tool_input, cwd=self.owned, **extra)
                record = action_policy.sanitize(
                    action_policy.decide(action, self.scope), action)
                self.assertIsNone(record["command_fingerprint"])

    def test_no_action_is_null(self):
        record = action_policy.sanitize(
            {"allow": False, "reason": "guard_unavailable"})
        self.assertIsNone(record["command_fingerprint"])

    def test_malformed_identity_never_reaches_the_record(self):
        good = action_policy.command_identity("frobnicate alpha")
        bad = ("frobnicate alpha", "cmd1:" + "a" * 63, "cmd1:" + "a" * 65,
               "cmd1:" + "A" * 64, "cmd1:" + "a" * 64 + "\n", "a" * 64,
               "cmd2:" + "a" * 64, "", 5, ["x"])
        for value in bad:
            with self.subTest(value=value):
                record = action_policy.sanitize(
                    {"allow": False}, {"class": "unknown", "targets": [],
                                       "command_identity": value})
                self.assertIsNone(record["command_fingerprint"])
        record = action_policy.sanitize(
            {"allow": False}, {"class": "unknown", "targets": [],
                               "command_identity": good})
        self.assertEqual(record["command_fingerprint"], good)

    def test_hand_built_action_without_identity_is_null(self):
        record = action_policy.sanitize(
            {"allow": True}, {"class": "read", "targets": [],
                              "proof": "inert_verb"})
        self.assertIsNone(record["command_fingerprint"])

    def test_record_shape_is_unchanged(self):
        for command in ("ls", "touch item.txt", "frobnicate alpha", ""):
            with self.subTest(command=command):
                record = self.record(command)
                keys = set(record)
                self.assertLessEqual(RECORD_KEYS, keys)
                self.assertLessEqual(keys - RECORD_KEYS, OPTIONAL_KEYS)
                self.assertNotIn("command_identity", json.dumps(record))

    def test_command_identity_is_independent_of_targets_and_proof(self):
        first = self.record("touch item.txt")
        second = self.record("cat source.txt > item.txt")
        self.assertEqual(first["path_digests"], second["path_digests"])
        self.assertEqual(first["target_count"], second["target_count"])
        self.assertEqual(first["action_class"], second["action_class"])
        self.assertNotEqual(first["command_fingerprint"],
                            second["command_fingerprint"])

    def test_same_command_in_two_directories_keeps_target_identity(self):
        here = self.record("touch item.txt", cwd=self.owned)
        there = self.record("touch item.txt", cwd=self.sub)
        self.assertEqual(here["command_fingerprint"],
                         there["command_fingerprint"])
        self.assertNotEqual(here["path_digests"], there["path_digests"])

    def test_decisions_do_not_depend_on_the_identity(self):
        commands = ("ls", "git status", "touch item.txt",
                    "cat source.txt > item.txt", "frobnicate alpha",
                    "ls && frobnicate", "ls && git status | head",
                    "cat < %s" % self.protected, "", "   ")
        for command in commands:
            with self.subTest(command=command):
                action = action_policy.classify_action(
                    "Bash", {"command": command}, cwd=self.owned)
                decision = action_policy.decide(action, self.scope)
                stripped = {key: value for key, value in action.items()
                            if key != "command_identity"}
                replaced = dict(action, command_identity="unrelated")
                self.assertEqual(
                    decision, action_policy.decide(stripped, self.scope))
                self.assertEqual(
                    decision, action_policy.decide(replaced, self.scope))


class BrokerLedgerWiringTests(_PolicyFixture):

    def setUp(self):
        super().setUp()
        self.actions = os.path.join(self.root, "actions.jsonl")
        parent = {
            "controller": "claude", "controller_source": "config_pinned",
            "model": "sonnet", "model_source": "config_pinned",
            "effort": "high", "effort_source": "config_pinned",
        }
        self.broker = guard_broker.GuardBroker(
            os.path.join(self.root, "guard.sock"), "token", self.scope,
            self.actions, os.path.join(self.root, "children.jsonl"),
            os.path.join(self.root, "trace.jsonl"), parent)
        self._counter = 0

    def _attempt(self):
        self._counter += 1
        return "attempt-%d" % self._counter

    def _handle(self, tool_input, event="PreToolUse", tool="Bash",
                token="token", **payload):
        attempt_id = self._attempt()
        body = {"hook_event_name": event, "tool_name": tool,
                "tool_input": tool_input, "cwd": self.owned}
        body.update(payload)
        self.broker.handle({"guard_attempt_id": attempt_id, "token": token,
                            "payload": body})
        return self._row(attempt_id)

    def _bash(self, command, **kwargs):
        return self._handle({"command": command}, **kwargs)

    def _row(self, attempt_id):
        with open(self.actions) as fh:
            rows = [json.loads(line) for line in fh]
        return next(row for row in rows
                    if row["guard_attempt_id"] == attempt_id)

    def test_different_denied_unknown_commands_are_distinguishable(self):
        commands = ("frobnicate alpha", "frobnicate beta", "quuxify alpha",
                    "quuxify alpha beta")
        rows = [self._bash(command) for command in commands]
        for row in rows:
            self.assertEqual(row["reason"], "shell_unprovable")
            self.assertEqual(row["action_class"], "unknown")
            self.assertEqual(row["target_count"], 0)
            self.assertEqual(row["path_digests"], [])
            self.assertRegex(row["command_fingerprint"], FORMAT)
        fingerprints = [row["command_fingerprint"] for row in rows]
        self.assertEqual(len(set(fingerprints)), len(commands))

    def test_retried_command_keeps_its_fingerprint(self):
        first = self._bash("frobnicate alpha")
        retry = self._bash("frobnicate alpha")
        spaced = self._bash("  frobnicate    alpha  ")
        self.assertNotEqual(first["guard_attempt_id"],
                            retry["guard_attempt_id"])
        self.assertEqual(first["command_fingerprint"],
                         retry["command_fingerprint"])
        self.assertEqual(first["command_fingerprint"],
                         spaced["command_fingerprint"])

    def test_durable_row_is_content_free(self):
        command = "frobnicate   /opt/neutral/area/sentinelzq91  sentinelzq91"
        row = self._bash(command)
        self.assertRegex(row["command_fingerprint"], FORMAT)
        serialized = json.dumps(row)
        for fragment in (
                "sentinelzq91", "/opt/neutral", command,
                "frobnicate /opt/neutral/area/sentinelzq91 sentinelzq91"):
            self.assertNotIn(fragment, serialized)

    def test_allowed_rows_carry_a_fingerprint(self):
        read = self._bash("ls")
        write = self._bash("touch item.txt")
        self.assertTrue(read["allow"])
        self.assertTrue(write["allow"])
        self.assertEqual(read["command_fingerprint"],
                         action_policy.command_identity("ls"))
        self.assertEqual(write["command_fingerprint"],
                         action_policy.command_identity("touch item.txt"))

    def test_mutation_evidence_matches_the_pre_execution_row(self):
        pre = self._bash("touch item.txt")
        post = self._bash("touch item.txt", event="PostToolUse")
        self.assertEqual(post["evidence_kind"], "mutation_effect")
        self.assertEqual(pre["command_fingerprint"],
                         post["command_fingerprint"])
        self.assertRegex(post["command_fingerprint"], FORMAT)

    def test_uncorrelated_child_row_carries_the_fingerprint(self):
        row = self._bash("frobnicate alpha", agent_id="unknown-agent")
        self.assertEqual(row["reason"], "child_agent_correlation_unavailable")
        self.assertEqual(row["command_fingerprint"],
                         action_policy.command_identity("frobnicate alpha"))

    def test_rows_without_command_text_are_null(self):
        self.assertIsNone(self._bash(
            "frobnicate alpha", token="wrong")["command_fingerprint"])
        write = self._handle(
            {"file_path": os.path.join(self.owned, "item.txt")}, tool="Write")
        self.assertIsNone(write["command_fingerprint"])
        self.assertIsNone(self._handle({})["command_fingerprint"])
        self.assertIsNone(self._bash("   ")["command_fingerprint"])


class LegacyCompatibilityTests(unittest.TestCase):

    def test_legacy_rows_pass_through_reconciliation(self):
        legacy_value = hashlib.sha256(b"legacy-neutral").hexdigest()
        legacy = {"guard_attempt_id": "legacy-1", "allow": False,
                  "reason": "shell_unprovable", "action_class": "unknown",
                  "target_count": 0, "path_digests": [],
                  "command_fingerprint": legacy_value}
        fresh = action_policy.sanitize(
            {"allow": False, "reason": "shell_unprovable"},
            {"class": "unknown", "targets": [],
             "command_identity": action_policy.command_identity(
                 "frobnicate alpha")},
            guard_attempt_id="fresh-1")
        records = measure.reconcile_guard_records([legacy, fresh], [])
        by_id = {record["guard_attempt_id"]: record for record in records}
        self.assertEqual(by_id["legacy-1"]["command_fingerprint"],
                         legacy_value)
        self.assertEqual(by_id["legacy-1"]["evidence_channel"], "broker")
        self.assertEqual(by_id["fresh-1"]["command_fingerprint"],
                         fresh["command_fingerprint"])
        self.assertNotEqual(legacy_value, fresh["command_fingerprint"])
        self.assertIsNone(FORMAT.match(legacy_value))


if __name__ == "__main__":
    unittest.main()
