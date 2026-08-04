---
name: cowork-cli
description: >-
  Run the cowork CLI correctly from an agent. Use when asked to run cowork,
  delegate work to a cowork team, kick off a scout/plan/build run, resume or
  switch controllers on a cowork session, read cowork artifacts, or produce a
  cowork token/cost report. Covers the non-interactive args path, --headless,
  --worktree, sessions, exit codes, and the traps that make bare `cowork` hang.
---

# Cowork CLI

`cowork` assembles a team of CLI-driven roles (`scout` → `planner` → `builder`,
each with a paired critical reviewer), launches a controller CLI per role
(`claude`, `codex`, or `opencode`), and drives a scout → plan → build loop over
a working tree.

`cowork` makes **no git commit and opens no PR**. Approved build output is left
in the working tree for a human to review.

## The one rule that matters

**Never run bare `cowork` from an agent.** The default path is an interactive
terminal app (questionary menus, a prompt_toolkit editor, streaming Rich
panels). It expects a TTY it owns. Launched from an agent it either blocks on a
menu forever or hits closed stdin and exits `130 cowork: input closed.`

Agents use the **non-interactive args path**. Passing any of `--team`,
`--config`, `--context`, `--context-file`, or `--headless` switches the
interactive UI off entirely.

Even on the args path, **gates still block for a human** unless `--headless` is
set. For unattended work, `--headless` is required, and it in turn requires
initial context.

## Recipes

### Unattended full run (the default agent invocation)

```bash
cowork --headless --context "Add a --dry-run flag to the CLI"
```

Runs scout → scout-reviewer → planner → planning-advisor → builder →
build-reviewer with no human gates. Leads never block (they record an
assumption and proceed), reviewers review with what they have, each phase ends
on reviewer consensus or the review-round cap.

### Detached launch (agent harnesses that reap child processes)

Many agent harnesses and host apps kill the process **group** of background
shells when a tool call ends or the app cleans up — a plain `&` background
run or even `nohup` dies mid-turn (the trace freezes at
`controller.turn.start` with no terminal event). Launch cowork detached via
double-fork + `setsid` so it survives:

```python
import os, subprocess, sys
pid = os.fork()
if pid > 0:
    os.waitpid(pid, 0); sys.exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
os.chdir(WORKDIR)
with open(LOGFILE, "ab") as log:
    subprocess.Popen(["cowork", "--headless", "--context-file", "brief.md"],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL)
os._exit(0)
```

A killed run is not lost: resuming redispatches the active phase's role onto
its persisted state (see Resume below).

### Unattended, isolated in a git worktree (preferred when editing a repo)

```bash
cowork --worktree --headless --context "Add a --dry-run flag to the CLI"
cowork --worktree my-feature --wt-controller codex --headless --context-file ./brief.md
```

A small worktree agent creates the worktree following the repo's documented
convention (read from `AGENTS.md`/`CLAUDE.md`), then cowork `chdir`s into it for
the rest of the run. Requires launching **inside a git work tree** (else rc 2).

Resume trap: with `--worktree` the session store stays in the **launch**
directory, not the worktree. Resume from the launch dir or via
`--session-file`.

### Subset of the flow

```bash
# scout only
cowork --team scout --headless --context "Map how auth tokens are refreshed"

# scout + planner, no reviewers
cowork --team scout,planner --headless --context-file ./brief.md
```

`--team` is comma-separated. A **fresh** team without `scout` exits 0 with a
note — every run begins with scouting.

### Context from a file or stdin

```bash
cowork --headless --context-file ./brief.md
echo "the brief" | cowork --headless --context-file -
```

Prefer `--context-file` for anything longer than a sentence.

### Read-only commands (safe to run anytime, no controllers spawned)

```bash
cowork --check                 # preflight: python, UI deps, controller CLIs
cowork --report                # token/byte report for this dir's newest session
cowork --report <SESSION_UUID> # a specific session
cowork --report --json         # the authoritative measurement record
cowork --report --rebuild      # rebuild the record from raw sources first
```

`--report` loads the existing `measurement.json` and never rebuilds implicitly;
pass `--rebuild` when the run finished after the last record was written.

### Resume / redirect an existing session

```bash
cowork --headless --context "…"        # resume, redirected to new context
cowork --new --headless --context "…"  # fresh session, prior ones stay intact
cowork --no-session --headless --context "…"  # never read or write the store
cowork --session-file .cowork/session.<uuid>.json --headless --context "…"
```

On resume without `--context`, cowork sends "Continue the session." and the
current phase's role picks up where it left off.

Resume-with-context is also the **supervisor recovery tool** for a wedged
phase: after an external kill, or after fixing an environment/harness bug
that blocked the active role, resume with a `--context` that states what was
fixed and what the role should do next. The active phase's role is
redispatched onto its persisted partial state (edits, artifacts) rather than
restarting the phase.

`--resume` opens an interactive picker — **needs a TTY, not for agents**. Target
a specific session with `--session-file` instead.

### Switch a stuck role's controller

```bash
cowork --switch-controller planner=codex
cowork --allow-controllers claude,codex \
       --switch-controller builder=codex \
       --switch-controller build-reviewer=claude
cowork --allow-controllers all          # lift the restriction
```

