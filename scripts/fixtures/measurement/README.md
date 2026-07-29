# Measurement criterion fixtures

Eight fixture sessions, one per acceptance surface named in the plan's
verification block. They are exercised by pointing `COWORK_SESSIONS_ROOT` at
this directory and running `cowork --report <name>` — the same code path a real
session takes, so there is no test-only branch the real run never exercises.

## What is checked in, and what is generated

**Checked in** — the *inputs*, authored by `build_fixtures.py`:

- `trace.jsonl`, `identities.json`, `scores.json` — the raw session state.
- `ledger.jsonl` in `c2-finding-lifecycle` — hand-authored, because that fixture
  is *about* finding lifecycle (a withdrawn finding that must survive as
  withdrawn).
- `controller_logs/` in `c3-controller-log` — fake Claude JSONL and Codex
  rollouts, including a deliberately truncated one. A session's own bundled
  logs are discovered automatically, so the report ingests these rather than
  reaching past the fixture to the real `~/.claude` / `~/.codex` roots.
- `measurement.json` in `c5-provenance-replay` — **deliberately disagrees with
  its own trace** (it says 7 productive turns; the trace has 2). That
  disagreement is the entire point: `--report` must print 7, and
  `--report --rebuild` must print 2. Never regenerate this one.

**Generated, and NOT checked in** — `measurement.json` and `ledger.jsonl` for
every other fixture. They are rebuilt on demand by the report itself. Committing
them would let a stale record mask a real change in the builder, which is
exactly the failure mode the C5 fixture exists to detect.

## Regenerating

    .venv/bin/python scripts/fixtures/measurement/build_fixtures.py

Run from the repository root. It rewrites the authored inputs; the generated
records reappear the next time a report runs.
