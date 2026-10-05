#!/usr/bin/env python3
"""Focused permanent tests for the context envelope vocabulary and ladder
(`cowork_context`), the per-(profile, role) envelope table in
`cowork_execution_profiles`, and the expansion / closure ledger kinds in
`cowork_ledger`.

Every input is neutral and synthetic: made-up roles, turn rows, ids and limits
in temp ledgers. Nothing here asserts a limit's value, a delivery receipt or a
point in time; the tests protect the structure and the decision rules.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_envelope
"""

import ast
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_context as ctx  # noqa: E402
import cowork_execution_profiles as profiles  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_measure as measure  # noqa: E402

ENVELOPE_CODE_PREFIXES = (
    "envelope_role_not_in_team:", "envelope_missing_for_role:",
    "policy_key_in_envelope:", "unknown_metric:", "limit_invalid:",
    "hard_below_warn:", "rotation_metric_not_session_window:",
    "expansion_invalid:")


def _row(work_id="w1", started_at="2000-01-01T00:00:00Z", duration_ms=10,
         input_tokens=100, cache_tokens=5, state="complete", kind="turn",
         scope=None):
    row = {"work_id": work_id, "started_at": started_at,
           "work_state": state, "work_kind": kind,
           "duration_ms": duration_ms,
           "usage": {"input_tokens": input_tokens,
                     "cache_read_input_tokens": cache_tokens}}
    if scope is not None:
        row["usage_scope"] = scope
    return row


def _limits(**kwargs):
    base = {m: {"warn": None, "hard": None} for m in ctx.METRICS}
    for metric, (warn, hard) in kwargs.items():
        base[metric] = {"warn": warn, "hard": hard}
    return base


def _envelope(limits=None, max_per_chain=2):
    return {"limits": limits if limits is not None else _limits(),
            "expansion": {"step_pct": 25, "max_per_chain": max_per_chain},
            "rotation_trigger_metrics": ["elapsed_ms"]}


def _obs(**values):
    return {m: {"value": values.get(m, ctx.UNKNOWN),
                "timing": ctx.TIMING_BY_METRIC[m]} for m in ctx.METRICS}


def _table():
    return profiles._thaw(profiles.CONTEXT_ENVELOPES)


