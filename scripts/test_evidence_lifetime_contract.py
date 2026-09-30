#!/usr/bin/env python3
"""Evidence-lifetime contract: delivery evidence must not become product tests.

A permanent test protects behavior expected of every future revision on
neutral inputs. Evidence about one delivery belongs to the session or package
directory, never to product source or Git. This module is the repository-owned
recurrence check for that rule. It scans the tracked, non-hidden product tree
for representative shapes of each forbidden category, proves its own rules on
neutral positive and negative examples built at runtime in temporary roots,
runs a throwaway copy of the repository with one injected artifact through a
child interpreter to show the check fails for the intended reason, and asserts
that the agent notes, the four planning and building role contracts and the
two orchestration skills carry the same rule.

Forbidden categories and the shape each rule detects:

- package receipt: a file whose name marks it as a receipt or run result, or a
  JSON object shaped like a run-result line or a verification receipt;
- audit: a file whose name marks it as an audit;
- candidate/base ancestry pin: a forty-hex value next to an ancestry word in
  test-module prose or fixture text, a pin-named JSON key in a fixture, or a
  pin-named assignment holding a forty-hex constant in test code;
- scope snapshot: prose claiming that only a listed set of paths differs from
  a base, or test code that lists git changes of this repository (rather than
  a throwaway repository) for comparison;
- gate transcript/count: a checked-in unittest log or harness summary (by name
  or JSON shape), prose claiming that a suite ran a number of tests and passed,
  or test code that pins the collected size of this repository's own suite to
  a literal;
- historical implementation-state assertion: prose asserting what was true of
  one candidate at delivery (retired wordings about a pinned base, a delta
  against the delivered tree, a candidate-relative view or a pinned
  capability), or a pin-named fixture file.

Legitimate categories and why each passes:

- version control operations in throwaway repositories: git calls whose
  working directory is a temporary root are never matched;
- controlled fixture values (synthetic ids, digests, timestamps, placeholder
  paths, a bare forty-hex constant with no ancestry context, a count over a
  suite built locally in the test): no rule fires without the ancestry,
  scope, gate or history context described above;
- security negative tests: a refused launch is not evidence about a delivery;
- compatibility inputs (real transcript or log shapes fed to code under test):
  phrase rules read only docstrings and comments of test modules, never code
  string literals, and measurement inputs are exempt as documented below;
- regression references (issue numbers, package labels in prose, observed
  field values used as inputs): none is a pattern;
- product fields (expected_test_count, receipt and transaction schemas used by
  the product): a shape rule needs the full artifact key set, and a product
  field name alone is never a finding.

Limits, stated plainly:

- The rules detect representative shapes only. This module performs no
  semantic classification of arbitrary assertions; a history-bound claim
  worded outside the patterns passes, and build review remains the backstop.
- Files under the measurement fixture directory are documented authored inputs
  and are exempt from every fixture-side rule except the name rule; a receipt
  placed there under a neutral name would pass.
- Real personal paths are not detected; a rule for them would need a personal
  allowlist, which this module must not carry.
- Numbers inside fixtures are not classified on their own.
- Code string literals in test modules are never matched by phrase rules.
- Hidden names are pruned at every level of the walk, so hidden directories
  other than the two generated-state directories checked explicitly are not
  scanned.
- A file that cannot be decoded as UTF-8, parsed as JSON or JSON lines, parsed
  as Python or tokenized is skipped by the rule that needed the parse; a
  decoding or parse failure is never a finding.
- Tuning narrows a rule; it never allowlists a path or file name.

Run through the offline harness with this module's name as the explicit id.
"""

import ast
import collections
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork  # noqa: E402

REPO_ROOT = cowork.SKILL_ROOT
SELF_PATH = os.path.realpath(__file__)
MODULE_NAME = os.path.splitext(os.path.basename(__file__))[0]

CATEGORY_RECEIPT = "package receipt"
CATEGORY_AUDIT = "audit"
CATEGORY_ANCESTRY = "candidate/base ancestry pin"
CATEGORY_SCOPE = "scope snapshot"
CATEGORY_GATE = "gate transcript/count"
CATEGORY_HISTORY = "historical implementation-state assertion"
FORBIDDEN_CATEGORIES = (CATEGORY_RECEIPT, CATEGORY_AUDIT, CATEGORY_ANCESTRY,
                        CATEGORY_SCOPE, CATEGORY_GATE, CATEGORY_HISTORY)
LEGITIMATE_CATEGORIES = ("version control", "controlled fixture",
                         "security negative", "compatibility input",
                         "regression reference", "product field")

