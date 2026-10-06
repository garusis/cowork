#!/usr/bin/env python3
"""Pure blocking-gate and closure predicates over an already-read authority chain.

Three functions, one definition of "blocked" and "closed":

    blocking_decision(read_meta, folded_state, session_uuid, phase, gate_candidate)
    closure_decision(folded_state, finding_id, gate_candidate, witness_screens,
                     proposal_coverage, consumed_decisions)
    validate_proposal(proposal, folded_state, *, gate_candidate, session_uuid,
                      phase, proposal_id)

PURE BY CONSTRUCTION. Every input is an already-read value (a chain read, a
folded state, a candidate object): nothing here opens a file, reads the
environment or a clock, or imports any cowork module except
`cowork_authority_candidate`. No input is mutated and every result is a fresh
object. This module is not a runtime call site.

ONE OWNER OF CANDIDATE EQUALITY. Every candidate check goes through
`cowork_authority_candidate` (`validate_candidate`, `candidate_compare`); a
candidate dict is never compared here by `==`, by `kind` or by a digest key.

FAIL CLOSED. An unreadable chain, a missing or malformed gate candidate, a stale
or foreign finding, an unconsumed or null-candidate decision, a reviewer-only
recommendation and a proposal-only claim never unblock or close anything.
Missing optional collections (proposals, recommendations, rounds, decisions) are
treated as empty, which can only remove a closure basis, never add one.

CLOSED REGISTRIES. `GATE_REASON_CODES` and `CLOSURE_REASON_CODES` are the whole
set of codes `blocking_decision` and `closure_decision` return.

RESOLVED SEMANTICS (each pinned by a named test):

  * Current round: the LATEST entry of `rounds` whose phase is the finding's
    phase. It is current only when its candidate equals the gate candidate.
    Rounds and recommendations are keyed by `round_id`, never the integer round.
    A finding is recommended-closed when at least one recommendation for it on
    that round says `closed` and none says still_open, withdrawn or duplicate.
  * Decision basis: a decision counts only when its candidate is valid and
    non-null, `consumed_decisions[request_id]` equals its `answer_sha256`, its
    principal is supervisor_agent or policy_principal, and it adjudicates the
    finding. It need not equal the gate candidate. The latest such decision
    wins: a later uphold supersedes an earlier close and an uphold never closes.
    An uphold does not veto the proposal-plus-recommendation path.
  * Closing predicate: an authorized consumed close decision, OR a valid
    proposal plus a current-round closed recommendation (a supervisor
    adjudication beside a proposal is subsumed by the first branch); in both
    cases the witness check must pass.
  * Witness: a finding with a `claim_class` needs a NON-EMPTY screen set (a list
    or a dict of screens) in which every screen is approvable. A finding without
    a claim_class ignores the screens.
  * Capability veto (literal): any VALID proposal citing the finding whose
    `requires` is non-empty and whose `proposal_coverage` entry is not literally
    True forces close=False with `capability_not_granted`, whatever other basis
    exists. A stale or invalid proposal carries no veto. An absent or None
    `requires` means [] and is not a defect; a present malformed one is invalid.
  * An unprovable session never closes: an empty or missing
    `folded_state.session_uuid` is `foreign_binding` in `closure_decision` and
    `proposal_invalid` in `validate_proposal`.
  * `validate_proposal(proposal, folded_state)` called with two arguments is an
    id and structure check ONLY (cited ids exist, are open or closed, and share
    session and phase). It checks no candidate and is never a validity or
    closure verdict. The keywords supply the rest; both decision predicates
    always pass all four.

Python 3.9+, stdlib only.
"""

import cowork_authority_candidate as _cand

GATE_REASON_CODES = (
    "unresolved",
    "stale_candidate",
    "foreign_binding",
    "chain_unreadable",
    "head_changed",
    "proposal_invalid",
    "candidate_unavailable",
)

