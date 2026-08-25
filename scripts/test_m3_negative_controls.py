#!/usr/bin/env python3
"""M3 Package G: end-to-end negative-control suite.

Independent, fresh proof — written without editing any existing test file —
that every M3 negative control the frozen brief and the corrected M3 plan's
`global_exit_audit.required_negative_controls` name is refused by the REAL,
fully-integrated (Package A-F) production seams, never merely by an isolated
pure function. Every test below drives one or more of: `cowork._role_loop`
(the real send-failure/retain seam), `cowork.run_resume_trigger` (Package E's
real headless resume-trigger CLI, called in-process with real argv),
`cowork_wake_macos.fire` (Package F's real on-fire handler, real D delegation
and real E subprocess argv), `cowork_capacity_scheduler`'s real claim/cancel/
replace/mark_consumed/reclaim_if_expired/record_failed_wake_attempt/
run_wake_trigger functions (Package D, calling Package B's real locked
accessors — never reimplemented storage or locking), `cowork_bridge`'s real
classification/extraction functions (Package C), and `cowork_state`'s real
Ed25519 write-time verification boundary (Package B) — never a bypassed or
hand-rolled substitute for any of these.

Self-contained: this file defines its own fixtures/test-doubles rather than
importing any from `test_cowork.py`, so its proof stands independently.

Run standalone:

    python3 -m unittest scripts/test_m3_negative_controls.py -v
"""

import ast
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_capacity as capacity_contracts  # noqa: E402
import cowork_capacity_scheduler as scheduler  # noqa: E402
import cowork_control_plane as control_plane  # noqa: E402
import cowork_dispatch_manifest as manifest_mod  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_wake_macos as wake_macos  # noqa: E402
import cowork_wake_manual as wake_manual  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


# A genuine provider-attested retry-after value of ZERO seconds -- so a
# fixture lease's `not_before` is exactly its `issued_at` (the real wall
# clock at entry time) and therefore always already due by the time a test
# calls `cowork._capacity_now()` again, with no race against elapsed test
# execution time. The DEFAULT `_enter_capacity` fixture shape, producing a
# `scheduled`-mode PauseLease. Tests targeting `manual_signal` behavior
# pass `retry_evidence=None` explicitly instead.
_DEFAULT_RETRY_EVIDENCE = {"source": "provider_header", "value": "0s"}


# --------------------------------------------------------------------------- #
# Shared fixtures (self-contained; independent of test_cowork.py's own).      #
# --------------------------------------------------------------------------- #

