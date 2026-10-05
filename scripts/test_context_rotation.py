#!/usr/bin/env python3
"""Focused permanent tests for session rotation: the pure decision
(`cowork_rotation.decide_rotation`), the compare-and-pop primitive and the
write-ahead rotation record in `cowork_state`, and the content-free
`rotation->successor:handoff` edge in `cowork_handoff`.

Every input is neutral and synthetic; sessions live under a throwaway
COWORK_SESSIONS_ROOT.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_rotation
"""

import copy
import hashlib
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_context as context  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_rotation as rotation  # noqa: E402
import cowork_state as state_store  # noqa: E402

BOUNDARIES = context.BOUNDARIES
SESSION = "11111111-2222-3333-4444-555555555555"
ROLE = "scout"
CHAIN = "chain-1"
BOUNDARY = "lead_after_phase_approved"
SEQ = 1
CONTROLLER = "claude"
SID = "provider-session-1"
EDGE = "rotation->successor:handoff"
_MISSING = object()


def _facts(boundary=BOUNDARY, **overrides):
    """Every precondition satisfied and the boundary's trigger present."""
    facts = {"state_readable": True, "profiled": True,
             "first_turn_of_epoch": False, "rotate_recommended": False,
             "warn_reached": False}
    for fact, _code in rotation.SAFETY_CHECKS:
        facts[fact] = False
    facts[rotation.TRIGGER_FACT_BY_BOUNDARY[boundary]] = True
    for key, value in overrides.items():
        if value is _MISSING:
            facts.pop(key, None)
        else:
            facts[key] = value
    return facts


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _tree_hashes(root, skip_dir=None):
    found = {}
    for base, _dirs, names in os.walk(root):
        if skip_dir and (base == skip_dir
                         or base.startswith(skip_dir + os.sep)):
            continue
        for name in names:
            path = os.path.join(base, name)
            found[os.path.relpath(path, root)] = _sha(path)
    return found