CONTRACT_FILES = (
    "AGENTS.md",
    os.path.join("roles", "planner.md"),
    os.path.join("roles", "planning-advisor.md"),
    os.path.join("roles", "builder.md"),
    os.path.join("roles", "build-reviewer.md"),
    os.path.join("skills", "cowork-orchestrate", "SKILL.md"),
    os.path.join("skills", "cowork-refactor-orchestrator", "SKILL.md"),
)
REQUIRED_TOKENS = (
    "future revision",
    "product source",
    "package receipt",
    "audits",
    "ancestry pin",
    "scope snapshot",
    "gate transcript",
    "implementation-state assertion",
    "mixed check",
    "neutral input",
    "non-historical",
    "version control",
    "controlled fixture",
    "security negative",
    "compatibility input",
    "regression reference",
    "product field",
    "keyword alone",
)
EDITED_SKILLS = ("cowork-orchestrate", "cowork-refactor-orchestrator")
SKILL_FRONTMATTER_KEYS = frozenset(
    ("name", "description", "license", "allowed-tools", "metadata"))
MEASUREMENT_INPUTS = os.path.join("scripts", "fixtures", "measurement")

Finding = collections.namedtuple("Finding", "category path detail")

HEX40 = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
ANCESTRY_WORDS = re.compile(
    r"\b(base|candidate|frozen|ancestor|ancestry)\b", re.IGNORECASE)
PIN_KEYS = frozenset(("base_commit", "candidate_commit", "base_tree",
                      "candidate_tree", "frozen_base", "base_sha",
                      "candidate_sha"))
PIN_NAME = re.compile(r"^(base|candidate|frozen)_?(commit|sha|tree|rev)$",
                      re.IGNORECASE)
SCOPE_PROSE = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bonly\s+\w+\s+(paths|files)\s+differ\b",
    r"\bevery\s+other\s+file\s+(is\s+)?identical\b",
    r"\bno\s+other\s+(paths?|files?)\s+(changed|differs?)\b",
))
GIT_LISTING = frozenset(("diff", "ls-files", "status", "log", "rev-parse"))
SUBPROCESS_CALLS = frozenset(
    ("run", "check_output", "check_call", "call", "Popen"))
REPO_NAMES = frozenset(("SKILL_ROOT", "REPO_ROOT", "__file__"))
GATE_RAN = re.compile(r"\bran\s+\d+\s+tests?\b", re.IGNORECASE)
GATE_VERDICT = re.compile(r"\b(ok|green|passed)\b", re.IGNORECASE)
GATE_SUITE = re.compile(
    r"\b(full|complete|whole)\s+(suite|regression)\b", re.IGNORECASE)
GATE_SUITE_CLAIM = re.compile(r"\b(ran|passed|is\s+green)\b", re.IGNORECASE)
GATE_COUNT = re.compile(r"\b\d+\s+tests?\b", re.IGNORECASE)
GATE_ASSERTIONS = frozenset(
    ("assertEqual", "assertGreaterEqual", "assertTrue"))
REPO_LOADERS = frozenset(
    ("discover", "loadTestsFromName", "loadTestsFromModule"))
HISTORY_PROSE = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bfrozen\s+base\b",
    r"\brebaselined\s+(onto|to|against)\b",
    r"\bproduction\s+delta\b",
    r"\bidentical\s+to\s+(that|the)\s+base\b",
    r"\bfrom\s+inside\s+the\s+candidate\b",
    r"\bas\s+of\s+this\s+(package|delivery|candidate)\b",
    r"\bat\s+delivery\s+time\b",
    r"\bcapability\s+pin\b",
))
HISTORY_PRECOMMIT = re.compile(r"\bpre-commit\b", re.IGNORECASE)
HISTORY_CANDIDATE = re.compile(r"\bcandidate\b", re.IGNORECASE)
RUN_RESULT_KEYS = frozenset(("cowork_result", "rc", "outcome"))
VERIFICATION_RECEIPT_KEYS = frozenset(
    ("transaction_id", "verdict", "final_suite_binding"))
HARNESS_SUMMARY_KEYS = frozenset(("gate", "exit_code", "sentinel_entries"))


# --------------------------------------------------------------------------- #
# Walk and scope
# --------------------------------------------------------------------------- #

