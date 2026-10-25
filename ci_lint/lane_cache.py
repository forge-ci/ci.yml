"""GATE-007: the local lane result cache (zackees/ci.yml#177).

A local gate split into lanes (`[gate.lanes.<id>]`) may skip a lane only
when everything the lane can read is byte-identical to a run of that same
lane that passed. "Everything the lane can read" is decided by exclusion,
never by inclusion:

- **Input set** = every path in the commit's tree, minus the lane's
  `exclude` globs, plus the lane's `keep` globs (re-included despite an
  exclusion), plus the *mandatory* inputs no exclusion can drop: the gate
  declaration file, every repository file named in the gate's or the
  lane's argv, lockfiles and toolchain pins. A forgotten exclusion costs a
  rerun; a forgotten include could silently skip required work, so
  include lists are not offered.
- **Key** = sha256 over a versioned header, the lane's argv, the versions
  of the tools the lane declares (`<tool> --version`), the values of its
  declared env vars, and `(path, blob sha)` for every input -- read from
  `git ls-tree`, so hashing costs one git call, not a file read.
- **Only passes are recorded**, under `<git common dir>/ci-lint/lane-cache/`
  (shared by every worktree of the clone, never pushed). An entry older
  than the lane's `max-age-hours` (default 24, GEN-021's freshness bound)
  is ignored, so environment drift (registries, toolchains, the network)
  cannot hide behind an old pass forever.
- **Provenance**: the gate records each lane as `run` or `reused@<key>` in
  the `Local-Gate:` trailer, so a reused lane is visible in the commit.

The cache is local only. CI's mirrored jobs always run (GATE-001); skipping
on the remote side is GEN-021's verified reuse, a different mechanism.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.freshness import DEFAULT_MAX_AGE_HOURS, fresh
from ci_lint.execution_pins import ExecutionPins
from ci_lint.globs import glob_to_regex
from ci_lint.proc import run_captured
from ci_lint.toml_cursor import Cursor, TomlValue

# v2 (#401): a lane that does not run ci-lint hashes ci.toml SEMANTICALLY
# (parsed, minus the top-level `linter` pin, sorted-key JSON) instead of by
# blob sha, so a pin-only bump stops invalidating every lane on the machine.
# Old v1 entries are not reusable across the semantics change.
KEY_VERSION = "gate-007/v2"
# EX_TEMPFAIL: an `optional` lane's way to say "cannot run on this host".
NOT_APPLICABLE_EXIT = 75
LANE_WEIGHTS: tuple[str, ...] = ("heavy", "light")
# Gate declarations: always a lane input, for every lane.
DECLARATION_BASENAMES: frozenset[str] = frozenset({"local-gate.toml", "ci.toml"})


@dataclass(frozen=True)
class EcosystemPins:
    """Lockfiles and toolchain pins a lane must treat as inputs when it
    drives any of `tools` (design revision 3, #177: soldr's Python-lint lane
    was invalidated by every Cargo.lock bump it cannot be affected by).

    `globs` are repository-path patterns for pins that are not one file name
    (a CMake module tree, `conanfile.*`); `identity` names extra tools whose
    `--version` joins the key whenever the ecosystem matches -- the compiler
    a CMake lane resolves (`cc`/`c++`) without the lane naming it (#393)."""

    tools: frozenset[str]
    basenames: frozenset[str]
    globs: tuple[str, ...] = ()
    identity: tuple[str, ...] = ()
    # The subset of `tools` that compiles; `identity` is probed only for a
    # lane declaring one of these (a clang-format-only lane never compiles).
    identity_when: frozenset[str] = frozenset()


# C/C++ (CMake) tools a lane may declare (zackees/ci.yml#393, docs/policy-cpp.md).
CPP_TOOLS: frozenset[str] = frozenset(
    {
        "cmake", "ctest", "cpack", "ninja", "make", "gmake", "meson",
        "cc", "c++", "gcc", "g++", "clang", "clang++", "clang-cl", "cl",
        "clang-format", "clang-tidy", "gersemi", "ccache", "zccache", "sccache",
        "vcpkg", "conan",
    }
)  # fmt: skip
CPP_PIN_BASENAMES: frozenset[str] = frozenset(
    {
        "CMakeLists.txt", "CMakePresets.json", "CMakeUserPresets.json",
        "vcpkg.json", "vcpkg-configuration.json", "conanfile.txt", "conanfile.py",
        "buildspec.json", ".gitmodules", ".clang-format", ".clang-tidy",
        ".gersemirc", "toolchain.cmake",
    }
)  # fmt: skip
# Root-anchored: `cmake/**` is the conventional CMake module directory; the
# `*.cmake` glob catches modules and toolchain files kept anywhere else.
CPP_PIN_GLOBS: tuple[str, ...] = ("cmake/**", "**/*.cmake", "**/conanfile.*")
CPP_COMPILER_IDENTITY: tuple[str, ...] = ("cc", "c++")
CPP_FORMAT_TOOLS: frozenset[str] = frozenset({"clang-format", "gersemi"})

ECOSYSTEM_PINS: tuple[EcosystemPins, ...] = (
    EcosystemPins(
        frozenset({"soldr", "cargo", "rustc", "rustup", "maturin"}),
        frozenset({"Cargo.lock", "rust-toolchain.toml", "rust-toolchain"}),
    ),
    EcosystemPins(
        frozenset({"uv", "uvx", "python", "python3", "pip", "poetry", "maturin"}),
        frozenset({"uv.lock", "poetry.lock", ".python-version"}),
    ),
    EcosystemPins(
        frozenset({"node", "npm", "npx", "pnpm", "yarn"}),
        frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}),
    ),
    EcosystemPins(CPP_TOOLS, CPP_PIN_BASENAMES, CPP_PIN_GLOBS, CPP_COMPILER_IDENTITY, CPP_TOOLS - CPP_FORMAT_TOOLS),
)
ALL_PIN_BASENAMES: frozenset[str] = frozenset().union(*(e.basenames for e in ECOSYSTEM_PINS))