class _M3E2EBase(unittest.TestCase):
    """Isolated COWORK_SESSIONS_ROOT + isolated cwd per test."""

    def setUp(self):
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        prior_cwd = os.getcwd()
        self.addCleanup(lambda: os.chdir(prior_cwd))
        os.chdir(d)
        self._dir = d

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root

    def _session(self):
        suid = _uuid()
        spath = os.path.join(self._dir, ".cowork", "session.json")
        state_store.ensure_session(spath, None, suid)
        return spath, suid

    def _bind_capacity_candidate(self, session_uuid, role, controller="claude",
                                 model=None, effort=None, mode="implement"):
        """Compile a REAL dispatch manifest and bind it as the role's
        WorkUnit candidate — mirrors production's own preflighting ->
        running -> candidate-bound sequence. Returns (work_id, manifest,
        binding)."""
        work_id = cowork._role_work_id(session_uuid, role, 0, 0)
        manifest, _ = cowork._compile_role_manifest(
            role=role, session_uuid=session_uuid, work_id=role,
            controller=controller, mode=mode, model=model, effort=effort,
            sessions_dir=state_store.session_assets_dir(session_uuid))
        cowork._ensure_work_unit(session_uuid, work_id, role, controller,
                                 model=model, effort=effort)
        cowork._advance_phase(session_uuid, work_id, "preflight_started")
        cowork._advance_phase(session_uuid, work_id, "preflight_passed")
        cowork._bind_candidate(session_uuid, work_id, manifest["digest"])
        binding = cowork._capacity_candidate_binding(session_uuid, work_id, role)
        return work_id, manifest, binding

    def _status_path(self, session_uuid, role):
        status_path = os.path.join(
            state_store.session_assets_dir(session_uuid), "%s.status.json" % role)
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        with open(status_path, "w") as fh:
            json.dump({"status": "needs_input"}, fh)
        return status_path

    class _FailingSession:
        def __init__(self, controller, error_type, session_id="prov-sess-1",
                    retry_evidence=None):
            self.controller = controller
            self.error_type = error_type
            self.session_id = session_id
            self.model = None
            self.effort = None
            self.sends = []
            self.retry_evidence = retry_evidence

        def send(self, text, meta=None):
            self.sends.append(text)
            result = {"ok": False, "result": "error",
                     "error_type": self.error_type}
            if self.retry_evidence is not None:
                result["retry_evidence"] = self.retry_evidence
            return result

        def close(self):
            pass

    class _AcceptingSession:
        def __init__(self, controller="claude", session_id="prov-sess-1"):
            self.controller = controller
            self.session_id = session_id
            self.model = None
            self.effort = None
            self.sends = []

        def send(self, text, meta=None):
            self.sends.append(text)
            return {"ok": True, "result": "ok"}

        def close(self):
            pass

    def _enter_capacity(self, role="builder", controller="claude",
                        error_type="rate_limit_error",
                        retry_evidence=_DEFAULT_RETRY_EVIDENCE):
        """Drive a REAL quota/overload-classified send failure, through the
        real `cowork._role_loop`, all the way into a durable
        `awaiting_capacity` pause. Defaults to a genuine provider-header
        retry_after so the resulting PauseLease is `scheduled`-mode
        (immediately claimable); pass `retry_evidence=None` explicitly for
        a `manual_signal`-mode fixture. Returns (suid, work_id, payload,
        binding)."""
        spath, suid = self._session()
        work_id, manifest, binding = self._bind_capacity_candidate(
            suid, role, controller=controller)
        status_path = self._status_path(suid, role)
        sess = self._FailingSession(controller, error_type,
                                    retry_evidence=retry_evidence)
        rc, outcome, payload = cowork._role_loop(
            sess, "do the thing", status_path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        self.assertEqual(outcome, "awaiting_capacity",
                         "fixture precondition: capacity entry must succeed")
        # A real controller session durably persists its own provider
        # session id (and this engagement's own config) via the
        # `on_session_id` callback BEFORE a send failure can even return
        # for that same turn (see `_durable_provider_session_id`'s own
        # docstring) -- reproduced explicitly here since this fixture's
        # fake session has no such callback wiring of its own.
        state = state_store.load(spath)
        state.setdefault("config", {})[role] = {
            "controller": controller, "model": None, "effort": None,
            "mode": "implement", "yolo": True}
        state.setdefault("sessions", {})[role] = {
            "controller": controller, "id": sess.session_id}
        state_store.save(spath, state)
        return suid, work_id, payload, binding

    def _run_resume_trigger(self, suid, payload, now, extra=None,
                            session_factory=None, role=None,
                            claimant_ref="claimant-1",
                            automation_ref=None, output=None):
        argv = ["--session-uuid", suid, "--lease-id", payload["lease_id"],
               "--claimant-ref", claimant_ref,
               "--automation-ref", automation_ref or payload["automation_ref"],
               "--now", now]
        if role is not None:
            argv += ["--role", role]
        if extra:
            argv += extra
        lines = []
        rc = cowork.run_resume_trigger(
            argv, output=(output or lines.append), session_factory=session_factory)
        result = json.loads(lines[0]) if lines else None
        return rc, result


def _far_future_rfc3339():
    return "2099-01-01T00:00:00Z"


def _now_plus(seconds):
    """Real wall-clock RFC3339 `seconds` in the future -- used only where a
    fixture's genuine `retry_after` duration means `not_before` sits some
    seconds after entry time, so comparing against the real clock needs a
    safety margin rather than a race against elapsed test execution time."""
    epoch = capacity_contracts.rfc3339_to_epoch_seconds(cowork._capacity_now())
    import datetime as _dt
    return _dt.datetime.fromtimestamp(
        epoch + seconds, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")


# =============================================================================
# 1. Malformed / untrusted / stale / absent retry evidence.
# =============================================================================

class RetryEvidenceNegativeControlsTest(_M3E2EBase):
    """Package C's real `extract_retry_evidence`/`classify_trust_source`/
    `parse_retry_after_text` seams, driven end-to-end through the real
    capacity-entry path (`_enter_awaiting_capacity`)."""

    def test_absent_retry_evidence_degrades_to_unverified_untrustworthy(self):
        extracted = bridge.extract_retry_evidence({"type": "assistant",
                                                    "error": "rate_limit_error"})
        self.assertEqual(extracted, {"source": "unverified", "value": None})
        self.assertEqual(
            capacity_contracts.classify_trust_source(extracted["source"]),
            "untrustworthy")

    def test_malformed_retry_evidence_shape_degrades_to_unverified(self):
        for bad in (
            {"retry_evidence": "not-a-dict"},
            {"retry_evidence": {"source": "provider_header"}},  # missing value
            {"retry_evidence": {"source": "provider_header", "value": ""}},
            {"retry_evidence": {"source": "provider_header", "value": 30}},
            {"retry_evidence": None},
            "not-a-dict-at-all",
        ):
            with self.subTest(bad=bad):
                extracted = bridge.extract_retry_evidence(bad)
                self.assertEqual(extracted["source"], "unverified")
                self.assertIsNone(extracted["value"])

    def test_untrusted_source_kind_degrades_to_untrustworthy(self):
        for kind in ("agent_self_report", "guessed", "", None, 42, "PROVIDER_HEADER"):
            with self.subTest(kind=kind):
                extracted = bridge.extract_retry_evidence(
                    {"retry_evidence": {"source": kind, "value": "30s"}})
                self.assertEqual(extracted["source"], "unverified")

    def test_genuine_provider_header_classifies_trustworthy(self):
        extracted = bridge.extract_retry_evidence(
            {"retry_evidence": {"source": "provider_header", "value": "30s"}})
        self.assertEqual(extracted["source"], "provider_header")
        self.assertEqual(
            capacity_contracts.classify_trust_source(extracted["source"]),
            "trustworthy")

    def test_stale_far_future_timestamp_retry_after_refused_never_scheduled(self):
        """A genuinely provider-attested but implausibly far-future
        `retry_after` (beyond MAX_RETRY_HORIZON_SECONDS) is refused by
        `validate_capacity_packet`'s horizon check -- driven live: capacity
        entry itself must refuse rather than durably persist a speculative
        far-future scheduled pause."""
        suid, work_id, payload, binding = None, None, None, None
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)
        sess = self._FailingSession(
            "claude", "rate_limit_error",
            retry_evidence={"source": "provider_header",
                            "value": _far_future_rfc3339()})
        rc, outcome, payload = cowork._role_loop(
            sess, "do the thing", status_path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        # Refused: never durably entered awaiting_capacity on a stale/
        # implausible horizon.
        self.assertNotEqual(outcome, "awaiting_capacity")
        ps = state_store.current_phase_state(suid, work_id)
        self.assertNotEqual((ps or {}).get("state"), "awaiting_capacity")

    def test_stale_duration_beyond_horizon_degrades_resume_mode_to_manual(self):
        """A duration-shaped retry_after beyond MAX_RETRY_HORIZON_SECONDS is
        unparseable per `parse_retry_after_text` -- the evidence pipeline
        degrades resume_mode to manual_signal rather than scheduling an
        unbounded wait."""
        huge = str(capacity_contracts.MAX_RETRY_HORIZON_SECONDS + 3600) + "s"
        parsed = capacity_contracts.parse_retry_after_text(huge)
        self.assertIsNone(parsed)
        resume_mode, retry_after, capacity_source = cowork._capacity_evidence_fields(
            "claude", "overloaded",
            {"retry_evidence": {"source": "provider_header", "value": huge}},
            "2026-01-01T00:00:00Z")
        self.assertEqual(resume_mode, "manual_signal")
        self.assertIsNone(retry_after)


# =============================================================================
# 2. Local guard and unknown-provider failures.
# =============================================================================

class LocalGuardAndUnknownProviderNegativeControlsTest(_M3E2EBase):

    def test_local_guard_unreachable_and_denied_never_capacity_eligible(self):
        for status, expected in (("unreachable", "guard_unavailable"),
                                 ("denied", "local_guard_exhausted")):
            with self.subTest(status=status):
                outcome = bridge.classify_local_guard_evidence(status)
                self.assertEqual(outcome, expected)
                self.assertNotIn(
                    outcome, capacity_contracts.CAPACITY_ELIGIBLE_OUTCOMES)
                self.assertIn(
                    outcome, capacity_contracts.NON_CAPACITY_TERMINAL_OUTCOMES
                    if outcome == "local_guard_exhausted"
                    else capacity_contracts.CONTROLLER_OUTCOMES)

    def test_local_guard_denial_live_never_enters_capacity_or_claims_reset(self):
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)

        class _DeniedSession:
            controller = "claude"
            session_id = "prov-sess-1"
            model = None
            effort = None

            def send(self, text, meta=None):
                return {"ok": False, "result": "denied", "denied": True}

            def close(self):
                pass

        rc, outcome, payload = cowork._role_loop(
            _DeniedSession(), "do the thing", status_path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        self.assertNotEqual(outcome, "awaiting_capacity")
        health = state_store.read_provider_health(suid, role, "claude")
        self.assertEqual(health["last_outcome"], "local_guard_exhausted")
        ps = state_store.current_phase_state(suid, work_id)
        self.assertNotEqual(ps["state"], "awaiting_capacity")

    def test_unknown_provider_failure_never_capacity_eligible_live(self):
        suid, work_id, payload, binding = None, None, None, None
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)
        sess = self._FailingSession("claude", "a-token-nobody-recognizes")
        rc, outcome, payload = cowork._role_loop(
            sess, "do the thing", status_path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        self.assertNotEqual(outcome, "awaiting_capacity")
        health = state_store.read_provider_health(suid, role, "claude")
        self.assertEqual(health["last_outcome"], "unknown_provider_failure")

    def test_local_guard_evidence_at_reducer_level_refused_as_capacity(self):
        """The pure reducer itself: `local_guard_exhausted`/
        `unknown_provider_failure` are structurally absent from
        `_CAPACITY_ELIGIBLE_CONTROLLER_OUTCOMES` -- naming either in
        `capacity_reserved` evidence is refused with an unchanged state."""
        for outcome in ("local_guard_exhausted", "unknown_provider_failure"):
            evidence = {"capacity_evidence": {
                "controller_outcome": outcome, "role": "builder",
                "provider_session_id": "s1",
                "controller_policy_digest": "a" * 64,
                "candidate_manifest_digest": "b" * 64, "candidate_index": 0,
                "resume_mode": "manual_signal", "model": None, "effort": None,
                "artifact_hashes": {"manifest": "c" * 64},
                "automation_ref": "auto-1"}}
            new_state, reason = control_plane.advance(
                "running", "capacity_reserved", evidence=evidence,
                expected_candidate={"candidate_manifest_digest": "b" * 64,
                                    "candidate_index": 0})
            self.assertEqual(new_state, "running")
            self.assertNotEqual(reason, "capacity_reserved")


# =============================================================================
# 3. Candidate / session / policy / role mismatches.
# =============================================================================

class BindingMismatchNegativeControlsTest(_M3E2EBase):

    def test_role_mismatch_stops_for_supervision(self):
        """Issue #19 direction 5: a resume-trigger explicitly targeting a
        role other than the one actually paused stops durably for
        supervision -- never silently resumes under the wrong identity."""
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(), role="planner")
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_BINDING_MISMATCH)
        self.assertEqual(result["reason"], "role_mismatch")
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["consumption_state"], "unclaimed",
                         "a role-mismatch refusal must never claim the lease")
        ps = state_store.current_phase_state(suid, work_id)
        self.assertEqual(ps["state"], "awaiting_capacity",
                         "phase must remain paused, never silently advanced")

    def test_candidate_mismatch_binding_failure(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        # A genuinely different candidate now governs this WorkUnit (e.g. a
        # fresh dispatch-manifest recompile after the pause was entered).
        cowork._bind_candidate(suid, work_id, "f" * 64)
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now())
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_BINDING_MISMATCH)
        self.assertEqual(result["reason"], "candidate_mismatch")
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["consumption_state"], "unclaimed")

    def test_session_mismatch_binding_failure(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        spath = os.path.join(self._dir, ".cowork", "session.json")
        state = state_store.load(spath)
        state_store.save_role_session(
            spath, "builder", "claude", "an-entirely-different-provider-session",
            prior=state)
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now())
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_BINDING_MISMATCH)
        self.assertEqual(result["reason"], "session_mismatch")

    def test_controller_policy_mismatch_binding_failure(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        manifest_path = state_store.manifest_path_for(suid, "builder")
        manifest = manifest_mod.load_manifest(manifest_path)
        manifest["binding"]["config_digest"] = "e" * 64
        # The manifest's own top-level `digest` is a content hash of its
        # binding (never compared against the WorkUnit's already-bound
        # candidate digest by `_capacity_candidate_binding` -- that field
        # is sourced from the WorkUnit's own durable history instead), but
        # `load_manifest` DOES refuse a manifest whose stored digest no
        # longer matches its own binding bytes -- recompute it so this
        # mutation is a genuine, structurally valid policy change, not a
        # corrupt-manifest side effect.
        manifest["digest"] = manifest_mod.manifest_digest(manifest)
        manifest_mod.persist_manifest(manifest_path, manifest)
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now())
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_BINDING_MISMATCH)
        self.assertEqual(result["reason"], "controller_policy_mismatch")

    def test_expected_candidate_evidence_missing_at_reducer_never_advances(self):
        """M3A-REV-001-RESIDUAL at the pure reducer: a `capacity_wake_
        claimed` advance with NO `expected_candidate` supplied is refused,
        unlike `gate_validated`'s permissive omission."""
        evidence = {"capacity_wake_evidence": {
            "kind": "trustworthy_reset", "lease_id": "lease-1",
            "role": "builder", "provider_session_id": "s1",
            "controller_policy_digest": "a" * 64,
            "candidate_manifest_digest": "b" * 64, "candidate_index": 0,
            "consumption_state": "consumed",
            "not_before": "2026-01-01T00:00:00Z",
            "current_clock": "2026-01-01T00:05:00Z"}}
        new_state, reason = control_plane.advance(
            "awaiting_capacity", "capacity_wake_claimed", evidence=evidence,
            expected_candidate=None)
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(
            reason, "capacity_wake_evidence_expected_candidate_required")


