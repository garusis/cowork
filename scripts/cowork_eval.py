#!/usr/bin/env python3
"""D2: evaluation isolation, the scoring policy, and the durable queue.

Three problems are solved together here, because they are the same problem.

1. SCORING CONTAMINATED THE WORK. Evaluation used to run as an extra turn on the
   role's OWN session, so the thing being measured and the thing measuring it
   shared a context: an agent's later work was influenced by having just scored
   its reviewer. Evaluation now runs in an isolated session that has never
   touched the work and can only read the files it was handed.

2. SCORING SAT IN THE CRITICAL PATH. A round waited for its own evaluation
   before the fix could be handed back. Evaluation is now SEALED AND ENQUEUED
   the instant a verdict is valid, and DRAINED at phase end — the correction
   handoff never waits, so measurement is passive in wall-clock terms and not
   merely in ordering.

3. SCORES WERE BOUND TO EVIDENCE THAT DID NOT EXIST YET (CV-008). Sealing
   happens after the verdict is written and validated, and the seal is
   re-verified at drain time. Evidence that changed while the entry sat in the
   queue marks the score `unverifiable` rather than being re-hashed to whatever
   it says now — re-hashing would make every score verifiable by construction.

THE COST OF DEFERRAL is bounded by making the queue durable: it is written
before the round continues, an entry is marked drained only after its scores are
aggregated, and anything a killed process left pending is drained at the next
start of that session. A session killed mid-phase leaves rounds QUEUED, not
lost, and pending entries are reported as pending rather than silently dropped.

Every failure in this module degrades the MEASUREMENT and never the run.

Python 3.9+, stdlib only.
"""

import datetime
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_handoff as handoff  # noqa: E402
import cowork_state as state_store  # noqa: E402

POLICIES = state_store.EVALUATION_POLICIES
DEFAULT_POLICY = state_store.DEFAULT_EVALUATION_POLICY

# The deterministic `sampled` rule (P7). Eligible rounds 1, 3, 5, ... are
# selected. Deterministic rather than random so a run is reproducible and so a
# four-round fixture yields exactly 2 selected and 2 skipped — both strictly
# between zero and all, which is what makes "sampled" testable at all.
SAMPLED_RULE = "every_other_eligible_round_from_first"

# --------------------------------------------------------------------------- #
# The entry lifecycle.                                                        #
#                                                                             #
# A queue entry used to have exactly two observable conditions: it had a       #
# `drained` marker or it did not. Everything else — how many times it had been #
# tried, why it failed, whether trying again could ever help — existed only    #
# inside the process that happened to be draining, and died with it. So a      #
# broken entry was charged again at every phase boundary and every resume,     #
# forever, and nothing on disk could say why.                                  #
#                                                                             #
# The lifecycle below is that missing record. It is APPEND-ONLY, like the rest #
# of the queue: state is a fold over the entry's markers in file order, never  #
# an in-place rewrite, so a killed process leaves a readable prefix rather     #
# than a corrupted entry.                                                      #
# --------------------------------------------------------------------------- #

# The largest per-class budget, and therefore the hard ceiling on the attempts
# ANY entry can accumulate. An entry already at the ceiling is made terminal
# BEFORE another attempt is spent, which is what bounds the one case no class
# limit can bound: a process that died between `attempt_started` and its
# outcome leaves no failure class on disk, so the class is genuinely unknowable
# and the ceiling is the only honest bound left.
MAX_ATTEMPTS = 2

# Total attempts allowed per failure class. Fixed and deterministic on purpose:
# a configurable budget makes "why did this run twice" unanswerable from the
# record alone. Only a transient failure gets a second go — for the others a
# retry cannot change the outcome, so spending one is pure cost.
RETRY_LIMITS = {
    "transient": 2,
    "malformed_output": 1,
    "permanent": 1,
    "malformed_entry": 1,
    "unclassified": 1,
}

# Retries are IMMEDIATE: the eligibility timestamp is always real and always
# recorded, and the interval between attempts is deliberately zero. A wall-clock
# backoff would buy nothing at this scope and would make every retry test
# non-deterministic.
RETRY_INTERVAL_S = 0

# CLOSED: the entry is finished with and no drain picks it up again.
# `failed_permanent` is the pre-lifecycle spelling and is still honoured, so a
# queue written before this existed still reads correctly.
CLOSED_STATES = ("drained", "failed_permanent", "terminal", "retired")

# OPEN: still awaiting scoring. `held` and `attempting` deliberately do NOT
# close an entry — holding is exactly what re-enabling evaluation releases, and
# an interrupted attempt is retried under the ceiling rather than written off on
# a guess about why it stopped.
OPEN_STATES = ("pending", "held", "attempting")

# The fields a real enqueue always writes: what an entry needs before it can be
# turned into an actual evaluation. An entry carrying NONE of them is a
# pre-lifecycle or minimal record — see `_is_prelifecycle_entry`.
EVALUATION_SHAPE_FIELDS = (
    "scratch_path", "scores_path", "identity_snapshot", "policy_decision",
    "envelope", "evaluator_seat", "criteria", "phase", "round", "evaluatee",
    "review_path",
)


