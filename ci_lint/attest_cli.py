"""`ci-lint attest ...` -- ci-attestations on the host and in CI (GATE-010, #198).

    attest verify   [--commit HEAD] [--base REV]   check a commit's Ci-Attestation trailers
    attest lineage  [--commit X] [--pr N]           print the commit's lineage label
    attest keys     --pr N --out-dir D              tiny side files + cache keys for each valid gate
    attest resolve  --family F [--pr N]             nearest ancestor cache entry to hydrate

`keys` and `resolve` are the caching half: a CI step saves each valid
attestation as its own tiny cache entry (`att1-<gate>-<lineage>`), and a
later run lists entries through the REST API (GET only) to choose which
build cache is valid to hydrate -- the nearest ancestor commit whose
required gates were attested.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from ci_lint.attestations import (
    MISSING,
    VALID,
    CommitAttestations,
    Definition,
    load_definition,
    parse_trailers,
    verify_commit,
)
from ci_lint.cache_lineage import Lineage, LineageError, cache_key, lineage_of, parse_key, resolve
from ci_lint.github_api import GitHubApiError, default_fetch
from ci_lint.proc import run_captured

SIDE_FAMILY = "att1"


def gate_family(gate: str, prefix: str = SIDE_FAMILY) -> str:
    """`rust/x86_64-unknown-linux-gnu/test` -> `att1-rust.x86_64-unknown-linux-gnu.test`."""

    return f"{prefix}-{gate.replace('/', '.')}"


def _write_outputs(lines: list[str]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def _definition(repo: Path, base: str | None) -> Definition | None:
    loaded = load_definition(repo, rev=base)
    if loaded is None:
        print("ci-lint attest: no ci-attestations.yml" + (f" at {base}" if base else ""), file=sys.stderr)
        return None
    for finding in loaded.findings:
        print(finding.render(), file=sys.stderr)
    return loaded.definition


def _lineage(repo: Path, args: argparse.Namespace) -> Lineage:
    return lineage_of(repo, args.commit, main_ref=args.main_ref, pr=args.pr)


def _tree_of(repo: Path, commit: str) -> str:
    """The commit's tree sha, for the message a blocked push prints."""

    out = run_captured(["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"]).stdout
    return out.strip() or commit


def _required_gates(args: argparse.Namespace) -> list[str]:
    """`--required a,b --required c` -> ['a', 'b', 'c'] in the order given."""

    gates: list[str] = []
    for value in getattr(args, "required", None) or []:
        gates.extend(gate.strip() for gate in value.split(",") if gate.strip())
    return gates


def _report_required(args: argparse.Namespace, repo: Path, result: CommitAttestations) -> int:
    """Report the `--required` gates the commit did not attest, honouring --mode.

    `off` prints nothing and returns 0 so the command behaves exactly as before;
    `warn` prints an annotation and still returns 0; `enforce` prints an ::error
    and returns 1. A stale or forged trailer arrives here as `missing`, never as
    a failure, so the message is always "not attested".
    """

    gates = _required_gates(args)
    if not gates or args.mode == "off":
        return 0
    attested = {status.gate for status in result.statuses if status.state == VALID}
    missing = [gate for gate in gates if gate not in attested]
    if not missing:
        return 0
    tree = _tree_of(repo, args.commit)
    for gate in missing:
        text = (
            f"{gate} not attested for tree {tree}. Run ./push "
            "(it runs 'soldr lint rust --attest' and stamps the commit)."
        )
        if args.mode == "enforce":
            print(f"::error title=Missing client attestation::{text}")
        else:
            print(f"::warning title=Missing client attestation::{text}")
    return 1 if args.mode == "enforce" else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    definition = _definition(repo, args.base)
    if definition is None:
        return 2
    result = verify_commit(repo, args.commit, definition)
    for status in sorted(result.statuses, key=lambda s: s.gate):
        mark = {VALID: "ok  ", MISSING: "--  "}.get(status.state, "FAIL")
        print(f"{mark} {status.gate}: {status.state} -- {status.detail}")
    for error in result.malformed:
        print(f"FAIL {error}")
    if result.errors or result.malformed:
        return 1
    return _report_required(args, repo, result)


def _cmd_lineage(args: argparse.Namespace) -> int:
    try:
        print(_lineage(Path(args.repo).resolve(), args).label())
    except LineageError as exc:
        print(f"ci-lint attest lineage: {exc}", file=sys.stderr)
        return 2
    return 0


