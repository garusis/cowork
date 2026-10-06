---
name: cowork-cli
description: >-
  Run the cowork CLI correctly from an agent. Use when asked to run cowork,
  delegate work to a cowork team, start a scout/plan/build run, continue a
  saved cowork session, answer a cowork stop, switch a role's controller, read
  cowork artifacts, or produce a cowork token/cost report. Covers invocation,
  the JSON run result and exit codes, session selection, decisions, capacity
  pauses, worktrees, and live supervision.
---

# Cowork CLI

`cowork` is invoked by an orchestrating agent with arguments. It assembles a
team of CLI-driven roles (`scout` → `planner` → `builder`, each with a paired
reviewer), launches a controller CLI per role (`claude`, `codex`, or
`opencode`), and ends every run with one structured JSON result. It never reads
a terminal or prompts, never approves by omission, and makes no git commit or
PR: approved build output is left in the working tree.

`cowork --help` lists every flag. The contract source is `build_parser`,
`select_session`, `build_run_result`, and `main` in `scripts/cowork.py`.

## Run result

Every run writes exactly one JSON object as the **last line of stdout**; the
provider transcript and `cowork:` notices go to stderr. Bind the record to the
process exit status (`rc` equals it) and treat a missing line as a failure.

Key fields: `rc`, `outcome`, `approved`, `session_uuid`, `session_file`,
`phase`, `role`, `stop`, `reason`, `resume_argv` (persisted sessions), and
`decision_argv` (open decision; replace the literal `<answer>` with the path of
your answer file).

| rc | `outcome` | Next step |
| --- | --- | --- |
| 0 | `approved` | Success only with `approved: true`. Inspect the working tree. |
| 1 | `failed` | Read `reason`/`stop`. Recover by a plain resume or `--switch-controller` only when the cause is fixed and authority permits. |
| 2 | `invalid_invocation` | Nothing was dispatched. Fix the arguments named by `reason`; do not retry unchanged. |
| 3 | `owner_conflict` | Another process owns the session. Check `cowork --session-owner`; never take over a live owner by default. |
| 4 | `stopped` | An open decision (`stop.request_id`). Answer within your authority or escalate. |
| 5 | `awaiting_capacity` | Provider capacity pause. Wait for `resume-trigger`; do not retry the same provider. |
| 17 | `terminated` | Provider refusal or no first token. Do not blindly retry; report or change controller/policy only with authority. |
| 130 / 143 | `interrupted` / `terminated` | SIGINT / SIGTERM. Continue persisted work explicitly with its session file. |

A stop, a capacity pause, or a missing result line is never an approval.
`--help` is not a run and emits no record. `--check`, `--report`,
`--session-owner`, and `--evaluate-role` print their own output and emit no
run-result record. Combining `--check`, `--report`, or `--session-owner` with a
session-mutating flag (`--switch-controller`, `--allow-controllers`,
`--take-over`, or a decision flag) is refused with an rc 2
`conflicting_arguments` record. `--evaluate-role` is not part of that check: it
is dispatched after those three and before any run, so any run or
session-mutating flag passed with it is silently ignored. Invoke it on its own.

## Start a session

```bash
# profiled implementation (models are configured separately)
cowork --profile standard --profile-rationale "bounded behavior change" --context-file ./brief.md

# investigation-only exception: profiles include building
cowork --team scout,scout-reviewer --context-file ./brief.md

# planning-only exception in an isolated worktree
cowork --worktree my-feature --team scout,scout-reviewer,planner,planning-advisor \
       --context-file ./brief.md
```

- A new session requires `--context` or `--context-file` (`-` reads stdin);
  otherwise rc 2 `context_required`. Prefer `--context-file` for anything
  longer than a sentence.
