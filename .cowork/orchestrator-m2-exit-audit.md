# M2 Package F — Global Exit Audit (v2)

- **Signed base**: `999085483c624ac899fde24d709eee1a4cc753c0` (`fix(dispatch): enforce live graph preflight`), confirmed as this worktree's exact `HEAD`. This supersedes F v1's base `b77d86b7cd7abe2bb18d1649b290e71f7b2ddf9e` (`feat: integrate M2 phase truth`) — Package E was reopened between F v1 and F v2 specifically to close finding **F-LIVE-GRAPH-WIRING-1** (see §0 and §6-a).
- **Frozen plan**: `/Users/marcos/.claude/plans/m2-identity-phase-truth-plan-v2.json` (sha256 `d67b206146111fc044a47c0e143210d6367b0edad3a59cdb7759390aabfc8529`), package `F-global-audit`.
- **Canonical backend-gate reference**: `/Users/marcos/.codex/skills/cowork-refactor-orchestrator/references/cowork-backend-gate.md`.
- **Scope of this package**: two end-to-end test files plus this artifact. **Zero production files touched.** Every finding below against A-E is candidate-bound and left for that package to reopen — nothing here is patched inline.
- **Claim scope**: this artifact assesses **backend-gate criteria 2 and 3 only**. Criteria 1, 4, 5, and 6 are **not assessed** by this package. **The full six-criterion backend gate is NOT claimed, in any part.** Both criterion 2 and criterion 3 are marked **`not_claimed`** below — this artifact is evidence-gathering for a future gating decision, not itself a gate-pass declaration.

---

## 0. Provenance: mechanical adoption of the F v1 candidate, with the graph negative control replaced

Per the frozen brief for this package (F v2), the exact three-file F v1 candidate (`scripts/test_m2_negative_controls.py`, `scripts/test_m2_crash_resume.py`, `.cowork/orchestrator-m2-exit-audit.md`) is mechanically adopted byte-for-byte, with exactly two deliberate deviations, both required by F v1's own fixed failure:

1. **`scripts/test_m2_negative_controls.py`**: `DependencyGraphNegativeControlsTest`'s seventh test — F v1's `test_dispatch_time_declaration_check_also_fails_closed`, which called the dormant `cowork_preflight.check_dependency_graph_declaration` helper directly and was the exact basis of F-LIVE-GRAPH-WIRING-1 — is replaced by `test_invalid_declaration_rejects_real_dispatch_path`, which instead drives the REAL, now fully wired `cowork.run_scout` entry point end to end (see §6-a). The unused `cowork_preflight` import this left behind is also removed. The six pure durable-store graph tests (cycles/dangling/duplicate/self-edge/cross-candidate/cross-policy) and every one of the other 15 tests in the file are byte-identical to F v1 — confirmed by direct diff against the F v1 worktree before this edit.
2. **`.cowork/orchestrator-m2-exit-audit.md`** (this file): updated truthfully throughout to reflect the new signed base, the resolved F-LIVE-GRAPH-WIRING-1 finding, the recomputed completion-emission enumeration (source lines shifted by the wiring fix), and the new real-dispatch-path graph proof — never a cosmetic-only or superficial edit.

`scripts/test_m2_crash_resume.py` required **zero changes** — confirmed byte-identical to the F v1 candidate by direct diff — because none of its nine tests touch the dispatch-time graph-declaration seam; they exercise Package B's durable-write boundaries (PhaseState, WorkUnit mint/transition, graph-revision append, rejected policy/config-transition byte identity), none of which the live-graph-wiring correction changed.

---

## 1. A-E signed integrity (mechanical check)

`HEAD` at audit time is byte-identical to the signed base named in the brief and in Package E's updated receipt:

```
$ git rev-parse HEAD
999085483c624ac899fde24d709eee1a4cc753c0
$ git status --short
?? scripts/test_m2_crash_resume.py
?? scripts/test_m2_negative_controls.py
```

File hashes recomputed from this worktree — **all five match exactly**, against two different signed reference points depending on whether the live-graph-wiring correction touched the file:

| File | sha256 (this worktree) | Matches | Reference |
|---|---|---|---|
| `scripts/cowork.py` | `783a7eb9e8bbbb8beaab7fd7d290de04573444c31f494fbde36cf750f5b256fd` | PASS | E's live-graph-wiring corrected review (`/Users/marcos/.claude/plans/m2-e-live-graph-wiring-v1-corrected-review.json`, candidate `ae7e5840-8439-4b7a-b961-54a1bf685826`) |
| `scripts/cowork_bridge.py` | `843d00a1bef792f2a95b9b99a3cacaa86220102ee59c26a232919407c049ea46` | PASS | unchanged since E's phase-truth-integration-v3 corrected review — this file was **not** touched by the live-graph-wiring correction (diff stat: `cowork.py` and `test_cowork.py` only) |
| `scripts/cowork_state.py` | `61d50cdfc22488388fd6c7e72495144339d43cfe935c9374060a97e285059df0` | PASS | unchanged since E's phase-truth-integration-v3 corrected review, same reason |
| `scripts/cowork_verification.py` | `2a3031e3a6d47ba4b218ca8677e35962efccc4f4d1a0d83aa79ba8ccfdb14004` | PASS | unchanged since E's phase-truth-integration-v3 corrected review, same reason |
| `scripts/test_cowork.py` | `2c8b77ae6de5ae2a4405003563140e3047744ae092a6aaaad5c26b0c581c52a2` | PASS | E's live-graph-wiring corrected review (same artifact as `cowork.py` above) |

