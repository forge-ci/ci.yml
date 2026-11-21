# Case study: one 16-CPU dev host, ~10 agents, load 145 — local gates that never finish

Case studies record verified evidence from a specific repository incident or
investigation. They are not policy: a rule only binds once it is written into
[policy-general.md](../policy-general.md) or [policy-rust.md](../policy-rust.md).
Recorded 2026-11-21 by the soldr dist orchestrator while running five Codex
workers alongside the fleet's other agents (M5 pool rollout, lint-policy,
FastLED, clud grind, hermes-slim).

## What was observed

| Time (UTC) | Evidence |
|---|---|
| ~21:08 | soldr#4182 (adds a standalone `ui/soldr-ui` package) — `ci-lint local-gate run` stopped after **20.6 min**, no `Local-Gate:` attestation. Worker log: "16 logical CPUs, load averages 83–107"; CI-lint, guards, Python/static and fmt passed, the Rust and workspace-test lanes were still compiling. |
| ~21:40 | zccache#2222 — local gate did not pass: Python lane lacked `pip` (1,096 passed, 1 failed); the retry was abandoned when HEAD moved. Remote CI then failed **Dylint** on three `ban_std_pathbuf` hits that a host `soldr lint` would have caught in minutes. |
| 21:45 | `uptime`: load average **80.29, 83.36, 86.71** on 16 CPUs, 125 GiB RAM (85 GiB available — CPU-bound, not memory-bound). |
| 21:55 | `uptime`: load average **145.32**, 106.76, 93.26. `ps` showed **101 concurrent `rustc`** processes plus clippy-drivers from at least six unrelated checkouts (fbuild, bosn-deferred, a soldr gate worktree, zccache, signal, kernal-api), two `ci-lint local-gate lint` processes at ~45% CPU each, a standalone `zccache-daemon` v1.15.12, a soldr-daemon v0.9.54, gcc `cc1` (zstd), Chromium, WebKit and a Protonmail bridge at 135% CPU. |

## Why it happens

1. **No cross-agent admission.** Each repo's build runs its own Cargo jobserver
   sized to the machine (16), and each soldr/zccache daemon version runs its own
   admission gate. Six concurrent builds × 16 jobs ≈ 100 rustc, which is what
   `ps` showed. Every individual build is "correctly" parallel; the host is not.
2. **Local gates widen the blast radius.** The gate for a PR that touches only an
   excluded package still ran the full soldr workspace test lane, because the PR
   also edited the root `Cargo.toml` (`[workspace] exclude`). Any root/lockfile
   path selects the full workspace (soldr#3731 rules), so a two-line root edit
   bought a 20-minute compile under contention.
3. **Abandoned gates are pure loss.** A gate that is cancelled at minute 20 (HEAD
   moved, worker timed out) leaves no attestation and no reusable verdict; the PR
   then falls back to full remote CI anyway.

## Workarounds applied in this run

- Serialize gate runs per orchestrator: only one of the orchestrator's workers
  runs a local gate at a time; the others run the affected crate's nextest plus
  host `soldr lint` and push without attestation, relying on remote CI.
- Give a standalone package its own empty `[workspace]` table instead of adding
  it to the root `exclude` list, so the PR touches only `ui/**` and does not
  force the full workspace lane.
- Run `soldr lint` on the host before pushing (cheap, catches Dylint rules such
  as `ban_std_pathbuf` that clippy does not).

## Fleet follow-ups worth filing

1. **Host-wide compile admission across agents.** A single host-level budget that
   every soldr daemon generation and standalone zccache daemon consults (one
   token pool per host, not per daemon) would turn "100 rustc on 16 CPUs" into a
   queue. This is the same shape as soldr's exclusive-admission ladder
   (soldr CLAUDE.md, soldr#3138) lifted from per-daemon to per-host.
2. **Offload compiles off the dev host.** soldr dist (forge-ci/soldr docs/DIST.md)
   sends cache-miss compiles to idle E640 dist nodes; a contended dev host is its
   first real customer.
3. **Gate abandonment telemetry.** Record gate runs that end without an
   attestation (duration, lane reached, reason) so the cost above is measured,
   not anecdotal.
4. **Gate path-selection for standalone packages.** A path that is its own
   workspace root should select only its own tests.
