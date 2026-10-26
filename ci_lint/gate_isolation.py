"""GATE-005: a self-hosted tool's test suite runs isolated, never on the
developer host (zackees/ci.yml#168, evidence zackees/soldr#3516).

Some fleet repositories build a tool that is also live infrastructure on the
developer's own machine (soldr, zccache, clud, bosn). Their tests start that
tool's daemons and touch its state roots; one leaked fixture on a host run
claimed the real `~/.soldr` root and wedged every soldr build on the machine.
So for these repositories "run it locally first" must mean "run it in an
isolated container", and the suite itself must refuse anything else.

Declared as `[gate.isolation]` (`local-gate.toml`) or
`[local.gate.isolation]` (`ci.toml`):

    marker = "SOLDR_TEST_ISOLATED"            # set only by the isolated environment
    runner = ["bosn", "run", "--task", "test"] # how the local gate runs the suite
    guard  = ".github/scripts/nextest_wrapper.sh"  # where the suite refuses otherwise

Static checks, all GATE-005:

1. The guard file exists and references both the marker and `CI` (GitHub
   runners and act set `CI=true`): the suite is self-refusing on a host.
2. A `bosn` runner names a task that exists in `bosn.toml`, and that task's
   command or its stack's Dockerfile sets the marker.
3. The local gate's own script (a repository file named in the gate's
   `run` argv) invokes the runner, so the gate really runs the suite
   isolated rather than skipping it.
4. A repository whose `origin` is a known self-hosted tool and that declares
   no isolation is `needs_review`.

The guard is the runtime enforcement; this module only proves it is wired.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.lane_cache import LaneConfig
from ci_lint.proc import run_captured
from ci_lint.toml_cursor import Cursor, TomlValue
from ci_lint.workflow_replay_config import ReplayConfig

SELF_HOSTED_TOOL_REPOS: frozenset[str] = frozenset(
    {"zackees/soldr", "zackees/zccache", "zackees/clud", "zackees/bosn",
     "forge-ci/soldr", "forge-ci/zccache", "talos-hq/clud", "forge-ci/bosn"}
)
_ORIGIN_RE = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$")


NONCE_FILE = ".gate-nonce"
NONCE_ECHO = "gate-nonce: "


@dataclass(frozen=True)
class IsolationConfig:
    marker: str
    runner: tuple[str, ...]
    guard: str
    # GATE-009 (zackees/ci.yml#196): the gate writes a fresh nonce to
    # NONCE_FILE in its worktree and the isolated entry script echoes
    # `NONCE_ECHO<contents>` from its mounted tree, so a runner bound to
    # another checkout (zackees/bosn#314) fails instead of attesting it.
    proves_tree: bool = False


def parse_isolation(raw: dict[str, TomlValue], *, path: str, source: str, findings: list[Finding]) -> IsolationConfig | None:
    sub = Cursor(raw, path, findings, source)
    marker = sub.str_("marker")
    runner = sub.list_str("runner")
    guard = sub.str_("guard")
    proves_tree = sub.bool_("proves-tree", required=False, default=False)
    sub.finish()
    if not marker or not runner or not guard:
        return None
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", marker):
        findings.append(
            Finding(rule="GATE-005", path=source, message=f"{path}.marker {marker!r} is not an environment variable name",
                    fix="use an upper-case variable name such as SOLDR_TEST_ISOLATED")
        )
        return None
    return IsolationConfig(marker=marker, runner=runner, guard=guard, proves_tree=bool(proves_tree))


def origin_slug(repo_root: Path) -> str | None:
    proc = run_captured(["git", "-C", str(repo_root), "remote", "get-url", "origin"])
    match = _ORIGIN_RE.search(proc.stdout.strip()) if proc.returncode == 0 else None
    return match.group("slug").lower() if match else None


def _invokes(text: str, runner: tuple[str, ...]) -> bool:
    if " ".join(runner) in re.sub(r"\s+", " ", text):
        return True
    # A Python/TOML argv: every token as a string literal, in order.
    pos = 0
    for token in runner:
        match = re.compile(rf"""["']{re.escape(token)}["']""").search(text, pos)
        if match is None:
            return False
        pos = match.end()
    return True


def _bosn_marker_findings(iso: IsolationConfig, repo_root: Path, source: str) -> list[Finding]:
    tokens = list(iso.runner)
    task = tokens[tokens.index("--task") + 1] if "--task" in tokens[:-1] else None
    manifest = repo_root / "bosn.toml"
    if task is None:
        return [Finding(rule="GATE-005", path=source, message="the bosn runner names no --task",
                        fix="write the runner as ['bosn', 'run', '--task', '<task>']")]
    if not manifest.is_file():
        return [Finding(rule="GATE-005", path="bosn.toml", message="the isolation runner is bosn but bosn.toml is missing",
                        fix=f"declare [task.{task}] in bosn.toml")]
    try:
        doc = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [Finding(rule="GATE-005", path="bosn.toml", message=f"invalid TOML: {exc}", fix="fix bosn.toml")]
    tasks = doc.get("task")
    spec = tasks.get(task) if isinstance(tasks, dict) else None
    if not isinstance(spec, dict):
        return [Finding(rule="GATE-005", path="bosn.toml", message=f"bosn.toml has no [task.{task}]",
                        fix=f"declare [task.{task}] running the test suite")]
    cmd = spec.get("cmd")
    if isinstance(cmd, str) and f"{iso.marker}=" in cmd:
        return []
    stacks = doc.get("stack")
    stack = stacks.get(spec.get("stack")) if isinstance(stacks, dict) and isinstance(spec.get("stack"), str) else None
    dockerfile = stack.get("dockerfile") if isinstance(stack, dict) else None
    if isinstance(dockerfile, str) and (repo_root / dockerfile).is_file():
        text = _join_continuations((repo_root / dockerfile).read_text(encoding="utf-8", errors="replace"))
        if re.search(rf"^\s*ENV\s+(?:.*\s)?{re.escape(iso.marker)}[= ]", text, re.MULTILINE):
            return []
    return [
        Finding(
            rule="GATE-005",
            path="bosn.toml",
            message=f"bosn task '{task}' never sets {iso.marker}, so the isolated run would be refused by the guard too",
            fix=f"add `ENV {iso.marker}=1` to the task's stack Dockerfile (or prefix the task cmd with {iso.marker}=1)",
        )
    ]


def _join_continuations(text: str) -> str:
    """Fold Dockerfile backslash continuations (`ENV A=1 \\` + `    B=2`) into
    one logical line, so a marker set on a continuation line is seen."""

    return re.sub(r"\\[ \t]*\r?\n", " ", text)


def _repo_mount_destination(doc: dict[str, TomlValue], stack_name: TomlValue) -> str:
    """Container path the task's stack mounts the repository (`source = "."`)
    at; bosn's default is `/repo` (zccache mounts at `/work`)."""

    stacks = doc.get("stack")
    stack = stacks.get(stack_name) if isinstance(stacks, dict) and isinstance(stack_name, str) else None
    mounts = stack.get("mounts") if isinstance(stack, dict) else None
    if isinstance(mounts, dict):
        for mount in mounts.values():
            if isinstance(mount, dict) and mount.get("source") in (".", "./"):
                dest = mount.get("destination")
                if isinstance(dest, str) and dest.startswith("/"):
                    return dest.rstrip("/") or "/"
    return "/repo"


def _shared_replay(iso: IsolationConfig, gate_run: tuple[str, ...], replay: ReplayConfig | None,
                   lanes: tuple[LaneConfig, ...]) -> bool:
    """The shared runtime validates the original receipt for a direct runner.

    Its source and provider checks replace a repository's private receipt
    parser. An exit code alone, a file-forwarding wrapper or legacy replay
    cannot supply this proof.
    """
    return bool(_is_bosn_ci(iso.runner) and gate_run[:len(iso.runner)] == iso.runner
                and replay is not None and replay.qualified and replay.provider_query
                and replay.report_source == "stdout"
                and {lane.id for lane in lanes}.issubset({lane for job in replay.jobs for lane in job.lanes})
                and all(lane.run[:len(iso.runner)] == iso.runner for lane in lanes))


def check_isolation(isolation: IsolationConfig | None, gate_run: tuple[str, ...], repo_root: Path, source: str,
                    *, replay: ReplayConfig | None = None, lanes: tuple[LaneConfig, ...] = ()) -> list[Finding]:
    if isolation is None:
        slug = origin_slug(repo_root)
        if slug in SELF_HOSTED_TOOL_REPOS:
            return [
                Finding(
                    rule="GATE-005",
                    path=source,
                    status=Status.NEEDS_REVIEW,
                    message=f"{slug} builds a tool that is live infrastructure on developer hosts, but its local gate "
                    "declares no [gate.isolation]",
                    fix="declare marker/runner/guard so the test suite refuses to run on the host and the gate runs it isolated",
                )
            ]
        return []
    iso = isolation
    findings: list[Finding] = []
    guard = repo_root / iso.guard
    if not guard.is_file():
        findings.append(Finding(rule="GATE-005", path=iso.guard, message="the isolation guard file does not exist",
                                fix="point 'guard' at the file every test process passes through"))
    else:
        text = guard.read_text(encoding="utf-8", errors="replace")
        missing = [name for name in (iso.marker, "CI") if not re.search(rf"\b{re.escape(name)}\b", text)]
        if missing:
            findings.append(
                Finding(
                    rule="GATE-005",
                    path=iso.guard,
                    message=f"the guard never checks {' or '.join(missing)}, so the suite can still run on a developer host",
                    fix=f"refuse to start unless CI=true or {iso.marker}=1, naming the isolated command ({' '.join(iso.runner)})",
                )
            )
    if _is_bosn_ci(iso.runner):
        pass  # act sets CI=true inside every job, and the guard must honour CI
    elif iso.runner[0] == "bosn":
        findings.extend(_bosn_marker_findings(iso, repo_root, source))
    scripts = [repo_root / token for token in gate_run if (repo_root / token).is_file()]
    shared = _shared_replay(iso, gate_run, replay, lanes)
    if not (shared and iso.proves_tree):
        findings.extend(_tree_proof_findings(iso, scripts, repo_root, source))
    if not shared and not any(_invokes(s.read_text(encoding="utf-8", errors="replace"), iso.runner) for s in scripts):
        findings.append(
            Finding(
                rule="GATE-005",
                path=source,
                message=f"the local gate ({' '.join(gate_run)}) never invokes the isolation runner `{' '.join(iso.runner)}`",
                fix="run the test suite from the gate script through the isolation runner",
            )
        )
    return findings


def _bosn_entry_scripts(iso: IsolationConfig, repo_root: Path) -> list[Path]:
    """Repository files the bosn task's cmd runs (`<mount>/<path>` tokens,
    where `<mount>` is the stack's `source = "."` mount destination, default
    `/repo`, or plain relative paths that exist)."""

    tokens = list(iso.runner)
    task = tokens[tokens.index("--task") + 1] if "--task" in tokens[:-1] else None
    manifest = repo_root / "bosn.toml"
    if task is None or not manifest.is_file():
        return []
    try:
        doc = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return []
    tasks = doc.get("task")
    spec = tasks.get(task) if isinstance(tasks, dict) else None
    cmd = spec.get("cmd") if isinstance(spec, dict) else None
    if not isinstance(cmd, str):
        return []
    prefix = _repo_mount_destination(doc, spec.get("stack") if isinstance(spec, dict) else None) + "/"
    out: list[Path] = []
    for token in re.split(r"[\s;&|]+", cmd):
        rel = token[len(prefix):] if token.startswith(prefix) else token
        if rel and (repo_root / rel).is_file():
            out.append(repo_root / rel)
    return out


def _is_bosn_ci(runner: tuple[str, ...]) -> bool:
    """`bosn ci run ...`: bosn's GitHub-Actions engine (act), the sanctioned
    local runner where a repository forbids direct bosn tasks (zackees/clud)."""

    return len(runner) >= 3 and runner[0] == "bosn" and runner[1] == "ci" and runner[2] == "run"


RUN_RECORD_FIELDS: tuple[str, ...] = ("workspace", "sha", "dirty")


def _tree_proof_findings(iso: IsolationConfig, gate_scripts: list[Path], repo_root: Path, source: str) -> list[Finding]:
    if not iso.proves_tree:
        return [
            Finding(
                rule="GATE-009",
                path=source,
                status=Status.NEEDS_REVIEW,
                message="the isolated runner does not prove it ran this worktree; a warm container bound to another "
                "checkout (zackees/bosn#314) would let the gate attest a tree it never tested",
                fix=f"set [gate.isolation] proves-tree = true: the gate writes a nonce to {NONCE_FILE}, the isolated "
                f"entry script prints `{NONCE_ECHO}<contents>` from its mounted tree, and the gate requires a match",
            )
        ]
    findings: list[Finding] = []
    if _is_bosn_ci(iso.runner):
        # bosn ci snapshots the tree honouring .gitignore, so a gitignored
        # nonce never reaches the job; the proof is bosn's own run record,
        # which names the workspace, the commit and whether the snapshot was
        # dirty. The gate must check all three against itself.
        texts = [s.read_text(encoding="utf-8", errors="replace") for s in gate_scripts]
        if not any(all(f'"{field}"' in t for field in RUN_RECORD_FIELDS) for t in texts):
            findings.append(Finding(
                rule="GATE-009", path=source,
                message="proves-tree is set with a `bosn ci run` runner, but the gate script never checks the run "
                "record's workspace, sha and dirty fields",
                fix="after `bosn ci run`, read its run record and require workspace == this worktree, sha == HEAD and "
                "dirty == null"))
        return findings
    if not any(NONCE_FILE in s.read_text(encoding="utf-8", errors="replace") for s in gate_scripts):
        findings.append(Finding(rule="GATE-009", path=source,
                                message=f"proves-tree is set but the gate script never writes {NONCE_FILE}",
                                fix=f"write a fresh nonce to {NONCE_FILE} before the isolated run and require its echo"))
    entries = _bosn_entry_scripts(iso, repo_root) if iso.runner[0] == "bosn" else []
    if iso.runner[0] == "bosn" and not any(
        NONCE_FILE in e.read_text(encoding="utf-8", errors="replace")
        and NONCE_ECHO in e.read_text(encoding="utf-8", errors="replace") for e in entries
    ):
        findings.append(Finding(rule="GATE-009", path="bosn.toml",
                                message="proves-tree is set but the isolated task's entry script never echoes the nonce",
                                fix=f"print `{NONCE_ECHO}<contents of {NONCE_FILE}>` from the mounted tree first thing"))
    return findings