def _is_prelifecycle_entry(entry):
    """Whether this record predates the current evaluation shape entirely.

    A queue written before any of this existed can hold a record that is little
    more than an id. Such a record carries nothing to build an evaluation from,
    so the current pipeline can never actually score it — and it can never start
    a controller turn either, which is what makes the distinction safe to draw.

    The bounded-retry contract therefore does NOT apply to it. Classifying it
    would mean naming a failure class for something that never ran, and then
    retiring it permanently on that guess; the honest reading is "this is not an
    entry this version knows how to run", so it keeps its historical behavior of
    staying pending and being REPORTED as pending. CV-039 was about a broken
    entry being CHARGED again and again — a record like this costs nothing per
    drain, so leaving it pending costs nothing either.

    A well-formed current entry is unaffected and stays bounded, including one
    whose paths are missing or malformed: it still carries the shape, so it is
    still something this version tried to run.
    """
    if not isinstance(entry, dict):
        return False
    return not any(entry.get(field) is not None
                   for field in EVALUATION_SHAPE_FIELDS)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def decide(policy, round_index, is_final_round=False):
    """Whether this round gets scored, and WHY.

    Returns `{"selected", "rule", "reason", "policy", "round"}`. The reason is
    recorded per round so the record can show what a lower policy actually
    skipped — a policy whose savings are invisible is not an informed choice.
    """
    policy = policy if policy in POLICIES else DEFAULT_POLICY
    out = {"policy": policy, "round": round_index, "rule": policy,
           "selected": False, "reason": ""}
    if policy == "off":
        out["reason"] = "evaluation disabled by policy"
    elif policy == "all_rounds":
        out["selected"] = True
        out["reason"] = "every round is scored under all_rounds"
    elif policy == "final_round":
        # A round cannot know it is the last one WHILE it is happening — only
        # the phase end knows that. Enqueuing only when `is_final_round` is
        # already true therefore selected nothing at all in a live run, because
        # nothing upstream can supply that flag mid-phase.
        #
        # So every round is enqueued as a CANDIDATE and each new candidate
        # supersedes the previous one; at drain time only the surviving
        # candidate per phase is scored. The selection is still `final_round`,
        # it is just resolved when finality is actually knowable.
        out["selected"] = True
        out["candidate"] = True
        out["supersedes_earlier_candidates"] = True
        out["reason"] = ("enqueued as the phase's final-round candidate; "
                         "superseded by any later round in the same phase")
    elif policy == "sampled":
        out["rule"] = SAMPLED_RULE
        try:
            index = int(round_index)
        except (TypeError, ValueError):
            index = 1
        out["selected"] = (index % 2) == 1
        out["reason"] = ("odd eligible round selected by %s" % SAMPLED_RULE
                         if out["selected"]
                         else "even eligible round skipped by %s"
                              % SAMPLED_RULE)
    return out


# --------------------------------------------------------------------------- #
# Sealing and enqueueing.                                                     #
# --------------------------------------------------------------------------- #


def seal_round(artifacts, validate=None, context=None):
    """Seal one round's evidence chain AFTER its verdict is written.

    The chain deliberately includes the PRIOR round's verdict and artifact
    revision (P6). Without them, criteria like "responsiveness to feedback" have
    nothing to be responsive TO, so every round would score `not_applicable` and
    the comparison the whole project exists for would be gutted. With them,
    round 1 is correctly `not_applicable` (there is no prior feedback) while
    later rounds are genuinely observable.

    Path-first, never embedded: the envelope carries paths and digests, and the
    isolated evaluator reads the files itself.
    """
    return handoff.seal_envelope(artifacts, validate=validate, context=context)


