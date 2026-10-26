"""GEN-030 (static, #322): a repository's dev-host tooling runs a broad
sweep instead of the owning reclaimer's verb.

docs/policy-general.md, "Dev-host hygiene: every local artifact class has
one reclaiming owner": every local artifact class has exactly one owner
(bosn for Docker objects and stale runs, `soldr gc`/the soldr broker for
`target/` dirs and soldr state, `local-gate` for its lane logs). A generic
sweep cannot tell what is in use and kills other sessions' runs on a shared
host.

Where it looks (dev-host surfaces only -- hosted CI job steps are exempt,
an ephemeral runner leaves nothing behind, so workflow `run:` blocks are
NOT scanned):

- every `.py`/`.sh`/`.bash` script under `ci/` or `scripts/`;
- every `.py`/`.sh`/`.bash` file under a `skills/` or `tools/` directory or
  under `.claude/` (GHAPI-001's agent-tooling convention);
- a root `Makefile`/`justfile` and `bosn.toml`.

What it reports (violation, one per line):

- `docker system prune`, `docker volume prune`, `docker container prune`,
  `docker builder prune`, `docker image prune -a/--all` -> bosn;
- `rm -rf` of `~/.cargo`/`$CARGO_HOME`, of a cross-repo `target` sweep
  (a glob or `..` path ending in `target`), or of a soldr/bosn state dir
  (`~/.soldr`, `.cache/soldr`, `$SOLDR_*`, `/tmp/bosn-*`, `$BOSN_STATE_DIR`,
  `~/.bosn`) -> `soldr gc` / bosn;
- an mtime-only `find ... -mtime ... -delete` -> the owning reclaimer.

Coordination with GEN-028 (raw `docker` instead of bosn): a `docker ...
prune` line is a GEN-030 finding only. `is_broad_sweep` is exported so
GEN-028, when implemented, skips lines this rule already reports.

A same-line `# ci-lint: allow GEN-030 <reason>` excuses one line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.repo_files import list_repo_files
from ci_lint.rules.test_invocations import allowed

RULE = "GEN-030"
POLICY = "docs/policy-general.md#dev-host-hygiene-every-local-artifact-class-has-one-reclaiming-owner-candidate-gen-030"
SCRIPT_SUFFIXES: frozenset[str] = frozenset({".py", ".sh", ".bash"})
SCRIPT_DIRS: tuple[str, ...] = ("ci/", "scripts/", ".claude/")
TOOL_DIR_NAMES: frozenset[str] = frozenset({"skills", "tools"})
ROOT_FILES: frozenset[str] = frozenset({"Makefile", "makefile", "GNUmakefile", "justfile", "Justfile", "bosn.toml"})


@dataclass(frozen=True)
class Sweep:
    kind: str
    owner: str


_DOCKER_PRUNE = re.compile(r"\bdocker\s+(?:system|volume|container|builder|buildx)\s+prune\b")
_DOCKER_IMAGE_ALL = re.compile(r"\bdocker\s+image\s+prune\b.*(?:\s-a\b|\s--all\b|\s-[a-z]*a[a-z]*\b)")
_RM_RF = re.compile(r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*[rR][a-zA-Z]*|-r\s+-f|-f\s+-r|--recursive\s+--force|--force\s+--recursive)\b(.*)")
_CARGO_HOME = re.compile(r"(?:~|\$\{?HOME\}?)/\.cargo\b|\$\{?CARGO_HOME\}?")
_SOLDR_BOSN = re.compile(
    r"(?:~|\$\{?HOME\}?)/\.(?:soldr|bosn)\b|\.cache/(?:soldr|bosn)\b|\$\{?(?:SOLDR|BOSN)_[A-Z_]*\}?|/tmp/bosn-|/tmp/soldr-"
)
_TARGET_SWEEP = re.compile(r"(?:\*|\.\.)[^\s]*/target/?(?:\s|$|\*)")
_FIND_MTIME_DELETE = re.compile(r"\bfind\b.*-mtime\b.*-delete\b")


def _normalize(line: str) -> str:
    # Python argv lists (`["docker", "system", "prune"]`) read like shell.
    return re.sub(r"\s+", " ", re.sub(r"[\"',\[\]()]", " ", line)) + " "


def is_broad_sweep(line: str) -> Sweep | None:
    text = _normalize(line)
    if _DOCKER_PRUNE.search(text) or _DOCKER_IMAGE_ALL.search(text):
        return Sweep("docker prune", "bosn (`bosn gc` / `bosn manifest volume-gc` / `bosn ci runners prune-cache`)")
    rm = _RM_RF.search(text)
    if rm is not None:
        args = rm.group(1) + " "
        if _CARGO_HOME.search(args):
            return Sweep("rm -rf of CARGO_HOME", "soldr (`soldr gc`)")
        if _SOLDR_BOSN.search(args):
            return Sweep("rm -rf of a soldr/bosn state dir", "the owning tool (`soldr gc` / soldr broker reaper / bosn gc)")
        if _TARGET_SWEEP.search(args):
            return Sweep("cross-repo rm -rf of target/ dirs", "soldr (`soldr gc`)")
    if _FIND_MTIME_DELETE.search(text):
        return Sweep("mtime-only find -delete", "the owning reclaimer (bosn / `soldr gc` / local-gate retention)")
    return None


def _scanned(rel: str) -> bool:
    if rel in ROOT_FILES:
        return True
    if Path(rel).suffix not in SCRIPT_SUFFIXES:
        return False
    return rel.startswith(SCRIPT_DIRS) or any(part in TOOL_DIR_NAMES for part in rel.split("/")[:-1])


def scanned_files(repo_root: Path) -> list[str]:
    return sorted(rel for rel in list_repo_files(repo_root) if _scanned(rel) and (repo_root / rel).is_file())


def _comment_only(line: str) -> bool:
    return line.lstrip().startswith("#")


def check_file(repo_root: Path, rel: str) -> list[Finding]:
    try:
        lines = (repo_root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    findings: list[Finding] = []
    for number, line in enumerate(lines, start=1):
        if _comment_only(line) or allowed(line, RULE):
            continue
        sweep = is_broad_sweep(line)
        if sweep is None:
            continue
        findings.append(
            Finding(
                rule=RULE,
                path=rel,
                line=number,
                status=Status.VIOLATION,
                message=f"broad host sweep ({sweep.kind}) in dev-host tooling: it cannot tell what is in use and "
                f"kills other sessions' runs on a shared host",
                fix=f"remove the sweep and let its owner reclaim it: {sweep.owner}; local-gate keeps its own last "
                f"20 lane-log runs. A deliberate recovery step takes a same-line `# ci-lint: allow {RULE} <reason>` ({POLICY})",
            )
        )
    return findings


def check_gen_030(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel in scanned_files(repo_root):
        findings.extend(check_file(repo_root, rel))
    return findings
