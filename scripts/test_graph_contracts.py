#!/usr/bin/env python3
"""Tests for cowork_graph: pure governed-graph contracts.

Every input is a neutral synthetic fixture: UUIDs built from integers,
digests of short tags, injected `now` strings and RootProbe dicts whose
paths are never touched. Nothing here reads or writes a root, spawns a
process or calls a provider.

Run standalone:

    python3 -m unittest scripts/test_graph_contracts.py -v
"""

import ast
import copy
import hashlib
import itertools
import os
import sys
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_execution_profiles as profiles  # noqa: E402
import cowork_graph as graph  # noqa: E402
import cowork_workunit  # noqa: E402


# --------------------------------------------------------------------------- #
# Neutral fixture builders.                                                   #
# --------------------------------------------------------------------------- #


def wid(n):
    return str(uuid.UUID(int=n))


def hex64(tag):
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def hex40(ch):
    return ch * 40


GRAPH_ID = wid(1000)
OTHER_GRAPH_ID = wid(2000)
OWNER_ID = "owner-a"
COMMIT = hex40("a")
NOW = "2026-01-01T00:00:00Z"
MAIN = "/work/main"
SESSIONS = "/state/sessions"


def later(seconds):
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "2026-01-01T%02d:%02d:%02dZ" % (hours, minutes, secs)


def ident(path):
    """A synthetic (dev, ino) identity, independent per path spelling."""
    return [7, int(hex64("inode:" + path)[:12], 16)]


def parents(path):
    """Ancestors of an absolute path, nearest first; none for a relative
    one (malformed fixtures never reach a probe check)."""
    out = []
    while path.startswith("/") and path != "/":
        path = path.rsplit("/", 1)[0] or "/"
        out.append(path)
    return out


def ancestors_of(path):
    return [ident(p) for p in parents(path)]


def session(n):
    return wid(5000 + n)


def manifest(n):
    return hex64("manifest-%d" % n)


def default_root(n):
    return "/work/wt/v%d" % n


def make_vertex(n, preds=(), profile="standard", root=None,
                authority_path=None):
    path = authority_path or "/work/authority/v%d.json" % n
    return {"work_id": wid(n), "root": root or default_root(n),
            "base_commit": COMMIT, "authority_path": path,
            "authority_digest": hex64("authority:" + path),
            "profile": profile, "predecessors": [wid(p) for p in preds]}


def make_revision(vertices, max_parallel=1, joins=None, claim_ttl_s=None):
    doc = {"schema_version": 1, "max_parallel": max_parallel,
           "vertices": copy.deepcopy(list(vertices))}
    if joins is not None:
        doc["joins"] = joins
    if claim_ttl_s is not None:
        doc["claim_ttl_s"] = claim_ttl_s
    return doc


def make_join(n, members):
    return {"join_id": wid(n), "rule": "all_succeeded",
            "requires": [wid(m) for m in members]}


def make_probe(root, **overrides):
    dev, ino = ident(root)
    probe = {"declared": root, "realpath": root, "exists": True,
             "is_dir": True, "dev": dev, "ino": ino,
             "path_has_symlink": False, "ancestors": ancestors_of(root),
             "toplevel_realpath": root, "head_commit": COMMIT,
             "main_checkout_realpath": MAIN,
             "main_checkout_dev_ino": ident(MAIN), "anchor_ignored": True}
    probe.update(overrides)
    return probe


def sessions_probe():
    dev, ino = ident(SESSIONS)
    return {"realpath": SESSIONS, "dev": dev, "ino": ino,
            "ancestors": ancestors_of(SESSIONS)}


def probes_for(vertices):
    return {v["work_id"].lower(): make_probe(v["root"]) for v in vertices}


def authority_for(vertices):
    return {v["work_id"].lower(): ("authority:" + v["authority_path"]).encode(
        "utf-8") for v in vertices}


def admit(state, doc, probes=None, authority=None, foreign=None,
          now=NOW, policy_fn=None):
    vertices = doc["vertices"]
    return graph.admit(
        state, doc, probes_for(vertices) if probes is None else probes,
        sessions_probe(), [] if foreign is None else foreign,
        authority_for(vertices) if authority is None else authority, now,
        policy_fn=policy_fn)


def fresh(vertices, max_parallel=1, joins=None, claim_ttl_s=None):
    return admit(graph.new_state(GRAPH_ID),
                 make_revision(vertices, max_parallel, joins, claim_ttl_s))


def chain(*specs):
    """Vertices from (n, preds) pairs."""
    return [make_vertex(n, preds) for n, preds in specs]


def root_pair(state, n):
    record = state["vertices"][wid(n)]
    revision = record["claim_revision"] or state["revision"]
    for vertex in state["revisions"][revision - 1]["vertices"]:
        if vertex["work_id"] == wid(n):
            return [vertex["root_dev"], vertex["root_ino"]]
    raise AssertionError("vertex %d not in revision" % n)


def claimed(state, n, now=NOW):
    return graph.claim(state, wid(n), now)[0]


def bound(state, n, now=NOW, profile="standard"):
    state = claimed(state, n, now)
    epoch = state["vertices"][wid(n)]["lease_epoch"]
    return graph.bind(state, wid(n), epoch, session(n), root_pair(state, n),
                      profile, False, now=now)


def make_txn_facts(session_uuid, repo_pair, digest_value, **overrides):
    facts = {"transaction_id": "txn-" + session_uuid[-4:],
             "request_session_uuid": session_uuid,
             "request_repo_dev_ino": repo_pair,
             "request_manifest_digest": digest_value,
             "result_manifest_digest": digest_value, "verdict": "green",
             "final_suite_binding": "components_ran_once",
             "disposition": "accepted", "inventory_labels": ["suite"]}
    facts.update(overrides)
    return facts


def txn_for(state, n, **overrides):
    return make_txn_facts(session(n), root_pair(state, n), manifest(n),
                          **overrides)


def receipt_for(state, n, txn, now=NOW, policy_fn=None):
    return graph.build_receipt(state, wid(n), session(n), OWNER_ID, 1, txn,
                               now, policy_fn=policy_fn)


def publish_vertex(state, n, now=NOW, **txn_overrides):
    txn = txn_for(state, n, **txn_overrides)
    receipt = receipt_for(state, n, txn, now)
    return graph.publish(state, receipt, txn, receipt["manifest_digest"],
                         True, now)


def holder(verdict, paused=False):
    return {"owner_verdict": verdict, "pause_lease_live": paused}


def patched_cap(n):
    """Every shipped profile with a bounded concurrency cap of n."""
    definitions = {}
    for name, definition in profiles.PROFILE_DEFINITIONS.items():
        thawed = profiles._thaw(definition)
        thawed["concurrency"] = {"contract_version": 1, "mode": "bounded",
                                 "max_parallel_vertices": n,
                                 "fan_out_shapes": []}
        definitions[name] = thawed
    return mock.patch.object(profiles, "PROFILE_DEFINITIONS",
                             profiles._freeze(definitions))


def policy_with(**concurrency):
    def policy_fn(profile, role):
        policy = profiles.resolved_vertex_policy("standard", role)
        policy["profile"] = profile
        policy["concurrency"].update(concurrency)
        return policy
    return policy_fn


