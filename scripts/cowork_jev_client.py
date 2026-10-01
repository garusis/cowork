#!/usr/bin/env python3
"""Bounded, single-attempt client for the Jev observational pilot.

Library only: no CLI and no runtime wiring. The transport, record store,
credential provider and clock are injected, so every behavior is testable
offline. Standard library only.

Guarantees:

- nothing is sent without a valid activation, a credential and a budget;
- one request attempt per unit, never retried, whatever the failure;
- before each attempt a worst-case reservation is taken against the spend cap
  and a write-ahead ``started`` record is fsynced; the first refused
  reservation closes the cohort;
- a charge that cannot be verified stays unknown (null amounts, reservation
  kept consumed); it is never recorded as zero;
- results are normalized to ``jev_response.v1``, ``jev_unit_result.v1`` and
  ``jev_cost.v1`` (class ``jev_observation``, kept apart from the measurement
  cost classes);
- credentials only ever appear in the Authorization header handed to the
  transport.
"""

import ast
import hashlib
import io
import json
import math
import os
import re
import socket
import threading
import time
import tokenize
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

MODEL = "jev-1.13.0"
QUESTION_SET = "jev-obs-q1"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
COST_CLASS = "jev_observation"
PRICE_USD_PER_M_INPUT = Decimal("0.042")
PRICE_BASIS = ("$0.042 per 1,000,000 input tokens as published at "
               "https://docs.typesafe.ai/models on 2026-09-30; re-verified at "
               "activation; output tokens complimentary.")
RESERVATION_TOKENS = 98400
STATE_TOKEN_LIMIT = 24000
TIMEOUT_S = 30
MAX_IN_FLIGHT = 4
UNITS_PER_CANDIDATE = 6
SUFFICIENCY_GATE = 0.70
ALERT_THRESHOLD = 0.80
UNCERTAIN_FLOOR = 0.40
FAILURE_CLASSES = ("auth_401", "invalid_422", "rate_429", "overloaded_529",
                   "http_5xx", "timeout", "connection", "schema_invalid",
                   "model_mismatch", "interrupted_unknown")

SCHEMA_RESPONSE = "jev_response.v1"
SCHEMA_UNIT = "jev_unit_result.v1"
SCHEMA_COST = "jev_cost.v1"
SCHEMA_STARTED = "jev_started.v1"
SCHEMA_OUTCOME = "jev_attempt_outcome.v1"
SCHEMA_CLOSED = "jev_cohort_closed.v1"

QUESTIONS = (
    {"id": "Q-SUF", "type": "noul",
     "instructions": "Is information that is essential for deciding whether "
                     "the code in changed_code satisfies unit.statement "
                     "missing from this state? Answer yes only when a "
                     "decision would require code, requirements or behavior "
                     "that is not shown."},
    {"id": "Q-REQ-1", "type": "noul",
     "instructions": "Does the code in changed_code leave any part of the "
                     "requirement in unit.statement unimplemented?"},
    {"id": "Q-REQ-2", "type": "noul",
     "instructions": "Does the code in changed_code behave in a way that "
                     "directly conflicts with the requirement in "
                     "unit.statement?"},
    {"id": "Q-ERR-1", "type": "noul",
     "instructions": "When the failure described in unit.statement occurs, "
                     "does the code conceal the failure from both the caller "
                     "and the user?"},
    {"id": "Q-ERR-2", "type": "noul",
     "instructions": "When the failure described in unit.statement occurs, "
                     "does the code leave data or state partly changed and "
                     "unreversed while the caller and the user hear nothing "
                     "about it?"},
    {"id": "Q-ERR-3", "type": "noul",
     "instructions": "When the failure described in unit.statement occurs, "
                     "does the code report a different outcome than the "
                     "requirement in task or the surrounding code says it "
                     "must report?"},
)
_QUESTION_BY_ID = {q["id"]: q for q in QUESTIONS}
QUESTION_IDS_BY_KIND = {
    "requirement": ("Q-SUF", "Q-REQ-1", "Q-REQ-2"),
    "error_behavior": ("Q-SUF", "Q-ERR-1", "Q-ERR-2", "Q-ERR-3"),
}

