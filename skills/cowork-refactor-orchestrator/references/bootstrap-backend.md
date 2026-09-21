# Cowork self-hosting runner

Use this treatment when the package changes the Cowork mechanism that would
otherwise authorize, dispatch, observe, recover, or accept that same package.
Examples include capability preflight, governed identity, terminal phase truth,
guards, provider health, or exact-role resume. Milestone labels alone never
select this treatment.

Self-hosting work still runs on Cowork. Launch it from a frozen stable runner —
an installed main checkout that the package never edits — driving an
isolated target worktree that holds the candidate. Freezing the runner resolves
the circularity: the mechanism executing the package is a fixed artifact, and
the mechanism under repair is a separate one.

## Selecting and freezing the runner

Resolve the runner explicitly, once per package. Do not inherit it from this
document, from a previous package, or from any commit written down in advance;
a literal pinned in prose goes stale silently and then names a runner nobody
validated. At package start, select the checkout to run on, verify it passes its
own environmental preflight, and record in the package's authority record the
runner's repository path together with the exact HEAD commit and tree it
resolved to.

That recorded HEAD/tree is frozen for the whole run. Every role in the package
uses the same recorded runner, and re-pointing it is a new package rather than
an amendment to this one.

Validated means validated for this package: the recorded runner passed its own
preflight. Evidence from an earlier package or runner does not transfer.

Runner and target are distinct artifacts even when they sit on the same commit.
A diff in the target does not mean the runner changed. Derive no runner-identity
claim from the target diff, and never edit the runner while it is running the
package. If the runner's environmental preflight fails, the package is blocked
and does not re-route.

Do not require Cowork to authoritatively run the mechanisms whose correctness
is under repair. A Cowork scout may provide advisory research, but do not make
its gates authoritative for a self-hosting package.

## Run shape

Cowork runs paired teams: a new session always begins in scouting with
`scout` and `scout-reviewer`, and every lead on the team needs its paired
reviewer. There is no standalone planner, builder, or reviewer session.

1. Create the worktree and package state before starting a worker.
2. Start one bounded Cowork session from the frozen runner against the target
   worktree. When the supervisor must gate the plan before building, start it
   with the planning team (`scout,scout-reviewer,planner,planning-advisor`);
   the run ends with the approved plan as its deliverable. With the full team,
   the planning-advisor's approval chains directly into building.
3. Adjudicate the plan gate from the plan, authority, candidate binding, and
   policy unless the package brief delegates implementation directly.
4. Continue the same saved session (`--session-file`) for building. A
   planning-team run leaves the saved phase at `planning`, so this run first
   sends the planner a continuation turn and the planning-advisor reviews the
   plan again; building starts only after that approval. Passing
   `--team` on a saved session replaces its saved team and resets per-role
   config to defaults plus any `--config`, so restate the pins you rely on and
   confirm the phase and role in the run result.
5. After the build-reviewer approves, perform the supervisor's own independent
   review of the candidate diff and evidence. For extra advisory assurance, a
   separate `scout,scout-reviewer` session may inspect the candidate read-only;
   its output stays advisory. Do not ask the builder to certify its own
   acceptance.
6. Convert all worker claims into `result.json`, `review.json`, and evidence
   references; then validate candidate binding and automatically adjudicate
   the package gates within delegated policy.

A self-hosting package defaults to `no-publish`. It must not commit, push, open a
pull request, or publish externally unless a separate candidate-bound policy
explicitly grants that action.

Let the adapter/controller own invocation details, process identifiers,
detachment, liveness checks, and safe termination. Do not embed provider CLI
flags or controller log formats in this skill; they are adapter details and
will change.

## Recovery

Record a provider or harness failure as a state transition, not malformed worker
output. Preserve worktree, candidate digest, role, and partial artifacts. Use a
fresh compact handoff for ordinary correction. Resume only the exact failed
role when the persisted state proves identity and candidate continuity.

For a subscription/plan quota signal with trustworthy provider retry/reset
metadata, persist the typed capacity packet, enter `awaiting_capacity`, schedule
the durable wake, and resume automatically. This is normal recovery; it needs
no human or authority amendment. The package default is `subscription_only`:
never buy or imply paid overage.

Do not confuse a controller-local `error_max_budget_usd` guard with provider
quota. It only exhausted a package limit and does not establish a provider
reset. It cannot be raised or resumed by an agent.

A capacity pause (rc 5) resumes only through the authorized
`cowork resume-trigger` path. While the session's capacity lease is live (not
consumed, cancelled, or expired), the supervisor never runs a plain launch or
resume of that session. The runtime does not enforce this in general: it
refuses a plain run with `decision_held_by_capacity_pause` only when the paused
turn holds a pending decision (`decision_bindings`). Without one, a plain
resume can resend the role despite a live lease, including a `manual_signal`
lease whose signal was never journaled. Before replaying the persisted pending turn,
`resume-trigger` claims the named PauseLease and re-checks the candidate,
provider-session, and policy binding, the pending-turn precondition, owner and
provider-session exclusivity, and the failed-wake-attempt ceiling; a mismatch
refuses without replaying. Two adapters exist:

- `scripts/cowork_wake_macos.py` (scheduled mode, trustworthy reset metadata)
  installs a launchd job whose fire handler claims the due lease and then
  invokes `resume-trigger`.
- `scripts/cowork_wake_manual.py` (manual-signal mode, no trustworthy reset
  time) verifies an externally produced Ed25519 capacity signal against pinned
  public keys and records it; it holds no signing key. A `manual_signal`-mode
  lease is claimable only by passing that signal to `resume-trigger` with
  `--manual-signal-record` and `--pinned-public-keys`.

Read each adapter's `--help` for its arguments. Workers and orchestrators never
sign, fabricate, or self-assert a capacity signal; without a genuinely signed
one the package stays `awaiting_capacity`. The signal controls resume timing
only. If billing mode or capacity is unknown, fail
closed without a speculative retry or reset claim. Other repeated provider
failures may use an already-authorized recovery such as a controller switch,
but never spend another same-provider retry by default. Local guard exhaustion remains
terminal under every authority amendment; an alternate route is a new attempt,
not a resume.