def enumerate_product_source(root):
    """Yield repository-relative posix paths of every non-hidden regular file
    under ``root``, pruning hidden names and bytecode caches at every level
    and skipping this module by real path."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            if os.path.realpath(path) == SELF_PATH:
                continue
            yield os.path.relpath(path, root).replace(os.sep, "/")


def is_test_module(rel):
    base = rel.rsplit("/", 1)[-1]
    return (rel.startswith("scripts/") and base.startswith("test_")
            and base.endswith(".py"))


def is_fixture_or_data(rel):
    return ((rel.startswith("scripts/fixtures/")
             or rel.startswith("scripts/data/"))
            and not rel.endswith(".py"))


def is_measurement_input(rel):
    return rel.startswith(MEASUREMENT_INPUTS.replace(os.sep, "/") + "/")


def is_artifact_candidate(rel):
    if "/" not in rel:
        return True
    return rel.startswith("scripts/") and not rel.endswith(".py")


# --------------------------------------------------------------------------- #
# Readers (a failure to read or parse is never a finding)
# --------------------------------------------------------------------------- #

def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (UnicodeDecodeError, OSError):
        return None


def load_json_head(text):
    """The JSON object in ``text`` or on its first non-empty line, else None."""
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    for candidate in (text, first):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_python(source):
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def prose_of_python(source, tree=None):
    """``[(lineno, text)]`` for every docstring line and comment in ``source``."""
    tree = tree if tree is not None else parse_python(source)
    if tree is None:
        return []
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc or not node.body:
            continue
        start = node.body[0].lineno
        for offset, text in enumerate(doc.splitlines()):
            lines.append((start + offset, text))
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                lines.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return lines


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def receipt_or_audit_findings(rel, text, measurement):
    """Name and JSON-shape rules for artifact candidates."""
    findings = []
    base = rel.rsplit("/", 1)[-1].lower()
    stem = os.path.splitext(base)[0]
    if "receipt" in base or "run-result" in base or "run_result" in base:
        findings.append(Finding(CATEGORY_RECEIPT, rel,
                                "file name marks a receipt or run result"))
    if "audit" in base:
        findings.append(Finding(CATEGORY_AUDIT, rel,
                                "file name marks an audit"))
    if rel.startswith("scripts/") and (stem.endswith("-pin")
                                       or stem.endswith("_pin")):
        findings.append(Finding(CATEGORY_HISTORY, rel,
                                "file name marks a pin"))
    if base in ("unittest.log", "summary.json") or (
            rel.startswith("scripts/") and base.endswith(".log")
            and not measurement):
        findings.append(Finding(CATEGORY_GATE, rel,
                                "file name marks a test log or summary"))
    if measurement or text is None:
        return findings
    head = load_json_head(text)
    if head is None:
        return findings
    keys = set(head)
    if RUN_RESULT_KEYS <= keys:
        findings.append(Finding(CATEGORY_RECEIPT, rel,
                                "JSON shaped like a run-result line"))
    if VERIFICATION_RECEIPT_KEYS <= keys:
        findings.append(Finding(CATEGORY_RECEIPT, rel,
                                "JSON shaped like a verification receipt"))
    if HARNESS_SUMMARY_KEYS <= keys:
        findings.append(Finding(CATEGORY_GATE, rel,
                                "JSON shaped like a harness summary"))
    return findings


def _prose_findings(rel, lines, where):
    """Ancestry, scope, gate and history phrase rules over ``(lineno, text)``."""
    findings = []
    for lineno, text in lines:
        at = "%s line %d" % (where, lineno)
        if HEX40.search(text) and ANCESTRY_WORDS.search(text):
            findings.append(Finding(CATEGORY_ANCESTRY, rel,
                                    at + ": forty-hex value next to an "
                                    "ancestry word"))
        if any(p.search(text) for p in SCOPE_PROSE):
            findings.append(Finding(CATEGORY_SCOPE, rel,
                                    at + ": claims only listed paths differ"))
        if GATE_RAN.search(text) and GATE_VERDICT.search(text):
            findings.append(Finding(CATEGORY_GATE, rel,
                                    at + ": claims a test run and its "
                                    "verdict"))
        elif (GATE_SUITE.search(text) and GATE_SUITE_CLAIM.search(text)
              and GATE_COUNT.search(text)):
            findings.append(Finding(CATEGORY_GATE, rel,
                                    at + ": claims a full-suite result with "
                                    "a test count"))
        if any(p.search(text) for p in HISTORY_PROSE) or (
                HISTORY_PRECOMMIT.search(text)
                and HISTORY_CANDIDATE.search(text)):
            findings.append(Finding(CATEGORY_HISTORY, rel,
                                    at + ": asserts one candidate's "
                                    "delivery-time state"))
    return findings


def _references_repo(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in REPO_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in REPO_NAMES:
            return True
    return False


def _string_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _int_value(node):
    if (isinstance(node, ast.Constant) and isinstance(node.value, int)
            and not isinstance(node.value, bool)):
        return node.value
    return None


def _call_name(node):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
    return None


def ancestry_code_findings(rel, tree):
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not PIN_NAME.match(target.id):
            continue
        value = _string_value(node.value)
        if value is not None and HEX40.fullmatch(value):
            findings.append(Finding(
                CATEGORY_ANCESTRY, rel,
                "line %d: pin-named assignment holds a forty-hex constant"
                % node.lineno))
    return findings


def scope_code_findings(rel, tree):
    findings = []
    for node in ast.walk(tree):
        if _call_name(node) not in SUBPROCESS_CALLS or not node.args:
            continue
        argv = node.args[0]
        if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
            continue
        if _string_value(argv.elts[0]) != "git":
            continue
        if not any(_string_value(e) in GIT_LISTING for e in argv.elts):
            continue
        targets_repo = any(
            kw.arg == "cwd" and _references_repo(kw.value)
            for kw in node.keywords)
        for index, elt in enumerate(argv.elts[:-1]):
            if _string_value(elt) == "-C" and _references_repo(
                    argv.elts[index + 1]):
                targets_repo = True
        if targets_repo:
            findings.append(Finding(
                CATEGORY_SCOPE, rel,
                "line %d: lists git changes of this repository" % node.lineno))
    return findings


def _repo_loader_call(node, assigned):
    """True when ``node`` is (or names) a suite collected from the repository."""
    if isinstance(node, ast.Name) and node.id in assigned:
        node = assigned[node.id]
    if _call_name(node) in REPO_LOADERS:
        return any(_references_repo(arg) for arg in node.args) or any(
            _references_repo(kw.value) for kw in node.keywords)
    return False


def _suite_count_expr(node, assigned):
    if _call_name(node) == "countTestCases" and isinstance(node.func,
                                                          ast.Attribute):
        return _repo_loader_call(node.func.value, assigned)
    if _call_name(node) == "len" and len(node.args) == 1:
        return _repo_loader_call(node.args[0], assigned)
    return False


def gate_code_findings(rel, tree):
    findings = []
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned = {}
        for node in ast.walk(scope):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                assigned[node.targets[0].id] = node.value
        for node in ast.walk(scope):
            name = _call_name(node)
            if name not in GATE_ASSERTIONS:
                continue
            pairs = []
            if name == "assertTrue" and node.args and isinstance(
                    node.args[0], ast.Compare):
                compare = node.args[0]
                pairs.append((compare.left, compare.comparators[0]))
            elif name != "assertTrue" and len(node.args) >= 2:
                pairs.append((node.args[0], node.args[1]))
            for left, right in pairs:
                for literal, counted in ((left, right), (right, left)):
                    if _int_value(literal) is not None and _suite_count_expr(
                            counted, assigned):
                        findings.append(Finding(
                            CATEGORY_GATE, rel,
                            "line %d: pins the collected size of this "
                            "repository's suite to a literal" % node.lineno))
                        break
    return findings


def test_module_findings(rel, text):
    tree = parse_python(text)
    if tree is None:
        return []
    findings = _prose_findings(rel, prose_of_python(text, tree), "prose")
    findings += ancestry_code_findings(rel, tree)
    findings += scope_code_findings(rel, tree)
    findings += gate_code_findings(rel, tree)
    return findings


def fixture_findings(rel, text):
    lines = list(enumerate(text.splitlines(), 1))
    findings = _prose_findings(rel, lines, "text")
    head = load_json_head(text)
    if head is not None:
        pins = sorted(PIN_KEYS & set(head))
        if pins:
            findings.append(Finding(CATEGORY_ANCESTRY, rel,
                                    "JSON key pins an ancestor: "
                                    + ", ".join(pins)))
    return findings


def classify_file(root, rel):
    path = os.path.join(root, *rel.split("/"))
    measurement = is_measurement_input(rel)
    artifact = is_artifact_candidate(rel)
    module = is_test_module(rel)
    fixture = is_fixture_or_data(rel) and not measurement
    if not (artifact or module or fixture):
        return []
    text = read_text(path)
    findings = []
    if artifact:
        findings += receipt_or_audit_findings(rel, text, measurement)
    if text is None:
        return findings
    if module:
        findings += test_module_findings(rel, text)
    if fixture:
        findings += fixture_findings(rel, text)
    return findings


def scan_repository(root=None):
    root = REPO_ROOT if root is None else root
    findings = []
    stats = collections.Counter()
    for rel in enumerate_product_source(root):
        stats["files"] += 1
        if is_test_module(rel):
            stats["test_modules"] += 1
        if is_fixture_or_data(rel):
            stats["fixtures"] += 1
        if "/" not in rel:
            stats["root_files"] += 1
        if rel.startswith("roles/"):
            stats["roles_files"] += 1
        findings += classify_file(root, rel)
    return findings, stats


def format_findings(findings):
    return "\n".join("evidence-lifetime: %s: %s: %s" % f for f in findings)


# --------------------------------------------------------------------------- #
# Helpers for example roots
# --------------------------------------------------------------------------- #

def _read_repo(rel):
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _join_words(*words):
    return " ".join(words)


class _ExampleRootMixin(object):
    def make_root(self):
        root = tempfile.mkdtemp(prefix="evidence-lifetime-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "scripts", "fixtures"))
        self.write(root, ".gitignore", ".cowork\n.plans\n")
        return root

    def write(self, root, rel, text):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return rel

    def module_path(self, stem):
        return "scripts/" + "test_" + stem + ".py"

    def assert_finding(self, findings, category, rel):
        hits = [f for f in findings if f.category == category and f.path == rel]
        self.assertTrue(hits, "no %r finding for %s in:\n%s"
                        % (category, rel, format_findings(findings)))

    def assert_clean(self, root):
        findings, _ = scan_repository(root)
        self.assertEqual(findings, [], "\n" + format_findings(findings))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

class RepositoryScanTest(unittest.TestCase):
    """Positive proof over the live tree."""

    def test_current_repository_has_no_delivery_evidence(self):
        findings, _ = scan_repository()
        self.assertEqual(findings, [], "\n" + format_findings(findings))

    def test_scan_discovers_product_source(self):
        _, stats = scan_repository()
        self.assertGreaterEqual(stats["test_modules"], 1)
        self.assertGreaterEqual(stats["fixtures"], 1)
        self.assertGreaterEqual(stats["roles_files"], 1)
        self.assertGreaterEqual(stats["root_files"], 1)


class GeneratedStateTest(unittest.TestCase):
    """Generated state stays ignored and untracked."""

    def test_gitignore_lists_generated_directories(self):
        entries = set()
        for line in _read_repo(".gitignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.add(line.rstrip("/"))
        self.assertIn(".cowork", entries)
        self.assertIn(".plans", entries)

    def test_generated_directories_are_untracked(self):
        if not os.path.exists(os.path.join(REPO_ROOT, ".git")):
            self.skipTest("no .git in this tree")
        env = dict(os.environ)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        proc = subprocess.run(
            ["git", "ls-files", "--", ".cowork", ".plans"], cwd=REPO_ROOT,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class ForbiddenExampleTest(_ExampleRootMixin, unittest.TestCase):
    """Every forbidden category is rejected on a neutral example composed at
    runtime; no trigger phrase appears in this module."""

    def setUp(self):
        self.root = self.make_root()
        self.hexval = hashlib.sha1(b"neutral example").hexdigest()

    def scan(self):
        findings, _ = scan_repository(self.root)
        return findings

    def test_run_result_shaped_json_is_a_package_receipt(self):
        rel = self.write(self.root, "scripts/fixtures/delivery.json",
                         json.dumps({"cowork_result": True, "rc": 0,
                                     "outcome": "approved"}))
        self.assert_finding(self.scan(), CATEGORY_RECEIPT, rel)

    def test_receipt_and_audit_names_are_findings(self):
        receipt = self.write(self.root, "scripts/notes-receipt.md", "notes\n")
        audit = self.write(self.root, "scripts/exit-audit.md", "notes\n")
        findings = self.scan()
        self.assert_finding(findings, CATEGORY_RECEIPT, receipt)
        self.assert_finding(findings, CATEGORY_AUDIT, audit)

    def test_verification_receipt_shape_is_a_package_receipt(self):
        rel = self.write(self.root, "scripts/fixtures/verify.json",
                         json.dumps({"transaction_id": "T-example",
                                     "verdict": "green",
                                     "final_suite_binding": "ran_once"}))
        self.assert_finding(self.scan(), CATEGORY_RECEIPT, rel)

    def test_ancestry_pin_in_docstring_fixture_and_code(self):
        prose = self.write(self.root, self.module_path("prose_pin"),
                           '"""Pinned to the %s %s."""\n'
                           % (_join_words("frozen", "base"), self.hexval))
        fixture = self.write(self.root, "scripts/fixtures/pins.json",
                             json.dumps({"base" + "_commit": self.hexval}))
        code = self.write(self.root, self.module_path("code_pin"),
                          "BASE_COMMIT = %r\n" % self.hexval)
        findings = self.scan()
        self.assert_finding(findings, CATEGORY_ANCESTRY, prose)
        self.assert_finding(findings, CATEGORY_ANCESTRY, fixture)
        self.assert_finding(findings, CATEGORY_ANCESTRY, code)

    def test_scope_snapshot_prose_and_git_listing(self):
        claim = _join_words("Only", "these", "paths", "differ", "from",
                            "the", "base")
        prose = self.write(self.root, self.module_path("scope_prose"),
                           '"""%s:\n\n  scripts/a.py\n  scripts/b.py\n"""\n'
                           % claim)
        code = self.write(self.root, self.module_path("scope_code"), "\n".join((
            "import subprocess",
            "import unittest",
            "import cowork",
            "",
            "",
            "class ScopeTest(unittest.TestCase):",
            "    def test_only_these_paths(self):",
            "        out = subprocess.run(['git', 'diff', '--name-only'],",
            "                             cwd=cowork.SKILL_ROOT,",
            "                             stdout=subprocess.PIPE).stdout",
            "        self.assertEqual(out.split(), [b'scripts/a.py'])",
            "")))
        findings = self.scan()
        self.assert_finding(findings, CATEGORY_SCOPE, prose)
        self.assert_finding(findings, CATEGORY_SCOPE, code)

    def test_gate_transcript_artifacts_and_count_claims(self):
        log = self.write(self.root, "scripts/unittest.log", "log text\n")
        summary = self.write(self.root, "scripts/summary.json",
                             json.dumps({"gate": "pass", "exit_code": 0,
                                         "sentinel_entries": 0}))
        claim = _join_words("Ran", "412", "tests", "in", "2.0s") + \
            " -- OK on this candidate."
        prose = self.write(self.root, self.module_path("gate_prose"),
                           '"""%s"""\n' % claim)
        code = self.write(self.root, self.module_path("gate_code"), "\n".join((
            "import os",
            "import unittest",
            "import cowork",
            "",
            "",
            "class CountTest(unittest.TestCase):",
            "    def test_count(self):",
            "        suite = unittest.defaultTestLoader.discover(",
            "            os.path.dirname(cowork.__file__))",
            "        self.assertEqual(suite.countTestCases(), 412)",
            "")))
        findings = self.scan()
        self.assert_finding(findings, CATEGORY_GATE, log)
        self.assert_finding(findings, CATEGORY_GATE, summary)
        self.assert_finding(findings, CATEGORY_GATE, prose)
        self.assert_finding(findings, CATEGORY_GATE, code)

    def test_historical_state_prose_and_pin_fixture(self):
        claim = _join_words("Zero", "production", "delta", "as", "of",
                            "this", "package")
        prose = self.write(self.root, self.module_path("history_prose"),
                           '"""%s."""\n' % claim)
        pin = self.write(self.root, "scripts/fixtures/tool-capability-pin.json",
                         "{}\n")
        findings = self.scan()
        self.assert_finding(findings, CATEGORY_HISTORY, prose)
        self.assert_finding(findings, CATEGORY_HISTORY, pin)


class LegitimateExampleTest(_ExampleRootMixin, unittest.TestCase):
    """Legitimate categories pass; a keyword alone never rejects."""

    def setUp(self):
        self.root = self.make_root()

    def test_version_control_in_throwaway_repo(self):
        self.write(self.root, self.module_path("git_throwaway"), "\n".join((
            "import subprocess",
            "import tempfile",
            "import unittest",
            "",
            "",
            "class ThrowawayRepoTest(unittest.TestCase):",
            "    def test_init_add_list(self):",
            "        root = tempfile.mkdtemp()",
            "        subprocess.run(['git', 'init', root], check=True)",
            "        subprocess.run(['git', 'add', '.'], cwd=root, check=True)",
            "        out = subprocess.check_output(['git', 'ls-files'], cwd=root)",
            "        self.assertEqual(out, b'')",
            "")))
        self.assert_clean(self.root)

    def test_controlled_fixture(self):
        digest = hashlib.sha256(b"controlled fixture").hexdigest()
        blob = hashlib.sha1(b"controlled fixture").hexdigest()
        home = "/".join(("", "Users", "someone", "repo"))
        self.write(self.root, self.module_path("controlled_fixture"), "\n".join((
            "import unittest",
            "",
            "SESSION_ID = 'S-0001'",
            "DIGEST = %r" % digest,
            "BLOB_ID = %r" % blob,
            "STAMP = '2026-01-01T00:00:00Z'",
            "HOME = %r" % home,
            "",
            "",
            "class FixtureTest(unittest.TestCase):",
            "    def test_shape(self):",
            "        record = {'session': SESSION_ID, 'digest': DIGEST,",
            "                  'blob': BLOB_ID, 'at': STAMP, 'home': HOME,",
            "                  'expected_test_count': 12}",
            "        self.assertEqual(record['expected_test_count'], 12)",
            "")))
        self.write(self.root, "scripts/fixtures/session.json",
                   json.dumps({"session": "S-0001", "digest": digest,
                               "expected_test_count": 12,
                               "captured_at": "2026-01-01T00:00:00Z"}))
        self.assert_clean(self.root)

    def test_security_negative(self):
        self.write(self.root, self.module_path("security_negative"), "\n".join((
            '"""A provider launch attempted from a test must be refused."""',
            "import subprocess",
            "import unittest",
            "",
            "from cowork_offline_guard import ProviderLaunchBlocked",
            "",
            "",
            "class RefusedLaunchTest(unittest.TestCase):",
            "    def test_launch_is_refused(self):",
            "        with self.assertRaises(ProviderLaunchBlocked):",
            "            subprocess.run(['provider', '--version'])",
            "")))
        self.assert_clean(self.root)

    def test_compatibility_input(self):
        transcript = _join_words("Ran", "12", "tests", "in", "0.1s") + "\n\nOK\n"
        self.write(self.root, self.module_path("compatibility_input"), "\n".join((
            "import unittest",
            "",
            "TRANSCRIPT = %r" % transcript,
            "",
            "",
            "def parse(text):",
            "    return text.split()",
            "",
            "",
            "class ParserTest(unittest.TestCase):",
            "    def test_parses_transcript(self):",
            "        self.assertEqual(parse(TRANSCRIPT)[0], 'Ran')",
            "")))
        self.assert_clean(self.root)

    def test_regression_reference(self):
        self.write(self.root, self.module_path("regression_reference"), "\n".join((
            '"""Focused regression for issue #64 implementation package P5."""',
            "import unittest",
            "",
            "OBSERVED_LABEL = 'focused regression suite'",
            "",
            "",
            "class LabelTest(unittest.TestCase):",
            "    def test_label_survives(self):",
            "        self.assertEqual(OBSERVED_LABEL.split()[0], 'focused')",
            "")))
        self.assert_clean(self.root)

    def test_product_field(self):
        self.write(self.root, self.module_path("product_field"), "\n".join((
            "import unittest",
            "",
            "",
            "class ReceiptSchemaTest(unittest.TestCase):",
            "    def test_fields(self):",
            "        receipt = {'expected_test_count': 3,",
            "                   'transaction_id': 'T-1', 'verdict': 'green'}",
            "        self.assertEqual(receipt['verdict'], 'green')",
            "")))
        self.write(self.root, "scripts/fixtures/inventory.json",
                   json.dumps({"expected_test_count": 3, "verdict": "green"}))
        self.assert_clean(self.root)

    def test_local_suite_count_is_a_controlled_fixture(self):
        self.write(self.root, self.module_path("local_suite"), "\n".join((
            "import unittest",
            "",
            "",
            "class _Probe(unittest.TestCase):",
            "    def test_probe(self):",
            "        pass",
            "",
            "",
            "class LocalSuiteTest(unittest.TestCase):",
            "    def test_local_suite_size(self):",
            "        suite = unittest.TestLoader().loadTestsFromTestCase(_Probe)",
            "        self.assertEqual(suite.countTestCases(), 1)",
            "")))
        self.assert_clean(self.root)

    def test_measurement_inputs_are_exempt(self):
        base = MEASUREMENT_INPUTS.replace(os.sep, "/") + "/x/"
        self.write(self.root, base + "report.json",
                   json.dumps({"cowork_result": True, "rc": 0,
                               "outcome": "approved"}))
        transcript = _join_words("Ran", "12", "tests") + "\n\nOK"
        self.write(self.root, base + "S-x.jsonl",
                   json.dumps({"type": "tool_result",
                               "content": transcript}) + "\n")
        self.write(self.root, base + "repo.json",
                   json.dumps({"base" + "_commit": "0" * 40}))
        self.assert_clean(self.root)


