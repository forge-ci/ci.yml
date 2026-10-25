# Case study: kernal-api PR CI 12m00s → 2m37s — a nine-step measured speed loop

Case studies record verified evidence from a specific repository incident or
investigation. They are not policy: a rule only binds once it is written into
[policy-general.md](../policy-general.md) or [policy-rust.md](../policy-rust.md).
Transcribed from the retrospective in
[zackees/ci.yml#147](https://github.com/zackees/ci.yml/issues/147) (loop run
2026-09-28/29); every number below was recorded there with its run or PR
evidence, and the follow-up items live in issues #82–#86.

The loop used this repository's policy as its guide, picked the largest
measured cost each iteration, changed it, and measured the effect on real
runs of `zackees/kernal-api`.

## Outcome

| Step | PR | Change | Measured (run evidence) |
|---|---|---|---|
| 0 | — | Baseline: `linux` gate → Dylint (`needs: linux`) → `Dylint coverage` sentinel | PR run **12m00s** (36346780139); Dylint job 9m27s, lint pass 357–388 s, `0 HIT / 2078 MISS` |
| 1 | kernal-api#369 | Dylint starts beside `linux` instead of after it | **9m27s** (36514594793) |
| 2 | kernal-api#368 | setup-soldr → v0.9.82 (`@v0`): Dylint caches finally save (`RUST-004`) | Dylint job **8m44s → 3m35s**; lint pass **~6 min → 37 s** |
| 3 | kernal-api#370 | Over budget, retire orphaned lock generations LRU-first | Main runs went from failing the cache budget (10.45 GB of 10.2 GB) to passing: 1 entry retired, 9.21 GB |
| 4 | kernal-api#371 | Run the Dylint lanes concurrently | Lint step 107 s → 96 s; Dylint job 3m35s → 3m13s (small, CPU-bound) |
| 5 | kernal-api#372 | Lint-library fixture suites only when `dylints/**`/shim/`ci.yml` change | *not yet measured*: no qualifying PR has run |
| 6 | kernal-api#373 | WebKit/GTK packages only in full mode | `linux` **2m16–2m22s → 1m47s** |
| 7 | kernal-api#375 | Drop the aggregate sentinel job; the Dylint lint step is the aggregate | PR run **7m31s → 3m55s** (the sentinel had waited 2m52s for a runner) |
| 8 | kernal-api#376 | apt install in the background during setup | Install on the critical path went from 22–46 s to **5 s**; PR run **3m09s** |
| 9 | kernal-api#377 | Stop restoring an unused cargo registry | **No time gain** (see lesson 7); frees 116 MB of budget |
| | | **Latest PR run** | **2m37s** (36528978012) |

## Lessons, ranked by impact

### 1. The biggest win was a cache that *claimed* to work

