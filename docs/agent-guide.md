# Agent operating guide

Use this guide when investigating or implementing fleet CI checks. The goal is to spend agent context on ambiguous cases after mechanical evidence has already been collected. This is a specification for planned automation, not a description of an existing scanner.

## Mechanical pass before agent diagnosis

1. Inventory active repositories under `zackees`, `FastLED`, and `TechWatchProject`; record default branch, language/artifact profile, workflow files, declared required checks, and documented exceptions. Keep the future inventory in one versioned central source of truth.
2. Parse current workflow YAML and follow local or accessible reusable workflow calls. Check the `pull_request` entry point, required check names, effective runner labels, Soldr/setup-soldr use, Dylint invocations, Python lint checks, and package smoke tests. A command hidden behind a script or unresolved expression is **needs review** until inspected.
   For native artifacts, record the execution host and each artifact target separately: Linux musl versus glibc 2.17, Windows GNU versus MSVC, and x64 versus ARM64. Do not infer libc or ABI from the runner name. Validate the artifact and its runtime compatibility where evidence is available.
3. Query Actions `pull_request` runs and their jobs and steps. Calculate the required PR critical path and job execution separately from queue time. Collect representative run, job, workflow, and commit links. Treat `pull_request_target` runs separately because their permissions and trigger semantics can differ.
4. Compare current configuration with historical runs. Mark fixed cases as historical; never infer that deleting a slow workflow preserved test coverage. Collect explicit cache restore/save and toolchain identity evidence for Dylint cache findings.
5. Apply the [general](policy-general.md), [Rust](policy-rust.md) and [C/C++](policy-cpp.md) rules. Emit a machine-readable result for each rule: status, repository, rule ID, subject, evidence URLs, observed values, and applicable exception. Send only violations and unresolved cases to agent review.

