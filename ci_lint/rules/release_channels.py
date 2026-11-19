"""REL-009: an auto-release never depends on an external package manager.

Owner directive 2026-11-19, from talos-hq/clud 2.8.67 and 2.8.68: a Homebrew
tap job inside the release workflow failed (the pushing GitHub App was not
installed on the tap repo) and turned both release runs red after the GitHub
Release, PyPI and the installer pages were already published and accepted.

A package-manager or downstream distribution channel (a Homebrew tap, a
Scoop bucket, a winget or Chocolatey manifest, an apt PPA, the AUR, nixpkgs,
Snap/Flathub) is never a job, a `needs:` or a status gate of the release
workflow. It runs in its own workflow, started by the published stable
release (or by the release workflow's completion when the release is
promoted with GITHUB_TOKEN) plus `workflow_dispatch`, so a channel failure
never turns a completed release red and is retried alone.

The static check scans every release workflow plus the local reusable
workflows it calls, and flags each reachable job that references a known
external channel. A release workflow is one a tag push starts, or one that
publishes the release's primary artifacts (a GitHub Release, PyPI, crates.io
or npm). A channel workflow started by `release`/`workflow_run` that publishes
no primary artifact is the compliant shape and is out of scope.

The rule is unwaivable for external channels. A package manager the
organization operates itself may be admitted later only by a policy amendment
that names it; it is not on the denylist below.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.release_recompute import reachable_from_release
from ci_lint.workflow_scan import ParsedYamlFile, as_dict, get_on_section, jobs_of, load_workflows
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "REL-009"
POLICY = "docs/policy-general.md#package-channels-never-gate-a-release-rel-009"


@dataclass(frozen=True)
class Channel:
    name: str
    pattern: re.Pattern[str]


def _c(name: str, pattern: str) -> Channel:
    return Channel(name, re.compile(pattern, re.IGNORECASE))


# Distribution channels only. Runner tooling (`brew install fish`,
# `apt-get install`, `choco install` of a build tool) publishes nothing and is
# not matched: a pattern names a tap/bucket/manifest or a publish verb.
EXTERNAL_CHANNELS: tuple[Channel, ...] = (
    _c("Homebrew", r"homebrew-tap|homebrew-core|\bbrew\s+(?:bump-formula-pr|bump-cask-pr|bump|tap-new|test-bot)\b"
       r"|bump-homebrew-formula|action-homebrew-bump"),
    _c("Scoop", r"scoop-bucket|\bscoop\s+(?:bucket|checkver)\b"),
    _c("winget", r"winget-pkgs|wingetcreate|winget-releaser"),
    _c("Chocolatey", r"\bchoco\s+push\b|push\.chocolatey\.org"),
    _c("apt PPA", r"\bdput\b|launchpad\.net|\bppa:[\w-]+/"),
    _c("AUR", r"aur\.archlinux\.org|deploy-aur"),
    _c("nixpkgs", r"NixOS/nixpkgs/pull|nixpkgs-review|nixpkgs-update"),
    _c("Snap", r"snapcraft\s+(?:upload|push|release)\b|snapcore/action-publish"),
    _c("Flathub", r"flathub"),
)

# Primary-artifact publication: a workflow doing one of these is the release.
_PRIMARY = re.compile(
    r"pypa/gh-action-pypi-publish|\btwine\s+upload\b|\bmaturin\s+(?:publish|upload)\b"
    r"|softprops/action-gh-release|\bgh\s+release\s+(?:create|upload)\b"
    r"|\bcargo\s+publish\b|\bnpm\s+publish\b"
)


def _strings(node: YamlValue) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def channels_in(text: str) -> list[str]:
    return [c.name for c in EXTERNAL_CHANNELS if c.pattern.search(text)]


def is_release_workflow(document: YamlValue) -> bool:
    doc = as_dict(document)
    on = get_on_section(doc)
    if "push" in on and "tags" in as_dict(on.get("push")):
        return True
    return any(_PRIMARY.search(text) for text in _strings(doc.get("jobs")))


def _local_callee(uses: YamlValue) -> str | None:
    if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
        return uses[2:].split("@", 1)[0]
    return None


def _release_scope(workflows: list[ParsedYamlFile]) -> list[ParsedYamlFile]:
    by_path = {wf.path: wf for wf in workflows}
    pending = [wf for wf in workflows if is_release_workflow(wf.document)]
    seen: dict[str, ParsedYamlFile] = {}
    while pending:
        wf = pending.pop()
        if wf.path in seen:
            continue
        seen[wf.path] = wf
        for job in jobs_of(as_dict(wf.document)).values():
            callee = _local_callee(job.get("uses"))
            if callee in by_path and reachable_from_release(job.get("if")):
                pending.append(by_path[callee])
    return list(seen.values())


def check_rel_009(repo_root: Path) -> list[Finding]:
    workflows = [wf for wf in load_workflows(repo_root) if wf.status == LoadStatus.OK]
    findings: list[Finding] = []
    for wf in _release_scope(workflows):
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            if not reachable_from_release(job.get("if")):
                continue
            hits = channels_in("\n".join(_strings(job)))
            if not hits:
                continue
            findings.append(Finding(
                rule=RULE, path=wf.path,
                message=f"release job '{job_id}' publishes to an external package channel ({', '.join(hits)}); "
                "a channel failure would turn a completed release red and could not be retried alone",
                fix="move the channel to its own workflow triggered by the published stable release "
                f"(or the release workflow's completion) plus workflow_dispatch, and drop any `needs:` on it ({POLICY})",
            ))
    return findings
