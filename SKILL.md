---
name: co-plan
description: >-
  Join or scaffold a two-agent planning chat backed by a shared Markdown file.
  Use when the user says "co-plan", "/co-plan mode [chat-file]", "init a
  co-plan in path", "let's start a co-plan", "join the plan chat as planner",
  "join the plan chat as advisor", "resume the co-plan", or "resolve a co-plan
  question". Modes are `init` (create the chat file and refine the goal with
  follow-up context, fetching any referenced ClickUp ticket and its comments
  verbatim via the ClickUp MCP), `planner`, `advisor`, `resolve decision`,
  `answer Q-id`, and `signoff`. The skill is plan-only:
  participants critique, refine, and reach consensus; they never implement code.
---

# Co-plan

Run a two-agent planning workflow through a shared Markdown chat file and a
current plan file. The chat is the audit trail. The plan file is the
implementation-ready deliverable Marcos signs off on.

## Invocation

```text
/co-plan init [chat-file-path]
/co-plan planner [chat-file-path]
/co-plan advisor [chat-file-path]
/co-plan resolve "<decision text>" [chat-file-path]
/co-plan answer <Q-id> "<answer text>" [chat-file-path]
/co-plan signoff [chat-file-path]
```

- `chat-file-path` defaults to `./agent-chat.md`.
- Relative paths resolve from the current working directory. For `resolve`,
  `answer`, and `signoff`, pass the real chat file explicitly whenever the
  session is not already in that directory. A missing default path is a hard
  error, never a no-op.
- The plan file is `<chat-stem>.plan.md` beside the chat file.
- The sidecar state file is `<chat-file>.state.json`.
- Rejoining a role is identical to invoking `/co-plan planner` or
  `/co-plan advisor` again.
- `resolve` appends a `### [marcos]` decision block.
- `answer` resolves a ledger question (`Q1`, `Q2`, ...).
- `signoff` records Marcos's approval after both agents have proposed the
  same plan-file hash and all ledger questions are answered.

Agent-facing helper commands:

```bash
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py turn --file ./agent-chat.md --self planner
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py deps-status
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py question --file ./agent-chat.md --self planner --question "..."
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py post --file ./agent-chat.md --self planner --body-file "$SCRATCH"
```

Marcos/admin helper commands:

```bash
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py init --file ./agent-chat.md --body-file "$SCRATCH"
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py update-goal --file ./agent-chat.md --body-file "$SCRATCH"
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py resolve --file ./agent-chat.md --decision "..."
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py answer-question --file ./agent-chat.md --id Q3 --answer "..."
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py signoff --file ./agent-chat.md
```

Always mint a fresh scratch path for every write:

```bash
SCRATCH=$(mktemp -t coplan.XXXXXX)
```

Never reuse fixed paths like `/tmp/coplan-msg.md`; stale scratch files can
silently append old content.

## Optional rtk and caveman

`rtk` and `caveman` are optional manual installs. Co-plan must not install,
clone, initialize, or modify global agent configuration for them. If they are
not installed, continue the normal workflow without warning.

When they are installed, planner/advisor agents must use them for agent-to-agent
work:

- Run `deps-status` at the start of each planner/advisor session, or read the
  `optional_dependencies` object returned by `turn`.
- If `optional_dependencies.rtk.available` is true, use `rtk`-wrapped shell
  exploration commands where practical so command output is compact.
- If `optional_dependencies.caveman.available` is true, write normal
  planner/advisor back-and-forth turns in terse caveman style at **ultra**
  intensity (regardless of the global default mode) while preserving technical
  accuracy, file/symbol citations, ledger IDs, and required receipt structure.
  Do not invoke `/caveman ultra` (that would change the user-visible global
  level); just apply ultra-level compression to your own agent-to-agent
  messages.
- Do not use caveman style for Marcos-facing content: escalations, ledger
  answers, non-ledger resolves, sign-off recap, final consensus, or direct user
  status. Those messages must stay clear, complete, and readable.

## File Protocol

The chat file is append-only after initialization. The only exception is goal
refinement during `init`, before any non-goal role posts.

Initial shape:

```markdown
# Co-plan chat

This file is the shared planning record for a two-agent planning chat. Messages
are append-only. The planner writes `### [consensus]` when the plan is agreed.

---

### [goal] 2026-05-11T14:00:00Z

Full planning goal in 1-3 paragraphs.
```

Later messages use:

```markdown
### [<role>] <ISO-8601 UTC timestamp>

