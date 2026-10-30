# ci.yml

Policy and automation for fast, reliable pull request CI across the `zackees`, `FastLED`, and `TechWatchProject` repositories.

The fleet-wide central checker (scanning every repository, filing issues, and comparing against run history) is still planned and not implemented. A **repository-local checker exists today**: [`ci_lint`](ci_lint/), a standard-library-only Python package built in this repository, checks a `ci.toml`-declaring repository against schema 3 ([docs/ci-toml.md](docs/ci-toml.md)) and computes each CI run's plan. It has a reference implementation, [`zackees/template-python-rust-cmd`](https://github.com/zackees/template-python-rust-cmd), built and measured round by round in [issue #6](https://github.com/zackees/ci.yml/issues/6). Adopting `ci_lint` in a repository is opt-in (via its own `ci.toml` and `ci-pre.yml`); no fleet repository is scanned automatically yet.

## Decision

Standardize each code repository on `.github/workflows/ci.yml` as the entry point for its required quick PR checks. For a repository that adopts `ci.toml` schema 3, `ci_lint` evaluates that workflow (and the rest of the repository) against the declared contract today — statically before any install (`ci-lint precheck`), against a real build's artifacts (`ci-lint units`/`tests size`/`wheel`/`gate`), and live against the repository's own Actions/settings APIs (`ci-lint cache audit`, `ci-lint audit`). A fleet-wide central checker that inspects historical run timings across every repository and opens or updates one issue per confirmed violation remains planned. A finding is resolved by fixing the repository or by changing the policy or recording a reviewed exception (`ci.toml`'s `[[exceptions]]`, for a `ci_lint`-checked repository).

This combines a predictable CI interface with measured evidence. Shared reusable workflows can implement common jobs, but their use alone does not prove that required checks ran or that PR feedback was fast.

## Policy at a glance

Each row is fleet-wide policy; where `ci_lint` checks it mechanically today, the row says so and names the rule ID (see [docs/policy-general.md](docs/policy-general.md)/[docs/policy-rust.md](docs/policy-rust.md)/[docs/policy-cpp.md](docs/policy-cpp.md) for the full rule tables).

| Area | Fleet rule |
| --- | --- |
| Quick gate | `ci.yml` runs the required checks for ordinary PRs on Linux, gated by a `ci-pre.yml` first job that installs nothing. Windows, macOS, packaging matrices, and long integration runs belong in separate workflows/tags when needed. `ci_lint` enforces the entrypoint/trigger shape (`GEN-001`, `GEN-002`, `GEN-008`) for a repository that has adopted `ci.toml` schema 3. |
| Coverage | A faster gate must preserve the checks required by the repository's profile; removing a workflow is not evidence of an improvement. |
| Default-branch CI | A push to the default branch may skip a required job only through verified reuse -- the merged tree is byte-identical to a PR head whose CI passed every one of those jobs recently -- never through a blanket `if:` or a deleted `push:` trigger (`GEN-021`; `ci_lint` checks the workflow shape statically and `ci-lint reuse-check` makes the fail-closed decision; see [the design](docs/designs/default-branch-verified-reuse.md)). |
| Native Linux artifacts | Prefer musl and build musl by default for supported architectures. Also target glibc 2.17 where feasible; verify the artifact's compatibility, or track a scoped exception. |
| Windows artifacts | Track GNU versus MSVC and x64 versus ARM64 as distinct target cases. A build for one combination cannot satisfy another. |
| Rust | Use `setup-soldr` and Soldr for compile-bearing Rust checks where the dependency graph permits it. Run Dylint once on Linux and verify intended platform-specific lint coverage. The full lint phase (clippy and Dylint for every declared target) is a mandatory local gate, and Dylint must be proven before the merge queue (`RUST-033`). |
| Python | Run Ruff formatting/import checks and Pylint on the relevant Python paths. Avoid duplicate Black or isort jobs. |
| Python benchmark data | Use dataclasses as the internal model for benchmark inputs, results, and reports; never pass raw dictionaries through benchmark code. JSON and Protocol Buffers are allowed on the wire. Type boundary dictionaries with concrete value types, never `Any`; for a list of records, use at least `list[dict[str, TypedData]]` with a declared concrete `TypedData` type before converting and validating each record into a dataclass. |
| Rust apps published to PyPI or npm | Test the installed Python wheel or packed npm artifact in the quick Linux gate; keep full platform artifact matrices in release or slower validation workflows. |
| Timing | Measure required PR critical path, runner queue, job and step execution, and known cold/warm cache behavior separately. Do not file a performance violation from one outlier. |
| Local CI engine | Bosn-managed local CI runs in one outer CI engine per host Docker engine, with one act2 service accepting jobs from every client, repository and state directory; jobs run concurrently inside it with per-run limits, isolation and cancellation. Per-run engines are legacy and are drained, not force-killed. Rationale: load image/tool/action state once and reuse it (to be shown with measurements, not assumed); storage must still be bounded by automatic maintenance. Decided, not mechanically enforced; rollout status in [the policy](docs/policy-general.md#one-local-ci-engine-per-machine-decided-enforcement-pending) ([zackees/bosn#544](https://github.com/zackees/bosn/issues/544), [zackees/bosn#508](https://github.com/zackees/bosn/issues/508)). |
| Exceptions | Record the rule, reason, owner, and review date. An undocumented exception does not silently satisfy the policy. |

The exact Dylint command is not a text-match rule. For example, `soldr cargo dylint --all --workspace` and an equivalent `soldr dylint` invocation can satisfy the policy if they cover the intended workspace and targets. Direct Dylint tool installation and duplicate native Dylint jobs are candidates for review.

## Evidence behind the policy

- [Issue #1](https://github.com/zackees/ci.yml/issues/1) records Dylint cache identity drift, cold versus warm timing, missing cross-target toolchain components, and platform coverage risks.
- [Issue #2](https://github.com/zackees/ci.yml/issues/2) inventories slow PR paths, current workflow violations, already fixed cases, and gaps in the sampled fleet. Its findings include `running-process`, `fbuild-ide`, `reld`, and historical `datalake-core` runs.
- [Issue #4](https://github.com/zackees/ci.yml/issues/4) proposes the composable CI contract, full coverage checks, release candidate gate, Linux libc and Windows ABI target model, and fbuild acceptance fixture.
- [Issue #5](https://github.com/zackees/ci.yml/issues/5) measures coverage-per-compile-minute gaps: no cheap cross-target check requirement, no cache budget/save-scope policy, and no toolchain-sourcing-order rule (`RUST-008`–`010`, `GEN-009`).
- [Issue #6](https://github.com/zackees/ci.yml/issues/6) is where `ci.toml` schema 3 and `ci_lint` were actually built and measured, round by round, against the reference repository [`zackees/template-python-rust-cmd`](https://github.com/zackees/template-python-rust-cmd); [docs/case-studies/template-python-rust-cmd.md](docs/case-studies/template-python-rust-cmd.md) logs each round's build, measurements (with run IDs), defects and open items. The [case studies](docs/case-studies/) (clud, soldr, zccache, fbuild) supply most of the candidate rules `ci_lint` has since adopted or is still tracking — see [docs/policy-general.md](docs/policy-general.md) and [docs/policy-rust.md](docs/policy-rust.md) for which is which.
- The sampled Rust workflows did not reveal a pinned manual Soldr installation without `setup-soldr`. The checker should still detect that pattern without asserting it exists fleet-wide.

## Documentation

- [General code repository policy](docs/policy-general.md): quick gate, the `ci.toml`/`ci-lint` contract, caching, secrets/publishing, local CI, the local gate first rule (`GATE-001..004`: the remote quick gate is a subset of one local command, and a PR head must carry a tree-bound `Local-Gate:` attestation before remote CI runs), remote-only checks that never gate a PR (`GATE-012`: CodeRabbit suppressed, act-impossible actions confined to non-PR events, zero wait on app checks), timing, and exceptions — with a full rule-ID table marking what `ci_lint` enforces today versus what remains a candidate.
- [Rust repository policy](docs/policy-rust.md): common Rust checks, the Soldr/platform-facade/Dylint/private-crate decisions, libraries, and apps published to PyPI or npm.
- [C/C++ (CMake) repository policy](docs/policy-cpp.md): `cpp/...` attestation gates, C/C++ lane-key pins and compiler identity, ccache/zccache cache families, no CMake build trees in caches, the cmake/ctest `GEN-005` allowlist, parallel `ctest` (`CPP-001`), and the format-lane and native-leg patterns ([#393](https://github.com/zackees/ci.yml/issues/393)).
- [`docs/ci-toml.md`](docs/ci-toml.md): the field-by-field schema-3 reference and rule catalog for `ci_lint`, the source of truth this repository's policy docs are checked against.
- [`ci_lint/`](ci_lint/): the checker itself (stdlib-only Python) — static precheck, the tag/flow planner, runtime build/wheel/units checks, cache key/save-ok/audit/janitor commands, the live settings/secrets audit, the OIDC mock-publish check, and the release-candidate gate.
- [`examples/rust-pypi-app/ci.toml`](examples/rust-pypi-app/ci.toml): the canonical `ci.toml`, identical (apart from repo-specific values) to the one in the reference repository, [`zackees/template-python-rust-cmd`](https://github.com/zackees/template-python-rust-cmd).
- [Designs](docs/designs/): multi-phase specifications spanning policy, `ci_lint`, and fleet repositories -- currently [verified reuse on default-branch pushes](docs/designs/default-branch-verified-reuse.md) (`GEN-021`).
- [Agent operating guide](docs/agent-guide.md): mechanical scan, evidence, issue lifecycle, and regression cases (the fleet-wide central-checker design; not `ci_lint`'s own per-repository checks, which have their own RED/GREEN fixtures under `ci_lint/tests/`).
- [Detailed proposal](proposal.md): repository inventory, release lifecycle, and target/libc/ABI model. Its original `ci.toml` contract/schema section is superseded by schema 3 — see the note at its top.
- [AGENTS.md](AGENTS.md): index and instructions for agents working in this repository, including the rule-ID registry (next free number per family).

## Status

`ci_lint` — the repository-local checker — exists and is merged: static precheck, the tag/flow planner, runtime build/artifact checks, the cache runtime (key building, save-ok, live audit, trim/janitor/heal/preprune, PR deltas), the live settings/secrets audit, the OIDC mock-publish check, and the release-candidate gate. It is proven against one repository, [`zackees/template-python-rust-cmd`](https://github.com/zackees/template-python-rust-cmd), which has adopted `ci.toml` schema 3 and runs `ci_lint`'s precheck on every PR. Every rule `ci_lint` enforces has a RED and a GREEN fixture (`ci_lint/tests/fixtures/<RULE-ID>/`).

**Not yet implemented:** a fleet-wide central scanner that inventories every repository, a scheduler, and automated issue synchronization — a repository must opt in with its own `ci.toml` and `ci-pre.yml` today; nothing scans a repository that hasn't adopted the contract. See [docs/policy-general.md](docs/policy-general.md) and [docs/policy-rust.md](docs/policy-rust.md)'s rule tables for the per-rule "Enforced by ci-lint" versus "Candidate" split — do not treat a candidate rule as already checked anywhere.