class ThrowawayCopyTest(unittest.TestCase):
    """A copy of the repository with one injected receipt fails in a child
    interpreter while the real tree stays untouched."""

    IGNORED = (".git", ".venv", ".cowork", ".plans", ".worktrees", ".opencode",
               "__pycache__", ".DS_Store")

    def test_injected_receipt_fails_the_copy(self):
        parent = tempfile.mkdtemp(prefix="evidence-lifetime-copy-")
        self.addCleanup(shutil.rmtree, parent, True)
        copy = os.path.join(parent, "repo")
        shutil.copytree(REPO_ROOT, copy, symlinks=True,
                        ignore=shutil.ignore_patterns(*self.IGNORED))
        injected = "package-run-" + uuid.uuid4().hex + ".json"
        with open(os.path.join(copy, "scripts", "fixtures", injected), "w",
                  encoding="utf-8") as fh:
            json.dump({"cowork_result": True, "rc": 0, "outcome": "approved"},
                      fh)
        child = subprocess.run(
            [sys.executable, "-m", "unittest",
             MODULE_NAME + ".RepositoryScanTest"],
            cwd=os.path.join(copy, "scripts"), env=dict(os.environ),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=600)
        self.assertNotEqual(child.returncode, 0, child.stdout)
        self.assertIn(CATEGORY_RECEIPT, child.stdout)
        self.assertIn("scripts/fixtures/" + injected, child.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(REPO_ROOT, "scripts", "fixtures", injected)))