def _matched_ecosystems(tools: tuple[str, ...]) -> tuple[EcosystemPins, ...]:
    return tuple(e for e in ECOSYSTEM_PINS if e.tools & set(tools))


def pin_basenames(tools: tuple[str, ...]) -> frozenset[str]:
    """The pins a lane's tools make mandatory. A lane that declares no
    recognized tool gets every pin: the safe default when the gate cannot
    tell what the lane resolves."""

    matched = [e.basenames for e in _matched_ecosystems(tools)]
    if not matched:
        return ALL_PIN_BASENAMES
    return frozenset().union(*matched)


def pin_globs(tools: tuple[str, ...]) -> tuple[str, ...]:
    """Path-pattern pins (`cmake/**`, `**/*.cmake`, ...), with the same
    every-pin default as `pin_basenames` for a lane with no recognized tool."""

    matched = _matched_ecosystems(tools) or ECOSYSTEM_PINS
    return tuple(g for e in matched for g in e.globs)


def key_tools(tools: tuple[str, ...]) -> tuple[str, ...]:
    """The tools whose `--version` a lane's key covers: the declared ones
    plus each matched ecosystem's identity tools (a compiling C/C++ lane's
    `cc`/`c++`, #393). Only for recognized compiling tools: an unknown-tool
    or format-only lane is not assumed to compile anything."""

    implied = {t for e in _matched_ecosystems(tools) if e.identity_when & set(tools) for t in e.identity}
    return tuple(sorted(set(tools) | implied))


