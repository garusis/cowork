#!/usr/bin/env python3
"""Focused suite for the verification permit/exit stall
(garusis/cowork-internal#79).

The incident: the owned-verification worker recorded `skipped_no_permit`
for two inventory entries whose permits did not arrive in time, then
exited -- and the parent, which matched ONLY `event == "terminal"` and
never observed the worker's exit, kept polling for evidence a dead worker
could not produce. Its poll was bounded by an attempt COUNT sized once from
the remaining deadline and never re-checked against the clock, so the
package could neither advance nor report a terminal failure; the exited
worker was left unreaped.

What is proven here, against the real code (no live providers, no long
sleeps, deterministic clocks/fakes, and only short-lived bounded real
subprocesses where genuine OS-level liveness is the thing under test):

  * every one of the worker's three deliberate-skip events resolves AT ONCE
    as terminal-but-UNEXECUTED (`EVIDENCE_ABSENT`, `exit_code=None`,
    `skipped=True`, `skip_reason`), is never waited for, and can never be
    adjudicated `pass`;
  * a permit that lands AFTER its entry was skipped never starts the
    command -- the skip is sticky and no later permit is honored;
  * an exited worker ends the parent's wait promptly, WITHOUT collapsing
    the genuine uncertainty of a command process group that is still alive;
  * a clock that jumps past the deadline mid-wait stops the wait, instead
    of spending every remaining attempt past it;
  * a permit wait that fails once is not repeated for every remaining
    entry -- one wait, then an immediate durable skip record per entry;
  * the parent spine emits a truthful terminal receipt after the skips and
    the worker's exit, preserving the evidence of everything that DID
    complete and never claiming the unexecuted gates passed.

Backward compatibility is proven too: the new parameters are optional and
default to the exact pre-existing behavior, the seam's re-export identity
is unchanged, and an injected evidence-wait double with the pre-existing
narrower signature is still called with the pre-existing argument shape.

Run standalone:

    python3 -m unittest scripts.test_verification_permit_exit -v
"""

import ast
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_state as state_store  # noqa: E402
import cowork_ledger as ledger  # noqa: E402
import cowork_verification as verification  # noqa: E402
import cowork_verification_evidence as evidence_module  # noqa: E402


_PYTHON = sys.executable or "python3"


def _recognized_skip_events():
    """The deliberate-skip event names `bounded_evidence_wait` recognizes,
    read out of that function's OWN source -- specifically, out of its
    `skip_events` tuple, located by parsing rather than by matching text,
    so the synthetic `"skipped_at"` key the same function writes into its
    result is never mistaken for one of them.

    Production deliberately keeps these as function-local literals rather
    than a module constant, so reading the source is the only way to assert
    against what production actually recognizes instead of re-declaring the
    list here -- a copy in the test would still pass if production's own
    tuple were emptied. `test_skip_events_match_what_the_worker_emits`
    below pins both the count and the exact membership against the worker's
    real emissions, so an empty or drifted production tuple fails there.
    """
    tree = ast.parse(inspect.getsource(evidence_module.bounded_evidence_wait))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name)
                   and target.id == "skip_events"
                   for target in node.targets):
            continue
        if not isinstance(node.value, ast.Tuple):
            break
        return tuple(element.value for element in node.value.elts
                     if isinstance(element, ast.Constant))
    raise AssertionError(
        "bounded_evidence_wait no longer binds a `skip_events` tuple -- "
        "skip recognition cannot be verified")


def _reap(proc):
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001 - best-effort teardown in tests
        pass