def keys_at_any_depth(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            for sub in keys_at_any_depth(inner):
                yield sub
    elif isinstance(value, list):
        for inner in value:
            for sub in keys_at_any_depth(inner):
                yield sub


class RefusalAssertions(object):
    def assertRefused(self, code, fn, *args, **kwargs):
        with self.assertRaises(graph.GraphRefusal) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def assertUnchangedRefusal(self, code, state, fn, *args, **kwargs):
        before = graph.canonical_json(state)
        refusal = self.assertRefused(code, fn, state, *args, **kwargs)
        self.assertEqual(graph.canonical_json(state), before)
        return refusal


# --------------------------------------------------------------------------- #
# Admission.                                                                  #
# --------------------------------------------------------------------------- #


class AdmissionStructureTests(RefusalAssertions, unittest.TestCase):
    def test_acyclic_revision_is_admitted_with_one_revision(self):
        state = fresh(chain((1, ()), (2, (1,)), (3, (1, 2))))
        self.assertEqual(state["revision"], 1)
        self.assertEqual(len(state["revisions"]), 1)
        self.assertEqual(graph.derive_statuses(state), {
            wid(1): "ready", wid(2): "waiting", wid(3): "waiting"})
        record = state["vertices"][wid(2)]
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["lease_epoch"], 0)
        self.assertEqual(record["first_admitted_revision"], 1)
        self.assertEqual([e["op"] for e in state["events"]], ["admit"])
        entry = state["revisions"][0]
        self.assertEqual(entry["effective_cap"], 1)
        self.assertEqual(entry["claim_ttl_s"], graph.DEFAULT_CLAIM_TTL_S)
        self.assertEqual(entry["vertices"][0]["root_realpath"],
                         default_root(1))

    def test_cycle_refused(self):
        doc = make_revision(chain((1, (2,)), (2, (1,))))
        self.assertUnchangedRefusal("cycle", graph.new_state(GRAPH_ID),
                                    admit, doc)

    def test_self_edge_refused(self):
        doc = make_revision(chain((1, (1,))))
        self.assertRefused("self_edge", admit, graph.new_state(GRAPH_ID), doc)

    def test_dangling_predecessor_refused(self):
        doc = make_revision(chain((1, (9,))))
        refusal = self.assertRefused("dangling_predecessor", admit,
                                     graph.new_state(GRAPH_ID), doc)
        self.assertEqual(refusal.work_id, wid(1))

    def test_duplicate_work_id_refused(self):
        doc = make_revision([make_vertex(1), make_vertex(1, root="/work/x")])
        self.assertRefused("duplicate_work_id", admit,
                           graph.new_state(GRAPH_ID), doc)

    def test_malformed_fields_refused_as_revision_malformed(self):
        def variant(**changes):
            vertex = make_vertex(1)
            vertex.update(changes)
            return make_revision([vertex])

        extra = make_vertex(1)
        extra["extra"] = True
        cases = [
            variant(work_id="not-a-uuid"), variant(base_commit="abc"),
            variant(root="relative/path"), make_revision([extra]),
            variant(predecessors=[wid(2), wid(2)]),
            variant(profile=""), variant(authority_path="rel"),
            {"schema_version": 1, "vertices": [make_vertex(1)]},
            dict(make_revision([make_vertex(1)]), schema_version=2),
            dict(make_revision([make_vertex(1)]), claim_ttl_s=10),
            dict(make_revision([make_vertex(1)]), unknown=1),
            make_revision([]),
        ]
        for doc in cases:
            with self.subTest(doc=doc):
                self.assertRefused("revision_malformed", admit,
                                   graph.new_state(GRAPH_ID), doc)

    def test_distinct_candidates_fan_in_is_not_cross_candidate(self):
        state = fresh(chain((1, ()), (2, ()), (3, (1, 2))))
        self.assertEqual(state["revision"], 1)
        self.assertEqual(graph.derive_statuses(state)[wid(3)], "waiting")

    def test_unknown_join_member_refused(self):
        doc = make_revision([make_vertex(1)], joins=[make_join(80, (1, 9))])
        refusal = self.assertRefused("join_unknown_member", admit,
                                     graph.new_state(GRAPH_ID), doc)
        self.assertEqual(refusal.work_id, wid(9))

    def test_malformed_join_refused_as_join_malformed(self):
        good = make_join(80, (1,))
        cases = [
            [dict(good, rule="any_succeeded")], [dict(good, requires=[])],
            [dict(good, requires=[wid(1), wid(1)])],
            [dict(good, join_id="not-a-uuid")], [good, dict(good)],
            [dict(good, extra=1)], {"join": good},
        ]
        for joins in cases:
            with self.subTest(joins=joins):
                doc = make_revision([make_vertex(1)], joins=joins)
                self.assertRefused("join_malformed", admit,
                                   graph.new_state(GRAPH_ID), doc)

    def test_structural_check_delegates_to_validate_revision(self):
        doc = make_revision([make_vertex(1), make_vertex(1, root="/work/x")])
        with mock.patch.object(cowork_workunit, "validate_revision",
                               wraps=cowork_workunit.validate_revision) as spy:
            self.assertRefused("duplicate_work_id", admit,
                               graph.new_state(GRAPH_ID), doc)
        spy.assert_called_once()
        projection = spy.call_args[0][0]
        self.assertIsInstance(projection, list)
        self.assertEqual([n["work_id"] for n in projection], [wid(1), wid(1)])
        for node in projection:
            self.assertIsNone(node["candidate_manifest_digest"])
            self.assertIsNone(node["candidate_index"])
            self.assertIsNone(node["governed_child_policy"])


class AdmissionRootTests(RefusalAssertions, unittest.TestCase):
    def refuse(self, code, vertices, probes=None, foreign=None):
        doc = make_revision(vertices)
        return self.assertRefused(code, admit, graph.new_state(GRAPH_ID),
                                  doc, probes=probes, foreign=foreign)

    def test_equal_realpaths_are_candidate_collision(self):
        self.refuse("candidate_collision",
                    [make_vertex(1), make_vertex(2, root=default_root(1))])

    def test_symlinked_declared_root_refused(self):
        vertex = make_vertex(1)
        self.refuse("root_symlink", [vertex], probes={
            wid(1): make_probe(vertex["root"], realpath="/work/real/v1")})
        self.refuse("root_symlink", [vertex], probes={
            wid(1): make_probe(vertex["root"], path_has_symlink=True)})

    def test_inode_alias_with_different_spelling_refused(self):
        v1, v2 = make_vertex(1), make_vertex(2, root="/work/wt/V1")
        dev, ino = ident(v1["root"])
        probes = probes_for([v1, v2])
        probes[wid(2)].update(dev=dev, ino=ino)
        self.refuse("root_alias", [v1, v2], probes=probes)

    def test_nested_roots_refused_both_directions(self):
        outer, inner = "/work/wt/a", "/work/wt/a/b"
        self.refuse("root_nested", [make_vertex(1, root=outer),
                                    make_vertex(2, root=inner)])
        self.refuse("root_nested", [make_vertex(1, root=inner),
                                    make_vertex(2, root=outer)])

    def test_case_variant_nesting_detected_by_ancestor_identity(self):
        v1 = make_vertex(1, root="/work/wt/a")
        v2 = make_vertex(2, root="/work/WT/A/b")
        probes = probes_for([v1, v2])
        probes[wid(2)]["ancestors"] = [ident("/work/wt/a")] + ancestors_of(
            "/work/wt/a")
        self.refuse("root_nested", [v1, v2], probes=probes)

    def test_root_equal_or_containing_main_checkout_refused(self):
        for root in (MAIN, "/work", "/"):
            with self.subTest(root=root):
                self.refuse("root_overlaps_main_checkout",
                            [make_vertex(1, root=root)])
        alias = make_vertex(1, root="/work/MAIN")
        dev, ino = ident(MAIN)
        self.refuse("root_overlaps_main_checkout", [alias], probes={
            wid(1): make_probe(alias["root"], dev=dev, ino=ino)})

    def test_root_inside_main_checkout_allowed(self):
        state = fresh([make_vertex(1, root=MAIN + "/.worktrees/v1")])
        self.assertEqual(state["revision"], 1)

    def test_root_touching_sessions_root_refused(self):
        for root in (SESSIONS, SESSIONS + "/inner", "/state"):
            with self.subTest(root=root):
                self.refuse("root_overlaps_sessions_root",
                            [make_vertex(1, root=root)])

    def test_non_toplevel_root_refused(self):
        vertex = make_vertex(1)
        self.refuse("root_not_worktree_toplevel", [vertex], probes={
            wid(1): make_probe(vertex["root"],
                               toplevel_realpath="/work/wt")})

    def test_base_commit_mismatch_refused(self):
        vertex = make_vertex(1)
        self.refuse("base_commit_mismatch", [vertex], probes={
            wid(1): make_probe(vertex["root"], head_commit=hex40("b"))})

    def test_unignored_anchor_dir_refused(self):
        vertex = make_vertex(1)
        self.refuse("anchor_dir_not_ignored", [vertex], probes={
            wid(1): make_probe(vertex["root"], anchor_ignored=False)})

    def test_root_in_use_by_other_graph_refused(self):
        vertex = make_vertex(1)
        for other in (vertex["root"], vertex["root"] + "/sub", "/work/wt"):
            with self.subTest(other=other):
                dev, ino = ident(other)
                refusal = self.refuse("root_in_use", [vertex], foreign=[{
                    "graph_id": OTHER_GRAPH_ID, "work_id": wid(2001),
                    "realpath": other, "dev": dev, "ino": ino,
                    "ancestors": ancestors_of(other)}])
                self.assertEqual(refusal.detail["graph_id"], OTHER_GRAPH_ID)

    def test_shared_base_commit_is_allowed(self):
        v1, v2 = make_vertex(1), make_vertex(2)
        self.assertEqual(v1["base_commit"], v2["base_commit"])
        self.assertEqual(fresh([v1, v2])["revision"], 1)

    def test_missing_or_null_probe_fields_fail_closed(self):
        vertex = make_vertex(1)
        root = vertex["root"]
        cases = [
            ("root_missing", {}),
            ("root_missing", {wid(1): make_probe(root, exists=False)}),
            ("root_missing", {wid(1): make_probe(root, dev=None)}),
            ("root_missing", {wid(1): make_probe(root, declared="/other")}),
            ("base_commit_mismatch", {wid(1): make_probe(
                root, head_commit=None)}),
            ("root_not_worktree_toplevel", {wid(1): make_probe(
                root, toplevel_realpath=None)}),
            ("root_overlaps_main_checkout", {wid(1): make_probe(
                root, main_checkout_realpath=None)}),
            ("root_overlaps_main_checkout", {wid(1): make_probe(
                root, main_checkout_dev_ino=None)}),
        ]
        for code, probes in cases:
            with self.subTest(code=code, probes=probes):
                self.refuse(code, [vertex], probes=probes)


