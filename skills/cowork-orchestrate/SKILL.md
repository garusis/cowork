---
name: cowork-orchestrate
description: Turn an engineering request into a bounded work package, run it on Cowork, and supervise it to an explicit outcome. Use for delegated implementation, planning, review, or recovery that needs explicit authority, candidate-bound acceptance, and fail-closed stopping; use cowork-cli for ordinary Cowork command mechanics and cowork-debug for session forensics.
---

# Cowork Orchestrate

Turn the request into one bounded package before dispatch. Record the objective,
base/candidate identity, allowed paths and actions, acceptance checks, provider
and spend policy, and stop/recovery conditions. Preserve the user's authority:
do not infer permission to publish, merge, spend, or widen scope, and do not ask
again for routine steps the package already authorizes.

## Transport

Cowork is the agent transport. Run the package through the `cowork` CLI per the
`cowork-cli` skill; do not duplicate its recipes.

- Before dispatch, run `cowork --check`. A failed preflight stops the package
  as `blocked` with the named missing piece; it is never an assumed pass.
- A `blocked` package stops and is reported. It never degrades into another
  transport. Run work through a direct Claude session instead only when the
  user explicitly asks for that for this work.
- Choose the smallest team and controller configuration that supplies the
  discovery, planning, implementation, and assurance the risk needs. Teams are
  paired and every new session begins with `scout` and `scout-reviewer`; there
  is no standalone planner, builder, or reviewer session.
- Select an explicit execution profile for implementation from the actual
  scope, risk and deliverable; use `cowork-cli` for preview and launch mechanics.
  Use `light` for bounded non-executable documentation batches, `standard` for
  behavior changes or executable tests, and `assurance` for architectural,
  security or authority invariants. Do not select by milestone or default to
  `standard` without judgment. Preserve explicit product/user checks and record
  the rationale and preview policy version/digest in package authority.
- Investigation-only and planning-only deliverables may use a manual paired
  team: every shipped profile includes building. Record the required stopping
  phase and why a profile exceeds scope; label the session manual/unprofiled.
  Do not invent profile-plus-team or stop-phase flags. An external supervisor
  validation is not a Cowork profile session.
- Existing unprofiled implementation sessions remain an explicit resume
  exception: Cowork cannot bind a profile to them after launch. Preserve their
  team, authority and manual/unprofiled attribution; use profiles for new work,
  not a restart or invented historical rationale for old work.

## Run and supervise

- Treat the final JSON run-result line as the outcome. Only rc 0 with
  `approved: true` is success; a stop, capacity pause, failure, or missing line
  is not.
- rc 4: answer a stop only within delegated authority, using the request-bound
  decision flags. A scope, risk, publication, or spending decision beyond it
  becomes `needs_authority` for the user.
- rc 5: preserve phase, candidate, policy, session identity, and completed
  evidence; resume only through the capacity `resume-trigger` path.
- rc 1/3/17: preserve evidence, then use only an authorized recovery (plain
  resume after fixing a cause, `--switch-controller`, or an authorized
  takeover). Never retry blindly or invent a reset, provider switch, or spend.
- For long-running work, schedule a 15-minute recurring wake unless durable
  state requests a later inspection. Query each active work once per wake from
  compact state; end silently when healthy and non-terminal. Do not add a
  polling loop or normal-path log tail.
- Use `cowork-debug` only when the run result, artifacts, trace, session
  identity, or status conflict.
- Verify the saved anchor and official execution-profile record against the
  intended selection after launch and at collection. Preserve the effective
  profile, promotions and their reasons on resume; never demote or relabel old
  sessions to reduce apparent cost. Missing historical rationale stays unknown,
  and retrospective explanations are marked as such. Compare equivalent task,
  risk, checks, model and evaluation cohorts using independent quality, usage,
  elapsed/recovery/repeated-verification time and external supervisor/review
  overhead; do not pool manual sessions with profiled ones.
- When observed work exposes an inadequately served task shape, use
  `cowork-internal` to find or file a `dogfooding` issue: include session/task
  evidence, why existing profiles or exceptions are insufficient, expected
  benefit as a hypothesis, retained safeguards and a measurable comparison.
  Consider adapting an existing profile before adding one. Recording an idea
  neither authorizes implementation nor blocks unrelated work.

## Governed parallel graph

When a package splits into independent vertices, run them through `cowork graph`
(field shapes and the reason table are in the README's "Governed parallel
graph" and the `cowork-cli` skill). Cowork records and fences; you create the
worktrees, launch the children and merge. Branch on the `cowork_graph_result`
line, never on stderr, and never treat a child's process exit as evidence.

- Pre-create one worktree per vertex at its base commit with `.cowork/` ignored
  and write one authority file per vertex. Write the revision document and run
  `cowork graph admit`; read `cowork graph status`.
- `cowork graph claim` the next ready vertex and read `cwd` and `launch_argv`
  from the claim result line. Launch the child as
  `cowork <launch_argv> --context-file BRIEF` from `cwd` and supervise it like
  any other run. Launch refusals arrive on the ordinary
  run-result line (`reason` holds the graph code), not on the graph line.
- When the child's paired reviewer approves with a green owned verification,
  run `cowork graph publish`. rc 0 `ok` also covers `publish_outcome:
  already_published`. rc 3 `owner_conflict` means retry later; use `--take-over`
  only when the owner is proved dead.
- Record an explicit failure with `cowork graph fail --reason-code`. Run
  `cowork graph cancel` to stop work; `cancel_requested` is not final, so
  repeat it until `cancelled`. Run `cowork graph reclaim` only after
  `claim_deadline` or for a proven-dead child.
- Run `cowork graph join` once the members are terminal, then act on its
  `decision`. A join is a decision record and not a merge: merging the
  candidates stays a separate act you do, within your authority.
- The production cap of 1 means claim one vertex at a time; do not assume
  parallel production.

## Accept

Cowork commits nothing. After an approved build, review the working-tree diff
against the package scope yourself and run verification proportional to the
risk: focused checks for a narrow change, broader suites when shared behavior
moved. Bind acceptance to the exact candidate you reviewed; a changed candidate
needs its affected checks re-run.

Cowork's owned transaction gives each verification command a fixed 300-second
outer deadline; a test runner's own larger timeout does not extend it. Schema-2
plans still require one last, genuinely complete `final_suite`. Do not relabel a
shard as complete. If the full suite cannot fit one command, use a schema-3
composed suite: check that the receipt's binding is `components_ran_once` with
every component green, and judge from the receipt's declared universe whether
it really is the complete regression suite (Cowork proves only the partition).
Separately authorized supervisor evidence may support the supervisor's
acceptance decision, but it does not turn a focused Cowork receipt into a
complete-suite receipt.

Tests that protect product behavior belong in the repository: a permanent test
protects behavior expected of every future revision on neutral inputs.
Delivery evidence stays outside product source and outside Git, in the package
directory: package receipts, audits, run results, candidate/base ancestry pins,
scope snapshots, gate transcripts/counts and historical
implementation-state assertions. A mixed check keeps its durable behavior with
neutral inputs and non-historical assertions and drops the rest.
Version control operations, controlled fixtures, security negatives,
compatibility inputs, regression references and product fields are
legitimate; a keyword alone is never grounds for rejection.

Report the package state, run outcome and reason, accepted candidate, checks
run and their results, and any remaining authority gap or unverified risk.
