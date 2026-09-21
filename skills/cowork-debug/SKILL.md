---
name: cowork-debug
description: >-
  Debug cowork sessions end to end. Use when the user asks why cowork showed a
  wrong gate/status, whether scout/reviewer/planner actually ran, how to inspect
  cowork session history, how to correlate .cowork artifacts with Claude/Codex
  logs, why a run ended with a given run-result record, or to diagnose stale
  intel/review/session/resume behavior.
---

# Cowork Debug

Reconstruct what happened in a cowork run by joining these evidence sources:

1. The run-result record: the last JSON line of the run's stdout (`rc`,
   `outcome`, `approved`, `stop`, `reason`, `session_file`). A missing line is a
   failure (for example SIGKILL), never an approval.
2. `.cowork/session.<uuid>.json` (project-local anchor in the launch directory;
   a legacy `.cowork/session.json` is still discovered) for cowork session
   UUID, team/config, role controller IDs, context revisions, and the open
   decision request.
3. `~/.cowork/sessions/<session_uuid>/trace.jsonl` for cowork orchestration
   decisions.
4. Durable control-plane records under the session root: work/phase state,
   `activity/history/<work_id>.jsonl`,
   `activity/scheduled_review/<work_id>.json`, and
   `checkpoints/<checkpoint_id>/{request,claim,result,receipt}.json`.
5. `~/.cowork/sessions/<session_uuid>/scout.intel.json` and
   `~/.cowork/sessions/<session_uuid>/scout-review.json` for
   current/final artifacts. Also useful in the same directory:
   `identities.json` (per-role tool + resolved model + controller session id —
   the fastest jump from a role to its controller log),
   `evaluation_queue.jsonl` (raw peer-evaluation lifecycle; malformed or
   silently-failed evaluations are visible only here, not in `scores.json`),
   and `orchestrator-evaluations.json` (targeted role evaluations written by
   the external orchestrator/driver via `cowork --evaluate-role`; the source
   for the per-role/controller/model breakdowns in `--report`, kept separate
   from peer `scores.json` and never read by any phase gate).
6. Claude/Codex/OpenCode local logs for role conversation and tool history.

Do not mutate session artifacts while debugging. The stderr transcript is a
symptom report only; verify it against the run result, trace events,
artifacts, and controller logs.

## Quick Workflow

1. Resolve the cowork session:
   - Use `session_file` from the run result, or the anchor the user names. Do
     not assume the directory's newest session is the one in question.
   - If the user gives a session UUID, verify it matches `session_uuid`.
   - Record `sessions.<role>.controller`, `sessions.<role>.id`, team/config,
     context revision, and `last_context_revision_seen`.
2. Read current durable state (under `~/.cowork/sessions/<uuid>/`):
   - Work/phase identity and latest terminal or non-terminal outcome.
   - Latest activity record and scheduled `next_inspection_at`.
   - Current checkpoint pointer plus request/claim/result/receipt when present.
3. Read current role artifacts:
   - Intel: `~/.cowork/sessions/<uuid>/scout.intel.json`
   - Review: `~/.cowork/sessions/<uuid>/scout-review.json`
   - Trace: `~/.cowork/sessions/<uuid>/trace.jsonl`
4. Locate controller logs only when durable state and artifacts do not answer
   the question:
   - Claude session id: `~/.claude/projects/**/<session_id>.jsonl`
   - Codex thread id: `~/.codex/sessions/**/rollout-*<thread_id>.jsonl`
   - OpenCode session id (`ses_…`): rows in
     `~/.local/share/opencode/opencode.db` (sqlite)
5. Build a timeline:
   - Start from persisted work/phase transitions, checkpoint receipts, and
     activity records.
   - Use trace events for ordering: status reads, gates, review rounds,
     decision requests/deliveries, invalidations, session saves, context acks,
     controller invocations.
   - Then use controller logs for role content: user messages, assistant replies,
     tool calls, artifact edits.
   - Finally compare final artifact state with the trace and controller writes.
6. Report findings with labels:
   - `evidence`: directly shown by trace, artifact, or controller log.
   - `inference`: likely conclusion from multiple evidence points.
   - `missing evidence`: needed fact is absent from all available logs.

## Trace Semantics

The cowork trace complements controller logs; it does not duplicate role
conversation. It records metadata only:

- Controller invocation metadata: controller, role, fresh/resume, mode/yolo, cwd,
  prompt file, session/thread id, redacted argv.
