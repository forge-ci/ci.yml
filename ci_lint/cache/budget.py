"""`ci-lint cache budget`: the `cache-budget` job's verdict (zackees/ci.yml#23
section 6). Evaluates live cache state independently of the janitor.

- `pull_request` (and a push to any branch other than the default branch):
  WARN-ONLY (exit 0) for state the PR did not create. It FAILS only for the
  PR's own entries -- those carrying its delimited `pr-<N>` key component or
  saved on `refs/pull/<N>/merge` -- when they alone exceed `[cache.pr].budget`
  or when the account is over `[cache].budget` and would not be without them
  (a breach caused by the PR's own additions).
- `push` to the default branch, `schedule`, `workflow_dispatch`: a HARD
  failure when live usage is over `[cache].budget`. A transient race with an
  in-flight janitor sweep resolves on the next run.

A failed cache listing is a warning (exit 0): the verdict never fails a run
on state it could not read.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ci_lint.cache.audit import _check_cache_003, _check_cache_006, classify
from ci_lint.cargo_messages import JsonValue
from ci_lint.cache.github_cache import CacheApiError, list_caches
from ci_lint.github_api import FetchFn
from ci_lint.rules.cache_static import parse_size, pr_budget_bytes
from ci_lint.schema import CiToml

HARD_EVENTS: frozenset[str] = frozenset({"schedule", "workflow_dispatch"})


@dataclass(frozen=True)
class BudgetVerdict:
    verdict: str  # "ok" | "warn" | "fail"
    total_bytes: int
    budget_bytes: int | None
    own_bytes: int
    pr_budget_bytes: int | None
    reason: str
    # GEN-009 (issue #5): a same-classification-pass rollup over the
    # CACHE-003 (off-branch base-layer saves) and CACHE-006 (superseded
    # generations) counts this verdict can see WITHOUT any extra live call
    # (no GraphQL PR-state lookup, so CACHE-008 is not counted here --
    # `ci-lint cache audit`'s GEN-009 is the authoritative, full rollup
    # including CACHE-004/CACHE-008; this is the cheap same-call summary
    # `cache budget` can afford). None when neither contributor fired.
    gen_009_reason: str | None = None

    @property
    def failed(self) -> bool:
        return self.verdict == "fail"

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "verdict": self.verdict,
            "total_bytes": self.total_bytes,
            "budget_bytes": self.budget_bytes,
            "own_bytes": self.own_bytes,
            "pr_budget_bytes": self.pr_budget_bytes,
            "reason": self.reason,
            "gen_009_reason": self.gen_009_reason,
        }

    def render(self) -> str:
        budget = f"{self.budget_bytes}B" if self.budget_bytes is not None else "?"
        line = f"ci-lint cache budget: {self.verdict.upper()} -- live {self.total_bytes}B / budget {budget}"
        if self.own_bytes:
            line += f"; this PR's own entries {self.own_bytes}B"
        line += f"\n  {self.reason}"
        if self.gen_009_reason:
            line += f"\n  GEN-009: {self.gen_009_reason}"
        return line


def is_hard_context(event_name: str, ref: str, default_branch: str) -> bool:
    if event_name in HARD_EVENTS:
        return True
    return event_name == "push" and ref == f"refs/heads/{default_branch}"


def evaluate(
    ci: CiToml,
    entries_total: int,
    *,
    own_bytes: int,
    hard: bool,
    pr_number: int | None,
) -> BudgetVerdict:
    budget = parse_size(ci.cache.budget) if ci.cache.budget else None
    pr_budget = pr_budget_bytes(ci)
    over = budget is not None and entries_total > budget
    fix = "run 'ci-lint cache janitor' (or wait for the next push sweep); if a family's steady state cannot fit, shrink its declared max/per (CACHE-004)"
    if hard:
        if over:
            return BudgetVerdict("fail", entries_total, budget, own_bytes, pr_budget, f"over [cache].budget on a hard-verdict ref: {fix}")
        return BudgetVerdict("ok", entries_total, budget, own_bytes, pr_budget, "within [cache].budget")
    if pr_number is not None:
        if pr_budget is not None and own_bytes > pr_budget:
            return BudgetVerdict(
                "fail", entries_total, budget, own_bytes, pr_budget,
                f"PR #{pr_number}'s own pr-{pr_number} entries exceed [cache.pr].budget: save fewer/smaller "
                "PR entries (only the declared [cache.pr].families, within max-per-pr)",
            )
        if over and budget is not None and entries_total - own_bytes <= budget:
            return BudgetVerdict(
                "fail", entries_total, budget, own_bytes, pr_budget,
                f"PR #{pr_number}'s own pr-{pr_number} entries push the account over [cache].budget: "
                "save fewer/smaller PR entries",
            )
    if over:
        return BudgetVerdict(
            "warn", entries_total, budget, own_bytes, pr_budget,
            f"over [cache].budget, but not because of this run's own entries (warn-only here): {fix}",
        )
    return BudgetVerdict("ok", entries_total, budget, own_bytes, pr_budget, "within [cache].budget")


def run_budget(
    ci: CiToml,
    *,
    fetch: FetchFn,
    token: str,
    repo: str,
    event_name: str,
    ref: str,
    pr_number: int | None,
    default_branch: str = "main",
) -> BudgetVerdict:
    try:
        entries = list_caches(fetch, token, repo)
    except CacheApiError as exc:
        return BudgetVerdict("warn", 0, None, 0, None, f"could not list live caches ({exc}); not failing on unread state")
    classified = classify(ci, entries)
    total = sum(c.entry.size_in_bytes for c in classified)
    own = sum(c.entry.size_in_bytes for c in classified if pr_number is not None and c.pr == pr_number)
    verdict = evaluate(
        ci, total, own_bytes=own, hard=is_hard_context(event_name, ref, default_branch), pr_number=pr_number
    )
    if verdict.verdict == "fail" and own:
        # Name the entries: "save fewer PR entries" alone left the author
        # hunting for which step saved them (FastLED #4921's ungated setup-uv).
        mine = sorted(
            (c for c in classified if c.pr == pr_number), key=lambda c: c.entry.size_in_bytes, reverse=True
        )
        listed = "; ".join(f"{c.entry.key} ({c.entry.size_in_bytes}B)" for c in mine[:5])
        verdict = replace(verdict, reason=f"{verdict.reason}. This PR's entries: {listed}")
    contributing = list(_check_cache_003(classified, default_branch)) + list(_check_cache_006(ci, classified))
    if contributing:
        counts: dict[str, int] = {}
        for f in contributing:
            counts[f.rule] = counts.get(f.rule, 0) + 1
        by_rule = ", ".join(f"{rule}={counts[rule]}" for rule in sorted(counts))
        verdict = replace(
            verdict,
            gen_009_reason=f"{len(contributing)} contributing finding(s) ({by_rule}) -- run 'ci-lint cache "
            "audit' for the full rollup including CACHE-004/CACHE-008",
        )
    return verdict
