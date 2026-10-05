#!/usr/bin/env python3
"""Session rotation decision.

A pure function over boundary facts: `decide_rotation(boundary, facts)` returns
exactly one closed reason code. Nothing here reads state, writes a record, emits
a trace event or alters dispatch; the caller gathers the facts and acts on the
code. A session that is not profiled never rotates.

The decision fails closed. A safety fact that is not exactly `False`, a missing
fact, a non-boolean fact and an undeclared fact all withhold rotation: a
gathering mistake can only keep a session, never rotate one. The only two
triggers are `rotate_recommended` (the role's latest context decision for its
chain) and `warn_reached` (its last completed turn reached the warn limit).

Facts (all booleans, supplied by the caller; FACT_MEANINGS states, per fact, the
precondition it stands for and the reader that produces it):

  state_readable, profiled, capacity_pause_in_flight, pending_turn,
  pending_switch, open_decision, context_gap, work_in_flight,
  verification_in_flight, first_turn_of_epoch, rotate_recommended,
  warn_reached.

Python 3.9+, stdlib only.
"""

import os
import sys
from collections.abc import Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_context as context  # noqa: E402

ROTATE = "rotate"

REASON_CODES = (
    ROTATE,
    "defer_not_boundary",
    "defer_first_turn_of_epoch",
    "refuse_capacity_pause_in_flight",
    "refuse_pending_turn",
    "refuse_pending_switch",
    "refuse_open_decision",
    "refuse_context_gap",
    "refuse_work_in_flight",
    "refuse_verification_in_flight",
    "refuse_unreadable_state",
    "refuse_unprofiled",
    "refuse_no_trigger",
)

# The trigger that licenses rotation at each boundary.
TRIGGER_FACT_BY_BOUNDARY = {
    "lead_after_phase_approved": "rotate_recommended",
    "reviewer_between_rounds": "rotate_recommended",
    "lead_before_correction_round": "warn_reached",
}

# Facts that must each be exactly False, checked in this order.
SAFETY_CHECKS = (
    ("capacity_pause_in_flight", "refuse_capacity_pause_in_flight"),
    ("pending_turn", "refuse_pending_turn"),
    ("pending_switch", "refuse_pending_switch"),
    ("open_decision", "refuse_open_decision"),
    ("context_gap", "refuse_context_gap"),
    ("work_in_flight", "refuse_work_in_flight"),
    ("verification_in_flight", "refuse_verification_in_flight"),
)

# fact -> (the precondition it stands for, the existing reader that supplies it)
FACT_MEANINGS = {
    "state_readable": (
        "every durable input consulted was readable: state.json loads and "
        "list_rotation_records has no 'unreadable' entry for the role "
        "(unreadable state keeps the session)",
        "cowork_state.load, cowork_state.list_rotation_records"),
    "profiled": (
        "the session carries an execution profile; an unprofiled session "
        "never rotates",
        "the session's profile record (profile_session is not None)"),
    "capacity_pause_in_flight": (
        "an in-flight capacity pause or hold exists for the role, including "
        "a capacity-held decision for the role",
        "the capacity hold readers (cowork _capacity_turn_role_hold)"),
    "pending_turn": (
        "a pending turn before pause exists for the role, acknowledged or "
        "not",
        "cowork_state.read_pending_turn_before_pause is not None"),
    "pending_switch": (
        "a controller switch for the role still carries a pending turn",
        "cowork_state.read_pending_switch"),
    "open_decision": (
        "an open orchestrator decision request that is not capacity-held "
        "exists for the role",
        "the session decision-request reader in cowork_state"),
    "context_gap": (
        "the role has not yet been given the current shared context revision",
        "cowork_state.role_context_gap is not None"),
    "work_in_flight": (
        "the role has an in-flight work id",
        "the role's work-entry ledger rows"),
    "verification_in_flight": (
        "an owned verification transaction for the candidate is in flight",
        "the owned verification transaction records"),
    "first_turn_of_epoch": (
        "the next turn would be the first turn of the role's phase epoch",
        "the role's session and work-entry rows for the current phase"),
    "rotate_recommended": (
        "the role's latest context decision for its chain was "
        "rotate_recommended",
        "cowork_context.evaluate over the role's observation"),
    "warn_reached": (
        "the role's last completed turn reported input at or beyond the warn "
        "limit",
        "cowork_context.evaluate over the role's observation"),
}
FACT_KEYS = tuple(FACT_MEANINGS)


def _is_true(value):
    return value is True


def _is_false(value):
    return value is False


def decide_rotation(boundary, facts):
    """The reason code for rotating the role's session at `boundary`.

    `boundary` is None when the dispatch is not at a boundary; otherwise it must
    be one of `cowork_context.BOUNDARIES` (anything else is a programming error
    and raises ValueError). `facts` is a mapping of the FACT_KEYS to booleans.
    Returns ROTATE only when the session is profiled, every precondition holds
    and the boundary's trigger is present."""
    if boundary is None:
        return "defer_not_boundary"
    if not isinstance(boundary, str) or boundary not in context.BOUNDARIES:
        raise ValueError("unknown rotation boundary: %r" % (boundary,))
    if not isinstance(facts, Mapping):
        return "refuse_unreadable_state"
    if any(not isinstance(key, str) or key not in FACT_MEANINGS
           for key in facts):
        return "refuse_unreadable_state"
    if not _is_true(facts.get("state_readable")):
        return "refuse_unreadable_state"
    if not _is_true(facts.get("profiled")):
        return "refuse_unprofiled"
    for fact, code in SAFETY_CHECKS:
        if not _is_false(facts.get(fact)):
            return code
    if not _is_false(facts.get("first_turn_of_epoch")):
        return "defer_first_turn_of_epoch"
    if not _is_true(facts.get(TRIGGER_FACT_BY_BOUNDARY[boundary])):
        return "refuse_no_trigger"
    return ROTATE
