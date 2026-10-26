# Fleet inventory

Recorded output of `ci-lint fleet scan` across every owner (#99). These are live, read-only (GET-only) scans of the GitHub API. They are evidence, not policy: rule meanings and enforcement status live in `docs/policy-general.md` and `docs/policy-rust.md`, and the issue workflow lives in `docs/agent-guide.md`. No issues were filed from this scan.

## 2026-10-25: first full-owner scan

- File: [`fleet-scan-2026-10-25.json`](fleet-scan-2026-10-25.json), full `--json` output, about 400 KB, not summarized. `scanned_at` is `2026-10-26T00:18:53Z`.
- Command: `python3 -m ci_lint fleet scan --owners zackees,FastLED,TechWatchProject --json --record rec.json`. That run made about 1,823 distinct GET requests and took 12 minutes. The JSON committed here is `--replay rec.json` of the same recording under `uv run --no-project --with pyyaml`, because the live run had no YAML parser and could not parse any workflow. The 48 MB raw recording is not committed.
- Scope: **157 repositories** (zackees 139, FastLED 15, TechWatchProject 3). These are all non-archived, non-fork repos. The scan reported zero errors.
- Every scanned repo has at least one violation. 153 of the 157 have no `ci.toml` schema 3.

### Repositories with each violated rule

A repo counts once per rule. The raw violation count, where it differs, is in parentheses.

| Rule | Repos | Meaning |
| --- | ---: | --- |
| FLEET-001 | 153 | no `ci.toml` schema 3 |
| GEN-001 | 146 | PR entrypoint problems |
| SEC-007 | 52 | workflow token permission default |
| RUN-001 | 37 (297) | literal non-fleet `runs-on` label |
| FLEET-003 | 24 | default-branch push runs are 20% or more of CI wall time, with no `reuse-check` decision and no live merge queue |
| GEN-008 | 24 | PR entrypoint problems |
| FLEET-002 | 5 | Actions cache at 80% or more of the 10 GiB limit: FastLED/dashboard 17.93 GiB, FastLED/fbuild 9.93, FastLED/FastLED 9.70, zackees/mimalloc-pprof 9.68, zackees/running-process 9.19 |
| GEN-006 | 1 | FastLED/fbuild: `strict` required checks with no merge-queue ruleset |

There are 5 `needs_review` findings: 4 `RUST-014` and 1 `RUN-001`.

### Top 15 by rank

The score is 10 x the number of distinct violated rule IDs, plus every violation and `needs_review` finding.

| # | Repo | Score | Violations | Review | Violated rules |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | zackees/clang-tool-chain | 119 | 68 | 1 | FLEET-001, FLEET-003, GEN-001, GEN-008, RUN-001 |
| 2 | FastLED/fbuild | 112 | 39 | 3 | FLEET-001, FLEET-002, GEN-001, GEN-006, GEN-008, RUN-001, SEC-007 |
| 3 | zackees/mimalloc-pprof | 96 | 55 | 1 | FLEET-002, GEN-001, GEN-008, RUN-001 |
| 4 | FastLED/FastLED | 73 | 23 | 0 | FLEET-002, GEN-001, GEN-008, RUN-001, SEC-007 |
| 5 | zackees/zcmds | 73 | 13 | 0 | FLEET-001, FLEET-003, GEN-001, GEN-008, RUN-001, SEC-007 |
| 6 | FastLED/cli | 67 | 7 | 0 | FLEET-001, FLEET-003, GEN-001, GEN-008, RUN-001, SEC-007 |
| 7 | zackees/static_ffmpeg | 64 | 14 | 0 | FLEET-001, FLEET-003, GEN-001, RUN-001, SEC-007 |
| 8 | zackees/transcribe-anything | 60 | 10 | 0 | FLEET-001, GEN-001, GEN-008, RUN-001, SEC-007 |
| 9 | zackees/zcmds_win32 | 58 | 8 | 0 | FLEET-001, FLEET-003, GEN-001, RUN-001, SEC-007 |
| 10 | FastLED/framework-arduino-lpc8xx | 57 | 7 | 0 | FLEET-001, FLEET-003, GEN-001, GEN-008, RUN-001 |
| 11 | zackees/codeup | 57 | 7 | 0 | FLEET-001, FLEET-003, GEN-001, GEN-008, RUN-001 |
| 12 | FastLED/fastled-fastled.github.io | 55 | 5 | 0 | FLEET-001, FLEET-003, GEN-001, RUN-001, SEC-007 |
| 13 | zackees/vids-db | 55 | 5 | 0 | FLEET-001, GEN-001, GEN-008, RUN-001, SEC-007 |
| 14 | zackees/ledmapper | 53 | 13 | 0 | FLEET-001, GEN-008, RUN-001, SEC-007 |
| 15 | zackees/running-process | 50 | 20 | 0 | FLEET-002, GEN-008, RUN-001 |

## Running the next scan and diffing it

1. Check the budget with `gh api rate_limit`. `gh` can return a cached answer, so read the `x-ratelimit-remaining` header from a direct `curl` instead. A full scan costs about 1,800 to 2,000 core requests. If the budget is short, scan in chunks with `--repos a,b,...` and merge the results.
2. Run the scan with PyYAML available and a token exported:
   `GH_TOKEN=$(gh auth token) uv run --no-project --with pyyaml python3 -m ci_lint fleet scan --owners zackees,FastLED,TechWatchProject --json > docs/fleet/fleet-scan-YYYY-MM-DD.json`
   A non-zero exit means violations were found. It does not mean the scan failed.
3. Diff the scans:
   `python3 -m ci_lint history docs/fleet/fleet-scan-2026-10-25.json docs/fleet/fleet-scan-YYYY-MM-DD.json`
   Add `--json` for machine-readable output. The diff lists new, persisting, resolved and unscanned findings for each repo and rule.
4. Preview the issue plan with `python3 -m ci_lint sync-issues --dry-run --from-scan <file>`. There is no write path yet.