class _StoreCase(unittest.TestCase):
    """A throwaway sessions root plus a state.json path."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patcher = mock.patch.dict(
            os.environ, {"COWORK_SESSIONS_ROOT": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.root, "project", "session.json")
        self.assets = state_store.session_assets_dir(SESSION)
        self.rotation_dir = os.path.join(self.assets, "rotation")

    def entry(self, **overrides):
        entry = {"controller": CONTROLLER, "id": SID, "model": "model-x",
                 "effort": "high", "last_context_revision_seen": 3,
                 "last_approved_baseline": {"digest": "d" * 8}}
        entry.update(overrides)
        return entry

    def write_state(self, scout=_MISSING, **extra):
        state = {"team": [ROLE, "builder"],
                 "config": {ROLE: {"controller": CONTROLLER}},
                 "controller_policy": {"mode": "fixed"},
                 "pending_switches": {"builder": {"to": "codex"}},
                 "sessions": {"builder": self.entry(id="other-session")}}
        if scout is _MISSING:
            scout = self.entry()
        if scout is not None:
            state["sessions"][ROLE] = scout
        state.update(extra)
        state_store.save(self.path, state)
        return state_store.load(self.path)

    def state_bytes(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def record_path(self, role=ROLE, chain=CHAIN, boundary=BOUNDARY, seq=SEQ):
        return state_store.rotation_record_path_for(
            SESSION, role, chain, boundary, seq)

    def intent(self, **overrides):
        args = dict(role=ROLE, chain=CHAIN, boundary=BOUNDARY,
                    boundary_seq=SEQ, controller=CONTROLLER, session_id=SID)
        args.update(overrides)
        return state_store.write_rotation_intent(SESSION, **args)

    def read(self):
        return state_store.read_rotation_record(
            SESSION, ROLE, CHAIN, BOUNDARY, SEQ)

    def advance(self, new_state):
        return state_store.advance_rotation_record(
            SESSION, ROLE, CHAIN, BOUNDARY, SEQ, new_state)

    def rotate(self, **overrides):
        args = dict(role=ROLE, chain=CHAIN, boundary=BOUNDARY,
                    boundary_seq=SEQ, controller=CONTROLLER, session_id=SID)
        args.update(overrides)
        return state_store.rotate_session_with_record(
            self.path, SESSION, **args)

    def write_sibling_stores(self):
        """Neutral stand-ins for stores a rotation must never touch."""
        for name in ("binding.json", "lease.json", "owner.json",
                     "identities.json"):
            os.makedirs(self.assets, exist_ok=True)
            with open(os.path.join(self.assets, name), "w") as fh:
                json.dump({"store": name, "value": 1}, fh)
        state_store.write_pending_turn_before_pause(
            SESSION, ROLE, "a pending turn")

    def assert_siblings_unchanged(self, before):
        self.assertEqual(
            _tree_hashes(self.assets, skip_dir=self.rotation_dir), before)

    def siblings(self):
        return _tree_hashes(self.assets, skip_dir=self.rotation_dir)


class DecideRotationTests(unittest.TestCase):
    def test_there_are_thirteen_unique_codes(self):
        self.assertEqual(len(rotation.REASON_CODES), 13)
        self.assertEqual(len(set(rotation.REASON_CODES)), 13)
        self.assertEqual(rotation.ROTATE, "rotate")

    def test_every_code_is_produced(self):
        cases = {
            "rotate": (BOUNDARY, _facts()),
            "defer_not_boundary": (None, None),
            "defer_first_turn_of_epoch": (
                BOUNDARY, _facts(first_turn_of_epoch=True)),
            "refuse_unreadable_state": (
                BOUNDARY, _facts(state_readable=False)),
            "refuse_unprofiled": (BOUNDARY, _facts(profiled=False)),
            "refuse_no_trigger": (BOUNDARY, _facts(rotate_recommended=False)),
        }
        for fact, code in rotation.SAFETY_CHECKS:
            cases[code] = (BOUNDARY, _facts(**{fact: True}))
        self.assertEqual(set(cases), set(rotation.REASON_CODES))
        for code, (boundary, facts) in cases.items():
            with self.subTest(code=code):
                self.assertEqual(
                    rotation.decide_rotation(boundary, facts), code)

    def test_result_is_always_a_closed_code_and_rotates_only_when_clear(self):
        values = (True, False, _MISSING)
        keys = ("state_readable", "profiled", "pending_turn",
                "first_turn_of_epoch", "rotate_recommended", "warn_reached")
        for boundary in BOUNDARIES:
            trigger = rotation.TRIGGER_FACT_BY_BOUNDARY[boundary]
            for combo in itertools.product(values, repeat=len(keys)):
                facts = _facts(boundary, **dict(zip(keys, combo)))
                code = rotation.decide_rotation(boundary, facts)
                self.assertIn(code, rotation.REASON_CODES)
                clear = (facts.get("state_readable") is True
                         and facts.get("profiled") is True
                         and facts.get("pending_turn") is False
                         and facts.get("first_turn_of_epoch") is False
                         and facts.get(trigger) is True)
                self.assertEqual(code == "rotate", clear, (boundary, facts))

    def test_rotate_at_each_boundary_with_its_own_trigger(self):
        for boundary in BOUNDARIES:
            with self.subTest(boundary=boundary):
                self.assertEqual(
                    rotation.decide_rotation(boundary, _facts(boundary)),
                    "rotate")

    def test_the_other_boundarys_trigger_does_not_license_rotation(self):
        for boundary in BOUNDARIES:
            trigger = rotation.TRIGGER_FACT_BY_BOUNDARY[boundary]
            other = ({"rotate_recommended", "warn_reached"} - {trigger}).pop()
            facts = _facts(boundary, **{trigger: False, other: True})
            with self.subTest(boundary=boundary):
                self.assertEqual(
                    rotation.decide_rotation(boundary, facts),
                    "refuse_no_trigger")

    def test_an_absent_or_non_boolean_trigger_is_no_trigger(self):
        for boundary in BOUNDARIES:
            trigger = rotation.TRIGGER_FACT_BY_BOUNDARY[boundary]
            for value in (False, None, 0, 1, "true", [], _MISSING):
                with self.subTest(boundary=boundary, value=value):
                    facts = _facts(boundary, **{trigger: value})
                    self.assertEqual(
                        rotation.decide_rotation(boundary, facts),
                        "refuse_no_trigger")

    def test_a_non_boundary_defers_without_consulting_facts(self):
        for facts in (None, {}, "not facts", _facts()):
            self.assertEqual(
                rotation.decide_rotation(None, facts), "defer_not_boundary")

    def test_an_unknown_boundary_is_a_programming_error(self):
        for boundary in ("phase_start", "", 3, ["lead_after_phase_approved"]):
            with self.subTest(boundary=boundary):
                with self.assertRaises(ValueError):
                    rotation.decide_rotation(boundary, _facts())

    def test_facts_that_are_not_a_mapping_are_unreadable(self):
        for facts in (None, "facts", 3, [("state_readable", True)]):
            with self.subTest(facts=facts):
                self.assertEqual(
                    rotation.decide_rotation(BOUNDARY, facts),
                    "refuse_unreadable_state")

    def test_any_undeclared_fact_key_is_unreadable_never_rotate(self):
        for boundary in BOUNDARIES:
            for key, value in (("capacity_held_decision", True),
                               ("capacity_held_decision", False),
                               ("extra", 0), (7, True), (None, True)):
                with self.subTest(boundary=boundary, key=key, value=value):
                    facts = _facts(boundary)
                    facts[key] = value
                    self.assertEqual(
                        rotation.decide_rotation(boundary, facts),
                        "refuse_unreadable_state")

    def test_fact_meanings_cover_exactly_the_fact_keys(self):
        self.assertEqual(set(rotation.FACT_MEANINGS), set(rotation.FACT_KEYS))
        self.assertEqual(len(rotation.FACT_KEYS), 12)
        for fact, meaning in rotation.FACT_MEANINGS.items():
            with self.subTest(fact=fact):
                precondition, reader = meaning
                self.assertTrue(precondition and reader)
        self.assertIn(
            "capacity-held decision",
            rotation.FACT_MEANINGS["capacity_pause_in_flight"][0])
        for fact, _code in rotation.SAFETY_CHECKS:
            self.assertIn(fact, rotation.FACT_KEYS)
        for trigger in rotation.TRIGGER_FACT_BY_BOUNDARY.values():
            self.assertIn(trigger, rotation.FACT_KEYS)

    def test_the_decision_is_pure(self):
        facts = _facts()
        before = copy.deepcopy(facts)
        first = rotation.decide_rotation(BOUNDARY, facts)
        self.assertEqual(rotation.decide_rotation(BOUNDARY, facts), first)
        self.assertEqual(facts, before)

    def test_refusal_precedence(self):
        everything_wrong = _facts(
            state_readable=False, profiled=False, first_turn_of_epoch=True,
            rotate_recommended=False,
            **{fact: True for fact, _code in rotation.SAFETY_CHECKS})
        self.assertEqual(rotation.decide_rotation(BOUNDARY, everything_wrong),
                         "refuse_unreadable_state")
        everything_wrong["state_readable"] = True
        self.assertEqual(rotation.decide_rotation(BOUNDARY, everything_wrong),
                         "refuse_unprofiled")
        everything_wrong["profiled"] = True
        for fact, code in rotation.SAFETY_CHECKS:
            self.assertEqual(
                rotation.decide_rotation(BOUNDARY, everything_wrong), code)
            everything_wrong[fact] = False
        self.assertEqual(rotation.decide_rotation(BOUNDARY, everything_wrong),
                         "defer_first_turn_of_epoch")
        everything_wrong["first_turn_of_epoch"] = False
        self.assertEqual(rotation.decide_rotation(BOUNDARY, everything_wrong),
                         "refuse_no_trigger")

    def test_every_safety_fact_must_be_exactly_false(self):
        for fact, code in rotation.SAFETY_CHECKS:
            for value in (True, None, 0, "", _MISSING):
                with self.subTest(fact=fact, value=value):
                    self.assertEqual(
                        rotation.decide_rotation(
                            BOUNDARY, _facts(**{fact: value})), code)

    def test_trigger_table_covers_exactly_the_boundaries(self):
        self.assertEqual(
            set(rotation.TRIGGER_FACT_BY_BOUNDARY), set(BOUNDARIES))


class UnprofiledNeverRotatesTests(unittest.TestCase):
    def test_an_unprofiled_session_never_rotates(self):
        for boundary in BOUNDARIES:
            for value in (False, None, 0, "yes", _MISSING):
                with self.subTest(boundary=boundary, value=value):
                    facts = _facts(boundary, profiled=value)
                    facts["rotate_recommended"] = True
                    facts["warn_reached"] = True
                    self.assertEqual(
                        rotation.decide_rotation(boundary, facts),
                        "refuse_unprofiled")


class PausedTurnRefusalTests(_StoreCase):
    def test_each_in_flight_fact_refuses_at_every_boundary(self):
        for boundary in BOUNDARIES:
            for fact, code in rotation.SAFETY_CHECKS:
                with self.subTest(boundary=boundary, fact=fact):
                    self.assertEqual(
                        rotation.decide_rotation(
                            boundary, _facts(boundary, **{fact: True})), code)

    def test_a_capacity_pause_wins_over_a_pending_turn(self):
        facts = _facts(capacity_pause_in_flight=True, pending_turn=True)
        self.assertEqual(rotation.decide_rotation(BOUNDARY, facts),
                         "refuse_capacity_pause_in_flight")

    def test_a_refused_rotation_leaves_a_pending_turn_record_untouched(self):
        self.write_state()
        self.write_sibling_stores()
        before = self.siblings()
        rotation.decide_rotation(
            BOUNDARY, _facts(pending_turn=True, capacity_pause_in_flight=True))
        # An abandoned rotation pops nothing and touches no sibling store.
        self.write_state(scout=self.entry(id="newer-session"))
        self.assertEqual(self.rotate()["outcome"], "abandoned_mismatch")
        self.assert_siblings_unchanged(before)


class BoundaryEdgeTests(unittest.TestCase):
    def test_first_turn_of_epoch_defers_unless_exactly_false(self):
        for boundary in BOUNDARIES:
            for value in (True, None, _MISSING):
                with self.subTest(boundary=boundary, value=value):
                    self.assertEqual(
                        rotation.decide_rotation(
                            boundary,
                            _facts(boundary, first_turn_of_epoch=value)),
                        "defer_first_turn_of_epoch")

    def test_not_a_boundary_defers(self):
        self.assertEqual(rotation.decide_rotation(None, _facts()),
                         "defer_not_boundary")

    def test_boundaries_come_from_the_context_vocabulary(self):
        self.assertEqual(set(rotation.TRIGGER_FACT_BY_BOUNDARY),
                         set(context.BOUNDARIES))
        self.assertEqual(len(context.BOUNDARIES), 3)


class RotateRoleSessionPrimitiveTests(_StoreCase):
    def test_an_exact_match_removes_only_the_id(self):
        before = self.write_state()
        after = state_store.rotate_role_session(
            self.path, ROLE, CONTROLLER, SID)
        expected = copy.deepcopy(before)
        del expected["sessions"][ROLE]["id"]
        self.assertEqual(state_store.load(self.path), expected)
        self.assertEqual(after, expected)
        entry = after["sessions"][ROLE]
        for key in ("controller", "model", "effort",
                    "last_context_revision_seen", "last_approved_baseline"):
            self.assertEqual(entry[key], before["sessions"][ROLE][key])

    def test_the_role_has_no_resumable_id_afterwards(self):
        self.write_state()
        after = state_store.rotate_role_session(
            self.path, ROLE, CONTROLLER, SID)
        self.assertIsNone(state_store.get_role_session(after, ROLE, CONTROLLER))
        self.assertEqual(after["sessions"][ROLE]["controller"], CONTROLLER)

    def test_any_mismatch_writes_nothing(self):
        cases = {
            "absent entry": self.entry,
            "different id": lambda: self.entry(id="another"),
            "different controller": lambda: self.entry(controller="codex"),
            "entry without id": lambda: {"controller": CONTROLLER},
        }
        for name, make in cases.items():
            with self.subTest(name):
                scout = None if name == "absent entry" else make()
                before = self.write_state(scout=scout)
                raw = self.state_bytes()
                result = state_store.rotate_role_session(
                    self.path, ROLE, CONTROLLER, SID)
                self.assertEqual(self.state_bytes(), raw)
                self.assertEqual(result, before)

    def test_a_prior_state_is_used_and_not_mutated(self):
        self.write_state(scout=self.entry(id="fresher"))
        prior = self.write_state()  # carries SID
        self.write_state(scout=self.entry(id="fresher"))
        snapshot = copy.deepcopy(prior)
        result = state_store.rotate_role_session(
            self.path, ROLE, CONTROLLER, SID, prior=prior)
        self.assertNotIn("id", result["sessions"][ROLE])
        self.assertEqual(prior, snapshot)

    def test_unlike_clear_the_baseline_and_ack_fields_survive(self):
        self.write_state()
        cleared = state_store.clear_role_session(
            self.path, ROLE, CONTROLLER, SID)
        self.assertNotIn(ROLE, cleared["sessions"])
        self.write_state()
        rotated = state_store.rotate_role_session(
            self.path, ROLE, CONTROLLER, SID)
        self.assertEqual(rotated["sessions"][ROLE]["last_approved_baseline"],
                         {"digest": "d" * 8})
        self.assertEqual(
            rotated["sessions"][ROLE]["last_context_revision_seen"], 3)

    def test_sibling_stores_are_untouched_by_a_pop_and_a_refused_pop(self):
        self.write_state()
        self.write_sibling_stores()
        before = self.siblings()
        state_store.rotate_role_session(self.path, ROLE, "codex", SID)
        self.assert_siblings_unchanged(before)
        state_store.rotate_role_session(self.path, ROLE, CONTROLLER, SID)
        self.assert_siblings_unchanged(before)
        self.assertFalse(os.path.exists(self.rotation_dir))


class RotationRecordTests(_StoreCase):
    def test_the_path_is_under_the_rotation_directory_and_injective(self):
        paths = set()
        for role, chain, boundary, seq in itertools.product(
                ("scout", "builder"), ("a-1", "a_1", "b"), BOUNDARIES,
                (0, 1, 12)):
            path = state_store.rotation_record_path_for(
                SESSION, role, chain, boundary, seq)
            self.assertEqual(os.path.dirname(path), self.rotation_dir)
            paths.add(path)
        self.assertEqual(len(paths), 2 * 3 * 3 * 3)

    def test_unsafe_key_parts_are_rejected_and_nothing_is_written(self):
        bad = [dict(role="a.b"), dict(role="../x"), dict(role=""),
               dict(chain="c.d"), dict(chain="-lead"), dict(chain="x" * 129),
               dict(boundary="phase_start"), dict(boundary_seq=-1),
               dict(boundary_seq=True), dict(boundary_seq="1"),
               dict(boundary_seq=1.0)]
        for overrides in bad:
            with self.subTest(overrides):
                with self.assertRaises(ValueError):
                    self.intent(**overrides)
        with self.assertRaises(ValueError):
            state_store.rotation_dir_for("../escape")
        self.assertFalse(os.path.exists(self.rotation_dir))

    def test_an_intent_record_carries_the_event_fields_and_the_pair(self):
        record = self.intent(metric="reported_input_tokens",
                             reason_code="warn_limit_reached")
        for field in context.ROTATION_EVENT_FIELDS:
            self.assertIn(field, record)
        self.assertEqual(record["state"], "intended")
        self.assertEqual(record["controller"], CONTROLLER)
        self.assertEqual(record["session_id"], SID)
        self.assertEqual(record["metric"], "reported_input_tokens")
        self.assertIn("intended_at", record)
        self.assertEqual(self.read(), {"status": "readable", "record": record})

    def test_a_repeated_intent_is_a_byte_identical_noop(self):
        self.intent()
        raw = _sha(self.record_path())
        self.intent()
        self.assertEqual(_sha(self.record_path()), raw)

    def test_a_different_session_on_the_same_key_is_refused(self):
        self.intent()
        raw = _sha(self.record_path())
        for overrides in (dict(session_id="other"),
                          dict(controller="codex")):
            with self.subTest(overrides):
                with self.assertRaises(ValueError):
                    self.intent(**overrides)
                self.assertEqual(_sha(self.record_path()), raw)

    def test_metric_and_reason_code_are_closed(self):
        with self.assertRaises(ValueError):
            self.intent(metric="vibes")
        with self.assertRaises(ValueError):
            self.intent(reason_code="because")
        self.assertFalse(os.path.exists(self.rotation_dir))

    def test_forward_transitions_succeed(self):
        self.intent()
        self.assertEqual(self.advance("popped")["state"], "popped")
        self.assertEqual(self.advance("delivered")["state"], "delivered")
        self.assertIn("delivered_at", self.read()["record"])
        self.intent(role="builder")
        state_store.advance_rotation_record(
            SESSION, "builder", CHAIN, BOUNDARY, SEQ, "abandoned")
        self.intent(role="reviewer")
        state_store.advance_rotation_record(
            SESSION, "reviewer", CHAIN, BOUNDARY, SEQ, "popped")
        state_store.advance_rotation_record(
            SESSION, "reviewer", CHAIN, BOUNDARY, SEQ, "abandoned")

    def test_repeating_the_current_state_is_a_noop(self):
        self.intent()
        self.advance("popped")
        raw = _sha(self.record_path())
        self.advance("popped")
        self.assertEqual(_sha(self.record_path()), raw)
        self.advance("delivered")
        raw = _sha(self.record_path())
        self.advance("delivered")
        self.assertEqual(_sha(self.record_path()), raw)

    def test_every_other_transition_is_refused_and_leaves_the_file_alone(self):
        paths = {
            "delivered": ("popped", "delivered"),
            "abandoned": ("abandoned",),
            "popped": ("popped",),
        }
        for start, forbidden in (
                ("delivered", ("abandoned", "popped", "intended")),
                ("abandoned", ("delivered", "popped", "intended")),
                ("popped", ("intended",))):
            self.setUp()
            self.intent()
            for step in paths[start]:
                self.advance(step)
            raw = _sha(self.record_path())
            for target in forbidden + ("unknown",):
                with self.subTest(start=start, target=target):
                    with self.assertRaises(ValueError):
                        self.advance(target)
                    self.assertEqual(_sha(self.record_path()), raw)

    def test_advancing_a_missing_record_is_refused(self):
        with self.assertRaises(ValueError):
            self.advance("popped")
        self.assertEqual(self.read()["status"], "absent")

    def test_a_torn_or_wrong_record_reads_unreadable(self):
        self.intent()
        path = self.record_path()
        good = self.read()["record"]
        wrong_key = dict(good, chain="elsewhere")
        wrong_schema = dict(good, schema=2)
        wrong_state = dict(good, state="finished")
        for name, text in (("truncated", '{"schema": 1, "sta'),
                           ("non-object", "[1, 2]"),
                           ("wrong key", json.dumps(wrong_key)),
                           ("wrong schema", json.dumps(wrong_schema)),
                           ("unknown state", json.dumps(wrong_state))):
            with self.subTest(name):
                with open(path, "w") as fh:
                    fh.write(text)
                self.assertEqual(self.read(),
                                 {"status": "unreadable", "record": None})

    def test_a_write_or_advance_on_a_torn_record_is_refused_untouched(self):
        self.intent()
        path = self.record_path()
        with open(path, "w") as fh:
            fh.write('{"torn":')
        raw = _sha(path)
        with self.assertRaises(state_store.CorruptRecordError):
            self.intent()
        with self.assertRaises(state_store.CorruptRecordError):
            self.advance("popped")
        self.assertEqual(_sha(path), raw)

    def test_listing_is_sorted_filtered_and_flags_torn_files(self):
        self.intent(role="scout")
        self.intent(role="builder", boundary_seq=2)
        torn = os.path.join(self.rotation_dir, "builder.chain-1.%s.9.json"
                            % BOUNDARY)
        with open(torn, "w") as fh:
            fh.write("{")
        with open(os.path.join(self.rotation_dir, "stray.tmp.1"), "w") as fh:
            fh.write("ignored")
        everything = state_store.list_rotation_records(SESSION)
        self.assertEqual([e["file"] for e in everything],
                         sorted(e["file"] for e in everything))
        self.assertEqual(len(everything), 3)
        builder = state_store.list_rotation_records(SESSION, role="builder")
        self.assertEqual(
            sorted(e["status"] for e in builder), ["readable", "unreadable"])
        self.assertEqual(
            [e["status"] for e in state_store.list_rotation_records(
                SESSION, role="scout")], ["readable"])
        self.assertEqual(state_store.list_rotation_records("no-such"), [])

    def test_only_the_rotation_directory_is_written(self):
        self.write_state()
        self.write_sibling_stores()
        before = self.siblings()
        state_bytes = self.state_bytes()
        self.intent()
        self.advance("popped")
        self.advance("delivered")
        state_store.list_rotation_records(SESSION)
        self.assert_siblings_unchanged(before)
        self.assertEqual(self.state_bytes(), state_bytes)
        written = {n for n in os.listdir(self.rotation_dir)}
        self.assertTrue(all(n.startswith(ROLE + ".") for n in written))


class RotationCrashRestartTests(_StoreCase):
    def test_a_crash_after_the_intent_resumes_the_pop_once(self):
        self.write_state()
        self.intent()
        result = self.rotate()
        self.assertEqual(result["outcome"], "popped")
        self.assertEqual(result["record"]["state"], "popped")
        self.assertNotIn("id", state_store.load(self.path)["sessions"][ROLE])

    def test_the_whole_flow_from_a_clean_start(self):
        before = self.write_state()
        result = self.rotate()
        self.assertEqual(result["outcome"], "popped")
        expected = copy.deepcopy(before)
        del expected["sessions"][ROLE]["id"]
        self.assertEqual(state_store.load(self.path), expected)
        self.assertEqual(self.read()["record"]["state"], "popped")

    def test_a_crash_after_the_pop_advances_without_writing_state(self):
        self.write_state()
        self.intent()
        state_store.rotate_role_session(self.path, ROLE, CONTROLLER, SID)
        raw = self.state_bytes()
        result = self.rotate()
        self.assertEqual(result["outcome"], "popped")
        self.assertEqual(self.read()["record"]["state"], "popped")
        self.assertEqual(self.state_bytes(), raw)

    def test_a_finished_record_is_already_complete_and_delivers_once(self):
        self.write_state()
        self.rotate()
        raw = self.state_bytes()
        again = self.rotate()
        self.assertEqual(again["outcome"], "already_complete")
        self.assertEqual(self.state_bytes(), raw)
        self.advance("delivered")
        delivered = _sha(self.record_path())
        self.advance("delivered")
        self.assertEqual(_sha(self.record_path()), delivered)

    def test_a_finished_record_never_pops_a_reappearing_id(self):
        for final in ("delivered", "abandoned"):
            with self.subTest(final=final):
                self.setUp()
                self.write_state()
                self.intent()
                if final == "delivered":
                    self.advance("popped")
                self.advance(final)
                self.write_state()  # the same id is present again
                raw = self.state_bytes()
                result = self.rotate()
                self.assertEqual(result["outcome"], "already_complete")
                self.assertEqual(self.state_bytes(), raw)
                self.assertEqual(
                    state_store.load(self.path)["sessions"][ROLE]["id"], SID)

    def test_a_different_id_written_since_the_intent_survives(self):
        self.write_state()
        self.intent()
        self.write_state(scout=self.entry(id="newer-session"))
        raw = self.state_bytes()
        result = self.rotate()
        self.assertEqual(result["outcome"], "abandoned_mismatch")
        self.assertEqual(self.read()["record"]["state"], "abandoned")
        self.assertEqual(self.state_bytes(), raw)
        self.assertEqual(
            state_store.load(self.path)["sessions"][ROLE]["id"],
            "newer-session")

    def test_the_same_id_under_another_controller_is_not_popped(self):
        for scout in (self.entry(controller="codex"), self.entry(id=12345)):
            with self.subTest(scout=scout):
                self.setUp()
                self.write_state(scout=scout)
                raw = self.state_bytes()
                result = self.rotate()
                self.assertEqual(result["outcome"], "abandoned_mismatch")
                self.assertEqual(self.state_bytes(), raw)
                self.assertEqual(self.read()["record"]["state"], "abandoned")

    def test_a_different_session_id_on_an_existing_key_pops_nothing(self):
        self.write_state()
        self.intent()
        raw = self.state_bytes()
        with self.assertRaises(ValueError):
            self.rotate(session_id="someone-else")
        self.assertEqual(self.state_bytes(), raw)
        self.assertEqual(self.read()["record"]["state"], "intended")

    def test_a_torn_record_pops_nothing(self):
        self.write_state()
        self.intent()
        with open(self.record_path(), "w") as fh:
            fh.write('{"torn":')
        raw = self.state_bytes()
        with self.assertRaises(state_store.CorruptRecordError):
            self.rotate()
        self.assertEqual(self.state_bytes(), raw)

    def test_unreadable_state_pops_nothing_and_keeps_the_intent(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write("{ not json")
        with self.assertRaises(ValueError):
            self.rotate()
        self.assertEqual(self.read()["record"]["state"], "intended")

    def test_a_role_without_an_entry_moves_to_popped(self):
        self.write_state(scout=None)
        raw = self.state_bytes()
        self.assertEqual(self.rotate()["outcome"], "popped")
        self.assertEqual(self.state_bytes(), raw)

    def test_other_roles_and_sibling_stores_are_untouched(self):
        before = self.write_state()
        self.write_sibling_stores()
        siblings = self.siblings()
        self.rotate()
        after = state_store.load(self.path)
        self.assertEqual(after["sessions"]["builder"],
                         before["sessions"]["builder"])
        self.assertEqual(after["pending_switches"], before["pending_switches"])
        self.assert_siblings_unchanged(siblings)


class RotationSuccessorEdgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def artifact(self, source, body="neutral"):
        path = os.path.join(self.dir, source + ".txt")
        with open(path, "w") as fh:
            fh.write(body)
        return {"label": source, "path": path, "kind": "text",
                "source": source}

    def facts(self, **overrides):
        facts = {"role": ROLE, "rotation_chain": CHAIN,
                 "rotation_boundary": BOUNDARY, "rotation_boundary_seq": SEQ}
        facts.update(overrides)
        return facts

    def render(self, sources=("rotation_record",), facts=None, **kwargs):
        return handoff.render_handoff(
            EDGE, artifacts=[self.artifact(s) for s in sources],
            facts=self.facts() if facts is None else facts, **kwargs)

    def test_the_edge_is_registered_with_distinct_slots_and_facts(self):
        spec = handoff.EDGES[EDGE]
        self.assertEqual(spec["required"], ["rotation_record"])
        self.assertEqual(
            set(spec["sources"]),
            {"rotation_record", "context", "artifacts", "rotation_packet"})
        self.assertNotIn("correction_packet", spec["sources"])
        self.assertFalse([k for k in spec["facts"]
                          if k.startswith("correction_")])
        self.assertEqual(
            set(spec["facts"]),
            {"role", "rotation_chain", "rotation_boundary",
             "rotation_boundary_seq"})

    def test_it_renders_path_first_for_every_slot(self):
        sources = ("rotation_record", "context", "artifacts",
                   "rotation_packet")
        block = self.render(sources)
        self.assertEqual(block.edge_id, EDGE)
        self.assertEqual(block.delivery, "path")
        self.assertEqual(len(block.descriptors), 4)
        for source in sources:
            path = os.path.join(self.dir, source + ".txt")
            self.assertIn(path, block)
            descriptor = [d for d in block.descriptors
                          if d["path"] == path][0]
            self.assertEqual(descriptor["sha256"], _sha(path))
            self.assertEqual(descriptor["embedded_bytes"], 0)
            self.assertIn(descriptor["sha256"][:12], block)
        for label in (handoff.SLOT_LABELS["rotation_record"],
                      handoff.SLOT_LABELS["rotation_packet"]):
            self.assertIn(label, block)

    def test_the_text_names_the_key_and_carries_no_foreign_marker(self):
        text = str(self.render())
        for value in (ROLE, CHAIN, BOUNDARY, str(SEQ)):
            self.assertIn(value, text)
        for marker in ("Correction (orchestrator-derived)", "TARGETED",
                       handoff.SWITCH_HANDOFF_MARKER):
            self.assertNotIn(marker, text)

    def test_the_required_record_and_absolute_paths_are_enforced(self):
        with self.assertRaises(handoff.MissingSourceError):
            self.render(sources=("context",))
        with self.assertRaises(handoff.MissingSourceError):
            self.render(sources=("rotation_record", "correction_packet"))
        with self.assertRaises(handoff.RelativePathError):
            handoff.render_handoff(
                EDGE, facts=self.facts(),
                artifacts=[{"label": "x", "path": "relative.txt",
                            "kind": "text", "source": "rotation_record"}])

    def test_inline_text_and_bad_values_are_rejected(self):
        bad = [dict(rotation_chain="two words"), dict(rotation_chain="a\nb"),
               dict(rotation_chain="x" * 129), dict(rotation_chain=""),
               dict(rotation_chain="-x"), dict(rotation_chain=3),
               dict(rotation_boundary="phase_start"),
               dict(rotation_boundary_seq=-1),
               dict(rotation_boundary_seq=True),
               dict(rotation_boundary_seq="1"),
               dict(role="not-a-role"), dict(correction_kind="correction")]
        for overrides in bad:
            with self.subTest(overrides):
                with self.assertRaises(handoff.ContentFreeError):
                    self.render(facts=self.facts(**overrides))

    def test_the_rotation_key_must_travel_together(self):
        for missing in ("role", "rotation_chain", "rotation_boundary",
                        "rotation_boundary_seq"):
            facts = self.facts()
            del facts[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(handoff.ContentFreeError):
                    self.render(facts=facts)

    def test_an_undeclared_ctx_key_is_rejected(self):
        with self.assertRaises(handoff.ContextError):
            self.render(ctx={"repos": []})

    def test_the_local_boundary_enum_matches_the_context_vocabulary(self):
        self.assertEqual(set(handoff._ROTATION_BOUNDARIES),
                         set(context.BOUNDARIES))
        for boundary in context.BOUNDARIES:
            self.render(facts=self.facts(rotation_boundary=boundary))

    def test_delivery_composition_accepts_the_edge(self):
        block = self.render()
        envelope = handoff.cross_role_delivery(block)
        self.assertEqual(list(envelope.edge_ids), [EDGE])
        composed = handoff.compose_handoff_blocks(block)
        self.assertEqual(composed.edge_id, EDGE)

    def test_the_topology_still_validates(self):
        self.assertTrue(handoff.validate_role_topology())


if __name__ == "__main__":
    unittest.main()
