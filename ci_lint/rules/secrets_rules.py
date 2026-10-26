"""Group 5: secrets and permissions.

SEC-001 is a raw-text regex scan (secrets can appear inside `env:`, `with:`,
`if:` or `run:` values alike, so matching text directly is both simpler and
more robust than depending on a full YAML parse). SEC-002 needs the parsed
job/permissions structure, so it follows the same PyYAML/yq/needs_review
fallback as the other structural rules.

Round-4B refinement: top-level workflow `permissions:` may be at most
`contents: read` + `actions: read` (evidence: zackees/template-python-rust-
cmd#19 had to add two [[exceptions]] just to grant `actions: read`, which
this refinement makes free instead of exception-worthy). Any JOB may also
hold `contents: read`/`actions: read` for free; any wider per-job grant
(`actions: write`, `id-token: write`, `contents: write`, ...) is SEC-002
unless the job's id is listed for that exact grant in `ci.toml`'s new
`[allow].permissions` table. `id-token: write` additionally must sit on an
OIDC publisher -- the `publish` job in `[publish].pypi.environment`, or a
crates.io trusted-publishing job running rust-lang/crates-io-auth-action
under an `environment:` (ci.yml#134) -- being allowlisted in
`[allow].permissions` does not waive that second constraint.
"""

from __future__ import annotations

from dataclasses import dataclass

import re
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import as_dict, jobs_of, load_composite_actions, load_workflows
from ci_lint.yaml_io import LoadStatus, YamlValue

SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
INHERIT_RE = re.compile(r"^\s*secrets:\s*inherit\s*$")

# The only grants a workflow's TOP-LEVEL `permissions:` may hold for free.
@dataclass(frozen=True)
class Grant:
    scope: str
    level: str


TOP_LEVEL_FREE_GRANTS: frozenset[Grant] = frozenset(
    {Grant("contents", "read"), Grant("actions", "read")}
)
# The only grants any JOB's `permissions:` may hold for free -- anything
# else must be listed for that job id in [allow].permissions.
JOB_FREE_GRANTS: frozenset[Grant] = frozenset(
    {Grant("contents", "read"), Grant("actions", "read")}
)


def _discover_raw_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    wf_dir = repo_root / ".github" / "workflows"
    if wf_dir.is_dir():
        out.extend(sorted(p for p in wf_dir.iterdir() if p.suffix in (".yml", ".yaml")))
    actions_dir = repo_root / ".github" / "actions"
    if actions_dir.is_dir():
        out.extend(sorted(actions_dir.rglob("action.yml")))
        out.extend(sorted(actions_dir.rglob("action.yaml")))
    for name in ("action.yml", "action.yaml"):
        p = repo_root / name
        if p.is_file():
            out.append(p)
    return out


def check_sec_001(repo_root: Path, allowed: tuple[str, ...] = ()) -> list[Finding]:
    """`allowed` is ci.toml's [allow].secrets (ci.yml#135): each named secret
    is an accepted reference. Every other non-GITHUB_TOKEN secret, and every
    `secrets: inherit`, stays a violation. The live SEC-005 audit still
    reports any stored secret against the OIDC-only policy."""

    findings: list[Finding] = []
    for path in _discover_raw_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in SECRET_REF_RE.finditer(line):
                name = m.group(1)
                if name != "GITHUB_TOKEN" and name not in allowed:
                    findings.append(
                        Finding(
                            rule="SEC-001",
                            path=rel,
                            line=lineno,
                            message=f"references secrets.{name}",
                            fix=f"remove the dependency on secrets.{name}; this profile is OIDC-only "
                            "(no repository secrets besides the default secrets.GITHUB_TOKEN)",
                        )
                    )
            if INHERIT_RE.match(line):
                findings.append(
                    Finding(
                        rule="SEC-001",
                        path=rel,
                        line=lineno,
                        message="'secrets: inherit' passes every caller secret into a reusable "
                        "workflow call",
                        fix="list only the specific secrets actually needed (none, in this profile) "
                        "instead of 'secrets: inherit'",
                    )
                )
    return findings


def _top_level_permission_findings(perms: YamlValue, path: str) -> list[Finding]:
    loc = "permissions"
    if isinstance(perms, str):
        if perms == "none":
            return []
        return [
            Finding(
                rule="SEC-002",
                path=path,
                message=f"{loc} = '{perms}' is wider than the top-level max grant "
                "(contents: read, actions: read)",
                fix=f"replace {loc} with an explicit table: 'permissions: {{ contents: read }}' "
                "(add 'actions: read' too only if this workflow actually needs it)",
            )
        ]
    if not isinstance(perms, dict):
        return []
    findings: list[Finding] = []
    for key, val in perms.items():
        if val in (None, "none"):
            continue
        if Grant(str(key), str(val)) in TOP_LEVEL_FREE_GRANTS:
            continue
        findings.append(
            Finding(
                rule="SEC-002",
                path=path,
                message=f"{loc}.{key} = {val!r} exceeds the top-level max grant "
                "(contents: read, actions: read)",
                fix=f"remove '{key}' from the top-level {loc} block in {path}; if a specific job "
                f"needs '{key}: {val}', grant it on that job's own permissions block and list the "
                f'job id in ci.toml\'s [allow].permissions."{key}: {val}"',
            )
        )
    return findings