CLOSURE_REASON_CODES = (
    "capability_not_granted",
    "candidate_unavailable",
    "stale_candidate",
    "proposal_invalid",
    "finding_unknown",
    "finding_not_open",
    "foreign_binding",
    "no_resolution_basis",
    "witness_not_approvable",
)

DECISION_PRINCIPALS = ("supervisor_agent", "policy_principal")

_FINDING_STATES = ("open", "closed", "withdrawn", "duplicate")
_PROPOSAL_FINDING_STATES = ("open", "closed")
_NOT_CLOSED_RECOMMENDATIONS = ("still_open", "withdrawn", "duplicate")

_UNSET = object()


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _valid_candidate(candidate):
    return _cand.validate_candidate(candidate)[0]


def _same_candidate(a, b):
    return _cand.candidate_compare(a, b)[0]


def _reason(finding_id, code):
    return {"id": finding_id, "code": code}


def _non_empty_str(value):
    return isinstance(value, str) and value != ""


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_gating(record):
    """True for an open finding that blocks: open and blocking not literally
    False, or any state outside the four known ones."""
    state = record.get("state")
    if state not in _FINDING_STATES:
        return True
    return state == "open" and record.get("blocking") is not False


def validate_proposal(proposal, folded_state, *, gate_candidate=_UNSET,
                      session_uuid=_UNSET, phase=None, proposal_id=None):
    """[{id, code: 'proposal_invalid'}] for a proposal, empty when valid.

    Called with two arguments this is an id and structure check only. With
    `gate_candidate` it also requires `changed_evidence.candidate` to equal it.
    `session_uuid` defaults to folded_state.session_uuid and `phase` to the
    phase of the first cited finding that exists. A cited-id defect is keyed by
    the finding id; a proposal-level defect is keyed by `proposal_id`.
    """
    proposal_level = [_reason(proposal_id, "proposal_invalid")]
    if not isinstance(proposal, dict) or not isinstance(folded_state, dict):
        return proposal_level
    findings = folded_state.get("findings")
    if not isinstance(findings, dict):
        return proposal_level
    cited = proposal.get("finding_ids")
    if not isinstance(cited, list) or not cited or not _is_str_list(cited):
        return proposal_level
    evidence = proposal.get("changed_evidence")
    if not isinstance(evidence, dict):
        return proposal_level
    requires = proposal.get("requires")
    if requires is not None and not _is_str_list(requires):
        return proposal_level

    session = session_uuid if _non_empty_str(session_uuid) else None
    if session is None:
        folded_session = folded_state.get("session_uuid")
        session = folded_session if _non_empty_str(folded_session) else None
    if session is None:
        return proposal_level
    if gate_candidate is not _UNSET and not _same_candidate(
            evidence.get("candidate"), gate_candidate):
        return proposal_level

    if not _non_empty_str(phase):
        phase = None
        for finding_id in cited:
            record = findings.get(finding_id)
            if isinstance(record, dict) and _non_empty_str(record.get("phase")):
                phase = record["phase"]
                break

    reasons = []
    seen = set()
    for finding_id in cited:
        if finding_id in seen:
            continue
        seen.add(finding_id)
        record = findings.get(finding_id)
        if (not isinstance(record, dict)
                or record.get("state") not in _PROPOSAL_FINDING_STATES
                or record.get("session_uuid") != session
                or (phase is not None and record.get("phase") != phase)):
            reasons.append(_reason(finding_id, "proposal_invalid"))
    return reasons


def _cites_gating_finding(proposal, findings):
    if not isinstance(proposal, dict):
        return False
    cited = proposal.get("finding_ids")
    if not isinstance(cited, list):
        return False
    for finding_id in cited:
        if not isinstance(finding_id, str):
            continue
        record = findings.get(finding_id)
        if isinstance(record, dict) and record.get("state") == "open" \
                and record.get("blocking") is not False:
            return True
    return False


