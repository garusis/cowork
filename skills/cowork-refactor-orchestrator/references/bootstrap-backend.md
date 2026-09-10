# Cowork self-hosting runner

Use this treatment when the package changes the Cowork mechanism that would
otherwise authorize, dispatch, observe, recover, or accept that same package.
Examples include capability preflight, governed identity, terminal phase truth,
backend-gate selection, guards, provider health, or exact-role resume. Absent or
invalid gate evidence does not select this treatment either; it blocks the
package. Milestone labels alone never select this treatment.

Self-hosting work still runs on Cowork. Launch it from a frozen stable runner —
an installed main checkout that the package never edits — driving an
isolated target worktree that holds the candidate. The circularity that the old
model avoided by switching to another backend is now resolved by freezing the
runner instead: the mechanism executing the package is a fixed artifact, and the
mechanism under repair is a separate one.

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

Validated means validated for this package. A newly selected runner does not
inherit any earlier runner's global accreditation, and selecting it is not an
accreditation event. Historical receipts stay historical: they certify the
release they were issued against and never transfer to a different runner, so a
past `GLOBAL_PASS` is never evidence that the runner recorded for this package
is accredited.

Runner and target are distinct artifacts even when they sit on the same commit.
A diff in the target does not mean the runner changed. Derive no runner-identity
claim from the target diff, and never edit the runner while it is running the
package. If the runner's environmental preflight fails, the package is blocked;
routing has no alternate backend to degrade to.

A self-hosting shape is never itself a reason to leave Cowork — that is exactly
what the frozen runner resolves. Only a real Cowork blocker (Cowork itself
cannot run the package) may be repaired through the `invoke-claude-agent` skill
under supervisor review, bounded to the repair and returning to Cowork
afterwards.

Do not require Cowork to authoritatively run the mechanisms whose correctness
is under repair. A Cowork scout may provide advisory research, but do not make
its gates authoritative for a self-hosting package.

## Run shape

1. Create the worktree and package state before starting a worker.
2. Start a fresh, bounded Cowork role session for investigation/planning. Claude
   remains a supported controller inside Cowork; only the direct routing
   destination is gone.
3. Run the plan gate before a builder starts unless the package brief delegates
   implementation directly. The supervisor adjudicates this gate automatically
   from the plan, authority, candidate binding, and policy.
4. Start a separate builder session in the same isolated worktree.
5. Start a fresh independent reviewer session after the builder's evidence is
   collected. Do not ask the builder to certify its own acceptance.
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
reset. It cannot be raised or resumed by an agent. If a provider quota signal
has no trustworthy reset time, persist `awaiting_capacity` in manual-signal
mode. Only an authenticated external application or top-level authority adapter
may journal the capacity-available signal. An agent-operated
CLI must reject self-asserted human principals/tokens; workers and orchestrators
must not fabricate or verify the signal. Until that adapter exists and writes
the bound event, generic launch/resume remains blocked. The signal controls
resume timing only. If billing mode or capacity is unknown, fail
closed without a speculative retry or reset claim. Other repeated provider
failures may use an already-authorized alternate recovery route, but never
spend another same-provider retry by default. Local guard exhaustion remains
terminal under every authority amendment; an alternate route is a new attempt,
not a resume.