INJECTION_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |all )?(previous|above|instructions)",
    r"you are now",
    r"answer (yes|no) (to|for) (this|the) question",
    r"respond with (yes|no)",
))

_GENERIC_SPAN = re.compile(
    r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`[^`]*`"""
    r"""|#[^\n]*|//[^\n]*|/\*.*?\*/""", re.DOTALL)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def question_set_digest():
    listing = [{"id": q["id"], "type": q["type"],
                "instructions": q["instructions"]} for q in QUESTIONS]
    return sha256_hex(canonical_json(listing))


QUESTION_SET_DIGEST = question_set_digest()


def estimate_tokens(text):
    return -(-len(text.encode("utf-8")) // 3)


def sanitize(text, secret):
    """Replace a credential (bare or as a bearer value) in a string."""
    if not isinstance(text, str) or not secret:
        return text
    return text.replace("Bearer " + secret, "<redacted>").replace(
        secret, "<redacted>")


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def contains_secret(body, secret):
    """True iff raw bytes carry the credential in any common encoding.

    Checks the plain text, JSON-escaped forms (ASCII and unicode escapes),
    URL-quoted form and every decoded JSON string or key.
    """
    if not secret:
        return False
    text = body.decode("utf-8", errors="replace")
    forms = {secret, json.dumps(secret)[1:-1],
             json.dumps(secret, ensure_ascii=False)[1:-1],
             urllib.parse.quote(secret, safe=""),
             "".join("\\u%04x" % ord(c) for c in secret),
             "".join("\\u%04X" % ord(c) for c in secret)}
    if any(f in text or f.encode("utf-8") in body for f in forms):
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return any(secret in s for s in _walk_strings(data))


def _scrub(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return sanitize(value, secret)
    if isinstance(value, dict):
        return {_scrub(k, secret): _scrub(v, secret)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secret) for v in value]
    return value


# --- injection scan ---------------------------------------------------------

def _python_spans(text):
    try:
        ast.parse(text)
        spans = []
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                spans.append(tok.string)
        return spans
    except (SyntaxError, ValueError, tokenize.TokenError,
            IndentationError, MemoryError, RecursionError):
        return None


def _code_spans(text):
    spans = _python_spans(text)
    if spans is None:
        spans = [m.group(0) for m in _GENERIC_SPAN.finditer(text)]
    return spans


def _scan_regions(state_text):
    try:
        state = json.loads(state_text)
    except (ValueError, TypeError):
        state = None
    if (isinstance(state, dict) and isinstance(state.get("changed_code"), list)
            and isinstance(state.get("context"), list)):
        regions = []
        for key in ("changed_code", "context"):
            for entry in state[key]:
                if isinstance(entry, dict) and isinstance(entry.get("text"),
                                                          str):
                    regions.append(entry["text"])
        return regions
    return [state_text]


def injection_scan(state_text):
    """True iff a string literal or comment of the code regions matches."""
    for region in _scan_regions(state_text):
        for span in _code_spans(region):
            if any(p.search(span) for p in INJECTION_PATTERNS):
                return True
    return False


# --- request and response ---------------------------------------------------

def question_ids_for(unit):
    kind = unit.get("kind")
    if kind not in QUESTION_IDS_BY_KIND:
        raise ValueError("unsupported unit kind")
    return QUESTION_IDS_BY_KIND[kind]


def build_request(unit):
    ids = question_ids_for(unit)
    questions = {i: {"type": _QUESTION_BY_ID[i]["type"],
                     "instructions": _QUESTION_BY_ID[i]["instructions"]}
                 for i in ids}
    state = unit["state_text"]
    body = {"model": MODEL, "state": state, "questions": questions}
    digest = sha256_hex(state + canonical_json(questions))
    return {"ids": ids, "body": canonical_json(body).encode("utf-8"),
            "request_digest": digest}


def attempt_id_for(unit_id, request_digest):
    return sha256_hex("%s|%s|%s" % (unit_id, QUESTION_SET_DIGEST,
                                    request_digest))


def _is_prob(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0


def parse_response(body, ids):
    """Strictly parse a 200 body.

    Returns (answers, usage, returned_model, failure_class, unknown_reason);
    on any violation answers and usage are None.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None, None, "schema_invalid", "failed_attempt"
    if not isinstance(data, dict):
        return None, None, None, "schema_invalid", "failed_attempt"
    returned = data.get("model")
    returned = returned if isinstance(returned, str) else None
    if returned != MODEL:
        return None, None, returned, "model_mismatch", "failed_attempt"
    raw = data.get("answers")
    if not isinstance(raw, dict) or set(raw) != set(ids):
        return None, None, returned, "schema_invalid", "failed_attempt"
    answers = {}
    for qid in ids:
        item = raw[qid]
        if (not isinstance(item, dict) or item.get("type") != "noul"
                or not _is_prob(item.get("noul"))):
            return None, None, returned, "schema_invalid", "failed_attempt"
        answers[qid] = item["noul"]
    usage = data.get("usage")
    if (not isinstance(usage, dict) or not _is_count(usage.get("input_tokens"))
            or not _is_count(usage.get("output_tokens"))):
        return None, None, returned, "schema_invalid", "usage_missing"
    return (answers, {"input_tokens": usage["input_tokens"],
                      "output_tokens": usage["output_tokens"]},
            returned, None, None)


def classify_status(status):
    """Failure class for an HTTP status, None for 200 (parse the body)."""
    if status == 200:
        return None
    return {401: "auth_401", 422: "invalid_422", 429: "rate_429",
            529: "overloaded_529"}.get(
        status, "http_5xx" if 500 <= status < 600 else
        "invalid_422" if 400 <= status < 500 else "schema_invalid")


# --- unit status (s4) -------------------------------------------------------

def _unit_record(unit_id, status, queried, sealed_at, *, injection=False,
                 abstain_reason=None, failure_class=None, max_p=None,
                 max_question=None, p_suf=None):
    return {"schema": SCHEMA_UNIT, "unit_id": unit_id, "status": status,
            "queried": queried, "injection_suspected": injection,
            "abstain_reason": abstain_reason, "failure_class": failure_class,
            "max_p": max_p, "max_question": max_question, "p_suf": p_suf,
            "sealed_at": sealed_at}


def unit_status(unit, *, queried, pre_abstain=None, response=None,
                sealed_at):
    """Pure s4 status function (steps 1-7)."""
    unit_id = unit["unit_id"]
    if pre_abstain is not None:
        return _unit_record(unit_id, "abstain", queried, sealed_at,
                            injection=pre_abstain == "injection_suspected",
                            abstain_reason=pre_abstain)
    if response is None:
        raise ValueError("a response is required without a pre-request "
                         "abstain")
    if response.get("outcome") != "ok":
        return _unit_record(unit_id, "service_failure", queried, sealed_at,
                            failure_class=response.get("failure_class"))
    answers = response["answers"]
    p_suf = answers["Q-SUF"]
    if p_suf >= SUFFICIENCY_GATE:
        return _unit_record(unit_id, "abstain", queried, sealed_at,
                            abstain_reason="insufficient_evidence",
                            p_suf=p_suf)
    max_p, max_q = None, None
    for qid in answers:
        if qid == "Q-SUF":
            continue
        if max_p is None or answers[qid] > max_p:
            max_p, max_q = answers[qid], qid
    if max_p >= ALERT_THRESHOLD:
        status, reason = "alert", None
    elif max_p >= UNCERTAIN_FLOOR:
        status, reason = "abstain", "uncertain"
    else:
        status, reason = "no_alert", None
    return _unit_record(unit_id, status, queried, sealed_at,
                        abstain_reason=reason, max_p=max_p,
                        max_question=max_q, p_suf=p_suf)


# --- cost and response records ---------------------------------------------

def _known_usd(input_tokens):
    return Decimal(input_tokens) * PRICE_USD_PER_M_INPUT / Decimal(1000000)


RESERVATION_USD = _known_usd(RESERVATION_TOKENS)


def _cost_record(unit_id, attempt_id, usage, unknown_reason):
    if usage is not None:
        return {"schema": SCHEMA_COST, "cost_class": COST_CLASS,
                "unit_id": unit_id, "attempt_id": attempt_id,
                "reserved_tokens": RESERVATION_TOKENS,
                "charge_status": "known",
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "usd": float(_known_usd(usage["input_tokens"])),
                "unknown_reason": None, "price_basis": PRICE_BASIS}
    return {"schema": SCHEMA_COST, "cost_class": COST_CLASS,
            "unit_id": unit_id, "attempt_id": attempt_id,
            "reserved_tokens": RESERVATION_TOKENS,
            "charge_status": "unknown", "input_tokens": None,
            "output_tokens": None, "usd": None,
            "unknown_reason": unknown_reason, "price_basis": PRICE_BASIS}


def _response_record(base, **fields):
    rec = {"schema": SCHEMA_RESPONSE,
           "record_id": sha256_hex(base["attempt_id"] + "|response"),
           "unit_id": base["unit_id"],
           "question_set_digest": QUESTION_SET_DIGEST,
           "request_digest": base["request_digest"],
           "model_requested": MODEL, "model_returned": None,
           "answers": None, "usage": None,
           "state_tokens_est": base["state_tokens_est"],
           "attempt_id": base["attempt_id"],
           "started_at": base["started_at"], "finished_at": None,
           "latency_ms": None, "outcome": "service_failure",
           "failure_class": None, "raw_response_ref": None,
           "raw_response_withheld": None,
           "price_basis": PRICE_BASIS}
    rec.update(fields)
    return rec


# --- stores -----------------------------------------------------------------

class FileStore:
    """Append-only fsynced JSONL registry plus immutable raw bodies."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.registry_path = os.path.join(out_dir, "jev_registry.jsonl")
        self.raw_dir = os.path.join(out_dir, "raw")
        self._lock = threading.Lock()
        os.makedirs(self.raw_dir, mode=0o700, exist_ok=True)

    def append(self, record):
        line = (json.dumps(record, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(self.registry_path,
                         os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                size = os.fstat(fd).st_size
                if size:
                    last = os.pread(fd, 1, size - 1)
                    if last != b"\n":
                        line = b"\n" + line
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def records(self):
        try:
            with open(self.registry_path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return []
        chunks = data.split(b"\n")
        chunks.pop()  # text after the last newline is a torn tail or empty
        out = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            try:
                rec = json.loads(chunk.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def put_raw(self, data):
        digest = sha256_hex(data)
        final = os.path.join(self.raw_dir, digest)
        if not os.path.exists(final):
            tmp = os.path.join(self.raw_dir, ".tmp-%d-%d-%s" % (
                os.getpid(), threading.get_ident(),
                os.urandom(4).hex()))
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, final)
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
        return "raw/" + digest


# --- replay and recovery ----------------------------------------------------

class _Cohort:
    """Counters rebuilt from started, outcome and closed records."""

    def __init__(self, records, cohort_id):
        self.started_count = 0
        self.closed = False
        self.attempts = {}
        reserved = {}
        settled = {}
        for rec in records:
            if rec.get("cohort_id") != cohort_id:
                continue
            schema = rec.get("schema")
            if schema == SCHEMA_STARTED:
                self.started_count += 1
                reserved[rec["attempt_id"]] = Decimal(rec["reserved_usd"])
                self.attempts[rec["attempt_id"]] = {"started": rec}
            elif schema == SCHEMA_OUTCOME:
                settled[rec["attempt_id"]] = Decimal(rec["committed_usd"])
                self.attempts.setdefault(rec["attempt_id"], {})["outcome"] = \
                    rec
            elif schema == SCHEMA_CLOSED:
                self.closed = True
        self.committed = Decimal(0)
        for attempt, amount in reserved.items():
            self.committed += settled.get(attempt, amount)


def recover(store, clock, *, cohort_id):
    """Resolve started-without-outcome attempts without any send."""
    records = store.records()
    cohort = _Cohort(records, cohort_id)
    appended = []
    for attempt_id, parts in cohort.attempts.items():
        if "outcome" in parts or "started" not in parts:
            continue
        started = parts["started"]
        now = clock()
        base = {"attempt_id": attempt_id, "unit_id": started["unit_id"],
                "request_digest": started["request_digest"],
                "state_tokens_est": started["state_tokens_est"],
                "started_at": started["started_at"]}
        response = _response_record(base, finished_at=now,
                                    failure_class="interrupted_unknown")
        cost = _cost_record(started["unit_id"], attempt_id, None,
                            "interrupted_unknown")
        unit = unit_status({"unit_id": started["unit_id"]}, queried=True,
                           response=response, sealed_at=now)
        outcome = {"schema": SCHEMA_OUTCOME, "cohort_id": cohort_id,
                   "attempt_id": attempt_id, "unit_id": started["unit_id"],
                   "response": response, "cost": cost, "unit_result": unit,
                   "committed_usd": started["reserved_usd"], "at": now}
        store.append(outcome)
        appended.append(outcome)
    return appended


# --- transport --------------------------------------------------------------

def default_transport(url, headers, body, timeout):
    """urllib transport; maps failures to statuses/exceptions, no messages."""
    req = urllib.request.Request(url, data=body, headers=dict(headers),
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (socket.timeout, TimeoutError):
        raise TimeoutError("timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise TimeoutError("timeout") from None
        raise ConnectionError("connection") from None
    except OSError:
        raise ConnectionError("connection") from None


# --- client -----------------------------------------------------------------

def _valid_cap(value):
    if isinstance(value, bool) or not isinstance(value, (int, float,
                                                         Decimal)):
        return None
    try:
        cap = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except ArithmeticError:
        return None
    if not cap.is_finite() or cap <= 0:
        return None
    return cap


def _valid_max_units(value):
    return (isinstance(value, int) and not isinstance(value, bool)
            and value > 0)


class JevClient:
    def __init__(self, transport, store, credential_provider, clock, *,
                 cap_usd, max_units_queried, max_workers=MAX_IN_FLIGHT,
                 guard=None, on_refusal=None):
        # guard() returns None to proceed or a reason string that blocks every
        # NEW attempt (checked before the credential is read and before each
        # reservation; started requests still settle). on_refusal() runs once,
        # just before the cohort-closed record of a refused reservation.
        self.guard = guard
        self.on_refusal = on_refusal
        self.transport = transport
        self.store = store
        self.credential_provider = credential_provider
        self.clock = clock
        self.cap_usd = cap_usd
        self.max_units_queried = max_units_queried
        self.max_workers = max(1, min(int(max_workers), MAX_IN_FLIGHT))
        self._call_lock = threading.RLock()
        self._lock = threading.Lock()
        self._in_flight = 0
        self._peak = 0

    # gates
    def _gate(self, activation):
        if not (isinstance(activation, dict)
                and activation.get("model") == MODEL
                and activation.get("question_set") == QUESTION_SET
                and activation.get("question_set_digest")
                == QUESTION_SET_DIGEST
                and isinstance(activation.get("cohort_id"), str)
                and activation["cohort_id"]):
            return "no_activation", None
        blocked = self._guard_reason()
        if blocked:
            return blocked, None
        try:
            secret = self.credential_provider()
        except Exception:
            secret = None
        if not isinstance(secret, str) or not secret:
            return "no_credential", None
        if (_valid_cap(self.cap_usd) is None
                or not _valid_max_units(self.max_units_queried)):
            return "no_budget", None
        return None, secret

    def _guard_reason(self):
        if self.guard is None:
            return None
        try:
            return self.guard() or None
        except Exception:
            return "guard_error"

    @staticmethod
    def _not_sent(reason):
        return {"status": "not_sent", "reason": reason,
                "cohort_closed": False, "close_reason": None, "units": [],
                "responses": [], "costs": [], "in_flight_peak": 0}

    def _append(self, record, secret):
        self.store.append(_scrub(record, secret))

    def query_candidate(self, candidate_id, units, activation, *,
                        withheld_unit_ids=()):
        with self._call_lock:
            reason, secret = self._gate(activation)
            if reason:
                return self._not_sent(reason)
            for unit in units:
                question_ids_for(unit)
            return self._run(candidate_id, list(units), activation, secret,
                             frozenset(withheld_unit_ids))

    def _run(self, candidate_id, units, activation, secret, withheld):
        cohort_id = activation["cohort_id"]
        cap = _valid_cap(self.cap_usd)
        recover(self.store, self.clock, cohort_id=cohort_id)
        cohort = _Cohort(self.store.records(), cohort_id)
        self._in_flight = 0
        self._peak = 0
        results = {}
        recorded = {}
        slots = threading.Semaphore(self.max_workers)
        futures = []
        closed_now = [cohort.closed]

        applicable = []
        for unit in units:
            if unit.get("applicable") is not True:
                results[unit["unit_id"]] = unit_status(
                    unit, queried=False, pre_abstain="not_applicable",
                    sealed_at=self.clock())
            else:
                applicable.append(unit)
        applicable.sort(key=lambda u: int(u["rank"], 16))
        slot_units = applicable[:UNITS_PER_CANDIDATE]
        for unit in applicable[UNITS_PER_CANDIDATE:]:
            results[unit["unit_id"]] = _unit_record(
                unit["unit_id"], "dropped_cap", False, self.clock())

        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            for unit in slot_units:
                unit_id = unit["unit_id"]
                state_text = unit.get("state_text")
                request = None
                if isinstance(state_text, str):
                    request = build_request(unit)
                    attempt_id = attempt_id_for(unit_id,
                                                request["request_digest"])
                    prior = cohort.attempts.get(attempt_id)
                    if prior and "outcome" in prior:
                        recorded[unit_id] = prior["outcome"]
                        continue
                if closed_now[0]:
                    results[unit_id] = _unit_record(
                        unit_id, "not_queried_cap", False, self.clock())
                    continue
                pre = None
                if request is None or unit_id in withheld:
                    pre = "data_withheld"
                elif injection_scan(state_text):
                    pre = "injection_suspected"
                elif self._state_tokens(unit) > STATE_TOKEN_LIMIT:
                    pre = "context_too_large"
                if pre:
                    results[unit_id] = unit_status(
                        unit, queried=True, pre_abstain=pre,
                        sealed_at=self.clock())
                    continue
                slots.acquire()
                if self._guard_reason():
                    # Suspended or revoked mid-candidate: no new attempt, no
                    # reservation; the unit is final and never retried.
                    slots.release()
                    results[unit_id] = _unit_record(
                        unit_id, "not_queried_interrupted", False,
                        self.clock())
                    continue
                started_at = self.clock()
                refused = None
                with self._lock:
                    if cohort.started_count + 1 > self.max_units_queried \
                            or cohort.committed + RESERVATION_USD > cap:
                        refused = True
                        cohort.closed = True
                    else:
                        cohort.started_count += 1
                        cohort.committed += RESERVATION_USD
                        self._in_flight += 1
                        self._peak = max(self._peak, self._in_flight)
                if refused:
                    slots.release()
                    closed_now[0] = True
                    if self.on_refusal is not None:
                        try:
                            self.on_refusal()
                        except Exception:
                            pass
                    self._append({"schema": SCHEMA_CLOSED,
                                  "cohort_id": cohort_id, "reason": "cap",
                                  "at": self.clock()}, secret)
                    results[unit_id] = _unit_record(
                        unit_id, "not_queried_cap", False, self.clock())
                    continue
                ctx = {"cohort_id": cohort_id, "candidate_id": candidate_id,
                       "unit": unit, "request": request,
                       "attempt_id": attempt_id, "started_at": started_at,
                       "state_tokens_est": self._state_tokens(unit),
                       "secret": secret}
                try:
                    self._append({
                        "schema": SCHEMA_STARTED, "cohort_id": cohort_id,
                        "candidate_id": candidate_id, "unit_id": unit_id,
                        "attempt_id": attempt_id,
                        "request_digest": request["request_digest"],
                        "state_tokens_est": ctx["state_tokens_est"],
                        "reserved_tokens": RESERVATION_TOKENS,
                        "reserved_usd": str(RESERVATION_USD),
                        "started_at": started_at}, secret)
                except BaseException:
                    with self._lock:
                        self._in_flight -= 1
                    slots.release()
                    raise
                futures.append(pool.submit(self._attempt, ctx, cohort,
                                           slots))
        finally:
            pool.shutdown(wait=True)

        responses, costs = [], []
        for fut in futures:
            outcome = fut.result()
            recorded[outcome["unit_id"]] = outcome
        for unit_id, outcome in recorded.items():
            results[unit_id] = outcome["unit_result"]
            responses.append(outcome["response"])
            costs.append(outcome["cost"])
        ordered = [results[u["unit_id"]] for u in units
                   if u["unit_id"] in results]
        result = {"status": "sent", "reason": None,
                  "cohort_closed": closed_now[0],
                  "close_reason": "cap" if closed_now[0] else None,
                  "units": ordered, "responses": responses, "costs": costs,
                  "in_flight_peak": self._peak}
        return _scrub(result, secret)

    @staticmethod
    def _state_tokens(unit):
        est = unit.get("state_tokens_est")
        if isinstance(est, int) and not isinstance(est, bool):
            return est
        return estimate_tokens(unit["state_text"])

    def _attempt(self, ctx, cohort, slots):
        unit = ctx["unit"]
        secret = ctx["secret"]
        base = {"attempt_id": ctx["attempt_id"], "unit_id": unit["unit_id"],
                "request_digest": ctx["request"]["request_digest"],
                "state_tokens_est": ctx["state_tokens_est"],
                "started_at": ctx["started_at"]}
        headers = {"Authorization": "Bearer " + secret,
                   "Content-Type": "application/json",
                   "Accept": "application/json"}
        t0 = time.monotonic()
        failure, status, body = None, None, None
        try:
            status, body = self.transport(ENDPOINT, headers,
                                          ctx["request"]["body"], TIMEOUT_S)
            if not isinstance(status, int) or isinstance(status, bool):
                failure = "connection"
            elif isinstance(body, str):
                body = body.encode("utf-8")
        except (socket.timeout, TimeoutError):
            failure = "timeout"
        except Exception:
            failure = "connection"
        latency = int(round((time.monotonic() - t0) * 1000))
        finished = self.clock()
        answers = usage = returned = raw_ref = withheld = None
        unknown_reason = "failed_attempt"
        if failure is None:
            failure = classify_status(status)
            if failure is None:
                if contains_secret(body, secret):
                    # Unsafe bytes are withheld, never stored altered.
                    withheld = "credential_present"
                else:
                    try:
                        raw_ref = self.store.put_raw(body)
                    except Exception:
                        raw_ref = None
                answers, usage, returned, failure, unknown_reason = \
                    parse_response(body, ctx["request"]["ids"])
        if failure is None:
            response = _response_record(
                base, model_returned=returned, answers=answers, usage=usage,
                finished_at=finished, latency_ms=latency, outcome="ok",
                raw_response_ref=raw_ref, raw_response_withheld=withheld)
            cost = _cost_record(unit["unit_id"], ctx["attempt_id"], usage,
                                None)
            committed = _known_usd(usage["input_tokens"])
        else:
            response = _response_record(
                base, model_returned=returned, finished_at=finished,
                latency_ms=latency, failure_class=failure,
                raw_response_ref=raw_ref, raw_response_withheld=withheld)
            cost = _cost_record(unit["unit_id"], ctx["attempt_id"], None,
                                unknown_reason)
            committed = RESERVATION_USD
        unit_result = unit_status(unit, queried=True, response=response,
                                  sealed_at=finished)
        outcome = {"schema": SCHEMA_OUTCOME, "cohort_id": ctx["cohort_id"],
                   "attempt_id": ctx["attempt_id"],
                   "unit_id": unit["unit_id"], "response": response,
                   "cost": cost, "unit_result": unit_result,
                   "committed_usd": str(committed), "at": finished}
        outcome = _scrub(outcome, secret)
        try:
            self.store.append(outcome)
        finally:
            with self._lock:
                cohort.committed += committed - RESERVATION_USD
                self._in_flight -= 1
            slots.release()
        return outcome