class EnvelopeTableTests(unittest.TestCase):
    def test_every_profile_covers_exactly_its_team(self):
        for profile in profiles.known_profiles():
            self.assertEqual(
                sorted(profiles.CONTEXT_ENVELOPES[profile]),
                sorted(profiles.team_for(profile)))

    def test_entry_structure(self):
        for profile in profiles.known_profiles():
            for role in profiles.team_for(profile):
                env = profiles.resolved_context_envelope(profile, role)
                self.assertEqual(set(env["limits"]), set(ctx.METRICS))
                for limit in env["limits"].values():
                    self.assertEqual(set(limit), {"warn", "hard"})
                    for value in limit.values():
                        self.assertTrue(
                            value is None or (
                                isinstance(value, int)
                                and not isinstance(value, bool)
                                and value >= 0))
                    if None not in limit.values():
                        self.assertGreaterEqual(limit["hard"], limit["warn"])
                for key in ("step_pct", "max_per_chain"):
                    self.assertGreater(env["expansion"][key], 0)
                self.assertTrue(set(env["rotation_trigger_metrics"])
                                <= set(ctx.SESSION_WINDOW_METRICS))
                self.assertEqual(env["profile"], profile)
                self.assertEqual(env["role"], role)
                self.assertEqual(env["envelope_version"],
                                 profiles.CONTEXT_ENVELOPE_VERSION)

    def test_resolved_envelope_is_an_independent_copy(self):
        first = profiles.resolved_context_envelope("standard", "builder")
        first["limits"]["prompt_bytes"]["hard"] = -1
        first["rotation_trigger_metrics"].append("junk")
        second = profiles.resolved_context_envelope("standard", "builder")
        self.assertNotEqual(second["limits"]["prompt_bytes"]["hard"], -1)
        self.assertNotIn("junk", second["rotation_trigger_metrics"])

    def test_role_outside_team_and_unknown_profile(self):
        self.assertIsNone(
            profiles.resolved_context_envelope("light", "planner"))
        self.assertIsNone(
            profiles.resolved_context_envelope("standard", "nobody"))
        with self.assertRaises(profiles.UnknownProfile):
            profiles.resolved_context_envelope("bogus", "builder")

    def test_table_is_frozen(self):
        with self.assertRaises(TypeError):
            profiles.CONTEXT_ENVELOPES["light"] = {}
        with self.assertRaises(TypeError):
            profiles.CONTEXT_ENVELOPES["light"]["builder"]["limits"][
                "prompt_bytes"]["hard"] = 1

    def test_metric_tuples_match_the_context_module(self):
        self.assertEqual(profiles._ENVELOPE_METRICS, ctx.METRICS)
        self.assertEqual(profiles._ENVELOPE_SESSION_WINDOW_METRICS,
                         ctx.SESSION_WINDOW_METRICS)

    def test_digest_is_stable_and_independent_of_policy_digest(self):
        self.assertEqual(profiles.context_envelope_digest(),
                         profiles.context_envelope_digest())
        policy_before = profiles.policy_digest()
        digest_before = profiles.context_envelope_digest()
        altered = _table()
        altered["standard"]["builder"]["limits"]["prompt_bytes"][
            "hard"] = 999999999
        with mock.patch.object(profiles, "CONTEXT_ENVELOPES", altered):
            self.assertNotEqual(profiles.context_envelope_digest(),
                                digest_before)
            self.assertEqual(profiles.policy_digest(), policy_before)
        self.assertEqual(profiles.policy_digest(), policy_before)
        self.assertEqual(profiles.context_envelope_digest(), digest_before)

    def test_definitions_carry_no_envelope_and_records_still_validate(self):
        for profile in profiles.known_profiles():
            for key in profiles.PROFILE_DEFINITIONS[profile]:
                self.assertNotIn("envelope", key)
                self.assertNotIn("context", key)
        record = profiles.new_record("standard", "neutral rationale",
                                     "2000-01-01T00:00:00Z")
        validated, reason = profiles.validate_record(
            record, profiles.binding_for(record))
        self.assertIsNone(reason)
        self.assertEqual(validated, record)