class ContractPresenceTest(unittest.TestCase):
    """The seven contract files carry the same rule."""

    @staticmethod
    def _normalized(rel):
        return " ".join(_read_repo(rel).lower().split())

    def test_each_contract_file_carries_the_rule(self):
        for rel in CONTRACT_FILES:
            with self.subTest(file=rel):
                text = self._normalized(rel)
                missing = [t for t in REQUIRED_TOKENS if t not in text]
                self.assertEqual(missing, [], "%s lacks %r" % (rel, missing))

    def test_skills_carry_no_placement_opt_out(self):
        clause = _join_words("unless", "the", "user", "asks", "otherwise")
        for skill in EDITED_SKILLS:
            rel = os.path.join("skills", skill, "SKILL.md")
            with self.subTest(skill=skill):
                self.assertNotIn(clause, self._normalized(rel))

    def test_agents_names_the_recurrence_check(self):
        self.assertIn(MODULE_NAME, _read_repo("AGENTS.md"))

    def test_contract_files_have_clean_whitespace(self):
        # The trailing-whitespace condition git diff --check reports by
        # default, carried in-harness because the hermetic checkout has no
        # object store for git to diff against.
        for rel in CONTRACT_FILES:
            with self.subTest(file=rel):
                offenders = [
                    lineno for lineno, line
                    in enumerate(_read_repo(rel).split("\n"), 1)
                    if line.endswith(" ") or line.endswith("\t")]
                self.assertEqual(offenders, [], "%s: trailing whitespace on "
                                 "line(s) %s" % (rel, offenders))


