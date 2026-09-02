# Live supervision

Use durable Cowork state as the primary supervision surface. Terminal output
and `trace.jsonl` are narrative signals, never proof of health or completion.

## One bounded inspection

At a scheduled wake:

1. Read the session anchor, current phase/work identity, and the latest durable
   activity and checkpoint state.
2. Read `scheduled_review/<work_id>.json` verbatim. Do not recompute whether a
   review is due from elapsed time. A missing or corrupt schedule means unknown.
3. If the work is non-terminal, healthy, and not due, stop the inspection.
4. When due, combine the durable classification with one real process probe.
5. Read a targeted diff or controller-log excerpt only when those facts conflict
   or a policy decision requires them.

Default external orchestration uses a 15-minute recurring wake. Query each
active work once per wake and do not add a second polling loop or event tail.

## Failure classification

- A stopped trace alone proves nothing. `process_crash` plus a dead controller
  probe can establish a crash; a live child with productive/provider-wait state
  is a healthy quiet turn.
- `hung_descendant` becomes a hard stall only when an independent process check
  confirms an orphan, zombie, or stopped descendant. Otherwise it remains a
  warning.
- Missing, torn, or unreadable activity/checkpoint files remain unknown. Never
  coerce them to success, failure, or "not due".
- Unknown provider failures stay `unknown_provider_failure`; do not relabel them
  as quota, policy denial, or local guard exhaustion.

Do not kill or resume a role merely because output is quiet or CPU is low. A
recovery requires durable failure evidence, a matching candidate/role/session,
and an authorized recovery path. Preserve completed evidence before resuming;
never invent a retry, reset, provider switch, or spending permission.

## Targeted forensics

Use `trace.jsonl` to reconstruct ordering and controller logs to inspect the
specific tool failure or denied write behind a contradiction. For OpenCode,
the `part` table in `~/.local/share/opencode/opencode.db` contains the durable
tool status and resolved permission error. For Claude and Codex, locate the
controller session from `identities.json`.

Escalate to the `cowork-debug` skill rather than expanding normal supervision
into full-log analysis.
