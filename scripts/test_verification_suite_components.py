#!/usr/bin/env python3
"""Contract tests for schema-3 composed final suites (cowork_verification).

A schema-3 plan expresses its complete suite as bounded
`final_suite_component` entries that run serially inside the one owned
transaction. What is proven here, on neutral synthetic repositories only:

  - normalization: the schema-3 shape, every structural rejection code, and
    the declared-schema discipline that keeps schema 2 and legacy plans
    exactly as they were;
  - the timeout policy: no component may exceed the effective per-command
    timeout and the whole transaction must fit the Cowork ceiling, checked
    before any snapshot work;
  - the partition proof over a snapshot manifest, at module and class
    granularity, and the static classification rule for split modules
    (each R-code has a negative, each allowed construct a positive);
  - the executed-test count check, including harness-shaped compatibility
    inputs;
  - real transactions: certification with `components_ran_once`, pre-spawn
    rejection with nothing minted or left behind, fail-fast on a failing,
    timed-out, mutating or zero-count component, and deferred
    reconciliation;
  - the reviewer surfaces: pointer, overlay, both reviewer edges, report and
    measurement, plus the gate plumbing that reads the suite declaration only
    for schema 3.

Run standalone:

    python3 -m unittest scripts/test_verification_suite_components.py -v
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_handoff as handoff  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_measure as measure  # noqa: E402
import cowork_report as report  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_verification as verification  # noqa: E402


SUITE_ID = "neutral-suite"
RUNNER = ["python3", "suite/run_ids.py"]
SPLIT = "suite/test_split.py"

RUN_IDS_SOURCE = textwrap.dedent('''\
    import os
    import sys
    import unittest

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    loaded = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
    outcome = unittest.TextTestRunner(verbosity=1).run(loaded)
    sys.exit(0 if outcome.wasSuccessful() else 1)
''')

RUN_NONE_SOURCE = textwrap.dedent('''\
    import sys

    sys.stderr.write("Ran 0 tests in 0.000s\\n\\nOK\\n")
''')

RUN_QUIET_SOURCE = "import sys\n"

MODULE_SOURCE = textwrap.dedent('''\
    import unittest


    class {name}(unittest.TestCase):
        def test_value(self):
            self.assertEqual(1 + 1, 2)
''')

SPLIT_SOURCE = textwrap.dedent('''\
    import contextlib
    import time
    import unittest

    ENABLED = True


    class _SharedMixin(object):
        def shared_value(self):
            return 1


    class _AbstractCase(unittest.TestCase):
        def helper(self):
            return 2


    class AlphaFirstTests(_SharedMixin, unittest.TestCase):
        @staticmethod
        def _static_value():
            return 3

        @classmethod
        def _class_value(cls):
            return 4

        @contextlib.contextmanager
        def _managed(self):
            yield 5

        def test_shared(self):
            self.assertEqual(self.shared_value(), 1)

        def test_decorated_helpers(self):
            with self._managed() as value:
                self.assertEqual(
                    (self._static_value(), self._class_value(), value),
                    (3, 4, 5))
    {alpha_extra}

    class AlphaSecondTests(_AbstractCase):
        def test_helper(self):
            self.assertEqual(self.helper(), {helper_expected})


    @unittest.skipUnless(ENABLED, "enabled")
    class BetaFirstTests(unittest.TestCase):
        def test_flag(self):
            self.assertTrue(ENABLED)


    class BetaSecondTests(unittest.TestCase):
        def test_placeholder(self):
            self.assertIsNone(None)


    for _name in ("test_generated",):
        setattr(BetaSecondTests, _name, lambda self: None)


    if __name__ == "__main__":
        unittest.main()
''')

SLEEP_EXTRA = '''
    def test_sleeps(self):
        time.sleep(30)
'''

MUTATE_EXTRA = '''
    def test_writes_live_tree(self):
        with open({path!r}, "w") as fh:
            fh.write("changed")
'''


def _git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write(repo, rel, text):
    path = os.path.join(repo, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _make_repo(alpha_extra="", helper_expected="2", split_source=None):
    """A committed throwaway repo: the real worker module and its imports
    under scripts/, plus a synthetic suite under suite/."""
    repo = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", repo], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t"),
                       ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    os.makedirs(os.path.join(repo, "scripts"))
    for name in ("cowork_verification.py", "cowork_state.py",
                 "cowork_policy.py", "cowork_ledger.py"):
        shutil.copyfile(os.path.join(_HERE, name),
                        os.path.join(repo, "scripts", name))
    _write(repo, "suite/run_ids.py", RUN_IDS_SOURCE)
    _write(repo, "suite/run_none.py", RUN_NONE_SOURCE)
    _write(repo, "suite/run_quiet.py", RUN_QUIET_SOURCE)
    _write(repo, "suite/test_alpha.py", MODULE_SOURCE.format(name="AlphaTests"))
    _write(repo, "suite/test_beta.py", MODULE_SOURCE.format(name="BetaTests"))
    if split_source is None:
        split_source = SPLIT_SOURCE.format(
            alpha_extra=alpha_extra, helper_expected=helper_expected)
    _write(repo, SPLIT, split_source)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def _declaration(**overrides):
    decl = {
        "suite_id": SUITE_ID,
        "runner": verification.SUITE_RUNNER_UNITTEST_IDS,
        "tests_dir": "suite",
        "universe": {"include": ["suite/test_*.py"], "exclude": []},
        "split_modules": [SPLIT],
    }
    decl.update(overrides)
    return decl


def _component(label, covers, command=None, **extra):
    entry = {"label": label, "command": list(command or RUNNER),
             "execution_mode": "isolated_snapshot",
             "kind": verification.KIND_FINAL_SUITE_COMPONENT,
             "suite_id": SUITE_ID, "covers": list(covers)}
    entry.update(extra)
    return entry


def _components(command=None, **extra_first):
    return [
        _component("modules", ["suite/test_[ab]*.py"], command=command,
                   **extra_first),
        _component("split-alpha", [SPLIT + "::Alpha*"], command=command),
        _component("split-rest", [SPLIT + "::[!A]*"], command=command),
    ]


def _baseline(label="compile", command=None):
    return {"label": label,
            "command": command or ["python3", "-c", "pass"],
            "execution_mode": "isolated_snapshot",
            "kind": verification.KIND_BASELINE}


class RecordingTrace(object):
    def __init__(self):
        self.events = []

    def event(self, name, **fields):
        self.events.append((name, fields))


class _SessionRootMixin(object):
    def isolate_sessions_root(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- #
# Normalization                                                               #
# --------------------------------------------------------------------------- #


class Schema3NormalizationTests(unittest.TestCase):

    def _normalize(self, raw, declared=3, suite="default"):
        if suite == "default":
            suite = _declaration()
        return verification.normalize_inventory(
            raw, declared_schema=declared, suite=suite)

    def _assert_code(self, code, raw, declared=3, suite="default"):
        with self.assertRaises(verification.InventoryError) as ctx:
            self._normalize(raw, declared=declared, suite=suite)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def test_valid_composed_inventory_normalizes(self):
        schema, entries, label = self._normalize(
            [_baseline()] + _components())
        self.assertEqual(schema, verification.SCHEMA_3)
        self.assertEqual(label, SUITE_ID)
        components = [e for e in entries
                      if e["kind"] == verification.KIND_FINAL_SUITE_COMPONENT]
        self.assertEqual(len(components), 3)
        self.assertEqual(components[0]["command"], RUNNER)
        self.assertEqual(components[1]["covers"], [SPLIT + "::Alpha*"])

    def test_declared_schema_mismatches(self):
        raw = _components()
        for declared in (None, 2, 1):
            with self.subTest(declared=declared):
                self._assert_code("declared_schema_mismatch", raw,
                                  declared=declared, suite=None)
        schema2 = [_baseline(), dict(_baseline("final"),
                                     kind=verification.KIND_FINAL_SUITE)]
        self._assert_code("declared_schema_mismatch", schema2)
        self._assert_code("declared_schema_mismatch", schema2, declared=2)
        # The paired schema-2 positive: no suite, declared 2.
        schema, _entries, label = verification.normalize_inventory(
            schema2, declared_schema=2)
        self.assertEqual((schema, label), (verification.SCHEMA_2, "final"))

    def test_each_structural_defect_has_its_own_code(self):
        base = [_baseline()] + _components()

        def with_component(index, **changes):
            raw = [dict(e) for e in base]
            raw[index].update(changes)
            return raw

        no_covers = [dict(e) for e in base]
        del no_covers[1]["covers"]
        no_covers[1]["kind"] = verification.KIND_FINAL_SUITE_COMPONENT
        cases = [
            ("missing_suite_declaration", base, None),
            ("bad_suite_declaration", base, _declaration(suite_id="bad id")),
            ("bad_suite_declaration", base, _declaration(runner="pytest")),
            ("bad_suite_declaration", base, _declaration(tests_dir="a/../b")),
            ("bad_suite_declaration", base,
             _declaration(universe={"include": []})),
            ("bad_suite_declaration",
             [_baseline()] + [dict(c, suite_id="compile")
                              for c in _components()],
             _declaration(suite_id="compile")),
            ("final_suite_in_schema3",
             base + [dict(_baseline("final"),
                          kind=verification.KIND_FINAL_SUITE)], "default"),
            ("missing_suite_components",
             [_baseline(), dict(_baseline("x"), covers=[])], "default"),
            ("components_not_last", base + [_baseline("late")], "default"),
            ("foreign_suite_component", with_component(1, suite_id="other"),
             "default"),
            ("empty_component", with_component(1, covers=[]), "default"),
            ("empty_component", no_covers, "default"),
            ("duplicate_component",
             with_component(3, covers=[SPLIT + "::Alpha*"]), "default"),
            ("bad_expected_test_count",
             with_component(1, expected_test_count=0), "default"),
            ("bad_expected_test_count",
             with_component(1, expected_test_count=True), "default"),
            ("unbound_component_argv",
             with_component(1, command=RUNNER + ["test_alpha"]), "default"),
            ("component_timeout_exceeds_policy",
             with_component(1, max_duration_s=-1), "default"),
        ]
        for code, raw, suite in cases:
            with self.subTest(code=code):
                self._assert_code(code, raw, suite=suite)
        # The paired positive: the same inventory minus every defect.
        self.assertEqual(self._normalize(base)[0], verification.SCHEMA_3)

    def test_a_stray_suite_with_another_declared_schema_is_a_mismatch(self):
        schema2 = [_baseline(), dict(_baseline("final"),
                                     kind=verification.KIND_FINAL_SUITE)]
        self._assert_code("declared_schema_mismatch", schema2, declared=2,
                          suite=_declaration())

    def test_schema2_is_unchanged(self):
        self.assertEqual(verification.KINDS, (
            verification.KIND_BASELINE, verification.KIND_FOCUSED,
            verification.KIND_PREFLIGHT, verification.KIND_FINAL_SUITE))
        final = dict(_baseline("final"), kind=verification.KIND_FINAL_SUITE)
        schema, entries, label = verification.normalize_inventory(
            [_baseline(), final])
        self.assertEqual((schema, label), (verification.SCHEMA_2, "final"))
        self.assertEqual([e["label"] for e in entries], ["compile", "final"])
        with self.assertRaises(verification.InventoryError) as ctx:
            verification.normalize_inventory([final, _baseline()])
        self.assertEqual(ctx.exception.code, "final_suite_not_last")
        with self.assertRaises(verification.InventoryError) as ctx:
            verification.normalize_inventory([final, dict(final, label="f2")])
        self.assertEqual(ctx.exception.code, "multiple_final_suite")

    def test_dedup_keeps_identical_component_commands(self):
        entries = [dict(e, command=list(RUNNER)) for e in _components()]
        kept, reused = verification.deduplicate_inventory(entries)
        self.assertEqual(len(kept), 3)
        self.assertEqual(reused, {})

    def test_request_key_binds_the_suite_record(self):
        _schema, entries, label = self._normalize(_components())
        common = ("S", "T", "/repo", "m" * 64, "i" * 64, {}, 3, entries,
                  label)
        first = verification.build_request(*common, suite_record={"a": 1})
        second = verification.build_request(*common, suite_record={"a": 2})
        self.assertNotEqual(first["request_key"], second["request_key"])
        self.assertEqual(first["suite"], {"a": 1})
        plain = verification.build_request(*common)
        self.assertNotIn("suite", plain)
        self.assertEqual(plain["timeout_policy"]["command_timeout_s"],
                         verification.DEFAULT_COMMAND_TIMEOUT_S)


class TimeoutPolicyTests(unittest.TestCase):

    def test_default_per_command_timeout_is_unchanged(self):
        self.assertEqual(verification.DEFAULT_COMMAND_TIMEOUT_S, 300)

    def test_component_bound_above_the_effective_timeout_is_rejected(self):
        entries = _components(max_duration_s=301)
        with self.assertRaises(verification.InventoryError) as ctx:
            verification.validate_timeout_policy(3, entries)
        self.assertEqual(ctx.exception.code,
                         "component_timeout_exceeds_policy")
        with self.assertRaises(verification.InventoryError) as ctx:
            verification.validate_timeout_policy(
                3, _components(max_duration_s=60), command_timeout_s=30)
        self.assertEqual(ctx.exception.code,
                         "component_timeout_exceeds_policy")
        verification.validate_timeout_policy(
            3, _components(max_duration_s=300))

    def test_overall_ceiling_is_enforced_for_schema3_only(self):
        many = [_component("c%02d" % i, ["suite/test_%02d.py" % i])
                for i in range(30)]
        with self.assertRaises(verification.InventoryError) as ctx:
            verification.validate_timeout_policy(3, many)
        self.assertEqual(ctx.exception.code, "overall_deadline_exceeds_policy")
        verification.validate_timeout_policy(2, many)
        verification.validate_timeout_policy(3, many[:20])


# --------------------------------------------------------------------------- #
# Partition proof                                                             #
# --------------------------------------------------------------------------- #


def _manifest(sources):
    import hashlib
    files = {}
    blobs = {}
    for path, text in sources.items():
        raw = text.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        files[path] = {"type": "file", "sha256": sha}
        blobs[sha] = raw
    return files, blobs.__getitem__


def _neutral_sources(**extra):
    sources = {
        "suite/test_alpha.py": MODULE_SOURCE.format(name="AlphaTests"),
        "suite/test_beta.py": MODULE_SOURCE.format(name="BetaTests"),
        SPLIT: SPLIT_SOURCE.format(alpha_extra="", helper_expected="2"),
        "suite/run_ids.py": RUN_IDS_SOURCE,
        "scripts/tool.py": "x = 1\n",
    }
    sources.update(extra)
    return sources


class PartitionProofTests(unittest.TestCase):

    def _prove(self, components=None, decl=None, sources=None):
        files, read_bytes = _manifest(sources or _neutral_sources())
        return verification.prove_suite_partition(
            decl or _declaration(), components or _components(), files,
            read_bytes)

    def _assert_code(self, code, **kwargs):
        with self.assertRaises(verification.InventoryError) as ctx:
            self._prove(**kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def test_exact_partition_produces_a_proof_record(self):
        record, resolved = self._prove()
        self.assertEqual(record["proof"], "partition_valid")
        self.assertEqual(record["granularity"], "module+class")
        self.assertEqual(record["member_count"], 6)
        self.assertEqual(resolved["modules"], ["test_alpha", "test_beta"])
        self.assertEqual(resolved["split-alpha"], [
            "test_split.AlphaFirstTests", "test_split.AlphaSecondTests"])
        self.assertEqual(resolved["split-rest"], [
            "test_split.BetaFirstTests", "test_split.BetaSecondTests"])
        self.assertEqual(len(record["universe_digest"]), 64)
        self.assertEqual([c["member_count"] for c in record["components"]],
                         [2, 2, 2])
        self.assertEqual(record["declaration"]["tests_dir"], "suite")

    def test_module_only_granularity(self):
        record, _resolved = self._prove(
            components=[_component("all", ["suite/test_*.py"])],
            decl=_declaration(split_modules=[]))
        self.assertEqual(record["granularity"], "module")
        self.assertEqual(record["member_count"], 3)

    def test_digests_are_deterministic_and_member_sensitive(self):
        first, _ = self._prove()
        second, _ = self._prove()
        self.assertEqual(first["universe_digest"], second["universe_digest"])
        # The module glob must also cover the added module, or the proof
        # (correctly) rejects it as missing.
        grown, _ = self._prove(
            components=[_component("modules", ["suite/test_*.py"])]
            + _components()[1:],
            sources=_neutral_sources(**{
                "suite/test_gamma.py": MODULE_SOURCE.format(
                    name="GammaTests")}))
        self.assertEqual(grown["member_count"], first["member_count"] + 1)
        self.assertNotEqual(first["universe_digest"],
                            grown["universe_digest"])

    def test_integrity_defects_fail_closed(self):
        components = _components()
        self._assert_code("missing_members", components=components[:2])
        self._assert_code("overlapping_members", components=components + [
            _component("again", [SPLIT + "::AlphaFirst*"])])
        self._assert_code("foreign_selector", components=components + [
            _component("ghost", ["suite/test_missing.py"])])
        self._assert_code("foreign_selector", components=[
            _component("modules", ["suite/test_alpha.py::A*"])]
            + components[1:])
        self._assert_code("empty_universe",
                          decl=_declaration(universe={"include": ["none/*"]}))
        self._assert_code("universe_member_not_runnable",
                          decl=_declaration(universe={"include": ["suite/*"]}))
        self._assert_code("foreign_split_module",
                          decl=_declaration(split_modules=["suite/x.py"]))
        bad_split = _neutral_sources(**{
            SPLIT: "import unittest\nclass A(unittest.TestCase, "
                   "metaclass=type):\n    def test_x(self):\n        pass\n"})
        self._assert_code("unclassifiable_split_module", sources=bad_split)

    def test_exclusions_are_recorded(self):
        record, _ = self._prove(
            components=[_component("modules", ["suite/test_alpha.py"])]
            + _components()[1:],
            decl=_declaration(
                universe={"include": ["suite/test_*.py"],
                          "exclude": ["suite/test_beta.py"]},
                exclusion_reasons={"suite/test_beta.py": "runs elsewhere"}))
        self.assertEqual(record["excluded_paths"], ["suite/test_beta.py"])
        self.assertEqual(record["exclusion_reasons"],
                         {"suite/test_beta.py": "runs elsewhere"})

    def test_resolved_entries_append_exactly_the_proven_ids(self):
        _record, resolved = self._prove()
        entries = verification.resolve_component_entries(
            [_baseline()] + _components(), resolved)
        self.assertEqual(entries[0]["command"], ["python3", "-c", "pass"])
        self.assertEqual(entries[2]["command"], RUNNER + [
            "test_split.AlphaFirstTests", "test_split.AlphaSecondTests"])
        self.assertEqual(entries[1]["command"],
                         RUNNER + ["test_alpha", "test_beta"])


# --------------------------------------------------------------------------- #
# Split-module classification (static, never executes the module)            #
# --------------------------------------------------------------------------- #


class SplitModuleClassificationTests(unittest.TestCase):

    def _classify(self, source):
        return verification._enumerate_split_module_classes(
            "suite/test_sample.py", textwrap.dedent(source).encode("utf-8"))

    def _reject(self, rule, source):
        with self.assertRaises(verification.InventoryError) as ctx:
            self._classify(source)
        self.assertEqual(ctx.exception.code, "unclassifiable_split_module")
        self.assertIn(": %s " % rule, str(ctx.exception))

    def test_allowed_constructs_classify(self):
        members = self._classify('''
            import contextlib
            import unittest
            from unittest import IsolatedAsyncioTestCase, TestCase, mock

            ENABLED = True


            class _Mixin(object):
                pass


            class _Abstract(unittest.TestCase):
                def helper(self):
                    return 1


            def _impl(self):
                return None


            class WithMixin(_Mixin, TestCase):
                test_assigned = _impl

                @property
                def value(self):
                    return self._value

                @value.setter
                def value(self, new):
                    self._value = new

                @staticmethod
                def _s():
                    return 1

                @classmethod
                def _c(cls):
                    return 2

                @contextlib.contextmanager
                def _m(self):
                    yield 3

                @mock.patch("os.getcwd")
                def test_patched(self, getcwd):
                    pass

                @unittest.skipUnless(ENABLED, "enabled")
                def test_skippable(self):
                    pass

                def __getattr__(self, name):
                    raise AttributeError(name)


            @unittest.skipUnless(ENABLED, "enabled")
            class FromAbstract(_Abstract):
                def test_one(self):
                    pass


            class Duplicate(unittest.TestCase):
                def test_first(self):
                    pass


            class Duplicate(unittest.TestCase):
                def test_second(self):
                    pass


            class Asyncish(IsolatedAsyncioTestCase):
                async def test_async(self):
                    pass


            class Generated(unittest.TestCase):
                pass


            for _name in [n for n in dir(FromAbstract) if n.startswith("t")]:
                setattr(Generated, _name, None)


            if __name__ == "__main__":
                class Ignored(unittest.TestCase):
                    def test_never(self):
                        pass
                unittest.main()
        ''')
        self.assertEqual(members, ["Asyncish", "Duplicate", "FromAbstract",
                                   "Generated", "WithMixin"])

    def test_each_rule_rejects_its_constructs(self):
        cases = [
            ("R1", "class A(:\n"),
            ("R2", '''
                from elsewhere import Base
                class A(Base):
                    def test_x(self):
                        pass
            '''),
            ("R3", "def load_tests(loader, tests, pattern):\n"
                   "    return tests\n"),
            ("R3", "def __getattr__(name):\n    raise AttributeError(name)\n"),
            ("R3", "__dir__ = list\n"),
            ("R4", '''
                import unittest
                if True:
                    class A(unittest.TestCase):
                        pass
            '''),
            ("R4", '''
                import unittest
                for _i in range(1):
                    class A(unittest.TestCase):
                        pass
            '''),
            ("R5", '''
                import unittest
                class A(unittest.TestCase, metaclass=type):
                    pass
            '''),
            ("R5", '''
                import unittest
                class A(unittest.TestCase, flavor=1):
                    pass
            '''),
            ("R6", '''
                import unittest
                class Base(unittest.TestCase):
                    def __init_subclass__(cls, **kwargs):
                        pass
                class A(Base):
                    def test_x(self):
                        pass
            '''),
            ("R7", "X = type('X', (), {})\n"),
            ("R7", "exec('pass')\n"),
            ("R7", "globals()\n"),
            ("R7", '''
                import unittest
                class A(unittest.TestCase):
                    globals()['B'] = type('B', (unittest.TestCase,), {})
            '''),
            ("R7", '''
                import unittest
                setattr(unittest, "flag", 1)
            '''),
            ("R8", '''
                def helper():
                    return 1
                VALUE = helper()
            '''),
            ("R8", '''
                import unittest
                def helper():
                    return 1
                class A(unittest.TestCase):
                    value = helper()
            '''),
            ("R8", '''
                import unittest
                def helper():
                    return 1
                class A(unittest.TestCase):
                    def test_x(self, value=helper()):
                        pass
            '''),
            ("R8", '''
                import unittest
                def mark(cls):
                    return cls
                @mark
                class A(unittest.TestCase):
                    pass
            '''),
            ("R8", '''
                import contextlib
                import unittest
                @contextlib.contextmanager
                class A(unittest.TestCase):
                    pass
            '''),
            ("R8", '''
                import unittest
                def wrap(fn):
                    return fn
                class A(unittest.TestCase):
                    @wrap
                    def test_x(self):
                        pass
            '''),
            ("R8", '''
                import unittest
                registry = {}
                class A(unittest.TestCase):
                    @registry.mark
                    def test_x(self):
                        pass
            '''),
            ("R8", '''
                import unittest
                staticmethod = classmethod
                class A(unittest.TestCase):
                    @staticmethod
                    def _s():
                        pass
            '''),
            ("R8", '''
                import contextlib
                import unittest
                contextlib = None
                class A(unittest.TestCase):
                    @contextlib.contextmanager
                    def _m(self):
                        yield
            '''),
            ("R9", '''
                import unittest
                class A(unittest.TestCase):
                    pass
                from elsewhere import A
            '''),
            ("R9", '''
                import unittest
                class A(unittest.TestCase):
                    pass
                class B(unittest.TestCase):
                    pass
                A = B
            '''),
            ("R9", '''
                import unittest
                class A(unittest.TestCase):
                    pass
                for A in ():
                    pass
            '''),
            ("R9", '''
                import unittest
                class A(unittest.TestCase):
                    pass
                def A():
                    pass
            '''),
            ("R9", '''
                import unittest
                class A(unittest.TestCase):
                    pass
                del A
            '''),
            ("R9", '''
                import unittest
                class A(unittest.TestCase):
                    pass
                def rebind():
                    global A
            '''),
            ("R10", '''
                import sys
                sys.modules[__name__].X = 1
            '''),
        ]
        for rule, source in cases:
            with self.subTest(rule=rule, source=source):
                self._reject(rule, source)

    def test_an_abstract_base_without_tests_is_not_a_member(self):
        members = self._classify('''
            import unittest
            class _Base(unittest.TestCase):
                def helper(self):
                    return 1
            class Concrete(_Base):
                def test_x(self):
                    pass
        ''')
        self.assertEqual(members, ["Concrete"])


# --------------------------------------------------------------------------- #
# Executed-test count                                                         #
# --------------------------------------------------------------------------- #


class TestCountCheckTests(unittest.TestCase):

    def check(self, terminal, **entry):
        return verification.component_test_count_check(entry, terminal)

    def test_each_outcome(self):
        self.assertEqual(self.check({"stderr": "Ran 3 tests in 0.1s\n"}),
                         (3, "ok"))
        self.assertEqual(self.check({"stderr": "Ran 1 test in 0.1s\n"}),
                         (1, "ok"))
        self.assertEqual(self.check({"stderr": "Ran 0 tests in 0.0s\n"}),
                         (0, "zero"))
        self.assertEqual(self.check({"stderr": "nothing here"}),
                         (None, "unstated"))
        self.assertEqual(
            self.check({"stderr": "Ran 3 tests\n"}, expected_test_count=4),
            (3, "mismatch"))
        self.assertEqual(
            self.check({"stderr": "Ran 4 tests\n"}, expected_test_count=4),
            (4, "ok"))
        self.assertEqual(
            self.check({"stderr": "Ran 3 tests\n", "stdout_truncated": True}),
            (3, "truncated"))

    def test_last_summary_wins_and_stdout_is_read_first(self):
        self.assertEqual(
            self.check({"stderr": "Ran 9 tests\nRan 2 tests\n"}), (2, "ok"))
        self.assertEqual(
            self.check({"stdout": "Ran 5 tests", "stderr": "Ran 7 tests"}),
            (5, "ok"))

    def test_offline_harness_brief_compatibility_inputs(self):
        brief = json.dumps({"exit_code": 0, "result_lines": ["Ran 3 tests",
                                                             "OK"]})
        self.assertEqual(self.check({"stdout": brief, "stderr": ""}),
                         (3, "ok"))
        empty = json.dumps({"exit_code": 0, "result_lines": []})
        self.assertEqual(self.check({"stdout": empty, "stderr": ""}),
                         (None, "unstated"))


# --------------------------------------------------------------------------- #
# Real transactions                                                           #
# --------------------------------------------------------------------------- #


class _TransactionFixture(_SessionRootMixin, unittest.TestCase):

    def setUp(self):
        self.isolate_sessions_root()

    def make_repo(self, **kwargs):
        repo = _make_repo(**kwargs)
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        return repo

    def run_suite(self, repo, raw, decl=None, **kwargs):
        return verification.run_transaction(
            repo, self.session_uuid, raw, declared_schema=3,
            suite=decl or _declaration(), **kwargs)

    def ledger_records(self):
        return [r for r in ledger.read_ledger(
            state_store.ledger_path_for(self.session_uuid))
            if r.get("kind") == "attempt" and not r.get("marker")]

    def latest(self, transaction_id, label):
        return verification._latest_ledger_record(
            state_store.ledger_path_for(self.session_uuid), transaction_id,
            label)

    def transaction_ids(self):
        root = os.path.dirname(state_store.verification_transaction_dir(
            self.session_uuid, "probe"))
        try:
            return sorted(os.listdir(root))
        except OSError:
            return []

    def assert_nothing_left(self):
        self.assertEqual(self.ledger_records(), [])
        for txn in self.transaction_ids():
            for path in (
                    state_store.verification_snapshot_manifest_path_for(
                        self.session_uuid, txn),
                    state_store.verification_snapshot_checkout_dir(
                        self.session_uuid, txn),
                    state_store.verification_request_path_for(
                        self.session_uuid, txn)):
                self.assertFalse(os.path.exists(path), path)

    @contextlib.contextmanager
    def no_spawn(self):
        def _refuse(*_args, **_kwargs):
            raise AssertionError("a worker was spawned for a rejected plan")
        with mock.patch.object(verification, "spawn_worker",
                               side_effect=_refuse):
            yield

    def assert_rejected(self, code, repo, raw, decl=None, **kwargs):
        with self.no_spawn():
            with self.assertRaises(verification.InventoryError) as ctx:
                self.run_suite(repo, raw, decl=decl, **kwargs)
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))
        self.assert_nothing_left()


class ComposedSuiteCertificationTests(_TransactionFixture):

    def test_exact_partition_is_certified_once(self):
        repo = self.make_repo()
        raw = [_baseline()] + _components()
        result = self.run_suite(repo, raw)
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN,
                         json.dumps(result, default=str)[:2000])
        self.assertEqual(result["final_suite_label"], SUITE_ID)
        self.assertEqual(result["final_suite_binding"],
                         verification.FINAL_SUITE_COMPONENTS_RAN_ONCE)
        components = [a for a in result["attempts"] if a.get("kind")
                      == verification.KIND_FINAL_SUITE_COMPONENT]
        self.assertEqual(len(components), 3)
        self.assertEqual(len(result["attempts"]), 4)
        for attempt in components:
            self.assertEqual(attempt["evidence_state"], "present")
            self.assertEqual(attempt["exit_code"], 0)
            self.assertEqual(attempt["test_count_check"], "ok")
            self.assertGreater(attempt["observed_test_count"], 0)
        self.assertEqual(len({a["ledger_attempt_id"] for a in components}), 3)
        request = state_store.read_json_tolerant(
            state_store.verification_request_path_for(
                self.session_uuid, result["transaction_id"]))
        self.assertEqual(result["suite"], request["suite"])
        self.assertEqual(result["suite"]["member_count"], 6)
        on_disk = state_store.read_json_tolerant(
            state_store.verification_result_path_for(
                self.session_uuid, result["transaction_id"]))
        self.assertEqual(on_disk["suite"], request["suite"])
        commands = {e["label"]: e["command"] for e in request["inventory"]}
        self.assertEqual(commands["split-alpha"], RUNNER + [
            "test_split.AlphaFirstTests", "test_split.AlphaSecondTests"])
        self.assertEqual(commands["modules"],
                         RUNNER + ["test_alpha", "test_beta"])
        for label in ("modules", "split-alpha", "split-rest"):
            self.assertEqual(self.latest(result["transaction_id"],
                                         label)["test_count_check"], "ok")

    def test_an_added_module_is_assigned_by_selector_or_missing(self):
        repo = self.make_repo()
        _write(repo, "suite/test_gamma.py",
               MODULE_SOURCE.format(name="GammaTests"))
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "add module")
        globbed = [_component("modules", ["suite/test_*.py"])] \
            + _components()[1:]
        result = self.run_suite(repo, globbed)
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(result["suite"]["member_count"], 7)
        literal = [_component("modules", ["suite/test_alpha.py",
                                          "suite/test_beta.py"])] \
            + _components()[1:]
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.assert_rejected("missing_members", repo, literal)


class PreSpawnRejectionTests(_TransactionFixture):

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def test_missing_class_member(self):
        raw = [_component("modules", ["suite/test_[ab]*.py"]),
               _component("split-alpha", [SPLIT + "::AlphaFirstTests"]),
               _component("split-rest", [SPLIT + "::[!A]*"])]
        self.assert_rejected("missing_members", self.repo, raw)

    def test_each_proof_and_policy_code(self):
        components = _components()
        cases = [
            ("overlapping_members", components + [
                _component("again", [SPLIT + "::Beta*"])], None, {}),
            ("foreign_selector", components + [
                _component("ghost", ["suite/test_ghost.py"])], None, {}),
            ("empty_universe", components,
             _declaration(universe={"include": ["nothing/*.py"]}), {}),
            ("universe_member_not_runnable", components,
             _declaration(universe={"include": ["suite/*.py"]}), {}),
            ("foreign_split_module", components,
             _declaration(split_modules=[SPLIT, "suite/test_none.py"]), {}),
            ("unbound_component_argv",
             [_component("modules", ["suite/test_[ab]*.py"],
                         command=RUNNER + ["test_alpha"])] + components[1:],
             None, {}),
            ("component_timeout_exceeds_policy",
             _components(max_duration_s=600), None, {}),
            ("component_timeout_exceeds_policy",
             _components(max_duration_s=20), None,
             {"command_timeout_s": 10}),
            ("overall_deadline_exceeds_policy", components, None,
             {"command_timeout_s": 3000}),
        ]
        for code, raw, decl, kwargs in cases:
            with self.subTest(code=code):
                self.session_uuid = "S-" + uuid.uuid4().hex[:8]
                self.assert_rejected(code, self.repo, raw, decl=decl,
                                     **kwargs)

    def test_unclassifiable_split_module(self):
        repo = self.make_repo(split_source=(
            "import unittest\n\n\nclass AlphaTests(unittest.TestCase, "
            "metaclass=type):\n    def test_x(self):\n        pass\n"))
        self.assert_rejected("unclassifiable_split_module", repo,
                             _components())

    def test_rejected_schema2_argv_no_longer_leaves_a_snapshot(self):
        raw = [dict(_baseline("final", ["python3", "/etc/hosts"]),
                    kind=verification.KIND_FINAL_SUITE)]
        with self.no_spawn():
            with self.assertRaises(verification.InventoryError) as ctx:
                verification.run_transaction(self.repo, self.session_uuid,
                                             raw)
        self.assertEqual(ctx.exception.code, "unsafe_argv_absolute_escape")
        self.assert_nothing_left()


class ComponentFailFastTests(_TransactionFixture):

    def _assert_stopped_after_first(self, result, reason_check=None):
        self.assertEqual(result["verdict"], verification.VERDICT_RED,
                         json.dumps(result, default=str)[:2000])
        self.assertEqual(result["final_suite_binding"], "not_reached")
        self.assertEqual(len(result["attempts"]), 1)
        txn = result["transaction_id"]
        for label in ("split-alpha", "split-rest"):
            self.assertEqual(self.latest(txn, label)["attempt_state"],
                             "not_reached")
        if reason_check is not None:
            first = self.latest(txn, "modules")
            self.assertEqual(first["test_count_check"], reason_check)
            self.assertEqual(first["adjudication"], "fail")

    def test_a_failing_component_stops_later_components(self):
        repo = self.make_repo(helper_expected="3")
        raw = [_component("split-alpha", [SPLIT + "::Alpha*"]),
               _component("modules", ["suite/test_[ab]*.py"]),
               _component("split-rest", [SPLIT + "::[!A]*"])]
        result = self.run_suite(repo, raw)
        self.assertEqual(result["verdict"], verification.VERDICT_RED)
        self.assertEqual(result["final_suite_binding"], "not_reached")
        self.assertEqual(result["attempts"][0]["exit_code"], 1)
        self.assertEqual(len(result["attempts"]), 1)
        for label in ("modules", "split-rest"):
            self.assertEqual(self.latest(result["transaction_id"],
                                         label)["attempt_state"],
                             "not_reached")

    def test_a_timed_out_component_is_torn_down(self):
        repo = self.make_repo(alpha_extra=SLEEP_EXTRA)
        raw = [_component("split-alpha", [SPLIT + "::Alpha*"]),
               _component("modules", ["suite/test_[ab]*.py"]),
               _component("split-rest", [SPLIT + "::[!A]*"])]
        result = self.run_suite(repo, raw, command_timeout_s=2)
        self.assertEqual(result["verdict"], verification.VERDICT_RED)
        attempt = result["attempts"][0]
        self.assertTrue(attempt["timed_out"])
        self.assertFalse(verification._pgid_alive(attempt.get("pgid")))
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(self.latest(result["transaction_id"],
                                     "modules")["attempt_state"],
                         "not_reached")

    def test_a_component_mutating_the_live_tree_stops_the_run(self):
        repo = self.make_repo()
        # The component's test writes into the LIVE repo by absolute path,
        # which the per-command snapshot checkout cannot isolate.
        live_file = os.path.join(repo, "suite", "written.txt")
        _write(repo, SPLIT, SPLIT_SOURCE.format(
            alpha_extra=MUTATE_EXTRA.format(path=live_file),
            helper_expected="2"))
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "bake path")
        raw = [_component("split-alpha", [SPLIT + "::Alpha*"]),
               _component("modules", ["suite/test_[ab]*.py"]),
               _component("split-rest", [SPLIT + "::[!A]*"])]
        result = self.run_suite(repo, raw)
        self.assertEqual(result["verdict"], verification.VERDICT_RED)
        self.assertIsNotNone(result["mutation"])
        self.assertTrue(os.path.exists(live_file))
        self.assertEqual(self.latest(result["transaction_id"],
                                     "modules")["attempt_state"],
                         "not_reached")

    def test_zero_unstated_and_mismatched_counts_are_red(self):
        repo = self.make_repo()
        cases = [
            ("zero", _components(command=["python3", "suite/run_none.py"])),
            ("unstated",
             _components(command=["python3", "suite/run_quiet.py"])),
            ("mismatch", _components(expected_test_count=5)),
        ]
        for check, raw in cases:
            with self.subTest(check=check):
                self.session_uuid = "S-" + uuid.uuid4().hex[:8]
                result = self.run_suite(repo, raw)
                self.assertEqual(result["attempts"][0]["exit_code"], 0)
                self.assertEqual(result["attempts"][0]["test_count_check"],
                                 check)
                self._assert_stopped_after_first(result, reason_check=check)
                self.assertIn(
                    "executed-test count %s" % check,
                    cowork._owned_transaction_reason(result))
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        paired = self.run_suite(repo, _components(expected_test_count=2))
        self.assertEqual(paired["verdict"], verification.VERDICT_GREEN)


class DeferredReconcileTests(_TransactionFixture):

    def _pending_copy(self, repo):
        result = self.run_suite(repo, _components())
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        txn = result["transaction_id"]
        path = state_store.verification_result_path_for(self.session_uuid,
                                                        txn)
        stored = state_store.read_json_tolerant(path)
        stored["attempts"][-1] = {
            "label": stored["attempts"][-1]["label"],
            "kind": verification.KIND_FINAL_SUITE_COMPONENT,
            "ledger_attempt_id": stored["attempts"][-1]["ledger_attempt_id"],
            "evidence_state": verification.EVIDENCE_UNRESOLVED,
            "exit_code": None}
        stored["verdict"] = verification.VERDICT_UNVERIFIED
        stored["final_suite_binding"] = "not_reached"
        stored["deferred_reconciliation"] = {
            "state": "pending", "transaction_id": txn,
            "still_pending": [], "deadline_hit": False}
        self.assertTrue(state_store.write_json_atomic(path, stored))
        return txn, stored

    def test_reconciles_green_from_its_own_evidence(self):
        repo = self.make_repo()
        _txn, stored = self._pending_copy(repo)
        result, outcome = verification.reconcile_deferred_transaction(
            repo, self.session_uuid, stored)
        self.assertEqual(outcome, "reconciled")
        self.assertEqual(result["verdict"], verification.VERDICT_GREEN)
        self.assertEqual(result["final_suite_binding"],
                         verification.FINAL_SUITE_COMPONENTS_RAN_ONCE)
        self.assertEqual(result["attempts"][-1]["test_count_check"], "ok")

    def test_a_zero_count_terminal_event_reconciles_red(self):
        repo = self.make_repo()
        txn, stored = self._pending_copy(repo)
        state_store.append_jsonl_atomic(
            state_store.verification_attempt_events_path_for(
                self.session_uuid, txn),
            {"event": "terminal", "label": stored["attempts"][-1]["label"],
             "exit_code": 0, "stdout": "",
             "stderr": "Ran 0 tests in 0.000s\n"})
        result, _outcome = verification.reconcile_deferred_transaction(
            repo, self.session_uuid, stored)
        self.assertEqual(result["verdict"], verification.VERDICT_RED)
        self.assertEqual(result["attempts"][-1]["test_count_check"], "zero")

    def test_a_tampered_suite_record_reconciles_unverified(self):
        repo = self.make_repo()
        txn, stored = self._pending_copy(repo)
        request_path = state_store.verification_request_path_for(
            self.session_uuid, txn)
        request = state_store.read_json_tolerant(request_path)
        request["suite"]["universe_digest"] = "0" * 64
        self.assertTrue(state_store.write_json_atomic(request_path, request))
        result, _outcome = verification.reconcile_deferred_transaction(
            repo, self.session_uuid, stored)
        self.assertEqual(result["verdict"], verification.VERDICT_UNVERIFIED)
        self.assertEqual(result["deferred_reconciliation"]["note"],
                         "suite_proof_mismatch")


# --------------------------------------------------------------------------- #
# Reviewer surfaces and gate plumbing                                         #
# --------------------------------------------------------------------------- #


BASE_OVERLAY_KEYS = {"txn_id", "manifest_digest", "index_digest", "verdict",
                     "final_suite_label", "final_suite_binding",
                     "command_count", "disposition", "contradiction"}


def _suite_record():
    files, read_bytes = _manifest(_neutral_sources())
    record, _resolved = verification.prove_suite_partition(
        _declaration(), _components(), files, read_bytes)
    return record


def _result(schema3=True):
    attempts = [{"label": "compile", "kind": verification.KIND_BASELINE,
                 "exit_code": 0, "evidence_state": "present",
                 "wall_time_s": 0.2}]
    result = {
        "transaction_id": "T-" + uuid.uuid4().hex[:8],
        "request_key": "k1",
        "verdict": verification.VERDICT_GREEN,
        "mutation": None,
        "worker_identity_verified": True,
        "reused_lock_result": False,
        "snapshot": {"manifest_digest": "ab" * 32, "index_digest": "cd" * 32},
        "created_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:10Z",
    }
    if schema3:
        for label in ("modules", "split-alpha", "split-rest"):
            attempts.append({
                "label": label,
                "kind": verification.KIND_FINAL_SUITE_COMPONENT,
                "exit_code": 0, "evidence_state": "present",
                "wall_time_s": 1.0, "observed_test_count": 2,
                "test_count_check": "ok"})
        result.update(final_suite_label=SUITE_ID,
                      final_suite_binding="components_ran_once",
                      suite=_suite_record())
    else:
        attempts.append({"label": "final", "kind":
                         verification.KIND_FINAL_SUITE, "exit_code": 0,
                         "evidence_state": "present", "wall_time_s": 1.0})
        result.update(final_suite_label="final",
                      final_suite_binding="ran_once")
    result["attempts"] = attempts
    return result


class ReviewerSurfaceTests(_SessionRootMixin, unittest.TestCase):

    def setUp(self):
        self.isolate_sessions_root()

    def _bind(self, result):
        receipt = state_store.verification_result_path_for(
            self.session_uuid, result["transaction_id"])
        self.assertTrue(state_store.write_json_atomic(receipt, result))
        assets = state_store.session_assets_dir(self.session_uuid)
        os.makedirs(assets, exist_ok=True)
        status = os.path.join(assets, "builder.status.json")
        with open(status, "w") as fh:
            json.dump({"status": "ready_for_review", "result": {
                "verification": [{"label": "compile", "ok": True,
                                  "source_manifest": "ab" * 32}]}}, fh)
        summary = os.path.join(assets, "builder.summary.md")
        with open(summary, "w") as fh:
            fh.write("# build summary\n")
        pointer = cowork._update_receipt_pointer_for_readiness(
            self.session_uuid, "builder", 1, None, result,
            {"state": "verified", "transaction_id": result["transaction_id"]},
            status, summary_path=summary)
        self.assertIsInstance(pointer, dict)
        plan_json = os.path.join(assets, "planner.plan.json")
        plan_md = os.path.join(assets, "planner.plan.md")
        with open(plan_json, "w") as fh:
            fh.write("{}")
        with open(plan_md, "w") as fh:
            fh.write("# plan\n")
        return pointer, receipt, status, plan_json, plan_md

    def test_composed_suite_facts_reach_every_reviewer_surface(self):
        result = _result()
        pointer, receipt, status, plan_json, plan_md = self._bind(result)
        self.assertEqual(pointer["final_suite_universe"]["tests_dir"],
                         "suite")
        self.assertEqual(pointer["final_suite_universe"]["split_modules"],
                         [SPLIT])
        self.assertEqual(pointer["final_suite_universe_digest"],
                         result["suite"]["universe_digest"])
        self.assertEqual(pointer["final_suite_member_count"], 6)
        self.assertEqual(pointer["final_suite_component_count"], 3)
        overlay, _pointer = cowork._current_verification_overlay(
            self.session_uuid)
        self.assertEqual(overlay["final_suite_binding"],
                         "components_ran_once")
        self.assertEqual(overlay["suite_member_count"], 6)
        self.assertEqual(overlay["suite_component_count"], 3)
        self.assertEqual(set(overlay), BASE_OVERLAY_KEYS | {
            "suite_universe_digest", "suite_member_count",
            "suite_component_count"})
        for edge in ("builder->build-reviewer:review_ctx",
                     "builder->build-reviewer:review_resume"):
            with self.subTest(edge=edge):
                handoff._assert_content_free(
                    edge, overlay, handoff.EDGES[edge]["facts"])
        fresh = cowork.assemble_build_reviewer_context(
            "goal", ["builder"], plan_json, plan_md, status,
            verification_receipt_path=receipt, verification_overlay=overlay)
        resumed = cowork.assemble_build_reviewer_resume_context(
            plan_json, plan_md, status, verification_receipt_path=receipt,
            verification_overlay=overlay)
        for text in (fresh, resumed):
            self.assertIn("composed suite: components=3 members=6", text)
        block = cowork.render_verification_overlay_block(overlay)
        self.assertIn("composed suite: components=3 members=6", block)

    def test_schema2_overlay_keeps_exactly_its_keys(self):
        self._bind(_result(schema3=False))
        overlay, pointer = cowork._current_verification_overlay(
            self.session_uuid)
        self.assertEqual(set(overlay), BASE_OVERLAY_KEYS)
        self.assertNotIn("final_suite_universe", pointer)
        self.assertNotIn("composed suite",
                         cowork.render_verification_overlay_block(overlay))

    def test_suite_facts_have_closed_schemas(self):
        edge = "builder->build-reviewer:review_ctx"
        allowed = handoff.EDGES[edge]["facts"]
        for key, value in (("suite_universe_digest", "not-hex"),
                           ("suite_member_count", -1),
                           ("suite_component_count", True),
                           ("final_suite_binding", "ran_twice")):
            with self.subTest(key=key):
                with self.assertRaises(handoff.ContentFreeError):
                    handoff._assert_content_free(edge, {key: value}, allowed)

    def test_report_and_measure_show_the_composed_suite(self):
        result = _result()
        cost = measure.owned_transaction_cost_summary(result)
        self.assertEqual(cost["initial_attempt_count"], 4)
        self.assertEqual(cost["focused_attempt_count"], 0)
        self.assertEqual(cost["final_suite_component_count"], 3)
        self.assertEqual(cost["final_suite_member_count"], 6)
        self.assertEqual(cost["final_suite_universe_digest"],
                         result["suite"]["universe_digest"])
        lines = report._section_owned_verification(
            {"owned_verification": {"latest": result, "cost": cost}})
        text = "\n".join(lines)
        self.assertIn("composed suite %s: components=3 members=6" % SUITE_ID,
                      text)
        self.assertIn("universe tests_dir=suite include=suite/test_*.py",
                      text)
        plain = _result(schema3=False)
        plain_cost = measure.owned_transaction_cost_summary(plain)
        self.assertNotIn("final_suite_component_count", plain_cost)
        plain_text = "\n".join(report._section_owned_verification(
            {"owned_verification": {"latest": plain, "cost": plain_cost}}))
        self.assertNotIn("composed suite", plain_text)


class GatePlumbingTests(_SessionRootMixin, unittest.TestCase):

    def setUp(self):
        self.isolate_sessions_root()
        self.calls = []

    def _plan(self, result_fields):
        assets = state_store.session_assets_dir(self.session_uuid)
        os.makedirs(assets, exist_ok=True)
        with open(os.path.join(assets, "planner.plan.json"), "w") as fh:
            json.dump({"result": result_fields}, fh)

    def _fake(self, repo, session_uuid, raw, **kwargs):
        self.calls.append(((repo, session_uuid, raw), kwargs))
        return {"transaction_id": "T-1", "request_key": "k",
                "verdict": verification.VERDICT_GREEN}

    def _gate(self):
        return cowork._run_owned_verification_transaction(
            self.session_uuid, "builder", 1, RecordingTrace(), repo="/repo",
            run_transaction_fn=self._fake, work_id="W-1")

    def _schema2(self):
        return [_baseline(), dict(_baseline("final"),
                                  kind=verification.KIND_FINAL_SUITE)]

    def test_schema3_plans_forward_the_declaration(self):
        raw = [_baseline()] + _components()
        self._plan({"verification_schema": 3, "verification": raw,
                    "verification_suite": _declaration()})
        result, reason = self._gate()
        self.assertIsNone(reason)
        self.assertEqual(result["transaction_id"], "T-1")
        (_args, kwargs), = self.calls
        self.assertEqual(kwargs, {"work_id": "W-1", "declared_schema": 3,
                                  "suite": _declaration()})
        schema, _entries, label = cowork._plan_inventory(self.session_uuid)
        self.assertEqual((schema, label), (verification.SCHEMA_3, SUITE_ID))

    def test_schema2_plans_call_exactly_as_before(self):
        raw = self._schema2()
        for extra in ({}, {"verification_suite": _declaration()}):
            with self.subTest(stray_suite=bool(extra)):
                self.calls = []
                self._plan(dict({"verification_schema": 2,
                                 "verification": raw}, **extra))
                _result_obj, reason = self._gate()
                self.assertIsNone(reason)
                (args, kwargs), = self.calls
                self.assertEqual(args, ("/repo", self.session_uuid, raw))
                self.assertEqual(kwargs, {"work_id": "W-1"})

    def test_defective_schema3_plans_are_rejected_before_running(self):
        for raw, decl, code in (
                ([_baseline()], _declaration(), "declared_schema_mismatch"),
                (_components(), None, "missing_suite_declaration"),
                (_components(max_duration_s=900), _declaration(),
                 "component_timeout_exceeds_policy")):
            with self.subTest(code=code):
                self.calls = []
                fields = {"verification_schema": 3, "verification": raw}
                if decl is not None:
                    fields["verification_suite"] = decl
                self._plan(fields)
                result, reason = self._gate()
                self.assertIsNone(result)
                self.assertIn("invalid (%s)" % code, reason)
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
