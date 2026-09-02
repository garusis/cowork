# Backend-gate pointer

Keep current backend eligibility outside the repository so refreshing or
expiring accreditation does not change the candidate it certifies. The stable
path is:

```text
${COWORK_ORCHESTRATOR_ROOT:-~/.cowork/orchestrator}/<repository-hash>/backend-gate/current.json
```

`repository-hash` is the first 16 lowercase hex characters of SHA-256 over the
canonical main-checkout root path encoded as UTF-8. A worktree resolves through
its Git common directory to the same main-checkout root.

The pointer is controller-owned and atomically replaced only after a global
accreditation passes independent review:

```json
{
  "schema_version": 1,
  "repository_root": "/absolute/canonical/repository/root",
  "head": "40 lowercase hex characters",
  "tree": "40 lowercase hex characters",
  "release_digest": "64 lowercase hex characters",
  "selector_evidence": {
    "path": "/absolute/path/to/selector-evidence.json",
    "sha256": "64 lowercase hex characters"
  },
  "global_adjudication": {
    "path": "/absolute/path/to/global-adjudication.json",
    "sha256": "64 lowercase hex characters"
  },
  "updated_at": "RFC3339 timestamp"
}
```

Before selecting Cowork:

1. Verify the pointer schema and canonical repository root.
2. Verify current HEAD and tree equal the pointer bindings.
3. Hash and parse both referenced files; hashes and release digests must match.
4. Require the adjudication verdict to be `GLOBAL_PASS`, with no blockers or
   majors, and require its candidate head/tree to match the pointer.
5. Verify the selector manifest's six receipt hashes against the actual receipt
   files admitted by the adjudication/accreditation packet.
6. Run `scripts/select_backend.py` on that verified selector manifest.

Any mismatch, missing pointer, stale receipt window, changed candidate, or
unverifiable external file selects `direct-claude`. Do not search arbitrary old
package directories for a convenient PASS and do not update this pointer from a
worker assertion.
