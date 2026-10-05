#!/usr/bin/env python3
"""Tests for the universal context views in `cowork_measure` and their
rendering in `cowork_report`.

Every session gets the same additive keys (`context`, `profile_attribution`,
`repeated_context`, `cost_split`, `recovery`, `lineage` and
`owned_verification.bound_reuse`), profiled or not. These tests build small
synthetic sessions under a temporary COWORK_SESSIONS_ROOT, and call the pure
view functions directly where the input is easier to state that way.

Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_measurement
"""

import ast
import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_context as context_vocab  # noqa: E402
import cowork_execution_profiles as exec_profiles  # noqa: E402
import cowork_ingest as ingest  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_lineage as lineage_store  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_recovery_evidence as recovery_evidence  # noqa: E402
import cowork_report as report  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402

UNKNOWN = "unknown"

# The keys the universal views add. Stripping exactly these from a record must
# leave the record a session had before the views existed.
NEW_TOP = frozenset(("context", "profile_attribution", "repeated_context",
                     "cost_split", "recovery", "lineage"))
NEW_OWNED = frozenset(("bound_reuse",))
NEW_BUILT_FROM = frozenset(("recovery_episodes", "lineage"))

LEGACY_TOP = frozenset((
    "schema_version", "session", "built_at", "built_from", "work",
    "orphan_ends", "cost", "nested", "contribution", "guard_decisions",
    "duration", "input_sources", "verification_attempts", "verification",
    "verification_summary", "owned_verification", "tool_activity",
    "environment_recurrences", "findings", "marginal_cost", "calibration",
    "ledger", "score_cohorts", "enhancements", "ingestion", "identities",
    "pricing", "evaluation_queue", "completion", "milestones", "readiness",
    "trace_summary", "scores_summary", "incomplete", "replay", "activity"))
LEGACY_OWNED = frozenset((
    "transactions", "transaction_count", "latest", "cost",
    "focused_attribution", "incurred_cost", "accepted_cost", "avoided_cost"))
LEGACY_BUILT_FROM = frozenset((
    "trace", "scores", "identities", "ledger", "evaluation_queue", "children",
    "actions", "orchestrator_evaluations"))

NEW_SECTIONS = ("_section_context", "_section_profile_attribution",
                "_section_repeated_context", "_section_cost_split",
                "_section_recovery", "_section_lineage",
                "_section_bound_reuse")
NEW_HEADINGS = ("Context envelope and consumption", "Profile attribution",
                "Repeated context", "Cost split", "Recovery episodes",
                "Lineage", "Bound verification reuse")
NEW_FIGURE_PREFIXES = ("context.", "profile_attribution.",
                       "repeated_context.", "cost_split.", "recovery.",
                       "lineage.", "owned.bound_reuse")
NEW_VIEW_FIELD_PREFIXES = ("record.context", "record.profile_attribution",
                           "record.repeated_context", "record.cost_split",
                           "record.recovery", "record.lineage")

C5_RECORD = os.path.join(_HERE, "fixtures", "measurement",
                         "c5-provenance-replay", "measurement.json")


# --------------------------------------------------------------------------- #
# Synthetic trace builders.                                                   #
# --------------------------------------------------------------------------- #


def _ts(second):
    return "2026-03-01T10:%02d:%02dZ" % (second // 60, second % 60)


def _sha(char):
    return char * 64


def _artifact(path, char, size=1000):
    return {"path": path, "bytes": size, "sha256": _sha(char),
            "delivery": "path", "embedded_bytes": 0}


def _start(work_id, role, second, phase="building", round_=1,
           prompt_bytes=100, artifacts=None, work_class=None):
    event = {"event": "controller.turn.start", "work_id": work_id,
             "role": role, "controller": "claude", "phase": phase,
             "round": round_, "prompt_bytes": prompt_bytes,
             "ts": _ts(second)}
    if artifacts is not None:
        event["artifacts"] = artifacts
    if work_class:
        event["work_class"] = work_class
    return event


def _end(work_id, role, second, usage=None, duration_ms=1000,
         work_class=None, usage_scope="turn_native"):
    event = {"event": "controller.turn.end", "work_id": work_id,
             "role": role, "controller": "claude", "ts": _ts(second),
             "result": "ok", "duration_ms": duration_ms,
             "usage_scope": usage_scope}
    if usage is not None:
        event["usage"] = usage
    if work_class:
        event["work_class"] = work_class
    return event


def _turn(work_id, role, second, phase="building", round_=1,
          prompt_bytes=100, artifacts=None, work_class=None, usage=None,
          duration_ms=30000, usage_scope="turn_native"):
    return [
        _start(work_id, role, second, phase=phase, round_=round_,
               prompt_bytes=prompt_bytes, artifacts=artifacts,
               work_class=work_class),
        _end(work_id, role, second + duration_ms // 1000, usage=usage,
             duration_ms=duration_ms, work_class=work_class,
             usage_scope=usage_scope),
    ]


def _usage(input_tokens, output_tokens):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _baseline_events(recovery=False):
    """Four turns of one session. The last builder turn re-delivers the plan
    unchanged; `recovery` tags it as a recovery turn."""
    work_class = "recovery" if recovery else None
    events = []
    events += _turn("W1", "scout", 0, phase="scouting", round_=1,
                    usage=_usage(1000, 100), duration_ms=30000,
                    artifacts=[_artifact("/a/intel.json", "a")])
    events += _turn("W2", "builder", 100, round_=1,
                    usage=_usage(2000, 200), duration_ms=60000,
                    artifacts=[_artifact("/a/plan.json", "b", 700)])
    events += _turn("W3", "build-reviewer", 170, round_=1,
                    usage=_usage(500, 50), duration_ms=30000,
                    artifacts=[_artifact("/a/status.json", "c")])
    events += _turn("W4", "builder", 210, round_=2,
                    usage=_usage(1500, 150), duration_ms=40000,
                    artifacts=[_artifact("/a/plan.json", "b", 700)],
                    work_class=work_class)
    return events


def _snapshot(*roots):
    """Path -> sha256 for every file under (or at) each root."""
    def digest(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    out = {}
    for root in roots:
        if os.path.isfile(root):
            out[root] = digest(root)
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                out[path] = digest(path)
    return out


def _diff(left, right, path=""):
    """Dotted paths at which two JSON-like values differ."""
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right), key=str):
            child = "%s.%s" % (path, key) if path else str(key)
            if key not in left or key not in right:
                out.append(child)
            else:
                out.extend(_diff(left[key], right[key], child))
        return out
    if (isinstance(left, list) and isinstance(right, list)
            and len(left) == len(right)):
        out = []
        for index, (a, b) in enumerate(zip(left, right)):
            out.extend(_diff(a, b, "%s[%d]" % (path, index)))
        return out
    return [] if left == right else [path]


def _strip(record):
    """A copy of `record` without exactly the keys the views add."""
    out = copy.deepcopy(record)
    for key in NEW_TOP:
        out.pop(key, None)
    out["owned_verification"].pop("bound_reuse", None)
    for key in NEW_BUILT_FROM:
        out["built_from"].pop(key, None)
    return out


def _collect(node, key, found=None):
    """Every value stored under a key named `key`, anywhere in `node`."""
    found = [] if found is None else found
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            _collect(value, key, found)
    elif isinstance(node, list):
        for item in node:
            _collect(item, key, found)
    return found


def _profile_doc(history, effective="standard"):
    return {"selected": "light", "effective": effective, "policy_version": 1,
            "counters": {"verification_executed": 0,
                         "verification_reused": 0},
            "promotion_history": history, "deferred_minor_notes": [],
            "batch": {"artifacts": []}}


def _promotion(seq, source, target, second):
    return {"seq": seq, "from": source, "to": target,
            "reason_codes": ["scope_grew"], "seam": "ready_for_review",
            "at": _ts(second)}