GitHub documents the [workflow run](https://docs.github.com/en/rest/actions/workflow-runs) and [workflow job](https://docs.github.com/en/rest/actions/workflow-jobs) APIs needed for timing collection. Reusable workflows can hide jobs behind a caller, so inspect their definitions as well as the caller; see [GitHub's reuse reference](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).

## CI-side diagnostics when local reproduction misleads (fingerprint recipe, #148)

Diagnosing a cache/build **fingerprint** mismatch locally is often wrong evidence before CI evidence is: soldr and plain cargo resolve different registry paths (observed as `PathToSourceChanged`), and a developer host may lack what the runner ships (a NixOS host: `libz`, X11, build-ids) -- both directions of "reproduces locally" / "does not reproduce locally" have lied (#148). Recipe, cheapest first:

1. **Read the CI fingerprint evidence before touching a checkout**: the failing job's cache restore/save lines (`exact hit` vs `saved id=…`), the marker identity the step wrote (e.g. the Dylint host-qualified marker), the toolchain and `--version` pins in that step, and its environment. The inputs that mismatched are named there.
2. **Reproduce in CI, not on the host**: push a **throwaway draft PR** scoped to the failing path so only the job under investigation runs (drafts notify no watchers; close it when done). CI-side diagnostics in a draft PR were repeatedly faster and more reliable than local attempts (#148).
3. **Exercise workflow logic without a runner**: extract the workflow's `run:` blocks and execute them under `bash -eo pipefail` with fake tools on `PATH`. This has caught real bugs a green local run missed -- a background apt subshell under `-e` would have hung a wait step for ten minutes ([kernal-api-ci-speed.md](case-studies/kernal-api-ci-speed.md), process lessons).
4. **Isolate tool state when a pin must be validated locally**: an isolated `SOLDR_CACHE_DIR` (and equivalents) -- mixed tool versions sharing a global state dir break the very wrapper under test (soldr#3456).
5. **Keep run IDs for every claim**: before/after pairs only; a summary total is not evidence of which phase set the wall time.

("Fingerprint" here means the build/cache key inputs -- toolchain, lockfile, marker identity, environment -- not the issue-fingerprint marker in [Findings and issue lifecycle](#findings-and-issue-lifecycle).)

## Audit platform coverage selection instructions

Inspect repository instructions, every platform skill and bundled agent
planning, review, integration and landing procedure for unconditional native
label or cross-target-loop instructions. Apply
[Selecting full PR coverage](policy-general.md#selecting-full-pr-coverage):
additional platform coverage requires a changed platform implementation,
not business logic calling it. Check the actual diff and name the affected
native implementation/contract. A target discovery tool may enumerate
release-only lanes; that list does not select PR coverage. Report ambiguous
cases for review rather than inferring a full matrix from a filename or
local runner gap. Preserve routine required checks and full release validation.

## Findings and issue lifecycle

Use the stable `GEN-*`, `PY-*`, and `PERF-001` IDs in the [general policy](policy-general.md) and `RUST-*` and `PKG-*` IDs in the [Rust policy](policy-rust.md), and `CPP-*` IDs in the [C/C++ policy](policy-cpp.md). Keep these IDs stable when implementing the result schema; change a rule's version rather than reusing an ID for different behavior.

For a confirmed finding, fingerprint `owner/repo + rule ID + workflow/job identity`. Search for an open issue with that fingerprint. Create one issue if absent; otherwise add new measurements and current workflow evidence to it. Include the violated rule, current file/line link, representative run and job links with dates and durations, whether cache state is known, affected coverage, and one concrete remedy. Do not file issues from a single ambiguous run or a text-only match to a script name.

The disposition is either:

- **Repository correction:** change the workflow, verify the required checks still execute and pass, and compare before/after timing on comparable runs.
- **Policy correction or exception:** document why the fleet rule is unsuitable, specify compensating coverage, name an owner and review date, and update the central policy or exception record before closing the finding.

For implementation changes to the checker, use focused regression fixtures from [issue #1](https://github.com/zackees/ci.yml/issues/1) and [issue #2](https://github.com/zackees/ci.yml/issues/2): repeated native Dylint, qualified versus short nightly cache identity, read-only restored output, missing cross-target `rust-std`, queue-heavy runs, a historical fixed case, a removed test workflow, equivalent Soldr command forms, and an approved dependency-cycle exception. Require a failing fixture before a fix and a passing fixture afterward.

## Current limits

The fleet inventory in issue #2 sampled repositories and PR history; it is not proof that uninspected repositories comply. The first central-scanner slice exists ([issue #38](https://github.com/zackees/ci.yml/issues/38)): `python3 -m ci_lint fleet scan` inventories repositories live and read-only (ci.toml adoption `FLEET-001`, PR entrypoints `GEN-001`/`GEN-008`, runner labels `RUN-001`, setup-soldr pin freshness `RUST-014`, settings `SEC-007`/`GEN-006`, cache usage `FLEET-002`) and ranks them, and `python3 -m ci_lint sync-issues --dry-run` prints the exact fingerprinted issue plan (one issue per repo + rule + subject, per the lifecycle above). No scheduler exists, and still no repository is enrolled: an agent still reports the observed scope rather than claiming fleet-wide enforcement for anything the scan does not check (Dylint, cache families, timing).

`python3 -m ci_lint history <scan.json> <scan.json>...` ([issue #99](https://github.com/zackees/ci.yml/issues/99)) diffs two or more recorded `fleet scan --json` files with no GitHub call: each finding is keyed by repo + rule + subject exactly as `sync-issues` fingerprints it, and the newest scan is compared with the previous one -- `new`, `persisting` (with the first date of its unbroken streak), `resolved`, or `unscanned` when the newest scan did not cover that repository (a narrowed `--repos` run never resolves anything). Scans are dated by the `scanned_at` field `fleet scan --json` now writes, or a `YYYY-MM-DD` in the filename for older recordings; `--json` gives a machine-readable report, and the exit code is 1 only when a new violation appeared.

**Scan cadence: daily** (decided 2026-10-25 for #99). Evidence: the three owners hold 157 non-archived, non-fork repositories (zackees 139, FastLED 15, TechWatchProject 3, counted live that day); a scan is GET-only and spends roughly 10-15 requests per repository (ci.toml, the workflow listing and files, Actions cache usage, branch protection, rulesets, settings) plus the paginated listings and one setup-soldr release list, so about 1,600-2,400 requests -- under half of one hour of the 5,000/hour authenticated REST budget, once per day. Weekly would be cheaper but too coarse for its consumers: `sync-issues` and PERF-001 escalate on two successive scans, and with weekly scans a regression would need two weeks to be confirmed and a fix a week to show `resolved`. More often than daily buys nothing, because fleet CI policy changes land through PRs on a day scale and each extra run competes for the same token budget as working agents. Run it off-peak from an agent's cron (never a new workflow file in this repository) and keep each day's file as `fleet-scan-YYYY-MM-DD.json`; `history` over the last two files is the daily delta, and over a month of files it gives first-seen dates. Re-measure the request count with `fleet scan --record` before raising the cadence.

Round M2-42 ([issue #98](https://github.com/zackees/ci.yml/issues/98)) added a write path, `python3 -m ci_lint sync-issues --apply`, gated by three independent guards a reader can verify without touching the network:

1. **Per-repository opt-in.** A repository must set `[fleet].sync-issues = true` in its own `ci.toml` (schema 3, `ci_lint.schema.FleetConfig`) before `--apply` will ever create, update, or close an issue there. `--dry-run` still prints the plan for every scanned repository regardless of this key, so an owner can see what enrolling would do before opting in. `--apply` against a repository that has findings but no opt-in reports it under "repos with findings but no opt-in" and performs zero API calls for it -- not even a GET.
2. **`--max-writes N`** (default 5) caps the number of issue writes (create + update + close, each counted once) performed in a single `--apply` run. Actions beyond the cap are reported as skipped, not silently dropped -- rerun to pick up the remainder.
3. **The fingerprint marker.** `fetch_existing` only ever recognizes an existing issue whose body contains `<!-- ci-lint-fingerprint: <16 hex> -->`; `update`/`close` act exclusively on issues found that way, and the write path re-checks the marker is present in the body it is about to send as a second, structural guarantee. `sync-issues` never touches an issue a human filed by hand with the `ci-lint` label but no marker.

`--apply` is idempotent by construction: `plan_repo`'s title/body comparison against the fetched `ExistingIssue` already classifies a matching issue as `unchanged` (round #38), and `run_apply` performs zero writes for an `unchanged` action, so a rerun against unchanged findings writes nothing. A finding that stops reproducing closes its issue with an explanatory comment rather than deleting it, preserving the fingerprinted history.

An agent MUST NOT run `--apply` against a real repository without the repository owner's explicit request naming that repository -- opting a repository in via `[fleet].sync-issues = true` is itself that consent for future scheduled runs, but a first `--apply` run against a newly-enrolled repository should still be announced. Never export a GitHub token into a shared shell environment to run this command; use the narrowest-scoped invocation available.