class EnvelopeNonWeakeningTests(unittest.TestCase):
    def _violations(self, profile, mutate):
        table = _table()
        mutate(table[profile])
        with mock.patch.object(profiles, "CONTEXT_ENVELOPES", table):
            return profiles.envelope_non_weakening_violations(profile)

    def _assert_closed(self, codes):
        for code in codes:
            self.assertTrue(code.startswith(ENVELOPE_CODE_PREFIXES), code)

    def test_shipped_profiles_have_no_violations(self):
        for profile in profiles.known_profiles():
            self.assertEqual(
                profiles.envelope_non_weakening_violations(profile), [])

    def test_unknown_profile(self):
        with self.assertRaises(profiles.UnknownProfile):
            profiles.envelope_non_weakening_violations("bogus")

    def test_legitimate_reviewer_role_names_are_not_policy_keys(self):
        codes = profiles.envelope_non_weakening_violations("standard")
        self.assertFalse(
            [c for c in codes if c.startswith("policy_key_in_envelope")])
        self.assertIn("scout-reviewer", profiles.team_for("standard"))
        self.assertIn("build-reviewer", profiles.team_for("light"))

    def test_team_role_missing_from_table(self):
        codes = self._violations("standard", lambda t: t.pop("planner"))
        self.assertEqual(codes, ["envelope_missing_for_role:planner"])

    def test_table_role_not_in_team(self):
        def add(t):
            t["planner"] = copy.deepcopy(t["builder"])
        codes = self._violations("light", add)
        self.assertEqual(codes, ["envelope_role_not_in_team:planner"])

    def test_policy_key_at_top_of_role_entry(self):
        for key in ("required_checks", "review", "cadence", "thresholds"):
            def add(t, key=key):
                t["builder"][key] = ["x"]
            codes = self._violations("standard", add)
            self.assertEqual(
                codes, ["policy_key_in_envelope:builder:%s" % key])

    def test_policy_key_nested_inside_role_entry(self):
        def add(t):
            t["builder"]["expansion"]["thresholds"] = {"revise": ["x"]}
        codes = self._violations("standard", add)
        self.assertEqual(
            codes, ["policy_key_in_envelope:builder:expansion.thresholds"])

        def deeper(t):
            t["builder"]["limits"]["prompt_bytes"]["note"] = {
                "inner": {"required_checks": []}}
        codes = self._violations("standard", deeper)
        self.assertEqual(codes, [
            "policy_key_in_envelope:builder:"
            "limits.prompt_bytes.note.inner.required_checks"])

    def test_profile_level_policy_key_is_a_role_not_in_team(self):
        def add(t):
            t["required_checks"] = {"anything": 1}
        codes = self._violations("standard", add)
        self.assertEqual(codes,
                         ["envelope_role_not_in_team:required_checks"])

    def test_invalid_limits(self):
        for bad in (-1, True, 1.5, "7"):
            def put(t, bad=bad):
                t["builder"]["limits"]["prompt_bytes"]["hard"] = bad
            codes = self._violations("standard", put)
            self.assertEqual(
                codes, ["limit_invalid:builder:prompt_bytes:hard"])

    def test_hard_below_warn(self):
        def put(t):
            t["builder"]["limits"]["elapsed_ms"] = {"warn": 10, "hard": 5}
        codes = self._violations("standard", put)
        self.assertEqual(codes, ["hard_below_warn:builder:elapsed_ms"])

    def test_unknown_limit_metric(self):
        def put(t):
            t["builder"]["limits"]["mystery"] = {"warn": 1, "hard": 2}
        codes = self._violations("standard", put)
        self.assertEqual(codes, ["unknown_metric:builder:mystery"])

    def test_rotation_metric_must_be_session_window(self):
        for metric in ("prompt_bytes", "mystery"):
            def put(t, metric=metric):
                t["builder"]["rotation_trigger_metrics"] = [metric]
            codes = self._violations("standard", put)
            self.assertEqual(codes, [
                "rotation_metric_not_session_window:builder:%s" % metric])

    def test_invalid_expansion(self):
        for bad in ({"step_pct": 0, "max_per_chain": 2},
                    {"step_pct": 25, "max_per_chain": True},
                    {"step_pct": 25}, None):
            def put(t, bad=bad):
                t["builder"]["expansion"] = bad
            codes = self._violations("standard", put)
            self.assertEqual(codes, ["expansion_invalid:builder"])

    def test_every_code_is_closed(self):
        def wreck(t):
            t["ghost"] = {}
            t["builder"]["review"] = 1
            t["builder"]["limits"]["x"] = {"warn": 1, "hard": 1}
            t["builder"]["limits"]["prompt_bytes"] = {"warn": 5, "hard": 1}
            t["builder"]["rotation_trigger_metrics"] = ["prompt_bytes"]
            t["builder"]["expansion"] = {}
            t.pop("planner")
        codes = self._violations("standard", wreck)
        self.assertGreaterEqual(len(codes), 6)
        self._assert_closed(codes)