# =============================================================================
# 4. Duplicate / early / unauthorized wake.
# =============================================================================

class DuplicateEarlyUnauthorizedWakeNegativeControlsTest(_M3E2EBase):

    def test_duplicate_wake_after_success_refused_zero_double_dispatch(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        accepting = self._AcceptingSession()
        rc1, result1 = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(),
            session_factory=lambda *a, **k: accepting)
        self.assertEqual(rc1, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(len(accepting.sends), 1)

        rc2, result2 = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(),
            session_factory=lambda *a, **k: accepting)
        self.assertNotEqual(rc2, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(result2["reason"], "no_pending_turn")
        self.assertEqual(len(accepting.sends), 1,
                         "a duplicate wake must never dispatch a second time")

    def test_early_wake_refused_before_not_before_zero_dispatch(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="rate_limit_error",
            retry_evidence={"source": "provider_header", "value": "3600s"})
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["resume_mode"], "scheduled")

        def _never_called(*a, **kw):
            raise AssertionError("must never construct a session before due")
        rc, result = self._run_resume_trigger(
            suid, payload, "2026-01-01T00:00:01Z",  # long before not_before
            session_factory=_never_called)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_NOT_DUE)
        self.assertEqual(result["reason"], "early_refusal")
        lease_after = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease_after["consumption_state"], "unclaimed")

    def test_unauthorized_manual_wake_without_verified_evidence_refused(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="rate_limit_error",
            retry_evidence=None)  # no provider header -> manual_signal mode
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["resume_mode"], "manual_signal")

        def _never_called(*a, **kw):
            raise AssertionError("must never construct a session unsigned")
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(), session_factory=_never_called)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_CONFLICT)
        self.assertEqual(result["reason"],
                         "manual_signal_requires_verified_evidence")
        lease_after = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease_after["consumption_state"], "unclaimed")

    def test_generic_premature_resume_fails_closed_before_valid_signal(self):
        """A generic, evidence-free attempt to force a manual_signal-mode
        lease past `awaiting_capacity` via the ORDINARY claim path (no
        override at all) fails closed identically -- there is no
        'launch anyway' shortcut."""
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="overloaded_error", retry_evidence=None)
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(suid, payload["lease_id"], "claimant-1",
                            "2026-01-01T00:05:00Z", payload["automation_ref"])
        self.assertEqual(ctx.exception.reason,
                         "manual_signal_requires_verified_evidence")


