"""CLI wiring for `ci-lint reuse-check` and `ci-lint reuse-report` (GEN-021,
zackees/ci.yml#156). Kept out of ci_lint/cli.py, which only calls
`register(sub)`, like `ci_lint.fleet_cli`.

Token: `GITHUB_TOKEN` (or `GH_TOKEN`), sent only as a bearer header to
api.github.com and never printed. `--record FILE` writes every (url ->
[status, body]) response the run made; `--replay FILE` runs against such a
recording instead of the network (the unit-test fixtures). Both reuse the
record/replay plumbing `ci-lint fleet scan` already has.

Exit codes. `reuse-check`: 0 whenever the arguments are valid -- including
every "cannot prove reuse" outcome, which is reported as `reuse=false` so the
calling workflow runs everything -- and 2 for a usage error only.
`reuse-report`: 0 = promotable (at least one verified safe skip and no
unanalysed false-reuse candidate), 1 = not promotable yet (insufficient
sample, a `no-op` window in which reuse never fired, unanalysed false-reuse
candidates, or a listing error), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ci_lint.default_branch_reuse import (
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MIN_RUNS,
    REPO_RE,
    SHA_RE,
    ReuseRequest,
    decide,
    github_output_lines,
    normalize_workflow,
    parse_time,
    render_report_text,
    render_step_summary,
    render_text,
    report_to_json_dict,
    run_report,
    to_json_dict,
)
from ci_lint.fleet_cli import RecordTable, _recording, _token, replay_fetch
from ci_lint.github_api import FetchStatusFn, default_fetch_status


def _usage(command: str, message: str) -> int:
    print(f"ci-lint {command}: {message}", file=sys.stderr)
    return 2


def _dedupe(values: list[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for v in values or []:
        if v not in out:
            out.append(v)
    return tuple(out)


def _now(args: argparse.Namespace) -> datetime | None:
    value = getattr(args, "now", None)
    if value is None:
        return datetime.now(timezone.utc)
    return parse_time(value)


def _fetcher(args: argparse.Namespace) -> tuple[FetchStatusFn | None, str | None, RecordTable | None]:
    """(fetch, token, record-table). A live run without a token returns
    (None, None, None): `decide` then fails closed with `no-token`."""

    if args.replay:
        return replay_fetch(args.replay_table), "replay", None
    token = _token()
    if not token:
        return None, None, None
    if args.record:
        recorded: RecordTable = {}
        return _recording(default_fetch_status, recorded), token, recorded
    return default_fetch_status, token, None


def _common_request(args: argparse.Namespace, command: str) -> tuple[ReuseRequest | None, int]:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or ""
    if not REPO_RE.match(repo):
        return None, _usage(command, f"--repo must be owner/name (got {repo!r}; or set GITHUB_REPOSITORY)")
    workflows = tuple(normalize_workflow(w) for w in _dedupe(args.workflow))
    if not workflows:
        return None, _usage(command, "at least one --workflow <file> is required")
    required = _dedupe(args.required_job)
    if not required:
        return None, _usage(command, "at least one --required-job <exact job name> is required")
    if not args.max_age_hours > 0:
        return None, _usage(command, "--max-age-hours must be > 0")
    if args.record and args.replay:
        return None, _usage(command, "--record and --replay are mutually exclusive")
    if args.replay:
        try:
            table = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return None, _usage(command, f"cannot read --replay {args.replay!r}: {exc}")
        if not isinstance(table, dict):
            return None, _usage(command, "--replay must be a `--record` JSON object")
        args.replay_table = table
    now = _now(args)
    if now is None:
        return None, _usage(command, f"--now must be an ISO-8601 timestamp (got {getattr(args, 'now', None)!r})")
    return (
        ReuseRequest(
            repo=repo,
            sha="0" * 40,
            workflows=workflows,
            required_jobs=required,
            now=now,
            max_age_hours=float(args.max_age_hours),
            default_branch=args.default_branch,
            ref=f"refs/heads/{args.default_branch}",
        ),
        0,
    )


def _write_record(args: argparse.Namespace, table: RecordTable | None) -> None:
    if table is not None and args.record:
        Path(args.record).write_text(json.dumps(table, indent=1, sort_keys=True), encoding="utf-8")


def _cmd_reuse_check(args: argparse.Namespace) -> int:
    template, code = _common_request(args, "reuse-check")
    if template is None:
        return code
    sha = args.sha or os.environ.get("GITHUB_SHA") or ""
    if not SHA_RE.match(sha):
        return _usage("reuse-check", f"--sha must be a 40-hex commit SHA (got {sha!r}; or set GITHUB_SHA)")
    event_name = args.event_name if args.event_name is not None else os.environ.get("GITHUB_EVENT_NAME", "")
    ref = args.ref if args.ref is not None else os.environ.get("GITHUB_REF", "")
    req = replace(
        template, sha=sha, mode=args.mode, event_name=event_name, ref=ref, queue_evidence=args.queue_evidence
    )
    fetch, token, table = _fetcher(args)
    decision = decide(req, fetch, token)
    _write_record(args, table)

    document = to_json_dict(decision)
    if args.out:
        Path(args.out).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, indent=2) if args.json else render_text(decision))

    if args.github_output:
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(github_output_lines(decision)) + "\n")
        else:
            print("ci-lint reuse-check: --github-output given but GITHUB_OUTPUT is not set", file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(render_step_summary(decision))
        except OSError:
            pass
    return 0


def _cmd_reuse_report(args: argparse.Namespace) -> int:
    template, code = _common_request(args, "reuse-report")
    if template is None:
        return code
    for flag, value in (("--since", args.since), ("--until", args.until)):
        if value is not None and parse_time(f"{value}T00:00:00Z") is None:
            return _usage("reuse-report", f"{flag} must be YYYY-MM-DD (got {value!r})")
    if args.min_runs <= 0 or args.limit <= 0:
        return _usage("reuse-report", "--min-runs and --limit must be positive")
    fetch, token, table = _fetcher(args)
    if fetch is None or token is None:
        return _usage("reuse-report", "GITHUB_TOKEN (or GH_TOKEN) is required for a live run (or pass --replay)")
    report = run_report(
        fetch,
        token,
        template,
        args.since,
        args.until,
        min_runs=args.min_runs,
        limit=args.limit,
        accept_flaky=frozenset(args.accept_flaky or ()),
    )
    _write_record(args, table)
    print(json.dumps(report_to_json_dict(report), indent=2) if args.json else render_report_text(report))
    return 0 if report.verdict == "promotable" else 1


def _shared(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=None, help="owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument(
        "--workflow",
        action="append",
        help="workflow file whose pull_request runs prove the tree (repeatable; e.g. ci.yml)",
    )
    p.add_argument(
        "--required-job",
        action="append",
        help="exact job display name that must be green in the proving run (repeatable), e.g. "
        "'Build linux-x64 / x86_64-unknown-linux-gnu'",
    )
    p.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    p.add_argument("--default-branch", default="main")
    p.add_argument("--json", action="store_true")
    p.add_argument("--record", default=None, help="write every API response to this JSON file")
    p.add_argument("--replay", default=None, help="replay a --record file instead of calling the API")


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p_check = sub.add_parser(
        "reuse-check",
        help="GEN-021: may this default-branch push reuse a validated PR head's CI? (GET-only, fail-closed)",
    )
    _shared(p_check)
    p_check.add_argument("--sha", default=None, help="the pushed commit (default: $GITHUB_SHA)")
    p_check.add_argument("--now", default=None, help="evaluation clock, ISO-8601 (default: current UTC time)")
    p_check.add_argument("--mode", choices=("enforce", "shadow"), default="enforce")
    p_check.add_argument(
        "--queue-evidence",
        action="store_true",
        help="also accept a green merge_group run on the pushed commit itself when the PR head's "
        "proof fails (tree-mismatch / no-successful-run): the queue tested exactly that commit",
    )
    p_check.add_argument("--event-name", default=None, help="default: $GITHUB_EVENT_NAME")
    p_check.add_argument("--ref", default=None, help="default: $GITHUB_REF")
    p_check.add_argument(
        "--github-output",
        action="store_true",
        help="append reuse, would_reuse, reason, pr, run_id, run_url, tree to $GITHUB_OUTPUT",
    )
    p_check.add_argument("--out", default=None, help="also write the --json document to this file")
    p_check.set_defaults(func=_cmd_reuse_check)

    p_report = sub.add_parser(
        "reuse-report",
        help="GEN-021 shadow evidence: re-evaluate past default-branch push runs and compare with their result",
    )
    _shared(p_report)
    p_report.add_argument("--since", required=True, help="YYYY-MM-DD (push runs created on/after)")
    p_report.add_argument("--until", default=None, help="YYYY-MM-DD (push runs created on/before)")
    p_report.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_MIN_RUNS,
        help="decisive runs required before promotion (default 1: a single verified, "
        "attested PR merge is enough, because the per-run decision is sound on its own "
        "terms). Raise it only to stage a rollout deliberately.",
    )
    p_report.add_argument("--limit", type=int, default=300, help="most push runs to evaluate")
    p_report.add_argument(
        "--accept-flaky",
        action="append",
        type=int,
        help="a false-reuse candidate run id analysed and proven to be a known flake (repeatable)",
    )
    p_report.set_defaults(func=_cmd_reuse_report)