Package E's updated receipt (`/Users/marcos/.cowork/orchestrator/2267e8b02f70ad8c/m2-integration-receipts/package-e.json`) confirms: `status: integrated_signed_published`, `commit: 999085483c624ac899fde24d709eee1a4cc753c0`, `supersedes_commit: b77d86b7cd7abe2bb18d1649b290e71f7b2ddf9e`, `signature_verified: true`, `review_verdict: approve`, `fixed_gate: PASS`. Package A-D receipts (`package-{a,b,c,d}.json`) are unaffected by this correction and still show `signature_verified: true` on the same sequentially-integrated, `main`-unchanged chain leading into this base.

**Package F's own exact changed-path allowlist** (matches the frozen brief's authority exactly, zero others):

- `scripts/test_m2_negative_controls.py` (new, untracked)
- `scripts/test_m2_crash_resume.py` (new, untracked, byte-identical to F v1 — see §0)
- `.cowork/orchestrator-m2-exit-audit.md` (new, gitignored)

---

## 2. Completion-emission audit (mechanical enumeration)

**Method**: every call site of `cowork._advance_phase(...)` in `scripts/cowork.py` was mechanically located and its literal event-string argument extracted (script-driven, not hand-curated). `cowork._advance_phase` is, by its own docstring, *"the one seam every production phase advance in this file passes through"* — `cowork_control_plane.advance`'s transition table names exactly one edge into `completed`: `("awaiting_gate", "gate_validated")`, and that edge additionally requires well-shaped, candidate-bound, passing gate-validation evidence (`_gate_evidence_valid` / `_gate_evidence_matches_candidate`). So enumerating every `_advance_phase` call site and its event exhaustively enumerates every code path that can possibly reach `completed`.

**Result: 42 live call sites, exactly ONE of which emits `gate_validated`.** (Re-run against this base: 37 at F v1's base, +5 net new — see the reconciliation note below the table.)

| Event emitted | Call-site count | Line numbers |
|---|---|---|
| `preflight_rejected` | 13 | 8974, 9247, 9345, 9367, 9406, 9417, 9476, 9487, 9538, 9638, 9724, 10030, 10122 |
| `preflight_passed` | 9 | 9427, 9496, 9545, 9837, 9892, 9934, 10256, 10311, 10353 |
| `execution_failed` | 7 | 7299, 7332, 7364, 7438, 7502, 8003, 8180 |
| `preflight_started` | 4 | 8972, 9228, 9615, 9998 |
| `capability_missing` | 3 | 9263, 9656, 10048 |
| `cancelled` | 2 | 8050, 8199 |
| `aborted` | 2 | 8220, 12641 |
| `turn_completed` | 1 | 9049 (inside `_complete_phase`) |
| **`gate_validated`** | **1** | **9053 (inside `_complete_phase`)** |
| `dependency_blocked`, `dependency_unblocked`, `capacity_reserved`, `gate_rejected` | 0 | never emitted anywhere in `cowork.py` |

**Reconciliation against F v1's 37**: the live-graph-wiring correction added exactly 5 new `_advance_phase` call sites, all non-completing, all `GraphDeclarationRejected`-driven: 3 inline `except GraphDeclarationRejected` handlers in `run_scout`/`run_planner`/`run_builder` (each one `preflight_rejected` call, at 9247, 9638, 10030) plus the new `_reject_graph_declaration` helper (`cowork.py:8944`), whose own body contributes one `preflight_started` (8972) and one `preflight_rejected` (8974) call site, shared by the `switch_controller` and `ensure_controller_dispatchable` seams that both call it. `9 preflight_rejected → 13` (+4) and `3 preflight_started → 4` (+1) account for the full +5; every other event count, and the sole `gate_validated` site, is unchanged in substance (only its line number shifted, 8954 → 9053, from the file growing above it).

**The sole `gate_validated` site** is inside `_complete_phase` (`cowork.py:9035-9054`), which is itself called from **exactly one** production site: `cowork.py:8092`, reached only when the interactive `ready_for_review` gate returns the `_END` outcome (an explicit human approval, or the headless auto-approve equivalent — never an exit code, EOF, or status-file read alone) **and** a real, previously-proven dispatch-manifest digest (`_approved_digest`) is present to bind as gate evidence. `_complete_phase` itself binds the candidate first (`_bind_candidate`), then drives `turn_completed` (running → awaiting_gate) then `gate_validated` with that exact digest as evidence — matching `cowork_control_plane.advance`'s fail-closed candidate-identity rule. This gating logic is untouched by the live-graph-wiring correction (confirmed by direct read of `cowork.py:8060-8100` and E's own corrected-review artifact, whose `verified_invariants` states the diff is "strictly additive" and names every seam it touched — this one is not among them).

