"""Publish an exact qualified commit through Git, without executing a gate.

An existing PR follows its branch update. The immutable SHA is the transport
source; worktree HEAD is never substituted. A lease protects competing remote
updates. The worktree's gate lock (zackees/ci.yml#397) spans validation and
transport.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ci_lint.gate_run_lock import hold
from ci_lint.lane_cache import run_lock_path
from ci_lint.local_gate import GitError, ZERO_SHA, _git, check_push, load_gate_config_at, tracked_changes
from ci_lint.proc import run_captured


@dataclass(frozen=True)
class PublishOutcome:
    exit_code: int
    message: str
    head_sha: str | None = None


@dataclass(frozen=True)
class SourceState:
    head: str
    branch: str
    tree: str
    parents: tuple[str, ...]


def _source(repo: Path, sha: str) -> SourceState:
    head = _git(repo, "rev-parse", "HEAD").strip()
    if head != sha or tracked_changes(repo):
        raise ValueError("expected stamped HEAD moved or tracked source has uncommitted changes")
    branch = _git(repo, "symbolic-ref", "--quiet", "HEAD").strip()
    if not branch.startswith("refs/heads/") or _git(repo, "rev-parse", branch).strip() != sha:
        raise ValueError("publication requires the expected checked-out branch")
    return SourceState(head, branch, _git(repo, "rev-parse", f"{sha}^{{tree}}").strip(),
                       tuple(_git(repo, "log", "-1", "--format=%P", sha).split()))


def _remote_head(repo: Path, remote: str, branch: str) -> str:
    result = run_captured(["git", "ls-remote", "--heads", "--", remote, branch], cwd=repo, timeout=120)
    if not result.ok:
        raise GitError(f"cannot read remote branch: {result.stderr.strip()}")
    lines = result.stdout.splitlines()
    if not lines:
        return ZERO_SHA
    if len(lines) != 1 or lines[0].split()[1:] != [branch]:
        raise ValueError("remote branch identity is ambiguous")
    sha = lines[0].split()[0]
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("remote branch has an unsupported commit identity")
    return sha


def _compatible_remote(repo: Path, tip: str, source: SourceState) -> None:
    if tip == ZERO_SHA or tip == source.head:
        return
    result = run_captured(["git", "merge-base", "--is-ancestor", tip, source.head], cwd=repo)
    if result.ok:
        return
    if result.returncode != 1:
        raise ValueError("remote commit is unavailable locally; fetch the branch before publishing")
    # A message-only attestation amend may replace the old remote head.
    if (_git(repo, "rev-parse", f"{tip}^{{tree}}").strip() != source.tree
            or tuple(_git(repo, "log", "-1", "--format=%P", tip).split()) != source.parents):
        raise ValueError("remote branch is not an ancestor or a message-only version of the qualified head")


def _destination(repo: Path, remote: str) -> str:
    destinations = _git(repo, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(destinations) != 1 or not destinations[0].strip():
        raise ValueError("publication requires exactly one effective Git push destination")
    return destinations[0]


def _transport(repo: Path, source: SourceState, destination: str, tip: str) -> PublishOutcome | None:
    try:
        pushed = run_captured(["git", "push", "--porcelain",
                               f"--force-with-lease={source.branch}:{'' if tip == ZERO_SHA else tip}",
                               "--", destination, f"{source.head}:{source.branch}"], cwd=repo, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return PublishOutcome(2, f"local-gate push: publication of {source.head} is unconfirmed: {error}", source.head)
    if not pushed.ok:
        return PublishOutcome(1, f"local-gate push: publication of {source.head} is unconfirmed; transport failed: "
                              + (pushed.stderr.strip() or pushed.stdout.strip()), source.head)
    return None


def _confirm(repo: Path, source: SourceState, destination: str, remote: str) -> PublishOutcome:
    try:
        if _remote_head(repo, destination, source.branch) != source.head:
            return PublishOutcome(1, f"local-gate push: transported {source.head}, but remote changed afterward", source.head)
        if _source(repo, source.head) != source:
            raise ValueError("source identity changed")
    except (OSError, GitError, ValueError, subprocess.SubprocessError) as error:
        return PublishOutcome(1, f"local-gate push: transport succeeded for {source.head}, "
                              f"but confirmation failed or local source changed: {error}", source.head)
    return PublishOutcome(0, f"local-gate push: published qualified {source.head} to {remote}/{source.branch[11:]}", source.head)


def _publish(repo: Path, sha: str, remote: str) -> PublishOutcome:
    source = _source(repo, sha)
    loaded = load_gate_config_at(repo, sha)
    config = loaded.config
    if (loaded.findings or config is None or config.replay is None
            or not config.replay.qualified or not config.replay.provider_query):
        raise ValueError("publication requires valid committed qualified replay enrollment")
    destination = _destination(repo, remote)
    tip = _remote_head(repo, destination, source.branch)
    _compatible_remote(repo, tip, source)
    checked = check_push(repo, f"{source.branch} {sha} {source.branch} {tip}\n")
    if checked.exit_code:
        return PublishOutcome(1, "local-gate push: refused:\n  " + "\n  ".join(checked.problems))
    if _source(repo, sha) != source:
        raise ValueError("source branch, tree or parents changed during publication validation")
    # Pass an immutable object id, never HEAD or a moving local ref. This
    # preserves qualified source even if an external editor ignores our lock.
    return _transport(repo, source, destination, tip) or _confirm(repo, source, destination, remote)


def publish(repo: Path, *, sha: str, remote: str = "origin") -> PublishOutcome:
    """Validate full outgoing proof and update this branch's existing PR head.

    No build, stamp, PR metadata write, unleased force or automatic retry.
    Concurrent external edits observed after transport return failure while
    identifying the immutable commit that was published.
    """
    try:
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ValueError("--sha must be the full stamped commit id")
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", remote) is None:
            raise ValueError("--remote must name a configured Git remote")
        with hold(run_lock_path(repo)):
            return _publish(repo, sha, remote)
    except (OSError, GitError, ValueError, subprocess.SubprocessError) as error:
        return PublishOutcome(2, f"local-gate push: {error}")
