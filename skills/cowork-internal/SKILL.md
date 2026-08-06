---
name: cowork-internal
description: Track cowork planning items (bugs, confusing behavior, orchestration/recovery problems, misattributed cost) as GitHub Issues in the private garusis/cowork-internal repo instead of the public cowork repo or markdown docs. Use when the user says "add a backlog item", "file this in cowork-internal", "track this UX/orchestration/cost-value issue" — AND proactively any time you (the agent), while working in the cowork repo on an unrelated task, notice/find/hit a bug, confusing behavior, silent failure, stale status, misattributed cost, or orchestration/recovery problem in Cowork itself. Don't just mention the bug in passing — check whether it's already filed and file it here so it isn't lost when the session ends. Also use to decide which category an item belongs to, or whether something belongs in this repo vs. the public repo's issues.
---

# cowork-internal

`garusis/cowork-internal` is a **private** GitHub repo used only to track
cowork planning items as Issues. It exists because these items can describe
confusing, broken, or sensitive behavior in cowork and shouldn't sit in the
public `garusis/cowork` repo's issue tracker.

It replaced three markdown backlog docs that used to live in `.cowork/` in the
`cowork` repo — content now lives as one GitHub Issue per item instead.

## Categories

Every item belongs to exactly one primary category (`area:*` label). Use these
definitions to decide where something goes:

### `area:ux` — interactive UX

Active user-experience problems in Cowork's interactive workflow. Whether a
user can understand what is happening, make a safe decision, and recover
without needing to inspect session files or provider logs.

An item belongs here only when the user-facing behavior is still confusing,
misleading, silent, or unnecessarily difficult to recover from.

### `area:orchestration` — orchestration reliability

Active orchestration, recovery, policy, and guardrail problems. Closed
implementation history belongs in Git and session artifacts, not here.

An item belongs here when it can materially affect:

- which role runs, with which controller, model, permissions, and inputs;
- whether a phase advances, stops, resumes, or reports success truthfully;
- whether work and evidence survive recovery without being repeated or lost;
- whether agent actions stay inside the configured repository and policy
  boundaries; or
- whether review and trace records describe what actually happened.

### `area:cost-value` — cost and value measurement

Problems and directions needed to make Cowork's cost and value data useful for
comparing agents, controllers, models, phases, and orchestration choices.

An item belongs here only when it can materially distort:

- the cost attributed to a piece of work;
- the value credited to an agent, reviewer, or phase;
- the cohort in which a result is compared;
- the reliability of the evidence behind a comparison; or
- the decision Cowork makes from that evidence.

### New categories

If an item doesn't fit any of the above, it's fine to propose a new
`area:<name>` category rather than force a fit (see label command below).

## Repo and label scheme

- Repo: `garusis/cowork-internal`
- Priority labels (exactly one per issue): `P0`, `P1`, `P2`
  - **P0**: current behavior can leave the user stuck, hide a blocking state, or make a valid workflow impossible.
  - **P1**: current behavior presents incomplete/stale info at a decision point, raising the chance of a wrong action or extra recovery work.
  - **P2**: understandable but missing useful context; not urgent.
- Area labels: `area:ux`, `area:orchestration`, `area:cost-value`, or a new `area:<name>` created with `gh label create "area:<name>" -R garusis/cowork-internal -c "<hex>"`.

## Issue body template

Match the existing style — terse, evidence-driven, no fluff:

```markdown
#### Problem

<what's wrong today, concretely — not a feature request framing>

#### Evidence

<observed behavior, logs, dates, repro steps if available>

#### Why it matters

<concrete consequence for the user if left unfixed>

#### Direction

1. <concrete step>
2. <concrete step>
...

#### Completion criteria

- <observable, checkable condition>
- <observable, checkable condition>
```

Omit a section only if there's genuinely nothing to say — don't pad it.

## Creating an item

1. Pick the category/categories.
2. Write the body to a temp file (avoids quoting issues with `--body`).
3. Create the issue (the issue number GitHub assigns is the ID — no manual prefix needed):

```bash
gh issue create -R garusis/cowork-internal \
  --title "Short imperative title" \
  --body-file /tmp/issue_body.md \
  --label "P1" --label "area:ux"
```

4. Report the issue URL back to the user.

## Finding / updating existing items

```bash
gh issue list -R garusis/cowork-internal --label area:ux --state open
gh issue view <number> -R garusis/cowork-internal
gh issue edit <number> -R garusis/cowork-internal --add-label P0 --remove-label P1
gh issue close <number> -R garusis/cowork-internal --comment "Fixed by <PR/commit>"
```

## What NOT to do

- Don't file these in `garusis/cowork` issues — that repo is public.
- Don't recreate the old markdown docs — the issues in cowork-internal are now the source of truth.
- Don't invent a new priority label — only P0/P1/P2 exist.
