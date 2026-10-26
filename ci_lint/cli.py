"""`python3 -m ci_lint <command> ...` argument parsing and dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ci_lint.act_audit import audit as run_act_audit
from ci_lint.hosted_attestations import verified_jobs, verified_merge_group_jobs
from ci_lint.attestations import JobDecision
from ci_lint.act_audit import render as render_act_audit_text
from ci_lint.act_audit import to_json_dict as act_audit_to_json_dict
from ci_lint.cache.audit import AuditError, run_audit
from ci_lint.cache.audit import render_text as render_cache_audit_text
from ci_lint.cache.audit import to_json_dict as cache_audit_to_json_dict
from ci_lint.cache.budget import run_budget as cache_run_budget
from ci_lint.rules.cache_payload import (
    PayloadManifestError,
    check_cache_007_manifest,
    load_manifest_paths,
)
from ci_lint.rules.cache_static import parse_size as cache_parse_size
from ci_lint.cache.delta import (
    DeltaError,
    StaleBaseError,
    apply as apply_delta,
    build_manifest,
    load_manifest,
    manifest_to_json,
    pack as pack_delta,
)
from ci_lint.cache.keys import CacheKeyError, build_delta_key, build_family_key
from ci_lint.cache.ops import heal as cache_heal
from ci_lint.cache.ops import janitor as cache_janitor
from ci_lint.cache.ops import preprune as cache_preprune
from ci_lint.cache.ops import trim as cache_trim
from ci_lint.cache.hit_rate import HitRateError, check_hit_rate
from ci_lint.cache.hit_rate import render_text as render_hit_rate_text
from ci_lint.cache.hit_rate import to_json_dict as hit_rate_to_json_dict
from ci_lint.cache.save_evidence import SaveEvidenceError, run_save_check
from ci_lint.cache.save_evidence import render_text as render_save_check_text
from ci_lint.cache.save_evidence import to_json_dict as save_check_to_json_dict
from ci_lint.cache.rework import ReworkError, render_rework_text, run_rework
from ci_lint.cache.save_ok import SaveOkInputError, SaveOkRequest, evaluate_save_ok
from ci_lint.cargo_messages import JsonValue, load_artifacts_file
from ci_lint.cargo_scan import discover_workspace
from ci_lint.example_drift import ExampleDriftError, compute_example_drift
from ci_lint.example_drift import render_text as render_example_drift_text
from ci_lint.example_drift import to_json_dict as example_drift_to_json_dict
from ci_lint.fbuild_coverage import FbuildCoverageError, check_coverage_preserved, load_coverage_set
from ci_lint.fbuild_coverage import render_text as render_fbuild_coverage_text
from ci_lint.fbuild_coverage import to_json_dict as fbuild_coverage_to_json_dict
from ci_lint.finding import Finding, Status
from ci_lint.fleet_cli import register as register_fleet
from ci_lint.gh_repo_cache import register as register_gh_cache
from ci_lint.reuse_cli import register as register_reuse
from ci_lint.attest_cli import register as register_attest
from ci_lint.local_gate_cli import register as register_local_gate
from ci_lint.py_lint import register as register_py
from ci_lint.remote_only import register as register_remote_only
from ci_lint.rules.complexity import register as register_complexity
from ci_lint.github_api import default_delete, default_fetch, default_fetch_status, default_graphql
from ci_lint.perf import (
    PERF_SCHEMA_VERSION,
    PerfCompareError,
    PerfCompareReport,
    compare as perf_compare,
    is_gating as perf_is_gating,
    load_benchmark_file,
)
from ci_lint.perf import render_text as render_perf_text
from ci_lint.perf import to_json_dict as perf_to_json_dict
from ci_lint.runtime.pr_timing import (
    HistoryScan,
    PrTimingError,
    compute_pr_timing,
    load_history_file,
)
from ci_lint.runtime.pr_timing import render_text as render_pr_timing_text
from ci_lint.runtime.pr_timing import to_json_dict as pr_timing_to_json_dict
from ci_lint.runtime.toolchain_build import (
    ToolchainBuildCheckError,
    compute_toolchain_build_report,
)
from ci_lint.runtime.toolchain_build import render_text as render_toolchain_build_text
from ci_lint.runtime.toolchain_build import to_json_dict as toolchain_build_to_json_dict
from ci_lint.plan import Plan, PlanError, build_act_event, compute_plan
from ci_lint.precheck import (
    has_violations,
    render_json,
    render_text,
    run_precheck,
    write_step_summary,
)
from ci_lint.publish_oidc import (
    OidcCheckError,
    assert_claims,
    decode_payload,
    request_oidc_token,
)
from ci_lint.publish_oidc import render as render_oidc_check
from ci_lint.publish_oidc import to_json_dict as oidc_check_to_json_dict
from ci_lint.release import (
    ReleaseVerifyError,
    verify_readback,
    verify_resume,
    verify_staged_artifacts,
    write_release_manifest,
)
from ci_lint.release import render_text as render_release_verify_text
from ci_lint.release_resume import register as register_release_resume
from ci_lint.release import to_json_dict as release_verify_to_json_dict
from ci_lint.reuse import ReuseResult, compute_reuse, empty_result
from ci_lint.rules.contract import extract_bracket_tokens
from ci_lint.runtime.dylint import DylintCoverageError, compute_dylint_coverage
from ci_lint.runtime.dylint import render_text as render_dylint_coverage_text
from ci_lint.runtime.dylint import to_json_dict as dylint_coverage_to_json_dict
from ci_lint.runtime.gate import QueueReuse, compute_gate
from ci_lint.runtime.gate import render_text as render_gate_text
from ci_lint.runtime.gate import to_json_dict as gate_to_json_dict
from ci_lint.runtime.suite import SuiteCheckError, compute_suite_check
from ci_lint.runtime.suite import render_text as render_suite_check_text
from ci_lint.runtime.suite import to_json_dict as suite_check_to_json_dict
from ci_lint.runtime.tests_size import compute_tests_size
from ci_lint.runtime.tests_size import render_text as render_tests_size_text
from ci_lint.runtime.tests_size import to_json_dict as tests_size_to_json_dict
from ci_lint.runtime.units import compute_units
from ci_lint.runtime.units import render_text as render_units_text
from ci_lint.runtime.units import to_json_dict as units_to_json_dict
from ci_lint.runtime.wheel import check_installed, check_wheel, soldr_requires_floor
from ci_lint.runtime.wheel import installed_check_to_json_dict, wheel_check_to_json_dict
from ci_lint.runtime.wheel import render_installed_check, render_wheel_check
from ci_lint.schema import CiToml, load_ci_toml
from ci_lint.selftest import run_selftest
from ci_lint.settings_audit import DEFAULT_GATE_CHECK_NAME
from ci_lint.settings_audit import render_text as render_settings_audit_text
from ci_lint.settings_audit import run_audit as run_settings_audit
from ci_lint.settings_audit import to_json_dict as settings_audit_to_json_dict


@dataclass(frozen=True)
class GitHubEvent:
    """The GitHub event payload (the JSON boundary, typed `JsonValue`);
    replaces the bare `dict[str, object]` record the helpers below took
    (PY-002, zackees/ci.yml#193)."""

    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class CacheCreds:
    token: str
    repo_slug: str


def _load_event(path: str | None) -> GitHubEvent:
    """Load the GitHub event payload from `--event`, falling back to the
    runner's `GITHUB_EVENT_PATH` (also set by act). Without this fallback a
    workflow step that omitted `--event` silently lost the PR head SHA, and
    title-edit reuse never fired (template-python-rust-cmd#19)."""
    if path is None:
        path = os.environ.get("GITHUB_EVENT_PATH") or None
    if path is None:
        return GitHubEvent({})
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return GitHubEvent({})
    return GitHubEvent(data if isinstance(data, dict) else {})


def _title_from_event(event: GitHubEvent) -> str | None:
    pr = event.payload.get("pull_request")
    if isinstance(pr, dict) and isinstance(pr.get("title"), str):
        return pr["title"]
    return None


def _sha_from_event(event: GitHubEvent) -> str | None:
    inputs = event.payload.get("inputs")
    if isinstance(inputs, dict) and isinstance(inputs.get("sha"), str):
        return inputs["sha"]
    return None


def _pr_number_from_event(event: GitHubEvent) -> int | None:
    pr = event.payload.get("pull_request")
    if isinstance(pr, dict):
        number = pr.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            return number
    return None


def _head_sha_from_event(event: GitHubEvent) -> str | None:
    pr = event.payload.get("pull_request")
    if isinstance(pr, dict):
        head = pr.get("head")
        if isinstance(head, dict) and isinstance(head.get("sha"), str):
            return head["sha"]
    return None


def _compute_reuse_for_plan(plan: Plan, event_name: str, event: GitHubEvent) -> ReuseResult:
    """Round-3A brief, Part 2. Only ever makes a network call for the
    `pull_request` event, and only when GITHUB_TOKEN, GITHUB_REPOSITORY,
    and the PR's head SHA (from the event payload) are all available --
    otherwise (any other event, or missing credentials/context) returns a
    deterministic "reuse nothing" result with no I/O, so this is always
    network-free in unit tests and on push/schedule/workflow_dispatch."""

    lane_digests = dict(plan.lane_digests)
    if event_name != "pull_request":
        return empty_result(lane_digests)
    token = os.environ.get("GITHUB_TOKEN")
    repo_slug = os.environ.get("GITHUB_REPOSITORY")
    head_sha = _head_sha_from_event(event)
    if not token or not repo_slug:
        return empty_result(lane_digests)
    if not head_sha:
        warning = (
            "reuse: no pull_request.head.sha in the event payload; pass --event or run "
            "inside GitHub Actions (GITHUB_EVENT_PATH) -- reusing nothing"
        )
        print(f"ci-lint: {warning}", file=sys.stderr)
        return empty_result(lane_digests, warning=warning)
    result = compute_reuse(
        repo=repo_slug,
        head_sha=head_sha,
        current_run_id=os.environ.get("GITHUB_RUN_ID"),
        lane_digests=lane_digests,
        fetch=default_fetch,
        token=token,
    )
    if result.warning:
        print(f"ci-lint: {result.warning}", file=sys.stderr)
    return result


def _plan_github_output_lines(
    plan: Plan, reuse_result: ReuseResult | None, pr_number: int | None = None
) -> list[str]:
    payload = plan.to_json_dict()
    compact = json.dumps(payload, separators=(",", ":"))
    platforms_json = json.dumps([p.id for p in plan.platforms])
    cross_platforms_json = json.dumps([p.id for p in plan.platforms if p.group != "linux"])
    suites_json = json.dumps(list(plan.suites))
    dylint_targets_json = json.dumps(list(plan.dylint_targets))
    platform_lanes_json = json.dumps([pl.to_json_dict() for pl in plan.platform_lanes], separators=(",", ":"))
    fast_suites_json = json.dumps(list(plan.fast_suites), separators=(",", ":"))
    lane_digests_json = json.dumps(dict(plan.lane_digests), separators=(",", ":"))
    # Round-4B: derived straight from cache_mode so a writer flow's steps
    # (setup-soldr's/astral-sh/setup-uv's 'save-cache' inputs) can pass this
    # straight through as their plan-driven expression -- see
    # ci_lint.rules.tools._is_plan_expr / ci_lint.rules.cache_static
    # .check_cache_003_setup_uv.
    cache_save = "true" if plan.cache_mode == "write" else "false"

    lines = [
        f"plan={compact}",
        f"platforms_json={platforms_json}",
        f"cross_platforms_json={cross_platforms_json}",
        f"suites_json={suites_json}",
        f"dylint_targets_json={dylint_targets_json}",
        f"needs_platform_lanes={'true' if plan.needs_platform_lanes else 'false'}",
        f"mergeable={'true' if plan.mergeable else 'false'}",
        # Round-3A brief, Part 1.
        f"platform_lanes_json={platform_lanes_json}",
        f"fast_suites_json={fast_suites_json}",
        f"lane_digests_json={lane_digests_json}",
        # Round-4B.
        f"cache_save={cache_save}",
        # zackees/ci.yml#23 §5 (CACHE-013): the delimited PR key component
        # every PR-context cache save embeds ('pr-<N>'), empty outside a PR
        # so main/nightly keys carry no PR tag.
        f"cache_key_pr={f'pr-{pr_number}' if pr_number is not None else ''}",
    ]

    if reuse_result is not None:
        reused_platform_ids = {
            key.split(":", 1)[1]
            for key, entry in reuse_result.reuse.items()
            if key.startswith("platform:") and entry is not None
        }
        platform_lanes_todo = [
            pl.to_json_dict() for pl in plan.platform_lanes if pl.id not in reused_platform_ids
        ]
        reuse_json = json.dumps(reuse_result.to_json_dict(), separators=(",", ":"))
        platform_lanes_todo_json = json.dumps(platform_lanes_todo, separators=(",", ":"))
        fast_reused = reuse_result.reuse.get("fast") is not None
        dylint_reused = reuse_result.reuse.get("dylint") is not None
        lines.extend(
            [
                # Round-3A brief, Part 2.
                f"reuse_json={reuse_json}",
                f"platform_lanes_todo_json={platform_lanes_todo_json}",
                f"fast_reused={'true' if fast_reused else 'false'}",
                f"dylint_reused={'true' if dylint_reused else 'false'}",
            ]
        )

    return lines


def _write_plan_github_output(
    plan: Plan, reuse_result: ReuseResult | None, pr_number: int | None = None
) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with open(gh_out, "a", encoding="utf-8") as fh:
        for line in _plan_github_output_lines(plan, reuse_result, pr_number):
            fh.write(line + "\n")


def _live_cache_findings(ci: CiToml | None, *, default_branch: str | None) -> list[Finding]:
    """Round-4A brief, deliverable 4: `precheck --live` runs `ci_lint.cache
    .audit` and folds its findings in as WARNINGS only (never fails a PR
    for cache state it didn't cause) -- except CACHE-009 (a live retired-
    family entry), which fails. This is the one place `ci_lint.precheck`'s
    otherwise network-free rule groups gain a GitHub API dependency, kept
    out of `ci_lint.precheck.run_precheck` itself so that module stays
    testable with no network, matching `_compute_reuse_for_plan` above."""

    if ci is None:
        return []
    token = os.environ.get("GITHUB_TOKEN")
    repo_slug = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo_slug:
        return [
            Finding(
                rule="CACHE-005",
                status=Status.NEEDS_REVIEW,
                message="skipped (--live): GITHUB_TOKEN/GITHUB_REPOSITORY not set",
                fix="run with GITHUB_TOKEN and GITHUB_REPOSITORY set (actions: read) to cover the "
                "live cache audit; it is never treated as passing without them",
            )
        ]
    try:
        report = run_audit(
            ci, fetch=default_fetch, graphql=default_graphql, token=token, repo=repo_slug,
            default_branch=default_branch,
        )
    except AuditError as exc:
        return [
            Finding(
                rule="CACHE-005",
                status=Status.NEEDS_REVIEW,
                message=f"skipped (--live): live cache audit failed: {exc}",
                fix="re-run once the GitHub Actions cache API is reachable; this is never treated as "
                "passing on failure",
            )
        ]
    out: list[Finding] = []
    for f in report.findings:
        # CACHE-009 (a retired family actually present in the live cache
        # account) fails; everything else the live audit can find is a
        # warning -- issue #6 §6: "It never blames a PR for cache state
        # the PR didn't cause."
        out.append(f if f.rule == "CACHE-009" else replace(f, status=Status.NEEDS_REVIEW))
    if report.warning:
        out.append(
            Finding(
                rule="CACHE-008",
                status=Status.NEEDS_REVIEW,
                message=f"--live: {report.warning}",
                fix="re-run once the GitHub API is reachable (PR-state GraphQL lookup failed; the "
                "rest of the live audit still ran)",
            )
        )
    return out


def _cmd_precheck(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    event = _load_event(args.event)
    title = args.title if args.title is not None else (_title_from_event(event) or "")

    result = run_precheck(repo_root, title=title, local=args.local, live=args.live)
    if args.live:
        extra = _live_cache_findings(result.ci, default_branch=args.default_branch)
        result = replace(result, findings=result.findings + tuple(extra))

    if args.json:
        print(render_json(result))
    else:
        print(render_text(result))
    write_step_summary(result)

    if args.plan_out or args.github_output:
        ci = load_ci_toml(repo_root).ci
        if ci is not None:
            event_name = "pull_request" if title else "push"
            try:
                plan = compute_plan(
                    ci,
                    event_name=event_name,
                    title=title,
                    dispatch_sha=_sha_from_event(event),
                    repo_root=repo_root,
                )
            except PlanError as exc:
                print(f"ci-lint plan: {exc}", file=sys.stderr)
            else:
                reuse_result = _compute_reuse_for_plan(plan, event_name, event) if args.reuse else None
                if args.plan_out:
                    payload = plan.to_json_dict()
                    if reuse_result is not None:
                        payload["reuse"] = reuse_result.to_json_dict()
                    Path(args.plan_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                if args.github_output:
                    _write_plan_github_output(plan, reuse_result, _pr_number_from_event(event))

    return 1 if has_violations(result) else 0


def _cmd_plan(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    _loaded = load_ci_toml(repo_root)
    ci = _loaded.ci
    findings = _loaded.findings
    if ci is None:
        for f in findings:
            print(f.render())
        print("ci-lint plan: cannot load ci.toml", file=sys.stderr)
        return 1

    event = _load_event(args.event)
    event_name = args.event_name or os.environ.get("GITHUB_EVENT_NAME")
    if event_name is None:
        print("ci-lint plan: --event-name is required (or set GITHUB_EVENT_NAME)", file=sys.stderr)
        return 1
    title = args.title if args.title is not None else (_title_from_event(event) or "")
    dispatch_sha = args.sha or _sha_from_event(event)

    try:
        plan = compute_plan(
            ci,
            event_name=event_name,
            title=title,
            ref=args.ref,
            dispatch_sha=dispatch_sha,
            repo_root=repo_root,
        )
    except PlanError as exc:
        print(f"ci-lint plan: {exc}", file=sys.stderr)
        return 1

    reuse_result = _compute_reuse_for_plan(plan, event_name, event) if args.reuse else None

    payload = plan.to_json_dict()
    if reuse_result is not None:
        payload["reuse"] = reuse_result.to_json_dict()
    text = json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)

    if args.act:
        Path(args.act).write_text(json.dumps(build_act_event(title), indent=2), encoding="utf-8")

    if args.github_output:
        _write_plan_github_output(plan, reuse_result, _pr_number_from_event(event))

    return 0


def _load_ci_or_die(repo_root: Path, command: str) -> CiToml | None:
    _loaded = load_ci_toml(repo_root)
    ci = _loaded.ci
    findings = _loaded.findings
    if ci is None:
        for f in findings:
            print(f.render())
        print(f"ci-lint {command}: cannot load ci.toml", file=sys.stderr)
    return ci


def _cmd_units(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "units")
    if ci is None:
        return 1
    crates = discover_workspace(repo_root)
    artifacts = load_artifacts_file(Path(args.artifacts))
    report = compute_units(ci, crates, artifacts)
    print(json.dumps(units_to_json_dict(report), indent=2) if args.json else render_units_text(report))
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


def _cmd_tests_size(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "tests size")
    if ci is None:
        return 1
    artifacts = load_artifacts_file(Path(args.artifacts))
    report = compute_tests_size(ci, artifacts)
    print(json.dumps(tests_size_to_json_dict(report), indent=2) if args.json else render_tests_size_text(report))
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


def _cmd_suite_check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "suite check")
    if ci is None:
        return 1
    try:
        report = compute_suite_check(
            ci,
            args.suite,
            [Path(p) for p in (args.cargo_test_log or [])],
            [Path(p) for p in (args.pytest_junit or [])],
        )
    except SuiteCheckError as exc:
        print(f"ci-lint suite check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(suite_check_to_json_dict(report), indent=2) if args.json else render_suite_check_text(report))
    return 1 if report.findings else 0


def _cmd_dylint_coverage(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "dylint coverage")
    if ci is None:
        return 1
    try:
        report = compute_dylint_coverage(ci, [Path(p) for p in (args.log or [])])
    except DylintCoverageError as exc:
        print(f"ci-lint dylint coverage: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(dylint_coverage_to_json_dict(report), indent=2)
        if args.json
        else render_dylint_coverage_text(report)
    )
    return 1 if report.findings else 0


def _cmd_example_drift(args: argparse.Namespace) -> int:
    example_path = Path(args.example)
    repo_ci_toml = Path(args.repo) / "ci.toml"
    try:
        entries = compute_example_drift(example_path, repo_ci_toml)
    except ExampleDriftError as exc:
        print(f"ci-lint example drift: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(example_drift_to_json_dict(entries), indent=2))
    else:
        print(render_example_drift_text(entries, example_path=example_path, repo_ci_toml=repo_ci_toml))
    return 1 if any(not e.repo_specific for e in entries) else 0


def _cmd_fbuild_coverage_check(args: argparse.Namespace) -> int:
    existing_path = Path(args.existing)
    declared_path = Path(args.declared)
    try:
        existing = load_coverage_set(existing_path, label="--existing")
        declared = load_coverage_set(declared_path, label="--declared")
    except FbuildCoverageError as exc:
        print(f"ci-lint fbuild coverage-check: {exc}", file=sys.stderr)
        return 2
    findings = check_coverage_preserved(existing, declared)
    if args.json:
        print(json.dumps(fbuild_coverage_to_json_dict(findings), indent=2))
    else:
        print(render_fbuild_coverage_text(findings, existing_path=existing_path, declared_path=declared_path))
    return 1 if findings else 0


def _cmd_wheel_check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "wheel check")
    if ci is None:
        return 1
    sdist_path = Path(args.sdist) if args.sdist else None
    report = check_wheel(ci, Path(args.wheel), sdist_path, soldr_floor=soldr_requires_floor(repo_root))
    print(json.dumps(wheel_check_to_json_dict(report), indent=2) if args.json else render_wheel_check(report))
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


def _cmd_wheel_installed(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "wheel installed")
    if ci is None:
        return 1
    report = check_installed(ci, repo_root, Path(args.venv))
    print(
        json.dumps(installed_check_to_json_dict(report), indent=2)
        if args.json
        else render_installed_check(report)
    )
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


def _attested_gate_jobs(args: argparse.Namespace, payload: dict[str, JsonValue],
                        needs: dict[str, JsonValue]) -> tuple[JobDecision, ...]:
    if not getattr(args, "attested_workflow", None):
        return ()
    try:
        return verified_jobs(
            Path(args.repo).resolve(), args.attested_workflow, payload,
            event=os.environ.get("GITHUB_EVENT_NAME", ""), needs=needs,
            local_replay=os.environ.get("ACT", "").strip().lower() == "true",
        )
    except (OSError, ValueError) as exc:
        print(f"ci-lint gate: local attestation unavailable: {exc}", file=sys.stderr)
        return ()


def _queue_gate_jobs(args: argparse.Namespace, payload: dict[str, JsonValue],
                     needs: dict[str, JsonValue]) -> QueueReuse | None:
    """#419 clause 5: merge_group skips need the aggregator's own GET-only re-derivation."""
    if not getattr(args, "attested_workflow", None):
        return None
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    try:
        return verified_merge_group_jobs(
            Path(args.repo).resolve(), args.attested_workflow, payload,
            event=os.environ.get("GITHUB_EVENT_NAME", ""), needs=needs,
            fetch=default_fetch_status if token else None, token=token,
            repo_slug=os.environ.get("GITHUB_REPOSITORY"),
            local_replay=os.environ.get("ACT", "").strip().lower() == "true",
        )
    except (OSError, ValueError) as exc:
        return QueueReuse((), unverifiable=f"queue attestation unavailable: {exc}")


def _gate_plan(args: argparse.Namespace, needs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if args.workflow_plan:
        from ci_lint.workflow_gate import build_plan
        return build_plan(Path(args.repo).resolve(), args.workflow_plan, args.gate_job,
                          needs, event=os.environ.get("GITHUB_EVENT_NAME", ""))
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    if not isinstance(plan, dict):
        raise ValueError("--plan must be a JSON object")
    return plan


def _cmd_gate(args: argparse.Namespace) -> int:
    try:
        with open(args.needs, encoding="utf-8") as fh:
            needs = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ci-lint gate: cannot read --needs {args.needs!r}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(needs, dict):
        print("ci-lint gate: --needs must be a JSON object", file=sys.stderr)
        return 1
    try:
        plan = _gate_plan(args, needs)
    except (OSError, ValueError) as exc:
        print(f"ci-lint gate: cannot resolve required-job plan: {exc}", file=sys.stderr)
        return 1
    reuse: dict[str, JsonValue] | None = None
    if args.reuse:
        try:
            with open(args.reuse, encoding="utf-8") as fh:
                reuse_raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ci-lint gate: cannot read --reuse {args.reuse!r}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(reuse_raw, dict):
            print("ci-lint gate: --reuse must be a JSON object", file=sys.stderr)
            return 1
        reuse = reuse_raw

    db_reuse: JsonValue | None = None
    if getattr(args, "default_branch_reuse", None):
        try:
            with open(args.default_branch_reuse, encoding="utf-8") as fh:
                db_reuse = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"ci-lint gate: cannot read --default-branch-reuse {args.default_branch_reuse!r}: {exc}",
                file=sys.stderr,
            )
            return 1

    event = _load_event(args.event)
    head_sha = _head_sha_from_event(event)
    # A default-branch push has no pull_request event, so the document's
    # sha is checked against the pushed commit itself.
    push_sha = os.environ.get("GITHUB_SHA") or head_sha
    token = os.environ.get("GITHUB_TOKEN")
    repo_slug = os.environ.get("GITHUB_REPOSITORY")
    fetch = default_fetch if token and repo_slug else None


    report = compute_gate(
        plan,
        needs,
        reuse=reuse,
        head_sha=head_sha,
        fetch=fetch,
        token=token,
        repo=repo_slug,
        default_branch_reuse=db_reuse,
        push_sha=push_sha,
        attested_jobs=_attested_gate_jobs(args, event.payload, needs),
        queue_reuse=_queue_gate_jobs(args, event.payload, needs),
        workflow_jobs=_native_gate_jobs(args, needs),
    )
    print(json.dumps(gate_to_json_dict(report), indent=2) if args.json else render_gate_text(report))
    if report.not_mergeable_message:
        print(report.not_mergeable_message, file=sys.stderr)
    return 0 if report.ok else 1


def _native_gate_jobs(args: argparse.Namespace, needs: dict[str, JsonValue]):
    from ci_lint.workflow_job_names import WorkflowJobNames
    if not args.workflow_plan:
        return None
    return WorkflowJobNames.from_gate(Path(args.repo).resolve(), args.workflow_plan, args.gate_job, needs)


def _cmd_selftest(_args: argparse.Namespace) -> int:
    return run_selftest()


# ── `ci-lint audit` (round-5: SEC-005/006/007, GEN-006/011) ────────────────


def _cmd_audit(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "audit")
    if ci is None:
        return 1
    creds = _cache_write_creds("audit")  # same env contract: GITHUB_TOKEN + GITHUB_REPOSITORY
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    report = run_settings_audit(
        ci,
        fetch_status=default_fetch_status,
        token=token,
        repo=repo_slug,
        default_branch=args.default_branch,
        gate_check_name=args.gate_check_name,
        repo_root=repo_root,
    )
    print(json.dumps(settings_audit_to_json_dict(report), indent=2) if args.json else render_settings_audit_text(report))
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


# ── `ci-lint publish oidc-check` (round-5) ──────────────────────────────────


def _cmd_publish_oidc_check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "publish oidc-check")
    if ci is None:
        return 1
    if ci.publish.pypi is None or ci.publish.pypi.mode != "mock":
        print(
            "ci-lint publish oidc-check: refusing to run -- ci.toml [publish].pypi.mode must be "
            f"\"mock\" (got {ci.publish.pypi.mode if ci.publish.pypi else None!r})",
            file=sys.stderr,
        )
        return 2

    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not request_url or not request_token:
        print(
            "ci-lint publish oidc-check: ACTIONS_ID_TOKEN_REQUEST_URL/_TOKEN not set -- the job needs "
            "'permissions: id-token: write'",
            file=sys.stderr,
        )
        return 2
    if not repository:
        print("ci-lint publish oidc-check: GITHUB_REPOSITORY not set", file=sys.stderr)
        return 2

    try:
        raw_token = request_oidc_token(
            default_fetch, request_url=request_url, request_token=request_token, audience=args.audience
        )
        payload = decode_payload(raw_token)
    except OidcCheckError as exc:
        print(f"ci-lint publish oidc-check: {exc}", file=sys.stderr)
        return 1
    del raw_token  # never referenced again -- nothing below this line may print it

    result = assert_claims(
        payload,
        repository=repository,
        expect_ref=args.expect_ref,
        expect_event=args.expect_event,
        environment=ci.publish.pypi.environment,
        audience=args.audience,
    )
    print(json.dumps(oidc_check_to_json_dict(result), indent=2) if args.json else render_oidc_check(result))
    return 0 if result.ok else 1


# ── `ci-lint release verify` (round-5) ──────────────────────────────────────


def _cmd_release_verify(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "release verify")
    if ci is None:
        return 1
    ci_toml_path = repo_root / "ci.toml"
    try:
        ci_toml_digest = hashlib.sha256(ci_toml_path.read_bytes()).hexdigest()
    except OSError as exc:
        print(f"ci-lint release verify: cannot read {ci_toml_path}: {exc}", file=sys.stderr)
        return 2
    smoke_dir = Path(args.smoke) if args.smoke else None
    try:
        report = verify_staged_artifacts(
            ci,
            Path(args.dist),
            candidate_sha=args.sha,
            ci_toml_digest=ci_toml_digest,
            smoke_dir=smoke_dir,
            soldr_floor=soldr_requires_floor(repo_root),
        )
    except ReleaseVerifyError as exc:
        print(f"ci-lint release verify: {exc}", file=sys.stderr)
        return 2
    extra_findings: list[Finding] = []
    if args.readback:
        extra_findings.extend(verify_readback(Path(args.dist), Path(args.readback), report.artifacts))
    if args.resume:
        extra_findings.extend(verify_resume(Path(args.dist), Path(args.resume), report.artifacts, candidate_sha=args.sha))
    if extra_findings:
        report = replace(report, findings=report.findings + tuple(extra_findings))
    manifest_path = write_release_manifest(report, Path(args.dist))
    print(json.dumps(release_verify_to_json_dict(report), indent=2) if args.json else render_release_verify_text(report))
    print(f"ci-lint release verify: wrote {manifest_path}", file=sys.stderr)
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


# ── `ci-lint perf compare` (round-5) ────────────────────────────────────────


def _cmd_perf_compare(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "perf compare")
    if ci is None:
        return 1
    try:
        baseline = load_benchmark_file(Path(args.baseline))
        current = load_benchmark_file(Path(args.current))
    except PerfCompareError as exc:
        print(f"ci-lint perf compare: {exc}", file=sys.stderr)
        return 2
    deltas = perf_compare(baseline, current, args.threshold_pct)
    gating = perf_is_gating(ci, args.threshold_pct)
    report = PerfCompareReport(
        schema_version=PERF_SCHEMA_VERSION, deltas=deltas, gating=gating, threshold_pct=args.threshold_pct
    )
    print(json.dumps(perf_to_json_dict(report), indent=2) if args.json else render_perf_text(report))
    if gating and any(d.regression for d in deltas):
        return 1
    return 0


# ── `ci-lint perf pr-timing` (M2-22, PERF-001) ──────────────────────────────


def _cmd_perf_pr_timing(args: argparse.Namespace) -> int:
    try:
        scans = [HistoryScan(str(p), load_history_file(Path(p))) for p in args.history]
    except PrTimingError as exc:
        print(f"ci-lint perf pr-timing: {exc}", file=sys.stderr)
        return 2
    report = compute_pr_timing(
        scans,
        min_samples=args.min_samples,
        window_days=args.window_days,
        job_threshold_seconds=args.job_threshold_minutes * 60.0,
        critical_path_threshold_seconds=args.critical_path_threshold_minutes * 60.0,
    )
    print(json.dumps(pr_timing_to_json_dict(report), indent=2) if args.json else render_pr_timing_text(report))
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


# ── `ci-lint rust toolchain-build-check` (M2-22, RUST-009) ──────────────────


def _cmd_rust_toolchain_build_check(args: argparse.Namespace) -> int:
    try:
        report = compute_toolchain_build_report([Path(p) for p in (args.log or [])])
    except ToolchainBuildCheckError as exc:
        print(f"ci-lint rust toolchain-build-check: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(toolchain_build_to_json_dict(report), indent=2)
        if args.json
        else render_toolchain_build_text(report)
    )
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


# ── `ci-lint cache ...` (round-4A) ──────────────────────────────────────────


def _parse_tags_arg(raw: str | None) -> tuple[str, ...]:
    """`--tags` accepts either a PR-title-shaped string (`"[ci-cache-save]
    [ci-full]"`) -- reusing the exact same bracket extraction TAG-001/002
    and the planner use, so a caller can pass the real PR title verbatim --
    or a bare space/comma-separated list (`"ci-cache-save"`)."""

    if not raw:
        return ()
    bracketed = extract_bracket_tokens(raw)
    if bracketed:
        return tuple(bracketed)
    return tuple(t for t in re.split(r"[\s,]+", raw.strip()) if t)


def _cmd_cache_key(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache key")
    if ci is None:
        return 1
    try:
        if args.pr is not None:
            if not args.base_key:
                print("ci-lint cache key: --pr requires --base-key", file=sys.stderr)
                return 2
            if not args.platform:
                print("ci-lint cache key: --pr requires --platform", file=sys.stderr)
                return 2
            key = build_delta_key(
                family_id=args.family, platform_id=args.platform, pr=args.pr, base_key=args.base_key
            )
        else:
            key = build_family_key(ci, args.family, platform_id=args.platform, repo_root=repo_root).key
    except CacheKeyError as exc:
        print(f"ci-lint cache key: {exc}", file=sys.stderr)
        return 2
    print(key)
    return 0


def _cmd_cache_save_ok(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache save-ok")
    if ci is None:
        return 1
    req = SaveOkRequest(
        family=args.family,
        flow=args.flow,
        event=args.event,
        pr=args.pr,
        fork=args.fork,
        act=args.act,
        build_failed=args.build_failed,
        exact_hit=args.exact_hit,
        new_units=args.new_units,
        payload_bytes=args.payload_bytes,
        base_key=args.base_key,
        current_base_key=args.current_base_key,
        lockfile_changed=args.lockfile_changed,
        tags=_parse_tags_arg(args.tags),
        rerun_saved=args.rerun_saved,
        paths=tuple(args.path or ()),
    )
    try:
        result = evaluate_save_ok(ci, req)
    except SaveOkInputError as exc:
        print(f"ci-lint cache save-ok: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_json_dict(), indent=2) if args.json else result.render())
    return 0


def _cache_write_creds(command: str) -> CacheCreds | None:
    token = os.environ.get("GITHUB_TOKEN")
    repo_slug = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo_slug:
        print(f"ci-lint cache {command}: GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return None
    return CacheCreds(token, repo_slug)


def _cmd_cache_payload_check(args: argparse.Namespace) -> int:
    """CACHE-007 runtime half: classify a save's actual path manifest
    (a JSON array of path strings) against `[cache].never`'s forbidden
    content classes. Pure filesystem -- no ci.toml load needed, since the
    `[cache].never` classes are fixed content patterns, not per-repo
    config."""

    try:
        paths = load_manifest_paths(Path(args.manifest))
    except PayloadManifestError as exc:
        print(f"ci-lint cache payload-check: {exc}", file=sys.stderr)
        return 2
    findings = check_cache_007_manifest(paths, manifest_path=args.manifest)
    if args.json:
        payload = [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "line": f.line,
             "message": f.message, "fix": f.fix}
            for f in findings
        ]
        print(json.dumps(payload, indent=2))
    else:
        if not findings:
            print(f"ci-lint cache payload-check: {args.manifest}: no forbidden content class found")
        for f in findings:
            print(f.render())
    return 1 if findings else 0


def _cmd_act_audit(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "act audit")
    if ci is None:
        return 1
    budget_bytes = cache_parse_size(ci.cache.budget)
    if budget_bytes is None:
        print(f"ci-lint act audit: ci.toml [cache].budget = {ci.cache.budget!r} does not parse", file=sys.stderr)
        return 1
    store_path = Path(args.store_dir).resolve()
    result = run_act_audit(store_path, budget_bytes=budget_bytes, retired_families=ci.cache.retired)
    print(json.dumps(act_audit_to_json_dict(result), indent=2) if args.json else render_act_audit_text(result))
    return 1 if not result.ok else 0


def _cmd_cache_audit(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache audit")
    if ci is None:
        return 1
    creds = _cache_write_creds("audit")  # "write" is a misnomer here -- audit only needs actions: read
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    try:
        report = run_audit(
            ci, fetch=default_fetch, graphql=default_graphql, token=token, repo=repo_slug,
            default_branch=args.default_branch,
        )
    except AuditError as exc:
        print(f"ci-lint cache audit: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(cache_audit_to_json_dict(report), indent=2) if args.json else render_cache_audit_text(report))
    return 1 if any(f.status == Status.VIOLATION for f in report.findings) else 0


def _cmd_cache_rework(args: argparse.Namespace) -> int:
    """CACHE-035 (#148 priority 1) from `--log` evidence -- pure filesystem,
    no token: rebuild-after-exact-hit and cross-step recompiles, both
    `needs_review` evidence for the diagnosis."""

    try:
        report = run_rework(args.log or [])
    except ReworkError as exc:
        print(f"ci-lint cache rework: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_json_dict(), indent=2) if args.json else render_rework_text(report))
    return 1 if report.findings else 0


def _cmd_cache_hit_rate(args: argparse.Namespace) -> int:
    """GEN-023 log-evidence clauses (#411): compile-cache stats present and hit rate judged."""

    try:
        report = check_hit_rate(
            [Path(p) for p in (args.log or [])], threshold=args.threshold, min_compilations=args.min_compilations
        )
    except HitRateError as exc:
        print(f"ci-lint cache hit-rate: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(hit_rate_to_json_dict(report), indent=2) if args.json else render_hit_rate_text(report))
    return 1 if report.findings else 0


def _cmd_cache_save_check(args: argparse.Namespace) -> int:
    """CACHE-011/CACHE-012 (issue #7) from `--log` evidence -- pure
    filesystem, no GITHUB_TOKEN/GITHUB_REPOSITORY required (unlike `cache
    audit`'s live listing): the caller already has the job log(s) in hand
    (`gh run view --job <id> --log`) plus the run's own conclusion."""

    try:
        report = run_save_check(
            [Path(p) for p in (args.log or [])],
            conclusion=args.conclusion,
            run_url=args.run_url,
            threshold=args.unusable_threshold,
            guard_threshold=args.guard_freeze_threshold,
        )
    except SaveEvidenceError as exc:
        print(f"ci-lint cache save-check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(save_check_to_json_dict(report), indent=2) if args.json else render_save_check_text(report))
    return 1 if report.findings else 0


def _cmd_cache_trim(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache trim")
    if ci is None:
        return 1
    creds = _cache_write_creds("trim")
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    delete_fn = None if args.dry_run else default_delete
    result = cache_trim(
        ci, fetch=default_fetch, graphql=default_graphql, delete=delete_fn, token=token, repo=repo_slug,
        max_deletes=args.max_deletes, dry_run=args.dry_run, default_branch=args.default_branch,
    )
    print(json.dumps(result.to_json_dict(), indent=2) if args.json else result.render())
    return 1 if result.errors else 0


def _cmd_cache_janitor(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache janitor")
    if ci is None:
        return 1
    creds = _cache_write_creds("janitor")
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    delete_fn = None if args.dry_run else default_delete
    janitor_report = cache_janitor(
        ci, fetch=default_fetch, graphql=default_graphql, delete=delete_fn, token=token, repo=repo_slug,
        max_deletes=args.max_deletes, dry_run=args.dry_run, stale_days=args.stale_days,
        default_branch=args.default_branch, enforce_budget=args.enforce_budget,
    )
    result, table = janitor_report.result, janitor_report.text
    if args.json:
        payload = result.to_json_dict()
        payload["table"] = table
        print(json.dumps(payload, indent=2))
    else:
        print(result.render())
        print(table)
    return 1 if result.errors else 0


def _cmd_cache_budget(args: argparse.Namespace) -> int:
    """zackees/ci.yml#23 section 6: the `cache-budget` job's verdict."""

    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache budget")
    if ci is None:
        return 1
    creds = _cache_write_creds("budget")  # only needs actions: read
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    event = _load_event(args.event)
    event_name = args.event_name or os.environ.get("GITHUB_EVENT_NAME") or ""
    ref = args.ref or os.environ.get("GITHUB_REF") or ""
    pr_number = args.pr if args.pr is not None else _pr_number_from_event(event)
    verdict = cache_run_budget(
        ci, fetch=default_fetch, token=token, repo=repo_slug, event_name=event_name, ref=ref,
        pr_number=pr_number, default_branch=args.default_branch,
    )
    print(json.dumps(verdict.to_json_dict(), indent=2) if args.json else verdict.render())
    return 1 if verdict.failed else 0


def _cmd_cache_heal(args: argparse.Namespace) -> int:
    if os.environ.get("ACT", "").lower() in ("true", "1"):
        from ci_lint.cache.local_heal import LocalHealError, heal_local

        try:
            local = heal_local(os.environ.get("ACTIONS_CACHE_URL", ""), args.key, ref=args.ref, dry_run=args.dry_run)
        except LocalHealError as exc:
            print(f"ci-lint cache heal: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(local.to_json_dict(), indent=2) if args.json else local.render())
        return 0
    creds = _cache_write_creds("heal")
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    delete_fn = None if args.dry_run else default_delete
    result = cache_heal(delete=delete_fn, token=token, repo=repo_slug, key=args.key, ref=args.ref, dry_run=args.dry_run)
    print(json.dumps(result.to_json_dict(), indent=2) if args.json else result.render())
    return 1 if result.errors else 0


def _cmd_cache_preprune(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    ci = _load_ci_or_die(repo_root, "cache preprune")
    if ci is None:
        return 1
    creds = _cache_write_creds("preprune")
    if creds is None:
        return 1
    token, repo_slug = creds.token, creds.repo_slug
    delete_fn = None if args.dry_run else default_delete
    preprune_report = cache_preprune(
        ci, fetch=default_fetch, graphql=default_graphql, delete=delete_fn, token=token, repo=repo_slug,
        lockfile_changed=args.lockfile_changed, max_deletes=args.max_deletes, dry_run=args.dry_run,
        default_branch=args.default_branch,
    )
    result, summary = preprune_report.result, preprune_report.text
    if args.json:
        payload = result.to_json_dict()
        payload["summary"] = summary
        print(json.dumps(payload, indent=2))
    else:
        print(summary)
        print(result.render())
    return 1 if result.errors else 0


def _cmd_cache_delta_manifest(args: argparse.Namespace) -> int:
    try:
        manifest = build_manifest(Path(args.dir))
    except DeltaError as exc:
        print(f"ci-lint cache delta manifest: {exc}", file=sys.stderr)
        return 2
    text = manifest_to_json(manifest)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def _cmd_cache_delta_pack(args: argparse.Namespace) -> int:
    try:
        base = load_manifest(Path(args.base_manifest))
        changed = pack_delta(
            Path(args.dir), base, Path(args.out), family=args.family, platform=args.platform, pr=args.pr
        )
    except DeltaError as exc:
        print(f"ci-lint cache delta pack: {exc}", file=sys.stderr)
        return 2
    print(f"ci-lint cache delta pack: packed {len(changed)} file(s) into {args.out}")
    return 0


def _cmd_cache_delta_apply(args: argparse.Namespace) -> int:
    try:
        base = load_manifest(Path(args.base_manifest))
    except DeltaError as exc:
        print(f"ci-lint cache delta apply: {exc}", file=sys.stderr)
        return 2
    try:
        extracted = apply_delta(Path(args.dir), Path(args.delta), base)
    except StaleBaseError as exc:
        print(f"ci-lint cache delta apply: {exc}", file=sys.stderr)
        return 3
    except DeltaError as exc:
        print(f"ci-lint cache delta apply: {exc}", file=sys.stderr)
        return 2
    print(f"ci-lint cache delta apply: applied {len(extracted)} file(s) from {args.delta}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci_lint")
    sub = parser.add_subparsers(dest="command", required=True)

    p_precheck = sub.add_parser("precheck", help="run the static precheck")
    p_precheck.add_argument("--repo", default=".")
    p_precheck.add_argument("--local", action="store_true")
    p_precheck.add_argument("--event", default=None)
    p_precheck.add_argument("--title", default=None)
    p_precheck.add_argument("--plan-out", default=None)
    p_precheck.add_argument(
        "--github-output",
        action="store_true",
        help="also write the plan's $GITHUB_OUTPUT keys (same as 'ci_lint plan --github-output')",
    )
    p_precheck.add_argument(
        "--reuse",
        action="store_true",
        help="compute title-edit reuse (round-3A Part 2) and include it in --plan-out/--github-output",
    )
    p_precheck.add_argument("--json", action="store_true")
    p_precheck.add_argument(
        "--live",
        action="store_true",
        help="also run the live cache audit (round-4A; ci_lint.cache.audit) and fold its findings in "
        "as warnings, except CACHE-009 which fails; requires GITHUB_TOKEN + GITHUB_REPOSITORY "
        "(actions: read), else reports a single needs_review instead of silently skipping",
    )
    p_precheck.add_argument(
        "--default-branch",
        default=None,
        help="the branch a base cache layer may legitimately be written from (--live's CACHE-003 check); "
        "default: inferred from $GITHUB_EVENT_PATH, then GET /repos/<owner>/<name> (#425)",
    )
    p_precheck.set_defaults(func=_cmd_precheck)

    p_plan = sub.add_parser("plan", help="compute the flow/tag selection")
    p_plan.add_argument("--repo", default=".")
    p_plan.add_argument("--event-name", default=None)
    p_plan.add_argument("--title", default=None)
    p_plan.add_argument("--ref", default=None)
    p_plan.add_argument("--event", default=None)
    p_plan.add_argument("--out", default=None)
    p_plan.add_argument("--github-output", action="store_true")
    p_plan.add_argument("--act", default=None)
    p_plan.add_argument("--sha", default=None)
    p_plan.add_argument(
        "--reuse",
        action="store_true",
        help="look up title-edit reuse for pull_request events (round-3A Part 2); "
        "requires GITHUB_TOKEN + GITHUB_REPOSITORY and the event's PR head SHA, else reuses nothing",
    )
    p_plan.set_defaults(func=_cmd_plan)

    p_units = sub.add_parser("units", help="compile-unit count + RUST-011 from a cargo --message-format=json run")
    p_units.add_argument("--repo", default=".")
    p_units.add_argument("--artifacts", required=True)
    p_units.add_argument("--json", action="store_true")
    p_units.set_defaults(func=_cmd_units)

    p_tests = sub.add_parser("tests", help="test-binary checks")
    tests_sub = p_tests.add_subparsers(dest="tests_command", required=True)
    p_tests_size = tests_sub.add_parser(
        "size", help="test-binary declaration + size budget (RUST-012) from a cargo --message-format=json run"
    )
    p_tests_size.add_argument("--repo", default=".")
    p_tests_size.add_argument("--artifacts", required=True)
    p_tests_size.add_argument("--json", action="store_true")
    p_tests_size.set_defaults(func=_cmd_tests_size)

    p_suite = sub.add_parser("suite", help="suite checks: check (TEST-001/002)")
    suite_sub = p_suite.add_subparsers(dest="suite_command", required=True)
    p_suite_check = suite_sub.add_parser(
        "check",
        help="TEST-001 (a skip in a required suite) / TEST-002 (zero executed tests in a required "
        "suite) from a libtest log and/or a pytest JUnit XML report",
    )
    p_suite_check.add_argument("--repo", default=".")
    p_suite_check.add_argument("--suite", required=True)
    p_suite_check.add_argument("--cargo-test-log", action="append", default=[])
    p_suite_check.add_argument("--pytest-junit", action="append", default=[])
    p_suite_check.add_argument("--json", action="store_true")
    p_suite_check.set_defaults(func=_cmd_suite_check)

    p_dylint = sub.add_parser("dylint", help="dylint checks: coverage (RUST-003)")
    dylint_sub = p_dylint.add_subparsers(dest="dylint_command", required=True)
    p_dylint_coverage = dylint_sub.add_parser(
        "coverage",
        help="RUST-003: every ci.toml [platforms] target (when [lint.dylint].targets = "
        "'all-platforms') must show a completed Dylint check pass in the given evidence, from a "
        "real Dylint job log and/or ci/dylint.py's own --results-out JSON",
    )
    p_dylint_coverage.add_argument("--repo", default=".")
    p_dylint_coverage.add_argument(
        "--log",
        action="append",
        default=[],
        help="a Dylint job log (gh run view --job <id> --log) or ci/dylint.py's --results-out JSON; "
        "repeatable",
    )
    p_dylint_coverage.add_argument("--json", action="store_true")
    p_dylint_coverage.set_defaults(func=_cmd_dylint_coverage)

    p_example = sub.add_parser("example", help="canonical-example checks: drift")
    example_sub = p_example.add_subparsers(dest="example_command", required=True)
    p_example_drift = example_sub.add_parser(
        "drift",
        help="key-by-key ci.toml diff against the canonical example, classified repo-specific "
        "vs schema/policy drift (issue #6's 'identical apart from repo-specific values' rule)",
    )
    p_example_drift.add_argument("--example", required=True)
    p_example_drift.add_argument("--repo", required=True)
    p_example_drift.add_argument("--json", action="store_true")
    p_example_drift.set_defaults(func=_cmd_example_drift)

    p_fbuild = sub.add_parser("fbuild", help="fbuild-shaped coverage-preservation checks (ci.yml#39)")
    fbuild_sub = p_fbuild.add_subparsers(dest="fbuild_command", required=True)
    p_fbuild_coverage = fbuild_sub.add_parser(
        "coverage-check",
        help="existing required coverage ⊆ declared coverage: board build cases, macOS "
        "architectures, ignored Python facade tests, full-graph gates, and standalone required "
        "checks, each from a ci.toml [coverage] table (proposal.md's fbuild-coverage-preserved "
        "acceptance fixture)",
    )
    p_fbuild_coverage.add_argument("--existing", required=True, help="the pinned/real ci.toml")
    p_fbuild_coverage.add_argument("--declared", required=True, help="the proposed/faster ci.toml")
    p_fbuild_coverage.add_argument("--json", action="store_true")
    p_fbuild_coverage.set_defaults(func=_cmd_fbuild_coverage_check)

    p_wheel = sub.add_parser("wheel", help="wheel packaging checks (PKG-003/004/005)")
    wheel_sub = p_wheel.add_subparsers(dest="wheel_command", required=True)
    p_wheel_check = wheel_sub.add_parser("check", help="check a built wheel (+ optional sdist) by its own bytes")
    p_wheel_check.add_argument("--repo", default=".")
    p_wheel_check.add_argument("--wheel", required=True)
    p_wheel_check.add_argument("--sdist", default=None)
    p_wheel_check.add_argument("--json", action="store_true")
    p_wheel_check.set_defaults(func=_cmd_wheel_check)

    p_wheel_installed = wheel_sub.add_parser("installed", help="check a wheel installed into a venv")
    p_wheel_installed.add_argument("--repo", default=".")
    p_wheel_installed.add_argument("--venv", required=True)
    p_wheel_installed.add_argument("--json", action="store_true")
    p_wheel_installed.set_defaults(func=_cmd_wheel_installed)

    p_gate = sub.add_parser("gate", help="CI OK aggregator: plan.json's required_jobs vs needs.json")
    p_gate.add_argument("--repo", default=".")
    gate_plan = p_gate.add_mutually_exclusive_group(required=True)
    gate_plan.add_argument("--plan", help="planner's JSON required-job plan")
    gate_plan.add_argument("--workflow-plan", help="derive native job selection from this workflow basename")
    p_gate.add_argument("--gate-job", default="ci-ok", help="native aggregation job whose needs define coverage")
    p_gate.add_argument("--needs", required=True)
    p_gate.add_argument("--attested-workflow", default=None,
                        help="workflow basename whose skipped jobs must be reverified from the PR base policy and head attestations")
    p_gate.add_argument(
        "--reuse",
        default=None,
        help="path to the plan's reuse map JSON (round-3A Part 3; from "
        "'ci_lint plan --reuse's reuse_json/--out \"reuse\" key)",
    )
    p_gate.add_argument(
        "--event",
        default=None,
        help="pull_request event JSON, to read the PR head SHA for reuse re-verification",
    )
    p_gate.add_argument(
        "--default-branch-reuse",
        default=None,
        help="path to a 'ci-lint reuse-check --out' document (GEN-021, zackees/ci.yml#158). A "
        "required job that is skipped counts as success only when that document proves it "
        "(reuse == true, this push's sha, a push event) AND a live job re-fetch still shows "
        "conclusion == 'success' on the PR head. This is NOT the same as --reuse above, which "
        "proves a lane of the same PR from its own earlier attempt",
    )
    p_gate.add_argument("--json", action="store_true")
    p_gate.set_defaults(func=_cmd_gate)

    p_selftest = sub.add_parser("selftest", help="run ci_lint's own unittest suite")
    p_selftest.set_defaults(func=_cmd_selftest)

    p_cache = sub.add_parser("cache", help="cache runtime: key, save-ok, audit, trim, janitor, budget, heal, preprune, delta")
    cache_sub = p_cache.add_subparsers(dest="cache_command", required=True)

    p_cache_key = cache_sub.add_parser("key", help="build a cache key for a declared family, or a PR delta key")
    p_cache_key.add_argument("family")
    p_cache_key.add_argument("--repo", default=".")
    p_cache_key.add_argument("--platform", default=None)
    p_cache_key.add_argument("--pr", type=int, default=None)
    p_cache_key.add_argument("--base-key", default=None, help="required with --pr: the restored base's literal key")
    p_cache_key.set_defaults(func=_cmd_cache_key)

    p_cache_save_ok = cache_sub.add_parser("save-ok", help="evaluate issue #6 §6's do-not-save table (CACHE-008)")
    p_cache_save_ok.add_argument("family")
    p_cache_save_ok.add_argument("--repo", default=".")
    p_cache_save_ok.add_argument("--flow", required=True)
    p_cache_save_ok.add_argument("--event", required=True)
    p_cache_save_ok.add_argument("--pr", type=int, default=None)
    p_cache_save_ok.add_argument("--fork", action="store_true")
    p_cache_save_ok.add_argument("--act", action="store_true")
    build_group = p_cache_save_ok.add_mutually_exclusive_group()
    build_group.add_argument("--build-ok", dest="build_failed", action="store_false")
    build_group.add_argument("--build-failed", dest="build_failed", action="store_true")
    p_cache_save_ok.set_defaults(build_failed=False)
    p_cache_save_ok.add_argument("--exact-hit", action="store_true")
    p_cache_save_ok.add_argument("--new-units", type=int, default=None)
    p_cache_save_ok.add_argument("--payload-bytes", type=int, default=None)
    p_cache_save_ok.add_argument("--base-key", default=None)
    p_cache_save_ok.add_argument("--current-base-key", default=None)
    p_cache_save_ok.add_argument("--lockfile-changed", action="store_true")
    p_cache_save_ok.add_argument("--tags", default=None)
    p_cache_save_ok.add_argument("--rerun-saved", action="store_true")
    p_cache_save_ok.add_argument(
        "--path", action="append", default=None,
        help="a path the save would archive (repeatable); rule 11 refuses a CMake build tree",
    )
    p_cache_save_ok.add_argument("--json", action="store_true")
    p_cache_save_ok.set_defaults(func=_cmd_cache_save_ok)

    p_cache_audit = cache_sub.add_parser("audit", help="live, read-only classification of every cache entry")
    p_cache_audit.add_argument("--repo", default=".")
    p_cache_audit.add_argument(
        "--default-branch",
        default=None,
        help="default: inferred from $GITHUB_EVENT_PATH, then GET /repos/<repo>; "
        "unresolvable -> CACHE-003 skipped as needs_review, never guessed (#425)",
    )
    p_cache_audit.add_argument("--json", action="store_true")
    p_cache_audit.set_defaults(func=_cmd_cache_audit)

    p_cache_hit_rate = cache_sub.add_parser(
        "hit-rate",
        help="GEN-023 (#411) from real job-log evidence: a compiling job with no compile-cache stats line, "
        "or a hit rate below the threshold (needs_review)",
    )
    p_cache_hit_rate.add_argument("--log", action="append", default=[], help="gh run view --log output; repeatable")
    p_cache_hit_rate.add_argument("--threshold", type=float, default=0.80, help="hit-rate floor (default: 0.80)")
    p_cache_hit_rate.add_argument(
        "--min-compilations", type=int, default=50, help="judge the rate only at or above this many (default: 50)"
    )
    p_cache_hit_rate.add_argument("--json", action="store_true")
    p_cache_hit_rate.set_defaults(func=_cmd_cache_hit_rate)

    p_cache_save_check = cache_sub.add_parser(
        "save-check",
        help="CACHE-011/012/026 from real job-log evidence: a green default-branch run that "
        "saved nothing, a restore stuck on a poisoned/empty archive, and a guard freezing cache writes",
    )
    p_cache_save_check.add_argument(
        "--log",
        action="append",
        default=[],
        help="a job log (gh run view --job <id> --log) or captured stdout of the same; repeatable, "
        "oldest-first for CACHE-012's consecutive-run check",
    )
    p_cache_save_check.add_argument(
        "--conclusion",
        required=True,
        help="the run/job's actual GitHub conclusion (e.g. 'success'); CACHE-011 only fires when this "
        "is exactly 'success'",
    )
    p_cache_save_check.add_argument("--run-url", default=None, help="the run's html_url, for finding messages")
    p_cache_save_check.add_argument(
        "--unusable-threshold", type=int, default=2,
        help="CACHE-012: consecutive unusable-restore logs required to fire (default: 2)",
    )
    p_cache_save_check.add_argument(
        "--guard-freeze-threshold", type=int, default=3,
        help="CACHE-026: consecutive default-branch logs whose cache guard disabled writes "
        "required to fire (default: 3; pass default-branch logs only, oldest-first)",
    )
    p_cache_save_check.add_argument("--json", action="store_true")
    p_cache_save_check.set_defaults(func=_cmd_cache_save_check)

    p_cache_rework = cache_sub.add_parser(
        "rework",
        help="CACHE-035 (issue #148) from real job-log evidence: rebuild after an exact cache hit, "
        "and the same crate compiled in more than one step",
    )
    p_cache_rework.add_argument(
        "--log",
        action="append",
        default=[],
        help="a job log (gh run view --job <id> --log) or captured stdout of the same; repeatable, "
        "all logs of one run/job for the cross-step check",
    )
    p_cache_rework.add_argument("--json", action="store_true")
    p_cache_rework.set_defaults(func=_cmd_cache_rework)

    p_cache_trim = cache_sub.add_parser("trim", help="delete CACHE-008 entries (closed/merged + stale-base PR deltas)")
    p_cache_trim.add_argument("--repo", default=".")
    p_cache_trim.add_argument("--default-branch", default=None, help="default: inferred (#425); see cache audit")
    p_cache_trim.add_argument("--max-deletes", type=int, default=50)
    p_cache_trim.add_argument("--dry-run", action="store_true")
    p_cache_trim.add_argument("--json", action="store_true")
    p_cache_trim.set_defaults(func=_cmd_cache_trim)

    p_cache_janitor = cache_sub.add_parser(
        "janitor", help="delete closed-PR entries, CACHE-001/003/005/006/009, LRU overflow, repo-wide LRU down to [cache].budget and stale entries (10-min grace, newest restored entry kept); prints a before/after table"
    )
    p_cache_janitor.add_argument("--repo", default=".")
    p_cache_janitor.add_argument(
        "--default-branch",
        default=None,
        help="default: inferred (#425); unresolvable skips CACHE-003 deletions (fail closed)",
    )
    p_cache_janitor.add_argument("--max-deletes", type=int, default=100)
    p_cache_janitor.add_argument("--stale-days", type=int, default=5)
    p_cache_janitor.add_argument(
        "--no-enforce-budget", dest="enforce_budget", action="store_false",
        help="skip the repo-wide LRU pass that keeps the total under [cache].budget (#405)",
    )
    p_cache_janitor.add_argument("--dry-run", action="store_true")
    p_cache_janitor.add_argument("--json", action="store_true")
    p_cache_janitor.set_defaults(func=_cmd_cache_janitor)

    p_cache_budget = cache_sub.add_parser(
        "budget", help="live budget verdict: warn-only in PR context (except the PR's own pr-<N> entries), "
        "hard fail on the default branch/schedule/dispatch when over budget"
    )
    p_cache_budget.add_argument("--repo", default=".")
    p_cache_budget.add_argument("--default-branch", default="main")
    p_cache_budget.add_argument("--event-name", default=None, help="default: $GITHUB_EVENT_NAME")
    p_cache_budget.add_argument("--ref", default=None, help="default: $GITHUB_REF")
    p_cache_budget.add_argument("--event", default=None, help="event JSON (default: $GITHUB_EVENT_PATH)")
    p_cache_budget.add_argument("--pr", type=int, default=None, help="PR number (default: from the event)")
    p_cache_budget.add_argument("--json", action="store_true")
    p_cache_budget.set_defaults(func=_cmd_cache_budget)

    p_cache_heal = cache_sub.add_parser("heal", help="delete exactly one cache key (a writer flow's self-heal)")
    p_cache_heal.add_argument("--repo", default=".")
    p_cache_heal.add_argument("--key", required=True)
    p_cache_heal.add_argument("--ref", default=None)
    p_cache_heal.add_argument("--dry-run", action="store_true")
    p_cache_heal.add_argument("--json", action="store_true")
    p_cache_heal.set_defaults(func=_cmd_cache_heal)

    p_cache_preprune = cache_sub.add_parser("preprune", help="forecast the lockfile-change peak with live sizes; prune if over budget")
    p_cache_preprune.add_argument("--repo", default=".")
    p_cache_preprune.add_argument("--default-branch", default=None, help="default: inferred (#425); see cache audit")
    p_cache_preprune.add_argument("--lockfile-changed", action="store_true")
    p_cache_preprune.add_argument("--max-deletes", type=int, default=50)
    p_cache_preprune.add_argument("--dry-run", action="store_true")
    p_cache_preprune.add_argument("--json", action="store_true")
    p_cache_preprune.set_defaults(func=_cmd_cache_preprune)

    p_cache_payload_check = cache_sub.add_parser(
        "payload-check",
        help="CACHE-007 runtime: classify a save's actual path manifest against [cache].never",
    )
    p_cache_payload_check.add_argument(
        "--manifest", required=True, help="a JSON array of path strings (e.g. from 'tar -tf' on the saved archive)"
    )
    p_cache_payload_check.add_argument("--json", action="store_true")
    p_cache_payload_check.set_defaults(func=_cmd_cache_payload_check)

    p_cache_delta = cache_sub.add_parser("delta", help="PR delta mechanics: manifest, pack, apply (pure filesystem)")
    delta_sub = p_cache_delta.add_subparsers(dest="cache_delta_command", required=True)

    p_delta_manifest = delta_sub.add_parser("manifest", help="sorted (relpath, size) list + sha256 digest of --dir")
    p_delta_manifest.add_argument("--dir", required=True)
    p_delta_manifest.add_argument("--out", required=True)
    p_delta_manifest.set_defaults(func=_cmd_cache_delta_manifest)

    p_delta_pack = delta_sub.add_parser("pack", help="pack files absent from/changed vs --base-manifest into --out")
    p_delta_pack.add_argument("--dir", required=True)
    p_delta_pack.add_argument("--base-manifest", required=True)
    p_delta_pack.add_argument("--out", required=True)
    p_delta_pack.add_argument("--family", required=True)
    p_delta_pack.add_argument("--platform", required=True)
    p_delta_pack.add_argument("--pr", type=int, required=True)
    p_delta_pack.set_defaults(func=_cmd_cache_delta_pack)

    p_delta_apply = delta_sub.add_parser("apply", help="apply --delta onto --dir; exit 3 if its base is stale")
    p_delta_apply.add_argument("--dir", required=True)
    p_delta_apply.add_argument("--delta", required=True)
    p_delta_apply.add_argument("--base-manifest", required=True)
    p_delta_apply.set_defaults(func=_cmd_cache_delta_apply)

    p_audit = sub.add_parser(
        "audit", help="live settings audit: SEC-005/006/007, GEN-006/011 (distinct from 'cache audit')"
    )
    p_audit.add_argument("--repo", default=".")
    p_audit.add_argument("--default-branch", default="main")
    p_audit.add_argument(
        "--gate-check-name",
        default=DEFAULT_GATE_CHECK_NAME,
        help=f"the required-status-check name GEN-011 expects (default: {DEFAULT_GATE_CHECK_NAME!r})",
    )
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=_cmd_audit)

    p_publish = sub.add_parser("publish", help="publish checks: oidc-check (issue #6 §7 mock publisher)")
    publish_sub = p_publish.add_subparsers(dest="publish_command", required=True)

    p_publish_oidc = publish_sub.add_parser(
        "oidc-check", help="mint the mock publisher's OIDC token, assert its claims, never print it"
    )
    p_publish_oidc.add_argument("--repo", default=".")
    p_publish_oidc.add_argument("--audience", required=True)
    p_publish_oidc.add_argument("--expect-ref", default="refs/heads/main")
    p_publish_oidc.add_argument("--expect-event", default="workflow_dispatch")
    p_publish_oidc.add_argument("--json", action="store_true")
    p_publish_oidc.set_defaults(func=_cmd_publish_oidc_check)

    p_release = sub.add_parser("release", help="release-candidate checks: verify, resume-plan, verify-frozen")
    release_sub = p_release.add_subparsers(dest="release_command", required=True)

    p_release_verify = release_sub.add_parser(
        "verify", help="verify the staged release artifact set (PKG-006) and write release-manifest.json"
    )
    p_release_verify.add_argument("--repo", default=".")
    p_release_verify.add_argument("--dist", required=True, help="directory holding the staged wheels + sdist")
    p_release_verify.add_argument("--sha", required=True, help="the candidate's exact 40-hex commit SHA")
    p_release_verify.add_argument(
        "--smoke", default=None, help="a directory of smoke-results/*.json (platform-run's own output)"
    )
    p_release_verify.add_argument(
        "--readback",
        default=None,
        help="REL-003: a directory of readback/<artifact>.json records proving the dry run read the mock "
        "publisher's staged destination back (zackees/ci.yml#8/#74)",
    )
    p_release_verify.add_argument(
        "--resume",
        default=None,
        help="REL-004: the prior (frozen) release-manifest.json a resume must reuse byte-identical "
        "artifacts from -- fails if the current --dist rebuilt them (zackees/ci.yml#8/#74)",
    )
    p_release_verify.add_argument("--json", action="store_true")
    p_release_verify.set_defaults(func=_cmd_release_verify)
    register_release_resume(release_sub)

    p_perf = sub.add_parser("perf", help="perf checks: compare, pr-timing (PERF-001)")
    perf_sub = p_perf.add_subparsers(dest="perf_command", required=True)

    p_perf_compare = perf_sub.add_parser("compare", help="compare a baseline and a current benchmark report")
    p_perf_compare.add_argument("--repo", default=".")
    p_perf_compare.add_argument("--baseline", required=True)
    p_perf_compare.add_argument("--current", required=True)
    p_perf_compare.add_argument("--threshold-pct", type=float, default=None)
    p_perf_compare.add_argument("--json", action="store_true")
    p_perf_compare.set_defaults(func=_cmd_perf_compare)

    p_act = sub.add_parser("act", help="local act cache-store checks")
    act_sub = p_act.add_subparsers(dest="act_command", required=True)

    p_act_audit = act_sub.add_parser(
        "audit", help="ACT-001: audit a local act cache-server store against [cache].budget/[cache].retired"
    )
    p_act_audit.add_argument("--repo", default=".")
    p_act_audit.add_argument("--store-dir", required=True, help="the act --cache-server-path directory")
    p_act_audit.add_argument("--json", action="store_true")
    p_act_audit.set_defaults(func=_cmd_act_audit)

    p_perf_pr_timing = perf_sub.add_parser(
        "pr-timing", help="PERF-001: PR timing breach from recorded run-history JSON (>= 2 scans -> --history twice)"
    )
    p_perf_pr_timing.add_argument(
        "--history", action="append", required=True, help="one recorded scan's history JSON; repeat in scan order"
    )
    p_perf_pr_timing.add_argument("--min-samples", type=int, default=5)
    p_perf_pr_timing.add_argument("--window-days", type=int, default=30)
    p_perf_pr_timing.add_argument("--job-threshold-minutes", type=float, default=10.0)
    p_perf_pr_timing.add_argument("--critical-path-threshold-minutes", type=float, default=15.0)
    p_perf_pr_timing.add_argument("--json", action="store_true")
    p_perf_pr_timing.set_defaults(func=_cmd_perf_pr_timing)

    p_rust = sub.add_parser("rust", help="rust checks: toolchain-build-check (RUST-009)")
    rust_sub = p_rust.add_subparsers(dest="rust_command", required=True)

    p_rust_toolchain_build = rust_sub.add_parser(
        "toolchain-build-check", help="RUST-009: per-run toolchain/std/driver builds from job-log evidence"
    )
    p_rust_toolchain_build.add_argument(
        "--log", action="append", help="a job log file (raw text) or gh run view --job <id> --log output"
    )
    p_rust_toolchain_build.add_argument("--json", action="store_true")
    p_rust_toolchain_build.set_defaults(func=_cmd_rust_toolchain_build_check)

    register_fleet(sub)
    register_gh_cache(sub)
    register_reuse(sub)
    register_local_gate(sub)
    register_attest(sub)
    register_py(sub)
    register_remote_only(sub)
    register_complexity(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def entrypoint() -> None:
    """The `ci-lint` console script (pyproject.toml)."""

    sys.exit(main(sys.argv[1:]))