def _configure_context(args: argparse.Namespace) -> int | None:
    if args.github_context:
        from ci_lint.attestation_context import publication_context  # noqa: PLC0415

        try:
            context = publication_context(os.environ)
        except ValueError as exc:
            print(f"ci-lint attest keys: {exc}", file=sys.stderr)
            return 2
        if not context.enabled:
            if args.github_output:
                _write_outputs(["count=0"])
            return 0
        args.commit, args.base, args.pr, args.main_ref = context.commit, context.base, context.pr, context.main_ref
        args.promote_merged_pr = context.promote
        args.fetch_promotion_source = context.promote
    return None


def _cmd_keys(args: argparse.Namespace) -> int:
    contextual_exit = _configure_context(args)
    if contextual_exit is not None:
        return contextual_exit
    return _publish_keys(args)


def _publish_keys(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    definition = _definition(repo, args.base)
    if definition is None:
        return 0  # nothing to publish is not an error
    try:
        lineage = _lineage(repo, args)
    except LineageError as exc:
        print(f"ci-lint attest keys: {exc}", file=sys.stderr)
        return 2
    result = verify_commit(repo, args.commit, definition)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    message = run_captured(["git", "-C", str(repo), "log", "-1", "--format=%B", args.commit]).stdout
    records = {p.attestation.gate: p.attestation for p in parse_trailers(message) if p.attestation is not None}
    if args.promote_merged_pr:
        from ci_lint.attestation_promotion import PromotionInput, promote  # noqa: PLC0415
        from ci_lint.attestations import CommitAttestations, GateStatus  # noqa: PLC0415

        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        slug = args.github_repo or os.environ.get("GITHUB_REPOSITORY", "")
        promoted = promote(
            repo,
            PromotionInput(
                slug,
                lineage.sha,
                os.environ.get("GITHUB_EVENT_NAME", ""),
                os.environ.get("GITHUB_REF", ""),
                args.main_ref.removeprefix("origin/"),
                int(time.time()),
                args.promotion_max_age_hours * 3600,
                args.fetch_promotion_source,
            ),
            default_fetch,
            token,
        )
        print(f"ci-lint attest keys: promotion {promoted.reason}; source={promoted.source}")
        for record in promoted.records:
            records[record.gate] = record
        statuses = {status.gate: status for status in result.statuses}
        for record in promoted.records:
            statuses[record.gate] = GateStatus(record.gate, VALID, f"promoted from {promoted.source}")
        result = CommitAttestations(result.sha, tuple(statuses.values()), result.malformed)
    lines: list[str] = []
    index = 0
    # Gates a remote job depends on (and therefore cache hydration, which
    # requires e.g. the test gate) are published first; the rest after, in
    # path order. Learned in the soldr pilot: 13 valid gates against 8 slots
    # dropped `rust/x86_64-unknown-linux-gnu/test` alphabetically.
    mapped = {gate for job in definition.jobs for gate in job.gates}
    valid = sorted((s for s in result.statuses if s.state == VALID), key=lambda s: (s.gate not in mapped, s.gate))
    dropped = [s.gate for s in valid[args.slots :]]
    if dropped:
        message = (
            f"{len(valid)} valid gates but only {args.slots} side-entry slots; not published: "
            f"{', '.join(dropped)} (add save slots and raise --slots)"
        )
        print(f"ci-lint attest keys: {message}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=ci-attestations side entries truncated::{message}")
    for status in valid[: args.slots]:
        path = out_dir / f"{index}.json"
        path.write_text(records[status.gate].compact() + "\n", encoding="utf-8")
        key = cache_key(gate_family(status.gate), lineage)
        stem = f"{gate_family(status.gate)}-{lineage.stem()}"
        print(f"{key}  <- {path}")
        lines += [f"key_{index}={key}", f"stem_{index}={stem}", f"path_{index}={path}"]
        index += 1
    lines.append(f"count={index}")
    lines.append(f"lineage={lineage.label()}")
    if args.github_output:
        _write_outputs(lines)
    return 0


def _list_keys(repo_slug: str, prefix: str, token: str) -> list[str]:
    keys: list[str] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"key": prefix, "per_page": 100, "page": page})
        doc = default_fetch(f"https://api.github.com/repos/{repo_slug}/actions/caches?{query}", token)
        caches = doc.get("actions_caches") if isinstance(doc, dict) else None
        if not isinstance(caches, list) or not caches:
            break
        keys += [c["key"] for c in caches if isinstance(c, dict) and isinstance(c.get("key"), str)]
        if len(caches) < 100:
            break
    return keys


