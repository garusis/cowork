# AGENTS.md

Shared, vendor-neutral working notes for any AI/CLI agent (Claude, Codex, etc.)
operating in this repo. Keep entries factual and tool-agnostic.

## Repo shape

- This repo contains the `cowork` CLI plus bundled support skills under
  `skills/`.
- `cowork` is a thin launcher for `scripts/cowork.py`. When `.venv/bin/python`
  exists, the launcher re-execs into that interpreter.
- `cowork` is driven agent-to-agent over process arguments. There is no
  human-interactive product path: no menus, keyboard prompts, terminal
  approval gates, or interactive recovery. Do not add one.
- `roles/*.md` are prompt contracts for the `cowork` roles. Behavior changes
  usually need matching updates in the relevant role spec, orchestration code,
  tests, and README.

## Development commands

```bash
python3 scripts/cowork_offline_tests.py test_cowork
python3 scripts/cowork_offline_tests.py test_cowork.SomeTest.test_x
python3 scripts/cowork_offline_tests.py test_evidence_lifetime_contract
./cowork --check
```

Notes:
- Run tests through `scripts/cowork_offline_tests.py` with explicit unittest
  ids, not plain `python3 -m unittest`. The suites use fakes, but a bug can
  still reach a real provider binary; the harness adds deny stubs, an
  in-process launch guard, a preflight probe, and a sentinel that fails the
  gate. It stops accidental launches by ordinary code only: it is not a
  sandbox against deliberately hostile code, and `HOME` is not redirected.
  Exit codes: 0 pass, 1 tests failed, 2 usage, 3 provider boundary violation,
  4 setup/preflight failed, 5 timeout.
- Run the harness self-tests plain from `scripts/`, never nested inside the
  harness: `cd scripts && python3 -m unittest test_cowork_offline_guard`.
- `COWORK_LIVE=1 python3 -m unittest scripts/test_cowork.py` runs live CLI
  integration tests that launch real providers and make API calls. It is a
  separate, explicit validation outside the offline barrier (which strips
  `COWORK_LIVE`); use it only when intentionally verifying installed
  controller behavior.
- The runtime is standard-library only; `requirements.txt` lists no packages.

## Session and generated state

- `.cowork/`, `.plans/`, `.venv/`, and `.worktrees/` are local/generated and
  gitignored. Roles on the `opencode` controller also generate
  `.opencode/agents/cowork-<role>.md` agent files in the working directory
  (rewritten every spawn — runtime state, not source).
- Project-local `.cowork/session*.json` files are resumable session anchors.
  Treat them as runtime state, not source files.
- Per-session artifacts live under `~/.cowork/sessions/<session_uuid>/`
  unless `COWORK_SESSIONS_ROOT` overrides the location.
- `cowork` does not commit or open PRs; approved build output is left in the
  working tree for the orchestrator to review.

## Evidence lifetime

- Permanent tests protect behavior expected of every future revision: they
  run on neutral inputs and assert nothing about one delivery.
- Delivery evidence stays outside product source and outside Git, in the
  session or package directory: package receipts, audits, run results,
  candidate/base ancestry pins (commit or tree ids, "frozen base" claims),
  scope snapshots ("only these paths differ from base"), gate transcripts/counts
  (unittest logs, harness summaries, "the suite ran N tests" claims) and
  historical implementation-state assertions (what was true of one candidate
  at delivery time).
- A mixed check keeps its durable behavior with neutral inputs and
  non-historical assertions and drops the rest.
- Legitimate, and never rejected on a keyword alone: version control
  operations in throwaway repositories, controlled fixtures (synthetic ids,
  digests, timestamps, placeholder paths), security negatives,
  compatibility inputs (real transcript or log shapes fed to code under
  test), regression references (issue numbers, observed field values used as
  inputs, package labels in prose) and product fields (`expected_test_count`,
  receipt and transaction schemas).
- Recurrence check: `python3 scripts/cowork_offline_tests.py
  test_evidence_lifetime_contract` scans product source for representative
  shapes of each forbidden category, proves itself on neutral positive and
  negative examples, and asserts that the role contracts and orchestration
  skills carry this rule. It detects representative shapes only; its module
  docstring documents what it cannot infer.

## Implementation notes

- `scripts/cowork_bridge.py` owns Claude/Codex/opencode command assembly, event
  parsing, stream handling, and probe behavior. Keep flag changes covered by
  focused tests.
- Controllers are `claude`, `codex`, and `opencode`. Each role config carries
  `controller`, `model`, `effort`, `yolo`, `mode` — model/effort `None` means
  the controller CLI's own default, and opencode model ids are
  `provider/model`. Switching a role's controller resets its model/effort.
- `scripts/cowork_state.py` owns session discovery and persistence. Preserve
  compatibility with legacy `.cowork/session.json` files when changing state.
- Owned-verification commands have a Cowork-controlled 300-second outer
  deadline that applies to every owned command. A test runner's larger
  timeout does not extend it. Schema-2 plans still require one last,
  genuinely complete `final_suite`; a complete suite that cannot fit one
  command uses schema-3 composed `final_suite_component` entries whose exact
  partition of the declared universe Cowork proves (see `roles/planner.md`).
  Never relabel a shard to work around the deadline.
- Role status/review artifacts are JSON contracts read by the orchestrator.
  Keep schema changes reflected in roles, README, and tests.
