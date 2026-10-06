# cowork

`cowork` is an agent-to-agent orchestration command. An orchestrating agent
invokes it with arguments; it assembles a team of CLI-driven roles, spins up
the controller CLI configured for each role (`claude`, `codex`, or
`opencode`), and reports every run as one structured JSON result. Every role
can also pin a model and a thinking-effort level (and, with opencode, the
provider — it is embedded in the `provider/model` id).

Command-line process invocation remains, but only as the transport between
agents: there is no human-interactive product path — no menus, keyboard
questions, terminal approval gates, or interactive recovery. Nothing is ever
approved by omission.

The phases:

- the **scouting phase** — the **scout** (a context gatherer that explores the
  work and confirms a solid starting point) paired with the **scout-reviewer**
  (a critical reviewer that checks the scout's questions, assumptions, and
  discoveries are actually aligned with the goal), and
- the **planning phase** — the **planner** (turns the approved intel into an
  implementation plan, delivered as a machine-readable plan JSON plus a
  readable plan markdown) paired with the **planning-advisor** (a critical
  reviewer of the plan with the same verdict semantics as the scout-reviewer),
  and
- the **building phase** — the **builder** (executes the approved plan by
  editing the repository, verifies the changes, and leaves them in the working
  tree) paired with the **build-reviewer** (a critical reviewer that checks the
  builder's working-tree diff against the plan, with the same verdict semantics
  as the other reviewers).

