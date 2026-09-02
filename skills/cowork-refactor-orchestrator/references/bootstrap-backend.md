# Direct-Claude self-hosting fallback

Use this backend when current gate evidence is absent/invalid or when the
package changes the Cowork mechanism that would otherwise authorize, dispatch,
observe, recover, or accept that same package. Examples include capability
preflight, governed identity, terminal phase truth, backend-gate selection,
guards, provider health, or exact-role resume. Milestone labels alone do not
select this backend.

Do not require Cowork to authoritatively run the mechanisms whose correctness
is under repair. A Cowork scout may provide advisory research, but do not make
its gates authoritative for a self-hosting fallback package.

## Run shape

1. Create the worktree and package state before starting a worker.
2. Start a fresh, bounded Claude session for investigation/planning.
3. Run the plan gate before a builder starts unless the package brief delegates
   implementation directly. The supervisor adjudicates this gate automatically
   from the plan, authority, candidate binding, and policy.
4. Start a separate builder session in the same isolated worktree.
5. Start a fresh independent reviewer session after the builder's evidence is
   collected. Do not ask the builder to certify its own acceptance.
6. Convert all worker claims into `result.json`, `review.json`, and evidence
   references; then validate candidate binding and automatically adjudicate
   the package gates within delegated policy.

The fallback package defaults to `no-publish`. It must not commit, push, open a
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
may journal the capacity-available signal. The fallback agent-operated
CLI must reject self-asserted human principals/tokens; workers and orchestrators
must not fabricate or verify the signal. Until that adapter exists and writes
the bound event, generic launch/resume remains blocked. The signal controls
resume timing only. If billing mode or capacity is unknown, fail
closed without a speculative retry or reset claim. Other repeated provider
failures may use an already-authorized alternate recovery route, but never
spend another same-provider retry by default. Local guard exhaustion remains
terminal under every authority amendment; an alternate route is a new attempt,
not a resume.
