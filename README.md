# cowork

`cowork` is a terminal command that assembles a team of CLI-driven roles, spins
up the controller CLI you pick for each role (`claude` or `codex`), and bridges
that CLI's conversation straight to you.

This release implements the **foundation** and the **first role**:

- the entry flow (choose your team, configure each role, give context), and
- the **scout** role — a context gatherer that explores the work and confirms a
  solid starting point before any planning or implementation begins.

The other roles (revisor, planner, advisor, builder) are named and reserved but
not yet implemented.

## How it works

`cowork` is a standalone executable that owns your terminal. When you run it:

1. **Choose your team.** A `gum` checkbox menu of roles (`scout`, `revisor`,
   `planner`, `advisor`, `builder`), all checked by default. Space toggles,
   Enter confirms.
2. **Configure each role.** Accept the defaults in one keystroke, or pick which
   roles to customize and choose a controller (`claude`/`codex`), a yolo
   (permission-bypass) toggle, and a mode (`plan`/`implement`) for each.
3. **Give context.** Type/paste the files/code/intent the work needs.

The interactive menus are rendered with [`gum`](https://github.com/charmbracelet/gum).
For tests and automation there is also a non-interactive **args path**
(`--team`/`--config`/`--context`) that skips the menus entirely — see
[Usage](#usage).

`cowork` then runs a **preflight** check and spins up the first role (`scout`)
using the controller you chose, bridging its live conversation to your terminal.

### The bridge

The two controllers are driven differently because their non-interactive modes
differ:

- **claude** runs as a single persistent duplex process
  (`claude -p --input-format stream-json --output-format stream-json`). Your
  typed lines are framed as stream-json user messages on stdin; the assistant's
  output streams back on stdout. A blank line ends the session.
- **codex** runs turn-based: the first turn is `codex exec --json`, from which
  `cowork` captures the session's `thread_id`; each follow-up turn is
  `codex exec resume <thread_id>`. (codex `exec` has no persistent stdin, so
  every turn is a fresh process resumed by id.)

### Controllers and modes

The flags `cowork` emits per (controller, mode, yolo), verified against
**Claude Code 2.1.x** and **codex-cli 0.133.x**:

| Setting | claude | codex |
| --- | --- | --- |
| plan mode | `--permission-mode plan` | `--sandbox read-only` |
| implement, yolo off | `--permission-mode acceptEdits` | `--sandbox workspace-write` |
| implement, yolo on | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |

Notes:

- `codex exec` is already non-interactive (it never prompts), so approval policy
  is set entirely by the sandbox — there is no `--ask-for-approval` flag on
  `exec`. `cowork` also passes `--skip-git-repo-check` so it runs outside a git
  repo, and `codex exec resume` inherits the original session's sandbox (it
  rejects `--sandbox`).
- The `scout` role spec is preloaded into claude via `--append-system-prompt-file`
  and into codex by prepending it to the prompt — `cowork` never writes an
  `AGENTS.md` into your repo.
- **yolo off has no interactive approval relay** in this release: a tool the
  permission/sandbox level does not auto-allow is denied and surfaced to you as
  an error (the run does not hang). `scout`'s defaults are plan + yolo, where
  this never triggers.

### Safety

With yolo on, claude runs with `--dangerously-skip-permissions` and codex with
`--dangerously-bypass-approvals-and-sandbox` — both bypass approval/sandbox
guards. Run `cowork` in a trusted/isolated workspace.

## Requirements

- Python 3.9 or newer (stdlib only — no third-party Python packages).
- **gum** — for the interactive menus: `brew install gum` (or see
  [gum installation](https://github.com/charmbracelet/gum#installation)). Only
  required for the interactive flow; the non-interactive args path does not need
  it.
- The controller CLIs you intend to use, on your `PATH`:
  - **Claude Code** — `npm install -g @anthropic-ai/claude-code`
  - **Codex CLI** — `npm install -g @openai/codex` (Node 18+) or
    `brew install --cask codex`

`cowork`'s preflight reports exactly which of these is missing before doing
anything (`gum` is checked only for the interactive flow).

## Install

Clone into your local skills directory and run the executable:

```bash
git clone https://github.com/garusis/co-plan.git ~/.claude/skills/co-plan
cd ~/.claude/skills/co-plan
./cowork --check     # verify Python + controller CLIs
```

Optionally symlink it onto your `PATH`:

```bash
ln -s ~/.claude/skills/co-plan/cowork ~/.local/bin/cowork
```

## Usage

### Interactive

```bash
./cowork            # run the full flow: team -> config -> context -> scout
./cowork --check    # run the preflight dependency check only
```

- **Team step:** a `gum` checkbox menu (all roles preselected). Space toggles,
  Enter confirms.
- **Config step:** the per-role defaults are printed as a table first, then you
  confirm "use these defaults?" to continue instantly — or decline, pick which
  roles to customize, and choose controller/permissions/mode for each.
- **Context step:** a `gum` multiline editor (Ctrl+D to finish).

### Non-interactive (args path)

Skip the menus entirely — useful for tests and automation. Providing any of
`--team`, `--config`, or `--context`/`--context-file` switches off the
interactive UI (and `gum` is not required):

```bash
# scout only, codex controller, no yolo, implement mode, context inline
./cowork --team scout --config "scout=codex,no-yolo,implement" --context "Refactor the auth module"

# context from a file (or '-' to read stdin)
./cowork --team scout --context-file ./brief.md
echo "the brief" | ./cowork --team scout --context-file -
```

- `--team` — comma-separated roles (default: all). Unknown roles error out.
- `--config ROLE=opt,opt` — repeatable; tokens are any of
  `claude|codex`, `yolo|no-yolo`, `plan|implement`.
- `--context TEXT` / `--context-file PATH` — initial context (`-` = stdin).
- `--session-file PATH` — use a specific session store (default
  `./.cowork/session.json`).
- `--no-session` — do not read or write the session store.

Defaults per role:

| Role | Controller | yolo | Mode |
| --- | --- | --- | --- |
| scout | claude | on | implement |
| revisor | codex | on | implement |
| planner | claude | on | implement |
| advisor | codex | on | implement |
| builder | claude | on | implement |

Roles default to **implement** mode (write-enabled). They are kept in their lane
by **role-spec guardrails**, not by plan mode — e.g. the scout may write only its
intel file (see below). This is instruction-level confinement, not an OS sandbox.

Only `scout` runs in this release; selecting a team without `scout` exits with a
note that the other roles are not yet available.

## Sessions

`cowork` persists each session in a project-local **`.cowork/session.json`** in
the directory you run it from (add `.cowork/` to your `.gitignore`). It stores:

- a **cowork session UUID** (`session_uuid`) — minted once per session, distinct
  from any claude/codex session id. It names this session's assets, e.g. the
  scout intel file `.cowork/scout.intel.<session_uuid>.json`;
- the **team** and **per-role config** — so the next run in the same directory
  does not re-ask them (you'll see `using saved session config`); and
- the scout's **CLI session id** (claude `session_id` / codex `thread_id`) — so a
  run that is killed can be **resumed where it left off**.

On the next run, if a saved session exists, `cowork` reuses the config and
**auto-resumes** the scout's CLI session (`claude --resume <id>` /
`codex exec resume <thread_id>`). The claude session id is pinned up front
(`--session-id <uuid>`) and saved immediately, so even an instant kill is
resumable.

Provide a fresh task for the resumed session with `--context`, or just rerun and
type. Use `--no-session` to disable persistence, or `--session-file` to point at
a different store. Changing the saved config is out of scope for now — delete
`.cowork/session.json` to start fresh.

## The scout role

`scout` doesn't gather blindly — it runs a short, consensus-building dialogue to
find the right thing to build, the way a good product conversation goes:

1. **Recon** — reads/searches the repo to ground itself.
2. **Clarify** — asks you the scope-defining questions (objective, definition of
   done, intended behavior). It asks blocking questions rather than guessing.
3. **Propose options** — when there are tradeoffs, it lays out concrete options
   *with a recommendation* instead of just asking open questions.
4. **Iterate** — refines with you until you reach product consensus.
5. **Hand off** — writes its intel and marks it ready for review.

Its **only write target** is its intel file
`.cowork/scout.intel.<session_uuid>.json`; it must not touch any other file
(reading/searching the whole repo is encouraged). Full spec:
[roles/scout.md](roles/scout.md).

### Intel file

A JSON object with a fixed top level; `result` is the scout's free-form
deliverable:

```json
{ "session": "<uuid>", "role": "scout",
  "status": "needs_input | ready_for_review",
  "result": { "objective": "…", "clarifications": [{"q":"…","a":"…"}],
              "relevant_code": "…", "open_unknowns": "…",
              "recommended_starting_point": "…", "plan?": "…" } }
```

cowork reads only `status`. The asked questions and your answers are recorded in
`result.clarifications`. If no `planner` role is on the team, the scout also
includes a lightweight plan in `result`.

### Interacting with scout — the three states

Each turn, cowork streams the reply, then reads the intel `status`:

- **working** — claude streams tokens after `scout ›`; codex shows a
  `scout working…` spinner.
- **`needs_input`** — scout asked you something (visible in its reply). cowork
  shows a `── scout needs your input ──` cue and waits for your answer.
- **`ready_for_review`** — scout finished the intel and posts a **summary in the
  chat**, then cowork shows the review gate:
  ```
  ✓ scout intel ready for review — .cowork/scout.intel.<id>.json
  Enter to approve & finish, or type feedback to revise › 
  ```
  **Enter** approves and ends the session; **typing feedback** sends another turn
  (status reverts to working) so you keep refining. The banner never blocks your
  typing.

Turns are labeled throughout — your input as `you ›`, the role's replies as
`scout ›` — so it's always clear who said what.

## Repository layout

```text
.
|-- cowork                      # executable entry point
|-- roles
|   `-- scout.md                # scout role spec (preloaded into the controller)
`-- scripts
    |-- cowork.py               # entry flow (gum menus + args path) + scout orchestration
    |-- cowork_bridge.py        # flag assembly, stream-json framing, codex resume, probe
    |-- cowork_preflight.py     # Python-version + gum/controller PATH checks
    |-- cowork_state.py         # .cowork/session.json store (config + resumable session ids)
    `-- test_cowork.py          # unit + live integration tests
```

## Development

Run the fast unit suite (fakes only — no CLIs spawned, no API calls):

```bash
python3 -m unittest scripts/test_cowork.py
```

The unit tests cover flag assembly, preflight, the `gum` menu seam (via a fake
runner — no `gum` install or TTY needed), the non-interactive args path, the
claude stream-json probe, event parsing, denial handling, the plan-only
fallthrough, and that `cowork` stays self-contained. `gum` itself is not
unit-tested (it is vetted external tooling); the real menu experience is a
manual check.

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
