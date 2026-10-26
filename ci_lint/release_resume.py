"""`ci-lint release resume-plan` / `ci-lint release verify-frozen`: REL-004's
*reuse* half (zackees/ci.yml#109, issue #8 point 5).

`release verify --resume` (ci_lint.release.verify_resume) already *refuses* a
resumed release whose staged bytes differ from the frozen
release-manifest.json. It does not make a resume *reuse* those bytes: on
`workflow_dispatch resume_from_run` every build/platform job still reran.
This module is the decision that lets a release workflow skip them:

  resume-plan  (GET-only, fail closed) -- may this run publish the prior
               run's `release-dist-verified` artifact instead of rebuilding?
               `resume=true` only when ALL of:
                 * the prior run belongs to --repo (not a fork head), is
                   `completed`, and its `head_sha` equals --sha;
                 * exactly one unexpired `release-dist-verified` artifact
                   exists on that run, carries a `sha256:` digest, and its
                   own `workflow_run.head_sha` equals --sha;
                 * every --required-job (exact name; the jobs that proved
                   the bytes: release-verify, platform-run, smoke...) is
                   `success` in that run's latest attempt;
                 * the downloaded artifact (--dist) holds a schema-1
                   release-manifest.json whose `candidate_sha` equals --sha,
                   every artifact it lists is present and hashes to its
                   frozen sha256, and no wheel/sdist outside the manifest is
                   present (tested == published).
               Anything else -- including any API error, 401/403/404, or a
               missing token -- is `resume=false` with a reason code; the
               workflow then rebuilds (and `release verify --resume` still
               refuses rebuilt bytes). Exit 0 for every decision, 2 only on
               a usage error, like `reuse-check`.

  verify-frozen (pure filesystem) -- the publish job's re-verification of
               the bytes it actually downloaded against the frozen manifest
               (REL-004 findings; exit 1 on any violation). This is the
               same local check resume-plan runs, repeated at the point of
               publication so the bytes uploaded are the bytes verified.

Fetches go through `FetchStatusFn`; `--record/--replay` reuse
`ci_lint.fleet_cli`'s plumbing, so the tests replay recorded fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status
from ci_lint.fleet_cli import RecordTable, _recording, _token, replay_fetch
from ci_lint.github_api import FetchStatusFn, GitHubApiError, default_fetch_status
from ci_lint.release import RELEASE_MANIFEST_SCHEMA_VERSION

API = "https://api.github.com"
DEFAULT_ARTIFACT = "release-dist-verified"
MANIFEST_NAME = "release-manifest.json"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_MAX_JOB_PAGES = 10

# Every reason code resume-plan can emit. Only "verified" means resume=true.
REASONS = (
    "verified",
    "not-a-resume",
    "no-token",
    "api-error",
    "run-not-found",
    "run-repo-mismatch",
    "run-not-completed",
    "run-sha-mismatch",
    "artifact-missing",
    "artifact-ambiguous",
    "artifact-expired",
    "artifact-no-digest",
    "artifact-sha-mismatch",
    "required-job-missing",
    "required-job-not-success",
    "dist-not-verified",
)


@dataclass(frozen=True)
class ResumeRequest:
    repo: str
    from_run: str  # "" = not a resume
    sha: str
    required_jobs: tuple[str, ...]
    dist: Path
    artifact_name: str = DEFAULT_ARTIFACT


@dataclass(frozen=True)
class ResumeDecision:
    resume: bool
    reason: str
    detail: str
    run_id: str = ""
    run_url: str = ""
    artifact_id: str = ""
    artifact_digest: str = ""
    findings: tuple[Finding, ...] = ()


# ── local half: the downloaded bytes against the frozen manifest ────────────


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel004(path: str, message: str, fix: str) -> Finding:
    return Finding(rule="REL-004", path=path, message=message, fix=fix)


_FIX_REDOWNLOAD = (
    "publish only the exact bytes the frozen run verified: re-download that run's "
    "release-dist-verified artifact unmodified, or drop resume_from_run and rebuild"
)


def verify_frozen_dist(dist: Path, sha: str) -> tuple[Finding, ...]:  # noqa: C901
    """REL-004 findings for a directory holding a downloaded
    `release-dist-verified` artifact: its release-manifest.json must be a
    schema-1 manifest for exactly `sha`, list >= 1 artifact, and every listed
    file must be present with its frozen sha256; any wheel/sdist present but
    not listed is an untested artifact. Empty tuple = the bytes are intact."""

    manifest_path = dist / MANIFEST_NAME
    if not dist.is_dir():
        return (_rel004(str(dist), f"{dist} is not a directory -- nothing was downloaded", _FIX_REDOWNLOAD),)
    try:
        raw: JsonValue = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return (_rel004(str(manifest_path), f"cannot read the frozen manifest: {exc}", _FIX_REDOWNLOAD),)
    if not isinstance(raw, dict) or raw.get("schema_version") != RELEASE_MANIFEST_SCHEMA_VERSION:
        return (
            _rel004(
                str(manifest_path),
                f"not a schema-{RELEASE_MANIFEST_SCHEMA_VERSION} release-manifest.json",
                "the frozen artifact must carry the release-manifest.json `ci-lint release verify` wrote",
            ),
        )
    findings: list[Finding] = []
    frozen_sha = raw.get("candidate_sha")
    if frozen_sha != sha:
        findings.append(
            _rel004(
                str(manifest_path),
                f"frozen manifest is for candidate {frozen_sha!r}, this release is for {sha!r}",
                "resume only from a run that verified this exact commit",
            )
        )
    entries = raw.get("artifacts")
    if not isinstance(entries, list) or not entries:
        findings.append(_rel004(str(manifest_path), "frozen manifest lists no artifacts", _FIX_REDOWNLOAD))
        return tuple(findings)
    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            findings.append(_rel004(str(manifest_path), f"malformed artifact entry {entry!r}", _FIX_REDOWNLOAD))
            continue
        name, frozen = entry.get("path"), entry.get("sha256")
        if not isinstance(name, str) or not isinstance(frozen, str) or "/" in name or "\\" in name or name in ("", ".", ".."):
            findings.append(_rel004(str(manifest_path), f"malformed artifact entry {entry!r}", _FIX_REDOWNLOAD))
            continue
        listed.add(name)
        file_path = dist / name
        if not file_path.is_file():
            findings.append(_rel004(name, f"'{name}' is frozen in the manifest but missing from {dist}", _FIX_REDOWNLOAD))
            continue
        actual = _sha256_file(file_path)
        if actual != frozen:
            findings.append(
                _rel004(
                    name,
                    f"'{name}' hashes to {actual[:16]}..., the frozen manifest says {frozen[:16]}... -- "
                    "these are not the bytes that were verified",
                    _FIX_REDOWNLOAD,
                )
            )
    for extra in sorted(dist.iterdir()):
        if extra.is_file() and (extra.suffix == ".whl" or extra.name.endswith(".tar.gz")) and extra.name not in listed:
            findings.append(
                _rel004(
                    extra.name,
                    f"'{extra.name}' is in {dist} but not in the frozen manifest -- it was never verified",
                    "publish only artifacts the frozen manifest lists; remove the extra file",
                )
            )
    return tuple(findings)


# ── remote half: the prior run and its artifact (GET-only) ─────────────────


def _no(reason: str, detail: str, **kw: str) -> ResumeDecision:
    return ResumeDecision(resume=False, reason=reason, detail=detail, **kw)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _Got:
    status: int
    body: JsonValue
    error: str = ""


def _get(fetch: FetchStatusFn, token: str, url: str) -> _Got:
    try:
        status, body = fetch(url, token)
    except GitHubApiError as exc:
        return _Got(0, None, str(exc))
    return _Got(int(status), body)  # type: ignore[arg-type]


def _str(obj: JsonValue, *keys: str) -> str:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(k)
    return cur if isinstance(cur, str) else ""


def _jobs(fetch: FetchStatusFn, token: str, base: str) -> list[dict[str, JsonValue]] | str:
    out: list[dict[str, JsonValue]] = []
    for page in range(1, _MAX_JOB_PAGES + 1):
        got = _get(fetch, token, f"{base}/jobs?per_page=100&filter=latest&page={page}")
        if got.status != 200 or not isinstance(got.body, dict) or not isinstance(got.body.get("jobs"), list):
            return f"GET jobs page {page}: {got.error or got.status}"
        jobs = got.body["jobs"]
        out.extend(j for j in jobs if isinstance(j, dict))  # type: ignore[union-attr]
        total = got.body.get("total_count")
        if not isinstance(total, int) or len(out) >= total or not jobs:
            return out
    return f"more than {_MAX_JOB_PAGES * 100} jobs"


def decide(req: ResumeRequest, *, fetch: FetchStatusFn | None, token: str | None) -> ResumeDecision:  # noqa: C901
    if not req.from_run:
        return _no("not-a-resume", "no --from-run given: build everything")
    if fetch is None or not token:
        return _no("no-token", "GITHUB_TOKEN/GH_TOKEN is not set; cannot verify the prior run")
    base = f"{API}/repos/{req.repo}/actions/runs/{req.from_run}"

    got = _get(fetch, token, base)
    if got.status == 404:
        return _no("run-not-found", f"run {req.from_run} does not exist in {req.repo}")
    if got.status != 200 or not isinstance(got.body, dict):
        return _no("api-error", f"GET run {req.from_run}: {got.error or got.status}")
    run = got.body
    ids = {"run_id": req.from_run, "run_url": _str(run, "html_url")}
    if _str(run, "repository", "full_name") != req.repo or _str(run, "head_repository", "full_name") != req.repo:
        return _no("run-repo-mismatch", f"run {req.from_run} is not a {req.repo} run on a {req.repo} head", **ids)
    if run.get("status") != "completed":
        return _no("run-not-completed", f"run {req.from_run} is {run.get('status')!r}, not completed", **ids)
    if _str(run, "head_sha") != req.sha:
        return _no("run-sha-mismatch", f"run {req.from_run} built {_str(run, 'head_sha')!r}, not {req.sha}", **ids)

    got = _get(fetch, token, f"{base}/artifacts?per_page=100&name={req.artifact_name}")
    if got.status != 200 or not isinstance(got.body, dict) or not isinstance(got.body.get("artifacts"), list):
        return _no("api-error", f"GET artifacts of run {req.from_run}: {got.error or got.status}", **ids)
    arts = [a for a in got.body["artifacts"] if isinstance(a, dict) and a.get("name") == req.artifact_name]  # type: ignore[union-attr]
    if not arts:
        return _no(
            "artifact-missing",
            f"run {req.from_run} has no {req.artifact_name!r} artifact (never uploaded, or deleted after retention)",
            **ids,
        )
    if len(arts) > 1:
        return _no("artifact-ambiguous", f"run {req.from_run} has {len(arts)} {req.artifact_name!r} artifacts", **ids)
    art = arts[0]
    ids["artifact_id"] = str(art.get("id") or "")
    if art.get("expired") is not False:
        return _no("artifact-expired", f"artifact {ids['artifact_id']} has expired", **ids)
    digest = _str(art, "digest")
    if not digest.startswith("sha256:"):
        return _no("artifact-no-digest", f"artifact {ids['artifact_id']} carries no sha256 digest", **ids)
    ids["artifact_digest"] = digest
    if _str(art, "workflow_run", "head_sha") != req.sha:
        return _no("artifact-sha-mismatch", f"artifact {ids['artifact_id']} was uploaded for another commit", **ids)

    jobs = _jobs(fetch, token, base)
    if isinstance(jobs, str):
        return _no("api-error", jobs, **ids)
    by_name: dict[str, list[str]] = {}
    for job in jobs:
        by_name.setdefault(_str(job, "name"), []).append(_str(job, "conclusion"))
    for name in req.required_jobs:
        conclusions = by_name.get(name)
        if not conclusions:
            return _no("required-job-missing", f"run {req.from_run} has no job named {name!r}", **ids)
        if any(c != "success" for c in conclusions):
            return _no("required-job-not-success", f"job {name!r} concluded {conclusions}", **ids)

    findings = verify_frozen_dist(req.dist, req.sha)
    if findings:
        return ResumeDecision(
            resume=False,
            reason="dist-not-verified",
            detail=f"{len(findings)} REL-004 finding(s) on the downloaded bytes in {req.dist}",
            findings=findings,
            **ids,
        )
    return ResumeDecision(
        resume=True,
        reason="verified",
        detail=f"publish artifact {ids['artifact_id']} from run {req.from_run}; skip build jobs",
        **ids,
    )


def github_output_lines(d: ResumeDecision) -> list[str]:
    return [
        f"resume={'true' if d.resume else 'false'}",
        f"reason={d.reason}",
        f"run_id={d.run_id}",
        f"artifact_id={d.artifact_id}",
        f"artifact_digest={d.artifact_digest}",
    ]


def _findings_json(findings: tuple[Finding, ...]) -> list[JsonValue]:
    return [
        {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
        for f in findings
    ]


def to_json_dict(d: ResumeDecision) -> dict[str, JsonValue]:
    return {
        "schema": 1,
        "resume": d.resume,
        "reason": d.reason,
        "detail": d.detail,
        "run_id": d.run_id,
        "run_url": d.run_url,
        "artifact_id": d.artifact_id,
        "artifact_digest": d.artifact_digest,
        "findings": _findings_json(d.findings),
    }


def render_text(d: ResumeDecision) -> str:
    lines = [f"ci-lint release resume-plan: resume={'true' if d.resume else 'false'} reason={d.reason}", f"  {d.detail}"]
    lines.extend(f.render() for f in d.findings)
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _usage(command: str, message: str) -> int:
    print(f"ci-lint release {command}: {message}", file=sys.stderr)
    return 2


@dataclass(frozen=True)
class _Fetcher:
    fetch: FetchStatusFn | None = None
    token: str | None = None
    table: RecordTable | None = None


def _fetcher(args: argparse.Namespace) -> _Fetcher | str:
    if args.replay:
        try:
            table = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"cannot read --replay {args.replay!r}: {exc}"
        if not isinstance(table, dict):
            return "--replay must be a `--record` JSON object"
        return _Fetcher(replay_fetch(table), "replay")
    token = _token()
    if not token:
        return _Fetcher()
    if args.record:
        recorded: RecordTable = {}
        return _Fetcher(_recording(default_fetch_status, recorded), token, recorded)
    return _Fetcher(default_fetch_status, token)


def _cmd_resume_plan(args: argparse.Namespace) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or ""
    if not REPO_RE.match(repo):
        return _usage("resume-plan", f"--repo must be owner/name (got {repo!r})")
    sha = (args.sha or os.environ.get("GITHUB_SHA") or "").lower()
    if not SHA_RE.match(sha):
        return _usage("resume-plan", f"--sha must be a 40-hex commit SHA (got {sha!r})")
    from_run = (args.from_run or "").strip()
    if from_run and not RUN_ID_RE.match(from_run):
        return _usage("resume-plan", f"--from-run must be a numeric run id or empty (got {from_run!r})")
    required = tuple(dict.fromkeys(args.required_job or ()))
    if not required:
        return _usage("resume-plan", "at least one --required-job <exact job name> is required")
    if args.record and args.replay:
        return _usage("resume-plan", "--record and --replay are mutually exclusive")
    fetcher = _fetcher(args)
    if isinstance(fetcher, str):
        return _usage("resume-plan", fetcher)
    req = ResumeRequest(
        repo=repo, from_run=from_run, sha=sha, required_jobs=required, dist=Path(args.dist), artifact_name=args.artifact_name
    )
    decision = decide(req, fetch=fetcher.fetch, token=fetcher.token)
    if fetcher.table is not None:
        Path(args.record).write_text(json.dumps(fetcher.table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_output:
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if not gh_out:
            print("ci-lint release resume-plan: --github-output given but GITHUB_OUTPUT is not set", file=sys.stderr)
            return 2
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(github_output_lines(decision)) + "\n")
    print(json.dumps(to_json_dict(decision), indent=2) if args.json else render_text(decision))
    return 0


def _cmd_verify_frozen(args: argparse.Namespace) -> int:
    sha = (args.sha or "").lower()
    if not SHA_RE.match(sha):
        return _usage("verify-frozen", f"--sha must be a 40-hex commit SHA (got {sha!r})")
    findings = verify_frozen_dist(Path(args.dist), sha)
    if args.json:
        print(json.dumps({"sha": sha, "findings": _findings_json(findings)}, indent=2))
    else:
        for f in findings:
            print(f.render())
        print(f"ci-lint release verify-frozen: {len(findings)} violation(s)")
    return 1 if any(f.status == Status.VIOLATION for f in findings) else 0


def register(release_sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = release_sub.add_parser(
        "resume-plan",
        help="REL-004 reuse: may a resumed release publish the prior run's frozen bytes? (GET-only, fail closed)",
    )
    p.add_argument("--repo", default=None, help="owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--from-run", default="", help="the prior run id (empty: not a resume, resume=false)")
    p.add_argument("--sha", default=None, help="this release's candidate SHA (default: $GITHUB_SHA)")
    p.add_argument(
        "--required-job",
        action="append",
        help="exact name of a job in the prior run that must be success (repeatable, >= 1)",
    )
    p.add_argument("--dist", required=True, help="directory the prior run's artifact was downloaded into")
    p.add_argument("--artifact-name", default=DEFAULT_ARTIFACT)
    p.add_argument("--github-output", action="store_true", help="append resume, reason, run_id, artifact_id, artifact_digest")
    p.add_argument("--json", action="store_true")
    p.add_argument("--record", default=None, help="write every GET response to this file")
    p.add_argument("--replay", default=None, help="answer GETs from a --record file instead of the network")
    p.set_defaults(func=_cmd_resume_plan)

    v = release_sub.add_parser(
        "verify-frozen",
        help="REL-004: re-verify downloaded release bytes against their frozen release-manifest.json",
    )
    v.add_argument("--dist", required=True)
    v.add_argument("--sha", required=True)
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=_cmd_verify_frozen)