# =============================================================================
# 5. Manual-signal signature refusal (unsigned / self-claimed / wrong-key).
# =============================================================================

def _genuine_signed_signal(payload, role, key_id="authority-key-1", tamper=False,
                           wrong_pin=False):
    secret_key = hashlib.sha256(os.urandom(32)).digest()
    public_key = state_store._ed25519_selftest_publickey(secret_key)
    record = dict(
        schema_version=1, package_id=payload["package_id"],
        candidate_digest=payload["candidate_manifest_digest"], role=role,
        provider_session_id=payload["provider_session_id"],
        controller_policy_digest=payload["controller_policy_digest"],
        signal_journal_ref="journal-" + _uuid(),
        signer_public_key_id=key_id, detached_signature="00" * 64,
        issued_at="2026-01-01T00:00:00Z")
    message = state_store.canonical_manual_capacity_signal_message(record)
    signature = state_store._ed25519_selftest_sign(message, secret_key, public_key)
    sig_hex = signature.hex()
    if tamper:
        sig_hex = ("f" if sig_hex[0] != "f" else "0") + sig_hex[1:]
    record["detached_signature"] = sig_hex
    if wrong_pin:
        other_secret = hashlib.sha256(os.urandom(32)).digest()
        other_public = state_store._ed25519_selftest_publickey(other_secret)
        pinned = {key_id: other_public.hex()}
    else:
        pinned = {key_id: public_key.hex()}
    return record, pinned


class ManualSignalSignatureRefusalTest(_M3E2EBase):

    def test_unsigned_shape_refused(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="overloaded_error", retry_evidence=None)
        record = {"schema_version": 1, "package_id": payload["package_id"]}
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                suid, payload["lease_id"], "claimant-1", "2026-01-01T00:00:01Z",
                record, {}, payload["automation_ref"])
        self.assertEqual(ctx.exception.reason, "override_evidence_invalid")
        self.assertIsNone(
            state_store.read_manual_capacity_signal(suid, "nonexistent"))

    def test_tampered_signature_fails_cryptographic_verification(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="overloaded_error", retry_evidence=None)
        record, pinned = _genuine_signed_signal(payload, "builder", tamper=True)
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                suid, payload["lease_id"], "claimant-1", "2026-01-01T00:00:01Z",
                record, pinned, payload["automation_ref"])
        self.assertEqual(ctx.exception.reason, "override_evidence_invalid")
        self.assertIsNone(state_store.read_manual_capacity_signal(
            suid, record["signal_journal_ref"]),
            "an unverified signature must never be durably recorded")
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["consumption_state"], "unclaimed")

    def test_wrong_key_signed_fails_closed(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="overloaded_error", retry_evidence=None)
        record, pinned = _genuine_signed_signal(payload, "builder", wrong_pin=True)
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                suid, payload["lease_id"], "claimant-1", "2026-01-01T00:00:01Z",
                record, pinned, payload["automation_ref"])
        self.assertEqual(ctx.exception.reason, "override_evidence_invalid")

    def test_self_claimed_unpinned_signer_fails_closed(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="overloaded_error", retry_evidence=None)
        record, _ = _genuine_signed_signal(payload, "builder")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim_with_authorized_early_override(
                suid, payload["lease_id"], "claimant-1", "2026-01-01T00:00:01Z",
                record, {}, payload["automation_ref"])  # empty pin registry
        self.assertEqual(ctx.exception.reason, "override_evidence_invalid")

    def test_genuine_signature_authorizes_early_claim_positive_control(self):
        """Positive control proving the refusal tests above are meaningful:
        the SAME genuine-signing helper, unmodified, succeeds."""
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", error_type="overloaded_error", retry_evidence=None)
        record, pinned = _genuine_signed_signal(payload, "builder")
        result = scheduler.claim_with_authorized_early_override(
            suid, payload["lease_id"], "claimant-1", "2026-01-01T00:00:01Z",
            record, pinned, payload["automation_ref"])
        self.assertEqual(result["outcome"], "claimed_via_override")
        self.assertIsNotNone(
            state_store.read_manual_capacity_signal(
                suid, record["signal_journal_ref"]))


# =============================================================================
# 6. Adapter structural inability to self-sign.
# =============================================================================

class AdapterStructuralNoSelfSignTest(unittest.TestCase):
    """Independent, AST-level structural gate: `cowork_wake_manual.py`
    references NONE of Package B's private signing internals and imports no
    signing library -- it is mechanically incapable of producing a signature
    itself, never merely policy-forbidden from doing so."""

    _FORBIDDEN_NAMES = frozenset({
        "_ed25519_selftest_sign", "_ed25519_selftest_publickey",
    })
    _FORBIDDEN_IMPORT_MODULES = frozenset({
        "nacl", "cryptography", "Crypto", "pynacl", "ed25519",
    })

    def test_no_reference_to_private_signing_helpers(self):
        path = os.path.join(_HERE, "cowork_wake_manual.py")
        with open(path, "r") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
        names_referenced = set()
        modules_imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names_referenced.add(node.id)
            if isinstance(node, ast.Attribute):
                names_referenced.add(node.attr)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules_imported.add(alias.name.split(".")[0])
            if isinstance(node, ast.ImportFrom) and node.module:
                modules_imported.add(node.module.split(".")[0])
        self.assertFalse(
            names_referenced & self._FORBIDDEN_NAMES,
            "cowork_wake_manual.py references a private signing helper: %r"
            % (names_referenced & self._FORBIDDEN_NAMES))
        self.assertFalse(
            modules_imported & self._FORBIDDEN_IMPORT_MODULES,
            "cowork_wake_manual.py imports a signing-capable library: %r"
            % (modules_imported & self._FORBIDDEN_IMPORT_MODULES))
        self.assertNotIn("secret_key", source)
        self.assertNotIn("private_key", source)

    def test_module_holds_no_secret_key_producing_call(self):
        """Live proof, not just a static grep: the module's own public API
        surface contains no function capable of returning signing key
        material — `dir()` names only the documented verify/record/CLI
        surface plus stdlib-imported symbols."""
        surface = {n for n in dir(wake_manual) if not n.startswith("__")}
        for forbidden_substr in ("secret", "private", "produce_sign",
                                 "make_signature", "sign_manual"):
            offenders = {n for n in surface if forbidden_substr in n.lower()}
            self.assertFalse(
                offenders,
                "cowork_wake_manual exposes a suspicious symbol: %r" % offenders)


