# pool-select

Sends a job to the shared self-hosted runner pool (the Dell hosts) when the
pool has idle capacity, and to GitHub-hosted runners otherwise. Any repository
in an organization the pool serves (obs-rust, FastLED, forge-ci, talos-hq) can
use it. It generalizes obs-rust/obs-studio's `ci/select_mq_runner.py`.

A selector job runs the action and publishes `runs-on`, a JSON value. The
real job reads it with `runs-on: ${{ fromJSON(needs.pool.outputs.runs-on) }}`.
The selector itself runs on `forge-quick`, the self-hosted class for short
administrative jobs, so the routing decision does not wait in the
organization's hosted-runner queue; unset `FORGE_QUICK_LABELS` and it runs
hosted again.

## Decision

The action picks the pool only when all of these hold. Otherwise it picks
`fallback`.

1. `labels` is a nonempty JSON array. An empty value (or `[]`) means hosted,
   which makes an unset repository variable the per-repo kill switch.
2. With `trusted-only: true` (the default), the event is `push`,
   `merge_group`, `workflow_dispatch`, `schedule`, or a `pull_request` whose
   head repository is the base repository. A fork PR, any
   `pull_request_target`, and any other event run hosted, without an API call.
3. `token` is set. With no token the action warns and runs hosted.
4. `GET /orgs/{github.repository_owner}/actions/runners` (at most 5 pages of
   100, 3 s timeout each) shows at least `min-idle` runners that are online,
   not busy, and carry every label in `labels`. Label matching ignores case.
   With `mode: prefer`, one matching online runner is enough even if it is
   busy; the job then queues for it.

Any failure picks the fallback and adds a `::warning::`: an API error, a
timeout, malformed data, an invalid input, or a selector that could not start
(uv setup failed). A pool outage never blocks CI. The action prints fixed
diagnostics and counts only, never API text, exception messages or the token.
It writes a short step summary of the decision.

Pool runners carry `self-hosted`, `forge-pool` and `<host>`, plus lane labels
(`obs-ubuntu-2604`, `clud-linux-x64`, `ubuntu-2404`, ...). The count covers the
whole organization, so name labels that only runners in a group your
repository can use carry, such as a lane label. Otherwise idle runners in
another group can win the selection and the job then waits for a runner it
cannot get. They run the Ubuntu 26.04 runner image, not `ubuntu-latest`;
onboard one job at a time (see gh-runner `docs/pool.md`).

## Inputs and outputs

| Input | Default | Meaning |
| --- | --- | --- |
| `labels` | `""` | JSON array of pool labels, e.g. `["self-hosted","forge-pool"]`. Empty means hosted. |
| `fallback` | `ubuntu-latest` | Hosted `runs-on`: a label, or a JSON array of labels. |
| `token` | `""` | Token that can list the owner's organization runners. |
| `min-idle` | `1` | Idle matching runners required. |
| `mode` | `""` | `""`/`idle`: require `min-idle` idle runners. `prefer`: any matching online runner. |
| `trusted-only` | `true` | Only `false` turns the event and fork checks off. |

| Output | Meaning |
| --- | --- |
| `runs-on` | JSON value for `fromJSON()`: the labels array, or the fallback. |
| `pool` | `true` when the pool was selected, otherwise `false`. |

## Token

Use the GitHub App `obs-rust-mq-runner-probe`, installed on the organization
with the organization permission **Self-hosted runners: read** and nothing
else. Store its app ID in a variable and its private key in a secret, and mint
a token for the repository owner with `actions/create-github-app-token`. Fork
PRs get no secrets, so the token step fails there. That is harmless, because
fork PRs run hosted anyway.

## Usage

```yaml
jobs:
  pool:
    name: Select runner
    # forge-quick through the kill switch (RUN-004); e.g. FORGE_QUICK_LABELS = ["self-hosted","forge-quick"]
    runs-on: ${{ vars.FORGE_QUICK_LABELS != '' && fromJSON(vars.FORGE_QUICK_LABELS) || 'ubuntu-latest' }}
    timeout-minutes: 2
    permissions: {}
    outputs:
      runs-on: ${{ steps.select.outputs.runs-on || '"ubuntu-latest"' }}
    steps:
      - name: Create pool probe token
        id: token
        if: vars.FORGE_POOL_LABELS != '' && vars.POOL_PROBE_APP_ID != ''
        continue-on-error: true
        uses: actions/create-github-app-token@f418cff399f7aef4c2f0ad47e25ca7a6abbdd6fc # v3.4.0
        with:
          app-id: ${{ vars.POOL_PROBE_APP_ID }}
          private-key: ${{ secrets.POOL_PROBE_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}
      - name: Select pool or hosted
        id: select
        continue-on-error: true
        uses: forge-ci/ci.yml/actions/pool-select@b53af98264b9f2b60cddb62d373dd879088376d9
        with:
          labels: ${{ vars.FORGE_POOL_LABELS }}   # e.g. ["self-hosted","forge-pool"]
          token: ${{ steps.token.outputs.token }}
          fallback: ubuntu-latest

  build:
    needs: pool
    runs-on: ${{ fromJSON(needs.pool.outputs.runs-on) }}
    steps:
      - run: echo "running on ${{ needs.pool.outputs.runs-on }}"
```

`continue-on-error` and the `|| '"ubuntu-latest"'` default keep the job on
hosted runners even if the action itself cannot run. Keep the two fallbacks
the same. Pin the action by commit SHA.

### Running the selector on forge-quick

The action pins `setup-uv` to uv `0.13.7`, the release the forge-quick image seeds in its
tool cache, so the selector downloads nothing and stays inside the 256 MiB budget. Bump it
together with soldrix `image/quick/Dockerfile`.

`ci_lint` rule `RUN-004` holds any job that can land on `forge-quick` to the
class's limits: `timeout-minutes` of 2 or less; every `uses:` in the shared
allowlist at its pinned sha (`ci_lint/data/forge_quick_allowlist.toml`, the
same list the forge-quick image pre-seeds into its action cache, so the job
downloads no action); no `container:` or `services:`; and `runs-on` through
the `vars.FORGE_QUICK_LABELS` kill switch with a hosted fallback, as above.
The example above passes it as written. The allowlist pins pool-select at
`b53af982` (#549, the first sha with the uv pin); a newer pool-select sha must be
added to the allowlist (and the image rebuilt) before a forge-quick job can use it. The forge-quick image
also limits each job at run time (wall clock and memory; forge-ci/soldrix
`image/quick/`). Apart from that, it uses only
`astral-sh/setup-uv` (pinned) and a standard-library Python script run with
`uv run`.

Tests: `ci_lint/tests/test_pool_select.py`, part of `python3 -m ci_lint selftest`.
