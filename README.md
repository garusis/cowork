# cowork

`cowork` is a terminal command that assembles a team of CLI-driven roles, spins
up the controller CLI you pick for each role (`claude`, `codex`, or
`opencode`), and bridges that CLI's conversation straight to you. Every role
can also pin a model and a thinking-effort level (and, with opencode, the
provider — it is embedded in the `provider/model` id).

This release implements the **foundation** and the **first two phases**:

- the entry flow (choose your team, configure each role, give context),
- the **scouting phase** — the **scout** (a context gatherer that explores the
  work and confirms a solid starting point) paired with the **scout-reviewer**
  (a critical reviewer that checks, before anything reaches you for approval,
  that the scout's questions, assumptions, and discoveries are actually aligned
  with the goal), and
- the **planning phase** — the **planner** (turns the approved intel into an
  implementation plan, delivered as a machine-readable plan JSON plus a
  human-first plan markdown) paired with the **planning-advisor** (a critical
  reviewer of the plan with the same verdict semantics as the scout-reviewer),
  and
- the **building phase** — the **builder** (executes the approved plan by
  editing the repository, verifies the changes, and leaves them in your working
  tree) paired with the **build-reviewer** (a critical reviewer that checks the
  builder's working-tree diff against the plan, with the same verdict semantics
  as the other reviewers).