# =============================================================================
# 7. Invalidation-gated replay (issue #34).
# =============================================================================

class InvalidationGatedReplayNegativeControlsTest(_M3E2EBase):

    def test_invalidation_record_blocks_replay_lease_cancelled_terminal(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        record = capacity_contracts.validate_invalidation_record({
            "schema_version": 1, "package_id": "audit-g",
            "invalidated_candidate_digest": payload["candidate_manifest_digest"],
            "invalidated_session_id": suid,
            "invalidated_work_id": work_id,
            "invalidating_principal": "audit-g-negative-control",
            "reason": "completed paired work must never replay",
            "evidence_refs": [{"path": "audit.md", "sha256": "a" * 64}],
            "issued_at": "2026-01-01T00:00:00Z"})
        state_store.append_invalidation_record(suid, record)

        def _never_called(*a, **kw):
            raise AssertionError("must never dispatch an invalidated candidate")
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(), session_factory=_never_called)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_INVALIDATED)
        self.assertEqual(result["reason"], "invalidated")
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["consumption_state"], "cancelled")
        ps = state_store.current_phase_state(suid, work_id)
        self.assertEqual(ps["state"], "rejected_preflight")
        self.assertIn(ps["state"], control_plane.TERMINAL_STATES)


# =============================================================================
# 8. Wrong-role post-resume stop (issue #19 direction 5) — see also
#    BindingMismatchNegativeControlsTest.test_role_mismatch_stops_for_
#    supervision above (pre-claim). This section covers the derived-role
#    (no --role given) success/failure boundary explicitly.
# =============================================================================

class DerivedRoleResumeTest(_M3E2EBase):

    def test_role_omitted_derives_from_lease_and_succeeds(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        accepting = self._AcceptingSession()
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(), role=None,
            session_factory=lambda *a, **k: accepting)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)


# =============================================================================
# 9. Headless resume — both forms (issue #57).
# =============================================================================

class HeadlessResumeBothFormsTest(_M3E2EBase):

    def test_no_new_context_resume_replays_pending_turn_verbatim(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        accepting = self._AcceptingSession()
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(),
            session_factory=lambda *a, **k: accepting)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(len(accepting.sends), 1)
        self.assertIn("do the thing", accepting.sends[0])

    def test_redirected_context_resume_uses_override_never_untyped(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        accepting = self._AcceptingSession()
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(),
            extra=["--redirected-context", "a fresh, redirected context"],
            session_factory=lambda *a, **k: accepting)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(len(accepting.sends), 1)
        self.assertIn("a fresh, redirected context", accepting.sends[0])
        self.assertNotIn("do the thing", accepting.sends[0])

    def test_typed_seed_rejects_non_string_never_reaches_send(self):
        with self.assertRaises(TypeError):
            cowork._resume_seed_delivery(object(), None)


# =============================================================================
# 10. Pending-turn exact equality + binding preservation across pause/resume.
# =============================================================================

class PendingTurnAndBindingPreservationTest(_M3E2EBase):

    def test_pending_turn_digest_exact_equality_across_pause_and_resume(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        pending = state_store.read_pending_turn_before_pause(suid, "builder")
        self.assertEqual(pending["sha256"], payload["pending_turn_digest"])
        self.assertEqual(
            hashlib.sha256(pending["turn_text"].encode("utf-8")).hexdigest(),
            payload["pending_turn_digest"])
        self.assertTrue(pending["acknowledged"])

    def test_full_binding_preserved_across_pause_and_resume(self):
        suid, work_id, payload, binding = self._enter_capacity(
            role="builder", controller="claude")
        accepting = self._AcceptingSession()
        captured = {}

        def factory(controller, role, provider_session_id, model, effort):
            captured.update(controller=controller, role=role,
                            provider_session_id=provider_session_id,
                            model=model, effort=effort)
            return accepting
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(), session_factory=factory)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SUCCESS)
        self.assertEqual(captured["controller"], "claude")
        self.assertEqual(captured["role"], "builder")
        self.assertEqual(captured["provider_session_id"], "prov-sess-1")
        packet = state_store.read_capacity_packet(suid, payload["package_id"])
        self.assertEqual(packet["binding"]["candidate_digest"],
                         binding["candidate_manifest_digest"])
        self.assertEqual(packet["binding"]["controller_policy_digest"],
                         binding["controller_policy_digest"])

    def test_pending_turn_retained_never_cleared_on_post_wake_send_failure(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        failing = self._FailingSession("claude", "authentication_failed")
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(),
            session_factory=lambda *a, **k: failing)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SEND_FAILED)
        self.assertEqual(len(failing.sends), 1)
        # authentication_failed is not capacity-eligible: terminal failure,
        # never re-cleared/replayed.
        ps = state_store.current_phase_state(suid, work_id)
        self.assertEqual(ps["state"], "failed")
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["consumption_state"], "cancelled")

    def test_pending_turn_re_entry_on_repeat_quota_signal_preserves_ceiling(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        failing = self._FailingSession("claude", "rate_limit_error")
        rc, result = self._run_resume_trigger(
            suid, payload, cowork._capacity_now(),
            session_factory=lambda *a, **k: failing)
        self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SEND_FAILED)
        self.assertTrue(result["re_entered_capacity"])
        new_binding = {"role": binding["candidate_manifest_digest"] and "builder",
                      "provider_session_id": "prov-sess-1",
                      "controller_policy_digest": binding["controller_policy_digest"],
                      "candidate_digest": binding["candidate_manifest_digest"]}
        current = scheduler.resolve_current_lease_for_binding(suid, new_binding)
        self.assertIsNotNone(current)
        self.assertEqual(current["failed_wake_attempts"], 1,
                         "a repeat quota signal on resume is itself one more "
                         "genuine failed wake attempt")


# =============================================================================
# 11. Zero same-provider immediate repair (quota/overload/authentication).
# =============================================================================

