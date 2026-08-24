#!/usr/bin/env python3
"""M3 Package F -- manual/emergency verification adapter.

Holds NO private key and contains NO signing path of any kind (see
`test_cowork_wake_manual.py`'s `NoSelfSignPathTest`, an AST-level structural
gate proving this source references none of Package B's private signing
helpers). The actual signer is an external, out-of-repo, human/hardware-
backed authority -- not an M3 artifact -- who produces a detached Ed25519
signature over `cowork_state.canonical_manual_capacity_signal_message` by
whatever means they choose; this module never participates in producing
that signature, only in verifying and durably recording one it is handed.

Exactly ONE verify-and-record path (F-N04) -- both this module's own public
API and its CLI (`run_verify`) call the SAME function,
`verify_and_record_manual_signal`, which makes exactly ONE call into
Package B (`cowork_state.write_manual_capacity_signal`): Package B's own
WRITE-TIME cryptographic-verification boundary (see its docstring) performs
the genuine Ed25519 verification against the caller-pinned public-key
registry FIRST, internally, and writes nothing at all unless that
verification genuinely succeeds -- an unsigned, malformed, or wrong-key
record raises and nothing is ever durably recorded. There is no separate,
parallel verify-then-write sequence anywhere else in this module.

A single Package B call can fail for several DISTINCT reasons, and this
module classifies which one occurred -- `classify_manual_signal_error`
(F-N05) -- without re-deriving or reimplementing any of Package B's own
logic, only labeling the exception Package B already raised:

  * `state_store.ManualSignalSignatureError` -> `"verification_failed"`
    (bad, unpinned, or malformed signature).
  * `state_store.CorruptRecordError` -> `"corrupt_state"`, DISTINCT from an
    ordinary conflict: Package B refuses to silently overwrite or discard
    an existing but unparseable on-disk record (M3B-REV-M03) rather than
    treating damaged state as merely absent.
  * a plain `ValueError` naming Package B's own documented conflict
    wording -> `"journal_conflict"` (a validly parsed, genuinely different
    prior record already occupies this exact `signal_journal_ref`).
  * any other plain `ValueError` -> `"invalid_arguments"` (malformed record
    shape).

This module performs no eligibility or locking/storage decision of its own,
and it does not itself claim any PauseLease -- that remains Package D's
`claim_with_authorized_early_override`, a separate, later step some other
caller takes using the now-durably-verified evidence this module recorded.

Public API:
    VERIFICATION_EXIT_CODES (success, internal_error, invalid_arguments,
        verification_failed, journal_conflict, corrupt_state)
    verify_and_record_manual_signal(session_uuid, record, pinned_public_keys)
        -- the ONE verify-and-record path; raises on failure
    classify_manual_signal_error(exc) -> one of VERIFICATION_EXIT_CODES'
        keys (excluding "success")
    build_arg_parser(), main(argv)

Python 3.9+, stdlib plus `cowork_state` (Package B) only.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_state as state_store  # Package B

# --------------------------------------------------------------------------- #
# Versioned exit-code contract (mirrors Package D's own by-name convention:  #
# callers consult this mapping by NAME, never a bare literal integer).       #
# --------------------------------------------------------------------------- #

VERIFICATION_EXIT_SUCCESS = 0
VERIFICATION_EXIT_INTERNAL_ERROR = 1
VERIFICATION_EXIT_INVALID_ARGUMENTS = 2
VERIFICATION_EXIT_VERIFICATION_FAILED = 3
VERIFICATION_EXIT_JOURNAL_CONFLICT = 4
VERIFICATION_EXIT_CORRUPT_STATE = 5

VERIFICATION_EXIT_CODES = {
    "success": VERIFICATION_EXIT_SUCCESS,
    "internal_error": VERIFICATION_EXIT_INTERNAL_ERROR,
    "invalid_arguments": VERIFICATION_EXIT_INVALID_ARGUMENTS,
    "verification_failed": VERIFICATION_EXIT_VERIFICATION_FAILED,
    "journal_conflict": VERIFICATION_EXIT_JOURNAL_CONFLICT,
    "corrupt_state": VERIFICATION_EXIT_CORRUPT_STATE,
}


# --------------------------------------------------------------------------- #
# Core: the ONE verify-and-record path (F-N04). A single call into Package  #
# B; nothing here re-derives or duplicates Package B's own logic.            #
# --------------------------------------------------------------------------- #


def verify_and_record_manual_signal(session_uuid, record, pinned_public_keys):
    """The SOLE verify-and-record path -- both a programmatic caller and
    this module's own CLI (`run_verify`) call exactly this function; there
    is no second, parallel implementation anywhere in this module.

    Delegates ENTIRELY, in ONE call, to Package B's own write-time
    verification boundary: `write_manual_capacity_signal` performs the
    genuine Ed25519 verification against the caller-pinned
    `pinned_public_keys` registry FIRST (internally), and writes nothing at
    all unless that verification genuinely succeeds. Raises `state_store.
    ManualSignalSignatureError` (bad/unpinned/malformed signature),
    `state_store.CorruptRecordError` (the ON-DISK record at this exact
    journal ref exists but fails to parse), or a plain `ValueError`
    (malformed record shape, or a genuinely different prior record already
    occupies this `signal_journal_ref`) -- see `classify_manual_signal_
    error` for how a caller distinguishes these. Returns the durably
    stored, verified record on success."""
    return state_store.write_manual_capacity_signal(
        session_uuid, record, pinned_public_keys)


# Package B's own fixed, documented wording for a genuine content conflict
# (see `cowork_state.write_manual_capacity_signal`'s docstring/`mutate`) --
# read-only text classification of an exception Package B ALREADY raised,
# never a reimplementation of Package B's own conflict decision. Pinned by
# `test_cowork_wake_manual.py`'s `test_journal_conflict_marker_matches_
# package_bs_actual_wording`, which triggers a REAL conflict and asserts
# this marker still matches -- so any future drift in Package B's wording
# fails this module's own test suite loudly rather than silently
# misclassifying a conflict as `"invalid_arguments"`.
_JOURNAL_CONFLICT_MESSAGE_MARKER = "already recorded with different content"


def classify_manual_signal_error(exc):
    """F-N05: classify an exception raised by `verify_and_record_manual_
    signal` (the ONE call into Package B) into one of this module's own
    distinct, NAMED dispositions -- never re-derives WHY Package B raised
    it, only labels the exception Package B already threw. Order matters:
    `ManualSignalSignatureError` and `CorruptRecordError` are both
    `ValueError` subclasses, so both are checked before the plain-
    `ValueError` fallback. Returns one of `VERIFICATION_EXIT_CODES`'s keys
    (excluding `"success"`, which this function is never called for)."""
    if isinstance(exc, state_store.ManualSignalSignatureError):
        return "verification_failed"
    if isinstance(exc, state_store.CorruptRecordError):
        return "corrupt_state"
    if isinstance(exc, ValueError):
        if _JOURNAL_CONFLICT_MESSAGE_MARKER in str(exc):
            return "journal_conflict"
        return "invalid_arguments"
    return "internal_error"


# --------------------------------------------------------------------------- #
# CLI.                                                                        #
# --------------------------------------------------------------------------- #


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="cowork_wake_manual",
        description="M3 Package F manual/emergency verification adapter.")
    sub = parser.add_subparsers(dest="command", required=True)

    verify_p = sub.add_parser(
        "verify", help="verify a caller-supplied signed record and journal it")
    verify_p.add_argument("--session-uuid", required=True)
    verify_p.add_argument(
        "--record-file", required=True,
        help="path to a JSON file holding the caller-supplied signed record")
    verify_p.add_argument(
        "--pinned-keys-file", required=True,
        help="path to a JSON file holding {signer_public_key_id: "
             "64-lowercase-hex-char Ed25519 public key}")
    return parser


def _read_json_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def run_verify(session_uuid, record_file, pinned_keys_file, output=None):
    """Runs the `verify` subcommand's full flow: read both input files,
    then call `verify_and_record_manual_signal` -- the ONE verify-and-record
    path (F-N04), the SAME function a programmatic caller uses. Writes
    exactly one JSON result line to `output` (defaults to
    `sys.stdout.write`) and returns one of `VERIFICATION_EXIT_CODES`. Never
    raises past this function -- every failure mode is translated to a
    truthful JSON payload plus exit code via `classify_manual_signal_error`
    (F-N05)."""
    write = output if output is not None else sys.stdout.write

    try:
        record = _read_json_file(record_file)
    except (OSError, json.JSONDecodeError) as exc:
        write(json.dumps({
            "outcome": "invalid_arguments",
            "detail": "could not read --record-file: %s" % exc}) + "\n")
        return VERIFICATION_EXIT_CODES["invalid_arguments"]

    try:
        pinned_public_keys = _read_json_file(pinned_keys_file)
    except (OSError, json.JSONDecodeError) as exc:
        write(json.dumps({
            "outcome": "invalid_arguments",
            "detail": "could not read --pinned-keys-file: %s" % exc}) + "\n")
        return VERIFICATION_EXIT_CODES["invalid_arguments"]

    try:
        stored = verify_and_record_manual_signal(
            session_uuid, record, pinned_public_keys)
    except OSError as exc:
        write(json.dumps({
            "outcome": "internal_error", "detail": str(exc)}) + "\n")
        return VERIFICATION_EXIT_CODES["internal_error"]
    except ValueError as exc:
        disposition = classify_manual_signal_error(exc)
        write(json.dumps({
            "outcome": disposition, "detail": str(exc)}) + "\n")
        return VERIFICATION_EXIT_CODES[disposition]

    write(json.dumps({"outcome": "success", "record": stored}) + "\n")
    return VERIFICATION_EXIT_CODES["success"]


def main(argv, output=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_verify(args.session_uuid, args.record_file,
                      args.pinned_keys_file, output=output)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
