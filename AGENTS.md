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
- The normal unit tests use fakes and should not spawn real Claude/Codex
  sessions or make API calls.
- `COWORK_LIVE=1 python3 -m unittest scripts/test_cowork.py` runs live CLI
  integration tests. Use it only when intentionally verifying installed
  controller behavior.
- The interactive UI dependencies are `rich`, `prompt_toolkit`, and
  `questionary`; the non-interactive args path can run without them.

## Session and generated state

- `.cowork/`, `.plans/`, `.venv/`, and `.worktrees/` are local/generated and
  gitignored.
- Project-local `.cowork/session*.json` files are resumable session anchors.
  Treat them as runtime state, not source files.
- Per-session artifacts live under `~/.cowork/sessions/<session_uuid>/`
  unless `COWORK_SESSIONS_ROOT` overrides the location.
- `cowork` does not commit or open PRs; approved build output is left in the
  working tree for the user to review.

## Implementation notes

- `scripts/cowork_bridge.py` owns Claude/Codex command assembly, event parsing,
  stream handling, and probe behavior. Keep flag changes covered by focused
  tests.
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