@dataclass(frozen=True)
class LaneConfig:
    id: str
    run: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS
    # `light` lanes run concurrently with each other and with the heavy
    # chain; `heavy` lanes (the default: compilers, test suites) run one at a
    # time, in declared order, so they do not fight over CPU.
    weight: str = "heavy"
    # Heavy lanes in different groups run as independent concurrent chains
    # (#399); each group is serial in declared order and stops at its own
    # first failure. Only for lanes that share no build directory or other
    # exclusive resource (e.g. a test suite in an isolated container beside
    # host compiles). "" is the default group.
    group: str = ""
    # A lane that cannot run on every host (a local VM, an emulator): it may
    # exit NOT_APPLICABLE_EXIT to say "not on this host". It is then never
    # cached and never attested (omission = not run), and the gate passes.
    optional: bool = False


def parse_lanes(raw: dict[str, TomlValue], *, path: str, source: str, findings: list[Finding]) -> tuple[LaneConfig, ...]:
    lanes: list[LaneConfig] = []
    for lane_id, body in raw.items():
        lane_path = f"{path}.{lane_id}"
        if not isinstance(body, dict):
            findings.append(
                Finding(rule="CT-002", path=source, message=f"'{lane_path}' must be a table",
                        fix=f"declare [{lane_path}] with run = [...]")
            )
            continue
        sub = Cursor(body, lane_path, findings, source)
        run = sub.list_str("run")
        exclude = sub.list_str("exclude", required=False)
        keep = sub.list_str("keep", required=False)
        tools = sub.list_str("tools", required=False)
        env = sub.list_str("env", required=False)
        max_age = sub.int_("max-age-hours", required=False)
        weight = sub.str_("weight", required=False, default="heavy") or "heavy"
        optional = bool(sub.bool_("optional", required=False, default=False))
        group = sub.str_("group", required=False, default=None)
        sub.finish()
        if weight not in LANE_WEIGHTS:
            findings.append(
                Finding(rule="GATE-007", path=source, message=f"'{lane_path}.weight' is {weight!r}",
                        fix=f"set it to one of {', '.join(LANE_WEIGHTS)}")
            )
            weight = "heavy"
        if group is not None and (not group.strip() or weight == "light"):
            findings.append(
                Finding(rule="GATE-007", path=source,
                        message=f"'{lane_path}.group' is {group!r} on a {weight} lane",
                        fix="name a non-empty group, and only on heavy lanes (light lanes already run concurrently)")
            )
            group = None
        if not run:
            continue
        lanes.append(
            LaneConfig(
                id=lane_id,
                run=run,
                exclude=exclude,
                keep=keep,
                tools=tools,
                env=env,
                max_age_hours=float(max_age) if max_age is not None else DEFAULT_MAX_AGE_HOURS,
                weight=weight,
                group=group or "",
                optional=optional,
            )
        )
    return tuple(lanes)


# ── input set and key ────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    proc = run_captured(["git", "-C", str(repo), *args])
    if not proc.ok:
        raise subprocess.CalledProcessError(proc.returncode, ["git", *args], proc.stdout, proc.stderr)
    return proc.stdout


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One path in a commit's tree and the sha of its blob (or gitlink)."""

    path: str
    sha: str


def tree_entries(repo: Path, tree: str) -> list[TreeEntry]:
    """Every blob/gitlink in `tree`, sorted by path."""

    out: list[TreeEntry] = []
    for line in _git(repo, "ls-tree", "-r", "-z", "--full-tree", tree).split("\0"):
        if not line:
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            out.append(TreeEntry(path=path, sha=parts[2]))
    return sorted(out)


def tree_file(repo: Path, tree: str, path: str) -> bytes | None:
    """The blob bytes of `path` inside `tree`, or `None` when the tree has
    no such path. Lets `lane_key` hash a file's *content* semantically
    (#401) while every other input keeps its `(path, blob sha)` form."""

    try:
        return _git(repo, "cat-file", "blob", f"{tree}:{path}").encode()
    except subprocess.CalledProcessError:
        return None