class _SessionFixture(unittest.TestCase):
    """An isolated `COWORK_SESSIONS_ROOT`, one session/transaction id pair,
    and a registry of real subprocesses this test owns (always reaped, even
    on failure)."""

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.transaction_id = "T-" + uuid.uuid4().hex[:8]
        self._procs = []
        self.addCleanup(self._reap_all)

    def _reap_all(self):
        for proc in self._procs:
            _reap(proc)

    # -- real, short-lived process-group evidence ------------------------- #

    def spawn_sleeper(self, seconds=10):
        """A real subprocess in its OWN process group: genuine `_pgid_alive`
        evidence, which no fake can stand in for without hollowing out the
        live-child-uncertainty case this suite must preserve."""
        proc = subprocess.Popen(
            [_PYTHON, "-c", "import time; time.sleep(%r)" % seconds],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(0.05)
        self._procs.append(proc)
        return proc, os.getpgid(proc.pid)

    # -- durable fixtures on the real on-disk paths ----------------------- #

    def events_path(self):
        return state_store.verification_attempt_events_path_for(
            self.session_uuid, self.transaction_id)

    def append_event(self, event):
        state_store.append_jsonl_atomic(self.events_path(), event)

    def write_skip_event(self, label, event="skipped_no_permit",
                         attempt_id=None):
        self.append_event({"event": event, "label": label,
                           "attempt_id": attempt_id,
                           "at": "2026-09-10T21:11:58.443754Z"})

    def write_terminal_event(self, label, **fields):
        event = {"event": "terminal", "label": label,
                 "at": "2026-09-10T20:56:32Z"}
        event.update(fields)
        self.append_event(event)

    def write_active_pgid(self, pgid, label):
        state_store.write_json_atomic(
            state_store.verification_active_pgid_path_for(
                self.session_uuid, self.transaction_id),
            {"pgid": pgid, "label": label,
             "started_at": "2026-09-10T20:56:00Z"})

    def mint(self, label, command=None):
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        return ledger.mint_owned_attempt(
            ledger_path, self.transaction_id, label,
            fields={"command": command or [_PYTHON, "-c", "pass"],
                    "execution_mode": "isolated_snapshot"})

    def ledger_records(self, label):
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        key = ledger.owned_attempt_key(self.transaction_id, label)
        return [r for r in ledger.read_ledger(ledger_path)
                if r.get("attempt_key") == key]


class _CountingSleep(object):
    """A `sleep` stand-in that records every call and never actually
    sleeps -- how this suite proves a wait was NOT spent."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)

    @property
    def count(self):
        return len(self.calls)


# =========================================================================== #
# 1. Every deliberate-skip event is terminal-and-UNEXECUTED, never a wait.    #
# =========================================================================== #


class SkipEventsAreTerminalAbsentTests(_SessionFixture):

    def test_every_skip_event_resolves_at_once_as_absent_and_unexecuted(self):
        for event in _recognized_skip_events():
            with self.subTest(event=event):
                self.transaction_id = "T-" + uuid.uuid4().hex[:8]
                self.write_skip_event("gate", event=event, attempt_id="V-0004")
                sleep = _CountingSleep()
                terminal = evidence_module.bounded_evidence_wait(
                    self.session_uuid, self.transaction_id, ["gate"],
                    poll_attempts=500, poll_delay_s=1.0, sleep=sleep)
                attempt = terminal["gate"]
                self.assertEqual(attempt["evidence_state"],
                                 evidence_module.EVIDENCE_ABSENT)
                self.assertIsNone(attempt["exit_code"])
                self.assertTrue(attempt["skipped"])
                self.assertEqual(attempt["skip_reason"], event)
                self.assertEqual(attempt["skipped_at"],
                                 "2026-09-10T21:11:58.443754Z")
                self.assertEqual(
                    sleep.count, 0,
                    "a durable skip record proves evidence can NEVER "
                    "arrive; %s must not be waited out" % event)

    def test_skip_never_becomes_pass_through_the_real_adjudication(self):
        # The whole point: an UNEXECUTED gate must reach the ledger as
        # unresolved/unknown with no exit code -- never `pass`, which is
        # reachable only from EVIDENCE_PRESENT.
        for event in _recognized_skip_events():
            with self.subTest(event=event):
                self.transaction_id = "T-" + uuid.uuid4().hex[:8]
                minted = self.mint("gate")
                self.write_skip_event("gate", event=event,
                                      attempt_id=minted["id"])
                entry = {"label": "gate", "command": [_PYTHON, "-c", "pass"],
                         "kind": verification.KIND_FINAL_SUITE,
                         "ledger_attempt_id": minted["id"]}
                attempt, ledger_ok = (
                    evidence_module._wait_for_attempt_and_revise_ledger(
                        self.session_uuid, self.transaction_id, entry,
                        request={"evidence_retry_policy": {
                            "poll_attempts": 2, "poll_delay_s": 0.01}},
                        ledger_path=state_store.ledger_path_for(
                            self.session_uuid),
                        overall_deadline=time.time() + 600,
                        timeout_policy={}, snapshot_manifest_digest="D"))
                self.assertTrue(ledger_ok)
                self.assertEqual(attempt["evidence_state"],
                                 evidence_module.EVIDENCE_ABSENT)
                self.assertIsNone(attempt["exit_code"])
                self.assertEqual(attempt["skip_reason"], event)
                record = self.ledger_records("gate")[-1]
                self.assertEqual(record["attempt_state"], "unresolved")
                self.assertEqual(record["exit_status"], "unknown")
                self.assertNotEqual(record["adjudication"], "pass")
                self.assertEqual(record["adjudication"], "unknown")
                self.assertIsNone(record["exit_code"])
                self.assertEqual(record["id"], minted["id"])

    def test_a_real_terminal_event_outranks_a_skip_record(self):
        # Really observed evidence is never overridden by a record of its
        # absence -- whichever order the two land in.
        self.write_skip_event("gate")
        self.write_terminal_event(
            "gate", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=1, poll_delay_s=0.01)
        self.assertEqual(terminal["gate"]["evidence_state"],
                         evidence_module.EVIDENCE_PRESENT)
        self.assertEqual(terminal["gate"]["exit_code"], 0)
        self.assertNotIn("skipped", terminal["gate"])

    def test_a_skip_for_a_different_label_resolves_nothing(self):
        self.write_skip_event("other")
        sleep = _CountingSleep()
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=3, poll_delay_s=0.01, sleep=sleep)
        self.assertEqual(terminal["gate"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)
        self.assertNotIn("skipped", terminal["gate"])
        self.assertEqual(sleep.count, 2, "an unrelated label's skip must "
                         "not shorten this label's own bounded poll")

    def test_skip_events_match_what_the_worker_emits(self):
        # Drift guard for the frozen function-local duplicate: if
        # `worker_main` ever emits a fourth skip event, recognition inside
        # `bounded_evidence_wait` must be extended with it rather than
        # silently reverting to an unbounded wait. This also pins the
        # count, so a production tuple that was emptied -- which would make
        # the two loops above iterate over nothing -- fails right here.
        recognized = _recognized_skip_events()
        emitted = set(re.findall(
            r'"event": "(skipped_\w+)"',
            inspect.getsource(verification.worker_main)))
        self.assertEqual(len(recognized), 3)
        self.assertEqual(len(emitted), 3)
        self.assertEqual(emitted, set(recognized))
        # And the literals live in the function, not at module level: the
        # seam exports no new public symbol for them.
        self.assertFalse(
            [name for name in vars(evidence_module)
             if "SKIP" in name.upper()],
            "skip-event recognition must stay local to "
            "bounded_evidence_wait -- no new module-level symbol")


# =========================================================================== #
# 2. Deadline and worker-liveness bounds (and what they must NOT collapse).   #
# =========================================================================== #


class DeadlineAndWorkerLivenessBoundTests(_SessionFixture):

    def test_clock_jump_across_the_deadline_stops_the_wait(self):
        # The incident's shape: an attempt COUNT sized from the remaining
        # deadline once, then a clock that jumps (host suspension) far past
        # it. The wait must end at the deadline, not spend its budget.
        started = 1_000_000.0
        ticks = [started, started + 1.0, started + 900.0]

        def now():
            return ticks.pop(0) if len(ticks) > 1 else ticks[0]

        sleep = _CountingSleep()
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=1000, poll_delay_s=1.0, sleep=sleep,
            deadline=started + 300.0, now=now)
        self.assertEqual(terminal["gate"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)
        self.assertEqual(terminal["gate"]["wait_stopped_by"],
                         "deadline_reached")
        self.assertLessEqual(
            sleep.count, 2,
            "the wait must stop once the clock passes the deadline, not "
            "after all 1000 attempts")

    def test_without_a_deadline_the_attempt_budget_is_spent_exactly_as_before(self):
        # Zero behavior change when the new parameters are omitted.
        sleep = _CountingSleep()
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=4, poll_delay_s=0.01, sleep=sleep)
        self.assertEqual(sleep.count, 3)
        self.assertEqual(terminal["gate"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)
        self.assertNotIn("wait_stopped_by", terminal["gate"])

    def test_a_deadline_in_the_future_never_shortens_the_poll(self):
        sleep = _CountingSleep()
        evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=4, poll_delay_s=0.01, sleep=sleep,
            deadline=time.time() + 3600)
        self.assertEqual(sleep.count, 3)

    def test_an_exited_worker_ends_the_wait_at_once(self):
        sleep = _CountingSleep()
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=1000, poll_delay_s=1.0, sleep=sleep,
            is_worker_alive=lambda: False)
        self.assertEqual(terminal["gate"]["evidence_state"],
                         evidence_module.EVIDENCE_ABSENT)
        self.assertEqual(terminal["gate"]["wait_stopped_by"],
                         "worker_exited")
        self.assertEqual(sleep.count, 0)

    def test_an_exited_worker_does_not_prove_the_child_group_is_gone(self):
        # The uncertainty that must NOT be collapsed: the worker is gone,
        # but ITS command's process group is genuinely still running, so
        # the honest state is UNRESOLVED -- never ABSENT, and never a
        # reason to re-launch or adjudicate anything.
        _proc, pgid = self.spawn_sleeper(5)
        self.write_active_pgid(pgid, "slow")
        sleep = _CountingSleep()
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["slow"],
            poll_attempts=1000, poll_delay_s=1.0, sleep=sleep,
            is_worker_alive=lambda: False)
        attempt = terminal["slow"]
        self.assertEqual(attempt["evidence_state"],
                         evidence_module.EVIDENCE_UNRESOLVED)
        self.assertIn("still alive", attempt["note"])
        self.assertIsNone(attempt["exit_code"])
        self.assertEqual(attempt["wait_stopped_by"], "worker_exited")
        self.assertEqual(sleep.count, 0)

    def test_evidence_written_before_the_exit_is_read_before_declaring_missing(self):
        # Liveness is sampled BEFORE the event stream is read, so a worker
        # that exits with its evidence already durable still yields that
        # evidence -- an exited worker is never, by itself, a reason to
        # discard what it wrote.
        self.write_terminal_event(
            "done", evidence_state=evidence_module.EVIDENCE_PRESENT,
            exit_code=0, timed_out=False)
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["done"],
            poll_attempts=1000, poll_delay_s=1.0, sleep=_CountingSleep(),
            is_worker_alive=lambda: False)
        self.assertEqual(terminal["done"]["evidence_state"],
                         evidence_module.EVIDENCE_PRESENT)
        self.assertEqual(terminal["done"]["exit_code"], 0)
        self.assertNotIn("wait_stopped_by", terminal["done"])

    def test_a_worker_that_exits_between_two_polls_still_yields_its_evidence(self):
        # It writes the event on the same turn it dies: the liveness sample
        # that observes the exit happens BEFORE the read that observes the
        # event, so nothing is lost.
        state = {"alive": True}

        def is_alive():
            if state["alive"]:
                state["alive"] = False
                return True
            self.write_terminal_event(
                "late", evidence_state=evidence_module.EVIDENCE_PRESENT,
                exit_code=0, timed_out=False)
            return False

        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["late"],
            poll_attempts=1000, poll_delay_s=0.01,
            sleep=_CountingSleep(), is_worker_alive=is_alive)
        self.assertEqual(terminal["late"]["evidence_state"],
                         evidence_module.EVIDENCE_PRESENT)

    def test_a_skip_still_wins_over_an_unspent_liveness_or_deadline_bound(self):
        self.write_skip_event("gate")
        terminal = evidence_module.bounded_evidence_wait(
            self.session_uuid, self.transaction_id, ["gate"],
            poll_attempts=1000, poll_delay_s=1.0, sleep=_CountingSleep(),
            deadline=time.time() + 3600, is_worker_alive=lambda: True)
        self.assertTrue(terminal["gate"]["skipped"])
        self.assertNotIn("wait_stopped_by", terminal["gate"])


# =========================================================================== #
# 3. The spine wait forwards its bounds -- without breaking injected doubles. #
# =========================================================================== #


class InjectedWaitCompatibilityTests(_SessionFixture):

    def _entry_and_args(self, label="gate"):
        minted = self.mint(label)
        entry = {"label": label, "command": [_PYTHON, "-c", "pass"],
                 "kind": verification.KIND_FINAL_SUITE,
                 "ledger_attempt_id": minted["id"]}
        return entry, {
            "session_uuid": self.session_uuid,
            "transaction_id": self.transaction_id,
            "entry": entry,
            "request": {"evidence_retry_policy": {"poll_attempts": 2,
                                                  "poll_delay_s": 0.01}},
            "ledger_path": state_store.ledger_path_for(self.session_uuid),
            "overall_deadline": time.time() + 600,
            "timeout_policy": {},
            "snapshot_manifest_digest": "D",
        }

    def test_a_pre_existing_narrow_double_is_called_with_the_old_shape(self):
        # EXACTLY the signature `scripts/test_cowork.py`'s own injected
        # doubles use. It must keep working, unchanged, even when the
        # caller supplies a liveness predicate.
        calls = []

        def fake_wait(session_uuid, transaction_id, labels, poll_attempts,
                      poll_delay_s):
            calls.append((poll_attempts, poll_delay_s))
            return {"gate": {"evidence_state":
                             evidence_module.EVIDENCE_UNRESOLVED}}

        entry, kwargs = self._entry_and_args()
        attempt, ledger_ok = (
            evidence_module._wait_for_attempt_and_revise_ledger(
                bounded_evidence_wait_fn=fake_wait,
                is_worker_alive=lambda: False, **kwargs))
        self.assertTrue(ledger_ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(attempt["evidence_state"],
                         evidence_module.EVIDENCE_UNRESOLVED)

    def test_a_double_that_accepts_them_receives_the_deadline_and_liveness(self):
        seen = []
        alive = lambda: False  # noqa: E731 - identity is what is asserted

        def wide_wait(session_uuid, transaction_id, labels, **kwargs):
            seen.append(kwargs)
            return {"gate": {"evidence_state":
                             evidence_module.EVIDENCE_ABSENT}}

        entry, kwargs = self._entry_and_args()
        evidence_module._wait_for_attempt_and_revise_ledger(
            bounded_evidence_wait_fn=wide_wait, is_worker_alive=alive,
            **kwargs)
        self.assertEqual(len(seen), 2)
        for observed in seen:
            self.assertEqual(observed["deadline"], kwargs["overall_deadline"])
            self.assertIs(observed["is_worker_alive"], alive)

    def test_no_liveness_argument_means_none_is_forwarded(self):
        seen = []

        def wide_wait(session_uuid, transaction_id, labels, **kwargs):
            seen.append(kwargs)
            return {"gate": {"evidence_state":
                             evidence_module.EVIDENCE_ABSENT}}

        entry, kwargs = self._entry_and_args()
        evidence_module._wait_for_attempt_and_revise_ledger(
            bounded_evidence_wait_fn=wide_wait, **kwargs)
        self.assertNotIn("is_worker_alive", seen[0])
        self.assertEqual(seen[0]["deadline"], kwargs["overall_deadline"])

    def test_a_mock_patched_module_attribute_delegating_to_a_narrow_double(self):
        # `mock.patch.object(evidence_module, "bounded_evidence_wait",
        # side_effect=<narrow fn>)` accepts anything at the mock's own
        # boundary but forwards to a function that does not: the delegate
        # is what decides the call shape.
        calls = []

        def narrow(session_uuid, transaction_id, labels, poll_attempts,
                   poll_delay_s):
            calls.append(poll_attempts)
            return {"gate": {"evidence_state":
                             evidence_module.EVIDENCE_ABSENT}}

        entry, kwargs = self._entry_and_args()
        with mock.patch.object(evidence_module, "bounded_evidence_wait",
                               side_effect=narrow):
            evidence_module._wait_for_attempt_and_revise_ledger(
                is_worker_alive=lambda: False, **kwargs)
        self.assertEqual(len(calls), 2)

    def test_a_genuine_typeerror_from_inside_a_wait_is_never_swallowed(self):
        def exploding_wait(session_uuid, transaction_id, labels,
                           poll_attempts=None, poll_delay_s=None,
                           deadline=None, is_worker_alive=None):
            raise TypeError("genuine failure inside the wait")

        entry, kwargs = self._entry_and_args()
        with self.assertRaises(TypeError) as caught:
            evidence_module._wait_for_attempt_and_revise_ledger(
                bounded_evidence_wait_fn=exploding_wait,
                is_worker_alive=lambda: True, **kwargs)
        self.assertIn("genuine failure inside the wait", str(caught.exception))

    def test_a_callable_object_with_a_non_callable_side_effect_is_probed_itself(self):
        # A non-callable `side_effect` is not a delegate to introspect --
        # the object's own `__call__` is, and it takes **kwargs, so it does
        # receive the bounds.
        seen = []

        class Opaque(object):
            side_effect = "not callable"

            def __call__(self, session_uuid, transaction_id, labels,
                         **kwargs):
                seen.append(kwargs)
                return {"gate": {"evidence_state":
                                 evidence_module.EVIDENCE_ABSENT}}

        entry, kwargs = self._entry_and_args()
        evidence_module._wait_for_attempt_and_revise_ledger(
            bounded_evidence_wait_fn=Opaque(),
            is_worker_alive=lambda: False, **kwargs)
        self.assertEqual(len(seen), 2)
        for observed in seen:
            self.assertEqual(observed["deadline"], kwargs["overall_deadline"])
            self.assertIn("is_worker_alive", observed)

    def test_a_wait_that_cannot_be_introspected_is_offered_nothing(self):
        # A builtin has no retrievable signature. The conservative choice
        # is the exact pre-existing call shape -- no bounds at all -- not a
        # TypeError from a probe that guessed wrong.
        seen = []

        class Uninspectable(object):
            """Delegates to a builtin, whose signature cannot be read."""

            def __init__(self):
                self.side_effect = len

            def __call__(self, session_uuid, transaction_id, labels,
                         **kwargs):
                seen.append(kwargs)
                return {"gate": {"evidence_state":
                                 evidence_module.EVIDENCE_ABSENT}}

        entry, kwargs = self._entry_and_args()
        evidence_module._wait_for_attempt_and_revise_ledger(
            bounded_evidence_wait_fn=Uninspectable(),
            is_worker_alive=lambda: False, **kwargs)
        self.assertEqual(len(seen), 2)
        for observed in seen:
            self.assertNotIn("deadline", observed)
            self.assertNotIn("is_worker_alive", observed)

    def test_the_signature_probe_adds_no_module_level_symbol(self):
        # The probe is confined to `_wait_for_attempt_and_revise_ledger`:
        # no new helper and no new module-level import escaped into the
        # frozen evidence seam.
        self.assertFalse(
            [name for name in vars(evidence_module)
             if "supported_wait" in name or "wait_kwargs" in name],
            "the signature probe must stay inside the authorized function")
        self.assertNotIn("inspect", vars(evidence_module))
        self.assertIn(
            "import inspect",
            inspect.getsource(
                evidence_module._wait_for_attempt_and_revise_ledger))


# =========================================================================== #
# 4. Sticky permit loss in the worker: one wait, then honest skip records.    #
# =========================================================================== #


class StickyPermitLossTests(_SessionFixture):

    def _write_request(self, labels, command=None, repo=None):
        inventory = []
        for label in labels:
            minted = self.mint(label, command=command)
            inventory.append({
                "label": label,
                "command": command or [_PYTHON, "-c", "pass"],
                "execution_mode": "in_place",
                "kind": verification.KIND_FINAL_SUITE,
                "ledger_attempt_id": minted["id"],
            })
        request = {
            "session_uuid": self.session_uuid,
            "transaction_id": self.transaction_id,
            "protocol_version": verification.PROTOCOL_VERSION,
            "inventory": inventory,
            "repo": repo,
            "timeout_policy": {"command_timeout_s": 5, "term_grace_s": 1},
        }
        request_path = state_store.verification_request_path_for(
            self.session_uuid, self.transaction_id)
        state_store.write_json_atomic(request_path, request)
        return request_path, inventory

    def _events(self):
        return state_store.read_jsonl_tolerant(self.events_path())

    def test_one_failed_permit_wait_is_never_repeated_for_later_entries(self):
        labels = ["V-a", "V-b", "V-c", "V-d", "V-e"]
        request_path, _inventory = self._write_request(labels)
        waits = []

        def never_permitted(session_uuid, transaction_id, index,
                            ledger_attempt_id, should_stop, **kwargs):
            waits.append(index)
            return False

        with mock.patch.object(verification, "_wait_for_permit",
                               side_effect=never_permitted):
            exit_code = verification.worker_main(request_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            waits, [0],
            "the parent issues entry N+1's permit only after entry N "
            "terminalizes, so once one wait has failed no later permit can "
            "arrive: exactly ONE wait, never one per remaining entry")
        events = self._events()
        self.assertEqual([e["event"] for e in events],
                         ["skipped_no_permit"] * len(labels))
        self.assertEqual([e["label"] for e in events], labels)

    def test_a_permit_landing_after_the_skip_never_starts_the_command(self):
        # The incident verbatim: the permit file appears AFTER the entry was
        # already skipped. It must never be honored retroactively -- no
        # start event, and the command itself must never run.
        marker_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(marker_dir, ignore_errors=True))
        marker = os.path.join(marker_dir, "executed")
        command = [_PYTHON, "-c", "open(%r, 'w').write('ran')" % marker]
        request_path, inventory = self._write_request(
            ["V-0004", "V-0005"], command=command)
        permit_path = state_store.verification_permit_path_for(
            self.session_uuid, self.transaction_id)

        def late_permit(session_uuid, transaction_id, index,
                        ledger_attempt_id, should_stop, **kwargs):
            # The permit lands 717ms too late, exactly as it did in the
            # incident: written here, immediately after the wait it was
            # meant to satisfy has already given up.
            state_store.write_json_atomic(permit_path, {
                "transaction_id": transaction_id, "index": index,
                "ledger_attempt_id": ledger_attempt_id,
                "issued_at": "2026-09-10T21:11:59.161048Z"})
            return False

        with mock.patch.object(verification, "_wait_for_permit",
                               side_effect=late_permit):
            exit_code = verification.worker_main(request_path)

        self.assertEqual(exit_code, 0)
        self.assertFalse(os.path.exists(marker),
                         "a permit that arrives after the skip must never "
                         "start the command")
        events = self._events()
        self.assertEqual([e["event"] for e in events],
                         ["skipped_no_permit", "skipped_no_permit"])
        self.assertEqual([e["attempt_id"] for e in events],
                         [entry["ledger_attempt_id"] for entry in inventory])
        # The stale permit is still on disk, still naming the FIRST entry --
        # never advanced, never consumed, never retroactively honored.
        permit = state_store.read_json_tolerant(permit_path)
        self.assertEqual(permit["index"], 0)

    def test_a_timely_permit_still_runs_every_entry_exactly_as_before(self):
        # The sticky flag must not fire when permits genuinely arrive: the
        # ordinary path is unchanged.
        request_path, inventory = self._write_request(["V-a", "V-b"])
        permit_path = state_store.verification_permit_path_for(
            self.session_uuid, self.transaction_id)

        def issue_then_wait(session_uuid, transaction_id, index,
                            ledger_attempt_id, should_stop, **kwargs):
            state_store.write_json_atomic(permit_path, {
                "transaction_id": transaction_id, "index": index,
                "ledger_attempt_id": ledger_attempt_id})
            return True

        with mock.patch.object(verification, "_wait_for_permit",
                               side_effect=issue_then_wait):
            exit_code = verification.worker_main(request_path)

        self.assertEqual(exit_code, 0)
        events = self._events()
        self.assertEqual([e["event"] for e in events],
                         ["start", "terminal", "start", "terminal"])
        for event in events:
            if event["event"] == "terminal":
                self.assertEqual(event["exit_code"], 0)

    def test_a_permit_lost_mid_inventory_leaves_earlier_evidence_intact(self):
        # The exact incident shape: the first entries run and terminalize,
        # the next loses its permit, and every remaining entry is skipped
        # without a fresh wait -- completed evidence untouched.
        request_path, inventory = self._write_request(
            ["V-a", "V-b", "V-c", "V-d"])
        permit_path = state_store.verification_permit_path_for(
            self.session_uuid, self.transaction_id)
        waits = []

        def permits_then_none(session_uuid, transaction_id, index,
                              ledger_attempt_id, should_stop, **kwargs):
            waits.append(index)
            if index >= 1:
                return False
            state_store.write_json_atomic(permit_path, {
                "transaction_id": transaction_id, "index": index,
                "ledger_attempt_id": ledger_attempt_id})
            return True

        with mock.patch.object(verification, "_wait_for_permit",
                               side_effect=permits_then_none):
            verification.worker_main(request_path)

        self.assertEqual(waits, [0, 1])
        events = self._events()
        self.assertEqual(
            [e["event"] for e in events],
            ["start", "terminal", "skipped_no_permit", "skipped_no_permit",
             "skipped_no_permit"])
        self.assertEqual(events[1]["exit_code"], 0)
        self.assertEqual([e["label"] for e in events[2:]],
                         ["V-b", "V-c", "V-d"])


# =========================================================================== #
# 5. The parent spine: a truthful terminal receipt after skips and exit.      #
# =========================================================================== #


def _init_git_repo():
    d = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "config", "commit.gpgsign", "false"],
                   check=True)
    with open(os.path.join(d, "f.txt"), "w") as fh:
        fh.write("x")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    return d


# The worker this spine test spawns IS the real `worker_main`, run in a real
# subprocess against the real request/permit/event files -- with exactly one
# thing shortened: `_wait_for_permit`'s 600s backstop, which no bounded test
# may sit through. Nothing else about the worker is faked.
_STUB_WORKER = """
import sys
sys.path.insert(0, %(scripts_dir)r)
import cowork_verification as v
_real = v._wait_for_permit
def _short(session_uuid, transaction_id, index, ledger_attempt_id,
           should_stop, **kwargs):
    kwargs["timeout_s"] = %(backstop)r
    return _real(session_uuid, transaction_id, index, ledger_attempt_id,
                 should_stop, **kwargs)
v._wait_for_permit = _short
sys.exit(v.worker_main(%(request_path)r, %(liveness_fd)r))
"""


class ParentSpineTerminalReceiptTests(unittest.TestCase):
    """End-to-end through the REAL spine (`run_transaction` ->
    `_run_owned_transaction`), a real ledger, real permits, a real event
    stream and a real worker subprocess -- the one deliberate substitution
    being `spawn_worker`, which otherwise resolves and launches the running
    installation's own source and cannot have its permit backstop shortened
    from here."""

    # Long enough that entries 0 and 1 get their permits comfortably within
    # the parent's own ordinary per-entry turnaround (a 1.0s evidence poll
    # delay plus ledger work), short enough that the one DELIBERATELY
    # withheld permit costs this test a few seconds rather than the real
    # 600s backstop.
    PERMIT_BACKSTOP_S = 3.0

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        prior = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root

        def restore():
            if prior is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = prior
        self.addCleanup(restore)
        self.repo = _init_git_repo()
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        self.session_uuid = "S-" + uuid.uuid4().hex[:8]
        self.spawned = []
        self.addCleanup(self._reap_all)

    def _reap_all(self):
        for proc in self.spawned:
            _reap(proc)

    def _spawn_stub_worker(self, python_executable, checkout_root,
                           request_path, session_uuid=None,
                           transaction_id=None):
        read_fd, write_fd = os.pipe()
        program = _STUB_WORKER % {
            "scripts_dir": _HERE, "backstop": self.PERMIT_BACKSTOP_S,
            "request_path": request_path, "liveness_fd": read_fd}
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            [_PYTHON, "-c", program], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env, pass_fds=(read_fd,),
            close_fds=True)
        os.close(read_fd)
        self.spawned.append(proc)
        # The spine reads its startup classification from the `proc`-stashed
        # fallback its own docstring documents; identity verification itself
        # is not what this test is about.
        proc._cowork_startup_classification = {
            "identity": {"pid": proc.pid, "protocol_version":
                         verification.PROTOCOL_VERSION},
            "worker_verified": True, "startup_failure": None}
        return (proc, write_fd, None)

    def test_delayed_permit_and_worker_exit_end_in_a_truthful_receipt(self):
        labels = ["V-0001", "V-0002", "V-0003", "V-0004"]
        # Distinct commands: an inventory of four IDENTICAL commands is
        # legitimately collapsed by `normalize_inventory`, and this test
        # needs four genuinely separate entries to permit in sequence.
        entries = [{"label": label,
                    "command": ["python3", "-c", "x=%d" % index],
                    "execution_mode": "isolated_snapshot",
                    "kind": (verification.KIND_FINAL_SUITE
                             if label == labels[-1] else "focused")}
                   for index, label in enumerate(labels)]
        real_issue = verification._issue_permit

        def _skip_is_recorded(transaction_id, label):
            events = state_store.read_jsonl_tolerant(
                state_store.verification_attempt_events_path_for(
                    self.session_uuid, transaction_id))
            return any(e.get("label") == label
                       and e.get("event") == "skipped_no_permit"
                       for e in events)

        def delayed_for_the_third_entry(session_uuid, transaction_id, index,
                                        ledger_attempt_id):
            if index == 2:
                # Issued too late to be honored -- the incident's 717ms
                # gap, made DETERMINISTIC rather than raced: this permit is
                # written only once the worker's own skip record for the
                # entry it authorizes is already durable on disk.
                deadline = time.time() + 30
                while (time.time() < deadline
                       and not _skip_is_recorded(transaction_id, labels[2])):
                    time.sleep(0.05)
                self.assertTrue(
                    _skip_is_recorded(transaction_id, labels[2]),
                    "the worker must have skipped this entry before its "
                    "permit is issued -- otherwise this test is not "
                    "exercising a late permit at all")
            return real_issue(session_uuid, transaction_id, index,
                              ledger_attempt_id)

        started = time.time()
        with mock.patch.object(verification, "spawn_worker",
                               side_effect=self._spawn_stub_worker), \
             mock.patch.object(verification, "_issue_permit",
                               side_effect=delayed_for_the_third_entry):
            result = verification.run_transaction(
                self.repo, self.session_uuid, entries)
        elapsed = time.time() - started

        transaction_id = result["transaction_id"]
        events = state_store.read_jsonl_tolerant(
            state_store.verification_attempt_events_path_for(
                self.session_uuid, transaction_id))
        by_label = {}
        for event in events:
            by_label.setdefault(event.get("label"), []).append(
                event.get("event"))

        # 1. A TERMINAL RECEIPT EXISTS -- the stall's defining absence.
        result_path = state_store.verification_result_path_for(
            self.session_uuid, transaction_id)
        self.assertTrue(os.path.exists(result_path),
                        "the parent must reach a terminal receipt instead "
                        "of waiting for evidence a dead worker cannot emit")
        receipt = state_store.read_json_tolerant(result_path)
        self.assertEqual(receipt["verdict"], result["verdict"])
        self.assertNotEqual(result["verdict"], verification.VERDICT_GREEN)

        # 2. BOUNDED: nowhere near the transaction's own overall deadline,
        # and far short of even one 600s permit backstop.
        self.assertLess(elapsed, 60,
                        "the transaction must terminalize promptly once the "
                        "worker has exited")

        # 3. COMPLETED EVIDENCE IS PRESERVED, exactly as observed.
        attempts = {a["label"]: a for a in result["attempts"]}
        for label in labels[:2]:
            self.assertEqual(attempts[label]["evidence_state"],
                             verification.EVIDENCE_PRESENT)
            self.assertEqual(attempts[label]["exit_code"], 0)
            self.assertEqual(by_label[label], ["start", "terminal"])

        # 4. THE SKIPPED GATE IS UNEXECUTED, never PASS.
        skipped = attempts["V-0003"]
        self.assertEqual(skipped["evidence_state"],
                         verification.EVIDENCE_ABSENT)
        self.assertIsNone(skipped["exit_code"])
        self.assertEqual(skipped["skip_reason"], "skipped_no_permit")
        self.assertEqual(by_label["V-0003"], ["skipped_no_permit"],
                         "a permit that landed after the skip must never "
                         "have started the command")

        # 5. THE FINAL SUITE NEVER RAN -- and is reported as such.
        self.assertEqual(result["final_suite_binding"], "not_reached")
        self.assertEqual(by_label.get("V-0004"), ["skipped_no_permit"])

        # 6. THE LEDGER AGREES, under the pre-minted ids, with no PASS.
        ledger_path = state_store.ledger_path_for(self.session_uuid)
        records = ledger.read_ledger(ledger_path)
        for label in ("V-0003", "V-0004"):
            key = ledger.owned_attempt_key(transaction_id, label)
            final = [r for r in records if r.get("attempt_key") == key][-1]
            self.assertIn(final["attempt_state"],
                          ("unresolved", "not_reached"))
            self.assertNotEqual(final.get("adjudication"), "pass")
            self.assertIsNone(final.get("exit_code"))
        for label in labels[:2]:
            key = ledger.owned_attempt_key(transaction_id, label)
            final = [r for r in records if r.get("attempt_key") == key][-1]
            self.assertEqual(final["attempt_state"], "terminal")
            self.assertEqual(final["adjudication"], "pass")

        # 7. THE WORKER IS REAPED -- no defunct child left behind.
        self.assertIsNotNone(self.spawned[0].poll(),
                             "the exited worker must have been reaped")


# =========================================================================== #
# 6. Backward compatibility of the widened production signatures.             #
# =========================================================================== #


class BackwardCompatibleSignatureTests(unittest.TestCase):

    def test_the_new_evidence_wait_parameters_are_optional(self):
        params = inspect.signature(
            evidence_module.bounded_evidence_wait).parameters
        self.assertIsNone(params["deadline"].default)
        self.assertIsNone(params["is_worker_alive"].default)
        self.assertIs(params["now"].default, time.time)
        # Unchanged, frozen defaults.
        self.assertEqual(params["poll_attempts"].default,
                         verification.DEFAULT_EVIDENCE_POLL_ATTEMPTS)
        self.assertEqual(params["poll_delay_s"].default,
                         verification.DEFAULT_EVIDENCE_POLL_DELAY_S)
        self.assertIs(params["sleep"].default, time.sleep)

    def test_the_ledger_wait_keeps_its_injection_point_and_adds_one_optional(self):
        params = inspect.signature(
            evidence_module._wait_for_attempt_and_revise_ledger).parameters
        self.assertIsNone(params["bounded_evidence_wait_fn"].default)
        self.assertIsNone(params["is_worker_alive"].default)
        self.assertEqual(
            [name for name in params][:8],
            ["session_uuid", "transaction_id", "entry", "request",
             "ledger_path", "overall_deadline", "timeout_policy",
             "snapshot_manifest_digest"])

    def test_the_seam_reexport_identity_is_unchanged(self):
        self.assertIs(verification.bounded_evidence_wait,
                      evidence_module.bounded_evidence_wait)
        self.assertIs(verification._wait_for_attempt_and_revise_ledger,
                      evidence_module._wait_for_attempt_and_revise_ledger)

    def test_the_permit_backstop_limit_itself_is_unchanged(self):
        # This repair enforces the existing limits; it never raises one.
        self.assertEqual(verification.DEFAULT_PERMIT_WAIT_S, 600)
        self.assertEqual(verification.DEFAULT_COMMAND_TIMEOUT_S, 300)


if __name__ == "__main__":
    unittest.main()
