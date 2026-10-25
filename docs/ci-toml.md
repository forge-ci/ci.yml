# `ci.toml` schema 3 reference

This is the field-by-field reference for `ci.toml` schema 3, the contract
`ci_lint` (this repository) loads, validates, plans against, and (from
round-2A) checks a real build's output against. It documents what
[issue #6](https://github.com/zackees/ci.yml/issues/6) implements so far:

- round-1A: the `ci_lint` package's static **precheck** and its **planner**.
- round-2A: the runtime commands that need a real build, wheel or venv --
  `python3 -m ci_lint units`, `tests size`, `wheel check`, `wheel
  installed`, and `gate` (the `CI OK` aggregator) -- plus `precheck
  --local` (skips checks needing the GitHub API, explicitly) and
  `selftest` (runs this package's own test suite; see
  zackees/zccache#1760).
- round-3A: the planner's platform-lane matrix outputs (`platform_lanes`,
  `fast_suites`, `lane_digests` -- see "The planner" below); title-edit
  **reuse** (`ci_lint plan --reuse`, `ci_lint precheck --reuse
  --github-output`; see "Title-edit reuse"); and `ci_lint gate --reuse`'s
  live reuse verification (see "Reuse verification" under "Runtime
  commands").
- round-4A: the cache runtime (`python3 -m ci_lint cache ...`) -- key
  building, `save-ok` (issue #6 §6's do-not-save table, CACHE-008), the
  live `audit` (CACHE-001/003/005/006/008/009 + the budget check),
  `trim`/`janitor`/`heal`/`preprune` (live deletes), PR delta mechanics
  (`cache delta manifest|pack|apply`), and `precheck --live`. See "Cache
  runtime (round-4A)" below.
- round-4B: per-job permission grants (`[allow].permissions`, `SEC-002`
  refinement); the sanctioned `actions/cache*` wrapper (`[allow].cache-
  actions`, `CACHE-001`/`CACHE-002` refinement); plan-driven cache saves
  (`[allow].setup-soldr.require`/`[allow].setup-uv.require` values may be
  the special string `"plan"`, and the planner's `cache_save` `--github-
  output` key); and the static `GEN-004` implementation (Ruff + Pylint in
  the `fast` job, no standalone Black/isort).
- round-5: `ci_lint audit` (`python3 -m ci_lint audit`) -- a live,
  read-only settings/secrets audit (`SEC-005`/`006`/`007`, `GEN-006`,
  `GEN-011`; see "Settings audit (round-5)" below) -- replaces the stub
  that previously exited 2. `ci_lint publish oidc-check` (`ci_lint.
  publish_oidc`) -- issue #6 §7's mock publisher: mints and asserts the
  claims of the OIDC token, never prints it, and stops before upload.
  `ci_lint release verify` (`ci_lint.release`) -- issue #6 §3/ci.yml#4's
  release-candidate gate: the staged wheel+sdist set must exactly match
  `[platforms]`, share one version, each pass the existing `wheel check`,
  and (optionally) have a passing `--smoke` result; writes `release-
  manifest.json` (`PKG-006`). `ci_lint perf compare` (`ci_lint.perf`) --
  a baseline/current benchmark comparison, non-gating unless `[suites.
  perf].gating = true` and `--threshold-pct` is given.
- #156 (`GEN-021`): `ci_lint reuse-check` -- verified reuse on
  default-branch pushes (GET-only, fail-closed, `enforce|shadow`) -- and
  `ci_lint reuse-report` (retroactive shadow evidence), plus the static
  `GEN-021` precheck rule. See "Verified reuse on default-branch pushes"
  under "Runtime commands".

`ci_lint audit` (round-5) is a live, read-only **settings/secrets** audit
-- `SEC-005`/`006`/`007`, `GEN-006`, `GEN-011` -- see "Settings audit
(round-5)" below. It is unrelated to `ci_lint cache audit` (round-4A,
below), which classifies GitHub Actions cache entries. proposal.md's
broader "ci-lint history" idea (comparing declared coverage against
*observed* GitHub run history over time) is still future scope; round-5
only covers the settings/secrets slice the round-5 brief named.

The canonical example is [`examples/rust-pypi-app/ci.toml`](../examples/rust-pypi-app/ci.toml),
copied verbatim from the design draft in
[issue #6](https://github.com/zackees/ci.yml/issues/6). Where this document
and that example ever disagree, treat it as a bug in this document.

`ci.toml` never generates YAML: it bounds what a repository's own
`.github/workflows/*.yml` may do, and decides what each CI run selects.

## Loading and strictness

`ci_lint.schema.load_ci_toml` reads `ci.toml` with `tomllib` into frozen
dataclasses. Loading is **strict**:

- An unknown key anywhere in the file (including inside a nested table) is
  `CT-001`, reported with the key's full dotted path (e.g.
  `platforms.linux-x64.bogus`).
- A key whose TOML value has the wrong type -- including a required key
  that is simply missing -- is `CT-002`. `CT-002` also covers referential
  integrity: a `flow`/`tags` entry that names a platform, suite, or flow
  that does not exist; an `extends` cycle; and a malformed `[[exceptions]]`
  entry (`issue` not a URL, `expires` not `YYYY-MM-DD`). The brief does not
  allocate separate rule IDs for these referential checks, so they share
  `CT-002` ("a value that does not resolve to a valid thing"); this is a
  round-1A decision, not a numbering the design fixed elsewhere.
- `schema` must equal exactly `3`; any other value is `CT-003`.

## Top-level keys

| Key | Type | Meaning |
| --- | --- | --- |
| `schema` | int | Must be `3`. |
| `profile` | string | e.g. `"rust-pypi-app"`. Only `rust-pypi-app` has enforced requirements in round 1A (below); other profiles load but are not yet cross-checked. |
| `linter` | string | `"zackees/ci.yml@<40-hex-sha>"` -- the exact `ci-lint` commit precheck runs. Validated by regex at load time; the workflow's actual checkout of `zackees/ci.yml` is cross-checked against it by `CT-004`. |

## `[platforms.<id>]`

One entry per platform id (`linux-x64`, `windows-arm64`, ...). Each id
generates a derived tag `ci-<id>`; each distinct `group` generates a
derived tag `ci-<group>`.

| Field | Type | Meaning |
| --- | --- | --- |
| `target` | string | Rust target triple, e.g. `x86_64-unknown-linux-gnu`. Cross-checked against `Cargo.toml`'s `[workspace.metadata.soldr].targets` by `CT-006`. |
| `runs-on` | string | A fleet runner label (see `RUN-001`'s fleet list below). |
| `group` | string | e.g. `linux`, `windows`, `macos`. Platforms sharing a group share a `ci-<group>` tag and an `os`-scoped cache cardinality. |
| `wheel` | string (optional) | e.g. `manylinux_2_17`. Informational in round 1A. |

## `[python]`

Required when `profile = "rust-pypi-app"`.

| Field | Type | Meaning |
| --- | --- | --- |
| `backend` | string | Must be `"soldr"` for the `rust-pypi-app` profile. |
| `cli.name` | string | The command the wheel installs on `PATH`. Cross-checked against `[project.scripts]`/`[project.gui-scripts]` (`PKG-003`: must NOT be present there -- the command must be the bundled native binary, not a Python shim). |
| `cli.crate` | string | The Rust crate bundled as that CLI. Cross-checked against `[tool.soldr.pep517].bundle-bins` (`PKG-003`: must be present there). |
| `cli.native` | bool (default `true`) | Informational in round 1A. |
| `abi3` | string | e.g. `"cp310"`. Informational in round 1A. |
| `pythons` | array of strings | e.g. `["3.11", "3.14"]`. Informational in round 1A. |

## `[rust]`

Required when `profile = "rust-pypi-app"`.

| Field | Type | Meaning |
| --- | --- | --- |
| `public` | string | The repo-relative directory of the public "amalgam" crate: re-exports and `dep:` feature wiring only. Its `[features]` are checked by `RUST-011`. |
| `private` | string (glob) | e.g. `"crates/private/*"`. Every matching crate is checked by `RUST-011`: `publish = false`, no `[features]`, no optional dependencies, no `cfg(feature)` in its sources. |
| `ship` | array of string arrays | The **only** amalgam feature sets ever compiled, e.g. `[[], ["json"]]`. Any `--features`/`--all-features`/`cargo hack`/`--feature-powerset` outside this set is `RUST-011`. `default` is compared as its members (both here and in `ci-lint units`' compiled sets), so with `default = []` (or no `default`) `["default"]` and `[]` are the same set -- ci.yml#89. |

### `[rust.tests]`

| Field | Type | Meaning |
| --- | --- | --- |
| `max-binary` | string (size) | Informational in round 1A (no runtime measurement yet). |
| `max-total` | string (size) | Informational in round 1A. |
| `binaries` | array of strings | The declared test-binary names, in the form `<crate>:lib`, `<crate>:bin:<name>`, or `<crate>:test:<name>` -- see `RUST-005`/`RUST-012` below. This is the fan-out budget: every test binary Cargo would actually build must appear here, and every entry here must correspond to something Cargo actually builds. |

## `[lint.dylint]`

| Field | Type | Meaning |
| --- | --- | --- |
| `targets` | string | `"all-platforms"` in round 1A (the only value the planner understands: dylint runs once per declared platform, all on Linux). |
| `shape` | string | `"multi-target"` \| `"sequential"` \| `"per-target-jobs"` (`"measure"` while undecided). The template measured multi-target faster than sequential in round 3B (template-python-rust-cmd#19); not enforced by precheck. |
| `budget.warm` / `budget.cold` | string (duration) | Informational in round 1A. |

## `[suites.<id>]`

One entry per suite id. Each id generates derived tags `ci-test-<suite>`
and `no-test-<suite>`.

| Field | Type | Meaning |
| --- | --- | --- |
| `run` | string | The command that runs the suite (e.g. `"ci/test.py unit"`). Not executed by precheck; informational/plan-only in round 1A. |
| `required` | bool (default `false`) | The `rust-pypi-app` profile requires a `unit` and a `smoke` suite with `required = true`. |
| `cache` | string (optional) | e.g. `"none"` for a suite that proves caches mask nothing. Informational in round 1A. |
| `kind` | string (optional) | e.g. `"bench"`. Informational in round 1A. |
| `gating` | bool (default `true`) | Whether a failure blocks merge. Informational in round 1A. |

If a suite id is exactly `perf`, a derived tag `ci-perf-<group>` is added
for every platform group.

## `[flow.<id>]`

One entry per flow id (`pr`, `main`, `release`, `nightly`, ...).

| Field | Type | Meaning |
| --- | --- | --- |
| `extends` | string (optional) | Another flow id. Fields not set on this flow inherit the extended flow's resolved value (base-to-derived merge; see `ci_lint.resolve.resolve_flow`). A cycle is `CT-002`. |
| `platforms` | `"all"` or array of platform ids | The base platform selection. |
| `suites` | `"all"` or array of suite ids | The base suite selection. |
| `dylint` | string (optional) | e.g. `"all-platforms"`. |
| `budget.critical-path` | string (duration, optional) | Informational in round 1A. |
| `cache` | string (optional) | `"write"` marks this flow as a cache **writer** (only `main` and `nightly` should set this -- see `[cache].write-on`). Planner: `cache_mode` is `"write"` when the resolved flow sets this, else `"read"`. |
| `schedule` | string (optional) | e.g. `"daily"`. Selects this flow for the `schedule` event. |
| `wheels` | string (optional) | e.g. `"all"`. Informational in round 1A. |
| `publish` | string (optional) | `"pypi"` (the real release path -- mapped through `[publish.pypi].mode`), `"rehearsal"`, or `"none"`. |
| `janitor` | bool (default `false`) | Marks a flow that runs the cache janitor. Informational in round 1A (no runtime implementation yet). |
| `pre-prune` | bool (default `false`) | A writer flow that prunes stale cache entries before writing. When **every** writer flow (`cache = "write"`) sets `pre-prune = true`, `CACHE-004`'s worst-case sum drops the lockfile-change-peak term (`worst = steady + [cache.pr].budget`); the budget-exceeded violation still fires if that smaller sum still exceeds `[cache].budget`. The waiver must be honoured by a real `ci-lint cache preprune` call (CACHE-034), and honouring it has structural preconditions -- a job holding `actions: write` plus ordering ahead of the saves within the same run; where neither can be had, removing the declaration is the only honest fix and the peak term comes back (see the CACHE-034 row in policy-general.md, #354). |

## `[tags.<id>]`

Declares a non-derived tag, matched against `[<id>]` tokens in the PR
title. `ci-full`, `ci-perf`, `ci-cache-save`, `no-test`, and `release` are
the tags the canonical example declares; a repository may declare more.

| Field | Type | Meaning |
| --- | --- | --- |
| `add.platforms` | `"all"` or array of platform ids (optional) | Union into the selection. |
| `add.suites` | array of suite ids (optional) | Union into the selection. |
| `remove.suites` | `"tests"` (sentinel: every suite) or array of suite ids (optional) | Subtracted from the selection. Any tag with a `remove` clause that matches makes the plan `mergeable = false`. |
| `cache` | string (optional) | e.g. `"pr-base"`. Informational in round 1A. |
| `flow` | string (optional) | Switches the selected flow entirely (e.g. `release` switches a PR run onto the `release` flow -- a release rehearsal). |
| `publish` | string (optional) | Overrides the resolved flow's `publish`. |

Besides declared tags, the planner and `TAG-001` also recognize **derived**
tags that need no `[tags.*]` entry: `ci-<platform-id>` and `ci-<group>` (add
platforms), `ci-test-<suite>` (add a suite), `no-test-<suite>` (remove one
suite; also sets `mergeable = false`), and `ci-perf-<group>` (add the `perf`
suite, when one is declared).

## `[cache.promote]` and `[cache.share.<id>]` (CACHE-024, #185)

Two tables describing how a cache entry moves **between** branches. Both are declarations of policy; neither is a mechanism on its own.

| `[cache.promote]` | type | default | meaning |
| --- | --- | --- | --- |
| `mode` | `"off"` \| `"ancestor"` | `"off"` | `ancestor`: a default-branch push inherits the entry a pull request proved, through the git DAG, instead of rewriting it on every push. `off` (today's behavior) rewrites. |
| `max-age-hours` | number | `168` | how old a promotable entry may be. |

| `[cache.family.<id>].promote` | `"ancestor"` or absent | absent | Declares that this family's saved keys carry the **lineage label** (`ci_lint.cache_lineage`), which is what makes a nearest-ancestor promotion search decidable from the keys alone. `CACHE-031`. |

| `[cache.share.<family-id>]` | type | default | meaning |
| --- | --- | --- | --- |
| `scope` | `"branch"` \| `"repo"` | `"branch"` | `branch`: the family is restored within this repository by any branch that proves the same lockfile/shape. `repo`: every branch reads and writes one shared entry. |

`[cache.share.<id>]` must name a family the `ci.toml` declares: a scope for a
family that does not exist describes nothing, and is `CT-002`. `scope = "repo"`
is only coherent for a family whose key is `lockfile = true`, since that is what
makes every branch's entry interchangeable.

**Promotion's mechanism is reachable through `v0`, and is still a pilot on GitHub-hosted runners.**
`zackees/setup-soldr` shipped `src/lib/ancestor-cache.ts` in
[#566](https://github.com/zackees/setup-soldr/pull/566), gated by `autoKeyEnabled` in
`src/main.ts`. On 2026-10-10 the sanctioned `zackees/setup-soldr@v0` float moved to
`b05310a` ([#574](https://github.com/zackees/setup-soldr/pull/574)), so a repository can
opt in with `key: auto` (or `auto-key: "true"`) without breaking `SEC-004`. No immutable
`v0.9.x` tag carries it yet. A repository that pins a 40-hex SHA older than `b05310a` is
still silently inert: the input parses, the pilot never runs, and restores fall back to the
legacy key.

The behaviour depends on the runner:
- **GitHub-hosted:** an opt-in pilot. `auto-key-trusted-writers` must name reviewed
  immutable writer jobs
  (`owner/repo/.github/workflows/file.yml@FULL_SOURCE_SHA:RUN_ID:ATTEMPT:JOB_ID`). Without
  one, saves go under per-source keys and restores use the legacy key.
- **Local runner (`ACT=true`, act/act2 under bosn), since #574:** no token and no reviewed
  writer are needed, because the local cache server holds only that machine's saves. The
  build cache saves under the source's own ancestor key and restores the newest
  same-identity entry, then the legacy keys. A stale legacy key that exact-hits therefore
  no longer blocks saving new workspace units. This is "newest", not true DAG-nearest.
  Zccache entries are content-addressed, so the choice affects speed only.

#574 also makes the save gate count compiles from a cargo nested in another tool (for
example uv -> a PEP 517 backend -> `soldr cargo build`) through `<build-cache>/history/`.
That fixes setup-soldr#573's "0 new compile(s)" skip.
[setup-soldr#552](https://github.com/zackees/setup-soldr/issues/552) tracks the rollout.

`CACHE-030` therefore reports `mode = "ancestor"` as **`needs_review`** — not because
nothing implements it, but because a declaration with the mechanism sitting off beside it
is a standing invitation to opt in, and the drift backstop must outlive the declaration
until promotion is observed actually working on that repository.

**A promotion claim is a claim about key *shapes*, not just intent.**
`CACHE-031` therefore also checks that the families can honor it. Promotion means: given
commit `M`, find the nearest ancestor's cache entry. That search is decidable from the
keys alone only when a key carries the lineage label
`m<n>[-c<k>]-<sha10>[-pr-<N>]`, where `m<n>` is the first-parent ordinal — so
`m<i>` precedes `m<j>` iff `i < j`. A key ending in a bare content hash
(setup-soldr's `...-<16 hex>`) cannot answer the question at all: a content hash is a
function of the input tree, so nothing in it records ancestry. That is not a gap a
janitor can bridge — the evidence is destroyed at save time.

Measured 2026-10-04 across the fleet's live cache listings, the label is adopted exactly
where it carries no bytes:

| repository | lineage-labeled entries | their size | bare-hash build caches |
| --- | --- | --- | --- |
| soldr | 70 | **0.00 GiB** (all `att1-*` attestation side-entries) | 5.97 GiB |
| bosn | 85 | **0.00 GiB** (same) | 4.41 GiB |
| zccache | 0 | — | 7.57 GiB |
| clud | 0 | — | 22 of 24 entries |
| kernal-api | 0 | — | 9 |

So a family opts in with `promote = "ancestor"` on `[cache.family.<id>]`, and its workflow
suffixes the key with the label from `ci-lint attest lineage` — the PR part already being
`${{ env.PR_CACHE_TAG }}` per `CACHE-013`. `CACHE-031` is **`needs_review`, never a hard
failure**: the opt-in is available but off, and changing a live key shape invalidates that
family's warm entries at once, which on a repository already at the 10 GB cap is a real
cost.

**Two encodings of ancestry, not two truths.** setup-soldr's shipped ancestor keys are
`setup-soldr-ancestor-build-v1-<identity>-source-<sha>-run-<n>-attempt-<n>[-pr-<n>]` and
select by **shortest parent-edge distance** from a live `git` DAG walk (`dagDistances`),
not by the `m<n>` first-parent ordinal — so `ci-lint.cache_lineage.parse_key` returns
`None` for them. Both record ancestry and both survive a merge; they differ in how
selection is decided. Reconciling the two is [#185](https://github.com/zackees/ci.yml/issues/185)'s
open question, and any normalization has to pick one rather than assume they already agree.

### Reading the cache must paginate (CACHE-033)

`GET /repos/{repo}/actions/caches` returns at most `per_page` entries. A read that stops after one page does not fail -- it returns a **shorter, plausible** list, and every caller that sums bytes undercounts silently. Undercounting a cache is the one direction that lets a footprint check pass that should have failed.

`ci-lint cache audit` paginates and **raises** at its ceiling (4000 entries) rather than reporting a truncated footprint. `CACHE-033` applies the same rule to repository-side code -- a script, `run:` block or composite action that reads the endpoint without a page walk is a finding. `?per_page=100` with no `page=` is explicitly **not** a walk.

## `[cache]`

| Field | Type | Meaning |
| --- | --- | --- |
| `budget` | string (size) | The declared total cache footprint. `CACHE-004` fails if it exceeds 10GB (GitHub's default per-repo cap) regardless of anything else. |
| `write-on` | array of strings | Which flows may write base cache layers, e.g. `["main", "nightly"]`. Informational in round 1A (not yet cross-checked against `[flow.*].cache`). |
| `never` | array of strings | Cache families that must never exist, e.g. `linked-tests`, `target-dir`. Informational in round 1A. |
| `retired` | array of strings | Retired cache families. `CACHE-009` fails if any `zackees/setup-soldr` input enables one. |
| `pr.mode` | string | e.g. `"delta"`. |
| `pr.families` | array of strings | Which families a PR may save a delta for. |
| `pr.max-per-pr` | string (size) | Per-PR delta cap. |
| `pr.budget` | string (size) | Counted once in `CACHE-004`'s worst-case sum. |
| `pr.trim` | string | e.g. `"on-close"`. Informational in round 1A. |
| `pr.expected-open-prs` | int (optional) | M2-17 (ci.yml#42): an estimate of how many PRs may hold an open, saved delta at once (a fleet-scale repository's steady PR count, not a hard cap). When set, `CACHE-014` fails if `max-per-pr x expected-open-prs > pr.budget` -- see "Sizing `[cache.pr]` for repository scale" below. Omit it and this half of `CACHE-014` is silent (no signal to check). |
| `pr.measured-largest-delta` | string (size, optional) | M2-17 (ci.yml#42): the largest delta size actually observed, e.g. copied from a `ci-lint cache delta manifest` run against a real large PR. When set, `CACHE-014` fails if it exceeds `max-per-pr` (a cap sized below an already-observed delta silently truncates that PR's saved cache on every save). Omit it and this half of `CACHE-014` is silent. |

### `[cache.family.<id>]`

| Field | Type | Meaning |
| --- | --- | --- |
| `via` | string | Which builder owns this family's real cache-key shape, and (round-4A) which prefix `ci_lint.cache.audit` expects a live key to start with. A **strict allowlist** (`ci_lint.cache.families.ALLOWED_VIA_VALUES`; unrecognized value is `CT-002`) -- see "Cache runtime (round-4A)" below for the full via -> prefix table. |
| `prefix` | string (one of `via` / `prefix` is required) | The literal key prefix this family's producer writes, for a shape no `via` describes: a sharded PATH cache like `sccache/<a>/<b>/<c>/<hash>` (203 live entries on zackees/zccache), or a repository's own action cache. Classification matches by `startswith`, so one stem covers a whole shard tree. Mutually exclusive with `via` -- a shape the allowlist knows is cited against its producer's source; a shape it does not is declared by the repository that writes it, which is where the knowledge lives. |
| `max` | string (size) | The per-instance cap, used directly in `CACHE-004`'s arithmetic and (round-4A) `cache save-ok`'s rule 6 and `cache preprune`'s live forecast. |
| `lockfile` | bool (default `false`) | If true, this family's steady-state size is counted **twice** in `CACHE-004` (steady state, plus the lockfile-change peak where old and new coexist), and (round-4A) `cache preprune` only prunes superseded entries of `lockfile = true` families. |
| `per` | string (optional) | Cardinality multiplier. A **strict enum**: `"none"` (the default, same as omitting it) -> `1`; `"platform"` -> number of declared platforms; `"cross-platform"` -> platforms minus one; `"os"` -> number of distinct platform groups. Any other value (e.g. `"target"`) is `CT-002` -- never a silent fallback to `1`, which would undercount `CACHE-004`'s arithmetic without warning. |
| `min` | string (size, optional) | Round-4A: the poison-guard floor `cache save-ok`'s rule 6 and the live `cache audit`'s `CACHE-005` both check payload/entry size against. A family that declares no `min` treats a live entry at or below 1KB as poisoned; a declared `min` replaces that floor (#417), so a family whose entries are tiny by design (soldr verdict records, empty pass markers) declares `min = "0B"`. One family is exempt from the floor without any declaration: ci-lint's own attestation side entries (`att1-*`, ~440 B per gate, #412) are a known ci-lint family -- a declared `min` for a family matching them still wins. |
| `evict` | string (optional) | zackees/ci.yml#23 §4.2: `"lru"` marks an evictable family (e.g. experiment lanes) -- `cache janitor` LRU-evicts its entries, least recently used first, down to its declared footprint (`max` × cardinality), always keeping its single newest entry. Omitted (the default): the janitor keeps the newest entry per key prefix (`CACHE-006`); an `"lru"` family is exempt from `CACHE-006`, because its keys may be content-addressed (#421). Any other value is `CT-002`. |
| `shapes` | table of `{ max = "<size>" }` (optional) | Per-shape ceilings for a family whose entries differ in size. When present the family's footprint is the **sum** of these and its cardinality is the number of them, instead of `max x cardinality`. |
| `key` | array of strings (optional) | Key components, e.g. `["os", "python", "uv.lock"]`. Scanned by `CACHE-002` for volatile tokens the same way a workflow's `with.key` is, and (round-4A, `via = "ci-lint"` families only) built by `ci-lint cache key <family>` -- `"os"` resolves to `--platform`'s id, `"python"` to `[python].pythons[0]` (the floor version), anything else to the first-16-hex-chars of that repo-relative file's sha256. |

`CACHE-004`'s formula: `worst = Σ(family.max × cardinality) + Σ(family.max × cardinality, families with lockfile=true) + [cache.pr].budget` -- unless **every** writer flow (`cache = "write"`) sets `pre-prune = true`, in which case the middle (lockfile-change-peak) term is dropped: `worst = Σ(family.max × cardinality) + [cache.pr].budget`. Either way, `worst > [cache].budget` is always a violation; pre-prune narrows the sum, it never waives the comparison. Precheck always prints the arithmetic and which formula it applied.

**A GATE-010 repository's attestation side entries (#412).** `ci-lint attest
keys` publishes one ~440 B cache entry per gate, keyed `att1-<gate>-<lineage>`
(`docs/ci-attestations.md`). ci-lint knows this family intrinsically: neither
`CACHE-001` (undeclared) nor `CACHE-005` (the 1 KB poison floor) ever fires on
`att1-*`, with or without a declaration, so a janitor-enabled repository no
longer deletes the very lineage `attest resolve --require-gate` needs
(bosn#721; obs-studio's undeclared janitor was losing it outright). Declare
the family anyway when you want it inside the budget arithmetic and bounded by
a cap -- the exemption is only from those two rules, and a `pr-<N>`-tagged side
entry is still reclaimed by `CACHE-008` when its PR closes:

```toml
[cache.family.attest]
prefix = "att1-"
max = "1MB"      # optional: brings att1-* into CACHE-004's arithmetic
evict = "lru"    # optional: bound growth by footprint instead of CACHE-006
```

### Sizing `[cache.pr]` for repository scale (M2-17, ci.yml#42)

`[cache.pr].max-per-pr`/`budget` are not fleet-wide constants -- they must be
sized from a repository's own measured delta sizes, the same way
`docs/case-studies/template-python-rust-cmd.md`'s round 4 derived
`max-per-pr = "128MB"`/`budget = "1GB"` from that repository's real PR
deltas. A clud-scale workspace (`docs/case-studies/clud-ci-cost.md`; the
round-4C clud-scale simulation, branch `sim/clud-scale`, a ballast crate
with 180 locked packages) produces much larger compile deltas than the
lean template, and copying the template's numbers verbatim into a
clud-scale `ci.toml` under-sizes both fields.

**Where the numbers come from.** `ci-lint cache delta manifest` (pure
filesystem, no network) gives the actual byte size of one PR's delta
against the current base. Run it against the largest real PR you have (or
a synthetic worst case like the clud-scale simulation) and record the
result in `[cache.pr].measured-largest-delta`. `[cache].budget`'s own
forecast -- `ci-lint cache preprune --lockfile-changed`'s live arithmetic,
identical in shape to `CACHE-004`'s static formula (steady + lockfile
peak + PR budget) -- tells you how much of the 10GB repository cap the
non-PR part of the cache already consumes; what's left over each PR delta
must share is `[cache].budget - (steady + lockfile peak)`, which
`[cache.pr].budget` should not exceed. Two measured examples from the
template's own case study, both well inside its `9GB`/`10GB` caps:

| Run | steady | lockfile peak | PR budget | forecast | `[cache].budget` |
| --- | --- | --- | --- | --- | --- |
| 36509863041 (`Cargo.lock`-only libc bump, D4) | 4,948,230,144 B | 957,349,888 B | 1,073,741,824 B | 6,979,321,856 B | 9,663,676,416 B (9GB) |
| 36514690030 (clud-scale simulation, `sim/clud-scale`, never merged) | 5,153,281,864 B | 1,162,401,608 B | 1,073,741,824 B | 7,389,425,296 B | 9,663,676,416 B (9GB) |

The clud-scale run's steady state is only ~4% larger than the template's
own -- most of a large compile's growth lands in the PR delta itself, not
the base layers -- which is why `max-per-pr`/`pr.budget` are the fields
that need repository-specific sizing, not `[cache].budget`.

**Formula.**

1. Measure the largest real (or worst-plausible) PR delta with
   `ci-lint cache delta manifest`; set `[cache.pr].max-per-pr` to at least
   that size (headroom recommended -- a cap sized exactly at one
   measurement fails on the next slightly larger PR).
2. Estimate `expected-open-prs`: the repository's typical number of PRs
   that could simultaneously hold a saved delta (its normal open-PR count,
   not a spike). Set `[cache.pr].expected-open-prs` to that estimate.
3. Set `[cache.pr].budget >= max-per-pr x expected-open-prs`.
4. Confirm `steady + lockfile peak + [cache.pr].budget <= [cache].budget`
   (`CACHE-004`'s own formula, above) and `[cache].budget <= 10GB`.

`CACHE-014` checks steps 1 and 3 mechanically whenever the optional inputs
(`measured-largest-delta`, `expected-open-prs`) are supplied; it is silent
on whichever half has no input, since sizing without a measurement is a
repository judgment call, not something `ci_lint` can prove wrong.

## Cache runtime (round-4A)

`python3 -m ci_lint cache ...` (`ci_lint.cache.*`), built on issue #6 §6
("Cache strategy"). Every live command is stdlib `urllib` through an
injectable `FetchFn`/`DeleteFn`/`GraphQLFn` (`ci_lint.github_api`), exactly
like round-3A's reuse lookup -- no test in this package ever touches the
network; fixtures live under
[`ci_lint/tests/fixtures/runtime/cache/`](../ci_lint/tests/fixtures/runtime/cache/)
(one, `caches-live-template.json`, copied verbatim from `gh api
repos/zackees/template-python-rust-cmd/actions/caches`; the others are
small synthetic API-shaped JSON built to exercise a rule the live repo
doesn't currently exhibit -- each file's own `"_note"` key says which).

### Family resolution table

`ci_lint.cache.families.EXTERNAL_FAMILY_SHAPES` -- every `via` value a
`[cache.family.<id>]` may declare, and the literal GitHub Actions
cache-key prefix it resolves to. Each row is cited against the exact line
of that action's own source that builds it (`setup-soldr:*` from the
reference clone named in the round-4A brief, verified 2026-09-28;
`setup-uv` read read-only via `gh api repos/astral-sh/setup-uv/contents/...`,
verified 2026-09-29 -- both actions are READ-ONLY for this worker); `via =
"ci-lint"` is this package's own convention (no external source to cite),
and `delta` is the PR-delta wrapper (`ci_lint.cache.keys.build_delta_key`),
not a `[cache.family].via` value at all.

| `via` | Key prefix | Source |
| --- | --- | --- |
| `setup-soldr:build-cache` | `setup-soldr-buildcache-v2-` | `resolve-setup.ts:829` |
| `setup-soldr:cargo-registry` | `setup-soldr-cargoregistry-v1-` | `resolve-setup.ts:1055` (v2 only if `cargoRegistryArchiveFormat()` resolves `"soldr-v2"`; v1 is what template-python-rust-cmd's main uses today) |
| `setup-soldr:cook` | `cook-base-v2-` | `cook-cache.ts:247` |
| `setup-soldr:cook-delta` | `cook-delta-v2-` | `cook-cache.ts:248` (retired fleet-wide, setup-soldr#533; declared only so a `[cache].retired` entry resolves to a real prefix) |
| `setup-soldr:cross-targets` | `setup-soldr-prepare-v3-` | `blessed-cross-prepare.ts:89` |
| `setup-soldr:dylint` | `setup-soldr-dylint-v2-` | `resolve-setup.ts:1185-1186` (one entry covers the dylint tool/driver/foundation together; v1 when `dylintModeEnabled` is false) |
| `setup-soldr:dylint-output` | `setup-soldr-dylint-output-v2-` | `resolve-setup.ts:1258` (v2 since setup-soldr v0.9.82; list the literal `setup-soldr-dylint-output-v1` in `[cache].retired` to delete the old generation) |
| `setup-soldr:target-cache` | `setup-soldr-targetcache-` | `resolve-setup.ts:965,974` -- `setup-soldr-targetcache-${buildCacheMode}-v2-...`; :957 is the `-off-v1` variant. The prefix is a template, and classification matches by `startswith`, so one entry covers every mode. |
| `setup-soldr:soldr-mini` | `soldr-mini-v2-` | `soldr-mini-cache.ts:85` |
| `setup-soldr:solo-toolchain` | `solo-toolchain-v3-` | `solo-toolchain-cache.ts:276,280` (retired in the template, issue #6 D14; declared only so a `[cache].retired` entry resolves to a real prefix) |
| `setup-uv` | `setup-uv-2-` | astral-sh/setup-uv `src/cache/restore-cache.ts:12,105` -- `CACHE_VERSION = "2"`, `` `setup-uv-${CACHE_VERSION}-${getArch()}-${platform}-${osNameVersion}-${version}${pruned}${python}${cacheDependencyPathHash}${suffix}` ``. Also accepts the legacy `setup-uv-1-` generation that setup-uv v6 (e.g. `d0d8abe`, `CACHE_VERSION = "1"`) still writes -- classification only; ci.yml#87. See "astral-sh/setup-uv's own cache" below. |
| `ccache` / `zccache` | `<family-id>-v1-` | C/C++ compiler object caches (`CCACHE_DIR`/`ZCCACHE_DIR`), zackees/ci.yml#393, [policy-cpp.md](policy-cpp.md). Neither tool writes GitHub cache keys, so the key is ci-lint's own convention, built from the family's `key` components exactly like `via = "ci-lint"`; saved through the sanctioned `actions/cache` wrapper on `[cache].write-on` flows only (`CACHE-008` rule 2). |
| `ci-lint` | `<family-id>-v1-` | this package (`ci_lint.cache.keys.build_family_key`); the round-4A brief writes this generically as `ci-lint:<family> -> <family>-v1-`, and `via = "ci-lint"` (the literal value schema-3 has always accepted, no colon) is treated as that same convention -- a round-4A decision, not a second `via` spelling |
| *(delta wrapper, not a `via` value)* | `delta-v1-pr-<N>-<family>-<platform>-b<base8>` | `ci_lint.cache.keys.build_delta_key` |

Round-4A added `setup-soldr:soldr-mini` and `setup-soldr:dylint` to the
allowlist and to `examples/rust-pypi-app/ci.toml`'s `[cache.family]`: the
live template repo saves `soldr-mini-v2-...` today, undeclared -- exactly
the "undeclared-but-saved family is `CACHE-001`" case the brief predicted
(reproduced in `ci_lint/tests/test_cache_audit.py`'s
`ClassifyLiveTemplateTest` before/after the two families were added).

#### astral-sh/setup-uv's own cache (round-4A amendment)

The live audit's first run flagged `setup-uv-2-...` as `CACHE-001`
(undeclared) on every push -- an owner decision then reclassified it as a
**real, declarable family**, not a permanent finding: `examples/rust-pypi
-app/ci.toml`'s `uv` family is now `via = "setup-uv"` (kept `max`/`per`
unchanged; dropped its old `key` array, since a non-`"ci-lint"` family's
real key is never ci-lint-built -- see `build_family_key`'s docstring,
which now refuses to append `.key` components onto an external prefix at
all, rather than silently fabricating a key shape that action never
produces).

After its fixed `setup-uv-2-` prefix, the key encodes (all from
astral-sh/setup-uv's own source): CPU arch (`getArch()`,
`src/utils/platforms.ts:18-31`, e.g. `x86_64`/`aarch64`) + a Rust-style
platform triple (`getPlatform()`:34-47, e.g. `unknown-linux-gnu` --
resolving musl vs glibc on Linux -- `apple-darwin`, `pc-windows-msvc`) + OS
name/version (`getOSNameVersion()`:86-103, e.g. `ubuntu-24.04`,
`macos-15`, `windows-2025`) + the resolved Python version + an optional
`-pruned` (the `prune-cache` input) + an optional `-py` (the `cache-python`
input) + a sha256 hash of every file the `cache-dependency-glob` input
matches (`restore-cache.ts:79-96`, via `hashFiles()` -- covers `uv.lock` by
that input's own default glob) + an optional user `cache-suffix`. This
means its live cardinality is per **(arch, platform, OS version, python
version)**, which can exceed a family declared `per = "os"` (3 groups) --
`template-python-rust-cmd` showed 6+ distinct `setup-uv-2-...` keys across
its 6 declared platforms on 2026-09-29. The amendment says "keep max/per",
so this is left as an open question for a future round rather than changed
here.

**The template must set its own `save-cache` input, not leave it at
`"auto"`.** Read from `astral-sh/setup-uv`'s `action.yml` (verified
2026-09-29): `enable-cache` defaults `"auto"` (caching on except for
`release`/tag-push/`pull_request_target`/`workflow_run` -- **`pull_request`
itself stays enabled**), and `save-cache` defaults `"auto"`, which "disables
saving for `merge_group` events" only -- a normal PR push is **not**
excluded. Left at its defaults, `astral-sh/setup-uv` would save on every
PR, exactly the base-layer-on-a-PR problem `CACHE-003` exists to catch (and
does: any `uv`-family entry the live audit finds on a `refs/pull/*` ref is
reported as `CACHE-003`, the identical rule a `setup-soldr:*` base layer
gets -- `ci_lint.cache.audit._check_cache_003` has no special case per
family, it fires for any non-delta, non-retired declared family's entry on
a non-default ref). The template's own `astral-sh/setup-uv` step must set
`save-cache` to a writer-flow-only condition (e.g. tied to the same ref
check a `cache = "write"` flow uses), not the bare `"auto"` default.

### `ci-lint cache key`

`ci-lint cache key <family> --repo . [--platform ID] [--pr N] [--base-key
K]` -- two distinct things depending on `--pr`:

- **Without `--pr`:** builds a `via = "ci-lint"` family's own base key from
  its declared `[cache.family.<id>].key` components (`"os"` ->
  `--platform`'s id, `"python"` -> `[python].pythons[0]`, anything else ->
  the first 16 hex chars of that repo-relative file's sha256), e.g. a
  hypothetical `mycache-v1-linux-x64-3.11-<16-hex>` for a
  `via = "ci-lint"` family declared `[cache.family.mycache]`. An external
  family (any `setup-soldr:*` shape, or `setup-uv`) has no `key` components
  to build from -- that action computes its real key itself, per issue #6
  §6's "two builders" rule -- so a `.key` array declared on one is ignored
  here (never appended onto its prefix, which would fabricate a key shape
  that action doesn't actually produce); `cache key` still resolves and
  returns its bare prefix, useful for e.g. `cache heal`'s exact-key
  deletes.
- **With `--pr N` (also needs `--platform` and `--base-key`):** builds the
  PR delta key `delta-v1-pr-<N>-<family>-<platform>-b<base8>`, where `base8`
  is the first 8 hex chars of `sha256(--base-key)` -- `--base-key` is the
  literal restored base cache's key string (whatever built it: setup-soldr
  or `ci-lint`), so a delta always self-heals the moment its base moves
  (issue #6 §6: "A delta whose `b<hash>` doesn't match the restored base is
  discarded").

Exit 2 on bad input (undeclared family, missing `--platform`/`--base-key`
with `--pr`, a missing key-component file).

### `ci-lint cache save-ok`

`ci-lint cache save-ok <family> --repo . --flow F --event E [--pr N]
[--fork] [--act] [--build-ok|--build-failed] [--exact-hit] [--new-units K]
[--payload-bytes B] [--base-key K --current-base-key K2]
[--lockfile-changed] [--tags "..."] [--rerun-saved] [--path P]... [--json]` evaluates
issue #6 §6's "when we do NOT save" table (`ci_lint.cache.save_ok
.evaluate_save_ok`), rule ID `CACHE-008`, **in order** -- the first rule
that applies wins:

| # | Fires when | CLI input |
| --- | --- | --- |
| 1 | fork or act | `--fork` / `--act` |
| 2 | a base-layer save (`--pr` absent) on a flow not in `[cache].write-on` | `--flow`, `[cache].write-on` |
| 3 | the family is in `[cache].retired` | (its own id) |
| 4 | the build step failed/cancelled (a test failure alone never blocks a save) | `--build-failed` |
| 5 | exact key hit, or fewer than the minimum new compile units (a fixed default of 1 -- ci.toml has no declared `-save-min-compiles` field) | `--exact-hit` / `--new-units` |
| 6 | payload below the family's `min`, or above its `max` | `--payload-bytes`, `[cache.family.<id>].min`/`.max` |
| 7 | (`--pr` given) the delta's restored base != the current base | `--base-key` != `--current-base-key` |
| 8 | (`--pr` given) the delta exceeds `[cache.pr].max-per-pr` | `--payload-bytes`, `[cache.pr].max-per-pr` |
| 9 | (`--pr` given) `--lockfile-changed` without a `[ci-cache-save]` tag | `--tags` |
| 10 | a re-run that already saved this exact key | `--rerun-saved` |
| 11 | a path the save would archive is a CMake build tree (`build/`, `CMakeFiles/`, `CMakeCache.txt`, ...); save the ccache/zccache object store instead (#393, [policy-cpp.md](policy-cpp.md)) | `--path` (repeatable) |

Prints `save: yes`, or `save: no (rule <n>: <reason>)`; exits 0 either way
(the reason is always in stdout/`--json`, never only inferred from the exit
code), exit 2 on bad input (family neither declared nor retired). Rule 8's
"...or the PR budget is still full after trimming closed PRs and evicting
by LRU" half needs live cache-account state across every open PR, which a
single `save-ok` call does not have -- that half is enforced by `cache
audit`/`ops` instead (a round-4A decision). `--tags` accepts either a raw
PR-title-shaped string (`"[ci-cache-save]"`) or a bare list
(`"ci-cache-save"`).

### `ci-lint cache audit` (live, read-only)

`ci-lint cache audit --repo . [--default-branch <branch>] [--json]` --
`actions: read`, `GITHUB_TOKEN` + `GITHUB_REPOSITORY`. Lists every cache
(`GET /repos/{repo}/actions/caches`, paginated) and classifies each by
`[cache.family]` prefix (`ci_lint.cache.audit.classify`):

**Default branch is inferred, never guessed (#425).** `--default-branch`
no longer defaults to `main`. Without it the audit resolves the branch
itself: the Actions event payload (`$GITHUB_EVENT_PATH` ->
`repository.default_branch`, zero API calls), then one read-only
`GET /repos/{repo}`. If neither resolves, `CACHE-003`, `CACHE-004`'s
live-excess check and `RUST-004` are skipped behind a single
`needs_review` finding -- never a guessed one (obs-studio's `master`
drew 33 false CACHE-003 under the old `main` assumption). `cache trim`,
`cache janitor`, `cache preprune` and `precheck --live` resolve the same
way, so an unresolvable branch means the janitor plans no CACHE-003
deletion at all (fail closed). `cache budget` and `ci-lint audit` keep
their own explicit `--default-branch` defaults.

**Run it on a schedule (#425).** A repository with `[cache].budget` or
family `max` values runs `cache audit` from its scheduled/default-branch
job so `CACHE-004` drift surfaces between on-demand audits; every
`cache janitor` run also reports live `CACHE-004` family drift as a
warning naming the measured size, before it plans any eviction (drift is
reported, never swept -- `CACHE-004` is not in the janitor's rule set).
See policy-general.md's "Cache policy".

| Rule | Fires on |
| --- | --- |
| `CACHE-001` | a key matching no declared family prefix (and not a delta, not retired, not a ci-lint attestation side entry: `att1-*` is a known ci-lint family, #412) |
| `CACHE-003` | a declared **base**-layer family's key saved on a ref other than `refs/heads/<default branch>` (inferred when `--default-branch` is omitted, #425; unresolvable -> skipped as one `needs_review`, never guessed) |
| `CACHE-005` | an entry below its family's declared `min`, else <= 1KB -- except `att1-*` attestation side entries, which are tiny by design and never judged by the floor (#412; a declared `min` still applies) |
| `CACHE-006` | >= 2 entries of the same family whose keys differ only in a trailing lockfile/version hash (best-effort: strips trailing hyphen-hex segments and groups what's left; a `setup-soldr-cargoregistry-` key keeps its trailing toolchain digest in the shape and only its Cargo.lock hash is a generation, since per-job digests are live at once -- ci.yml#88) -- all but the most-recently-accessed are flagged |
| `CACHE-008` | an entry of a closed/merged PR -- its key carries a delimited `pr-<N>` component (#23 §5; the legacy `delta-v1-pr<N>-...` form is still recognized) or it was saved on `refs/pull/<N>/merge` -- or a PR delta whose PR is closed/merged (one GraphQL query, all referenced PR numbers batched via aliased fields), or whose `b<base8>` matches none of the family's live base entries' own key hashes |
| `CACHE-009` | a key matching a `[cache].retired` prefix |
| `RUST-004` (round-6E) | a declared `via = "setup-soldr:dylint-output"`/`"setup-soldr:dylint"` family with NO live entry at all on `refs/heads/<--default-branch>`, while the default branch's latest run has a successful `dylint`-named job -- NOT "no entry created since that job's `started_at`": a live run against the real template repo proved that stricter freshness signal false-positives on an entirely healthy "exact hit, skipping save" case (an unrelated commit legitimately reproduces the identical `dylintOutputHash`), so only bare presence is trusted. Only evaluated when a `--fetch` is available (`cache audit`/`precheck --live` always have one; `ci_lint.cache.ops`'s trim/janitor/preprune classify+audit for their own rule subsets, none of which include RUST-004, and so never pass one -- zero extra live calls there) and only when the repo declares one of those two families; silent (no finding, not a pass) when no successful default-branch `dylint` job is found yet, so a repo that has simply never run Dylint successfully is never flagged |
| `CACHE-004` | (appended, not a live-only rule) total live bytes >= 90% of `[cache].budget`; (ci.yml#139, live) a declared family's default-branch entries exceed its model -- more than `per`'s cardinality (x2 when `lockfile = true`) or any entry above `max` -- a violation when the family's live bytes exceed its declared worst case, else `needs_review`. |

A PR delta's family/platform are parsed against `ci.toml`'s own declared
platform ids (`_split_family_platform`), not a hyphen-blind regex --
`compile`/`linux-x64` both legitimately contain hyphens, so a naive
`(?P<family>.+)-(?P<platform>[^-]+)-...` mis-splits them.

Exit 1 if any finding, 0 clean (`cache audit`'s own findings are all
`violation` status -- it is `precheck --live`, below, that downgrades all
but `CACHE-009` to a warning).

### `ci-lint cache save-check` (runtime, from `--log` evidence)

`ci-lint cache save-check --log <file> [--log <file> ...] --conclusion
<success|failure|...> [--run-url <url>] [--unusable-threshold 2] [--json]`
-- issue #7: `CACHE-010`'s static kill-switch check has no visibility into
whether a run actually saved anything, so these two rules read real job-log
evidence instead (`gh run view --job <id> --log`, or any captured stdout of
the same -- same "no live network assumed, inject `--log`" convention as
`ci-lint dylint coverage`/`ci-lint suite check`):

The reader accepts bare action stdout and GitHub CLI logs with tab-delimited
job/step fields, ISO timestamps, and setup-soldr's elapsed-time prefixes.
These wrappers are removed before matching the layer's evidence.

| Rule | Fires on |
| --- | --- |
| `CACHE-011` | `--conclusion` is exactly `success`, and the log shows a declared layer's save skipped for a reason other than an exact hit (`<layer>: ... skipping save`, `final <layer> session stats: missing`), or a `final ... summary: ...` line reporting `saved id=-1`. Never fires on a non-`success` conclusion -- a failed/cancelled build legitimately skips saving. |
| `CACHE-012` | `--log` files given OLDEST-FIRST show the SAME layer's restore reporting `produced an unusable payload: archive=<N>B extracted_files=0 extracted_bytes=0` on `--unusable-threshold` (default 2) or more CONSECUTIVE logs, with no successful save of that layer in between. |

Pure filesystem -- no `GITHUB_TOKEN`/`GITHUB_REPOSITORY` needed (unlike
`cache audit`'s live listing); the caller supplies the run's actual
conclusion since ci-lint cannot infer it from a log body alone. RED
evidence: zackees/clud `main` run `36463271709`, job `109067163056`
(`CACHE-011` -- conclusion `success`, both Dylint layers skip their save,
`saved id=-1`) and PR run `36467947289`'s restore (`CACHE-012` -- `archive=22B
extracted_files=0 extracted_bytes=0`, reproduced on repeated pushes since
the writer that should replace it is the same broken one `CACHE-011`
names). Exit 1 if any finding, 2 if a `--log` file is unreadable, 0 clean.

### `ci-lint cache trim` / `janitor` / `heal` / `preprune` (live, read-write)

On GitHub, all four need `actions: write`; `--dry-run` (or omitting a delete
function) never calls `DELETE` -- it only prints the plan.

| Command | Deletes | Extra output |
| --- | --- | --- |
| `cache trim [--max-deletes N] [--dry-run]` | `CACHE-008` entries only (closed/merged + stale-base PR deltas) | -- |
| `cache janitor [--dry-run] [--max-deletes N] [--stale-days D=5]` | (#23 §4) every entry of a closed/merged PR (by `pr-<N>` key component or `refs/pull/<N>/merge` ref; a failed PR lookup keeps them -- fail safe); `CACHE-001/003/005/006/009` entries (`CACHE-006` keeps the newest entry per key prefix); an `evict = "lru"` family's overflow beyond its footprint; anything not accessed in >= `D` days. **Safety:** never an entry created within `JANITOR_GRACE_SECONDS` (10 minutes), and never the newest base entry of a family/key shape that a required job restores (unless it is poisoned, `CACHE-005`, or retired, `CACHE-009`). | a before/after table (count, bytes per family label) |
| `cache heal --key K [--ref R] [--dry-run]` | exactly `key` (`DELETE .../actions/caches?key=<key>`, no id lookup) -- a writer flow's self-heal after an unusable restore | -- |
| `cache preprune --lockfile-changed [--max-deletes N] [--dry-run]` | the superseded (`CACHE-006`) entries of `lockfile = true` families, but only when the forecast is over budget | the forecast (same steady + lockfile-peak + PR-budget arithmetic as `CACHE-004`, but using each family's live max observed size where one exists, else its declared `max`) |

Under act2 (`ACT=true`), the same `cache heal --key K` command instead uses
the local `ACTIONS_CACHE_URL` namespace's `cache-exact-delete-v1` API. It
deletes completed versions of the exact key and reports actual entry count
and reclaimed archive bytes; other keys and in-flight reservations remain.
It uses no GitHub credential, rejects `--ref` (the local namespace has no
GitHub ref filter), and never falls back to GitHub. `--dry-run` sends no
delete. Missing support, an unavailable service or ambiguous accounting
fails explicitly, with no claimed deletion. The endpoint must be a local
literal IP with the cache server's token path; redirects are refused.

This shared client and act2 server extension were each tested against the
same wire contract for issue #362. Public provider release and Template's actual
cleanup qualification remain pending; this is not yet pilot completion.

### `ci-lint cache budget` (live, read-only verdict)

The `cache-budget` job of `ci-pre.yml` (#23 §6), evaluated independently
of the janitor. `--event-name`/`--ref` default to `$GITHUB_EVENT_NAME`/
`$GITHUB_REF`; `--pr` defaults to the event payload's
`pull_request.number`.

| Context | Over `[cache].budget` | Exit |
| --- | --- | --- |
| `push` to the default branch, `schedule`, `workflow_dispatch` | hard failure | 1 |
| `pull_request` | warn-only, **except** the PR's own entries (`pr-<N>` key component or `refs/pull/<N>/merge`) exceed `[cache.pr].budget`, or the account would be under budget without them (its own additions caused the breach) -- then a failure | 0 / 1 |
| `push` to any other branch | warn-only | 0 |
| cache listing failed | warn-only (never fail on unread state) | 0 |

Never deletes anything unclassified, and never on a fork PR (the CLI
requires `GITHUB_TOKEN`/`GITHUB_REPOSITORY`, which a fork PR's default
token does not carry the scope for in the first place).

### `ci-lint cache delta manifest|pack|apply`

Pure filesystem (stdlib `tarfile`/`hashlib`/`json`), no network, no
`ci.toml` -- the PR-delta packing/unpacking mechanics behind issue #6 §6's
"PR caches: a small delta, never a base".

- `cache delta manifest --dir D --out M` -- a schema-2 sorted list of
  `(relpath, size, sha256)` content identities plus a digest of that list.
  The command streams each file to hash its contents. Same-size metadata
  and generation-pointer updates are significant; file size alone is not
  an identity. Legacy size-only manifests must be regenerated.
- `cache delta pack --dir D --base-manifest M --out T --family F --platform
  P --pr N` -- packs only the files under `D` absent from `M` or present
  with different contents into `T` (`.tar.gz`), whose first member is a small
  JSON header (`base_digest`, `family`, `platform`, `pr`).
- `cache delta apply --dir D --delta T --base-manifest M` -- verifies the
  header's `base_digest` equals `M`'s digest; on a mismatch, refuses with
  **exit 3** ("stale base, treat as miss" -- issue #6 §6's self-heal).
  The digest includes the manifest schema, so old size-only delta headers
  also miss, including for an empty base. On a match, extracts every
  non-header member into `D`, overlaying
  whatever base restore is already there. Exit 2 on any other bad input
  (missing dir/manifest/delta file, malformed header).

### `ci-lint precheck --live`

`--live` (default off; the template's precheck turns it on) additionally
runs `cache audit` and folds its findings into the precheck report as
**warnings only** -- `CACHE-009` (a retired family actually present in the
live account) is the one exception, which still fails, matching issue #6
§9's precheck (live) row: "Warns only ... except CACHE-009". Missing
`GITHUB_TOKEN`/`GITHUB_REPOSITORY`, or a live-audit error, degrades to one
`needs_review` finding (never a crash, never a silent pass) -- the same
pattern round-3A's reuse lookup uses.

`--local` (without `--live`) keeps reporting `CACHE-005`/`006`/`008` as
explicitly "skipped (local)" `needs_review` findings (`ci_lint.precheck
.LOCAL_SKIPPED_CHECKS`) exactly as before -- `--live` is what actually
covers them now, so passing `--local --live` together drops those three
from the "skipped" list (their real findings appear instead) while leaving
`ACT-001` (the local act cache-store audit -- a different mechanism from
the remote GitHub Actions cache API `--live` covers) as still explicitly
skipped inside `precheck`; it is implemented as its own command instead,
`ci-lint act audit --store-dir <dir>` (round M2-20), run separately against
a real local act `--cache-server-path` directory.

## `[local]`

| Field | Type | Meaning |
| --- | --- | --- |
| `runner` | string | e.g. `"bosn-act"`. Informational in round 1A. |
| `lanes` | array of strings | e.g. `["precheck", "fast", "dylint"]`. Informational in round 1A. |
| `cache` | string | e.g. `"machine"`. Informational in round 1A. |
| `gate` | table | `[local.gate]`, the local gate (GATE-001..004, #166). See below. |

### `[local.gate]` (GATE-001..005, 007; zackees/ci.yml#166, #168, #177)

For direct runner integration, `[gate.replay]` (or `[local.gate.replay]`)
accepts `report-source = "stdout"`. The declared command must output exactly
one terminal JSON document, as `bosn ci run ... --wait --json` does. Ci-lint
captures and verifies that document directly; stderr remains diagnostic output.
The default, `report-source = "file"`, retains the existing
`CI_LINT_GATE_REPLAY_REPORT` file contract. Both use the same strict verifier;
zero exit status alone never proves execution. See the evolving
[CI tool protocol design](designs/ci-tool-protocol.md) for the rollout proof plan.

For gates with lanes, every invocation validates current input/tool keys and
cached-pass freshness, including an already stamped HEAD. An unchanged fresh
repeat skips execution and keeps HEAD unchanged. `--no-cache` reruns the lanes.
Per-gate trailers emitted from reused passes retain the original execution time.

`[gate.replay] qualified = true` derives execution identities from the selected
workflow graph: caller IDs and every literal caller/leaf matrix leg. In this
mode `key` is optional and diagnostic; each `source-job` is declared once.
Omitting `steps` derives every executable step and its name from the source
for each bound caller profile; explicitly empty `steps` is invalid. Explicit
step lists keep their existing checked contract. Derived declarations cannot
supply manual skip lists. Input interpolation may resolve a step name; every
resulting name must be unique and nonempty. The same finite source condition
proof is used by static validation and runtime. Unknown guards leave checks
mandatory. Derived proof additionally binds the declared receipt event and
excludes failure/cancellation diagnostics only while requiring all validation
steps to succeed. Excluded steps still need explicit completed, skipped Main
evidence. The runner must supply matching structured
`identity` components and consistent leaf `job_id`/`matrix` fields. Dynamic,
duplicate or unbounded matrices and missing/duplicate execution identities
reject. This opt-in verifier is implemented; deployed runner qualification and
the adopter pilots remain pending under issue #362.

The one local command the remote quick gate must be a subset of. A
repository without `ci.toml` declares the identical table as `[gate]` in a
repo-root `local-gate.toml`; declaring both is `GATE-001`. Parsed strictly
(unknown keys `CT-001`, wrong types `CT-002`).

| Field | Type | Meaning |
| --- | --- | --- |
| `run` | array of strings (required) | The gate's argv, e.g. `["python3", "ci/local_gate.py"]`. Run from the repository root. |
| `mirrors` | array of `"<workflow file>:<job id>"` | Remote jobs whose `run:` lines must each start with `run` (trailing args such as `--lane lint` and leading `VAR=value` assignments allowed; `set -e…`/`shopt`/`cd` boilerplate and comments ignored). `GATE-001`. Empty is `needs_review`. |
| `setup-steps` | array of strings | Step names in a mirrored job that only prepare the environment (e.g. `"Enlarge swap (OOM headroom)"`); exempt from `GATE-001`. |
| `verify` | `"<workflow file>:<job id>"` | The PR entry job that runs `ci-lint local-gate verify`; every other job in that workflow must transitively `needs:` it. `GATE-002`. |
| `verify-exempt` | array of strings | Job ids in the verify workflow allowed to run before/without it (e.g. `ci-pre`). |
| `exempt-authors` | array of strings | PR authors verify passes without a trailer. Default `["dependabot[bot]", "renovate[bot]", "github-actions[bot]"]`. |
| `mode` | `"enforce"` (default) or `"shadow"` | `shadow`: verify reports a missing/stale attestation but exits 0. |
| `isolation.marker` | string (env var name) | `GATE-005`: set only by the isolated test environment, e.g. `SOLDR_TEST_ISOLATED`. |
| `isolation.runner` | array of strings | `GATE-005`: how the local gate runs the suite isolated, e.g. `["bosn", "run", "--task", "test"]`. A `bosn` runner's task must exist in `bosn.toml`, and its command or stack Dockerfile (`ENV <marker>=1`) must set the marker; the gate's script (a repository file named in `run`) must invoke the runner. |
| `isolation.guard` | path | `GATE-005`: the file every test process passes through (soldr: its nextest run-wrapper), which must check both `CI` and the marker and refuse otherwise. |
| `isolation.proves-tree` | bool (default false) | `GATE-009`: the gate writes a nonce to `.gate-nonce` and the isolated entry script echoes `gate-nonce: <contents>` from its mounted tree; the gate requires the match. False is `needs_review`. |
| `lanes.<id>.run` | array of strings | `GATE-007`: one lane of the gate, run in declaration order by `local-gate run`. Must be the gate's `run` plus arguments (e.g. `[..., "ci/local_gate.py", "--lane", "lint"]`), so the lanes together are the gate the remote jobs mirror. With no lanes, `run` executes as one command and nothing is cached. |
| `lanes.<id>.exclude` | array of globs | `GATE-007`: paths that cannot affect this lane. A lane's input set is the whole commit tree minus these (never an include list). Mandatory inputs -- the gate declaration, every repository file named in the gate's or lane's argv, lockfiles, toolchain pins -- are re-included whatever the globs say; an exclusion that *names* one literally is a violation. |
| `lanes.<id>.keep` | array of globs | `GATE-007`: re-include paths an `exclude` glob would drop (e.g. exclude `**/*.py`, keep `.github/scripts/nextest*`). |
| `lanes.<id>.tools` | array of strings | `GATE-007`: executables whose `--version` is part of the key, so an upgrade invalidates the cache. Empty is `needs_review`. |
| `lanes.<id>.env` | array of strings | `GATE-007`: environment variables whose values are part of the key. |
| `lanes.<id>.max-age-hours` | int (default 24) | `GATE-007`: a cached pass older than this is ignored. |
| `lanes.<id>.weight` | `"heavy"` (default) or `"light"` | `GATE-007`: `light` lanes (linters, Python tests) run concurrently with each other and alongside the heavy chain; `heavy` lanes (compilers, test suites) run one at a time in declared order. Each lane's output goes to a log under the worktree's git dir (`ci-lint/lane-logs/`), shown on completion -- in full on failure. |
| `lanes.<id>.group` | non-empty string; heavy lanes only (default: one shared group) | `GATE-007` (#399): heavy lanes in different groups run as concurrent chains, each serial in declared order with its own stop-at-first-failure. Only for lanes that share no build directory or other exclusive resource. `group` on a light lane, or an empty group, is a finding. |
| `lanes.<id>.optional` | bool (default false) | `GATE-007`/`GATE-010`: a lane that cannot run on every host (a local VM or emulator, zackees/ci.yml#202). It may exit **75** (EX_TEMPFAIL) to mean "not applicable here". It is then recorded as `<id>:n/a` in the trailer, never cached, never attested (its gates are omitted, so CI runs them), and the gate still passes. GATE-008 accepts `n/a` only for lanes the PR base declares optional. A required lane exiting 75 is a failure. |
| `full-run.receipt` | `"lane-passes-v1"` | `GATE-007`: opt into seeding distinct required lane passes from one successful full gate command. The command writes the exact tree-bound receipt below, or qualified workflow replay derives it from validated execution evidence. |
| `full-run.min-misses` | int (default 2; minimum 2) | Run the full gate command when at least this many lanes miss their caches. With fewer misses, run the individual missing lanes. |
| `trust.mode` | `"never"` (default), `"shadow"`, `"enforce"` | `GATE-008`: whether an attested PR head may stand in for the remote quick-gate jobs. Read from the PR's **base** commit, never the head. `shadow` reports `would_trust` and changes nothing. |
| `trust.skip` | array of `<workflow>:<job>` | `GATE-008`: the remote jobs a trusted head skips. Each needs a job-level `if:` consuming `needs.<verify job>.outputs.trusted`, and its workflow must also trigger on default-branch pushes (the post-merge run), directly or, for an `on: workflow_call` workflow, through a push-triggered caller. |
| `trust.covered-by` | table `"<workflow>:<job>" = [lane ids]` | `GATE-008`: required for a skip job that is not a GATE-001 mirror: the lanes the owner asserts cover what it runs. |
| `trust.surfaces` | array of globs | `GATE-008`: extra paths whose change forces the remote run (e.g. scripts only the skipped jobs execute). Always included: the declaration, files named in the gate's or a lane's argv, their statically resolvable tracked Python imports (transitively, from the base revision), the skip and verify jobs' workflow files, and every local reusable workflow or action they reference. Dynamic imports need explicit surfaces; symlinked modules force every change to run remotely, and an unparseable/unreadable Python helper protects its directory. |
| `trust.full-labels` | array of strings (default `["ci-full"]`) | `GATE-008`: a PR carrying one of these labels always runs remotely. |
| `trust.audit-rate` | int (default 10; 0 = off) | `GATE-008`: 1 in N attested in-policy heads, chosen by a hash of the head SHA, still runs remotely to measure attestation honesty. |

A repository with no `.github/workflows` that declares neither `mirrors`
nor `verify` gets no `GATE-001`/`GATE-002` finding: its pre-push hook is
the whole enforcement (this repository's own `local-gate.toml`).

**Lane cache (GATE-007).** With `[gate.lanes]` declared, `local-gate run` computes each lane's key -- sha256 over the lane argv, its declared tools' versions and env values, and `(path, blob sha)` of its input set, read from `git ls-tree` -- and skips the lane when a pass with that key, younger than `max-age-hours`, is recorded under `<git common dir>/ci-lint/lane-cache/<lane>/` (shared by every worktree of the clone, never pushed). Only passes are recorded. `--no-cache` runs every lane; `ci-lint local-gate lanes` prints each lane's key, input count and hit/miss for HEAD. The trailer gains `lanes=<id>:run|<id>:reused@<key12>,...`.

**Workflow replay evidence (GATE-001, #289).** An optional `[gate.replay]` table adds runner execution proof to the gate. It does not waive the existing no-mirrors finding or establish fleet adoption. Declare `repository = "owner/name"`, `workflow = "ci.yml"`, and the runner `mode = "minimal"` or `"full"`. Each `[[gate.replay.jobs]]` has `source-job = "ci.yml:<job id>"`, the exact `key = "<workflow name>/<job name>"` (local reusable calls prepend each caller's display name, falling back to its job ID when unnamed; bound string inputs may supply that name), `steps = ["<literal executed step name>", ...]`, and `lanes = ["<declared lane id>", ...]`. Every executable check and action must be named and declared; `setup-steps` supplies no replay waiver. Optional `cache-save-steps = ["Save compile cache"]` names a subset of `steps` whose completed, skipped Main section is permitted only after static verification: an official `actions/cache/save` step must use `steps.<restore-id>.outputs.cache-hit != 'true'`, follow exactly one official `actions/cache/restore` with that ID, have the same nonempty `path`, and use `${{ steps.<restore-id>.outputs.cache-primary-key }}` as its key. Neither step may contain `run`. This handles an exact-hit cache-write no-op; it cannot excuse a validation command or establish cache-payload compliance. A missing, failed or cancelled save section still fails proof, and all validation steps still need executed-success evidence. For a minimal PR selection only, `minimal-skip-steps` declares a disjoint subset of `steps` that must have an explicit completed, skipped Main section; executed success, absence, failure and cancellation are rejected. `minimal-mode-step` names the mandatory successful mode producer. Static binding requires a unique earlier unconditional `id: mode` run step with that name, and exactly one of the source guards `steps.mode.outputs.mode == 'full'`, `steps.mode.outputs.mode != 'minimal'`, or `always() && steps.mode.outputs.mode == 'full'`. The exact failure-only PR run-cancellation guard and `gh run cancel ${{ github.run_id }}` command are also recognized on a successful job. Unknown guards never waive checks. This proves exclusion from the selected minimal profile; it establishes no full-mode coverage and cannot be used for full or dispatch replay. Full-mode and native coverage and the base trust policy remain required. Only a single literal informational `echo` in an ordinary shell may be omitted. Job names containing only `${{ inputs.<id> }}` interpolation can be resolved for a literal repository-local call when the declared string inputs have literal caller values, literal defaults, or direct forwarding of a previously bound string input. Explicit empty values override defaults. Missing required inputs, undeclared caller inputs, unresolved expressions, non-string name inputs and general expressions remain unproven; this is identity binding, not expression evaluation or permission to skip a step. Workflow names remain literal. Other dynamic names, matrices and remote reusable calls remain `needs_review`. Literal repository-local reusable calls are expanded for explicit selections: declare the concrete called jobs using `source-job = "check.yml:<job id>"` and keys prefixed by the calling job display name (or ID when unnamed), for example `key = "linux/Check Ubuntu/Check (ubuntu-latest)"`. A virtual reusable caller has no executable steps and is not itself a proof job. Every called job and every caller prerequisite is required; this is not a subset selector. Cycles, missing or unparsed files, non-callable workflows, ambiguous repeated invocations, and expansion exceeding eight nested calls or 512 graph nodes remain unproven.

For a PR replay in either mode, `pr-cache-save-steps` names a disjoint subset of `steps` excluded by a non-PR writer guard. Static binding accepts only an official `actions/cache/save` action with nonempty `path` and `key`, no `run`, and exactly `github.event_name != 'pull_request'` or `github.ref == 'refs/heads/main'`, optionally followed by ` && steps.<id>.outputs.cache-hit != 'true'`. Expression wrappers are allowed; other guards are unknown. Each declared save must have exactly one completed, skipped Main section; successful execution, absence, failure, cancellation and Post-only evidence reject. Dispatch selections cannot declare these exclusions. Unlike an exact-hit save, exclusion follows from the PR event rather than a restored cache, so this concession requires no matching restore and establishes no cache-payload or writer compliance. Validation commands, restores, install steps and general conditional actions remain mandatory.

For explicit local reusable selections, `input-skip-steps` declares a disjoint subset of `steps` excluded by the caller’s statically bound typed inputs. The shared finite condition proof supports input strings/booleans, same-type `==`/`!=`, ASCII case-insensitive string comparisons and `contains`, `&&`/`||` and parentheses. It also binds `steps.<id>.outcome` to `skipped` when the unique earlier producer is itself proven excluded. Expression wrappers are allowed; constants alone, unknown inputs and mixed-type comparisons never establish an exclusion. Every selected caller binding must establish the exclusions. Runtime requires an explicit completed, skipped Main section for each excluded step; execution success, failure, absence and duplicate evidence reject. Every remaining step, including fallback source builds and validation, remains mandatory. This establishes exclusion from the actual selected workflow, not coverage of a branch using a nonempty input.

For lane-specific selections, each `[[gate.replay.selections]]` declares `lane`, either the selected `job` id or `all-jobs = true`, `event = "pull_request"` or `"workflow_dispatch"`, and optional string-valued `inputs = { tier = "test" }`. An optional `workflow = "dylint.yml"` basename selects a different entrypoint for that lane; omission retains `[gate.replay].workflow`. The lane must cover exactly the named job and its recursively resolved `needs` dependencies, or every concrete job and dependency in the explicit whole-workflow selection. `job` and `all-jobs = true` are mutually exclusive; omitting both is invalid. A whole-workflow receipt must carry no job filter, and its workflow must match the lane’s entrypoint exactly. Missing or unreachable job claims are violations; dynamic dependency expressions and cycles cannot establish proof. Runtime input matching is exact, so a minimal-tier receipt cannot satisfy a test-tier expectation. Without selections, the default event is `pull_request`. In qualified mode, a full command may prove the union of all selections when their workflow, event and inputs are identical. Every proof lane needs exactly one selection. The receipt carries no job filter and must prove every selected job and its dependency closure; shared execution identities appear once. Incompatible invocations and legacy multi-selection full commands reject before execution. A narrow lane command still requires that lane’s exact filter.

Before the command, the checker creates a fresh output path in `CI_LINT_GATE_REPLAY_REPORT`. The command writes the original Bosn terminal JSON report there. A zero exit alone cannot pass: the report must bind the exact repository, resolved workspace, HEAD SHA and Git tree, clean source, workflow, selection, event, inputs and runner mode. It requires act2.3 or later, terminal success, zero runner/act exit codes, removed engine, unambiguous jobs, and every declared check completed successfully in a Main section with positive ordered execution sequence numbers -- except source-derived *conditional* steps (#415), whose `if:` reads a runtime-dynamic step output of a step that runs (a pass-marker `cache-hit` lookup): exactly one Main section that either succeeded or explicitly skipped proves them, since a marker hit skips them by design. Missing, duplicate-key, symlink, oversized, malformed, failed, or (for non-conditional checks) skipped evidence rejects before a lane record or attestation is written. Static replay coverage is checked before cached passes or an already-attested early exit. Synthetic fixtures are validation tests, never executed gate evidence.

**Qualified replay full runs.** With qualified replay selections, the checker derives lane passes directly from the validated runner report. The repository command does not write or interpret a separate lane-pass receipt. The gate applies the same complete lane-accounting check before recording passes; an uncovered lane rejects. Shared prerequisites are validated once. Reported per-lane seconds are the full invocation's wall time, not additive job time or CPU time. Reused lanes keep their original records and timestamps even when a full run executes their checks again. This mechanism is fixture-tested; deployed provider and pilot qualification remain pending under #362.

**Execution provider identity (#362).** Qualified replay may declare
`provider-query = ["bosn", "ci", "runners", "list", "--json"]`. The query must
exit successfully within 60 seconds and produce a JSON status object no larger
than 64 KiB with `runners.execution_pins`. These pins use Bosn's existing
`ActPins` fields: `interface_schema = 1`, an act2.11-or-later `act_version`, and
five lowercase SHA-256 digests named `act_binary_digest`,
`engine_manifest_digest`, `engine_config_digest`, `runner_manifest_digest`,
and `runner_config_digest`. Unknown, missing, malformed or duplicate fields
refuse proof. The effective pins enter every lane key, and terminal receipts
must carry exactly the same `execution_pins` and matching `act_version`.
Provider changes during execution or reuse refuse publication. Query scripts
named in the argv are mandatory lane inputs. A gate without lanes reruns when
enrolled, because its old summary trailer does not bind provider identity.
Omitting `provider-query` preserves existing contracts and keys; it does not
qualify their CLI version as proof of the running daemon's pins. The enrolled
mechanism has synthetic regression coverage; real adopter qualification
remains pending under #362.

**Full-run receipts (GATE-007).** Repositories whose full gate runs jobs concurrently may declare `[gate.full-run]` with `receipt = "lane-passes-v1"` and two or more required lanes. On a multi-lane miss, `local-gate run` invokes `[gate].run` once and supplies `CI_LINT_GATE_RECEIPT` (a unique output path), `CI_LINT_GATE_TREE` and `CI_LINT_GATE_HEAD`. The command must check the actual jobs and write `{"version":1,"tree":"<CI_LINT_GATE_TREE>","passes":[{"lane":"<id>","secs":12},...]}` with every declared lane accounted for exactly once. An optional lane that cannot run on this host must appear in `"not-applicable":["<id>",...]` instead of `passes`; only lanes declared `optional = true` may use this field. Unavailable lanes produce `n/a` provenance and no cache record or `Ci-Attestation:` trailer. Missing lanes, duplicate results, and marking a required lane unavailable all fail closed. The checker records no pass unless the command succeeds, the receipt matches the tree and complete lane set, and the tracked tree stays unchanged. The receipt is a claim by the repository's gate command, so that command must validate its runner's structured job results before writing it. A single miss still runs the named lane. `--no-cache` forces a full run when this mode is declared.

**Attested skip (GATE-008).** `ci-lint local-gate verify --trust --github-output` (in the verify job, with full history: `fetch-depth: 0`) writes `trusted`, `would_trust` and `trust_reason`. It is `true` only when **every** check holds: the event is `pull_request`; the base commit's declaration enables `[gate.trust]`; no full label; not a fork; author association is OWNER, MEMBER or COLLABORATOR; the head carries a trailer for its exact tree (not merely an attested first parent); the trailer's lane provenance covers every lane the base declares; no changed path (merge-base diff) matches a surface; and the head is not in the audit sample. Every other case is a reason code, and the jobs run as before. Pushes, schedules and dispatches never skip.

**Attestation.** `Local-Gate: v1 tree=<40-hex tree sha> secs=<n>`, the last
such trailer in the commit message. States: `attested` (tree matches),
`attested-parent` (an unattested merge commit whose first parent is
attested), `stale` (trailer tree differs from the commit's tree), `missing`.

**Commands** (`ci-lint local-gate ...`; exit 0 pass, 1 violation/failure, 2 usage/environment error):

| Command | Does |
| --- | --- |
| `run [--repo .] [--no-stamp] [--force]` | Refuses (2) on uncommitted tracked changes; no-op (0) if HEAD is already attested; runs `run`; exits with the gate's code on failure; fails (1) if the gate changed tracked files or moved HEAD; else amends HEAD's message with the trailer (`git commit --amend --no-verify`, tree unchanged). |
| `verify [--repo .] [--sha S] [--event E] [--author A] [--github-output]` | `GATE-003`. Defaults from `GITHUB_EVENT_PATH`/`GITHUB_EVENT_NAME`. Non-PR events and exempt authors pass. Exit 1 on `missing`/`stale` in `enforce` mode (with a `::error` annotation in Actions). Local workflow replay (`ACT=true`) reports `state=local-replay`, `attested=false`, and, with `--trust`, false trust and per-job skip outputs so checks execute before proof is created. `--github-output` appends `attested=`/`state=`. Needs only git, no token; check out the PR head (`fetch-depth: 2` if merge-from-base commits should resolve via their first parent). |
| `check-push` | pre-push hook body: reads git's stdin, refuses (1) any pushed branch head that is not attested; tags and deletes pass; a repository with no gate declared passes. `CI_LINT_GATE_WIP_PUSH=<reason>` admits unattested heads only (a paused WIP/handoff branch, #406) and names each on stderr; policy and publication problems still refuse, and hosted `verify` still fails the PR. Use it instead of `--no-verify`. |
| `install-hook [--repo .] [--launcher CMD] [--force]` | Writes `$(git rev-parse --git-path hooks)/pre-push`; refuses (1) to replace a hook it did not write unless `--force`. |
| `lint [--repo .]` | `GATE-001`/`GATE-002` for any repository, with or without `ci.toml` (needs PyYAML or `yq`). |
| `first-pass --slug o/r --since ISO [--workflow ci.yml] [--target 0.8] [--min-prs 5] [--json]` | `GATE-004`, live GET-only (`GITHUB_TOKEN`/`GH_TOKEN`). `--workflow` is the workflow *file* name, matched against each run's `path`. Reports strict single-head adoption and a separate earliest-head pass/fail/unknown companion. |

JSON preserves the existing `rate`, `prs`, target and GATE-004 denominator. The new `first_head.summary` contains `passed`, `failed`, `unknown`, the pass rate among known outcomes (`known_rate`), evidence coverage (`coverage`) and observed earliest-head attestation counts. `first_head.prs` includes every scanned PR, including PRs without matching runs, with its head SHA, outcome and reason, plus the earliest observed workflow-run cohort's timestamp, run IDs and size. Same-head timestamp ties stay together; later duplicate triggers do not change the initial outcome. A known-outcome rate of 100% with unknown PRs does not establish 100% CI success. Overwritten first-attempt history stays unknown without additional attempt evidence; this command makes no extra API calls to reconstruct it. Historical reports without `first_head` have no companion evidence.

With `ci.toml`, `precheck` runs `GATE-001`/`GATE-002` as group 17.

**Running without a checkout of this repository.** `pyproject.toml`
packages `ci_lint` (still stdlib-only; `ci-lint[yaml]` adds PyYAML):

    uvx --from git+https://github.com/zackees/ci.yml@<sha> ci-lint local-gate run
    uvx --from 'ci-lint[yaml] @ git+https://github.com/zackees/ci.yml@<sha>' ci-lint local-gate lint

## `[allow]`

| Field | Type | Meaning |
| --- | --- | --- |
| `workflows` | table of string -> array of strings | Every workflow filename this repository may have, mapped to its allowed triggers (`"pull_request"`, `"push:main"`, `"schedule"`, `"workflow_dispatch"`, `"workflow_call"`). `GEN-008`. |
| `actions` | array of strings | Allowed `owner/repo` action slugs. Every `uses:` (in a workflow or a composite action) must resolve to one of these, pinned to a 40-hex commit SHA. `SEC-004`. |
| `setup-soldr.only-in` | string | The one directory allowed to call `zackees/setup-soldr` directly (a composite-action wrapper). `CACHE-009`. |
| `setup-soldr.require` | table of string -> string | Inputs that wrapper's `zackees/setup-soldr` step must set, and to what value. A value of the special string `"plan"` (round-4B) means the actual input must instead be an expression derived from the precheck plan (contains `needs.precheck.outputs.`, or passes through an `inputs.*` value inside the composite) -- used for `save-cache`, so a save only fires when `plan.cache_mode == "write"`. `CACHE-009`. |
| `tools` | array of strings | Allowed bare tool names outside the `TOOL-001` banned list. Informational in round 1A. |
| `secrets` | array of strings | Allowed additional `secrets.*` references beyond `secrets.GITHUB_TOKEN`; `SEC-001` does not report a declared name (ci.yml#135), while the live `SEC-005` audit still reports the stored secret against the OIDC-only policy. Empty in the canonical example (OIDC-only, no repository secrets). |
| `platform-selector` | string | The one file allowed to contain a host `cfg`/`sys.platform` selector. `LAYOUT-001`. |
| `platform-code` | array of globs | Paths (globs; `**` supported) allowed to contain host selectors. `LAYOUT-001`. |
| `permissions` | table of string -> array of strings (round-4B) | Maps one non-free permission grant, written `"<permission>: <value>"` (e.g. `"actions: write"`), to the job ids allowed to hold it. A top-level `permissions:` block may hold at most `contents: read` + `actions: read` (free, no entry needed); any job may hold those two for free as well, but any wider grant must list that job's id here. `id-token: write` additionally must be on an OIDC publisher: the `publish` job with `environment:` set to `[publish].pypi.environment` (default `pypi`), or a crates.io trusted-publishing job that runs `rust-lang/crates-io-auth-action` under an `environment:` (ci.yml#134), allowlisted or not. `SEC-002`. |
| `cache-actions.only-in` | string (default `.github/actions/cache`) | The one directory allowed to call `actions/cache`/`actions/cache/save`/`actions/cache/restore` directly (a SHA-pinned composite-action wrapper). `CACHE-001`. Inside it, the wrapper's `key:` input must itself be a dynamic expression (a prior step's output, or an `inputs.*` passthrough), never a literal. `CACHE-002`. |
| `setup-uv.require` | table of string -> string (round-4B) | Inputs every `astral-sh/setup-uv` use (anywhere -- unlike setup-soldr, there is no single wrapper location) must set, and to what value; `"plan"` works exactly like `setup-soldr.require`'s special value, above. Used for `save-cache`; missing or a literal `"true"` is `CACHE-003` (both save unconditionally, including on PRs -- see "astral-sh/setup-uv's own cache" below). |

## `[publish]`

| Field | Type | Meaning |
| --- | --- | --- |
| `pypi.auth` | string | Must be `"oidc"` for the `rust-pypi-app` profile. |
| `pypi.environment` | string | e.g. `"pypi"`. The PyPI job allowed `id-token: write` must be named `publish` and set this as its `environment` (a crates.io trusted-publishing job is the other accepted `id-token: write` holder -- see `SEC-002`). `SEC-002`. |
| `pypi.mode` | string | `"mock"` in the template: proves the OIDC identity, never uploads. The planner maps a flow's `publish = "pypi"` through this value. |

## `[[exceptions]]`

An array of tables, one per declared exception.

| Field | Type | Meaning |
| --- | --- | --- |
| `rule` | string | The rule ID this exception covers, e.g. `"GEN-005"`. |
| `path` | string | The exact finding path this exception matches (e.g. `"ci.sh"`). Matching is `(rule, path)` equality. |
| `reason` | string | Free text; must justify the deviation, never "skip the check". |
| `issue` | string (URL) | The tracking issue for removing the exception. |
| `expires` | string (`YYYY-MM-DD`) | Once past, the exception no longer suppresses its finding (which reappears as a normal violation), and the expired exception itself becomes a violation: `CT-005`. |

A finding matching a non-expired exception is printed with status
`approved_exception` on every run (never silent); it does not count toward
precheck's exit code.

## `[reuse.default-branch]` (GEN-021 phase 2, zackees/ci.yml#158)

Declares how a **default-branch push** may skip a required job because a `pull_request` run already proved a tree-identical tree. This is the consumer half of `ci-lint reuse-check` (Phase 0, #156): without it the decision is computed and then thrown away. Background and evidence: [designs/default-branch-verified-reuse.md](designs/default-branch-verified-reuse.md), [case study](case-studies/clud-main-push-ci-burn.md).

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `"off"` \| `"shadow"` \| `"enforce"` | `"off"` | `shadow` computes and reports the decision but never skips; `enforce` skips. `off` disables the mechanism. Promote to `enforce` only on `ci-lint reuse-report` evidence (`>= 12` decisive runs, 0 false-reuse candidates). |
| `workflows` | array of string | `["ci.yml"]` | Workflow files whose `pull_request` runs can prove a tree. Non-empty; each must be a key of `[allow].workflows`, else `CT-002` (a decision reading a workflow the repository does not ship would silently prove nothing). |
| `required-jobs` | `"plan"` or array of string | `"plan"` | Exact **job display names** the proving run must have green. `"plan"` means the list is the plan's main-flow lane digests, not a hand-maintained array. |
| `exempt-jobs` | array of string | `[]` | Job ids that never skip on reuse (cache writers -- see the design's section 8 cache-writer strategies). |
| `max-age-hours` | number | `24` | Every proving job must have completed within this many hours of the decision. `0 < x <= 168`. |
| `nightly` | bool | `true` | A scheduled full default-branch run exists and is the drift backstop. Required in `enforce`. |

An absent table -- or a bare `[reuse]` with no `default-branch` sub-table -- is `mode = "off"`, so every `ci.toml` that predates this one loads unchanged and skips nothing. Parsed by `ci_lint.schema._parse_reuse` into `CiToml.reuse: ReuseConfig` (frozen); unknown keys are `CT-001` like every other table, bad values `CT-002`.

### Consuming the decision: `ci-lint gate --default-branch-reuse`

```
python3 -m ci_lint gate --plan plan.json --needs needs.json --default-branch-reuse reuse-check.json
```

This is a **separate** mechanism from `ci-lint gate --reuse`, which proves a lane of the same pull request from an earlier attempt of its own head. A skipped required job counts as success only when every one of these holds:

1. the document is schema 1, `reuse == true`, `event_name == "push"`, and `sha` equals `$GITHUB_SHA` (a shadow document, which sets `reuse = false` and `would_reuse = true`, is never evidence);
2. it names a proving job whose display name is this job's id, or its plan-derived `<id> [<lane digest>]` form -- so a lane digest mismatch is what makes a stale proof fail;
3. a live `GET /repos/{r}/actions/jobs/{job_id}` still returns `conclusion == "success"` and `head_sha` equal to the document's `pr_head_sha`.

Anything unverifiable (no `GITHUB_TOKEN`, unreachable API, a job with no id to re-fetch) is `needs_review`, never a silent pass; any mismatch is the pre-existing `TEST-001` "a skip is not a pass" failure. An accepted job is reported with its provenance -- `reused from PR #<n> run <id>` in the text and `reused_from_pr`/`reused_from_run` in `--json` -- so a green default-branch commit is never an unexplained skip.

## `[fleet]` (round M2-42, zackees/ci.yml#98)

| Field | Type | Meaning |
| --- | --- | --- |
| `sync-issues` | bool (default `false`) | This repository's explicit opt-in for `ci-lint sync-issues --apply` (the central fleet scanner, `ci_lint.fleet`/`ci_lint.sync_issues`) to create, update, and close fingerprinted `ci-lint`-labelled GitHub issues here. Absent table or key both mean `false` -- `--apply` performs zero API calls (not even a GET) against a repository without this key set `true`; `--dry-run` still prints the plan regardless, so an owner can preview before opting in. |

This table is optional (`ci.toml` predating round M2-42 still loads unchanged) and is parsed by `ci_lint.schema._parse_fleet` into `CiToml.fleet: FleetConfig`, so an unrecognized key under `[fleet]` is still `CT-001` like every other table.

### `ci-lint sync-issues --apply` (write path)

```
python3 -m ci_lint sync-issues --apply [--max-writes N] [--owners ...] [--repos ...] [--from-scan scan.json]
```

Requires `GITHUB_TOKEN`/`GH_TOKEN` with `issues: write` on every repository it may touch; cannot be combined with `--offline` (existing issues must be read live) or `--replay` (no write replay support -- use `--dry-run` against a recording instead). Three independent guards, every one of which must hold before a single write happens (see docs/agent-guide.md's "Findings and issue lifecycle" for the full rationale):

1. **Per-repository opt-in**: `[fleet].sync-issues = true`, above.
2. **`--max-writes N`** (default 5): a hard cap on writes (create + update + close, each counted once) per run; actions beyond the cap are reported under `skipped_cap`, not dropped silently -- rerun to continue.
3. **The fingerprint marker**: `update`/`close` only ever act on an issue `fetch_existing` matched by `<!-- ci-lint-fingerprint: <16 hex> -->` in its body, re-verified immediately before every write.

Idempotent: an `unchanged` action (title and body already match) performs no write, so a rerun against the same findings writes nothing; a finding that stops reproducing closes its issue with an explanatory comment (state -> `closed`) rather than deleting it. Exit code is `1` if any write failed (see the `errors` list in `--json`/text output), else `0`.

## Rule catalog

Every finding names its rule ID, a `path:line` when known, what is wrong,
and a concrete fix -- never "add it to the allowlist". A finding's status
is one of `violation`, `approved_exception`, or `needs_review` (a rule that
depends on parsed workflow YAML reports `needs_review`, not a silent pass,
when neither PyYAML nor `yq` is available).

| Rule | Checks | Fix direction |
| --- | --- | --- |
| `CT-001` | Unknown key anywhere in `ci.toml`. | Remove the key, or use the documented field name. |
| `CT-002` | Wrong TOML type, a missing required key, or a dangling/cyclic reference (flow/tag/suite/platform names, `[[exceptions]]` URL/date format). | Fix the value's type or the reference. |
| `CT-003` | `schema` is not `3`. | Set `schema = 3`. |
| `CT-004` | The workflow's checkout of `zackees/ci.yml` does not use the exact SHA in `ci.toml`'s `linter` field (or no such checkout exists). | Align the checkout `ref` (or `linter`) to the intended commit. |
| `CT-005` | A declared `[[exceptions]]` entry is past its `expires` date. | Fix the underlying finding and delete the entry, or renew it after review. |
| `CT-006` | `[platforms].*.target` does not match `Cargo.toml`'s `[workspace.metadata.soldr].targets` (or that table is missing). | Make the two target sets identical. |
| `TAG-001` | A PR-title bracket token looks reserved (`ci-`, `no-test`, `release` prefix) but is not a known tag. | Fix the spelling, or declare the tag in `[tags]`. |
| `TAG-002` | `[release]` combined with any `[no-test*]` tag. | Remove one or the other. |
| `TAG-003` | `on.pull_request.types` omits `edited`. | Add `edited` to the `types` list. |
| `GEN-001` | `.github/workflows/ci.yml` is missing or lacks `on.pull_request`. | Add it as the PR entry point. |
| `GEN-002` | A job with a non-Linux runner has no `if:`/`strategy.matrix` referencing `needs.precheck.outputs`. | Gate the job on the precheck plan's output. |
| `GEN-004` | (round-4B) The repo has Python sources but the `fast` job (directly in a `run:` line, or one level into a tracked `ci/*.py` script it calls) does not invoke `ruff check`, `ruff format --check`, and `pylint`; a standalone `black`/`isort` invocation is also `GEN-004`. An unresolvable script reference (missing file, parse error) is `needs_review`, never a silent pass. | Add the missing Ruff/Pylint invocation(s) to the `fast` job (directly or via a `ci/*.py` script); remove any Black/isort call. |
| `GEN-005` | A `run:` step longer than one line, shell control syntax, a banned `shell:`, or a tracked `.sh/.ps1/.bat/.cmd`/shebang script file. One `cmake -S/-B`/`cmake --preset`, `cmake --build` or `ctest` command may span trailing-backslash continuation lines (#393, [policy-cpp.md](policy-cpp.md)). | Move the logic into a Python script, called as one line. |
| `GEN-008` | A workflow file (or one of its triggers) not declared in `[allow].workflows`, or more than one file declaring `pull_request`. | Declare it in `[allow].workflows`, or remove the trigger/file. |
| `GEN-014` | (#23 §1) A `ci-precheck.yml` workflow file, a `uses:` of one, or an `[allow].workflows` entry naming it. The fast first workflow is `ci-pre.yml`. | Rename it to `ci-pre.yml` (`git mv`), and update `ci.yml`'s `uses:` and `[allow].workflows`. |
| `GEN-015` | (#23 §3) A workflow-level `concurrency:` in `ci-pre.yml` (it would cancel a janitor sweep mid-delete). `WF-002`'s "every workflow has a top-level concurrency" does not apply to `ci-pre.yml`. | Delete it; concurrency in `ci-pre.yml` is per job, and the caller `ci.yml` may keep its own per-ref cancel group. |
| `GEN-016` | (#23 §3) A `ci-pre.yml` job other than `cache-janitor` with `concurrency:`, or with `needs: cache-janitor`. | Remove it: check jobs take no lock and never wait on a queued sweep. |
| `GEN-017` | (#23 §3) The `cache-janitor` job lacks the repo-wide, non-cancelling group. | `concurrency: { group: cache-janitor, cancel-in-progress: false }`. |
| `GEN-018` | (#23 §2) An install step in `ci-pre.yml`: `astral-sh/setup-uv`, `actions/setup-python`, or a `run:` invoking `uv`/`uvx`/`pip`/`pip3`/`pipx`/`python -m pip`. | Use stdlib `python3` only, or move the step into a later `ci.yml` job. |
| `GEN-021` | (#156, static) In a workflow triggered by both `pull_request` and a `push` reaching the default branch: a job-level `if:` conjunct that runs a job on PRs but never on a default-branch push (`github.event_name != 'push'`, `github.event_name == 'pull_request'`, `github.ref != 'refs/heads/<default>'`, `github.ref_name != '<default>'`, `startsWith(github.ref, 'refs/pull/')`) on a job the gate job (`ci-ok`/`gate`, or named `CI OK`) needs, with no `ci-lint reuse-check` decision in the workflow, is a violation. `needs_review`: the same skip beside a reuse decision; no recognizable gate job; an event/ref expression the classifier cannot resolve (`||` mixing PR and non-PR events, `!(...)`, `contains(...)`); a `needs.<X>.outputs.reuse` whose job `<X>` does not run `reuse-check` (directly or in its local reusable workflow); a consumption other than `needs.<X>.outputs.reuse != 'true'`; a literal `--required-job` matching no job display name; a job skipped on reuse that no `--required-job` covers. A job that never runs on a PR (`github.event_name == 'push'`, `github.ref == 'refs/heads/<default>'`, ...) is not a subject. | Run the job on default-branch pushes, or skip it only through `if: needs.<decision job>.outputs.reuse != 'true'` fed by `ci-lint reuse-check` (see "Verified reuse on default-branch pushes" below); a live merge queue validating the exact merge commit (`GEN-010` live half) is the only other justification, recorded as a `[[exceptions]]` entry. |
| `GEN-022` | (#405, static) No workflow triggers on `merge_group`; or, with a nonzero `[cache].budget`, no `merge_group` workflow job runs `cache janitor` as the final job (needs every other job, `always()`, non-cancelling `cache-janitor` group). |
| `SEC-001` | `secrets.<X>` other than `secrets.GITHUB_TOKEN` or a name in `[allow].secrets`, or `secrets: inherit`. | Remove the dependency; this profile is OIDC-only. |
| `SEC-002` | Top-level permissions wider than `contents: read` + `actions: read` (round-4B: `actions: read` is now free at the top level too). Per job: any grant beyond `contents: read`/`actions: read` (free) whose job id is not listed in `[allow].permissions."<permission>: <value>"`; `id-token: write` additionally must be on an OIDC publisher: the `publish` job with `environment:` set to `[publish].pypi.environment` (default `pypi`), or a crates.io trusted-publishing job that runs `rust-lang/crates-io-auth-action` under an `environment:` (ci.yml#134). Checked identically inside a reusable (`workflow_call`) workflow file, by its own job ids. | Narrow the permissions block, or add the job id to `[allow].permissions` for that exact grant. |
| `SEC-003` | `pull_request_target` or `workflow_run` declared anywhere. | Use `pull_request` instead. |
| `SEC-004` | A `uses:` action not in `[allow].actions`, or not pinned to a 40-hex commit SHA. | Allowlist it and/or pin it by SHA. |
| `RUN-001` | A `runs-on` label outside the fleet list, or any `-latest` label. A `runs-on: ${{ matrix.<var>.runs_on }}` job whose `strategy.matrix.<var>` is built from a precheck plan platform-lanes output (`platform_lanes_json`/`platform_lanes_todo_json` -- these enumerate `[platforms]`) is resolved statically the same way: every `[platforms].*.runs-on` is checked, reported against `ci.toml` if bad. Any other dynamic `runs-on` expression is `needs_review`, not a violation. | Use a fleet label: `ubuntu-24.04`, `ubuntu-24.04-arm`, `windows-2025`, `windows-11-arm`, `macos-15`, `macos-15-intel`. |
| `RUN-002` | ci.yml#12: a cross-compiled job's display name must show where it builds and where it runs. Detected on the same platform-lanes matrix signal as `RUN-001`'s matrix case. A leg whose `runs-on` is a STATIC label (it always builds there, e.g. soldr cross-compiling on `ubuntu-24.04` for a Windows/macOS target) but whose `name:` has no `(on <host>...)` annotation is a violation; a leg whose `runs-on` is the dynamic `${{ matrix.<var>.runs_on }}` expression (it executes natively, compiling assumed impossible) but whose `name:` has no `(native...)` annotation is a violation. | Add a `(on <build-host>, <tool>)` annotation to the build leg's `name:` (e.g. `(on ubuntu-24.04, soldr)`) and a `(native...)` annotation to the exec leg's `name:`, so the Checks tab shows both sides of the split without opening the job. |
| `WF-001` | A job has no `timeout-minutes`. | Add one. |
| `WF-002` | A workflow has no top-level `concurrency` (except `ci-pre.yml`, where it is `GEN-015`). | Add one. |
| `WF-003` | `continue-on-error` anywhere. | Remove it; fix or gate the step instead. |
| `TOOL-001` | Bare `cargo`/`rustc`/`rustup`/`cargo-*`/`maturin`/`cross`/`cibuildwheel`/`pip`/`pipx`/`twine`/`curl`/`wget` as a command (in `run:` lines or `ci/*.py`/root `*.py` subprocess literals). | Wrap it through `soldr` or `uv`. |
| `TOOL-002` | A dependency-resolving `cargo` subcommand (`build`/`test`/`check`/`clippy`/`doc`/`run`/`nextest`) without `--locked`; a same-line `# ci-lint: allow TOOL-002 <reason>` on a workflow/composite-action `run:` line excuses that one line (ci.yml#138). | Add `--locked`. |
| `TOOL-003` | In a Rust repo (`pyproject.toml` with `[build-system]`, or `Cargo.toml` with `[workspace]`): a plain `uv run`/`uv sync` (in a `run:` line, `ci/**/*.py` subprocess argv, or a `uv run` shebang) missing `--no-project`/`--no-sync`/`--script`. | Add `--no-project`/`--no-sync`/`--script`, or set job-level `UV_NO_SYNC: "1"`. |
| `CACHE-009` | Static: `zackees/setup-soldr` used outside `[allow].setup-soldr.only-in`, missing/wrong `require` inputs (round-4B: a `require` value of `"plan"` means the actual input must be an expression derived from the precheck plan, not a literal), or an input enabling a retired cache family. Live (round-4A, `cache audit`/`precheck --live`): a live cache entry's key actually matches a `[cache].retired` prefix. | Call it only from the wrapper, with the required inputs; delete a live retired entry (`ci-lint cache janitor`). |
| `CACHE-010` (issue #7) | Static: a workflow/job/step env sets a kill-switch var (`ZCCACHE_DISABLE`, `SOLDR_NO_CACHE`, `SOLDR_CACHE_DISABLE`, `SOLDR_DYLINT_NO_CACHE`) truthy while `ci.toml` declares a `[cache.family]` via a `setup-soldr:*`/`zccache*` backend, or `CCACHE_DISABLE` truthy while it declares a `via = "ccache"` family (#393). | Remove the env var (or set it falsy); if intentional, retire the matching `[cache.family]` entry instead. |
| `CACHE-011` (issue #7) | Runtime, `ci-lint cache save-check --log ... --conclusion success`: a successful run's log shows a declared layer's save skipped for a non-exact-hit reason, or `saved id=-1`. | Inspect the writer job's cache-save gate for that layer (setup-soldr's success-marker identity check, or the invocation path that should write it). |
| `CACHE-012` (issue #7) | Runtime, `ci-lint cache save-check`: a layer's restore reports an unusable (0-extracted) payload on >= 2 consecutive `--log` files with no writer save in between. | Delete the poisoned entry (`ci-lint cache heal`) AND fix the writer flow (see the matching `CACHE-011` finding). |
| `RUST-002` (round-6E) | Dylint invoked from more than one job; a Dylint job whose `runs-on` doesn't resolve to Linux (`needs_review` for an unresolvable matrix/expression); bare `cargo dylint`/`cargo-dylint`/`dylint-link` (not `soldr dylint`/`soldr cargo dylint`) in a `run:` line or one level into a `ci/*.py` script (GEN-004's own follow-one-level convention, reused); `cargo install cargo-dylint`/`dylint-link`; `--workspace` without `--all` on any Dylint-related invocation. | Consolidate into one Linux `dylint` job; wrap every invocation through `soldr dylint`/`soldr cargo dylint`; drop the `cargo install`; add `--all`. |
| `LAYOUT-001` | A host selector (`cfg(...)`, `sys.platform`, ...) outside `[allow].platform-code`/`platform-selector`. | Move it behind the platform facade. |
| `RUST-005` | More integration-test targets exist than are declared in `[rust.tests].binaries`. | Declare each target, or consolidate `tests/*.rs`. |
| `RUST-011` | A private crate without `publish = false` / with `[features]` / with an optional dependency / with `cfg(feature)`; the public crate's `[features]` not of the form `x = ["dep:<private-crate>"]`; or `--all-features`/`cargo hack`/`--feature-powerset`/an out-of-`[rust].ship` `--features` value. | Fix the crate's manifest, or use only a `[rust].ship` feature set. |
| `RUST-012` | An undeclared test target, a declared-but-absent one, or an enabled `<crate>:lib` harness whose crate has zero `#[test]`. | Reconcile `[rust.tests].binaries` with the actual Cargo targets. |
| `RUST-013` | An exact `soldr==` pin in `pyproject.toml`'s `[build-system].requires`, or a literal `version:` input on a `zackees/setup-soldr` step, with no matching `[[exceptions]]` entry. | Float: `soldr>=<floor>` and no `version:` input; or add a `[[exceptions]]` entry recording the reason, owner, and bump path. |
| `PKG-003` | `[project.scripts]`/`[project.gui-scripts]` shadows `[python].cli.name`, or `[tool.soldr.pep517].bundle-bins` omits `[python].cli.crate`. | Remove the Python shim entry; add the crate to `bundle-bins`. |
| `PKG-004` | `pyproject.toml`'s build backend isn't `"soldr"`, `requires` has no `soldr` version requirement (`soldr>=<floor>` by default, or an exact `soldr==` pin per `RUST-013`), maturin appears in build requires/dependency-groups, or `uv.lock` has a non-soldr package depending on maturin. Static precheck applies it only to profiles that ship a native wheel (`rust-pypi-app`); a `rust-library` repo's unrelated pure-Python package may use any backend (ci.yml#90). | Use `soldr` as the sole backend and maturin dependent. |
| `PKG-005` | A `try/except ImportError` around an import of `._native`. | Import it unconditionally so a missing native module fails loudly. |
| `CACHE-001` | Static: a raw `actions/cache` (or any of its `/save`/`/restore` sub-actions) used anywhere OTHER than the one sanctioned wrapper directory `[allow].cache-actions.only-in` (round-4B; default `.github/actions/cache`, still SHA-pinned per `SEC-004`). Live (round-4A): a cache entry whose key matches no declared `[cache.family]` prefix. | Move the call into the wrapper (or declare a family for a live undeclared entry, or stop writing it via `ci-lint cache janitor`). |
| `CACHE-002` | A volatile component (`github.sha`, `github.run_id`, `github.run_number`) in a `key:`/`cache-key-suffix:` input or a `[cache.family].key` entry. Round-4B: inside the sanctioned wrapper, a `key:` on the `actions/cache*` step that is a literal at all (even with no volatile token) -- it must be a step-output or `inputs.*` expression built by `ci_lint cache key`. | Key on `hashFiles(...)` or a date rotation instead; inside the wrapper, build the key from a prior step's output or an `inputs.*` passthrough. |
| `CACHE-003` | (round-4A, live) A declared **base**-layer family's cache entry was saved on a ref other than the default branch. (round-4B, static) `astral-sh/setup-uv`'s `save-cache` input, when `[allow].setup-uv.require` marks it `"plan"`-driven, is missing or a literal `"true"` -- both save unconditionally, including on PRs. | Delete the live entry (`ci-lint cache janitor`); check the writer job's ref condition and `save-ok` call site; set `save-cache` to a plan-derived expression. |
| `CACHE-004` | The proven worst-case cache footprint exceeds `[cache].budget`, or `[cache].budget` exceeds 10GB; (round-4A, live) total live bytes >= 90% of budget; (ci.yml#139, live) a declared family's default-branch entries exceed its model -- more than `per`'s cardinality (x2 when `lockfile = true`) or any entry above `max` -- a violation when the family's live bytes exceed its declared worst case, else `needs_review`. `pre-prune = true` on every writer flow removes only the lockfile-change-peak term from the sum -- it does not waive the rest of the proof; `worst = steady + [cache.pr].budget` still must fit. | Lower family sizes/cardinality, raise the budget (up to 10GB), and/or set `pre-prune = true` on every writer flow (removes the lockfile-peak term only); live, run `ci-lint cache janitor`. |
| `CACHE-005` | (round-4A, live) A cache entry is poisoned: below its family's declared `min`, or <= 1KB when no `min` is declared (`min = "0B"` keeps tiny-by-design entries, #417). | Delete the exact key (`ci-lint cache heal --key <key>`) so the next writer run repopulates it. |
| `CACHE-006` | (round-4A, live) >= 2 entries of the same declared family whose keys differ only in a trailing lockfile/version hash -- the family is superseded, not per-platform-distinct. | Delete the superseded (non-newest) entry (`ci-lint cache janitor`); disambiguate with `[cache.family.<id>].per` if more than one entry is legitimate. |
| `CACHE-008` | (round-4A, live; widened by #23 §4.1) Any entry of a closed/merged PR -- key with a delimited `pr-<N>` component, a legacy `delta-v1-pr<N>-...` key, or ref `refs/pull/<N>/merge` -- or a PR delta whose base hash matches none of that family's live base entries. A `pr-<N>`-keyed entry is PR-scoped, never a `CACHE-003` base layer. | Delete it (`ci-lint cache janitor` deletes closed-PR entries on every push sweep; `ci-lint cache trim` also covers stale-base deltas). |
| `CACHE-013` | (#23 §5, static) A cache save reachable from a PR (a workflow with `pull_request`/`workflow_call`, or a composite action) whose key input lacks a delimited `pr-<N>` component: `zackees/setup-soldr` `cache-key-suffix` (unless `save-cache: "false"`), `astral-sh/setup-uv` `cache-suffix` (unless `enable-cache: false` or `save-cache: false`), `actions/cache`/`actions/cache/save` `key`. Accepted: an expression containing `github.event.pull_request.number`, or the plan output `cache_key_pr` (`pr-<N>` on `pull_request`, empty elsewhere, from `ci-lint plan/precheck --github-output`); inside a composite action, an `inputs.*` passthrough. | Set the key input to `${{ needs.precheck.outputs.cache_key_pr }}` (or a `format('pr-{0}', github.event.pull_request.number)` expression). |
| `CACHE-029` | (static) A `[cache.family.<id>]` declaring `max` >= 256 MB with no `evict = "lru"` -- nothing bounds how many entries the family may hold at once. `max` sizes ONE entry, not the family's footprint, so it cannot catch this. `per` is **not** accepted as an exemption: it declares the writer shape, not the entry count. | Add `evict = "lru"` to the family so `ci-lint cache janitor` keeps it at `max x cardinality`. Measure first with `ci-lint cache audit --repo .` -- CACHE-006 names the superseded entries eating the budget. Declaring `[cache.family]` is also what makes the boundary machine-checkable: kernal-api reclaimed 6.5 GiB with the janitor, while clud and running-process, which declare none, can only be inferred from the key shape. |
| `CACHE-031` | (static, `needs_review`) `[cache.promote] mode = "ancestor"` declared while a `[cache.family.<id>]` of `max` >= 256 MB does not itself declare `promote = "ancestor"` -- its keys carry a content hash, which discards the git-DAG ancestry a nearest-ancestor search needs, so no promotion can select one. Blocked on setup-soldr#552 for setup-soldr-owned families. | Add `promote = "ancestor"` to the family and suffix its key with `ci-lint attest lineage`'s label. Sequence it: a live key-shape change invalidates that family's warm entries. |
| `CACHE-032` | (static, `needs_review`) `[cache.promote] mode = "ancestor"` declared while setup-soldr's ancestor pilot (#566) is merged and unreleased, so a `SEC-004` SHA-pinned setup-soldr cannot reach it. | Confirm the referenced revision's `action.yml` declares `auto-key`. None does today — not a `SEC-004` SHA pin, and not the sanctioned `v0` float, which trails `main` by 33 commits. The pilot must ship in a release (#552). |
| `CACHE-014` | (M2-17, ci.yml#42, static; only fires when the corresponding optional input is set) `[cache.pr].max-per-pr x expected-open-prs > [cache.pr].budget`, and/or `[cache.pr].max-per-pr < [cache.pr].measured-largest-delta`. | Lower `max-per-pr` or `expected-open-prs`, or raise `[cache.pr].budget`; and/or raise `max-per-pr` to at least the measured largest delta -- see "Sizing `[cache.pr]` for repository scale" above. |
| `CACHE-025` | (#209, static; also `ci-lint local-gate lint` and `ci-lint fleet scan`) A workflow or composite-action step `uses: Swatinem/rust-cache@<ref>`. | Delete it. Rust build caching goes through `zackees/setup-soldr@v0` (cache on) or `soldr cargo`/`soldr cook`. There are no exceptions (maintainer decision 2026-10-02): no same-line allow comment, no `[[exceptions]]` entry, not for a soldr bootstrap job or a benchmark baseline (docs/policy-rust.md). |
| `SEC-005` | (round-5, live, `ci-lint audit`) Any repository or `[publish].pypi.environment` environment Actions secret exists; a 403 (needs an admin token) is `needs_review`, never a pass. | This profile is OIDC-only (issue #6 §7): delete the stored secret(s). |
| `SEC-006` | (round-5, live, `ci-lint audit`) `[publish].pypi.environment` is missing, or its deployment branch policy doesn't restrict deploys to the default branch. | Create/restrict the environment's deployment branch policy to exactly the default branch. |
| `SEC-007` | (round-5, live, `ci-lint audit`) The repository's default Actions workflow permissions (`GET .../actions/permissions/workflow`) are not `"read"`. | Set "Read repository contents permission" (never "Read and write") in repo Settings -> Actions -> General. |
| `GEN-006` | (round-5, live, `ci-lint audit`) `required_status_checks.strict = true` on the default branch with no active merge-queue ruleset covering it (docs/case-studies/fbuild-ci-cost.md's candidate rule). | Configure a merge queue, or set `strict = false`. |
| `GEN-011` | (round-5, live, `ci-lint audit`) The default branch's required status checks don't include the gate check name (`--gate-check-name`, default `"CI OK"`); `needs_review` (not a violation) when branch protection is absent entirely -- a policy decision, not a code defect. | Add the gate check name to the branch's required status checks. |
| `PKG-006` | (round-5, `ci-lint release verify`) The staged release artifact set is missing/duplicates/has an extra wheel for a declared platform, is missing/duplicates the sdist, disagrees on version across artifacts, or (with `--smoke`) is missing/has a failing native-install smoke result for a staged wheel. | Stage exactly one wheel per declared platform plus one sdist, all at the same version, each passing its native install smoke. |
| `PKG-001` | (round M2-20, static) The PR-push flow (`[flow.pr]`, or its `extends` chain, or the first declared flow) has no `required = true` suite whose `run` command performs a wheel install smoke. | Add a suite (e.g. `suites.smoke = { run = "ci/wheel.py smoke", required = true }`) that builds+installs the wheel into a clean venv and smoke-tests it, and select it in the PR flow. |
| `PKG-002` | (round M2-20, static; applies only when `package.json` exists at the repo root) No workflow step runs both `npm pack` and `npm ci`/`npm install`. | Add a quick-gate step that packs the tarball, installs it into a clean directory, and smoke-tests the installed package. |
| `CACHE-007` | (round M2-20) Static: a sanctioned `actions/cache*` step's declared `path:` input names a linked test binary, a nextest archive, `incremental/`, a whole `target/` directory, or a CMake build tree (`build/`, `build-*/`, `build_*/`, `cmake-build-*/`, `out/build/*`, `CMakeFiles/`, `CMakeCache.txt`; #393). Runtime (`ci-lint cache payload-check --manifest <file>`): same classification against a save's actual path manifest. | Remove the forbidden path from the cache payload; publish it as a short-retention artifact instead ([cache].never; zackees/zccache#1525, soldr#2931-#2938). |
| `BIN-001` | (round M2-20, static; only when `[python].cli.native = true`) No `group = "linux"` platform's `target` ends with `-unknown-linux-musl`. | Add a `[platforms.<id>]` entry with a `*-unknown-linux-musl` target (the portable Linux floor), or record a `[[exceptions]]` entry. |
| `BIN-002` | (round M2-20, static; only when `[python].cli.native = true`) A `group = "windows"` platform uses the GNU ABI (`-pc-windows-gnu`) instead of MSVC, a platform's target architecture doesn't match the one its id implies (e.g. a `windows-arm64` id with an `x86_64-` triple), or `group = "windows"` platforms don't cover both x64 and arm64. | Use `*-pc-windows-msvc` triples with the architecture matching each platform id; declare both a `windows-x64` and a `windows-arm64` entry. |
| `ACT-001` | (round M2-20, runtime, `ci-lint act audit --store-dir <dir>`) A local act cache-server store's total on-disk bytes exceed `ci.toml [cache].budget`, or a complete cache-index entry's key matches a `[cache].retired` family. | Garbage-collect the machine-scoped act-cache-server volume, or remove whatever input re-enabled the retired family (see `CACHE-009`). |
| `CPP-001` | (#393, static, precheck and `local-gate lint`) A CI `ctest` invocation (a `run:` line, one level into a referenced `ci/*.py` script, or a `ci/*.py` script the local gate names) with no `-j`/`--parallel`, no `CTEST_PARALLEL_LEVEL` and no test-preset `execution.jobs`, or one pinning the level to 1; an unresolvable `--preset` or a dynamically built script argv is `needs_review`. See [policy-cpp.md](policy-cpp.md). | Pass `-j` (or `--parallel <N>`), or set `CTEST_PARALLEL_LEVEL` in `env:`; keep host-sharing tests serial with `RESOURCE_LOCK`/`RUN_SERIAL`, or excuse one line with `# ci-lint: allow CPP-001 <reason>`. |
| `GATE-001` | (#166, static, precheck group 17 / `local-gate lint`) The local gate declaration is invalid or duplicated, or a `mirrors` job runs a `run:` line or non-setup `uses:` step other than the gate command; no mirrors is `needs_review`. | Move the command into the gate script and have the job invoke the gate, or name a pure-setup step in `setup-steps`. |
| `GATE-002` | (#166, static) No `verify` job, verify does not run `ci-lint local-gate verify`, or a job does not transitively `needs:` it; a job-level `if:` on verify is `needs_review`. | Add the verify step and route every job's `needs:` through it, or list a must-run-first job in `verify-exempt`. |
| `GATE-003` | (#166, runtime, `local-gate verify` / `check-push`) The PR head has no `Local-Gate:` trailer for its exact tree. | `ci-lint local-gate run`, then `git push --force-with-lease`. |
| `GATE-004` | (#166, live, `local-gate first-pass`) First-push pass rate below target (default 80%) over >= `--min-prs` merged PRs; `needs_review`. | Move each recurring first-push failure class into the local gate, or fix the flaky remote lane. |
| `GATE-005` | (#168, static, group 17 / `local-gate lint`) `[gate.isolation]` declared but its guard ignores `CI` or the marker, its bosn runner never sets the marker, or the gate never invokes the runner; a known self-hosted tool repo (soldr, zccache, clud, bosn) with no isolation is `needs_review`. |
| `GATE-008` | (#190, static, group 17 / `local-gate lint`; runtime `local-gate verify --trust`) `[gate.trust]` enabled with no verify job; a `skip` job with no job-level `if:` consuming the verify job's `trusted` output; a non-mirror `skip` job with no `covered-by` lanes (or naming undeclared lanes); a skip job's workflow with no push trigger, direct or inherited from a push-triggered `workflow_call` caller (no post-merge run); a bad `mode`/`audit-rate`. | Wire the `if:`, declare `covered-by`, keep the default-branch push run. |
| `RUST-001` (gate surfaces) | (#170, static, `local-gate lint` / precheck group 17) In a repository with a root `Cargo.toml`: a bare `cargo`/`rustc`/`rustup`/`maturin`/`cargo-nextest` in a workflow or composite-action `run:` line (`local-gate lint` only; precheck's group-4 RUST-001 covers a ci.toml repo's workflows), in the local gate's `run` argv or a file it names (shell lines; Python string-literal argv), or in a `bosn.toml` task `cmd`. `"$soldr" rustup ...` (the tool as soldr's argument) is not bare. Excuse one line with `# ci-lint: allow RUST-001 <reason>`. |

## The planner

`python3 -m ci_lint plan` computes `selection = (flow base ∪ tag adds) − tag
removes` for one event, and writes it as `plan.json` (see
`ci_lint.plan.Plan`): `flow`, `event_name`, `tags`, `platforms`
(`{id, target, runs_on, group}`), `suites`, `dylint_targets` (**every**
declared platform when `[lint.dylint].targets = "all-platforms"` -- Dylint
checks every declared target from one Linux job regardless of which
platforms this run actually builds, issue #6 §2/§3), `cache_mode`
(`"read"`/`"write"`), `publish` (`"none"`/`"rehearsal"`/`"mock"`, or
whatever `[publish.pypi].mode` resolves a flow's `publish = "pypi"` to),
`mergeable` (`false` if any tag removed a suite), a `digest` (sha256 of the
selection), `reasons` (why each lane was selected), `required_jobs` (the
job `id:`s `ci-lint gate` requires to succeed), and `needs_platform_lanes`
(`true` exactly when the selection includes any platform other than
`[flow.pr]`'s own default fast-lane platform(s) -- **not** simply "more
than one platform": `flow.release`/`flow.nightly`'s `platforms = "all"`
base is itself already beyond that fixed baseline, so they need the
platform matrix even though no tag added anything). Tags are read from the
PR title, and only for the `pull_request` event -- never for
`push`/`schedule`/`workflow_dispatch`.

### Platform-lane matrix outputs (round-3A)

Three more fields, computed alongside everything above (`ci_lint.plan.Plan`,
`ci_lint.plan.PlatformLane`):

| Field | Type | Meaning |
| --- | --- | --- |
| `platform_lanes` | array of `{id, target, runs_on, group, wheel, suites}` | Every selected platform **except** `[flow.pr]`'s own default fast-lane platform(s) (normally just `linux-x64`) -- one entry per non-default `platform-build`/`platform-run` matrix leg. `wheel` is `[platforms.<id>].wheel` or `null`. `suites` is `Plan.suites` (below) repeated per lane: every platform lane runs the same resolved suite selection the fast lane does (`unit`/`smoke` always, plus `integration`/`init`/`perf` when a flow or tag selected them, minus anything a tag removed) -- there is no separate per-platform suite composition to derive. Empty exactly when `needs_platform_lanes` is `false`. |
| `fast_suites` | array of strings | The suites for the `fast` (default-platform) lane -- identical to `Plan.suites`, exposed under its own name so the template can bind the `fast` job's suite input unambiguously alongside each platform lane's own `suites`. |
| `lane_digests` | object, lane key -> 12-hex-char string | A sha256 digest (truncated to 12 hex chars) of exactly the inputs that determine each lane's work, plus a shared "environment fingerprint" (`ci.toml`'s `linter` pin, and -- when `compute_plan` is given a `repo_root` -- the sha256 blob hashes of `Cargo.lock`, `uv.lock` and `rust-toolchain.toml`, each only if present). Keys: `"fast"` (fast-lane platform id(s)+target(s)+`fast_suites`), `"dylint"` (the sorted `dylint_targets`), and `"platform:<id>"` per platform lane (id+target+runs_on+suites+wheel). The template puts this digest in each job's display name, e.g. `fast [a1b2c3d4e5f6]`, `platform-run (windows-x64) [c1d2e3f4a5b6]` -- see "Title-edit reuse" below, which matches on exactly that bracketed substring. |

`--github-output` (both `ci_lint plan` and, from round-3A, `ci_lint
precheck --github-output`) adds `platform_lanes_json`, `fast_suites_json`
and `lane_digests_json` (each the field above, compact-JSON-encoded) on top
of the round-1A keys (`plan`, `platforms_json`, `cross_platforms_json`,
`suites_json`, `dylint_targets_json`, `needs_platform_lanes`,
`mergeable`). Round-4B adds one more: `cache_save`, `"true"`/`"false"` =
`(plan.cache_mode == "write")` -- a writer-flow step (`astral-sh/setup-uv`'s
`save-cache`, or a `setup-soldr` wrapper's `save-cache` passthrough) binds
straight to `needs.precheck.outputs.cache_save`, satisfying
`[allow].setup-uv.require`/`[allow].setup-soldr.require`'s `"plan"` value.

## Title-edit reuse (round-3A)

`ci_lint plan --reuse` (and `ci_lint precheck --reuse`, which computes the
same thing alongside its `--plan-out`/`--github-output`) looks up whether
any lane in this run's `lane_digests` already has a **proven-green** job
for the **identical digest** on the **identical PR head SHA**, so a title
edit that only adds or removes tags never re-runs work that is still
valid.

**Security note:** reuse only ever *skips* work already proven green for
the identical digest on the identical head SHA. It is recomputed fresh on
every `plan --reuse` invocation (never read from a cached/stale file), it
is scoped by the GitHub API's own `head_sha=` filter, and (see "Reuse
verification" below) `ci_lint gate --reuse` re-verifies it live before
ever treating a skipped required job as a pass. A lane whose inputs
changed gets a different digest and is never matched against an older
job's name; a different commit's jobs are excluded by the `head_sha=`
query itself.

Only consulted for the `pull_request` event, and only when `GITHUB_TOKEN`
and `GITHUB_REPOSITORY` are both set and the event JSON carries a PR head
SHA -- `push`/`schedule`/`workflow_dispatch` and any missing precondition
always produce "reuse nothing" with no network call (`ci_lint.reuse`'s
`empty_result`). On any GitHub API error, the result is the same "reuse
nothing", plus a warning printed to stderr -- reuse computation never
fails the precheck.

**Lookup (`ci_lint.reuse.compute_reuse`, stdlib `urllib`, injectable
`fetch` in tests -- fixtures under
`ci_lint/tests/fixtures/runtime/reuse/`):** `GET
/repos/{repo}/actions/runs?head_sha=<PR head sha>&event=pull_request&per_page=50`,
keep only runs whose `path` ends with `.github/workflows/ci.yml`, newest
first, excluding the current run (`GITHUB_RUN_ID`); for each such run `GET
/repos/{repo}/actions/runs/{id}/jobs?per_page=100`. A lane is reusable
when some previous job has `conclusion == "success"` and its `name`
contains the exact bracketed digest (`[<digest>]`) for that lane.

**Output**, added to `plan.json`'s `"reuse"` key and to `--github-output`:

| Key | Type | Meaning |
| --- | --- | --- |
| `reuse_json` | object, lane key -> `{run_id, job_id, html_url}` or `null` | Every lane in `lane_digests`, mapped to the reused job found (or `null`). |
| `platform_lanes_todo_json` | array, same shape as `platform_lanes` | `platform_lanes` minus every platform lane whose `"platform:<id>"` key in `reuse_json` is non-null -- the platform legs the matrix job(s) still actually need to run. |
| `fast_reused` | bool | `reuse_json["fast"]` is non-null. |
| `dylint_reused` | bool | `reuse_json["dylint"]` is non-null. |

### `required_jobs` / the gate job-id convention

`ci_lint.plan._required_job_ids` is the one place this convention is
defined; the template's `ci.yml` must name its jobs to match:

| Job `id:` | Always required? | What it covers |
| --- | --- | --- |
| `precheck` | always | the precheck job itself |
| `fast` | always | the default `linux-x64` build+unit+smoke lane |
| `dylint` | when `dylint_targets` is non-empty | the one Linux Dylint job (all declared targets) |
| `platform-build` | when `needs_platform_lanes` | the Linux cross-build matrix job -- GitHub aggregates every matrix leg into one `needs.platform-build.result` |
| `platform-run` | when `needs_platform_lanes` | the native-runner execute matrix job -- same aggregation |
| `init` | when the `init` suite is selected | the from-zero job: no cache restore, template instantiation, precheck + build + smoke of the generated repo |
| `perf` | when the `perf` suite is selected | the perf job; it must succeed even when the suite is non-gating (gating only decides whether a regression fails it) |
| `release-verify` | when `publish` is `rehearsal` or `mock` | staged-artifact completeness (`ci-lint release verify`) |
| `publish` | when `publish` is `mock` | the top-level OIDC mock publisher (PyPI can't trust a reusable workflow, warehouse#11096) |

`ci-ok` (the gate job itself) is **never** in `required_jobs` -- a job
cannot require its own result.

| Event | Base flow |
| --- | --- |
| `pull_request` | `pr` |
| `push` (to the default branch) | `main` |
| `schedule` | `nightly` |
| `workflow_dispatch` | `release` (requires a 40-hex `sha` input, recorded as `dispatch_sha`) |

`--github-output` (writes to `$GITHUB_OUTPUT`) adds: `plan` (the compact
JSON), `platforms_json`, `cross_platforms_json`, `suites_json`,
`dylint_targets_json`, `needs_platform_lanes`, `mergeable`.

## Runtime commands (round-2A)

These run *after* a build/wheel/install, unlike the precheck rule groups
above, which are all static.

| Command | Reads | Checks | Exit |
| --- | --- | --- | --- |
| `ci-lint units --repo <path> --artifacts <file.jsonl> [--json]` | `cargo ... --message-format=json` lines (`ci_lint.cargo_messages`; non-artifact lines ignored) | `RUST-011`: a private crate ([rust].private) compiled with more than one distinct feature set in the same profile; the public crate ([rust].public) compiled with a feature set not in `[rust].ship`. Prints a table per crate of every distinct `(features, profile, target kind, target triple)` combination actually compiled -- the target triple is read from the build output path, matched against `[platforms].*.target`, else `"host"`. | 1 if any violation |
| `ci-lint tests size --repo <path> --artifacts <file.jsonl>` | same artifacts file, `profile.test == true` records with an `executable` | `RUST-012`: an undeclared test binary, a declared one never produced, one over `[rust.tests].max-binary`, or the sum over `[rust.tests].max-total`. Names map `<crate>:lib` / `<crate>:bin:<name>` / `<crate>:test:<name>`. | 1 if any violation |
| `ci-lint wheel check --repo <path> --wheel <path.whl> [--sdist <path.tar.gz>]` | the wheel's own bytes (stdlib `zipfile`) | `PKG-003`: `<dist>.data/scripts/<cli>[.exe]` exists, has no `#!` shebang, its magic bytes match the wheel's platform tag (ELF/PE/Mach-O + machine/cputype), and no `console_scripts` entry shadows it. `PKG-004`: `*.dist-info/WHEEL`'s `Generator` must name soldr (soldr >= 0.9.26 stamps `Generator: soldr <version> (...)`, zackees/soldr#3450): an unstamped Generator is a violation when the repository's `pyproject.toml` `[build-system].requires` soldr floor is >= 0.9.26, and `needs_review` below that floor or when no floor is readable (ci.yml#71); the abi tag matches `[python].abi3`; with `--sdist`, its bundled `pyproject.toml` also declares `build-backend = "soldr"`. `PKG-005`: a `_native` extension file exists in the wheel. | 1 if any violation |
| `ci-lint wheel installed --repo <path> --venv <dir>` | the venv's `bin`/`Scripts` dir + a subprocess run of the installed CLI and the venv's `python` | Same magic-byte check against the current host; `<cli> --version` exits 0 (`PKG-003`); `<venv python> -c "import <pkg>, <pkg>._native as n"` succeeds and `n.__file__` ends in a compiled-extension suffix (`PKG-005`). `<pkg>` is `pyproject.toml`'s `[project].name` with `-` replaced by `_`. | 1 if any violation |
| `ci-lint gate --repo <path> --plan <plan.json> --needs <needs.json> [--reuse <reuse.json>] [--event <event.json>]` | `ci-lint plan`'s own JSON output + GitHub's `toJSON(needs)` verbatim, plus (round-3A) the plan's reuse map and the PR event JSON | The `CI OK` aggregator: every job in `plan.required_jobs` must have `needs[job].result == "success"`, or be a **verified reuse** (see "Reuse verification" below) when `skipped`. A `skipped` job that is neither is `TEST-001`; a job id absent from `needs` entirely is `needs_review`, never a silent pass. If `plan.mergeable` is `false`, prints `not mergeable: tag(s) <tags> remove required coverage` to stderr regardless of job results. | 1 if not green |
| `ci-lint suite check --repo <path> --suite <id> [--cargo-test-log <file>...] [--pytest-junit <file>...]` (round-6C) | Every `--cargo-test-log` file, scanned for libtest summary lines (`test result: <outcome>. N passed; M failed; K ignored; ...`, one per test binary -- a merged-doctest binary's own line is summed in like any other); every `--pytest-junit` file, parsed as JUnit XML (stdlib `xml.etree.ElementTree`, `<testsuite>`/`<testsuites>` `tests`/`failures`/`errors`/`skipped` attributes) | `TEST-001`: `[suites.<id>].required = true` and at least one test was ignored/skipped across all sources. `TEST-002`: `[suites.<id>].required = true` and zero tests were executed (passed + failed) across all sources -- including when no `--cargo-test-log`/`--pytest-junit` files were given at all. A non-required suite only prints per-source and total counts; neither rule fires for it. An unknown `--suite` id, or a file that isn't a recognizable libtest log / JUnit XML, is an error (exit 2), never silently "zero tests". | 1 if any violation, 2 on a bad `--suite`/file |
| `ci-lint dylint coverage --repo <path> [--log <file>...] [--json]` (round-6E) | Each `--log`, auto-detected as one of two real shapes: `ci/dylint.py --results-out`'s own `DylintPassResult[]` JSON (`{shape, targets, command, seconds, returncode}` -- `targets` already names every triple, host included, one invocation covered), or a raw GitHub Actions job log (`gh run view --job <id> --log`), regex-matched for the `+ soldr dylint ...`/`+ soldr cargo dylint ...` invocation trace line's `--target` flags, rustc's `` Checking with toolchain `...` `` banner (names the HOST triple as a suffix), a `Finished` completion marker printed AFTER that banner (one printed before it is the lint-library's own build, not the workspace check), and an `` error[E0463]: can't find crate for `core` `` + its `` the `<target>` target may not be installed `` note | `RUST-003`: every `ci.toml [platforms]` id in `ci_lint.plan.dylint_target_platform_ids(ci)` (declared when `[lint.dylint].targets = "all-platforms"`) must have its target triple proven covered by a completed pass in the combined evidence from every `--log` given; a multi-target invocation's several `--target` flags (or one `DylintPassResult.targets` entry) each count toward their own platform. An `E0463` failure is reported by name (`"provision prebuilt target std via 'soldr dylint prepare --target T'"`), in addition to (not instead of) the generic "not covered" finding for every OTHER target that invocation named but never proved finished. No `--log` given flags every declared target. A missing/malformed `--log` file is an error (exit 2), never silently "zero coverage". | 1 if any violation, 2 on an unreadable/malformed `--log` |
| `ci-lint example drift --example <path> --repo <path>` (round-6C) | Both ci.toml files, parsed with stdlib `tomllib` as raw tables (never through the typed schema loader, which would normalize away exactly the key-presence gaps this needs to catch) | Issue #6's round rule, "the ci.toml example is identical in both repos apart from repo-specific values," made machine-checked: every differing key is repo-specific (allowed) or schema/policy drift. See "example drift classification" below for the exact allowed list. | 1 if any schema/policy drift, 2 if a file can't be read/parsed |
| `ci-lint precheck --local` (or env `ACT=true`) [`--github-output`] [`--reuse`] | -- | Runs every static group as usual, then adds one `needs_review` finding per check that needs the GitHub API and is not implemented yet (`CACHE-005`/`006`/`008`) or needs a real local act cache-store path (`ACT-001`), each explicitly labeled `skipped (local): ...` -- never silently passed. Round-3A: `--github-output` writes the same `$GITHUB_OUTPUT` keys as `ci-lint plan --github-output` (computing a plan internally, same as `--plan-out` already did); `--reuse` adds the title-edit reuse lookup to both `--plan-out`'s written JSON (under a `"reuse"` key) and `--github-output`. | as `precheck` |
| `ci-lint cache payload-check --manifest <file.json> [--json]` (round M2-20) | a JSON array of path strings (e.g. from `tar -tf` on a saved cache archive, or a wrapper-emitted manifest) | `CACHE-007` runtime half: classifies each path against `[cache].never`'s forbidden content classes (linked test binary, nextest archive, `incremental/`, a whole `target/` directory) | 1 if any violation, 2 on a bad `--manifest` |
| `ci-lint act audit --repo <path> --store-dir <dir> [--json]` (round M2-20) | a local act `--cache-server-path` directory (`bolt.db`'s cache-index records + real bytes on disk under `cache/`) | `ACT-001`: the local act cache store's total on-disk bytes must fit `ci.toml [cache].budget`, and no complete cache-index entry's key may match a `[cache].retired` family | 1 if any violation |
| `ci-lint reuse-check --repo <owner/name> --sha <pushed sha> --workflow <file>... --required-job <name>... [--max-age-hours 24] [--mode enforce\|shadow] [--queue-evidence] [--default-branch main] [--event-name E] [--ref R] [--now T] [--json] [--out F] [--github-output] [--record F\|--replay F]` (#156) | GitHub REST, GET only: the pushed commit's associated PRs, both commits' trees, the PR head's `pull_request` runs and their jobs | `GEN-021`'s runtime decision: may this default-branch push skip validation because the one merged PR's head had the identical tree and a green, fresh, per-job proof? See "Verified reuse on default-branch pushes" below. `--repo`/`--sha`/`--event-name`/`--ref` default to `$GITHUB_REPOSITORY`/`$GITHUB_SHA`/`$GITHUB_EVENT_NAME`/`$GITHUB_REF`. | 0 whenever the arguments are valid (every "cannot prove" outcome is `reuse=false`), 2 on a usage error |
| `ci-lint reuse-report --repo <owner/name> --since YYYY-MM-DD [--until YYYY-MM-DD] --workflow <file>... --required-job <name>... [--max-age-hours 24] [--default-branch main] [--min-runs 1] [--limit 300] [--accept-flaky <run id>...] [--json] [--record F\|--replay F]` (#156) | GitHub REST, GET only: the first workflow's completed default-branch `push` runs since the date, then the same reads as `reuse-check` per run | Retroactive shadow mode: re-evaluates each push run with the clock set to its `created_at` and compares the verdict with the run's real conclusion -- `safe-skip`, `false-reuse-candidate` (would have reused, but the push run failed), `accepted-flaky` (a candidate passed via `--accept-flaky` after analysis), `must-run`, `no-signal` (cancelled). Verdict `promotable` needs >= `--min-runs` decisive runs (default 1 -- a single verified, attested PR merge is enough, since the per-run decision is sound on its own terms) with at least one `safe-skip` and zero candidates; otherwise `no-op` (decisive runs, but reuse never fired), `insufficient-sample`, `blocked`, or `error`. | 0 = promotable, 1 = not promotable, 2 on a usage error |
| `ci-lint selftest` | -- | Runs this package's own `unittest` suite (`ci_lint.selftest`; zackees/zccache#1760 -- agents must be able to run this directly). | 0/1 |

### Reuse verification (round-3A, `ci-lint gate --reuse`)

A required job whose `needs[job].result == "skipped"` is treated as
**success** iff:

1. `--reuse <reuse.json>` (the plan's own `reuse_json`/`"reuse"` map) marks
   the job's lane(s) as reused, **and**
2. when a GitHub token is available (env `GITHUB_TOKEN` + `GITHUB_REPOSITORY`
   both set), the referenced job is **re-fetched live**
   (`GET /repos/{repo}/actions/jobs/{job_id}`) and confirmed
   `conclusion == "success"`, its `name` contains the exact bracketed
   digest (`plan.lane_digests[lane]`) for that lane, and -- when
   `--event <event.json>` supplies the current run's PR head SHA -- the
   referenced job's own `head_sha` matches it.

`fast` and `dylint` map to the reuse map's `"fast"`/`"dylint"` keys 1:1.
`platform-build` and `platform-run` are matrix aggregates (GitHub folds
every leg of a matrix job into one `needs.<id>.result`), so they verify as
reused only when **every** currently-selected `"platform:<id>"` lane (read
from `plan.lane_digests`) is itself reused -- exactly the condition under
which `platform_lanes_todo_json` is empty.

**Without a token**, a job the plan marks reused but cannot be live-verified
is `needs_review` (`ci-lint gate` still exits non-zero) -- never a silent
pass. A job the plan does *not* mark reused, or whose live verification
fails (wrong digest, wrong head SHA, not `success`), is the pre-existing
`TEST-001` "a skip is a failure" outcome. The gate's summary table shows
`reused from run <N>` next to any job whose skip was accepted this way.

### Verified reuse on default-branch pushes (`ci-lint reuse-check`, GEN-021, #156)

**Merge-queue evidence (`--queue-evidence`, opt-in).** A merge queue merges a PR that is behind the default branch, so the pushed tree differs from the PR head's (`tree-mismatch`) even though the queue already validated that exact commit. With `--queue-evidence`, a `tree-mismatch`, `no-successful-run`, or `required-job-not-success` on the PR head (the last covers a locally attested head whose PR run skipped the routine lanes, #429) falls back to the newest decisive `merge_group` run whose `head_sha` is the pushed commit itself: same selection, per-job and freshness rules, and the merged-PR association is still required. No queue run keeps the PR-head reason; a red or unfinished queue run is a refusal of its own. The document's `proving_sha` names the commit the proving jobs ran on (the PR head, or the pushed commit for queue evidence); `ci-lint gate` re-verifies each proving job's `head_sha` against it (`pr_head_sha` for documents that predate the field, #424).

A different mechanism from title-edit reuse above (which reuses lanes
within the SAME pull_request head by lane digest, `ci_lint.reuse`, whose
behavior is unchanged): `ci_lint.default_branch_reuse` decides whether a
**default-branch push** may skip validation because a pull request already
validated the identical tree. Policy: docs/policy-general.md "Default-branch
validation"; full design, threat model, rollout, and reference wiring:
[docs/designs/default-branch-verified-reuse.md](designs/default-branch-verified-reuse.md).
The decision is only worth computing once something consumes it: the
consumer is `ci-lint gate --default-branch-reuse`, declared by
`[reuse.default-branch]` in `ci.toml` (see above).

Procedure (stdlib `urllib` through the injectable `FetchStatusFn`, GET only;
the typical verified decision is 5 calls):

1. Preconditions, no API call: the event is `push` and the ref is
   `refs/heads/<default branch>`; a token (`GITHUB_TOKEN`/`GH_TOKEN`) exists.
2. `GET /repos/{r}/commits/{sha}/pulls`: exactly one PR with `merged_at` set,
   `base.ref` = the default branch, and `merge_commit_sha` = the pushed SHA
   (GitHub's documented squash commit / merge commit / rebase tip), whose
   head is in this repository.
3. `GET /repos/{r}/git/commits/{sha}` and `.../git/commits/{head}`: the tree
   SHAs are equal.
4. `GET /repos/{r}/actions/runs?head_sha={head}&event=pull_request` (up to
   3 pages): per `--workflow`, the newest run created at or before the
   clock, passing over `cancelled`/`skipped`/`stale`, must be completed,
   `success`, and for this repository.
5. `GET /repos/{r}/actions/runs/{id}/jobs?filter=latest` (up to 3 pages):
   every `--required-job` (exact display name) is `success` and completed
   within `--max-age-hours` before the clock (and not after it).

`--mode shadow` computes everything but always reports `reuse=false` and the
proof as `would_reuse`. `--github-output` appends `reuse`, `would_reuse`,
`reason`, `pr`, `run_id`, `run_url`, `tree`; when `$GITHUB_STEP_SUMMARY` is
set a provenance block (reason, PR, tree, proving run, mode) is appended.
`--json`/`--out` write the schema-1 document (`schema`, `command`, `repo`,
`sha`, `default_branch`, `event_name`, `ref`, `mode`, `reuse`, `would_reuse`,
`reason`, `detail`, `pr`, `pr_head_sha`, `tree`, `run_id`, `run_url`,
`runs[]`, `workflows`, `required_jobs`, `jobs[]`, `max_age_hours`,
`evaluated_at`, `api_calls`). Token permissions: `contents: read`,
`actions: read`, `pull-requests: read` (in a `ci.toml` repository the last
needs an `[allow].permissions` grant for the decision job, `SEC-002`).

Reason codes (every value except `verified` means `reuse=false`,
`would_reuse=false`, exit 0):

| Reason | Condition |
| --- | --- |
| `verified` | Every condition above holds. |
| `event-not-push` | The event is not `push` (PR, `workflow_dispatch`, `schedule`, ...). |
| `ref-not-default-branch` | The ref is not `refs/heads/<default branch>` (a tag push, another branch). |
| `no-token` | No `GITHUB_TOKEN`/`GH_TOKEN`. |
| `api-error` | A non-200 response, transport failure, or invalid JSON. |
| `rate-limited` | HTTP 429, or a 403 whose message mentions a rate limit. |
| `api-malformed` | A required field is missing or mistyped. |
| `no-associated-pr` | No merged PR into the default branch with `merge_commit_sha` = the pushed SHA (a direct push, or the association is not indexed yet). |
| `ambiguous-pr` | More than one such PR. |
| `fork-head` | The PR head lives in another (or a deleted) repository. |
| `tree-mismatch` | The pushed tree differs from the PR head's tree. |
| `too-many-runs` | More than 300 runs on the head SHA. |
| `run-in-progress` | The newest non-cancelled run of the workflow on the head has not completed. |
| `newest-run-not-success` | The newest decisive run concluded something other than `success`. |
| `no-successful-run` | No decisive run of the workflow on the head. |
| `fork-run` | The proving run's `head_repository` is not this repository. |
| `too-many-jobs` | More than 300 jobs in a proving run. |
| `required-job-missing` | No job with that exact display name in the proving run(s) (renamed job, different tier). |
| `required-job-not-success` | A required job is skipped, cancelled, neutral, failed, or unfinished (e.g. an iteration-mode run that skipped the Linux lanes). |
| `ambiguous-required-job` | A required job *display name* matches more than one job in the proving run, and not all of them succeeded. The name cannot say which job proved what -- e.g. soldr's `ci.yml`, where `lint-docs` (a deliberate no-op for docs-only and attested heads, soldr#3318) and the real `lint` are both named `Lint`, and the no-op skips by design. Fail-closed, same refusal as `required-job-not-success`; the fix is to give each job a distinct display name. |
| `stale-run` | A proving job completed more than `--max-age-hours` before the clock. |
| `run-after-decision` | A proving job completed after the clock (retroactive evaluation only). |

Fixtures: `ci_lint/tests/fixtures/runtime/default-branch-reuse/` (live
`--record` recordings of zackees/clud PRs #1643, #1575, #1630, trimmed).
`ci-lint reuse-report` (row above) is the shadow-mode promotion evidence.

### Example drift classification (round-6C, `ci-lint example drift`)

`ci_lint.example_drift` walks both parsed `ci.toml` tables key by key and
classifies every difference (missing key, extra key, or a differing
value) as one of:

- **repo-specific (allowed)**: the top-level `linter` pin; each
  `[platforms.<id>]`'s `runs-on` and `wheel`; `[rust].public`,
  `[rust].private`, `[rust].ship` and `[rust.tests].binaries`;
  `[allow].platform-selector` and `[allow].platform-code`; each
  `[suites.<id>]`'s `run` command (its own test-invocation path -- but
  **not** `required`/`gating`/`cache`/`kind`, which are policy, not a
  path); each `[cache.family.<name>]`'s `max` and `min`; the whole
  `[[exceptions]]` array; `[python].cli`'s `name` and `crate`.
- **schema/policy drift (must match)**: everything else -- a missing or
  extra table/key at any level (including a whole extra platform, suite,
  cache family, flow or tag the example doesn't declare), a different
  enum or value (`[lint.dylint].shape`/`.budget`, `[suites.<id>]`'s
  `required`/`gating`/`kind`, `[cache.family.<name>]`'s `via`/`lockfile`/
  `per`, `[flow.*]`, `[tags.*]`, `[allow].permissions`, `[publish]`, ...).

This is checked, not designed here: run `ci-lint example drift` against a
candidate repository and read its report. If a repository's `ci.toml`
disagrees with the example outside the allowed list, either the example
needs the same fix (the repository found something the example got
wrong) or the repository does (it drifted) -- the report doesn't decide
which; that's a human/orchestrator call, recorded back into this file and
`examples/rust-pypi-app/ci.toml` once made.

Every command above is stdlib-only (`zipfile`, `tarfile`, `subprocess`,
`tomllib`, `json`, `xml.etree.ElementTree`) and every finding is a
`ci_lint.finding.Finding` (`--json` prints the same fields as
`precheck --json`'s `findings` array).

A `[tags.<id>]` entry with `flow` set switches the base flow itself (e.g.
`[release]` on a PR runs the `release` flow as a rehearsal, with
`publish = "rehearsal"`).

## Settings audit (round-5)

`ci-lint audit --repo . [--default-branch main] [--gate-check-name "CI OK"]
[--json]` -- live, read-only, `GITHUB_TOKEN` + `GITHUB_REPOSITORY`
(`ci_lint.settings_audit`, injectable `FetchStatusFn` --
`ci_lint.github_api.default_fetch_status`, which returns `(status_code,
body)` instead of raising, so a 403 ("needs an admin token") and a 404
("legitimately doesn't exist") are never collapsed into one generic
error). Distinct from `ci_lint cache audit` (which classifies GitHub
Actions cache entries) -- this module never touches the cache API.

| Rule | Checks | On 403 | On 404 |
| --- | --- | --- | --- |
| `SEC-005` | Any repository Actions secret (`GET .../actions/secrets`) or `[publish].pypi.environment`'s environment secret (`GET .../environments/<name>/secrets`) exists. | `needs_review` ("requires an admin token") -- **never** a pass. | not applicable (no secrets to report). |
| `SEC-006` | `[publish].pypi.environment` is missing (`GET .../environments/<name>`), or its `deployment_branch_policy` doesn't restrict deploys to `--default-branch` (`protected_branches`, or a `custom_branch_policies` pattern list equal to exactly `[--default-branch]`, checked via `GET .../environments/<name>/deployment-branch-policies`). | `needs_review`. | violation ("does not exist"). |
| `SEC-007` | `GET .../actions/permissions/workflow`'s `default_workflow_permissions` is not `"read"`. | `needs_review`. | n/a (this endpoint always exists for a real repo). |
| `GEN-006` | Classic branch protection's `required_status_checks.strict` is `true` with no **active** `merge_queue` ruleset (`GET .../rulesets` + per-id `GET .../rulesets/<id>`) covering `--default-branch` (best-effort `conditions.ref_name.include` match: `~ALL`/`~DEFAULT_BRANCH`/the literal `refs/heads/<branch>`). docs/case-studies/fbuild-ci-cost.md's candidate rule. | `needs_review`. | **treated as none** -- no branch protection means nothing for GEN-006 to flag (GEN-011 covers the "no protection at all" case). |
| `GEN-011` | The default branch's required status checks (`required_status_checks.contexts` + the newer `.checks[].context`) don't include `--gate-check-name` (default `"CI OK"`). | `needs_review`. | `needs_review` ("a policy decision, not a code defect") -- **not** a violation. |

Every check is independent (one missing/forbidden endpoint never blocks
another). A transport failure (DNS, timeout -- `GitHubApiError`) on any
single call degrades that one check to `needs_review`, never a crash and
never a silent pass. Prints a per-rule summary table, then every finding;
exits 1 if any finding is `violation` (not on `needs_review` alone --
matching the rest of ci_lint's convention that only a `--live`
network-dependent check downgrades an inconclusive result rather than
failing on it).

## Publish (round-5)

`ci-lint publish oidc-check --repo . --audience <aud> [--expect-ref
refs/heads/main] [--expect-event workflow_dispatch] [--json]` --
`ci_lint.publish_oidc`, issue #6 §7's mock publisher. Refuses to run at all
(**exit 2**, before any network call) unless `ci.toml`'s
`[publish].pypi.mode == "mock"`.

Needs `ACTIONS_ID_TOKEN_REQUEST_URL` + `ACTIONS_ID_TOKEN_REQUEST_TOKEN`
(set only when the job has `permissions: id-token: write`) and
`GITHUB_REPOSITORY`. Requests the token from `ACTIONS_ID_TOKEN_REQUEST_URL
+ "&audience=" + <aud>`, authenticating with
`ACTIONS_ID_TOKEN_REQUEST_TOKEN` (`ci_lint.publish_oidc
.request_oidc_token`, through the same injectable `FetchFn` as everywhere
else in this package). Decodes **only** the JWT's middle (payload)
segment, base64url, in memory (`decode_payload` -- the header and
signature segments are never read; this module does not and cannot verify
the signature itself, matching how `actions/toolkit`'s own
`core.getIDToken` trusts the Actions runtime's authenticated channel).

Asserts exactly these claims (`assert_claims`):

| Claim | Expected |
| --- | --- |
| `repository` | `GITHUB_REPOSITORY` |
| `ref` | `--expect-ref` |
| `environment` | `ci.toml`'s `[publish].pypi.environment` |
| `event_name` | `--expect-event` |
| `workflow_ref` | `"<repository>/.github/workflows/ci.yml@<expect-ref>"` -- asserting the **full** shape (not just a suffix) is what proves the workflow is a top-level, non-reusable one, per PyPI's trusted-publishing requirement (warehouse#11096). |
| `aud` | `--audience` |

Prints only the claim **name** and PASS/FAIL for every assertion; the
**value** is additionally printed only for the five claims above named
`repository`/`ref`/`environment`/`event_name`/`workflow_ref` (never `aud`,
and never anything else) -- `ci_lint.publish_oidc.PRINTABLE_CLAIMS`. The
raw token, its `jti`, and its signature segment are **never** printed,
logged, or included in `--json` output, by construction: the token string
is returned exactly once (from `request_oidc_token` to its one caller) and
never touches a print/log/exception-message call anywhere in this module
or the CLI layer (`ci_lint/tests/test_publish_oidc.py`'s
`NeverPrintsTokenTest` asserts this, including one test that drives the
actual CLI command end-to-end with a fake token and greps its captured
stdout+stderr). Then prints `mock publish: stopping before upload` and
exits 0 if every assertion passed, else 1.

## Release verify (round-5)

`ci-lint release verify --repo . --dist <dir> --sha <candidate sha>
[--smoke <dir>] [--json]` -- `ci_lint.release`, issue #6 §3/ci.yml#4's
release-candidate gate. Pure filesystem (stdlib `zipfile`/`tarfile`/
`hashlib`, via the reused `ci_lint.runtime.wheel.parse_wheel_filename`/
`check_wheel`) -- no network, no subprocess.

For every `[platforms.<id>]` entry, `expected_wheel_tag_pattern` derives
the wheel `platform_tag` regex its wheel must match, from the platform's
`group` and its `target` triple's CPU architecture:

| `[platforms.<id>].group` | Expected `platform_tag` |
| --- | --- |
| `linux` | `<[platforms.<id>].wheel or "manylinux_2_17">_<x86_64\|aarch64>` |
| `windows` | `win_amd64` (x86_64) / `win_arm64` (aarch64) |
| `macos` | `macosx_*_arm64` (aarch64) / `macosx_*_x86_64` (x86_64) -- the macOS deployment-target prefix is a wildcard |

`--dist` must contain exactly one wheel matching each declared platform's
pattern, plus exactly one sdist (`{name}-{version}.tar.gz`), every staged
artifact (wheel or sdist) agreeing on the same `{version}`, and abi3
`cp310` per `[python].abi3` (checked by the reused `wheel check`, not
re-implemented). `PKG-006` covers: a platform with zero matching wheels
(missing), more than one (duplicate), a wheel matching no declared
platform (extra), an unparseable `.whl`/`.tar.gz` filename, a missing or
duplicated sdist, and a version disagreement across the staged set.
`check_wheel`'s own `PKG-003`/`004`/`005` findings are folded straight
into the report (reused, not duplicated) -- the sdist backend check
(`PKG-004`) runs once, paired with the first staged wheel.

`--smoke <dir>` (optional) consumes an already-written
`smoke-results/*.json` directory (each file `{"platform": ..., "wheel":
"<the staged wheel's exact filename>", "passed": bool, "detail":
"..."}`) -- the per-wheel **native install smoke** itself runs elsewhere
(the platform-run job, on that platform's own native runner); this command
only requires a passing result per staged wheel (`PKG-006`: missing or
failing).

Always writes `<dist>/release-manifest.json` (schema-versioned): the
candidate SHA, `ci.toml`'s own sha256 digest, the resolved version, and
one `{path, kind, platform, version, sha256}` record per staged artifact
(`sha256` computed from the artifact's own bytes, streamed, never trusted
from a filename or a build log). Exits 1 if any finding is `violation`
(any rule, not just `PKG-006`), else 0; exits 2 on bad input (`--dist` not
a directory, `--sha` not a 40-hex commit SHA).

## Perf compare (round-5)

`ci-lint perf compare --repo . --baseline <b.json> --current <c.json>
[--threshold-pct N] [--json]` -- `ci_lint.perf`. AGENTS.md's typed-
benchmark rule applies directly: `BenchmarkRecord` (`name`, `unit`,
`samples: tuple[float, ...]`, `median`) and `BenchmarkFile`
(`schema_version`, `benchmarks`) are frozen dataclasses; `load_benchmark_
file` is the one place the JSON wire boundary (typed as `JsonRecord =
dict[str, JsonScalar | list[float]]`, never `dict[str, Any]`) is read,
validated field-by-field, and converted -- nothing past that function ever
sees a raw dict again. The file format:

```json
{
  "schema_version": 1,
  "benchmarks": [
    {"name": "build_time", "unit": "s", "samples": [1.21, 1.19, 1.25], "median": 1.21}
  ]
}
```

`compare` reports every benchmark present in either file: a `delta_pct`
(`None` for a benchmark new in `--current`, removed since `--baseline`, or
whose baseline median is exactly `0`), and `regression` (only ever `true`
when `--threshold-pct` is given **and** `delta_pct` exceeds it). **Non-
gating unless BOTH** `ci.toml`'s `[suites.perf].gating = true` **and**
`--threshold-pct` is supplied (`is_gating`) -- every delta is always
printed either way; the command only exits 1 (instead of 0) when gating is
active and at least one benchmark regressed past the threshold. The
printed/`--json` report itself carries a `schema_version` field
(`PERF_SCHEMA_VERSION`).


### Qualified replay public outputs (candidate pilot, #362)

A `[[local.gate.replay.jobs]]` entry (or `[[gate.replay.jobs]]` in
`local-gate.toml`) accepts `capture-outputs = ["matrix"]` when qualified
replay is enabled. Names must be distinct, valid identifiers and declared
in that source job's `outputs`. Declare only public, nonsecret values needed
for replay proof. The shared tool compiles qualified Bosn request flags and
checks the actual output evidence; do not repeat `--ci-output` flags in lane
commands. Absence means request nothing and preserves existing consumers.
See [the transport design](designs/ci-tool-protocol.md#public-output-request-transport-candidate-issue-362)
for bounds and pending pilot qualification.
