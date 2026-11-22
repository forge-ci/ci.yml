"""ci-attestations: per-gate local evidence written into PR commit messages
(candidate GATE-010, zackees/ci.yml#198; pilot zackees/soldr#3534).

**Definition (YAML, commentable).** A repository-root `ci-attestations.yml`
declares the gates CI knows about, which local-gate lane proves each, and
which remote job each gate stands in for:

    version: 1
    gates:
      rust/x86_64-unknown-linux-gnu/test: {lane: tests}   # nextest + doctests
      rust/x86_64-unknown-linux-gnu/clippy: {lane: rust}
    jobs:
      ci.yml:build-linux-x64: [rust/x86_64-unknown-linux-gnu/test, rust/x86_64-unknown-linux-gnu/clippy]

Gate paths follow the platform schema pattern `<ecosystem>/<platform>/<check>`:
`rust/<target triple | all>/<check>` first, then `python/...`, then
`cpp/<os-arch | target triple | all>/<check>` (C/C++, CMake; zackees/ci.yml#393,
docs/policy-cpp.md), then `general/...`. The file is parsed with `ci_lint.mini_yaml` (restricted YAML,
standard library only), so a bare developer host can read it.

**Attestation (compact JSON, the transit form).** `ci-lint local-gate run`
writes one trailer per attested gate into the commit message -- never into
the tree:

    Ci-Attestation: {"at":...,"gate":"rust/x86_64-unknown-linux-gnu/test","host":"linux-x86_64","key":"9f3c...","lane":"tests","parents":["..."],"secs":506,"stamp":"...","tree":"...","v":1,"via":"run"}

The stamp is `sha256(tree \\n parents \\n canonical JSON without the stamp)`,
truncated to 32 hex: amending content, rebasing, or editing the JSON
breaks it (tamper-evident, not a signature). A commit's own hash covers its
message, so the stamp binds the tree and parents instead.

**Rules (owner decisions 2026-10-02).** Omission means not run. An entry
must name a declared gate. CI skips a declared remote job only when every
gate it lists is attested and valid *and* the head passes GATE-008's
head-level policy; the definition is read from the PR base. Release always
ignores attestations.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding
from ci_lint.freshness import DEFAULT_MAX_AGE_HOURS, fresh
from ci_lint.mini_yaml import MiniYamlError, parse
from ci_lint.proc import run_captured

DEFINITION_FILE = "ci-attestations.yml"
TRAILER_KEY = "Ci-Attestation"
VERSION = 1
STAMP_HEX = 32
ECOSYSTEMS: tuple[str, ...] = ("rust", "python", "cpp", "general")
FIDELITIES: tuple[str, ...] = ("native", "vm", "emulation")
_GATE = re.compile(r"^(?P<eco>[a-z]+)/(?P<platform>[A-Za-z0-9_.-]+)/(?P<check>[a-z0-9][a-z0-9-]*)$")
_TRIPLE = re.compile(r"^[a-z0-9_]+(-[a-z0-9_]+){2,3}$")
# C/C++ platforms (#393): `all`, `<os>-<arch>` (the CMake-preset / runner
# spelling, e.g. `linux-x64`, `windows-arm64`), or a target triple.
CPP_OSES: tuple[str, ...] = ("linux", "windows", "macos", "freebsd", "android", "ios")
CPP_ARCHES: tuple[str, ...] = ("x64", "x86", "arm64", "armv7", "riscv64", "universal")
_CPP_OS_ARCH = re.compile(rf"^(?:{'|'.join(CPP_OSES)})-(?:{'|'.join(CPP_ARCHES)})$")
_TRAILER = re.compile(rf"^{TRAILER_KEY}:\s*(?P<body>\{{.*\}})\s*$", re.MULTILINE)
_JOB = re.compile(r"^[^/:]+:[^:]+$")


@dataclass(frozen=True)
class GateDef:
    path: str
    lane: str
    # GATE-011 (zackees/ci.yml#202): how faithfully the lane reproduces the
    # gate's platform. `native` (default): the real platform or the same
    # host triple. `vm`: the real OS in a local VM (a dockur/windows guest,
    # a macOS Recovery guest). `emulation`: a translation layer (Wine,
    # Darling). Only native/vm may prove a `test` check.
    fidelity: str = "native"


@dataclass(frozen=True)
class JobGates:
    job: str  # `<workflow file>:<job id>`
    gates: tuple[str, ...]


@dataclass(frozen=True)
class Definition:
    version: int
    gates: tuple[GateDef, ...]
    jobs: tuple[JobGates, ...]
    source: str

    def gate(self, path: str) -> GateDef | None:
        for gate in self.gates:
            if gate.path == path:
                return gate
        return None

    def gates_of_lane(self, lane: str) -> tuple[GateDef, ...]:
        return tuple(g for g in self.gates if g.lane == lane)

    def job(self, job: str) -> JobGates | None:
        for entry in self.jobs:
            if entry.job == job:
                return entry
        return None


@dataclass(frozen=True)
class DefinitionLoad:
    definition: Definition | None
    findings: list[Finding]


def validate_gate_path(path: str) -> str | None:
    """None when `path` follows `<ecosystem>/<platform>/<check>`, else why not."""

    match = _GATE.match(path)
    if match is None:
        return "not '<ecosystem>/<platform>/<check>'"
    if match.group("eco") not in ECOSYSTEMS:
        return f"ecosystem '{match.group('eco')}' is not one of {', '.join(ECOSYSTEMS)}"
    plat = match.group("platform")
    if match.group("eco") == "rust" and plat != "all" and not _TRIPLE.match(plat):
        return f"Rust platform '{plat}' is not 'all' or a target triple"
    if match.group("eco") == "cpp" and plat != "all" and not (_CPP_OS_ARCH.match(plat) or _TRIPLE.match(plat)):
        return f"C/C++ platform '{plat}' is not 'all', '<os>-<arch>' (e.g. linux-x64) or a target triple"
    return None


def parse_definition(text: str, *, source: str = DEFINITION_FILE, lanes: tuple[str, ...] | None = None) -> DefinitionLoad:  # noqa: C901
    findings: list[Finding] = []

    def bad(message: str, fix: str) -> None:
        findings.append(Finding(rule="GATE-010", path=source, message=message, fix=fix))

    try:
        doc = parse(text)
    except MiniYamlError as exc:
        bad(f"not restricted YAML: {exc}", "use plain mappings, flow {..}/[..] of scalars and comments only")
        return DefinitionLoad(None, findings)
    if not isinstance(doc, dict):
        bad("the top level is not a mapping", "start with `version: 1`, `gates:` and `jobs:`")
        return DefinitionLoad(None, findings)
    # `lanes` and `mode` are prooft's: lane `run`/`stage`/`inputs`/`max-age-hours`
    # (the runner, GATE-014/017) and shadow/enforce. ci-lint reads them in
    # `ci_lint.gate_push_prooft`; here they are accepted, not interpreted.
    unknown = sorted(set(doc) - {"version", "gates", "jobs", "lanes", "mode"})
    if unknown:
        bad(f"unknown top-level key(s) {', '.join(unknown)}", "only version, mode, lanes, gates and jobs are defined")
    version = doc.get("version")
    if version != VERSION:
        bad(f"version is {version!r}", f"set `version: {VERSION}`")
    gates: list[GateDef] = []
    raw_gates = doc.get("gates")
    if not isinstance(raw_gates, dict) or not raw_gates:
        bad("`gates` is missing or empty", "declare at least one gate, e.g. `rust/x86_64-unknown-linux-gnu/test: {lane: tests}`")
        raw_gates = {}
    for path, body in raw_gates.items():
        why = validate_gate_path(path)
        if why is not None:
            bad(f"gate '{path}': {why}", "e.g. rust/all/clippy, rust/x86_64-unknown-linux-gnu/test")
            continue
        lane = body.get("lane") if isinstance(body, dict) else None
        if not isinstance(lane, str) or not lane:
            bad(f"gate '{path}' names no `lane`", "set `{lane: <local-gate lane id>}`")
            continue
        if isinstance(body, dict) and set(body) - {"lane", "fidelity"}:
            bad(f"gate '{path}' has unknown key(s) {', '.join(sorted(set(body) - {'lane', 'fidelity'}))}",
                "only `lane` and `fidelity` are defined")
        fidelity = body.get("fidelity", "native") if isinstance(body, dict) else "native"
        if fidelity not in FIDELITIES:
            findings.append(Finding(rule="GATE-011", path=source,
                                    message=f"gate '{path}' declares fidelity {fidelity!r}",
                                    fix=f"use one of {', '.join(FIDELITIES)}"))
            fidelity = "native"
        if fidelity == "emulation" and path.rsplit("/", 1)[-1] == "test":
            findings.append(Finding(
                rule="GATE-011", path=source,
                message=f"gate '{path}' is a `test` check proven by an emulation lane; emulation (Wine, Darling) "
                "cannot stand in for the platform's test suite",
                fix="attest a narrower, distinctly named check (e.g. `.../unit` for the crates that pass in full) or "
                "prove `test` with a native or vm lane"))
        if lanes is not None and lane not in lanes:
            bad(f"gate '{path}' names lane '{lane}', which the local gate does not declare",
                f"use one of: {', '.join(lanes) or '(no lanes declared)'}")
        gates.append(GateDef(path, lane, str(fidelity)))
    jobs: list[JobGates] = []
    raw_jobs = doc.get("jobs")
    if raw_jobs is not None and not isinstance(raw_jobs, dict):
        bad("`jobs` is not a mapping", "map `<workflow>:<job>` to a list of gate paths")
        raw_jobs = {}
    declared = {g.path for g in gates}
    for job, listed in (raw_jobs or {}).items():
        if not _JOB.match(job):
            bad(f"job '{job}' is not '<workflow file>:<job id>'", "e.g. ci.yml:build-linux-x64")
            continue
        if not isinstance(listed, list) or not listed or not all(isinstance(x, str) for x in listed):
            bad(f"job '{job}' must list at least one gate path", "e.g. [rust/x86_64-unknown-linux-gnu/test]")
            continue
        missing = [x for x in listed if x not in declared]
        if missing:
            bad(f"job '{job}' lists undeclared gate(s) {', '.join(missing)}", "declare them under `gates`")
        jobs.append(JobGates(job, tuple(str(x) for x in listed)))
    return DefinitionLoad(Definition(VERSION, tuple(gates), tuple(jobs), source), findings)


def _git(repo: Path, *args: str) -> str | None:
    out = run_captured(["git", "-C", str(repo), *args])
    return out.stdout if out.ok else None


def load_definition(repo: Path, *, rev: str | None = None, lanes: tuple[str, ...] | None = None) -> DefinitionLoad | None:
    """None when the repository (at `rev`, if given) has no definition."""

    if rev is None:
        path = repo / DEFINITION_FILE
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    else:
        shown = _git(repo, "show", f"{rev}:{DEFINITION_FILE}")
        if shown is None:
            return None
        text = shown
    return parse_definition(text, lanes=lanes)


# ── the attestation record ───────────────────────────────────────────────────


@dataclass(frozen=True)
class GateAttestation:
    gate: str
    tree: str
    parents: tuple[str, ...]
    lane: str
    key: str  # the GATE-007 lane key (inputs + tools + env), or "" for an unlaned gate
    via: str  # "run" | "reused"
    secs: int | None
    host: str
    at: int
    stamp: str = ""

    def body(self) -> dict[str, JsonValue]:
        return {
            "v": VERSION, "gate": self.gate, "tree": self.tree, "parents": list(self.parents), "lane": self.lane,
            "key": self.key, "via": self.via, "secs": self.secs, "host": self.host, "at": self.at,
        }

    def compute_stamp(self) -> str:
        canonical = json.dumps(self.body(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{self.tree}\n{','.join(self.parents)}\n{canonical}".encode()).hexdigest()
        return digest[:STAMP_HEX]

    def stamped(self) -> GateAttestation:
        return replace(self, stamp=self.compute_stamp())

    def compact(self) -> str:
        body = self.body()
        body["stamp"] = self.stamp
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def trailer(self) -> str:
        return f"{TRAILER_KEY}: {self.compact()}"


def host_label() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def make(gate: str, *, tree: str, parents: tuple[str, ...], lane: str, key: str, via: str,
         secs: int | None, at: int | None = None) -> GateAttestation:
    return GateAttestation(gate, tree, parents, lane, key, via, secs, host_label(),
                           int(time.time()) if at is None else at).stamped()


@dataclass(frozen=True)
class ParsedTrailer:
    attestation: GateAttestation | None
    error: str | None  # why the trailer is malformed


def _record(raw: dict[str, JsonValue]) -> GateAttestation:
    parents = raw.get("parents")
    secs = raw.get("secs")
    fields = (raw.get("gate"), raw.get("tree"), raw.get("lane"), raw.get("key"), raw.get("via"), raw.get("host"),
              raw.get("stamp"))
    if type(raw.get("v")) is not int or raw.get("v") != VERSION \
            or not all(isinstance(f, str) for f in fields) or not isinstance(parents, list) \
            or not all(isinstance(p, str) for p in parents) or type(raw.get("at")) is not int \
            or not (secs is None or type(secs) is int):
        raise ValueError("missing or mistyped field")
    gate, tree, lane, key, via, host, stamp = (str(f) for f in fields)
    at = raw.get("at")
    assert isinstance(at, int)
    return GateAttestation(gate, tree, tuple(str(p) for p in parents), lane, key, via,
                           secs if isinstance(secs, int) else None, host, at, stamp)


def parse_trailers(message: str) -> list[ParsedTrailer]:
    out: list[ParsedTrailer] = []
    for match in _TRAILER.finditer(message):
        try:
            raw = json.loads(match.group("body"))
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            out.append(ParsedTrailer(_record(raw), None))
        except (ValueError, json.JSONDecodeError) as exc:
            out.append(ParsedTrailer(None, f"malformed {TRAILER_KEY} trailer: {exc}"))
    return out


def strip_trailers(message: str) -> str:
    return "\n".join(line for line in message.splitlines() if not line.startswith(f"{TRAILER_KEY}:")) + "\n"


# ── verification ─────────────────────────────────────────────────────────────

VALID = "valid"
MISSING = "missing"  # omitted: not run locally, so CI runs it


@dataclass(frozen=True)
class GateStatus:
    gate: str
    state: str  # valid | missing | undeclared | bad-stamp | wrong-tree | wrong-parents | lane-mismatch | duplicate | stale
    detail: str


@dataclass(frozen=True)
class CommitAttestations:
    sha: str
    statuses: tuple[GateStatus, ...]
    malformed: tuple[str, ...]

    def state(self, gate: str) -> str:
        for status in self.statuses:
            if status.gate == gate:
                return status.state
        return MISSING

    @property
    def errors(self) -> tuple[GateStatus, ...]:
        return tuple(s for s in self.statuses if s.state not in (VALID, MISSING))


def verify_commit(repo: Path, sha: str, definition: Definition, *,  # noqa: C901
                  max_age_hours: Mapping[str, float] | None = None,
                  now: float | None = None) -> CommitAttestations:
    """Validate integrity, and optionally the consumer's lane freshness policy."""
    current = time.time() if now is None else now
    message = _git(repo, "log", "-1", "--format=%B", sha) or ""
    tree = (_git(repo, "rev-parse", f"{sha}^{{tree}}") or "").strip()
    parents = tuple((_git(repo, "log", "-1", "--format=%P", sha) or "").split())
    parsed = parse_trailers(message)
    seen: dict[str, int] = {}
    statuses: list[GateStatus] = []
    for item in parsed:
        att = item.attestation
        if att is None:
            continue
        seen[att.gate] = seen.get(att.gate, 0) + 1
        declared = definition.gate(att.gate)
        if declared is None:
            statuses.append(GateStatus(att.gate, "undeclared", "not a gate in the definition"))
        elif seen[att.gate] > 1:
            statuses.append(GateStatus(att.gate, "duplicate", "attested more than once"))
        elif att.compute_stamp() != att.stamp:
            statuses.append(GateStatus(att.gate, "bad-stamp", "stamp does not match the record"))
        elif att.tree != tree:
            statuses.append(GateStatus(att.gate, "wrong-tree", f"attests tree {att.tree[:12]}, commit has {tree[:12]}"))
        elif att.parents != parents:
            statuses.append(GateStatus(att.gate, "wrong-parents", "attested on other parents (rebased or cherry-picked)"))
        elif att.lane != declared.lane:
            statuses.append(GateStatus(att.gate, "lane-mismatch", f"proved by lane '{att.lane}', definition says '{declared.lane}'"))
        elif max_age_hours is not None and not fresh(
                att.at, current, max_age_hours.get(declared.lane, DEFAULT_MAX_AGE_HOURS) * 3600):
            statuses.append(GateStatus(att.gate, "stale", "execution timestamp is expired or outside the clock-skew bound"))
        else:
            statuses.append(GateStatus(att.gate, VALID, f"{att.via} by lane {att.lane} on {att.host}"))
    attested = {s.gate for s in statuses}
    for gate in definition.gates:
        if gate.path not in attested:
            statuses.append(GateStatus(gate.path, MISSING, "omitted: not run locally"))
    return CommitAttestations(sha, tuple(statuses), tuple(i.error for i in parsed if i.error))