- `--team` is comma-separated. Every lead needs its paired reviewer
  (`reviewer_not_selected`), and a run starting in scouting needs `scout`
  (`scout_not_selected`). Every new session starts in scouting, so there is no
  standalone planner, builder, or reviewer session. The useful teams are
  `scout,scout-reviewer`, that plus `planner,planning-advisor` (the plan is the
  deliverable), or all six roles. An unprofiled manual team with a builder pair
  but no planner pair is accepted but never reaches building. Pick the smallest team that supplies
  the discovery, planning, implementation, and assurance the risk needs.
- For implementation, launch with an explicit **execution profile**:
  `cowork --preview-profile light|standard|assurance` prints the complete policy
  as one JSON object (read-only, nothing dispatched), then start the session
  with `--profile NAME --profile-rationale TEXT` (it derives the team, so it
  cannot be combined with `--team` or `--no-session`). `light`
  suits a bounded documentation batch, `standard` is the default for a behavior
  change, `assurance` suits invariant or architectural work. Promotion to a
  stricter profile is automatic and one-way; resume with a higher `--profile`
  to promote explicitly (a lower one is refused). See the README's "Execution
  profiles" for the refusal codes and the `review_profile_rejected` stop.
  Investigation-only/planning-only teams are explicit unprofiled exceptions;
  no shipped profile stops before building. Follow `cowork-orchestrate` for
  selection, exception evidence, cohort comparison and profile-opportunity
  tracking. Read selected/effective profile and promotions from the official
  session `execution_profile.json`, not from role count or model names.
  Existing unprofiled sessions cannot acquire a profile on resume
  (`profile_not_bound`); preserve that classification rather than restarting
  work or inventing a selected profile.
- `--worktree [NAME]` / `--wt` requires launching inside a git work tree
  (`worktree_requires_git`); `--wt-controller` picks the worktree role's
  controller. The session anchor stays in the **launch** directory.
- `--evaluation-policy all_rounds|final_round|sampled|off` controls peer
  scoring; its overhead is reported separately.

### `--config` grammar

`--config ROLE=opt,opt`, repeatable, one per role:

| Token | Values |
| --- | --- |
| controller | `claude` \| `codex` \| `opencode` |
| `model=<id>` | controller-specific; opencode ids are `provider/model`; `model=default` resets |
| `effort=<level>` | controller-specific; `effort=default` resets |
| access | `yolo` \| `no-yolo` |
| mode | `plan` \| `implement` |

Roles: `scout`, `scout-reviewer`, `planner`, `planning-advisor`, `builder`,
`build-reviewer`. Defaults: `claude`, controller-default model/effort, yolo on,
implement mode. Pinning a lead and its reviewer to specific models is the
supported way to compare their scores and token use.

## Continue a saved session

Select saved work explicitly. A run with no selector always starts a new
session; it never resumes.

```bash
cowork --session-file .cowork/session.<uuid>.json                       # continue
cowork --session-file PATH --context-file ./redirect.md                 # new context revision
cowork --session-file PATH --switch-controller builder=codex            # move a current-phase role
cowork --session-file PATH --allow-controllers claude,codex \
       --switch-controller builder=codex --switch-controller build-reviewer=claude
```

- Use `session_file` / `resume_argv` from the earlier run result.
  `--resume` selects the directory's most recent saved session; use it only
  when you know that is the intended session. At most one of
  `--session-file`, `--resume`, `--new`, `--no-session`.
- Saved-session operations (`--switch-controller`, `--allow-controllers`,
  decisions, `--take-over`) require `--session-file` or `--resume`.
- A switch applies only to current-phase roles, resets that role's model/effort
  pins, and all switches plus a policy change land as one all-or-nothing write.
  Neither flag combines with `--team` or `--config`; restrict controllers on a
  fresh configured run by choosing them in `--config`.
- A resume re-enters the persisted phase and redispatches its role onto its
  persisted partial state. After fixing an environment or harness cause, a
  resume with context stating what changed is the recovery tool.
- `--take-over` is never implicit: a crashed owner is reclaimed only with proof
  of death, a live same-host owner is terminated, and an unprovable owner is
  refused. Use it only when you are authorized to end that owner.

## Answer a stop (rc 4)

