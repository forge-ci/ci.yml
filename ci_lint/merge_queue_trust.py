"""GEN-021/GATE-008 merge_group extension: queue-time verified reuse (#419).

A `merge_group` run may skip a `[gate.trust].skip` lane only when the
queue entry is provably the exact tree a PR head already attested:

1. the group holds exactly one PR -- the queue ref is
   `gh-readonly-queue/<base>/pr-<N>-<base-sha>`, the payload's `base_sha`
   equals `<base-sha>`, that commit is on `<base>` itself (a later entry of
   a multi-PR group is based on the previous entry's queue commit, which is
   not), and the queue head's only parent is that base;
2. the queue head's tree equals PR `<N>`'s current head tree (GET-only);
3. PR `<N>`'s head passes the unchanged GATE-008 decision (`decide`) under
   the queue base's `[gate.trust]` -- attestation, lanes, surfaces, fork,
   author, labels, and the same deterministic per-PR-head audit sample.

Every other outcome -- an unreadable API, an unparseable ref, a multi-PR
group, a tree mismatch, a closed PR -- is a reason code with no skip. It
never applies to `push`: `decide_merge_group` refuses any other event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ci_lint.gate_trust import TrustDecision, TrustInput, decide
from ci_lint.github_api import FetchStatusFn, GitHubApiError
from ci_lint.cargo_messages import JsonValue

_API = "https://api.github.com"
_QUEUE_REF = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/(?P<base>.+)/pr-(?P<pr>[1-9][0-9]*)-(?P<sha>[0-9a-f]{40})$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class QueueRef:
    base: str
    pr: int
    base_sha: str


@dataclass(frozen=True)
class MergeGroupEvent:
    repo_slug: str
    head_ref: str
    head_sha: str
    base_sha: str


@dataclass(frozen=True)
class MergeGroupDecision:
    decision: TrustDecision
    pr_input: TrustInput | None  # the PR-head input decide() judged; per-job GATE-010 decisions reuse it
    pr: int | None
    tree: str | None = None  # the proven tree, for the aggregator's provenance line


def parse_queue_ref(ref: str) -> QueueRef | None:
    match = _QUEUE_REF.match(ref)
    if match is None:
        return None
    return QueueRef(match.group("base"), int(match.group("pr")), match.group("sha"))


def merge_group_event(payload: dict[str, JsonValue], repo_slug: str | None) -> MergeGroupEvent | None:
    group = payload.get("merge_group")
    if not isinstance(group, dict):
        return None
    repo_obj = payload.get("repository")
    slug = repo_obj.get("full_name") if isinstance(repo_obj, dict) else None
    slug = slug if isinstance(slug, str) else repo_slug
    head_ref, head_sha, base_sha = (group.get(k) for k in ("head_ref", "head_sha", "base_sha"))
    if not (isinstance(slug, str) and isinstance(head_ref, str) and isinstance(head_sha, str)
            and isinstance(base_sha, str)):
        return None
    return MergeGroupEvent(slug, head_ref, head_sha, base_sha)


def _no(reason: str, detail: str, pr: int | None = None) -> MergeGroupDecision:
    return MergeGroupDecision(TrustDecision(False, False, reason, detail), None, pr)


class _Unreadable(Exception):
    pass


def _get(fetch: FetchStatusFn, token: str, path: str) -> dict[str, JsonValue]:
    try:
        status, body = fetch(f"{_API}{path}", token)
    except GitHubApiError as exc:
        raise _Unreadable(str(exc)) from exc
    if status != 200 or not isinstance(body, dict):
        raise _Unreadable(f"GET {path} -> HTTP {status}")
    return body


def _text(obj: JsonValue, *keys: str) -> str | None:
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj if isinstance(obj, str) else None


@dataclass(frozen=True)
class _GroupFacts:
    pull: dict[str, JsonValue]
    pr_head: str
    pr_tree: str


def _read_group(event: MergeGroupEvent, ref: QueueRef, *, fetch: FetchStatusFn,
                token: str) -> _GroupFacts | MergeGroupDecision:
    """GET-only clauses 1-2: one PR in the group, identical trees."""

    slug = event.repo_slug
    try:
        queue_commit = _get(fetch, token, f"/repos/{slug}/git/commits/{event.head_sha}")
        parents = queue_commit.get("parents")
        parent_shas = [_text(p, "sha") for p in parents] if isinstance(parents, list) else []
        if parent_shas != [event.base_sha]:
            return _no("multi-pr-group", f"queue head {event.head_sha[:12]} is not one commit on the group base", ref.pr)
        compare = _get(fetch, token, f"/repos/{slug}/compare/{quote(ref.base, safe='')}...{event.base_sha}")
        if compare.get("status") not in ("identical", "behind"):
            return _no("multi-pr-group",
                       f"group base {event.base_sha[:12]} is not on {ref.base} (an earlier queue entry precedes PR #{ref.pr})",
                       ref.pr)
        pull = _get(fetch, token, f"/repos/{slug}/pulls/{ref.pr}")
        pr_head = _text(pull, "head", "sha")
        if pull.get("state") != "open" or _text(pull, "base", "ref") != ref.base or pr_head is None \
                or not _SHA.match(pr_head):
            return _no("queue-pr-mismatch", f"PR #{ref.pr} is not an open PR into {ref.base}", ref.pr)
        pr_commit = _get(fetch, token, f"/repos/{slug}/git/commits/{pr_head}")
    except _Unreadable as exc:
        return _no("queue-unreadable", f"GitHub API unreadable, failing closed: {exc}", ref.pr)
    queue_tree, pr_tree = _text(queue_commit, "tree", "sha"), _text(pr_commit, "tree", "sha")
    if queue_tree is None or pr_tree is None:
        return _no("queue-unreadable", "a commit's tree is missing from the API response", ref.pr)
    if queue_tree != pr_tree:
        return _no("tree-mismatch",
                   f"queue tree {queue_tree[:12]} != PR #{ref.pr} head tree {pr_tree[:12]}; the base moved, run everything",
                   ref.pr)
    return _GroupFacts(pull, pr_head, pr_tree)


def decide_merge_group(repo: Path, event_name: str, event: MergeGroupEvent | None, *,
                       fetch: FetchStatusFn, token: str) -> MergeGroupDecision:
    if event_name != "merge_group":
        return _no("not-merge-group", f"event '{event_name}' never reuses through the merge-queue path")
    if event is None:
        return _no("queue-unreadable", "the merge_group payload lacks head_ref/head_sha/base_sha")
    ref = parse_queue_ref(event.head_ref)
    if ref is None:
        return _no("queue-ref-unparseable", f"{event.head_ref!r} is not gh-readonly-queue/<base>/pr-<N>-<sha>")
    if not (_SHA.match(event.head_sha) and _SHA.match(event.base_sha)):
        return _no("queue-unreadable", "merge_group head_sha/base_sha are not commit SHAs", ref.pr)
    if ref.base_sha != event.base_sha:
        return _no("multi-pr-group", f"queue ref base {ref.base_sha[:12]} != group base {event.base_sha[:12]}", ref.pr)
    facts = _read_group(event, ref, fetch=fetch, token=token)
    if isinstance(facts, MergeGroupDecision):
        return facts
    pull, pr_head, pr_tree = facts.pull, facts.pr_head, facts.pr_tree
    raw_labels = pull.get("labels")
    labels = tuple(label["name"] for label in (raw_labels if isinstance(raw_labels, list) else [])
                   if isinstance(label, dict) and isinstance(label.get("name"), str))
    pr_input = TrustInput(
        event="pull_request",
        head_sha=pr_head,
        base_sha=event.base_sha,  # the queue base owns the policy, as a PR base does
        author_association=_text(pull, "author_association"),
        head_repo=_text(pull, "head", "repo", "full_name"),
        base_repo=_text(pull, "base", "repo", "full_name"),
        labels=labels,
    )
    inner = decide(repo, pr_input)
    detail = f"PR #{ref.pr} head {pr_head[:12]} tree {pr_tree[:12]} == queue tree: {inner.detail}"
    return MergeGroupDecision(TrustDecision(inner.trusted, inner.would_trust, inner.reason, detail), pr_input, ref.pr,
                              pr_tree)
