"""REL-006 static clauses: macOS signing secrets never reach PR jobs (#462).

docs/policy-general.md "macOS app signing and notarization" decides that
Developer ID signing and notarization run in release jobs only -- tag
pushes, `release` events and release `workflow_dispatch` runs -- and never
on `pull_request`, `pull_request_target` or `merge_group`. This module
enforces the part of REL-006 a workflow file can show:

- A *signing job* is a job that references one of the documented signing
  secrets (`secrets.MACOS_SIGNING_*`, `secrets.MACOS_NOTARY_*`, or the
  accepted fallback `MACOS_NOTARIZATION_USERNAME/PASSWORD`), in its own
  body or through a workflow-level `env:`, or that uses
  `actions/app-signing-macos` (local `./actions/...` or
  `zackees/ci.yml/actions/app-signing-macos@...`).
- In a workflow triggered by `pull_request_target`, a signing job is a
  violation whatever its `if:` says: that event runs with secrets in the
  base repository's context for fork PRs, so the secrets must not be
  reachable from such a workflow at all.
- In a workflow triggered by `pull_request`/`merge_group`, a signing job
  with no `if:` is a violation; one whose `if:` confines it to tag pushes,
  `release` or `workflow_dispatch` passes; any other `if:` (including one
  that merely excludes PR events) is `needs_review`.
- A local reusable workflow called from such a workflow is scanned with the
  caller job's confinement, as GATE-012 does.
- A signing job anywhere with no `environment:` is `needs_review`: the
  policy keeps the secrets in a GitHub environment restricted to the default
  branch and `v*` tags, but also allows repository-level secrets, so the
  absence alone cannot be proven wrong offline.

Not enforced statically (Candidate): the signing procedure itself (inside-out
`codesign`, keychain cleanup, notarize/staple/spctl) when a repository does
not use the reference action, and "an unsigned macOS artifact is published".
"""

from __future__ import annotations

from ci_lint import repo_identity

import re
from enum import Enum
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.workflow_scan import ParsedYamlFile, as_dict, get_on_section, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "REL-006"
POLICY = "docs/policy-general.md#macos-app-signing-and-notarization-candidate-rel-006"

SIGNING_SECRETS: tuple[str, ...] = (
    "MACOS_SIGNING_IDENTITY",
    "MACOS_SIGNING_CERT",
    "MACOS_SIGNING_CERT_PASSWORD",
    "MACOS_NOTARY_API_KEY",
    "MACOS_NOTARY_API_KEY_ID",
    "MACOS_NOTARY_API_ISSUER_ID",
    # The accepted Apple ID + app-specific password fallback.
    "MACOS_NOTARIZATION_USERNAME",
    "MACOS_NOTARIZATION_PASSWORD",
)
_SECRET_REF = re.compile(r"secrets\s*\.\s*(" + "|".join(SIGNING_SECRETS) + r")\b|secrets\[\s*['\"](" + "|".join(SIGNING_SECRETS) + r")['\"]\s*\]")
_ACTION = re.compile(rf"^(\./|{repo_identity.CI_YML_RE}/)actions/app-signing-macos(@|/?$)", re.IGNORECASE)

PR_EVENTS: frozenset[str] = frozenset({"pull_request", "pull_request_target", "merge_group"})

# Conditions that keep a job to tag pushes, release events or dispatch runs.
_RELEASE_ONLY = (
    re.compile(r"github\.event_name\s*==\s*'(release|workflow_dispatch)'"),
    re.compile(r"startswith\(\s*github\.ref\s*,\s*'refs/tags/"),
    re.compile(r"github\.ref\s*==\s*'refs/tags/"),
    re.compile(r"github\.ref_type\s*==\s*'tag'"),
    re.compile(r"^false$"),
)


class Gate(str, Enum):
    NONE = "none"
    RELEASE_ONLY = "release-only"
    UNKNOWN = "unknown"


def classify_if(expr: YamlValue) -> Gate:
    if expr is None:
        return Gate.NONE
    if expr is False:
        return Gate.RELEASE_ONLY
    if not isinstance(expr, str):
        return Gate.UNKNOWN
    text = expr.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    text = re.sub(r"\s+", " ", text.replace('"', "'")).strip().lower()
    parts = [p.strip(" ()") for p in text.split("||")] if "||" in text else [text]
    if all(any(rx.search(part) for rx in _RELEASE_ONLY) for part in parts):
        return Gate.RELEASE_ONLY
    return Gate.UNKNOWN


def _combine(outer: Gate, inner: Gate) -> Gate:
    if Gate.RELEASE_ONLY in (outer, inner):
        return Gate.RELEASE_ONLY
    if Gate.UNKNOWN in (outer, inner):
        return Gate.UNKNOWN
    return Gate.NONE