Dylint "had caching": setup-soldr was configured and the layers were
declared. But every successful run skipped its own save (`no matching
successful Dylint marker`). Fixing that one upstream bug
(setup-soldr#539/#541) was worth ~6 min per PR, more than every other
change combined. Follow-ups: `RUST-004`/`CACHE-011` should run on every
repo's latest main run, and the save line (`saved id=…`) belongs in the
evidence required to confirm any cache layer as working.

### 2. Being behind on setup-soldr was the root cause, fleet-wide

10 of 13 repos pinned stale setup-soldr refs across 9 distinct
SHAs/versions (#31); SHA-pinning guard tests made drift the default.
#33/M2-29 settled the direction: `setup-soldr@v0` is the one sanctioned
floating ref, and `RUST-014` covers the repos that must SHA-pin.
Remaining edges recorded in #413-era follow-ups: promotion itself was a
bottleneck (`update-v0-tag` only promotes main's tip — setup-soldr#546).

### 3. Queue time is now the dominant cost, and policy measures compute

After the compute fixes, the largest single item was a 3-second
aggregate job that waited **37 s to 2m52s for a runner** (#86); pre-job
queue waits of 1.5–5 min appeared on several runs. Follow-ups:
`PERF-001` reports queue time per job (landed — `queue_seconds`), and
every extra job on the critical path costs a queue wait: prefer in-job
aggregation and fewer, fatter jobs.

### 4. Ordering edges that protect nothing

"Linux first so red runs don't waste Apple/Windows runners" was applied
to a Linux job and cost the gate's full duration (#83). A `needs:`
edge must carry an artifact or protect a scarce runner; otherwise the
job derives its mode and SHA itself.

### 5. Match installed tools and packages to the feature graph each mode compiles

The gate installed WebKit/GTK in every mode, but only full mode
compiles them — `cargo tree` proves it cheaply (no `-sys` crates in the
default graph; #84). When packages are needed, start apt in the
background as the first step and wait before the first compile
(22–46 s → 5 s). System dependencies follow `[rust].ship` and the
mode's feature sets.

### 6. Concurrency on 4-core runners disappoints; skipping work doesn't

Three compile lanes at once only cut the lint step 107 s → 96 s, while
the workspace pass slowed 37 s → 74 s under contention. The real lever
was **not compiling at all**: path-gating the lint-crate fixture suites
to `dylints/**` (#82). `RUST-006` rightly bans caching test binaries,
so path-scoping is the only way to avoid that cost.

### 7. Read the timeline before optimizing, not the summary table

A 41 s registry restore was removed expecting ~20 s saved; the setup
step stayed at 42 s because that restore ran *concurrently* — the real
wall time came from sequential Dylint → toolchain → snapshot phases
(setup-soldr#548). Two other "obvious" wins were measured and dropped
(`--no-install-recommends`: 114 → 104 s; the sentinel's cost was queue,
not compute). A speed finding needs a before/after run pair; a summary
total isn't evidence of which phase sets the wall time.

### 8. Budget guards can turn a speed fix into a red main

The first successful Dylint cache save pushed usage over the 10.2 GB
budget, and the retirement job failed because nothing was eligible —
exactly the orphaned-lock-generation class (#85, since shipped as the
janitor's over-budget LRU-first eviction in `ci_lint.cache.ops`). Expect
every repo whose Dylint cache starts saving after #31 to grow ~1 GB.

## Process lessons for agents running this loop

- **Measure on real runs and keep the IDs.** Each iteration recorded a
  before/after run; the only claims that held up were the ones with run
  IDs.
- **Validate locally before spending CI.** The Dylint marker identity was
  checked with the pinned soldr (a cold 348 s local pass wrote the
  host-qualified marker v0.9.81 expects), and workflow `run:` blocks were
  extracted and run under `bash -eo pipefail` with fake tools — which
  caught a real bug: a background apt subshell under `-e` would have hung
  the wait step 10 minutes on failure.
- **Some changes can't measure themselves.** A PR that edits `ci.yml`
  runs every gated suite, so path-gating savings show only on a later
  ordinary PR. Plan a follow-up measurement instead of claiming the win.
- **Local soldr gotcha.** Mixed soldr versions sharing `~/.soldr` break
  the Dylint wrapper (soldr#3456); use an isolated `SOLDR_CACHE_DIR`.
- **Some decisions belong to the owner.** Removing the aggregate, moving
  `v0`, and fleet-wide pin changes were asked or directed; everything
  else was reversible CI work.

## Evidence index

- **kernal-api PRs:** #368 #369 #370 #371 #372 #373 #375 #376 #377
- **Fleet:** #31, #33, #40, zackees/soldr#3445, and the float PRs listed in #31
- **Tooling:** zackees/soldr#3456, zackees/setup-soldr#546, zackees/setup-soldr#548
- **Policy issues:** #82 #83 #84 #85 #86

## What became policy

Verified against the current Status columns: `RUST-004`/`CACHE-011`
(the continuous "did the layer actually save" checks) and `PERF-001`'s
queue reporting are **implemented**; the setup-soldr float direction
landed as `@v0` + `RUST-014`; lesson 8's orphan retirement shipped in
`ci_lint.cache.ops` (closed as #85); lessons 4–6 are recorded as the
candidate/rollout items in #83/#84/#82. The run table and process
lessons above are the evidence those decisions rest on.