Phases form a **loop**, not a one-way chain: approving the scout's intel chains
straight into planning in the same run, approving the plan chains into building,
and a user-confirmed hand-back can run either edge backward — the planner back
to the scout, or the builder back to the planner (see
[Phases and the hand-back](#phases-and-the-hand-back)). Approving the build ends
the run; cowork makes no git commit and opens no PR.

## How it works

`cowork` is a standalone executable that owns your terminal. When you run it:

1. **Choose your team.** A checkbox menu of roles (`scout`, `scout-reviewer`,
   `planner`, `planning-advisor`, `builder`, `build-reviewer`), all checked by
   default. Space toggles, Enter confirms.
2. **Configure each role.** One screen: the current config is shown as a table
   (controller · model · effort · permissions · mode) and the menu is
   "✓ start with this config" (the default — one Enter accepts everything),
   one entry per role, and "← back: change team", which reopens the team
   checkbox with your current picks checked — role edits survive the round
   trip (a role dropped and re-added resets to its defaults). Picking a role
   walks a short edit —
   controller (`claude`/`codex`/`opencode`) → model → thinking effort →
   access (`yolo` / `safe` / `read-only`) — and returns to the same screen.
   Model lists are discovered live (preloaded once when this screen opens):
   claude from the public [models.dev](https://models.dev) catalog (keyless,
   full ids newest-first), codex from `codex debug models` (visible models in
   the CLI's own flagship-first order, with the effort picker narrowed to the
   chosen model's supported levels). For opencode the model pick is two steps:
   provider first (discovered live from `opencode models`, so only providers
   you have credentials for appear), then that provider's models. If discovery
   fails (offline, CLI missing), the picker silently falls back to the curated
   presets (claude aliases `opus`/`sonnet`/`haiku`, codex default/custom).
   Model and effort default to the controller CLI's own settings; every picker
   has a `custom…` free-text escape hatch.
3. **Give context.** Type/paste the files/code/intent the work needs.

The interactive UI uses [rich](https://github.com/Textualize/rich) (streaming
markdown + panels), [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit)
(multiline input), and [questionary](https://github.com/tmbo/questionary) (menus +
confirm). For tests and automation there is also a non-interactive **args path**
(`--team`/`--config`/`--context`) that skips the menus entirely (and needs none of
those packages) — see [Usage](#usage).

`cowork` then runs a **preflight** check and spins up the first role (`scout`)
using the controller you chose, bridging its live conversation to your terminal.

### The bridge

The three controllers are driven differently because their non-interactive
modes differ:

- **claude** runs as a single persistent duplex process
  (`claude -p --input-format stream-json --output-format stream-json`). Your
  typed lines are framed as stream-json user messages on stdin; the assistant's
  output streams back on stdout. A blank line ends the session.
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
(one source of truth, no re-inference). Reviewer findings and questions reach the
user in the lead role's **own voice** — the lead reads the review file by path
and relays it, so the single-voice behavior is unchanged. **Direct user
messages to the active lead may remain inline**, both for the initial turn and
later answers/revision feedback. Every cross-role re-delivery of that shared
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
user turn, static role instructions, and user-facing lead follow-ups. Raw
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
- **yolo off has no interactive approval relay** in this release: a tool the
  permission/sandbox level does not auto-allow is denied and surfaced to you as
  an error (the run does not hang). `scout`'s defaults are plan + yolo, where
  this never triggers.
- opencode has **no OS sandbox**; its agent permission rules are the only
  guardrail, and in a headless run any rule that resolves to `ask` is
  auto-rejected by opencode (acts as a hard deny — the run never hangs). Its
  plan mode is "no edits, no shell" rather than codex's read-only-commands
  sandbox.

### Safety

With yolo on, claude runs with `--dangerously-skip-permissions`, codex with
`--dangerously-bypass-approvals-and-sandbox`, and opencode with `--auto` — all
bypass approval/sandbox guards. Run `cowork` in a trusted/isolated workspace.

## Requirements

- Python 3.9 or newer.
- The interactive UX uses three pip packages — **rich** (streaming markdown +
  panels), **prompt_toolkit** (multiline input), **questionary** (menus + confirm).
  Install them into the **same interpreter** `./cowork` runs under (its shebang is
  `#!/usr/bin/env python3`):

  ```bash
  python3 -m pip install -r requirements.txt
  ```

  Use `python3 -m pip`, not a bare `pip` (often absent) or a `pip3` from a
  different Python — installing into the wrong interpreter leaves `./cowork`
  reporting the packages as missing. Only the interactive flow needs them; the
  non-interactive args path uses a plain readline/print fallback and needs none.
- The controller CLIs you intend to use, on your `PATH`:
  - **Claude Code** — `npm install -g @anthropic-ai/claude-code`
  - **Codex CLI** — `npm install -g @openai/codex` (Node 18+) or
    `brew install --cask codex`
  - **opencode** (optional) — `curl -fsSL https://opencode.ai/install | bash`,
    `npm install -g opencode-ai`, or `brew install sst/tap/opencode`; then
    authenticate providers with `opencode auth login` (the provider/model
    picker lists whatever `opencode models` reports)

`cowork --check` reports exactly which of these is missing. During a normal run,
Python and interactive UI packages are checked up front, while controller CLIs
are checked when the role that needs them is about to launch; that lets a saved
session offer controller switching instead of failing before the phase starts.

## Install

Clone this repository into a local tool directory and run the installer:

```bash
git clone <repo-url> ~/.local/share/cowork
cd ~/.local/share/cowork
./install.sh
```

`install.sh` creates a dedicated `.venv` for the pip packages (immune to
PEP 668 / Homebrew "externally-managed-environment"), adds the checkout dir to your
`PATH` via `~/.zshrc`, makes `cowork` executable, links bundled skills from
`./skills/` into both `~/.claude/skills` and `~/.codex/skills`, and runs the
preflight to report any missing controller CLIs. It is idempotent — safe to
re-run.

Open a new terminal (or `source ~/.zshrc`) once, then run `cowork` from **any
folder** — the launcher self-bootstraps the venv, the project-local
`.cowork/session.json` anchor lands in the current directory, and the session's
produced artifacts live under `~/.cowork/sessions/<session_uuid>/`. Re-verify
anytime with `cowork --check`.

> Manual alternative: `python3 -m pip install -r requirements.txt` then run
> `./cowork` from this directory.

## Usage

### Interactive

```bash
./cowork            # run the full flow: team -> config -> context -> scout
./cowork --check    # run the preflight dependency check only
```

- **Team step:** a questionary checkbox menu (all roles preselected). Space
  toggles, Enter confirms.
- **Config step:** one screen. The current config is a table
  (controller · model · effort · permissions · mode); the menu default is
  "✓ start with this config" (one Enter continues), and picking a role instead
  walks controller → model → effort → access for that role and returns to the
  table. Access is a single pick: `yolo` (full access), `safe` (edits only), or
  `read-only` (plan mode).
- **Context step:** a multiline prompt_toolkit editor (Enter sends; Ctrl+J /
  Alt+Enter insert a newline).

### Non-interactive (args path)

Skip the menus entirely — useful for tests and automation. Providing any of
`--team`, `--config`, or `--context`/`--context-file` switches off the
interactive UI (and none of the pip packages are required):

```bash
# scout only, codex controller, no yolo, implement mode, context inline
./cowork --team scout --config "scout=codex,no-yolo,implement" --context "Refactor the auth module"

# builder on opencode with a pinned provider/model + effort
./cowork --config "builder=opencode,model=anthropic/claude-sonnet-4-5,effort=max" --context "…"

# pin the claude scout's model and thinking effort
./cowork --config "scout=claude,model=opus,effort=high" --context "…"

# context from a file (or '-' to read stdin)
./cowork --team scout --context-file ./brief.md
echo "the brief" | ./cowork --team scout --context-file -
```

- `--team` — comma-separated roles (default: all). Unknown roles error out.
- `--config ROLE=opt,opt` — repeatable; tokens are any of
  `claude|codex|opencode`, `model=<id>`, `effort=<level>`, `yolo|no-yolo`,
  `plan|implement`. `model=default`/`effort=default` reset to the controller
  CLI's own setting; opencode model ids are `provider/model`. E.g.
  `--config "scout=claude,model=opus" --config "scout-reviewer=codex,model=gpt-5-codex"`
  runs a scout/reviewer pair on two specific models so their evaluation scores
  and token consumption can be compared (see
  [Evaluation traceability](#evaluation-traceability)).
- `--context TEXT` / `--context-file PATH` — initial context (`-` = stdin).
- `--session-file PATH` — use a specific session store (default
  `./.cowork/session.json`).
- `--no-session` — do not read or write the session store.
- `--switch-controller ROLE=CONTROLLER` — update one current-phase role in an
  existing saved session to `claude`, `codex`, or `opencode`, then continue
  that session. **Repeatable**: every switch in one invocation is applied as a
  single all-or-nothing update. A switch resets the role's model/effort pins
  (they are controller-specific). Examples:

  ```bash
  ./cowork --switch-controller planner=codex
  ./cowork --session-file .cowork/session.<uuid>.json --switch-controller builder=claude
  ```

  The role must be in the effective current phase pair: `scout` or
  `scout-reviewer` during scouting, `planner` or `planning-advisor` during
  planning, and `builder` or `build-reviewer` during building. `--session-file`
  targets one saved session; `--resume` can be used to pick an existing session
  interactively. `--switch-controller` requires saved team/config state and
  cannot be combined with `--team`, `--config`, `--new`, `--no-session`,
  `--check`, or `--report`.
- `--allow-controllers LIST` — restrict a saved session to the named controllers
  (`--allow-controllers claude,codex`), or lift an existing restriction with
  `--allow-controllers all`. Combines with `--switch-controller`; the policy
  change and every role move are validated and persisted together before
  anything resumes. Same conflict rules as `--switch-controller`. See
  [Controller policy](#controller-policy).
- `--worktree [NAME]` / `--wt [NAME]` — before scouting, spin up a small agent
  that creates a git **worktree** for the launch repo and runs the rest of the
  session inside it. The agent follows the repo's documented worktree
  convention (and any documented setup it states — e.g. a per-worktree venv and
  dependency install); a repo with no convention gets a sibling
  `../<repo>-worktrees/<name>` folder. `NAME` is optional (default
  `cowork-<short session id>`); the branch is the same name off HEAD. Requires
  launching **inside a git work tree** — otherwise it fails fast (rc 2). One
  explicit base repo only (no multi-root). cowork validates the created worktree
  (it must exist and be git-registered) before switching into it. On a name
  clash, an explicit `NAME` stops (or reuses an exact match) rather than
  silently renaming; an auto name picks a free numbered variant.
- `--wt-controller claude|codex|opencode` — controller for the worktree agent
  (default `claude`).
- `--headless` / `--auto` — drive the whole scout → plan → build flow with **no
  human gates**: leads never block (they record an assumption and proceed
  instead of asking), reviewers review with what they have (a would-be user
  question becomes a `revise`), and each phase ends on reviewer consensus or the
  review-round cap (accept-with-dissent at the cap, never a hard fail).
  Auto-progression happens **only** with this flag — without it every gate
  blocks exactly as today. Headless **requires** initial context up front
  (`--context`/`--context-file`, else a hard error).

  ```bash
  # provision a worktree, then run the whole flow headless inside it
  ./cowork --worktree --headless --context "Add a --dry-run flag to the CLI"

  # explicit worktree name + a specific worktree-agent controller
  ./cowork --worktree my-feature --wt-controller codex --context "…"
  ```

  Resume note: with `--worktree`, cowork's own session record stays in the
  folder you **launched from** (the per-session assets are home-dir keyed and
  always found). To resume that session later, relaunch from the **same launch
  folder** (or point at it with `--session-file`) — resuming from *inside* the
  worktree will not find it.

Defaults per role (model/effort default to the controller CLI's own setting):

| Role | Controller | Model | Effort | yolo | Mode |
| --- | --- | --- | --- | --- | --- |
| scout | claude | default | default | on | implement |
| scout-reviewer | codex | default | default | on | implement |
| planner | claude | default | default | on | implement |
| planning-advisor | codex | default | default | on | implement |
| builder | claude | default | default | on | implement |
| build-reviewer | codex | default | default | on | implement |

Roles default to **implement** mode (write-enabled). The user-facing roles are
kept in their lane by **role-spec guardrails**, not by plan mode — the scout may
write only its two intel files (JSON + markdown), the planner only its two plan
files; the builder edits the repository freely to execute the plan but makes no
git commit (and also emits a markdown build summary). The reviewers each write
only their own review file (see below). This is instruction-level confinement,
not an OS sandbox.

All three phases — scouting, planning, building — run in this release. A
**fresh** team without `scout` exits with a note: every run begins with
scouting, so the scout has to be on the team (a session already past scouting
resumes into its saved phase without re-running earlier roles).

## Sessions

`cowork` persists each session in a project-local **`.cowork/session.json`** in
the directory you run it from (add `.cowork/` to your `.gitignore`). It stores:

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
- the **team** and **per-role config** — so the next run in the same directory
  does not re-ask them (you'll see `using saved session config`);
- the **current phase** (`scouting`/`planning`/`building`) — so a killed run
  resumes into the phase it was in (see
  [Phases and the hand-back](#phases-and-the-hand-back));
- each role's **CLI session id** (claude `session_id` / codex `thread_id`) —
  scout, scout-reviewer, planner, planning-advisor, builder, and build-reviewer
  — so a run that is killed can be **resumed where it left off**, with the
  reviewers keeping their accumulated review context too;
- the **current session context**, versioned (see below); and
- each paired reviewer's **last-approved hash-gate baseline** (the artifact
  composite it last approved, scoped by phase epoch + acknowledged context
  revision) — so the [reviewer skip on unchanged artifacts](#reviewer-skip-on-unchanged-artifacts-hash-gate)
  survives a resume.

On the next run, if a saved session exists, `cowork` reuses the config and
**auto-resumes** the saved CLI sessions (`claude --resume <id>` /
`codex exec resume <thread_id>`). The claude session id is pinned up front
(`--session-id <uuid>`) and saved immediately, so even an instant kill is
resumable.

On a resume, `cowork` **skips the goal prompt and continues automatically** —
it sends "Continue the session." so the current phase's role picks up where it
left off with its prior context. To **redirect** the resumed session to a new
task, pass `--context "…"`; to **start fresh**, use `--no-session` (or delete
`.cowork/session.json`). `--session-file` points at a different store. Changing
the saved config is out of scope for now — delete
`.cowork/session.json` to start fresh.

### Controller switching

If the active controller for the current role is unavailable or stops making
progress, cowork can switch that role to the other controller and keep the
session moving. The cowork phase, artifacts, shared context, review baselines,
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

The switch option appears at these recovery points:

- missing active controller executable at role launch;
- controller start/resume/probe failure;
- lead-role turn failure without status-artifact progress;
- the stuck lead-role gate; and
- the reviewer/advisor failure gate.

The same state update is available from the CLI:

```bash
./cowork --switch-controller planner=codex
./cowork --session-file .cowork/session.<uuid>.json --switch-controller builder=claude
```

Before committing the switch, cowork checks the target controller executable and
uses the existing install guidance if it is missing. When the target is Claude,
cowork also runs the stream-json probe for that role's prompt/mode/permission
settings. A failed target check leaves the current controller unchanged.

V1 is manual only. There is no automatic rate-limit failover, no migration of
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
./cowork --allow-controllers claude,codex \
         --switch-controller builder=codex \
         --switch-controller build-reviewer=claude

# lift the restriction again
./cowork --allow-controllers all
```

`--switch-controller` is **repeatable**, and `--allow-controllers all` removes
the restriction entirely (the session file goes back to its pre-feature shape).

**A switch without `--allow-controllers` never changes the allowed set.** It is
still checked *against* it — moving a role to a controller the session forbids is
rejected — but the saved allowed list is left byte-for-byte as it was. The mirror
also holds: a policy change never reassigns a role on its own.

The interactive equivalent is **"Change this session's controllers"**, the third
entry in the resume-or-new menu. It lets you pick the allowed set and remap the
current phase's roles, ending in one confirmation. It goes through the same code
path as the flags, so the same request produces the same saved session either
way — including the case where you confirm without touching the pre-checked
boxes, which leaves the policy untouched exactly like the plain switch command.

**Ordering and the all-or-nothing guarantee.** cowork validates the whole
proposal first — the allowed set, every role move, and whether the current phase
would still be compliant — then checks that the target controllers are installed
and working (only ever checking controllers that will be permitted once the
command finishes), then persists the policy and every role move as one
**single write**. The whole update completes **before anything resumes**: no role is
dispatched until that one write has landed. If any part of the proposal is wrong
you get one clear message, a non-zero exit, and a session file that is
untouched — the update is **all-or-nothing**.

**Current phase vs. the rest.** Roles in the **current phase** must comply in the
same command — a role left on a now-forbidden controller is a hard error telling
you to add the matching `--switch-controller`. Roles from finished phases are
only reported as a **warning**, left exactly as they are, and blocked if they are
ever reached.

From then on every attempt to start a controller consults the policy first, for
leads, paired reviewers, resumes, recovery relaunches and the `--worktree` agent
alike. A blocked attempt never starts the process — not even a probe or an
opencode agent-file write — and prints which role wanted which controller and
what the session allows. Under `--headless` it exits cleanly instead of waiting
for a human.

**Recovery gates** stop offering a vague "alternate controller" on a session that
carries a policy and list the specific controllers it still permits. When the
policy leaves **no eligible controller**, the switch option disappears and the
reason is stated; retry and end remain.

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
retry budget can be reconstructed from the trace alone. A foreground drain screen
that fails to render or prompt records `eval.gate.error` rather than failing
silently.

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
- **One final suite, bound to the reviewed candidate.** The plan's inventory
  names at most one `kind: final_suite` entry, always last; readiness requires
  every approved command green, evidence present, the final suite run exactly
  once, and the transaction's own captured manifest/index still matching what
  was actually reviewed.
- **Bounded evidence, never a silent rerun.** If a command's terminal result is
  slow to land, Cowork polls the same pre-minted attempt for a bounded number
  of attempts; past that bound the attempt is recorded `unresolved`/`absent`
  and polling stops — delayed evidence is never resolved by launching a
  replacement command.

Legacy (schema-1) plans — `{label, command}` only, no `execution_mode`/`kind`
— are still accepted: they run isolated, keep their historical
whole-inventory readiness comparison, and report their final-suite guarantee
as `legacy_unknown` rather than inventing one. See `roles/planner.md` for the
schema-2 inventory format plans should write going forward.

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

**When a drain is genuinely going to block the run, it says so.** You get a
distinct screen naming the governing policy and four honest counts —
pending/running, completed, held/skipped and terminal/failed — with four safe
choices: **continue** the eligible work, **hold** it for later, **retry**
eligible failed work, or **end** without scoring. Holding, ending, dismissing
the prompt and walking away all leave every queue entry intact, and none of them
records a success for work that did not succeed. Superseded work is reported
inside held/skipped, never as completed — retiring a superseded candidate scores
nothing. The screen never uses phase-approval wording, because evaluation-blocked
time is not a phase you approved. Under `off`, with an empty queue, or with
nothing left to do, there is no prompt at all — just a short status line, exactly
as quiet as before. Headless runs — and any run that is not attached to a real
terminal, such as a scripted or piped one — get identical bookkeeping and
identical counts, and continue without prompting: there is nobody there to
answer, and a drain that used to be silent must never be able to stall a
non-interactive run.

**Failures are classified and retries are bounded.** Every attempt is recorded
*before* it runs, so a crash costs at most one attempt instead of looping
forever. A transient failure gets a second attempt; missing or unparseable
evaluator output, a permanent failure and an unusable entry each stop after the
first — a retry cannot change any of those. Once the budget is spent the entry is
**terminal**, and it stays terminal across a resume with its attempt count,
failure class and history intact. Only an explicit **retry** reopens it, and that
retry links back to the earlier attempts rather than overwriting them.

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
| `held` | held by policy (`off`) or by you; visible, durable, deliberately unscored |
| `drained` | scored successfully — the only state that counts as completed |
| `retired` | superseded by a later round; never scored, and **not** a failure |
| `terminal` | its retry budget is spent; needs an explicit retry to reopen |
| `retried` | you explicitly reopened a terminal entry; its earlier history is preserved |

`unverifiable` above is unchanged by any of this and is still **not** a failure:
such an entry drains successfully, costs no retry budget, and is simply excluded
from the aggregates — it is reported beside the completed count rather than
folded silently into it.

**Pricing ships as a schema with an empty snapshot.** Real prices baked into a
repository are stale by construction, so by default every model resolves to
`unpriced` and nothing claims to be money. Every cost field carries the schema
version and snapshot id that produced it.

### Time

Productive, review, evaluation, verification, recovery and **time spent waiting
on you** are shown separately. The waiting figure comes from timing every prompt
that actually blocks on a human — all six of them, from the team menu to the
approval gates — and never from guessing at gaps between events. A gap is equally
an ingestion stall, a controller hang or a suspended process; it is not evidence
of a person.

### Old sessions

Sessions recorded before this layer existed still report. They say plainly which
records they predate: turn ids are synthesized (and labelled as such), user-wait
is `unknown` rather than inferred, and every gap is listed in `record.incomplete[]`
with its reason.

### Context revisions

Explicit context (`--context`/the goal prompt) is a **session-wide event**, not a
one-off prompt to the scout. It is persisted as the current session context with
a monotonically increasing **revision** (`{text, hash, revision, source}`), and
every role records the last revision it acknowledged
(`last_context_revision_seen`). The invariant:

> Any role invoked after context is provided must receive the current context,
> unless it has already acknowledged that revision.

Fresh role sessions get it in their prompt naturally. **Resumed** sessions that
have not acknowledged the current revision are woken with an explicit
context-update block — "new user context was provided … treat this as the
current task context, keep prior session knowledge only where it remains
compatible" — so redirecting a resumed session keeps continuity without any role
quietly operating on stale assumptions. A role acknowledges a revision only after
it actually ran against it; a crash before that re-delivers the block on the next
resume.

## The scout role

`scout` doesn't gather blindly — it runs a short, consensus-building dialogue to
find the right thing to build, the way a good product conversation goes:

1. **Recon** — reads/searches the repo to ground itself.
2. **Clarify** — asks you the scope-defining questions (objective, definition of
   done, intended behavior). It asks blocking questions rather than guessing.
3. **Propose options** — when there are tradeoffs, it lays out concrete options
   *with a recommendation* instead of just asking open questions.
4. **Make the goal measurable** — turns the agreed goal into explicit
   **success criteria** (1–5, each with a concrete measurement, an expected
   result, and a must/should tier — the measurement fitting what's being
   built: a bugfix by its reproduction, a feature by observable behavior, a
   perf goal by a metric vs a baseline, a refactor by invariants + the suite).
   A goal it can't make measurable becomes a blocking question, never an
   assumption; the criteria freeze at approval.
5. **Iterate** — refines with you until you reach product consensus.
6. **Hand off** — writes its intel and marks it ready for review.

Its **only write targets** are its two intel files,
`~/.cowork/sessions/<session_uuid>/scout.intel.json` (machine source of truth +
status channel) and `scout.intel.md` (the human-first rendering you review at the
gate, like the planner's `plan.md`); it must not touch any other file
(reading/searching the whole repo is encouraged). Full spec:
[roles/scout.md](roles/scout.md).

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
"done" the user approves (rendered as a dedicated **Success criteria** section
in `scout.intel.md`) and the contract the plan must cover. Intel that reaches
review without a non-empty list gets an orchestrator **structural auto-finding**
in the reviewer's brief (structure only — quality judgment stays with the
reviewer).

cowork reads only `status`. The asked questions and your answers are recorded in
`result.clarifications`. If no `planner` role is on the team, the scout also
includes a lightweight plan in `result`. Alongside the JSON, `scout.intel.md` is
a readable rendering of the same intel — the scout-reviewer reviews both and
checks the markdown stays consistent with the JSON, and the scout's approve gate
points you at the `.md`.

## The scout-reviewer role

With `scout-reviewer` on the team, every time the scout marks its intel
`ready_for_review`, cowork **deterministically** runs the reviewer **before**
showing you the approve gate — orchestrator control flow, not a model deciding
when to review. The reviewer starts from the **same context the scout was given**
(the shared context + the team framing + the scout's current intel; never the
scout's own write-target brief) and critically checks objective alignment,
**goal measurability** (each success criterion binary-decidable from its
stated measurement, the measurement fitting what's being built, the `must`
set covering the agreed goal), whether blocking product questions were buried
as assumptions, whether cited discoveries hold up, and completeness — it is
instructed to find gaps, not to rubber-stamp.

It writes a verdict to its own file, `~/.cowork/sessions/<session_uuid>/scout-review.json`
(its **only** write target, cleared before each pass so a stale verdict is never
read back):

- **`approve`** — the intel proceeds to your normal approve/revise gate.
- **`revise`** — the findings are handed back to the scout as its next turn; the
  scout fixes the intel and re-proposes. Bounded to **2 rounds** per
  `ready_for_review`; if the reviewer still hasn't approved, the gate is shown to
  you anyway **with the reviewer's unresolved notes attached** (it never
  hard-blocks). A missing or malformed verdict counts as `revise` — the safe
  non-approving default.
- **`needs_user`** — the reviewer found an unresolved **product** question only
  you can answer. The scout relays it to you **in its own voice** (it may
  rephrase, but must not change the meaning or drop context) and waits for your
  answer.

**Single voice:** the scout is the only role that talks to you. The reviewer is
not a secret — you'll see a small `reviewed: ...` status marker each time it runs
— but its raw output never interleaves into the conversation; its questions
reach you only through the scout's faithful relay. Full spec:
[roles/scout-reviewer.md](roles/scout-reviewer.md).

The reviewer is a **persistent session** like the scout: its CLI session id is
saved and resumed on every pass and across cowork resumes, and it participates in
[context revisions](#context-revisions) — a resumed reviewer that hasn't seen the
latest `--context` gets it as an explicit update block on its next pass.

### The review gate (Ask a question / Request changes / Approve & finish / Stop)

Every interactive review gate spells out the consequence of each choice **as a
label**, before you pick it, so nothing is a surprise. The approve wording is
phase- and team-aware: the word **finish** appears only when approval actually
ends the run.

**Approve is never the highlighted choice**, and nothing approves by omission —
see [Gates ignore input typed before they were
ready](#gates-ignore-input-typed-before-they-were-ready) below.

When the scout marks its intel — or the planner its plan — `ready_for_review`,
the gate gives you these choices, in this order:

- **Ask a question** — the highlighted choice, because it changes nothing. Put a
  plain question to the role. It answers conversationally in chat and **leaves
  the artifact exactly as it is** (the label reads "the intel/plan stays
  as-is"): no edit, no status flip, and — because nothing changed on disk — no
  re-review (the
  [hash-gate](#reviewer-skip-on-unchanged-artifacts-hash-gate) skips the paired
  reviewer). You land right back at the same gate, so you can ask as many
  questions as you like for free before approving or requesting changes. If a
  question genuinely surfaces new work, the role can still edit the artifact and
  reopen — and then a re-review is correct.
- **Request changes** — the role revises and you'll be asked for feedback; the
  label names the resuming role ("the scout/planner/builder revises").
- **Approve** — accept the work. With a downstream role on the team the label
  previews the transition (**continue to planning** at the scout gate when a
  planner is on the team, **continue to building** at the planner gate when a
  builder is on the team); otherwise it reads **Approve & finish** and names the
  deliverable (intel or plan).
- **Stop** — the last choice; exits the phase cleanly **without approving and
  without requesting changes** (see the Stop wording below).

The **builder** gate is a 3-way select on a TTY, in the order **Request
changes** / **Approve & finish** / **Stop**: Request changes (the highlighted
choice) has the builder revise from your feedback; Approve & finish previews
finishing so you can review your working tree; Stop exits cleanly. It has no
"Ask a question" choice. On the preview-less compatibility path the gate is a
plain **Approve & finish?** confirm, which now defaults to **No**, so pressing
Return alone never approves.

**Feedback is never a sign-off.** If you pick Request changes and then submit
nothing — a blank line, whitespace only, a cancelled editor, or end-of-input —
you land back at the gate rather than finishing, exactly like a blank question
has always behaved. The only way to approve is to choose approve.

When the reviewer's round cap is reached without approval, the **dissent** gate
offers four choices, with continued iteration as the safe default.
**Keep iterating** hands the reviewer's findings back to the resuming role;
**Tell it what to do** sends your instructions to that role; **Approve anyway**
accepts despite the reviewer (the label reads "Approve anyway — continue to
planning/building" on a non-terminal gate and "Approve & finish anyway" only
when approval ends the run); **Stop** exits cleanly.

The **Stop** label follows persistence. For a saved session it reads **Stop —
session remains resumable**: the phase ends without approving, and the saved
state is left intact so you can pick the run back up later (see
[Sessions](#sessions)). Stopping never approves, never queues a revision,
and never advances the phase.

Under `--no-session` nothing is persisted, so the same Stop choice instead reads
**Stop — end this run without approving**; the clean-exit outcome is identical.

Cancelling any preview-enabled review menu (including a single **Ctrl-C**) takes
that same Stop path. It never silently becomes "Request changes" or "Keep
iterating".

Off a TTY (scripted/non-interactive runs) the historical contract is unchanged
and there is no Stop choice: a blank line finishes, any other text requests
changes. The question path is scoped to the scout and planner gates only.

### Gates ignore input typed before they were ready

A role turn can run for minutes, and anything you type while it runs goes
nowhere: it is **discarded**, always. A gate becomes active only once it has
finished drawing, and only what you type *after* that can select anything.

**Whenever cowork sees that input waiting**, the gate shows a short notice
telling you input was ignored and should be entered again. The notice carries no
trace of what you typed — **at most a count** of how many characters arrived.
cowork never reads that input, never keeps it, never records it in the trace,
and never replays it into a later prompt.

Even that count is best-effort: cowork asks the operating system how many bytes
are waiting rather than looking at them, and on a terminal that cannot answer
the question the notice simply appears without a number. Nothing else changes —
the input is discarded either way. The same applies to the private trace record,
which carries the gate name and the count when there is one, and no count at all
when there isn't.

What your **terminal** does with it is a separate matter. While a role turn is
running there is no cowork editor attached, so the terminal echoes your
keystrokes itself and they stay on screen. Seeing your text sitting there does
not mean cowork received it — it didn't, and it never will. Type it again at the
gate.

The discarding is absolute; the notice is best-effort. cowork checks the input
queue and then clears it, and those two steps cannot be made one operation, so a
keystroke landing in the sliver between them is **still discarded and still
cannot select anything** — it just goes unmentioned. cowork never warns
speculatively either, so a notice you do see always refers to input that really
was queued.

Two cases deliberately fail safe rather than proceeding. If cowork cannot clear
the leftover input at all — the terminal's own queue, the editor library's
replay buffer, or the open widget's key buffers — it says so and **refuses to
run the gate**, because it can no longer tell old keystrokes from new ones, and
the phase ends without approving. The same happens if input keeps arriving
through several re-opens. The session stays resumable in both cases.

There is no environment variable and no flag that turns any of this off.

Pasting into an **open** answer box is unaffected: a multi-line paste still
lands whole as text and still needs an explicit Enter.

### Reviewer skip on unchanged artifacts (hash-gate)

So you can keep chatting with the scout (or planner) without forcing a pointless
review pass, cowork **skips** the paired reviewer when the artifact set it would
review is **byte-for-byte identical to what that reviewer last approved** in the
current phase. It is never a silent bypass: you see a `review skipped — unchanged
since last approved` marker, the prior approval is reused, and you land at your
normal approve gate. The "unchanged" check is a composite over **every** file the
reviewer sees (scout = `scout.intel.json` + `scout.intel.md`; planner =
`planner.plan.json` + `planner.plan.md`), so any edit — including a markdown-only
one — forces a full review again. Only a real prior **approve** ever seeds a skip
(a `revise`, a round-cap dissent, a `needs_user`, or a reviewer-failure skip never
does), and the baseline is tied to the phase and to the context revision the
reviewer actually acknowledged — a phase re-entry (e.g. a planner→scout hand-back)
or any newer context clears it. The hash-gate covers the **scout and planner
only**; the builder is out (its summary is a deliverable, not a skip baseline).

## The planner role

When you approve the scout's intel and `planner` is on the team, cowork chains
straight into the planning phase **in the same run**: the planner is seeded with
the approved intel JSON plus the current shared context, and becomes the single
voice you talk to. Like the scout, it runs a dialogue — it asks the decisions
only you can make (scope, behavior, tradeoffs) as they appear, and marks the
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
- `~/.cowork/sessions/<session_uuid>/planner.plan.md` — the **human-first plan** you review
  at the plan gate: TL;DR; What we're building; Key decisions; How it will
  work; What changes; How we'll know it works; Out of scope; Risks &
  assumptions. Sections stay small and scannable; when you want deeper detail,
  ask the planner — it answers conversationally from the JSON instead of
  inflating the markdown.

At the plan gate you get the same approve/decline flow as the scout's: decline
with feedback and the planner keeps revising; approve and — with a `builder` on
the team — the session **chains into the building phase**. Without a builder,
plan approval ends the run with the plan as the deliverable (a rerun resumes the
planner conversation like any other resume). The plan JSON may also carry a
`result.verification` list of `{label, command}` steps the build phase runs.
Full spec: [roles/planner.md](roles/planner.md).

## The planning-advisor role

The planning-advisor pairs with the planner exactly as the scout-reviewer pairs
with the scout: each time the planner marks the plan `ready_for_review`, cowork
deterministically runs the advisor against **both** plan artifacts before
showing you the gate. Its checks include **criteria coverage**: every intel
success criterion mapped to steps and to a verification that measures what the
criterion actually states — uncovered, mis-measured, weakened, or dropped
criteria are findings. Same verdict semantics — `approve` proceeds to your gate,
`revise` findings go back to the planner (bounded to 2 rounds, then the gate is
shown with the advisor's unresolved notes attached; never a hard block),
`needs_user` questions reach you only through the planner's faithful relay, and
a missing/malformed verdict counts as `revise`. Its only write target is
`~/.cowork/sessions/<session_uuid>/planner-review.json`, cleared before each pass. Full
spec: [roles/planning-advisor.md](roles/planning-advisor.md).

## The builder role

When you approve the plan and a `builder` is on the team, the session chains
into the **building phase**. The builder is seeded with the approved plan (JSON
+ markdown) plus the current shared context, and becomes the single voice you
talk to. Unlike the scout and planner, its write target is the **whole
repository** — it executes the plan by editing source files. Its
`~/.cowork/sessions/<session_uuid>/builder.status.json` is only a status + verification
channel (`needs_input | ready_for_review | handoff_back`, plus a
`result.verification` log), not a write restriction.

The builder keeps a **high bar for interrupting you**: routine progress and test
failures it can fix itself never reach you — it only asks when truly blocked,
when a big deviation from the plan surfaces, or when the reviewer needs a product
decision. Before marking the build ready it runs a self-audit: re-read the plan,
walk every per-file change, run each plan-listed verification command, and record
the results. At that self-audit it also emits a human-first build summary,
`~/.cowork/sessions/<session_uuid>/builder.summary.md` — what changed per file,
the verification results, and any issues/deviations — the readable surface you
review at the build gate (the status JSON stays the machine source of truth). The
build-reviewer reads the summary and **consistency-checks it against the real
working-tree delta** before it reaches you, so it can't mask the build. The
builder itself stays **out** of the reviewer hash-gate: the summary is a
deliverable, not a skip baseline. Verification is **strict** — it does not declare
the build ready while a verification command is failing for a reason it
introduced. A failure it cannot fix in the working tree (a missing dependency,
broken local tooling) is routed to **you**, not silently past the reviewer. The
builder runs **no git commit and opens no PR**: approval ends the run with the
changes in your working tree. Full spec: [roles/builder.md](roles/builder.md).

## The build-reviewer role

The build-reviewer pairs with the builder exactly as the other reviewers pair
with their roles: each time the builder marks the build `ready_for_review`,
cowork deterministically runs it before showing you the gate. Its unit of review
is the builder's **full working-tree delta** — it captures the delta itself
(`git status --porcelain` for staged/unstaged/untracked, `git diff HEAD` for
tracked changes, and it reads new untracked files directly, since plain
`git diff` misses staged and untracked files) and checks it against the approved
plan, the builder's status, and the shared context. cowork records the build's
baseline commit at building entry and **warns you if the worktree was already
dirty** (so pre-existing changes are not silently attributed to the builder).
Same verdict semantics — `approve` proceeds to your gate,
`revise` findings go back to the builder (bounded by the round cap, then the gate
shows the unresolved notes; never a hard block), `needs_user` reaches you only
through the builder's faithful relay, and a missing/malformed verdict counts as
`revise`. Its only write target is
`~/.cowork/sessions/<session_uuid>/builder-review.json`, cleared before each pass; it never
edits code — fixes go through the builder. Full spec:
[roles/build-reviewer.md](roles/build-reviewer.md).

## Phases and the hand-back

The session phase (`scouting`/`planning`/`building`) is persisted in
`.cowork/session.json`, and the flow is a **loop**:

```text
scouting ─(you approve the intel; planner on team)─▶ planning ─(you approve the plan; builder on team)─▶ building ─(you approve the build)─▶ done (run ends)
   ▲                                                    │  ▲                                                │
   └──────(you confirm the planner's hand-back)─────────┘  └──────(you confirm the builder's hand-back)─────┘
```

Mid-planning, the planner can **hand the work back to the scout**, and
mid-building, the builder can **hand the work back to the planner** — say a
foundation in the plan turns out wrong. The role writes a handoff note (what
changed, what to re-do, what to keep) and signals `handoff_back`; cowork shows
you an explicit confirmation gate. On yes, the **pre-processor's session
resumes**, woken with the handoff note, runs its full cycle again, and on your
re-approval the downstream role resumes (woken with the updated artifact to
digest) and continues. On no, the role is told and keeps working. A
`handoff_back` without a note degrades to the normal needs-input prompt — never
an implicit hand-back.

The signal contract is role-generic (any role → its pre-processor); planner →
scout and builder → planner are wired. A killed run resumes into the persisted
phase: a session mid-building re-enters the builder conversation directly,
without re-running the scout or planner. If the resumed phase's lead role is not
on the team, the resume cascades down (building → planning → scouting) to the
nearest phase whose role is present.

### Interacting with scout — the three states

Each turn, cowork streams the reply, then reads the intel `status`:

- **working** — a `scout working…` spinner fills the gap before the first token,
  then the reply renders **live as markdown** (Rich `Live`) under `scout ›` —
  length-independent, so replies taller than the screen still render. Off a
  terminal (piped/scripted), tokens stream raw with no rendering.
- **`needs_input`** — scout asked you something. The exact question is recorded
  in `result.pending_question`, repeated in the `scout needs your input` panel,
  and then cowork waits for your answer. If a role writes `needs_input` without
  a question, cowork gives it one automatic repair turn instead of showing a
  blank answer gate; a second malformed turn is called out explicitly.
- **`ready_for_review`** — scout finished the intel and posts a **summary in the
  chat**. If the scout-reviewer is on the team it runs first (you'll see a
  `reviewed: approved`, `reviewed: changes requested`, or
  `reviewed: needs user input` marker; see
  [The scout-reviewer role](#the-scout-reviewer-role)), then cowork shows the
  [review gate](#the-review-gate-ask-a-question--request-changes--approve--finish--stop)
  (**Ask a question / Request changes / Approve & finish / Stop**): approve
  ends the session; a question is answered in chat for free (no intel edit, no
  re-review); requesting changes sends another turn so you keep refining; and
  Stop exits without approving. Approve is never the highlighted choice and
  blank feedback re-opens the gate rather than finishing. Off a terminal the
  historical blank=finish / text=revise contract is unchanged.

**Input.** On a terminal each turn is a prompt_toolkit multiline editor: real line
editing (arrow keys, word-jump, paste, history) and multiline answers. A dim hint
sits right above the input line — **Enter to send · Ctrl+J or Alt+Enter for a new
line**. A **blank line re-prompts**; to stop scout before it's ready, use **Ctrl-C**
or type **`/quit`**.

About **Shift+Enter**: terminals send the same byte for Enter and Shift+Enter
unless the Kitty keyboard protocol is active, and prompt_toolkit has no Shift+Enter
key, so the portable newline keys are **Ctrl+J** and **Alt+Enter**. You can map
Shift+Enter to send Alt+Enter (ESC+Enter) in your terminal's keymap (VS Code,
iTerm2, …) to get a newline on Shift+Enter — the same approach as Claude Code's
`/terminal-setup`.

Turns are color-labeled throughout — your input as `you ›` (cyan), the role's
replies as `scout ›` (green). All of this uses rich/prompt_toolkit/questionary;
piped/scripted runs fall back to plain text and `readline`.

## Repository layout

```text
.
|-- cowork                      # executable entry point
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
    |-- cowork.py               # entry flow (questionary menus + args path) + phase loop + role orchestration
    |-- cowork_bridge.py        # flag assembly, stream-json framing, codex resume, probe
    |-- cowork_profiles.py      # private controller state + reference-only authentication reuse
    |-- cowork_action_policy.py # controller capability matrix + content-free action decisions
    |-- cowork_ui.py            # shared UX layer: prompt_toolkit input, Rich markdown/panels, color
    |-- cowork_preflight.py     # Python-version + pip-package + controller PATH checks
    |-- cowork_trace.py         # private JSONL orchestration trace writer
    |-- cowork_state.py         # .cowork/session.json store (config, phase, session ids, context revisions, verdicts)
    |-- cowork_measure.py       # builds the AUTHORITATIVE measurement record; the only reader of raw sources
    |-- cowork_report.py        # PURE renderer of that record (computes nothing; refuses a raw source)
    |-- cowork_ingest.py        # read-only, fallible ingestion of the controllers' own session logs
    |-- cowork_ledger.py        # the sole writer of ledger.jsonl and sole minter of stable IDs
    |-- cowork_eval.py          # evaluation policy, sealed envelopes, the durable queue, isolation
    |-- cowork_pricing.py       # versioned normalization + pricing schema and snapshot loader
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

Writable scope is exactly the selected worktree, the acting role's declared
outputs, and its private temp/controller-state directories. Deletes require an
exact owned and recoverable target. A generated operating-system sandbox
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
controller, a bounded authentication-method category, success, duration, and
error type.

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

Run the fast unit suite (fakes only — no CLIs spawned, no API calls):

```bash
python3 -m unittest scripts/test_cowork.py
```

The unit tests cover flag assembly, preflight (including the pip-package check),
the menus (via injected ask-callables — no questionary prompt or TTY needed), the
non-interactive args path, the claude stream-json probe, event parsing, denial
handling, the plan-only fallthrough, the phase loop (scout→planner chaining, the
hand-back round trip, resume-into-planning, the scout-less refusal), the planner
loop and planning-advisor gate (via injected fakes), and that `cowork` stays
self-contained. Tests that exercise the real rich/prompt_toolkit libraries skip
when the packages aren't installed (like the `COWORK_LIVE` tests); install
`requirements.txt` to run them. The real terminal experience (live markdown, the
editor, panels) is a manual check — as is one live end-to-end phase loop:
scout → approve → planner → hand back → scout → approve → planner → approve.

### Live integration tests

To verify the real contracts against the installed CLIs (catching flag/version
drift), set `COWORK_LIVE=1`. These spawn real `claude`/`codex` processes, make
real API calls, and are slow:

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
