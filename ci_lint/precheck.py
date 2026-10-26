"""The static precheck: run every group 1-10 rule, cheapest first, and
report every finding (round-1A brief, section B).

Exit code: 1 if any finding's status is `violation` after exceptions are
applied; 0 otherwise. A `needs_review` finding does not by itself fail the
run (agent-guide.md: it is a distinct status from both pass and violation),
but it is always printed prominently so it cannot be silently treated as a
pass.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ci_lint.exceptions import apply_exceptions
from ci_lint.finding import Finding, Status
from ci_lint.rules.bin_artifacts import check_group_bin
from ci_lint.rules.build_parallelism import check_gen_012
from ci_lint.rules.cache_payload import check_cache_007_static
from ci_lint.rules.cache_restore_copy import check_gen_013
from ci_lint.rules.cache_static import check_group9
from ci_lint.rules.complexity import check_complexity
from ci_lint.rules.default_branch_skip import check_gen_021
from ci_lint.rules.merge_queue import check_gen_022
from ci_lint.gate_bare_tools import check_no_bare_rust
from ci_lint.local_gate import GATE_FILE, check_gate_static
from ci_lint.remote_only import check_gate_012
from ci_lint.rules.ci_pre import check_group12
from ci_lint.rules.contract import check_ct_006, check_tag_001, check_tag_002
from ci_lint.rules.layout import check_group6
from ci_lint.rules.m2_22 import check_group_m2_22
from ci_lint.rules.cook_cache import check_group_cook_cache
from ci_lint.rules.packaging import NATIVE_WHEEL_PROFILES, check_group8
from ci_lint.rules.wheel_soldr_only import check_pkg_010
from ci_lint.rules.profile_required import check_group13
from ci_lint.rules.py_benchmark import check_group14
from ci_lint.rules.doc_claims import check_gen_010_static
from ci_lint.rules.ghapi_reread import check_ghapi_001
from ci_lint.rules.host_sweeps import check_gen_030
from ci_lint.rules.release_gate import check_group11
from ci_lint.rules.rust_dylint_target import check_rust_016
from ci_lint.rules.cpp_ctest import check_cpp_001
from ci_lint.rules.rust_async_guards import check_rust_019
from ci_lint.rules.cache_stats_summary import check_gen_023
from ci_lint.rules.rust_nocapture import check_rust_017
from ci_lint.rules.rust_test_selectors import check_rust_015
from ci_lint.rules.rust_units import check_group7
from ci_lint.rules.secrets_rules import check_group5
from ci_lint.rules.cache_paging import check_cache_033
from ci_lint.rules.cache_target_settings import check_cache_039
from ci_lint.rules.swatinem_ban import check_cache_025
from ci_lint.rules.shell import check_group3
from ci_lint.rules.tools import check_group4
from ci_lint.rules.workflows import check_group2
from ci_lint.schema import CiToml, load_ci_toml

# Checks issue #6 §9 documents as needing live GitHub Actions API/cache
# state ("cache (live)": CACHE-005/006/008, plus the local `act`-store
# variant ACT-001). Round-4A implemented CACHE-001/003/005/006/008/009 as
# `ci_lint.cache.audit` (`ci-lint cache audit`, and `precheck --live` --
# see `_cmd_precheck` in ci_lint/cli.py, which is the layer that actually
# calls the GitHub API and folds the live findings in, keeping this module
# itself network-free). ACT-001 (the local act cache-store audit) is still
# a later round's scope. `--local` (or env ACT=true) without `--live` must
# not leave CACHE-005/006/008's absence unexplained -- it reports each one
# explicitly as skipped, with `needs_review` (never `passed`), so a local
# run is never mistaken for having covered live cache state.
LOCAL_SKIPPED_CHECKS: tuple[tuple[str, str], ...] = (
    ("CACHE-005", "live cache poisoned/superseded-entry audit (GitHub Actions cache API)"),
    ("CACHE-006", "live cache usage-near-budget audit (GitHub Actions cache API)"),
    (
        "CACHE-008",
        "PR cache save/trim policy audit against live cache entries "
        "(GitHub Actions cache API + GraphQL PR state)",
    ),
    (
        "ACT-001",
        "local act cache-store audit against the remote cache policy -- implemented as a "
        "distinct command, 'ci-lint act audit --store-dir <dir>' (round M2-20), not folded into "
        "precheck because it needs a real local act cache-store path",
    ),
)

# The subset of LOCAL_SKIPPED_CHECKS that `--live` actually covers (round
# 4A). ACT-001 is deliberately excluded: it audits the *local* act cache
# store, a different mechanism `--live` (the remote GitHub Actions cache
# API) does not touch.
LIVE_COVERED_RULES: frozenset[str] = frozenset({"CACHE-005", "CACHE-006", "CACHE-008"})


def _local_skip_findings(*, live: bool = False) -> list[Finding]:
    checks = LOCAL_SKIPPED_CHECKS
    if live:
        checks = tuple((rule, desc) for rule, desc in checks if rule not in LIVE_COVERED_RULES)
    return [
        Finding(
            rule=rule,
            status=Status.NEEDS_REVIEW,
            message=f"skipped (local): {desc}",
            fix="run 'ci-lint precheck' without --local (in CI, where the GitHub API is reachable, "
            "or under bosn -> act's remote-equivalent lane) to cover this check; it is never treated "
            "as passing in a --local/ACT=true run",
        )
        for rule, desc in checks
    ]


GROUP_LABELS: dict[int, str] = {
    1: "contract (CT-001..006, TAG-001, TAG-002)",
    2: "workflows (GEN-001/002/008/012, TAG-003, SEC-003/004, RUN-001/002, WF-001..003, CT-004)",
    3: "shell budget (GEN-005)",
    4: "tools (TOOL-001/002/003, CACHE-009, RUST-002, GEN-004)",
    5: "secrets (SEC-001/002)",
    6: "layout (LAYOUT-001)",
    7: "tests & units (RUST-005/011/012/013)",
    8: "packaging (PKG-001/002/003/004/005)",
    9: "cache static (CACHE-001/002/003/004/007)",
    10: "lint pin (CT-004, checked inside group 2)",
    11: "release gate (REL-001/002/005)",
    12: "ci-pre.yml shape + PR cache keys (GEN-014..018, CACHE-013)",
    15: "paths blast radius + platform check-only coverage + cook safety (GEN-007, RUST-008/010)",
    16: "default-branch skips only through verified reuse (GEN-021)",
    17: "local gate first: remote quick gate mirrors the local gate, PR entry verifies it (GATE-001/002)",
    18: "remote-only checks never gate a PR: CodeRabbit suppressed, act-impossible steps confined, no app waits (GATE-012)",
    19: "function-complexity ratchet: clippy and ruff ceilings, per-function expect/noqa only (RUST-018, PY-004)",
    20: "GitHub API budget: no unbounded GitHub re-read loops in run:, ci/ or skill/tool scripts (GHAPI-001)",
    21: "async guard lints: workspace deny + member inheritance + clippy.toml well-formed (RUST-019)",
    22: "cook cache interim: env after cook, hand-rolled cook caches, PR-only cooks (RUST-024/025, CACHE-018)",
}


@dataclass(frozen=True)
class PrecheckResult:
    findings: tuple[Finding, ...]
    elapsed_seconds: float
    ci: CiToml | None
    cache_arithmetic: str | None
    local: bool = False


def is_local_run(local_flag: bool) -> bool:
    """`--local` OR env `ACT=true` (bosn -> act's local runner sets this,
    matching `actions/checkout`'s own convention) -- round-2A brief, part 2e."""

    return local_flag or os.environ.get("ACT") == "true"


def run_precheck(repo_root: Path, *, title: str = "", local: bool = False, live: bool = False) -> PrecheckResult:
    start = time.monotonic()
    ci, findings = load_ci_toml(repo_root)
    all_findings: list[Finding] = list(findings)
    cache_arithmetic: str | None = None
    local_run = is_local_run(local)

    if ci is not None:
        all_findings.extend(check_tag_001(ci, title))
        all_findings.extend(check_tag_002(title))
        all_findings.extend(check_ct_006(ci, repo_root))

        all_findings.extend(check_group2(ci, repo_root))
        all_findings.extend(check_gen_012(repo_root))
        all_findings.extend(check_gen_013(repo_root))
        all_findings.extend(check_group3(ci, repo_root))
        all_findings.extend(check_group4(ci, repo_root))
        all_findings.extend(check_group5(ci, repo_root))
        all_findings.extend(check_group6(ci, repo_root))
        all_findings.extend(check_group7(ci, repo_root))
        all_findings.extend(check_group8(ci, repo_root))
        if ci.profile in NATIVE_WHEEL_PROFILES:
            all_findings.extend(check_pkg_010(repo_root))
        findings9, cache_arithmetic = check_group9(ci, repo_root)
        all_findings.extend(findings9)
        all_findings.extend(check_cache_007_static(ci, repo_root))
        all_findings.extend(check_cache_025(repo_root))
        all_findings.extend(check_cache_033(repo_root))
        all_findings.extend(check_group_bin(ci, repo_root))
        all_findings.extend(check_group11(ci, repo_root))
        all_findings.extend(check_group12(ci, repo_root))
        all_findings.extend(check_group13(ci, repo_root))
        all_findings.extend(check_group14(ci, repo_root))
        gen_010_findings, _gen_010_claims = check_gen_010_static(repo_root)
        all_findings.extend(gen_010_findings)
        all_findings.extend(check_group_m2_22(ci, repo_root))
        all_findings.extend(check_group_cook_cache(ci, repo_root))
        all_findings.extend(check_rust_017(repo_root))
        all_findings.extend(check_cpp_001(repo_root))
        all_findings.extend(check_rust_015(repo_root))
        all_findings.extend(check_rust_016(repo_root))
        all_findings.extend(check_cache_039(repo_root))
        all_findings.extend(check_gen_021(repo_root))
        all_findings.extend(check_gen_022(ci, repo_root))
        all_findings.extend(check_gate_012(repo_root))
        all_findings.extend(check_complexity(repo_root).findings)
        all_findings.extend(check_rust_019(repo_root))
        all_findings.extend(check_ghapi_001(repo_root))
        all_findings.extend(check_gen_023(repo_root))
        all_findings.extend(check_gen_030(repo_root))
        if ci.local.gate is not None:
            all_findings.extend(check_gate_static(ci.local.gate, repo_root))
            all_findings.extend(check_no_bare_rust(repo_root, ci.local.gate.run, workflows=False))
            if (repo_root / GATE_FILE).is_file():
                all_findings.append(
                    Finding(
                        rule="GATE-001",
                        path=GATE_FILE,
                        message="the local gate is declared in both ci.toml [local.gate] and local-gate.toml",
                        fix="delete local-gate.toml; ci.toml's [local.gate] is the declaration",
                    )
                )

        outcome = apply_exceptions(ci, all_findings)
        all_findings = outcome.findings

        if local_run:
            # Never run through apply_exceptions: these are an explicit,
            # administrative "not covered locally" notice, not a violation
            # an exception entry could legitimately waive.
            all_findings.extend(_local_skip_findings(live=live))

    elapsed = time.monotonic() - start
    return PrecheckResult(
        findings=tuple(all_findings),
        elapsed_seconds=elapsed,
        ci=ci,
        cache_arithmetic=cache_arithmetic,
        local=local_run,
    )


def render_text(result: PrecheckResult) -> str:
    lines: list[str] = []
    if result.local:
        lines.append("ci-lint precheck: --local/ACT=true -- checks needing the GitHub API are skipped (see below).")
        lines.append("")
    by_rule: dict[str, list[Finding]] = {}
    for f in result.findings:
        by_rule.setdefault(f.rule, []).append(f)

    if not result.findings:
        lines.append("ci-lint precheck: no findings.")
    for rule in sorted(by_rule):
        lines.append(f"== {rule} ({len(by_rule[rule])}) ==")
        for f in by_rule[rule]:
            lines.append(f.render())
        lines.append("")

    if result.cache_arithmetic:
        lines.append(result.cache_arithmetic)
        lines.append("")

    n_violation = sum(1 for f in result.findings if f.status == Status.VIOLATION)
    n_exception = sum(1 for f in result.findings if f.status == Status.APPROVED_EXCEPTION)
    n_review = sum(1 for f in result.findings if f.status == Status.NEEDS_REVIEW)
    lines.append(
        f"ci-lint precheck: {n_violation} violation(s), {n_exception} approved exception(s), "
        f"{n_review} needs_review, in {result.elapsed_seconds:.2f}s"
    )
    return "\n".join(lines)


def render_markdown_summary(result: PrecheckResult) -> str:
    lines: list[str] = ["# ci-lint precheck", ""]
    by_rule: dict[str, list[Finding]] = {}
    for f in result.findings:
        by_rule.setdefault(f.rule, []).append(f)
    if not result.findings:
        lines.append("No findings.")
    for rule in sorted(by_rule):
        lines.append(f"## {rule}")
        lines.append("")
        lines.append("| status | location | what | fix |")
        lines.append("| --- | --- | --- | --- |")
        for f in by_rule[rule]:
            lines.append(
                f"| {f.status.value} | `{f.location()}` | {f.message} | {f.fix} |"
            )
        lines.append("")
    if result.cache_arithmetic:
        lines.append("```")
        lines.append(result.cache_arithmetic)
        lines.append("```")
        lines.append("")
    n_violation = sum(1 for f in result.findings if f.status == Status.VIOLATION)
    lines.append(f"**{n_violation} violation(s)** in {result.elapsed_seconds:.2f}s")
    return "\n".join(lines)


def render_json(result: PrecheckResult) -> str:
    payload = [
        {
            "rule": f.rule,
            "status": f.status.value,
            "path": f.path,
            "line": f.line,
            "message": f.message,
            "fix": f.fix,
        }
        for f in result.findings
    ]
    return json.dumps(
        {
            "findings": payload,
            "elapsed_seconds": result.elapsed_seconds,
            "violations": sum(1 for f in result.findings if f.status == Status.VIOLATION),
        },
        indent=2,
    )


def has_violations(result: PrecheckResult) -> bool:
    return any(f.status == Status.VIOLATION for f in result.findings)


def write_step_summary(result: PrecheckResult) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(render_markdown_summary(result))
            fh.write("\n")
    except OSError:
        pass
