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
3. The capacity source shows at least `min-idle` runners that are online,
   not busy, and carry every label in `labels`. Label matching ignores case.
   With `mode: prefer`, one matching online runner is enough even if it is
   busy; the job then queues for it. The source is chosen by `source`:
   - `signal`, the default when `token` is empty: `GET
     https://signal.forge-ci.com/v1/pool?org={github.repository_owner}` (3 s
     timeout), with no token and no Authorization header. The droplet answers
     from the forge-quick-runners App, so no repository or organization holds
     a credential. It lists the org's online runners as label sets with online
     and busy counts, cached for 10 s (schema `forge-ci.pool-status` version 1,
     forge-ci/signal.forge-ci.com `docs/droplet.md`). The action sums every
     label set that carries all of `labels`. It runs hosted when the response
     isn't HTTP 200, has another schema or version, has no `ok` entry for the
     org, or has data older than 30 s (or the server's `max_age_s`). It ignores
     fields it doesn't know.

     With `count-room: true` (the default), the signal source also counts
     the Dell dispatchers' room to start a runner (forge-ci/gh-runner#67).
     For every entry in the org's `dispatcher.hosts` whose lane `labels`
     carry all of `labels` and whose report is at most 180 s old, it adds
     that lane's `spare`: room the dispatcher has (lane max, host cap, no
     refill hold) that queued jobs have not already claimed. A lane at floor
     0 has no idle runner, but with spare room it is still selectable. The
     job queues, signal's demand poller sees it within about 20 s, and the
     host's dispatcher starts a runner for it on its next pass. A missing,
     null or malformed `dispatcher`, a stale report, or a host taken out of
     CI (cap 0, which signal leaves out) adds no room. That is never an
     error, and idle runners still count. `count-room: false` counts idle
     runners only.
   - `github`, the default when `token` is set (the original behaviour):
     `GET /orgs/{github.repository_owner}/actions/runners` with the token (at
     most 5 pages of 100, 3 s timeout each). With `source: github` and no
     token, the action warns and runs hosted.

   Both sources read state without reserving it, so two selectors can race
   for the last idle runner. The forge pool ledger's OIDC lease
   (signal.forge-ci.com, dynamic-pool M3) is the long-term default. Its probe
   fallback is this read.

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
| `token` | `""` | Token that can list the owner's organization runners. Not needed with the signal source. |
| `source` | `""` | `""`/`auto`: `github` with a token, else `signal`. `signal`: tokenless pool status. `github`: the org runners API with `token`. |
| `status-url` | `https://signal.forge-ci.com/v1/pool` | Pool status for `source: signal`; https only; `?org=<owner>` is appended. |
| `min-idle` | `1` | Matching capacity required: idle runners, plus the dispatchers' spare room (signal source, `count-room`). |
| `count-room` | `true` | Count the Dell dispatchers' spare room (`/v1/pool` `dispatcher`) as capacity, so a floor-0 lane with headroom is selectable. |
| `mode` | `""` | `""`/`idle`: require `min-idle` idle runners. `prefer`: any matching online runner. |
| `trusted-only` | `true` | Only `false` turns the event and fork checks off. |

| Output | Meaning |
| --- | --- |
| `runs-on` | JSON value for `fromJSON()`: the labels array, or the fallback. |
| `pool` | `true` when the pool was selected, otherwise `false`. |

## Token (optional)

No token is needed: with none, the action reads the signal. A repository
that wants to probe GitHub itself can still pass a token and get the
original behaviour. Use the GitHub App `obs-rust-mq-runner-probe`, installed on the organization
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
      - name: Select pool or hosted
        id: select
        continue-on-error: true
        # the sha in ci_lint/data/forge_quick_allowlist.toml (below)
        uses: forge-ci/ci.yml/actions/pool-select@<40-hex sha>
        with:
          labels: ${{ vars.FORGE_POOL_LABELS }}   # e.g. ["self-hosted","forge-pool"]
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
The example above passes it with the allowlisted sha, today
`a36bc81ad6ada5fb17398e3e7ba38435cc67a951` (#550, the first with the signal
source; `b53af982` is still allowed). A newer pool-select
sha must be added to the allowlist (and the image rebuilt) before a
forge-quick job can use it. `c7ceb022` predates the signal source and the uv
pin: it needs a token, and on forge-quick its uv step is killed by memguard.
The forge-quick network can reach the droplet's Caddy on 443 and nothing
else on the host, which is enough for the signal source. The forge-quick image
also limits each job at run time (wall clock and memory; forge-ci/soldrix
`image/quick/`). Apart from that, it uses only
`astral-sh/setup-uv` (pinned) and a standard-library Python script run with
`uv run`.

Tests: `ci_lint/tests/test_pool_select.py`, part of `python3 -m ci_lint selftest`.