| `stop.kind` | Consumed by |
| --- | --- |
| `needs_input`, `reviewer_question`, `review_round_cap`, `review_not_approved` | `--answer REQUEST_ID` with `--context-file` |
| `handoff_requested` | `--authorize-handoff REQUEST_ID` or `--decline-handoff REQUEST_ID [--context-file]` |

```bash
cowork --session-file PATH --answer REQUEST_ID --context-file ./answer.md
cowork --session-file PATH --authorize-handoff REQUEST_ID
```

One decision flag per invocation, bound to `stop.request_id`. An answer is
delivered to the role by path and never approves anything by itself; the paired
reviewer still has to approve. Answer only within the authority you hold; a
scope, risk, or spending decision beyond it goes back to your principal.

## Capacity pause (rc 5)

`stop` carries the PauseLease facts, including `lease_id` and
`automation_ref`. The pause does not schedule a wake itself; the paused turn
is replayed only when something fires the separate `resume-trigger` entry
point:

```bash
cowork resume-trigger --session-uuid UUID --lease-id LEASE_ID \
       --claimant-ref CLAIMANT_REF --automation-ref AUTOMATION_REF --cwd LAUNCH_DIR
```

All four identities are required. `--claimant-ref` is not in `stop`: the caller
chooses a stable claim identity and reuses it for every retrigger of that
claim. `--cwd` is the launch directory even for a `--worktree` run. To schedule
or signal the wake, use `scripts/cowork_wake_macos.py` (trustworthy reset time)
or `scripts/cowork_wake_manual.py` (signed capacity signal) and read its
`--help`. After it reports `success`, continue with a plain
`cowork --session-file PATH`. Delivery is at least once. If a plain run is
refused with `decision_held_by_capacity_pause`, retrigger with the identities
that refusal names; a retrigger can replace the lease, so re-read the current
lease before triggering again.

## Read-only commands

```bash
cowork --check                          # preflight: python + controller CLIs
cowork --report [SESSION_UUID] [--json] [--rebuild]
cowork --session-owner [SESSION_UUID] [--json]
```

`--report` loads `measurement.json` and never rebuilds implicitly; pass
`--rebuild` when the run finished after the record was written. Without a
UUID, `--report` and `--session-owner` read the directory's most recent
session.

## Targeted role evaluations (`--evaluate-role`)

An external orchestrator can record per-contribution scores in
`orchestrator-evaluations.json`, separate from peer `scores.json` and never read
by a phase gate:

```bash
cowork --evaluate-role builder --eval-session <SESSION_UUID> --work-id <WORK_ID> \
       --output-quality 4 --intent-alignment 5 --evidence-quality 4 \
       --self-sufficiency 3 --cost-worthiness 4 --notes "clean diff, one re-review"

cowork --evaluate-role orchestration --eval-session <SESSION_UUID> --phase building \
       --output-quality 5 --intent-alignment 5 --evidence-quality 4 \
       --self-sufficiency 5 --cost-worthiness 4
```

- Targets: the six roles plus `orchestration`. The session flag is
  `--eval-session` (not `--session`).
- `--work-id` is required for team roles; take it from `controller.turn.start`
  events in `trace.jsonl` (not from `evaluation_queue.jsonl`). An unrecognized
  `(role, work_id)` exits 2 and writes nothing.
- `--phase` is required for `orchestration` (`scouting|planning|building|session`).
- Scores are integers 1–5, higher is better; `--self-sufficiency` 5 means no
  correction was needed. Artifact provenance comes from the historical trace
  fingerprint; there is no `--artifact-digest`.
- Re-evaluating appends; `--report` scores the latest entry per target.
- Exit codes: 0 recorded, 1 could not record (existing file preserved),
  2 invalid arguments.

## Where the output is

Project-local anchor in the launch directory: `.cowork/session.<uuid>.json`
(team, per-role config, phase, controller session ids, context revisions,
reviewer baselines, open decision).

Per-session artifacts under `~/.cowork/sessions/<session_uuid>/`
(`COWORK_SESSIONS_ROOT` overrides the root):