**Every one of the other 41 sites targets a non-completing state** (`preflight_started/passed/rejected`, `capability_missing` → `needs_authority`, `execution_failed` → `failed`, `cancelled`, `aborted`). None of these 41 sites, and no other code path in `cowork.py`, ever constructs `gate_validated` evidence.

**Finding — `blocked`/`dependency_blocked` live-dormant (non-blocking, informational)**: the reducer legally supports `running --dependency_blocked--> blocked --dependency_unblocked--> running` (`cowork_control_plane.TRANSITIONS`), and Package A's own test suite (`test_cowork_control_plane.py`) exercises it structurally, but **zero live call sites in `cowork.py` ever emit `dependency_blocked`** — this is separate from `awaiting_capacity`'s by-construction reducer-level unreachability (see §5) and is not itself part of any invariant this audit was asked to assess; recorded here only because the mechanical enumeration surfaced it.

**PASS** — completion-emission audit: exactly one gate-validated, candidate-bound path to `completed`; every other of the 42 live call sites targets an explicit non-completing state.

---

## 3. Eight required-coverage items

| # | Invariant | Status | Named test(s) | Gate reference |
|---|---|---|---|---|
| 1 | WorkUnit is the join key across preflight, controller lifecycle, children, status, findings, verification, gates, and recovery | **PASS** | `test_cowork_workunit.py` (schema); `test_m2_crash_resume.py::WorkUnitMintCrashResumeTest`, `::WorkUnitTransitionCrashResumeTest` (live mint/transition through `cowork._ensure_work_unit`/`_bind_candidate`); `test_cowork.py::PhaseTruthCompletionBindingTest::test_scout_approval_reaches_completed_with_candidate_bound_evidence` (WorkUnit mirrors PhaseState at completion) | `python3 -m unittest scripts/test_cowork_workunit.py scripts/test_m2_crash_resume.py -v` |
| 2 | The versioned dependency graph rejects duplicate work IDs, dangling predecessors, self-edges, cycles, cross-candidate fan-in, and cross-policy fan-in | **PASS** at both the schema/store level AND, for the invalid/stale/malformed shape, the live dispatch-time seam — **see §6-a**: F-LIVE-GRAPH-WIRING-1 is now resolved, with an accepted residual (R-1: nothing in `cowork.py` yet populates a WorkUnit's `graph_revision`/`predecessor_work_ids` itself, so the enforcement fires only for an externally pre-minted declaration) | `test_cowork_workunit.py` (pure validators); `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest` (all 6 pure shapes through B's real durable `append_graph_revision` store, PLUS `test_invalid_declaration_rejects_real_dispatch_path` through the real `cowork.run_scout` dispatch path); `test_cowork.py::LiveGraphWiringTest`, `::GraphDeclarationPreLaunchAndSwitchSeamTest`, `::GraphDeclarationStructuralGateTest` (E's own live-wiring proof) | `python3 -m unittest scripts/test_m2_negative_controls.py -v` |
| 3 | Ungoverned children are blocked or correlated to parent WorkUnit and effective controller/model/effort/policy | **PASS** | `test_cowork_dispatch_identity.py` (isolated); `test_cowork.py::RealBrokerChildDispatchCorrelationUnavailableTest`, `::GuardBrokerSessionProvenanceWiringTest` (live); `test_m2_negative_controls.py::UncorrelatedChildrenTest` (independent live re-proof) | `python3 -m unittest scripts/test_m2_negative_controls.py -v` |
| 4 | Controller policy/config transitions are atomic before preflight | **PASS** | `test_cowork_policy_atomic.py` (isolated CAS primitive); `test_cowork.py::PhaseTruthAtomicPolicyFaultInjectionTest`, `::ControllerPolicyStateTest::test_committed_atomic_transition_matches_both_stores` (live); `test_m2_negative_controls.py::InvalidPolicyTransitionTest`, `::ControllerSwitchInterruptionTest`; `test_m2_crash_resume.py::PolicyConfigTransitionCrashResumeTest` (crash-injected at the CAS write boundary through the real `--switch-controller` seam) | `python3 -m unittest scripts/test_m2_negative_controls.py scripts/test_m2_crash_resume.py -v` |
| 5 | Phase taxonomy is closed; failures cannot persist ambiguous completion | **PASS** | `test_cowork_control_plane.py` (exhaustiveness table over every (state, event) pair); `test_cowork.py::StructuralGateTest::test_zero_ambiguous_ended_literal` (structural grep, guard-lifecycle `ended` named-exempt per F7); §2 above (mechanical completion-emission enumeration) | §2 of this artifact; `python3 -m unittest scripts/test_cowork_control_plane.py -v` |
| 6 | Completion advances only through the pure reducer after bound gate evidence | **PASS** | `cowork_control_plane.advance` (A, pure); §2 of this artifact (42/42 live call sites accounted for, 1 gate-validated, candidate-bound); `test_cowork.py::PhaseTruthCompletionBindingTest`; `test_m2_negative_controls.py::NonCompletionMatrixTest` (3 non-completion causes, live) | §2 of this artifact |
| 7 | Recovery breaker is durable and keyed by causal fingerprint | **PASS** | `test_cowork_recovery_breaker.py` (isolated, threshold/changed-cause/crash-restart fixtures); `test_cowork.py::PhaseTruthRecoveryBreakerTest` (live); `test_m2_negative_controls.py::RepeatedIdenticalRepairTest` (independent live re-proof, 4th identical-cause retry refused before dispatch) | `python3 -m unittest scripts/test_m2_negative_controls.py -v` |
| 8 | Legacy session anchors and completed M1 manifest/dispatch behavior remain compatible | **PASS** | `test_cowork_state_m2.py` (legacy-fixture round-trip against an 8c13adb-captured session file); full `test_cowork.py` regression (see §7) — every pre-M2 test, unmodified, green at this signed base | §7 of this artifact |

---

## 4. Fifteen required negative controls

All fifteen are proven end-to-end against the real, fully-wired production seam: `cowork.run_flow`/`cowork.run_scout`/`cowork._role_loop`, the real `cowork_bridge._guard_runtime` GuardBroker AF_UNIX socket, or — for the six dependency-graph shapes — B's real durable `append_graph_revision` store (the append boundary these six pure shapes target). The dispatch-time graph-declaration seam itself (the shape the module-level comment above `DependencyGraphNegativeControlsTest` used to flag as dormant, F-LIVE-GRAPH-WIRING-1) is now separately proven end-to-end through the real `cowork.run_scout` entry point — see the bonus row below and §6-a.

| # | Negative control | Status | Named test |
|---|---|---|---|
| 1 | Uncorrelated children | **PASS** | `test_m2_negative_controls.py::UncorrelatedChildrenTest::test_uncorrelated_child_blocked_with_durable_terminal_record` |
| 2 | Guard disappearance | **PASS** | `test_m2_negative_controls.py::NonCompletionMatrixTest::test_guard_disappearance_reaches_failed_never_completed` |
| 3 | Invalid policy transition | **PASS** | `test_m2_negative_controls.py::InvalidPolicyTransitionTest::test_rejected_transition_zero_dispatch_byte_identical` |
| 4 | Controller abort | **PASS** | `test_m2_negative_controls.py::NonCompletionMatrixTest::test_controller_abort_reaches_aborted_never_completed` |
| 5 | EOF | **PASS** | `test_m2_negative_controls.py::NonCompletionMatrixTest::test_eof_reaches_cancelled_never_completed` |
| 6 | External kill (positive durable-terminal-record assertion) | **PASS** | `test_m2_negative_controls.py::ExternalKillPositiveTerminalRecordTest::test_sigterm_at_gate_positive_durable_aborted_record` |
| 7 | Repeated identical repair | **PASS** | `test_m2_negative_controls.py::RepeatedIdenticalRepairTest::test_fourth_identical_cause_retry_blocked_before_dispatch` |
| 8 | Graph cycles | **PASS** | `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_graph_cycles_rejected_by_durable_store` |
| 9 | Dangling predecessors | **PASS** | `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_dangling_predecessors_rejected_by_durable_store` |
| 10 | Duplicate work IDs | **PASS** | `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_duplicate_work_ids_rejected_by_durable_store` |
| 11 | Self-edges | **PASS** | `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_self_edges_rejected_by_durable_store` |
| 12 | Cross-candidate fan-in | **PASS** | `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_cross_candidate_fan_in_rejected_by_durable_store` |
| 13 | Cross-policy fan-in | **PASS** | `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_cross_policy_fan_in_rejected_by_durable_store` |
| 14 | Context-ack failure before first accepted send (issue #11) | **PASS** | `test_m2_negative_controls.py::ContextAckFailureTest::test_first_send_failure_withholds_ack_resume_redelivers_both` |
| 15 | Controller-switch interruption (issue #30) | **PASS** | `test_m2_negative_controls.py::ControllerSwitchInterruptionTest::test_replace_failure_leaves_prior_identity_intact_zero_dispatch` |

Bonus (beyond the required 15, F-LIVE-GRAPH-WIRING-1 real dispatch-path proof — see §0 and §6-a): `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_invalid_declaration_rejects_real_dispatch_path`. Unlike F v1's `test_dispatch_time_declaration_check_also_fails_closed` (which this test replaces — F v1's fixed failure named it directly), this test never calls `cowork_preflight.check_dependency_graph_declaration` itself: it mints a scout WorkUnit whose own persisted graph declaration is a self-edge and drives it through the real `cowork.run_scout`, asserting all three of (a) `rc == 1` before the scout loop starts, (b) a WorkUnit-bound `rejected_preflight`/`preflight_rejected` record (both the PhaseState and the WorkUnit's own `lifecycle_state`/`terminal_reason`), and (c) zero manifest/session/send dispatch (`load_manifest` is `None`; the `session_factory` stub raises if ever called).

`gate reference` for every row above: `python3 -m unittest scripts/test_m2_negative_controls.py -v` (see §7 for actual run output).

---

## 5. "Paused" unreachability (M2, by design)

Per the frozen plan's own Package A invariant: *"`awaiting_capacity` exists in the enum and transition table for future M3 activation but has no legal inbound transition in M2 ... backend-gate criterion 3's 'paused' class is mapped to this member as provably unreachable rather than exercised."* This is confirmed structurally: `cowork_control_plane.TRANSITIONS` names zero `(state, event) -> awaiting_capacity` entries, proven by the exhaustiveness table test plus the dedicated `test_cowork_control_plane.py::test_awaiting_capacity_unreachable_m2`. **`paused` is intentionally unreachable in M2** — this is a documented design boundary, not a gap this audit is raising.

---

## 6. Crash/resume: every durable state-write boundary from Package B, through the live seam

| Boundary | Fault injected | Status | Named test(s) |
|---|---|---|---|
| PhaseState append (`cowork._advance_phase`) | Short/interrupted write | **PASS** | `test_m2_crash_resume.py::PhaseStateCrashResumeTest::test_short_write_crash_mid_advance_leaves_no_torn_tail_then_resumes` |
| PhaseState append | `fsync` failure | **PASS** | `test_m2_crash_resume.py::PhaseStateCrashResumeTest::test_fsync_failure_crash_mid_advance_rolls_back_then_resumes` |
| PhaseState append — repair/reopen/next append | Pre-existing torn tail, then a real next live append | **PASS** | `test_m2_crash_resume.py::PhaseStateCrashResumeTest::test_torn_tail_repaired_before_next_live_advance` |
| WorkUnit mint (`cowork._ensure_work_unit`) | Short/interrupted write | **PASS** | `test_m2_crash_resume.py::WorkUnitMintCrashResumeTest::test_short_write_crash_mid_mint_mints_nothing_then_resumes` |
| WorkUnit mint — resume idempotency | Clean re-derivation of the same deterministic work_id | **PASS** | `test_m2_crash_resume.py::WorkUnitMintCrashResumeTest::test_second_ensure_after_clean_mint_is_idempotent_not_a_remint` |
| WorkUnit transition (`cowork._bind_candidate`) | Short/interrupted write | **PASS** | `test_m2_crash_resume.py::WorkUnitTransitionCrashResumeTest::test_short_write_crash_mid_bind_leaves_prior_binding_then_resumes` |
| WorkUnit transition — repair/reopen/next append | Pre-existing torn tail, then a real next live transition | **PASS** | `test_m2_crash_resume.py::WorkUnitTransitionCrashResumeTest::test_torn_tail_in_work_unit_store_repaired_before_next_live_transition` |
| Dependency-graph revision (`cowork_state.append_graph_revision`) | Short/interrupted write | **PASS** | `test_m2_crash_resume.py::GraphRevisionCrashResumeTest::test_short_write_crash_mid_append_persists_no_revision_then_resumes` |
| Rejected policy/config transition byte identity | CAS write (`write_json_atomic`) failure through the real `--switch-controller` `run_flow` seam | **PASS** | `test_m2_crash_resume.py::PolicyConfigTransitionCrashResumeTest::test_cas_write_failure_leaves_byte_identical_state_then_resume_commits` |

Every test above asserts BOTH halves of the required invariant: (a) the crash leaves no torn/partial/fabricated record and the durable current state is unchanged from the pre-attempt truth, and (b) an un-faulted resume through the exact same live call reproduces exactly what a clean run would have produced (never re-derives a duplicate, never skips the boundary, never diverges).

**Finding — the dependency-graph revision APPEND boundary still has no live `cowork.py` call site to crash-test against** (distinct from the dispatch-time CHECK boundary resolved in §6-a below): the graph-revision crash test above exercises Package B's real, durable `append_graph_revision` function directly — the deepest live seam that exists — because **no `cowork.py` call site writes a graph revision**. This is unaffected by the live-graph-wiring correction and is confirmed as an accepted residual (R-1) by E's own corrected review, not a new gap this audit is raising — see §6-a.

`gate reference`: `python3 -m unittest scripts/test_m2_crash_resume.py -v` (see §7).

### 6-a. F-LIVE-GRAPH-WIRING-1: RESOLVED — `check_dependency_graph_declaration` is now called from the live `cowork.py` dispatch seam

**F v1's fixed failure, quoted verbatim**: *"The live manifest-preflight seam in scripts/cowork.py calls run_manifest_preflight directly. Mechanical inspection finds zero live calls to cowork_preflight.check_dependency_graph_declaration or decide_work_unit_preflight. F's graph negative-control test calls the dormant helper directly, so it is not an end-to-end dispatch proof."* Its `required_resolution` directed: reopen Package E, bind the current WorkUnit and persisted graph revision into the live pre-dispatch decision so invalid declarations persist `rejected_preflight` and perform zero dispatch, obtain fresh review, integrate a signed commit, then rebase F.

**Resolution, mechanically re-verified at this base** (commit `999085483c624ac899fde24d709eee1a4cc753c0`, candidate `ae7e5840-8439-4b7a-b961-54a1bf685826`, reviewed APPROVE with 0 blockers/0 majors in `/Users/marcos/.claude/plans/m2-e-live-graph-wiring-v1-corrected-review.json`):

- `cowork.py:8558` `_compile_role_manifest` — the sole live call site of `preflight.run_manifest_preflight` (`cowork.py:8720`, confirmed a unique match by grep) — now calls `preflight.check_dependency_graph_declaration` unconditionally at `cowork.py:8640`, strictly before that `run_manifest_preflight` call, and any exception it raises (a malformed/hand-corrupted persisted node) or `ok: False` result is converted into the new `GraphDeclarationRejected` exception (`cowork.py:8540`) — the manifest is never compiled, preflighted, or persisted for that call.
- Five live seams now pass their own current, attempt-scoped `role_work_id` into this check and catch `GraphDeclarationRejected` ahead of their pre-existing broad `except Exception`: `run_scout` (`cowork.py:9246`), `run_planner` (`9637`), `run_builder` (`10029`), `switch_controller` (`11416`), and `ensure_controller_dispatchable`/pre-launch (`11523`, both routed through the new `_reject_graph_declaration` helper at `cowork.py:8944`, which idempotently mints the WorkUnit and advances it through `preflight_started` before `preflight_rejected` so the terminal transition is legal). The new `_current_role_work_id` closure (`cowork.py:12452`) derives the pre-launch/switch identity from the same epoch/attempt boxes the runners already use, so all five seams bind the identical WorkUnit — never a fabricated or parallel identity.
- This is proven end-to-end, not by inspection, by both E's own new tests (`test_cowork.py::LiveGraphWiringTest`, `::GraphDeclarationPreLaunchAndSwitchSeamTest`, `::GraphDeclarationStructuralGateTest`) and, independently, by this package's own `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_invalid_declaration_rejects_real_dispatch_path` (§0, §4) — which was written without reading E's test bodies beyond what the diff/review disclose, driving `cowork.run_scout` fresh.

**Consequence for coverage item 2 and negative controls 8-13**: the six pure graph shapes (cycles / dangling predecessors / duplicate work IDs / self-edges / cross-candidate fan-in / cross-policy fan-in) remain proven at the schema layer (Package A) and the durable-store append-boundary layer (Package B) exactly as before — §4's rows 8-13 are unchanged. What changed is the *dispatch-time* proof: F v1 proved `check_dependency_graph_declaration` fails closed only by calling it directly (the dormant-helper defect); F v2 additionally proves the exact same fail-closed outcome is what the real `cowork.run_scout` entry point itself produces, with zero manifest/session/send dispatch and a WorkUnit-bound `rejected_preflight` record — F-LIVE-GRAPH-WIRING-1's precise objection.

**Two residual observations, candidate-bound against Package E, non-blocking (mirrors E's own corrected-review residual-risk disclosure verbatim in substance)**:

1. **R-1, accepted by E's review, re-confirmed here**: no `cowork.py` call site populates a WorkUnit's `graph_revision`/`predecessor_work_ids` — `_ensure_work_unit` still mints them `None`/`[]` (confirmed: `append_graph_revision` has zero call sites in `cowork.py`, matching the crash/resume finding above), and `cowork_state.append_graph_revision` likewise has no production caller. Every *ordinary* real dispatch therefore still resolves to `check_dependency_graph_declaration`'s trivial-pass case (`graph_revision is None`); the enforcement this section proves is exercisable today only for a WorkUnit an external caller pre-mints with a graph declaration already attached — exactly the scenario `test_invalid_declaration_rejects_real_dispatch_path` and E's own `LiveGraphWiringTest` construct. Package E's updated receipt names this identical fact as `residual_binding` R-1 and states it explicitly: *"graph enforcement applies to persisted applicable declarations while legacy/no-applicable-graph sessions remain compatible; Package E did not auto-mint graph revisions."* This is a scope boundary the frozen brief accepts, not a defect this audit is newly raising.
2. `cowork_preflight.decide_work_unit_preflight` (the composed, WorkUnit-typed preflight decision Package C also built) still has **zero call sites** in `cowork.py` — confirmed by grep. E's correction wired `check_dependency_graph_declaration` directly rather than through that composed function; this achieves the same fail-closed outcome (confirmed by the tests above) but leaves `decide_work_unit_preflight` itself dormant. Recorded as **required disclosure**, not a blocker — E's review assessed and approved the direct-call approach.

---

## 7. Carried-forward Package E review minors and residual (F1, F2, F3, R-1 per E's updated residual_binding)

Per the CURRENT `package-e.json`'s `residual_binding` (updated when E was reopened for the live-graph-wiring correction): *"Carry the prior non-blocking E review minors plus accepted live-graph residual R-1 into Package F. R-1 records that graph enforcement applies to persisted applicable declarations while legacy/no-applicable-graph sessions remain compatible; Package E did not auto-mint graph revisions."* R-1 is assessed in §6-a (residual observation 1). The three prior minors (F1, F2, F3) are re-verified present, unresolved, and non-blocking (exactly as E's review scoped them) at this new base — none are silently omitted; two of the three cite lines that shifted with the live-graph-wiring correction, re-confirmed by direct read at this base:

| # | Minor | Location (this worktree) | Status |
|---|---|---|---|
| 1 | Scout pending-turn callbacks: the claude scout branch forwards `save_pending_turn_fn`/`clear_pending_turn_fn` to `_scout_loop`, aligning it with the opencode/codex branches — likely a correct latent-bug fix but outside E's named seams and uncommented | `cowork.py:9442` (`save_pending_turn_fn=save_pending_turn_fn` inside the claude branch; was `:9336` at F v1's base, shifted by the live-graph-wiring correction) | **PRESENT, unresolved, non-blocking** — carried forward, not fixed (outside F's authority: production file) |
| 2 | `cowork_state` append docstring accuracy: `_jsonl_append_unlocked`'s docstring claim that the residual final-window sliver "never applies" to `mint_work_unit`/`append_work_unit_transition` is imprecise now that `append_work_unit_transition_unlocked` writes from a signal handler | `cowork_state.py:2955-2958` (unchanged — `cowork_state.py` was not touched by the live-graph-wiring correction, confirmed by the §1 hash match) | **PRESENT, unresolved, non-blocking** — documentation accuracy only; the WorkUnit mirror is explicitly non-authoritative (`current_phase_state` remains sole authority) |
| 3 | Restoration of a `None` prior SIGTERM handler: `run_flow` only restores the prior SIGTERM handler when it is not `None`; when `signal.getsignal` returned `None` before install, `_handle_external_kill` stays installed after `run_flow` returns | `cowork.py:13004` (`if _prior_sigterm_handler is not None:`; was `:12820` at F v1's base, shifted by the live-graph-wiring correction) | **PRESENT, unresolved, non-blocking** — confirmed via direct code read, matches E's review exactly |
| R-1 | Graph enforcement (§6-a) applies only to a persisted, applicable (non-`None`) `graph_revision` declaration; no `cowork.py` seam auto-mints one, so legacy/no-applicable-graph sessions dispatch exactly as before | `cowork.py` (confirmed: zero `append_graph_revision` call sites; `_ensure_work_unit` still mints `graph_revision=None`) | **ACCEPTED residual, not a defect** — Package E's own updated receipt names this explicitly; carried forward as an accepted scope boundary, not reopened |

All three prior minors remain **findings**, not silently omitted, and not fixed here (production files are read-only to this package). R-1 is an **accepted residual**, not a reopen item, per Package E's own updated receipt. Each of F1-F3 is a candidate-bound reopen item against Package E.

---

## 8. Backend-gate criteria 2 and 3 evidence mapping

Canonical text, quoted verbatim from `/Users/marcos/.codex/skills/cowork-refactor-orchestrator/references/cowork-backend-gate.md`:

> **Criterion 2**: Ungoverned children are blocked or correlated to parent work and controller policy.

> **Criterion 3**: Aborted, denied, paused, and successful turns produce distinct persisted outcomes; a process exit cannot masquerade as a completed phase.

### 8-a. Criterion 2 evidence

Ungoverned/uncorrelated children are durably blocked with a governed terminal record, proven live over the real AF_UNIX GuardBroker socket: `test_m2_negative_controls.py::UncorrelatedChildrenTest::test_uncorrelated_child_blocked_with_durable_terminal_record` (independent re-proof); `test_cowork.py::RealBrokerChildDispatchCorrelationUnavailableTest`, `::GuardBrokerSessionProvenanceWiringTest` (E's own live wiring); `test_cowork_dispatch_identity.py` (C's isolated primitive). Effective controller/model/effort/policy inheritance failing closed on any missing field is proven in `test_cowork_dispatch_identity.py`.

**Status: `not_claimed`.** This evidence, together with §3 row 3, supports criterion 2's specific claim, but per the frozen brief this package explicitly withholds claiming it — see §9.

### 8-b. Criterion 3 evidence — five outcome classes mapped to a taxonomy member and a named test

| Outcome class | Taxonomy member | Named test |
|---|---|---|
| **Aborted** | `aborted` | `test_m2_negative_controls.py::NonCompletionMatrixTest::test_controller_abort_reaches_aborted_never_completed` (Ctrl-C); `test_m2_negative_controls.py::ExternalKillPositiveTerminalRecordTest::test_sigterm_at_gate_positive_durable_aborted_record` (SIGTERM, positive durable-record assertion) |
| **Denied** | `rejected_preflight` (capability/manifest/graph-declaration preflight refusal) and `needs_authority` (missing required capability) | `test_cowork.py::PreflightRejectionOrderingTest::test_session_start_failure_reaches_rejected_preflight_not_stuck_running`; `test_cowork_control_plane.py` (`("preflighting"/"running", "capability_missing") -> needs_authority`, the only edges targeting it); live `capability_missing` call sites `cowork.py:9263,9656,10048` (§2); `test_m2_negative_controls.py::DependencyGraphNegativeControlsTest::test_invalid_declaration_rejects_real_dispatch_path` (graph-declaration `rejected_preflight`, real dispatch path, §6-a) |
| **Paused** | `awaiting_capacity` — **provably unreachable in M2 by construction**, per Package A's own design (see §5) | `test_cowork_control_plane.py::test_awaiting_capacity_unreachable_m2` |
| **Successful** | `completed` — reachable ONLY via the sole candidate-bound `gate_validated` edge (§2) | `test_cowork.py::PhaseTruthCompletionBindingTest::test_scout_approval_reaches_completed_with_candidate_bound_evidence` |
| **Process-exit** | Never itself a taxonomy member/event (`cowork_control_plane`'s event vocabulary contains no raw exit code, EOF marker, or status-file-present signal — module docstring); a real process-killing signal (SIGTERM) durably lands on `aborted`, never `completed` | `test_m2_negative_controls.py::ExternalKillPositiveTerminalRecordTest::test_sigterm_at_gate_positive_durable_aborted_record`; §2's mechanical completion-emission enumeration (42/42 call sites accounted for, 0 process-exit-triggered paths to `gate_validated`) |

**Status: `not_claimed`.** This mapping is grounded in the canonical text and named, passing tests for all five classes including the deliberately unreachable `paused` class, but per the frozen brief this package explicitly withholds claiming it — see §9.

---

## 9. Explicit non-claim (mandatory disclaimer)

- **Criterion 2: `not_claimed`.**
- **Criterion 3: `not_claimed`.**
- **Criteria 1, 4, 5, and 6 are NOT ASSESSED by this package** — no evidence for them is gathered or implied anywhere in this artifact.
- **The full six-criterion Cowork backend gate is NOT CLAIMED, in whole or in part, by this audit.** Nothing in this document, nor in `test_m2_negative_controls.py`/`test_m2_crash_resume.py`, constitutes or implies a decision to move any package from `direct-Claude` to Cowork. This artifact is evidence only, for a future, separately-authorized gating decision.
- §6-a records that F-LIVE-GRAPH-WIRING-1 (dependency-graph declaration check never wired into any live `cowork.py` dispatch call site) is now **RESOLVED** by Package E's live-graph-wiring correction on this signed base, with two non-blocking residual observations carried forward: R-1 (enforcement applies only to an externally pre-minted graph declaration — no `cowork.py` seam auto-mints one) and `decide_work_unit_preflight` remaining an uncalled, dormant composed function even though the equivalent check it would run is now wired directly.
- §7 carries forward three non-blocking Package E review minors (F1-F3), still open, not fixed by this package (production files are read-only to F), plus the accepted R-1 residual (not a reopen item, per Package E's own updated receipt).

---

## 10. Controller-owned gates run (implementer-level verification; supervisor is the sole acceptance authority — see brief: "the supervisor runs each acceptance gate once")

Re-run fresh at this base (commit `999085483c624ac899fde24d709eee1a4cc753c0`), from the repo root, after the graph-negative-control replacement (§0):

1. `python3 -m unittest scripts/test_m2_negative_controls.py scripts/test_m2_crash_resume.py -v` — **RESULT: OK, 25/25 passed** (16 in `test_m2_negative_controls.py`, including `test_invalid_declaration_rejects_real_dispatch_path`; 9 in `test_m2_crash_resume.py`, byte-identical to F v1 — §0).
2. Combined run (exactly `scripts/test_cowork.py` plus every new A-F test file, no others): `python3 -m unittest scripts.test_cowork scripts.test_cowork_workunit scripts.test_cowork_control_plane scripts.test_cowork_state_m2 scripts.test_cowork_dispatch_identity scripts.test_cowork_policy_atomic scripts.test_cowork_recovery_breaker scripts.test_m2_negative_controls scripts.test_m2_crash_resume` — **RESULT: `Ran 2115 tests in 264.514s — OK (skipped=7)`. Zero failures, zero errors.** (`scripts/test_dispatch_contract_characterization.py` is deliberately excluded from this exact count: it was added in the pre-M2 commit `4935c8e` and is not a new A-F package test file; an earlier exploratory run that included it alongside these nine files also passed clean at `Ran 2155 tests ... OK (skipped=7)`, confirming it introduces no interaction failure either way, but the number cited above is the brief's exact scope.)
3. Audit completeness: §3 (8/8 coverage items) and §4 (15/15 negative controls) each carry an explicit PASS/FAIL line, a named test, and a gate reference — no prose-only claim.
4. Backend-mapping completeness: §8 quotes criteria 2 and 3 verbatim, maps all five outcome classes (including the deliberately-unreachable `paused`), and §9 marks both criteria `not_claimed` with the full-gate disclaimer present.
5. Exact three-path allowlist/hash and signed A-E integrity: §1 (all five signed-file hashes match exactly against the correct reference point per file; `HEAD` matches the signed base; F's own changed paths are exactly the three the brief allows).
