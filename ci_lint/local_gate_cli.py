"""CLI wiring for `ci-lint local-gate ...` (GATE-001..004, zackees/ci.yml#166).
Kept out of ci_lint/cli.py, which only calls `register(sub)`.

    local-gate run          run the declared gate; stamp HEAD with a tree-bound trailer
    local-gate verify       CI side (GATE-003): fail fast on an unattested PR head
    local-gate check-push   git pre-push hook body (reads git's stdin)
    local-gate install-hook write .git/hooks/pre-push calling check-push
    local-gate lint         static GATE-001/002 against the workflows
    local-gate first-pass   live GET-only first-push pass rate (GATE-004)

Exit codes: 0 pass, 1 violation/failure, 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path

from ci_lint.hosted_attestations import job_decisions as _job_decisions, trust_input
from ci_lint.attestations import output_name as attestation_output_name
from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Status
from ci_lint.first_pass import DEFAULT_MIN_PRS, DEFAULT_TARGET, collect, render_text
from ci_lint.github_api import GitHubApiError, default_fetch, default_fetch_status
from ci_lint.merge_queue_trust import decide_merge_group, merge_group_event
from ci_lint.gate_bare_tools import check_no_bare_rust
from ci_lint.remote_only import check_gate_012
from ci_lint.rules.cpp_ctest import check_cpp_001
from ci_lint.rules.swatinem_ban import check_cache_025
from ci_lint.rules.tools import CI_SCRIPT_RE
from ci_lint.gate_trust import TrustDecision, TrustInput
from ci_lint.gate_trust import decide as decide_trust
from ci_lint.lane_cache import ToolVersions, lane_key, lookup, run_audit, simulate, tree_entries, tree_file
from ci_lint.workflow_replay_runtime import query_execution_pins
from ci_lint.gate_publish import publish
from ci_lint.local_gate import (
    GateConfig,
    GitError,
    _git,
    VerifyOutcome,
    WIP_PUSH_ENV,
    check_gate_static,
    check_push,
    default_launcher,
    install_hook,
    load_gate_config,
    load_gate_config_at,
    run_gate,
    verify,
)


def _config(repo: Path, command: str) -> GateConfig | None:
    loaded = load_gate_config(repo)
    config = loaded.config
    for finding in loaded.findings:
        print(finding.render(), file=sys.stderr)
    if config is None:
        print(
            f"ci-lint local-gate {command}: no local gate declared (ci.toml [local.gate] or local-gate.toml [gate])",
            file=sys.stderr,
        )
    return config


def _event_payload() -> dict[str, JsonValue]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).is_file():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = _config(repo, "run")
    if config is None:
        return 2
    outcome = run_gate(repo, config, stamp=not args.no_stamp, force=args.force, use_cache=not args.no_cache)
    print(outcome.message, file=sys.stderr if outcome.exit_code else sys.stdout)
    return outcome.exit_code


def _cmd_push(args: argparse.Namespace) -> int:
    outcome = publish(Path(args.repo).resolve(), sha=args.sha, remote=args.remote)
    print(outcome.message, file=sys.stderr if outcome.exit_code else sys.stdout)
    return outcome.exit_code


def _verify_policy(repo: Path, config: GateConfig, inp: TrustInput, *,
                   author: str | None, local_replay: bool) -> VerifyOutcome:
    if local_replay:
        return VerifyOutcome(0, False, "local-replay",
                             "[GATE-003] local workflow replay: run checks before creating attestation")
    if inp.event in ("pull_request", "pull_request_target"):
        if inp.base_sha is None or re.fullmatch(r"[0-9a-f]{40}", inp.base_sha) is None:
            return VerifyOutcome(2, False, "base-unavailable", "[GATE-003] a valid PR base commit is required")
        try:
            _git(repo, "cat-file", "-e", f"{inp.base_sha}^{{commit}}")
        except GitError:
            return VerifyOutcome(2, False, "base-unavailable",
                                 "[GATE-003] PR base commit is unavailable; fetch full history")
        base_policy = load_gate_config_at(repo, inp.base_sha)
        if base_policy.findings:
            return VerifyOutcome(2, False, "invalid-base-policy",
                                 "\n".join(finding.render() for finding in base_policy.findings))
        # Initial enrollment can have no base declaration. Once adopted, a
        # head cannot weaken mode or exemptions; the base owns both policies.
        config = base_policy.config or config
    return verify(repo, config, sha=inp.head_sha, event=inp.event, author=author)


def _cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    config = _config(repo, "verify")
    if config is None:
        return 2
    payload = _event_payload()
    pr = payload.get("pull_request")
    pr = pr if isinstance(pr, dict) else {}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    event = args.event or os.environ.get("GITHUB_EVENT_NAME") or ""
    sha = args.sha or (head.get("sha") if isinstance(head.get("sha"), str) else None) or os.environ.get("GITHUB_SHA")
    author = args.author or (user.get("login") if isinstance(user.get("login"), str) else None)
    if not sha:
        print("ci-lint local-gate verify: --sha is required outside GitHub Actions", file=sys.stderr)
        return 2
    # ACT is the fleet's local-runner signal. A replay creates proof only
    # after its checks finish; a previous stamp must never skip those checks.
    local_replay = os.environ.get("ACT", "").strip().lower() == "true"
    policy_input = _trust_input(args, payload, event, sha)
    outcome = _verify_policy(repo, config, policy_input, author=author, local_replay=local_replay)
    print(outcome.message)
    if outcome.exit_code and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=GATE-003 local gate not run::{outcome.message.splitlines()[0]}")
    lines = [f"attested={'true' if outcome.attested else 'false'}", f"state={outcome.state}"]
    if args.trust:
        outcome_trust = _trust_decision(repo, event, payload, policy_input, local_replay=local_replay)
        decision, trust_input = outcome_trust.decision, outcome_trust.input
        print(decision.render())
        lines += [
            f"trusted={'true' if decision.trusted else 'false'}",
            f"would_trust={'true' if decision.would_trust else 'false'}",
            f"trust_reason={decision.reason}",
        ]
        report = [decision.render()]
        for job in _job_decisions(repo, trust_input, decision.trusted):
            lines.append(f"{attestation_output_name(job.job)}={'true' if job.skip else 'false'}")
            report.append(f"[GATE-010] {job.job}: {'skip' if job.skip else 'run'} ({job.reason})")
        for line in report[1:]:
            print(line)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("\n\n".join(report) + "\n")
    if args.github_output:
        target = os.environ.get("GITHUB_OUTPUT")
        if target:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    return outcome.exit_code


@dataclass(frozen=True)
class _TrustOutcome:
    decision: TrustDecision
    input: TrustInput  # the head the per-job GATE-010 decisions judge


def _trust_decision(repo: Path, event: str, payload: dict[str, JsonValue], inp: TrustInput, *,
                    local_replay: bool) -> _TrustOutcome:
    if local_replay:
        return _TrustOutcome(TrustDecision(False, False, "local-replay", "local execution never reuses a commit attestation"), inp)
    if event == "merge_group":
        # #419: reuse only a single-PR, tree-identical, attested queue entry.
        group = decide_merge_group(
            repo, event, merge_group_event(payload, os.environ.get("GITHUB_REPOSITORY")),
            fetch=default_fetch_status, token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
        return _TrustOutcome(group.decision, group.pr_input or inp)
    return _TrustOutcome(decide_trust(repo, inp), inp)


def _trust_input(args: argparse.Namespace, payload: dict[str, JsonValue], event: str, sha: str) -> TrustInput:
    inp = trust_input(payload, event, sha)
    return replace(inp, base_sha=args.base_sha or inp.base_sha,
                   author_association=args.author_association or inp.author_association,
                   labels=tuple(args.label) if args.label else inp.labels)


def _cmd_lanes(args: argparse.Namespace) -> int:
    """GATE-007: show each lane's key, input count and cache state for HEAD."""

    repo = Path(args.repo).resolve()
    config = _config(repo, "lanes")
    if config is None:
        return 2
    if not config.lanes:
        print("no [gate.lanes] declared: the gate runs as one command and nothing is cached")
        return 0
    if args.audit:
        lane = next((ln for ln in config.lanes if ln.id == args.audit), None)
        if lane is None:
            print(f"no lane {args.audit!r}; declared: {', '.join(ln.id for ln in config.lanes)}", file=sys.stderr)
            return 2
        audit = run_audit(repo, lane, config.run, config.source)
        if audit is None:
            print("local-gate lanes --audit needs strace on PATH (Linux)", file=sys.stderr)
            return 2
        print(f"{audit.lane}: opened {audit.opened} tracked files; excluded but read: {len(audit.excluded_reads)}")
        for path in audit.excluded_reads:
            print(f"  UNSAFE EXCLUSION: {path}")
        print("note: reads by daemons or containers outside the traced process tree are not visible")
        return 1 if audit.excluded_reads else 0
    if args.simulate:
        for sim in simulate(repo, config.lanes, config.run, config.source, commits=args.simulate):
            print(f"{sim.lane:10} reusable {sim.reusable}/{sim.commits} ({sim.rate:.0%})  most often forced by: "
                  + (", ".join(sim.top_triggers) or "-"))
        return 0
    return _show_lane_cache(repo, config)