class AdmissionAuthorityTests(RefusalAssertions, unittest.TestCase):
    def refuse(self, code, vertices, authority):
        return self.assertRefused(code, admit, graph.new_state(GRAPH_ID),
                                  make_revision(vertices),
                                  authority=authority)

    def test_missing_authority_refused(self):
        vertex = make_vertex(1)
        for authority in ({}, {wid(1): None}, {wid(1): "text"}):
            with self.subTest(authority=authority):
                self.refuse("authority_missing", [vertex], authority)

    def test_malformed_digest_refused(self):
        for bad in (hex64("x").upper(), hex64("x")[:63]):
            with self.subTest(digest=bad):
                vertex = dict(make_vertex(1), authority_digest=bad)
                self.refuse("authority_malformed", [vertex],
                            authority_for([vertex]))

    def test_digest_mismatch_refused(self):
        vertex = make_vertex(1)
        self.refuse("authority_digest_mismatch", [vertex],
                    {wid(1): b"different bytes"})

    def test_shared_authority_refused(self):
        shared = "/work/authority/shared.json"
        vertices = [make_vertex(1, authority_path=shared),
                    make_vertex(2, authority_path=shared)]
        refusal = self.refuse("authority_shared", vertices,
                              authority_for(vertices))
        self.assertEqual(refusal.work_id, wid(2))

    def test_distinct_authorities_admitted(self):
        state = fresh([make_vertex(1), make_vertex(2)])
        digests = {v["authority_digest"]
                   for v in state["revisions"][0]["vertices"]}
        self.assertEqual(len(digests), 2)


class AdmissionPolicyTests(RefusalAssertions, unittest.TestCase):
    def test_unknown_profile_refused(self):
        doc = make_revision([make_vertex(1, profile="turbo")])
        self.assertRefused("unknown_profile", admit,
                           graph.new_state(GRAPH_ID), doc)

    def test_shipped_profiles_effective_cap_is_one(self):
        for name in profiles.PROFILES:
            with self.subTest(profile=name):
                state = fresh([make_vertex(1, profile=name)])
                self.assertEqual(graph.effective_cap(state), 1)
                self.assertEqual(state["revisions"][0]["policy_cap"], 1)

    def test_ceiling_above_shipped_policy_refused(self):
        doc = make_revision([make_vertex(1), make_vertex(2)], max_parallel=2)
        self.assertRefused("ceiling_above_policy", admit,
                           graph.new_state(GRAPH_ID), doc)

    def test_patched_cap_admits_bounded_ceiling(self):
        vertices = [make_vertex(n) for n in (1, 2, 3, 4)]
        with patched_cap(3):
            state = fresh(vertices, max_parallel=3)
            self.assertEqual(graph.effective_cap(state), 3)
            self.assertEqual(state["revisions"][0]["policy_cap"], 3)
            self.assertRefused("ceiling_above_policy", admit,
                               graph.new_state(GRAPH_ID),
                               make_revision(vertices, max_parallel=4))

    def test_unsupported_contract_version_refused(self):
        def no_concurrency(profile, role):
            return {"profile": profile}

        for policy_fn in (policy_with(contract_version=2),
                          policy_with(max_parallel_vertices=0),
                          policy_with(contract_version=True),
                          no_concurrency):
            with self.subTest(policy_fn=policy_fn):
                self.assertRefused(
                    "unsupported_concurrency_contract", admit,
                    graph.new_state(GRAPH_ID),
                    make_revision([make_vertex(1)]), policy_fn=policy_fn)

    def test_invalid_ceiling_refused(self):
        for ceiling in (0, -1, "2", True, None):
            with self.subTest(ceiling=ceiling):
                self.assertRefused(
                    "ceiling_invalid", admit, graph.new_state(GRAPH_ID),
                    make_revision([make_vertex(1)], max_parallel=ceiling))

    def test_policy_consulted_with_builder_role(self):
        calls = []

        def spy(profile, role):
            calls.append((profile, role))
            return profiles.resolved_vertex_policy(profile, role)

        doc = make_revision([make_vertex(1, profile="standard"),
                             make_vertex(2, profile="light")])
        admit(graph.new_state(GRAPH_ID), doc, policy_fn=spy)
        self.assertEqual(calls, [("light", "builder"),
                                 ("standard", "builder")])
        with mock.patch.object(profiles, "resolved_vertex_policy",
                               wraps=profiles.resolved_vertex_policy) as wrap:
            fresh([make_vertex(1)])
        wrap.assert_called_once_with("standard", "builder")


class RevisionSupersessionTests(RefusalAssertions, unittest.TestCase):
    def test_new_revision_keeps_claimed_vertices_identical(self):
        vertices = [make_vertex(1), make_vertex(2)]
        state = claimed(fresh(vertices), 1)
        slots = copy.deepcopy(state["slots"])
        state = admit(state, make_revision(vertices + [make_vertex(3)]))
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["vertices"][wid(1)]["state"], "claimed")
        self.assertEqual(state["vertices"][wid(1)]["claim_revision"], 1)
        self.assertEqual(state["slots"], slots)
        self.assertEqual(state["vertices"][wid(3)]["first_admitted_revision"],
                         2)

    def test_changed_declaration_of_claimed_vertex_refused(self):
        state = claimed(fresh([make_vertex(1)]), 1)
        changed = make_vertex(1, profile="light")
        self.assertUnchangedRefusal("revision_conflict", state, admit,
                                    make_revision([changed]))

    def test_dropping_running_vertex_refused(self):
        state = bound(fresh([make_vertex(1), make_vertex(2)]), 1)
        refusal = self.assertUnchangedRefusal(
            "revision_conflict", state, admit,
            make_revision([make_vertex(2)]))
        self.assertEqual(refusal.work_id, wid(1))

    def test_reused_work_id_with_new_declaration_refused(self):
        state = fresh([make_vertex(1), make_vertex(2)])
        moved = make_vertex(2, root="/work/wt/moved")
        self.assertRefused("revision_conflict", admit, state,
                           make_revision([make_vertex(1), moved]))
        dropped = admit(state, make_revision([make_vertex(1)]))
        self.assertRefused("revision_conflict", admit, dropped,
                           make_revision([make_vertex(1), moved]))

    def test_failed_work_reauthorized_under_new_id(self):
        v1 = make_vertex(1)
        state = claimed(fresh([v1]), 1)
        state = graph.fail(state, wid(1), "abandoned", None, NOW)
        v3 = make_vertex(3, root=v1["root"])
        state2 = admit(state, make_revision([v1, v3]),
                       probes=probes_for([v3]), authority=authority_for([v3]))
        self.assertEqual(state2["vertices"][wid(1)]["state"], "failed")
        self.assertEqual(graph.ready_order(state2), [wid(3)])
        self.assertRefused("revision_conflict", admit, state,
                           make_revision([v3]))

    def test_cancelled_graph_refuses_admission(self):
        state, _ = graph.cancel(fresh([make_vertex(1)]), None, {}, NOW)
        self.assertUnchangedRefusal("graph_cancelled", state, admit,
                                    make_revision([make_vertex(2)]))

    def test_dropped_pending_vertex_is_not_claimable(self):
        state = fresh([make_vertex(1), make_vertex(2)])
        state = admit(state, make_revision([make_vertex(1)]))
        self.assertNotIn(wid(2), graph.derive_statuses(state))
        self.assertRefused("vertex_unknown", graph.claim, state, wid(2), NOW)
        state = admit(state, make_revision([make_vertex(1), make_vertex(2)]))
        self.assertEqual(state["vertices"][wid(2)]["first_admitted_revision"],
                         1)
        state, info = graph.claim(state, wid(2), NOW)
        self.assertEqual(info["work_id"], wid(2))

    def test_terminal_vertex_needs_no_probe_or_authority_in_later_revision(
            self):
        vertices = chain((1, ()), (2, (1,)))
        state = publish_vertex(bound(fresh(vertices), 1), 1)
        later_state = admit(state, make_revision(vertices),
                            probes=probes_for(vertices[1:]),
                            authority=authority_for(vertices[1:]))
        first = state["revisions"][0]["vertices"][0]
        carried = later_state["revisions"][1]["vertices"][0]
        for key in ("root_realpath", "root_dev", "root_ino"):
            self.assertEqual(carried[key], first[key])
        self.assertEqual(graph.ready_order(later_state), [wid(2)])


# --------------------------------------------------------------------------- #
# Slot ledger.                                                                #
# --------------------------------------------------------------------------- #