def parse_frontmatter(text):
    """``(fields, body, error)`` for a skill file with a simple frontmatter."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None, None, "missing opening frontmatter marker"
    end = None
    for index in range(1, len(lines)):
        if lines[index] == "---":
            end = index
            break
    if end is None:
        return None, None, "missing closing frontmatter marker"
    fields = {}
    current = None
    for line in lines[1:end]:
        if not line.strip():
            continue
        if line[0] in " \t" and current is not None:
            fields[current] += " " + line.strip()
            continue
        if ":" not in line:
            return None, None, "frontmatter line without a key: %r" % line
        key, _, value = line.partition(":")
        current = key.strip()
        fields[current] = value.strip()
    return fields, "\n".join(lines[end + 1:]), None


class SkillFrontmatterTest(unittest.TestCase):
    """Stdlib mirror of the skill validator's frontmatter rules."""

    NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_edited_skills_have_valid_frontmatter(self):
        for skill in EDITED_SKILLS:
            rel = os.path.join("skills", skill, "SKILL.md")
            with self.subTest(skill=skill):
                fields, body, error = parse_frontmatter(_read_repo(rel))
                self.assertIsNone(error)
                extra = set(fields) - SKILL_FRONTMATTER_KEYS
                self.assertEqual(extra, set())
                self.assertIn("name", fields)
                self.assertIn("description", fields)
                self.assertRegex(fields["name"], self.NAME)
                self.assertEqual(fields["name"], skill)
                description = fields["description"]
                self.assertTrue(description)
                self.assertNotIn("<", description)
                self.assertNotIn(">", description)
                self.assertLessEqual(len(description), 1024)
                self.assertNotIn("[TODO:", body)


