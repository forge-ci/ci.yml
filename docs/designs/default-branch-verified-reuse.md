# Design: verified reuse on default-branch pushes (`GEN-021`)

| | |
| --- | --- |
| Status | Phase 0 implemented (this repository): policy text, `ci-lint reuse-check`, `ci-lint reuse-report`, the static `GEN-021` precheck rule, fixtures, case study. Phases 1-4 are specified here and tracked as issues. |
| Tracking | [#156](https://github.com/zackees/ci.yml/issues/156) |
| Follow-ups | Phase 1 [#157](https://github.com/zackees/ci.yml/issues/157) (clud shadow), Phase 2 [#158](https://github.com/zackees/ci.yml/issues/158) (enforce + tooling), Phase 3 [#159](https://github.com/zackees/ci.yml/issues/159) (fleet scan `FLEET-003`), Phase 4 [#160](https://github.com/zackees/ci.yml/issues/160) (template), clud-side [zackees/clud#1651](https://github.com/zackees/clud/issues/1651) (false merge-queue claim, flake follow-up) |
| Evidence | [docs/case-studies/clud-main-push-ci-burn.md](../case-studies/clud-main-push-ci-burn.md) |
| Policy | [docs/policy-general.md](../policy-general.md), "Default-branch validation" and the `GEN-021` row |
| Reference | [docs/ci-toml.md](../ci-toml.md), "Verified reuse on default-branch pushes (`ci-lint reuse-check`)" |

This is the first document under `docs/designs/`. The repository had no design-document home before; case studies record evidence, policy docs record decisions, and a design that spans several phases and repositories fits neither, so it lives here. A design is not policy: `GEN-021`'s binding text is in `docs/policy-general.md`.

## 0. Summary

Every PR in `zackees/clud` is validated, merged, and then validated again on `main`, usually as the same bytes. The `main` push run costs about 22% of all clud CI runner time. This design skips that second validation **only when it is provably redundant**: the pushed commit's tree is byte-identical to the head of the one merged pull request associated with it, the newest decisive `pull_request` run of the same workflow on that head passed every job the `main` run would execute, the proof is fresh, and every uncertainty resolves to "run everything". A shadow mode proves zero lost catches before anything is skipped.

Rule, in one line: **a default-branch push run may skip a required job only through verified reuse (`ci-lint reuse-check`), never through a blanket `if:` or a deleted `push:` trigger, unless a live merge queue validates the exact merge commit.**

## 1. Goals and non-goals

**Goals**

1. Stop re-validating, on the default branch, source trees that a pull request already validated with the same checks.
2. Lose no catch the default-branch run provides today. In the clud sample (100 `main` runs, 12 failures) a tree-identity reuse would have skipped 7 failures, all of them one flaky test, and none of the 5 real ones (case study, section 4).
3. Fail closed: an API error, a rate limit, a missing field, an ambiguous PR, a renamed job, or a mode mismatch runs the full default-branch tier.
4. Keep every green default-branch commit explainable: a reused run says which PR, which run, which tree.
5. Keep default-branch caches warm (`CACHE-003`: only default-branch/nightly flows write base layers).
6. Be adoptable fleet-wide with one runtime command, one static rule, and (Phase 2) one `ci.toml` table.

**Non-goals**

- Replacing PR CI, or weakening it. PR runs are 63% of clud's burn (1,606 of 2,533 runner-min); that is a related opportunity (e.g. `GEN-007` path scoping, `PERF-002` candidate #151, `TEST-003` candidate #152), out of scope here.
- Weakening release gating. `workflow_dispatch` release proofs, tag pushes, and `schedule` runs never reuse (`event-not-push`/`ref-not-default-branch`, zero API calls).
- Replacing a merge queue where one is live. A live queue that validates the exact merge commit is an alternative justification (section 11a).
- Detecting flaky tests. Reuse removes flake-driven red `main` runs as a side effect; it does not diagnose them.

## 2. Background

- `pull_request` CI in clud checks out `github.event.pull_request.head.sha` (provenance pinning), not the test-merge commit. The `main` push run is therefore the only execution of the merged tree, and for 40% of merges (30 of 75 since 2026-09-28) the merged tree was never validated by any green PR run.
- clud documents a merge queue that "always runs the full matrix before merge". There is none: rulesets empty, `branches/main/protection` 404, zero `merge_group` runs ever (the `GEN-010` failure class; zackees/clud#1651).
- Of 75 merged PRs, 60 (80%) landed with a tree identical to the PR head, 59 (79%) had a green PR run on the head, and 45 (60%) had both. Those 45 are the reuse-eligible population.

## 3. Soundness argument

### 3.1 What "same tree + same checks" proves

A Git tree SHA is a content hash of every file, including `.github/workflows/*`, reusable workflows, composite actions, `ci/` scripts, lockfiles, and toolchain pins. If the pushed commit's tree equals the PR head's tree, then:

- the source bytes are identical;
- the CI definition that a push run would load is byte-identical to the one in the head tree;
- every input the repository controls (dependency versions pinned by lockfiles, toolchain files, test data in the tree) is identical.

Given a deterministic build and test process, the same inputs produce the same outcome. So a green PR run whose jobs cover the default-branch tier (section 3.5) proves what the default-branch run would prove, **up to the non-determinism in 3.2**.

### 3.2 What it does not prove

| Residual | Example | Bounded by |
| --- | --- | --- |
| Environment drift between the PR run and the push | a dependency resolved at build time without a lock, a new runner image, a toolchain channel moving | freshness (`--max-age-hours`, default 24 h), the scheduled full run (section 8), lockfiles (`TOOL-002 --locked`) |
| Wall-clock or remote-data-dependent tests | clud's `test_committed_seed_covers_the_reported_model` reads live model data | freshness + scheduled run; such tests belong in a scheduled lane anyway |
| Flaky tests | clud's `test_codex_installer_rm` (7 red `main` runs) | not bounded, and not a loss: a flaky failure on an identical tree is noise, not a catch |
| Results that depend on repository/org settings or secrets | a job that behaves differently with or without a secret | same-repository heads only (`fork-head`, `fork-run`); `SEC-005` keeps fleet repos secret-free |
| The pull_request run's workflow definition comes from the test-merge commit | see 3.3 | 3.3 |

### 3.3 The test-merge subtlety

A `pull_request` run loads its workflow YAML from `GITHUB_SHA`, which is the test-merge commit `refs/pull/N/merge` (the PR head merged into the base branch as of the run), and `actions/checkout` defaults to that commit too. clud overrides the checkout to the head SHA; most repositories do not. So the tree a PR run actually tested is the test-merge tree `merge(base_at_run, head)` for default-checkout repositories, and `head` (sources) plus the test-merge workflow YAML for clud.

When the pushed tree equals the head tree, the base changes present at merge time are all already in the head. The test-merge tree at run time then also equals the head tree, **except** when the base gained a change after the PR's merge-base, the PR run tested with it, and the base reverted it before the merge (a revert pair on the base that the head never contained). This design accepts that residual, because:

- it requires a revert pair on the default branch inside the freshness window, which is rare and visible in history;
- the GitHub API cannot supply the run's test-merge tree after the fact: `workflow_runs[].pull_requests[]` (which carries the base SHA the run tested against) was **empty for every recorded clud PR run after its PR merged** (fixtures `clud-pr1643.json`, `clud-pr1575.json`), so a stricter "the run's base equals the pushed commit's first parent" binding cannot be built from run data (it was prototyped and removed during Phase 0 because it would always fail closed);
- testing the merge ref and recording its tree (alternative 11e) removes the residual entirely and is tracked as an open question (section 15).

### 3.4 Why the residual risk is bounded

1. **Fail closed.** Every step of section 5 that cannot prove its condition returns `reuse=false`, and the workflow runs everything. A bug in the decision can only waste runner time, never skip a check, as long as the wiring consumes `reuse` as `!= 'true'` (checked statically, section 4.4).
2. **Freshness.** Every proving job must have completed within `max_age_hours` (default 24 h) before the decision, and not after it. Drift older than that forces a full run.
3. **Scheduled full run.** In `enforce` mode a scheduled full default-branch run at least daily re-validates `main` in the current environment regardless of reuse (section 8), so any drift is caught within a day, with a small blame window.
4. **Shadow before enforce, briefly.** Promotion requires one verified, attested merge with no false-reuse candidate (section 9); the run quota that used to sit in front of it was removed on 2026-10-04.

### 3.5 Same tier, not just same workflow

A PR run can be green in a narrower tier than the default branch: clud's `ci-windows` label runs static + Windows lanes and **skips every Linux job**, yet its run concludes `success`. The proof is therefore **per job**: the caller lists every job the default-branch run would execute (`--required-job`, exact display names), and each must be `success` in the proving run. A skipped, cancelled, neutral, or missing job is not proof. Only the newest decisive run on the head is consulted, so a later iteration-mode run disqualifies reuse instead of an older full-tier run being cherry-picked.

### 3.6 Threat model

Actors: a PR author (possibly outside collaborator), a maintainer with write access, a compromised or buggy workflow, and GitHub API failures. Trust roots: GitHub's API over TLS and the repository's own default-branch tree (the reuse wiring and its required-job list live there).

| Threat | Mitigation |
| --- | --- |
| Push a tree-identical commit with a tampered workflow | Impossible by construction: workflow files are part of the tree, so tree identity implies an identical workflow. |
| Reuse evidence produced under different secrets/permissions (fork PR, restricted `GITHUB_TOKEN`, no secrets) | The PR head repository must equal the base repository (`fork-head`), and so must the proving run's `head_repository` (`fork-run`). |
| Force-push after CI passed | The PR's final `head.sha` at merge time is used; runs are looked up by that exact SHA (`head_sha=` filter, re-checked client-side). A force-pushed head is a different SHA with its own runs. |
| Re-run an old successful run to make it "fresh" | A re-run re-executes the jobs (a real, current execution). A partial re-run keeps the old attempt's unchanged jobs with their old `completed_at`, so freshness is evaluated per proving job, not per run. |
| Cherry-pick an older green run over a newer failure | Only the newest decisive run (cancelled/skipped/stale passed over) is consulted; a newer failure or an unfinished newer run fails closed (`newest-run-not-success`, `run-in-progress`). |
| Evidence from a different workflow file | The run's `path` must equal the named workflow file exactly. |
| Evidence from a narrower tier (`ci-windows`) | Per-job proof of every default-branch job (3.5). |
| A hand-written "reuse" flag, or reading the output the unsafe way | The static rule flags a `reuse` output not produced by `ci-lint reuse-check`, and any consumption other than `!= 'true'` (section 4.4). Phase 2's `ci-lint gate --default-branch-reuse` re-verifies each proving job live. |
| Labels, PR comments, artifacts, commit messages | Never read. Only API objects GitHub computes (PR association, commit trees, run/job conclusions and timestamps) are inputs. |
| A PR shrinks the required-job list | The list lives in the tree, so shrinking it is a reviewed workflow change, like deleting a job outright (which a write-access author can already do). The static coverage check flags a job skipped on reuse that no `--required-job` proves. |
| Flake-washing (re-running a failed PR job until it passes) | Accepted: equivalent to merging on a green re-run today. `reuse-report` surfaces `run_attempt`; the scheduled run and the shadow analysis catch systematic cases. |
| API unavailable, rate-limited, or slow | Fail closed (`api-error`, `rate-limited`); the workflow runs everything. |
| Token over-privilege | GET-only code path; job-scoped `contents: read`, `actions: read`, `pull-requests: read` (section 5.4). |

## 4. Architecture and data flow

### 4.1 Components

| Component | Where | Phase |
| --- | --- | --- |
| (a) Decision step: `ci-lint reuse-check` in the first job of `ci.yml` on a default-branch push | repository workflow | 1 (shadow), 2 (enforce) |
| (b) `ci-lint reuse-check`: stdlib, GET-only decision, JSON schema 1, `--github-output` | `ci_lint.default_branch_reuse`, `ci_lint.reuse_cli` | 0 (done) |
| (c) Downstream consumption: `if: needs.<first job>.outputs.reuse != 'true' && <existing condition>` on validation jobs only | repository workflow | 2 |
| (d) Aggregator (`CI OK`): accepts `skipped` only for skip-on-reuse jobs and only when `reuse == 'true'`; prints provenance | repository workflow / script | 2 |
| (e) `ci-lint gate --default-branch-reuse <reuse-check.json>` for `ci.toml` repositories | `ci_lint.runtime.gate` | 2 |
| (f) `ci-lint reuse-report`: retroactive shadow evidence and promotion verdict | `ci_lint.default_branch_reuse` | 0 (done) |
| (g) Static `GEN-021` precheck rule | `ci_lint.rules.default_branch_skip` | 0 (done); cross-checks against `[reuse.default-branch]` in 2 |
| (h) `[reuse.default-branch]` table in `ci.toml` | `ci_lint.schema` | 2 |
| (i) `FLEET-003` in `ci-lint fleet scan` | `ci_lint.fleet` | 3 |

### 4.2 Where the decision runs: a step in the first job

The canonical host is **a step inside the workflow's first job** (clud: `static`; a `ci.toml` repository: the `precheck` job in `ci-pre.yml`), exposed as a job output. Reasons:

- GitHub skips every job whose `needs:` includes a skipped job unless that job's `if:` uses a status function. A separate `reuse` job gated to push events would skip every dependent job on pull requests; working around that with `!cancelled()` also disables failed-`needs` propagation, which is easy to get wrong.
- A step adds no job start-up time, queue slot, or runner-minute rounding. On pull requests the step is skipped, its output is empty, and `'' != 'true'` runs every job.
- Every downstream job already `needs:` the first job.

A dedicated job is acceptable only if it runs on **every** event (no job-level `if:`) and exits with `reuse=false` itself on non-push events (the command does this with zero API calls). The static rule accepts either shape: a job "makes a reuse decision" when one of its steps, or a job of the local reusable workflow it calls, runs `ci-lint reuse-check`.

### 4.3 `ci-lint reuse-check` interface

```
python3 -m ci_lint reuse-check
    --repo OWNER/NAME            # default $GITHUB_REPOSITORY
    --sha SHA                    # the pushed commit; default $GITHUB_SHA
    --workflow FILE              # repeatable; ci.yml == .github/workflows/ci.yml
    --required-job NAME          # repeatable; exact job display name
    [--max-age-hours 24]         # > 0
    [--mode enforce|shadow]      # default enforce
    [--default-branch main]
    [--event-name EVENT]         # default $GITHUB_EVENT_NAME
    [--ref REF]                  # default $GITHUB_REF
    [--now ISO8601]              # evaluation clock; default now (tests)
    [--queue-evidence]           # opt-in merge_group fallback (section 5.7)
    [--json] [--out FILE] [--github-output]
    [--record FILE | --replay FILE]
```

Token: `GITHUB_TOKEN` or `GH_TOKEN`, sent only as a bearer header to `api.github.com`, never printed. Without one, the decision is `no-token` (exit 0, `reuse=false`).

**Exit codes.** `0` whenever the arguments are valid, including every "cannot prove" outcome (reported as `reuse=false`, so the workflow runs everything). `2` for a usage error only: a malformed `--repo`/`--sha`/`--now`, no `--workflow`, no `--required-job`, `--max-age-hours <= 0`, or `--record` with `--replay`. A usage error is a wiring bug and turns the run red, which is also fail-safe.

**`$GITHUB_OUTPUT` keys** (`--github-output`): `reuse` (`true` only in enforce mode with a verified proof), `would_reuse` (the proof held, any mode), `reason`, `pr`, `run_id`, `run_url`, `tree` (empty string when unknown).

**Step summary.** When `$GITHUB_STEP_SUMMARY` is set the command appends a provenance block: headline (verified reuse / shadow would-reuse / no reuse), reason and detail, source PR and head SHA, tree SHA, proving run URL and attempt, mode, max age, API calls.

**JSON document** (`--json` on stdout, `--out FILE`), schema 1:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | int | `1`. Bumped on any incompatible change. |
| `command` | string | `"reuse-check"` |
| `repo`, `sha`, `default_branch`, `event_name`, `ref` | string | Inputs as evaluated. |
| `mode` | string | `"enforce"` or `"shadow"`. |
| `reuse` | bool | What the workflow acts on: `true` only in enforce mode with a verified proof. |
| `would_reuse` | bool | The proof held (independent of mode). |
| `reason` | string | A code from the fail-closed table (section 5.6), or `"verified"`. |
| `detail` | string | Human-readable explanation. |
| `pr` | int or null | The associated merged PR. |
| `pr_head_sha` | string or null | Its head SHA at merge. |
| `proving_sha` | string or null | The commit the proving jobs ran on: `pr_head_sha`, or the pushed commit when `--queue-evidence` proved them (section 5.7). Additive; absent on documents written before #424 (schema stays 1). |
| `tree` | string or null | The pushed commit's tree SHA. |
| `run_id`, `run_url` | int/string or null | The first proving run. |
| `runs` | array of `{workflow, run_id, run_url, run_attempt, created_at}` | One proving run per `--workflow`. |
| `workflows`, `required_jobs` | array of string | Inputs as normalized. |
| `jobs` | array of `{name, job_id, run_id, conclusion, completed_at, html_url}` | The proving job for every required name (empty unless verified). |
| `max_age_hours` | number | Input. |
| `evaluated_at` | string (UTC ISO-8601) | The evaluation clock. |
| `api_calls` | int | GET requests made by this decision. |

### 4.4 Downstream consumption

A validation job skips on reuse with its reuse test as its own `&&` conjunct:

```yaml
if: needs.static.outputs.reuse != 'true' && needs.static.outputs.mode != 'windows'
```

Rules (the static `GEN-021` rule checks the first three):

1. Only `!= 'true'`. Any other reading (`== 'false'`, `!needs...`, `== ''`) skips the job whenever the output is empty, which is every pull request and every failed decision.
2. The `needs.<X>.outputs.reuse` producer `<X>` must run `ci-lint reuse-check`.
3. Every job that consumes `reuse` must be covered by a `--required-job` (its display name, `<name> / <callee job>` for a reusable-workflow call, `<name> (<matrix values>)` for a matrix leg).
4. Cache-writer jobs never consume `reuse` under strategy (i) (section 8).

### 4.5 Aggregator semantics (`CI OK`)

`CI OK` stays the single required check and stays fail-closed:

1. The decision host (first job) must be `success`.
2. For each job in the declared skip-on-reuse set: `success` passes; `skipped` passes **only if** `needs.<host>.outputs.reuse` is exactly `'true'`; anything else fails. A job that ran on a reused push and failed still fails the run.
3. Every other required job (including cache writers) must be `success`, as today.
4. If `reuse == 'true'`, print the provenance: an annotation `::notice title=Verified reuse::skipped <jobs>; proof: PR #<pr>, run <run_url>, tree <tree>` and the same lines in the step summary. A green `main` commit is never an unexplained skip.
5. A commit status (`ci/verified-reuse`) is optional and needs `statuses: write`, a `SEC-002` grant; annotations need no permission, so they are the default.

Pseudocode for a repository script (`ci/ci_ok.py`; one-line `run:` per `GEN-005`):

```python
needs = json.loads(os.environ["RESULTS"])            # toJSON(needs)
reused = os.environ.get("REUSE") == "true"           # exactly 'true', nothing else
skippable = set(os.environ.get("REUSE_SKIPPABLE", "").split())
failed = []
for job_id, entry in needs.items():
    result = entry.get("result")
    if result == "success":
        continue
    if result == "skipped" and reused and job_id in skippable:
        continue
    if result == "skipped" and job_id not in required_for_mode(os.environ["MODE"]):
        continue                                     # the repository's existing tier logic
    failed.append(f"{job_id}={result}")
if failed:
    print(f"::error::CI OK: {', '.join(failed)}"); sys.exit(1)
if reused:
    print(f"::notice title=Verified reuse::skipped {' '.join(sorted(skippable))}; "
          f"proof: PR #{os.environ['REUSE_PR']}, {os.environ['REUSE_RUN_URL']}, tree {os.environ['REUSE_TREE']}")
```

### 4.6 `ci-lint gate` integration (Phase 2)

For `ci.toml` repositories `CI OK` is `ci-lint gate`. Phase 2 adds `--default-branch-reuse <reuse-check.json>` beside the existing title-edit `--reuse`:

- The document must be schema 1, `reuse == true`, `sha == $GITHUB_SHA`, `event_name == "push"`.
- A required job whose `needs` result is `skipped` is success iff its display name is in the document's `jobs[]` (for plan-derived names, `<job id> [<lane digest>]`) and a live `GET /repos/{r}/actions/jobs/{job_id}` still returns `conclusion == "success"` and `head_sha == pr_head_sha`. This is the same "re-verify live before a skip counts" principle as title-edit reuse (`TEST-001`, `ci_lint.runtime.gate._verify_reuse`).
- Without a token: `needs_review`, never a silent pass (as today). A job not in the document, or failing re-verification: the existing `TEST-001` failure.
- The gate's text output adds `reused from PR #<pr> run <run_id>` per accepted job.

### 4.7 Sequence diagrams

Squash-merged PR (the common case, 60% of clud merges):

```
PR author        GitHub                         main push run (ci.yml)             GitHub API
   | push head H  |                                      |                               |
   |------------->| pull_request run R on H: green       |                               |
   | merge (squash)                                      |                               |
   |------------->| creates S (tree(S) == tree(H)),       |                               |
   |              | push event on main ----------------->| static: reuse-check --sha S   |
   |              |                                      |-- GET commits/S/pulls ------->| [#N merged, merge_commit_sha=S, head H]
   |              |                                      |-- GET git/commits/S --------->| tree T
   |              |                                      |-- GET git/commits/H --------->| tree T   (equal)
   |              |                                      |-- GET runs?head_sha=H&event=pull_request -> R newest decisive, success
   |              |                                      |-- GET runs/R/jobs ----------->| every required job success, fresh
   |              |                                      | reuse=true (enforce) -> validation jobs skipped,
   |              |                                      | writers run, CI OK green + provenance
```

Rebase-merged PR: GitHub rewrites each PR commit onto `main`; the push's tip is X with `merge_commit_sha = X`. Only X's tree is compared with the PR head's tree; intermediate commits are irrelevant. If `main` moved since the head's base, tree(X) contains `main`'s newer changes and differs from tree(H): `tree-mismatch`, run everything.

```
merge (rebase) -> commits A', B', X on main; push event after=X
reuse-check --sha X: commits/X/pulls -> #N (merge_commit_sha == X) -> tree(X) vs tree(H) -> equal? proceed : tree-mismatch
```

Merge-commit PR: the merge commit M has two parents; `merge_commit_sha = M`. tree(M) equals tree(H) exactly when the head already contained `main` (e.g. the PR branch was updated by merging `main` and CI ran on that head), or when `main` did not move. Conflicts resolved at merge time produce a tree no PR run tested: `tree-mismatch`.

Direct push (no PR, e.g. a bot or docs commit):

```
push event after=D -> commits/D/pulls -> [] (or no merged PR with merge_commit_sha == D) -> no-associated-pr -> run everything (1 API call)
```

Revert commit: a revert made through a PR follows the PR paths above (it is just a PR). A `git revert` pushed directly is a direct push. A revert whose tree happens to equal an older `main` tree is **not** reused from that older run: association is only ever to the PR that introduced the pushed commit.

Tag push: `GITHUB_REF=refs/tags/...` -> `ref-not-default-branch`, zero API calls; release workflows never reuse.

`workflow_dispatch` (e.g. clud's full-matrix `candidate_sha` proofs): `event-not-push`, zero API calls. The decision step's own `if:` already excludes it; the command re-checks.

PR whose head CI never ran (merged without CI, e.g. clud PRs #1514/#1515 in the 09-27 incident):

```
commits/S/pulls -> #N, head H -> trees equal -> runs?head_sha=H&event=pull_request -> none for ci.yml -> no-successful-run -> run everything
```

## 5. The decision procedure

### 5.1 Steps

Inputs: `repo`, `sha`, `workflows[]`, `required_jobs[]`, `max_age_hours`, `mode`, `default_branch`, `event_name`, `ref`, `now`.

```
0. Preconditions (no API call):
   event_name != "push"                          -> event-not-push
   ref != "refs/heads/" + default_branch         -> ref-not-default-branch
   no token                                      -> no-token

1. GET /repos/{repo}/commits/{sha}/pulls?per_page=100
   keep PRs with merged_at != null
              and base.ref == default_branch
              and merge_commit_sha == sha
   0 kept -> no-associated-pr ; >1 kept -> ambiguous-pr
   pr.number / pr.head.sha (40-hex) missing      -> api-malformed
   pr.head.repo.full_name != repo (or null)      -> fork-head

2. GET /repos/{repo}/git/commits/{sha}           -> tree.sha  (T_push)
   GET /repos/{repo}/git/commits/{pr.head.sha}   -> tree.sha  (T_head)
   T_push != T_head                              -> tree-mismatch

3. GET /repos/{repo}/actions/runs?head_sha={pr.head.sha}&event=pull_request&per_page=100&page=1..3
   (> 300 runs -> too-many-runs)
   for each workflow W:
     candidates = runs with path == W, head_sha == pr.head.sha, event == pull_request,
                  created_at <= now; newest first by (created_at, id)
     walk candidates:
       status != completed                        -> run-in-progress
       conclusion in {cancelled, skipped, stale}  -> pass over (no signal)
       conclusion != success                      -> newest-run-not-success
       head_repository.full_name != repo          -> fork-run
       else select this run; stop
     nothing selected                              -> no-successful-run

4. for each selected run R:
     GET /repos/{repo}/actions/runs/{R.id}/jobs?filter=latest&per_page=100&page=1..3
     (> 300 jobs -> too-many-jobs)

5. for each required name J (exact match on job `name` across the selected runs' jobs):
     no job named J                               -> required-job-missing
     any match with status != completed or conclusion != success -> required-job-not-success
     completed_at missing/unparsable              -> api-malformed
     completed_at > now                           -> run-after-decision
     completed_at < now - max_age_hours           -> stale-run

6. verified. reuse = (mode == "enforce"); would_reuse = true.
```

Any non-200 response, transport failure, or JSON error in steps 1-4 -> `api-error` (or `rate-limited` for HTTP 429, or 403 whose message mentions a rate limit). Nothing in the procedure raises to the caller.

Notes:

- `merge_commit_sha` is documented by GitHub as the squash commit (squash merge), the merge commit (merge commit), or "the commit that the base branch was updated to" (rebase merge), so the binding in step 1 is uniform across merge methods and rejects a push whose tip was not produced by that PR's merge.
- `filter=latest` returns the latest attempt of each job, which is what the run's conclusion reflects. `run_attempt` is recorded for the report.
- The evaluation clock `now` is the current time in a live decision. `reuse-report` sets it to each historical push run's `created_at`, which is why runs created after `now` are ignored and jobs completed after it are rejected.

### 5.2 Pagination

`per_page=100` and `page=1..3` for the runs-on-head listing and each jobs listing; the walk stops at the first page shorter than 100 or once `total_count` is covered. More than 300 entries fails closed (`too-many-runs`, `too-many-jobs`) rather than evaluating a truncated list. `commits/{sha}/pulls` is read as one page of 100 (a commit maps to at most a handful of PRs; if more, the extra PRs could only add ambiguity, and the filter already requires exactly one).

### 5.3 Rate-limit budget

| Outcome | GET calls |
| --- | --- |
| Precondition failure (`event-not-push`, `ref-not-default-branch`, `no-token`) | 0 |
| `no-associated-pr`, `ambiguous-pr`, `fork-head` | 1 |
| `tree-mismatch` | 3 |
| `verified` (one workflow, <= 100 runs and jobs) | 5 (measured: clud #1643, #1575) |
| Worst case, W workflows | 1 + 2 + 3 + 3W (9 for W = 1) |

`GITHUB_TOKEN` allows 1,000 REST requests per hour per repository (15,000 on GitHub Enterprise Cloud). clud's ~30 `main` pushes/day at 5 calls is ~150 calls/day. `reuse-report` costs about 5 calls per evaluated push run plus one per 100 listed runs (about 500 for 100 runs), so run it with a personal token or spread it out.

### 5.4 Permissions

| Call | Fine-grained / `GITHUB_TOKEN` permission |
| --- | --- |
| `GET /repos/{r}/commits/{sha}/pulls` | `contents: read` and `pull-requests: read` |
| `GET /repos/{r}/git/commits/{sha}` | `contents: read` |
| `GET /repos/{r}/actions/runs`, `.../runs/{id}/jobs` | `actions: read` |

Grant them on the decision job only:

```yaml
permissions:
  contents: read
  actions: read
  pull-requests: read
```

In a `ci.toml` repository `pull-requests: read` is a per-job grant beyond `SEC-002`'s free `contents: read`/`actions: read`, so it needs `[allow].permissions."pull-requests: read" = ["<decision job id>"]`. Unauthenticated access (60 requests/hour per IP, shared by hosted runners) is not supported: without a token the decision is `no-token`.

### 5.5 Caching

Within one process identical URLs are fetched once (`_Api.cache`); `reuse-report` benefits because consecutive pushes often share PR heads and runs. Nothing is cached across processes or runs: a decision always reads current API state, so no stale cache can ever widen a proof. HTTP conditional requests (`If-None-Match`) are a possible later optimization; they do not change semantics.

### 5.6 Fail-closed table

Every reason `reuse-check` can report. All except `verified` mean `reuse=false`, `would_reuse=false`, exit 0.

| Reason | Condition | Typical cause |
| --- | --- | --- |
| `verified` | All conditions hold. | Squash/rebase/merge-commit PR with a green identical head. |
| `event-not-push` | `event_name` is not `push`. | PR, `workflow_dispatch`, `schedule`, `merge_group`. |
| `ref-not-default-branch` | `ref` is not `refs/heads/<default>`. | Tag push, push to another branch. |
| `no-token` | No `GITHUB_TOKEN`/`GH_TOKEN`. | Local run, missing `env:`. |
| `api-error` | Non-200, transport failure, or invalid JSON. | Outage, missing permission (403/404). |
| `rate-limited` | HTTP 429, or 403 mentioning a rate limit. | Exhausted token budget. |
| `api-malformed` | A required field is missing or mistyped. | API change; truncated body. |
| `no-associated-pr` | No PR with `merged_at`, `base.ref == default`, `merge_commit_sha == sha`. | Direct push; association not indexed yet (open question 15.3). |
| `ambiguous-pr` | More than one such PR. | Unusual stacked/duplicate merges. |
| `fork-head` | The PR head is in another (or a deleted) repository. | Fork PR. |
| `tree-mismatch` | Pushed tree != PR head tree. | `main` moved and the head was not updated; merge-time conflict resolution. |
| `too-many-runs` | More than 300 runs on the head SHA. | Pathological re-run loops. |
| `run-in-progress` | The newest non-cancelled run of the workflow on the head has not completed. | Label change right before merge. |
| `newest-run-not-success` | The newest decisive run concluded `failure`, `timed_out`, `action_required`, `neutral`, `startup_failure`, ... | Merged on red; a later label run failed. |
| `no-successful-run` | No decisive run of the workflow on the head. | Merged without CI (clud #1514/#1515); only cancelled runs. |
| `fork-run` | The proving run's `head_repository` is not this repository. | Fork run. |
| `too-many-jobs` | More than 300 jobs in a proving run. | Very large matrix. |
| `required-job-missing` | No job with that exact display name in the proving run(s). | Renamed job; different tier; pre-rename history (clud #1575 vs the sharded names). |
| `required-job-not-success` | A required job is skipped, cancelled, neutral, failed, or not completed. | Iteration-mode run (`ci-windows`); a skipped lane. |
| `ambiguous-required-job` | A required job display name matches more than one job in the proving run and not all succeeded. | Two jobs sharing a display name (soldr: `lint-docs` + `lint`, both `Lint`), where one skips by design. The name proves nothing; give each a distinct name. |
| `stale-run` | A proving job completed more than `max_age_hours` before `now`. | PR validated days before merge. |
| `run-after-decision` | A proving job completed after `now`. | Only in retroactive evaluation (`reuse-report`). |

### 5.7 Merge-queue evidence (`--queue-evidence`, opt-in; #419, implemented in #423/#424/#429)

A merge queue merges a PR that is behind the default branch, so the pushed tree differs from the PR head's and the procedure above answers `tree-mismatch` -- even though the queue already validated the exact commit this push introduces. The default-branch push then reruns every lane the queue run just ran. `--queue-evidence` (off by default; `reuse` itself never turns on implicitly) adds one fallback to step 3:

- **Trigger.** The PR-head proof fails with `tree-mismatch`, `no-successful-run`, or `required-job-not-success` (`QUEUE_FALLBACK_REASONS`). The last one covers a locally attested PR head (GATE-008): its PR run *skips* the routine lanes, so per-job proof on the head cannot hold, while the queue run executes those very lanes (#429).
- **Fallback evidence.** The newest decisive `merge_group` run whose `head_sha` is the pushed commit itself, selected with the same `_select_run` / `_check_jobs` rules parameterized by event: per-workflow selection, per-job `success`, fork and freshness checks all apply unchanged. Tree identity needs no separate check -- the queue tested this exact commit.
- **Still bound to a PR.** Steps 1-2 run first: the merged-PR association is required, so a direct push never reaches queue evidence. (The queue can only ever exist for a PR merge, but the binding is checked, not assumed.)
- **Refusals are their own.** No `merge_group` run at all leaves the PR-head reason standing (`no-successful-run` on the queue falls back to the original `head_stop`). A red, unfinished, stale, forked, or lane-skipping queue run refuses with its own reason -- a `required-job-not-success` *in the queue run* is a real catch, not answerable by fallback. No new reason codes are introduced; `event-not-push` above still holds: the decision only ever runs on a `push`, and a `merge_group` run is evidence, never a decision point.
- **`proving_sha` and the consumer (#424).** The decision document's `proving_sha` names the commit the proving jobs ran on: the PR head normally, the pushed commit under queue evidence. `ci-lint gate --default-branch-reuse` re-fetches each proving job and requires `head_sha == proving_sha` (falling back to `pr_head_sha` for documents that predate the field), so a skip is accepted only when the proving jobs really ran on this push's commit. Without this, every queue-proven skip would have failed `CI OK` (queue runs execute on the pushed commit, not the PR head).
- **Scope.** This saves the *default-branch push* rerun. Skipping lanes *inside* the `merge_group` run itself (queue-time attested skip) is a separate, unimplemented idea -- see #419's first acceptance bullet.

Tests: `QueueEvidenceTest` (6 cases: identical-commit queue run reuses; no queue run keeps the PR-head reason; red queue run refuses; built from clud #1630's tree-mismatch recording plus #1643's run/jobs), `AttestedHeadTest` (#429: a queue run proves a skipped attested head; no queue run keeps the refusal; a skipped lane in the queue run refuses), and the gate's `proving_sha` cases (queue doc verified; a jobs-on-a-third-commit mismatch rejected; documents without `proving_sha` fall back to `pr_head_sha`). Adoption reference: talos-hq/clud#2048 wiring, live on talos-hq/clud#2135 → main push 40153941757 (340 s re-run) answered by merge_group run 40153565385.

## 6. Edge-case catalogue

| Case | Decision | Why |
| --- | --- | --- |
| Squash merge, head up to date with `main` | reuse (if the head run is green and fresh) | tree(squash) == tree(head); `merge_commit_sha` = the squash commit. clud #1643 fixture. |
| Squash merge, `main` moved, head not updated | no reuse (`tree-mismatch`) | The merged tree contains changes the PR run never saw. clud #1630 fixture (a real catch). |
| Rebase merge (several commits land) | reuse iff tree(tip) == tree(head) | Only the tip's tree matters; `merge_commit_sha` is the tip. Intermediate commits are never deployed as a run's final state by this push. |
| Merge commit, conflicts resolved at merge time | no reuse (`tree-mismatch`) | The resolution is untested bytes. |
| PR branch updated by merging `main`, CI green on that head | reuse | The updated head is the tested head; its tree equals the squash result. |
| Multiple PRs map to one commit | no reuse (`ambiguous-pr`) | Cannot tell which PR's evidence applies. |
| Stacked PRs (B based on A, A merged, B retargeted and merged) | reuse iff B's final head tree == pushed tree and B's newest decisive run on that head is green | B's run may have tested against A's branch as the base (3.3 residual); tree identity still pins the bytes. Shadow data will show how often this occurs. |
| Bot/docs-only direct push | no reuse (`no-associated-pr`) | No PR evidence. A docs-only change is a `GEN-007`/paths question, not reuse. |
| Revert via PR | same as any PR | A revert is a PR. |
| Revert pushed directly | no reuse (`no-associated-pr`) | Never matched to an older run with the same tree. |
| Cherry-pick pushed to `main` | no reuse (`no-associated-pr`) | Not introduced by a merged PR. |
| Force-push after CI, then merge | uses the final head only | Runs are looked up by the final `head.sha`; the pre-force-push runs are for another SHA. |
| Head green, later commit on the PR failed, merged anyway | no reuse (`newest-run-not-success`) | The later commit is the final head; its run failed. |
| Same head: minimal run green, later `ci-windows` label run green | no reuse (`required-job-not-success`) | Newest decisive run skipped the Linux jobs (test `test_newer_iteration_mode_run_disqualifies_older_green_minimal_run`). |
| Same head: newer run cancelled | reuse from the older green run | Cancelled carries no signal. |
| Same head: newer run in progress at merge | no reuse (`run-in-progress`) | Outcome unknown. |
| Failed job re-run to green (`run_attempt` 2) | reuse | `filter=latest` reflects the latest attempt; `reuse-report` shows the attempt. |
| Partial re-run days later | freshness per job | Unchanged jobs keep old `completed_at` and can be `stale-run`. |
| Matrix job (`Test linux-x64 (unit)` with `shard: [rust, py1of2, py2of2]`) | each leg is its own required name | GitHub names legs `<name> (<value>)`; through a reusable workflow, `<name> (<value>) / <callee job name>`, e.g. `Test linux-x64 (unit) (py1of2) / x86_64-unknown-linux-gnu unit`. |
| Reusable-workflow job naming | exact names from a real run | `Build linux-x64 / x86_64-unknown-linux-gnu`, `Dylint / Dylint`. Copy names from `GET .../runs/{id}/jobs` of a green run, never guess (section 13.3). |
| Path-filtered workflow (`on.pull_request.paths`) that did not run on the PR | no reuse (`no-successful-run`) | No evidence. If the push is also path-filtered out, nothing runs anyway. |
| A required job that legitimately skips on PRs but runs on `main` (e.g. a `main`-only integration lane) | no reuse (`required-job-not-success`) if listed; if it is not listed it must not consume `reuse` | The PR never proved it. Such a job must keep running on `main`; the static coverage check flags it if it consumes `reuse` without a `--required-job`. |
| PR changed workflow files (including the reuse wiring or required-job list) | normal rules | The workflow is in the tree; tree identity means the push runs the same definition the head carries. The PR's own run used the test-merge definition (3.3). |
| Iteration-mode PR (clud `ci-windows`) merged | no reuse | Linux jobs skipped in the proving run. |
| Fork PR | no reuse (`fork-head`) | Restricted token/secrets; different trust. |
| Concurrency cancellation of the `main` run by a newer push | irrelevant to the decision | clud's `main` concurrency group uses `run_id`, so `main` runs are not cancelled by each other. |
| `commits/{sha}/pulls` not yet indexed right after a merge | no reuse (`no-associated-pr`) | Costs a full run, never a skip; Phase 1 measures it (15.3). |
| Queue merges a behind-base PR (single-PR group), `--queue-evidence`, queue run green and fresh | reuse, `proving_sha` = the pushed commit | The queue validated exactly this commit; the PR-head `tree-mismatch` is answered by the `merge_group` run (5.7, #423). |
| Same, but no `merge_group` run exists | no reuse, PR-head reason stands | Absence is not evidence; the fallback only *substitutes* proof, it never invents it (#423). |
| Same, but the queue run is red, unfinished, stale, or skips a required lane | no reuse, queue run's own reason | The queue is the post-merge catch too; a lane-skipping queue run is a real catch (#429). |
| Locally attested PR head (GATE-008) whose PR run skipped the routine lanes, queue run green | reuse via `required-job-not-success` fallback | The attested head proved its lanes locally, the queue executed them remotely; per-job proof on the head alone could never hold (#429). |
| Batched queue group (2+ PRs landing as one commit) | association decides first: several PRs with `merge_commit_sha == sha` -> `ambiguous-pr`, no reuse; exactly one associated PR -> the usual PR-head proof first, then 5.7's queue fallback against the batched queue run | The batched queue run tested exactly the landed tree, so a green one is sound evidence for the push; the ambiguity guard never guesses which PR's evidence applies. The group-size-1 restriction in #419's *proposal* governs the unimplemented queue-time skip (5.7 scope), not this push-side fallback. |

## 7. Configuration design (`[reuse.default-branch]`, Phase 2)

Phase 0 ships the command with CLI flags only. The schema-3 table below was specified exactly for Phase 2 (#158) and **is implemented** (`ci_lint.schema.ReuseConfig`/`_parse_reuse`, consumed by `ci-lint gate --default-branch-reuse`) -- see docs/ci-toml.md's `[reuse.default-branch]` section for the shipped field semantics. The planner's `"plan"` derivation of `required_jobs` and the static cross-checks listed at the end of this section remain candidates.

```toml
[reuse.default-branch]
mode = "shadow"                         # "off" | "shadow" | "enforce"   (default "off")
workflows = ["ci.yml"]                  # non-empty array of workflow file names (default ["ci.yml"])
required-jobs = "plan"                  # "plan" | non-empty array of exact job display names
exempt-jobs = ["dylint", "build-linux-x64"]   # job ids that never skip on reuse (cache writers)
max-age-hours = 24                      # number, 0 < x <= 168                 (default 24)
nightly = true                          # bool: a scheduled full default-branch run exists (default true)
```

| Field | Type | Default | Validation (`ci_lint.schema._parse_reuse` -> `ReuseConfig`) |
| --- | --- | --- | --- |
| `mode` | string | `"off"` | One of `off`, `shadow`, `enforce`; else `CT-002`. |
| `workflows` | array of string | `["ci.yml"]` | Non-empty; each entry must be in `[allow].workflows`; else `CT-002`. |
| `required-jobs` | `"plan"` or array of string | `"plan"` | Array must be non-empty with unique non-empty strings; any other string `CT-002`. |
| `exempt-jobs` | array of string | `[]` | Each must be a job id in the workflow (static cross-check, `GEN-021`). |
| `max-age-hours` | int or float | `24` | `0 < x <= 168`; else `CT-002`. |
| `nightly` | bool | `true` | `mode = "enforce"` with `nightly = true` requires an `on.schedule` trigger in a listed workflow (static, `GEN-021`). |

Unknown keys are `CT-001` (the loader's existing strictness). `ReuseConfig` is a frozen dataclass: `mode: str`, `workflows: tuple[str, ...]`, `required_jobs: tuple[str, ...] | None` (`None` = `"plan"`), `exempt_jobs: tuple[str, ...]`, `max_age_hours: float`, `nightly: bool`.

**`required-jobs = "plan"`.** The planner computes the main-flow plan for the pushed tree and emits `reuse_required_json`: for every id in `required_jobs` except `precheck` and the gate, the display name the template gives it, `<id> [<lane digest>]` (and `platform-build (<platform>) [<digest>]`/`platform-run (<platform>) [<digest>]` per platform lane). Lane digests already encode each lane's platform, target, suites, and environment fingerprint, so **a digest match is a mechanical "same tier" proof**: if the PR flow selected fewer suites than the main flow, the digests differ and the decision is `required-job-missing`. This is the natural fit for `ci.toml` repositories and the reason Phase 4 prefers it to a hand-maintained list.

**Static cross-checks added in Phase 2** (extending `ci_lint.rules.default_branch_skip`): the `reuse-check` invocation's `--mode`, `--max-age-hours`, and `--required-job` values agree with the table; no `exempt-jobs` entry consumes `reuse`; every job consuming `reuse` is covered by `required-jobs`; `mode = "enforce"` has the scheduled run when `nightly = true`.

## 8. Cache writers and the scheduled run

Default-branch caches are written only by default-branch pushes and the nightly flow (`CACHE-003`, `[cache].write-on`). Skipping a writer job on a reused push stops that write. Two supported strategies:

| | (i) Keep writers, skip validation | (ii) Skip everything, write on a schedule |
| --- | --- | --- |
| On a reused push | writers (clud: Build linux-x64 240 s, Dylint 269 s) run; validation (Clippy 122 s, unit 77+105+106 s) skips | only the decision host (Static 28 s) and `CI OK` (4 s) run |
| Skipped per clud run | 410 s of 951 s (43.1%) | 919 s of 951 s (96.6%) |
| Cache freshness | every `main` push refreshes caches | caches refresh once per scheduled run (daily); PRs restore up-to-a-day-old base layers |
| `CACHE-004` budget | unchanged (same writers, same keys) | unchanged or lower (fewer lockfile-peak coexistence windows); the scheduled run should `pre-prune` |
| Scheduled run | recommended; required in `enforce` (drift backstop) | required: it is the cache writer and the drift backstop |
| Scheduled run cost (clud) | 15.9 runner-min/day (one full minimal-tier run) | 15.9 runner-min/day (writers alone would be 8.5) |
| When to choose | caches whose staleness costs PRs noticeably (large dependency graphs); writers that are also validation (clud's Dylint runs the custom lint) | writers that are cheap to be stale, or already frozen |

The scheduled run is a **full** default-branch-tier run (validation + writers), not a writer-only run: it doubles as the drift backstop of section 3.4. A writer-only schedule (8.5 runner-min/day for clud) is acceptable under (ii) only if the repository already has another daily full validation.

clud today: its Linux build cache is frozen by an oversize-payload skip on the default branch (zackees/ci.yml#153, `CACHE-022` candidate), so strategy (ii) loses nothing for that cache right now; the Dylint cache is a real `main`-only writer, so (ii) would make Dylint restores up to a day old. Recommendation for clud Phase 2: strategy (i) first (lowest risk), reconsider (ii) after #153 is fixed and cache hit rates are measured.

## 9. Shadow mode and proof of zero loss (the rollout gate)

### 9.1 What shadow mode does

`--mode shadow` computes the full decision, reports `would_reuse`, and always sets `reuse=false`. No job is skipped; the workflow's behavior is unchanged.

### 9.2 Records

1. **Step summary** of the decision step on every default-branch push (section 4.3): the human record.
2. **`CI OK` summary line**: `reuse: shadow would_reuse=<bool> reason=<code> PR #<n> <run_url>` (repository script, Phase 1).
3. **Optional artifact**: `--out reuse-check.json` uploaded as `reuse-check-${{ github.run_id }}` with 30-day retention. Not required for promotion.
4. **`ci-lint reuse-report`** (implemented in Phase 0): the authoritative evidence. It does not read artifacts or logs; it re-evaluates every completed default-branch push run of the workflow since a date with the clock set to each run's `created_at`, and compares the verdict with what the push run actually concluded. It is deterministic, re-runnable, and works retroactively, so a repository can compute its shadow evidence for the last 14 days **before** adopting anything.

```
python3 -m ci_lint reuse-report --repo OWNER/NAME --since YYYY-MM-DD [--until YYYY-MM-DD]
    --workflow ci.yml --required-job NAME ... [--max-age-hours 24] [--default-branch main]
    [--min-runs 100] [--limit 300] [--accept-flaky RUN_ID ...] [--json] [--record F | --replay F]
```

Per push run it reports one outcome:

| Outcome | Meaning |
| --- | --- |
| `safe-skip` | `would_reuse` and the push run concluded `success`. |
| `false-reuse-candidate` | `would_reuse` but the push run failed (`failure`, `timed_out`, `startup_failure`). Blocks promotion until analysed. |
| `accepted-flaky` | A candidate the analyst passed with `--accept-flaky <run id>` after proving it a known flake. |
| `must-run` | No proof (any reason); the push run's result is its own. |
| `no-signal` | The push run was cancelled/skipped/neutral; excluded from the sample. |

Summary: decisive runs, `would_reuse` count and rate, safe skips, candidate URLs, accepted flakes, must-run reasons histogram, API calls, and a verdict: `promotable`, `no-op`, `insufficient-sample`, `blocked`, or `error`. Exit code 0 only for `promotable`.

### 9.3 Promotion criteria (shadow -> enforce)

All of:

1. **One verified, attested merge is enough.** `reuse-report` reports verdict `promotable` — meaning at least one decisive run was a `safe-skip` and there are zero `false-reuse-candidate`s. This is the default (`--min-runs 1`).

   *Revised 2026-10-04.* The original criterion here demanded 100 decisive runs (30 for low-volume repositories, with owner sign-off). That quota bought confidence in the **tool**, not soundness in the **decision**: each individual decision already stands on its own (exactly one merged PR, an identical tree, a green and fresh per-job proving run). Requiring a large sample before trusting that bought very little -- by the rule of three, 100 clean runs only bounds the false-reuse rate at ~3%, and even 12 bounds it at ~25% -- while permanently excluding low-traffic repositories, which can never accumulate the quota and therefore could never adopt the mechanism at all. Measured on clud, the reuse rate is 42.5% (17 safe skips in 40 decisive runs) with 0 candidates.

   `--min-runs N` remains available to stage a rollout deliberately; only the default moved. The `no-op` verdict exists so a window in which reuse never fired once (every push a must-run: squash batches, drifted trees) reports honestly that there is nothing to promote rather than looking like a clean pass.
2. Every `false-reuse-candidate` in the window is analysed in the promotion issue. It may be passed with `--accept-flaky` only if it is a **known flake**: (a) the same test failed on an unrelated run (different tree, PR or default branch) within 7 days, or (b) re-running the same SHA passed (the push run's own re-run, or a manual re-run). Any candidate that is not a known flake blocks promotion: find why tree identity plus per-job proof missed it (environment drift -> consider a smaller `max-age-hours` or moving the test to the scheduled lane; a job missing from the required list -> fix the list), fix it, and restart the window.
3. The scheduled full default-branch run exists and is green on the last 3 days. **This, not the run count, is the real drift backstop** — which is why the run count above can be small.
4. For clud specifically: the `test_codex_installer_rm` flake (7 of 12 red `main` runs, 09-29/30) shows zero recurrences after clud#1596 (merged 2026-09-30T04:46Z), tracked in zackees/clud#1651.

Replayed on the recorded clud sample (test `ReportTest`), the flake-class push run for PR #1575 is exactly a `false-reuse-candidate` that analysis accepts as a known flake, the PR #1630 push run is `must-run` (a real catch reuse would never have skipped), and the PR #1643 push run is a `safe-skip`.

### 9.4 After promotion

In `enforce` mode a reused push run's own result is no longer independent evidence (it skipped the validation jobs), so `reuse-report` stops being a loss detector. Post-promotion monitoring is the scheduled full run: any failure there on a tree that was reused within the last day is a potential false reuse and is triaged like a Phase 1 candidate. Reverting to `--mode shadow` is a one-line change.

## 10. Observability and cost model

### 10.1 Metrics per repository

| Metric | Source | Phase |
| --- | --- | --- |
| Default-branch share of CI runner time | runs + jobs API, summed job execution seconds by event (queue excluded) | 3 (`FLEET-003`) |
| Reuse rate (`would_reuse` / decisive runs) | `reuse-report` | 0 |
| False-reuse count (unaccepted candidates) | `reuse-report` | 0 |
| Decision latency | the decision step's duration in the jobs API `steps[]` | 1 |
| API calls per decision | `api_calls` in the JSON document | 0 |
| Must-run reasons histogram | `reuse-report` | 0 |

### 10.2 Savings formula

Let `M` = default-branch runner-minutes in a window of `W` hours, `e` = eligible fraction (identical tree AND validated head), `f` = fraction of a default-branch run's job time skipped on reuse, `N` = scheduled-run cost per day.

```
D = M * 24 / W                 # default-branch runner-minutes per day
S = e * D * f - N              # net runner-minutes saved per day
```

clud inputs (40 h window, 2,533 runner-min total, `main` 562 min = 22%, 50 `main` runs): `D = 562 * 24 / 40 = 337.2` min/day (~30 runs/day). `e = 0.60` (45 of 75 PRs). A current full `main` run (36692419899) is 951 s: Static 28, Dylint 269, Build 240, Clippy 122, unit 77 + 105 + 106, CI OK 4.

| Scenario | `f` | Gross/day | `N` | Net/day | Share of all CI time (1,519.8 min/day) |
| --- | --- | --- | --- | --- | --- |
| Brief A: skip everything on eligible runs | 1.0 | 202 | 0 | ~200 | 13% |
| Brief B: skip Static/Clippy/unit/CI OK, keep Build + Dylint | 0.465 (442/951) | 94 | 0 | ~94 | 6% |
| Brief C: skip everything + nightly writer | 1.0 | 202 | 8.5 | ~190 | 12.5% |
| This design, strategy (i) | 0.431 (410/951: Static and CI OK always run) | 87 | 15.9 | 71 | 4.7% |
| This design, strategy (ii) | 0.966 (919/951) | 195 | 15.9 | 180 | 11.8% |

The brief's figures (the task statement that commissioned this design) count Static and `CI OK` as skippable and a writer-only nightly; this design keeps the decision host and the aggregator running and uses a full-tier scheduled run as the drift backstop, which is why its rows are lower. Assumptions: the window's average run cost (11.2 min, including cancelled/failed runs and pre-sharding runs) stands for the steady state; eligible runs cost the same as ineligible ones; skipped jobs free their runner-minutes entirely.

Sensitivity (net/day):

| `e` | Strategy (i) | Strategy (ii) |
| --- | --- | --- |
| 0.40 | 42 | 114 |
| 0.60 | 71 | 180 |
| 0.80 | 100 | 245 |

Using the current full-run cost (15.9 min x 30 runs/day = 475.5 min/day) instead of the window average raises strategy (ii) at `e = 0.60` to 260 min/day. Second-order benefit, not quantified: fewer concurrent `main` runs shorten PR runner queues (44-149 s observed when several CIs overlapped).

## 11. Alternatives considered

| Alternative | Benefit | Cost | Verdict |
| --- | --- | --- | --- |
| (a) GitHub merge queue | Validates the exact merge commit; `main` is fast-forwarded to it, so a `push` re-run is truly redundant; removes the 3.3 residual. | Needs live rulesets (clud has none); clud's docs promise a full matrix per queued merge (~121 vs ~16 runner-min, x ~30 merges/day = ~3,600 min/day); queue latency conflicts with clud's immediate-merge tooling. | Accepted as an **alternative justification only when live settings prove it** (`GEN-010`'s live half, `ci-lint audit`), with the queue running the minimal tier, recorded as a `GEN-021` `[[exceptions]]` entry. Not a replacement for verified reuse in repos without a queue. |
| (b) Drop `push: main` CI | Saves all 337 min/day. | Loses the only test of 40% of merged trees (clud #1630-class catches, the 09-27 broken-`main` incident) and every default-branch cache write (`CACHE-003`). | Rejected. |
| (c) Schedule-only validation of `main` | Cheap. | Detection latency up to a day; a failure's blame spans ~30 merges; caches written once a day. | Rejected as a replacement; adopted as the backstop (section 8). |
| (d) Path filters | Skips runs whose changes cannot affect a lane (`GEN-007`). | Orthogonal: does not address identical trees; a path-filtered required check can go missing; blast-radius reasoning is error-prone. | Complementary, not a substitute. |
| (e) Test the merge ref (`refs/pull/N/merge`) on PRs | PR CI validates `merge(base, head)`; if `main` did not move before merge the pushed tree equals the tested tree even when the head is behind, raising eligibility above 60%, and 3.3's residual disappears. | The API does not expose the test-merge tree after the fact, so the PR run would have to record it (e.g. in a job name, as title-edit reuse does with lane digests); clud deliberately checks out the head SHA for provenance pinning. | A strong alternative worth its own evaluation (open question 15.5); out of scope for Phase 0-2. |
| (f) Strict required status checks ("branches must be up to date") | Forces every head to contain `main` before merge, pushing tree identity toward 100%. | Every merge restarts every other open PR's required checks (exactly `GEN-006`'s finding: strict without a queue multiplies cost by concurrent-PR count); clud has no branch protection at all. | Rejected. |

## 12. Rollout plan

The task brief that commissioned this design calls the work in this PR "Phase 1 of the design"; the rollout below numbers it **Phase 0** so that "Phase 1" names the first adopter.

| Phase | Scope | Owner | Entry | Exit | Issue |
| --- | --- | --- | --- | --- | --- |
| 0 | This design; `GEN-021` in policy-general.md; `ci-lint reuse-check` + `reuse-report`; static `GEN-021` precheck rule; fixtures; case study; AGENTS.md registry/index | ci.yml | #156 | PR merged, `ci-lint selftest` green | [#156](https://github.com/zackees/ci.yml/issues/156) |
| 1 | zackees/clud wires `reuse-check --mode shadow` in `static`; `CI OK` prints the decision; retroactive `reuse-report` baseline | clud (PR in clud), ci.yml reviews | Phase 0 merged | 14 days of shadow data; interim reports posted | [#157](https://github.com/zackees/ci.yml/issues/157) |
| 2 | Promotion to `enforce` on clud (strategy (i)); `[reuse.default-branch]` schema, static cross-checks, `"plan"` derivation, `ci-lint gate --default-branch-reuse`, optional association-lag retry, scheduled full run | ci.yml + clud | Phase 1 criteria (section 9.3) incl. the flake fix held (zackees/clud#1651) | First enforced reused `main` run green with provenance; no false reuse in 14 days of scheduled-run monitoring | [#158](https://github.com/zackees/ci.yml/issues/158) |
| 3 | `FLEET-003` in `ci-lint fleet scan`: default-branch share of runner time >= 20% with no `reuse-check` wiring and no live merge queue; `sync-issues` plan | ci.yml | Phase 2 enforced somewhere | Fleet scan reports `FLEET-003` with fixtures; first scan triaged | [#159](https://github.com/zackees/ci.yml/issues/159) |
| 4 | zackees/template-python-rust-cmd adopts `[reuse.default-branch]` with `required-jobs = "plan"`; `examples/rust-pypi-app/ci.toml` mirrors it | template + ci.yml | Phase 2 tooling merged | Template precheck clean; a reused `main` run with provenance; example drift clean | [#160](https://github.com/zackees/ci.yml/issues/160) |
| clud | Correct the false merge-queue claim (`ci.yml`, `docs/architecture/ci.md`) or configure a real queue; confirm the flake fix held | clud | now | `GEN-010` live half clean for clud | [zackees/clud#1651](https://github.com/zackees/clud/issues/1651) |

Deliverables of this PR by phase: all Phase 0. Nothing here changes any repository's CI behavior; adoption is opt-in per repository.

## 13. Reference wiring (documentation only)

This is a snippet inside a design document, not a workflow file. It shows clud's `ci.yml` with strategy (i) in **enforce** mode; Phase 1 uses the same wiring with `--mode shadow`. Job names are clud's real display names as of 2026-09-30 (run 36742138072). Actions are shown with placeholder SHAs; pin real 40-hex SHAs (`SEC-004`).

### 13.1 Workflow

```yaml
name: CI
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, unlabeled]
  push:
    branches: [main]
  schedule:
    - cron: "17 4 * * *"          # daily full default-branch run: drift backstop + cache writer
  workflow_dispatch:
    inputs:
      candidate_sha: { description: Exact reachable commit SHA to validate, required: true, type: string }

concurrency:
  group: ${{ github.workflow }}-${{ github.ref == 'refs/heads/main' && github.run_id || github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}

permissions:
  contents: read
  actions: read

jobs:
  static:
    name: Static checks
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    permissions:                  # the decision job only
      contents: read
      actions: read
      pull-requests: read
    outputs:
      mode: ${{ steps.mode.outputs.tier }}
      source_ref: ${{ steps.source.outputs.ref }}
      reuse: ${{ steps.reuse.outputs.reuse }}
      reuse_pr: ${{ steps.reuse.outputs.pr }}
      reuse_run_url: ${{ steps.reuse.outputs.run_url }}
      reuse_tree: ${{ steps.reuse.outputs.tree }}
    steps:
      - uses: actions/checkout@<40-hex sha>   # existing source checkout (clud pins head/candidate SHA)
      - name: Check out ci-lint
        if: github.event_name == 'push' && github.ref == 'refs/heads/main'
        uses: actions/checkout@<40-hex sha>
        with:
          repository: zackees/ci.yml
          ref: <40-hex zackees/ci.yml commit>
          path: .ci-lint
          persist-credentials: false
      - name: Verified reuse decision (GEN-021)
        id: reuse
        if: github.event_name == 'push' && github.ref == 'refs/heads/main'
        working-directory: .ci-lint
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: >-
          python3 -m ci_lint reuse-check --workflow ci.yml --mode enforce --github-output
          --required-job "Static checks"
          --required-job "Dylint / Dylint"
          --required-job "Clippy linux-x64 / x86_64-unknown-linux-gnu"
          --required-job "Build linux-x64 / x86_64-unknown-linux-gnu"
          --required-job "Test linux-x64 (unit) (rust) / x86_64-unknown-linux-gnu unit"
          --required-job "Test linux-x64 (unit) (py1of2) / x86_64-unknown-linux-gnu unit"
          --required-job "Test linux-x64 (unit) (py2of2) / x86_64-unknown-linux-gnu unit"
      # ... clud's existing provenance, mode-resolution, sync, setup-soldr and static-check steps ...

  dylint:                         # cache writer (strategy i): never consumes reuse
    name: Dylint
    needs: static
    uses: ./.github/workflows/_dylint.yml
    with:
      source_ref: ${{ needs.static.outputs.source_ref }}

  lint-linux-x64:                 # validation: skipped on a verified reuse
    name: Clippy linux-x64
    needs: static
    if: needs.static.outputs.reuse != 'true' && needs.static.outputs.mode != '' && needs.static.outputs.mode != 'windows'
    uses: ./.github/workflows/_build-target.yml
    with:
      source_ref: ${{ needs.static.outputs.source_ref }}
      target: x86_64-unknown-linux-gnu
      runs-on: ubuntu-24.04
      strategy: soldr
      clippy: true
      compile: false
      bundle: false
      save-cache: "false"

  build-linux-x64:                # cache writer (strategy i): never consumes reuse
    name: Build linux-x64
    needs: static
    if: needs.static.outputs.mode != '' && needs.static.outputs.mode != 'windows'
    uses: ./.github/workflows/_build-target.yml
    with:
      source_ref: ${{ needs.static.outputs.source_ref }}
      target: x86_64-unknown-linux-gnu
      runs-on: ubuntu-24.04
      strategy: soldr
      clippy: false

  test-linux-x64-unit:            # validation: skipped on a verified reuse
    name: Test linux-x64 (unit)
    needs: [static, build-linux-x64]
    if: needs.static.outputs.reuse != 'true'
    strategy:
      fail-fast: false
      matrix:
        shard: [rust, py1of2, py2of2]
    uses: ./.github/workflows/_run-tests.yml
    with:
      source_ref: ${{ needs.static.outputs.source_ref }}
      target: x86_64-unknown-linux-gnu
      runs-on: ubuntu-24.04
      suite: unit
      shard: ${{ matrix.shard }}

  # ... integration/harness/Windows/macOS/arm jobs unchanged: they never run in
  #     the minimal tier a main push resolves to, so they never consume reuse ...

  ci-ok:
    name: CI OK
    if: always()
    needs: [static, dylint, lint-linux-x64, build-linux-x64, test-linux-x64-unit]   # + the other tiers' jobs
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<40-hex sha>
      - name: Check results
        env:
          RESULTS: ${{ toJSON(needs) }}
          MODE: ${{ needs.static.outputs.mode }}
          REUSE: ${{ needs.static.outputs.reuse }}
          REUSE_SKIPPABLE: lint-linux-x64 test-linux-x64-unit
          REUSE_PR: ${{ needs.static.outputs.reuse_pr }}
          REUSE_RUN_URL: ${{ needs.static.outputs.reuse_run_url }}
          REUSE_TREE: ${{ needs.static.outputs.reuse_tree }}
        run: python3 ci/ci_ok.py      # section 4.5 semantics
```

`ci_lint/tests/fixtures/GEN-021/green/.github/workflows/ci.yml` is a trimmed copy of this wiring (in its Phase 1 `--mode shadow` form) and is checked clean by `GEN-021` in the test suite, so the snippet's shape and the rule cannot silently drift apart.

### 13.2 Strategy (ii) variant

Add `if: needs.static.outputs.reuse != 'true' && ...` to `dylint` and `build-linux-x64` too, list them in `REUSE_SKIPPABLE`, and make sure the `schedule` run resolves to a tier that runs (and saves from) both writers.

### 13.3 Keeping required-job names correct

- Copy names from a real green run of the target tier: `gh api repos/OWNER/NAME/actions/runs/<id>/jobs --jq '.jobs[].name'`. GitHub composes them as `<caller name> / <callee job name>` for reusable workflows and `<name> (<matrix values>)` for matrix legs (both at once for a matrix over a reusable workflow).
- A rename can never silently disable reuse in the unsafe direction: an unknown name is `required-job-missing` and the run executes everything (test `test_renamed_required_job_fails_closed`).
- `ci-lint precheck` (`GEN-021`) reports `needs_review` when a literal `--required-job` matches no job of the workflow, and when a job that consumes `reuse` is covered by no `--required-job`.
- In shadow mode, a persistent `required-job-missing` reason in `reuse-report`'s histogram is the signal that the list is stale.

## 14. Test plan and acceptance criteria per phase

### 14.1 Phase 0 (this PR) -- implemented

Runtime decision (`ci_lint/tests/test_default_branch_reuse.py`, replayed from live `--record` recordings of zackees/clud, no network):

| Failure mode / case | Test |
| --- | --- |
| Happy path, squash merge (clud #1643), 5 API calls | `test_squash_merge_pr1643_is_verified_in_five_calls` |
| Flake-class merge that reuse would have skipped (clud #1575) | `test_flake_class_pr1575_would_have_been_skipped` |
| Rebase merge: pushed SHA differs from the head SHA, trees equal | `test_rebase_merge_tip_with_new_sha_but_same_tree_is_verified` |
| Merge commit with two parents and the head's tree | `test_merge_commit_with_two_parents_and_head_tree_is_verified` |
| Newer cancelled run passed over | `test_newer_cancelled_run_is_passed_over` |
| Run created after the evaluation clock ignored | `test_run_created_after_the_clock_is_ignored` |
| Jobs pagination | `test_jobs_listing_is_paginated` |
| Shadow mode never reuses; outputs/summary | `test_shadow_mode_reports_would_reuse_but_never_reuses` |
| Tree mismatch (clud #1630), stops after 3 calls | `test_tree_mismatch_pr1630_stops_after_three_calls` |
| Zero PRs (direct push) | `test_zero_associated_prs_direct_push` |
| Open/unmerged PR association | `test_open_unmerged_pr_is_not_an_association` |
| PR merged as a different commit | `test_pr_whose_merge_commit_is_another_sha` |
| Ambiguous PRs | `test_ambiguous_prs` |
| Fork head / fork run | `test_fork_head_is_never_reused`, `test_fork_run_is_never_reused` |
| PR head CI failed | `test_pr_head_ci_failed` |
| No run of the named workflow | `test_no_run_of_the_named_workflow` |
| Newer run in progress | `test_newer_run_still_in_progress` |
| Wrong-mode run lacking the Linux jobs (`ci-windows`) | `test_newer_iteration_mode_run_disqualifies_older_green_minimal_run` |
| A required job skipped / cancelled / neutral | `test_required_job_skipped`, `test_required_job_cancelled_or_neutral` |
| Workflow conclusion success with a required job missing | `test_successful_run_with_a_required_job_missing` |
| Renamed job fails closed | `test_renamed_required_job_fails_closed` |
| Stale run (older than max age) | `test_stale_run` |
| Evidence after the clock | `test_job_completed_after_the_clock` |
| API error, transport failure | `test_api_error_fails_closed` |
| Rate limit (403 message, 429) | `test_rate_limit_fails_closed` |
| Malformed payload | `test_malformed_payload_fails_closed` |
| Too many runs | `test_too_many_runs_fails_closed` |
| PR / dispatch / schedule / tag / other-branch / no-token: zero API calls | `test_preconditions_make_no_api_call` |
| Every reason code documented here and in ci-toml.md | `test_every_reason_code_is_documented` |
| CLI: `$GITHUB_OUTPUT` keys, `--out`, step summary, exit 0 on no-proof, exit 2 on usage errors, no-token live run | `CliTest` |
| `reuse-report`: outcomes, promotion verdicts, rename effect, listing error, CLI exit codes | `ReportTest` |

Static rule (`ci_lint/tests/test_gen_021.py`, fixtures under `ci_lint/tests/fixtures/GEN-021/`):

| Case | Fixture / test |
| --- | --- |
| Blanket skip of gate-required jobs, no reuse decision -> violation | `red`, `test_red_blanket_skips_are_violations` |
| Reference wiring -> clean (incl. a PR-only job the gate does not need) | `green`, `test_green_reference_wiring_is_clean` |
| Unresolvable expression -> needs_review | `review-unresolvable` |
| No gate job -> needs_review, not violation | `review-no-gate` |
| Blanket skip beside a reuse decision -> needs_review | `review-partial` |
| Hand-written reuse flag -> needs_review | `review-unverified` |
| Uncovered skip-on-reuse job, unknown required name, `== 'false'` consumption -> needs_review | `review-coverage` |
| Expression classifier (skip / clean / unknown, default-branch name) | `ClassifyConditionTest` |
| Wired into `ci-lint precheck`; `[[exceptions]]` suppresses it | `PrecheckWiringTest` |

Acceptance: `python3 -m ci_lint selftest` passes; no `.github/workflows` file added to this repository; the rule found no violation and no false positive across the 133 existing fixture repositories, and none on the live workflows of zackees/template-python-rust-cmd, zackees/clud, zackees/zccache, zackees/kernal-api, or zackees/setup-soldr (2026-09-30; soldr, running-process, and FastLED/fbuild produce only `needs_review` findings for PR-only helper jobs with no recognizable gate job, or for event logic the classifier leaves unresolved).

### 14.2 Phase 1 (#157)

- Retroactive `reuse-report` baseline over clud's last 14 days attached before the clud PR merges.
- Every clud `main` push after adoption shows the decision block; no change to which jobs run.
- Median decision step duration and API calls recorded; association-lag count recorded.

### 14.3 Phase 2 (#158)

- RED -> GREEN schema fixtures for every validation error in section 7; cross-check fixtures; gate `--default-branch-reuse` tests replayed from recorded job responses (verified, wrong SHA, job no longer success, no token -> needs_review).
- Promotion criteria of section 9.3 met and recorded; first enforced reused run green with provenance; a non-eligible push runs everything.

### 14.4 Phase 3 (#159)

- RED (clud-shaped recording: 22% share, no wiring, no queue) -> `FLEET-003` violation; GREEN with wiring; GREEN with a live merge queue; `needs_review` on unreadable history.

### 14.5 Phase 4 (#160)

- Template precheck clean with `[reuse.default-branch]`; a reused template `main` run with provenance; `example drift` clean.

## 15. Open questions and unknowns

| # | Question | How to resolve |
| --- | --- | --- |
| 15.1 | The 15 cancelled clud `main` runs (all `Static checks`, 2026-09-28/29) are unexplained. | Phase 1: inspect their logs/cancellation source (`gh run view --log`), check whether they recur and whether a cancelled `static` would leave `reuse` empty (it would: everything downstream is skipped by the cancellation anyway, and `CI OK` fails). |
| 15.2 | The evidence is 5 days and 75 PRs; PR association used `GET /commits/{sha}/pulls`. | Phase 1's 14-day shadow window and `reuse-report` over more history; re-state eligibility in #157. |
| 15.3 | Does `commits/{sha}/pulls` lag right after a merge (the push event can arrive seconds after the merge)? | Phase 1 counts `no-associated-pr` on commits that are PR merges. If > 0, Phase 2 adds a bounded retry (<= 3 attempts, <= 20 s total) for that reason only. Lag costs runner time, never safety. |
| 15.4 | Organization-level or secondary rate limits (a 403 "secondary rate limit" was observed once during this investigation's own ad-hoc API use). | The decision is 5 calls; `rate-limited` fails closed. Monitor the reasons histogram; `reuse-report` should use a personal token. |
| 15.5 | Should fleet repositories test the merge ref on PRs (alternative 11e) and record the tested tree, making reuse exact and raising eligibility? | Separate evaluation issue after Phase 2; needs a way for the PR run to publish its tested tree as API-visible data. |
| 15.6 | Environment-drift exposure: how often does a green PR tree fail on `main` within 24 h for non-flaky reasons? | Shadow `false-reuse-candidate` analysis; tune `max-age-hours` per repository. |
| 15.7 | Stacked PRs and PRs retargeted between bases (3.3 residual). | Count them in shadow data (PR base ref at run time is not in run data; use the PR's timeline events if needed). |
| 15.8 | Decision latency on hosted runners. | Phase 1 metric; expected single-digit seconds (5 sequential GETs), not yet measured in CI. |