class ObserveTests(unittest.TestCase):
    def test_every_metric_with_its_timing(self):
        obs = ctx.observe([_row()], {"prompt_bytes": 1, "artifact_bytes": 2,
                                     "repository_reads": 3})
        self.assertEqual(set(obs), set(ctx.METRICS))
        for metric, item in obs.items():
            self.assertEqual(item["timing"], ctx.TIMING_BY_METRIC[metric])
            self.assertIn(item["timing"], ctx.TIMING)
        self.assertEqual(obs["prompt_bytes"]["value"], 1)
        self.assertEqual(obs["artifact_bytes"]["value"], 2)
        self.assertEqual(obs["repository_reads"]["value"], 3)

    def test_tokens_come_from_the_last_row_only(self):
        rows = [_row("w2", "2000-01-02T00:00:00Z", input_tokens=900,
                     cache_tokens=40),
                _row("w1", "2000-01-01T00:00:00Z", input_tokens=100,
                     cache_tokens=5)]
        obs = ctx.observe(rows, None)
        self.assertEqual(obs["reported_input_tokens"]["value"], 900)
        self.assertEqual(obs["cache_read_tokens"]["value"], 40)

    def test_work_id_orders_rows_with_equal_start(self):
        rows = [_row("w1", input_tokens=1), _row("w2", input_tokens=2)]
        self.assertEqual(
            ctx.observe(rows, None)["reported_input_tokens"]["value"], 2)

    def test_elapsed_is_the_sum_and_one_unknown_poisons_it(self):
        rows = [_row("a", duration_ms=10), _row("b", duration_ms=32)]
        self.assertEqual(ctx.observe(rows, None)["elapsed_ms"]["value"], 42)
        rows.append(_row("c", duration_ms="unknown"))
        self.assertEqual(ctx.observe(rows, None)["elapsed_ms"]["value"],
                         ctx.UNKNOWN)

    def test_last_row_unknown_does_not_fall_back(self):
        rows = [_row("a", "2000-01-01T00:00:00Z", input_tokens=50),
                _row("b", "2000-01-02T00:00:00Z", input_tokens=None)]
        obs = ctx.observe(rows, None)
        self.assertEqual(obs["reported_input_tokens"]["value"], ctx.UNKNOWN)

    def test_incomparable_last_row_makes_tokens_unknown(self):
        obs = ctx.observe([_row(scope="incomparable")], None)
        self.assertEqual(obs["reported_input_tokens"]["value"], ctx.UNKNOWN)
        self.assertEqual(obs["cache_read_tokens"]["value"], ctx.UNKNOWN)

    def test_non_complete_and_child_rows_are_excluded(self):
        rows = [_row("a", input_tokens=7),
                _row("b", "2000-01-02T00:00:00Z", state="in_flight",
                     input_tokens=999),
                _row("c", "2000-01-03T00:00:00Z", kind="child",
                     input_tokens=888),
                _row("d", "2000-01-04T00:00:00Z", kind="child_attempt",
                     input_tokens=777)]
        obs = ctx.observe(rows, None)
        self.assertEqual(obs["reported_input_tokens"]["value"], 7)
        self.assertEqual(obs["elapsed_ms"]["value"], 10)

    def test_no_participating_row_is_unknown_not_zero(self):
        for rows in ([], None, [_row(state="blocked")]):
            obs = ctx.observe(rows, None)
            for metric in ctx.SESSION_WINDOW_METRICS:
                self.assertEqual(obs[metric]["value"], ctx.UNKNOWN)

    def test_bad_values_are_unknown_never_zero(self):
        for bad in (None, "3", True, -1, 1.5):
            row = _row(input_tokens=bad, cache_tokens=bad, duration_ms=bad)
            obs = ctx.observe([row], {"prompt_bytes": bad,
                                      "artifact_bytes": bad,
                                      "repository_reads": bad})
            for metric in ctx.METRICS:
                self.assertEqual(obs[metric]["value"], ctx.UNKNOWN, bad)
        row = _row()
        row["usage"] = "not a mapping"
        self.assertEqual(
            ctx.observe([row], None)["reported_input_tokens"]["value"],
            ctx.UNKNOWN)

    def test_zero_is_a_real_measurement(self):
        obs = ctx.observe([_row(input_tokens=0, duration_ms=0)],
                          {"prompt_bytes": 0})
        self.assertEqual(obs["reported_input_tokens"]["value"], 0)
        self.assertEqual(obs["elapsed_ms"]["value"], 0)
        self.assertEqual(obs["prompt_bytes"]["value"], 0)

    def test_missing_delivery_is_unknown(self):
        for delivery in (None, {}, "junk"):
            obs = ctx.observe([_row()], delivery)
            for metric in ctx.CURRENT_TURN_METRICS + ("repository_reads",):
                self.assertEqual(obs[metric]["value"], ctx.UNKNOWN)

    def test_inputs_are_not_mutated(self):
        rows = [_row("a"), _row("b", "2000-01-02T00:00:00Z")]
        delivery = {"prompt_bytes": 4}
        before = copy.deepcopy((rows, delivery))
        ctx.observe(rows, delivery)
        self.assertEqual((rows, delivery), before)