- Execution-profile policy (light/standard/assurance) lives only in
  `scripts/cowork_execution_profiles.py` — never in `cowork_profiles.py`, which
  owns controller authentication. Runtime modules consult it through thin hooks
  guarded by `profile_session is not None`, and an unprofiled session must take
  its unchanged paths. Later packages that schedule work consume
  `resolved_vertex_policy`; they do not re-derive policy.
- Measurable-goal contract: scout intel must carry `result.success_criteria`
  (1–5 of `{statement, measurement, expected, tier: must|should}`); the plan
  must map each criterion in `result.criteria_coverage` to steps + a
  `result.verification` entry. `_success_criteria_flag` (cowork.py) injects a
  structure-only auto-finding into the scout-reviewer brief when the list is
  missing/empty — quality judgment stays in the reviewer prompts
  (scout-reviewer "goal measurability", planning-advisor "criteria coverage",
  mirrored in EVAL_CRITERIA).
- Evaluation traceability: `scores.json` entries (schema 2) are stamped with
  evaluator/evaluatee tool+model+session-id, per-eval `usage`/`duration_ms`,
  `eval_turn_id`/`specs_in_turn` (shared-turn dedupe), and `reviewed_verdict`.
  The stamps come from two optional inputs read by `_aggregate_eval`: the
  eval-turn sidecar `<scratch>.turn.json` (written by the eval sender) and the
  per-session `identities.json` registry (refreshed by `_send` on every turn).
  Both are tolerant — absent inputs reproduce the legacy entry shape, so test
  fakes need no changes. Per-role model pins ride config token `model=<id>`
  (claude `--model`; codex `--model` fresh / `-c model=…` on resume).
  `cowork --report` appends the scores/usage analysis when `scores.json`
  exists (`cowork_report.summarize_scores` / `render_scores_report`).

## Agent command transport

Contract source: `build_parser`, `select_session`, `build_run_result` and
`main` in `scripts/cowork.py`; the full description is README "Usage".

- Every run writes exactly one JSON run-result line as the last line of stdout
  (`cowork_result`, `rc`, `outcome`, `approved`, `stop`, `reason`,
  `session_file`, `resume_argv`, and `decision_argv` for an open decision).
  The transcript goes to stderr. `rc` equals the exit status; a missing line
  is a failure. Exit codes: 0 approved, 1 failed, 2 invalid invocation,
  3 owner conflict, 4 stopped for a decision, 5 awaiting provider capacity,
  17 provider refusal/no first token, 130 SIGINT, 143 SIGTERM.
- Session selection is explicit and exclusive: no selector or `--new` starts a
  new session (requires `--context`/`--context-file`); `--session-file PATH`
  names one; `--resume` selects this directory's most recent saved session;
  `--no-session` is ephemeral and also requires `--context`/`--context-file`.
  A run without a selector never resumes saved work.
- Decisions are request-bound: `--answer REQUEST_ID` (with context),
  `--authorize-handoff REQUEST_ID`, `--decline-handoff REQUEST_ID`, one per
  invocation, each requiring `--session-file` or `--resume`. Approval comes
  only from the paired reviewer's `approve` verdict; a missing reviewer,
  answer, or authorization never approves.
- Capacity pauses (rc 5) are replayed by the separate `cowork resume-trigger`
  entry point, which requires all four identities (`--session-uuid`,
  `--lease-id`, `--claimant-ref`, `--automation-ref`) plus `--cwd LAUNCH_DIR`,
  because the session anchor lives in the launch directory even with
  `--worktree` (the default is the current directory); the session then
  continues with a plain `--session-file` run. Decision delivery on that path is at least once, not
  exactly once. A crashed claimant is retriggered with the same four
  identities and `--cwd LAUNCH_DIR`; that retrigger can replace the claimed
  lease, so re-read the current lease from a plain resume before triggering
  again.

## Git worktrees

Worktrees may live **inside** the repo under `.worktrees/` (already gitignored),
which keeps `git status` clean — no babysitting what to commit.

```bash
git worktree add .worktrees/feature-x -b feature-x
```

Notes:
- `.worktrees/` is in `.gitignore`; add it to `.git/info/exclude` too as a
  local backstop if you want belt-and-suspenders.
- Each worktree needs its own venv (not auto-copied):
  `cd .worktrees/feature-x && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- `.cowork/` session state is per-working-tree — not shared across worktrees.
- Sibling-outside (`../cowork-worktrees/`) is the cleaner general default;
  inside `.worktrees/` is the chosen approach here.

### `--worktree`

- `cowork --worktree [name]` runs a small **worktree role** before scouting. It
  reads THIS file to follow the repo's worktree convention — for this repo:
  `.worktrees/<name>` created with `git worktree add .worktrees/<name> -b
  <name>`, **plus** the documented per-worktree setup above (its own venv +
  `pip install -r requirements.txt`). The role applies that setup as part of
  following the convention; a repo that documents no setup gets a bare worktree.
  cowork then redirects (`os.chdir`) into the worktree for the rest of the run.
- Resume-from-launch-dir constraint: with `--worktree`, the cowork session store
  (`.cowork/session.<uuid>.json`) stays in the **launch** directory, not the
  worktree. Resume the session from the launch directory (or via
  `--session-file`), not from inside the worktree, and pass
  `--cwd LAUNCH_DIR` to `cowork resume-trigger`. Per-session assets under
  `~/.cowork/sessions/<uuid>/` are always found.