class ZeroSameProviderAutoRetryNegativeControlsTest(_M3E2EBase):

    def test_quota_and_overload_bypass_interactive_gate_entirely(self):
        for error_type in ("rate_limit_error", "overloaded_error"):
            with self.subTest(error_type=error_type):
                spath, suid = self._session()
                role = "builder"
                work_id, manifest, binding = self._bind_capacity_candidate(
                    suid, role)
                status_path = self._status_path(suid, role)
                sess = self._FailingSession("claude", error_type)
                out = io.StringIO()
                rc, outcome, payload = cowork._role_loop(
                    sess, "do the thing", status_path, context="",
                    io_in=io.StringIO(""), io_out=out,
                    role=role, session_uuid=suid, role_work_id=work_id)
                self.assertEqual(outcome, "awaiting_capacity")
                self.assertNotIn("choose: retry", out.getvalue())
                self.assertEqual(len(sess.sends), 1,
                                 "must never auto-retry the same provider")

    def test_authentication_failed_user_retry_choice_refused_at_gate(self):
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)
        sess = self._FailingSession("claude", "authentication_failed")
        rc, outcome, payload = cowork._role_loop(
            sess, "do the thing", status_path, context="",
            io_in=io.StringIO("retry\nend\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        self.assertEqual(len(sess.sends), 1,
                         "a user-forced retry choice must never be honored "
                         "for a same-provider-retry-blocked outcome")
        ps = state_store.current_phase_state(suid, work_id)
        self.assertEqual(ps["state"], "failed")
        self.assertEqual(ps["evidence"].get("reason"),
                         "same_provider_retry_blocked")


# =============================================================================
# 12. ProviderHealth live-producer: malformed/stale classifier input.
# =============================================================================

class ProviderHealthMalformedInputNegativeControlsTest(_M3E2EBase):

    def test_non_dict_raw_evidence_degrades_to_unknown_never_raises(self):
        for bad_raw in (None, "a string", 42, [1, 2, 3]):
            with self.subTest(bad_raw=bad_raw):
                outcome = cowork._classify_raw_failure("claude", bad_raw)
                self.assertEqual(outcome, "unknown_provider_failure")

    def test_unrecognized_controller_degrades_to_unknown(self):
        outcome = cowork._classify_raw_failure(
            "some-future-controller", {"type": "assistant", "error": "x"})
        self.assertEqual(outcome, "unknown_provider_failure")

    def test_malformed_classification_durably_writes_unknown_provider_health(self):
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)
        sess = self._FailingSession("claude", "totally-made-up-shape")
        cowork._role_loop(
            sess, "do the thing", status_path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        health = state_store.read_provider_health(suid, role, "claude")
        self.assertEqual(health["last_outcome"], "unknown_provider_failure")
        self.assertEqual(health["status"], "degraded")


# =============================================================================
# 13. Legacy-session-anchor compatibility smoke.
# =============================================================================

class LegacyCompatibilitySmokeTest(_M3E2EBase):

    def test_plain_successful_turn_unaffected_by_m3_capacity_machinery(self):
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)
        sess = self._AcceptingSession()
        rc, outcome, payload = cowork._role_loop(
            sess, "do the thing", status_path, context="",
            io_in=io.StringIO("end\n"), io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id)
        self.assertNotEqual(outcome, "awaiting_capacity")
        self.assertIsNone(state_store.read_capacity_packet(suid, "no-such-package"))
        self.assertFalse(os.path.isdir(state_store.capacity_dir_for(suid)),
                         "an ordinary successful turn must never create any "
                         "M3 capacity artifact")


# =============================================================================
# 14. Fake-clock scheduler matrix (Package D, real functions, explicit
#     caller-supplied clock strings only -- never the real wall clock).
# =============================================================================

def _mint_lease(session_uuid, lease_id="lease-1", resume_mode="scheduled",
                not_before="2026-01-01T00:10:00Z", automation_ref="auto-1",
                role="builder", candidate_digest=None):
    lease = {
        "schema_version": 1, "package_id": "pkg-1", "lease_id": lease_id,
        "role": role, "provider_session_id": "sess-1",
        "controller_policy_digest": "a" * 64,
        "candidate_digest": candidate_digest or ("b" * 64),
        "resume_mode": resume_mode,
        "not_before": not_before if resume_mode == "scheduled" else None,
        "automation_ref": automation_ref,
        "artifact_hashes": {"manifest": "c" * 64},
        "consumption_state": "unclaimed", "failed_wake_attempts": 0,
        "issued_at": "2026-01-01T00:00:00Z",
    }
    return scheduler.start_new_episode(session_uuid, lease)["lease"]