Message body.
```

Allowed roles are `goal`, `planner`, `advisor`, `marcos`, `marcos-signoff`,
and `consensus`.

Two body markers are semantic:

- `--- escalating to marcos ---`: planner pauses for a decision.
- `--- proposing consensus ---`: role believes the current plan file is ready.

Do not write provider metadata, internal task IDs, or prior plan versions into
the chat.

## Plan File

`<chat-stem>.plan.md` is the current implementation plan. It is rewritten
during planning and must not be append-only.

Plan-file hygiene rules:

- Keep only the current plan. Do not keep previous versions, revision history,
  stale alternatives, copied chat turns, or "advisor said / planner said"
  commentary.
- Do not propose consensus with placeholders such as `TBD`, `TODO`, `fixme`,
  `open question`, or unresolved ledger IDs in the plan.
- Every scope exclusion must name the reason.
- Every behavioral claim about existing code must be cited with file/symbol
  evidence or explicitly marked as unverified until resolved.
- Every ticket/goal interpretation that maps a phrase to a code component must
  cite the exact goal phrase or mark the mapping as unverified.

Required final plan structure:

```markdown
# <short plan title>

## Summary
## Goal Coverage
## Decisions
## Approach
## Implementation Changes
## Tests
## Risks and Verification
## Assumptions
```

The helper's `post` command enforces required headings, placeholder cleanup,
stale-version markers, and unresolved ledger references when a proposal receipt
is posted. The planner and advisor still own plan quality beyond those
structural checks.

## Planning Phases

### Phase 1: Goal intake

`init` mode creates the chat and refines only the `### [goal]` section.