# The invocation shapes that make a lane execute ci-lint itself: a direct
# `ci-lint`/`ci_lint` token, or the pinned `...ci.yml@<sha>` package form
# (`uvx --from git+https://github.com/zackees/ci.yml@<sha> ci-lint ...`).
_CI_LINT_TOKENS = ("ci-lint", "ci_lint", "ci.yml@")


def lane_runs_ci_lint(lane: LaneConfig) -> bool:
    """#401: does this lane execute ci-lint, and therefore depend on
    ci.toml's top-level `linter` pin? Detected from the lane's own argv and
    declared tools: a `ci-lint`/`ci_lint` token, or the pinned
    `...ci.yml@<sha>` package form (`uvx --from git+.../ci.yml@<sha>
    ci-lint ...`) -- the invocation shapes soldr#3556's consumers use. This
    is an argv heuristic, so a lane that reaches ci-lint some other way
    would miss a pin-only invalidation; that residual is the known cost of
    the decided design (#401: semantic hashing, not a per-lane knob the
    consumer could get wrong)."""

    return any(token in part for part in (*lane.run, *lane.tools) for token in _CI_LINT_TOKENS)


def semantic_ci_toml_sha(data: bytes) -> str:
    """#401: ci.toml's key contribution for a lane that does not run
    ci-lint: sha256 over the parsed document minus its top-level `linter`
    pin, as sorted-key JSON (datetimes stringified). A pin-only bump then
    leaves the key untouched; any other field change still changes it.
    An unparseable document hashes its bytes instead -- fail closed to
    byte sensitivity rather than pretend we understood the file."""

    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return hashlib.sha256(data).hexdigest()
    parsed.pop("linter", None)
    return hashlib.sha256(json.dumps(parsed, sort_keys=True, default=str).encode()).hexdigest()


def mandatory_paths(lane: LaneConfig, gate_run: tuple[str, ...], gate_source: str, paths: set[str]) -> set[str]:
    named = {posixpath.normpath(token.replace("\\", "/")) for token in (*gate_run, *lane.run)} & paths
    named.add(gate_source)
    always = DECLARATION_BASENAMES | pin_basenames(lane.tools)
    named.update(p for p in paths if p.rsplit("/", 1)[-1] in always)
    globs = [glob_to_regex(g) for g in pin_globs(lane.tools)]
    if globs:
        named.update(p for p in paths if any(r.match(p) for r in globs))
    return named & paths


@dataclass
class _Matcher:
    exclude: list[re.Pattern[str]]
    keep: list[re.Pattern[str]]

    @staticmethod
    def of(lane: LaneConfig) -> _Matcher:
        return _Matcher([glob_to_regex(p) for p in lane.exclude], [glob_to_regex(p) for p in lane.keep])

    def excluded(self, path: str) -> bool:
        return any(r.match(path) for r in self.exclude) and not any(r.match(path) for r in self.keep)


def lane_inputs(
    entries: list[TreeEntry], lane: LaneConfig, gate_run: tuple[str, ...], gate_source: str
) -> list[TreeEntry]:
    mandatory = mandatory_paths(lane, gate_run, gate_source, {e.path for e in entries})
    matcher = _Matcher.of(lane)
    return [e for e in entries if e.path in mandatory or not matcher.excluded(e.path)]


@dataclass(frozen=True)
class ToolVersion:
    tool: str
    version: str


@dataclass
class ToolVersions:
    """`<tool> --version`, memoized per gate run. A missing tool records
    `missing`, so installing it later changes the key."""

    known: list[ToolVersion] = field(default_factory=list)

    def get(self, tool: str) -> ToolVersion:
        for item in self.known:
            if item.tool == tool:
                return item
        exe = shutil.which(tool)
        if exe is None:
            version = "missing"
        else:
            proc = run_captured([exe, "--version"], timeout=60)
            text = (proc.stdout + proc.stderr).strip()
            # soldr prints a delegation notice before its version; keep version-shaped lines only.
            lines = [ln for ln in text.splitlines() if re.search(r"\d+\.\d+", ln) and "delegating" not in ln]
            version = (lines[-1] if lines else text).strip()
        item = ToolVersion(tool=tool, version=version)
        self.known.append(item)
        return item