class SelfCheckTest(unittest.TestCase):
    """The detector encodes no release hash, personal path, installed-copy
    access or filename inventory, and states its categories and limits."""

    @classmethod
    def setUpClass(cls):
        with open(SELF_PATH, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.tree = ast.parse(cls.source)
        cls.constants = [node.value for node in ast.walk(cls.tree)
                         if isinstance(node, ast.Constant)
                         and isinstance(node.value, str)]

    def test_no_release_hash(self):
        pattern = r"\b[0-9a-f]{" + "40" + r"}\b"
        self.assertIsNone(re.search(pattern, self.source, re.IGNORECASE))

    def test_no_personal_or_installed_paths(self):
        needles = (
            "".join(("/Us", "ers/")),
            "".join(("/ho", "me/")),
            "".join(("expand", "user")),
            "".join(("shutil.", "which")),
            "".join((".cla", "ude")),
            "".join((".co", "dex")),
        )
        for needle in needles:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.source)
                for value in self.constants:
                    self.assertNotIn(needle, value)

    def test_no_test_filename_inventory(self):
        pattern = re.compile("^test_" + ".*" + r"\.py$")
        for value in self.constants:
            self.assertIsNone(pattern.search(value), value)

    def test_self_exclusion_by_identity(self):
        found = False
        for node in ast.walk(self.tree):
            if (_call_name(node) == "realpath" and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "__file__"):
                found = True
        self.assertTrue(found)

    def test_clean_whitespace(self):
        self.assertNotIn("\t", self.source)
        for lineno, line in enumerate(self.source.splitlines(), 1):
            self.assertFalse(line.endswith(" "), "trailing space on line %d"
                             % lineno)

    def test_docstring_names_categories_and_limits(self):
        doc = ast.get_docstring(self.tree).lower()
        for category in FORBIDDEN_CATEGORIES + LEGITIMATE_CATEGORIES:
            with self.subTest(category=category):
                self.assertIn(category, doc)
        self.assertIn("limit", doc)


if __name__ == "__main__":
    unittest.main()
