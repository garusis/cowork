#!/usr/bin/env python3
"""Opt-in automatic Jev observation configuration and session bindings.

Configuration is JSON outside the repository; shell files are never sourced.
Repository matching uses Git's canonical common-dir identity, which includes
linked worktrees but excludes path-prefix and remote-name lookalikes.
"""

import datetime
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
import threading
from decimal import Decimal

import cowork_jev_capture as cap
import cowork_jev_client as jc
import cowork_state as state_store

SCHEMA = "jev_auto_observation.v1"
CONFIG_ENV = "COWORK_JEV_AUTO_CONFIG"
DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".cowork",
                              "jev-auto-config.json")
SESSION_SCHEMA = "jev_auto_session.v1"
_OVERRIDES = {}
_WORKERS = {}
_WORKER_LOCK = threading.Lock()


def configure(**overrides):
    _OVERRIDES.update(overrides)


def reset_overrides():
    _OVERRIDES.clear()
    with _WORKER_LOCK:
        _WORKERS.clear()


@contextlib.contextmanager
def _budget_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def repository_identity(path):
    """Return canonical Git common-dir identity for a worktree, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", os.path.realpath(path), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=3, check=False)
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return os.path.normcase(os.path.realpath(value)) if value else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def repository_roots(path):
    """Canonical primary repository root plus the active linked worktree."""
    roots = set()
    try:
        result = subprocess.run(
            ["git", "-C", os.path.realpath(path), "rev-parse",
             "--show-toplevel", "--path-format=absolute",
             "--git-common-dir"], capture_output=True, text=True,
            timeout=3, check=False)
        if result.returncode != 0:
            return ()
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            roots.add(os.path.realpath(lines[0]))
            roots.add(os.path.realpath(os.path.dirname(lines[-1])))
        return tuple(roots)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()


def trusted_candidate_baseline(repo):
    """Return HEAD only when the enrollment tree is clean and therefore stable."""
    try:
        dirty = subprocess.run(
            ["git", "-C", os.path.realpath(repo), "status", "--porcelain",
             "--untracked-files=all"], capture_output=True, text=True,
            timeout=3, check=False)
        head = subprocess.run(
            ["git", "-C", os.path.realpath(repo), "rev-parse", "--verify",
             "HEAD^{commit}"], capture_output=True, text=True,
            timeout=3, check=False)
        if dirty.returncode or head.returncode or dirty.stdout.strip():
            return None
        return head.stdout.strip() or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def config_path(environ=None):
    env = os.environ if environ is None else environ
    return env.get(CONFIG_ENV) or DEFAULT_CONFIG


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json_atomic(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _load_config_any_state(path=None):
    path = path or config_path()
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return None, "not_configured"
    except (OSError, ValueError):
        return None, "invalid_config"
    if isinstance(value, dict):
        value["_config_path"] = os.path.realpath(path)
    return value, None


def _binding_disabled_reason(binding, repo):
    config, reason = _load_config_any_state(binding.get("config_path"))
    if reason:
        return "configuration_unavailable"
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return "configuration_disabled"
    validated, reason = load_config(binding.get("config_path"))
    if reason or validated is None:
        return "configuration_disabled" if reason == "disabled" else \
            "configuration_invalid"
    if (os.path.normcase(os.path.realpath(validated["repository_identity"]))
            != os.path.normcase(os.path.realpath(
                binding.get("repository_identity", "")))
            or os.path.realpath(validated["pilot_dir"])
            != os.path.realpath(binding.get("pilot_dir", ""))):
        return "configuration_binding_changed"
    return match_config(validated, repo)


def _work_item_path(binding):
    return os.path.join(state_store.session_assets_dir(binding["session_id"]),
                        "jev_auto_work.json")


def _write_work_item(binding, value):
    item = {"schema": "jev_auto_work.v1", "session_id":
            binding["session_id"], "updated_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
    item.update(value)
    _write_json_atomic(_work_item_path(binding), item)


def load_config(path=None, environ=None):
    """Load valid config and return (config, reason); never reads credentials."""
    path = path or config_path(environ)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None, "not_configured"
    except (OSError, ValueError):
        return None, "invalid_config"
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None, "invalid_schema"
    if value.get("enabled") is not True:
        return None, "disabled"
    if value.get("mode") != "observation_only":
        return None, "unsupported_mode"
    if not isinstance(value.get("pilot_dir"), str) or not value["pilot_dir"]:
        return None, "missing_pilot_dir"
    repo_id = value.get("repository_identity")
    if not isinstance(repo_id, str) or not os.path.isabs(repo_id):
        return None, "missing_repository_identity"
    effective = _parse_time(value.get("effective_at"))
    if effective is None:
        return None, "invalid_effective_at"
    if value.get("shared_budget_usd") != 5:
        return None, "invalid_shared_budget"
    credential_env = value.get("credential_env", "JEV_API_KEY")
    if not isinstance(credential_env, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", credential_env):
        return None, "invalid_credential_env"
    value["_config_path"] = os.path.realpath(path)
    return value, None


def match_config(config, repo, now=None):
    if not isinstance(config, dict):
        return "not_configured"
    actual = repository_identity(repo)
    expected = os.path.normcase(os.path.realpath(config["repository_identity"]))
    if not actual or actual != expected:
        return "repository_not_authorized"
    try:
        pilot_dir = os.path.realpath(config["pilot_dir"])
        config_file = config.get("_config_path")
        repo_roots = repository_roots(repo)
        if not repo_roots:
            return "repository_identity_error"
        for candidate in (pilot_dir, config_file):
            if candidate:
                real_candidate = os.path.realpath(candidate)
                for repo_root in repo_roots:
                    if os.path.commonpath((repo_root, real_candidate)) == repo_root:
                        return "configuration_or_artifact_inside_repository"
    except (OSError, subprocess.SubprocessError, ValueError):
        return "repository_identity_error"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    if now < _parse_time(config["effective_at"]):
        return "not_yet_effective"
    return None


def session_binding(config, session_id, repo, objective, now=None):
    """Create a stable, non-secret binding for one newly-created session."""
    reason = match_config(config, repo, now)
    if reason:
        return {"schema": SESSION_SCHEMA, "session_id": session_id,
                "status": "not_enrolled", "reason": reason,
                "mode": "observation_only"}
    objective = objective if isinstance(objective, str) else ""
    requirements = [sentence for sentence in cap._requirement_sentences(
        objective) if cap.MODAL_RE.search(sentence)]
    base_ref = trusted_candidate_baseline(repo)
    return {"schema": SESSION_SCHEMA, "session_id": session_id,
            "status": "enrolled", "reason": None,
            "mode": "observation_only",
            "repository_identity": config["repository_identity"],
            "pilot_dir": os.path.realpath(config["pilot_dir"]),
            "config_path": config.get("_config_path") or
                os.path.realpath(config_path()),
            "effective_at": config["effective_at"],
            "base_ref": base_ref,
            "credential_env": config.get("credential_env", "JEV_API_KEY"),
            "provenance": "cowork_session:%s" % session_id,
            "objective_text": objective,
            "requirements_text": "\n".join(requirements),
            "independent_adjudication": "unavailable_not_authorized",
            "accuracy_metrics": "pending_independent_ground_truth"}


def persist_session_binding(binding):
    """Create truthful initial status until an eligible candidate appears."""
    if isinstance(binding, dict) and binding.get("status") == "enrolled":
        _write_json_atomic(os.path.join(
            state_store.session_assets_dir(binding["session_id"]),
            "jev_auto_binding.json"), binding)
    return write_session_status(binding, "not_applicable",
                                "no_eligible_builder_candidate_yet")


def session_binding_from_file(session_id, repo):
    """Read a previously persisted enrollment from this repo's session file."""
    path = os.path.join(os.path.realpath(repo), ".cowork",
                        "session.%s.json" % session_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = None
    binding = state.get("jev_observation") if isinstance(state, dict) else None
    if binding is None:
        try:
            binding = _read_json(os.path.join(
                state_store.session_assets_dir(session_id),
                "jev_auto_binding.json"))
        except (OSError, ValueError):
            binding = None
    if binding is None:
        # Worktree sessions keep their anchor in the launch directory; a
        # durable external receipt remains locatable even after disablement.
        config, reason = _load_config_any_state()
        if config and repository_identity(repo) == os.path.normcase(
                os.path.realpath(config.get("repository_identity", ""))):
            receipt_path = os.path.join(os.path.realpath(config["pilot_dir"]),
                                        "automatic", "sessions",
                                        session_id + ".json")
            try:
                binding = _read_json(receipt_path)
            except (OSError, ValueError):
                binding = None
    if (isinstance(binding, dict) and binding.get("schema") == SESSION_SCHEMA
            and binding.get("status") == "enrolled"
            and binding.get("session_id") == session_id):
        return binding
    return None


def record_candidate_boundary(binding, repo, _capture_only=False):
    """Capture and observe a candidate with one durable pilot-wide budget."""
    if repository_identity(repo) != os.path.normcase(
            os.path.realpath(binding.get("repository_identity", ""))):
        return write_session_status(binding, "unavailable",
                                    "repository_identity_changed")
    disabled = _binding_disabled_reason(binding, repo)
    if disabled:
        return write_session_status(binding, "unavailable", disabled,
                                    candidate_boundary_reached=True)
    auto_root = os.path.join(binding["pilot_dir"], "automatic")
    if os.path.exists(os.path.join(auto_root, "SUSPENDED")):
        return write_session_status(binding, "excluded", "suspended",
                                    candidate_boundary_reached=True)
    if not binding.get("objective_text", "").strip():
        return write_session_status(binding, "not_applicable",
                                    "no_objective_captured",
                                    candidate_boundary_reached=True)
    if not binding.get("base_ref"):
        return write_session_status(binding, "not_applicable",
                                    "trusted_baseline_unavailable",
                                    candidate_boundary_reached=True)
    if not binding.get("requirements_text", "").strip():
        return write_session_status(binding, "not_applicable",
                                    "requirements_unavailable",
                                    candidate_boundary_reached=True)
    captures_dir = os.path.join(auto_root, "captures")
    os.makedirs(captures_dir, mode=0o700, exist_ok=True)
    capture_started = time.monotonic()
    provenance = binding["provenance"]
    captured = cap.capture_candidate(
        repo, binding["objective_text"], binding.get("requirements_text", ""),
        provenance, [binding["session_id"]], captures_dir,
        base_ref=binding["base_ref"],
        salt="jev-auto-observation.v1", cohort_id="jev-auto-observation.v1",
        clock=cap._utc_now, worktree_root=repo)
    capture_ms = int(round((time.monotonic() - capture_started) * 1000))
    if not captured.get("ok"):
        return write_session_status(binding, "excluded",
                                    captured.get("exclusion_reason") or
                                    "capture_error",
                                    capture_error=(captured.get("error") or
                                                   {}).get("reason"),
                                    capture_ms=capture_ms)
    record = captured.get("record") or {}
    candidate_id = record.get("candidate_id")
    if not candidate_id:
        return write_session_status(binding, "excluded", "capture_error",
                                    capture_ms=capture_ms)
    prepared = {"auto_root": auto_root, "captured": captured,
                "record": record, "candidate_id": candidate_id,
                "capture_ms": capture_ms, "repo": os.path.realpath(repo)}
    if _capture_only:
        return {"prepared": prepared}
    return _query_candidate_boundary(binding, prepared)


def _query_candidate_boundary(binding, prepared):
    auto_root = prepared["auto_root"]
    captured = prepared["captured"]
    record = prepared["record"]
    candidate_id = prepared["candidate_id"]
    capture_ms = prepared["capture_ms"]
    repo = prepared.get("repo") or binding.get("repo_path") or ""
    disabled = _binding_disabled_reason(binding, repo)
    if disabled:
        return write_session_status(binding, "unavailable", disabled,
                                    candidate_boundary_reached=True)
    _write_work_item(binding, {"status": "started",
                               "capture_id": captured.get("capture_id"),
                               "candidate_id": candidate_id,
                               "repo_path": prepared.get("repo") or
                                   binding.get("repo_path")})
    jev_dir = os.path.join(auto_root, "jev")
    os.makedirs(jev_dir, mode=0o700, exist_ok=True)
    activation = {"cohort_id": "jev-auto-observation.v1",
                  "model": jc.MODEL, "question_set": jc.QUESTION_SET,
                  "question_set_digest": jc.QUESTION_SET_DIGEST}

    def query_guard():
        if os.path.exists(os.path.join(auto_root, "SUSPENDED")):
            return "suspended"
        return _binding_disabled_reason(binding, repo)

    client = jc.JevClient(
        _OVERRIDES.get("transport") or jc.default_transport,
        jc.FileStore(jev_dir),
        lambda: os.environ.get(binding.get("credential_env", "JEV_API_KEY")),
        _OVERRIDES.get("clock") or cap._utc_now,
        cap_usd=5.0,
        max_units_queried=max(1, int(Decimal("5") // jc.RESERVATION_USD)),
        max_workers=jc.MAX_IN_FLIGHT,
        guard=query_guard)
    query_started = time.monotonic()
    # The Jev client reservation ledger is append-only. Serialize the read /
    # reserve / append sequence across processes so all worktrees share one cap.
    with _budget_lock(os.path.join(auto_root, "budget.lock")):
        jc.recover(client.store, _OVERRIDES.get("clock") or cap._utc_now,
                   cohort_id="jev-auto-observation.v1")
        result = client.query_candidate(candidate_id, record.get("units") or [],
                                        activation)
    query_ms = int(round((time.monotonic() - query_started) * 1000))
    observed = any(unit.get("queried") is True
                   for unit in (result.get("units") or []))
    if not record.get("units"):
        final_status, unavailable = "not_applicable", "no_eligible_units"
    elif observed:
        final_status, unavailable = "observed", None
    else:
        final_status = "unavailable"
        unavailable = (result.get("reason") or
                       ("budget_exhausted" if result.get("cohort_closed")
                        else "no_eligible_query"))
    costs = result.get("costs") or []
    known_costs = [cost for cost in costs
                   if cost.get("charge_status") == "known"]
    unknown_costs = [cost for cost in costs
                     if cost.get("charge_status") != "known"]
    responses = result.get("responses") or []
    receipt = write_session_status(
        binding, final_status, unavailable,
        capture_id=captured.get("capture_id"),
        candidate_id=candidate_id, capture_ms=capture_ms, query_ms=query_ms,
        usage={"attempts": len(costs),
               "known_input_tokens": sum(c.get("input_tokens", 0) or 0
                                          for c in known_costs),
               "known_output_tokens": sum(c.get("output_tokens", 0) or 0
                                           for c in known_costs),
               "unknown_charge_count": len(unknown_costs),
               "costs": costs},
        latency_ms={"total_query_ms": query_ms,
                    "attempts": [response.get("latency_ms")
                                 for response in responses]},
        errors={"reason": result.get("reason"),
                "failures": [response.get("failure_class")
                             for response in responses
                             if response.get("failure_class")]},
        responses=responses,
        signals=result.get("units") or [],
        accuracy_metrics="pending_independent_ground_truth")
    _write_work_item(binding, {"status": "complete",
                               "capture_id": captured.get("capture_id"),
                               "candidate_id": candidate_id,
                               "observation_status": final_status,
                               "repo_path": repo})
    return receipt


def start_candidate_observation(binding, repo):
    """Capture before review; run the remote query off the delivery path."""
    session_id = binding.get("session_id")
    write_session_status(binding, "candidate_pending",
                         "candidate_capture_started",
                         candidate_boundary_reached=True)
    binding = dict(binding)
    binding["repo_path"] = os.path.realpath(repo)
    captured = record_candidate_boundary(binding, repo, _capture_only=True)
    prepared = captured.get("prepared") if isinstance(captured, dict) else None
    if prepared is None:
        return captured
    prepared["repo"] = os.path.realpath(repo)
    _write_work_item(binding, {"status": "queued",
                               "capture_id": prepared["captured"].get(
                                   "capture_id"),
                               "candidate_id": prepared["candidate_id"],
                               "repo_path": os.path.realpath(repo),
                               "capture_ms": prepared["capture_ms"]})
    return _launch_query_worker(binding, prepared)


def _launch_query_worker(binding, prepared):
    session_id = binding.get("session_id")
    with _WORKER_LOCK:
        worker = _WORKERS.get(session_id)
        if worker is not None and worker.is_alive():
            return {"session_id": session_id, "status": "already_running"}
        def work():
            try:
                _query_candidate_boundary(binding, prepared)
            except Exception:
                write_session_status(binding, "unavailable",
                                     "observer_error",
                                     candidate_boundary_reached=True)

        starter = _OVERRIDES.get("thread_starter")
        if starter is not None:
            starter(work)
            return {"session_id": session_id, "status": "started"}
        worker = threading.Thread(target=work,
                                  name="jev-auto-" + str(session_id)[:12],
                                  daemon=True)
        _WORKERS[session_id] = worker
        worker.start()
    return {"session_id": session_id, "status": "started"}


def recover_session_observation(binding, repo):
    """Resume a queued/started observation without a new builder promotion."""
    if not isinstance(binding, dict) or binding.get("status") != "enrolled":
        return None
    path = _work_item_path(binding)
    item = _read_json(path)
    if not isinstance(item, dict) or item.get("status") == "complete":
        return None
    if _binding_disabled_reason(binding, repo):
        return write_session_status(binding, "unavailable",
                                    _binding_disabled_reason(binding, repo),
                                    candidate_boundary_reached=True)
    auto_root = os.path.join(binding["pilot_dir"], "automatic")
    captures_dir = os.path.join(auto_root, "captures")
    captured = cap.load_capture(captures_dir, item.get("capture_id"))
    if not isinstance(captured, dict):
        return write_session_status(binding, "unavailable",
                                    "queued_capture_missing",
                                    candidate_boundary_reached=True)
    prepared = {"auto_root": auto_root, "captured": {
        "capture_id": item.get("capture_id"), "record": captured},
        "record": captured, "candidate_id": captured.get("candidate_id"),
        "capture_ms": item.get("capture_ms", 0), "repo": os.path.realpath(repo)}
    write_session_status(binding, "candidate_pending", "query_recovery_started",
                         candidate_boundary_reached=True)
    return _launch_query_worker(binding, prepared)


def write_session_status(binding, status, reason=None, **details):
    """Persist user-visible status outside Git; session binding is immutable."""
    if not isinstance(binding, dict) or binding.get("status") != "enrolled":
        return None
    root = os.path.join(binding["pilot_dir"], "automatic", "sessions")
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, binding["session_id"] + ".json")
    record = dict(binding)
    record.update({"observation_status": status, "unavailable_reason": reason,
                   "updated_at": datetime.datetime.now(
                       datetime.timezone.utc).isoformat().replace("+00:00", "Z")})
    record.update(details)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return record