class SlotTransitionTests(RefusalAssertions, unittest.TestCase):
    def test_claim_acquires_one_slot_and_bumps_epoch(self):
        state = claimed(fresh([make_vertex(1)]), 1)
        record = state["vertices"][wid(1)]
        self.assertEqual(record["state"], "claimed")
        self.assertEqual(record["lease_epoch"], 1)
        self.assertEqual(record["claim_revision"], 1)
        self.assertEqual(len(state["slots"]), 1)
        self.assertEqual(graph.held_slots(state)[0]["lease_epoch"], 1)
        self.assertEqual(state["events"][-1]["op"], "claim")

    def test_claim_info_shape_and_deadline(self):
        state, info = graph.claim(fresh([make_vertex(1)], claim_ttl_s=120),
                                  None, NOW)
        self.assertEqual(info, {
            "graph_id": GRAPH_ID, "work_id": wid(1), "lease_epoch": 1,
            "root": default_root(1), "profile": "standard",
            "authority_digest": make_vertex(1)["authority_digest"],
            "claim_deadline": later(120), "cwd": default_root(1),
            "launch_argv": ["--new", "--profile", "standard",
                            "--graph-vertex",
                            "%s:%s:1" % (GRAPH_ID, wid(1))]})
        _, info = graph.claim(fresh([make_vertex(1)]), None, NOW)
        self.assertEqual(info["claim_deadline"], later(900))
        _, info = graph.claim(fresh([make_vertex(1)], claim_ttl_s=120), None,
                              "2026-01-01T00:00:00.5Z")
        self.assertEqual(info["claim_deadline"],
                         "2026-01-01T00:02:00.500000Z")

    def test_shipped_cap_refuses_second_claim(self):
        state = claimed(fresh([make_vertex(1), make_vertex(2)]), 1)
        self.assertUnchangedRefusal("cap_reached", state, graph.claim, None,
                                    NOW)
        self.assertUnchangedRefusal("cap_reached", state, graph.claim,
                                    wid(2), NOW)

    def test_patched_cap_n_bounds_exactly_n(self):
        for n in (2, 3):
            with self.subTest(n=n), patched_cap(n):
                state = fresh([make_vertex(i) for i in range(1, n + 2)],
                              max_parallel=n)
                for _ in range(n):
                    state, _info = graph.claim(state, None, NOW)
                self.assertEqual(len(graph.held_slots(state)), n)
                self.assertRefused("cap_reached", graph.claim, state, None,
                                   NOW)

    def test_n_plus_one_claimable_only_after_release(self):
        with patched_cap(2):
            state = fresh([make_vertex(i) for i in (1, 2, 3)], max_parallel=2)
            state = claimed(claimed(state, 1), 2)
            self.assertRefused("cap_reached", graph.claim, state, wid(3),
                               NOW)
            state = graph.fail(state, wid(1), "abandoned", None, NOW)
            state, info = graph.claim(state, wid(3), NOW)
        self.assertEqual(info["work_id"], wid(3))
        self.assertEqual(len(graph.held_slots(state)), 2)

    def test_duplicate_claim_of_held_vertex_refused(self):
        state = claimed(fresh([make_vertex(1)]), 1)
        self.assertUnchangedRefusal("vertex_held", state, graph.claim,
                                    wid(1), NOW)
        state = bound(fresh([make_vertex(1)]), 1)
        self.assertUnchangedRefusal("vertex_held", state, graph.claim,
                                    wid(1), NOW)

    def test_release_happens_exactly_once_per_acquisition(self):
        with patched_cap(3):
            state = fresh([make_vertex(i) for i in (1, 2, 3)], max_parallel=3)
            state = publish_vertex(bound(state, 1), 1)
            state = graph.fail(claimed(state, 2), wid(2), "abandoned", None,
                               NOW)
            state = graph.reclaim(claimed(state, 3), wid(3), None,
                                  later(900))
            state, _ = graph.cancel(claimed(state, 3), wid(3), {}, NOW)
        reasons = [(s["work_id"], s["lease_epoch"], s["release_reason"])
                   for s in state["slots"]]
        self.assertEqual(reasons, [
            (wid(1), 1, "terminal_receipt"), (wid(2), 1, "failed"),
            (wid(3), 1, "reclaim"), (wid(3), 3, "cancelled")])
        self.assertEqual(graph.held_slots(state), [])

    def test_duplicate_release_refused_state_unchanged(self):
        state = claimed(fresh([make_vertex(1), make_vertex(2)]), 1)
        failed = graph.fail(state, wid(1), "abandoned", None, NOW)
        self.assertUnchangedRefusal("vertex_terminal", failed, graph.fail,
                                    wid(1), "abandoned", None, NOW)
        reclaimed = graph.reclaim(state, wid(1), None, later(900))
        self.assertUnchangedRefusal("vertex_not_claimed", reclaimed,
                                    graph.reclaim, wid(1), None, later(900))
        scratch = copy.deepcopy(failed)
        self.assertRefused("graph_state_inconsistent", graph._release_slot,
                           scratch, wid(1), 1, "failed", NOW)

    def test_invariant_check_rejects_forged_states(self):
        base = claimed(fresh([make_vertex(1), make_vertex(2)]), 1)

        over_cap = copy.deepcopy(base)
        over_cap["vertices"][wid(2)].update(
            state="claimed", lease_epoch=1, claim_revision=1, claimed_at=NOW,
            claim_deadline=later(900))
        over_cap["slots"].append({"seq": 2, "work_id": wid(2),
                                  "lease_epoch": 1, "acquired_at": NOW,
                                  "released_at": None,
                                  "release_reason": None})

        double_release = graph.fail(base, wid(1), "abandoned", None, NOW)
        double_release["slots"].append(dict(double_release["slots"][0],
                                            seq=2))

        orphan = fresh([make_vertex(1), make_vertex(2)])
        orphan["slots"].append({"seq": 1, "work_id": wid(2),
                                "lease_epoch": 0, "acquired_at": NOW,
                                "released_at": None, "release_reason": None})

        with patched_cap(2):
            shared = fresh([make_vertex(1), make_vertex(2)], max_parallel=2)
            shared = claimed(bound(shared, 1), 2)
        shared["vertices"][wid(2)].update(state="running",
                                          session_uuid=session(1))

        for name, forged in (("over cap", over_cap),
                             ("double release", double_release),
                             ("orphan slot", orphan),
                             ("shared session", shared)):
            with self.subTest(forged=name):
                self.assertRefused("graph_state_inconsistent",
                                   graph.check_invariants, forged)
                self.assertUnchangedRefusal("graph_state_inconsistent",
                                            forged, graph.claim, None, NOW)
        corrupt = copy.deepcopy(base)
        del corrupt["slots"]
        self.assertRefused("graph_state_corrupt", graph.claim, corrupt,
                           None, NOW)

    def test_lowered_cap_blocks_new_claims_without_preemption(self):
        vertices = [make_vertex(i) for i in (1, 2, 3)]
        with patched_cap(2):
            state = fresh(vertices, max_parallel=2)
            state = claimed(claimed(state, 1), 2)
            state = admit(state, make_revision(vertices, max_parallel=1))
        self.assertEqual(graph.effective_cap(state), 1)
        self.assertEqual(len(graph.held_slots(state)), 2)
        graph.check_invariants(state)
        self.assertRefused("cap_reached", graph.claim, state, wid(3), NOW)
        state = graph.fail(state, wid(1), "abandoned", None, NOW)
        self.assertRefused("cap_reached", graph.claim, state, wid(3), NOW)
        state = graph.fail(state, wid(2), "abandoned", None, NOW)
        state, info = graph.claim(state, wid(3), NOW)
        self.assertEqual(info["work_id"], wid(3))

    def test_malformed_now_is_argument_error(self):
        state = fresh([make_vertex(1)])
        for now in ("2026-01-01 00:00:00", "2026-13-01T00:00:00Z", None,
                    "2026-01-01T00:00:00+00:00", "2026-02-30T00:00:00Z"):
            with self.subTest(now=now):
                self.assertUnchangedRefusal("argument_error", state,
                                            graph.claim, None, now)

    def test_status_view_shape(self):
        state = claimed(fresh([make_vertex(1), make_vertex(2)],
                              joins=[make_join(80, (1, 2))]), 1)
        view = graph.status_view(state)
        self.assertEqual(set(view), {"graph_id", "revision", "cancelled",
                                     "effective_cap", "held_slots",
                                     "ready_order", "statuses", "joins"})
        self.assertEqual(view["held_slots"], [{
            "seq": 1, "work_id": wid(1), "lease_epoch": 1,
            "acquired_at": NOW}])
        self.assertEqual(view["statuses"], {wid(1): "claimed",
                                            wid(2): "ready"})
        self.assertEqual(view["ready_order"], [wid(2)])
        self.assertEqual((view["revision"], view["effective_cap"],
                          view["cancelled"], view["joins"]),
                         (1, 1, False, {}))


