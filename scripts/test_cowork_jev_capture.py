#!/usr/bin/env python3
"""Offline behavior checks for the pre-review Jev capture library.

Every case builds a throwaway git repository in a temporary directory and uses
neutral, invented content; fake secrets are assembled at runtime. Run through
the offline harness with this module's name as the explicit id.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cowork_jev_capture as cap  # noqa: E402

SALT = "jev-obs-q1/cohort-1"
OBJECTIVE = "Add error handling to export_report."
REQUIREMENT = ("The export_report function must exit non-zero when the "
               "output directory is not writable.")

BASE_APP = "def export_report(path):\n    return 1\n"
NEW_APP = ("def export_report(path):\n    try:\n        write(path)\n"
           "    except OSError:\n        pass\n")


def fake_entropy_token(seed="x"):
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=tester", "-c",
         "user.email=tester@example.invalid", "-c", "commit.gpgsign=false"]
        + list(args), cwd=cwd, check=True, capture_output=True)


def make_repo(root, files):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    for rel, content in files.items():
        write(root, rel, content)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return git(root, "rev-parse", "HEAD").stdout.decode().strip()


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w" if isinstance(content, str) else "wb") as fh:
        fh.write(content)


class Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return "2026-10-01T00:00:%02dZ" % self.n


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="jevcap-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "wt-one")
        self.store = os.path.join(self.tmp, "store")
        self.registry = os.path.join(self.tmp, "registry")
        self.clock = Clock()

    def base_repo(self, extra=None):
        files = {"app.py": BASE_APP, "README.md": "docs\n"}
        files.update(extra or {})
        self.base = make_repo(self.repo, files)
        return self.base

    def capture(self, **kw):
        args = dict(repo=self.repo, objective_text=OBJECTIVE,
                    requirement_text=REQUIREMENT, ticket_ref="Demo#1",
                    session_ids=["sess-a"], store_dir=self.store,
                    base_ref=getattr(self, "base", None), salt=SALT,
                    cohort_id="cohort-1", clock=self.clock)
        args.update(kw)
        return cap.capture_candidate(**args)


class PreservationTests(TmpCase):
    def test_content_unchanged_after_later_edits(self):
        self.base_repo({"gone.py": "x = 1\n"})
        write(self.repo, "app.py", NEW_APP)
        write(self.repo, "new_file.py", "def added():\n    return 2\n")
        os.remove(os.path.join(self.repo, "gone.py"))
        req_file = os.path.join(self.tmp, "ticket.txt")
        write(self.tmp, "ticket.txt", REQUIREMENT)
        with open(req_file) as fh:
            requirement = fh.read()
        result = self.capture(requirement_text=requirement)
        self.assertTrue(result["ok"], result)
        cid = result["capture_id"]
        before = json.loads(json.dumps(result["record"]))
        statuses = {f["rel_path"]: f["status"] for f in before["files"]}
        self.assertEqual(statuses["app.py"], "modified")
        self.assertEqual(statuses["new_file.py"], "added")
        self.assertEqual(statuses["gone.py"], "deleted")

        write(self.repo, "app.py", "def export_report(path):\n    raise X\n")
        write(self.repo, "new_file.py", "changed\n")
        write(self.repo, "later.py", "later\n")
        write(self.tmp, "ticket.txt", "A different requirement.")

        self.assertEqual(cap.load_capture(self.store, cid), before)
        self.assertEqual(cap.read_capture_file(self.store, cid, "app.py"),
                         NEW_APP.encode())
        self.assertEqual(cap.read_capture_file(self.store, cid,
                                               "new_file.py"),
                         b"def added():\n    return 2\n")
        self.assertIsNone(cap.read_capture_file(self.store, cid, "later.py"))
        self.assertEqual(cap.verify_capture(self.store, cid)["consistent"],
                         True)
        self.assertIn("+    except OSError:", before["diff"])
        self.assertEqual(before["requirement_text"], REQUIREMENT)

    def test_blobs_are_read_only_and_outside_worktree(self):
        self.base_repo()
        result = self.capture()
        blob = os.path.join(self.store, result["capture_id"], "files",
                            hashlib.sha256(BASE_APP.encode()).hexdigest())
        self.assertTrue(os.path.exists(blob))
        self.assertFalse(os.access(blob, os.W_OK) and os.geteuid() != 0)
        self.assertFalse(self.store.startswith(self.repo))


class FailureClassificationTests(TmpCase):
    def test_between_passes_mutation_is_capture_race(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        result = self.capture(
            between_passes=lambda: write(self.repo, "app.py", "mutated\n"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["exclusion_reason"], "capture_race")
        self.assertFalse(os.path.exists(self.store) and os.listdir(self.store))
        line = cap.record_acceptance(self.registry, result, clock=self.clock)
        self.assertEqual(line["kind"], "excluded")
        self.assertNotIn("acceptance_seq", line)

    def test_removed_between_passes_is_capture_race(self):
        self.base_repo()
        result = self.capture(between_passes=lambda: os.remove(
            os.path.join(self.repo, "app.py")))
        self.assertEqual(result["exclusion_reason"], "capture_race")

    def test_errors_are_capture_error_not_race(self):
        non_git = os.path.join(self.tmp, "plain")
        os.makedirs(non_git)
        res = self.capture(repo=non_git, base_ref=None)
        self.assertEqual(res["exclusion_reason"], "capture_error")
        self.assertEqual(res["error"]["reason"], "git_ls_files_failed")

        self.base_repo()
        if os.geteuid() != 0:
            write(self.repo, "locked.py", "x = 1\n")
            os.chmod(os.path.join(self.repo, "locked.py"), 0)
            res = self.capture()
            self.assertEqual(res["exclusion_reason"], "capture_error")
            self.assertEqual(res["error"]["reason"],
                             "unsupported_or_unreadable_entry")
            os.chmod(os.path.join(self.repo, "locked.py"), 0o644)
            os.remove(os.path.join(self.repo, "locked.py"))

        os.mkfifo(os.path.join(self.repo, "pipe"))
        res = self.capture()
        if res["exclusion_reason"] == "capture_error":
            self.assertEqual(res["error"]["reason"],
                             "unsupported_or_unreadable_entry")
        os.remove(os.path.join(self.repo, "pipe"))

        self.assertEqual(self.capture(salt=None)["error"]["reason"],
                         "salt_required")

    def test_path_escape_is_capture_error(self):
        make_repo(self.repo, {"sub/f.txt": "inside\n"})
        outside = os.path.join(self.tmp, "outside")
        write(outside, "f.txt", "outside\n")
        shutil.rmtree(os.path.join(self.repo, "sub"))
        os.symlink(outside, os.path.join(self.repo, "sub"))
        res = self.capture(base_ref=None)
        self.assertEqual(res["exclusion_reason"], "capture_error")
        self.assertEqual(res["error"]["reason"], "path_escapes_repo")

    def _registered(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        res = self.capture()
        self.assertTrue(res["ok"])
        cap.record_acceptance(self.registry, res, clock=self.clock)
        return res["capture_id"]

    def _edit_record(self, cid, **changes):
        path = os.path.join(self.store, cid, "record.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec.update(changes)
        return path, rec

    def _dump(self, path, rec):
        with open(path, "w") as fh:
            json.dump(rec, fh)

    def test_tamper_blob_detected(self):
        cid = self._registered()
        blob = os.path.join(self.store, cid, "files",
                            hashlib.sha256(NEW_APP.encode()).hexdigest())
        os.chmod(blob, 0o644)
        write(self.store, os.path.relpath(blob, self.store), "tampered")
        out = cap.verify_capture(self.store, cid, self.registry)
        self.assertFalse(out["consistent"])
        self.assertTrue(out["excluded"])
        self.assertTrue(any(p.startswith("blob_mismatch")
                            for p in out["problems"]))

    def test_tamper_candidate_and_sessions_detected(self):
        for change in ({"candidate_id": "other#9@1"},
                       {"session_ids": ["someone-else"]}):
            self.tearDown_dirs()
            cid = self._registered()
            path, rec = self._edit_record(cid, **change)
            self._dump(path, rec)
            out = cap.verify_capture(self.store, cid)
            self.assertFalse(out["consistent"])
            self.assertIn("record_digest_mismatch", out["problems"])

    def tearDown_dirs(self):
        for d in (self.repo, self.store, self.registry):
            shutil.rmtree(d, ignore_errors=True)

    def test_tamper_with_recomputed_digest_detected_via_registry(self):
        cid = self._registered()
        path, rec = self._edit_record(cid, candidate_id="other#9@1")
        rec["record_digest"] = cap.record_digest(rec)
        self._dump(path, rec)
        self.assertTrue(cap.verify_capture(self.store, cid)["consistent"])
        out = cap.verify_capture(self.store, cid, self.registry)
        self.assertFalse(out["consistent"])
        self.assertIn("registry_mismatch:candidate_id", out["problems"])
        self.assertIn("registry_mismatch:record_digest", out["problems"])

    def test_excluded_marker_keeps_record_unmodified(self):
        cid = self._registered()
        before = cap.load_capture(self.store, cid)
        self.assertTrue(cap.mark_excluded(self.store, cid, "capture_race",
                                          clock=self.clock))
        self.assertEqual(cap.load_capture(self.store, cid), before)
        out = cap.verify_capture(self.store, cid)
        self.assertTrue(out["excluded"])
        self.assertTrue(out["consistent"])
        self.assertFalse(cap.mark_excluded(self.store, "../etc", "x"))

    def test_missing_capture_is_reported_not_raised(self):
        out = cap.verify_capture(self.store, "0" * 32)
        self.assertEqual((out["consistent"], out["excluded"]), (False, True))
        self.assertIsNone(cap.load_capture(self.store, "bad id"))


class RedactionAndBaseTests(TmpCase):
    def planted(self):
        return {
            "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
            "gh": "ghp_" + "a1B2c3D4e5F6g7H8i9J0",
            "sk": "sk-" + "abcdEFGH1234ijklMNOP",
            "bearer": "Bearer " + "abcdef1234567890xyz",
            "entropy": fake_entropy_token("planted"),
            "email": "someone" + "@example.invalid",
            "home": "/Users/" + "jdoe/projects/app",
        }

    def test_credential_redaction_and_ordinary_text_passthrough(self):
        p = self.planted()
        for rule, value in (("aws_access_key", p["aws"]),
                            ("github_token", p["gh"]),
                            ("sk_key", p["sk"]), ("bearer_token", p["bearer"]),
                            ):
            red, counts = cap.redact_text("x = '%s'" % value)
            self.assertEqual(counts, {rule: 1}, (rule, red))
            self.assertIn("<redacted:%s>" % rule, red)
            self.assertNotIn(value, red)
        key = ("-----BEGIN RSA PRIVATE KEY-----\nabc\n"
               "-----END RSA PRIVATE KEY-----")
        self.assertEqual(cap.redact_text(key)[1], {"private_key": 1})
        for ordinary in (p["email"], p["home"], p["entropy"],
                         "ghx_" + "A" * 20, "a" * 40):
            self.assertEqual(cap.redact_text(ordinary), (ordinary, {}))

    def test_forbidden_path(self):
        for rel in (".env", ".env.local", "a/b.pem", "a/b.key",
                    "identity.p12", "credentials.json", ".cowork/s.json",
                    "x/.cowork/y"):
            self.assertTrue(cap.forbidden_path(rel), rel)
        for rel in ("app.py", "src/util.py", "docs/readme.md",
                    "keys/x.py", "monkey.py", "my_secret.txt",
                    "secrets.md"):
            self.assertFalse(cap.forbidden_path(rel), rel)

    def test_out_of_scope_material_never_in_provider_facing_data(self):
        p = self.planted()
        self.base_repo({".gitignore": "ignored.txt\n"})
        write(self.repo, "app.py", NEW_APP.replace(
            "write(path)", "write(path, '%s')" % p["gh"]))
        write(self.repo, ".env", "TOKEN=%s\n" % p["aws"])
        write(self.repo, "server.pem", "pem %s\n" % p["sk"])
        write(self.repo, "ignored.txt", "ignored %s\n" % p["bearer"])
        write(self.repo, ".cowork/state.json", "{\"t\": \"%s\"}" %
              p["entropy"])
        write(self.repo, "notes.py", "# %s %s %s\nVALUE = 1\n" % (
            p["email"], p["home"], p["entropy"]))
        res = self.capture()
        self.assertTrue(res["ok"], res)
        rec = res["record"]
        view = cap.capture_content_view(rec)
        provider = json.dumps(view) + "".join(
            u["state_text"] for u in rec["units"]) + rec["diff"]
        for value in (p["aws"], p["gh"], p["sk"], p["bearer"]):
            self.assertNotIn(value, provider)
        for value in (p["email"], p["home"], p["entropy"]):
            self.assertIn(value, provider)
        self.assertIn("<redacted:github_token>", rec["diff"])
        by_path = {f["rel_path"]: f for f in rec["files"]}
        for rel in (".env", "server.pem"):
            self.assertTrue(by_path[rel]["redacted"])
            self.assertEqual(by_path[rel]["rule_id"], "forbidden_path")
        self.assertNotIn("ignored.txt", by_path)
        self.assertFalse([f for f in rec["files"] if ".cowork" in
                          f["rel_path"]])
        self.assertFalse(by_path["notes.py"]["redacted"])
        rules = {(r["rel_path"], r["rule_id"]) for r in rec["redactions"]}
        self.assertNotIn(("notes.py", "email"), rules)
        self.assertNotIn(("notes.py", "home_path"), rules)
        # No blob or diff for forbidden files; local metadata keeps sha256.
        self.assertIsNone(cap.read_capture_file(self.store, res["capture_id"],
                                                ".env"))
        self.assertTrue(by_path[".env"]["sha256"])
        self.assertNotIn(".env", rec["diff"])
        blobs = os.listdir(os.path.join(self.store, res["capture_id"],
                                        "files"))
        self.assertNotIn(by_path[".env"]["sha256"], blobs)
        # Provider-facing view: no name or hash for forbidden files.
        labeled = [f for f in view["files"] if "rel_label" in f]
        self.assertEqual(len(labeled), 2)
        for f in labeled:
            self.assertNotIn("sha256", f)
            self.assertNotIn("rel_path", f)
        self.assertNotIn(by_path[".env"]["sha256"], json.dumps(view))
        self.assertNotIn("server.pem", json.dumps(view))
        # Fingerprint still reflects forbidden content.
        write(self.repo, ".env", "TOKEN=changed\n")
        again = self.capture()
        self.assertNotEqual(again["record"]["raw_content_fingerprint"],
                            rec["raw_content_fingerprint"])

    def test_secret_in_function_body_is_cleanly_redacted(self):
        p = self.planted()
        self.base_repo()
        write(self.repo, "app.py",
              "def export_report(path):\n    token = '%s'\n    try:\n"
              "        write(path)\n    except OSError:\n        pass\n"
              % p["gh"])
        res = self.capture()
        units = [u for u in res["record"]["units"]
                 if u["kind"] == "error_behavior"]
        self.assertEqual(len(units), 1)
        self.assertIsNone(units[0]["omission"])
        self.assertIn("redacted:github_token", units[0]["state_text"])
        self.assertNotIn(p["gh"], units[0]["state_text"])

    def test_symbol_names_and_random_identifiers_do_not_withhold_units(self):
        name = "secret_export_key_aZ3kQ9xT2mW7vB5nR8cL1dF6gH4jY0pEqW"
        self.assertFalse(cap.redact_text(name)[1])
        self.base_repo()
        write(self.repo, "app.py", NEW_APP + (
            "\ndef %s():\n    raise ValueError('x')\n"
            "\ndef ghp_abcdefghijklmnopqrst():\n"
            "    raise ValueError('y')\n" % name))
        rec = self.capture()["record"]
        errors = [u for u in rec["units"] if u["kind"] == "error_behavior"]
        self.assertTrue(any(name in u["state_text"] for u in errors))
        self.assertIn(name, rec["diff"])
        serialized = json.dumps(rec)
        self.assertNotIn("ghp_abcdefghijklmnopqrst", serialized)
        self.assertNotIn("data_withheld", serialized)

    def test_credential_in_requirement_is_scrubbed_without_exclusion(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        res = self.capture(requirement_text=REQUIREMENT + " Mail " +
                           self.planted()["aws"] + " when done.")
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["exclusion_reason"])
        self.assertTrue(res["record"]["units"])
        self.assertNotIn(self.planted()["aws"], json.dumps(res["record"]))
        rules = {(r["rel_path"], r["rule_id"])
                 for r in res["record"]["redactions"]}
        self.assertIn(("<requirement>", "aws_access_key"), rules)

    def test_ordinary_objective_content_is_preserved_and_eligible(self):
        self.base_repo()
        write(self.repo, "monkey.py", NEW_APP)
        objective = ("Implement export handling in /Users/example/work/src, "
                     "coordinate with owner@example.invalid, and preserve "
                     "build " + self.planted()["entropy"] + ".")
        requirement = ("The exporter must write to /Users/example/work/src "
                       "for owner@example.invalid using "
                       + self.planted()["entropy"] + ".")
        res = self.capture(objective_text=objective,
                           requirement_text=requirement)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["record"]["objective_text"], objective)
        self.assertEqual(res["record"]["requirement_text"], requirement)
        self.assertTrue(res["record"]["units"])
        self.assertTrue(any(u["state_text"] for u in res["record"]["units"]
                            if u["applicable"]))

    def test_missing_objective_and_requirement(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        res = self.capture(objective_text="   ")
        self.assertEqual(res["exclusion_reason"], "no_objective_captured")
        self.assertFalse(res["ok"])
        res = self.capture(requirement_text="")
        self.assertTrue(res["ok"])
        self.assertEqual(res["record"]["insufficient_evidence"],
                         ["requirement_text_missing"])

    def test_context_too_large(self):
        self.base_repo()
        body = "".join("    value_%d = %d\n" % (i, i) for i in range(9000))
        write(self.repo, "app.py",
              "def export_report(path):\n" + body +
              "    try:\n        write(path)\n    except OSError:\n"
              "        pass\n")
        rec = self.capture()["record"]
        errors = [u for u in rec["units"] if u["kind"] == "error_behavior"]
        self.assertEqual(errors[0]["omission"], "context_too_large")
        self.assertEqual(errors[0]["state_text"], "")

    def test_state_budget_drops_context_groups(self):
        self.assertLessEqual(cap.estimate_tokens("abc"), 1)
        small = [{"symbol": "s", "file": "F1", "text": "x"}]
        big = [{"symbol": "b", "file": "F2", "text": "y" * 90000}]
        text, tokens = cap._fit_state("o", "r", "requirement", "st", small,
                                      [[], [], big])
        self.assertIsNotNone(text)
        self.assertNotIn("yyyy", text)
        self.assertLessEqual(tokens, cap.STATE_BUDGET_TOKENS)

    def test_base_capture(self):
        base = self.base_repo({".env": "TOKEN=" + self.planted()["aws"] + "\n"})
        write(self.repo, "app.py", NEW_APP)
        res = self.capture()
        rec = res["record"]
        files = cap.read_base_files(self.repo, base)
        view_parts = cap._base_view(files)
        digest, raw_fp = view_parts[1], view_parts[2]
        self.assertEqual(rec["base_digest"], digest)
        self.assertEqual(rec["base_raw_fingerprint"], raw_fp)
        self.assertIsNone(rec["base_omitted"])
        view = json.dumps(cap.capture_content_view(rec))
        self.assertIn(digest, view)
        self.assertNotIn(raw_fp, view)
        self.assertNotIn(rec["raw_content_fingerprint"], view)
        self.assertNotIn(self.planted()["aws"], json.dumps(rec))
        self.assertTrue(any(r["rule_id"] == "forbidden_path"
                            for r in rec["base_redactions"]))
        # A base edit changes the digest; a missing base records omission.
        none = self.capture(base_ref=None)["record"]
        self.assertIsNone(none["base_digest"])
        self.assertIsNone(none["base_raw_fingerprint"])
        self.assertEqual(none["base_omitted"], {"reason": "no_base_supplied"})
        bad = self.capture(base_ref="no-such-ref")["record"]
        self.assertIsNone(bad["base_digest"])
        self.assertEqual(bad["base_omitted"]["reason"], "base_unreadable")

    def test_base_dir_supported(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        base_dir = os.path.join(self.tmp, "basedir")
        write(base_dir, "app.py", BASE_APP)
        rec = self.capture(base_ref=None, base_dir=base_dir)["record"]
        self.assertTrue(rec["base_digest"])
        self.assertIn("-    return 1", rec["diff"])

    def _base_dir_capture(self, **kw):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        self.base_dir = os.path.join(self.tmp, "basedir")
        write(self.base_dir, "app.py", BASE_APP)
        write(self.base_dir, "lib/util.py", "def helper():\n    return 7\n")
        write(self.base_dir, ".env", "TOKEN=" + self.planted()["aws"] + "\n")
        write(self.base_dir, "blob.bin", b"\x00\x01\x02")
        write(self.base_dir, "notes.py",
              "# mail %s\nX = 1\n" % self.planted()["email"])
        res = self.capture(base_ref=None, base_dir=self.base_dir, **kw)
        return res

    def test_base_view_survives_base_edit_and_delete(self):
        res = self._base_dir_capture()
        self.assertTrue(res["ok"], res)
        cid, rec = res["capture_id"], res["record"]
        before = json.loads(json.dumps(rec))
        shutil.rmtree(self.base_dir)
        self.assertEqual(cap.load_capture(self.store, cid), before)
        self.assertEqual(
            cap.read_capture_base_file(self.store, cid, "app.py"), BASE_APP)
        self.assertEqual(
            cap.read_capture_base_file(self.store, cid, "lib/util.py"),
            "def helper():\n    return 7\n")
        self.assertTrue(cap.verify_capture(self.store, cid)["consistent"])
        self.assertEqual(cap.base_digest_for(rec["base_files"]),
                         rec["base_digest"])

    def test_base_view_withholds_credential_containers_not_ordinary_text(self):
        res = self._base_dir_capture()
        rec, cid = res["record"], res["capture_id"]
        planted = self.planted()
        view = cap.capture_content_view(rec)
        text = json.dumps(view)
        for value in (planted["aws"], rec["base_raw_fingerprint"],
                      rec["raw_content_fingerprint"]):
            self.assertNotIn(value, text)
        self.assertIn(planted["email"], text)
        self.assertNotIn(".env", text)
        self.assertIsNone(cap.read_capture_base_file(self.store, cid, ".env"))
        self.assertIsNone(cap.read_capture_base_file(self.store, cid,
                                                     "blob.bin"))
        reasons = {e["rel_path"]: e.get("rule_id")
                   for e in rec["base_files"]}
        self.assertEqual(reasons[".env"], "forbidden_path")
        self.assertEqual(reasons["blob.bin"], "non_text")
        self.assertIn(planted["email"], cap.read_capture_base_file(
            self.store, cid, "notes.py"))
        # Withheld entries stay listed in the adjudicator view with a reason.
        withheld = [e for e in view["base_files"] if e.get("withheld_reason")]
        self.assertEqual(len(withheld), 2)
        for blob_dir in ("base", "files"):
            for name in os.listdir(os.path.join(self.store, cid, blob_dir)):
                with open(os.path.join(self.store, cid, blob_dir, name),
                          "rb") as fh:
                    if blob_dir == "base":
                        self.assertNotIn(planted["aws"].encode(), fh.read())

    def test_base_view_tamper_detected(self):
        res = self._base_dir_capture()
        cid, rec = res["capture_id"], res["record"]
        sha = [e["sha256"] for e in rec["base_files"]
               if e["rel_path"] == "app.py"][0]
        blob = os.path.join(self.store, cid, "base", sha)
        os.chmod(blob, 0o644)
        write(self.store, os.path.relpath(blob, self.store), "tampered")
        out = cap.verify_capture(self.store, cid)
        self.assertFalse(out["consistent"])
        self.assertIn("base_blob_mismatch:app.py", out["problems"])

    def test_base_entries_tamper_detected(self):
        res = self._base_dir_capture()
        cid = res["capture_id"]
        path = os.path.join(self.store, cid, "record.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["base_files"] = [e for e in rec["base_files"]
                             if e["rel_path"] != "lib/util.py"]
        rec["record_digest"] = cap.record_digest(rec)
        with open(path, "w") as fh:
            json.dump(rec, fh)
        out = cap.verify_capture(self.store, cid)
        self.assertIn("base_digest_mismatch", out["problems"])

    def test_base_changed_during_capture_fails_closed(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        base_dir = os.path.join(self.tmp, "basedir")
        write(base_dir, "app.py", BASE_APP)
        res = self.capture(
            base_ref=None, base_dir=base_dir,
            between_passes=lambda: write(base_dir, "app.py", "changed\n"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["exclusion_reason"], "capture_race")
        self.assertEqual(res["error"]["reason"], "base_changed_during_capture")
        self.assertFalse(os.path.exists(self.store) and os.listdir(self.store))
        # Deleting the base mid-capture also fails closed.
        res = self.capture(base_ref=None, base_dir=base_dir,
                           between_passes=lambda: shutil.rmtree(base_dir))
        self.assertEqual(res["error"]["reason"], "base_changed_during_capture")

    def test_unreadable_or_missing_base_is_explicit(self):
        self.base_repo()
        res = self.capture(base_ref=None,
                           base_dir=os.path.join(self.tmp, "nope"))
        rec = res["record"]
        # A missing directory is an explicit omission, not an empty base.
        self.assertIsNone(rec["base_digest"])
        self.assertEqual(rec["base_omitted"]["reason"], "base_unreadable")
        res = self.capture(base_ref="no-such-ref")
        rec = res["record"]
        self.assertEqual(rec["base_files"], [])
        self.assertEqual(rec["base_omitted"]["reason"], "base_unreadable")
        view = cap.capture_content_view(rec)
        self.assertEqual(view["base_omitted"]["reason"], "base_unreadable")
        self.assertIsNone(view["base_digest"])


class ProtocolIdentityTests(TmpCase):
    def test_n1(self):
        n1 = cap.normalize_n1
        self.assertEqual(n1("  Hello\n\tWORLD  "), "hello world")
        self.assertEqual(n1("See src/app.py and notes.md"),
                         "see <path> and <path>")
        self.assertEqual(n1("Fix #123 at 2026-10-01T00:00:00Z now"),
                         "fix <id> at <id> now")
        self.assertEqual(n1("id 3f2a9c1 and "
                            "123e4567-e89b-12d3-a456-426614174000"),
                         "id <id> and <id>")
        self.assertEqual(n1("**must** call `export_report`"),
                         "must call export_report")
        self.assertEqual(n1("_leading and trailing_ my_var"),
                         "leading and trailing my_var")
        self.assertEqual(n1("- item one\n- item two"), "item one item two")
        self.assertEqual(n1("\uff21\uff22"), "ab")

    def test_unit_keys_are_stable(self):
        a = cap.unit_key("requirement", "The tool **must** log /a/b.py #12.")
        b = cap.unit_key("requirement", "the tool must log  /c/d.py #99.")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("requirement:"))
        e1 = cap.unit_key("error_behavior", symbol="f", exit_kind="raise",
                          error_class="ValueError")
        self.assertEqual(e1, "error_behavior:" + hashlib.sha256(
            b"f|raise|ValueError").hexdigest())
        self.assertEqual(cap.unit_key("error_behavior", symbol="f",
                                      exit_kind="raise"),
                         cap.unit_key("error_behavior", symbol="f",
                                      exit_kind="raise",
                                      error_class="generic"))
        self.assertEqual(cap.unit_id("t#1@1", e1), "t#1@1/" + e1)
        self.assertEqual(cap.unit_statement("error_behavior", symbol="S",
                                            exit_kind="swallow",
                                            error_class="generic"),
                         "Function S catches an error and does nothing "
                         "with it.")
        self.assertEqual(cap.unit_statement("error_behavior", symbol="S",
                                            exit_kind="raise",
                                            error_class="E"),
                         "Function S raises E.")

    def test_requirement_unit_extraction(self):
        units = cap.extract_units(
            "Improve the exporter.",
            "- The tool must log failures.\n- Nice to have colors.\n"
            "It should also retry. The tool must log failures.")
        self.assertEqual([u["statement"] for u in units],
                         ["The tool must log failures.",
                          "It should also retry."])

    def test_error_behavior_extraction(self):
        self.base_repo()
        code = (
            "import sys\n"
            "def a():\n    raise ValueError('x')\n"
            "def b():\n    try:\n        run()\n    except KeyError:\n"
            "        pass\n"
            "def c():\n    try:\n        run()\n    except OSError:\n"
            "        logger.warning('x')\n"
            "def d():\n    try:\n        run()\n    except OSError:\n"
            "        return None\n"
            "def e():\n    sys.exit(1)\n"
            "class K:\n    def m(self):\n        raise RuntimeError()\n")
        write(self.repo, "app.py", code)
        units = self.capture()["record"]["units"]
        statements = sorted(u["statement"] for u in units
                            if u["kind"] == "error_behavior")
        self.assertEqual(statements, sorted([
            "Function a raises ValueError.",
            "Function b catches KeyError and does nothing with it.",
            "Function c logs OSError and continues.",
            "Function d returns an error value for OSError.",
            "Function e ends the process with a nonzero status on an error.",
            "Function K.m raises RuntimeError."]))

    def test_non_python_textual_extraction(self):
        self.base_repo()
        write(self.repo, "tool.js", (
            "function load() {\n  throw new Error('x');\n}\n"
            "function save() {\n  try { go(); } catch (e) {\n  }\n}\n"))
        units = self.capture()["record"]["units"]
        statements = {u["statement"] for u in units
                      if u["kind"] == "error_behavior"}
        self.assertIn("Function load raises Error.", statements)

    def test_unit_rank_and_order(self):
        a = cap.unit_rank(SALT, "t#1@1", "k")
        self.assertEqual(a, cap.unit_rank(SALT, "t#1@1", "k"))
        self.assertNotEqual(a, cap.unit_rank(SALT, "t#2@1", "k"))
        self.assertNotEqual(a, cap.unit_rank(SALT + "x", "t#1@1", "k"))
        self.assertLess(a, 2 ** 64)
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        units = self.capture()["record"]["units"]
        applicable = [u for u in units if u["applicable"]]
        self.assertTrue(all(len(u["rank"]) == 16 for u in applicable))
        for kind in ("requirement", "error_behavior"):
            ranks = [u["rank"] for u in applicable if u["kind"] == kind]
            self.assertEqual(ranks, sorted(ranks))
        req = [u for u in units if u["kind"] == "requirement"][0]
        self.assertEqual(req["symbols"], ["export_report"])
        self.assertTrue(req["state_text"])

    def test_requirement_only_doc_change_is_not_applicable(self):
        self.base_repo()
        write(self.repo, "README.md", "changed docs\n")
        units = self.capture()["record"]["units"]
        req = [u for u in units if u["kind"] == "requirement"][0]
        self.assertFalse(req["applicable"])
        self.assertEqual(req["not_applicable_reason"],
                         "no_changed_source_file")
        self.assertIsNone(req["rank"])

    def test_state_text_shape_and_dependency_context(self):
        self.base_repo({"util.py": "def helper():\n    return 1\n"})
        write(self.repo, "app.py",
              "def export_report(path):\n    try:\n        helper()\n"
              "    except OSError:\n        pass\n")
        write(self.repo, "test_app.py",
              "def test_export_report():\n    export_report('x')\n")
        units = self.capture()["record"]["units"]
        unit = [u for u in units if u["kind"] == "error_behavior"][0]
        state = json.loads(unit["state_text"])
        self.assertEqual(sorted(state), ["boundary", "changed_code",
                                         "context", "task", "unit"])
        self.assertEqual(state["unit"]["kind"], "error_behavior")
        self.assertEqual(state["task"]["objective"], OBJECTIVE)
        self.assertEqual([b["symbol"] for b in state["changed_code"]],
                         ["export_report"])
        labels = {b["symbol"]: b["file"] for b in state["context"]}
        self.assertIn("helper", labels)
        self.assertIn("test_export_report", labels)
        self.assertTrue(all(b["file"].startswith("F")
                            for b in state["changed_code"] + state["context"]))
        self.assertNotIn("app.py", unit["state_text"])

    def test_candidate_identity_and_winners(self):
        self.assertEqual(cap.candidate_id_for("  Demo#1 "), "demo#1@1")
        entries = [
            {"ticket_key": "demo#1", "accepted_at": "2026-10-01T00:00:05Z",
             "acceptance_seq": 3},
            {"ticket_key": "demo#1", "accepted_at": "2026-10-01T00:00:05Z",
             "acceptance_seq": 2},
            {"ticket_key": "demo#1", "accepted_at": "2026-10-01T00:00:09Z",
             "acceptance_seq": 4},
            {"ticket_key": "demo#2", "accepted_at": "2026-09-30T00:00:00Z",
             "acceptance_seq": 1},
        ]
        out = cap.pick_candidate_winners(entries, {
            "activated_at": "2026-10-01T00:00:00Z", "close_seq": 3})
        self.assertEqual(out["winners"]["demo#1"]["acceptance_seq"], 2)
        self.assertEqual(out["states"], {
            3: "duplicate_ticket", 2: "winner", 4: "outside_cohort_window",
            1: "outside_cohort_window"})
        self.assertNotIn("demo#2", out["winners"])
        no_window = cap.pick_candidate_winners(entries)
        self.assertEqual(no_window["winners"]["demo#2"]["acceptance_seq"], 1)

    def test_inclusion_list(self):
        self.assertEqual(cap.check_inclusion({"s1": " Demo#1 "}, "s1"),
                         {"included": True, "ticket_ref": "demo#1",
                          "exclusion_reason": None})
        listed = [{"session_id": "s2", "ticket_ref": "x#2"}]
        self.assertTrue(cap.check_inclusion(listed, "s2")["included"])
        out = cap.check_inclusion(listed, "s9")
        self.assertEqual(out["exclusion_reason"], "not_in_inclusion_list")
        self.assertFalse(cap.check_inclusion({"s1": ""}, "s1")["included"])

    def _fingerprint_for(self, root_name, session, mutate=None):
        root = os.path.join(self.tmp, root_name)
        make_repo(root, {"app.py": BASE_APP, "run.sh": "echo hi\n"})
        write(root, "app.py", NEW_APP)
        if mutate:
            mutate(root)
        req = REQUIREMENT + " Session %s." % session
        res = self.capture(repo=root, base_ref=None, requirement_text=req,
                           session_ids=[session],
                           store_dir=os.path.join(self.store, root_name))
        self.assertTrue(res["ok"], res)
        return res["record"]["raw_content_fingerprint"]

    def test_fingerprint_independent_of_path_and_session(self):
        a = self._fingerprint_for("worktree-a", "11111111-aaaa")
        b = self._fingerprint_for("elsewhere/worktree-b", "22222222-bbbb")
        self.assertEqual(a, b)

    def test_fingerprint_sees_mode_symlink_delete_and_requirement(self):
        base = self._fingerprint_for("w1", "sess-x")
        mode = self._fingerprint_for(
            "w2", "sess-x",
            lambda r: os.chmod(os.path.join(r, "run.sh"), 0o755))
        link = self._fingerprint_for(
            "w3", "sess-x",
            lambda r: os.symlink("app.py", os.path.join(r, "ln")))
        gone = self._fingerprint_for(
            "w4", "sess-x", lambda r: os.remove(os.path.join(r, "run.sh")))
        self.assertEqual(len({base, mode, link, gone}), 4)
        entries = [["a", "644", "x"]]
        fp = cap.raw_content_fingerprint
        self.assertNotEqual(fp(entries, "t"), fp(entries, "t2"))
        self.assertNotEqual(fp(entries, "t"),
                            fp(entries + [["b", "deleted", None]], "t"))
        self.assertEqual(fp(entries, "root=/x/y s1", ["s1"], "/x/y"),
                         fp(entries, "root=/q s2", ["s2"], "/q"))
        self.assertNotEqual(fp(entries, "root=/x/y"),
                            fp(entries, "root=/q"))
        # Only the exact known id is masked, not look-alikes.
        self.assertNotEqual(fp(entries, "id s1", ["s1"]),
                            fp(entries, "id s2", ["s1"]))

    def test_record_keys_and_content_view_exclusions(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        rec = self.capture()["record"]
        self.assertEqual(rec["schema"], "jev_capture.v1")
        allowed = {"schema", "capture_id", "cohort_id", "ticket_ref",
                   "ticket_key", "candidate_id", "session_ids", "accepted_at",
                   "acceptance_seq", "objective_text", "requirement_text",
                   "raw_content_fingerprint", "base_raw_fingerprint",
                   "base_digest", "files", "redactions", "base_redactions",
                   "base_files", "diff", "units", "exclusion_reason", "captured_at",
                   "insufficient_evidence", "base_omitted", "record_digest"}
        self.assertEqual(set(rec), allowed)
        for unit in rec["units"]:
            for banned in ("status", "stratum", "injection_suspected",
                           "verdict", "transcript", "answers"):
                self.assertNotIn(banned, unit)
        self.assertEqual(rec["candidate_id"], "demo#1@1")
        self.assertEqual(rec["session_ids"], ["sess-a"])
        view = cap.capture_content_view(rec)
        self.assertEqual(sorted(view), [
            "base_digest", "base_files", "base_omitted", "base_redactions",
            "diff", "files", "objective_text", "redactions",
            "requirement_text", "schema"])
        text = json.dumps(view)
        for u in rec["units"]:
            self.assertNotIn(u["rank"] or "no-rank", text)
            if u["state_text"]:
                self.assertNotIn(u["state_text"], text)
        self.assertNotIn(rec["raw_content_fingerprint"], text)
        self.assertNotIn("verdict", json.dumps(rec).lower())
        self.assertNotIn(".cowork", json.dumps(rec))


class OrderEvidenceTests(TmpCase):
    def test_acceptance_seq_after_capture_and_absent_for_excluded(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        first = self.capture()
        second = self.capture(ticket_ref="demo#2")
        raced = self.capture(between_passes=lambda: write(
            self.repo, "app.py", "mutated\n"))
        lines = [cap.record_acceptance(self.registry, r, clock=self.clock)
                 for r in (first, raced, second)]
        self.assertEqual([ln["kind"] for ln in lines],
                         ["accepted", "excluded", "accepted"])
        self.assertEqual([ln.get("acceptance_seq") for ln in lines],
                         [1, None, 2])
        self.assertLess(first["record"]["captured_at"], lines[0]["accepted_at"])
        self.assertEqual(lines[0]["record_digest"],
                         first["record"]["record_digest"])
        self.assertEqual(lines[0]["candidate_id"], "demo#1@1")
        with open(os.path.join(self.registry, "acceptance.jsonl")) as fh:
            written = [json.loads(x) for x in fh]
        self.assertEqual(len(written), 3)

    def test_capture_precedes_review(self):
        record = {"captured_at": "2026-10-01T00:00:05Z"}
        self.assertTrue(cap.capture_precedes_review(
            record, "2026-10-01T00:00:06Z")["precedes"])
        self.assertFalse(cap.capture_precedes_review(
            record, "2026-10-01T00:00:05Z")["precedes"])
        self.assertFalse(cap.capture_precedes_review(
            record, "2026-10-01T00:00:04Z")["precedes"])
        self.assertFalse(cap.capture_precedes_review(
            record, "not a time")["precedes"])
        self.assertFalse(cap.capture_precedes_review(
            {}, "2026-10-01T00:00:06Z")["precedes"])
        self.assertTrue(cap.capture_precedes_review(
            record, "2026-10-01T00:00:09Z",
            "2026-10-01T00:00:07Z")["precedes"])
        self.assertFalse(cap.capture_precedes_review(
            record, "2026-10-01T00:00:06Z",
            "2026-10-01T00:00:07Z")["precedes"])

    def test_capture_time_precedes_later_review_seal(self):
        self.base_repo()
        res = self.capture()
        sealed = "2026-10-01T01:00:00Z"
        self.assertTrue(cap.capture_precedes_review(res["record"],
                                                    sealed)["precedes"])

    def test_comparator_statuses(self):
        self.assertEqual(cap.compare_fingerprints("a", "a"), "verifiable")
        self.assertEqual(cap.compare_fingerprints("a", "b"),
                         "unverifiable_changed")
        self.assertEqual(cap.compare_fingerprints(None, "b"),
                         "unverifiable_unknown")
        self.assertEqual(cap.compare_fingerprints("a", ""),
                         "unverifiable_unknown")

    def test_review_fingerprint_matches_capture(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        res = self.capture()
        rec = res["record"]
        manifest, _raw, absent = cap._pass(self.repo)
        reviewed = cap.raw_content_fingerprint(
            cap.fingerprint_entries(manifest, absent),
            cap.join_ticket_text(OBJECTIVE, REQUIREMENT), ["sess-a"],
            self.repo)
        self.assertEqual(cap.compare_fingerprints(
            reviewed, rec["raw_content_fingerprint"]), "verifiable")
        write(self.repo, "app.py", "changed after capture\n")
        manifest, _raw, absent = cap._pass(self.repo)
        later = cap.raw_content_fingerprint(
            cap.fingerprint_entries(manifest, absent),
            cap.join_ticket_text(OBJECTIVE, REQUIREMENT), ["sess-a"],
            self.repo)
        self.assertEqual(cap.compare_fingerprints(
            later, rec["raw_content_fingerprint"]), "unverifiable_changed")

    def test_compute_raw_fingerprint_matches_and_detects_edit(self):
        self.base_repo()
        write(self.repo, "app.py", NEW_APP)
        res = self.capture()
        recorded = res["record"]["raw_content_fingerprint"]
        other_root = os.path.join(self.tmp, "elsewhere")
        same = cap.compute_raw_fingerprint(
            self.repo, OBJECTIVE, REQUIREMENT, ["sess-a"],
            worktree_root=other_root)
        self.assertEqual(same, recorded)
        write(self.repo, "app.py", NEW_APP + "#\n")
        self.assertNotEqual(cap.compute_raw_fingerprint(
            self.repo, OBJECTIVE, REQUIREMENT, ["sess-a"]), recorded)

    def test_compute_raw_fingerprint_none_when_listing_fails(self):
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain)
        self.assertIsNone(cap.compute_raw_fingerprint(
            plain, OBJECTIVE, REQUIREMENT, ["sess-a"]))


if __name__ == "__main__":
    unittest.main()