```
scout.intel.json / scout.intel.md        scout-review.json
planner.plan.json / planner.plan.md      planner-review.json
builder.status.json / builder.summary.md builder-review.json
scores.json  orchestrator-evaluations.json  identities.json
measurement.json  trace.jsonl  ledger.jsonl
verification/transactions/<txn>/result.json
activity/history/<work_id>.jsonl         activity/scheduled_review/<work_id>.json
checkpoints/<checkpoint_id>/{request,claim,result,receipt}.json
```

Explain a run from the result record, durable state, and artifacts; the stderr
transcript is a symptom report only. For forensics use `cowork-debug`. After a
run that included the builder, inspect `git status` and `git diff` yourself.

## Long runs and supervision

Many agent harnesses kill a background shell's process group when a tool call
ends, which freezes the trace at `controller.turn.start`. Launch detached,
release the launching tool's inherited stdio so its call returns, keep stdout
separate so the result line stays parseable, and have the detached parent wait
and record the exit status:

```python
import json, os, subprocess, sys
ARGV = ["cowork", "--session-file", SESSION_FILE]  # the exact run you intend
pid = os.fork()
if pid > 0:
    os.waitpid(pid, 0); sys.exit(0)
os.setsid()
devnull = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(devnull, fd)
if os.fork() > 0:
    os._exit(0)
os.chdir(WORKDIR)
with open(RESULT_FILE, "wb") as out, open(TRANSCRIPT_FILE, "ab") as err:
    rc = subprocess.call(ARGV, stdout=out, stderr=err,
                         stdin=subprocess.DEVNULL)
with open(RC_FILE + ".tmp", "w") as fh:
    json.dump({"rc": rc}, fh)
os.replace(RC_FILE + ".tmp", RC_FILE)
os._exit(0)
```

Use fresh `RESULT_FILE` and `RC_FILE` paths per launch. Pass `--context-file`
on a saved session only when you intend a new context revision; a plain
continuation is `--session-file` alone. The run is finished once `RC_FILE`
exists. Accept its outcome only when the last line of `RESULT_FILE` parses as
the result record and its `rc` equals the recorded exit status (a negative
value means a signal ended the process). A missing `RC_FILE` with no live
process, a missing or torn result line, or an `rc` mismatch is a failure.

Do not keep the turn open to poll. Schedule a recurring wake every 15 minutes
unless durable `next_inspection_at` requests a later one; at each wake inspect
each active work once from compact durable state, and end silently when it is
healthy and non-terminal. Read a diff, process probe, or controller log only
when compact state makes it decision-relevant. For failure classification and
bounded recovery, read [live supervision](references/live-supervision.md).

`stale_noop` means a lead's turn left its status artifact unchanged; after one
automatic repair turn the run ends rc 1 (`stale_noop.unresolved` in the trace). Before blaming the model, check the controller log for
denied writes (opencode: `~/.local/share/opencode/opencode.db`); a blocked role
may have parked its artifact in a fallback location such as
`~/.local/share/opencode/tool-output/`.

## Operating notes

- **Do not nest.** Roles refuse controller-native child agents; never invoke
  `cowork` from inside a cowork role.
- **`cwd` decides where the session lands**; run from the directory you mean.
- **`cwd` does not decide which code runs.** The `cowork` on PATH executes its
  own checkout's `scripts/`, and the installed skills are symlinks into the
  checkout `install.sh` last ran from. A fix that exists only on another branch
  or worktree does not run until that checkout has it. Synchronize the
  installed launcher and skills only after the change is reviewed and
  integrated, by re-running `install.sh` from that durable checkout; never
  repoint them at an unreviewed candidate worktree.
- **Confinement is instruction-level plus a broker/kernel boundary**, not a
  general sandbox. Writable scope is the selected worktree, the role's declared
  outputs, and its private state. On Linux, opencode roles lack the macOS
  claude/codex broker receipts and OS write boundary.
- Run `cowork --check` first when a run fails at launch; it names the missing
  piece.
