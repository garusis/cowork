#!/usr/bin/env python3
"""Focused permanent tests for the bounded-correction building blocks:
`cowork_correction` (closed packet schema, risk classes, review-scope decision),
the additive scoped renders in `cowork_handoff`, and the reviewer role wording.

Every input is neutral and synthetic: made-up paths, ids and digests and
placeholder files created in temp directories. Nothing here asserts anything
about one delivery, a point in time or this repository's own suite.

Run with the offline harness:

    python3 scripts/cowork_offline_tests.py test_context_correction
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

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_correction as correction  # noqa: E402
import cowork_execution_profiles as profiles  # noqa: E402
import cowork_handoff as handoff  # noqa: E402

_ROOT = os.path.dirname(_HERE)
_DIR_TOKEN = "<DIR>"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
_EMPTY_SHA12 = "e3b0c44298fc"


def _tempdir(case):
    root = os.path.realpath(tempfile.mkdtemp())
    case.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
    return root


# --------------------------------------------------------------------------- #
# Default-render goldens.                                                     #
#                                                                             #
# A render with no correction facts must keep producing exactly this text for  #
# every registered edge. The placeholder files are empty (so every descriptor  #
# carries the empty-content digest) and `<DIR>` stands for their directory.    #
# The prose below is literal; only the descriptor line format is composed.     #
# --------------------------------------------------------------------------- #

_NEUTRAL_FACT_VALUES = {
    "team": ["scout"], "role": "scout", "phase": "scouting",
    "from_controller": "claude", "to_controller": "codex",
    "artifact_noun": "intel", "reason_code": "reviewer_failure",
    "source_code": "gate",
    "txn_id": "T-1", "manifest_digest": "ab" * 32,
    "index_digest": "cd" * 32, "verdict": "green",
    "final_suite_label": "full", "final_suite_binding": "ran_once",
    "command_count": 1, "disposition": "pending_review",
    "contradiction": False,
    "suite_universe_digest": "ef" * 32, "suite_member_count": 1,
    "suite_component_count": 1,
    "checkpoint_id": "CP-1", "checkpoint_phase": "building",
    "checkpoint_verdict": "accepted", "checkpoint_state": "terminal",
    "checkpoint_disposition": "pending_review",
    "checkpoint_superseded_count": 0,
}
_CORRECTION_SOURCE = "correction_packet"
_TWO_REPOS = [{"path": "/neutral/repo-a", "has_head": True},
              {"path": "/neutral/repo-b", "has_head": False}]
_RESUME_PREFIX_EDGES = (
    "scout->scout-reviewer:review_resume",
    "planner->planning-advisor:review_resume",
    "builder->build-reviewer:review_resume",
)
_BUILD_REVIEWER_EDGES = (
    "builder->build-reviewer:review_ctx",
    "builder->build-reviewer:review_resume",
)


def _neutral_facts(spec):
    return {k: _NEUTRAL_FACT_VALUES[k] for k in spec["facts"]
            if not k.startswith("correction_")}


def _neutral_ctx(spec):
    return ({"repos": []} if "repos" in (spec.get("ctx_keys") or ())
            else None)


def _placeholder_path(root, source):
    path = os.path.join(root, source + ".txt")
    if not os.path.exists(path):
        with open(path, "w"):
            pass
    return path


def _default_sources(spec):
    return [s for s in (spec["required"] or spec["sources"][:1])
            if s != _CORRECTION_SOURCE]


def _artifacts(root, spec):
    return [{"label": s, "path": _placeholder_path(root, s), "kind": "text",
             "source": s} for s in _default_sources(spec)]


def render_default_cases(root):
    """Render every registered edge (plus the variants of the edges whose
    output depends on composition) with neutral inputs and no correction
    facts. Returns {case_id: text with the temp dir replaced by `<DIR>`}."""
    rendered = {}
    for edge_id, spec in handoff.EDGES.items():
        rendered[edge_id] = str(handoff.render_handoff(
            edge_id, artifacts=_artifacts(root, spec),
            facts=_neutral_facts(spec), ctx=_neutral_ctx(spec)))
    prefix = handoff.render_handoff(
        "context->update",
        artifacts=_artifacts(root, handoff.EDGES["context->update"]))
    for edge_id in _RESUME_PREFIX_EDGES:
        spec = handoff.EDGES[edge_id]
        ctx = dict(_neutral_ctx(spec) or {})
        ctx["context_update_prefix"] = prefix
        rendered[edge_id + "#prefix"] = str(handoff.render_handoff(
            edge_id, artifacts=_artifacts(root, spec),
            facts=_neutral_facts(spec), ctx=ctx))
    for edge_id in _BUILD_REVIEWER_EDGES:
        spec = handoff.EDGES[edge_id]
        rendered[edge_id + "#repos"] = str(handoff.render_handoff(
            edge_id, artifacts=_artifacts(root, spec),
            facts=_neutral_facts(spec), ctx={"repos": _TWO_REPOS}))
        rendered[edge_id + "#team_only"] = str(handoff.render_handoff(
            edge_id, artifacts=_artifacts(root, spec),
            facts={"team": ["builder"]}, ctx={"repos": []}))
    rendered["build_diff_recipe#none"] = handoff.build_diff_recipe(None)
    rendered["build_diff_recipe#empty"] = handoff.build_diff_recipe([])
    rendered["build_diff_recipe#repos"] = handoff.build_diff_recipe(_TWO_REPOS)
    return {k: v.replace(root, _DIR_TOKEN) for k, v in rendered.items()}


def _line(label, source):
    return ("  - %s: %s/%s.txt  [0 bytes, sha256 %s]"
            % (label, _DIR_TOKEN, source, _EMPTY_SHA12))


_L = {
    "context": _line("shared session context (same the active roles were "
                     "given)", "context"),
    "intel_json": _line("scout intel JSON (machine source of truth)",
                        "intel_json"),
    "intel_md": _line("scout intel markdown (readable review surface)",
                      "intel_md"),
    "plan_json": _line("plan JSON (machine source of truth)", "plan_json"),
    "plan_md": _line("plan markdown (readable review surface)", "plan_md"),
    "build_status": _line("builder status JSON (status + verification log)",
                          "build_status"),
    "build_baseline": _line("build-baseline metadata (per-root start commit "
                            "+ dirty)", "build_baseline"),
    "review": _line("reviewer verdict + findings (JSON)", "review"),
    "payload": _line("hand-back note", "payload"),
    "artifacts": _line("session artifact", "artifacts"),
    "answer": _line("orchestrator answer to a decision request", "answer"),
    "verdict": _line("reviewer verdict + findings (JSON)", "verdict"),
    "upstream": _line("consumed upstream artifact", "upstream"),
    "checkpoint_status": _line("checkpoint status for your own pending "
                               "dispatch (orchestrator-derived)",
                               "checkpoint_status"),
}


def _lines(*sources):
    return "\n".join(_L[s] for s in sources)


_FULL = ("Read the FULL current files from disk at the paths above. They are "
         "the authoritative current source of truth for your review.")

_RECIPE_CWD = (
    "The unit of review is the builder's FULL working-tree delta against "
    "this plan. The delta is NOT embedded here — capture the COMPLETE delta "
    "yourself. Plain `git diff` is insufficient: it omits STAGED changes and "
    "UNTRACKED new files (and the builder creates files). Run:"
    "\n  - `git status --porcelain` — every staged, unstaged, and untracked "
    "path at a glance;"
    "\n  - `git diff HEAD` (or `git diff --stat HEAD` first, then targeted "
    "`git diff HEAD -- <path>`) — all tracked staged+unstaged changes since "
    "the last commit;"
    "\n  - read each untracked/new file directly — it will NOT appear in "
    "`git diff`."
    "\nReview the full delta critically against the plan and context above.")

_RECIPE_REPOS = (
    "The unit of review is the builder's FULL working-tree delta against "
    "this plan, taken as the UNION of the deltas of EACH of these selected "
    "repo roots. The delta is NOT embedded here — capture the COMPLETE delta "
    "yourself, per root. Plain `git diff` is insufficient: it omits STAGED "
    "changes and UNTRACKED new files (and the builder creates files). "
    "Capture the delta of EACH of these repos:"
    "\n  Repo /neutral/repo-a (has a baseline commit):"
    "\n    - `git -C /neutral/repo-a status --porcelain` — staged, unstaged, "
    "and untracked paths;"
    "\n    - `git -C /neutral/repo-a diff HEAD` (or `git -C /neutral/repo-a "
    "diff --stat HEAD` first, then targeted `git -C /neutral/repo-a diff "
    "HEAD -- <path>`) — all tracked staged+unstaged changes since the last "
    "commit;"
    "\n    - read each untracked/new file under /neutral/repo-a directly — "
    "it will NOT appear in `git diff`."
    "\n  Repo /neutral/repo-b (NO baseline commit — unborn repo or non-git "
    "fallback; do NOT run `git diff HEAD`, it fails):"
    "\n    - `git -C /neutral/repo-b status --porcelain` — every path at a "
    "glance;"
    "\n    - `git -C /neutral/repo-b diff --cached` and `git -C "
    "/neutral/repo-b diff` — staged and unstaged changes;"
    "\n    - read untracked/new files under /neutral/repo-b directly."
    "\nReview the union of per-root deltas critically against the plan and "
    "context above. An empty delta in a repo the plan touches is a finding; "
    "ignore repos the plan does not list.")

_OWNED = "\n".join([
    "Owned verification receipt (orchestrator-derived — the authoritative",
    "verification fact base for this review; the builder's own verification",
    "prose is secondary to it):",
    "  transaction=T-1  verdict=green  final_suite=full (ran_once)",
    "  manifest=abababababab  index=cdcdcdcdcdcd  commands=1",
    "  disposition=pending_review",
    "  composed suite: components=1 members=1 universe=efefefefefef",
    "  The receipt file itself (result.json) reaches you by absolute path",
    "  among the artifacts above (the verification_receipt slot).",
    "",
    "Owned checkpoint receipt (orchestrator-derived — a single checkpoint's",
    "terminal verdict; the receipt file itself reaches you by absolute path",
    "among the artifacts above, the checkpoint_receipt slot — never restated",
    "in prose here):",
    "  checkpoint=CP-1  phase=building  verdict=accepted  "
    "disposition=pending_review",
])

_BUILD_FILES = ("plan_json", "plan_md", "build_status", "build_baseline")


def _build_reviewer_ctx(recipe, team="scout", owned=True):
    parts = [
        "The files below are the current authoritative files on disk — the "
        "SAME shared session context the builder was given, both approved "
        "plan files, the builder's status JSON, the builder's markdown "
        "summary (when present), and the build-baseline metadata. Read them "
        "from disk.\n" + _lines("context", *_BUILD_FILES),
        "Team on this session: " + team,
    ]
    if owned:
        parts.append(_OWNED)
    parts.extend([_FULL, recipe])
    return "\n\n".join(parts)


def _build_reviewer_resume(recipe, owned=True):
    parts = [
        "The builder has updated its work since your last review. Re-review "
        "the current full working-tree delta against the plan and the "
        "builder's current status. The current authoritative files are on "
        "disk:\n" + _lines(*_BUILD_FILES),
    ]
    if owned:
        parts.append(_OWNED)
    parts.extend([_FULL, recipe])
    return "\n\n".join(parts)


def _review_resume(*sources):
    return (
        "The reviewed role has updated its artifact(s) since your last "
        "review. Re-review the current authoritative files below against the "
        "current task context, and write your verdict to the review file "
        "again.\n" + _lines(*sources) + "\n\n" + _FULL)


_CONTEXT_UPDATE = (
    "New orchestrator context was provided for this resumed cowork "
    "session.\n\nTreat this as the current task context. Keep prior session "
    "knowledge only where it remains compatible. Read the current context "
    "from the file on disk:\n" + _lines("context"))

GOLDEN = {
    "scout->scout-reviewer:review_ctx": (
        "The files below are the current authoritative files on disk — the "
        "SAME shared initial context the reviewed role was given, plus the "
        "artifact(s) to review. Read them from disk.\n"
        + _lines("context", "intel_json")
        + "\n\nTeam on this session: scout\n\n"
        "Review the current artifact(s) critically against the shared "
        "context above.\n\n" + _FULL),
    "scout->scout-reviewer:review_resume": _review_resume("intel_json"),
    "reviewer->lead:handback_revise": (
        "[reviewer handoff] A reviewer checked your intel and it is not "
        "ready to hand off yet. Read the reviewer's findings from the review "
        "file on disk:\n" + _lines("review")
        + "\nAddress them, update your intel, and set status back to "
        "ready_for_review when done."),
    "scout->planner:seed": (
        "The scout phase is complete and the scout-reviewer APPROVED the "
        "scout intel. Digest it and produce the plan. The approved intel AND "
        "the current shared context are the files on disk below.\n\n"
        + _lines("context", "intel_json") + "\n\n" + _FULL),
    "scout->planner:intel_updated": (
        "The scout intel changed since you started planning: your hand-back "
        "was executed, the scout re-investigated, and the scout-reviewer "
        "approved the updated intel. Digest it and continue planning. Keep "
        "prior plan content only where it remains compatible.\n\n"
        + _lines("intel_json") + "\n\n" + _FULL),
    "planner->planning-advisor:review_ctx": (
        "The files below are the current authoritative files on disk — the "
        "SAME shared session context the planner was given, the planner's "
        "current plan to review, AND the approved scout intel the plan must "
        "cover. Read them from disk.\n"
        + _lines("context", "plan_json", "plan_md", "intel_json", "intel_md")
        + "\n\nTeam on this session: scout\n\n"
        "Review the planner's current plan critically against the shared "
        "context and verify its criteria-coverage against the approved "
        "intel above.\n\n" + _FULL),
    "planner->planning-advisor:review_resume": _review_resume(
        "plan_json", "plan_md"),
    "planner->builder:seed": (
        "The planning phase is complete and the planning-advisor APPROVED "
        "the plan. Execute it: make the code changes and verify them. The "
        "approved plan AND the current shared context are the files on disk "
        "below.\n\n" + _lines("context", "plan_json", "plan_md")
        + "\n\n" + _FULL),
    "scout->builder:seed": (
        "The scouting phase is complete and the scout-reviewer APPROVED the "
        "intel. This session runs under the light execution profile, so "
        "that intel is your approved plan: execute it, make the changes and "
        "verify them. The approved intel (plan) AND the current shared "
        "context are the files on disk below.\n\n"
        + _lines("context", "plan_json", "plan_md") + "\n\n" + _FULL),
    "planner->builder:plan_updated": (
        "The plan changed since you started building: your hand-back was "
        "executed, the planner re-planned, and the planning-advisor approved "
        "the UPDATED plan. Digest the changes and continue building. Keep "
        "prior work only where it remains compatible.\n\n"
        + _lines("plan_json", "plan_md") + "\n\n" + _FULL),
    "builder->build-reviewer:review_ctx": _build_reviewer_ctx(_RECIPE_CWD),
    "builder->build-reviewer:review_resume": _build_reviewer_resume(
        _RECIPE_CWD),
    "planner->scout:handback_wake": (
        "The planner handed the work back to you mid-planning (the "
        "orchestrator authorized the hand-back). Re-run your full cycle: "
        "investigate, resolve what the context settles, raise an authority "
        "request only for what it does not, update your intel file, and set "
        "status ready_for_review when done. Read the planner's hand-back "
        "note from the file on disk:\n" + _lines("payload")),
    "builder->planner:handback_wake": (
        "The builder handed the work back to you mid-build (the "
        "orchestrator authorized the hand-back). Re-plan as needed: update "
        "your plan files, raise an authority request only for what the "
        "context does not settle, and set status ready_for_review when "
        "done. Read the builder's hand-back note from the file on disk:\n"
        + _lines("payload")),
    "controller->switch": (
        "[controller switch handoff]\n"
        "You are continuing an existing cowork scouting phase as scout.\n"
        "Controller switched: claude -> codex.\n"
        "This is a fresh codex provider conversation. Hidden chat history "
        "from claude is not available; cowork-visible session state, "
        "artifacts, shared context, and the working tree continue.\n"
        "Switch reason: reviewer_failure.\n"
        "Switch source: gate.\n\n"
        "The shared context, the current artifacts, any free-form switch "
        "recovery note, and the failed pending turn (if any) are the "
        "authoritative files on disk below — read them from disk to orient "
        "yourself, then process the failed pending turn:\n"
        + _lines("context")),
    "lead->pending_resume": (
        "[pending turn resume]\n"
        "You are resuming a session after a failed turn. Read the files "
        "below from disk to orient yourself, then process the failed "
        "pending turn:\n" + _lines("context")),
    "context->update": _CONTEXT_UPDATE,
    "orchestrator->lead:decision_answer": (
        "[orchestrator decision] The orchestrator answered the request this "
        "phase stopped on. Read the answer from the file on disk, record it "
        "in your artifact (remove the pending question), and continue your "
        "phase. The original task context is unchanged; the answer refines "
        "it:\n" + _lines("answer")),
    "orchestrator->role:decision_record": (
        "Orchestrator answers given earlier in this session refine (never "
        "replace) the task context. Read them from disk:\n"
        + _lines("answer")),
    "eval->reviewer_verdict": (
        "The reviewer's verdict + findings for this round (and the artifact "
        "you reviewed, when named) are the current files on disk — read "
        "them before scoring:\n" + _lines("verdict") + "\n\n" + _FULL),
    "eval->upstream": (
        "The upstream artifact(s) this phase consumed (current files on "
        "disk — the authoritative source of truth; read them before "
        "scoring):\n" + _lines("upstream") + "\n\n" + _FULL),
    "cowork->role:checkpoint_wake": (
        "Your dispatched checkpoint has reached a terminal verdict "
        "(accepted). Read its current status from disk:\n"
        + _lines("checkpoint_status") + "\n\n"
        "This is an orchestrator-run, deterministic checkpoint result — "
        "never agent prose. Continue once you have read it."),
    # Variants whose output depends on composition.
    "scout->scout-reviewer:review_resume#prefix": (
        _CONTEXT_UPDATE + "\n\n" + _review_resume("intel_json")),
    "planner->planning-advisor:review_resume#prefix": (
        _CONTEXT_UPDATE + "\n\n" + _review_resume("plan_json", "plan_md")),
    "builder->build-reviewer:review_resume#prefix": (
        _CONTEXT_UPDATE + "\n\n" + _build_reviewer_resume(_RECIPE_CWD)),
    "builder->build-reviewer:review_ctx#repos": _build_reviewer_ctx(
        _RECIPE_REPOS),
    "builder->build-reviewer:review_resume#repos": _build_reviewer_resume(
        _RECIPE_REPOS),
    "builder->build-reviewer:review_ctx#team_only": _build_reviewer_ctx(
        _RECIPE_CWD, team="builder", owned=False),
    "builder->build-reviewer:review_resume#team_only": _build_reviewer_resume(
        _RECIPE_CWD, owned=False),
    "build_diff_recipe#none": _RECIPE_CWD,
    "build_diff_recipe#empty": _RECIPE_CWD,
    "build_diff_recipe#repos": _RECIPE_REPOS,
}


class HandoffDefaultByteIdenticalTests(unittest.TestCase):
    """With no correction facts every registered edge renders exactly the
    literal text above."""

    def test_golden_keys_cover_every_registered_edge(self):
        root = _tempdir(self)
        cases = render_default_cases(root)
        self.assertEqual(set(cases), set(GOLDEN))
        for edge_id in handoff.EDGES:
            self.assertIn(edge_id, GOLDEN)

    def test_default_render_is_the_literal_text(self):
        root = _tempdir(self)
        cases = render_default_cases(root)
        for key in sorted(GOLDEN):
            with self.subTest(case=key):
                self.assertEqual(cases[key], GOLDEN[key])

    def test_default_renders_carry_no_correction_text(self):
        root = _tempdir(self)
        for key, text in render_default_cases(root).items():
            with self.subTest(case=key):
                self.assertNotIn("Correction (orchestrator-derived)", text)
                self.assertNotIn("TARGETED", text)


# --------------------------------------------------------------------------- #
# Packet fixtures.                                                            #
# --------------------------------------------------------------------------- #

_LINKS = {
    "request_id": "req-1", "authority_id": None,
    "candidate_manifest_digest": SHA_A, "candidate_index_digest": SHA_B,
    "verification_transaction_id": "txn-1", "disposition": "pending_review",
    "prior_reviewed_manifest_digest": SHA_A,
}
_PRIOR = {"recorded_manifest_digest": SHA_A,
          "verified_manifest_digest": SHA_A}
_BASIS = {"phase": "planning", "discoverer": "planning-advisor", "round": 2,
          "rule_version": "1"}


def _finding(**over):
    finding = {"finding_id": "F-0001", "severity": "major",
               "criterion": "neutral criterion", "evidence_path": None,
               "evidence_sha256": None}
    finding.update(over)
    return finding


def _import_finding(**over):
    finding = {"source_finding_id": "F-0007", "source_session": "S-1",
               "severity": "blocking", "criterion": None,
               "evidence_path": None, "evidence_sha256": None,
               "discoverer": "planning-advisor", "round": 2,
               "phase": "planning", "source_record_sha256": SHA_C}
    finding.update(over)
    return finding


def _delta(changed=("docs/a.md",), executable=(), unchanged=False,
           dependency_hit=False):
    return {"changed_paths": list(changed),
            "executable_paths": list(executable),
            "candidate_unchanged": unchanged,
            "dependency_hit": dependency_hit}


_UNCHANGED = _delta(changed=(), unchanged=True)


def _policy(profile="standard"):
    return profiles.resolved_vertex_policy(profile, "builder")


def _correction_packet(**over):
    args = dict(session_uuid="S-1", phase="building", role="builder",
                round_index=1, delta=_delta(), findings=[_finding()],
                signals=None, links=_LINKS, outcome="addressed",
                policy=_policy(), prior_reviewed=_PRIOR)
    args.update(over)
    return correction.build_correction_packet(**args)


def _import_packet(**over):
    args = dict(session_uuid="S-2", phase="planning", role="planner",
                findings=[_import_finding()], unresolved_basis=_BASIS)
    args.update(over)
    return correction.build_finding_import_packet(**args)


def _mutated(packet, fn):
    clone = copy.deepcopy(packet)
    fn(clone)
    return clone


def _first_entry(packet):
    return packet["findings"][0]


class CorrectionPacketSchemaTests(unittest.TestCase):
    def test_both_kinds_build_and_validate(self):
        for packet in (_correction_packet(), _import_packet()):
            with self.subTest(kind=packet["kind"]):
                self.assertEqual(correction.validate_packet(packet),
                                 (True, None))

    def test_closed_key_sets(self):
        top = ["findings", "kind", "links", "outcome", "phase", "review_scope",
               "risk_class", "role", "round", "schema_version",
               "session_uuid", "unresolved_basis"]
        entry = ["criterion", "evidence_path", "evidence_sha256",
                 "evidence_state", "finding_id", "severity",
                 "source_finding_id", "source_session", "state"]
        imported = entry + ["discoverer", "phase", "round",
                            "source_record_sha256"]
        packet = _correction_packet()
        self.assertEqual(sorted(packet), top)
        self.assertEqual(sorted(_first_entry(packet)), entry)
        self.assertEqual(sorted(_first_entry(_import_packet())),
                         sorted(imported))

    def test_builders_mint_no_id_and_no_time(self):
        packet = _correction_packet()
        self.assertEqual(_first_entry(packet)["finding_id"], "F-0001")
        imported = _import_packet()
        self.assertIsNone(_first_entry(imported)["finding_id"])
        for doc in (packet, imported):
            self.assertFalse(
                {"id", "packet_id", "created_at", "timestamp"} & set(doc))

    def test_every_reject_code_has_a_negative(self):
        correction_packet = _correction_packet()
        import_packet = _import_packet()
        entry_of = _first_entry
        cases = [
            ("packet_not_object", None),
            ("packet_not_object", []),
            ("packet_not_object", "packet"),
            ("schema_version_unknown", _mutated(
                correction_packet, lambda p: p.update(schema_version=2))),
            ("schema_version_unknown", _mutated(
                correction_packet, lambda p: p.update(schema_version=True))),
            ("kind_unknown", _mutated(
                correction_packet, lambda p: p.update(kind="other"))),
            ("field_unknown", _mutated(
                correction_packet, lambda p: p.update(extra=1))),
            ("field_unknown", _mutated(
                correction_packet, lambda p: entry_of(p).update(extra=1))),
            ("field_unknown", _mutated(
                correction_packet, lambda p: p["links"].update(extra=1))),
            ("field_unknown", _mutated(
                correction_packet,
                lambda p: p["review_scope"].update(extra=1))),
            ("field_missing", _mutated(
                correction_packet, lambda p: p.pop("role"))),
            ("field_missing", _mutated(
                correction_packet, lambda p: entry_of(p).pop("severity"))),
            ("field_missing", _mutated(
                correction_packet, lambda p: p["links"].pop("request_id"))),
            ("field_missing", _mutated(
                import_packet, lambda p: entry_of(p).pop("discoverer"))),
            ("field_type", _mutated(
                correction_packet, lambda p: p.update(round=-1))),
            ("field_type", _mutated(
                correction_packet, lambda p: p.update(round="1"))),
            ("field_type", _mutated(
                correction_packet, lambda p: p.update(findings="x"))),
            ("field_type", _mutated(
                correction_packet,
                lambda p: p.update(session_uuid="has space"))),
            ("field_type", _mutated(
                correction_packet, lambda p: p.update(links=[]))),
            ("field_type", _mutated(
                correction_packet, lambda p: entry_of(p).update(criterion=3))),
            ("field_type", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(criterion="x" * 1001))),
            ("field_type", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(evidence_sha256="zz"))),
            ("field_type", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(finding_id=None))),
            ("field_type", _mutated(
                import_packet,
                lambda p: entry_of(p).update(finding_id="F-0001"))),
            ("field_type", _mutated(
                import_packet,
                lambda p: entry_of(p).update(source_finding_id=None))),
            ("phase_unknown", _mutated(
                correction_packet, lambda p: p.update(phase="other"))),
            ("phase_unknown", _mutated(
                import_packet, lambda p: entry_of(p).update(phase="other"))),
            ("outcome_unknown", _mutated(
                correction_packet, lambda p: p.update(outcome="done"))),
            ("risk_class_unknown", _mutated(
                correction_packet, lambda p: p.update(risk_class="minor"))),
            ("scope_unknown", _mutated(
                correction_packet,
                lambda p: p["review_scope"].update(scope="partial"))),
            ("reason_code_unknown", _mutated(
                correction_packet,
                lambda p: p["review_scope"].update(reason_code="because"))),
            ("severity_unknown", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(severity="critical"))),
            ("evidence_state_unknown", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(evidence_state="maybe"))),
            ("state_unknown", _mutated(
                correction_packet, lambda p: entry_of(p).update(state="closed"))),
            ("state_unknown", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(state="approved"))),
            ("state_unknown", _mutated(
                correction_packet, lambda p: entry_of(p).update(state="fixed"))),
            ("link_not_token", _mutated(
                correction_packet,
                lambda p: p["links"].update(request_id="has space"))),
            ("link_not_token", _mutated(
                correction_packet,
                lambda p: p["links"].update(candidate_manifest_digest="abc"))),
            ("link_not_token", _mutated(
                correction_packet,
                lambda p: p["links"].update(disposition="closed"))),
            ("kind_field_mismatch", _mutated(
                correction_packet, lambda p: p.update(outcome=None))),
            ("kind_field_mismatch", _mutated(
                correction_packet, lambda p: p.update(risk_class=None))),
            ("kind_field_mismatch", _mutated(
                correction_packet, lambda p: p.update(review_scope=None))),
            ("kind_field_mismatch", _mutated(
                correction_packet,
                lambda p: p.update(unresolved_basis=dict(_BASIS)))),
            ("kind_field_mismatch", _mutated(
                correction_packet,
                lambda p: entry_of(p).update(discoverer="planning-advisor"))),
            ("kind_field_mismatch", _mutated(
                import_packet, lambda p: p.update(outcome="addressed"))),
            ("kind_field_mismatch", _mutated(
                import_packet, lambda p: p.update(risk_class="focused_code"))),
            ("unresolved_basis_invalid", _mutated(
                import_packet, lambda p: p.update(unresolved_basis={}))),
            ("unresolved_basis_invalid", _mutated(
                import_packet, lambda p: p.update(unresolved_basis="other"))),
            ("unresolved_basis_invalid", _mutated(
                import_packet, lambda p: p.update(unresolved_basis="none"))),
            ("unresolved_basis_invalid", _mutated(
                import_packet,
                lambda p: p["unresolved_basis"].update(phase="other"))),
        ]
        for key in correction.FORBIDDEN_PACKET_KEYS:
            cases.append(("forbidden_field", _mutated(
                correction_packet, lambda p, k=key: p.update({k: True}))))
            cases.append(("forbidden_field", _mutated(
                correction_packet,
                lambda p, k=key: entry_of(p).update({k: True}))))
        seen = set()
        for code, packet in cases:
            with self.subTest(code=code):
                self.assertEqual(correction.validate_packet(packet),
                                 (False, code))
                seen.add(code)
        self.assertEqual(seen, set(correction.PACKET_REJECT_CODES))

    def test_every_outcome_is_accepted_and_an_unknown_one_is_not(self):
        for outcome in correction.CORRECTION_OUTCOMES:
            with self.subTest(outcome=outcome):
                packet = _correction_packet(outcome=outcome)
                self.assertEqual(packet["outcome"], outcome)
        with self.assertRaises(ValueError) as caught:
            _correction_packet(outcome="unknown")
        self.assertEqual(caught.exception.args[0], "outcome_unknown")

    def test_a_packet_cannot_represent_closure_or_approval(self):
        self.assertEqual(correction.FINDING_STATES, ("open",))
        self.assertEqual(_first_entry(_correction_packet())["state"], "open")
        for key in ("closed", "closes", "closure", "approve", "approved",
                    "verdict"):
            self.assertIn(key, correction.FORBIDDEN_PACKET_KEYS)

    def test_builders_raise_the_reject_code_for_invalid_input(self):
        with self.assertRaises(ValueError) as caught:
            _correction_packet(findings=[_finding(severity="critical")])
        self.assertEqual(caught.exception.args[0], "severity_unknown")
        with self.assertRaises(ValueError) as caught:
            _correction_packet(phase="other")
        self.assertEqual(caught.exception.args[0], "phase_unknown")
        with self.assertRaises(ValueError) as caught:
            _correction_packet(findings="not a list")
        self.assertEqual(caught.exception.args[0], "field_type")
        with self.assertRaises(ValueError) as caught:
            _correction_packet(links={"request_id": "has space"})
        self.assertEqual(caught.exception.args[0], "link_not_token")
        with self.assertRaises(ValueError) as caught:
            _correction_packet(links={"unknown_link": "x"})
        self.assertEqual(caught.exception.args[0], "field_unknown")

    def test_risk_class_and_scope_are_computed_not_asserted(self):
        packet = _correction_packet(
            delta=_delta(changed=("src/a.py",), executable=("src/a.py",)))
        self.assertEqual(packet["risk_class"], "focused_code")
        self.assertEqual(packet["review_scope"],
                         {"scope": "full",
                          "reason_code": "executable_delta_full"})
        packet = _correction_packet(reviewer_failure=True)
        self.assertEqual(packet["review_scope"]["reason_code"],
                         "reviewer_failure_full")

    def test_validate_never_raises(self):
        torn = [None, 1, 1.5, "x", b"x", [], (), {}, set(),
                {"kind": object()}, {"schema_version": 1, "kind": []},
                {"findings": object()}, float("nan")]
        packet = _correction_packet()
        torn.append(_mutated(packet, lambda p: p.update(findings=[1])))
        torn.append(_mutated(packet, lambda p: p.update(findings=[[]])))
        torn.append(_mutated(packet, lambda p: p.update(phase=[])))
        torn.append(_mutated(packet, lambda p: p.update(links={1: 2})))
        for value in torn:
            with self.subTest(value=repr(value)[:40]):
                ok, code = correction.validate_packet(value)
                self.assertFalse(ok)
                self.assertIn(code, correction.PACKET_REJECT_CODES)

    def test_round_trip_and_determinism(self):
        for build in (_correction_packet, _import_packet):
            first, second = build(), build()
            self.assertEqual(first, second)
            decoded = json.loads(json.dumps(first))
            self.assertEqual(decoded, first)
            self.assertEqual(correction.validate_packet(decoded),
                             (True, None))


class RiskClassTests(unittest.TestCase):
    def test_one_positive_per_class(self):
        self.assertEqual(
            correction.classify_correction(_UNCHANGED, [_finding()]),
            "artifact_only")
        self.assertEqual(
            correction.classify_correction(_delta(), [_finding()]),
            "documentation_metadata")
        self.assertEqual(
            correction.classify_correction(
                _delta(changed=("src/a.py",), executable=("src/a.py",)),
                [_finding()]),
            "focused_code")
        self.assertEqual(
            correction.classify_correction(
                _delta(), [_finding(risk_class="architectural")]),
            "architectural")

    def test_classes_are_ordered_weakest_to_strongest(self):
        self.assertEqual(
            correction.RISK_CLASSES,
            ("artifact_only", "documentation_metadata", "focused_code",
             "architectural"))

    def test_a_role_claim_never_lowers_the_class(self):
        executable = _delta(changed=("src/a.py",), executable=("src/a.py",))
        claim = _finding(claimed_class="artifact_only")
        for signals in (None, {"claimed_class": "artifact_only"},
                        {"risk_class": "artifact_only"}):
            with self.subTest(signals=signals):
                self.assertEqual(
                    correction.classify_correction(executable, [claim],
                                                   signals),
                    "focused_code")
        self.assertEqual(
            correction.classify_correction(
                executable, [_finding(risk_class="artifact_only")]),
            "architectural")

    def test_documentation_with_a_dependency_hit_is_focused_code(self):
        for hit in (True, None):
            with self.subTest(dependency_hit=hit):
                self.assertEqual(
                    correction.classify_correction(
                        _delta(dependency_hit=hit), [_finding()]),
                    "focused_code")

    def test_a_symlink_or_mode_change_listed_as_executable_is_focused_code(
            self):
        delta = _delta(changed=("docs/link.md",),
                       executable=("docs/link.md",))
        self.assertEqual(
            correction.classify_correction(delta, [_finding()]),
            "focused_code")

    def test_bytecode_byproducts_are_ignored(self):
        delta = _delta(changed=("pkg/__pycache__/a.cpython-310.pyc",),
                       unchanged=True)
        self.assertEqual(
            correction.classify_correction(delta, []), "artifact_only")
        delta = _delta(changed=("docs/a.md", "pkg/a.pyc"))
        self.assertEqual(
            correction.classify_correction(delta, []),
            "documentation_metadata")

    def test_an_unmeasurable_delta_is_architectural(self):
        bad = [
            None, "delta", [],
            {"changed_paths": None, "executable_paths": [],
             "candidate_unchanged": False},
            {"changed_paths": ["a.md"], "executable_paths": None,
             "candidate_unchanged": False},
            {"changed_paths": ["a.md"], "executable_paths": [],
             "candidate_unchanged": "no"},
            {"changed_paths": [1], "executable_paths": [],
             "candidate_unchanged": False},
        ]
        for delta in bad:
            with self.subTest(delta=repr(delta)):
                self.assertEqual(
                    correction.classify_correction(delta, []),
                    "architectural")

    def test_an_inconsistent_delta_is_architectural_never_artifact_only(self):
        inconsistent = [
            _delta(changed=("docs/a.md",), unchanged=True),
            _delta(changed=(), executable=("src/a.py",), unchanged=True),
            _delta(changed=("docs/a.md",), executable=("src/a.py",)),
            _delta(changed=(), unchanged=False),
            _delta(changed=("pkg/a.pyc",), unchanged=False),
        ]
        for delta in inconsistent:
            with self.subTest(delta=repr(delta)):
                self.assertEqual(
                    correction.classify_correction(delta, []),
                    "architectural")

    def test_findings_and_signals_that_cannot_be_trusted_are_architectural(
            self):
        for findings, signals in (
                ([_finding(risk_class="architectural")], None),
                ([_finding(risk_class="focused_code")], None),
                ([_finding(severity="critical")], None),
                ([_finding(severity=None)], None),
                (["not a dict"], None),
                ("not a list", None),
                ([_finding()], {"scope_escape": True}),
                ([_finding()], {"signal_malformed": True}),
                ([_finding()], "not a dict")):
            with self.subTest(findings=repr(findings), signals=signals):
                self.assertEqual(
                    correction.classify_correction(_UNCHANGED, findings,
                                                   signals),
                    "architectural")

    def test_a_clean_signal_set_does_not_raise_the_class(self):
        self.assertEqual(
            correction.classify_correction(
                _UNCHANGED, [_finding()],
                {"scope_escape": False, "signal_malformed": False}),
            "artifact_only")
        self.assertEqual(
            correction.classify_correction(_UNCHANGED, None, {}),
            "artifact_only")


class EnvelopeTests(unittest.TestCase):
    def test_the_verdict_is_required_for_every_class(self):
        for risk_class in correction.RISK_CLASSES:
            with self.subTest(risk_class=risk_class):
                self.assertTrue(correction.envelope_for(
                    risk_class)["reviewer_verdict_required"])

    def test_only_artifact_only_is_binding_eligible(self):
        eligible = [c for c in correction.RISK_CLASSES
                    if correction.envelope_for(c)["binding_eligible"]]
        self.assertEqual(eligible, ["artifact_only"])
        self.assertFalse(
            correction.envelope_for("artifact_only")["new_transaction"])

    def test_production_changing_classes_keep_the_complete_validation(self):
        for risk_class in ("documentation_metadata", "focused_code",
                           "architectural"):
            with self.subTest(risk_class=risk_class):
                envelope = correction.envelope_for(risk_class)
                self.assertTrue(envelope["complete_final_validation"])
                self.assertTrue(envelope["new_transaction"])

    def test_only_architectural_has_a_full_ceiling(self):
        ceilings = {c: correction.envelope_for(c)["review_scope_ceiling"]
                    for c in correction.RISK_CLASSES}
        self.assertEqual(
            ceilings,
            {"artifact_only": "targeted", "documentation_metadata": "targeted",
             "focused_code": "targeted", "architectural": "full"})

    def test_the_table_is_immutable_and_carries_no_policy_keys(self):
        with self.assertRaises(TypeError):
            correction.CORRECTION_ENVELOPES["extra"] = {}
        with self.assertRaises(TypeError):
            correction.CORRECTION_ENVELOPES["artifact_only"][
                "binding_eligible"] = False
        copy_of = correction.envelope_for("artifact_only")
        copy_of["binding_eligible"] = False
        self.assertTrue(
            correction.envelope_for("artifact_only")["binding_eligible"])
        keys = set(correction.envelope_for("focused_code"))
        self.assertEqual(
            keys, {"reviewer_verdict_required", "review_scope_ceiling",
                   "verification", "binding_eligible", "new_transaction",
                   "complete_final_validation"})

    def test_an_unknown_class_is_rejected(self):
        with self.assertRaises(ValueError):
            correction.envelope_for("unknown")


def _decide(policy="standard", delta=None, findings=None, signals=None,
            prior=_PRIOR, failure=False):
    if isinstance(policy, str):
        policy = _policy(policy)
    return correction.decide_review_scope(
        policy, _delta() if delta is None else delta,
        [_finding()] if findings is None else findings, signals, prior,
        failure)


class ScopeDecisionTests(unittest.TestCase):
    def assertDecision(self, decision, scope, code):
        self.assertEqual(decision, {"scope": scope, "reason_code": code})

    def test_targeted_ok_positives(self):
        for profile in ("light", "standard"):
            for delta in (_UNCHANGED, _delta()):
                with self.subTest(profile=profile, delta=repr(delta)):
                    self.assertDecision(
                        _decide(profile, delta), "targeted", "targeted_ok")

    def test_documentation_hit_on_a_dependency_is_targeted_without_executable(
            self):
        delta = _delta(dependency_hit=True)
        self.assertEqual(
            correction.classify_correction(delta, [_finding()]),
            "focused_code")
        self.assertDecision(_decide("standard", delta), "targeted",
                            "targeted_ok")

    def test_an_executable_delta_is_full(self):
        delta = _delta(changed=("src/a.py",), executable=("src/a.py",))
        self.assertDecision(_decide("standard", delta), "full",
                            "executable_delta_full")

    def test_assurance_unprofiled_and_no_reuse_are_full(self):
        no_reuse = dict(_policy("standard"), reuse_mode="none")
        for policy in (_policy("assurance"), None, no_reuse, "not a policy",
                       {"profile": "light"}):
            with self.subTest(policy=repr(policy)[:40]):
                decision = correction.decide_review_scope(
                    policy, _delta(), [_finding()], None, _PRIOR)
                self.assertDecision(decision, "full", "assurance_full")

    def test_an_architectural_finding_is_full(self):
        self.assertDecision(
            _decide(findings=[_finding(risk_class="architectural")]),
            "full", "architectural_full")

    def test_a_missing_or_unverifiable_prior_reference_is_full(self):
        for prior in (None, {}, "x",
                      {"recorded_manifest_digest": SHA_A},
                      {"verified_manifest_digest": SHA_A},
                      {"recorded_manifest_digest": "zz",
                       "verified_manifest_digest": SHA_A},
                      {"recorded_manifest_digest": SHA_A,
                       "verified_manifest_digest": None}):
            with self.subTest(prior=repr(prior)):
                self.assertDecision(_decide(prior=prior), "full",
                                    "prior_ref_missing_full")

    def test_a_mismatched_prior_reference_is_full(self):
        prior = {"recorded_manifest_digest": SHA_A,
                 "verified_manifest_digest": SHA_B}
        self.assertDecision(_decide(prior=prior), "full",
                            "prior_ref_mismatch_full")

    def test_a_reviewer_failure_retry_is_full(self):
        self.assertDecision(_decide(failure=True), "full",
                            "reviewer_failure_full")

    def test_malformed_signals_are_full(self):
        cases = [
            {"signals": {"signal_malformed": True}},
            {"findings": [_finding(severity="critical")]},
            {"findings": [_finding(risk_class="focused_code")]},
            {"delta": {"changed_paths": None, "executable_paths": None,
                       "candidate_unchanged": None}},
            {"signals": "not a dict"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=repr(kwargs)[:60]):
                self.assertDecision(_decide(**kwargs), "full",
                                    "malformed_signal_full")

    def test_a_scope_escape_is_full(self):
        self.assertDecision(
            _decide(signals={"scope_escape": True}), "full",
            "scope_escape_full")

    def test_a_severity_outside_the_revise_severities_is_full(self):
        self.assertNotIn("minor", _policy("standard")["thresholds"][
            "revise_severities"])
        self.assertDecision(
            _decide(findings=[_finding(severity="minor")]), "full",
            "severity_threshold_full")
        policy = dict(_policy("standard"), thresholds={})
        self.assertDecision(_decide(policy), "full",
                            "severity_threshold_full")

    def test_every_reason_code_is_reachable(self):
        seen = set()
        executable = _delta(changed=("src/a.py",), executable=("src/a.py",))
        for decision in (
                _decide(), _decide("assurance"),
                _decide(findings=[_finding(risk_class="architectural")]),
                _decide(delta=executable), _decide(prior=None),
                _decide(prior={"recorded_manifest_digest": SHA_A,
                               "verified_manifest_digest": SHA_B}),
                _decide(failure=True),
                _decide(signals={"signal_malformed": True}),
                _decide(signals={"scope_escape": True}),
                _decide(findings=[_finding(severity="minor")])):
            seen.add(decision["reason_code"])
        self.assertEqual(seen, set(correction.REASON_CODES))

    def test_precedence_resolves_toward_full(self):
        assurance = _policy("assurance")
        executable = _delta(changed=("src/a.py",), executable=("src/a.py",))
        self.assertDecision(
            _decide(assurance, signals={"scope_escape": True}, failure=True),
            "full", "reviewer_failure_full")
        self.assertDecision(
            _decide(assurance, signals={"scope_escape": True}), "full",
            "assurance_full")
        self.assertDecision(
            _decide(signals={"scope_escape": True,
                             "signal_malformed": True}),
            "full", "scope_escape_full")
        self.assertDecision(
            _decide(findings=[_finding(severity="minor",
                                       risk_class="architectural")]),
            "full", "architectural_full")
        self.assertDecision(
            _decide(delta=executable, findings=[_finding(severity="minor")]),
            "full", "severity_threshold_full")
        self.assertDecision(
            _decide(delta=executable, prior=None), "full",
            "executable_delta_full")

    def test_a_role_claim_never_yields_targeted(self):
        executable = _delta(changed=("src/a.py",), executable=("src/a.py",))
        claim = _finding(claimed_class="artifact_only",
                         claimed_scope="targeted")
        decision = _decide(
            delta=executable, findings=[claim],
            signals={"claimed_class": "artifact_only",
                     "review_scope": "targeted"})
        self.assertDecision(decision, "full", "executable_delta_full")

    def test_the_result_has_exactly_scope_and_reason_code(self):
        self.assertEqual(set(_decide()), {"scope", "reason_code"})

    def test_garbage_never_raises_and_is_full(self):
        for garbage in (5, "x", [], object()):
            with self.subTest(garbage=repr(garbage)[:30]):
                decision = correction.decide_review_scope(
                    _policy(), garbage, garbage, garbage, garbage)
                self.assertEqual(decision["scope"], "full")

    def test_the_decision_never_exceeds_the_envelope_ceiling(self):
        deltas = [
            _UNCHANGED, _delta(), _delta(dependency_hit=True),
            _delta(dependency_hit=None),
            _delta(changed=("src/a.py",), executable=("src/a.py",)),
            _delta(changed=("docs/a.md",), executable=("docs/a.md",)),
            _delta(changed=("docs/a.md",), unchanged=True),
            {"changed_paths": None},
        ]
        finding_sets = [
            [], [_finding()], [_finding(severity="minor")],
            [_finding(risk_class="architectural")],
            [_finding(risk_class="other")], [_finding(severity="bad")],
        ]
        signal_sets = [None, {}, {"scope_escape": True},
                       {"signal_malformed": True}]
        policies = [_policy("light"), _policy("standard"),
                    _policy("assurance"), None]
        priors = [_PRIOR, None, {"recorded_manifest_digest": SHA_A,
                                 "verified_manifest_digest": SHA_B}]
        for delta, findings, signals, policy, prior, failure in (
                itertools.product(deltas, finding_sets, signal_sets,
                                  policies, priors, (False, True))):
            decision = correction.decide_review_scope(
                policy, delta, findings, signals, prior, failure)
            self.assertIn(decision["reason_code"], correction.REASON_CODES)
            self.assertIn(decision["scope"], correction.REVIEW_SCOPES)
            self.assertEqual(decision["scope"] == "targeted",
                             decision["reason_code"] == "targeted_ok")
            if decision["scope"] == "targeted":
                risk_class = correction.classify_correction(
                    delta, findings, signals)
                self.assertEqual(
                    correction.envelope_for(risk_class)[
                        "review_scope_ceiling"], "targeted")
                self.assertFalse(failure)


class FindingImportPacketTests(unittest.TestCase):
    def _evidence(self, content=b"evidence"):
        root = _tempdir(self)
        path = os.path.join(root, "evidence.txt")
        with open(path, "wb") as fh:
            fh.write(content)
        return path, hashlib.sha256(content).hexdigest()

    def test_source_identity_is_kept_verbatim(self):
        packet = _import_packet(findings=[
            _import_finding(source_finding_id="F-0001",
                            source_session="S-source"),
            _import_finding(source_finding_id="X-9",
                            source_session="S-source")])
        self.assertEqual(
            [(e["source_finding_id"], e["source_session"])
             for e in packet["findings"]],
            [("F-0001", "S-source"), ("X-9", "S-source")])
        for entry in packet["findings"]:
            self.assertIsNone(entry["finding_id"])
            self.assertEqual(entry["state"], "open")

    def test_entries_carry_discovery_provenance(self):
        entry = _first_entry(_import_packet())
        self.assertEqual(entry["discoverer"], "planning-advisor")
        self.assertEqual(entry["round"], 2)
        self.assertEqual(entry["phase"], "planning")
        self.assertEqual(entry["source_record_sha256"], SHA_C)
        self.assertEqual(entry["severity"], "blocking")

    def test_the_packet_is_the_round_zero_import_record(self):
        packet = _import_packet()
        self.assertEqual(packet["kind"], "finding_import")
        self.assertEqual(packet["round"], 0)
        self.assertIsNone(packet["risk_class"])
        self.assertIsNone(packet["outcome"])
        self.assertIsNone(packet["review_scope"])
        self.assertEqual(packet["unresolved_basis"], _BASIS)
        self.assertEqual(set(packet["links"]), set(correction._LINK_KEYS))
        self.assertTrue(all(v is None for v in packet["links"].values()))

    def test_evidence_state_comes_from_the_file(self):
        path, sha = self._evidence()
        root = _tempdir(self)
        findings = [
            _import_finding(source_finding_id="F-1", evidence_path=path,
                            evidence_sha256=sha),
            _import_finding(source_finding_id="F-2", evidence_path=path,
                            evidence_sha256=SHA_B),
            _import_finding(source_finding_id="F-3",
                            evidence_path=os.path.join(root, "absent.txt"),
                            evidence_sha256=sha),
            _import_finding(source_finding_id="F-4", evidence_path=path,
                            evidence_sha256="not-a-digest"),
            _import_finding(source_finding_id="F-5", evidence_path=None,
                            evidence_sha256=sha),
        ]
        packet = _import_packet(findings=findings)
        self.assertEqual(
            [e["evidence_state"] for e in packet["findings"]],
            ["verified", "sha_mismatch", "missing", "missing", "missing"])
        self.assertEqual(len(packet["findings"]), 5)

    def test_evidence_state_for_directly(self):
        path, sha = self._evidence()
        self.assertEqual(correction.evidence_state_for(path, sha), "verified")
        self.assertEqual(correction.evidence_state_for(path, SHA_A),
                         "sha_mismatch")
        self.assertEqual(correction.evidence_state_for(None, sha), "missing")
        self.assertEqual(correction.evidence_state_for(path, None), "missing")
        self.assertEqual(
            correction.evidence_state_for(os.path.dirname(path), sha),
            "missing")

    def test_zero_findings_with_no_basis_is_valid(self):
        packet = _import_packet(findings=[], unresolved_basis="none")
        self.assertEqual(packet["findings"], [])
        self.assertEqual(packet["unresolved_basis"], "none")
        self.assertEqual(correction.validate_packet(packet), (True, None))

    def test_a_none_basis_with_findings_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            _import_packet(unresolved_basis="none")
        self.assertEqual(caught.exception.args[0], "unresolved_basis_invalid")

    def test_an_unknown_severity_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            _import_packet(findings=[_import_finding(severity="critical")])
        self.assertEqual(caught.exception.args[0], "severity_unknown")

    def test_a_missing_source_identity_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            _import_packet(findings=[_import_finding(source_finding_id=None)])
        self.assertEqual(caught.exception.args[0], "field_type")


class ScopedRenderTests(unittest.TestCase):
    EDGES_UNDER_TEST = (
        ("scout->scout-reviewer:review_resume", {"team": ["scout"]}),
        ("planner->planning-advisor:review_resume", {"team": ["planner"]}),
        ("builder->build-reviewer:review_resume", {"team": ["builder"]}),
        ("reviewer->lead:handback_revise", {"artifact_noun": "plan"}),
    )

    def _facts(self, base, scope="targeted", kind="correction", count=2,
               severity="major"):
        facts = dict(base)
        facts.update(correction_kind=kind, correction_scope=scope,
                     correction_finding_count=count,
                     correction_max_severity=severity)
        return facts

    def _arts(self, root, edge_id, packet=True):
        spec = handoff.EDGES[edge_id]
        sources = list(spec["required"]) + (
            ["correction_packet"] if packet else [])
        out = []
        for source in sources:
            path = os.path.join(root, source + ".txt")
            with open(path, "w") as fh:
                fh.write("placeholder %s\n" % source)
            out.append({"label": source, "path": path, "kind": "text",
                        "source": source})
        return out

    def _ctx(self, edge_id, **extra):
        ctx = ({"repos": []}
               if "repos" in (handoff.EDGES[edge_id].get("ctx_keys") or ())
               else {})
        ctx.update(extra)
        return ctx or None

    def _render(self, root, edge_id, base, packet=True, ctx_extra=None,
                **fact_kwargs):
        return handoff.render_handoff(
            edge_id, artifacts=self._arts(root, edge_id, packet),
            facts=self._facts(base, **fact_kwargs),
            ctx=self._ctx(edge_id, **(ctx_extra or {})))

    def test_targeted_names_the_packet_and_artifacts_and_drops_the_reread(
            self):
        for edge_id, base in self.EDGES_UNDER_TEST:
            with self.subTest(edge=edge_id):
                root = _tempdir(self)
                arts = self._arts(root, edge_id)
                block = self._render(root, edge_id, base)
                text = str(block)
                for art in arts:
                    self.assertIn(art["path"], text)
                packet_path = [a["path"] for a in arts
                               if a["source"] == "correction_packet"][0]
                self.assertIn("correction packet (typed bounded-correction "
                              "record)", text)
                self.assertIn(packet_path, [d["path"]
                                            for d in block.descriptors])
                self.assertIn("TARGETED", text)
                self.assertIn("verdict is still required", text)
                self.assertNotIn(handoff.FULL_REREAD_INSTRUCTION, text)
                self.assertIn("kind=correction scope=targeted findings=2 "
                              "max_severity=major", text)

    def test_full_scope_keeps_the_full_reread_instruction(self):
        for edge_id, base in self.EDGES_UNDER_TEST:
            with self.subTest(edge=edge_id):
                root = _tempdir(self)
                text = str(self._render(root, edge_id, base, scope="full"))
                self.assertNotIn("TARGETED", text)
                self.assertIn("scope=full", text)
                if edge_id != "reviewer->lead:handback_revise":
                    self.assertIn(handoff.FULL_REREAD_INSTRUCTION, text)

    def test_absent_facts_render_without_correction_text(self):
        for edge_id, base in self.EDGES_UNDER_TEST:
            with self.subTest(edge=edge_id):
                root = _tempdir(self)
                text = str(handoff.render_handoff(
                    edge_id, artifacts=self._arts(root, edge_id, False),
                    facts=base, ctx=self._ctx(edge_id)))
                self.assertNotIn("Correction (orchestrator-derived)", text)
                self.assertNotIn("TARGETED", text)

    def test_an_import_packet_may_ride_the_same_facts(self):
        root = _tempdir(self)
        edge_id, base = self.EDGES_UNDER_TEST[0]
        text = str(self._render(root, edge_id, base, kind="finding_import",
                                scope="full", count=0, severity="none"))
        self.assertIn("kind=finding_import scope=full findings=0 "
                      "max_severity=none", text)

    def test_targeted_without_the_packet_slot_fails_closed(self):
        for edge_id, base in self.EDGES_UNDER_TEST:
            with self.subTest(edge=edge_id):
                root = _tempdir(self)
                with self.assertRaises(handoff.MissingSourceError):
                    self._render(root, edge_id, base, packet=False)

    def test_correction_facts_must_travel_together(self):
        for edge_id, base in self.EDGES_UNDER_TEST:
            for missing in handoff.CORRECTION_FACT_KEYS:
                with self.subTest(edge=edge_id, missing=missing):
                    root = _tempdir(self)
                    facts = self._facts(base)
                    del facts[missing]
                    with self.assertRaises(handoff.ContentFreeError):
                        handoff.render_handoff(
                            edge_id, artifacts=self._arts(root, edge_id),
                            facts=facts, ctx=self._ctx(edge_id))

    def test_a_body_cannot_ride_a_correction_fact(self):
        edge_id, base = self.EDGES_UNDER_TEST[2]
        bad = {
            "correction_kind": ["free text with spaces", "other", None, 3],
            "correction_scope": ["partial", "free text", None, True],
            "correction_finding_count": [-1, True, "3", 1.5, None],
            "correction_max_severity": ["high", "free text", None, 0],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(fact=key, value=repr(value)):
                    root = _tempdir(self)
                    facts = self._facts(base)
                    facts[key] = value
                    with self.assertRaises(handoff.ContentFreeError):
                        handoff.render_handoff(
                            edge_id, artifacts=self._arts(root, edge_id),
                            facts=facts, ctx=self._ctx(edge_id))

    def test_correction_facts_on_an_undeclared_edge_are_rejected(self):
        root = _tempdir(self)
        path = os.path.join(root, "context.txt")
        with open(path, "w"):
            pass
        with self.assertRaises(handoff.ContentFreeError):
            handoff.render_handoff(
                "scout->planner:seed",
                artifacts=[{"label": "context", "path": path, "kind": "text",
                            "source": "context"},
                           {"label": "intel", "path": path, "kind": "text",
                            "source": "intel_json"}],
                facts=self._facts({}, scope="full"))

    def test_the_packet_slot_is_declared_only_where_it_renders(self):
        declared = {edge_id for edge_id, spec in handoff.EDGES.items()
                    if "correction_packet" in spec["sources"]}
        self.assertEqual(declared, {edge_id for edge_id, _b
                                    in self.EDGES_UNDER_TEST})
        with_facts = {edge_id for edge_id, spec in handoff.EDGES.items()
                      if "correction_kind" in spec["facts"]}
        self.assertEqual(with_facts, declared)

    def test_invalid_changed_paths_are_rejected(self):
        edge_id, base = self.EDGES_UNDER_TEST[2]
        bad = [["/abs/path"], ["../up"], ["a/../b"], ["-flag"], ["a b"],
               ["a\nb"], ["a;b"], ["a`b"], ["$(x)"], [], "docs/a.md",
               ["x"] * 201, [1], ["a/./b"], ["."], [""], ["x" * 257]]
        for value in bad:
            with self.subTest(value=repr(value)[:40]):
                root = _tempdir(self)
                with self.assertRaises(handoff.ContextError):
                    self._render(root, edge_id, base,
                                 ctx_extra={"changed_paths": value})

    def test_changed_paths_on_an_edge_that_does_not_declare_it_is_rejected(
            self):
        edge_id, base = self.EDGES_UNDER_TEST[0]
        root = _tempdir(self)
        with self.assertRaises(handoff.ContextError):
            self._render(root, edge_id, base,
                         ctx_extra={"changed_paths": ["docs/a.md"]})

    def test_the_scoped_recipe_lists_only_the_changed_paths(self):
        paths = ["docs/a.md", "src/b.py"]
        text = handoff.build_diff_recipe(None, paths)
        for path in paths:
            self.assertIn("    - " + path, text)
        self.assertIn("git status --porcelain", text)
        self.assertIn("scope escape", text)
        self.assertIn("untracked/new file", text)
        self.assertIn("git diff HEAD -- <the listed paths>", text)
        self.assertNotIn("--stat", text)
        self.assertNotIn("FULL working-tree delta", text)

    def test_the_scoped_recipe_covers_each_repo_root(self):
        text = handoff.build_diff_recipe(_TWO_REPOS, ["docs/a.md"])
        self.assertIn("git -C /neutral/repo-a status --porcelain", text)
        self.assertIn("git -C /neutral/repo-a diff HEAD -- <the listed paths>",
                      text)
        self.assertIn("git -C /neutral/repo-b diff --cached -- <the listed "
                      "paths>", text)
        self.assertIn("git -C /neutral/repo-b diff -- <the listed paths>",
                      text)
        self.assertIn("    - docs/a.md", text)

    def test_an_empty_or_absent_path_list_keeps_the_default_recipe(self):
        for value in (None, [], ()):
            self.assertEqual(handoff.build_diff_recipe(None, value),
                             handoff.build_diff_recipe(None))
            self.assertEqual(handoff.build_diff_recipe(_TWO_REPOS, value),
                             handoff.build_diff_recipe(_TWO_REPOS))

    def test_the_recipe_rejects_an_invalid_list(self):
        for value in (["/abs"], ["a b"], "docs/a.md", ["x"] * 201):
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(handoff.ContextError):
                    handoff.build_diff_recipe(None, value)

    def test_the_builder_review_scopes_its_recipe_when_targeted(self):
        edge_id, base = self.EDGES_UNDER_TEST[2]
        root = _tempdir(self)
        text = str(self._render(
            root, edge_id, base,
            ctx_extra={"changed_paths": ["docs/a.md", "src/b.py"]}))
        self.assertIn("    - docs/a.md", text)
        self.assertIn("    - src/b.py", text)
        self.assertIn("scope escape", text)
        self.assertNotIn("FULL working-tree delta", text)
        self.assertNotIn(handoff.FULL_REREAD_INSTRUCTION, text)

    def test_targeted_without_changed_paths_keeps_the_default_recipe(self):
        edge_id, base = self.EDGES_UNDER_TEST[2]
        root = _tempdir(self)
        text = str(self._render(root, edge_id, base))
        self.assertIn("FULL working-tree delta", text)
        self.assertNotIn(handoff.FULL_REREAD_INSTRUCTION, text)

    def test_changed_paths_are_ignored_unless_the_scope_is_targeted(self):
        edge_id, base = self.EDGES_UNDER_TEST[2]
        root = _tempdir(self)
        extra = {"changed_paths": ["docs/a.md"]}
        with_paths = str(self._render(root, edge_id, base, scope="full",
                                      ctx_extra=extra))
        without = str(self._render(root, edge_id, base, scope="full"))
        self.assertEqual(with_paths, without)
        plain = handoff.render_handoff(
            edge_id, artifacts=self._arts(root, edge_id, False), facts=base,
            ctx=self._ctx(edge_id, **extra))
        default = handoff.render_handoff(
            edge_id, artifacts=self._arts(root, edge_id, False), facts=base,
            ctx=self._ctx(edge_id))
        self.assertEqual(str(plain), str(default))

    def test_the_prompt_text_does_not_address_a_live_user(self):
        for edge_id, base in self.EDGES_UNDER_TEST:
            root = _tempdir(self)
            lowered = str(self._render(root, edge_id, base)).lower()
            for phrase in ("ask the user", "to the user", "with the user"):
                self.assertNotIn(phrase, lowered)


class ReviewerRoleDocTests(unittest.TestCase):
    ROLES = ("build-reviewer", "scout-reviewer", "planning-advisor")

    def _text(self, name):
        with open(os.path.join(_ROOT, "roles", name + ".md"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_each_reviewer_states_the_targeted_scope_contract(self):
        for name in self.ROLES:
            with self.subTest(role=name):
                text = self._text(name)
                self.assertIn("## Targeted re-review and source-finding "
                              "closure", text)
                for phrase in ("narrows what you re-read", "never whether",
                               "verdict is always required",
                               "closed_source_findings",
                               "only your verdict approves or closes"):
                    self.assertIn(phrase, " ".join(text.split()))
                self.assertIn("## Approval and authority", text)

    def test_the_closure_field_is_part_of_the_verdict_shape(self):
        for name in self.ROLES:
            with self.subTest(role=name):
                block = self._text(name).split("```json", 1)[1].split(
                    "```", 1)[0]
                self.assertIn('"closed_source_findings"', block)

    def test_the_build_reviewer_keeps_its_zero_corrective_findings_rule(self):
        text = " ".join(self._text("build-reviewer").split())
        self.assertIn("An approving round has zero corrective findings",
                      text)
        self.assertIn("an approving round still has zero corrective findings",
                      text)


if __name__ == "__main__":
    unittest.main()
