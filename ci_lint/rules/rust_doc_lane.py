"""RUST-027: no rustdoc lane in the PR gate unless the docs publish (#116).

Static signal: a `cargo doc` / `cargo rustdoc` / `soldr cargo doc` /
`rustdoc` invocation in a `run:` line of a workflow triggered by
`pull_request`/`pull_request_target`/`merge_group`, or one level into a
`ci/*.py` script such a line names. Composite actions are not scanned (their
trigger is unknowable statically). `cargo test --doc` (doctests) and
`RUSTDOCFLAGS` alone are never doc lanes.

Publishing is read from `Cargo.toml` only. A member publishes docs when its
resolved `publish` is not `false` (absent, `true` or a non-empty registry
list; `publish.workspace = true` inherits `[workspace.package].publish`) or
it declares `[package.metadata.docs.rs]`.

- violation: a PR-gate doc lane while no workspace member publishes docs.
- violation: some members publish, but the lane documents `--workspace`/
  `--all`, or `-p` names a member that does not publish.
- needs_review: some members publish, the workspace has more than one
  member, and the lane names neither `-p` nor `--workspace` (default-members
  decide what it documents).

A separately hosted doc site is the case Cargo.toml cannot express: excuse
the line with a same-line `# ci-lint: allow RUST-027 <reason>`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_scan import discover_workspace
from ci_lint.finding import Finding, Status
from ci_lint.rules.test_invocations import (
    CommandSite,
    allowed,
    is_cargo_token,
    iter_run_lines,
    script_calls,
    source_line,
    split_commands,
)
from ci_lint.rules.tools import CI_SCRIPT_RE
from ci_lint.toml_cursor import TomlValue
from ci_lint.workflow_scan import as_dict, get_on_section, load_workflows
from ci_lint.yaml_io import LoadStatus

RULE = "RUST-027"
PR_EVENTS: frozenset[str] = frozenset({"pull_request", "pull_request_target", "merge_group"})


@dataclass(frozen=True)
class DocMember:
    name: str
    publishes_docs: bool


@dataclass(frozen=True)
class DocInvocation:
    args: tuple[str, ...]
    site: CommandSite


def find_doc_invocation(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    """Arguments after `cargo doc`/`cargo rustdoc`/`rustdoc`, else None."""

    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] in ("rustdoc", "rustdoc.exe") and i == _command_start(tokens):
            return tokens[i + 1 :]
        if not is_cargo_token(tok):
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].startswith("+"):
            j += 1
        if j < len(tokens) and tokens[j] in ("doc", "rustdoc"):
            return tokens[j + 1 :]
        return None
    return None


def _command_start(tokens: tuple[str, ...]) -> int:
    """Index of the program token, skipping wrapper launchers (`soldr`,
    `rustup run <tc>`)."""

    i = 0
    if i < len(tokens) and tokens[i].rsplit("/", 1)[-1] == "soldr":
        i += 1
    if i + 2 < len(tokens) and tokens[i] == "rustup" and tokens[i + 1] == "run":
        i += 3
    return i


def _load(path: Path) -> dict[str, TomlValue] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _publish_value(raw: TomlValue, root_ws_publish: TomlValue) -> bool:
    if isinstance(raw, dict) and raw.get("workspace") is True:
        raw = root_ws_publish
    if raw is False:
        return False
    if isinstance(raw, list):
        return len(raw) > 0
    return True


def workspace_doc_members(repo_root: Path) -> list[DocMember]:
    root = _load(repo_root / "Cargo.toml") or {}
    ws = root.get("workspace")
    ws_pkg = ws.get("package") if isinstance(ws, dict) else None
    root_ws_publish: TomlValue = ws_pkg.get("publish", True) if isinstance(ws_pkg, dict) else True
    out: list[DocMember] = []
    for crate in discover_workspace(repo_root):
        manifest = _load(repo_root / crate.manifest_path) or {}
        pkg = manifest.get("package")
        pkg = pkg if isinstance(pkg, dict) else {}
        meta = pkg.get("metadata")
        docs = meta.get("docs") if isinstance(meta, dict) else None
        has_docs_rs = isinstance(docs, dict) and isinstance(docs.get("rs"), dict)
        publishes = _publish_value(pkg.get("publish", True), root_ws_publish)
        out.append(DocMember(crate.name, publishes or has_docs_rs))
    return out


def _pr_gate_workflows(repo_root: Path) -> set[str]:
    out: set[str] = set()
    for wf in load_workflows(repo_root):
        if wf.status == LoadStatus.OK and PR_EVENTS & set(get_on_section(as_dict(wf.document))):
            out.add(wf.path)
    return out


def iter_pr_gate_doc_invocations(repo_root: Path) -> list[DocInvocation]:
    gate = _pr_gate_workflows(repo_root)
    out: list[DocInvocation] = []
    scripts: set[str] = set()
    for run in iter_run_lines(repo_root):
        if run.path not in gate:
            continue
        for tokens in split_commands(run.line):
            for tok in tokens:
                if CI_SCRIPT_RE.match(tok) and (repo_root / tok).is_file():
                    scripts.add(tok)
            args = find_doc_invocation(tuple(tokens))
            if args is not None:
                out.append(DocInvocation(args, CommandSite(tuple(tokens), run.path, None, run.loc, run.line)))
    for rel in sorted(scripts):
        script = repo_root / rel
        for call in script_calls(script):
            args = find_doc_invocation(call.tokens)
            if args is not None:
                raw = source_line(script, call.line)
                out.append(DocInvocation(args, CommandSite(call.tokens, rel, call.line, f"line {call.line}", raw)))
    return out


def _packages(args: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for i, arg in enumerate(args):
        if arg in ("-p", "--package") and i + 1 < len(args):
            out.append(args[i + 1])
        elif arg.startswith("--package="):
            out.append(arg.split("=", 1)[1])
        elif arg.startswith("-p") and len(arg) > 2:
            out.append(arg[2:])
    return out


def _finding(inv: DocInvocation, message: str, fix: str, status: Status = Status.VIOLATION) -> Finding:
    return Finding(
        rule=RULE,
        path=inv.site.path,
        line=inv.site.line,
        status=status,
        message=f"{inv.site.loc}: {message}",
        fix=fix,
    )


def check_rust_027(repo_root: Path) -> list[Finding]:
    if not (repo_root / "Cargo.toml").is_file():
        return []
    invocations = [i for i in iter_pr_gate_doc_invocations(repo_root) if not allowed(i.site.raw, RULE)]
    if not invocations:
        return []
    members = workspace_doc_members(repo_root)
    publishing = sorted(m.name for m in members if m.publishes_docs)
    findings: list[Finding] = []
    for inv in invocations:
        if not publishing:
            findings.append(
                _finding(
                    inv,
                    "the PR gate runs a rustdoc lane, but no workspace member publishes docs (every member "
                    "is publish = false and none declares [package.metadata.docs.rs])",
                    "remove the doc lane from the PR gate; doctests keep running under cargo test. If the "
                    "docs are hosted separately (e.g. Pages), excuse the line with '# ci-lint: allow "
                    "RUST-027 <reason>'",
                )
            )
            continue
        pkgs = _packages(inv.args)
        non_pub = sorted(set(pkgs) - set(publishing))
        scope_fix = "document only the publishing members: " + " ".join(f"-p {n}" for n in publishing)
        if "--workspace" in inv.args or "--all" in inv.args:
            findings.append(_finding(inv, "the rustdoc lane documents the whole workspace", scope_fix))
        elif non_pub:
            findings.append(
                _finding(inv, f"the rustdoc lane documents non-publishing member(s) {', '.join(non_pub)}", scope_fix)
            )
        elif not pkgs and len(members) > 1:
            findings.append(
                _finding(
                    inv,
                    "the rustdoc lane names no -p, so the workspace's default members decide what it "
                    "documents; cannot confirm it is limited to the publishing members",
                    scope_fix,
                    Status.NEEDS_REVIEW,
                )
            )
    return findings