def blocking_decision(read_meta, folded_state, session_uuid, phase, gate_candidate):
    """{blocked, reasons: [{id, code}], head} for a gate in `phase`.

    An unreadable chain short-circuits to chain_unreadable. Otherwise reasons
    accumulate in a fixed order: chain-level (head_changed, a folded-session
    foreign_binding, candidate_unavailable), then per finding in folded order
    (foreign_binding, else unresolved or stale_candidate), then proposal_invalid
    for an invalid proposal that cites an open blocking finding.
    """
    meta = read_meta if isinstance(read_meta, dict) else None
    findings = folded_state.get("findings") if isinstance(folded_state, dict) else None
    if meta is None or meta.get("ok") is not True or not isinstance(findings, dict):
        head = meta.get("head") if meta is not None else None
        return {
            "blocked": True,
            "reasons": [_reason(None, "chain_unreadable")],
            "head": head,
        }

    reasons = []
    if meta.get("head") != folded_state.get("head"):
        reasons.append(_reason(None, "head_changed"))
    if folded_state.get("session_uuid") != session_uuid:
        reasons.append(_reason(None, "foreign_binding"))
    gate_ok = _valid_candidate(gate_candidate)
    if not gate_ok:
        reasons.append(_reason(None, "candidate_unavailable"))

    for finding_id, record in findings.items():
        if not isinstance(record, dict):
            reasons.append(_reason(finding_id, "foreign_binding"))
            continue
        if not _is_gating(record):
            continue
        if record.get("session_uuid") != session_uuid or record.get("phase") != phase:
            reasons.append(_reason(finding_id, "foreign_binding"))
        elif gate_ok:
            if _same_candidate(record.get("candidate"), gate_candidate):
                reasons.append(_reason(finding_id, "unresolved"))
            else:
                reasons.append(_reason(finding_id, "stale_candidate"))

    if gate_ok:
        for proposal_id, proposal in _as_dict(folded_state.get("proposals")).items():
            if not _cites_gating_finding(proposal, findings):
                continue
            reasons.extend(validate_proposal(
                proposal, folded_state, gate_candidate=gate_candidate,
                session_uuid=session_uuid, phase=phase, proposal_id=proposal_id))

    return {
        "blocked": bool(reasons),
        "reasons": reasons,
        "head": folded_state.get("head"),
    }


def _proposals_for(folded_state, finding_id, finding, gate_candidate):
    """(valid proposal ids in order, whether any citing proposal is invalid)."""
    valid = []
    any_invalid = False
    for proposal_id, proposal in _as_dict(folded_state.get("proposals")).items():
        if not isinstance(proposal, dict):
            continue
        cited = proposal.get("finding_ids")
        if not isinstance(cited, list) or finding_id not in cited:
            continue
        errors = validate_proposal(
            proposal, folded_state, gate_candidate=gate_candidate,
            session_uuid=folded_state.get("session_uuid"),
            phase=finding.get("phase"), proposal_id=proposal_id)
        if errors:
            any_invalid = True
        else:
            valid.append(proposal_id)
    return valid, any_invalid


def _capability_veto(folded_state, valid_ids, proposal_coverage):
    """The first requires token of the first valid proposal that is not
    covered, else None."""
    proposals = _as_dict(folded_state.get("proposals"))
    coverage = _as_dict(proposal_coverage)
    for proposal_id in valid_ids:
        requires = proposals[proposal_id].get("requires")
        if requires and coverage.get(proposal_id) is not True:
            return requires[0]
    return None


def _current_round_recommends_closed(folded_state, finding_id, phase, gate_candidate):
    if not _non_empty_str(phase):
        return False
    rounds = folded_state.get("rounds")
    latest = None
    for entry in rounds if isinstance(rounds, list) else ():
        if isinstance(entry, dict) and entry.get("phase") == phase:
            latest = entry
    if latest is None or not _same_candidate(latest.get("candidate"), gate_candidate):
        return False
    round_id = latest.get("round_id")
    if round_id is None:
        return False
    entries = _as_dict(folded_state.get("recommendations")).get(finding_id)
    closed = False
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or entry.get("round_id") != round_id:
            continue
        recommend = entry.get("recommend")
        if recommend == "closed":
            closed = True
        elif recommend in _NOT_CLOSED_RECOMMENDATIONS:
            return False
    return closed