def _cmd_resolve(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        head = _lineage(repo, args)
    except LineageError as exc:
        print(f"ci-lint attest resolve: {exc}", file=sys.stderr)
        return 2
    if args.keys_file:
        keys = [k.strip() for k in Path(args.keys_file).read_text(encoding="utf-8").splitlines() if k.strip()]
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        slug = args.github_repo or os.environ.get("GITHUB_REPOSITORY")
        if not token or not slug:
            print("ci-lint attest resolve: needs --keys-file, or GITHUB_TOKEN and GITHUB_REPOSITORY", file=sys.stderr)
            return 2
        try:
            keys = _list_keys(slug, args.family, token)
            for gate in args.require_gate:
                keys += _list_keys(slug, gate_family(gate), token)
        except GitHubApiError as exc:
            print(f"ci-lint attest resolve: {exc}", file=sys.stderr)
            return 2
    required: frozenset[str] | None = None
    for gate in args.require_gate:
        shas = frozenset(
            e.lineage.sha for e in (parse_key(k) for k in keys) if e is not None and e.family == gate_family(gate)
        )
        required = shas if required is None else required & shas
    result = resolve(repo, head, keys, family=args.family, required_shas=required)
    print(f"ci-lint attest resolve: {result.key or '(none)'} -- {result.reason}")
    if args.github_output:
        _write_outputs([f"restore_key={result.key or ''}", f"lineage={head.label()}"])
    return 0


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("attest", help="ci-attestations: verify, lineage, cache side entries, resolve (GATE-010, #198)")
    at = p.add_subparsers(dest="attest_command", required=True)

    def common(q: argparse.ArgumentParser) -> None:
        q.add_argument("--repo", default=".")
        q.add_argument("--commit", default="HEAD")

    def lineage_args(q: argparse.ArgumentParser) -> None:
        q.add_argument("--pr", type=int, help="PR number (omit for a default-branch commit)")
        q.add_argument("--main-ref", default="origin/main", help="the default branch ref (full history needed)")

    v = at.add_parser("verify", help="check a commit's Ci-Attestation trailers against ci-attestations.yml")
    common(v)
    v.add_argument("--base", help="read the definition from this commit (a PR's base) instead of the work tree")
    v.add_argument(
        "--required",
        action="append",
        metavar="GATE[,GATE]",
        help="also require these gates to be attested (repeatable, comma-separated)",
    )
    v.add_argument(
        "--mode",
        choices=("off", "warn", "enforce"),
        default="off",
        help="what a missing --required gate does: off prints nothing, warn annotates and exits 0, "
        "enforce annotates and exits 1 (default: off)",
    )
    v.set_defaults(func=_cmd_verify)

    lin = at.add_parser(
        "lineage", help="print the commit's ancestor-defining label (m<n>-<sha10> / m<b>-c<k>-<sha10>-pr-<N>)"
    )
    common(lin)
    lineage_args(lin)
    lin.set_defaults(func=_cmd_lineage)

    k = at.add_parser("keys", help="write each valid gate attestation as a tiny side file and print its cache key")
    common(k)
    lineage_args(k)
    k.add_argument("--github-context", action="store_true", help="derive publication inputs from the GitHub event")
    k.add_argument("--base", help="read the definition from this commit (a PR's base)")
    k.add_argument("--out-dir", required=True)
    k.add_argument("--slots", type=int, default=8, help="max side entries (one cache-save step per slot)")
    k.add_argument(
        "--github-output",
        action="store_true",
        help="write key_<i> (full), stem_<i> (append ${{ env.PR_CACHE_TAG }}), path_<i>, count, lineage",
    )
    k.add_argument(
        "--promote-merged-pr",
        action="store_true",
        help="on default-branch push, publish exact-tree trusted merged-PR evidence under main keys",
    )
    k.add_argument(
        "--fetch-promotion-source", action="store_true", help="fetch missing merged PR head (bounded 60s read)"
    )
    k.add_argument("--github-repo", help="owner/name (default: GITHUB_REPOSITORY)")
    k.add_argument("--promotion-max-age-hours", type=int, default=24)
    k.set_defaults(func=_cmd_keys)

    r = at.add_parser("resolve", help="the nearest ancestor cache entry of a family to hydrate (GET-only REST listing)")
    common(r)
    lineage_args(r)
    r.add_argument("--family", required=True, help="cache key family, e.g. zccache-unit-v1-x86_64-unknown-linux-gnu")
    r.add_argument(
        "--require-gate",
        action="append",
        default=[],
        help="only hydrate from commits whose attestation side entry for this gate exists (repeatable)",
    )
    r.add_argument("--keys-file", help="offline: one cache key per line instead of the live listing")
    r.add_argument("--github-repo", help="owner/name (default: GITHUB_REPOSITORY)")
    r.add_argument("--github-output", action="store_true", help="write restore_key/lineage outputs")
    r.set_defaults(func=_cmd_resolve)