def _entry(role="builder", phase="building", round_=1,
           work_class="productive", usage=None, duration_ms=1000, **extra):
    entry = {"role": role, "phase": phase, "round": round_,
             "work_class": work_class, "duration_ms": duration_ms,
             "work_state": "complete", "usage_scope": "turn_native"}
    if usage is not None:
        entry["usage"] = usage
    entry.update(extra)
    return entry


class _SessionTestCase(unittest.TestCase):
    """A private session root per test, plus writers for synthetic sources."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._root, True)
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = os.path.join(self._root,
                                                          "sessions")
        self.addCleanup(self._restore_root, prior)

    @staticmethod
    def _restore_root(prior):
        if prior is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = prior

    def _assets(self, session):
        path = state_store.session_assets_dir(session)
        os.makedirs(path, exist_ok=True)
        return path

    def _write_trace(self, session, events):
        self._assets(session)
        with open(trace_store.trace_path_for(session), "w") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True) + "\n")

    def _write_json(self, session, name, data):
        path = os.path.join(self._assets(session), name)
        with open(path, "w") as fh:
            json.dump(data, fh, sort_keys=True)
        return path

    def _write_text(self, session, name, text):
        path = os.path.join(self._assets(session), name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def _write_transaction(self, session, transaction_id, wall_time_s):
        directory = os.path.join(state_store.verification_root_for(session),
                                 "transactions", transaction_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "result.json"), "w") as fh:
            json.dump({"transaction_id": transaction_id, "verdict": "green",
                       "finished_at": _ts(5),
                       "attempts": [{"label": "unit", "kind": "baseline",
                                     "wall_time_s": wall_time_s,
                                     "exit_code": 0}]}, fh)

    def _build(self, session, ingest_results=None):
        return measure.build_record(
            session, ingest_results={} if ingest_results is None
            else ingest_results)

    def _built(self, session, events, ingest_results=None):
        self._write_trace(session, events)
        return self._build(session, ingest_results)


# --------------------------------------------------------------------------- #
# Additivity and report purity.                                               #
# --------------------------------------------------------------------------- #


class UniversalAdditivityTests(_SessionTestCase):

    def test_record_minus_new_keys_is_the_legacy_shape(self):
        record = self._built("S1", _baseline_events())
        self.assertTrue(NEW_TOP <= set(record))
        self.assertTrue(NEW_OWNED <= set(record["owned_verification"]))
        stripped = _strip(record)
        self.assertEqual(set(stripped), LEGACY_TOP)
        self.assertEqual(set(stripped["owned_verification"]), LEGACY_OWNED)
        self.assertEqual(set(stripped["built_from"]), LEGACY_BUILT_FROM)
        self.assertNotIn("execution_profile", record)

    def test_views_never_append_to_the_legacy_incomplete_list(self):
        record = self._built("S1", _baseline_events())
        fields = [item.get("field") or "" for item in record["incomplete"]]
        for field in fields:
            self.assertFalse(field.startswith(NEW_VIEW_FIELD_PREFIXES),
                             field)

    def test_legacy_values_equal_independent_recomputation(self):
        events = _baseline_events()
        record = self._built("S1", events)
        work, _orphans = measure.build_work(events)
        records = ledger.read_ledger(state_store.ledger_path_for("S1"))
        self.assertEqual(record["work"], work)
        self.assertEqual(record["cost"], measure.classify_costs(work))
        self.assertEqual(record["replay"], measure.replay_rounds(record))
        self.assertEqual(
            record["duration"]["by_class"],
            measure.duration_by_class(work, 0, user_wait_spans=[]))
        self.assertEqual(record["findings"],
                         measure.finding_lifecycle(records))
        self.assertEqual(record["marginal_cost"],
                         measure.marginal_cost_per_finding(work, records))
        self.assertEqual(record["ledger"], ledger.collapse(records))
        # Literal anchors, so a derivation that moved would show here too.
        self.assertEqual(record["cost"]["by_class"]["productive"]["turns"], 4)
        self.assertEqual(record["cost"]["by_class"]["recovery"]["turns"], 0)

    def test_work_entries_do_not_carry_the_new_fields(self):
        record = self._built("S1", _baseline_events())
        for entry in record["work"].values():
            for key in ("repeated_context", "profile", "limits", "metrics"):
                self.assertNotIn(key, entry)

    def test_recovery_edge_turn_differs_only_in_the_allowed_fields(self):
        self._write_trace("S1", _baseline_events(recovery=False))
        baseline = copy.deepcopy(self._build("S1"))
        self._write_trace("S1", _baseline_events(recovery=True))
        tagged = copy.deepcopy(self._build("S1"))
        differences = _diff(_strip(baseline), _strip(tagged))
        allowed = ("work.W4.work_class", "cost.by_class.", "replay",
                   "duration.by_class.", "built_from.trace", "built_at")
        unexpected = [path for path in differences
                      if not path.startswith(allowed)]
        self.assertEqual(unexpected, [])
        self.assertIn("work.W4.work_class", differences)
        self.assertEqual(baseline["incomplete"], tagged["incomplete"])
        self.assertEqual(tagged["work"]["W4"]["work_class"], "recovery")
        self.assertEqual(tagged["cost"]["by_class"]["recovery"]["turns"], 1)

    def test_unprofiled_session_has_no_profile_or_conditional_stamps(self):
        record = self._built("S1", _baseline_events())
        self.assertNotIn("execution_profile", record)
        for key in NEW_BUILT_FROM:
            self.assertNotIn(key, record["built_from"])

    def test_sources_add_their_stamps_without_touching_legacy_incomplete(self):
        events = _baseline_events()
        without = self._built("S1", events)
        episode = recovery_evidence.build_episode(
            "builder", "W2", "W4", "unchanged_replay",
            before_artifacts=[{"path": "/a/p", "sha256": _sha("a")}],
            after_artifacts=[{"path": "/a/p", "sha256": _sha("a")}],
            before_records=[], after_records=[])
        recovery_evidence.append_episode(
            os.path.join(self._assets("S1"), "recovery"), episode)
        self._write_json("S1", "lineage.json", lineage_store.new_lineage_record(
            source_session="SRC", replacement_session="S1", reason="redo",
            start_role="builder"))
        with_sources = self._build("S1")
        self.assertIn("recovery_episodes", with_sources["built_from"])
        self.assertIn("lineage", with_sources["built_from"])
        self.assertEqual(without["incomplete"], with_sources["incomplete"])

    def test_provenance_checks_the_new_stamps_only_when_stamped(self):
        events = _baseline_events()
        plain = self._built("S1", events)
        episodes = os.path.join(self._assets("S1"), "recovery")
        episode = recovery_evidence.build_episode(
            "builder", "W2", "W4", "unchanged_replay")
        recovery_evidence.append_episode(episodes, episode)
        self.assertEqual(measure.check_provenance("S1", plain)["state"],
                         "fresh")
        stamped = self._build("S1")
        self.assertEqual(measure.check_provenance("S1", stamped)["state"],
                         "fresh")
        second = recovery_evidence.build_episode(
            "builder", "W1", "W3", "schema_repair")
        recovery_evidence.append_episode(episodes, second)
        result = measure.check_provenance("S1", stamped)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["diverged"], ["recovery_episodes"])
        self._write_json("S1", "lineage.json", lineage_store.new_lineage_record(
            source_session="SRC", replacement_session="S1", reason="redo",
            start_role="builder"))
        lineaged = self._build("S1")
        self.assertEqual(measure.check_provenance("S1", lineaged)["state"],
                         "fresh")
        self._write_json("S1", "lineage.json", lineage_store.new_lineage_record(
            source_session="OTHER", replacement_session="S1", reason="redo",
            start_role="builder"))
        self.assertEqual(measure.check_provenance("S1", lineaged)["diverged"],
                         ["lineage"])

    def test_new_sections_are_the_last_calls_of_render_report(self):
        source = inspect.getsource(report.render_report)
        calls = re.findall(r"lines\.extend\((_section_\w+)\(record\)\)",
                           source)
        self.assertEqual(tuple(calls[-len(NEW_SECTIONS):]), NEW_SECTIONS)
        self.assertEqual(len(set(calls)), len(calls))

    def _legacy_shaped_records(self):
        stripped = _strip(self._built("S1", _baseline_events()))
        with open(C5_RECORD) as fh:
            stored = json.load(fh)
        return [stripped, stored,
                {"schema_version": 1, "built_at": "t", "session": "S"}]

    def test_legacy_shaped_records_render_no_new_section(self):
        for record in self._legacy_shaped_records():
            for name in NEW_SECTIONS:
                self.assertEqual(getattr(report, name)(record), [], name)
            lines = report.render_report(record).splitlines()
            for heading in NEW_HEADINGS:
                self.assertNotIn(heading, lines)

    def test_full_render_is_the_legacy_render_plus_the_new_blocks(self):
        full = self._built("S1", _baseline_events())
        stripped = _strip(full)
        blocks = []
        for name in NEW_SECTIONS:
            blocks.extend(getattr(report, name)(full))
        self.assertTrue(blocks)
        self.assertEqual(
            report.render_report(full),
            report.render_report(stripped) + "\n".join(blocks) + "\n")
        lines = report.render_report(full).splitlines()
        positions = [lines.index(heading) for heading in NEW_HEADINGS]
        self.assertEqual(positions, sorted(positions))

    def test_new_sections_compute_nothing(self):
        tree = ast.parse(inspect.getsource(report))
        checked = 0
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) \
                    or node.name not in NEW_SECTIONS:
                continue
            checked += 1
            for child in ast.walk(node):
                if isinstance(child, ast.Call) \
                        and isinstance(child.func, ast.Name):
                    self.assertNotIn(child.func.id, ("len", "sum"),
                                     node.name)
        self.assertEqual(checked, len(NEW_SECTIONS))

    def test_every_new_printed_figure_resolves_in_a_built_record(self):
        record = self._built("S1", _baseline_events())
        lineage = report.rendered_lineage(record)
        fresh = [figure for figure in lineage
                 if figure.startswith(NEW_FIGURE_PREFIXES)]
        self.assertTrue(fresh)
        unresolved = [figure for figure in fresh
                      if not lineage[figure]["resolved"]]
        self.assertEqual(unresolved, [])

    def test_each_view_shares_its_keys_with_the_unknown_fallback(self):
        record = self._built("S1", _baseline_events())
        shapes = measure._unknown_views()
        for key, shape in shapes.items():
            self.assertEqual(set(record[key]), set(shape), key)


# --------------------------------------------------------------------------- #
# Envelope and consumption.                                                   #
# --------------------------------------------------------------------------- #


class ContextEnvelopeViewTests(_SessionTestCase):

    def _rows(self, record):
        return {row["work_id"]: row
                for row in record["context"]["dispatches"]}

    def test_unprofiled_rows_have_no_limits(self):
        record = self._built("S1", _baseline_events())
        view = record["context"]
        self.assertEqual(view["limits_basis"], "unprofiled")
        self.assertEqual(view["dispatch_count"], 4)
        for row in view["dispatches"]:
            self.assertIsNone(row["limits"])
            self.assertEqual(row["profile"], "unprofiled")
        self.assertEqual([row["work_id"] for row in view["dispatches"]],
                         ["W1", "W2", "W3", "W4"])

    def test_metrics_come_from_the_recorded_turn(self):
        rows = self._rows(self._built("S1", _baseline_events()))
        metrics = rows["W2"]["metrics"]
        self.assertEqual(set(metrics), set(context_vocab.METRICS))
        self.assertEqual(metrics["prompt_bytes"]["value"], 100)
        self.assertEqual(metrics["artifact_bytes"]["value"], 700)
        self.assertEqual(metrics["elapsed_ms"]["value"], 60000)
        self.assertEqual(metrics["reported_input_tokens"]["value"], 2000)
        self.assertEqual(metrics["cache_read_tokens"]["value"], UNKNOWN)
        self.assertEqual(metrics["repository_reads"]["value"], UNKNOWN)

    def test_a_missing_artifact_list_is_unknown_not_zero(self):
        events = _turn("W1", "builder", 0, usage=_usage(10, 1))
        rows = self._rows(self._built("S1", events))
        self.assertEqual(rows["W1"]["metrics"]["artifact_bytes"]["value"],
                         UNKNOWN)

    def test_incomparable_usage_is_unknown_and_noted_only_in_the_view(self):
        events = _turn("W1", "builder", 0, usage=_usage(10, 1))
        events += _turn("W2", "builder", 100, usage=_usage(20, 2),
                        usage_scope="incomparable")
        record = self._built("S1", events)
        rows = self._rows(record)
        self.assertEqual(
            rows["W2"]["metrics"]["reported_input_tokens"]["value"], UNKNOWN)
        self.assertEqual(
            rows["W1"]["metrics"]["reported_input_tokens"]["value"], 10)
        fields = [note["field"] for note in record["context"]["incomplete"]]
        self.assertIn("record.context.by_role[builder].reported_input_tokens",
                      fields)
        for item in record["incomplete"]:
            self.assertFalse(
                (item.get("field") or "").startswith("record.context"))

    def test_ingestion_that_is_not_ok_leaves_repository_reads_unknown(self):
        record = self._built("S1", _baseline_events(),
                             {"builder": ingest.Result("missing")})
        rows = self._rows(record)
        self.assertEqual(rows["W2"]["metrics"]["repository_reads"]["value"],
                         UNKNOWN)

    def test_ok_ingestion_counts_reads_and_searches_per_turn(self):
        calls = [
            {"intent": "read", "started_at": _ts(110),
             "command_identity": "cat a"},
            {"intent": "search", "started_at": _ts(120),
             "command_identity": "grep x"},
            {"intent": "mutate", "started_at": _ts(130),
             "command_identity": "touch f"},
        ]
        record = self._built("S1", _baseline_events(), {
            "builder": ingest.Result("ok", tool_activity=calls)})
        rows = self._rows(record)
        self.assertEqual(rows["W2"]["metrics"]["repository_reads"]["value"],
                         2)
        # Ingested and quiet is a known zero; an uningested role stays unknown.
        self.assertEqual(rows["W4"]["metrics"]["repository_reads"]["value"],
                         0)
        self.assertEqual(rows["W3"]["metrics"]["repository_reads"]["value"],
                         UNKNOWN)

    def test_by_role_reports_the_largest_known_value(self):
        record = self._built("S1", _baseline_events())
        builder = record["context"]["by_role"]["builder"]
        self.assertEqual(builder["dispatches"], 2)
        self.assertEqual(builder["max"]["reported_input_tokens"], 2000)
        self.assertEqual(builder["max"]["repository_reads"], UNKNOWN)
        self.assertEqual(builder["unknown_count"]["repository_reads"], 2)

    def test_profile_in_force_follows_the_promotion_boundary(self):
        events = _turn("W1", "builder", 100, usage=_usage(10, 1))
        events += _turn("W3", "builder", 200, usage=_usage(30, 3))
        events += _turn("W2", "builder", 300, usage=_usage(20, 2))
        self._write_json("S1", "execution_profile.json", _profile_doc(
            [_promotion(1, "light", "standard", 200)]))
        record = self._built("S1", events)
        rows = self._rows(record)
        self.assertEqual(record["context"]["limits_basis"], "profiled")
        self.assertEqual(rows["W1"]["profile"], "light")
        self.assertEqual(rows["W2"]["profile"], "standard")
        expected = exec_profiles.resolved_context_envelope(
            "light", "builder")["limits"]
        self.assertEqual(rows["W1"]["limits"], expected)
        self.assertEqual(
            rows["W2"]["limits"],
            exec_profiles.resolved_context_envelope(
                "standard", "builder")["limits"])
        # A turn exactly on the boundary is never guessed.
        self.assertEqual(rows["W3"]["profile"], UNKNOWN)
        self.assertEqual(rows["W3"]["limits"], UNKNOWN)

    def test_profile_attribution_splits_totals_at_the_boundary(self):
        events = _turn("W1", "builder", 100, usage=_usage(10, 1))
        events += _turn("W3", "builder", 200, usage=_usage(30, 3))
        events += _turn("W2", "builder", 300, usage=_usage(20, 2))
        self._write_json("S1", "execution_profile.json", _profile_doc(
            [_promotion(1, "light", "standard", 200)]))
        view = self._built("S1", events)["profile_attribution"]
        self.assertEqual(view["state"], "profiled")
        self.assertEqual(view["initial"], "light")
        self.assertEqual(view["by_profile"]["light"]["turns"], 1)
        self.assertEqual(view["by_profile"]["light"]["usage"],
                         _usage(10, 1))
        self.assertEqual(view["by_profile"]["standard"]["usage"],
                         _usage(20, 2))
        self.assertEqual(view["by_profile"][UNKNOWN]["turns"], 1)
        self.assertEqual(view["unknown_turns"], 1)
        self.assertTrue(view["incomplete"])

    def test_unprofiled_attribution_uses_one_label(self):
        view = self._built("S1", _baseline_events())["profile_attribution"]
        self.assertEqual(view["state"], "unprofiled")
        self.assertEqual(list(view["by_profile"]), ["unprofiled"])
        self.assertEqual(view["by_profile"]["unprofiled"]["turns"], 4)
        self.assertEqual(view["unknown_turns"], 0)

    def test_an_unparsable_boundary_makes_every_turn_unknown(self):
        bad = _promotion(1, "light", "standard", 200)
        bad["at"] = "not a time"
        self._write_json("S1", "execution_profile.json", _profile_doc([bad]))
        record = self._built("S1", _turn("W1", "builder", 100,
                                         usage=_usage(10, 1)))
        row = self._rows(record)["W1"]
        self.assertEqual(row["profile"], UNKNOWN)
        self.assertEqual(row["limits"], UNKNOWN)
        self.assertEqual(record["context"]["limits_basis"], UNKNOWN)
        self.assertEqual(record["profile_attribution"]["state"], UNKNOWN)

    def test_a_torn_profile_record_is_unknown(self):
        self._write_text("S1", "execution_profile.json", "{not json")
        record = self._built("S1", _turn("W1", "builder", 100,
                                         usage=_usage(10, 1)))
        self.assertEqual(self._rows(record)["W1"]["limits"], UNKNOWN)
        self.assertEqual(record["context"]["limits_basis"], UNKNOWN)

    def test_an_unknown_profile_name_gives_unknown_limits(self):
        self._write_json("S1", "execution_profile.json", _profile_doc(
            [_promotion(1, "mystery", "mystery", 50)]))
        record = self._built("S1", _turn("W1", "builder", 100,
                                         usage=_usage(10, 1)))
        row = self._rows(record)["W1"]
        self.assertEqual(row["profile"], "mystery")
        self.assertEqual(row["limits"], UNKNOWN)

    def test_a_role_off_the_profile_team_has_no_limits(self):
        self._write_json("S1", "execution_profile.json",
                         _profile_doc([], effective="light"))
        record = self._built("S1", _turn(
            "W1", "planner", 100, phase="planning", usage=_usage(10, 1)))
        row = self._rows(record)["W1"]
        self.assertEqual(row["profile"], "light")
        self.assertIsNone(row["limits"])

    def test_section_prints_none_and_unknown_never_zero(self):
        record = self._built("S1", _baseline_events())
        text = "\n".join(report._section_context(record))
        self.assertIn("Context envelope and consumption", text)
        self.assertIn("limits: none (unprofiled)", text)
        self.assertIn("max=unknown", text)
        self.assertNotIn("max=0", text)
        profiled = copy.deepcopy(record)
        profiled["context"]["limits_basis"] = UNKNOWN
        self.assertIn("limits: unknown",
                      "\n".join(report._section_context(profiled)))

    def test_attribution_section_lists_each_label(self):
        record = self._built("S1", _baseline_events())
        text = "\n".join(report._section_profile_attribution(record))
        self.assertIn("Profile attribution", text)
        self.assertIn("unprofiled", text)
        self.assertIn("turns=4", text)


# --------------------------------------------------------------------------- #
# Each work id, eval turn and child counted once; bound reuse is not executed.  #
# --------------------------------------------------------------------------- #


class MeasurementDedupeTests(_SessionTestCase):

    def test_a_work_id_recorded_twice_is_one_row_and_one_cost(self):
        once = self._built("S1", _baseline_events())
        events = _baseline_events()
        duplicated = []
        for event in events:
            duplicated.append(event)
            if event["work_id"] == "W2":
                duplicated.append(dict(event))
        twice = self._built("S2", duplicated)
        ids = [row["work_id"] for row in twice["context"]["dispatches"]]
        self.assertEqual(ids.count("W2"), 1)
        self.assertEqual(twice["context"]["dispatch_count"],
                         once["context"]["dispatch_count"])
        self.assertEqual(twice["cost_split"]["buckets"],
                         once["cost_split"]["buckets"])
        self.assertEqual(twice["repeated_context"]["deliveries"],
                         once["repeated_context"]["deliveries"])

    def test_a_shared_evaluation_turn_is_one_row(self):
        events = _baseline_events()
        for _ in range(3):
            events.append({"event": "eval.turn.start", "work_id": "E1",
                           "role": "evaluator", "controller": "claude",
                           "work_class": "evaluation", "ts": _ts(300)})
            events.append({"event": "eval.turn.end", "work_id": "E1",
                           "role": "evaluator", "controller": "claude",
                           "work_class": "evaluation", "ts": _ts(310),
                           "duration_ms": 10000, "result": "ok",
                           "usage": _usage(5, 1),
                           "usage_scope": "turn_native"})
        record = self._built("S1", events)
        ids = [row["work_id"] for row in record["context"]["dispatches"]]
        self.assertEqual(ids.count("E1"), 1)
        self.assertEqual(record["cost_split"]["unmapped_classes"],
                         {"evaluation": 1})
        self.assertEqual(record["cost_split"]["buckets"]["unknown"]["turns"],
                         1)

    def test_nested_child_usage_is_not_added_to_its_parent(self):
        plain = self._built("S1", _baseline_events())
        events = _baseline_events()
        self._write_trace("S2", events)
        child = [
            {"guard_attempt_id": "c-start", "work_id": "C1",
             "parent_work_id": "W2", "state": "started", "ts": _ts(110)},
            {"guard_attempt_id": "c-end", "work_id": "C1", "state": "ended",
             "ts": _ts(120), "duration_ms": 10000,
             "usage": {"input_tokens": 999, "output_tokens": 99},
             "usage_scope": "child_native_sum",
             "terminal_source": "agent_tool_result"},
        ]
        with open(state_store.children_path_for("S2"), "w") as fh:
            for row in child:
                fh.write(json.dumps(row) + "\n")
        record = self._build("S2")
        self.assertEqual(record["work"]["C1"]["work_kind"], "child")
        ids = [row["work_id"] for row in record["context"]["dispatches"]]
        self.assertNotIn("C1", ids)
        self.assertEqual(record["cost_split"]["buckets"],
                         plain["cost_split"]["buckets"])
        self.assertEqual(record["profile_attribution"]["by_profile"],
                         plain["profile_attribution"]["by_profile"])
        self.assertEqual(
            record["context"]["dispatches"][1]["metrics"],
            plain["context"]["dispatches"][1]["metrics"])

    def _owned_session(self, extra_events=()):
        self._write_transaction("S1", "T1", 12.5)
        events = _baseline_events() + list(extra_events)
        return self._built("S1", events)

    def test_no_bound_reuse_is_a_known_zero(self):
        owned = self._owned_session()["owned_verification"]
        self.assertEqual(owned["transaction_count"], 1)
        self.assertEqual(owned["bound_reuse"], {
            "count": 0, "by_transaction": [],
            "avoided_subprocess_wall_time_s": 0.0})

    def test_a_bound_reuse_is_never_counted_as_executed(self):
        before = self._owned_session()["owned_verification"]
        event = {"event": "verification.transaction", "ts": _ts(400),
                 "transaction_id": "T2", "bound_reuse": True,
                 "bound_prior_transaction_id": "T1"}
        owned = self._owned_session([event])["owned_verification"]
        self.assertEqual(owned["bound_reuse"], {
            "count": 1,
            "by_transaction": [{"transaction_id": "T1", "bound_count": 1,
                                "subprocess_wall_time_s": 12.5}],
            "avoided_subprocess_wall_time_s": 12.5})
        for key in ("transaction_count", "incurred_cost", "accepted_cost",
                    "avoided_cost"):
            self.assertEqual(owned[key], before[key], key)

    def test_a_legacy_lock_reuse_still_feeds_only_avoided_cost(self):
        event = {"event": "verification.transaction", "ts": _ts(400),
                 "transaction_id": "T1", "reused_lock_result": True}
        owned = self._owned_session([event])["owned_verification"]
        self.assertEqual(owned["avoided_cost"]["reuse_count"], 1)
        self.assertEqual(owned["bound_reuse"]["count"], 0)

    def test_a_bound_reuse_of_an_unowned_transaction_is_unknown_not_zero(self):
        event = {"event": "verification.transaction", "ts": _ts(400),
                 "transaction_id": "T2", "bound_reuse": True,
                 "bound_prior_transaction_id": "T9"}
        reuse = self._owned_session([event])["owned_verification"][
            "bound_reuse"]
        self.assertEqual(reuse["count"], 1)
        self.assertEqual(reuse["by_transaction"][0]["subprocess_wall_time_s"],
                         UNKNOWN)
        self.assertEqual(reuse["avoided_subprocess_wall_time_s"], UNKNOWN)

    def test_bound_reuse_section_renders_what_the_record_holds(self):
        event = {"event": "verification.transaction", "ts": _ts(400),
                 "transaction_id": "T2", "bound_reuse": True,
                 "bound_prior_transaction_id": "T1"}
        record = self._owned_session([event])
        text = "\n".join(report._section_bound_reuse(record))
        self.assertIn("Bound verification reuse", text)
        self.assertIn("bound reuses: 1", text)
        self.assertIn("T1", text)

    def _lineage_fixture(self):
        self._write_trace("SRC", _turn("S-1", "builder", 100,
                                       usage=_usage(2000, 200))
                          + _turn("S-2", "build-reviewer", 170,
                                  usage=_usage(500, 50)))
        measure.write_record("SRC", self._build("SRC"))
        self._write_json("S1", "lineage.json", lineage_store.new_lineage_record(
            source_session="SRC", replacement_session="S1", reason="redo",
            start_role="builder"))

    def test_lineage_counts_each_session_and_work_id_once(self):
        self._lineage_fixture()
        work = {
            "S-1": _entry(usage=_usage(2000, 200), session_uuid="SRC"),
            "N-1": _entry(usage=_usage(7, 1)),
        }
        view = measure.lineage_view("S1", work, [], [])
        reconciliation = view["reconciliation"]
        self.assertEqual(reconciliation["preserved_count"], 2)
        self.assertEqual(reconciliation["new_count"], 1)
        self.assertEqual(reconciliation["duplicates_skipped_count"], 1)
        # Preserved cost is the source's own and is not added again.
        self.assertEqual(reconciliation["totals"]["preserved"]["usage"],
                         _usage(2500, 250))
        self.assertEqual(reconciliation["totals"]["new"]["usage"],
                         _usage(7, 1))


# --------------------------------------------------------------------------- #
# Repeated context.                                                           #
# --------------------------------------------------------------------------- #


class RepeatedContextReportTests(_SessionTestCase):

    PLAN = "/a/plan.json"

    def _view(self, events, records=(), tool_view=None):
        return measure.repeated_context_view(events, list(records),
                                             tool_view or {})

    def _deliver(self, work_id, second, char="b", role="builder", size=1000):
        return _start(work_id, role, second,
                      artifacts=[_artifact(self.PLAN, char, size)])

    def test_first_delivery_is_not_repeated(self):
        view = self._view([self._deliver("W1", 10)])
        self.assertEqual(view["deliveries"]["first"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)
        self.assertEqual(view["repeated_work_ids"], [])
        self.assertEqual(view["repeated_bytes"], 0)

    def test_a_changed_digest_is_not_repeated(self):
        view = self._view([self._deliver("W1", 10, "b"),
                           self._deliver("W2", 20, "c")])
        self.assertEqual(view["deliveries"]["changed"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)

    def test_the_same_digest_with_nothing_between_is_repeated(self):
        view = self._view([self._deliver("W1", 10),
                           self._deliver("W2", 20, size=700)])
        self.assertEqual(view["deliveries"]["repeated"], 1)
        self.assertEqual(view["repeated_work_ids"], ["W2"])
        self.assertEqual(view["repeated_bytes"], 700)
        self.assertEqual(view["repeated"][0]["previous_work_id"], "W1")

    def test_a_verification_transaction_between_renews_the_delivery(self):
        transaction = {"event": "verification.transaction", "ts": _ts(15),
                       "transaction_id": "T1"}
        view = self._view([self._deliver("W1", 10), transaction,
                           self._deliver("W2", 20)])
        self.assertEqual(view["deliveries"]["renewed"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)
        self.assertEqual(view["repeated_work_ids"], [])

    def test_a_finding_recorded_between_renews_the_delivery(self):
        finding = {"id": "F-0001", "kind": "finding", "state": "open",
                   "recorded_at": _ts(15)}
        view = self._view([self._deliver("W1", 10), self._deliver("W2", 20)],
                          [finding])
        self.assertEqual(view["deliveries"]["renewed"], 1)
        on_turn = dict(finding, recorded_at=_ts(20))
        view = self._view([self._deliver("W1", 10), self._deliver("W2", 20)],
                          [on_turn])
        self.assertEqual(view["deliveries"]["renewed"], 1)

    def test_evidence_outside_the_interval_does_not_renew(self):
        before = {"id": "F-0001", "kind": "finding", "state": "open",
                  "recorded_at": _ts(5)}
        marker = {"id": "F-0002", "kind": "finding", "state": "withdrawn",
                  "marker": True, "recorded_at": _ts(15)}
        view = self._view([self._deliver("W1", 10), self._deliver("W2", 20)],
                          [before, marker])
        self.assertEqual(view["deliveries"]["repeated"], 1)

    def test_missing_descriptors_are_unknown_never_repeated(self):
        view = self._view([self._deliver("W1", 10, "b"),
                           _start("W2", "builder", 20, artifacts=[
                               {"path": self.PLAN, "bytes": 10}])])
        self.assertEqual(view["deliveries"]["unknown"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)
        malformed = self._view([_start("W1", "builder", 10,
                                       artifacts="not a list")])
        self.assertEqual(malformed["deliveries"]["unknown"], 1)
        silent = self._view([_start("W1", "builder", 10)])
        self.assertEqual(silent["deliveries"]["total"], 0)
        self.assertTrue(silent["incomplete"])

    def test_unreadable_times_are_unknown_never_repeated(self):
        late = self._deliver("W2", 20)
        late["ts"] = "garbage"
        view = self._view([self._deliver("W1", 10), late])
        self.assertEqual(view["deliveries"]["unknown"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)
        bad_evidence = {"id": "F-0001", "kind": "finding", "state": "open",
                        "recorded_at": "garbage"}
        view = self._view([self._deliver("W1", 10), self._deliver("W2", 20)],
                          [bad_evidence])
        self.assertEqual(view["deliveries"]["unknown"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)

    def test_the_same_path_under_another_role_is_independent(self):
        view = self._view([self._deliver("W1", 10, role="scout"),
                           self._deliver("W2", 20, role="builder")])
        self.assertEqual(view["deliveries"]["first"], 2)
        self.assertEqual(view["deliveries"]["repeated"], 0)

    def test_a_work_id_started_twice_is_delivered_once(self):
        view = self._view([self._deliver("W1", 10), self._deliver("W1", 10)])
        self.assertEqual(view["deliveries"]["total"], 1)
        self.assertEqual(view["deliveries"]["repeated"], 0)

    def test_unknown_artifact_size_makes_repeated_bytes_unknown(self):
        sized = self._deliver("W2", 20)
        sized["artifacts"][0]["bytes"] = "many"
        view = self._view([self._deliver("W1", 10), sized])
        self.assertEqual(view["deliveries"]["repeated"], 1)
        self.assertEqual(view["repeated_bytes"], UNKNOWN)

    def test_no_turn_start_is_unknown(self):
        view = self._view([])
        self.assertEqual(view["state"], UNKNOWN)
        self.assertEqual(view["repeated_bytes"], UNKNOWN)

    def test_built_record_flags_the_repeated_turn(self):
        record = self._built("S1", _baseline_events())
        view = record["repeated_context"]
        self.assertEqual(view["repeated_work_ids"], ["W4"])
        self.assertEqual(view["deliveries"]["total"], 4)
        self.assertEqual(view["deliveries"]["first"], 3)

    def test_section_prints_only_what_the_record_holds(self):
        view = self._view([self._deliver("W1", 10),
                           self._deliver("W2", 20, size=700)])
        text = "\n".join(report._section_repeated_context(
            {"repeated_context": view}))
        self.assertIn("Repeated context", text)
        self.assertIn("repeated=1", text)
        self.assertIn("repeated bytes: 700 B", text)
        self.assertIn("reread state: unknown", text)
        self.assertNotIn("prevented", text)


# --------------------------------------------------------------------------- #
# Cost split.                                                                 #
# --------------------------------------------------------------------------- #


class CostSplitViewTests(_SessionTestCase):

    def _split(self, work, repeated=(), incurred=None):
        return measure.cost_split_view(
            work, list(repeated), incurred if incurred is not None
            else {"work_items": 0, "subprocess_wall_time_s": 0.0})

    def _mapped_work(self):
        return {
            "A": _entry(round_=1, usage=_usage(10, 1)),
            "B": _entry(round_=2, usage=_usage(20, 2)),
            "C": _entry(round_=3, usage=_usage(30, 3)),
            "D": _entry(role="build-reviewer", round_=1,
                        usage=_usage(40, 4)),
            "E": _entry(role="build-reviewer", round_=2,
                        work_class="review", usage=_usage(50, 5)),
            "F": _entry(round_=4, work_class="recovery",
                        usage=_usage(60, 6)),
            "G": _entry(round_=None, usage=_usage(70, 7)),
            "H": _entry(phase=None, usage=_usage(80, 8)),
            "I": _entry(role="evaluator", phase=None, round_=None,
                        work_class="evaluation", usage=_usage(90, 9)),
            "J": _entry(round_=5, work_class="failed"),
            "K": _entry(round_=6, work_class="cancelled"),
            "L": _entry(work_kind="child", usage=_usage(999, 99)),
        }

    def test_turns_land_in_exactly_one_bucket(self):
        view = self._split(self._mapped_work())
        turns = {name: rollup["turns"]
                 for name, rollup in view["buckets"].items()}
        self.assertEqual(turns, {"implementation": 1, "correction": 2,
                                 "review": 2, "recovery": 1, "unknown": 5})
        self.assertEqual(view["buckets"]["implementation"]["usage"],
                         _usage(10, 1))
        self.assertEqual(view["buckets"]["correction"]["usage"],
                         _usage(50, 5))
        self.assertEqual(view["unmapped_classes"],
                         {"productive": 2, "evaluation": 1, "failed": 1,
                          "cancelled": 1})

    def test_child_work_is_in_no_bucket(self):
        view = self._split(self._mapped_work())
        for rollup in view["buckets"].values():
            usage = rollup["usage"]
            if isinstance(usage, dict):
                self.assertNotEqual(usage.get("input_tokens"), 999)

    def test_each_phase_has_its_own_first_round(self):
        work = {"P": _entry(role="planner", phase="planning", round_=2),
                "Q": _entry(role="planner", phase="planning", round_=3),
                "R": _entry(round_=1)}
        view = self._split(work)
        self.assertEqual(view["buckets"]["implementation"]["turns"], 2)
        self.assertEqual(view["buckets"]["correction"]["turns"], 1)

    def test_rework_is_an_overlay_of_correction_recovery_and_repeats(self):
        view = self._split(self._mapped_work(), repeated=["A", "B"])
        rework = view["rework"]
        self.assertTrue(rework["overlay"])
        self.assertEqual(rework["work_ids"], ["A", "B", "C", "F"])
        self.assertEqual(rework["turns"], 4)
        self.assertEqual(rework["usage"], _usage(120, 12))
        # The overlay is not part of any total: the buckets still partition
        # the non-child turns.
        self.assertEqual(
            sum(rollup["turns"] for rollup in view["buckets"].values()), 11)

    def test_verification_is_a_separate_wall_time_unit(self):
        view = self._split(self._mapped_work(), incurred={
            "work_items": 2, "subprocess_wall_time_s": 9.5})
        self.assertEqual(view["verification"], {
            "work_items": 2, "subprocess_wall_time_s": 9.5,
            "unit": "wall_seconds"})
        view = self._split({}, incurred="nope")
        self.assertEqual(view["verification"]["work_items"], UNKNOWN)
        self.assertEqual(view["verification"]["subprocess_wall_time_s"],
                         UNKNOWN)

    def test_missing_usage_and_duration_are_unknown_not_zero(self):
        work = {"A": _entry(round_=1, duration_ms=None),
                "B": _entry(round_=2, usage=_usage(5, 1))}
        view = self._split(work)
        implementation = view["buckets"]["implementation"]
        self.assertEqual(implementation["usage"], UNKNOWN)
        self.assertEqual(implementation["duration_ms"], UNKNOWN)
        self.assertEqual(implementation["unknown_usage_turns"], 1)
        empty = view["buckets"]["review"]
        self.assertEqual(empty["turns"], 0)
        self.assertEqual(empty["usage"], UNKNOWN)

    def test_an_incomparable_turn_contributes_unknown_usage(self):
        work = {"A": _entry(round_=1, usage=_usage(10, 1),
                            usage_scope="incomparable")}
        view = self._split(work)
        self.assertEqual(view["buckets"]["implementation"]["usage"], UNKNOWN)

    def test_existing_class_costs_are_untouched(self):
        events = _baseline_events(recovery=True)
        record = self._built("S1", events)
        work, _orphans = measure.build_work(events)
        self.assertEqual(record["cost"], measure.classify_costs(work))
        buckets = record["cost_split"]["buckets"]
        self.assertEqual(buckets["recovery"]["turns"], 1)
        self.assertEqual(buckets["correction"]["turns"], 0)

    def test_section_renders_the_split(self):
        view = self._split(self._mapped_work(), repeated=["A"])
        text = "\n".join(report._section_cost_split({"cost_split": view}))
        self.assertIn("Cost split", text)
        self.assertIn("implementation", text)
        self.assertIn("overlay", text)
        self.assertIn("unmapped class evaluation: 1 turn(s)", text)


# --------------------------------------------------------------------------- #
# Recovery.                                                                   #
# --------------------------------------------------------------------------- #


class RecoveryViewTests(_SessionTestCase):

    def _dir(self, session="S1"):
        return os.path.join(self._assets(session), "recovery")

    def _episode(self, failed="W1", recovery="W2", after_char="a",
                 before=True, reason="unchanged_replay"):
        return recovery_evidence.build_episode(
            "builder", failed, recovery, reason,
            before_artifacts=([{"path": "/a/p", "sha256": _sha("a")}]
                              if before else None),
            after_artifacts=[{"path": "/a/p",
                              "sha256": _sha(after_char)}],
            before_records=[], after_records=[])

    def _append(self, episode, session="S1"):
        recovery_evidence.append_episode(self._dir(session), episode)

    def _view(self, work=None, session="S1"):
        return measure.recovery_view(self._assets(session), work or {})

    def test_no_episode_file_is_unknown_and_creates_nothing(self):
        view = self._view()
        self.assertEqual(view["state"], UNKNOWN)
        self.assertEqual(view["episode_count"], UNKNOWN)
        self.assertEqual(view["episodes"], [])
        self.assertTrue(view["incomplete"])
        self.assertFalse(os.path.exists(self._dir()))

    def test_a_valid_episode_carries_its_deltas_and_value(self):
        self._append(self._episode())
        view = self._view()
        self.assertEqual(view["state"], "ok")
        self.assertEqual(view["episode_count"], 1)
        row = view["episodes"][0]
        self.assertEqual(row["failed_work_id"], "W1")
        self.assertEqual(row["recovery_work_id"], "W2")
        self.assertEqual(row["reason_class"], "unchanged_replay")
        self.assertEqual(row["artifact_delta_state"], "unchanged")
        self.assertEqual(row["finding_delta_state"], "unchanged")
        self.assertEqual(row["new_finding_count"], 0)
        self.assertEqual(row["value_state"], "zero")
        self.assertEqual(row["recovery_overhead"], "full")

    def test_zero_positive_and_unknown_value_stay_distinct(self):
        self._append(self._episode("W1", "W2"))
        self._append(self._episode("W3", "W4", after_char="c"))
        self._append(self._episode("W5", "W6", before=False))
        view = self._view()
        self.assertEqual(view["value_by_state"],
                         {"zero": 1, "positive": 1, "unknown": 1})
        states = {row["recovery_work_id"]: row["value_state"]
                  for row in view["episodes"]}
        self.assertEqual(states, {"W2": "zero", "W4": "positive",
                                  "W6": "unknown"})

    def test_a_torn_tail_is_tolerated(self):
        self._append(self._episode())
        path = os.path.join(self._dir(), recovery_evidence.EPISODES_FILENAME)
        with open(path, "ab") as fh:
            fh.write(b'{"failed_work_id": "W9", "recovery_wo')
        view = self._view()
        self.assertEqual(view["episode_count"], 1)
        self.assertEqual(view["invalid_episode_count"], 0)

    def test_an_invalid_episode_is_skipped_and_counted(self):
        self._append(self._episode())
        path = os.path.join(self._dir(), recovery_evidence.EPISODES_FILENAME)
        with open(path, "a") as fh:
            fh.write(json.dumps({"failed_work_id": "W7",
                                 "recovery_work_id": "W8"}) + "\n")
        view = self._view()
        self.assertEqual(view["episode_count"], 1)
        self.assertEqual(view["invalid_episode_count"], 1)
        self.assertTrue(any("invalid" in note["reason"]
                            for note in view["incomplete"]))

    def test_a_file_with_no_valid_episode_is_unknown(self):
        os.makedirs(self._dir())
        path = os.path.join(self._dir(), recovery_evidence.EPISODES_FILENAME)
        with open(path, "w") as fh:
            fh.write(json.dumps({"failed_work_id": "W7",
                                 "recovery_work_id": "W8"}) + "\n")
        view = self._view()
        self.assertEqual(view["state"], UNKNOWN)
        self.assertEqual(view["episode_count"], UNKNOWN)
        self.assertEqual(view["invalid_episode_count"], 1)

    def test_an_unrecognised_overhead_reads_as_unknown(self):
        episode = self._episode()
        episode["value"]["recovery_overhead"] = "weird"
        self._append(episode)
        row = self._view()["episodes"][0]
        self.assertEqual(row["recovery_overhead"], UNKNOWN)
        self.assertEqual(row["value_state"], "zero")

    def test_a_recovery_turn_without_an_episode_is_listed(self):
        self._append(self._episode("W1", "R2"))
        work = {"R1": _entry(work_class="recovery"),
                "R2": _entry(work_class="recovery"),
                "P1": _entry()}
        view = self._view(work)
        self.assertEqual(view["recovery_turn_count"], 2)
        self.assertEqual(view["unattributed_recovery_work_ids"], ["R1"])

    def test_building_a_record_leaves_the_episode_file_unchanged(self):
        self._append(self._episode("W2", "W4"))
        path = os.path.join(self._dir(), recovery_evidence.EPISODES_FILENAME)
        before = _snapshot(path)
        record = self._built("S1", _baseline_events(recovery=True))
        self.assertEqual(_snapshot(path), before)
        self.assertEqual(record["recovery"]["episode_count"], 1)
        self.assertEqual(record["recovery"]["unattributed_recovery_work_ids"],
                         [])

    def test_a_record_without_episodes_says_unknown_not_zero(self):
        record = self._built("S1", _baseline_events())
        self.assertEqual(record["recovery"]["state"], UNKNOWN)
        self.assertEqual(record["recovery"]["episode_count"], UNKNOWN)

    def test_section_renders_the_episode(self):
        self._append(self._episode())
        text = "\n".join(report._section_recovery(
            {"recovery": self._view()}))
        self.assertIn("Recovery episodes", text)
        self.assertIn("value=zero", text)
        self.assertIn("W1 -> W2", text)


# --------------------------------------------------------------------------- #
# Lineage and cohort comparability.                                           #
# --------------------------------------------------------------------------- #


class LineageReportTests(_SessionTestCase):

    COMPLETE = {"recovery": False, "recovery_reason": None,
                "evaluation_policy": "all_rounds"}

    def _source(self):
        self._write_trace("SRC", _turn("S-1", "builder", 100,
                                       usage=_usage(2000, 200))
                          + _turn("S-2", "build-reviewer", 170,
                                  usage=_usage(500, 50)))
        path = state_store.ledger_path_for("SRC")
        for summary in ("S1", "S2"):
            ledger.append_finding(
                path, summary=summary, severity="major", criterion="c1",
                discoverer="build-reviewer", round_index=1, phase="building")
        measure.write_record("SRC", self._build("SRC"))

    def _lineage(self, **fields):
        base = {"source_session": "SRC", "replacement_session": "S1",
                "reason": "redo", "start_role": "builder",
                "unresolved_finding_ids": ["F-0001", "F-0002"]}
        base.update(fields)
        self._write_json("S1", "lineage.json",
                         lineage_store.new_lineage_record(**base))

    def _replacement(self):
        events = _turn("R-1", "builder", 100, usage=_usage(1200, 120),
                       artifacts=[_artifact("/a/plan.json", "b")])
        events += _turn("R-2", "builder", 200, usage=_usage(1000, 100),
                        artifacts=[_artifact("/a/plan.json", "b")])
        path = state_store.ledger_path_for("S1")
        for summary in ("S1", "S3"):
            ledger.append_finding(
                path, summary=summary, severity="major", criterion="c1",
                discoverer="build-reviewer", round_index=1, phase="building")
        for cited in ("F-0001", "F-0001", "F-0099"):
            ledger.append_closure(path, source_finding_id=cited,
                                  source_session="SRC")
        return self._built("S1", events)

    def test_no_lineage_file_is_state_none_with_a_fixed_shape(self):
        view = self._built("S1", _baseline_events())["lineage"]
        self.assertEqual(view["state"], "none")
        for key in ("reconciliation", "closure", "cohort"):
            self.assertIsNone(view[key])
        self.assertEqual(view["incomplete"], [])

    def test_foreign_or_torn_lineage_files_are_unknown(self):
        cases = {
            "torn": lambda: self._write_text("S1", "lineage.json", "{oops"),
            "not an object": lambda: self._write_json("S1", "lineage.json",
                                                      [1, 2]),
            "wrong schema": lambda: self._write_json(
                "S1", "lineage.json", {"schema": 99,
                                       "source_session": "SRC"}),
            "path-like source": lambda: self._write_json(
                "S1", "lineage.json", {"schema": 1,
                                       "source_session": "../SRC"}),
        }
        for name, write in cases.items():
            write()
            view = self._build("S1")["lineage"]
            self.assertEqual(view["state"], UNKNOWN, name)
            self.assertTrue(view["incomplete"], name)

    def test_preserved_repeated_and_new_work_are_counted_from_both_sides(self):
        self._source()
        self._lineage()
        view = self._replacement()["lineage"]
        self.assertEqual(view["state"], "ok")
        self.assertEqual(view["source_session"], "SRC")
        self.assertEqual(view["unresolved_finding_count"], 2)
        reconciliation = view["reconciliation"]
        self.assertEqual(reconciliation["state"], "ok")
        self.assertEqual(reconciliation["preserved_count"], 2)
        self.assertEqual(reconciliation["repeated_count"], 1)
        self.assertEqual(reconciliation["new_count"], 1)
        totals = reconciliation["totals"]
        self.assertEqual(totals["preserved"]["usage"], _usage(2500, 250))
        self.assertEqual(totals["repeated"]["usage"], _usage(1000, 100))
        self.assertEqual(totals["new"]["usage"], _usage(1200, 120))

    def test_closure_value_is_counted_once_and_replays_earn_nothing(self):
        self._source()
        self._lineage()
        closure = self._replacement()["lineage"]["closure"]
        self.assertEqual(closure, {
            "closures": 1, "new_findings": 1, "replay_findings": 1,
            "replay_earned": 0, "rejected_count": 1})

    def test_an_unreadable_source_leaves_the_figures_unknown(self):
        self._lineage(source_session="GHOST")
        view = self._built("S1", _baseline_events())["lineage"]
        self.assertEqual(view["state"], "ok")
        self.assertEqual(view["reconciliation"]["state"], UNKNOWN)
        self.assertEqual(view["reconciliation"]["preserved_count"], UNKNOWN)
        self.assertEqual(view["closure"]["new_findings"], UNKNOWN)
        self.assertEqual(view["closure"]["closures"], UNKNOWN)
        self.assertTrue(view["incomplete"])

    def test_no_imported_findings_still_counts_new_ones(self):
        self._source()
        self._lineage(unresolved_finding_ids=[])
        path = state_store.ledger_path_for("S1")
        ledger.append_finding(path, summary="S9", severity="minor",
                              criterion="c2", discoverer="build-reviewer",
                              round_index=1, phase="building")
        view = self._built("S1", _baseline_events())["lineage"]
        self.assertEqual(view["closure"]["new_findings"], 1)
        self.assertEqual(view["closure"]["closures"], 0)

    def test_the_source_session_is_unchanged_by_a_build(self):
        self._source()
        self._lineage()
        source = state_store.session_assets_dir("SRC")
        before = _snapshot(source)
        self._replacement()
        self.assertEqual(_snapshot(source), before)

    def test_cohort_comparison_is_never_silent(self):
        full = self.COMPLETE
        self.assertEqual(measure.cohort_view(full, dict(full)),
                         {"comparable": True, "code": None})
        flagged = dict(full, recovery=True)
        self.assertEqual(measure.cohort_view(full, flagged)["code"],
                         "recovery_flag_differs")
        reason_a = dict(flagged, recovery_reason="schema")
        reason_b = dict(flagged, recovery_reason="debug")
        self.assertEqual(measure.cohort_view(reason_a, reason_b)["code"],
                         "recovery_reason_differs")
        self.assertEqual(
            measure.cohort_view(full, dict(full, evaluation_policy="off"))[
                "code"], "evaluation_policy_differs")
        for code in ("recovery_flag_differs", "recovery_reason_differs",
                     "evaluation_policy_differs", "descriptor_missing"):
            self.assertIn(code, lineage_store.COHORT_REFUSAL_CODES)

    def test_an_incomplete_descriptor_is_refused_not_defaulted(self):
        full = self.COMPLETE
        partial = {"recovery": False, "recovery_reason": None}
        for left, right in ((full, partial), (partial, full), (full, None),
                            (full, UNKNOWN), (partial, partial)):
            self.assertEqual(
                measure.cohort_view(left, right),
                {"comparable": False, "code": "descriptor_missing"})

    def test_a_real_session_comparison_reports_a_missing_descriptor(self):
        self._source()
        self._lineage()
        cohort = self._replacement()["lineage"]["cohort"]
        self.assertFalse(cohort["comparable"])
        self.assertEqual(cohort["code"], "descriptor_missing")
        self.assertEqual(cohort["own_descriptor"],
                         {"recovery": True, "recovery_reason": "redo"})

    def test_section_renders_the_lineage(self):
        self._source()
        self._lineage()
        record = self._replacement()
        text = "\n".join(report._section_lineage(record))
        self.assertIn("Lineage", text)
        self.assertIn("preserved=2", text)
        self.assertIn("replay earned=0", text)
        self.assertIn("code=descriptor_missing", text)
        plain = self._built("S2", _baseline_events())
        self.assertIn("(not an imported session)",
                      "\n".join(report._section_lineage(plain)))


# --------------------------------------------------------------------------- #
# Reread vocabulary.                                                          #
# --------------------------------------------------------------------------- #


class RereadStateVocabularyTests(_SessionTestCase):

    ALLOWED = {"instructed", "detected", UNKNOWN}

    def _calls(self):
        return [{"intent": "read", "started_at": _ts(110),
                 "command_identity": "cat a"},
                {"intent": "read", "started_at": _ts(120),
                 "command_identity": "cat a"}]

    def _new_views(self, record):
        return {key: record[key] for key in NEW_TOP}

    def test_every_reread_state_is_in_the_closed_vocabulary(self):
        self._write_json("S1", "lineage.json", lineage_store.new_lineage_record(
            source_session="SRC", replacement_session="S1", reason="redo",
            start_role="builder"))
        episode = recovery_evidence.build_episode(
            "builder", "W2", "W4", "unchanged_replay")
        episode["reread_state"] = "prevented"
        recovery_evidence.append_episode(
            os.path.join(self._assets("S1"), "recovery"), episode)
        record = self._built("S1", _baseline_events(), {
            "builder": ingest.Result("ok", tool_activity=self._calls()),
            "scout": ingest.Result("missing")})
        views = self._new_views(record)
        found = _collect(views, "reread_state")
        self.assertTrue(found)
        self.assertTrue(set(found) <= self.ALLOWED, found)
        self.assertNotIn("prevented", json.dumps(views))
        self.assertEqual(record["recovery"]["invalid_episode_count"], 0)
        self.assertEqual(record["recovery"]["episode_count"], 1)
        self.assertEqual(record["recovery"]["episodes"][0]["reread_state"],
                         UNKNOWN)

    def test_ingestion_that_is_not_ok_makes_the_state_unknown(self):
        record = self._built("S1", _baseline_events(), {
            "builder": ingest.Result("ok", tool_activity=self._calls()),
            "scout": ingest.Result("unreadable")})
        view = record["repeated_context"]
        self.assertEqual(view["commands"]["by_role"]["scout"], {
            "state": UNKNOWN, "repeated_targets": UNKNOWN})
        self.assertEqual(view["reread_state"], UNKNOWN)

    def test_ok_ingestion_reports_detected_repeats(self):
        record = self._built("S1", _baseline_events(), {
            "builder": ingest.Result("ok", tool_activity=self._calls())})
        view = record["repeated_context"]
        self.assertEqual(view["commands"]["by_role"]["builder"], {
            "state": "detected", "repeated_targets": 1})
        self.assertEqual(view["reread_state"], "detected")

    def test_no_ingestion_at_all_is_unknown(self):
        record = self._built("S1", _baseline_events())
        self.assertEqual(record["repeated_context"]["reread_state"], UNKNOWN)
        self.assertEqual(record["repeated_context"]["commands"],
                         {"by_role": {}})


if __name__ == "__main__":
    unittest.main()
