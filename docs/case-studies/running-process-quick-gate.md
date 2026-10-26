# Case study: running-process quick gate 13m09s -> ~5m15s (#148)

Source: [zackees/ci.yml#148](https://github.com/zackees/ci.yml/issues/148). Repository: `zackees/running-process` (a Rust workspace plus a PyO3/maturin `rust-pypi-app`), 2026-09-28 to 2026-09-29.

An agent loop picked the largest measured cost each iteration, fixed it, and measured again. The Linux quick-gate job `preflight (linux-x86)` went from **13m09s to ~5m15s** (-60%). The loop stopped once overlap experiments showed the job was CPU-bound on the 4-core runner.

## Measured steps

Unless noted, each step was measured on a warm `main` run. Evidence lives in the running-process PRs named below, which are the run references the issue recorded.

| # | Change (running-process PR) | Measured effect | Rule |
| --- | --- | --- | --- |
| 1 | Drop CI `--no-capture` from nextest (#1231) | nextest 318.6 s -> 181.8 s; job 13m09s -> 8m18s | `RUST-017` |
| 2 | `opt-level = 3` for sha2/blake3/digest in `[profile.dev.package.*]` (#1232) | nextest 181.8 s -> 50.3 s | `RUST-031` |
| 3 | Name the target binaries in a filtered feature pass (#1234) | compile 32.4 s -> 18.6 s | `RUST-015` |
| 4 | Split the Dylint fixture and gate target dirs; run fixtures from their crate dir so `.cargo/config.toml` applies (#1235, #1237) | fixtures 34 + 43 s -> 3 + 2 s; gate 27 -> 16 s | `RUST-016` |
| 5 | Cache the second Cargo workspace (#1238) | 23.4 s -> 1.9 s | `RUST-029` |
| 6 | Exclude the PyO3 crate from the plain workspace build (#1241), then mirror its features on the build step (#1242) | build 63 -> 35 s; pre-nextest compile 45.7 -> 28.5 s; job -> 5m19s | `RUST-030`, `PKG-009`, `CACHE-039` |
| 7 | Dylint as a parallel job (#1244) | ~37 s of steps off the critical path (inside runner noise) | `RUST-032` clause (e) |
| x | pytest-xdist `-n auto --dist loadfile` (#1243, closed) | ~20 PTY / zombie-tracker / mock-serialization failures | `TEST-003` |
| x | Overlap pytest with nextest in one job (#1245, closed) | CPU saturated: pytest 33 -> 81 s, nextest 41 -> 57 s | ci-toml.md "When to stop" |

## What it taught

- **Most of the cost was invisible rework.** Six of seven wins were compile or cache work that was done and then discarded, and none of it failed anything. `CACHE-035` (`ci-lint cache rework --log`) now detects a rebuild after an exact hit and a cross-step recompile. The third signal, the serial-execution ratio, is not implemented because the log body carries no step timing; `RUST-017` and `TEST-003` cover serial execution statically.
- **One root cause, many incidents.** When two steps build into the same `target/` under a different linker, build-script env, crate type or unified feature set, each overwrites the other's units. `CACHE-039` states this as the general rule. `RUST-016`, `RUST-030` and `PKG-009` are its named triggers.
- **Measurement decides the verdict.** Measure cache-affecting changes on warm `main`, take rerun attempt 2, and use the median of N runs against the ±15% runner band (ci-toml.md "Measurement protocol"). A save that runs only on a miss can freeze a bad entry (`CACHE-040`).
- **Diagnose in CI.** The fingerprint recipe is in docs/agent-guide.md ("CI-side diagnostics when local reproduction misleads").
- **Process.** Admin-merge only on a green quick gate, and stage explicit paths (docs/agent-guide.md "Agent merge hygiene"). A file referenced by tracked config must itself be tracked (`GEN-032`). Repository config must not pin a soldr version (`RUST-013`'s agent-config clause).