The role must be in the current phase pair (`scout`/`scout-reviewer` while
scouting, `planner`/`planning-advisor` while planning, `builder`/`build-reviewer`
while building). A switch resets that role's model/effort pins. Repeatable
switches apply as one all-or-nothing write. Cannot combine with `--team`,
`--config`, `--new`, `--no-session`, `--check`, or `--report`.

Known gap: `--allow-controllers` also cannot combine with a fresh
`--config` team — to restrict controllers on a configured fresh run, enforce
the restriction yourself at each switch decision instead of passing the flag.

## `--config` grammar

`--config ROLE=opt,opt` — repeatable, one per role.

| Token | Values |
| --- | --- |
| controller | `claude` \| `codex` \| `opencode` |
| `model=<id>` | controller-specific; opencode ids are `provider/model`; `model=default` resets |
| `effort=<level>` | controller-specific; `effort=default` resets |
| access | `yolo` \| `no-yolo` |
| mode | `plan` \| `implement` |

```bash
cowork --config "scout=claude,model=opus,effort=high" \
       --config "scout-reviewer=codex,model=gpt-5-codex" \
       --headless --context "…"

cowork --config "builder=opencode,model=anthropic/claude-sonnet-4-5,effort=max" \
       --headless --context "…"
```

Roles: `scout`, `scout-reviewer`, `planner`, `planning-advisor`, `builder`,
`build-reviewer`. Defaults: leads on `claude`, reviewers on `codex`, model and
effort inherit the controller CLI's own setting, yolo on, implement mode.

Pinning a lead and its reviewer to two specific models is the supported way to
compare their evaluation scores and token consumption.

`--evaluation-policy all_rounds|final_round|sampled|off` controls how much of
the run gets peer-scored; its overhead is reported separately.

## Where the output is

Project-local anchor, in the directory cowork was launched from:

```
.cowork/session.json          # or session.<uuid>.json
```

Holds `session_uuid`, team + per-role config, current phase
(`scouting`/`planning`/`building`), each role's controller session id, the
versioned context, and reviewer hash-gate baselines.

Per-session artifacts, keyed by that UUID (override root with
`COWORK_SESSIONS_ROOT`):

```
~/.cowork/sessions/<session_uuid>/
  scout.intel.json / scout.intel.md      # scout output
  scout-review.json                      # scout-reviewer verdict (latest only)
  planner.plan.json / planner.plan.md    # the plan
  planner-review.json                    # planning-advisor verdict
  builder.status.json                    # builder state
  builder.summary.md                     # human-readable build summary
  builder-review.json                    # build-reviewer verdict
  scores.json                            # aggregate peer-eval
  identities.json                        # tool + model + session id per role
  measurement.json                       # what --report renders
  trace.jsonl                            # orchestration trace (metadata only)
```

To check on a run in flight or explain what happened, read `trace.jsonl` and the
artifacts — not the terminal transcript. For deep session forensics use the
`cowork-debug` skill.

**After any run that included the builder, inspect the working tree yourself**
(`git status`, `git diff`). cowork commits nothing; the edits are sitting there
uncommitted.

## Status values a lead role writes

- `working` — still going.
- `needs_input` — the role asked a question, recorded in
  `result.pending_question`. Under `--headless` this does not happen: leads
  record an assumption and proceed.
- `ready_for_review` — artifact finished; the paired reviewer runs, then the
  human gate (or, headless, auto-progression on consensus).

Mid-planning the planner can hand back to the scout, and mid-building the
builder can hand back to the planner, with a handoff note. A killed run resumes
into its persisted phase without re-running earlier roles.

Trace events `stale_noop` / `stale_noop.unresolved` mean a lead's turn ended
**without its artifact changing on disk**: cowork re-nudges once
(`stale_noop`), and if the follow-up turn still changes nothing
(`stale_noop.unresolved`) the headless run ends. Before blaming the model,
check whether the role's writes are being **denied** — inspect the
controller's own session log for write-tool errors (for opencode:
`~/.local/share/opencode/opencode.db`). A role whose canonical writes are
blocked may have delivered a complete artifact to a fallback location such as
`~/.local/share/opencode/tool-output/`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | ran to completion, or cancelled cleanly / nothing to do |
| 1 | preflight failed (missing dep or controller CLI) — the message lists what |
| 2 | usage error: bad `--team`/`--config`, `--headless` without context, `--worktree` outside a git tree, controller-policy violation or unreadable policy, worktree creation failure |
| 130 | interrupted (Ctrl-C) or stdin closed at a prompt — **usually means an agent ran an interactive path** |

Exit 130 from a non-interactive invocation is the signature of a missing
`--headless` or a gate waiting on a human. Do not retry the same command;
add `--headless` (with context) or hand the session to the user.

## Operating notes

- **Runs are long.** A full headless scout → plan → build spawns real controller
  CLIs doing real work. Launch it in the background and check artifacts /
  `trace.jsonl` rather than blocking on it.
- **Do not nest.** Cowork roles refuse controller-native child agents by design
  (`Agent`/`Task` dispatches are denied and recorded). Never invoke `cowork`
  from inside a cowork role.
- **`cwd` decides where the session lands.** Run from the repo root you mean.
- **Confinement is instruction-level plus a broker/kernel boundary, not a
  promise of a sandbox.** Writable scope is the selected worktree, the acting
  role's declared outputs, and its private state. On Linux, opencode roles do
  not get the per-action broker receipts or OS write boundary that macOS
  claude/codex roles get.
- Run `cowork --check` first when a run fails at launch; it names the missing
  piece.
