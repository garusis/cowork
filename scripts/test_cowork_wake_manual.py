#!/usr/bin/env python3
"""Focused tests for M3 Package F's manual/emergency verification adapter
(`cowork_wake_manual.py`), built against Package B's own signed-record
verification and journal accessors (`cowork_state.py`).

Signature tests use a fixture Ed25519 key pair (this file reaches into
`cowork_state`'s own self-contained self-test signer, mirroring
test_cowork_capacity_scheduler.py's/test_cowork_state_m3.py's established
`_signed_manual_signal` convention). Production code
(`cowork_wake_manual.py`) contains no such signing path -- see
`NoSelfSignPathTest`, an AST-level structural gate.

Run standalone:

    python3 -m unittest scripts/test_cowork_wake_manual.py -v
"""

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cowork_state as state_store  # noqa: E402
import cowork_wake_manual as wake_manual  # noqa: E402


def _uuid():
    return str(uuid.uuid4())


class _WakeManualEnvMixin:
    """Isolated COWORK_SESSIONS_ROOT per test (mirrors test_cowork_state_
    m3.py's _M3EnvMixin), so nothing ever touches the real home dir."""

    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self._old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = self._root
        self.addCleanup(self._restore_root)
        self._files_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._files_dir, ignore_errors=True))

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("COWORK_SESSIONS_ROOT", None)
        else:
            os.environ["COWORK_SESSIONS_ROOT"] = self._old_root

    def _write_json(self, name, obj):
        path = os.path.join(self._files_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path


# --------------------------------------------------------------------------- #
# Fixture builder (mirrors test_cowork_capacity_scheduler.py's/test_cowork_ #
# state_m3.py's _signed_manual_signal convention exactly).                   #
# --------------------------------------------------------------------------- #


def _signed_manual_signal(secret_key=None, key_id="key-1", **overrides):
    """Build a manual-capacity-signal record with a GENUINE Ed25519
    signature, reaching into `cowork_state`'s own self-contained self-test
    signer -- production code (`cowork_wake_manual.py`) never does this;
    only this TEST file does, standing in for the external, out-of-repo,
    human/hardware-backed signer. Returns (record, pinned_public_keys)."""
    secret_key = secret_key or hashlib.sha256(os.urandom(32)).digest()
    public_key = state_store._ed25519_selftest_publickey(secret_key)
    record = dict(schema_version=1, package_id="pkg-1",
                 candidate_digest="b" * 64, role="builder",
                 provider_session_id="sess-1",
                 controller_policy_digest="a" * 64,
                 signal_journal_ref="journal-" + _uuid(),
                 signer_public_key_id=key_id,
                 detached_signature="00" * 64,
                 issued_at="2024-01-01T00:00:00Z")
    record.update(overrides)
    message = state_store.canonical_manual_capacity_signal_message(record)
    signature = state_store._ed25519_selftest_sign(message, secret_key, public_key)
    record["detached_signature"] = signature.hex()
    pinned = {key_id: public_key.hex()}
    return record, pinned


# --------------------------------------------------------------------------- #
# verify_and_record_manual_signal: the core two-step delegation to Package B.#
# --------------------------------------------------------------------------- #


class VerifyAndRecordTest(_WakeManualEnvMixin, unittest.TestCase):
    def test_genuine_signature_verifies_and_journals(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        stored = wake_manual.verify_and_record_manual_signal(
            session_id, record, pinned)
        self.assertEqual(stored["signal_journal_ref"], record["signal_journal_ref"])
        read_back = state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"])
        self.assertEqual(read_back, stored)

    def test_unsigned_record_fails_and_journals_nothing(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record["detached_signature"] = "00" * 64  # never actually signed
        with self.assertRaises(state_store.ManualSignalSignatureError):
            wake_manual.verify_and_record_manual_signal(session_id, record, pinned)
        self.assertIsNone(state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"]))

    def test_malformed_record_fails_and_journals_nothing(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        del record["role"]
        with self.assertRaises(ValueError):
            wake_manual.verify_and_record_manual_signal(session_id, record, pinned)

    def test_wrong_key_fails_and_journals_nothing(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        _, other_pinned = _signed_manual_signal(key_id="key-1")
        with self.assertRaises(state_store.ManualSignalSignatureError):
            wake_manual.verify_and_record_manual_signal(
                session_id, record, other_pinned)
        self.assertIsNone(state_store.read_manual_capacity_signal(
            session_id, record["signal_journal_ref"]))

    def test_unpinned_signer_fails(self):
        session_id = _uuid()
        record, _pinned = _signed_manual_signal(key_id="unknown-key")
        with self.assertRaises(state_store.ManualSignalSignatureError):
            wake_manual.verify_and_record_manual_signal(session_id, record, {})

    def test_tampered_field_after_signing_fails(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record["role"] = "reviewer"  # mutated after the signature was taken
        with self.assertRaises(state_store.ManualSignalSignatureError):
            wake_manual.verify_and_record_manual_signal(session_id, record, pinned)

    def test_idempotent_retry_of_identical_record_succeeds(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        first = wake_manual.verify_and_record_manual_signal(session_id, record, pinned)
        second = wake_manual.verify_and_record_manual_signal(session_id, record, pinned)
        self.assertEqual(first, second)

    def test_conflicting_content_for_same_journal_ref_raises_plain_value_error(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        wake_manual.verify_and_record_manual_signal(session_id, record, pinned)
        other_record, other_pinned = _signed_manual_signal(
            key_id="key-2", signal_journal_ref=record["signal_journal_ref"])
        merged_pinned = dict(pinned)
        merged_pinned.update(other_pinned)
        with self.assertRaises(ValueError):
            wake_manual.verify_and_record_manual_signal(
                session_id, other_record, merged_pinned)

    def test_corrupt_on_disk_record_raises_corrupt_record_error(self):
        """F-N05: a damaged existing record is a DISTINCT failure mode from
        an ordinary conflict -- Package B refuses to silently overwrite or
        discard it (M3B-REV-M03)."""
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        path = state_store.manual_capacity_signal_path_for(
            session_id, record["signal_journal_ref"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json!!")
        with self.assertRaises(state_store.CorruptRecordError):
            wake_manual.verify_and_record_manual_signal(session_id, record, pinned)


class OneVerifyAndRecordPathTest(_WakeManualEnvMixin, unittest.TestCase):
    """F-N04: the CLI (`run_verify`) has NO parallel implementation of its
    own -- it calls exactly `verify_and_record_manual_signal`, the SAME
    function a programmatic caller uses."""

    def test_run_verify_source_calls_the_shared_function_not_state_store_directly(self):
        module_path = os.path.join(_HERE, "cowork_wake_manual.py")
        with open(module_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=module_path)
        run_verify_node = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "run_verify")
        called_names = set()
        for node in ast.walk(run_verify_node):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)
        self.assertIn("verify_and_record_manual_signal", called_names)
        self.assertNotIn("write_manual_capacity_signal", called_names)
        self.assertNotIn("verify_manual_capacity_signal", called_names)

    def test_run_verify_calls_shared_function_exactly_once(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        with mock.patch.object(
                wake_manual, "verify_and_record_manual_signal",
                wraps=wake_manual.verify_and_record_manual_signal) as spy:
            wake_manual.run_verify(session_id, record_path, keys_path,
                                   output=lambda s: None)
        self.assertEqual(spy.call_count, 1)


class ClassifyManualSignalErrorTest(unittest.TestCase):
    def test_signature_error_classified_verification_failed(self):
        exc = state_store.ManualSignalSignatureError("bad signature")
        self.assertEqual(wake_manual.classify_manual_signal_error(exc),
                         "verification_failed")

    def test_corrupt_record_error_classified_corrupt_state(self):
        exc = state_store.CorruptRecordError("damaged on-disk record")
        self.assertEqual(wake_manual.classify_manual_signal_error(exc),
                         "corrupt_state")

    def test_journal_conflict_message_classified_journal_conflict(self):
        exc = ValueError(
            "manual capacity signal for signal_journal_ref 'x' already "
            "recorded with different content")
        self.assertEqual(wake_manual.classify_manual_signal_error(exc),
                         "journal_conflict")

    def test_other_value_error_classified_invalid_arguments(self):
        exc = ValueError("role must be a nonempty string, got None")
        self.assertEqual(wake_manual.classify_manual_signal_error(exc),
                         "invalid_arguments")

    def test_journal_conflict_marker_matches_package_bs_actual_wording(self):
        """Pins `_JOURNAL_CONFLICT_MESSAGE_MARKER` against a REAL conflict
        Package B raises -- if Package B's own wording ever drifts, this
        test fails loudly instead of `classify_manual_signal_error` silently
        misclassifying a genuine conflict as `invalid_arguments`."""
        session_id = _uuid()
        root = tempfile.mkdtemp()
        old_root = os.environ.get("COWORK_SESSIONS_ROOT")
        os.environ["COWORK_SESSIONS_ROOT"] = root
        try:
            record, pinned = _signed_manual_signal()
            wake_manual.verify_and_record_manual_signal(session_id, record, pinned)
            other_record, other_pinned = _signed_manual_signal(
                key_id="key-2", signal_journal_ref=record["signal_journal_ref"])
            merged_pinned = dict(pinned)
            merged_pinned.update(other_pinned)
            try:
                wake_manual.verify_and_record_manual_signal(
                    session_id, other_record, merged_pinned)
                self.fail("expected a journal conflict ValueError")
            except ValueError as exc:
                self.assertEqual(
                    wake_manual.classify_manual_signal_error(exc),
                    "journal_conflict")
        finally:
            if old_root is None:
                os.environ.pop("COWORK_SESSIONS_ROOT", None)
            else:
                os.environ["COWORK_SESSIONS_ROOT"] = old_root
            shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI: run_verify / main.                                                    #
# --------------------------------------------------------------------------- #


class CliVerifyTest(_WakeManualEnvMixin, unittest.TestCase):
    def test_success_exit_code_and_payload(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.run_verify(session_id, record_path, keys_path,
                                      output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["success"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "success")
        self.assertEqual(payload["record"]["signal_journal_ref"],
                         record["signal_journal_ref"])

    def test_verification_failed_exit_code_for_bad_signature(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record["detached_signature"] = "11" * 64
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.run_verify(session_id, record_path, keys_path,
                                      output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["verification_failed"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "verification_failed")

    def test_invalid_arguments_exit_code_for_malformed_shape(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        del record["candidate_digest"]
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.run_verify(session_id, record_path, keys_path,
                                      output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["invalid_arguments"])

    def test_invalid_arguments_exit_code_for_missing_record_file(self):
        session_id = _uuid()
        _record, pinned = _signed_manual_signal()
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.run_verify(
            session_id, os.path.join(self._files_dir, "nope.json"), keys_path,
            output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["invalid_arguments"])

    def test_invalid_arguments_exit_code_for_malformed_json(self):
        session_id = _uuid()
        _record, pinned = _signed_manual_signal()
        bad_path = os.path.join(self._files_dir, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.run_verify(session_id, bad_path, keys_path,
                                      output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["invalid_arguments"])

    def test_journal_conflict_exit_code(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        wake_manual.run_verify(session_id, record_path, keys_path,
                               output=lambda s: None)
        other_record, other_pinned = _signed_manual_signal(
            key_id="key-2", signal_journal_ref=record["signal_journal_ref"])
        merged_pinned = dict(pinned)
        merged_pinned.update(other_pinned)
        other_record_path = self._write_json("other_record.json", other_record)
        other_keys_path = self._write_json("other_keys.json", merged_pinned)
        lines = []
        code = wake_manual.run_verify(session_id, other_record_path,
                                      other_keys_path, output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["journal_conflict"])

    def test_corrupt_state_exit_code_distinct_from_journal_conflict(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        path = state_store.manual_capacity_signal_path_for(
            session_id, record["signal_journal_ref"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json!!")
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.run_verify(session_id, record_path, keys_path,
                                      output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["corrupt_state"])
        self.assertNotEqual(code, wake_manual.VERIFICATION_EXIT_CODES["journal_conflict"])
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "corrupt_state")

    def test_main_wires_argv_through_to_run_verify(self):
        session_id = _uuid()
        record, pinned = _signed_manual_signal()
        record_path = self._write_json("record.json", record)
        keys_path = self._write_json("keys.json", pinned)
        lines = []
        code = wake_manual.main(
            ["verify", "--session-uuid", session_id,
            "--record-file", record_path, "--pinned-keys-file", keys_path],
            output=lines.append)
        self.assertEqual(code, wake_manual.VERIFICATION_EXIT_CODES["success"])

    def test_exit_codes_are_a_versioned_exported_contract(self):
        self.assertEqual(wake_manual.VERIFICATION_EXIT_CODES, {
            "success": 0, "internal_error": 1, "invalid_arguments": 2,
            "verification_failed": 3, "journal_conflict": 4,
            "corrupt_state": 5,
        })


# --------------------------------------------------------------------------- #
# Structural gates: no private key/self-sign path, no D import, exact       #
# four-path allowlist, A/B/D integrity.                                      #
# --------------------------------------------------------------------------- #


class NoSelfSignPathTest(unittest.TestCase):
    """Production code holds no private key and contains no self-sign
    path -- an AST-level check of the SOURCE (immune to being fooled by a
    docstring mentioning these names in prose), not merely 'no test in this
    file uses one'."""

    def _module_path(self):
        return os.path.join(_HERE, "cowork_wake_manual.py")

    def _tree(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=self._module_path())

    def _code_attribute_names(self):
        """Every `.attr` name referenced anywhere in the module's CODE
        (function/attribute names), skipping the module docstring."""
        tree = self._tree()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            if isinstance(node, ast.Name):
                names.add(node.id)
        return names

    def test_no_signing_helper_referenced_in_code(self):
        names = self._code_attribute_names()
        for forbidden in ("_ed25519_selftest_sign", "_ed25519_selftest_publickey",
                          "_ed25519_scalarmult", "_ed25519_encodepoint"):
            self.assertNotIn(forbidden, names,
                             "production code references signing helper %r" % forbidden)

    def test_no_hardcoded_private_key_bytes_or_seed_string(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("secret_key", source)
        self.assertNotIn("private_key", source)
        self.assertNotIn("hashlib.sha256(b\"cowork", source)

    def test_top_level_imports_never_include_hashlib(self):
        """No plausible signing-adjacent hashing import at all in
        production code -- verification is entirely Package B's job."""
        tree = self._tree()
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        self.assertNotIn("hashlib", names)


class StructuralGatesTest(unittest.TestCase):
    def _module_path(self):
        return os.path.join(_HERE, "cowork_wake_manual.py")

    def _tree(self):
        with open(self._module_path(), "r", encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=self._module_path())

    def _top_level_imports(self):
        names = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_imports_only_expected_modules(self):
        imported = self._top_level_imports()
        self.assertEqual(imported, {"argparse", "json", "os", "sys", "cowork_state"})

    def test_never_imports_package_d_scheduler(self):
        """The manual adapter's own scope is verify-then-journal only; it
        never claims a PauseLease itself, so it never needs Package D."""
        self.assertNotIn("cowork_capacity_scheduler", self._top_level_imports())

    def test_never_imports_a_control_plane_module(self):
        self.assertNotIn("cowork_control_plane", self._top_level_imports())


class ABDIntegrityTest(unittest.TestCase):
    """Package F imports Package B without needing to modify it, and their
    own test suites are unaffected by this addition."""

    def test_package_b_manual_signal_accessors_present(self):
        for name in ("verify_manual_capacity_signal", "write_manual_capacity_signal",
                    "read_manual_capacity_signal", "ManualSignalSignatureError",
                    "canonical_manual_capacity_signal_message"):
            self.assertTrue(hasattr(state_store, name))

    def test_package_a_still_importable_and_functional(self):
        import cowork_capacity as capacity  # noqa: F401

    def test_package_d_scheduler_still_importable(self):
        import cowork_capacity_scheduler  # noqa: F401


if __name__ == "__main__":
    unittest.main()