def _strings(value: YamlValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _secrets_in(value: YamlValue) -> list[str]:
    names: list[str] = []
    for text in _strings(value):
        for match in _SECRET_REF.finditer(text):
            name = match.group(1) or match.group(2)
            if name not in names:
                names.append(name)
    return names


def _uses_action(job: dict[str, YamlValue]) -> bool:
    return any(isinstance(s.get("uses"), str) and _ACTION.match(str(s.get("uses"))) for s in steps_of(job))


def _what(secrets: list[str], action: bool) -> str:
    parts = []
    if action:
        parts.append("uses `actions/app-signing-macos`")
    if secrets:
        parts.append("references " + ", ".join(f"`secrets.{n}`" for n in secrets))
    return " and ".join(parts)


def _events(document: YamlValue) -> set[str]:
    return {str(e) for e in get_on_section(as_dict(document))}


def _local_reusable(uses: YamlValue) -> str | None:
    if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
        return uses[2:].split("@", 1)[0]
    return None


def _job_findings(wf: ParsedYamlFile, events: set[str], context: Gate) -> list[Finding]:
    doc = as_dict(wf.document)
    wf_secrets = _secrets_in(doc.get("env"))
    findings: list[Finding] = []
    for job_id, job in jobs_of(doc).items():
        secrets = [*wf_secrets, *(n for n in _secrets_in(job) if n not in wf_secrets)]
        action = _uses_action(job)
        if not secrets and not action:
            continue
        where = f"job '{job_id}'"
        what = _what(secrets, action)
        if "pull_request_target" in events:
            findings.append(Finding(
                rule=RULE, path=wf.path,
                message=f"{where} {what} in a workflow triggered by `pull_request_target`, which runs with "
                "the base repository's secrets for fork PRs",
                fix="move the signing job to a workflow triggered only by tag pushes, `release` or "
                f"`workflow_dispatch`; never expose signing secrets to `pull_request_target` ({POLICY})",
            ))
        elif events & PR_EVENTS:
            gate = _combine(context, classify_if(job.get("if")))
            pr = ", ".join(f"`{e}`" for e in sorted(events & PR_EVENTS))
            if gate == Gate.NONE:
                findings.append(Finding(
                    rule=RULE, path=wf.path,
                    message=f"{where} {what} and runs on every {pr} event; signing secrets must never reach a PR job",
                    fix="add `if: startsWith(github.ref, 'refs/tags/v')` (or `github.event_name == 'release'`/"
                    f"`'workflow_dispatch'`), or move the job to a release-only workflow ({POLICY})",
                ))
            elif gate == Gate.UNKNOWN:
                findings.append(Finding(
                    rule=RULE, path=wf.path, status=Status.NEEDS_REVIEW,
                    message=f"{where} {what} in a {pr} workflow; its `if:` is not recognized as confining it "
                    "to tag pushes, `release` or `workflow_dispatch`",
                    fix="confine it with `if: startsWith(github.ref, 'refs/tags/v')` or "
                    f"`github.event_name == 'release'`/`'workflow_dispatch'` ({POLICY})",
                ))
        if "environment" not in job and "uses" not in job:
            findings.append(Finding(
                rule=RULE, path=wf.path, status=Status.NEEDS_REVIEW,
                message=f"{where} {what} but declares no `environment:`; the policy keeps signing secrets in a "
                "GitHub environment restricted to the default branch and `v*` tags",
                fix="store the signing secrets in an environment such as `release` (deployment branches: default "
                f"branch and `v*` tags) and add `environment: release` to this job ({POLICY})",
            ))
    return findings


def check_rel_006(repo_root: Path) -> list[Finding]:
    workflows = [wf for wf in load_workflows(repo_root) if wf.status == LoadStatus.OK]
    by_path = {wf.path: wf for wf in workflows}
    findings: list[Finding] = []
    scanned_as_callee: set[str] = set()
    for wf in workflows:
        events = _events(wf.document)
        if not events & PR_EVENTS:
            continue
        for job in jobs_of(as_dict(wf.document)).values():
            callee = _local_reusable(job.get("uses"))
            target = by_path.get(callee) if callee else None
            if target is None or callee in scanned_as_callee or _events(target.document) & PR_EVENTS:
                continue
            gate = classify_if(job.get("if"))
            if gate == Gate.RELEASE_ONLY and "pull_request_target" not in events:
                continue
            scanned_as_callee.add(target.path)
            findings.extend(_job_findings(target, events & PR_EVENTS, gate))
    for wf in workflows:
        events = _events(wf.document)
        if wf.path in scanned_as_callee:
            continue
        findings.extend(_job_findings(wf, events, Gate.NONE))
    return findings
