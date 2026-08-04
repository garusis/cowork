"""Build the five criterion fixture sessions."""
import hashlib, json, os, sys
ROOT = "scripts/fixtures/measurement"


def w(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for line in lines:
            fh.write(json.dumps(line, sort_keys=True) + "\n")


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def ident(controller, model, sid, source="live_event"):
    return {"controller": controller, "provider":
            "anthropic" if controller == "claude" else "openai",
            "model": model, "model_source": source,
            "controller_session_id": sid}


# ---------------------------------------------------------------- C1 --------
# A probe, a completed turn, an in-flight turn, a failed turn, a cancelled
# turn, and a RESUMED Codex evaluation turn whose thread counters are
# cumulative. The Codex figure must come out as that turn's own cost.
c1 = f"{ROOT}/c1-turn-lifecycle"
w(f"{c1}/trace.jsonl", [
    {"ts": "2026-07-28T10:00:00Z", "event": "controller.probe.start",
     "event_id": "e0", "controller": "claude", "role": "scout",
     "work_id": "W-probe", "work_class": "probe"},
    {"ts": "2026-07-28T10:00:02Z", "event": "controller.probe.end",
     "event_id": "e1", "controller": "claude", "role": "scout",
     "work_id": "W-probe", "work_class": "probe", "duration_ms": 2000,
     "usage_scope": "turn_native", "usage": {"input_tokens": 10,
                                             "output_tokens": 2},
     "usage_native": {"input_tokens": 10, "output_tokens": 2}},

    {"ts": "2026-07-28T10:01:00Z", "event": "controller.turn.start",
     "event_id": "e2", "controller": "claude", "role": "scout",
     "work_id": "W-done", "work_class": "productive", "phase": "scouting",
     "round": 1, "prompt_bytes": 100, "usage_scope": "turn_native",
     "identity": ident("claude", "claude-opus-5", "S-claude")},
    {"ts": "2026-07-28T10:01:30Z", "event": "controller.turn.end",
     "event_id": "e3", "controller": "claude", "role": "scout",
     "work_id": "W-done", "work_class": "productive", "result": "ok",
     "duration_ms": 30000, "usage_scope": "turn_native",
     "usage": {"input_tokens": 1000, "output_tokens": 200},
     "usage_native": {"input_tokens": 1000, "output_tokens": 200},
     "identity": ident("claude", "claude-opus-5", "S-claude")},

    # In flight: a start with no end. Duration must read `unknown`, never 0.
    {"ts": "2026-07-28T10:02:00Z", "event": "controller.turn.start",
     "event_id": "e4", "controller": "claude", "role": "planner",
     "work_id": "W-inflight", "work_class": "productive", "phase": "planning",
     "round": 1, "prompt_bytes": 50, "usage_scope": "turn_native",
     "identity": ident("claude", "claude-opus-5", "S-claude")},

    {"ts": "2026-07-28T10:03:00Z", "event": "controller.turn.start",
     "event_id": "e5", "controller": "claude", "role": "planner",
     "work_id": "W-failed", "work_class": "productive", "phase": "planning",
     "round": 2, "prompt_bytes": 60, "usage_scope": "turn_native",
     "identity": ident("claude", "claude-opus-5", "S-claude")},
    {"ts": "2026-07-28T10:03:05Z", "event": "controller.turn.end",
     "event_id": "e6", "controller": "claude", "role": "planner",
     "work_id": "W-failed", "work_class": "failed", "result": "error",
     "error_type": "eof", "duration_ms": 5000, "usage_scope": "turn_native",
     "identity": ident("claude", "claude-opus-5", "S-claude")},

    {"ts": "2026-07-28T10:04:00Z", "event": "controller.turn.start",
     "event_id": "e7", "controller": "codex", "role": "builder",
     "work_id": "W-cancelled", "work_class": "productive", "phase": "building",
     "round": 1, "prompt_bytes": 70, "usage_scope": "turn_delta",
     "identity": ident("codex", "gpt-5.6-sol", "T-codex")},
    {"ts": "2026-07-28T10:04:07Z", "event": "controller.turn.end",
     "event_id": "e8", "controller": "codex", "role": "builder",
     "work_id": "W-cancelled", "work_class": "cancelled",
     "result": "cancelled", "duration_ms": 7000, "usage_scope": "unknown",
     "identity": ident("codex", "gpt-5.6-sol", "T-codex")},

    # THE CUMULATIVE-CODEX CASE. usage_native is the thread's running total
    # (5000 input); usage is this turn's own share (1200). A report that used
    # the native counter would report the whole thread as this turn's cost.
    {"ts": "2026-07-28T10:05:00Z", "event": "eval.turn.start",
     "event_id": "e9", "controller": "codex", "role": "evaluator",
     "work_id": "W-eval", "work_class": "evaluation", "phase": "building",
     "round": 1, "usage_scope": "turn_delta",
     "identity": ident("codex", "gpt-5.6-sol", "T-codex-eval")},
    {"ts": "2026-07-28T10:05:20Z", "event": "eval.turn.end",
     "event_id": "e10", "controller": "codex", "role": "evaluator",
     "work_id": "W-eval", "work_class": "evaluation", "result": "ok",
     "duration_ms": 20000, "usage_scope": "turn_delta",
     "usage": {"input_tokens": 1200, "output_tokens": 300},
     "usage_native": {"input_tokens": 5000, "output_tokens": 900},
     "identity": ident("codex", "gpt-5.6-sol", "T-codex-eval")},

    {"ts": "2026-07-28T10:06:00Z", "event": "user.wait.start",
     "event_id": "e11", "work_id": "WAIT-1", "reason": "confirm"},
    {"ts": "2026-07-28T10:06:12Z", "event": "user.wait.end",
     "event_id": "e12", "work_id": "WAIT-1", "reason": "confirm",
     "outcome": "answered", "duration_ms": 12000},
])
wj(f"{c1}/identities.json", {
    "scout": {"tool": "claude", "model": "claude-opus-5",
              "session_id": "S-claude"},
    "builder": {"tool": "codex", "model": "gpt-5.6-sol",
                "session_id": "T-codex"},
    "observations": [
        {"role": "scout", "work_id": "W-done", "tool": "claude",
         "model": "claude-opus-5", "observed_at": "2026-07-28T10:01:00Z"},
        {"role": "builder", "work_id": "W-cancelled", "tool": "codex",
         "model": "gpt-5.6-sol", "observed_at": "2026-07-28T10:04:00Z"},
    ],
})
wj(f"{c1}/scores.json", {"evaluations": []})
print("c1 ok")

# ---------------------------------------------------------------- C2 --------
# Finding lifecycle: an approving round (ZERO corrective findings), a
# withdrawn finding that must survive as withdrawn, and two evaluator claims
# that must come back unverifiable — one citing a round that never existed,
# one counting the withdrawn finding as real.
c2 = f"{ROOT}/c2-finding-lifecycle"
w(f"{c2}/trace.jsonl", [
    {"ts": "2026-07-28T11:00:00Z", "event": "controller.turn.start",
     "event_id": "f0", "controller": "claude", "role": "builder",
     "work_id": "W1", "work_class": "productive", "phase": "building",
     "round": 1, "prompt_bytes": 200, "usage_scope": "turn_native",
     "identity": ident("claude", "claude-opus-5", "S-b")},
    {"ts": "2026-07-28T11:00:40Z", "event": "controller.turn.end",
     "event_id": "f1", "controller": "claude", "role": "builder",
     "work_id": "W1", "work_class": "productive", "result": "ok",
     "duration_ms": 40000, "usage_scope": "turn_native",
     "usage": {"input_tokens": 500, "output_tokens": 90},
     "usage_native": {"input_tokens": 500, "output_tokens": 90},
     "identity": ident("claude", "claude-opus-5", "S-b")},
    {"ts": "2026-07-28T11:01:00Z", "event": "review.verdict",
     "event_id": "f2", "role": "build-reviewer", "round": 1,
     "verdict": "revise", "findings_count": 2},
    {"ts": "2026-07-28T11:02:00Z", "event": "review.verdict",
     "event_id": "f3", "role": "build-reviewer", "round": 2,
     "verdict": "approve", "findings_count": 0},
])
w(f"{c2}/ledger.jsonl", [
    {"id": "F-0001", "kind": "finding", "state": "open",
     "recorded_at": "2026-07-28T11:01:00Z", "summary": "missing guard",
     "severity": "blocking", "discoverer": "build-reviewer", "round": 1,
     "phase": "building", "disposition": "confirmed"},
    {"id": "F-0002", "kind": "finding", "state": "open",
     "recorded_at": "2026-07-28T11:01:00Z", "summary": "wrong constant",
     "severity": "minor", "discoverer": "build-reviewer", "round": 1,
     "phase": "building"},
    # Withdrawn, and it MUST stay visible as withdrawn.
    {"id": "F-0002", "kind": "finding", "state": "withdrawn", "marker": True,
     "recorded_at": "2026-07-28T11:02:00Z",
     "reason": "reviewer retracted on round 2: the constant was correct"},
    {"id": "V-0001", "kind": "attempt", "state": "open",
     "attempt_key": "S-b:tool_1", "attempt_state": "fresh",
     "recorded_at": "2026-07-28T11:02:30Z", "role": "builder",
     "controller": "claude", "controller_session_id": "S-b",
     "tool_call_id": "tool_1", "command_identity": "python -m unittest <arg>",
     "exit_status": 0, "adjudication": "pass", "executed_count": 859,
     "tty_stdin_mode": "unknown"},
])
wj(f"{c2}/identities.json", {
    "builder": {"tool": "claude", "model": "claude-opus-5",
                "session_id": "S-b"},
    "build-reviewer": {"tool": "codex", "model": "gpt-5.6-sol",
                       "session_id": "T-r"},
})
wj(f"{c2}/scores.json", {"evaluations": [
    {"evaluator": "builder", "evaluatee": "build-reviewer",
     "phase": "building", "round": 1, "context": "review-round",
     "reviewed_verdict": "revise", "evaluatee_tool": "codex",
     "evaluatee_model": "gpt-5.6-sol",
     "criteria": [{"name": "accuracy of findings", "score": 4,
                   "feedback": "both findings were real"}],
     "timestamp": "2026-07-28T11:03:00Z"},
    # UNVERIFIABLE: cites a round that never happened.
    {"evaluator": "builder", "evaluatee": "build-reviewer",
     "phase": "building", "round": 9, "context": "review-round",
     "verification_state": "changed", "citation_failure": True,
     "citations": {"valid": [], "invented": ["F-0099"], "withdrawn": []},
     "evaluatee_tool": "codex", "evaluatee_model": "gpt-5.6-sol",
     "criteria": [{"name": "accuracy of findings", "score": 5,
                   "feedback": "cites a round that never existed"}],
     "timestamp": "2026-07-28T11:03:10Z"},
    # UNVERIFIABLE: counts the withdrawn finding as real.
    {"evaluator": "builder", "evaluatee": "build-reviewer",
     "phase": "building", "round": 2, "context": "review-round",
     "verification_state": "changed", "citation_failure": True,
     "citations": {"valid": [], "invented": [], "withdrawn": ["F-0002"]},
     "evaluatee_tool": "codex", "evaluatee_model": "gpt-5.6-sol",
     "criteria": [{"name": "accuracy of findings", "score": 5,
                   "feedback": "counts a withdrawn finding as real"}],
     "timestamp": "2026-07-28T11:03:20Z"},
    # An honest non-numeric score: round 1 has no prior feedback.
    {"evaluator": "build-reviewer", "evaluatee": "builder",
     "phase": "building", "round": 1, "context": "review-round",
     "reviewed_verdict": "revise", "evaluatee_tool": "claude",
     "evaluatee_model": "claude-opus-5",
     "criteria": [{"name": "responsiveness to feedback",
                   "score": "not_applicable",
                   "feedback": "round 1: no prior feedback to respond to"}],
     "timestamp": "2026-07-28T11:03:30Z"},
]})
print("c2 ok")

# ---------------------------------------------------------------- C3 --------
# Controller-log fixture: a zero-test run that EXITS 0, a truncated red run, an
# unresolved timeout, overlapping runs, an unrestored mutation, and four
# distinct log-failure modes.
c3 = f"{ROOT}/c3-controller-log"
w(f"{c3}/trace.jsonl", [
    {"ts": "2026-07-28T12:00:00Z", "event": "controller.turn.start",
     "event_id": "g0", "controller": "claude", "role": "builder",
     "work_id": "W-c3", "work_class": "productive", "phase": "building",
     "round": 1, "prompt_bytes": 120, "usage_scope": "turn_native",
     "identity": ident("claude", "claude-opus-5", "S-c3")},
    {"ts": "2026-07-28T12:05:00Z", "event": "controller.turn.end",
     "event_id": "g1", "controller": "claude", "role": "builder",
     "work_id": "W-c3", "work_class": "productive", "result": "ok",
     "duration_ms": 300000, "usage_scope": "turn_native",
     "usage": {"input_tokens": 800, "output_tokens": 150},
     "usage_native": {"input_tokens": 800, "output_tokens": 150},
     "identity": ident("claude", "claude-opus-5", "S-c3")},
])
wj(f"{c3}/identities.json", {
    "builder": {"tool": "claude", "model": "claude-opus-5",
                "session_id": "S-c3"},
})
wj(f"{c3}/scores.json", {"evaluations": []})


_CLOCK = {"n": 0}


def _tick(minutes=1):
    """Distinct timestamps. Real controller logs stamp each call separately;
    giving every fixture call the same instant made sequential runs look
    concurrent, which is the fixture lying rather than the reader failing."""
    _CLOCK["n"] += minutes
    return "2026-07-28T12:%02d:00Z" % _CLOCK["n"]


def claude_tool(uid, name, command, result_text, is_error=None, ts=None,
                end_ts=None):
    ts = ts or _tick()
    use = {"type": "assistant", "sessionId": "S-c3", "timestamp": ts,
           "message": {"model": "claude-opus-5",
                       "usage": {"input_tokens": 100, "output_tokens": 20},
                       "content": [{"type": "tool_use", "id": uid,
                                    "name": name,
                                    "input": {"command": command}}]}}
    block = {"type": "tool_result", "tool_use_id": uid,
             "content": [{"type": "text", "text": result_text}]}
    if is_error is not None:
        block["is_error"] = is_error
    res = {"type": "user", "sessionId": "S-c3", "timestamp": end_ts or ts,
           "message": {"content": [block]}}
    return [use, res]


logs = f"{c3}/controller_logs/claude/-fixture-c3"
records = []
# 1. ZERO TESTS, EXIT 0. Exit status alone certifies nothing.
records += claude_tool("t_zero", "Bash", "python -m unittest discover nothing",
                       "Ran 0 tests in 0.001s\n\nOK\nExit code 0")
# 2. A RED run. It must stay red even though a green run follows.
records += claude_tool("t_red", "Bash", "python -m unittest suite",
                       "Ran 12 tests\nFAILED (failures=3)\nExit code 1",
                       is_error=True)
# 3. The later GREEN run of the same suite: a DIFFERENT attempt.
records += claude_tool("t_green", "Bash", "python -m unittest suite",
                       "Ran 12 tests\n\nOK\nExit code 0")
# 4. A TIMEOUT: unresolved, and terminal — no later pass can close it.
records += claude_tool("t_timeout", "Bash", "pytest tests/slow",
                       "command timed out after 120s")
# 5. Genuinely OVERLAPPING runs: two suites whose windows intersect.
records += claude_tool("t_over_a", "Bash", "pytest a", "Exit code 0",
                       ts="2026-07-28T12:30:00Z", end_ts="2026-07-28T12:34:00Z")
records += claude_tool("t_over_b", "Bash", "pytest b", "Exit code 0",
                       ts="2026-07-28T12:32:00Z", end_ts="2026-07-28T12:36:00Z")
# 6. An UNRESTORED mutation that lands WHILE a run is in flight: that run
#    tested a tree that no longer exists, so its evidence must be refused.
records += claude_tool("t_unsafe", "Bash", "pytest guarded", "Exit code 0",
                       ts="2026-07-28T12:40:00Z", end_ts="2026-07-28T12:44:00Z")
records += claude_tool("t_mutate", "Edit", None, "edited",
                       ts="2026-07-28T12:42:00Z")
# 7. A call with NO result: never completed, recorded as unresolved.
records.append({"type": "assistant", "sessionId": "S-c3",
                "timestamp": "2026-07-28T12:50:00Z",
                "message": {"usage": {"input_tokens": 10, "output_tokens": 1},
                            "content": [{"type": "tool_use", "id": "t_orphan",
                                         "name": "Bash",
                                         "input": {"command": "pytest late"}}]}})
w(f"{logs}/S-c3.jsonl", records)

# The four log-failure modes, each its own file.
w(f"{c3}/controller_logs/claude/-fixture-c3/S-unrecognised.jsonl",
  [{"type": "something_else", "nope": 1}])
os.makedirs(f"{c3}/controller_logs/claude/-fixture-c3", exist_ok=True)
with open(f"{c3}/controller_logs/claude/-fixture-c3/S-truncated.jsonl", "w") as fh:
    fh.write(json.dumps({"type": "assistant", "sessionId": "S-t",
                         "timestamp": "2026-07-28T12:00:00Z",
                         "message": {"usage": {"input_tokens": 5},
                                     "content": []}}) + "\n")
    fh.write('{"type": "assistant", "sessionId": "S-t", "mess')  # cut mid-line
# `missing` needs no file; `unreadable` is made at test time (a directory).
print("c3 ok")

# ------------------------------------------------------------- C4 / C5 ------
def multi_round(name, policy):
    """A four-round session, one per policy value. Each ships a PRE-BUILT
    measurement.json so `--report` renders the record rather than rebuilding."""
    base = f"{ROOT}/{name}"
    events = []
    for rnd in (1, 2, 3, 4):
        wid = "W-r%d" % rnd
        events += [
            {"ts": "2026-07-28T13:0%d:00Z" % rnd,
             "event": "controller.turn.start", "event_id": "h%d0" % rnd,
             "controller": "claude", "role": "builder", "work_id": wid,
             "work_class": "productive", "phase": "building", "round": rnd,
             "prompt_bytes": 100 * rnd, "usage_scope": "turn_native",
             "identity": ident("claude", "claude-opus-5", "S-op")},
            {"ts": "2026-07-28T13:0%d:30Z" % rnd,
             "event": "controller.turn.end", "event_id": "h%d1" % rnd,
             "controller": "claude", "role": "builder", "work_id": wid,
             "work_class": "productive", "result": "ok", "duration_ms": 30000,
             "usage_scope": "turn_native",
             "usage": {"input_tokens": 100, "output_tokens": 20},
             "usage_native": {"input_tokens": 100, "output_tokens": 20},
             "identity": ident("claude", "claude-opus-5", "S-op")},
            # ORDERING: the correction handoff is recorded BEFORE the round's
            # evaluation turn starts. C4 asserts exactly this.
            {"ts": "2026-07-28T13:0%d:31Z" % rnd,
             "event": "review.handoff.recorded", "event_id": "h%d2" % rnd,
             "phase": "building", "round": rnd, "from_role": "build-reviewer",
             "to_role": "builder", "kind": "revise"},
        ]
    selected = {"all_rounds": [1, 2, 3, 4], "final_round": [4],
                "sampled": [1, 3], "off": []}[policy]
    for rnd in selected:
        # ISOLATION: every evaluation turn runs on its own session id, never on
        # the operational role's `S-op`.
        events += [
            {"ts": "2026-07-28T13:1%d:00Z" % rnd, "event": "eval.turn.start",
             "event_id": "k%d0" % rnd, "controller": "claude",
             "role": "evaluator", "work_id": "W-e%d" % rnd,
             "work_class": "evaluation", "phase": "building", "round": rnd,
             "usage_scope": "turn_native",
             "identity": ident("claude", "claude-opus-5", "S-eval-%d" % rnd)},
            {"ts": "2026-07-28T13:1%d:10Z" % rnd, "event": "eval.turn.end",
             "event_id": "k%d1" % rnd, "controller": "claude",
             "role": "evaluator", "work_id": "W-e%d" % rnd,
             "work_class": "evaluation", "result": "ok", "duration_ms": 10000,
             "usage_scope": "turn_native",
             "usage": {"input_tokens": 300, "output_tokens": 40},
             "usage_native": {"input_tokens": 300, "output_tokens": 40},
             "identity": ident("claude", "claude-opus-5", "S-eval-%d" % rnd)},
        ]
    # A RECOVERY turn: a replay of unchanged work, worth nothing new.
    events += [
        {"ts": "2026-07-28T13:20:00Z", "event": "controller.turn.start",
         "event_id": "r0", "controller": "claude", "role": "builder",
         "work_id": "W-recov", "work_class": "recovery", "phase": "building",
         "round": 4, "prompt_bytes": 100, "usage_scope": "turn_native",
         "identity": ident("claude", "claude-opus-5", "S-op")},
        {"ts": "2026-07-28T13:20:20Z", "event": "controller.turn.end",
         "event_id": "r1", "controller": "claude", "role": "builder",
         "work_id": "W-recov", "work_class": "recovery", "result": "ok",
         "duration_ms": 20000, "usage_scope": "turn_native",
         "usage": {"input_tokens": 100, "output_tokens": 20},
         "usage_native": {"input_tokens": 100, "output_tokens": 20},
         "identity": ident("claude", "claude-opus-5", "S-op")},
        # An honest status transition: observed state actually moved.
        {"ts": "2026-07-28T13:21:00Z", "event": "status.invalidated",
         "event_id": "r2", "role": "builder", "changed": True,
         "requested_status": "needs_input", "before": "ready_for_review",
         "after": "needs_input", "reason": "work_reopened",
         "triggering_event_id": "h42"},
    ]
    w(f"{base}/trace.jsonl", events)
    wj(f"{base}/identities.json", {
        "builder": {"tool": "claude", "model": "claude-opus-5",
                    "session_id": "S-op"}})
    wj(f"{base}/scores.json", {"evaluations": [
        {"evaluator": "builder", "evaluatee": "build-reviewer",
         "phase": "building", "round": rnd, "context": "review-round",
         "reviewed_verdict": "revise", "evaluatee_tool": "claude",
         "evaluatee_model": "claude-opus-5",
         "criteria": [{"name": "accuracy of findings", "score": 4,
                       "feedback": "ok"},
                      {"name": "responsiveness to feedback",
                       "score": ("not_applicable" if rnd == 1 else 4),
                       "feedback": ("round 1 has no prior feedback"
                                    if rnd == 1 else "addressed")}],
         "timestamp": "2026-07-28T13:3%d:00Z" % rnd}
        for rnd in selected]})
    return base


for pol, nm in (("all_rounds", "c4-multi-round-all-rounds"),
                ("final_round", "c4-multi-round-final-round"),
                ("sampled", "c4-multi-round-sampled"),
                ("off", "c4-multi-round-off")):
    multi_round(nm, pol)
print("c4 ok")

# ---------------------------------------------------------------- C5 --------
# THE RENDERER-PURITY FIXTURE. Its pre-built measurement.json says the
# productive class holds 7 turns; its own trace.jsonl contains 2. `--report`
# without --rebuild MUST print 7. A renderer that recomputes prints 2 and fails,
# which is what makes this a real test rather than a restatement.
c5 = f"{ROOT}/c5-provenance-replay"
c5_events = []
for i in (1, 2):
    c5_events += [
        {"ts": "2026-07-28T14:0%d:00Z" % i, "event": "controller.turn.start",
         "event_id": "p%d0" % i, "controller": "claude", "role": "builder",
         "work_id": "W-p%d" % i, "work_class": "productive",
         "phase": "building", "round": i, "prompt_bytes": 100,
         "usage_scope": "turn_native",
         "identity": ident("claude", "claude-opus-5", "S-p")},
        {"ts": "2026-07-28T14:0%d:10Z" % i, "event": "controller.turn.end",
         "event_id": "p%d1" % i, "controller": "claude", "role": "builder",
         "work_id": "W-p%d" % i, "work_class": "productive", "result": "ok",
         "duration_ms": 10000, "usage_scope": "turn_native",
         "usage": {"input_tokens": 100, "output_tokens": 20},
         "usage_native": {"input_tokens": 100, "output_tokens": 20},
         "identity": ident("claude", "claude-opus-5", "S-p")},
    ]
w(f"{c5}/trace.jsonl", c5_events)
wj(f"{c5}/identities.json", {
    "builder": {"tool": "claude", "model": "claude-opus-5",
                "session_id": "S-p"}})
wj(f"{c5}/scores.json", {"evaluations": []})

DISAGREEING_TURNS = 7      # the RECORD's value
TRACE_TURNS = 2            # what a rebuild would produce


def owned_verification_block():
    """The additive ORCH-050 `owned_verification.*` fields for a pre-built
    record that models NO owned transactions (the only pre-built fixture,
    c5): every subfield the build side now always emits, at its empty/default
    value — present so the record body names the current fields, with
    contents that cannot change what the report renders for it. A fixture
    that models owned transactions should be produced by running
    `cowork_measure.build_record` over synthetic raw sources instead, so the
    disposition join / cost rollups are computed by the real build side."""
    return {
        "transactions": [],
        "transaction_count": 0,
        "latest": None,
        "cost": None,
        "focused_attribution": [],
        "incurred_cost": {"work_items": 0, "subprocess_wall_time_s": 0.0},
        "accepted_cost": {"work_items": 0, "subprocess_wall_time_s": 0.0},
        "avoided_cost": {"reuse_count": 0, "subprocess_wall_time_s": 0.0,
                         "reused": []},
    }


wj(f"{c5}/measurement.json", {
    "schema_version": 1,
    "session": "c5-provenance-replay",
    "built_at": "2026-07-28T14:30:00Z",
    "built_from": {"trace": {"state": "ok", "sha256": "stale-on-purpose",
                             "lines": 4, "bytes": 0}},
    "work": {},
    "orphan_ends": [],
    "cost": {"by_class": {"productive": {"turns": DISAGREEING_TURNS,
                                         "usage": {"input_tokens": 700},
                                         "duration_ms": 70000,
                                         "duration_unknown_turns": 0}},
             "classified_total": {"input_tokens": 700},
             "turn_total": {"input_tokens": 700},
             "turns_with_native_counters": DISAGREEING_TURNS,
             "unreconciled": {}, "reconciled": True,
             "incomparable": {"turns": 0, "work_ids": []},
             "unclassified": {"turns": 0, "work_ids": []}},
    "duration": {"by_class": {"productive_ms": 70000, "user_wait_ms": 0,
                              "turns_with_unknown_duration": 0},
                 "user_wait_spans": [], "user_wait_unresolved": []},
    "input_sources": {"measured_bytes": {}, "measured_bytes_total": 0,
                      "provider_token_axes": {}, "attributed_input_tokens": 700,
                      "unattributed_input_tokens": 0, "incomparable_turns": 0,
                      "note": "fixture"},
    "verification_attempts": [],
    "findings": {"total": 0, "confirmed": 0, "withdrawn": 0, "superseded": 0,
                 "open": 0, "by_severity": {}},
    "ledger": {}, "score_cohorts": {}, "enhancements": {}, "ingestion": {},
    "identities": {}, "replay": [],
    # ORCH-050 additive fields: present at their empty/default values (this
    # fixture models no owned transactions) — never a relaxed assertion.
    "owned_verification": owned_verification_block(),
    "pricing": {"schema_version": 1, "snapshot_id": "empty",
                "captured_at": None, "priced_turns": 0,
                "unpriced_turns": DISAGREEING_TURNS, "by_model": {},
                "note": "fixture"},
    "evaluation_queue": {"pending": 0, "pending_entries": []},
    "completion": [{"item": "measurement foundation", "state": "delivered",
                    "evidence": ["record.cost.by_class"], "reason": None}],
    "trace_summary": {"turn_count": DISAGREEING_TURNS},
    "incomplete": [],
})
print("c5 ok  (record says %d turns, trace has %d)"
      % (DISAGREEING_TURNS, TRACE_TURNS))

# --- C3, part 2: a fake CODEX rollout, so the fixture covers BOTH formats ----
codex_dir = f"{c3}/controller_logs/codex/sessions/2026/07/28"
codex = [
    {"timestamp": "2026-07-28T12:10:00Z", "type": "session_meta",
     "payload": {"session_id": "T-c3", "id": "T-c3",
                 "cwd": "/fixture", "cli_version": "0.0.0-fixture"}},
    {"timestamp": "2026-07-28T12:10:01Z", "type": "event_msg",
     "payload": {"type": "task_started", "turn_id": "turn-1"}},
    {"timestamp": "2026-07-28T12:10:02Z", "type": "response_item",
     "payload": {"type": "custom_tool_call", "call_id": "cx_red",
                 "name": "exec",
                 "input": '{"cmd":"python -m unittest suite"}'}},
    # A RED codex run: the output leads with an error form, not "Script
    # completed", which is the only status signal codex exposes.
    {"timestamp": "2026-07-28T12:10:09Z", "type": "response_item",
     "payload": {"type": "custom_tool_call_output", "call_id": "cx_red",
                 "output": [{"type": "input_text",
                             "text": "Script failed\nWall time 7.0 seconds\n"
                                     "Output:\nRan 12 tests\n"
                                     "FAILED (failures=2)"}]}},
    {"timestamp": "2026-07-28T12:10:10Z", "type": "response_item",
     "payload": {"type": "custom_tool_call", "call_id": "cx_zero",
                 "name": "exec",
                 "input": '{"cmd":"pytest tests/empty"}'}},
    # ZERO tests, and codex says the script completed. Exit-shaped success with
    # nothing verified.
    {"timestamp": "2026-07-28T12:10:12Z", "type": "response_item",
     "payload": {"type": "custom_tool_call_output", "call_id": "cx_zero",
                 "output": [{"type": "input_text",
                             "text": "Script completed\nWall time 1.0 seconds\n"
                                     "Output:\ncollected 0 items"}]}},
    {"timestamp": "2026-07-28T12:10:13Z", "type": "event_msg",
     "payload": {"type": "patch_apply_end", "call_id": "cx_patch",
                 "turn_id": "turn-1", "success": True,
                 "changes": {"/fixture/a.py": {"type": "modify"}}}},
    {"timestamp": "2026-07-28T12:10:20Z", "type": "event_msg",
     "payload": {"type": "token_count", "info": {
         "last_token_usage": {"input_tokens": 400, "output_tokens": 60},
         "total_token_usage": {"input_tokens": 400, "output_tokens": 60}}}},
    {"timestamp": "2026-07-28T12:10:21Z", "type": "event_msg",
     "payload": {"type": "task_complete", "turn_id": "turn-1"}},
]
w(f"{codex_dir}/rollout-2026-07-28T12-10-00-T-c3.jsonl", codex)

# A TRUNCATED red codex rollout: records before the cut are retained.
os.makedirs(codex_dir, exist_ok=True)
with open(f"{codex_dir}/rollout-2026-07-28T12-20-00-T-trunc.jsonl", "w") as fh:
    for rec in codex[:6]:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    fh.write('{"timestamp": "2026-07-28T12:20:99Z", "type": "event_ms')

# Both roles now name a controller log, so the report exercises ingestion.
wj(f"{c3}/identities.json", {
    "builder": {"tool": "claude", "model": "claude-opus-5",
                "session_id": "S-c3"},
    "build-reviewer": {"tool": "codex", "model": "gpt-5.6-sol",
                       "session_id": "T-c3"},
})
print("c3 codex ok")