- Prompt-like content as `prompt_sha256` and `prompt_bytes`, never raw text.
- Orchestration decisions: `status.read`, `status.invalidated`,
  `review.round.start`, `review.run.*`, `review.skipped`, `gate.show`,
  `gate.decision` (the runtime's own decision), `decision.request.open`,
  `decision.delivery.*`, `handoff.*`, `controller.switch.*`,
  `controller.policy.*`, `context.*`, `run.*`.

If a trace file is missing, say so and fall back to artifacts + controller logs.
Older cowork runs may not have trace data.

Privacy rule: never add prompt text, model output, answers, artifact contents,
or raw transcript text to the trace. If more detail is needed, add metadata
counters or booleans.

## Reading Claude Logs

Guarded Claude roles keep transcripts under the role's `controller_state_dir`
recorded in `identities.json`; older sessions may still be under
`~/.claude/projects`. Given a session id from the session anchor or
`identities.json`, search those locations for `<session_id>.jsonl`.

Useful records:

- `user`: user messages, tool results, and replayed cowork prompts.
- `assistant`: assistant text and tool calls.
- `toolUseResult`: file edits and command results, often with exact file paths.
- `last-prompt`: latest prompt summary pointer, useful for navigation.
- `summary` or compaction records, when present: lossy navigation aids only.

Claude logs can prove what the role saw or wrote. They cannot by themselves
prove why cowork chose a gate; use trace for that.

## Reading Codex Logs

Codex roles run with a private `CODEX_HOME` under the role's
`controller_state_dir` in `identities.json`; older sessions may still be under
`~/.codex/sessions`.
Given a thread id from the session anchor, search for
`rollout-*<thread_id>.jsonl`.

Useful records:

- `session_meta`: thread id, cwd, CLI version, model/provider.
- `turn_context`: cwd, sandbox/approval policy, workspace roots.
- `event_msg.user_message`: user prompt sent to Codex.
- `response_item.message`: assistant message content.
- `response_item.function_call` and `function_call_output`: tool calls/results.
- `event_msg.task_complete`: turn completion.

Codex may include reasoning or summaries. Treat summaries as lossy unless raw
messages/tool events are unavailable.

## Reading OpenCode Logs

OpenCode has no per-session JSONL; everything lives in the sqlite database
`~/.local/share/opencode/opencode.db`. Given a `ses_…` id from the session
anchor or `identities.json`:

- `session` — one row per session (`id`, `time_created` in epoch ms).
- `part` — the conversation: each row's `data` column is JSON. Filter with
  `json_extract(data,'$.type')='tool'` for tool calls;
  `$.tool` is the tool name, `$.state.status` is
  `completed`/`error`, `$.state.input.filePath` the target, and
  `$.state.error` carries the full denial text **including the resolved
  permission rule list** — the ground truth for "why was this write/read
  rejected".
- `permission` — durable per-project permission approvals.
- `message` / `session_message` — assistant/user message bodies.

Example — every tool error in a role's session:

```bash
sqlite3 ~/.local/share/opencode/opencode.db \
  "SELECT json_extract(data,'\$.tool'), json_extract(data,'\$.state.error')
   FROM part WHERE session_id='ses_XXX'
   AND json_extract(data,'\$.type')='tool'
   AND json_extract(data,'\$.state.status')='error'"
```

The model's own prose about *why* something failed is narrative, not
evidence — always confirm against `$.state.error` and the rule list.

## Common Diagnoses

- Unexpected run result (wrong `rc`/`outcome`, or an approval that should not
  exist): only a paired reviewer `approve` verdict approves. Check the latest
  `status.read`, `review.run.end`, `gate.decision`, and any
  `status.invalidated` for that phase; if trace is missing, compare the last
  controller write to the status and review artifacts.
- rc 4 stop that looks wrong: compare `stop.kind`/`stop.request_id` with the
  session's open decision and `decision.request.open`; a response to a
  different or consumed request is refused, not applied.
- Reviewer seemed absent: check `review.round.start`, `review.run.start`,
  controller invocation events for `scout-reviewer`, and the Codex/Claude
  reviewer log by saved id.
- Review file has only one verdict: expected. Review files are latest-only; use
  trace + controller logs for history.
- Intel not updated until resume: compare controller artifact-edit records,
  final intel mtime/content, trace `status.read`, and any `context.gap` /
  `run.resume` events.
- Stale resume/context: compare `context.current`, `context.gap`,
  `context.ack`, and each role's `last_context_revision_seen`.
- Trace ends at `controller.turn.start` with no terminal event (no
  `controller.turn.end`, no `run.end`): classify it as incomplete trace
  evidence, not as a crash. Compare the latest durable activity/checkpoint
  state and one real process probe. A dead process plus durable crash evidence
  may establish external termination; a live process may still be productive
  or waiting on the provider. Resume only when the preserved role/session/
  candidate state and policy authorize it.
- `stale_noop` / `stale_noop.unresolved` on a lead: the turn produced no
  artifact-byte change. Before blaming the model, check the controller log
  for **denied writes** (opencode: `part` rows with `$.state.status='error'`)
  — a role whose canonical writes are blocked may have parked a complete
  artifact in a fallback location such as
  `~/.local/share/opencode/tool-output/`.

## Output Shape

Keep the user-facing report short:

- Timeline: key timestamped events with source labels.
- Finding: what failed or behaved correctly.
- Evidence: paths and line/event references.
- Gaps: anything not logged that prevents certainty.
- Suggested fix/test when relevant.