class EvaluateLadderTests(unittest.TestCase):
    def test_vocabularies(self):
        self.assertEqual(
            ctx.DECISION_PRECEDENCE,
            ("needs_authority", "rotate_recommended", "expand_delegated",
             "warn", "within"))
        self.assertEqual(set(ctx.DECISIONS), set(ctx.DECISION_PRECEDENCE))
        self.assertEqual(len(ctx.DECISIONS), 5)

    def test_unprofiled_is_within(self):
        for obs in (_obs(), _obs(prompt_bytes=10 ** 9)):
            self.assertEqual(
                ctx.evaluate(None, obs, 0),
                {"decision": "within", "metric": None,
                 "reason_code": "unprofiled_no_envelope"})

    def test_bad_arguments_raise(self):
        with self.assertRaises(ValueError):
            ctx.evaluate([], _obs(), 0)
        for bad in (-1, True, 1.5, None, "0"):
            with self.assertRaises(ValueError):
                ctx.evaluate(_envelope(), _obs(), bad)

    def test_unknown_values_and_none_limits_never_breach(self):
        env = _envelope(_limits(prompt_bytes=(1, 2)))
        self.assertEqual(ctx.evaluate(env, _obs(), 0)["decision"], "within")
        self.assertEqual(
            ctx.evaluate(_envelope(), _obs(prompt_bytes=10 ** 9),
                         0)["decision"], "within")
        self.assertEqual(ctx.evaluate(env, {}, 0)["decision"], "within")
        self.assertEqual(
            ctx.evaluate(env, _obs(prompt_bytes=0), 0),
            {"decision": "within", "metric": None,
             "reason_code": "within_limits"})

    def test_hard_only_limit_breaches_hard(self):
        env = _envelope(_limits(prompt_bytes=(None, 10)))
        self.assertEqual(
            ctx.evaluate(env, _obs(prompt_bytes=9), 0)["decision"],
            "within")
        self.assertEqual(
            ctx.evaluate(env, _obs(prompt_bytes=10), 0)["decision"],
            "expand_delegated")

    def test_boundary_values(self):
        env = _envelope(_limits(elapsed_ms=(5, 10)))
        self.assertEqual(
            ctx.evaluate(env, _obs(elapsed_ms=4), 0)["decision"], "within")
        self.assertEqual(
            ctx.evaluate(env, _obs(elapsed_ms=5), 0),
            {"decision": "warn", "metric": "elapsed_ms",
             "reason_code": "warn_limit_reached"})
        self.assertEqual(
            ctx.evaluate(env, _obs(elapsed_ms=9), 0)["decision"], "warn")
        self.assertEqual(
            ctx.evaluate(env, _obs(elapsed_ms=10), 0)["decision"],
            "expand_delegated")

    def test_hard_under_bound_expands_for_any_class(self):
        env = _envelope(_limits(prompt_bytes=(1, 2), elapsed_ms=(1, 2)),
                        max_per_chain=2)
        for metric in ("prompt_bytes", "elapsed_ms"):
            for used in (0, 1):
                result = ctx.evaluate(env, _obs(**{metric: 2}), used)
                self.assertEqual(result, {
                    "decision": "expand_delegated", "metric": metric,
                    "reason_code": "hard_limit_within_expansion_bound"})

    def test_hard_at_bound_depends_on_metric_class(self):
        env = _envelope(_limits(prompt_bytes=(1, 2), elapsed_ms=(1, 2)),
                        max_per_chain=2)
        self.assertEqual(
            ctx.evaluate(env, _obs(prompt_bytes=2), 2),
            {"decision": "needs_authority", "metric": "prompt_bytes",
             "reason_code": "current_turn_hard_limit_beyond_bound"})
        self.assertEqual(
            ctx.evaluate(env, _obs(elapsed_ms=2), 2),
            {"decision": "rotate_recommended", "metric": "elapsed_ms",
             "reason_code": "session_window_hard_limit_beyond_bound"})
        self.assertEqual(
            ctx.evaluate(env, _obs(elapsed_ms=2), 7)["decision"],
            "rotate_recommended")

    def test_missing_expansion_block_means_no_expansion(self):
        env = {"limits": _limits(prompt_bytes=(1, 2))}
        self.assertEqual(
            ctx.evaluate(env, _obs(prompt_bytes=2), 0)["decision"],
            "needs_authority")

    def test_precedence_over_realizable_pairs(self):
        env = _envelope(_limits(prompt_bytes=(1, 2), elapsed_ms=(1, 2),
                                repository_reads=(1, 100)),
                        max_per_chain=2)
        cases = [
            # needs_authority > rotate_recommended (both at bound)
            (dict(prompt_bytes=2, elapsed_ms=2), 2, "needs_authority",
             "prompt_bytes"),
            # needs_authority > warn
            (dict(prompt_bytes=2, repository_reads=1), 2,
             "needs_authority", "prompt_bytes"),
            # rotate_recommended > warn
            (dict(elapsed_ms=2, repository_reads=1), 2,
             "rotate_recommended", "elapsed_ms"),
            # expand_delegated > warn
            (dict(elapsed_ms=2, repository_reads=1), 0,
             "expand_delegated", "elapsed_ms"),
            # warn > within
            (dict(repository_reads=1), 0, "warn", "repository_reads"),
        ]
        for values, used, decision, metric in cases:
            result = ctx.evaluate(env, _obs(**values), used)
            self.assertEqual((result["decision"], result["metric"]),
                             (decision, metric), values)

    def test_tie_names_first_metric_in_metrics_order(self):
        env = _envelope(_limits(prompt_bytes=(1, 9), artifact_bytes=(1, 9),
                                elapsed_ms=(1, 9)))
        result = ctx.evaluate(
            env, _obs(prompt_bytes=1, artifact_bytes=1, elapsed_ms=1), 0)
        self.assertEqual(result["metric"], "prompt_bytes")
        result = ctx.evaluate(env, _obs(artifact_bytes=1, elapsed_ms=1), 0)
        self.assertEqual(result["metric"], "artifact_bytes")

    def test_result_shape_and_reason_codes(self):
        env = _envelope(_limits(prompt_bytes=(1, 2), elapsed_ms=(1, 2)))
        for values in ({}, dict(prompt_bytes=1), dict(prompt_bytes=2),
                       dict(elapsed_ms=2)):
            for used in (0, 5):
                result = ctx.evaluate(env, _obs(**values), used)
                self.assertEqual(set(result),
                                 {"decision", "metric", "reason_code"})
                self.assertIn(result["reason_code"], ctx.REASON_CODES)
                self.assertIn(result["decision"], ctx.DECISIONS)

    def test_invalid_limits_are_treated_as_absent(self):
        env = _envelope(_limits(prompt_bytes=(True, "9")))
        self.assertEqual(
            ctx.evaluate(env, _obs(prompt_bytes=10 ** 6), 0)["decision"],
            "within")


class ExpansionLedgerKindTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "ledger.jsonl")

    def _expansion(self, **overrides):
        fields = dict(role="builder", work_id="w1", chain="c1",
                      metric="elapsed_ms", prior_limit=10, new_limit=12,
                      reason_code="hard_limit_within_expansion_bound",
                      envelope_digest="d", authorized_by="lead")
        fields.update(overrides)
        return ledger.append_expansion(self.path, **fields)

    def test_mints_sequential_expansion_ids(self):
        first = self._expansion()
        second = self._expansion()
        self.assertEqual((first["id"], second["id"]), ("X-0001", "X-0002"))
        self.assertEqual(first["kind"], "expansion")
        self.assertEqual(first["state"], "open")
        self.assertIn("recorded_at", first)

    def test_none_fields_are_dropped_and_field_names_match_context(self):
        meta = {"id", "kind", "recorded_at", "state"}
        full = self._expansion()
        self.assertEqual(set(full) - meta,
                         set(ctx.EXPANSION_RECORD_FIELDS))
        bare = self._expansion(authorized_by=None)
        self.assertEqual(
            set(bare) - meta,
            set(ctx.EXPANSION_RECORD_FIELDS) - {"authorized_by"})

    def test_expansions_for_chain_filters_and_keeps_order(self):
        self._expansion(chain="c1", new_limit=12)
        self._expansion(chain="c2")
        self._expansion(role="planner", chain="c1")
        self._expansion(chain="c1", new_limit=15)
        records = ledger.read_ledger(self.path)
        got = ledger.expansions_for_chain(records, "builder", "c1")
        self.assertEqual([r["id"] for r in got], ["X-0001", "X-0004"])
        self.assertEqual([r["new_limit"] for r in got], [12, 15])
        self.assertEqual(
            [r["id"] for r in ledger.expansions_for_chain(
                records, "planner", "c1")], ["X-0003"])
        self.assertEqual(
            ledger.expansions_for_chain(records, "builder", "none"), [])

    def test_withdrawn_expansion_is_excluded(self):
        self._expansion()
        self._expansion()
        ledger.withdraw(self.path, "X-0001", reason="retracted")
        got = ledger.expansions_for_chain(
            ledger.read_ledger(self.path), "builder", "c1")
        self.assertEqual([r["id"] for r in got], ["X-0002"])

    def test_empty_records(self):
        self.assertEqual(ledger.expansions_for_chain([], "builder", "c"), [])
        self.assertEqual(ledger.expansions_for_chain(None, "builder", "c"),
                         [])

    def test_returned_records_are_copies(self):
        self._expansion()
        records = ledger.read_ledger(self.path)
        got = ledger.expansions_for_chain(records, "builder", "c1")
        got[0]["new_limit"] = -1
        again = ledger.expansions_for_chain(records, "builder", "c1")
        self.assertEqual(again[0]["new_limit"], 12)


class ClosureLedgerKindTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "ledger.jsonl")

    def test_mints_closure_with_typed_fields(self):
        record = ledger.append_closure(
            self.path, closes="F-0001", source_finding_id="F-0009",
            source_session="s1", verdict_ref="r1", round_index=2,
            phase="building")
        self.assertEqual(record["id"], "C-0001")
        self.assertEqual(record["kind"], "closure")
        self.assertEqual(record["state"], "open")
        self.assertNotEqual(record["state"], "closed")
        self.assertEqual(record["round"], 2)
        self.assertNotIn("round_index", record)
        self.assertEqual(record["closes"], "F-0001")
        self.assertEqual(record["verdict_ref"], "r1")

    def test_none_fields_are_dropped(self):
        record = ledger.append_closure(self.path, closes="F-0001")
        self.assertEqual(set(record),
                         {"id", "kind", "recorded_at", "state", "closes"})

    def test_a_closure_leaves_the_finding_record_unchanged(self):
        finding = ledger.append_finding(self.path, summary="s",
                                        severity="major")
        before = ledger.collapse(ledger.read_ledger(self.path))[
            finding["id"]]
        ledger.append_closure(self.path, closes=finding["id"])
        after = ledger.collapse(ledger.read_ledger(self.path))[
            finding["id"]]
        self.assertEqual(before, after)
        self.assertEqual(after["state"], "open")


class LedgerBackCompatTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.plain = os.path.join(self._dir.name, "plain.jsonl")
        self.mixed = os.path.join(self._dir.name, "mixed.jsonl")

    def _fill(self, path, with_new_kinds):
        ledger.append_finding(path, summary="a", severity="major",
                              disposition="confirmed")
        if with_new_kinds:
            ledger.append_expansion(path, role="builder", chain="c")
        ledger.append_finding(path, summary="b", severity="minor")
        if with_new_kinds:
            ledger.append_closure(path, closes="F-0001")
        ledger.append_decision(path, summary="d")
        ledger.append_escape(path, summary="e", severity="major")
        ledger.append_finding(path, summary="c", severity="major")
        ledger.withdraw(path, "F-0003", reason="retracted")

    def test_finding_lifecycle_ignores_new_kinds(self):
        self._fill(self.plain, False)
        self._fill(self.mixed, True)
        self.assertEqual(
            measure.finding_lifecycle(ledger.read_ledger(self.mixed)),
            measure.finding_lifecycle(ledger.read_ledger(self.plain)))

    def test_collapse_adds_new_ids_and_keeps_old_ones(self):
        self._fill(self.plain, False)
        self._fill(self.mixed, True)
        plain = ledger.collapse(ledger.read_ledger(self.plain))
        mixed = ledger.collapse(ledger.read_ledger(self.mixed))
        self.assertIn("X-0001", mixed)
        self.assertIn("C-0001", mixed)
        for rid, record in plain.items():
            # Each ledger stamped its own recorded_at; everything else must
            # match.
            self.assertEqual(
                {k: v for k, v in mixed[rid].items() if k != "recorded_at"},
                {k: v for k, v in record.items() if k != "recorded_at"})

    def test_withdraw_and_citations_treat_new_ids_like_any_id(self):
        self._fill(self.mixed, True)
        marker = ledger.withdraw(self.mixed, "X-0001")
        self.assertEqual(marker["kind"], "expansion")
        marker = ledger.withdraw(self.mixed, "C-0001")
        self.assertEqual(marker["kind"], "closure")
        result = ledger.validate_citations(
            ledger.read_ledger(self.mixed),
            ["X-0001", "C-0001", "F-0001", "X-0099"])
        self.assertEqual(result["withdrawn"], ["X-0001", "C-0001"])
        self.assertEqual(result["valid"], ["F-0001"])
        self.assertEqual(result["invented"], ["X-0099"])
        allowed = ledger.validate_citations(
            ledger.read_ledger(self.mixed), ["X-0001", "C-0001"],
            allow_withdrawn=True)
        self.assertEqual(allowed["valid"], ["X-0001", "C-0001"])

    def test_next_id_per_prefix_is_independent(self):
        self._fill(self.plain, False)
        self._fill(self.mixed, True)
        plain = ledger.read_ledger(self.plain)
        mixed = ledger.read_ledger(self.mixed)
        for kind in ("finding", "decision", "amendment", "escape",
                     "attempt"):
            self.assertEqual(ledger.next_id(plain, kind),
                             ledger.next_id(mixed, kind))
        self.assertEqual(ledger.next_id(mixed, "expansion"), "X-0002")
        self.assertEqual(ledger.next_id(mixed, "closure"), "C-0002")

    def test_prefixes_are_unique_and_existing_kinds_keep_theirs(self):
        self.assertEqual(len(set(ledger.KINDS.values())),
                         len(ledger.KINDS))
        for kind, prefix in (("finding", "F"), ("decision", "D"),
                             ("amendment", "A"), ("escape", "E"),
                             ("attempt", "V")):
            self.assertEqual(ledger.KINDS[kind], prefix)


class ModuleBoundaryTests(unittest.TestCase):
    ALLOWED_IMPORTS = {"__future__", "typing", "collections.abc"}
    FORBIDDEN_CALLS = {"open", "exec", "eval", "__import__", "compile"}
    FORBIDDEN_NAMES = {"os", "sys", "subprocess", "socket", "pathlib",
                       "shutil", "tempfile", "urllib", "http"}

    def _tree(self, name):
        with open(os.path.join(_HERE, name), "r", encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def _imported(self, tree):
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        return names

    def test_context_module_imports_only_allowlisted_stdlib(self):
        for name in self._imported(self._tree("cowork_context.py")):
            self.assertIn(name, self.ALLOWED_IMPORTS)
            self.assertFalse(name.startswith("cowork_"))

    def test_context_module_does_no_io_or_dynamic_execution(self):
        tree = self._tree("cowork_context.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Name):
                self.assertNotIn(node.func.id, self.FORBIDDEN_CALLS)
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, self.FORBIDDEN_NAMES)
            if isinstance(node, ast.Attribute) and isinstance(
                    node.value, ast.Name):
                self.assertNotIn(node.value.id, self.FORBIDDEN_NAMES)

    def test_context_module_body_is_declarations_only(self):
        tree = self._tree("cowork_context.py")
        for index, node in enumerate(tree.body):
            if index == 0:
                self.assertIsInstance(node, ast.Expr)  # the docstring
                continue
            self.assertIsInstance(
                node, (ast.Import, ast.ImportFrom, ast.Assign,
                       ast.FunctionDef), ast.dump(node))
            if isinstance(node, ast.Assign):
                for sub in ast.walk(node.value):
                    self.assertNotIsInstance(sub, ast.Call, ast.dump(node))

    def test_no_import_cycle_with_the_policy_modules(self):
        for name in ("cowork_execution_profiles.py", "cowork_ledger.py"):
            self.assertNotIn("cowork_context",
                             self._imported(self._tree(name)), name)


if __name__ == "__main__":
    unittest.main()