CRATES_IO_AUTH_ACTION = "rust-lang/crates-io-auth-action"


def _pypi_environment(ci: CiToml) -> str:
    pypi = ci.publish.pypi
    return pypi.environment if pypi is not None and pypi.environment else "pypi"


def _uses_action(job: dict[str, YamlValue], action: str) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    for step in steps:
        uses = step.get("uses") if isinstance(step, dict) else None
        if isinstance(uses, str) and uses.split("@", 1)[0] == action:
            return True
    return False


def _is_oidc_publisher(
    ci: CiToml, job_id: str, job: dict[str, YamlValue], env_name: YamlValue
) -> bool:
    """ci.yml#134: the PyPI publisher is the 'publish' job in
    [publish].pypi.environment; a crates.io trusted publisher is any job
    that exchanges its OIDC token through rust-lang/crates-io-auth-action
    inside a deployment environment (crates.io binds the registration to
    the workflow/job/environment, so its names are not ours to choose).
    Either way the job must still be listed in [allow].permissions."""

    if job_id == "publish" and env_name == _pypi_environment(ci):
        return True
    return isinstance(env_name, str) and bool(env_name) and _uses_action(job, CRATES_IO_AUTH_ACTION)


def _job_permission_findings(
    ci: CiToml, perms: YamlValue, path: str, job_id: str, job: dict[str, YamlValue]
) -> list[Finding]:
    loc = f"jobs.{job_id}.permissions"
    if isinstance(perms, str):
        if perms == "none":
            return []
        return [
            Finding(
                rule="SEC-002",
                path=path,
                message=f"{loc} = '{perms}' must be an explicit table so each grant can be checked "
                "against ci.toml's [allow].permissions",
                fix=f"replace {loc} with an explicit table naming only the grants jobs.{job_id} "
                "actually needs",
            )
        ]
    if not isinstance(perms, dict):
        return []

    environment = job.get("environment")
    env_name = (
        environment
        if isinstance(environment, str)
        else (environment.get("name") if isinstance(environment, dict) else None)
    )

    findings: list[Finding] = []
    for key, val in perms.items():
        if val in (None, "none"):
            continue
        if Grant(str(key), str(val)) in JOB_FREE_GRANTS:
            continue
        grant = f"{key}: {val}"
        allowed_jobs = ci.allow.permissions.get(grant) or ()
        if job_id not in allowed_jobs:
            findings.append(
                Finding(
                    rule="SEC-002",
                    path=path,
                    message=f"{loc}.{key} = {val!r} ('{grant}') is not granted to job '{job_id}' in "
                    "ci.toml's [allow].permissions",
                    fix=f'add "{job_id}" to ci.toml\'s [allow].permissions."{grant}" array, or narrow '
                    f"{loc}.{key}",
                )
            )
        if key == "id-token" and val == "write" and not _is_oidc_publisher(ci, job_id, job, env_name):
            pypi_env = _pypi_environment(ci)
            findings.append(
                Finding(
                    rule="SEC-002",
                    path=path,
                    message=f"{loc}.id-token = 'write' is only allowed on an OIDC publisher: the 'publish' "
                    f"job with 'environment: {pypi_env}' ([publish].pypi.environment), or a job that "
                    f"exchanges the token via {CRATES_IO_AUTH_ACTION} inside a deployment environment "
                    f"(job '{job_id}' has environment={env_name!r})",
                    fix="remove 'id-token: write' here; mint OIDC tokens only in the publishing job -- "
                    f"'publish' with 'environment: {pypi_env}', or a crates.io trusted-publishing job "
                    f"that runs {CRATES_IO_AUTH_ACTION} under an 'environment:'",
                )
            )
    return findings


def check_sec_002(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status == LoadStatus.NEEDS_REVIEW:
            findings.append(
                Finding(
                    rule="SEC-002",
                    path=wf.path,
                    status=Status.NEEDS_REVIEW,
                    message=f"cannot evaluate SEC-002 for {wf.path}: {wf.reason}",
                    fix="make PyYAML importable or `yq` available on PATH so ci-lint can parse "
                    f"{wf.path}, then re-run precheck",
                )
            )
            continue
        # Reusable workflows (`on: workflow_call` files) are checked exactly
        # the same way, using their own job ids -- there is nothing
        # workflow_call-specific here because `jobs_of(doc)` already reads
        # whatever `jobs:` that one file declares.
        doc = as_dict(wf.document)
        top_perms = doc.get("permissions")
        if top_perms is None:
            findings.append(
                Finding(
                    rule="SEC-002",
                    path=wf.path,
                    message="no top-level 'permissions' block (the default can be wider than "
                    "'contents: read')",
                    fix="add a top-level 'permissions: { contents: read }' to " + wf.path,
                )
            )
        else:
            findings.extend(_top_level_permission_findings(top_perms, wf.path))

        for job_id, job in jobs_of(doc).items():
            job_perms = job.get("permissions")
            if job_perms is None:
                continue
            findings.extend(_job_permission_findings(ci, job_perms, wf.path, job_id, job))
    return findings


def check_group5(ci: CiToml, repo_root: Path) -> list[Finding]:
    return check_sec_001(repo_root, ci.allow.secrets) + check_sec_002(ci, repo_root)