class BindAndFenceTests(RefusalAssertions, unittest.TestCase):
    def setUp(self):
        self.state = claimed(fresh([make_vertex(1), make_vertex(2)]), 1)
        self.root = root_pair(self.state, 1)

    def bind(self, state, epoch=1, sess=None, cwd=None, profile="standard",
             conflict=False):
        return graph.bind(state, wid(1), epoch, sess or session(1),
                          cwd or self.root, profile, conflict, now=NOW)

    def test_bind_marks_running(self):
        state = self.bind(self.state)
        record = state["vertices"][wid(1)]
        self.assertEqual((record["state"], record["session_uuid"]),
                         ("running", session(1)))
        self.assertEqual(state["events"][-1]["op"], "bind")
        self.assertEqual(graph.assert_holder(state, wid(1), 1, session(1)),
                         "ok")

    def test_second_session_for_vertex_refused(self):
        state = self.bind(self.state)
        self.assertUnchangedRefusal("vertex_held", state, graph.bind, wid(1),
                                    1, session(9), self.root, "standard",
                                    False)

    def test_session_bound_elsewhere_refused(self):
        self.assertRefused("session_already_bound", self.bind, self.state,
                           conflict=True)
        with patched_cap(2):
            state = fresh([make_vertex(1), make_vertex(2)], max_parallel=2)
            state = claimed(bound(state, 1), 2)
        self.assertUnchangedRefusal("session_already_bound", state,
                                    graph.bind, wid(2), 1, session(1),
                                    root_pair(state, 2), "standard", False)

    def test_stale_epoch_bind_refused(self):
        self.assertRefused("vertex_lease_superseded", self.bind, self.state,
                           epoch=0)
        reclaimed = graph.reclaim(self.state, wid(1), None, later(900))
        reclaimed = claimed(reclaimed, 1)
        self.assertRefused("vertex_lease_superseded", self.bind, reclaimed,
                           epoch=1)

    def test_root_and_profile_mismatch_refused(self):
        self.assertRefused("bind_root_mismatch", self.bind, self.state,
                           cwd=[9, 9])
        self.assertRefused("bind_profile_mismatch", self.bind, self.state,
                           profile="light")
        self.assertRefused("argument_error", self.bind, self.state,
                           cwd=[9])

    def test_rebind_same_session_idempotent(self):
        state = self.bind(self.state)
        again = self.bind(state)
        self.assertEqual(graph.canonical_json(again),
                         graph.canonical_json(state))
        self.assertRefused("bind_root_mismatch", self.bind, state,
                           cwd=[9, 9])

    def test_fence_after_reclaim_refused(self):
        state = self.bind(self.state)
        state = graph.reclaim(state, wid(1), holder("stale_dead_owner"), NOW)
        self.assertRefused("vertex_lease_superseded", graph.assert_holder,
                           state, wid(1), 1, session(1))

    def test_fence_reports_bind_incomplete(self):
        self.assertEqual(graph.assert_holder(self.state, wid(1), 1,
                                             session(1)), "bind_incomplete")
        self.assertRefused("vertex_lease_superseded", graph.assert_holder,
                           self.state, wid(1), 2, session(1))
        self.assertRefused("vertex_unknown", graph.assert_holder,
                           self.state, wid(99), 1, session(1))

    def test_fence_refuses_cancel_requested(self):
        state = self.bind(self.state)
        state, outcomes = graph.cancel(state, wid(1),
                                       {wid(1): holder("live_owner")}, NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancel_requested")
        self.assertRefused("vertex_cancel_requested", graph.assert_holder,
                           state, wid(1), 1, session(1))
        state, _ = graph.cancel(self.bind(self.state), None,
                                {wid(1): holder("live_owner")}, NOW)
        self.assertRefused("vertex_cancel_requested", graph.assert_holder,
                           state, wid(1), 1, session(1))


class HolderLifecycleTests(RefusalAssertions, unittest.TestCase):
    def running(self):
        return bound(fresh([make_vertex(1), make_vertex(2)]), 1)

    def test_cancel_pending_and_claimed(self):
        state = claimed(fresh([make_vertex(1), make_vertex(2)]), 1)
        state, outcomes = graph.cancel(state, wid(1), {}, NOW)
        self.assertEqual(outcomes, [{"work_id": wid(1),
                                     "outcome": "cancelled"}])
        record = state["vertices"][wid(1)]
        self.assertEqual((record["state"], record["lease_epoch"],
                          record["terminal_reason"]),
                         ("cancelled", 2, "cancelled"))
        self.assertEqual(state["slots"][0]["release_reason"], "cancelled")
        state, outcomes = graph.cancel(state, wid(2), {}, NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancelled")
        self.assertEqual(len(state["slots"]), 1)
        state, outcomes = graph.cancel(state, wid(2), {}, NOW)
        self.assertEqual(outcomes[0]["outcome"], "already_cancelled")

    def test_cancel_live_running_requests_only(self):
        facts = {wid(1): holder("live_owner")}
        state, outcomes = graph.cancel(self.running(), wid(1), facts, NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancel_requested")
        record = state["vertices"][wid(1)]
        self.assertEqual((record["state"], record["cancel_requested"]),
                         ("running", True))
        self.assertEqual(len(graph.held_slots(state)), 1)
        again, outcomes = graph.cancel(state, wid(1), facts, NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancel_requested")
        self.assertEqual(graph.canonical_json(again),
                         graph.canonical_json(state))

    def test_cancel_dead_running_confirms_and_releases(self):
        for verdict in ("stale_dead_owner", "unowned"):
            with self.subTest(verdict=verdict):
                state, outcomes = graph.cancel(
                    self.running(), wid(1), {wid(1): holder(verdict)}, NOW)
                self.assertEqual(outcomes[0]["outcome"], "cancelled")
                self.assertEqual(state["vertices"][wid(1)]["lease_epoch"], 2)
                self.assertEqual(graph.held_slots(state), [])
                self.assertEqual(state["events"][-1]["detail"],
                                 {"owner_verdict": verdict})

    def test_cancel_paused_confirms(self):
        state, outcomes = graph.cancel(
            self.running(), wid(1), {wid(1): holder("unowned", True)}, NOW)
        self.assertEqual(outcomes[0]["outcome"], "cancelled")
        self.assertEqual(graph.held_slots(state), [])

    def test_cancel_unproven_holder_requests_only(self):
        for facts in (holder("stale_unproven"), holder("corrupt"),
                      holder("no_session"), None, {"owner_verdict": "?"},
                      {"owner_verdict": "unowned"}):
            with self.subTest(facts=facts):
                state, outcomes = graph.cancel(self.running(), wid(1),
                                               {wid(1): facts}, NOW)
                self.assertEqual(outcomes[0]["outcome"], "cancel_requested")
                self.assertEqual(len(graph.held_slots(state)), 1)

    def test_graph_cancel_applies_per_vertex_in_order(self):
        with patched_cap(3):
            state = fresh([make_vertex(i) for i in (1, 2, 3, 4)],
                          max_parallel=3)
            state = publish_vertex(bound(state, 4), 4)
            state = claimed(bound(state, 1), 2)
        facts = {wid(1): holder("live_owner")}
        state, outcomes = graph.cancel(state, None, facts, NOW)
        self.assertEqual(outcomes, [
            {"work_id": wid(1), "outcome": "cancel_requested"},
            {"work_id": wid(2), "outcome": "cancelled"},
            {"work_id": wid(3), "outcome": "cancelled"}])
        self.assertTrue(state["cancelled"])
        self.assertEqual(state["vertices"][wid(4)]["state"], "succeeded")
        self.assertRefused("graph_cancelled", graph.claim, state, None, NOW)
        state, outcomes = graph.cancel(
            state, None, {wid(1): holder("stale_dead_owner")}, NOW)
        self.assertEqual([o["outcome"] for o in outcomes],
                         ["cancelled", "already_cancelled",
                          "already_cancelled"])
        self.assertEqual(graph.held_slots(state), [])

    def test_reclaim_refused_for_live_unproven_corrupt_paused(self):
        state = self.running()
        for code, facts in (
                ("vertex_live", holder("live_owner")),
                ("holder_unproven", holder("stale_unproven")),
                ("holder_unproven", holder("corrupt")),
                ("holder_unproven", holder("no_session")),
                ("holder_unproven", None),
                ("vertex_paused", holder("stale_dead_owner", True)),
                ("vertex_paused", holder("unowned", True))):
            with self.subTest(code=code, facts=facts):
                self.assertUnchangedRefusal(code, state, graph.reclaim,
                                            wid(1), facts, NOW)
        self.assertEqual(graph.REASON_RC["vertex_paused"], 3)

    def test_reclaim_allowed_for_dead_or_unowned_without_pause(self):
        for verdict in ("stale_dead_owner", "unowned"):
            with self.subTest(verdict=verdict):
                state = graph.reclaim(self.running(), wid(1),
                                      holder(verdict), NOW)
                record = state["vertices"][wid(1)]
                self.assertEqual(
                    (record["state"], record["lease_epoch"],
                     record["session_uuid"], record["claim_revision"]),
                    ("pending", 2, None, None))
                self.assertEqual(state["slots"][0]["release_reason"],
                                 "reclaim")
                self.assertEqual(state["events"][-1]["detail"],
                                 {"owner_verdict": verdict,
                                  "landed": "pending"})
                self.assertEqual(graph.ready_order(state), [wid(1), wid(2)])

    def test_unbound_claim_reclaim_waits_for_deadline(self):
        state = claimed(fresh([make_vertex(1)]), 1)
        self.assertUnchangedRefusal("claim_not_expired", state,
                                    graph.reclaim, wid(1), None, later(899))
        state = graph.reclaim(state, wid(1), None, later(900))
        self.assertEqual(state["vertices"][wid(1)]["state"], "pending")

    def test_reclaim_of_cancel_requested_vertex_lands_cancelled(self):
        state, _ = graph.cancel(self.running(), wid(1),
                                {wid(1): holder("live_owner")}, NOW)
        state = graph.reclaim(state, wid(1), holder("stale_dead_owner"), NOW)
        record = state["vertices"][wid(1)]
        self.assertEqual((record["state"], record["terminal_reason"]),
                         ("cancelled", "cancelled"))
        self.assertEqual(graph.held_slots(state), [])

    def test_fail_requires_non_live_holder(self):
        state = self.running()
        for code, facts in (("vertex_live", holder("live_owner")),
                            ("holder_unproven", holder("stale_unproven")),
                            ("vertex_paused", holder("unowned", True))):
            with self.subTest(code=code):
                self.assertUnchangedRefusal(code, state, graph.fail, wid(1),
                                            "abandoned", facts, NOW)
        self.assertRefused("vertex_not_claimed", graph.fail, state, wid(2),
                           "abandoned", None, NOW)
        failed = graph.fail(state, wid(1), "abandoned",
                            holder("stale_dead_owner"), NOW)
        record = failed["vertices"][wid(1)]
        self.assertEqual((record["state"], record["terminal_reason"],
                          record["lease_epoch"]),
                         ("failed", "abandoned", 2))
        self.assertEqual(failed["slots"][0]["release_reason"], "failed")

    def test_fail_reason_code_token_validated(self):
        state = claimed(fresh([make_vertex(1)]), 1)
        for token in ("Bad", "", "1x", "a" * 65, None, "has space"):
            with self.subTest(token=token):
                self.assertUnchangedRefusal("argument_error", state,
                                            graph.fail, wid(1), token, None,
                                            NOW)
        failed = graph.fail(state, wid(1), "a" * 64, None, NOW)
        self.assertEqual(failed["vertices"][wid(1)]["terminal_reason"],
                         "a" * 64)

    def test_terminal_transitions_preserve_other_receipts(self):
        with patched_cap(3):
            state = fresh([make_vertex(i) for i in (1, 2, 3)], max_parallel=3)
            state = publish_vertex(bound(state, 1), 1)
            receipt = graph.canonical_json(state["vertices"][wid(1)]["receipt"])
            state = bound(state, 2)
            state = graph.fail(state, wid(2), "abandoned",
                               holder("unowned"), NOW)
            state, _ = graph.cancel(claimed(state, 3), wid(3), {}, NOW)
        self.assertEqual(
            graph.canonical_json(state["vertices"][wid(1)]["receipt"]),
            receipt)
        self.assertEqual(state["vertices"][wid(1)]["state"], "succeeded")


class ReadyOrderingTests(RefusalAssertions, unittest.TestCase):
    def test_order_is_revision_then_work_id(self):
        first = [make_vertex(5), make_vertex(3)]
        state = fresh(first)
        state = admit(state, make_revision(
            first + [make_vertex(2), make_vertex(1)]))
        self.assertEqual(graph.ready_order(state),
                         [wid(3), wid(5), wid(1), wid(2)])
        state, info = graph.claim(state, None, NOW)
        self.assertEqual(info["work_id"], wid(3))

    def test_dependent_ready_only_after_all_predecessors_succeed(self):
        with patched_cap(2):
            state = fresh(chain((1, ()), (2, ()), (3, (1, 2))),
                          max_parallel=2)
            state = publish_vertex(bound(state, 1), 1)
            self.assertEqual(graph.derive_statuses(state)[wid(3)], "waiting")
            self.assertRefused("vertex_not_ready", graph.claim, state,
                               wid(3), NOW)
            state = publish_vertex(bound(state, 2), 2)
        self.assertEqual(graph.ready_order(state), [wid(3)])

    def test_failed_predecessor_blocks_transitively(self):
        state = claimed(fresh(chain((1, ()), (2, (1,)), (3, (2,)))), 1)
        state = graph.fail(state, wid(1), "abandoned", None, NOW)
        statuses = graph.derive_statuses(state)
        self.assertEqual((statuses[wid(2)], statuses[wid(3)]),
                         ("blocked", "blocked"))
        self.assertRefused("vertex_blocked", graph.claim, state, wid(3), NOW)
        self.assertRefused("none_ready", graph.claim, state, None, NOW)

    def test_order_independent_of_dict_insertion_order(self):
        vertices = [make_vertex(n) for n in (4, 2, 3, 1)]
        forward = fresh(vertices)
        backward = fresh(list(reversed(vertices)))
        self.assertEqual(graph.canonical_json(forward),
                         graph.canonical_json(backward))
        shuffled = copy.deepcopy(forward)
        shuffled["vertices"] = dict(reversed(list(
            shuffled["vertices"].items())))
        self.assertEqual(graph.ready_order(shuffled),
                         graph.ready_order(forward))
        self.assertEqual(graph.ready_order(forward),
                         [wid(1), wid(2), wid(3), wid(4)])


# --------------------------------------------------------------------------- #
# Receipts.                                                                   #
# --------------------------------------------------------------------------- #


_DEFAULT = object()


class ReceiptValidationTests(RefusalAssertions, unittest.TestCase):
    def setUp(self):
        self.state = bound(fresh([make_vertex(1), make_vertex(2)]), 1)
        self.txn = txn_for(self.state, 1)
        self.receipt = receipt_for(self.state, 1, self.txn)

    def refuse(self, code, receipt=_DEFAULT, txn=_DEFAULT,
               fingerprint=_DEFAULT, owner_ok=True, state=None,
               policy_fn=None):
        receipt = self.receipt if receipt is _DEFAULT else receipt
        txn = self.txn if txn is _DEFAULT else txn
        if fingerprint is _DEFAULT:
            fingerprint = receipt.get("manifest_digest") if isinstance(
                receipt, dict) else None
        return self.assertUnchangedRefusal(
            code, self.state if state is None else state, graph.publish,
            receipt, txn, fingerprint, owner_ok, NOW, policy_fn=policy_fn)

    def with_txn(self, **overrides):
        txn = txn_for(self.state, 1, **overrides)
        return txn, receipt_for(self.state, 1, txn)

    def test_valid_receipt_accepted_and_slot_released(self):
        self.assertEqual(graph.validate_receipt(
            self.state, self.receipt, self.txn, manifest(1), True), "valid")
        state = graph.publish(self.state, self.receipt, self.txn,
                              manifest(1), True, NOW)
        record = state["vertices"][wid(1)]
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual(record["receipt"], self.receipt)
        self.assertEqual(state["slots"][0]["release_reason"],
                         "terminal_receipt")
        self.assertEqual(self.receipt["required_checks"], {
            c: True for c in profiles.REQUIRED_CHECKS})
        self.assertEqual(graph.receipt_identity(self.receipt), (
            GRAPH_ID, 1, wid(1), 1, session(1), self.txn["transaction_id"],
            manifest(1)))

    def test_malformed_receipt_refused(self):
        missing = dict(self.receipt)
        del missing["owner_id"]
        for bad in (missing, dict(self.receipt, extra=1),
                    dict(self.receipt, record="Receipt"),
                    dict(self.receipt, manifest_digest="abc"),
                    dict(self.receipt, lease_epoch=True),
                    dict(self.receipt, published_at="yesterday"),
                    dict(self.receipt, required_checks={"x": "yes"}),
                    None):
            with self.subTest(receipt=bad):
                self.refuse("receipt_malformed", receipt=bad,
                            fingerprint=manifest(1))

    def test_cross_vertex(self):
        for bad in (dict(self.receipt, graph_id=OTHER_GRAPH_ID),
                    dict(self.receipt, work_id=wid(99)),
                    dict(self.receipt, session_uuid=session(9))):
            with self.subTest(receipt=bad):
                self.refuse("receipt_cross_vertex", receipt=bad)

    def test_stale_epoch(self):
        self.refuse("receipt_stale_epoch",
                    receipt=dict(self.receipt, lease_epoch=2))

    def test_stale_revision(self):
        state = admit(self.state, make_revision(
            [make_vertex(1), make_vertex(2)]))
        self.refuse("receipt_stale_revision", state=state,
                    receipt=dict(self.receipt, graph_revision=2))

    def test_candidate_changed(self):
        self.refuse("receipt_candidate_changed", fingerprint=manifest(9))
        self.refuse("receipt_candidate_changed", fingerprint=None)

    def test_wrong_candidate_digest_mismatch(self):
        txn = dict(self.txn, request_manifest_digest=manifest(9))
        self.refuse("receipt_wrong_candidate", txn=txn)
        receipt = dict(self.receipt, manifest_digest=manifest(9))
        self.refuse("receipt_wrong_candidate", receipt=receipt)

    def test_wrong_candidate_repo_identity(self):
        for pair in ([9, 9], None):
            with self.subTest(pair=pair):
                self.refuse("receipt_wrong_candidate",
                            txn=dict(self.txn, request_repo_dev_ino=pair))

    def test_wrong_candidate_other_session(self):
        self.refuse("receipt_wrong_candidate",
                    txn=dict(self.txn, request_session_uuid=session(9)))
        self.refuse("receipt_wrong_candidate",
                    txn=dict(self.txn, transaction_id="txn-other"))

    def test_wrong_candidate_not_green(self):
        txn, receipt = self.with_txn(verdict="red")
        self.refuse("receipt_wrong_candidate", receipt=receipt, txn=txn)

    def test_wrong_candidate_not_accepted(self):
        for disposition in (None, "rejected"):
            with self.subTest(disposition=disposition):
                txn, receipt = self.with_txn(disposition=disposition)
                self.refuse("receipt_wrong_candidate", receipt=receipt,
                            txn=txn)

    def published_pair(self):
        with patched_cap(2):
            state = fresh([make_vertex(1), make_vertex(2)], max_parallel=2)
            state = publish_vertex(bound(state, 1), 1)
            state = bound(state, 2)
        return state

    def test_collision_same_transaction(self):
        state = self.published_pair()
        stored = state["vertices"][wid(1)]["receipt"]["transaction_id"]
        txn = txn_for(state, 2, transaction_id=stored)
        receipt = receipt_for(state, 2, txn)
        refusal = self.refuse("receipt_candidate_collision", state=state,
                              receipt=receipt, txn=txn)
        self.assertEqual(refusal.detail, wid(1))

    def test_collision_same_session(self):
        state = self.published_pair()
        state["vertices"][wid(1)]["receipt"]["session_uuid"] = session(2)
        txn = txn_for(state, 2)
        receipt = receipt_for(state, 2, txn)
        self.refuse("receipt_candidate_collision", state=state,
                    receipt=receipt, txn=txn)

    def test_missing_final_suite_binding_evidence(self):
        txn, receipt = self.with_txn(final_suite_binding="none")
        self.assertFalse(receipt["required_checks"][
            "owned_verification_final_suite"])
        self.refuse("receipt_missing_required_check", receipt=receipt,
                    txn=txn)

    def test_reused_binding_requires_dependency_digest_reuse_mode(self):
        txn, receipt = self.with_txn(
            final_suite_binding="reused_dependency_bound")
        state = graph.publish(self.state, receipt, txn, manifest(1), True,
                              NOW)
        self.assertEqual(state["vertices"][wid(1)]["state"], "succeeded")
        assured = bound(fresh([make_vertex(1, profile="assurance")]), 1,
                        profile="assurance")
        txn = txn_for(assured, 1,
                      final_suite_binding="reused_dependency_bound")
        receipt = receipt_for(assured, 1, txn)
        self.assertFalse(receipt["required_checks"][
            "owned_verification_final_suite"])
        self.refuse("receipt_missing_required_check", state=assured,
                    receipt=receipt, txn=txn)

    def test_missing_reviewer_acceptance(self):
        evidence = graph.required_check_evidence(
            profiles.resolved_vertex_policy("standard", "builder"),
            dict(self.txn, disposition=None))
        self.assertFalse(evidence["paired_reviewer_approval"])
        self.assertTrue(evidence["owned_verification_final_suite"])
        txn, receipt = self.with_txn(disposition=None)
        self.refuse("receipt_wrong_candidate", receipt=receipt, txn=txn)
        forged = dict(self.receipt, required_checks=dict(
            self.receipt["required_checks"], paired_reviewer_approval=False))
        self.refuse("receipt_missing_required_check", receipt=forged)

    def test_unknown_required_check_fails_closed(self):
        def with_checks(checks):
            def policy_fn(profile, role):
                policy = profiles.resolved_vertex_policy(profile, role)
                policy["required_checks"] = checks
                return policy
            return policy_fn

        mystery = with_checks(list(profiles.REQUIRED_CHECKS) + ["mystery"])
        evidence = graph.required_check_evidence(mystery("standard",
                                                         "builder"), self.txn)
        self.assertFalse(evidence["mystery"])
        self.refuse("receipt_missing_required_check", policy_fn=mystery)
        self.refuse("receipt_missing_required_check",
                    policy_fn=with_checks([]))

    def test_non_owner(self):
        for owner_ok in (False, None, "yes"):
            with self.subTest(owner_ok=owner_ok):
                refusal = self.refuse("receipt_non_owner", owner_ok=owner_ok)
                self.assertEqual(refusal.rc, 3)

    def test_no_accepted_transaction(self):
        self.refuse("receipt_no_accepted_transaction", txn=None)
        self.refuse("receipt_no_accepted_transaction",
                    txn={"transaction_id": self.txn["transaction_id"]})

    def test_cancel_requested_vertex_refuses_publication(self):
        state, _ = graph.cancel(self.state, wid(1),
                                {wid(1): holder("live_owner")}, NOW)
        self.refuse("vertex_cancel_requested", state=state)

    def test_identical_republication_is_idempotent(self):
        state = graph.publish(self.state, self.receipt, self.txn,
                              manifest(1), True, NOW)
        again = graph.publish(state, dict(self.receipt, published_at=later(5)),
                              self.txn, None, False, later(5))
        self.assertEqual(graph.canonical_json(again),
                         graph.canonical_json(state))
        self.assertEqual(graph.validate_receipt(state, self.receipt, None,
                                                None, False),
                         "already_published")

    def test_exit_zero_without_receipt_never_succeeds(self):
        state = admit(self.state, make_revision(
            [make_vertex(1), make_vertex(2)], joins=[make_join(80, (1,))]))
        self.assertEqual(graph.derive_statuses(state)[wid(1)], "running")
        self.assertIsNone(state["vertices"][wid(1)]["receipt"])
        self.assertRefused("early_join", graph.join, state, wid(80), {}, NOW)
        txn, receipt = self.with_txn(verdict="red")
        self.assertRefused("receipt_wrong_candidate", graph.publish, state,
                           dict(receipt, graph_revision=1), txn,
                           manifest(1), True, NOW)
        self.assertEqual(graph.derive_statuses(state)[wid(1)], "running")


# --------------------------------------------------------------------------- #
# Join reducer.                                                               #
# --------------------------------------------------------------------------- #


class JoinReducerTests(RefusalAssertions, unittest.TestCase):
    def three_running(self, extra=()):
        with patched_cap(3):
            vertices = [make_vertex(1), make_vertex(2), make_vertex(3)]
            vertices += list(extra)
            state = fresh(vertices, max_parallel=3,
                          joins=[make_join(80, (1, 2, 3))])
            for n in (1, 2, 3):
                state = bound(state, n)
        return state

    def fingerprints(self, members=(1, 2, 3)):
        return {wid(n): manifest(n) for n in members}

    def test_all_six_arrival_orders_yield_one_decision_digest(self):
        decisions = set()
        digests = set()
        for order in itertools.permutations((1, 2, 3)):
            state = self.three_running()
            with patched_cap(3):
                for step, n in enumerate(order):
                    state = publish_vertex(state, n, now=later(10 * step))
            _state, decision = graph.join(state, wid(80), self.fingerprints(),
                                          later(100))
            self.assertEqual(decision["outcome"], "joined")
            decisions.add(graph.canonical_json(decision))
            digests.add(decision["decision_digest"])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(len(digests), 1)

    def test_ready_order_stable_across_arrival_orders(self):
        finals = set()
        for order in itertools.permutations((1, 2, 3)):
            state = self.three_running(extra=[make_vertex(4, (1, 2, 3))])
            with patched_cap(3):
                for n in order:
                    self.assertEqual(graph.ready_order(state), [])
                    state = publish_vertex(state, n)
            finals.add(tuple(graph.ready_order(state)))
        self.assertEqual(finals, {(wid(4),)})

    def test_early_join_refused_for_pending_claimed_running(self):
        state = fresh([make_vertex(1)], joins=[make_join(80, (1,))])
        self.assertUnchangedRefusal("early_join", state, graph.join, wid(80),
                                    {}, NOW)
        state = claimed(state, 1)
        self.assertUnchangedRefusal("early_join", state, graph.join, wid(80),
                                    {}, NOW)
        state = graph.bind(state, wid(1), 1, session(1), root_pair(state, 1),
                           "standard", False)
        self.assertUnchangedRefusal("early_join", state, graph.join, wid(80),
                                    {}, NOW)

    def test_early_join_refused_while_slot_held(self):
        with patched_cap(2):
            state = fresh([make_vertex(1), make_vertex(2)], max_parallel=2,
                          joins=[make_join(80, (1, 2))])
            state = publish_vertex(bound(state, 1), 1)
            state = bound(state, 2)
        self.assertEqual([s["work_id"] for s in graph.held_slots(state)],
                         [wid(2)])
        refusal = self.assertUnchangedRefusal(
            "early_join", state, graph.reduce_join, wid(80),
            self.fingerprints((1, 2)))
        self.assertEqual(refusal.work_id, wid(2))

    def test_failed_or_cancelled_member_blocks(self):
        for terminal in ("fail", "cancel"):
            with self.subTest(terminal=terminal), patched_cap(2):
                state = fresh([make_vertex(1), make_vertex(2)],
                              max_parallel=2, joins=[make_join(80, (1, 2))])
                state = publish_vertex(bound(state, 1), 1)
                state = claimed(state, 2)
                if terminal == "fail":
                    state = graph.fail(state, wid(2), "abandoned", None, NOW)
                else:
                    state, _ = graph.cancel(state, wid(2), {}, NOW)
                _state, decision = graph.join(state, wid(80), {}, NOW)
                self.assertEqual(decision["outcome"], "blocked")
                self.assertEqual(decision["members"][1]["state"],
                                 "failed" if terminal == "fail"
                                 else "cancelled")

    def test_member_blocked_by_failed_predecessor_blocks_join(self):
        state = fresh(chain((1, ()), (2, (1,))), joins=[make_join(80, (2,))])
        state = graph.fail(claimed(state, 1), wid(1), "abandoned", None, NOW)
        _state, decision = graph.join(state, wid(80), {}, NOW)
        self.assertEqual(decision["outcome"], "blocked")
        self.assertEqual(decision["members"], [{
            "work_id": wid(2), "state": "blocked", "lease_epoch": 0,
            "session_uuid": None, "transaction_id": None,
            "manifest_digest": None}])

    def test_join_idempotent_and_single_record(self):
        state = publish_vertex(bound(fresh(
            [make_vertex(1)], joins=[make_join(80, (1,))]), 1), 1)
        state, decision = graph.join(state, wid(80), self.fingerprints((1,)),
                                     NOW)
        again, repeat = graph.join(state, wid(80), {}, later(60))
        self.assertEqual(repeat, decision)
        self.assertEqual(graph.canonical_json(again),
                         graph.canonical_json(state))
        self.assertEqual(list(state["joins"][wid(80)]), ["1"])
        self.assertEqual([e["op"] for e in state["events"]].count("join"), 1)

    def test_join_revalidates_live_fingerprint(self):
        state = publish_vertex(bound(fresh(
            [make_vertex(1)], joins=[make_join(80, (1,))]), 1), 1)
        for fingerprints in ({wid(1): manifest(9)}, {}, None):
            with self.subTest(fingerprints=fingerprints):
                self.assertUnchangedRefusal("receipt_candidate_changed",
                                            state, graph.join, wid(80),
                                            fingerprints, NOW)

    def test_unknown_join_refused(self):
        state = fresh([make_vertex(1)], joins=[make_join(80, (1,))])
        for join_id in (wid(77), "not-a-uuid", None):
            with self.subTest(join_id=join_id):
                self.assertUnchangedRefusal("join_unknown", state, graph.join,
                                            join_id, {}, NOW)

    def test_decision_has_no_merge_and_receipts_unchanged(self):
        state = self.three_running()
        with patched_cap(3):
            for n in (1, 2, 3):
                state = publish_vertex(state, n)
        receipts = {w: graph.canonical_json(r["receipt"])
                    for w, r in state["vertices"].items()}
        revisions = graph.canonical_json(state["revisions"])
        joined, decision = graph.join(state, wid(80), self.fingerprints(),
                                      NOW)
        for key in keys_at_any_depth(decision):
            self.assertNotIn("merge", key)
            self.assertNotIn("source", key)
        self.assertEqual(set(decision), {
            "schema_version", "record", "graph_id", "graph_revision",
            "join_id", "rule", "outcome", "members", "decision_digest"})
        self.assertEqual({w: graph.canonical_json(r["receipt"])
                          for w, r in joined["vertices"].items()}, receipts)
        self.assertEqual(graph.canonical_json(joined["revisions"]),
                         revisions)


# --------------------------------------------------------------------------- #
# Closed codes and purity.                                                    #
# --------------------------------------------------------------------------- #


ACCEPTED_REASON_CODES = {
    1: ["graph_state_corrupt", "graph_state_inconsistent", "lock_timeout",
        "io_error"],
    2: ["argument_error", "revision_malformed", "cycle", "self_edge",
        "dangling_predecessor", "duplicate_work_id", "root_missing",
        "root_symlink", "root_not_worktree_toplevel", "base_commit_mismatch",
        "anchor_dir_not_ignored", "candidate_collision", "root_alias",
        "root_nested", "root_overlaps_main_checkout",
        "root_overlaps_sessions_root", "root_in_use", "authority_missing",
        "authority_malformed", "authority_digest_mismatch",
        "authority_shared", "unknown_profile", "ceiling_invalid",
        "ceiling_above_policy", "unsupported_concurrency_contract",
        "join_malformed", "join_unknown_member", "revision_conflict",
        "graph_cancelled", "graph_unknown", "vertex_unknown",
        "vertex_not_ready", "vertex_blocked", "none_ready", "cap_reached",
        "vertex_terminal", "vertex_not_claimed", "vertex_not_running",
        "bind_root_mismatch", "bind_profile_mismatch",
        "graph_vertex_malformed", "graph_vertex_requires_profile",
        "graph_vertex_requires_new_session", "graph_vertex_flag_conflict",
        "receipt_malformed", "receipt_cross_vertex", "receipt_stale_epoch",
        "receipt_stale_revision", "receipt_no_accepted_transaction",
        "receipt_wrong_candidate", "receipt_candidate_collision",
        "receipt_missing_required_check", "receipt_candidate_changed",
        "join_unknown", "early_join"],
    3: ["vertex_held", "session_already_bound", "vertex_lease_superseded",
        "vertex_cancel_requested", "vertex_live", "vertex_paused",
        "holder_unproven", "claim_not_expired", "receipt_non_owner",
        "owner_conflict"],
}


class ReasonCodeTableTests(RefusalAssertions, unittest.TestCase):
    def test_reason_table_equals_accepted_contract(self):
        expected = {code: rc for rc, codes in ACCEPTED_REASON_CODES.items()
                    for code in codes}
        self.assertEqual(graph.REASON_RC, expected)
        self.assertEqual(len(graph.REASON_RC), 69)
        self.assertEqual([len(ACCEPTED_REASON_CODES[rc]) for rc in (1, 2, 3)],
                         [4, 55, 10])
        self.assertEqual(graph.REASON_CODES, tuple(sorted(expected)))

    def test_unknown_code_rejected(self):
        with self.assertRaises(ValueError):
            graph.GraphRefusal("not_a_code")

    def test_refusal_carries_rc_and_code(self):
        refusal = graph.GraphRefusal("cap_reached", wid(1), "full")
        self.assertEqual((refusal.code, refusal.rc, refusal.work_id,
                          refusal.detail), ("cap_reached", 2, wid(1), "full"))
        self.assertEqual(str(refusal), "cap_reached: %s: full" % wid(1))
        self.assertEqual(graph.GraphRefusal("vertex_live").rc, 3)
        self.assertEqual(graph.GraphRefusal("io_error").rc, 1)

    def test_every_refusal_leaves_input_unchanged(self):
        joined = fresh([make_vertex(1), make_vertex(2)],
                       joins=[make_join(80, (1,))])
        claimed_state = claimed(joined, 1)
        running = bound(joined, 1)
        txn = txn_for(running, 1)
        receipt = receipt_for(running, 1, txn)
        published = graph.publish(running, receipt, txn, manifest(1), True,
                                  NOW)
        matrix = [
            ("cycle", joined, admit,
             (make_revision(chain((1, (2,)), (2, (1,)))),)),
            ("cap_reached", claimed_state, graph.claim, (None, NOW)),
            ("bind_root_mismatch", claimed_state, graph.bind,
             (wid(1), 1, session(1), [9, 9], "standard", False)),
            ("receipt_non_owner", running, graph.publish,
             (receipt, txn, manifest(1), False, NOW)),
            ("early_join", running, graph.join, (wid(80), {}, NOW)),
            ("vertex_live", running, graph.fail,
             (wid(1), "abandoned", holder("live_owner"), NOW)),
            ("vertex_paused", running, graph.reclaim,
             (wid(1), holder("unowned", True), NOW)),
            ("vertex_terminal", published, graph.cancel, (wid(1), {}, NOW)),
        ]
        for code, state, fn, args in matrix:
            with self.subTest(code=code):
                self.assertUnchangedRefusal(code, state, fn, *args)

    def test_successful_transitions_do_not_mutate_input(self):
        base = graph.new_state(GRAPH_ID)
        doc = make_revision([make_vertex(1), make_vertex(2)],
                            joins=[make_join(80, (1,))])
        admitted = admit(base, doc)
        claimed_state = claimed(admitted, 1)
        running = bound(admitted, 1)
        txn = txn_for(running, 1)
        receipt = receipt_for(running, 1, txn)
        published = graph.publish(running, receipt, txn, manifest(1), True,
                                  NOW)
        steps = [
            (base, lambda s: admit(s, doc)),
            (admitted, lambda s: graph.claim(s, None, NOW)),
            (claimed_state, lambda s: graph.bind(
                s, wid(1), 1, session(1), root_pair(s, 1), "standard",
                False)),
            (claimed_state, lambda s: graph.cancel(s, wid(1), {}, NOW)),
            (claimed_state, lambda s: graph.fail(s, wid(1), "abandoned",
                                                 None, NOW)),
            (claimed_state, lambda s: graph.reclaim(s, wid(1), None,
                                                    later(900))),
            (running, lambda s: graph.publish(s, receipt, txn, manifest(1),
                                              True, NOW)),
            (published, lambda s: graph.join(s, wid(80), {
                wid(1): manifest(1)}, NOW)),
            (published, graph.status_view),
        ]
        for index, (state, step) in enumerate(steps):
            with self.subTest(step=index):
                before = graph.canonical_json(state)
                result = step(state)
                self.assertEqual(graph.canonical_json(state), before)
                self.assertIsNot(result, state)


_ALLOWED_IMPORTS = frozenset({"copy", "datetime", "hashlib", "json", "re",
                              "cowork_workunit", "cowork_execution_profiles"})
_FORBIDDEN_MODULES = frozenset({"os", "sys", "io", "time", "subprocess",
                                "socket", "pathlib", "shutil", "tempfile",
                                "threading", "signal"})
_FORBIDDEN_NAME_CALLS = frozenset({"open", "exec", "eval", "__import__"})
_FORBIDDEN_ATTR_CALLS = frozenset({"now", "utcnow", "today", "system",
                                   "popen", "run", "Popen", "socket"})


class PurityBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(graph.__file__, encoding="utf-8") as handle:
            cls.tree = ast.parse(handle.read())

    def imported_names(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        return names

    def test_imports_are_stdlib_allowlist_plus_two_siblings(self):
        names = self.imported_names()
        self.assertLessEqual(names, _ALLOWED_IMPORTS)
        self.assertIn("cowork_workunit", names)
        self.assertIn("cowork_execution_profiles", names)

    def test_no_io_process_or_clock_calls(self):
        self.assertFalse(self.imported_names() & _FORBIDDEN_MODULES)
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                self.assertNotIn(func.id, _FORBIDDEN_NAME_CALLS)
            elif isinstance(func, ast.Attribute):
                self.assertNotIn(func.attr, _FORBIDDEN_ATTR_CALLS)


if __name__ == "__main__":
    unittest.main()