def _adjudication_close(folded_state, finding_id, consumed_decisions):
    consumed = _as_dict(consumed_decisions)
    outcome = None
    for decision in _as_dict(folded_state.get("decisions")).values():
        if not isinstance(decision, dict):
            continue
        if not _valid_candidate(decision.get("candidate")):
            continue
        request_id = decision.get("request_id")
        answer = decision.get("answer_sha256")
        if not _non_empty_str(request_id) or not _non_empty_str(answer):
            continue
        if consumed.get(request_id) != answer:
            continue
        if decision.get("principal") not in DECISION_PRINCIPALS:
            continue
        adjudicates = decision.get("adjudicates")
        for entry in adjudicates if isinstance(adjudicates, list) else ():
            if (isinstance(entry, dict) and entry.get("finding_id") == finding_id
                    and entry.get("outcome") in ("close", "uphold")):
                outcome = entry["outcome"]
    return outcome == "close"


def _witness_ok(finding, witness_screens):
    if finding.get("claim_class") is None:
        return True
    if isinstance(witness_screens, dict):
        screens = list(witness_screens.values())
    elif isinstance(witness_screens, (list, tuple)):
        screens = list(witness_screens)
    else:
        return False
    return bool(screens) and all(
        isinstance(screen, dict) and screen.get("approvable") is True
        for screen in screens)


def _refusal(*codes, escalation_needed=None):
    return {"close": False, "reasons": list(codes), "escalation_needed": escalation_needed}


def closure_decision(folded_state, finding_id, gate_candidate, witness_screens,
                     proposal_coverage, consumed_decisions):
    """{close, reasons: [code], escalation_needed} for one finding.

    Returns the first matching refusal tier: finding unknown, finding not open,
    gate candidate unavailable, foreign session, capability veto, witness not
    approvable, no basis. Closes (reasons [] and escalation_needed None) only on
    a consumed authorized close decision, or on a valid proposal plus a
    current-round closed recommendation, and only when the witness screens
    approve.
    """
    findings = folded_state.get("findings") if isinstance(folded_state, dict) else None
    finding = findings.get(finding_id) if isinstance(findings, dict) \
        and isinstance(finding_id, str) else None
    if not isinstance(finding, dict):
        return _refusal("finding_unknown")
    if finding.get("state") != "open":
        return _refusal("finding_not_open")
    if not _valid_candidate(gate_candidate):
        return _refusal("candidate_unavailable")
    session = folded_state.get("session_uuid")
    if not _non_empty_str(session) or finding.get("session_uuid") != session:
        return _refusal("foreign_binding")

    valid_ids, any_invalid = _proposals_for(folded_state, finding_id, finding, gate_candidate)
    token = _capability_veto(folded_state, valid_ids, proposal_coverage)
    if token is not None:
        return _refusal("capability_not_granted", escalation_needed=token)

    adjudicated = _adjudication_close(folded_state, finding_id, consumed_decisions)
    recommended = bool(valid_ids) and _current_round_recommends_closed(
        folded_state, finding_id, finding.get("phase"), gate_candidate)
    if adjudicated or recommended:
        if not _witness_ok(finding, witness_screens):
            return _refusal("witness_not_approvable")
        return {"close": True, "reasons": [], "escalation_needed": None}

    reasons = []
    if not _same_candidate(finding.get("candidate"), gate_candidate):
        reasons.append("stale_candidate")
    if any_invalid:
        reasons.append("proposal_invalid")
    reasons.append("no_resolution_basis")
    return _refusal(*reasons)
