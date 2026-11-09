# prooft: a pure-Python GATE-010 producer, adopted from zero (retrospective)

**Repository:** [forge-ci/prooft](https://github.com/forge-ci/prooft) (0.1.0 on PyPI, 2026-11-09).
**Linter pin during adoption:** `24362f3e60248ebe72ffd261e5585640228cbc5d`.
**Outcome:** `ci-lint precheck` 0 violations, 1 approved exception (GEN-004).
Status of every proposal below: **Candidate** -- nothing here changes a rule.

## What prooft is

A standard-library-only Python package that writes and judges the GATE-010
`Ci-Attestation` trailers documented in [ci-attestations.md](../ci-attestations.md).
Its trailers are byte-compatible with `ci_lint.attestations` (a prooft test
builds the same record with both and compares the trailer text). On top of
the documented format it adds:

- **per-job skip decisions** from a `ci-attestations.yml` read at the PR base
  (`prooft plan`), with a GATE-008-style head-trust subset (forks, title
  tags, trust-surface edits run remotely);
- **merge-queue credit** for a single-PR group (`prooft queue`) when the
  queue commit's tree equals the attested PR head, or when a lane's declared
  inputs are unchanged at the queue commit (input-keyed proofs);
- a pre-push hook with `mode: shadow | enforce` (strictest of pushed commit,
  remote tip and default branch);
- a soldr adapter mapping `soldr lint` steps to per-target Rust gates;
- JSON bundles (`emit`/`apply`) for build farms that cannot amend a commit;
- an offline GitHub simulator (PRs, CI runs, merge queue) used by its tests.

## Evidence

| Event | Result |
| --- | --- |
| forge-ci/prooft#2 (adds the workflows; trust surface) | every job ran; green |
| forge-ci/prooft#3 (attested docs change) | `fast`, `test-linux-x64` **skipped** on `pull_request`; `CI OK` credited both |
| forge-ci/prooft#7 (attested, through the merge queue) | `merge_group` run: `verify: merge group for PR #7, PR head 972ef16b4b02, queue head 0f5062ee04cb`; both jobs **skipped**; merged |
| forge-ci/prooft#9 (real `soldr lint` run) | adapter fix: a schema-2 success line has no `steps` |

## Findings

1. **No pure-Python profile; required tables with nothing to say;
   `[cache.pr].budget` documented optional but required; GEN-022 janitor
   with a zero cache budget.** [#528](https://github.com/forge-ci/ci.yml/issues/528)
2. **GEN-004 accepts only `pylint` as the Python checker**, not
   basedpyright/pyright. [#529](https://github.com/forge-ci/ci.yml/issues/529)
3. **GATE-003 names a producer (`ci-lint local-gate`) rather than the
   trailer contract; and the policy does not say the *verifier code* must
   come from the base.** A PR could otherwise edit `ci/verify.py` (not a
   `.github/` path) to approve itself. prooft's `verify` job checks out the
   base SHA (`merge_group.base_sha` for the queue) and fetches heads only
   as data. [#530](https://github.com/forge-ci/ci.yml/issues/530)
4. **GEN-024 misreads a `toJSON(needs)` aggregator** as ordering-only.
   [#531](https://github.com/forge-ci/ci.yml/issues/531)
5. **Declared inputs drift from real inputs.** `ruff format --check .`
   also formats Python blocks in `*.md`; the lint lane's include-globs
   missed them, so a README-only change would have kept a stale proof
   valid. Found by hand during dogfooding.
   [#532](https://github.com/forge-ci/ci.yml/issues/532)
6. **(soldr)** A successful porcelain line (schema 2) drops `steps`, which
   an attestation producer needs. [forge-ci/soldr#4126](https://github.com/forge-ci/soldr/issues/4126)

## What worked

- GATE-010's trailer format was precise enough to implement byte-for-byte
  from the document alone.
- The restricted-YAML rule made a zero-dependency policy parser practical.
- GEN-005 (no bash entry points) pushed the repository to a single
  `ci.py`, which is also what its attestation lanes call.
- Single-PR merge groups (`max_entries_to_merge = 1`) make queue credit a
  pure function of (base, PR head, queue head); multi-PR groups need
  input-keyed proofs per lane.
