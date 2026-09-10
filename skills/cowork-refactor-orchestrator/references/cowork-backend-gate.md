# Cowork backend gate

Use `cowork-orchestrate` and its stable backend-gate pointer contract before
dispatch. Cowork is eligible only after integrity-verified evidence shows that
the relevant release satisfies all of these conditions:

1. Each paid role dispatch has a capability preflight that binds repository
   constraints, effective controller/model/effort identity, artifacts, allowed
   actions, guard health, and runtime paths.
2. Ungoverned children are blocked or correlated to parent work and controller
   policy.
3. Aborted, denied, paused, and successful turns produce distinct persisted
   outcomes; a process exit cannot masquerade as a completed phase.
4. A trustworthy subscription/plan quota signal enters `awaiting_capacity`,
   schedules a once-only exact-role wake, and resumes without spending a
   same-provider repair turn; recovery preserves phase, candidate, controller
   policy, and completed paired work. A quota signal without a trustworthy
   reset must remain `awaiting_capacity` pending a capacity-available signal
   journaled by an authenticated external application/top-level authority
   adapter. An agent-operated CLI cannot self-assert that signal, and generic
   launch/resume remains blocked until it exists. Local controller guards are
   not provider quota evidence, cannot claim a reset, and cannot be raised or
   resumed by an agent under any authority amendment.
5. Liveness state distinguishes productive work, provider wait, policy denial,
   crash, and stall from durable artifacts rather than terminal output.
6. Deterministic gates are persisted, candidate-bound, and automatically
   adjudicated by the supervisor under delegated policy; insufficient authority
   produces a typed `needs_authority` escalation with a durable resume token.

Prefer also having candidate-bound verification receipts and phase-blocking
findings before using Cowork for packages whose acceptance depends on them.

The gate yields exactly one of two outcomes: Cowork, or `blocked`. The gate has
no alternate backend to degrade to, so a package that does not clear it stops
with its named reason. A failed environmental preflight of the runner
(`cowork --check`) yields `blocked` on the same terms. A validity window that is
coherent but has already elapsed does not by itself fail the gate; it is
recorded as `validity_elapsed`.

Cowork is the default. Only a real Cowork blocker (Cowork itself cannot run the
package) may be repaired through the `invoke-claude-agent` skill under
supervisor review, bounded to the repair and returning to Cowork afterwards.
An elapsed window, a failed gate, or a self-hosting shape never qualifies.

## Launch policy after the gate

Use the existing `cowork-cli` skill for exact non-interactive commands,
session-file handling, worktree behavior, resume, and reporting. Pass context
through an explicit context file. Keep the session anchor available for
recovery. Start with one package and the smallest risk-appropriate profile;
turn off nonessential evaluation overhead during early dogfooding.

Treat Cowork's compact state, checkpoint receipts, and artifacts as evidence
inputs. Trace is supplemental narrative evidence. The external package
controller still validates authority, candidate binding, budget, deterministic
gate evidence, and the final collection packet. It automatically advances
policy-bounded packages; use `needs_authority` only for capability gaps. Use
`cowork-debug` only if those inputs conflict.

Do not use a static six-role topology by default. Promote roles only when the
package risk, ambiguous ownership, failed evidence, scope growth, or explicit
assurance requirement warrants it. Promotion must not remove existing safety
checks, authority, candidate binding, evidence, or crash-consistent event
journaling. Treat CLI/UI surfaces as observability and emergency-control
adapters, not the primary control path.