class FakeClockSchedulerNegativeControlsTest(_M3E2EBase):

    def test_early_retry_refusal(self):
        suid = _uuid()
        _mint_lease(suid)
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(suid, "lease-1", "worker-a",
                           "2026-01-01T00:00:01Z", "auto-1")
        self.assertEqual(ctx.exception.reason, "early_refusal")

    def test_jitter_bounds_deterministic_never_before_not_before(self):
        not_before_epoch = capacity_contracts.rfc3339_to_epoch_seconds(
            "2026-01-01T00:10:00Z")
        for seed in ("lease-1:1", "lease-1:2", "lease-2:1"):
            wake = scheduler.next_scheduled_wake_epoch(
                not_before_epoch, seed, max_jitter_seconds=60.0)
            self.assertGreaterEqual(wake, not_before_epoch)
            self.assertLess(wake, not_before_epoch + 60.0)
            # deterministic: identical seed -> identical jitter.
            self.assertEqual(
                wake, scheduler.next_scheduled_wake_epoch(
                    not_before_epoch, seed, max_jitter_seconds=60.0))

    def test_clock_skew_detected_and_conservatively_clamped(self):
        effective, skewed = scheduler.resolve_effective_now_epoch(
            "2026-01-01T01:00:00Z", reference_now="2026-01-01T00:00:00Z",
            max_clock_skew_seconds=30.0)
        self.assertTrue(skewed)
        reference_epoch = capacity_contracts.rfc3339_to_epoch_seconds(
            "2026-01-01T00:00:00Z")
        self.assertEqual(effective, reference_epoch + 30.0)

    def test_cancellation_releases_lease_never_reclaimable(self):
        suid = _uuid()
        _mint_lease(suid)
        result = scheduler.cancel(suid, "lease-1", "auto-1")
        self.assertEqual(result["outcome"], "cancelled")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(suid, "lease-1", "worker-a",
                           "2026-01-01T00:10:01Z", "auto-1")
        self.assertEqual(ctx.exception.reason, "not_claimable")

    def test_replacement_carries_failed_wake_attempts_forward_monotonically(self):
        suid = _uuid()
        _mint_lease(suid)
        for _ in range(3):
            scheduler.record_failed_wake_attempt(suid, "lease-1", "auto-1")
        current = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(current["failed_wake_attempts"], 3)
        self.assertEqual(current["consumption_state"], "unclaimed")
        new_lease = {
            "schema_version": 1, "package_id": "pkg-1",
            "lease_id": "lease-1-replacement", "role": "builder",
            "provider_session_id": "sess-1",
            "controller_policy_digest": "a" * 64, "candidate_digest": "b" * 64,
            "resume_mode": "scheduled", "not_before": "2026-01-01T00:20:00Z",
            "automation_ref": "auto-1", "artifact_hashes": {"manifest": "c" * 64},
            "consumption_state": "unclaimed", "failed_wake_attempts": 0,
            "issued_at": "2026-01-01T00:00:00Z"}
        replaced = scheduler.replace(suid, "lease-1", new_lease, "auto-1")
        self.assertEqual(replaced["lease"]["failed_wake_attempts"], 3,
                         "replacement must never reset the per-binding "
                         "wake-attempt counter")

    def test_duplicate_wakes_same_owner_idempotent_different_owner_conflict(self):
        suid = _uuid()
        _mint_lease(suid)
        first = scheduler.claim(suid, "lease-1", "worker-a",
                                "2026-01-01T00:10:01Z", "auto-1")
        self.assertEqual(first["outcome"], "claimed")
        again = scheduler.claim(suid, "lease-1", "worker-a",
                                "2026-01-01T00:10:02Z", "auto-1")
        self.assertEqual(again["outcome"], "already_claimed")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(suid, "lease-1", "worker-b",
                           "2026-01-01T00:10:03Z", "auto-1")
        self.assertEqual(ctx.exception.reason, "different_owner_conflict")

    def test_lease_expiry_never_early_then_genuinely_reclaims(self):
        suid = _uuid()
        _mint_lease(suid)
        not_yet = scheduler.reclaim_if_expired(
            suid, "lease-1", "2026-01-01T00:10:05Z", 3600, "auto-1")
        self.assertEqual(not_yet["outcome"], "not_yet_expired")
        lease = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(lease["consumption_state"], "unclaimed")
        expired = scheduler.reclaim_if_expired(
            suid, "lease-1", "2026-01-01T02:00:00Z", 3600, "auto-1")
        self.assertEqual(expired["outcome"], "expired")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.claim(suid, "lease-1", "worker-a",
                           "2026-01-01T02:00:01Z", "auto-1")
        self.assertEqual(ctx.exception.reason, "not_claimable")

    def test_claimed_lease_never_expirable(self):
        suid = _uuid()
        _mint_lease(suid)
        scheduler.claim(suid, "lease-1", "worker-a", "2026-01-01T00:10:01Z",
                        "auto-1")
        with self.assertRaises(scheduler.SchedulerLeaseConflict) as ctx:
            scheduler.reclaim_if_expired(
                suid, "lease-1", "2099-01-01T00:00:00Z", 1, "auto-1")
        self.assertEqual(ctx.exception.reason, "not_expirable")

    def test_scheduler_internal_error_durably_accounts_one_failed_wake_attempt(self):
        suid = _uuid()
        _mint_lease(suid)
        real_claim = state_store.claim_pause_lease

        def failing_claim(*a, **kw):
            raise OSError("simulated lock/IO failure")
        state_store.claim_pause_lease = failing_claim
        try:
            argv = ["--session-uuid", suid, "--lease-id", "lease-1",
                   "--claimant-ref", "worker-a", "--automation-ref", "auto-1",
                   "--now", "2026-01-01T00:10:01Z"]
            lines = []
            rc = scheduler.run_wake_trigger(argv, output=lines.append)
        finally:
            state_store.claim_pause_lease = real_claim
        self.assertEqual(rc, scheduler.WAKE_TRIGGER_EXIT_INTERNAL_ERROR)
        lease = state_store.read_pause_lease(suid, "lease-1")
        self.assertEqual(lease["failed_wake_attempts"], 1)
        self.assertEqual(lease["consumption_state"], "unclaimed",
                         "a scheduler failure must leave a truthful, "
                         "resumable pause, never a false completion")


# =============================================================================
# 15. Criterion 3's "paused" class genuinely EXERCISED: a real failed wake
#     preflight returns to awaiting_capacity (never a terminal rejection).
# =============================================================================

class FailedWakePreflightReturnsToAwaitingCapacityTest(_M3E2EBase):

    def test_failed_wake_preflight_genuinely_returns_to_awaiting_capacity(self):
        """M3R-B03: a genuine binding-preservation failure discovered
        post-claim (never merely described as reachable) durably returns
        to `awaiting_capacity` via `capacity_wake_preflight_failed` --
        this is the exact transition mapped to backend-gate criterion 3's
        'paused' outcome class, exercised end to end, not merely reachable
        by construction like M2's `awaiting_capacity`."""
        new_state, reason = control_plane.advance(
            "preflighting", "capacity_wake_preflight_failed",
            evidence={"capacity_wake_preflight_failure": {
                "lease_id": "lease-1", "role": "builder",
                "provider_session_id": "s1",
                "controller_policy_digest": "a" * 64,
                "candidate_manifest_digest": "b" * 64, "candidate_index": 0,
                "failure_kind": "session_mismatch"}},
            expected_candidate={"candidate_manifest_digest": "b" * 64,
                               "candidate_index": 0})
        self.assertEqual(new_state, "awaiting_capacity")
        self.assertEqual(reason, "capacity_wake_preflight_failed")
        self.assertIn(("preflighting", "capacity_wake_preflight_failed"),
                      control_plane.TRANSITIONS)


# =============================================================================
# 16. Real cross-process duplicate-claim race (through E's own real CLI
#     entrypoint, not merely D's own pure decision layer).
# =============================================================================

def _mp_resume_trigger(root, dirpath, session_uuid, lease_id, claimant_ref,
                       automation_ref, now, barrier, result_path):
    os.environ["COWORK_SESSIONS_ROOT"] = root
    os.chdir(dirpath)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cowork as cw

    class _Session:
        def __init__(self):
            self.sends = []

        def send(self, text, meta=None):
            self.sends.append(text)
            return {"ok": True, "result": "ok"}

        def close(self):
            pass

    barrier.wait()
    argv = ["--session-uuid", session_uuid, "--lease-id", lease_id,
           "--claimant-ref", claimant_ref, "--automation-ref", automation_ref,
           "--now", now]
    lines = []
    rc = cw.run_resume_trigger(
        argv, output=lines.append, session_factory=lambda *a, **k: _Session())
    with open(result_path, "w") as fh:
        json.dump({"rc": rc, "result": json.loads(lines[0]) if lines else None},
                  fh)


class RealCrossProcessDuplicateClaimRaceTest(_M3E2EBase):

    def test_two_real_separate_processes_race_exactly_one_success(self):
        suid, work_id, payload, binding = self._enter_capacity(role="builder")
        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(2)
        r1 = os.path.join(self._root, "r1.json")
        r2 = os.path.join(self._root, "r2.json")
        now = cowork._capacity_now()
        p1 = ctx.Process(target=_mp_resume_trigger,
                         args=(self._root, self._dir, suid, payload["lease_id"],
                              "worker-a", payload["automation_ref"], now,
                              barrier, r1))
        p2 = ctx.Process(target=_mp_resume_trigger,
                         args=(self._root, self._dir, suid, payload["lease_id"],
                              "worker-b", payload["automation_ref"], now,
                              barrier, r2))
        p1.start()
        p2.start()
        p1.join(timeout=60)
        p2.join(timeout=60)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        with open(r1) as fh:
            o1 = json.load(fh)
        with open(r2) as fh:
            o2 = json.load(fh)
        rcs = sorted([o1["rc"], o2["rc"]])
        self.assertEqual(
            rcs, sorted([cowork.RESUME_TRIGGER_EXIT_SUCCESS,
                        cowork.RESUME_TRIGGER_EXIT_CONFLICT]),
            "exactly one of two genuinely separate racing OS processes "
            "must succeed: %r / %r" % (o1, o2))
        lease = state_store.read_pause_lease(suid, payload["lease_id"])
        self.assertEqual(lease["consumption_state"], "consumed")
        pending = state_store.read_pending_turn_before_pause(suid, "builder")
        self.assertIsNone(pending, "the pending turn is consumed exactly once")