Phases form a **loop**, not a one-way chain: a reviewer-approved intel chains
straight into planning in the same run, an approved plan chains into building,
and an orchestrator-authorized hand-back can run either edge backward — the
planner back to the scout, or the builder back to the planner (see
[Phases and the hand-back](#phases-and-the-hand-back)). An approved build ends
the run; cowork makes no git commit and opens no PR.

## How it works

An orchestrating agent runs `cowork` as a child process. Each invocation:

1. **Selects a session from arguments alone** — a new session (the default, or
   `--new`), a named saved session (`--session-file PATH`), the directory's
   most recent saved session (`--resume`), or an ephemeral one
   (`--no-session`). At most one selector; saved work is never picked
   implicitly.
2. **Takes team, config and context from arguments** — `--team`, `--config`,
   `--context`/`--context-file` (a new session requires context), or the saved
   team/config on a resumed session.
3. **Runs a preflight** and drives the current phase's lead role and its paired
   reviewer through the configured controllers. Approval comes only from the
   paired reviewer's explicit `approve` verdict.
4. **Ends with one JSON run-result line on stdout** and a matching exit code.
   The provider transcript goes to stderr. Anything that needs an answer, an
   authorization, or an unavailable reviewer stops the run unapproved with a
   structured request; the orchestrator resumes the session with another
   invocation (see [Usage](#usage)).

### The bridge

The three controllers are driven differently because their non-interactive
modes differ:

- **claude** runs as a single persistent duplex process
  (`claude -p --input-format stream-json --output-format stream-json`). Each
  orchestrated turn is framed as a stream-json user message on stdin; the
  assistant's output streams back on stdout.
- **codex** runs turn-based: the first turn is `codex exec --json`, from which
  `cowork` captures the session's `thread_id`; each follow-up turn is
  `codex exec resume <thread_id>`. (codex `exec` has no persistent stdin, so
  every turn is a fresh process resumed by id.)
- **opencode** runs turn-based too: each turn is
  `opencode run --format json`; the first turn reveals the session id
  (`ses_…`) and follow-ups pass `--session <id>`. The role prompt is delivered
  as a generated agent file (`.opencode/agents/cowork-<role>.md`, a system
  prompt like claude's) rewritten on every spawn so it always matches the
  current config.

#### Controller process cleanup

Every controller process starts in its own process group, and Cowork records
that group at spawn. When the process closes, Cowork first asks it to finish,
waits a bounded time, and then escalates. The escalation is `SIGTERM`, a grace
period, and then `SIGKILL`, sent only to that recorded group. Cowork then
checks that the leader and every member of the group are gone:

- **claude** closes when the role or session closes: stdin EOF, then up to 5 s
  to exit by itself. A turn that ends, or that times out waiting for its
  first token, leaves the process running for the next turn.
- **codex** and **opencode** close at the end of every turn. After stdout
  reaches EOF, the process gets up to 10 s to exit by itself. An exception, an
  interrupt, or a first-token timeout sends `SIGTERM` at once. Any process the
  turn left behind in its group is ended too. A process that moved itself into
  a new session is not.
- The grace period after `SIGTERM` is 3 s, and `SIGKILL` gets 3 s to be
  confirmed.

The result is secondary evidence and never changes the turn's or the role's
result. It is recorded in the `controller.cleanup` trace event (`outcome`:
`already_exited`, `graceful`, `terminated`, `killed` or `failed`, plus
`confirmed`) and in the `cleanup_outcome`/`cleanup_confirmed` fields of
`role.end`. A `resume-trigger` process closes the controller it started on
every path. If it receives `SIGTERM`, it still releases its owner lease and
writes one `internal_error` line (unless it already wrote one), with the
internal-error exit code. Unrelated processes and verification workers are
never signalled.

Known limit: if the owner process is killed together with its whole process
group (for example `SIGKILL` or `SIGHUP` to the group), a codex or opencode
turn that is producing no output keeps running, because it is in its own
group. A claude process and a turn that is still writing output end on their
own, through stdin EOF or a broken pipe.

### Cross-role handoff (one file-only transport)

Every hand-off between roles — scout↔scout-reviewer, scout→planner,
planner↔planning-advisor, planner→builder, builder↔build-reviewer, both
hand-backs, every controller switch, the peer evaluations, and context
revision/resume — goes through **one shared, topology-driven transport**
(`scripts/cowork_handoff.py`). A cross-role prompt carries only:

- **absolute authoritative file paths** (each with size + sha256), which the
  receiving CLI reads from disk — never the pasted body, findings, question,
  hand-back payload, verdict, or context text; and
- a few **content-free orchestration facts** (closed-schema enums like
  role/phase/controller, counts, hashes, path/byte metadata, and normalized
  reason codes).

A declarative **edge registry** lists every hand-off in one place and a single
renderer (`render_handoff`) is the only thing that emits a cross-role prompt, so
a new role can't reintroduce a divergent "paste the body" pattern. It **fails
closed on structure**: every artifact must be tagged with a declared source
slot, every required slot must be filled (per-file cardinality, so a plan pair
can't ship half), facts are validated against closed per-key enums, and the
`ctx` composition is restricted to declared, type-checked keys — labels are
registry-owned, so nothing free-form can ride through a label or ctx field. The
same handoff object that builds the prompt also feeds the trace/token accounting
(one source of truth, no re-inference). Reviewer findings reach the lead by
path — the lead reads the review file itself. The **initial context for the
active lead may remain inline**; orchestrator answers are written to a session
file and delivered by path. Every cross-role re-delivery of that shared
context — including the scout→planner and planner→builder seeds — carries it by
path. Reviewers re-read the current authoritative files each round (there is no
derived incremental-diff packet — a generated diff could go stale and compete
with the real files). The invariant is enforced structurally, not by
convention: a full-module AST analyzer flags any function that builds a prompt
from a body-like input outside `render_handoff`, a registry-driven live-route
matrix ties every role/pair to its required edges, and closed schemas reject
free-form facts, undeclared `ctx`, and smuggled labels.

Delivery is provenance-checked at the central controller gateway as well as at
render time. Cross-role turns must arrive in an opaque envelope produced from
one or more real `HandoffBlock`s; the envelope retains their registered edge
identities and exact descriptors. Its delivered bytes are assembled inside the
transport from those exact blocks plus typed static-role fragments; the factory
does not accept an independent free-text body. Arbitrary prompt text plus forged
`prompt_kind`/`artifacts` metadata is rejected before it reaches a controller.
The only non-handoff envelopes come from closed constructors for the initial
user turn, static role instructions, and typed lead continuations. Raw
controller `.send()` remains private to that gateway.

Roles and reviewer pairs are declared in the same canonical registry that
drives role selection, fact validation, and topology validation. Every
handoff-capable role must occur in the edge graph, and every reviewer pair must
have its review-context edge; a role can opt out only through the explicit
`non_handoff` classification used by the pre-phase worktree helper.

### Controllers and modes

The flags `cowork` emits per (controller, mode, yolo), verified against
**Claude Code 2.1.x**, **codex-cli 0.133.x**, and **opencode 1.17.x**:

| Setting | claude | codex | opencode |
| --- | --- | --- | --- |
| plan mode | `--permission-mode plan` | `--sandbox read-only` | agent `permission: edit: deny, bash: ask` |
| implement, yolo off | `--permission-mode acceptEdits` | `--sandbox workspace-write` | agent `permission: edit: allow, bash: ask` |
| implement, yolo on | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--auto` |

Per-role **model** and **thinking effort**, when set (both default to the
controller CLI's own setting):

| | claude | codex | opencode |
| --- | --- | --- | --- |
| model | `--model <alias-or-id>` | `-c model="<id>"` | `--model <provider/model>` |
| effort | `--effort <level>` (low…max) | `-c model_reasoning_effort="<level>"` | `--variant <level>` (provider-specific) |

Notes:

- `codex exec` is already non-interactive (it never prompts), so approval policy
  is set entirely by the sandbox — there is no `--ask-for-approval` flag on
  `exec`. `cowork` also passes `--skip-git-repo-check` so it runs outside a git
  repo, and `codex exec resume` inherits the original session's sandbox (it
  rejects `--sandbox`). Model/effort use `-c` (not `-m`) so fresh and resumed
  turns take the identical spelling.
- The `scout` role spec is preloaded into claude via `--append-system-prompt-file`,
  into codex by prepending it to the prompt, and into opencode via the
  generated `.opencode/agents/cowork-<role>.md` agent file — `cowork` never
  writes an `AGENTS.md` into your repo.
- **yolo off has no approval relay**: a tool the permission/sandbox level does
  not auto-allow is denied and surfaced as an error in the transcript (the run
  does not hang).
- opencode has **no OS sandbox**; its agent permission rules are the only
  guardrail, and in its non-interactive `run` mode any rule that resolves to `ask` is
  auto-rejected by opencode (acts as a hard deny — the run never hangs). Its
  plan mode is "no edits, no shell" rather than codex's read-only-commands
  sandbox.

### Safety

With yolo on, claude runs with `--dangerously-skip-permissions`, codex with
`--dangerously-bypass-approvals-and-sandbox`, and opencode with `--auto` — all
bypass approval/sandbox guards. Run `cowork` in a trusted/isolated workspace.

## Requirements

- **A git work tree.** This is a prerequisite, not a runtime condition: every
  cowork run must be launched inside a git work tree, because each role runs
  confined to a write boundary derived from the launch directory's git
  toplevel, and outside one there is no boundary to confine a role to. A run
  launched outside a git work tree is refused before anything is dispatched
  (rc 2, `reason: requires_git_work_tree`) — launch from inside a repository,
  or run `git init` there first. There is no opt-out.
- Python 3.9 or newer. The runtime uses only the standard library
  (`requirements.txt` lists no packages).
- The controller CLIs you intend to use, on your `PATH`:
  - **Claude Code** — `npm install -g @anthropic-ai/claude-code`
  - **Codex CLI** — `npm install -g @openai/codex` (Node 18+) or
    `brew install --cask codex`
  - **opencode** (optional) — `curl -fsSL https://opencode.ai/install | bash`,
    `npm install -g opencode-ai`, or `brew install sst/tap/opencode`; then
    authenticate providers with `opencode auth login` (`opencode models` lists
    the `provider/model` ids you can pass as `model=`)

`cowork --check` reports exactly which of these is missing. During a normal run
the interpreter is checked up front, while controller CLIs are checked when the
role that needs them is about to launch; a missing controller ends the run with
a structured failure, and the orchestrator can re-invoke with
`--switch-controller`.

## Install

Clone this repository into a local tool directory and run the installer:

```bash
git clone <repo-url> ~/.local/share/cowork
cd ~/.local/share/cowork
./install.sh
```

`install.sh` creates a dedicated `.venv` (installing `requirements.txt` into
it), adds the checkout dir to your `PATH` via `~/.zshrc`, makes `cowork`
executable, links bundled skills from `./skills/` into both `~/.claude/skills`
and `~/.codex/skills`, and runs the preflight to report any missing controller
CLIs. It is idempotent — safe to re-run.

After a new shell (or `source ~/.zshrc`), agents can invoke `cowork` from **any
folder inside a git work tree** — the launcher re-execs into the venv when it
exists, the project-local `.cowork/session.<uuid>.json` store lands in the
current directory, and the session's produced artifacts live under
`~/.cowork/sessions/<session_uuid>/`. Re-verify anytime with `cowork --check`.

> Manual alternative: run `./cowork` from this directory with any Python 3.9+.

## Usage

`cowork` is invoked by an orchestrating agent. It never reads a terminal or
prompts; every run is driven by arguments.

### Run result and exit codes

Every run invocation writes **exactly one JSON object as the final line of
stdout**; the provider transcript and `cowork:` notices go to stderr. The
record's `rc` equals the process exit status. A consumer binds the record to
that status and treats a missing line (for example after SIGKILL) as a failure.

Fields: `cowork_result` (schema version, `1`), `rc`, `outcome`, `approved`,
`session_uuid`, `session_file`, `persisted`, `phase`, `role`, `phase_outcome`,
`stop` (the structured request or failure details, or `null`), and `reason` (a
closed refusal/failure code, or `null`). A persisted session also carries
`resume_argv` (`["--session-file", PATH]`); an open decision request adds
`decision_argv`, the argument vectors that can consume it. The answer variant
contains the literal placeholder `<answer>`, which the orchestrator replaces
with the path of a file holding its answer. `decision_ack_failed` appears when
an accepted decision delivery could not be acknowledged (it is re-sent later).

| rc | `outcome` | meaning |
| --- | --- | --- |
| 0 | `approved` | the last phase that ran was approved by its paired reviewer |
| 1 | `failed` | controller/preflight/reviewer failure, a failed phase, or an internal error |
| 2 | `invalid_invocation` | refused before anything was dispatched (`reason` names why) |
| 3 | `owner_conflict` | another process holds the session's single-writer owner lease |
| 4 | `stopped` | an open decision request needs an answer or authorization (`stop.request_id`) |
| 5 | `awaiting_capacity` | durably paused on a provider capacity signal |
| 17 | `terminated` | provider refusal, or no first token before the deadline |
| 130 | `interrupted` | SIGINT |
| 143 | `terminated` | SIGTERM |

Only rc 0 with `approved: true` is success. A stop, a capacity pause, or a
missing result line is never an approval.

`cowork graph OP` is a separate command: it emits its own single result line
(`cowork_graph_result`) with its own exit codes 0, 1, 2 and 3, never 5, where
rc 3 means an ownership or lease conflict. See
[Governed parallel graph](#governed-parallel-graph).

A run launched outside a git work tree is refused with rc 2 and
`reason: requires_git_work_tree` before any session, lease, trace or dispatch
exists; from such a directory that refusal pre-empts the session-selection and
decision refusals described below.

`--help` (exit 0) is not a run and emits no record. The read-only commands
`--check`, `--report`, and `--session-owner` print their own output and emit no
run-result record, except that combining one with a session-mutating flag is
refused with an rc 2 record. `--evaluate-role` also emits no run-result record
and uses its own exit codes: 0 recorded, 1 could not record, 2 invalid
arguments.

### Starting and selecting sessions

```bash
# new session (the default selector): scouting with its paired reviewer
cowork --team scout,scout-reviewer --context-file ./brief.md

# per-role controller/model/effort; context from stdin
cowork --config "scout=codex,model=gpt-5-codex" \
       --config "builder=opencode,model=anthropic/claude-sonnet-4-5,effort=high" \
       --context-file - < ./brief.md

# continue a saved session with nothing to decide
cowork --session-file .cowork/session.<uuid>.json
```

- **Session selectors — at most one** (`conflicting_session_selectors`
  otherwise):
  - none, or `--new` — a new session at `.cowork/session.<uuid>.json`;
    requires `--context`/`--context-file`.
  - `--session-file PATH` — resumes that saved session when the file exists,
    or creates a new session there (with context) when it does not.
  - `--resume` — explicitly resumes this directory's most recent saved session
    (an error when there is none). Prefer `--session-file` with the
    `session_file` from an earlier run result when more than one session may
    exist.
  - `--no-session` — ephemeral; nothing is read or written (no trace, no
    `resume_argv`). Requires `--context`/`--context-file`.
- A plain invocation without a selector never resumes saved work.
- `--team ROLES` — comma-separated roles (default: every role, or the saved
  team on resume). Every lead on the team needs its paired reviewer
  (`reviewer_not_selected` otherwise), and a run that starts in scouting needs
  `scout` (`scout_not_selected`).
- `--config ROLE=opt,opt` — repeatable; tokens are `claude|codex|opencode`,
  `model=<id>`, `effort=<level>`, `yolo|no-yolo`, `plan|implement`.
  `model=default`/`effort=default` reset to the controller CLI's own setting;
  opencode model ids are `provider/model`. Running a pair on two specific
  models lets their evaluation scores and token use be compared (see
  [Evaluation traceability](#evaluation-traceability)).
- `--context TEXT` / `--context-file PATH` (`-` = stdin) — initial context for
  a new session. On a plain resume it redirects the session (a new
  [context revision](#context-revisions)); with `--answer` it is the answer and
  never replaces the goal. A resume without context continues the current
  phase.
- `--evaluation-policy all_rounds|final_round|sampled|off` — see
  [Scoring stays out of the way](#scoring-stays-out-of-the-way).
- `--output-root DIR` — repeatable; declare an external evidence output root
  the **builder only** may write into. `DIR` must be an existing directory;
  it is canonicalized to its real path and bound to the session record before
  anything is created. Refused (rc 2) when it is exactly `/`, your home,
  `/tmp` or `/private/tmp` (`output_root_unsafe`); when it equals, lies
  inside or contains the repository, any registered worktree, the cowork
  sessions root, the session-file directory, or a controller's real
  home/state directory (`~/.claude` or `CLAUDE_CONFIG_DIR`, `~/.codex` or
  `CODEX_HOME`, opencode's data dir) — also `output_root_unsafe`; when it
  is not absolute or not a usable string (`output_root_invalid`); when it
  is not an existing directory (`output_root_missing`); or when two declared
  roots duplicate or nest each other (`output_roots_conflict`). It cannot be
  combined with `--no-session` (`conflicting_arguments`). On a resume, omit
  the flag to reuse the saved roots or repeat them exactly; a saved session
  that has no declared roots accepts a first declaration; a set that differs
  from saved roots is refused (`output_roots_conflict`) and the record is
  left untouched. The grant is re-applied on every builder spawn (claude
  and codex `--add-dir`, codex resume `sandbox_workspace_write.writable_roots`)
  and enforced by the hook policy and the OS sandbox: parents, siblings,
  `..` and symlink aliases of a declared root stay denied.
- `--take-over` — take over a saved session's single-writer owner lease from a
  prior process. Never implicit: a crashed owner is reclaimed only with proof
  of death, a live same-host owner is terminated first, and an owner that
  cannot be proved dead (foreign host, unreadable process table) is refused.
- Saved-session operations (`--switch-controller`, `--allow-controllers`,
  `--answer`, `--authorize-handoff`, `--decline-handoff`, `--take-over`)
  require an explicit `--session-file` or `--resume`
  (`saved_session_selector_required`).
- Every refusal in this section assumes the launch directory is a git work
  tree. From a directory that is not one, `requires_git_work_tree` (rc 2)
  pre-empts them — including `session_not_found`,
  `saved_session_selector_required`, `conflicting_session_selectors` and every
  decision refusal — because such a directory can never have hosted a session.

### Execution profiles

An **execution profile** is an explicit, versioned policy that jointly decides
the role topology, the batch boundary, the validation and review cadence, how
evidence is invalidated and reused, and when the session is promoted to a
stricter profile. Sessions started without `--profile` run exactly as before.

```bash
# read the complete effective policy first; prints one JSON object, dispatches
# nothing and creates no session
cowork --preview-profile light

# start a NEW session under it (the profile derives the team)
cowork --profile light --profile-rationale "two docs, no code" \
       --context-file ./brief.md
```

| Profile | Roles | Reuse | Review notes | Invalidation |
| --- | --- | --- | --- | --- |
| `light` | scout, scout-reviewer, builder, build-reviewer (the approved scout intel is the plan) | per entry, by dependency digest | minor notes deferred on an approve | changed artifact plus its direct derivatives |
| `standard` | all six | per entry, by dependency digest | minor notes deferred on an approve | changed artifact plus its direct derivatives |
| `assurance` | all six | none across candidates | every finding must be fixed; deferred notes are refused | everything reruns on any change |

Every profile requires the owned verification transaction with its final
suite, paired reviewer approval of each phase that runs, and any
user-declared check: a profile never weakens a required check. Every phase that
runs keeps its paired reviewer; `light` omits the planning pair, not review.

- `--profile NAME` — `light`, `standard` or `assurance`; an unknown name is
  refused (`unknown_profile`). `--profile-rationale TEXT` stores why.
- `--preview-profile NAME` — read-only; prints the policy as one JSON object
  (rc 0), or `{"error": "unknown_profile", ...}` with rc 2. It cannot be
  combined with a session-mutating flag (`conflicting_arguments`).
- Refusals (rc 2, nothing written, session files byte-identical):
  `profile_requires_session` (`--no-session`), `profile_team_conflict`
  (`--team` with `--profile`, or `--team` on a profiled session),
  `profile_not_bound` (attaching a profile to an existing unprofiled session),
  `profile_demotion_refused` (a lower `--profile` on resume) and
  `execution_profile_unreadable` (a damaged or mismatched profile record; there
  is no reader that treats damage as "no profile").
- **Promotion is deterministic and one-way.** Scope expansion past the declared
  batch, an executable or generator change, conflicting sources, failed
  evidence (a lint/format-only failure excepted), a blocking or major finding, an
  architectural risk tag, a repeated review-round cap, an explicit higher
  `--profile` on resume and any malformed signal each promote to the target
  fixed in the preview's `promotion.triggers` table, with that table's reason
  code. Nothing ever demotes; the history is part of the record.
- **Deferred minor notes.** Under `light`/`standard` a build-reviewer `approve`
  may carry `deferred_minor_notes` outside `corrective_findings`. An approve
  that carries a corrective finding (any profile), deferred notes under
  `assurance`, or malformed notes stops the phase unapproved with kind
  `review_profile_rejected` and `profile_rejected` set to
  `corrective_findings_on_approve`, `deferred_notes_refused` or
  `deferred_notes_malformed`. A `revise` always reopens the builder.
- The run result gains an additive `execution_profile` object (`selected`,
  `effective`, `promotion_count`, `deferred_minor_note_count`) for profiled
  sessions only. `--report` shows an Execution profile section for them.
- A profile exposes a frozen serial concurrency contract
  (`mode: serial`, `max_parallel_vertices: 1`) and a per-vertex policy accessor
  for a later scheduler; nothing here schedules parallel work.

### Stops and orchestrator decisions

A phase that needs an answer or an authorization stops unapproved with rc 4
and a `stop` request. The request is durably recorded as the session's one open
decision; its `stop.request_id` binds the response, and exactly one decision
flag is accepted per invocation.

| `stop.kind` | raised when | consumed by |
| --- | --- | --- |
| `needs_input` | the lead recorded a question in `result.pending_question` | `--answer` |
| `reviewer_question` | the reviewer emitted `needs_user` | `--answer` |
| `review_round_cap` | the reviewer still requests changes after the round cap | `--answer` |
| `review_not_approved` | the only blocking findings were superseded verification challenges, so the reviewer never approved | `--answer` |
| `handoff_requested` | the lead signalled `handoff_back` with a payload | `--authorize-handoff` or `--decline-handoff` |

```bash
cowork --session-file PATH --answer REQUEST_ID --context-file ./answer.md
cowork --session-file PATH --authorize-handoff REQUEST_ID
cowork --session-file PATH --decline-handoff REQUEST_ID [--context-file ./why.md]
```

- `--answer` requires `--context`/`--context-file`
  (`answer_requires_context`). The answer is stored in the session and
  delivered to the target role **by path**; the role continues and the paired
  reviewer still has to approve. An answer never approves anything by itself.
- `--authorize-handoff` executes the recorded hand-back as is and takes no
  context. `--decline-handoff` returns the lead to its own phase, with any
  supplied context delivered alongside.
- A response that names a different, already-consumed, or mismatched request
  is refused (`decision_no_open_request`, `decision_request_mismatch`,
  `decision_response_kind_mismatch`). Consumption happens once, under a lock.
- A non-approving end that needs no authority is a failure, not a stop: rc 1
  with `stop.requires` of `reviewer` or `operator` and no `decision_argv` (for
  example `reviewer_unavailable`, `reviewer_absent`, `review_not_approved`
  without an approving verdict, `controller_failure`, `stale_noop`,
  `verification_not_current`). Recovery is a machine re-invocation: a plain
  resume, or `--switch-controller`. When the failure is a Claude
  authentication failure (`controller_outcome: authentication_failed`), the
  stop carries `recovery_route` and `upstream_artifacts_reusable: true`:
  re-authenticate Claude Code, then a plain `--session-file` resume. The
  resume pays one uncached live probe; if the provider accepts it, the role
  continues exactly once even when the recovery budget for that cause is
  spent, reusing the approved upstream artifacts. A still-rejected probe ends
  at the probe seam with the same route and no role turn.
- After `reviewer_unavailable` (the planning-advisor or build-reviewer could not
  return a usable verdict), the next plain resume or `--switch-controller` of
  that reviewer dispatches the reviewer first, against the exact candidate it
  was judging, and never re-sends the completed lead. A recovery binding that
  no longer holds ends rc 1 with `stop.kind` `recovery_binding_mismatch` before
  any send. See [Controller switching](#controller-switching).

### Controller updates

- `--switch-controller ROLE=CONTROLLER` — move one current-phase role of a
  saved session to `claude`, `codex`, or `opencode`, then continue.
  **Repeatable**; all switches in one invocation are one all-or-nothing update,
  and a switch resets that role's model/effort pins. See
  [Controller switching](#controller-switching).
- `--allow-controllers LIST|all` — set or lift the session's allowed
  controllers. See [Controller policy](#controller-policy).
- Both reuse the saved team/config and cannot be combined with `--team` or
  `--config`.

### Provider capacity pauses (rc 5)

When a controller turn fails with a genuine `quota_limited`/`overloaded`
classification, cowork does not retry the same provider. It persists the
pending turn, mints a PauseLease bound to the role, provider session,
controller policy and candidate, and ends with `outcome: awaiting_capacity`.
`stop` carries the lease facts (`lease_id`, `automation_ref`, `package_id`,
role and provider identity).

A paused turn is replayed by the separate, versioned `resume-trigger` entry
point (its own JSON result line and exit-code table), normally fired by a wake
adapter such as `scripts/cowork_wake_macos.py`:

```bash
cowork resume-trigger --session-uuid UUID --lease-id LEASE_ID \
       --claimant-ref CLAIMANT_REF --automation-ref AUTOMATION_REF \
       --cwd LAUNCH_DIR
```

All four identities are required. `--cwd` names the directory whose `.cowork/`
holds the session anchor (it defaults to the current directory); that is the
launch directory, even for a `--worktree` run. The trigger claims the due
lease, re-checks that its binding still matches the session, and sends the
persisted turn; it does not drive the rest of the phase. A `success` report is
not by itself permission to resume: a plain `cowork --session-file PATH`
continues the session **only after its lease is released** (consumed,
cancelled, or past its retry horizon), and is refused while that lease is still
live — see the bullet below.

- **Delivery is at least once, not exactly once.** Orchestrator decisions
  carried by the paused turn are acknowledged after the provider accepts it; a
  failed acknowledgment is reported (`decision_ack_failed`) and a later plain
  run re-sends the decision block. No phase or epoch transition is applied
  twice.
- **A live lease owns its paused turn (runtime enforcement, not supervisor
  policy).** While a PauseLease is live — `unclaimed` or `claimed` and within
  its retry horizon — a plain `cowork --session-file PATH` is refused by cowork
  itself with reason `role_held_by_capacity_pause` and exit code 5 (`outcome:
  awaiting_capacity`), before anything is written, drained or dispatched. This
  covers an ordinary pause that carries no orchestrator decision as well as a
  decision-bound one. The refusal is **run-level**: one held role refuses the
  whole run, including a run whose `--team` excludes that role. The stop names
  the held `role`, its `lease_id`, `lease_state`, `claimant_ref` /
  `automation_ref` and `horizon_release_at`, and a claimed lease's message
  prints the full `resume-trigger` invocation including `--cwd`. Only
  `resume-trigger` replays that turn; the hold ends when the lease is consumed,
  cancelled, or past its retry horizon.
- **Crashed claimant.** If a claimant stopped after claiming, a plain run is
  refused with `decision_held_by_capacity_pause`, naming the lease, its
  `claimant_ref`/`automation_ref`, and its retry-horizon release time.
  Retrigger with those same four identities, verbatim, with `--cwd LAUNCH_DIR`
  and without `--redirected-context`. That retrigger may replace the claimed
  lease with a new lease for the same binding (for example when the send fails again with a
  capacity outcome, reported as `send_failed` with `re_entered_capacity:
  true`), so the old `lease_id` is no longer the one to trigger. Run a plain
  `cowork --session-file PATH` afterwards; while a decision is still held, its
  refusal names the lease that currently holds it. Once the lease is consumed or past its retry
  horizon, a plain resume delivers the decision.

### Worktrees

- `--worktree [NAME]` / `--wt [NAME]` — before scouting, a small worktree role
  creates a git worktree following the repo's documented convention (and any
  documented setup, such as a per-worktree venv); a repo with no convention
  gets a sibling `../<repo>-worktrees/<name>` folder. `NAME` defaults to
  `cowork-<short session id>`; the branch has the same name off HEAD. It
  requires launching inside a git work tree (`worktree_requires_git`, rc 2) —
  the flag-specific form of the general
  [git work tree prerequisite](#requirements), which `--worktree` pre-empts
  with its own code so the diagnosis names the flag.
  cowork validates the created worktree before switching into it. On a name
  clash, an explicit `NAME` stops (or reuses an exact match); an auto name
  picks a free numbered variant.
- `--wt-controller claude|codex|opencode` — controller for the worktree role
  (default `claude`).
- With `--worktree`, the session store stays in the **launch** directory, not
  the worktree. Resume from the launch directory or with its `--session-file`,
  and pass `--cwd LAUNCH_DIR` to `resume-trigger`.

### Governed parallel graph

`cowork graph OP` is an agent-only command for running several independent work
packages as the vertices of one governed graph. OP is one of `admit`, `status`,
`claim`, `publish`, `cancel`, `fail`, `reclaim` or `join`. It is dispatched
before the flat argument parser and has its own result line (below). Cowork
records and fences the graph; it never spawns children, creates worktrees or
merges anything. The orchestrator pre-creates one git worktree per vertex,
launches each child itself and integrates the results itself. Execution
profiles expose the serial concurrency contract this command consumes (see
[Execution profiles](#execution-profiles)); the production cap of 1 still
applies, so nothing here runs vertices in parallel.

**Revision document.** `admit` reads one JSON file:

```json
{"schema_version": 1, "max_parallel": 1, "claim_ttl_s": 900,
 "vertices": [{"work_id": "<uuid>", "root": "/abs/worktree",
               "base_commit": "<40-hex>", "authority_path": "/abs/authority",
               "authority_digest": "<sha256 of the authority file>",
               "profile": "standard", "predecessors": ["<work_id>"]}],
 "joins": [{"join_id": "<uuid>", "rule": "all_succeeded",
            "requires": ["<work_id>"]}]}
```

`max_parallel` is at least 1; `claim_ttl_s` is 60 to 86400 and defaults to 900;
`joins` is optional. No other keys are accepted (`revision_malformed`,
`join_malformed`). Ids are lowercase UUIDs.

**`cowork graph admit --revision-file PATH (--new | --graph-id ID)`** admits a
revision into a new or an existing graph and returns `graph_id`,
`graph_revision` and `effective_cap`. Admission fails closed and writes nothing
on any refusal. It checks the predecessor structure (`cycle`, `self_edge`,
`dangling_predecessor`, `duplicate_work_id`), then for every non-terminal
vertex its root (an existing, symlink-free git worktree top level whose HEAD
equals `base_commit`, with `.cowork/` ignored, and not the main checkout, the
sessions root, another vertex's root, or a root in use by another active
graph), and its authority file (present, bytes matching `authority_digest`, a
distinct digest per vertex). The effective cap is `max_parallel`; it may not
exceed the policy cap, which is read only through
`resolved_vertex_policy(profile, 'builder')` (`ceiling_above_policy`). A later
revision may add vertices, but every vertex that is no longer pending must be
repeated with an identical normalized declaration, no `work_id` may change its
declaration, and a join that already has a decision may not change
(`revision_conflict`).

**`cowork graph status --graph-id ID`** is read-only and takes no lock. Fields:
`graph_id`, `revision`, `cancelled`, `effective_cap`, `held_slots`,
`ready_order` and `statuses`, which maps every current vertex to `waiting`,
`ready`, `blocked`, `claimed`, `running`, `succeeded`, `failed` or `cancelled`;
`joins` holds the stored decisions.

**`cowork graph claim --graph-id ID [--work-id ID]`** atomically takes one slot
for a ready vertex (without `--work-id`, the first of `ready_order`) and bumps
its lease epoch. The claim result line carries these fields flat, with no nested
object: `graph_id`, `work_id`, `lease_epoch`, `root`, `profile`,
`authority_digest`, `claim_deadline`, `cwd` (the vertex root) and `launch_argv`.
Refusals include `cap_reached`,
`none_ready`, `vertex_not_ready`, `vertex_blocked`, `vertex_held`,
`vertex_terminal` and `graph_cancelled`.

**Launch.** The orchestrator runs `cowork` with `launch_argv` (`--new --profile
PROFILE --graph-vertex GRAPH_ID:WORK_ID:EPOCH`) plus its own `--context-file`,
from the vertex root, and supervises it like any other run. `--graph-vertex` is
set only from `launch_argv`: it needs a matching `--profile` and a new session,
and cannot be combined with `--no-session`, `--worktree`, `--team` or
`--output-root`. The bind is the first statement after the child owns its
session lease; it records the session in a write-once index and moves the
vertex to `running`. A graph-bound run result carries the additive
`graph_vertex` object (`graph_id`, `work_id`, `lease_epoch`). Every later entry
of that session re-checks the fence and is refused with `vertex_lease_superseded`
or `vertex_cancel_requested` once it no longer holds the vertex.

Launch refusals are reported on the ordinary run-result line (`cowork_result`),
not on a `cowork_graph_result` line, with the closed graph code in `reason`.
The `--graph-vertex` argument refusals (`graph_vertex_malformed`,
`graph_vertex_requires_profile`, `graph_vertex_requires_new_session`,
`graph_vertex_flag_conflict`) are rc 2. A store bind refusal keeps its own rc: rc
3, read there as `outcome: owner_conflict`, for `vertex_lease_superseded`,
`session_already_bound` and `vertex_held`, rc 1 for a store lock, corrupt or
inconsistent state, or I/O failure (`lock_timeout`, `graph_state_corrupt`,
`graph_state_inconsistent`, `io_error`), rc 2 otherwise. The graph argument
conflicts are checked after the earlier `profile_requires_session` and
`profile_team_conflict` refusals, so those codes win for `--no-session` and
`--team`. The argument refusals and the read-only bind pre-check write
nothing. A bind refused later, in the owned region (a race after the pre-check,
or `session_already_bound`), comes after the new session record and owner
lease exist; it still dispatches nothing.

**`cowork graph publish --graph-id ID --work-id ID [--take-over]`** accepts the
vertex's result. It takes no session argument: the child is the session bound
to the vertex. It acquires that session's owner lease under the entry point
`graph_publish`; a live, unproven or corrupt owner is refused as
`owner_conflict`, and `--take-over` replaces only an owner proved dead. The
evidence is the child's own accepted, green owned-verification transaction
whose result manifest equals its request manifest and the candidate manifest
now on disk in the vertex root, with the transaction's requesting session and
repository matching the vertex, and every required check of the vertex profile
satisfied. The child's process exit is never evidence. Success returns
`receipt_identity`, `session_uuid`, `slot_released` and `publish_outcome`
(`published`). Publishing the same receipt again is idempotent: rc 0, `outcome:
ok`, `publish_outcome: already_published`, state unchanged.

**`cowork graph cancel --graph-id ID [--work-id ID]`** returns `outcomes`, a
list of `{work_id, outcome, pause_cleanup}` where `outcome` is `cancelled`,
`cancel_requested` or `already_cancelled`. A pending or claimed vertex is
cancelled at once. A running vertex is cancelled only when its holder is
provably not live (`stale_dead_owner` or `unowned`); otherwise a durable cancel
request is recorded, its slot stays held and the child's next fence entry is
refused, so repeat the cancel until `cancelled`. Without `--work-id` the whole
graph is cancelled (no further claims) and every vertex that has not succeeded
or failed is handled the same way. For a cancelled vertex with a session, live
capacity PauseLeases are cancelled and listed in `pause_cleanup`.

**`cowork graph fail --graph-id ID --work-id ID --reason-code TOKEN`** records a
claimed or running vertex as failed (`TOKEN` matches `[a-z][a-z0-9_]{0,63}`)
and returns `state: failed` and `slot_released`. A failure is always explicit,
never inferred from an exit status. A running vertex needs a holder proved not
live and not paused (`vertex_live`, `holder_unproven`, `vertex_paused`).

**`cowork graph reclaim --graph-id ID --work-id ID`** releases an abandoned
vertex: a claimed one only after its `claim_deadline` (`claim_not_expired`), a
running one only when the holder is proved dead or unowned and has no live
PauseLease. It returns `new_lease_epoch`, `holder_verdict` (`null` for a claimed
vertex) and `slot_released`. The vertex returns to pending (or cancelled, when a
cancel was requested) under a bumped epoch, so the old session can never
rebind.

**Slots.** A slot is held from claim until exactly one of: accepted
publication, `fail`, a confirmed cancel, or an audited reclaim. A process exit
or a capacity pause never releases it.

**`cowork graph join --graph-id ID --join-id ID`** returns `decision`, the
deterministic `JoinDecision`: `outcome` (`joined` or `blocked`), the ordered
`members` and a `decision_digest`. It refuses `early_join` while any member is
waiting, ready, claimed, running or holding a slot, and is `blocked` when a
member failed, was cancelled or is blocked. `joined` requires each member's
receipt to still bind its vertex and the live candidate manifest to equal the
receipt's (`receipt_candidate_changed`). A decision is stored once per join and
revision, and repeating the join returns it unchanged. A join is a decision
record, not a merge: it reads and writes no vertex root, and merging candidates
is the orchestrator's own, separate act.

**Result line.** Every `cowork graph` invocation ends stdout with exactly one
JSON object, and the process exit status equals its `rc`:

```json
{"cowork_graph_result": 1, "rc": 0, "op": "claim", "outcome": "ok",
 "reason": null, "graph_id": "<uuid>", "work_id": "<uuid>"}
```

`outcome` is `ok`, `refused` (a closed `reason` code from the table below, plus
a string `detail` when it has one) or `error` (an unexpected failure, rc 1,
`reason: io_error`). The op's own fields are added to the object, and the keys
above always win over them. An argument error is rc 2 `argument_error`; `--help`
emits no line. Diagnostics go to stderr; branch on the result line only. Exit
codes are 0, 1, 2 and 3, never 5: rc 5 is provider capacity and is not used
here.

| rc | meaning | reason codes |
| --- | --- | --- |
| 1 | corrupt state, lock timeout or I/O failure | `graph_state_corrupt`, `graph_state_inconsistent`, `lock_timeout`, `io_error` |
| 2 | contract refusal; nothing changed | `argument_error`, `revision_malformed`, `cycle`, `self_edge`, `dangling_predecessor`, `duplicate_work_id`, `root_missing`, `root_symlink`, `root_not_worktree_toplevel`, `base_commit_mismatch`, `anchor_dir_not_ignored`, `candidate_collision`, `root_alias`, `root_nested`, `root_overlaps_main_checkout`, `root_overlaps_sessions_root`, `root_in_use`, `authority_missing`, `authority_malformed`, `authority_digest_mismatch`, `authority_shared`, `unknown_profile`, `ceiling_invalid`, `ceiling_above_policy`, `unsupported_concurrency_contract`, `join_malformed`, `join_unknown_member`, `revision_conflict`, `graph_cancelled`, `graph_unknown`, `vertex_unknown`, `vertex_not_ready`, `vertex_blocked`, `none_ready`, `cap_reached`, `vertex_terminal`, `vertex_not_claimed`, `vertex_not_running`, `bind_root_mismatch`, `bind_profile_mismatch`, `graph_vertex_malformed`, `graph_vertex_requires_profile`, `graph_vertex_requires_new_session`, `graph_vertex_flag_conflict`, `receipt_malformed`, `receipt_cross_vertex`, `receipt_stale_epoch`, `receipt_stale_revision`, `receipt_no_accepted_transaction`, `receipt_wrong_candidate`, `receipt_candidate_collision`, `receipt_missing_required_check`, `receipt_candidate_changed`, `join_unknown`, `early_join` |
| 3 | ownership or lease conflict | `vertex_held`, `session_already_bound`, `vertex_lease_superseded`, `vertex_cancel_requested`, `vertex_live`, `vertex_paused`, `holder_unproven`, `claim_not_expired`, `receipt_non_owner`, `owner_conflict` |

**Concurrency.** The production cap of 1 holds for every shipped profile, and
an admitted `max_parallel` may not exceed it. Raising it needs a separately
authorized policy revision; nothing in this command claims parallel
production.

**Interplay.** Policy is read only through `resolved_vertex_policy` and never
re-derived. Vertex ownership and liveness come only from the single-writer
owner lease. A vertex paused on provider capacity keeps its slot, `reclaim` and
`fail` refuse it while its PauseLease is live, `cancel` cancels the lease, and
rc 5 is never used by the graph. Each vertex's receipt references the child's
own owned-verification transaction and changes none of it. Static plan-step
dependency validation is separate from this runtime graph admission.

State lives under `<sessions_root>/graphs/` (`COWORK_SESSIONS_ROOT` overrides
the sessions root): a registry, one `graph.json` per graph and a write-once
session index.

### Read-only and side-channel commands

- `cowork --check` — preflight dependency check only.
- `cowork --report [SESSION_UUID] [--json] [--rebuild]` — see
  [Measurement](#measurement).
- `cowork --session-owner [SESSION_UUID] [--json]` — read-only owner-lease
  view (who owns it, heartbeat freshness, recovery command); acquires nothing
  and always exits 0.
- `cowork --preview-profile NAME` — read-only; one JSON object with the
  profile's complete effective policy (see [Execution profiles](#execution-profiles)).
  Reads no session and dispatches nothing.
- `cowork --evaluate-role ROLE --eval-session SESSION_UUID ...` — record one
  orchestrator-owned evaluation to `orchestrator-evaluations.json`, separate
  from peer `scores.json` and never read by a phase gate (`--help` lists the
  score flags). No run-result record; exits 0 recorded, 1 could not record,
  2 invalid arguments.

`--report` and `--session-owner` without a UUID read this directory's most
recent session; they never run a phase. `--output-root` cannot be combined
with `--check`, `--report`, `--session-owner` or `--evaluate-role`
(`conflicting_arguments`): none of them dispatches a builder, so a
declaration there would be silently dropped rather than bound.

Defaults per role (model/effort default to the controller CLI's own setting):

| Role | Controller | Model | Effort | yolo | Mode |
| --- | --- | --- | --- | --- | --- |
| scout | claude | default | default | on | implement |
| scout-reviewer | claude | default | default | on | implement |
| planner | claude | default | default | on | implement |
| planning-advisor | claude | default | default | on | implement |
| builder | claude | default | default | on | implement |
| build-reviewer | claude | default | default | on | implement |

Roles default to **implement** mode (write-enabled). The lead roles are
kept in their lane by **role-spec guardrails**, not by plan mode — the scout may
write only its two intel files (JSON + markdown), the planner only its two plan
files; the builder edits the repository freely to execute the plan but makes no
git commit (and also emits a markdown build summary). The reviewers each write
only their own review file (see below). This is instruction-level confinement,
not an OS sandbox.

All three phases — scouting, planning, building — run in this release. A
run that starts in scouting without `scout` is refused (`scout_not_selected`):
every session begins with scouting (a session already past scouting resumes
into its saved phase without re-running earlier roles).

## Sessions

`cowork` persists each session in a project-local
**`.cowork/session.<uuid>.json`** in the directory it runs from (add `.cowork/`
to your `.gitignore`; a legacy single `.cowork/session.json` is still
discovered). It stores:

- a **cowork session UUID** (`session_uuid`) — minted once per session, distinct
  from any claude/codex session id. It names this session's assets, all of which
  live under `~/.cowork/sessions/<session_uuid>/`: the scout intel files
  `scout.intel.json` / `.md`, the review file
  `scout-review.json`, the planner's plan files
  `planner.plan.json` / `.md`, the planning-advisor's review file
  `planner-review.json`, the builder's status file
  `builder.status.json` and build summary `builder.summary.md`, the
  build-reviewer's review file
  `builder-review.json`, the aggregate peer-eval `scores.json`, the
  role-identity registry `identities.json` (which tool + model + provider
  session id each role actually ran with),
  and the private orchestration trace `trace.jsonl`;
- the **team** and **per-role config** — so a resumed run needs no `--team` or
  `--config`;
- the **current phase** (`scouting`/`planning`/`building`) — so a killed run
  resumes into the phase it was in (see
  [Phases and the hand-back](#phases-and-the-hand-back));
- each role's **CLI session id** (claude `session_id` / codex `thread_id`) —
  scout, scout-reviewer, planner, planning-advisor, builder, and build-reviewer
  — so a run that is killed can be **resumed where it left off**, with the
  reviewers keeping their accumulated review context too;
- the **current session context**, versioned (see below);
- the session's **declared external output roots** (`declared_output_roots`,
  a sorted list of canonical absolute directories declared with
  `--output-root`) — so a resume reproduces exactly the authorized roots and
  never a re-derived or widened set; and
- each paired reviewer's **last-approved hash-gate baseline** (the artifact
  composite it last approved, scoped by phase epoch + acknowledged context
  revision) — so the [reviewer skip on unchanged artifacts](#reviewer-skip-on-unchanged-artifacts-hash-gate)
  survives a resume; and
- for a session started with `--profile`, an immutable **`execution_profile`
  binding** (`selected`, `policy_version`, `policy_digest`) written in the same
  save as the team and config. The mutable record — effective profile, rationale,
  batch, accepted evidence, invalidation graph, deferred notes and promotion
  history — lives beside the other session assets as
  `execution_profile.json`, with the building-entry baseline in
  `execution_profile.baseline.json`. A resume re-reads and validates the record
  and stops with `execution_profile_unreadable` on any damage. A policy-version
  change makes an in-flight profiled session unreadable on purpose (fail
  closed); a future version adds an explicit migration.

A saved session is resumed only when it is selected explicitly
(`--session-file PATH` or `--resume`); a run with no selector starts a new
session. A resumed run reuses the saved config and resumes the saved CLI
sessions (`claude --resume <id>` / `codex exec resume <thread_id>`). The claude
session id is pinned up front (`--session-id <uuid>`) and saved immediately, so
even an instant kill is resumable.

A resume without context continues automatically — the current phase's role
picks up where it left off with its prior context. To **redirect** the resumed
session, pass `--context`/`--context-file`; to **start fresh**, start a new
session (no selector, or `--new`) or use `--no-session`. Changing the saved
team/config of an existing session is not supported beyond the controller
updates below.

### Controller switching

If the active controller for the current role is unavailable or stops making
progress, the orchestrator can switch that role to another controller and keep
the session moving. The cowork phase, artifacts, shared context, review baselines,
epochs, and working tree stay in place. The provider conversation itself starts
fresh because Claude and Codex do not expose a shared hidden-chat migration path;
cowork seeds the new controller with an explicit handoff packet.

That packet is **file-only**. Only content-free routing facts travel inline — the
phase, the role, the from/to controllers, and a normalized reason/source code
when one exists. Everything with a body is carried **by file path**, for the
switched role to read from disk itself: the shared session context, the relevant
session artifacts, any free-form recovery/diagnostic text, and the failed pending
turn when there was one. No artifact bodies, context text, or turn contents are
pasted into the packet.

cowork offers no in-process switch prompt. A missing executable, a
start/resume/probe failure, a lead turn with no status progress, a stale lead,
or an unusable reviewer ends the run with a structured failure; the
orchestrator then re-invokes with the switch:

```bash
cowork --session-file .cowork/session.<uuid>.json --switch-controller planner=codex
```

A failed paired reviewer recovers first. When the planning-advisor or
build-reviewer stops the phase with `reviewer_unavailable`, cowork records the
failed request, the candidate's sha256 (and, for the builder, its summary's),
the verdict-file identity and the lead's session and work identities in the
reviewer's own pending entry. A plain resume, or a `--switch-controller` of the
reviewer, then dispatches that reviewer first through the same path-based
handoff packet and sends nothing to the completed lead; the lead's artifacts,
session id, review rounds, limits, capacity leases, context revisions and
controller policy are held. The builder's owned verification transaction still
runs before the build-reviewer gate (it is not a lead send). If the reviewer
approves, the phase completes as usual; if it genuinely asks for changes, the
lead is reopened through the ordinary revise handoff, with any unseen context
update delivered ahead of it. A reviewer entry written by an older run (a switch
marker or bare pending turn, no recorded failure) takes the same route only
while the lead's candidate is `ready_for_review` and has no usable verdict on
disk.

The lead is reopened first only for a linked reason: a durable
`InvalidationRecord` naming the cowork session, the failed lead attempt's work
id and the lead's dispatch-manifest digest (appended after the failure), or a
trusted orchestrator decision still owed to the lead. A context-revision bump or
a lead controller switch is not one. Without such a reason, a candidate whose
bytes changed (`candidate_changed`), a lead that is no longer `ready_for_review`
(`lead_not_ready`) or whose saved session no longer matches
(`lead_session_mismatch`) stops with `recovery_binding_mismatch` (rc 1,
`requires: operator`, no send); the stop carries the session uuid, the failed
lead work id and the manifest digest. The supervisor's exit is to append that
`InvalidationRecord` with `state_store.append_invalidation_record`, after which
the next run reopens the lead. A record naming the wrong role or phase, a
reviewer that is not on the team, or a malformed record (`wrong_first_role`,
`phase_mismatch`, `reviewer_not_on_team`, `malformed_record`) has no such exit
and needs the session anchor repaired out of band. The capacity resume-trigger
is unchanged.

Before committing the switch, cowork checks the target controller executable and
uses the existing install guidance if it is missing. When the target is Claude,
cowork also runs the stream-json probe for that role's prompt/mode/permission
settings. A failed target check leaves the current controller unchanged.

Switching is explicit only. There is no automatic rate-limit failover
(capacity signals pause instead; see
[Provider capacity pauses](#provider-capacity-pauses-rc-5)), no migration of
hidden Claude/Codex conversation history, and no guarantee that switching back
later resumes the exact old provider session id; the saved role entry records one
active controller/id pair at a time.

### Controller policy

A session can declare **which controllers it is allowed to use at all**. The
allowed set is saved with the session. A session with **no** policy is
**unrestricted** and behaves exactly as every session saved before this feature
existed — nothing changes until you set one.

```bash
# restrict this session, and move both current-phase roles in the same command
cowork --session-file PATH --allow-controllers claude,codex \
       --switch-controller builder=codex \
       --switch-controller build-reviewer=claude

# lift the restriction again
cowork --session-file PATH --allow-controllers all
```

`--switch-controller` is **repeatable**, and `--allow-controllers all` removes
the restriction entirely (the session file goes back to its pre-feature shape).

**A switch without `--allow-controllers` never changes the allowed set.** It is
still checked *against* it — moving a role to a controller the session forbids is
rejected — but the saved allowed list is left byte-for-byte as it was. The mirror
also holds: a policy change never reassigns a role on its own.

**Ordering and the all-or-nothing guarantee.** cowork validates the whole
proposal first — the allowed set, every role move, and whether the current phase
would still be compliant — then checks that the target controllers are installed
and working (only ever checking controllers that will be permitted once the
command finishes), then persists the policy and every role move as one
**single write**. The whole update completes **before anything resumes**: no role is
dispatched until that one write has landed. If any part of the proposal is wrong
the run reports one clear message, a non-zero exit, and a session file that is
untouched — the update is **all-or-nothing**.

**Current phase vs. the rest.** Roles in the **current phase** must comply in the
same command — a role left on a now-forbidden controller is a hard error naming the
matching `--switch-controller` to add. Roles from finished phases are
only reported as a **warning**, left exactly as they are, and blocked if they are
ever reached.

From then on every attempt to start a controller consults the policy first, for
leads, paired reviewers, resumes, recovery relaunches and the `--worktree` agent
alike. A blocked attempt never starts the process — not even a probe or an
opencode agent-file write — and ends the run with a structured failure naming
which role wanted which controller and what the session allows.

**If a saved policy is unreadable** — a bad hand-edit, a half-written file —
cowork stops before starting anything rather than treating a damaged restriction
as no restriction. It names the session file and the two repairs: re-run with
`--allow-controllers` (which replaces the policy outright and continues), or
remove the `controller_policy` field from the session file by hand. Read-only
commands (`--check`, `--report`) keep working throughout.

Every policy change, rejected update, unreadable policy and blocked dispatch is
recorded in the [orchestration trace](#orchestration-trace) as
`controller.policy.change`, `controller.policy.rejected`,
`controller.policy.invalid` and `controller.dispatch.blocked`.

### Orchestration trace

Each persisted session run appends private structured events to
`~/.cowork/sessions/<session_uuid>/trace.jsonl` (`--no-session` stays ephemeral
and does not write a trace). This trace does **not** duplicate Claude or Codex transcripts;
those controller CLIs already keep their own local logs. Instead, cowork records
the missing orchestration layer: when a controller was invoked, whether it was
fresh or resumed, which non-content params were used, which artifact
status/verdict was read, which gate was shown, and why stale state was
invalidated.

Prompt-like content is recorded only as `*_sha256` + `*_bytes`; argv entries that
would contain prompt bodies are replaced with `<prompt>`. The trace is intended
for local debugging with the `cowork-debug` skill, not for terminal output or a
shareable transcript.

Evaluation drains record which policy governed them and what became of each
entry: `eval.drain.start` and `eval.drain.end` both carry `policy=`, and
`eval.drain.end` also carries the `terminal` and `retired` counts alongside the
existing ones. Every entry state change emits `eval.entry.lifecycle`
(`entry_id`, `from_state`, `to_state`, `attempt`, `limit`, `error_class`), so a
retry budget can be reconstructed from the trace alone.

### Evaluation traceability

Every peer-evaluation entry in `scores.json` (schema 2) is stamped with full
provenance so scores, outcomes, and token consumption can be analyzed per
**tool + model** combination:

- **who evaluated** — `evaluator_tool`, `evaluator_model`,
  `evaluator_session_id` (the live identity of the session that produced the
  scores, captured from the controller's own stream events — claude names its
  model on the system-init event; codex falls back to the pinned
  `model=<id>` when its events don't name one);
- **who was evaluated** — `evaluatee_tool`, `evaluatee_model`,
  `evaluatee_session_id`, looked up from the per-session `identities.json`
  registry, which the orchestrator refreshes on every turn;
- **what the evaluation cost** — the eval turn's controller-reported `usage`
  (input/output/cache token counts) and wall-clock `duration_ms`, plus
  `eval_turn_id` and `specs_in_turn`: a round-1 consumed-upstream bundle rides
  the same send, so entries sharing an `eval_turn_id` share one turn's usage
  (count it once, not per entry);
- **what outcome it accompanied** — `reviewed_verdict`
  (`approve`/`revise`/`needs_user`) on review-round entries, so score levels
  can be correlated with round outcomes.

`cowork --report [<session-uuid>]` renders the analysis: scores received per
evaluatee tool+model (per-criterion averages), evaluation cost per evaluator
tool+model (shared turns deduped), average score by verdict, and — from the
trace — total turns + token usage per role/tool/model. Entries written before
schema 2 still aggregate; they simply fold into `(unknown)` identity buckets.

## Measurement

Cowork can tell you how many tokens a session burned. The measurement layer
tells you what that money bought — and, where it genuinely cannot know, says so
instead of printing zero.

### One authoritative record; the report is its rendering

`measurement.json` in the session directory is the authority. Everything else
derives from it:

- `cowork --report` prints a rendering of the record.
- `cowork --report --json` prints the record itself.
- `builder.summary.md`'s completion section is a labelled derived view of
  `record.completion[]` — there is deliberately no second hand-written account
  to drift from it.

**Building and printing are separate jobs.** The record is built from the raw
sources (trace, scores, identities, ledger, controller logs) at known moments —
every phase transition, session end, and session start — and is authoritative
once written. The printer only ever loads and prints it; it performs no
arithmetic, and passing it a file path raises rather than being loaded.

A **third, separate step** hashes the raw sources and warns you above the report
when they have moved on since the record was built. It produces no number and
never feeds the printer, which is what lets "the report computes nothing" stay
literally true while a stale record still warns you. A stale record still renders
the RECORD's values under that banner — reporting stale-but-authoritative numbers
with a warning is honest; silently recomputing them is not.

`cowork --report --rebuild` refreshes the record on demand. A report never
rebuilds implicitly.

For a session started with `--profile`, the record also carries an additive
`execution_profile` key (and its source fingerprint in `built_from`): the
selected and effective profile, the policy version, the promotion history with
reason codes, the deferred minor note count, the batch artifact count and the
executed versus reused verification entries; each owned transaction summary
adds `executed_entry_count`/`reused_entry_count` when a reuse policy was in
force. The key is absent for every other session, so legacy records and reports
are unchanged, and `--report` renders an Execution profile section from it for
equivalent-cohort comparison.

### Where the money went

Cost splits into **exclusive classes** — productive, review, evaluation,
verification, recovery, probe, in-flight, failed, cancelled — that reconcile
against the turns' own reported usage, with any leftover named explicitly rather
than hidden inside a total.

Every controller turn, probe and evaluation carries a stable `work_id` joining
its start to its end, plus its class, duration and a canonical identity. So an
in-flight, failed or cancelled turn is recorded as what it is instead of
vanishing, and a turn still running reports its duration as `unknown` rather than
as 0.

**Evaluation attempts stay in their own class**, away from productive phase
work, and a **failed** attempt is booked to the `failed` class — it is not an
evaluation success. What it is no longer is free: a failed evaluation attempt
used to report a duration of exactly 0, which under-reported what scoring
actually cost now that failed attempts are counted, bounded work. It reports the
time it really took. Each queue entry also keeps its **final disposition** —
held, terminal, retired or completed — along with how many attempts it used, so
the report can show what was scored, what was held and what stopped trying.

**A resumed Codex turn reports what that turn cost**, not the thread's running
total. Codex's counters are cumulative, so a resumed turn re-reported every
earlier turn's tokens; cowork differences them into the turn's own share and
keeps the provider's raw counters untouched alongside as `usage_native`. If the
cumulative reading ever moves backwards there is no honest per-turn figure, and
the turn is marked `incomparable` rather than clamped to something plausible.

### Owned verification transaction

At a builder's ready-for-review gate, Cowork itself — not the agent, and not
inside the agent's own controller turn — runs the plan's approved verification
inventory as **one owned, hermetic, manifest-bound transaction**:

- **Immutable snapshot, one fresh checkout per command.** Before anything
  runs, Cowork copies the candidate's tracked-plus-untracked-non-ignored
  source bytes (with executable mode and symlink targets preserved) and the
  raw Git index into a content-addressed object store, re-enumerating and
  re-hashing before and after the copy so a concurrent edit during capture is
  caught rather than silently copied half-and-half — objects are keyed by
  hash and never overwritten in place, so the store itself is effectively
  append-only. Every `execution_mode: isolated_snapshot` command then gets
  its OWN fresh, disposable checkout materialized from that store — never a
  checkout shared with any other command — with functional local Git
  semantics of its own (the captured index written directly into it, so
  `git ls-files`/`git rev-parse` work without ever touching the live
  candidate), used for exactly one command, and removed immediately after
  that command's terminal event is recorded, so one command's output can
  never leak into the next. The one `execution_mode: candidate_read_only`
  command (the CLI preflight) is the sole exception permitted to touch the
  live candidate, and even then Cowork — never the plan or the command —
  sets its working directory. A separate, per-transaction bootstrap checkout
  is where the worker process itself is spawned from — excluded from the
  normal command-input path (static argv validation rejects any literal
  reference to it before launch), though this is isolation, not an access-
  control guarantee: it doesn't stop a command's own inline logic from
  discovering or constructing that path at runtime.
- **Current-source worker, no restart.** A small worker process is spawned
  from *inside the snapshot*, so it always runs the candidate's current code
  even when the long-running parent process started from an older version. The
  worker reports its own source hash and protocol version before running
  anything; a mismatch makes the transaction `unverified` rather than trusted.
  This is also what keeps a 19-command inventory from costing 19 conversational
  turns: the whole inventory runs as one orchestration work item outside the
  agent's context entirely.
- **Owned process lifecycle.** Every command runs one at a time, stdin wired to
  `/dev/null`, in its own process group. A hung or over-time command gets
  `SIGTERM`, a bounded grace period, then `SIGKILL`, and Cowork verifies no
  descendant survives before moving on. A worker that hangs or crashes is
  bounded by an overall deadline and torn down the same way, including its
  active command's process group — no orphaned process, and no orphaned
  poller, in any of these paths.
- **Fail-closed mutation detection.** Before and after every command, Cowork
  re-diffs the live candidate's source manifest and Git index against the
  values captured in the snapshot. Any movement stops the transaction
  immediately, reports exactly which paths changed, certifies nothing, and
  leaves the live tree untouched — there is no automatic rollback, because
  overwriting a mutation could destroy the evidence or someone else's
  concurrent work.
- **Single-flight, not duplicated.** Concurrent or repeated requests for the
  same snapshot digest, Git-index digest, configuration, and approved
  inventory (execution mode included) share one transaction; a bounded waiter
  reuses only a *terminal* result for that exact key, and a dead lock owner is
  reclaimed rather than blocking the next attempt forever.
- **One final suite, bound to the reviewed candidate.** A schema-2 inventory
  names exactly one `kind: final_suite` entry, always last; readiness requires
  every approved command green, evidence present, the final suite run exactly
  once (`final_suite_binding: ran_once`), and the transaction's own captured
  manifest/index still matching what was actually reviewed.
- **Each command has a fixed 300-second outer bound.** The worker terminates a
  command that exceeds this bound even when that command supplies a larger
  tool-level timeout such as `--timeout 3600`; the inner timeout does not
  enlarge Cowork's process deadline. A schema-2 `final_suite` must therefore
  be a genuinely complete regression command that can finish inside 300
  seconds. Do not label one shard as the final suite merely to satisfy the
  schema.
- **A complete suite longer than one command is composed (schema 3).** A plan
  that declares `verification_schema: 3` and a `verification_suite` universe
  (test-id root, include/exclude selectors with reasons, and optional modules
  split by test class) expresses the complete suite as contiguous, last
  `kind: final_suite_component` entries. Every component is still one
  300-second command. Before anything runs, Cowork proves from the immutable
  snapshot that the components **exactly partition** the declared universe —
  a missing, duplicated, overlapping or foreign member, a selector matching
  nothing, a split module whose classes cannot be enumerated statically, a
  component bound above the per-command timeout, or a transaction whose
  overall deadline exceeds Cowork's ceiling is rejected with no worker
  spawned, no attempt minted and no snapshot left behind. Cowork appends each
  component's proven test ids to its runner prefix itself, so a component
  cannot run something narrower than it claims. A component that ran zero
  tests, printed no `Ran N tests` summary, disagreed with its
  `expected_test_count`, or overflowed the worker's output cap is red even on
  exit 0. The suite certifies only as `final_suite_binding:
  components_ran_once` with every component green, all inside the same
  single transaction. Cowork does **not** prove that the declared universe is
  the repo's complete regression suite, or that `tests_dir` is the runner's
  test-id root: the planning-advisor and build-reviewer judge both from the
  receipt. See `roles/planner.md` for both inventory schemas.
- **Bounded evidence, never a silent rerun.** If a command's terminal result is
  slow to land, Cowork polls the same pre-minted attempt for a bounded number
  of attempts; past that bound the attempt is recorded `unresolved`/`absent`
  and polling stops — delayed evidence is never resolved by launching a
  replacement command.
- **Deferred evidence is reconciled, not re-run.** When a final-suite command
  outlives the bounded poll while still alive, the transaction is recorded
  deferred and the builder is told to set `ready_for_review` again with the
  tree unchanged. That next promotion for the same candidate reconciles the
  *same* transaction from its own evidence under the single-flight lock:
  still running stays deferred, finished becomes a terminal pass or fail, and
  gone with no evidence is `absent` — never a pass. A dead supervisor's
  abandoned deferred transaction is reconciled fail-closed before anything
  new launches. `--report` and `--check` never reconcile.

Under an [execution profile](#execution-profiles) a schema-2 entry may also
declare two optional fields: `depends_on` (repo-relative paths, globs or
trailing-slash prefixes its result depends on) and `check_class` (`lint` or
`format`, for a deterministic check whose failure alone never promotes). Under
`light`/`standard` the transaction reuses an entry per entry instead of
rerunning it, but only when every dependency pattern still matches a path, the
dependency digest equals the one it ran against, no executable file changed
since its source transaction and no direct derivative of a changed artifact is
involved; an entry without `depends_on` always reruns, and `assurance` never
reuses. A reused entry is not sent to the worker: it is listed in the receipt's
`evidence_reuse` with its source transaction id and dependency digest, and
`executed ∪ reused` always equals the inventory. A reused final suite is bound
`reused_dependency_bound`, never `ran_once`, and the request key carries a reuse
suffix so a partly reused result is never single-flight-reused by a candidate
that reused nothing. A transaction whose every entry is reused runs no worker
and is green only if the live candidate still equals its snapshot.

Legacy (schema-1) plans — `{label, command}` only, no `execution_mode`/`kind`
— are still accepted: they run isolated, keep their historical
whole-inventory readiness comparison, and report their final-suite guarantee
as `legacy_unknown` rather than inventing one. See `roles/planner.md` for the
schema-2 and schema-3 inventory formats plans should write going forward.

#### The receipt downstream: overlays, dispositions, supersession, reuse

The transaction's terminal result is a first-class **receipt**
(`verification/transactions/<txn>/result.json`), and what decides things
downstream is the receipt — never the builder's prose about verification:

- **One derived overlay, both gates.** A verified promotion persists a
  current-receipt pointer (transaction id, candidate manifest+index binding,
  verdict, final-suite identity, command count, review round, disposition).
  From it Cowork renders ONE overlay of content-free facts on two surfaces:
  the build-reviewer's handoff (fresh and resumed edges, with the receipt
  file delivered by absolute path alongside the other artifacts) and the
  building-phase review notice in the run transcript (a compact block, with
  the agent's own verification prose labeled separately as self-reported). A
  structured **contradiction signal** — computed once, from owned state —
  marks the overlay on both surfaces when the builder's verification prose is
  missing or disagrees with the receipt. It is visible, never blocking.
  For a schema-3 composed suite the receipt also carries the partition proof
  (`suite`: the declared universe, excluded paths with reasons, split
  modules, per-component member ids and digests, member count and universe
  digest); the pointer carries the universe selectors and counts, and the
  overlay adds the universe digest plus member and component counts.
- **Four-state review disposition.** Every owned transaction carries
  `pending_review` → `accepted` / `superseded_by_finding` / `rejected`, bound
  to (transaction id, candidate manifest) and recorded as a
  `verification.disposition` trace event (the trace is authoritative; a small
  per-session sidecar is the reconciled read-through cache). `accepted`
  requires the build-reviewer's approving verdict with the
  accepted candidate manifest still equal to the transaction's captured
  manifest; a valid later blocking finding supersedes the green transaction;
  a red/unverified transaction or an abandoned candidate is `rejected`. The
  measurement record joins the disposition onto each transaction; the report
  lists every transaction with its disposition, prints **incurred**
  verification cost (all transactions) separately from **accepted**
  verification cost (accepted only), and completion value is granted only for
  an `accepted` transaction. Legacy sessions with no owned transactions keep
  the controller-log-derived completion path unchanged.
- **Mechanical supersession of defeated verification challenges.** A reviewer
  verification challenge against a candidate a green owned receipt certifies
  must cite the receipt (`corrective_findings[*].verification_challenge:
  {transaction_id, reason_code}`). A `revise` whose ONLY blocking findings
  are challenges that are uncited — or contradicted by the owned receipt —
  does NOT reopen the builder: the findings are recorded
  `closure=superseded` + `superseded_by_transaction` (never erased), the
  transaction survives as `pending_review`, and — because the reviewer still
  did not approve — the phase stops with a `review_not_approved` request for
  the orchestrator. Any non-verification blocking finding, or a
  validly-cited challenge, reopens exactly as before.
- **Reuse booked as avoided cost.** A correction that changes only
  agent-authored artifacts (candidate manifest and Git index unchanged) hits
  the existing single-flight reuse: no command re-runs, readiness stays bound
  to the ORIGINAL transaction id, and the reuse is recorded from the
  `verification.transaction` trace event's `reused_lock_result` flag as
  **avoided cost** attributed to the reused transaction — with no second
  incurred transaction.

#### Evidence lifetime and repository hygiene

Product tests committed to the repository protect behavior expected of every
future revision. They use neutral inputs and may cover security, compatibility,
architecture, integrity, negative controls and regressions.

Evidence about one delivery does not become a permanent product test. Package
receipts, audits, run results, candidate/base ancestry pins, one-delivery path
allowlists, scope snapshots, gate transcripts or counts, and assertions about
one historical implementation state belong in the session or package artifact
directory outside product source and outside Git. A mixed check keeps its
durable product assertion with neutral inputs and moves or drops the historical
delivery portion. Legitimate Git behavior tests with throwaway repositories,
controlled fixtures, security negatives, compatibility inputs, regression
references and product receipt fields remain valid; no keyword alone decides
the classification.

The durable recurrence check is
`python3 scripts/cowork_offline_tests.py test_evidence_lifetime_contract`.
It scans representative forbidden shapes and verifies that the role and
orchestration contracts carry this boundary; it is not a substitute for
reviewing the semantics of a new test.

#### Checkpoints: typed, candidate-bound, deterministically-executed

Beyond the whole-inventory owned transaction above, individual dispatch/
role-loop points can require a **checkpoint**: a typed `CheckpointRequest`
Cowork itself authors — never the plan, never the agent — and runs through a
deterministic, non-model executor, exactly once:

- **Typed and closed-schema.** `CheckpointRequest`/`CheckpointResult`/
  `CheckpointReceipt` are versioned, closed-key JSON documents (unknown keys
  rejected), never free-form prose relay. A request names an exact `argv` and
  an orchestrator-owned `cwd`, a `mutation_class` (`read_only` / `isolated` /
  `live_candidate`), and — for `live_candidate` only — a non-empty
  `declared_output_paths`.
- **Exclusive, once-only claim/lease.** A checkpoint is claimed by kernel-
  exclusive file creation (`O_CREAT|O_EXCL`): two racing claimants can never
  both win, and a checkpoint already claimed — by this or any other executor
  — is never re-run. The terminal receipt is published exactly once,
  receipt-first (durable before the claim is marked terminal), so a crash
  between the two never leaves a false terminal marker with no receipt
  behind it.
- **Fail-closed cross-checks, not the result's own say-so.** A submitted
  result is rejected — never silently accepted — for: a missing request, a
  malformed/unversioned document, a mismatched `executor_identity`, wrong
  `argv`/`cwd`, a reported output path outside the request's own declared
  set (over-broad), or any reported mutation for a `read_only`/`isolated`
  request (which must run genuinely unmutating). A `live_candidate`
  checkpoint's mutated paths are additionally authorized through the SAME,
  unmodified `cowork_action_policy` ownership/recoverability rule every other
  agent-side mutation is checked against — never a parallel rule of the
  checkpoint gateway's own invention.
- **Exact candidate binding, never stale/superseded.** A checkpoint's receipt
  binds one `candidate_digest`; advancing the real phase gate on it requires
  that digest to equal the candidate actually being advanced (the same,
  unmodified `gate_validated` evidence-matching the owned transaction's own
  candidate binding already uses) — a stale or superseded checkpoint (any
  checkpoint id other than the CURRENT one bound to that role's work) is
  structurally never consulted, so it can never advance the real control
  plane even if its own receipt were still `accepted`.
- **Reconstructed from artifacts alone.** A checkpoint's pending, claimed, or
  terminal state after a crash/resume is read back entirely from its own
  request/claim/result/receipt files on disk — no separate index that could
  itself drift out of sync.

### Evidence comes from the controllers' logs

For everything **outside** an owned verification transaction — tool use,
non-owned sessions, and any legacy session with no transaction artifact —
Cowork takes facts from Claude's and Codex's own session logs rather than from
agent prose. An agent that omits a failure cannot omit it from the log.

The reader is **strictly read-only** — every ingested file's content digest is
taken before and after the read and recorded, so the property is evidence rather
than a promise — and **fallible**: a log that is missing, unreadable, truncated
or in an unrecognised format yields `unknown`, never a guess and never a broken
run. A run with every controller log deleted mid-flight still completes.

What this buys you:

- A test run that executed **zero tests** fails its check even though it exited
  0. Exit status alone certifies nothing.
- A red run **stays red** after a later green one. The later run is a different
  attempt; it does not close the earlier one.
- A run that **timed out** is `unresolved` — terminal, and never closed by a
  later pass, so "re-run until it passes" cannot launder a hang into a pass.
- A claim an agent made with nothing in the log behind it is labelled
  `self_reported`; a claim the log contradicts keeps **both** sides.

Extraction is content-free: commands are reduced to a sanitized identity (the
program and its option-shaped arguments), outputs to counters and flags.

### The ledgers

Findings, decisions, human amendments, escaped defects and verification attempts
all get their IDs from **one writer**, `cowork_ledger`. No agent-supplied id is
ever accepted, and the record builder never writes — so printing a report ten
times leaves `ledger.jsonl` byte-identical.

The ledger is **append-only**. A later record may add or supersede; it may never
rewrite or delete. A withdrawn finding survives as withdrawn, because retracting
a false finding is good work and erasing it would make it indistinguishable from
never having looked.

Legacy verification attempts arrive by **reconciliation**: ingestion emits
id-free observations keyed on `(controller_session_id, tool_call_id)`, and
reconciliation mints an id for each key it has not seen. Replaying the same
log appends nothing the second time. Owned-transaction attempts are minted
directly — one stable id allocated *before* the command launches, revised in
place as terminal evidence arrives — and never collide with or get
reconstructed by legacy reconciliation, so the same command is never counted
twice under two identities.

### Scoring stays out of the way

Evaluation runs in **isolated sessions** that have never touched the work and can
only read the files they were given, on the same controller and model as the seat
they occupy (collapsing them onto one controller would break comparability with
sessions already recorded).

It is also **deferred**. The moment a reviewer's verdict is written and
validated, cowork seals an evidence envelope, drops it in a durable queue, and
hands the fix straight back — the round never waits for scoring. The queue drains
at three boundaries: **session start** (for anything a crash left pending),
**phase end**, and **session end**. Before a score counts, the seal is
re-checked: evidence that changed while the entry sat in the queue is marked
`unverifiable` rather than re-hashed to whatever the file says now.

Sealing **after** the verdict exists is also the structural fix for evidence
binding: a digest can no longer be taken before the evidence it describes.

`--evaluation-policy` takes `all_rounds` (the default), `final_round`, `sampled`
or `off`, and the overhead of the choice is reported as its own cost class, so
the choice can be made from data.

**The policy governs when the queue is drained, not just when work is added to
it.** The policy in force *now* is what applies, so switching a session to `off`
takes effect on work queued before the switch: with `off`, an ordinary run or
resume starts **zero** evaluator turns at every one of those three boundaries.
Queued work is neither deleted nor called successful — it is **held**, durably
and visibly, with the reason recorded on disk, and turning evaluation back on
releases it. Holding is idempotent, so a session resumed ten times under `off`
accumulates one hold, not ten.

**A drain never waits for anyone.** It runs with no prompt; the trace's
`eval.drain.*` events carry the governing policy and the pending/running,
completed, held/skipped and terminal/failed counts. No drain outcome records a
success for work that did not succeed, and superseded work is reported inside
held/skipped, never as completed — retiring a superseded candidate scores
nothing.

**Failures are classified and retries are bounded.** Every attempt is recorded
*before* it runs, so a crash costs at most one attempt instead of looping
forever. A transient failure gets a second attempt; missing or unparseable
evaluator output, a permanent failure and an unusable entry each stop after the
first — a retry cannot change any of those. Once the budget is spent the entry is
**terminal**, and it stays terminal across a resume with its attempt count,
failure class and history intact. Only an explicit **retry** record reopens it,
linking back to the earlier attempts rather than overwriting them; no `cowork`
command currently issues one.

Queue files written before any of this loads unchanged: entries with no lifecycle
data read as pending with a fresh budget, and are never rewritten in place.

### Missing data reads as missing

These are real values, not absences, and none of them is ever coerced to 0 or
ranked:

| value | means |
| --- | --- |
| `unknown` | no source for this figure |
| `incomparable` | the provider's counters cannot yield an honest per-turn figure |
| `not_applicable` | the criterion cannot apply here (round-1 responsiveness has no prior feedback) |
| `insufficient_evidence` | the evaluator could not judge from what it was given |
| `self_reported` | an agent claimed it; the log does not show it |
| `unverifiable` | the evidence changed, or a cited record never existed |
| `unpriced` | no price for this model in the pricing snapshot |

`not_applicable` and `insufficient_evidence` are first-class scores. A criterion
that does not parse is recorded as `insufficient_evidence` rather than dropped —
dropping it shrank the denominator, so the criteria an evaluator *could* judge
looked like the whole picture.

An evaluation queue entry ends up in exactly one of these states, and none of
them is quietly reported as done:

| state | means |
| --- | --- |
| `pending` | waiting to be scored |
| `attempting` | an attempt was recorded but its outcome was not — an interrupted run |
| `held` | held by policy (`off`); visible, durable, deliberately unscored |
| `drained` | scored successfully — the only state that counts as completed |
| `retired` | superseded by a later round; never scored, and **not** a failure |
| `terminal` | its retry budget is spent; needs an explicit retry to reopen |
| `retried` | a terminal entry was explicitly reopened; its earlier history is preserved |

`unverifiable` above is unchanged by any of this and is still **not** a failure:
such an entry drains successfully, costs no retry budget, and is simply excluded
from the aggregates — it is reported beside the completed count rather than
folded silently into it.

**Pricing ships as a schema with an empty snapshot.** Real prices baked into a
repository are stale by construction, so by default every model resolves to
`unpriced` and nothing claims to be money. Every cost field carries the schema
version and snapshot id that produced it.

### Time

Productive, review, evaluation, verification and recovery time are shown
separately. User-wait time is taken only from recorded `user.wait` spans, never
inferred from gaps between events; the agent-only runtime emits no such spans,
so a new session reports user-wait as `unknown`. A gap is equally an ingestion
stall, a controller hang or a suspended process; it is not evidence of anyone
waiting.

### Old sessions

Sessions recorded before this layer existed still report. They say plainly which
records they predate: turn ids are synthesized (and labelled as such), user-wait
is `unknown` rather than inferred, and every gap is listed in `record.incomplete[]`
with its reason.

### Context revisions

Explicit context (`--context`/`--context-file`) is a **session-wide event**, not
a one-off prompt to the scout. It is persisted as the current session context with
a monotonically increasing **revision** (`{text, hash, revision, source}`), and
every role records the last revision it acknowledged
(`last_context_revision_seen`). The invariant:

> Any role invoked after context is provided must receive the current context,
> unless it has already acknowledged that revision.

Fresh role sessions get it in their prompt naturally. **Resumed** sessions that
have not acknowledged the current revision are woken with an explicit
context-update block — "New orchestrator context was provided for this
resumed cowork session" — so redirecting a resumed session keeps continuity without any role
quietly operating on stale assumptions. A role acknowledges a revision only after
it actually ran against it; a crash before that re-delivers the block on the next
resume.

## Jev observational pilot (off by default)

An optional observer measures whether Jev gives useful signals on real builder
reviews. It has **no authority**: it never changes the reviewer's packet or
context, approval, delivery, the run result or any prompt, and a failing,
hung or suspended observer leaves the ordinary outcome identical. Reviewer
verdicts, Jev answers and adjudication results never reach the implementer, the
ordinary reviewer or the adjudicator brief.

**Activation.** Set `COWORK_JEV_PILOT_DIR` to a coordinator-written pilot
directory that lives **outside** any agent workspace and outside Git. Unset,
missing or invalid activation leaves every hook inert. The directory holds:

- `cohort_activation.json` (`cohort_activation.v1`): model, question set and
  digest, thresholds and salt exactly as in the accepted protocol, `caps.jev`
  `{candidates_per_cohort, max_units_queried, proposed_cap_usd}`,
  `caps.adjudication` `{cohort_caps: {tokens, tool_calls, wall_minutes},
  units_cap, per_unit_envelope: {tokens: 400000, tool_calls: 30, minutes: 20}}`
  and `authorizations`: `budget_ref`, `credential_source_ref` (the **name** of
  the environment variable that holds the credential; the value is never
  recorded), `data_scope.repositories`, `recipients` (must include `TypeSafe`
  and the adjudicator provider), `vendor_retention_status` and `docs_recheck`
  `{checked_at, price_usd_per_million_input: "0.042", limits_recorded: true}`.
  `adjudicator` carries `model_id`, `provider` and the brief version, and
  `inclusion_list_digest` is the sha256 of the canonical inclusion list. The
  observer only checks that these records are present and consistent (it does
  not verify human intent); any absent or mismatched record, or an inclusion
  list edited after activation, leaves it inert before any credential is read.
- `inclusion_list.json`: entries `{session_id, ticket_ref, objective_text,
  requirement_text, repository, repo_root}` (optional `base_ref`). A session
  without an entry, whose `repository` is not in `data_scope.repositories` or
  whose `repo_root` is not the working repository is excluded as
  `not_in_inclusion_list`; an entry without objective text is excluded as
  `no_objective_captured`. Nothing is guessed.
- `alias_log.jsonl` (optional, written before metrics are computed).

Everything else (captures, acceptance registry, Jev records, observer registry,
reports) is written by the observer under the same directory. No real call is
made without an authorized activation, a credential and a budget.

**Automatic observation mode (opt-in, observation-only).** A separate JSON
configuration may be placed at `~/.cowork/jev-auto-config.json`, or its path
may be supplied through `COWORK_JEV_AUTO_CONFIG`. Cowork reads JSON directly;
it never sources shell configuration. Example schema:

```json
{
  "schema": "jev_auto_observation.v1",
  "enabled": true,
  "mode": "observation_only",
  "effective_at": "2026-10-02T00:00:00Z",
  "repository_identity": "/absolute/path/to/repository/.git",
  "pilot_dir": "/absolute/path/outside/git/jev-pilot",
  "shared_budget_usd": 5,
  "credential_env": "JEV_API_KEY"
}
```

`repository_identity` must be the canonical Git common-dir identity; linked
worktrees share it. Each newly created matching session stores its binding and
gets a per-session status receipt under `<pilot_dir>/automatic/sessions/`.
Resumes do not enroll old sessions. At the existing pre-review candidate
boundary, the observer captures only against the clean Git `HEAD` recorded at
session enrollment; an unborn or dirty enrollment tree has no trusted baseline
and is explicitly not queried. Requirement sentences are derived from the
persisted objective; candidates without requirements are explicitly not
queried. Ordinary candidate content is not rejected or rewritten based on
home-directory paths, email addresses, identifier entropy or symbol names;
these inputs reach Jev unchanged. Explicit credential patterns in text are
still scrubbed, and dedicated credential containers (such as `.env*`, private
key formats and `credentials*` files) remain excluded. The candidate snapshot
and a durable queued-work record are written
before detached observation starts, and ordinary resume recovers queued work.
Started attempts are recovered as unknown charges and never resent. A disabled,
missing, invalid or changed config uniformly blocks paid attempts for both
launch-root and linked-worktree sessions while preserving the binding for a
later re-enable. Config and pilot paths are checked against both the active
worktree and primary repository root after resolving symlinks. The observer
then uses a single append-only Jev reservation ledger shared by every session
and linked worktree. A cross-process lock serializes reservations against the USD5 cap;
unknown charges stay reserved, exhausted/suspended pilots stop paid requests,
and ordinary review and approval are unaffected. `JEV_API_KEY` (or the
configured environment-variable name) is read only when a query is eligible;
its value is never saved. The session anchor stores the enrollment and initial
no-candidate status; the external per-session receipt carries updated
missing-key, suspension, cap, usage, latency, error and signal states.
Accuracy/precision/recall remain pending because independent adjudication is
not authorized. The frozen-cohort mode above is unchanged.

**Suspension and recovery.** Create `SUSPENDED` in the pilot directory (or call
`cowork_jev_observer.suspend(pilot)`) to suspend: no new capture or query starts,
ordinary review is unaffected and the cohort clock keeps running. Remove it (or
call `resume(pilot)`) to resume; `resume` and `recover` never re-send a query.
Suspension and a recorded hard stop (`record_hard_stop`: data-scope violation,
credential exposure, budget revocation) are checked before every new paid
attempt, reservation and adjudication start, including queued work; requests
already started settle without retries. `resume` never clears a hard stop.
An attempt that started without an outcome becomes `service_failure`
(`interrupted_unknown`) with its charge unknown (null, reservation kept); units
that were never sent are final as `not_queried_interrupted`. An adjudication
that started without an outcome is recorded as `unknown` and is never restarted
(`recover_adjudications`, coordinator-invoked).

**Reports.** `cowork_jev_report.compute_report` renders the `jev_obs` metrics
M1-M10 and the decision cascade; `close_cohort` writes the immutable closed
report and `write_supplement` writes dated supplements
(`cohort_<n>_supplement_<date>`) for results that arrive later. Adjudication is
run by the coordinator through `select_adjudication`, `build_brief`,
`begin_adjudication` and `submit_adjudication`; the observer starts no agent.

## The scout role

`scout` doesn't gather blindly — it grounds itself, settles scope, and makes the
goal measurable before anything is planned:

1. **Recon** — reads/searches the repo to ground itself.
2. **Clarify** — resolves ordinary ambiguity itself, choosing the most
   reasonable interpretation and recording it in `result.assumptions`. Only a
   decision that needs authority it does not have becomes a `needs_input`
   request (one exact question in `result.pending_question`), which stops the
   run for an orchestrator `--answer`.
3. **Propose options** — when there are tradeoffs, it lays out concrete options
   *with a recommendation* instead of just asking open questions.
4. **Make the goal measurable** — turns the goal into explicit
   **success criteria** (1–5, each with a concrete measurement, an expected
   result, and a must/should tier — the measurement fitting what's being
   built: a bugfix by its reproduction, a feature by observable behavior, a
   perf goal by a metric vs a baseline, a refactor by invariants + the suite).
   The criteria freeze at approval.
5. **Hand off** — writes its intel and marks it ready for review.

Its **only write targets** are its two intel files,
`~/.cowork/sessions/<session_uuid>/scout.intel.json` (machine source of truth +
status channel) and `scout.intel.md` (the readable rendering, like the
planner's `plan.md`); it must not touch any other file (reading/searching the
whole repo is encouraged). Full spec: [roles/scout.md](roles/scout.md).

### Intel files

The JSON object has a fixed top level; `result` is the scout's free-form
deliverable:

```json
{ "session": "<uuid>", "role": "scout",
  "status": "needs_input | ready_for_review",
  "result": { "objective": "…",
              "success_criteria": [{"statement":"…","measurement":"…",
                                    "expected":"…","tier":"must|should"}],
              "clarifications": [{"q":"…","a":"…"}],
              "relevant_code": "…", "open_unknowns": "…",
              "recommended_starting_point": "…", "plan?": "…" } }
```

`result.success_criteria` is required: it is the measurable definition of
"done" the scout-reviewer approves (rendered as a dedicated **Success criteria** section
in `scout.intel.md`) and the contract the plan must cover. Intel that reaches
review without a non-empty list gets an orchestrator **structural auto-finding**
in the reviewer's brief (structure only — quality judgment stays with the
reviewer).

cowork reads only `status`. Questions and orchestrator answers are recorded in
`result.clarifications`. If no `planner` role is on the team, the scout also
includes a lightweight plan in `result`. Alongside the JSON, `scout.intel.md` is
a readable rendering of the same intel — the scout-reviewer reviews both and
checks the markdown stays consistent with the JSON.

## The scout-reviewer role

With `scout-reviewer` on the team, every time the scout marks its intel
`ready_for_review`, cowork **deterministically** runs the reviewer —
orchestrator control flow, not a model deciding when to review. The reviewer
starts from the **same context the scout was given**
(the shared context + the team framing + the scout's current intel; never the
scout's own write-target brief) and critically checks objective alignment,
**goal measurability** (each success criterion binary-decidable from its
stated measurement, the measurement fitting what's being built, the `must`
set covering the goal), whether blocking product questions were buried
as assumptions, whether cited discoveries hold up, and completeness — it is
instructed to find gaps, not to rubber-stamp.

It writes a verdict to its own file, `~/.cowork/sessions/<session_uuid>/scout-review.json`
(its **only** write target, cleared before each pass so a stale verdict is never
read back):

- **`approve`** — the only way the phase is approved; the run chains into the
  next phase or ends with rc 0.
- **`revise`** — the findings are handed back to the scout as its next turn; the
  scout fixes the intel and re-proposes. Bounded by the review round cap
  (`REVIEW_ROUND_CAP`, currently 5 consecutive reviewer passes; the count
  resets on an approval, on a cap stop, or when the lead reports
  `needs_input`, not on each re-submission); past it, the phase stops
  unapproved with a `review_round_cap` request carrying the reviewer's
  findings.
- **`needs_user`** — the reviewer found a decision that needs authority beyond
  the review; the phase stops with a `reviewer_question` request for the
  orchestrator.

A missing or malformed verdict never approves: the reviewer is retried once,
then the run ends with a `reviewer_unavailable` failure. Full spec:
[roles/scout-reviewer.md](roles/scout-reviewer.md).

The reviewer is a **persistent session** like the scout: its CLI session id is
saved and resumed on every pass and across cowork resumes, and it participates in
[context revisions](#context-revisions) — a resumed reviewer that hasn't seen the
latest `--context` gets it as an explicit update block on its next pass.

Every stop and its response is described in
[Stops and orchestrator decisions](#stops-and-orchestrator-decisions).

### Reviewer skip on unchanged artifacts (hash-gate)

cowork **skips** the paired reviewer when the artifact set it would review is
**byte-for-byte identical to what that reviewer last approved** in the current
phase. It is never a silent bypass: the transcript shows a `review skipped —
unchanged since last approved` marker and the prior approval is reused. The
"unchanged" check is a composite over **every** file the
reviewer sees (scout = `scout.intel.json` + `scout.intel.md`; planner =
`planner.plan.json` + `planner.plan.md`), so any edit — including a markdown-only
one — forces a full review again. Only a real prior **approve** ever seeds a skip
(a `revise`, a round-cap stop, a `needs_user`, or a reviewer failure never
does), and the baseline is tied to the phase and to the context revision the
reviewer actually acknowledged — a phase re-entry (e.g. a planner→scout hand-back)
or any newer context clears it. The hash-gate covers the **scout and planner
only**; the builder is out (its summary is a deliverable, not a skip baseline).

## The planner role

When the scout's intel is approved and `planner` is on the team, cowork chains
straight into the planning phase **in the same run**: the planner is seeded with
the approved intel JSON plus the current shared context. Like the scout, it
resolves ordinary ambiguity itself and raises only decisions that need outside
authority (scope, behavior, tradeoffs) as `needs_input` stops, and marks the
plan ready when it is decision-complete.

The planner produces **two artifacts** (its only write targets):

- `~/.cowork/sessions/<session_uuid>/planner.plan.json` — the **machine deliverable** and
  source of truth for downstream roles, carrying the dense engineering detail:
  goal-coverage mapping, a **criteria-coverage mapping**
  (`result.criteria_coverage` — every intel success criterion mapped to named
  steps and to the `result.verification` entry that measures it, or explicitly
  marked unverifiable-in-build with a reason), decisions with rationale,
  file/symbol-cited evidence, per-file change lists, and the test inventory. Its top level mirrors the
  scout intel (`{session, role, status, handoff?, result}`) and doubles as the
  planner's status channel
  (`needs_input | ready_for_review | handoff_back`).
- `~/.cowork/sessions/<session_uuid>/planner.plan.md` — the **readable plan**:
  TL;DR; What we're building; Key decisions; How it will work; What changes;
  How we'll know it works; Out of scope; Risks & assumptions. Sections stay
  small and scannable; the dense detail stays in the JSON.

When the planning-advisor requests changes, the planner keeps revising; on the
advisor's approval — with a `builder` on the team — the session **chains into
the building phase**. Without a builder, plan approval ends the run with the
plan as the deliverable. The plan JSON may also carry a
`result.verification` inventory the build phase runs (see
[Owned verification transaction](#owned-verification-transaction)).
Full spec: [roles/planner.md](roles/planner.md).

## The planning-advisor role

The planning-advisor pairs with the planner exactly as the scout-reviewer pairs
with the scout: each time the planner marks the plan `ready_for_review`, cowork
deterministically runs the advisor against **both** plan artifacts. Its checks
include **criteria coverage**: every intel
success criterion mapped to steps and to a verification that measures what the
criterion actually states — uncovered, mis-measured, weakened, or dropped
criteria are findings. Same verdict semantics — only `approve` approves,
`revise` findings go back to the planner (bounded by the round cap, then a
`review_round_cap` stop), `needs_user` stops with a `reviewer_question`
request, and a missing/malformed verdict never approves. Its only write target is
`~/.cowork/sessions/<session_uuid>/planner-review.json`, cleared before each pass. Full
spec: [roles/planning-advisor.md](roles/planning-advisor.md).

## The builder role

When the plan is approved and a `builder` is on the team, the session chains
into the **building phase**. The builder is seeded with the approved plan (JSON
+ markdown) plus the current shared context. Unlike the scout and planner, its
write target is the **whole
repository** — it executes the plan by editing source files. Its
`~/.cowork/sessions/<session_uuid>/builder.status.json` is only a status + verification
channel (`needs_input | ready_for_review | handoff_back`, plus a
`result.verification` log), not a write restriction.

The builder keeps a **high bar for stopping**: routine progress and test
failures it can fix itself never become requests — it raises `needs_input` only
when truly blocked or when a big deviation from the plan surfaces. Before
marking the build ready it runs a self-audit: re-read the plan,
walk every per-file change, run each plan-listed verification command, and record
the results. At that self-audit it also emits a readable build summary,
`~/.cowork/sessions/<session_uuid>/builder.summary.md` — what changed per file,
the verification results, and any issues/deviations (the status JSON stays the
machine source of truth). The
build-reviewer reads the summary and **consistency-checks it against the real
working-tree delta**, so it can't mask the build. The
builder itself stays **out** of the reviewer hash-gate: the summary is a
deliverable, not a skip baseline. Verification is **strict** — it does not declare
the build ready while a verification command is failing for a reason it
introduced. A failure it cannot fix in the working tree (a missing dependency,
broken local tooling) becomes a `needs_input` request, not something silently
passed to the reviewer. The builder runs **no git commit and opens no PR**:
approval ends the run with the changes in the working tree. Full spec:
[roles/builder.md](roles/builder.md).

## The build-reviewer role

The build-reviewer pairs with the builder exactly as the other reviewers pair
with their roles: each time the builder marks the build `ready_for_review`,
cowork deterministically runs it. Its unit of review
is the builder's **full working-tree delta** — it captures the delta itself
(`git status --porcelain` for staged/unstaged/untracked, `git diff HEAD` for
tracked changes, and it reads new untracked files directly, since plain
`git diff` misses staged and untracked files) and checks it against the approved
plan, the builder's status, and the shared context. cowork records the build's
baseline commit at building entry and **flags a worktree that was already
dirty** (so pre-existing changes are not silently attributed to the builder).
Same verdict semantics — only `approve` approves,
`revise` findings go back to the builder (bounded by the round cap, then a
`review_round_cap` stop), `needs_user` stops with a `reviewer_question`
request, and a missing/malformed verdict never approves. Its only write target is
`~/.cowork/sessions/<session_uuid>/builder-review.json`, cleared before each pass; it never
edits code — fixes go through the builder. Full spec:
[roles/build-reviewer.md](roles/build-reviewer.md).

## Phases and the hand-back

The session phase (`scouting`/`planning`/`building`) is persisted in the
session store, and the flow is a **loop**:

```text
scouting ─(reviewer approves the intel; planner on team)─▶ planning ─(advisor approves the plan; builder on team)─▶ building ─(build-reviewer approves)─▶ done (run ends)
   ▲                                                          │  ▲                                                    │
   └──────(orchestrator authorizes the planner's hand-back)───┘  └──────(orchestrator authorizes the builder's hand-back)┘
```

Mid-planning, the planner can **hand the work back to the scout**, and
mid-building, the builder can **hand the work back to the planner** — say a
foundation in the plan turns out wrong. The role writes a handoff note (what
changed, what to re-do, what to keep) and signals `handoff_back`; the phase
stops with a `handoff_requested` request (rc 4). With `--authorize-handoff`,
the **pre-processor's session resumes**, woken with the handoff note, runs its
full cycle again, and after its reviewer re-approves, the downstream role
resumes (woken with the updated artifact to digest) and continues. With
`--decline-handoff`, the lead is told and continues its own phase. A
`handoff_back` without a note degrades to a `needs_input` stop — never an
implicit hand-back.

Under the **light** [execution profile](#execution-profiles) the loop is shorter:
on intel approval a clean documentation batch chains straight from `scouting`
to `building` with the approved intel as the plan (the builder is seeded through
the `scout->builder:seed` edge), and a missing batch or inventory, an executable
path, a source conflict or an architectural risk tag promotes the session and
goes through `planning` instead. Right before the first builder launch of a
building epoch Cowork snapshots a profile-owned baseline of the candidate with
the same enumeration the owned transaction uses; at each ready-for-review the
changed paths are measured against it (untracked files that were already
present, and Python bytecode, never promote). A promotion takes effect
immediately for the next transaction and verdict and adds the stricter profile's
roles to the team for any later authorized hand-back, but never inserts a
planning phase mid-build. A builder hand-back on a light session targets the
planner, which is not on its team until it is promoted: pass `--profile
standard` on the same invocation as `--authorize-handoff` to promote first (the
fresh planner is seeded from the intel and the hand-back note).

The signal contract is role-generic (any role → its pre-processor); planner →
scout and builder → planner are wired. A resumed session re-enters the persisted
phase: a session mid-building re-enters the builder conversation directly,
without re-running the scout or planner. If the resumed phase's lead role is not
on the team, the resume cascades down (building → planning → scouting) to the
nearest phase whose role is present.

### Role statuses

Each lead turn streams to the transcript on stderr; cowork then reads the
status artifact:

- **`needs_input`** — the role recorded an authority question in
  `result.pending_question`; the phase stops with a `needs_input` request. If a
  role writes `needs_input` without a question, cowork gives it one automatic
  repair turn; if the question is still missing, the stop carries no `question`
  and the transcript says so.
- **`ready_for_review`** — the paired reviewer runs (the transcript shows a
  `reviewed: …` marker), and its verdict decides what happens next (see the
  reviewer sections above).
- **`handoff_back`** — see above.
- A turn that ends in neither state (no status written, or an in-progress one)
  ends the run as a `role_turn_incomplete` failure.

#### Durable milestones

The role's own status JSON can sit unchanged through a long turn, so cowork
keeps a coarse progress record of its own. It never asks the role for it and
never changes the status schema. Each lead send is one round. Within a round,
cowork appends milestones in this fixed order, skipping any that do not apply:

- **`started`** — the send begins.
- **`discovery_complete`** (scout, planner) — the status file changed and
  parses as a JSON object.
- **`implementation_started`** (builder) — the working tree changed since the
  round began, going by `git status` plus each listed path's size and mtime.
  An edit to a file that was already dirty still counts.
- **`self_audit_started`** (builder) — the build summary was first written.
- **`waiting_on_orchestration`** — the send succeeded and changed the status
  file, and the new status is `ready_for_review`, `needs_input` or
  `handoff_back`.

Milestones are checked only when a controller tool call ends and when the turn
ends. There is no timer, no extra model turn, and the in-turn activity tick
never writes one. Each round records a milestone at most once and never goes
backwards, so a send adds at most five records. They go to
`<session assets>/milestones/<role>.jsonl`. Each record carries the round, the
boundary that triggered it, the status file's sha256, and `recorded_at`.

Stop payloads (`stopped`, `ended`, `awaiting_capacity`, `process_terminated`)
carry `status_diagnostics`, and a resumed role emits a
`role.status_milestone.resume` trace event with the same fields:

- `milestone`, `round` and `milestone_recorded_at`
- `status_sha256`
- `status_age_s` — time since the newer of the last milestone and the status
  file's mtime
- `controller_output_age_s` — time since the last tool end or turn end, or the
  last productive or tool-work activity record (liveness ticks don't count)
- `status_liveness`, one of:
  - `fresh`
  - `stale_status_active_controller` — status older than 900s while the
    controller produced output in the last 300s
  - `inactive_controller` — no output for more than 300s
  - `unknown` — no output evidence

These fields are diagnostic only: watchdog verdicts do not use them. A session
from before this store existed reads as `milestone: null`.

## Repository layout

```text
.
|-- cowork                      # executable entry point (re-execs into .venv when present)
|-- roles
|   |-- scout.md                # scout role spec (preloaded into the controller)
|   |-- scout-reviewer.md       # scout-reviewer role spec (critical review + verdict schema)
|   |-- planner.md              # planner role spec (dual plan artifacts + hand-back contract)
|   |-- planning-advisor.md     # planning-advisor role spec (plan critique + verdict schema)
|   |-- builder.md              # builder role spec (executes the plan + verification policy + hand-back)
|   |-- build-reviewer.md       # build-reviewer role spec (working-tree diff critique + verdict schema)
|   `-- evaluator.md            # isolated evaluator role spec (scores from a sealed evidence envelope only)
|-- pricing
|   `-- snapshot.json           # pricing snapshot (ships EMPTY: everything resolves to `unpriced`)
`-- scripts
    |-- cowork.py               # argument parser + run-result contract + phase loop + role orchestration + resume-trigger
    |-- cowork_bridge.py        # flag assembly, stream-json framing, codex resume, probe
    |-- cowork_profiles.py      # private controller state + reference-only authentication reuse
    |-- cowork_execution_profiles.py # execution-profile policy (light/standard/assurance); distinct from cowork_profiles.py, which owns controller authentication
    |-- cowork_action_policy.py # controller capability matrix + content-free action decisions
    |-- cowork_transcript.py    # plain-text transcript writer (stderr during a run; never reads input)
    |-- cowork_preflight.py     # Python-version + controller PATH checks
    |-- cowork_trace.py         # private JSONL orchestration trace writer
    |-- cowork_state.py         # .cowork/session.<uuid>.json store (config, phase, session ids, context revisions, decisions)
    |-- cowork_measure.py       # builds the AUTHORITATIVE measurement record; the only reader of raw sources
    |-- cowork_report.py        # PURE renderer of that record (computes nothing; refuses a raw source)
    |-- cowork_ingest.py        # read-only, fallible ingestion of the controllers' own session logs
    |-- cowork_ledger.py        # the sole writer of ledger.jsonl and sole minter of stable IDs
    |-- cowork_eval.py          # evaluation policy, sealed envelopes, the durable queue, isolation
    |-- cowork_pricing.py       # versioned normalization + pricing schema and snapshot loader
    |-- cowork_capacity_scheduler.py # PauseLease claim/replace/consume for provider capacity pauses
    |-- cowork_wake_macos.py    # launchd wake adapter that fires the capacity resume-trigger
    |-- fixtures/measurement/   # the five criterion fixture sessions + fake controller logs
    `-- test_cowork.py          # unit + live integration tests
```

## Nested-agent governance and accounting

Cowork decides controller-native child attempts before they start and gives
every refused attempt a stable work id. The currently installed transports do
not expose enough documented correlation to allow a child: Claude's Agent
PreToolUse event has a tool-use id, but SubagentStart has only `agent_id` and
`agent_type`, so parallel children cannot be joined deterministically. Every
Agent/Task dispatch is therefore refused and durably recorded. Historical
child telemetry remains readable; it is not evidence that current delegation
is enabled.

The capability matrix fails closed. Claude and Codex can run normal governed
roles on macOS because both expose catch-all local-tool hooks and run inside
the generated kernel boundary. Both run non-delegating. Claude explicitly
disallows `Agent` and legacy `Task`; Codex starts with `multi_agent` disabled
through two independent config pins. The broker independently denies an
`Agent`/`Task` attempt if either removal is bypassed. A nested hook carrying an
unmatched `agent_id` is recorded as
`child_agent_correlation_unavailable`. OpenCode has no child-correlation hook,
so Cowork instead hard-removes its native Task tool in the generated role agent
before process launch, using both the current `permission.task: deny` control
and the compatible `tools.task: false` control. OpenCode documents that a
denied task target is removed from the model's tool description.

**Linux limitation:** the authenticated private-profile and kernel-boundary
path is currently implemented only for macOS Claude and Codex roles. OpenCode
can run non-delegating through its controller-native permissions, but it does
not provide the same per-action broker receipts or operating-system write
boundary. Reports preserve that capability difference rather than treating the
controllers as equivalent. The presence of a bubblewrap profile generator does
not constitute Linux support for the private-profile path.

Every Claude/Codex local tool call reaches an orchestrator-owned broker. Built-in
mutation adapters must resolve all targets; Bash is proof-based and rejects
unresolved globs, substitutions, inline interpreters, invoked scripts, unknown
verbs, and incomplete redirects. Unknown local, plugin, and MCP tools are
denied. Allowlisted reads require a tested installed-schema digest, and schema
drift denies. Durable decisions contain hashes and reason codes rather than raw
commands, delegated prompts, or absolute paths. Child requests retain only
controller/model/effort identity, a digest, and byte length.

Bash commands may be composed only with `&&`, `;` and `|`. The command is
split at unquoted operators and every stage must independently prove
read-only; a compound containing any write, delete, unknown or unproven stage
is denied as a whole, even when the write target is owned. Operator characters
inside quotes or after a backslash, every other operator (`||`, `&`, `|&`,
newlines), every redirect (including `2>&1`, `>|` and any `<`), and
expansions (including `~` and zsh `=word`) stay denied. Beyond the inert
verbs, the proof path accepts the stdout-only helpers `cat`, `sort`, `uniq`,
`echo` and `which` with explicit flag tables (for example `sort -o` and
`uniq`'s second operand are denied). They are not part of the OpenCode
read-only allowlist. File operands of `cat`, `sort`, `uniq`, `head`, `tail`
and `wc` are read targets, so protected controller state stays unreadable
through them. A `shell_unprovable` denial names the offending stage in the
hook reason, for example `shell_unprovable (stage 2 of 3, unknown_command:
"foo") guard_attempt_id=…`, with the fragment JSON-escaped and truncated to
160 characters. The attempt id is always the last token. The action record
stores only an `unprovable` object: stage index and count, the operator and
construct as closed enums, the verb and flag name when they are short safe
tokens, and the fragment's sha256 and byte length. It never stores the raw
text.

Writable scope is exactly the selected worktree, the acting role's declared
outputs, the session's declared external output roots (`--output-root`,
builder only; parents, siblings and aliases of a root stay denied), and the
role's private temp/controller-state directories. A delete is allowed only
for a role-temp target, or for a tracked regular file inside the selected
worktree whose path as typed is not a symlink and resolves to that file,
whose index blob, HEAD blob and current on-disk hash agree, and whose
worktree and index are clean for that path — facts the broker derives from
Git on every attempt (`ls-files`, `rev-parse`, `ls-tree`, `diff`,
`diff --cached`, `hash-object`); hook payload lists are ignored, and
`git rm`, `mv`, `dd`, `find -delete`, symlink and directory deletes stay
denied. Every allowed mutation's action record carries `authorities` (the
authorizing root's `kind` and `root_digest`), and an allowed delete carries
`recoverability` (`git_head_blob` with the commit and blob ids that make it
recoverable). A generated operating-system sandbox
independently enforces those same roots. Registered sibling worktrees are
discovered before every Claude launch and explicitly denied in both the action
policy and kernel profile when they sit at or below a writable root. When the
selected worktree itself lives below the main checkout's `.worktrees/`
directory, that registered parent remains read-only under the default deny
without shadowing the more-specific selected root.
The trace, action ledger, and child ledger remain outside the controller's
writable scope. Isolated evaluators receive only their exact scratch output;
live compatibility probes use the same guard, private state, non-delegating
tool set, and kernel boundary as normal roles. Both are read-only with respect
to the repository: evaluator scope adds only its exact scratch file, while a
probe adds no role outputs at all. The probe work id is published to the hook
context before its process launches, so any attempted action joins to the
diagnostic work item. If discovery, the broker, or the kernel boundary is
unavailable, the process is refused rather than silently downgraded.
Claude's Bash tool writes its working directory to a per-call
`/tmp/claude-<4 hex>-cwd` file after every command. The macOS profile for a
Claude launch (role spawn and live probe only) allows writes to exactly that
file shape through two anchored rules for `/tmp` and `/private/tmp`, and to
nothing else in `/tmp`. Codex, OpenCode and authentication-check profiles do
not get the rule. The file is not part of the owned scope, so a model-issued
write to it is still denied by the hook. The accepted residual is that one
Claude sandbox could overwrite another live Claude session's cwd file. The
Linux bubblewrap profile does not include the rule.
Broker sockets use a short nonce-derived `/tmp` pathname so deeply nested
session roots cannot exceed the platform AF_UNIX limit; the random token still
authenticates every request, permissions are owner-only, and the broker verifies
the connecting UID using Darwin `LOCAL_PEERCRED` (or Linux `SO_PEERCRED` where
available). A platform with neither mechanism fails closed. Inode-checked
cleanup cannot unlink a newer broker's socket.

Guarded controller authentication is reference-only and checked inside the
exact production boundary before every process can make a model turn. Claude
keeps the existing authenticated profile for macOS Keychain lookup, excludes
its user/project/local settings, and temporarily links only Cowork's
preselected session id to a transcript and `session-env` directory in the
role-private controller-state directory. The links are removed when the session
closes; the private state remains resumable. Controller-native `ToolSearch` is
classified as read-only discovery; any tool it exposes is still intercepted
and classified independently before use. Codex receives a private `CODEX_HOME`
whose `auth.json` is a read-only symlink to the existing owner-only login file.
Its Cowork-owned
`hooks.json`, the auth link, and the auth target are protected from controller
writes. Neither path copies tokens, setup credentials, or an entire controller
profile. Missing, permissively readable, mismatched, or unauthenticated
references fail before a model process starts. The trace records only the
controller, a bounded authentication-method category, login-metadata presence
(never live authentication proof), duration, and error type. A probe-cache hit
reports `auth_revalidated: false`; only an uncached, provider-accepted probe
turn is live proof.

Claude transcripts remain in a stable per-role controller-state directory
recorded in `identities.json`. On the first resume of a legacy session, Cowork
copies its uniquely matching transcript from the default Claude projects tree
into that private layout; ambiguous matches fail closed. Ordinary trace events
use serialized constant-work JSONL appends, while guard attempt records retain
the scan-and-fsync exact-once path.

Child deltas come from content snapshots at child boundaries, including tracked
and untracked repository files and declared outputs outside the repository.
Reverted changes produce no delta. Actor evidence—not the enclosing snapshot
window—decides credit: child-only and parent-only paths go to their actual
actor, paths with evidence from multiple actors are contested, and changes
without actor evidence remain explicitly unattributed. Descendant-attributed
paths are therefore not inherited by an ancestor merely because its window
also enclosed the mutation.

Nested cost is exact-once per token axis. The measurement record states whether
provider evidence proves the counter is parent-inclusive or proves
parent-direct-plus-children arithmetic. It never infers an additive basis from
complete-looking native components alone. Missing provider evidence, missing
child telemetry, or irreconcilable counters remain `unknown` and make the
comparison non-comparable; they are never guessed or coerced to zero. Legacy
and direct-only sessions remain readable with nested facts marked unavailable.

## Development

Run offline tests through the provider barrier, naming explicit unittest ids
(a module, class, or single test; at least one is required):

```bash
python3 scripts/cowork_offline_tests.py test_cowork
python3 scripts/cowork_offline_tests.py test_cowork.SomeTest.test_x test_workflow_negative_controls
```

The suites use fakes, but a bug can still reach a real `claude`/`codex`/
`opencode` binary, so plain `python3 -m unittest` is not the recommended way to
run them. The harness (`scripts/cowork_offline_tests.py`, with
`scripts/cowork_offline_guard.py`) builds a scratch directory outside the repo,
drops credentials and `COWORK_LIVE` from the child environment, puts deny stubs
first on `PATH`, installs an in-process guard that refuses provider launches
and provider credential paths in every child Python, and runs a preflight probe
before any test. It passes only when preflight passed, unittest exited 0, the
run did not time out, and the provider sentinel is empty. Exit codes: 0 pass,
1 tests failed, 2 usage, 3 provider boundary violation, 4 setup/preflight
failed, 5 timeout.

This barrier catches accidental launches by ordinary test and product code. It
is not a sandbox against deliberately hostile code (ctypes, direct
`_posixsubprocess` calls, C-level file access, obfuscated shell text, or a
non-Python child that ignores the stub `PATH`), and `HOME` is not redirected,
so other host files stay readable.

The harness's own self-tests are harmless and run plain from `scripts/`, never
nested inside the harness (the outer guard would make the inner preflight fail
closed):

```bash
cd scripts && python3 -m unittest test_cowork_offline_guard
```

The unit tests cover flag assembly, preflight, argument parsing and session
selection, the run-result record and exit codes, the claude stream-json probe,
event parsing, denial handling, the phase loop (scout→planner chaining, the
authorized hand-back round trip, resume-into-planning, the scout-less refusal),
and the reviewer and decision-request paths (via injected fakes). One live
end-to-end agent-driven loop remains a manual check.

### Live integration tests

Live tests are a separate, explicit validation outside the offline barrier (the
harness strips `COWORK_LIVE`). Run them only when you intend to verify the real
contracts against the installed CLIs (catching flag/version drift). They spawn
real `claude`/`codex` processes, make real API calls, and are slow:

```bash
COWORK_LIVE=1 python3 -m unittest scripts/test_cowork.py
```

They are skipped automatically when `COWORK_LIVE` is unset or the CLI is not on
`PATH`. Tune the per-call timeout with `COWORK_LIVE_TIMEOUT` (seconds, default
240). The live tests assert that:

- claude accepts `cowork`'s stream-json stdin message shape and returns
  `assistant` + `result` events (and the probe passes);
- codex `exec --json` emits a `thread.started` `thread_id` and an agent message;
- `codex exec resume <thread_id>` resumes the same session by explicit id.