@dataclass(frozen=True)
class EnvValue:
    name: str
    value: str


@dataclass(frozen=True)
class KeyHeader:
    """Everything but the input files that a lane's key covers; serialized
    to JSON only at the hashing boundary."""

    v: str
    run: tuple[str, ...]
    tools: tuple[ToolVersion, ...]
    env: tuple[EnvValue, ...]
    provider: ExecutionPins | None = None


@dataclass(frozen=True)
class LaneKey:
    key: str
    inputs: int
    tools: tuple[ToolVersion, ...]


def lane_key(
    entries: list[TreeEntry],
    lane: LaneConfig,
    *,
    gate_run: tuple[str, ...],
    gate_source: str,
    versions: ToolVersions,
    environ: Mapping[str, str] | None = None,
    provider: ExecutionPins | None = None,
    ci_toml: bytes | None = None,
) -> LaneKey:
    """... (#401): when the caller supplies `ci_toml` (the tree's own
    ci.toml bytes) and the lane does not run ci-lint, ci.toml contributes
    its semantic digest (minus the `linter` pin) instead of its blob sha.
    No `ci_toml` argument -> byte hashing, exactly as before."""

    env = os.environ if environ is None else environ
    inputs = lane_inputs(entries, lane, gate_run, gate_source)
    header = KeyHeader(
        v=KEY_VERSION,
        run=lane.run,
        tools=tuple(versions.get(tool) for tool in key_tools(lane.tools)),
        env=tuple(EnvValue(name, env.get(name, "")) for name in sorted(set(lane.env))),
        provider=provider,
    )
    document = asdict(header)
    if provider is None:
        document.pop("provider")
    h = hashlib.sha256(json.dumps(document, sort_keys=True).encode())
    ci_sha = semantic_ci_toml_sha(ci_toml) if ci_toml is not None and not lane_runs_ci_lint(lane) else None
    for entry in inputs:
        h.update(b"\0")
        h.update(entry.path.encode())
        h.update(b"\0")
        h.update((ci_sha if ci_sha is not None and entry.path == "ci.toml" else entry.sha).encode())
    return LaneKey(key=h.hexdigest(), inputs=len(inputs), tools=header.tools)


# ── the cache itself ─────────────────────────────────────────────────────────


def lane_log_dir(repo: Path) -> Path:
    """Per-worktree (`git rev-parse --git-dir`), so two worktrees running the
    gate at once never write the same log."""

    git_dir = Path(_git(repo, "rev-parse", "--git-dir").strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    return git_dir / "ci-lint" / "lane-logs"


def cache_dir(repo: Path) -> Path:
    common = Path(_git(repo, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = repo / common
    return common / "ci-lint" / "lane-cache"


@dataclass(frozen=True)
class CacheEntry:
    lane: str
    key: str
    passed_at: float
    secs: int
    head: str
    tree: str

    def age_hours(self, now: float) -> float:
        return (now - self.passed_at) / 3600.0


def lookup(repo: Path, lane: LaneConfig, key: str, *, now: float | None = None) -> CacheEntry | None:
    path = cache_dir(repo) / lane.id / f"{key}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("key") != key or raw.get("lane") != lane.id:
        return None
    passed_at = raw.get("passed_at")
    secs = raw.get("secs")
    if not isinstance(passed_at, (int, float)) or not isinstance(secs, int):
        return None
    current = time.time() if now is None else now
    if not fresh(passed_at, current, lane.max_age_hours * 3600):
        return None
    return CacheEntry(lane.id, key, float(passed_at), secs, str(raw.get("head", "")), str(raw.get("tree", "")))


def record(repo: Path, lane: LaneConfig, key: str, *, secs: int, head: str, tree: str) -> Path:
    directory = cache_dir(repo) / lane.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"lane": lane.id, "key": key, "passed_at": time.time(), "secs": secs, "head": head, "tree": tree}),
        encoding="utf-8",
    )
    tmp.replace(path)
    prune(directory, keep=64)
    return path