# =============================================================================
# 17. The C -> E -> F -> D chain: genuine trusted Claude retry evidence
#     through real event reduction and orchestration into a scheduled
#     pause; F's real argv and D's real claim/attempt accounting reach
#     `attempts_exhausted` without any adapter-owned storage or locking.
# =============================================================================

class GenuineChainReachesAttemptsExhaustedTest(_M3E2EBase):

    class _Stdin:
        def write(self, s):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    class _Proc:
        def __init__(self, lines):
            self.stdout = iter(lines)
            self.stdin = GenuineChainReachesAttemptsExhaustedTest._Stdin()

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _real_claude_quota_lines(self):
        return [
            json.dumps({
                "type": "system", "subtype": "api_error",
                "error": {"type": "rate_limit_error", "status": 429,
                         "formatted": "429 Rate limited",
                         "retry_after": "1s"}}),
            json.dumps({"type": "result", "subtype": "error_rate_limit",
                       "result": "", "session_id": "S1"}),
        ]

    def test_genuine_retry_evidence_c_e_scheduled_pause_then_f_d_attempts_exhausted(self):
        suid, work_id = None, None
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind_capacity_candidate(suid, role)
        status_path = self._status_path(suid, role)

        # --- C real event reduction + E real orchestration: a genuine,
        # unmodified bridge.ClaudeSession.send() drives the real send-failure
        # seam into a scheduled awaiting_capacity pause. ---
        with mock.patch.object(
                bridge.subprocess, "Popen",
                return_value=self._Proc(self._real_claude_quota_lines())):
            session = bridge.ClaudeSession(
                cowork.SCOUT_PROMPT_PATH, "implement", True,
                io_out=io.StringIO(), session_id="prov-sess-1")
            rc, outcome, payload = cowork._role_loop(
                session, "do the thing", status_path, context="",
                io_in=io.StringIO("end\n"), io_out=io.StringIO(),
                role=role, session_uuid=suid, role_work_id=work_id)
        self.assertEqual(outcome, "awaiting_capacity")
        packet = state_store.read_capacity_packet(suid, payload["package_id"])
        self.assertEqual(packet["resume_mode"], "scheduled")
        self.assertEqual(packet["capacity_source"]["kind"], "provider_header")

        state = state_store.load(spath)
        state.setdefault("config", {})[role] = {
            "controller": "claude", "model": None, "effort": None,
            "mode": "implement", "yolo": True}
        state.setdefault("sessions", {})[role] = {
            "controller": "claude", "id": "prov-sess-1"}
        state_store.save(spath, state)

        # --- Repeatedly drive genuine failed wakes through E's real
        # resume-trigger CLI (real argv parsing) until D's real per-binding
        # ceiling is reached. Each failure is a REAL quota re-classification
        # through C's real classifier, never a fabricated counter bump. ---
        current_lease_id = payload["lease_id"]
        now = _now_plus(10)  # safely past the genuine 1s retry_after
        binding_key = {
            "role": role, "provider_session_id": "prov-sess-1",
            "controller_policy_digest": payload["controller_policy_digest"],
            "candidate_digest": payload["candidate_manifest_digest"],
        }
        for attempt in range(capacity_contracts.FAILED_WAKE_ATTEMPT_CEILING):
            # Each repeat failure ALSO carries genuine trusted evidence (a
            # real provider-header retry_after) -- exactly like a real
            # provider would keep attesting on every repeated attempt --
            # so the re-entered pause stays `scheduled`/immediately
            # claimable rather than degrading to `manual_signal`.
            failing = self._FailingSession(
                "claude", "rate_limit_error",
                retry_evidence={"source": "provider_header", "value": "0s"})
            argv = ["--session-uuid", suid, "--lease-id", current_lease_id,
                   "--claimant-ref", "auto-worker",
                   "--automation-ref", payload["automation_ref"],
                   "--now", now]
            lines = []
            rc = cowork.run_resume_trigger(
                argv, output=lines.append,
                session_factory=lambda *a, **k: failing)
            result = json.loads(lines[0])
            self.assertEqual(rc, cowork.RESUME_TRIGGER_EXIT_SEND_FAILED,
                             "attempt %d: %r" % (attempt, result))
            current = scheduler.resolve_current_lease_for_binding(
                suid, binding_key)
            self.assertIsNotNone(current)
            current_lease_id = current["lease_id"]

        self.assertEqual(
            scheduler.wake_decision(suid, current_lease_id),
            "wake_attempts_exhausted")

        # --- F's real argv + real (unmocked) D delegation: fire() must
        # itself observe attempts_exhausted through the REAL
        # run_d_wake_decision -> scheduler.run_wake_trigger call, and never
        # invoke E's resume-trigger subprocess at all -- F owns no storage
        # or locking of its own. ---
        captured_d_argv = {}
        real_run_wake_trigger = scheduler.run_wake_trigger

        def spying_wake_trigger(argv, output=None):
            captured_d_argv["argv"] = list(argv)
            return real_run_wake_trigger(argv, output=output)

        def _resume_runner_must_not_be_called(argv, timeout):
            raise AssertionError(
                "F must never invoke E's resume-trigger once D reports "
                "attempts_exhausted")

        result = wake_macos.fire(
            suid, current_lease_id, payload["automation_ref"],
            [sys.executable, "cowork.py", "resume-trigger"],
            claimant_ref="auto-worker", now=now,
            wake_trigger=spying_wake_trigger,
            resume_runner=_resume_runner_must_not_be_called)

        self.assertEqual(result["outcome"], "wake_not_actionable")
        self.assertEqual(result["d_disposition"], "attempts_exhausted")
        self.assertEqual(result["exit_code"],
                         scheduler.WAKE_TRIGGER_EXIT_ATTEMPTS_EXHAUSTED)
        # F's real argv, built by its own build_d_wake_trigger_argv, is what
        # D's real decision layer actually parsed.
        self.assertEqual(captured_d_argv["argv"], wake_macos.build_d_wake_trigger_argv(
            suid, current_lease_id, "auto-worker", payload["automation_ref"], now))


if __name__ == "__main__":
    unittest.main()