def _show_lane_cache(repo: Path, config: GateConfig) -> int:
    entries = tree_entries(repo, "HEAD")
    ci_toml = tree_file(repo, "HEAD", "ci.toml")  # #401: semantic pin hashing per lane
    versions = ToolVersions()
    try:
        pins = query_execution_pins(repo, config.replay)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"local-gate lanes: execution provider refused: {exc}", file=sys.stderr)
        return 2
    for lane in config.lanes:
        query = config.replay.provider_query if config.replay else ()
        key = lane_key(entries, lane, gate_run=(*config.run, *query), gate_source=config.source,
                       versions=versions, provider=pins, ci_toml=ci_toml)
        hit = lookup(repo, lane, key.key)
        state = f"HIT (passed {hit.age_hours(time.time()):.1f} h ago in {hit.secs}s)" if hit else "miss"
        tools = ", ".join(f"{t.tool}={t.version}" for t in key.tools) or "none declared"
        print(f"{lane.id:10} {state:34} key={key.key[:12]} inputs={key.inputs} tools: {tools}")
    return 0


def _cmd_check_push(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    push = check_push(repo, sys.stdin.read(), wip_reason=os.environ.get(WIP_PUSH_ENV))
    code, problems = push.exit_code, push.problems
    for head in push.wip:
        print(f"ci-lint local-gate: WIP push of an unattested head ({WIP_PUSH_ENV}="
              f"{os.environ[WIP_PUSH_ENV].strip()!r}): {head}", file=sys.stderr)
    if code:
        print("ci-lint local-gate: push refused -- these heads have not passed the local gate (GATE-003):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("  fix: check out the refused head and run ci-lint local-gate run, then push again", file=sys.stderr)
        print(f"  WIP/handoff branch that cannot pass yet: {WIP_PUSH_ENV}='<reason>' git push "
              "(admits unattested heads only; CI verify still fails the PR)", file=sys.stderr)
    return code


def _cmd_install_hook(args: argparse.Namespace) -> int:
    result = install_hook(Path(args.repo).resolve(), args.launcher or default_launcher(), force=args.force)
    print(result.message, file=sys.stderr if result.exit_code else sys.stdout)
    return result.exit_code


def _cmd_lint(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    loaded = load_gate_config(repo)
    config, findings = loaded.config, loaded.findings
    if config is None and not findings:
        print("ci-lint local-gate lint: no local gate declared (ci.toml [local.gate] or local-gate.toml [gate])", file=sys.stderr)
        return 1
    if config is not None:
        findings = findings + check_gate_static(config, repo) + check_no_bare_rust(repo, config.run) + check_gate_012(repo)
        findings = findings + check_cache_025(repo)
        gate_scripts = tuple(
            tok for argv in (config.run, *(lane.run for lane in config.lanes)) for tok in argv if CI_SCRIPT_RE.match(tok)
        )
        findings = findings + check_cpp_001(repo, extra_scripts=gate_scripts)
    for finding in findings:
        print(finding.render())
    violations = [f for f in findings if f.status == Status.VIOLATION]
    print(f"local-gate lint: {len(violations)} violation(s), {len(findings) - len(violations)} other finding(s)")
    return 1 if violations else 0


def _cmd_first_pass(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("ci-lint local-gate first-pass: GITHUB_TOKEN (or GH_TOKEN) is required", file=sys.stderr)
        return 2
    try:
        since = datetime.fromisoformat(args.since)
    except ValueError:
        print(f"ci-lint local-gate first-pass: --since {args.since!r} is not an ISO date", file=sys.stderr)
        return 2
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    try:
        report = collect(args.slug, args.workflow, since, default_fetch, token, target=args.target, min_prs=args.min_prs,
                         limit=args.limit)
    except GitHubApiError as exc:
        print(f"ci-lint local-gate first-pass: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_json_dict(), indent=2) if args.json else render_text(report))
    return 0


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("local-gate", help="local gate first: run, attest, verify, hook (GATE-001..004, ci.yml#166)")
    lg = p.add_subparsers(dest="local_gate_command", required=True)

    run = lg.add_parser("run", help="run the declared gate and stamp HEAD with a tree-bound Local-Gate trailer")
    run.add_argument("--repo", default=".")
    run.add_argument("--no-stamp", action="store_true", help="run only; do not amend HEAD")
    run.add_argument("--force", action="store_true", help="run even when HEAD is already attested")
    run.add_argument("--no-cache", action="store_true", help="GATE-007: run every lane, ignoring cached passes")
    run.set_defaults(func=_cmd_run)

    push = lg.add_parser("push", help="validate and publish an exact stamped head; updates its existing PR")
    push.add_argument("--repo", default=".")
    push.add_argument("--sha", required=True, help="full commit id returned by local-gate run")
    push.add_argument("--remote", default="origin", help="configured Git remote (default: origin)")
    push.set_defaults(func=_cmd_push)

    ver = lg.add_parser("verify", help="CI side (GATE-003): fail when the PR head is not attested")
    ver.add_argument("--repo", default=".")
    ver.add_argument("--sha", help="commit to verify (default: the PR head from GITHUB_EVENT_PATH)")
    ver.add_argument("--event", help="event name (default: GITHUB_EVENT_NAME)")
    ver.add_argument("--author", help="PR author login (default: from the event payload)")
    ver.add_argument("--github-output", action="store_true", help="append attested=/state= to $GITHUB_OUTPUT")
    ver.add_argument("--trust", action="store_true",
                     help="GATE-008: also decide whether the attested head may skip [gate.trust].skip jobs "
                     "(outputs trusted / would_trust / trust_reason)")
    ver.add_argument("--base-sha", help="PR base commit (default: from GITHUB_EVENT_PATH); its declaration is the policy")
    ver.add_argument("--author-association", help="PR author association (default: from GITHUB_EVENT_PATH)")
    ver.add_argument("--label", action="append", help="PR label (repeatable; default: from GITHUB_EVENT_PATH)")
    ver.set_defaults(func=_cmd_verify)

    hook = lg.add_parser("check-push", help="pre-push hook body: refuse unattested branch heads")
    hook.add_argument("--repo", default=".")
    hook.add_argument("hook_args", nargs="*", help="git's <remote> <url> (ignored)")
    hook.set_defaults(func=_cmd_check_push)

    inst = lg.add_parser("install-hook", help="install the pre-push hook")
    inst.add_argument("--repo", default=".")
    inst.add_argument("--launcher", help="command that runs ci-lint (default: this interpreter + this checkout)")
    inst.add_argument("--force", action="store_true", help="replace an existing non-ci-lint pre-push hook")
    inst.set_defaults(func=_cmd_install_hook)

    lanes = lg.add_parser("lanes", help="GATE-007: each lane's cache key and hit/miss for HEAD")
    lanes.add_argument("--repo", default=".")
    lanes.add_argument("--simulate", type=int, metavar="N",
                       help="replay the last N first-parent commits: how often would each lane's inputs be unchanged?")
    lanes.add_argument("--audit", metavar="LANE",
                       help="run LANE under strace and fail if it reads a file its exclusions drop (Linux)")
    lanes.set_defaults(func=_cmd_lanes)

    lint = lg.add_parser("lint", help="static GATE-001/002 for a repository with or without ci.toml")
    lint.add_argument("--repo", default=".")
    lint.set_defaults(func=_cmd_lint)

    fp = lg.add_parser("first-pass", help="live first-push pass rate of merged PRs (GATE-004)")
    fp.add_argument("--slug", required=True, help="owner/name")
    fp.add_argument("--workflow", default="ci.yml",
                    help="workflow file name (default: ci.yml); only PRs with a pull_request run of it are sampled")
    fp.add_argument("--limit", type=int, default=0,
                    help="keep only the newest N merged PRs (default 0: all, paginated); the total is always printed")
    fp.add_argument("--since", required=True, help="ISO date/time; PRs merged at or after it")
    fp.add_argument("--target", type=float, default=DEFAULT_TARGET)
    fp.add_argument("--min-prs", type=int, default=DEFAULT_MIN_PRS)
    fp.add_argument("--json", action="store_true")
    fp.set_defaults(func=_cmd_first_pass)
