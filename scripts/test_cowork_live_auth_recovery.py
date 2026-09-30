#!/usr/bin/env python3
"""#101: live Claude authentication proof and recovery freshness.

Provider-free, deterministic regressions for the four layers the fix adds:

1. login METADATA (`claude auth status`) is distinguished from provider-
   accepted live proof in code, trace fields and wording -- a probe-cache hit
   never claims revalidation, and metadata alone is never live proof;
2. a live Claude 401 on a real role turn ends the phase once, as
   `authentication_failed`, naming the safe recovery route and that approved
   upstream artifacts are reused;
3. after a provider-accepted live proof that postdates the failures, exactly
   ONE same-session continuation is admitted (a durable one-shot permit bound
   to the exhausted breaker cause), the accepted send clears the forced-probe
   trigger, and a second continuation without new proof is refused before any
   send;
4. unchanged credentials / unchanged failures still exhaust the bounded
   breaker, a still-rejected probe ends at the probe seam with one spawn and
   zero sends, and non-auth / other-controller paths are byte-identical.

Self-contained: own fixtures and doubles (modeled on
`test_m3_negative_controls._M3E2EBase` and `test_cowork._hermetic_claude_probe`)
rather than imports from any other test module. Every test uses injected
fakes; no real provider is ever contacted. Run through the offline harness:

    python3 scripts/cowork_offline_tests.py test_cowork_live_auth_recovery
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork  # noqa: E402
import cowork_action_policy as action_policy  # noqa: E402
import cowork_bridge as bridge  # noqa: E402
import cowork_capacity as capacity_contracts  # noqa: E402
import cowork_control_plane as control_plane  # noqa: E402
import cowork_dispatch  # noqa: E402
import cowork_probe_cache as probe_cache  # noqa: E402
import cowork_recovery_breaker as recovery_breaker  # noqa: E402
import cowork_state as state_store  # noqa: E402
import cowork_trace as trace_store  # noqa: E402


_ROUTE = cowork._AUTH_RECOVERY_ROUTE

_API_ERROR_401 = {"type": "system", "subtype": "api_error",
                  "error": {"formatted": "401 OAuth token expired"}}
_RESULT_ERROR = {"type": "result", "subtype": "error_during_execution"}
_RESULT_SUCCESS = {"type": "result", "subtype": "success"}
_ASSISTANT = {"type": "assistant",
              "message": {"content": [{"type": "text", "text": "pong"}]}}


def _uuid():
    return str(uuid.uuid4())


def _events(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _cache_keys(path):
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return dict(data.get("keys") or {})


def _write_status(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(value, fh)


_NEEDS_INPUT = {"status": "needs_input",
                "result": {"pending_question": "q"}}


class _FailingSession:
    """A controller session whose every send fails with a flattened
    `error_type` (and, optionally, the additive `http_status` a token-less
    claude api_error now carries)."""

    def __init__(self, controller, error_type, http_status=None,
                 session_id="prov-sess-1"):
        self.controller = controller
        self.error_type = error_type
        self.http_status = http_status
        self.session_id = session_id
        self.model = None
        self.effort = None
        self.sends = []

    def send(self, text, meta=None):
        self.sends.append(text)
        result = {"ok": False, "result": "error",
                  "error_type": self.error_type}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result

    def close(self):
        pass


class _AcceptingSession:
    """A controller session whose send is accepted and leaves a needs_input
    status (with a question) so the role loop stops after exactly one send."""

    def __init__(self, status_path=None, controller="claude",
                 session_id="prov-sess-1"):
        self.status_path = status_path
        self.controller = controller
        self.session_id = session_id
        self.model = None
        self.effort = None
        self.sends = []

    def send(self, text, meta=None):
        self.sends.append(text)
        if self.status_path:
            _write_status(self.status_path, _NEEDS_INPUT)
        return {"ok": True, "result": "ok"}

    def close(self):
        pass


class _Base(unittest.TestCase):
    """Isolated COWORK_SESSIONS_ROOT + isolated cwd per test; nested guard
    inactive unless a test activates it."""

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
        previous = bridge.set_nested_guard_active(False)
        self.addCleanup(bridge.set_nested_guard_active, previous)

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

    def _bind(self, session_uuid, role, controller="claude", model=None,
              effort=None, mode="implement"):
        """Compile a REAL dispatch manifest and bind it as the role's
        WorkUnit candidate. Returns (work_id, manifest, binding)."""
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
            state_store.session_assets_dir(session_uuid),
            "%s.status.json" % role)
        _write_status(status_path, _NEEDS_INPUT)
        return status_path

    def _trace(self, session_uuid):
        path = trace_store.trace_path_for(session_uuid)
        return path, trace_store.Trace(path, session_uuid=session_uuid,
                                       run_id="R")

    def _tmp_trace(self, name):
        path = os.path.join(self._dir, name + ".trace.jsonl")
        return path, trace_store.Trace(path, session_uuid=name, run_id="R")

    def _cause(self, manifest):
        return (manifest["binding"]["config_digest"], manifest["digest"])

    def _exhaust(self, session_uuid, role, config_digest, candidate):
        for _ in range(recovery_breaker.TRIP_THRESHOLD):
            recovery_breaker.attempt(
                state_store.ledger_path_for(session_uuid), role,
                config_digest, "claude", candidate, "controller_failure")

    def _history(self, session_uuid, role, config_digest, candidate):
        return recovery_breaker.history(
            state_store.ledger_path_for(session_uuid), role, config_digest,
            "claude", candidate, "controller_failure")

    def _write_auth_failed_health(self, session_uuid, role,
                                  provider="claude"):
        record = {
            "role": role, "provider": provider, "status": "unavailable",
            "consecutive_failures": 1,
            "last_outcome": "authentication_failed",
            "last_updated_at": cowork._capacity_now(),
        }
        state_store.write_provider_health(session_uuid, record)
        return record

    # -- hermetic probe seams (replicates test_cowork._hermetic_claude_probe,
    #    plus a constant resolved CLI path so the cache key is deterministic).
    def _hermetic_probe(self):
        cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache_dir, True)
        unexpected = []

        def unexpected_spawn(command, stdin_text):
            unexpected.append(list(command))
            raise AssertionError(
                "unscripted live claude probe spawn: %r" % (command,))

        self.addCleanup(lambda: self.assertEqual(
            unexpected, [], "a claude probe reached the real spawn seam"))
        old_cache = os.environ.get("COWORK_PROBE_CACHE")
        os.environ["COWORK_PROBE_CACHE"] = os.path.join(
            cache_dir, "probe_cache.json")

        def restore_cache():
            if old_cache is None:
                os.environ.pop("COWORK_PROBE_CACHE", None)
            else:
                os.environ["COWORK_PROBE_CACHE"] = old_cache
        self.addCleanup(restore_cache)
        for patcher in (
                mock.patch.object(
                    bridge.probe_cache, "claude_version",
                    lambda path: "claude 0.0.0-offline" if path else None),
                mock.patch.object(
                    bridge.probe_cache, "resolve_claude_path",
                    lambda command=None: "/offline/claude"),
                mock.patch.object(bridge, "_real_claude_spawn",
                                  unexpected_spawn)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _cache_key(self, cfg):
        # run_scout passes extra_writable_dir=sessions_dir whenever a
        # session_uuid is set, so the production key has the extradir bit.
        return probe_cache.probe_cache_key(
            "/offline/claude", "claude 0.0.0-offline",
            cowork.SCOUT_PROMPT_PATH, cfg["mode"], cfg["yolo"], True)

    def _prime_cache(self, cfg):
        probe_cache.cache_store(self._cache_key(cfg),
                                path=os.environ["COWORK_PROBE_CACHE"])

    def _scout_manifest(self, session_uuid, cfg):
        """Compile/persist the scout manifest exactly as run_scout does."""
        manifest, _ = cowork._compile_role_manifest(
            role="scout", session_uuid=session_uuid, work_id="scout",
            controller="claude", mode=cfg["mode"], model=None, effort=None,
            instruction_paths=[cowork.SCOUT_PROMPT_PATH],
            sessions_dir=state_store.session_assets_dir(session_uuid))
        return manifest

    def _scripted_spawn(self, events):
        calls = []

        def spawn(command, stdin_text):
            calls.append(list(command))
            return [dict(e) for e in events]
        return calls, spawn


# =============================================================================
# 1. Metadata vs live proof.
# =============================================================================

class MetadataVsProofTests(_Base):

    def test_parse_auth_status_returns_login_metadata_not_authenticated(self):
        import cowork_profiles as controller_profiles
        claude = controller_profiles.parse_auth_status(
            "claude", 0, '{"loggedIn":true,"authMethod":"SECRET"}')
        self.assertEqual(
            claude, {"login_metadata_present": True, "method": "other"})
        codex = controller_profiles.parse_auth_status(
            "codex", 0, "Logged in using ChatGPT")
        self.assertEqual(
            codex, {"login_metadata_present": True, "method": "chatgpt"})
        self.assertNotIn("authenticated", claude)
        self.assertNotIn("authenticated", codex)
        absent = controller_profiles.parse_auth_status(
            "claude", 0, '{"loggedIn":false}')
        self.assertEqual(
            absent, {"login_metadata_present": False, "method": None})

    def _guard_runtime(self):
        return {
            "settings_path": "/guard/settings.json",
            "delegation_allowed": False,
            "scope": action_policy.OwnedScope(),
            "env": {"TMPDIR": "/guard/tmp"},
            "broker": None,
            "profile": None,
            "protected_paths": (),
        }

    def test_cache_hit_never_claims_revalidation_guarded_and_unguarded(self):
        with self.subTest(path="guarded"):
            calls = {"auth": 0, "spawn": 0}

            def auth_run(*_args, **_kwargs):
                calls["auth"] += 1
                return subprocess.CompletedProcess(
                    [], 0,
                    stdout='{"loggedIn":true,"authMethod":"claude.ai"}',
                    stderr="")

            def spawn(*_args, **_kwargs):
                calls["spawn"] += 1
                return []

            previous = bridge.set_nested_guard_active(True)
            self.addCleanup(bridge.set_nested_guard_active, previous)
            tpath, trace = self._tmp_trace("guarded-hit")
            report = {}
            with mock.patch.object(
                    bridge, "_guard_runtime",
                    return_value=self._guard_runtime()), \
                    mock.patch.object(
                        bridge, "kernel_write_boundary",
                        return_value={
                            "available": True, "platform": "darwin",
                            "argv": ["claude", "auth", "status", "--json"],
                        }), \
                    mock.patch.object(
                        bridge.probe_cache, "resolve_claude_path",
                        return_value="/usr/bin/claude"), \
                    mock.patch.object(
                        bridge.probe_cache, "cache_hit", return_value=True):
                ok, alert = bridge.probe_claude_stream_json(
                    spawn, trace=trace, role="scout",
                    extra_writable_dir="/session/assets",
                    cache_enabled=True, version_fn=lambda _path: "1.2.3",
                    auth_run=auth_run, report=report)
            bridge.set_nested_guard_active(False)
            self.assertTrue(ok)
            self.assertIsNone(alert)
            self.assertEqual(calls, {"auth": 1, "spawn": 0})
            events = _events(tpath)
            auth_event = next(e for e in events
                              if e["event"] == "controller.auth.status")
            self.assertIs(auth_event["login_metadata_present"], True)
            self.assertNotIn("authenticated", auth_event)
            cache_event = next(e for e in events
                               if e["event"] == "controller.probe.cache_hit")
            self.assertIs(cache_event["auth_revalidated"], False)
            self.assertIs(cache_event["live_auth_proven"], False)
            self.assertIs(cache_event["login_metadata_present"], True)
            self.assertEqual(report, {
                "cache_hit": True, "live_auth_proven": False,
                "login_metadata_present": True, "controller_outcome": None})
            self.assertFalse(any(e.get("auth_revalidated") is True
                                 for e in events))

        with self.subTest(path="unguarded"):
            bridge.set_nested_guard_active(False)
            spawned = []

            def spawn2(*_args, **_kwargs):
                spawned.append(1)
                return []

            tpath, trace = self._tmp_trace("unguarded-hit")
            report = {}
            with mock.patch.object(
                    bridge.probe_cache, "resolve_claude_path",
                    return_value="/usr/bin/claude"), \
                    mock.patch.object(
                        bridge.probe_cache, "cache_hit", return_value=True):
                ok, alert = bridge.probe_claude_stream_json(
                    spawn2, trace=trace, role="scout",
                    extra_writable_dir="/session/assets",
                    cache_enabled=True, version_fn=lambda _path: "1.2.3",
                    report=report)
            self.assertTrue(ok)
            self.assertIsNone(alert)
            self.assertEqual(spawned, [])
            events = _events(tpath)
            cache_event = next(e for e in events
                               if e["event"] == "controller.probe.cache_hit")
            self.assertIs(cache_event["auth_revalidated"], False)
            self.assertIs(cache_event["live_auth_proven"], False)
            # Metadata was never consulted on this path: no observation.
            self.assertNotIn("login_metadata_present", cache_event)
            self.assertIs(report["login_metadata_present"], None)
            self.assertIs(report["cache_hit"], True)
            self.assertIs(report["live_auth_proven"], False)
            self.assertFalse(any(e.get("auth_revalidated") is True
                                 for e in events))

    def _live_probe(self, name, events):
        cache_path = os.path.join(self._dir, name + ".cache.json")
        tpath, trace = self._tmp_trace(name)
        calls, spawn = self._scripted_spawn(events)
        report = {}
        with mock.patch.object(bridge.probe_cache, "resolve_claude_path",
                               return_value="/offline/claude"):
            ok, alert = bridge.probe_claude_stream_json(
                spawn, mode="plan", yolo=True,
                role_prompt_file=cowork.SCOUT_PROMPT_PATH, trace=trace,
                role="scout", cache_enabled=True,
                version_fn=lambda _path: "v1", cache_path=cache_path,
                report=report)
        key = probe_cache.probe_cache_key(
            "/offline/claude", "v1", cowork.SCOUT_PROMPT_PATH, "plan", True,
            False)
        return ok, alert, report, _events(tpath), calls, key, cache_path

    def test_uncached_probe_401_fails_never_cached_and_classified_auth(self):
        with self.subTest(stream="401 then error result"):
            ok, alert, report, events, calls, key, cache_path = (
                self._live_probe("p401", [_API_ERROR_401, _RESULT_ERROR]))
            self.assertFalse(ok)
            self.assertEqual(len(calls), 1)
            self.assertIn("authentication failed", alert)
            self.assertIn(_ROUTE, alert)
            self.assertFalse(probe_cache.cache_hit(key, path=cache_path))
            self.assertEqual(_cache_keys(cache_path), {})
            end = next(e for e in events
                       if e["event"] == "controller.probe.end")
            self.assertEqual(end["result"], "error")
            self.assertEqual(end["controller_outcome"], "authentication_failed")
            self.assertIs(end["live_auth_proven"], False)
            self.assertFalse(any(e["event"] == "controller.probe.cache_store"
                                 for e in events))
            self.assertEqual(report["controller_outcome"],
                             "authentication_failed")
            self.assertIs(report["live_auth_proven"], False)
            self.assertIs(report["cache_hit"], False)
            self.assertFalse(any(e.get("auth_revalidated") is True
                                 for e in events))

        with self.subTest(stream="assistant cannot override a later 401"):
            ok, alert, report, events, calls, key, cache_path = (
                self._live_probe(
                    "p401b", [_ASSISTANT, _API_ERROR_401, _RESULT_ERROR]))
            self.assertFalse(ok)
            self.assertIn("authentication failed", alert)
            self.assertEqual(report["controller_outcome"],
                             "authentication_failed")
            self.assertFalse(probe_cache.cache_hit(key, path=cache_path))

        with self.subTest(stream="accepted turn is live proof"):
            ok, alert, report, events, calls, key, cache_path = (
                self._live_probe("pok", [_ASSISTANT, _RESULT_SUCCESS]))
            self.assertTrue(ok)
            self.assertIsNone(alert)
            self.assertIs(report["live_auth_proven"], True)
            self.assertIsNone(report["controller_outcome"])
            self.assertIsNotNone(capacity_contracts.rfc3339_to_epoch_seconds(
                report["proven_at"]))
            self.assertTrue(report["probe_work_id"])
            end = next(e for e in events
                       if e["event"] == "controller.probe.end")
            self.assertEqual(end["result"], "ok")
            self.assertIs(end["live_auth_proven"], True)
            self.assertTrue(probe_cache.cache_hit(key, path=cache_path))

    def test_bridge_source_never_emits_auth_revalidated_true(self):
        with open(os.path.join(_HERE, "cowork_bridge.py"), "r") as fh:
            source = fh.read()
        self.assertNotIn("auth_revalidated=True", source)
        self.assertEqual(source.count("auth_revalidated=False"), 2)
        self.assertNotIn("logged in globally", source)


# =============================================================================
# 2. A live 401 on a real role turn.
# =============================================================================

class RoleTurn401Tests(_Base):

    def test_401_status_survives_flattening_and_classifies_authentication_failed(self):
        parsed = bridge.parse_claude_event(dict(_API_ERROR_401))
        self.assertEqual(parsed["kind"], "error")
        self.assertEqual(parsed["error_type"], "api_error")
        self.assertEqual(parsed["http_status"], 401)
        # A real token wins and the status is never attached beside it.
        tokened = bridge.parse_claude_event({
            "type": "system", "subtype": "api_error",
            "error": {"type": "rate_limit_error", "status": 401,
                      "formatted": "401 whatever"}})
        self.assertEqual(tokened["error_type"], "rate_limit_error")
        self.assertNotIn("http_status", tokened)
        status_only = bridge.parse_claude_event({
            "type": "system", "subtype": "api_error",
            "error": {"status": 401, "message": "expired"}})
        self.assertEqual(status_only["error_type"], "api_error")
        self.assertEqual(status_only["http_status"], 401)
        # The flattened send result rebuilds the raw no-token shape.
        raw = cowork._synthesize_raw_failure_evidence(
            "claude", {"error_type": "api_error", "http_status": 401})
        self.assertEqual(raw, {"type": "system", "subtype": "api_error",
                               "error": {"status": 401}})
        self.assertEqual(cowork._classify_raw_failure("claude", raw),
                         "authentication_failed")
        # Without a status the existing assistant shape is byte-identical.
        raw_plain = cowork._synthesize_raw_failure_evidence(
            "claude", {"error_type": "api_error"})
        self.assertEqual(raw_plain, {"type": "assistant", "error": "api_error"})
        self.assertEqual(cowork._classify_raw_failure("claude", raw_plain),
                         "unknown_provider_failure")
        # Other controllers ignore a stray status entirely.
        self.assertEqual(
            cowork._synthesize_raw_failure_evidence(
                "codex", {"error_type": "api_error", "http_status": 401}),
            {"type": "error", "code": "api_error"})
        self.assertEqual(
            cowork._synthesize_raw_failure_evidence(
                "opencode", {"error_type": "api_error", "http_status": 401}),
            {"type": "error", "error": {"name": "api_error"}})

    def test_role_turn_401_ends_phase_once_with_route_and_reuse(self):
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind(suid, role)
        config_digest, candidate = self._cause(manifest)
        status_path = self._status_path(suid, role)
        tpath, trace = self._trace(suid)
        sess = _FailingSession("claude", "api_error", http_status=401)
        out = io.StringIO()
        rc, outcome, payload = cowork._role_loop(
            sess, "do the thing", status_path, context="", io_out=out,
            role=role, session_uuid=suid, role_work_id=work_id, trace=trace)
        self.assertEqual(len(sess.sends), 1)
        self.assertEqual(outcome, "ended")
        self.assertEqual(payload["kind"], "controller_failure")
        self.assertEqual(payload["controller_outcome"], "authentication_failed")
        self.assertEqual(payload["recovery_route"], _ROUTE)
        self.assertIs(payload["upstream_artifacts_reusable"], True)
        self.assertIn("authentication failed", out.getvalue())
        self.assertIn(_ROUTE, out.getvalue())
        health = state_store.read_provider_health(suid, role, "claude")
        self.assertEqual(health["last_outcome"], "authentication_failed")
        self.assertEqual(health["status"], "unavailable")
        ps = state_store.current_phase_state(suid, work_id)
        self.assertEqual(ps["state"], "failed")
        self.assertEqual(ps["evidence"].get("reason"), "send_failed")
        self.assertEqual(
            len(self._history(suid, role, config_digest, candidate)), 1)


# =============================================================================
# 3. One-shot continuation after a provider-accepted live proof.
# =============================================================================

class OneShotContinuationTests(_Base):

    def _permit(self, role="builder", fingerprint="a" * 64, **overrides):
        now = cowork._capacity_now()
        record = {
            "role": role, "provider": "claude", "fingerprint": fingerprint,
            "failure_marker": now, "proof_work_id": "proof-1",
            "proven_at": now, "issued_at": now, "consumed_at": None,
        }
        record.update(overrides)
        return record

    def test_live_auth_permit_store_validate_write_read_consume_once(self):
        suid = _uuid()
        good = self._permit()
        self.assertEqual(state_store.validate_live_auth_permit(good), good)
        for label, bad in (
                ("extra key", dict(good, extra=1)),
                ("missing key", {k: v for k, v in good.items()
                                 if k != "issued_at"}),
                ("non-hex fingerprint", dict(good, fingerprint="Z" * 64)),
                ("short fingerprint", dict(good, fingerprint="a" * 63)),
                ("bad marker", dict(good, failure_marker="yesterday")),
                ("bad proven_at", dict(good, proven_at=12)),
                ("bad consumed_at", dict(good, consumed_at="later")),
                ("empty role", dict(good, role="")),
                ("not a dict", ["x"])):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    state_store.validate_live_auth_permit(bad)
        self.assertIsNone(
            state_store.read_live_auth_permit(suid, "builder", "claude"))
        state_store.write_live_auth_permit(suid, good)
        self.assertEqual(
            state_store.read_live_auth_permit(suid, "builder", "claude"), good)
        marker = good["failure_marker"]
        # Mismatched fingerprint / marker: no consumption, no write.
        self.assertIsNone(state_store.consume_live_auth_permit(
            suid, "builder", "claude", "b" * 64, marker, "2030-01-01T00:00:00Z"))
        self.assertIsNone(state_store.consume_live_auth_permit(
            suid, "builder", "claude", "a" * 64, "2029-01-01T00:00:00Z",
            "2030-01-01T00:00:00Z"))
        self.assertIsNone(state_store.read_live_auth_permit(
            suid, "builder", "claude")["consumed_at"])
        # Exact match consumes once; a second consume returns None.
        now = "2030-01-01T00:00:00Z"
        consumed = state_store.consume_live_auth_permit(
            suid, "builder", "claude", "a" * 64, marker, now)
        self.assertEqual(consumed["consumed_at"], now)
        self.assertEqual(consumed["fingerprint"], "a" * 64)
        self.assertEqual(state_store.read_live_auth_permit(
            suid, "builder", "claude")["consumed_at"], now)
        self.assertIsNone(state_store.consume_live_auth_permit(
            suid, "builder", "claude", "a" * 64, marker,
            "2030-01-01T00:00:01Z"))
        # Missing record: None, no file created.
        self.assertIsNone(state_store.consume_live_auth_permit(
            _uuid(), "builder", "claude", "a" * 64, marker, now))

    def test_live_proof_admits_exactly_one_continuation_then_refuses(self):
        spath, suid = self._session()
        role = "builder"
        work_id, manifest, binding = self._bind(suid, role)
        config_digest, candidate = self._cause(manifest)
        assets = state_store.session_assets_dir(suid)
        os.makedirs(assets, exist_ok=True)
        artifacts = {}
        for name, body in (("scout.intel.json", '{"intel": "approved"}\n'),
                           ("planner.plan.json", '{"plan": "approved"}\n')):
            path = os.path.join(assets, name)
            with open(path, "w") as fh:
                fh.write(body)
            artifacts[path] = _sha256(path)
        self._exhaust(suid, role, config_digest, candidate)
        health = self._write_auth_failed_health(suid, role)
        fingerprint = control_plane.fingerprint(
            role, config_digest, "claude", candidate, "controller_failure")
        now = cowork._capacity_now()
        state_store.write_live_auth_permit(suid, {
            "role": role, "provider": "claude", "fingerprint": fingerprint,
            "failure_marker": health["last_updated_at"],
            "proof_work_id": "proof-1", "proven_at": now, "issued_at": now,
            "consumed_at": None,
        })
        status_path = self._status_path(suid, role)
        tpath, trace = self._trace(suid)
        sess = _AcceptingSession(status_path)
        out = io.StringIO()
        rc, outcome, payload = cowork._role_loop(
            sess, "seed", status_path, context="", io_out=out, role=role,
            session_uuid=suid, role_work_id=work_id, trace=trace)
        self.assertEqual(len(sess.sends), 1, "exactly one admitted send")
        self.assertIn("continues once", out.getvalue())
        events = _events(tpath)
        admits = [e for e in events if e.get("event") == "gate.decision"
                  and e.get("gate") == "recovery_budget"
                  and e.get("action") == "admit_once"]
        self.assertEqual(len(admits), 1)
        self.assertEqual(admits[0]["reason"], "live_auth_proof")
        self.assertEqual(admits[0]["fingerprint"], fingerprint)
        self.assertEqual(admits[0]["proof_work_id"], "proof-1")
        permit = state_store.read_live_auth_permit(suid, role, "claude")
        self.assertIsNotNone(permit["consumed_at"])
        cleared = state_store.read_provider_health(suid, role, "claude")
        self.assertEqual(cleared["status"], "healthy")
        self.assertIsNone(cleared["last_outcome"])
        self.assertEqual(cleared["consecutive_failures"], 0)
        self.assertEqual(
            len([e for e in events
                 if e.get("event") == "provider_health.cleared"]), 1)
        for path, digest in artifacts.items():
            self.assertEqual(_sha256(path), digest)
        self.assertEqual(
            len(self._history(suid, role, config_digest, candidate)),
            recovery_breaker.TRIP_THRESHOLD)
        # The consumed permit still on disk admits nothing more (no
        # duplicate dispatch): refused before any send.
        sess2 = _AcceptingSession(status_path)
        rc, outcome, payload = cowork._role_loop(
            sess2, "seed", status_path, context="", io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id, trace=trace)
        self.assertEqual(len(sess2.sends), 0)
        self.assertEqual(payload["kind"], "recovery_budget_exhausted")
        # A fresh auth failure (new marker) without a new proof: refused,
        # naming the route.
        self._write_auth_failed_health(suid, role)
        sess3 = _AcceptingSession(status_path)
        rc, outcome, payload = cowork._role_loop(
            sess3, "seed", status_path, context="", io_out=io.StringIO(),
            role=role, session_uuid=suid, role_work_id=work_id, trace=trace)
        self.assertEqual(len(sess3.sends), 0)
        self.assertEqual(payload["kind"], "recovery_budget_exhausted")
        self.assertEqual(payload["controller_outcome"], "authentication_failed")
        self.assertEqual(payload["recovery_route"], _ROUTE)
        self.assertEqual(
            len(self._history(suid, role, config_digest, candidate)),
            recovery_breaker.TRIP_THRESHOLD)

    def test_forced_probe_after_reauth_mints_permit_and_ordinary_launch_is_zero_spawn_after_clear(self):
        self._hermetic_probe()
        cfg = {"controller": "claude", "yolo": True, "mode": "plan"}
        config = {"scout": cfg}
        suid = _uuid()
        health = self._write_auth_failed_health(suid, "scout")
        intel = os.path.join(state_store.session_assets_dir(suid),
                             "scout.intel.json")
        calls, spawn = self._scripted_spawn([_ASSISTANT, _RESULT_SUCCESS])
        tpath, trace = self._trace(suid)
        fake = _AcceptingSession(intel)
        out = io.StringIO()
        rc = cowork.run_scout(
            config, "goal", ["scout"], io_out=out, intel_path=intel,
            session_factory=lambda *a, **k: fake,
            reviewer_runner=lambda *a, **k: {"verdict": "approve"},
            claude_spawn=spawn, session_uuid=suid, trace=trace)
        self.assertEqual(len(calls), 1, "the forced probe ran uncached")
        self.assertEqual(len(fake.sends), 1, "the role was reached once")
        events = _events(tpath)
        forced = [e for e in events if e.get("event") == "live_auth.probe.forced"]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["failure_marker"], health["last_updated_at"])
        self.assertEqual(
            len([e for e in events
                 if e.get("event") == "live_auth.permit.issued"]), 1)
        permit = state_store.read_live_auth_permit(suid, "scout", "claude")
        self.assertIsNotNone(permit)
        self.assertEqual(permit["fingerprint"],
                         cowork._breaker_fingerprint_for(suid, "scout", "claude"))
        self.assertEqual(permit["failure_marker"], health["last_updated_at"])
        cleared = state_store.read_provider_health(suid, "scout", "claude")
        self.assertEqual(cleared["status"], "healthy")
        self.assertIsNone(cleared["last_outcome"])
        # The forced probe ran with the cache disabled: nothing was stored.
        self.assertEqual(_cache_keys(os.environ["COWORK_PROBE_CACHE"]), {})
        # Trigger cleared: an ordinary launch with the production key primed
        # is zero-spawn and reports the hit honestly.
        self._prime_cache(cfg)
        fake2 = _AcceptingSession(intel)
        rc = cowork.run_scout(
            config, "goal", ["scout"], io_out=io.StringIO(), intel_path=intel,
            session_factory=lambda *a, **k: fake2,
            reviewer_runner=lambda *a, **k: {"verdict": "approve"},
            claude_spawn=spawn, session_uuid=suid, trace=trace,
            review_packet_ctx={"attempt": 1})
        self.assertEqual(len(calls), 1, "no new probe spawn after the clear")
        self.assertEqual(len(fake2.sends), 1)
        events = _events(tpath)
        self.assertEqual(
            len([e for e in events
                 if e.get("event") == "live_auth.probe.forced"]), 1)
        hits = [e for e in events
                if e.get("event") == "controller.probe.cache_hit"]
        self.assertEqual(len(hits), 1)
        self.assertIs(hits[0]["auth_revalidated"], False)
        self.assertIs(hits[0]["live_auth_proven"], False)


# =============================================================================
# 4. Bounded breaker and the probe seam for unchanged credentials.
# =============================================================================

class BoundedBreakerTests(_Base):

    # The exact pre-#101 shape of a non-auth controller_failure end on a
    # session-bound loop: `_agent_stop_payload`'s six observed facts plus the
    # `status_path` the call site passes, plus the `status_diagnostics` the
    # role loop appends to every stop payload when milestones are enabled
    # (session_uuid + role_work_id bound). None of these come from #101.
    _CONTROLLER_FAILURE_KEYS = frozenset({
        "kind", "role", "requires", "approved", "controller",
        "controller_outcome", "status_path", "status_diagnostics"})

    def test_non_auth_controller_failure_payload_unchanged(self):
        for controller, error_type in (("claude", "ProviderError"),
                                       ("codex", "some_error")):
            with self.subTest(controller=controller):
                spath, suid = self._session()
                role = "builder"
                work_id, manifest, binding = self._bind(
                    suid, role, controller=controller)
                status_path = self._status_path(suid, role)
                sess = _FailingSession(controller, error_type)
                out = io.StringIO()
                rc, outcome, payload = cowork._role_loop(
                    sess, "seed", status_path, context="", io_out=out,
                    role=role, session_uuid=suid, role_work_id=work_id)
                self.assertEqual(len(sess.sends), 1)
                self.assertEqual(outcome, "ended")
                self.assertEqual(payload["kind"], "controller_failure")
                self.assertEqual(set(payload), self._CONTROLLER_FAILURE_KEYS)
                self.assertEqual(payload["controller"], controller)
                self.assertEqual(payload["controller_outcome"],
                                 "unknown_provider_failure")
                self.assertNotIn("recovery_route", payload)
                self.assertNotIn("upstream_artifacts_reusable", payload)
                self.assertNotIn("authentication", out.getvalue())

    def test_exhausted_auth_cause_refuses_before_send_with_route(self):
        with self.subTest(trigger="authentication_failed"):
            spath, suid = self._session()
            role = "builder"
            work_id, manifest, binding = self._bind(suid, role)
            config_digest, candidate = self._cause(manifest)
            self._exhaust(suid, role, config_digest, candidate)
            self._write_auth_failed_health(suid, role)
            status_path = self._status_path(suid, role)
            tpath, trace = self._trace(suid)
            sess = _AcceptingSession(status_path)
            rc, outcome, payload = cowork._role_loop(
                sess, "seed", status_path, context="", io_out=io.StringIO(),
                role=role, session_uuid=suid, role_work_id=work_id,
                trace=trace)
            self.assertEqual(len(sess.sends), 0)
            self.assertEqual((rc, outcome), (0, "ended"))
            self.assertEqual(payload["kind"], "recovery_budget_exhausted")
            self.assertEqual(payload["requires"], "operator")
            self.assertEqual(payload["attempts"], recovery_breaker.TRIP_THRESHOLD)
            self.assertEqual(payload["threshold"], recovery_breaker.TRIP_THRESHOLD)
            self.assertEqual(payload["controller_outcome"],
                             "authentication_failed")
            self.assertEqual(payload["recovery_route"], _ROUTE)
            self.assertIs(payload["upstream_artifacts_reusable"], True)
            refused = [e for e in _events(tpath)
                       if e.get("event") == "gate.decision"
                       and e.get("gate") == "recovery_budget"
                       and e.get("action") == "refuse"]
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["reason"], "authentication_failed")
            self.assertEqual(
                len(self._history(suid, role, config_digest, candidate)),
                recovery_breaker.TRIP_THRESHOLD)

        with self.subTest(trigger="none (M2 parity)"):
            spath, suid = self._session()
            role = "builder"
            work_id, manifest, binding = self._bind(suid, role)
            config_digest, candidate = self._cause(manifest)
            self._exhaust(suid, role, config_digest, candidate)
            status_path = self._status_path(suid, role)
            tpath, trace = self._trace(suid)
            sess = _AcceptingSession(status_path)
            rc, outcome, payload = cowork._role_loop(
                sess, "seed", status_path, context="", io_out=io.StringIO(),
                role=role, session_uuid=suid, role_work_id=work_id,
                trace=trace)
            self.assertEqual(len(sess.sends), 0)
            self.assertEqual(payload["kind"], "recovery_budget_exhausted")
            # M2 parity: the refusal payload plus the role loop's standing
            # `status_diagnostics` (appended to every session-bound stop);
            # no auth fact is present without the trigger.
            self.assertEqual(set(payload), {
                "kind", "role", "requires", "approved", "controller",
                "attempts", "threshold", "status_diagnostics"})
            self.assertNotIn("controller_outcome", payload)
            self.assertNotIn("recovery_route", payload)
            refused = [e for e in _events(tpath)
                       if e.get("event") == "gate.decision"
                       and e.get("gate") == "recovery_budget"
                       and e.get("action") == "refuse"]
            self.assertEqual(len(refused), 1)
            self.assertNotIn("reason", refused[0])

    def test_still_expired_token_terminates_at_probe_seam_bounded(self):
        self._hermetic_probe()
        cfg = {"controller": "claude", "yolo": True, "mode": "plan"}
        config = {"scout": cfg}
        suid = _uuid()
        manifest = self._scout_manifest(suid, cfg)
        config_digest, candidate = self._cause(manifest)
        self._exhaust(suid, "scout", config_digest, candidate)
        self._write_auth_failed_health(suid, "scout")
        # A primed cache must NOT be consulted while the trigger is set.
        self._prime_cache(cfg)
        primed = self._cache_key(cfg)
        calls, spawn = self._scripted_spawn([_API_ERROR_401, _RESULT_ERROR])
        factory_calls = []

        def factory(*args, **kwargs):
            factory_calls.append(args)
            raise AssertionError(
                "a rejected probe must never construct a controller session")

        intel = os.path.join(state_store.session_assets_dir(suid),
                             "scout.intel.json")
        tpath, trace = self._trace(suid)
        out = io.StringIO()
        rc = cowork.run_scout(
            config, "goal", ["scout"], io_out=out, intel_path=intel,
            session_factory=factory, claude_spawn=spawn, session_uuid=suid,
            trace=trace)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1, "exactly one probe spawn")
        self.assertEqual(factory_calls, [], "zero role sends")
        self.assertIsNone(
            state_store.read_live_auth_permit(suid, "scout", "claude"))
        self.assertEqual(_cache_keys(os.environ["COWORK_PROBE_CACHE"]),
                         {primed: True}, "no new cache store")
        events = _events(tpath)
        self.assertFalse(any(
            e.get("event") in ("controller.probe.cache_store",
                               "controller.probe.cache_hit")
            for e in events))
        self.assertEqual(
            len(self._history(suid, "scout", config_digest, candidate)),
            recovery_breaker.TRIP_THRESHOLD, "breaker history untouched")
        health = state_store.read_provider_health(suid, "scout", "claude")
        self.assertEqual(health["last_outcome"], "authentication_failed")
        text = out.getvalue()
        self.assertIn("authentication failed", text)
        self.assertIn(_ROUTE, text)
        role_work_id = cowork._role_work_id(suid, "scout", None, None)
        ps = state_store.current_phase_state(suid, role_work_id)
        self.assertEqual(ps["state"], "rejected_preflight")
        self.assertEqual(ps["evidence"], {
            "reason": "probe_failed",
            "controller_outcome": "authentication_failed",
            "recovery_route": _ROUTE,
            "upstream_artifacts_reusable": True})
        ends = [e for e in events if e.get("event") == "role.end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["result"], "probe_failed")
        self.assertEqual(ends[0]["controller_outcome"], "authentication_failed")
        self.assertEqual(ends[0]["recovery_route"], _ROUTE)
        self.assertIs(ends[0]["upstream_artifacts_reusable"], True)
        alert_line = next(line for line in text.splitlines()
                          if line.startswith("cowork: Claude rejected"))
        alert = alert_line[len("cowork: "):]
        fact = cowork._probe_fact(alert)
        self.assertEqual(set(fact),
                         {"allowed", "refusal_code", "refusal_message", "source"})
        self.assertEqual(fact["refusal_code"], "probe_failed")
        self.assertIn(_ROUTE, fact["refusal_message"])
        cowork_dispatch._validate_fact(fact, "probe_result")

    def test_non_auth_probe_failure_evidence_unchanged(self):
        self._hermetic_probe()
        config = {"scout": {"controller": "claude", "yolo": True,
                            "mode": "plan"}}
        suid = _uuid()
        calls, bad_spawn = self._scripted_spawn([{"type": "other"}])
        intel = os.path.join(state_store.session_assets_dir(suid),
                             "scout.intel.json")
        tpath, trace = self._trace(suid)
        out = io.StringIO()
        rc = cowork.run_scout(
            config, "ctx", ["scout"], io_out=out, intel_path=intel,
            claude_spawn=bad_spawn, session_uuid=suid, trace=trace)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        role_work_id = cowork._role_work_id(suid, "scout", None, None)
        ps = state_store.current_phase_state(suid, role_work_id)
        self.assertEqual(ps["state"], "rejected_preflight")
        self.assertEqual(ps["evidence"], {"reason": "probe_failed"})
        events = _events(tpath)
        self.assertFalse(any(str(e.get("event", "")).startswith("live_auth.")
                             for e in events))
        ends = [e for e in events if e.get("event") == "role.end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["result"], "probe_failed")
        self.assertNotIn("recovery_route", ends[0])
        self.assertNotIn("controller_outcome", ends[0])
        self.assertEqual(cowork._probe_failed_facts({}), {})
        self.assertEqual(
            cowork._probe_failed_facts({"controller_outcome": None}), {})
        self.assertEqual(
            cowork._probe_failed_facts(
                {"controller_outcome": "unknown_provider_failure"}), {})


if __name__ == "__main__":
    unittest.main()