def prune(directory: Path, *, keep: int) -> None:
    """Keep the newest `keep` entries per lane; the rest can never be
    looked up again in practice (a key only recurs for an identical tree)."""

    entries = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in entries[keep:]:
        stale.unlink(missing_ok=True)


# ── static checks (GATE-007) ─────────────────────────────────────────────────


def check_lanes_static(
    lanes: tuple[LaneConfig, ...], gate_run: tuple[str, ...], gate_source: str, repo_root: Path
) -> list[Finding]:
    findings: list[Finding] = []
    if not lanes:
        return findings
    try:
        entries = tree_entries(repo_root, "HEAD")
    except subprocess.CalledProcessError:
        return [Finding(rule="GATE-007", path=gate_source, status=Status.NEEDS_REVIEW,
                        message="cannot list HEAD's tree to check lane exclusions", fix="run inside a git checkout")]
    paths = {e.path for e in entries}
    for lane in lanes:
        if tuple(lane.run[: len(gate_run)]) != gate_run:
            findings.append(
                Finding(
                    rule="GATE-007",
                    path=gate_source,
                    message=f"lane '{lane.id}' runs `{' '.join(lane.run)}`, which is not the gate command "
                    f"`{' '.join(gate_run)}` plus arguments",
                    fix="make every lane a slice of the one gate command (e.g. `<gate> --lane <id>`), so the union "
                    "of the lanes is the gate the remote jobs mirror",
                )
            )
        # Mandatory inputs are re-included at runtime whatever the globs say, so
        # a broad glob that happens to cover one (`**/*.py` over the gate
        # script) is harmless. An exclusion that *names* one is a statement of
        # intent to drop it, and that is what gets reported.
        mandatory = mandatory_paths(lane, gate_run, gate_source, paths)
        dropped = sorted(p for p in lane.exclude if not any(c in p for c in "*?[") and p in mandatory)
        if dropped:
            findings.append(
                Finding(
                    rule="GATE-007",
                    path=gate_source,
                    message=f"lane '{lane.id}' excludes mandatory inputs: {', '.join(dropped[:8])}",
                    fix="narrow the exclusion (or add a `keep` glob): the gate script, its declaration, lockfiles "
                    "and toolchain pins are always lane inputs",
                )
            )
        if not lane.tools:
            findings.append(
                Finding(
                    rule="GATE-007",
                    path=gate_source,
                    status=Status.NEEDS_REVIEW,
                    message=f"lane '{lane.id}' declares no `tools`, so a tool upgrade cannot invalidate its cache",
                    fix="list the executables the lane drives (e.g. tools = [\"soldr\", \"uv\"])",
                )
            )
    ids = [lane.id for lane in lanes]
    if len(set(ids)) != len(ids):
        findings.append(Finding(rule="GATE-007", path=gate_source, message="duplicate lane ids", fix="give each lane a unique id"))
    return findings


# ── history simulation (tuning exclusions from real commits) ─────────────────


@dataclass(frozen=True)
class LaneSimulation:
    lane: str
    commits: int
    reusable: int
    # The paths that most often forced this lane to run, most frequent first.
    top_triggers: tuple[str, ...]

    @property
    def rate(self) -> float:
        return self.reusable / self.commits if self.commits else 0.0