def enqueue(queue_path, entry):
    """Append one self-contained entry to the durable queue.

    Self-contained is the requirement: a later process — possibly after a crash,
    with none of this one's memory — must be able to drain it. The entry
    therefore carries the sealed envelope, the evaluator/evaluatee pair, the
    criteria, the phase and round, the policy decision that selected it, and the
    identity snapshot.
    """
    if not queue_path or not isinstance(entry, dict):
        return False
    record = dict(entry)
    record.setdefault("queued_at", _utc_now())
    record.setdefault("state", "pending")
    try:
        line = json.dumps(record, sort_keys=True)
    except (TypeError, ValueError):
        return False
    try:
        dirname = os.path.dirname(queue_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(queue_path, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


# How a queue read turned out. The distinction is load-bearing: a queue that is
# ABSENT genuinely holds nothing, while a queue that could not be READ holds an
# unknown amount, and reporting the second as zero states a fact nobody
# established. Anything consuming counts from this module must be able to tell
# them apart.
QUEUE_OK = "ok"
QUEUE_MISSING = "missing"
QUEUE_UNREADABLE = "unreadable"


def read_queue_status(queue_path):
    """`{state, records}` — every queue record, oldest first, AND how the read
    went.

    `read_queue` swallows an OSError and hands back a list, which is the right
    call for a drain (a queue it cannot read is a measurement problem, never a
    reason to break the run) but is the wrong thing to publish: an empty list
    from an unreadable file is indistinguishable from an empty list from an
    empty one. Callers that REPORT counts use this and keep the two apart.

    A partially-read file still returns the records it got, with state
    `unreadable`, because those records are real — they are simply not the whole
    story, and the state says so.
    """
    if not queue_path or not os.path.exists(queue_path):
        return {"state": QUEUE_MISSING, "records": []}
    out = []
    try:
        with open(queue_path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    # A malformed LINE is tolerated (a partially-written tail);
                    # it says nothing about whether the file could be read.
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return {"state": QUEUE_UNREADABLE, "records": out}
    return {"state": QUEUE_OK, "records": out}


def read_queue(queue_path):
    """Every queue record, oldest first. Tolerant of a partially-written tail.

    Tolerant on purpose: a drain must never break the run over an unreadable
    queue. Callers that REPORT counts rather than act on them want
    `read_queue_status`, which says whether the read actually succeeded.
    """
    return read_queue_status(queue_path)["records"]


def _partition(entries, phase_closed=None):
    """Split pending entries into (score now, retire, hold until phase end).

    A `final_round` candidate may only be resolved once its phase is CLOSED.
    Resolving it at any drain looked correct while every drain happened at phase
    end — but a drain also runs at the next session start, so a process that
    died after round 1 would find round 1 as the only candidate, score it as
    "final", and then score round 2 as well when the phase really ended. Two
    rounds scored, one of them not final, and the policy quietly became
    `all_rounds` for that session.

    `phase_closed(phase) -> bool` says whether a phase is over. When it is not
    known (a recovery drain at session start), candidates are HELD pending
    rather than resolved — deferring costs a delay; resolving early costs
    correctness.
    """
    scoreable, retire, held = [], [], []
    candidates = []
    for entry in entries or []:
        decision = entry.get("policy_decision") or {}
        if decision.get("supersedes_earlier_candidates"):
            candidates.append(entry)
        else:
            scoreable.append(entry)
    best = {}
    for entry in candidates:
        phase = entry.get("phase")
        if phase_closed is not None and not phase_closed(phase):
            # Phase still open: this candidate may yet be superseded.
            held.append(entry)
            continue
        if phase_closed is None:
            # No finality signal at all — the safe reading is "not yet".
            held.append(entry)
            continue
        key = (phase, entry.get("evaluator_seat"))
        try:
            round_index = int(entry.get("round") or 0)
        except (TypeError, ValueError):
            round_index = 0
        current = best.get(key)
        if current is None:
            best[key] = (round_index, entry)
        elif round_index >= current[0]:
            retire.append(current[1])
            best[key] = (round_index, entry)
        else:
            retire.append(entry)
    scoreable.extend(entry for _, entry in best.values())
    return scoreable, retire, held


def resolve_candidates(entries, phase_closed=None):
    """The entries a drain would score, given a phase-finality signal."""
    return _partition(entries, phase_closed)[0]
def superseded_candidates(entries, phase_closed=None):
    """The candidates a drain retires — reported, not hidden."""
    return _partition(entries, phase_closed)[1]


def held_candidates(entries, phase_closed=None):
    """Candidates deliberately left pending because their phase is still open
    (or its finality is unknown). Reported as pending, never as scored."""
    return _partition(entries, phase_closed)[2]


def _new_fold(entry_id, queued_at=None):
    """The lifecycle of an entry with no markers yet: pending, nothing spent.

    This is also how a PRE-LIFECYCLE entry reads — the fields simply are not on
    disk, so the fold supplies them and the entry starts with a fresh budget.
    Old queue files are never rewritten to add them.
    """
    return {"entry_id": entry_id, "state": "pending", "attempts": 0,
            "limit": MAX_ATTEMPTS, "error_class": None,
            "eligible_at": queued_at, "held_reason": None,
            "unverifiable": False, "transition_history": []}


def _fold_all(records):
    """Fold EVERY entry's markers, in file order, in a single pass.

    One pass rather than one pass per entry: a drain reads the queue once and
    resolves every entry from the same snapshot, so two entries can never be
    decided against two different views of the file.
    """
    folds = {}
    for rec in records:
        entry_id = rec.get("entry_id")
        if not entry_id:
            continue
        state = rec.get("state")
        if state == "pending":
            # The enqueue record. `setdefault` so a re-enqueued id keeps the
            # lifecycle it already accumulated rather than silently resetting.
            folds.setdefault(entry_id,
                             _new_fold(entry_id, rec.get("queued_at")))
            continue
        fold = folds.get(entry_id)
        if fold is None:
            # A marker whose enqueue record is missing (a truncated head). Fold
            # it anyway: dropping it would make a closed entry read as absent.
            fold = _new_fold(entry_id)
            folds[entry_id] = fold
        marker_id = rec.get("marker_id")
        if marker_id:
            # History is APPENDED to, never truncated — including across a
            # `retried`, which is the whole point of linking a retry to what
            # came before instead of overwriting it.
            fold["transition_history"].append(marker_id)
        if state == "attempt_started":
            try:
                attempt = int(rec.get("attempt"))
            except (TypeError, ValueError):
                attempt = fold["attempts"] + 1
            # max(): the attempt count never goes backwards, so a duplicated or
            # out-of-order marker cannot hand an entry extra budget.
            fold["attempts"] = max(fold["attempts"], attempt)
            fold["state"] = "attempting"
            fold["eligible_at"] = rec.get("marked_at")
            fold["held_reason"] = None
        elif state == "held":
            fold["state"] = "held"
            fold["held_reason"] = rec.get("held_reason")
            fold["eligible_at"] = None
        elif state == "terminal":
            fold["state"] = "terminal"
            fold["error_class"] = rec.get("error_class")
            for field in ("attempts", "limit"):
                try:
                    fold[field] = int(rec.get(field))
                except (TypeError, ValueError):
                    pass
            fold["eligible_at"] = None
            fold["held_reason"] = None
        elif state == "retired":
            fold["state"] = "retired"
            fold["eligible_at"] = None
            fold["held_reason"] = None
        elif state == "retried":
            # The ONLY thing that reopens a closed entry, and only by explicit
            # user action. The budget is fresh; the history is not erased.
            fold["state"] = "pending"
            fold["attempts"] = 0
            fold["limit"] = MAX_ATTEMPTS
            fold["error_class"] = None
            fold["eligible_at"] = rec.get("marked_at")
            fold["held_reason"] = None
        elif state in ("drained", "failed_permanent"):
            fold["state"] = state
            fold["eligible_at"] = None
            fold["held_reason"] = None
            if state == "drained" and rec.get("unverifiable"):
                # Drained successfully, but against evidence that had already
                # changed. NOT a failure (see `drain`) — it is reported beside
                # the completed count rather than folded silently into it.
                fold["unverifiable"] = True
    return folds


def read_entry_lifecycle(records, entry_id):
    """`{state, attempts, limit, error_class, eligible_at, held_reason,
    transition_history}` for one entry, folded from `records` in file order.

    Pure: it takes records that were already read, so a caller resolving many
    entries reads the queue exactly once.
    """
    fold = _fold_all(records).get(entry_id)
    return fold if fold is not None else _new_fold(entry_id)


def is_pending(fold):
    """Whether a folded entry is still awaiting scoring.

    THE LOAD-BEARING DISTINCTION. `held` and `attempting` are pending: held work
    is what re-enabling releases, and an interrupted attempt is still owed one.
    `terminal` and `retired` are not: they are finished, and a drain that picked
    them up again is the unbounded-retry bug this lifecycle exists to close.
    """
    return (fold or {}).get("state") in OPEN_STATES


def _pending_from(records, folds):
    """The pending enqueue records, given an already-folded snapshot."""
    seen = {}
    for rec in records:
        entry_id = rec.get("entry_id")
        if not entry_id or rec.get("state") != "pending" or entry_id in seen:
            continue
        if not is_pending(folds.get(entry_id)):
            continue
        seen[entry_id] = rec
    return list(seen.values())


def pending_entries(queue_path):
    """Entries still awaiting scoring.

    The queue is append-only, so an entry's condition is recorded by appending a
    marker naming it rather than by rewriting it, and "pending" is a FOLD over
    those markers (`is_pending`) rather than the absence of one. That is what
    lets held work stay visible AND releasable while terminal and retired work
    stays closed — a flat "has no marker" test could not express both.
    """
    records = read_queue(queue_path)
    return _pending_from(records, _fold_all(records))


def _append_marker(queue_path, marker):
    """Append one lifecycle marker. Returns False rather than raising, because
    a queue that cannot be written degrades the measurement and never the run."""
    if not (queue_path and marker.get("entry_id")):
        return False
    marker.setdefault("marked_at", _utc_now())
    # A stable id per marker, so a terminal record can name the attempts that
    # preceded it and a retry can point back at the terminal record it reopened.
    marker.setdefault("marker_id", uuid.uuid4().hex)
    try:
        line = json.dumps(marker, sort_keys=True)
    except (TypeError, ValueError):
        return False
    try:
        with open(queue_path, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


def mark_drained(queue_path, entry_id, state="drained", detail=None,
                 unverifiable=False):
    """Record that an entry COMPLETED SUCCESSFULLY.

    `drained` now means exactly that and nothing else. A superseded candidate
    used to share this marker, which made retirement indistinguishable from
    completion to anything reading the queue; it has its own `retired` state now
    (see `mark_retired`).

    An entry that fails is no longer silently left pending either: it is
    classified and either retried under its budget or made `terminal`.
    `unverifiable` records that the entry drained against evidence that had
    changed — still a success, but reported as its own breakdown.
    """
    if not (queue_path and entry_id):
        return False
    marker = {"entry_id": entry_id, "state": state}
    if detail:
        marker["detail"] = detail
    if unverifiable:
        marker["unverifiable"] = True
    return _append_marker(queue_path, marker)


def mark_held(queue_path, entry_id, held_reason="policy_off", fold=None):
    """Hold an otherwise-scoreable entry: visible, durable, NOT scored.

    IDEMPOTENT against `fold`. A resume under `off` re-reads the same queue and
    would otherwise append an identical marker at every boundary forever, which
    would turn a held entry's history into noise.

    `eligible_at` is null on purpose: held work is released by a policy change
    or a user action, never by the passage of time.
    """
    if fold is not None and fold.get("state") == "held" \
            and fold.get("held_reason") == held_reason:
        return True
    return _append_marker(queue_path, {
        "entry_id": entry_id, "state": "held", "held_reason": held_reason,
        "eligible_at": None})


def mark_attempt_started(queue_path, entry_id, attempt, limit=MAX_ATTEMPTS):
    """Record an attempt BEFORE it runs.

    Deliberately the pessimistic direction. Writing it afterwards would mean a
    process killed mid-attempt spent a real attempt that nothing recorded — and
    an attempt that is never counted can be repeated without bound, which is the
    bug. Recording it first costs at most one lost attempt after a crash.
    """
    return _append_marker(queue_path, {
        "entry_id": entry_id, "state": "attempt_started",
        "attempt": int(attempt), "limit": int(limit),
        "eligible_at": _utc_now()})


def mark_terminal(queue_path, entry_id, error_class, attempts, limit,
                  transition_history=None):
    """Record that an entry is finished for good: its budget is spent.

    Terminal SURVIVES A RESUME — that is the point. Only an explicit user retry
    reopens it, and the history recorded here is what that retry links back to.
    """
    return _append_marker(queue_path, {
        "entry_id": entry_id, "state": "terminal",
        "error_class": error_class, "attempts": int(attempts),
        "limit": int(limit), "eligible_at": None,
        "transition_history": list(transition_history or ())})


def mark_retired(queue_path, entry_id, detail=None):
    """Retire a superseded final-round candidate.

    Its own state rather than `drained`, because retirement is NOT completion:
    nothing was scored. Sharing the completion marker is what let a retired
    entry be reported as work that finished.
    """
    return _append_marker(queue_path, {
        "entry_id": entry_id, "state": "retired", "eligible_at": None,
        "detail": detail or "superseded by a later round in the same phase"})


def mark_retried(queue_path, entry_id, prior_attempt_ref=None):
    """Explicitly reopen a terminal entry, LINKED to what came before.

    `prior_attempt_ref` names the terminal marker being reopened, so the earlier
    record stays readable verbatim instead of being overwritten by the retry.
    """
    return _append_marker(queue_path, {
        "entry_id": entry_id, "state": "retried",
        "prior_attempt_ref": prior_attempt_ref, "eligible_at": _utc_now()})


def classify_outcome(raw):
    """Normalize whatever `score_fn` returned into `{ok, error_class}`.

    The contract is a classified dict, but a bare bool is still accepted: every
    pre-existing caller and test stub returns one. A legacy `False` becomes
    `unclassified` — a NAMED class with a limit of 1 — rather than being left
    unclassified-and-unbounded, which was the original hole.
    """
    if isinstance(raw, dict):
        ok = bool(raw.get("ok"))
        if ok:
            return {"ok": True, "error_class": None}
        return {"ok": False,
                "error_class": raw.get("error_class") or "unclassified"}
    if raw:
        return {"ok": True, "error_class": None}
    return {"ok": False, "error_class": "unclassified"}


def empty_summary(policy=None):
    """THE one constructor for a drain summary.

    Every key exists and is zeroed in BOTH preview and drain, so a renderer or a
    gate can never KeyError on a quiet queue and never print a blank where a
    count belongs.

    Two scopes live here, and confusing them is exactly how a screen comes to
    lie. The PER-DRAIN fields (`drained`, `failed`, `unverifiable`,
    `superseded`, `terminal`) describe only this pass and exist for callers and
    for the existing tests; none of them is ever rendered as a bucket. The
    WHOLE-QUEUE fields (`pending_running`, `drained_total`, `held`,
    `terminal_total`, plus the `superseded_total` / `unverifiable_total`
    breakdowns) describe the queue as it now stands and are precomputed here so
    the renderer performs no arithmetic. A completed line drawn from the
    per-drain `drained` would read 0 on every preview-rendered screen, however
    much work the queue had actually finished.
    """
    return {
        # Legacy, unchanged in meaning.
        "drained": 0, "failed": 0, "unverifiable": 0, "pending": 0,
        "superseded": 0, "state": "ok",
        # This drain.
        "scoreable": 0, "terminal": 0,
        # The whole queue, as it now stands. These are the rendered figures.
        "pending_running": 0, "drained_total": 0, "held": 0,
        "terminal_total": 0, "terminal_existing": 0, "retired": 0,
        "retire_pending": 0, "superseded_total": 0, "unverifiable_total": 0,
        "transitions": [], "policy": policy,
    }


def _terminal_count(folds):
    return sum(1 for fold in folds.values()
               if fold.get("state") in ("terminal", "failed_permanent"))


def _render_fields(queue_path, phase_closed, terminal_existing):
    """Recompute every WHOLE-QUEUE figure from the queue as it now stands.

    Called at the end of every path through `preview` and `drain` — including
    the policy-off return — so the counts a screen shows are the durable state
    and not a memory of what this process happened to do.

    The four rendered buckets are mutually exclusive and together account for
    every entry in the queue, which is what makes "the counts equal the durable
    state" a thing a test can assert rather than a claim in a docstring.
    """
    records = read_queue(queue_path)
    folds = _fold_all(records)
    pending = _pending_from(records, folds)
    _scoreable, retire, candidate_held = _partition(pending, phase_closed)

    retire_ids = {entry.get("entry_id") for entry in retire}
    # Sets, not sums: an entry can be BOTH user-held and a deferred candidate,
    # and counting it twice would break the "buckets account for everything"
    # invariant in the one case nobody looks at.
    held_ids = {entry_id for entry_id, fold in folds.items()
                if fold.get("state") == "held"}
    held_ids |= {entry.get("entry_id") for entry in candidate_held}
    held_ids |= {entry_id for entry_id, fold in folds.items()
                 if fold.get("state") == "retired"}
    held_ids |= retire_ids
    held_ids.discard(None)

    retired = sum(1 for fold in folds.values()
                  if fold.get("state") == "retired")
    drained_total = sum(1 for fold in folds.values()
                        if fold.get("state") == "drained")
    unverifiable_total = sum(1 for fold in folds.values()
                             if fold.get("state") == "drained"
                             and fold.get("unverifiable"))
    pending_running = sum(
        1 for entry_id, fold in folds.items()
        if fold.get("state") in ("pending", "attempting")
        and entry_id not in held_ids)
    return {
        "pending_running": pending_running,
        "drained_total": drained_total,
        "unverifiable_total": unverifiable_total,
        "held": len(held_ids),
        "retired": retired,
        "retire_pending": len(retire_ids),
        "superseded_total": retired + len(retire_ids),
        "terminal_existing": terminal_existing,
        "terminal_total": _terminal_count(folds),
    }


def preview(queue_path, phase_closed=None, effective_policy=DEFAULT_POLICY):
    """What a drain WOULD do, executing nothing.

    Reads the queue, folds every entry and partitions it, and returns the full
    `empty_summary` shape with the counts a drain would produce. It calls no
    `score_fn` and writes NO marker, so only the per-drain fields are zero —
    every whole-queue figure a screen renders is populated, which is what lets
    the foreground gate be drawn from a preview and still tell the truth about
    work that finished on an earlier pass.

    This is what decides whether a drain is going to block, and it is what that
    screen renders.
    """
    summary = empty_summary(policy=effective_policy)
    try:
        records = read_queue(queue_path)
        folds = _fold_all(records)
        pending = _pending_from(records, folds)
        scoreable, _retire, candidate_held = _partition(pending, phase_closed)
    except Exception:  # noqa: BLE001 - a preview never breaks the run
        summary["state"] = "failed"
        return summary
    # Under `off` nothing is scoreable, because nothing would be scored.
    summary["scoreable"] = 0 if effective_policy == "off" else len(scoreable)
    summary["pending"] = len(candidate_held)
    summary.update(_render_fields(queue_path, phase_closed,
                                  _terminal_count(folds)))
    return summary


def drain(queue_path, score_fn, verify_fn=None, phase_closed=None,
          effective_policy=DEFAULT_POLICY, on_transition=None):
    """Drain every ELIGIBLE pending entry through `score_fn`.

    `score_fn(entry, verification) -> {"ok": bool, "error_class": str|None}`
    runs the isolated evaluator and aggregates its scores. A bare bool is still
    accepted and normalized by `classify_outcome`, so existing callers and stubs
    are unchanged. `verify_fn(envelope) -> dict` re-checks the seal and defaults
    to `cowork_handoff.verify_envelope`.

    THE EFFECTIVE POLICY GOVERNS CONSUMPTION, NOT JUST ENQUEUEING. That is the
    fix: a session set to `off` used to keep spending money draining work queued
    before the switch, because the policy was only ever consulted when work was
    ADDED. Under `off` the scoreable set is HELD — durably, visibly, with a
    reason — and no evaluator turn starts.

    THE POLICY CHECK RUNS AFTER `_partition`, never before. The existing
    final-round candidate hold has to happen first or a `final_round` session
    would start scoring non-final rounds; and the two holds stay distinguishable
    in the record, because a candidate hold writes no marker while a policy hold
    writes one that says why.

    THE SEAL IS RE-VERIFIED BEFORE SCORING COUNTS. An entry whose evidence
    changed while it waited is still passed to `score_fn`, with
    `verification['state'] == 'changed'`, which marks the score `unverifiable`
    and excludes it from aggregates. That is NOT a failure and costs no retry
    budget — re-hashing the file instead would make every score verifiable by
    construction and prove nothing.

    Returns a summary; never raises.
    """
    verify_fn = verify_fn or handoff.verify_envelope
    summary = empty_summary(policy=effective_policy)
    try:
        records = read_queue(queue_path)
        folds = _fold_all(records)
        pending = _pending_from(records, folds)
        entries, retire, candidate_held = _partition(pending, phase_closed)
    except Exception:  # noqa: BLE001
        summary["state"] = "failed"
        return summary
    terminal_existing = _terminal_count(folds)

    def record_transition(entry_id, from_state, to_state, attempt=None,
                          limit=None, error_class=None):
        event = {"entry_id": entry_id, "from_state": from_state,
                 "to_state": to_state, "attempt": attempt, "limit": limit,
                 "error_class": error_class}
        summary["transitions"].append(event)
        if on_transition is not None:
            try:
                on_transition(event)
            except Exception:  # noqa: BLE001 - telemetry never breaks a drain
                pass

    def finish():
        summary.update(_render_fields(queue_path, phase_closed,
                                      terminal_existing))
        return summary

    summary["pending"] += len(candidate_held)

    if effective_policy == "off":
        # HOLD, do not score, and do not retire. Retirement is a scoring-side
        # decision, so deferring it keeps `off` from mutating the queue beyond
        # an explicit, reversible hold. The deferred set is still ACCOUNTED
        # FOR — `_render_fields` counts it as retire_pending inside held/skipped
        # — because an entry that is on disk and in no bucket is how a queue
        # goes quietly wrong.
        for entry in entries:
            entry_id = entry.get("entry_id")
            fold = folds.get(entry_id) or _new_fold(entry_id)
            if fold.get("state") == "held":
                # Already held — including by an explicit user hold, whose
                # reason must not be overwritten by the policy's.
                continue
            if mark_held(queue_path, entry_id, held_reason="policy_off",
                         fold=fold):
                record_transition(entry_id, fold.get("state"), "held")
        return finish()

    summary["scoreable"] = len(entries)
    for entry in retire:
        # A superseded final-round candidate is retired explicitly, so it is
        # neither scored nor left pending forever. Only ever reached once the
        # phase is CLOSED, so it can no longer be superseded by a later round.
        entry_id = entry.get("entry_id")
        mark_retired(queue_path, entry_id)
        summary["superseded"] += 1
        record_transition(entry_id, (folds.get(entry_id) or {}).get("state"),
                          "retired")

    for entry in entries:
        entry_id = entry.get("entry_id")
        fold = folds.get(entry_id) or _new_fold(entry_id)
        # A pre-lifecycle/minimal record keeps its historical behavior: no
        # attempt is recorded, no class is assigned, and a failure leaves it
        # pending and reported as pending. See `_is_prelifecycle_entry`.
        if _is_prelifecycle_entry(entry):
            try:
                envelope = entry.get("envelope")
                verification = verify_fn(envelope) if envelope else {
                    "state": "unknown", "changed": []}
                changed = verification.get("state") == "changed"
                if changed:
                    summary["unverifiable"] += 1
                outcome = classify_outcome(score_fn(entry, verification))
            except Exception:  # noqa: BLE001 - a bad entry never breaks the run
                changed = False
                outcome = {"ok": False, "error_class": "malformed_entry"}
            if outcome["ok"]:
                mark_drained(queue_path, entry_id, unverifiable=changed)
                summary["drained"] += 1
                record_transition(entry_id, fold.get("state"), "drained")
            else:
                summary["failed"] += 1
                summary["pending"] += 1
            continue
        if fold["attempts"] >= MAX_ATTEMPTS:
            # At the ceiling with no outcome recorded: a process died mid
            # attempt. The failure class is genuinely unknowable, so the ceiling
            # is the bound rather than a guess about what went wrong.
            error_class = fold.get("error_class") or "unclassified"
            mark_terminal(queue_path, entry_id, error_class, fold["attempts"],
                          MAX_ATTEMPTS, fold.get("transition_history"))
            summary["terminal"] += 1
            record_transition(entry_id, fold.get("state"), "terminal",
                              attempt=fold["attempts"], limit=MAX_ATTEMPTS,
                              error_class=error_class)
            continue
        attempt = fold["attempts"] + 1
        if not mark_attempt_started(queue_path, entry_id, attempt,
                                    MAX_ATTEMPTS):
            # THE ATTEMPT COULD NOT BE RECORDED, SO IT MUST NOT BE SPENT.
            # Scoring anyway would run a real evaluator against an attempt no
            # drain can ever see: the next fold would still read zero attempts,
            # score it again, and keep doing so — the unbounded retry loop that
            # writing the marker FIRST exists to prevent. Fail in the direction
            # that costs nothing: leave the entry pending and report it, so an
            # unwritable queue delays scoring instead of charging for it
            # forever.
            summary["pending"] += 1
            record_transition(entry_id, fold.get("state"), "pending",
                              attempt=fold["attempts"], limit=MAX_ATTEMPTS)
            continue
        record_transition(entry_id, fold.get("state"), "attempting",
                          attempt=attempt, limit=MAX_ATTEMPTS)
        changed = False
        try:
            envelope = entry.get("envelope")
            verification = verify_fn(envelope) if envelope else {
                "state": "unknown", "changed": []}
            changed = verification.get("state") == "changed"
            if changed:
                summary["unverifiable"] += 1
            outcome = classify_outcome(score_fn(entry, verification))
        except Exception:  # noqa: BLE001 - a bad entry never breaks the run
            outcome = {"ok": False, "error_class": "malformed_entry"}
        if outcome["ok"]:
            mark_drained(queue_path, entry_id, unverifiable=changed)
            summary["drained"] += 1
            record_transition(entry_id, "attempting", "drained",
                              attempt=attempt, limit=MAX_ATTEMPTS)
            continue
        error_class = outcome["error_class"] or "unclassified"
        limit = RETRY_LIMITS.get(error_class, 1)
        summary["failed"] += 1
        if attempt >= limit:
            history = list(fold.get("transition_history") or ())
            mark_terminal(queue_path, entry_id, error_class, attempt, limit,
                          history)
            summary["terminal"] += 1
            record_transition(entry_id, "attempting", "terminal",
                              attempt=attempt, limit=limit,
                              error_class=error_class)
        else:
            # Budget left: left pending on purpose, so the next drain retries it
            # and until then the record reports it as pending, never as scored.
            summary["pending"] += 1
            record_transition(entry_id, "attempting", "pending",
                              attempt=attempt, limit=limit,
                              error_class=error_class)
    return finish()


def queue_dispositions(queue_path):
    """The queue's final dispositions, as the authoritative record consumes them.

    `by_state` ships SCALARS because the report renderer is forbidden from
    computing anything, including counting the members of a collection: a figure
    it had to derive is a figure that can disagree with the record.

    `state` is `ok`, `missing` or `unreadable` (see `read_queue_status`). The
    counts below are only authoritative when it is `ok` or `missing`: on
    `unreadable` they describe whatever could be read, which is an unknown
    fraction of the queue, and the caller is responsible for reporting them as
    unknown rather than as fact.
    """
    by_state = {"pending": 0, "held": 0, "attempting": 0, "terminal": 0,
                "retired": 0, "drained": 0}
    entries = []
    try:
        read = read_queue_status(queue_path)
        # DELIBERATELY NOT `state`: the loop below binds each entry's own
        # lifecycle state, and sharing the name would silently return the LAST
        # entry's state ('drained', 'pending', …) as the queue's read status on
        # every non-empty queue.
        read_state = read["state"]
        folds = _fold_all(read["records"])
    except Exception:  # noqa: BLE001
        # The read itself blew up in a way even the status path did not expect:
        # unreadable, not empty.
        return {"by_state": by_state, "entries": entries,
                "state": QUEUE_UNREADABLE}
    for entry_id in sorted(folds):
        fold = folds[entry_id]
        entry_state = fold.get("state")
        # The legacy spelling is reported as what it is: a terminal failure.
        disposition = ("terminal" if entry_state == "failed_permanent"
                       else entry_state)
        if disposition in by_state:
            by_state[disposition] += 1
        entries.append({
            "entry_id": entry_id,
            "state": entry_state,
            "attempts": fold.get("attempts"),
            "limit": fold.get("limit"),
            "error_class": fold.get("error_class"),
            "eligible_at": fold.get("eligible_at"),
            "held_reason": fold.get("held_reason"),
            "disposition": disposition,
        })
    return {"by_state": by_state, "entries": entries, "state": read_state}


# --------------------------------------------------------------------------- #
# Isolation.                                                                  #
# --------------------------------------------------------------------------- #


def evaluator_identity(identities, role):
    """The controller/model an isolated evaluator must run on for `role`'s seat.

    The evaluator KEEPS the role's own controller and model (P5). The project
    exists to compare Claude against Codex; collapsing every evaluation onto one
    controller would make this run's scores incomparable with the 62 evaluations
    already recorded in the two sessions on disk.
    """
    identity = (identities or {}).get(role)
    if not isinstance(identity, dict):
        return {"tool": None, "model": None, "state": "unknown"}
    return {
        "tool": identity.get("tool") or identity.get("controller"),
        "model": identity.get("model"),
        "effort": identity.get("effort"),
        "state": "ok" if identity.get("tool") else "unknown",
    }


def isolation_violations(trace_events, operational_session_ids):
    """Find evaluation turns that ran on an OPERATIONAL role's session.

    The invariant is the whole point of D2: an evaluation turn must never share
    a controller session with the work it scores. Asserted against the trace
    rather than trusted, because the failure is silent — a contaminated score
    looks exactly like a clean one.
    """
    violations = []
    operational = set(operational_session_ids or ())
    for event in trace_events or []:
        if event.get("work_class") != "evaluation":
            continue
        identity = event.get("identity") or {}
        session_id = identity.get("controller_session_id")
        if session_id and session_id in operational:
            violations.append({
                "work_id": event.get("work_id"),
                "controller_session_id": session_id,
                "role": event.get("role"),
            })
    return violations


def ordering_violations(trace_events):
    """Find rounds whose evaluation turn started BEFORE the correction handoff
    was recorded.

    Criterion 4's ordering invariant: scoring must never sit between a
    reviewer's verdict and the fix going back to the role. A violation here
    means measurement delayed the work, which is the one thing a passive
    measurement system may not do.
    """
    handoffs = {}
    violations = []
    for event in trace_events or []:
        name = event.get("event")
        key = (event.get("phase"), event.get("round"))
        if name == "review.handoff.recorded":
            handoffs.setdefault(key, event.get("ts"))
        elif name == "eval.turn.start":
            recorded = handoffs.get(key)
            if recorded is None or (event.get("ts") or "") < recorded:
                violations.append({
                    "phase": key[0], "round": key[1],
                    "eval_started_at": event.get("ts"),
                    "handoff_recorded_at": recorded,
                })
    return violations
