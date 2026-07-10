# AGENTS.md

Shared, vendor-neutral working notes for any AI/CLI agent (Claude, Codex, etc.)
operating in this repo. Keep entries factual and tool-agnostic.

## Repo shape

- This repo contains the `cowork` CLI plus bundled support skills under
  `skills/`.
- `cowork` is a thin launcher for `scripts/cowork.py`. When `.venv/bin/python`
  exists, the launcher re-execs into that interpreter.
- `roles/*.md` are prompt contracts for the `cowork` roles. Behavior changes
  usually need matching updates in the relevant role spec, orchestration code,
  tests, and README.

## Development commands

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest scripts/test_cowork.py
./cowork --check
```

Notes:
- The normal unit tests use fakes and should not spawn real
  Claude/Codex/opencode sessions or make API calls.
- `COWORK_LIVE=1 python3 -m unittest scripts/test_cowork.py` runs live CLI
  integration tests. Use it only when intentionally verifying installed
  controller behavior.
- The interactive UI dependencies are `rich`, `prompt_toolkit`, and
  `questionary`; the non-interactive args path can run without them.

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
  working tree for the user to review.

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
- Role status/review artifacts are JSON contracts read by the orchestrator.
  Keep schema changes reflected in roles, README, and tests.

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

### `--worktree` / `--headless` (automatic operation)

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
  `--session-file`), not from inside the worktree. Per-session assets under
  `~/.cowork/sessions/<uuid>/` are always found.
- `cowork --headless` (alias `--auto`) drives the whole flow with no human
  gates: roles never block (they record assumptions and proceed), reviewers
  work with what they have, rounds end on reviewer consensus or the review-round
  cap. It requires `--context`/`--context-file`. The builder contract is
  unchanged under headless — working-tree edits only, no commit/PR.
