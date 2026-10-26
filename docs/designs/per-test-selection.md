# Design: per-test selection inside a lane (GATE-007 follow-up)

**Status:** design only. Nothing here is enforced, and no rule ID is assigned yet. Issue: [#188](https://github.com/zackees/ci.yml/issues/188) (pytest half). The Rust/nextest half is generalized in [#191](https://github.com/zackees/ci.yml/issues/191) under the PR critical-path umbrella [#189](https://github.com/zackees/ci.yml/issues/189). The binding text, once measured, goes into `GATE-007` in [policy-general.md](../policy-general.md) or into a new `GATE` ID taken from the registry in AGENTS.md (`GATE-014` is the next free one today).

## Problem

GATE-007 ([`ci_lint/lane_cache.py`](../../ci_lint/lane_cache.py)) caches a whole lane. The key covers every tracked path not excluded from the lane, so a lane is either fully reused or fully rerun.

- **soldr, pytest.** A docs-only change costs about 76 s locally, because the `guards` lane reruns every Python test (about 73 s). The lane's `strace` audit (`ci-lint local-gate lanes --audit guards`) showed that excluding `docs/**` is unsafe: 13 test files read `docs/*.md` as doc-contract tests. A docs edit has to rerun those 13 files, not the other ~2,000 tests.
- **soldr, nextest.** A Rust change costs about 498 s locally, almost all of it in the bosn `tests` lane: 3,571 tests, of which 132 take 5 s or more and account for 83% of test time (#191). The crate-graph experiment H2 in [pr-critical-path.md](pr-critical-path.md) found that every Rust PR still selects 96% or more, because `soldr-cli` holds 95% of test time and depends on everything.
- Once GATE-008 lets attested PRs skip the remote quick gate, the local gate *is* the PR critical path. Its lanes are where time now goes.

Fleet layout, surveyed from each repository's default-branch tree (GET-only `gh api .../git/trees/HEAD?recursive=1`, 2026-10-25):

| Repo | Workspace crates | `tests/*.rs` files | Python `test_*.py` | `conftest.py` | nextest config | pytest plugins |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| soldr | 48 | 163 | 203 | 1 | yes | `pytest-xdist` |
| zccache | 39 | 132 | 83 | 1 | yes | none |
| running-process | 26 | 73 | 109 | 2 (incl. `tests/pty/`) | yes | `pytest-timeout`, `pytest-cov` |
| clud | 10 | 17 | 220 | 3 (incl. `tests/harness/`, `tests/integration/`) | no | `pytest-timeout`, `pytest-cov` |

So both halves matter, the Python suites are large enough to be worth selecting in all four, and nested `conftest.py` files are common (three repos).

soldr already narrows its bosn `tests` lane in-repo at crate level (soldr#3731: `ci/bosn_workspace_test.py` plus a shared `ci/crate_graph.py` that walks path, dev and build dependencies; the full workspace runs on any root, lockfile, toolchain, test-infra or unknown path, dirty tree or git error). Its trailer still says lane-level `pass`. This design keeps that approach as level 1 and adds the per-test level, with visible provenance.

## Decisions

| # | Decision | Evidence |
| --- | --- | --- |
| D1 | **Observed inputs, never declared ones.** A test's inputs are what it was seen to read on its last passing run. | soldr's `docs/*.md` contract tests: nothing in their imports says they read docs. The GATE-007 audit found them only by tracing. |
| D2 | **Python: record imports and file opens with a CPython audit hook (`sys.addaudithook`, events `import` and `open`), not pytest-testmon.** | testmon fingerprints Python code blocks through coverage; it does not record non-Python files a test opens, which is exactly soldr's 13-file case. An audit hook sees both, works inside each `pytest-xdist` worker (soldr runs xdist), and needs no ptrace, so it also runs where `strace` is missing (lane_cache's `run_audit` returns `None` then). |
| D3 | **Python selection unit is the test file (module), not the test node,** in phase 1 and 2. Node-level selection is phase 4 and only if measured worthwhile. | Collection-time imports, module- and session-scoped fixtures and parametrization run once per module, so their reads cannot be attributed to a single node soundly. soldr's problem is file-shaped (13 files vs ~200). |
| D4 | **Rust: the selection unit is the nextest test binary, and its key includes the binary's own content hash.** | A test binary links every crate it depends on, so its bytes change whenever any compiled input (source, dependency, feature, `build.rs` output, toolchain) changes. That is sound by construction, with no per-test coverage. Building is far cheaper than running here: the tests lane is ~480 s of nextest. nextest already exposes binaries (`cargo nextest list --message-format json` gives `binary-id` and `binary-path`) and filters on them (`-E 'binary_id(...)'`). |
| D5 | **Per-test (inside one binary) Rust selection is not designed here.** | It needs per-test coverage profiles (`cargo llvm-cov nextest`), which is #191's "decide from data" step. H2 already shows the cost is concentrated in one binary family (`soldr-cli` / `cargo_front_door`), so the binary key alone will not help soldr's worst case; that is #191's measurement to make, not an assumption to build on. |
| D6 | **Local only.** CI's mirrored jobs, the default-branch push run, `ci-full` and the release gate always run the full suite. | GATE-001 mirrors; pr-critical-path.md's round 2 showed the `main` push run caught all 9 pre-gate breakages. #191: "the release gate always runs the full suite." |
| D7 | **A partial lane does not count as a lane pass for GATE-008 trust** until the promotion criterion below is met for that repository. | GATE-008 condition 6 requires the trailer to cover every lane the base declares. A selected run is a different, weaker claim and must be distinguishable. |

## Soundness argument

A test is skipped only when **every** input it read on a recorded passing run is byte-identical now, **and** nothing that decides what the test *is* has changed. That is GATE-007's argument, applied at a finer grain:

1. The record is a superset of what the test could read on this tree, *provided* the test is deterministic about which files it reads. A test whose read set depends on unrecorded state (time, network, environment) is caught by expiry and by the lane-wide invalidators, not by the record.
2. If every recorded file is unchanged, and the interpreter/toolchain, lockfile, environment and selector configuration are unchanged, then the test runs the same code over the same files as the recorded pass.
3. A test that was never recorded, or whose record is ambiguous, runs. A missing input costs a rerun; it can never cause a skip.

Reads outside the repository (the venv, the toolchain, `~/.cargo`) are not hashed per test. They are covered by the lane-wide invalidators (lockfile, toolchain pins, tool versions in the GATE-007 key) and by expiry.

### Fail-closed table

| Condition | Outcome |
| --- | --- |
| No record for the test file / binary | run it |
| Record older than the lane's `max-age-hours` (default 24, as GATE-007) | run it |
| Any recorded tracked input's blob sha differs, or the file is gone | run it |
| A recorded read of an **untracked** repository path (generated or ignored file) | run it always (cannot be keyed) |
| Record written by a different selector `KEY_VERSION` | run everything |
| Gate declaration, the lane's argv files, any lockfile, toolchain pin, `pyproject.toml`, `pytest.ini`/`setup.cfg`/`tox.ini`, `.config/nextest.toml`, `.cargo/**` changed | run everything |
| Any `conftest.py` changed | run every test file at or below that directory |
| A test file was added or renamed, or collection output (node ids) differs from the record | run the new/changed files; if collection itself fails, run everything |
| Dirty tree, git error, `git ls-tree` failure | run everything (as soldr#3731) |
| Last run of the test failed, errored, was skipped by a marker, or was interrupted | run it (only passes are recorded) |
| Audit hook unavailable (non-CPython, hook disabled) | run everything |
| Rust: binary not rebuilt deterministically (hash differs with no source change) | run it (costs time, never correctness) |
| Test reads the network or spawns a process whose reads the hook cannot see | see below: such tests are recorded as `opaque` and always run |

**Subprocesses.** A Python audit hook does not see reads done by a child process (`subprocess`, a spawned `soldr` binary). A test that triggers the `subprocess.Popen` / `os.exec*` / `os.posix_spawn` audit events is marked `opaque` and always runs, unless the lane opts into `strace` recording for that file (phase 3), which follows children (`strace -f`, as `run_audit` already does). Most of soldr's expensive Rust-side behaviour is exercised this way, so this is the main reason the selected fraction may be larger than hoped; it is measured in phase 1 rather than guessed.

## Input-key model

### Python (per test file)

```
key = sha256(
  "pts/v1",
  lane GATE-007 key minus the tree component   # argv, tool versions, declared env
  python implementation + version, venv lockfile hash,
  sorted (path, blob sha) for:
    the test file itself,
    every conftest.py on its path to the rootdir,
    every tracked repository file seen by the `import` audit event
      (the observed import graph, transitive, incl. dynamic imports),
    every tracked repository file seen by the `open` audit event
      (docs, fixtures, data files),
  node-id list collected for the file,
)
```

The import graph is observed rather than parsed, so `importlib.import_module(name)` and plugin entry points are included. Reads are recorded from the start of collection for that module until its last test's teardown, which attributes collection-time and module-fixture reads to the file (D3). Session-scoped fixture reads go to a `session` pseudo-record that every file depends on.

### Rust (per nextest binary)

```
key = sha256(
  "pts/v1",
  lane GATE-007 key minus the tree component,
  binary-id, sha256 of the built test binary,
  sorted (path, blob sha) for every tracked file the binary's tests opened at runtime
    (recorded with `strace -f` on the binary; a binary with no trace record is never skipped),
  sorted (path, blob sha) of tracked files under the crate's `tests/` and fixture dirs,
)
```

`include_str!`/`include_bytes!` inputs are already in the binary hash. Runtime reads (fixtures, `env!("CARGO_MANIFEST_DIR")`-relative files) are not, so they come from the trace.

### Storage

Records live next to the lane cache (`<git common dir>/ci-lint/lane-cache/<lane>/tests/`), shared across worktrees, never pushed, only for passing runs, as GATE-007 entries are.

## Interaction with attestations, GATE-008 and the merge queue

- **Trailer provenance.** A lane that ran a selection records `<lane>:partial@<key12>(ran <n>/<total>)` in the `Local-Gate:` trailer instead of `run` or `reused@<key12>` (format from #188; `ci_lint/local_gate.py` builds the existing items). The `Ci-Attestation:` trailer (GATE-010) records the same lane with `"selected": true` and the counts.
- **GATE-008.** During rollout, `partial` does not cover a lane for condition 6, so an attested-but-partial head runs the remote quick gate as before (D7). A repository may opt in to treating `partial` as covering once its shadow record meets the promotion criterion; that opt-in is read from the **base** `[gate.trust]`, like every other trust input. The audit sample is unchanged and still runs the full remote jobs.
- **GATE-001.** Unaffected: the mirrored remote job runs the full suite, so a selection miss surfaces as a remote failure on any non-trusted head.
- **GEN-021 / merge queue.** Unaffected. Reuse proves a remote job's `success`; a local selection never produces one. A `merge_group` run is remote and always full.
- **Release.** The release candidate's exact-SHA run is full. A selector must refuse to run when invoked from a release flow.

## Rollout

| Phase | Work | Acceptance |
| --- | --- | --- |
| 0. Measure | Add `ci-lint local-gate lanes --simulate-tests <lane>`: replay the last N commits of soldr's history against recorded inputs, report would-run files/binaries and test-seconds, using GATE-007's existing lane replay (`LaneSimulation`) as the harness. | Before/after numbers for soldr: a docs-only change, a single guard-script change, a single-crate leaf change, and a `soldr-cli` change. Recorded in #188/#191. Stop here if the median saving is under 20% of lane time. |
| 1. Shadow, Python | pytest plugin (shipped from `ci_lint`, stdlib-only) records per-file inputs; the lane still runs everything and logs `would skip`. | 30 gate runs on soldr and one more fleet repo (clud: 220 test files, nested conftests). Zero tests that the selector would have skipped failed. Fraction of `opaque` files reported. |
| 2. Enforce, Python | Local `guards`-style lanes run the selection; trailer says `partial`. | soldr docs-only change: `guards` under 15 s (from ~73 s), with the 13 doc-contract files still run. GATE-008 still treats `partial` as not covering. |
| 3. Rust binaries | Binary-hash key plus `strace -f` runtime reads; shadow then enforce, same criterion as phase 1. Shares soldr's `ci/crate_graph.py` closure as the level-1 pre-filter. | Shadow: zero misses over 30 runs. Report what fraction of test-seconds is in binaries that any ordinary change selects (expected high on soldr, per H2). |
| 4. Optional | Node-level Python selection; per-test Rust selection via coverage (#191). | Only if phase 2/3 data shows the file/binary unit leaves more than 30% of lane time on the table. |
| 5. Policy | Write the measured rule into GATE-007 or a new GATE ID; add a static check that a selector is never wired into a release, `ci-full`, `merge_group` or default-branch push job. | Policy text lands with the rule's Status column marked per what `ci_lint` actually enforces. |

## Non-goals

- Selecting tests in **remote CI** (mirrored jobs, `main` pushes, `ci-full`, `merge_group`, release). Remote speed comes from GATE-008 and GEN-021.
- Selection from **declared** inputs, path-to-test maps, or naming conventions.
- **Flaky-test** handling or retry. A flaky test's failure simply leaves no record, so it reruns.
- **Test ordering, sharding, or prioritisation** (H5 rejected sharding for now).
- Per-test selection **inside a Rust binary** (D5, owned by #191).
- Non-CPython interpreters and non-pytest Python runners: they get "run everything".
- Replacing GATE-007's lane cache: per-test selection only runs on a lane miss.