def simulate(
    repo: Path, lanes: tuple[LaneConfig, ...], gate_run: tuple[str, ...], gate_source: str, *, commits: int
) -> list[LaneSimulation]:
    """For each of the last `commits` first-parent commits of HEAD, would
    each lane's inputs have been unchanged from the commit's parent? That is
    the best case for the cache (a pass of the parent recorded), which makes
    it the right signal for tuning `exclude`: a lane that is rarely reusable
    either reads everything or excludes too little."""

    shas = _git(repo, "rev-list", "--first-parent", f"--max-count={commits}", "HEAD").split()
    paths = {e.path for e in tree_entries(repo, "HEAD")}
    reusable = {lane.id: 0 for lane in lanes}
    triggers: dict[str, dict[str, int]] = {lane.id: {} for lane in lanes}
    counted = 0
    for sha in shas:
        try:
            changed = [p for p in _git(repo, "diff", "--name-only", f"{sha}^", sha).splitlines() if p]
        except subprocess.CalledProcessError:
            continue  # root commit
        counted += 1
        for lane in lanes:
            matcher = _Matcher.of(lane)
            mandatory = mandatory_paths(lane, gate_run, gate_source, paths | set(changed))
            hits = [p for p in changed if p in mandatory or not matcher.excluded(p)]
            if not hits:
                reusable[lane.id] += 1
            for p in hits:
                top = p.split("/", 2)
                bucket = "/".join(top[:2]) if len(top) > 2 else p
                triggers[lane.id][bucket] = triggers[lane.id].get(bucket, 0) + 1
    out: list[LaneSimulation] = []
    for lane in lanes:
        ranked = sorted(triggers[lane.id].items(), key=lambda kv: -kv[1])[:5]
        out.append(LaneSimulation(lane.id, counted, reusable[lane.id], tuple(f"{p} ({n})" for p, n in ranked)))
    return out



# ── exclusion audit (design revision 4, #177) ────────────────────────────────


@dataclass(frozen=True)
class TraceAudit:
    """Tracked files a lane opened while running under `strace`, and those
    of them its exclusions would have dropped (each one an unsafe
    exclusion: the lane's result can depend on a file its key ignores)."""

    lane: str
    opened: int
    excluded_reads: tuple[str, ...]


_TRACE_PATH = re.compile(r'"(/[^"]+)"')


def audit_trace(
    repo_root: Path, lane: LaneConfig, gate_run: tuple[str, ...], gate_source: str, trace_text: str
) -> TraceAudit:
    """Only successful opens/execs of *tracked* files count. Reads made by
    processes outside the traced tree -- a build daemon, a container -- are
    invisible here, so a clean audit proves nothing about them."""

    root = str(repo_root.resolve()).rstrip("/") + "/"
    tracked = {e.path for e in tree_entries(repo_root, "HEAD")}
    opened: set[str] = set()
    for line in trace_text.splitlines():
        if " = -1 " in line:
            continue
        for path in _TRACE_PATH.findall(line):
            if path.startswith(root):
                rel = path[len(root):]
                if rel in tracked:
                    opened.add(rel)
    mandatory = mandatory_paths(lane, gate_run, gate_source, tracked)
    matcher = _Matcher.of(lane)
    bad = tuple(sorted(p for p in opened if p not in mandatory and matcher.excluded(p)))
    return TraceAudit(lane=lane.id, opened=len(opened), excluded_reads=bad)


def run_audit(repo_root: Path, lane: LaneConfig, gate_run: tuple[str, ...], gate_source: str) -> TraceAudit | None:
    """Run the lane under `strace -f` and audit it; None when strace is absent."""

    strace = shutil.which("strace")
    if strace is None:
        return None
    trace_file = cache_dir(repo_root).parent / f"audit-{lane.id}.trace"
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [strace, "-f", "-qq", "-e", "trace=openat,open,execve,stat,lstat,newfstatat,statx", "-o", str(trace_file), *lane.run],
        cwd=repo_root,
        check=False,
    )
    try:
        text = trace_file.read_text(encoding="utf-8", errors="replace")
    finally:
        trace_file.unlink(missing_ok=True)
    return audit_trace(repo_root, lane, gate_run, gate_source, text)