1. Resolve the chat path, defaulting to `./agent-chat.md`.
2. If the user's context references a ClickUp ticket (a URL like
   `https://app.clickup.com/t/<id>`, a `CU-<id>` reference, or a bare task ID),
   ALWAYS fetch it through the ClickUp MCP before drafting the goal. This is
   non-optional: do not paraphrase from memory, do not skip even if the user
   pasted a partial description, and do not substitute web fetch. Call
   `mcp__clickup__clickup_get_task` for the task and
   `mcp__clickup__clickup_get_task_comments` for every comment. Embed both
   verbatim in the goal body under clearly labeled sections (for example
   `## ClickUp ticket: <id> - <title>`, then the full description as written,
   then `## Comments` with each comment's author, timestamp, and exact text).
   Only after the verbatim block may you append a short synthesized
   "Planning goal" paragraph that names objective, constraints, success
   criteria, failure modes, and non-goals derived from the ticket plus any
   extra user context.
3. If no ClickUp ticket is referenced and substantive goal context is in the
   user request, use it verbatim.
4. If no usable goal exists, ask for at least one sentence and stop.
5. Use `init --body-file` for multi-line goals (required whenever a ClickUp
   ticket is embedded, since the body will exceed a single line).
6. If the chat already has non-goal messages, never edit the goal; join the
   requested role instead.

The goal should capture objective, known constraints, success criteria, known
failure modes, and any explicit non-goals. When a ClickUp ticket is the source
of truth, the verbatim ticket and comments are part of the goal record so the
planner and advisor can re-read the original wording later without re-querying
ClickUp.

### Phase 2: Scope and early questions

The planner's first substantive turn must create the plan file before posting
the chat reply.

The first draft must include:

- A `Goal Coverage` table with one row per concrete requirement, named
  failure mode, success criterion, and explicit non-goal from the goal.
- A `Decisions` section that lists resolved decisions and ledger-backed open
  questions.
- An `Approach` section that is allowed to be partial but must not silently
  choose product, UX, risk, or scope tradeoffs.
- `Risks and Verification` for unverified repo facts or goal mappings.

Before selecting the architecture, run the Early Question Gate:

- If a missing answer could change scope, user-visible behavior, data shape,
  migration strategy, compatibility, or rollout risk, add a ledger question in
  the same turn with `question`.
- The planner does not need to interrupt mid-turn. At the end of every planner
  turn, run the Planner Open-Question Gate from the Working Turn Loop; that gate
  surfaces all open Q-ids together and exits instead of polling.
- Ask early. Do not wait until the plan is otherwise finished.
- Do not use the ledger for normal implementation mechanics that the agents can
  resolve from the repo.
- Do not self-answer or defer a ledger question to a later engineer.

The planner's chat reply should summarize scope coverage, name any newly opened
Q-ids, and state what repo facts still need verification.

### Phase 3: Discovery and draft refinement

Both agents may read and search the repository. They must not implement code,
run migrations, install packages, generate code, or run formatters.

When making a behavioral claim, cite the file and preferably the symbol. If the
claim is not verified, mark it as an assumption in `Risks and Verification` and
do not let it justify extra defensive machinery without explicit agreement.

The planner updates the plan file before each chat message that changes the
proposed implementation. The chat message is only the delta and rationale.

The advisor critiques the current plan file, not only the last chat message.
Every advisor turn checks:

- Scope completeness against the goal.
- Whether goal phrases were mapped to code components with evidence.
- Whether every user-impacting choice is resolved or in the ledger.
- Whether implementation changes are concrete enough for another engineer.
- Whether the test plan covers success, failure, regression, and migration or
  compatibility risks introduced by the plan.
- Whether the plan is overbuilt or underbuilt. "Avoid overengineering" means
  removing unproven scaffolding, not accepting a vague or cheap plan.
- Whether the plan file contains stale content, previous versions, unresolved
  placeholders, or chat history.

If scope coverage is wrong, the advisor should lead with that and defer
implementation-detail critique until the planner fixes scope.

If the advisor adds or observes an open ledger question, the advisor cannot
escalate directly and must not continue normal critique. It should name the
blocking Q-id if it just raised one; `post` or `turn` will wait until the next
planner turn ends. The next planner turn owns the Marcos-facing escalation.

### Phase 4: Final readiness

Before proposing consensus, both agents must verify the plan is decision
complete:

- The `Goal Coverage` section maps every goal item to planned work, verified
  existing behavior, or a justified non-goal.
- The `Decisions` section lists all answered Q-ids and no open questions.
- The `Approach` and `Implementation Changes` sections specify the intended
  behavior, data flow, interfaces, failure handling, and compatibility impact.
- The `Tests` section names concrete unit, integration, regression, and manual
  checks as appropriate for the risk.
- `Risks and Verification` contains only accepted residual risks, not work that
  must be solved before implementation.
- `Assumptions` are explicit and safe for an implementer to rely on.

Posting a `## Plan Review Receipt` through `post` records the proposal and runs
the structural validation automatically.

### Phase 5: Consensus and sign-off

Consensus is plan-file based. Prior chat agreement does not count unless it is
reflected in the current plan file at proposal time.

Stored proposal hashes are valid only while the registered plan file still has
the same SHA256 hash. If the plan file changes, `next-action` must route back to
normal planning, not sign-off recap. Any normal planner/advisor turn withdraws
that role's prior proposal; if both roles withdraw, the sidecar proposal state is
cleared. New ledger questions, Marcos answers, and non-ledger Marcos decisions
clear proposal state because they can change the plan's decision basis.

When ready, an agent:

1. Reviews the whole current plan file.
2. Writes a chat body with a `## Plan Review Receipt` section. The helper treats
   that body as a proposal and adds `--- proposing consensus ---` if missing.
3. Runs `post --self <role> --body-file "$SCRATCH"` so the helper appends the
   proposal and records the plan hash atomically.

The receipt must mention every `##` and `###` heading in the plan file and
include:

- Plan file path.
- Reviewed end-to-end: yes.
- Status by section.
- Changes since last review, if any.
- Remaining risks or assumptions being accepted.

After both roles propose the same plan-file hash, the planner posts a
"Ready for sign-off - Question Ledger recap" message naming the plan file and
each Q-id's answer, then exits.

Marcos reviews the plan file and runs `/co-plan signoff`. After sign-off, the
planner joins again; `turn --self planner` writes:

```markdown
### [consensus] <timestamp>

Final agreed implementation plan lives at `<chat-stem>.plan.md`.
```

The helper refuses sign-off or consensus if any open question remains, either
role has not proposed, hashes differ, or the plan file changed after proposal.

## Working Turn Loop

`/co-plan planner` and `/co-plan advisor` are long-running role loops. Joining
again is the resume path. The helper owns waiting; agents do not call
`next-action`, `poll-for-other`, or low-level append commands in normal use.

Role isolation is mandatory. An agent running one co-plan role must never spawn,
delegate to, simulate, or otherwise create an agent to act as the opposite role
for the same chat. If the helper returns `wait`, the agent must wait through the
helper until it receives an actionable turn or a timeout. A timeout is the only
normal way to stop waiting when the protocol says to wait.

Loop algorithm:

1. Run `turn --self <role>` and inspect `optional_dependencies`. Installed
   optional tools are mandatory for the relevant planner/advisor work; missing
   tools are ignored.
2. If it returns `compose_initial_plan`, the planner creates
   `<chat-stem>.plan.md` before posting. The helper refuses the first planner
   post if the plan file is missing or lacks `Goal Coverage`, `Decisions`,
   `Approach`, and `Risks and Verification`.
3. If it returns `compose_turn`, read the chat and plan file, update the plan
   file first when needed, then write a concise turn body.
4. If it returns `compose_escalation`, create the initial plan file first when
   `must_create_plan_file` is true, then write an escalation body mentioning
   every returned Q-id, why the answers matter, concrete options or a
   recommended default, and what work is paused.
5. If it returns `compose_signoff_recap`, write the "Ready for sign-off -
   Question Ledger recap" body naming the plan file and every answered Q-id.
6. Run `post --self <role> --body-file "$SCRATCH"`. `post` appends the correct
   kind of turn, records proposal hashes when the body has a `## Plan Review
   Receipt`, and then waits internally whenever the protocol says this role
   should wait.
7. If `post` returns another `turn.action` beginning with `compose_`, continue
   from the matching step above. If it returns `timeout`, exit with a brief
   normal-session note. Do not append a waiting/status note to the chat.

Helper-enforced gates:

- `turn` and `post` block internally when the role must wait; the model does not
  decide whether waiting is worth it.
- Waiting must not be bypassed by spawning or delegating to the opposite role.
- `question` records open ledger questions. When any question is open, planner's
  next actionable state is `compose_escalation`; advisor keeps waiting.
- `post` validates escalation bodies, proposal receipts, plan hashes, sign-off
  recap ordering, and first-planner-turn plan-file creation.
- `post` only accepts a sign-off recap when the body actually says the plan is
  ready for sign-off and includes a Question Ledger recap or Q-id references.
  A normal planner turn must never be appended as `kind: signoff_recap`.
- After Marcos sign-off, `turn --self planner` writes the final consensus entry
  itself. The planner does not compose consensus text.

Explicit stop conditions:

- The planner posts an escalation ending with `--- escalating to marcos ---`.
- The helper-enforced open-question gate finds open questions and posts the required
  escalation.
- The planner posts the "Ready for sign-off - Question Ledger recap".
- `turn` or `post` times out while waiting for another role.
- Consensus exists.
- The user interrupts the running session.

Open ledger questions are not normal background state. They pause normal
planning as soon as the planner reaches the end-of-turn gate and surfaces them
to Marcos. Advisor sessions do not exit just because questions are open; the
helper waits until the planner has taken the next turn.

## Question Ledger

The ledger is the mechanism for asking Marcos questions early and blocking
consensus until they are answered.

- Add any open product, UX, business, scope, threshold, compatibility, or
  residual-risk choice with `question` in the same turn it is identified.
- `post` automatically checks whether open questions require planner escalation
  before normal planning can continue.
- Advisor-raised questions are not escalated by the advisor. The advisor waits
  for the next planner turn to end; that planner turn performs the escalation.
- Only Marcos answers ledger questions via `/co-plan answer <Q-id> "<answer>"`.
- Latest answer wins; prior answers remain in chat history.
- The advisor must raise omitted ledger questions when the planner guessed on
  Marcos's behalf.

## Escalation and Resolve

The planner is the only role that escalates to Marcos.

When input is needed:

1. Add ledger questions for concrete decisions.
2. Write an escalation body naming every open Q-id, why the decisions matter,
   concrete options or a recommended default, and what work is paused.
3. Run `post --self planner --body-file "$SCRATCH"`.
4. Exit. The helper does not wait after an escalation because Marcos input is
   required.

`/co-plan resolve "<decision>" [chat-file]` is backed by:

```bash
python3 /Users/marcos/.claude/skills/co-plan/scripts/co_plan_file.py resolve --file ./agent-chat.md --decision "..."
```

It appends a `### [marcos]` block for decisions that are not tied to a specific
Q-id and clears any recorded proposal hashes. `/co-plan answer` is preferred for
ledger questions because it also updates the question ledger.

If `./agent-chat.md` is not the active chat, pass the explicit chat path. If the
helper reports that the chat file or sidecar is missing, surface that error and
stop; do not manually edit the chat or state file to compensate.

## Iron Rule: No Implementation

Planning is the work under this skill.

Allowed:

- Read/search repo files to ground a plan.
- Read or post to the chat through deterministic helper commands.
- Edit the plan file.
- Run helper validation.

Forbidden:

- Editing repo code.
- Running migrations, package installers, code generation, or formatters.
- Running side-effecting project commands whose purpose is implementation.
- Treating chat consensus as permission to implement.

If asked to implement while in co-plan, refuse inside the chat and steer back to
planning. Implementation requires a separate workflow outside this skill.

## Do Not

- Do not rubber-stamp another participant's message.
- Do not spawn, delegate to, or simulate an agent as the opposite co-plan role.
- Do not unilaterally cut scope.
- Do not bury questions at the end; ask material questions as soon as they can
  change the plan.
- Do not use "avoid overengineering" as permission for vague, low-quality, or
  untestable plans.
- Do not keep previous plan versions or chat recaps in the plan file.
- Do not announce a changed implementation in chat before updating the plan
  file.
- Do not accept uncited behavioral claims as plan premises.
- Do not add speculative defensive code paths without evidence or explicit
  acceptance as residual risk.
- Do not bypass `post` when proposing consensus.
- Do not write to `<chat-file>.state.json` directly.
- Do not manually append `### [marcos]`; use `resolve` or `answer-question` so
  proposal state is invalidated correctly.
- Do not keep watching after final consensus exists.
