"""CLI wiring for `ci-lint fleet scan` and `ci-lint sync-issues --dry-run`
(zackees/ci.yml#38). Kept out of ci_lint/cli.py, which only calls
`register(sub)`.

Token: `GITHUB_TOKEN` (or `GH_TOKEN`) from the environment; it is sent only
as a bearer header to api.github.com and never printed. `--record FILE`
writes every (url -> [status, body]) response the run made, for building
the replayed unit-test fixtures; `--replay FILE` runs against such a
recording instead of the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.fleet import DEFAULT_OWNERS, FleetReport, load_report_json, run_fleet_scan
from ci_lint.fleet import render_text as render_fleet_text
from ci_lint.fleet import to_json_dict as fleet_to_json_dict
from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import FetchStatusFn, GitHubApiError, default_fetch_status, default_write
from ci_lint.history import build_history, load_snapshot
from ci_lint.history import render_text as render_history_text
from ci_lint.history import to_json_dict as history_to_json_dict
from ci_lint.sync_issues import apply_to_json, plan_sync, run_apply
from ci_lint.sync_issues import render_apply_text as render_sync_apply_text
from ci_lint.sync_issues import render_text as render_sync_text
from ci_lint.sync_issues import to_json_list as sync_to_json_list

DEFAULT_MAX_WRITES = 5

# A recorded `--record`/`--replay` fixture: URL -> `[status, body]`, a
# single-level JSON document at its serialization boundary (PY-002).
RecordTable = dict[str, JsonValue]


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def replay_fetch(table: RecordTable) -> FetchStatusFn:
    def fetch_status(url: str, token: str) -> tuple[int, object]:
        entry = table.get(url)
        if entry is None:
            raise GitHubApiError(f"GET {url}: not in the recorded fixture")
        return int(entry[0]), entry[1]  # type: ignore[arg-type]

    return fetch_status  # type: ignore[return-value]


def _recording(inner: FetchStatusFn, table: RecordTable) -> FetchStatusFn:
    def fetch_status(url: str, token: str) -> tuple[int, object]:
        status, body = inner(url, token)
        table[url] = [status, body]
        return status, body

    return fetch_status  # type: ignore[return-value]


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _fetcher(args: argparse.Namespace, command: str) -> tuple[FetchStatusFn, str, RecordTable | None] | None:
    if args.replay:
        table = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        return replay_fetch(table), "replay", None
    token = _token()
    if not token:
        print(f"ci-lint {command}: GITHUB_TOKEN (or GH_TOKEN) is required for a live run", file=sys.stderr)
        return None
    if args.record:
        table: RecordTable = {}
        return _recording(default_fetch_status, table), token, table
    return default_fetch_status, token, None


def _scan(args: argparse.Namespace, fetch: FetchStatusFn, token: str) -> FleetReport:
    owners = _split(args.owners) or DEFAULT_OWNERS
    return run_fleet_scan(fetch, token, owners=owners, repos=_split(args.repos), include_forks=args.include_forks)


def _cmd_fleet_scan(args: argparse.Namespace) -> int:
    got = _fetcher(args, "fleet scan")
    if got is None:
        return 2
    fetch, token, table = got
    report = _scan(args, fetch, token)
    if table is not None:
        Path(args.record).write_text(json.dumps(table, indent=1, sort_keys=True), encoding="utf-8")
    if args.json:
        doc = fleet_to_json_dict(report)
        # `ci-lint history` dates each recorded scan by this field (#99).
        doc["scanned_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(json.dumps(doc, indent=2))
    else:
        print(render_fleet_text(report))
    return 1 if any(f.status == Status.VIOLATION for s in report.repos for f in s.findings) else 0


def _cmd_sync_issues(args: argparse.Namespace) -> int:  # noqa: C901
    if args.dry_run and args.apply:
        print("ci-lint sync-issues: --dry-run and --apply are mutually exclusive", file=sys.stderr)
        return 2
    if not args.dry_run and not args.apply:
        print("ci-lint sync-issues: pass exactly one of --dry-run or --apply", file=sys.stderr)
        return 2
    if args.apply:
        if args.offline:
            print("ci-lint sync-issues --apply: --offline cannot be combined with --apply "
                  "(existing issues must be read live so updates/closes are correct)", file=sys.stderr)
            return 2
        if args.replay:
            print("ci-lint sync-issues --apply: --replay has no write path; use --dry-run to test "
                  "against a recording", file=sys.stderr)
            return 2
        if args.max_writes <= 0:
            print("ci-lint sync-issues --apply: --max-writes must be a positive integer", file=sys.stderr)
            return 2

    if args.from_scan:
        try:
            report = load_report_json(Path(args.from_scan).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"ci-lint sync-issues: cannot read --from-scan: {exc}", file=sys.stderr)
            return 2
        if args.dry_run and args.offline:
            actions, errors = plan_sync(report, None, "")
            print(json.dumps(sync_to_json_list(actions), indent=2) if args.json else render_sync_text(actions, errors))
            return 0
        got = _fetcher(args, "sync-issues")
        if got is None:
            return 2
        fetch, token, _ = got
    else:
        got = _fetcher(args, "sync-issues")
        if got is None:
            return 2
        fetch, token, _ = got
        report = _scan(args, fetch, token)

    if args.apply:
        result = run_apply(report, fetch, default_write, token, args.max_writes)
        print(json.dumps(apply_to_json(result), indent=2) if args.json else render_sync_apply_text(result))
        return 1 if result.errors else 0

    actions, errors = plan_sync(report, None if args.offline else fetch, token)
    print(json.dumps(sync_to_json_list(actions), indent=2) if args.json else render_sync_text(actions, errors))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    try:
        report = build_history([load_snapshot(Path(p)) for p in args.scans])
    except (OSError, ValueError) as exc:
        print(f"ci-lint history: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(history_to_json_dict(report), indent=2) if args.json else render_history_text(report))
    return 1 if any(e.state == "new" and e.status == Status.VIOLATION.value for e in report.entries) else 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--owners", default=",".join(DEFAULT_OWNERS), help="comma-separated owners/orgs")
    p.add_argument("--repos", default=None, help="comma-separated repo names (bare or owner/name) to narrow to")
    p.add_argument("--include-forks", action="store_true")
    p.add_argument("--replay", default=None, help="replay a --record file instead of calling the API")
    p.add_argument("--json", action="store_true")


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p_fleet = sub.add_parser("fleet", help="fleet-wide, read-only checks: scan (zackees/ci.yml#38)")
    fleet_sub = p_fleet.add_subparsers(dest="fleet_command", required=True)
    p_scan = fleet_sub.add_parser("scan", help="live, read-only ranked inventory of fleet repositories")
    _common(p_scan)
    p_scan.add_argument("--record", default=None, help="write every API response to this JSON file")
    p_scan.set_defaults(func=_cmd_fleet_scan)

    p_sync = sub.add_parser("sync-issues", help="print the fingerprinted issues a fleet scan would create/update")
    _common(p_sync)
    p_sync.add_argument("--dry-run", action="store_true", help="print the plan only; no writes")
    p_sync.add_argument(
        "--apply",
        action="store_true",
        help="perform the writes (create/update/close), gated by per-repo [fleet].sync-issues opt-in "
        "and --max-writes (zackees/ci.yml#98)",
    )
    p_sync.add_argument(
        "--max-writes",
        type=int,
        default=DEFAULT_MAX_WRITES,
        help=f"cap on issue writes in one --apply run (default {DEFAULT_MAX_WRITES})",
    )
    p_sync.add_argument("--from-scan", default=None, help="a `fleet scan --json` file instead of a live scan")
    p_sync.add_argument("--offline", action="store_true", help="skip reading existing issues (plan all as create)")
    p_sync.set_defaults(func=_cmd_sync_issues, record=None)

    p_hist = sub.add_parser(
        "history", help="diff recorded `fleet scan --json` files: new/persisting/resolved findings (#99)"
    )
    p_hist.add_argument("scans", nargs="+", help="two or more `fleet scan --json` files (dated by scanned_at or filename)")
    p_hist.add_argument("--json", action="store_true")
    p_hist.set_defaults(func=_cmd_history)