@dataclass(frozen=True)
class JobDecision:
    job: str
    skip: bool
    reason: str


def decide_jobs(definition: Definition | None, skip_jobs: tuple[str, ...], *, head_trusted: bool,
                commit: CommitAttestations | None) -> tuple[JobDecision, ...]:
    """Per declared skip job: skip only when the head passed GATE-008's
    head-level policy and (with a definition) every gate the job lists is
    attested and valid. Without a definition the GATE-008 behavior holds."""

    out: list[JobDecision] = []
    for job in skip_jobs:
        if not head_trusted:
            out.append(JobDecision(job, False, "head not trusted (GATE-008)"))
            continue
        if definition is None or commit is None:
            out.append(JobDecision(job, True, "trusted head; no ci-attestations.yml on the base"))
            continue
        mapped = definition.job(job)
        if mapped is None:
            out.append(JobDecision(job, False, f"{job} is not mapped to gates in {DEFINITION_FILE}"))
            continue
        lacking = [g for g in mapped.gates if commit.state(g) != VALID]
        if lacking:
            states = ", ".join(f"{g}={commit.state(g)}" for g in lacking)
            out.append(JobDecision(job, False, f"not attested: {states}"))
        else:
            out.append(JobDecision(job, True, f"all {len(mapped.gates)} gate(s) attested"))
    return tuple(out)


def output_name(job: str) -> str:
    """`ci.yml:build-linux-x64` -> `skip_build-linux-x64` (the GITHUB_OUTPUT name)."""

    return "skip_" + job.split(":", 1)[1]
