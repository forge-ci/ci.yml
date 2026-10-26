"""CPP-001: a CI `ctest` run with no parallelism runs the suite serially.

zackees/ci.yml#393 (docs/policy-cpp.md), the C/C++ analogue of RUST-017.
`ctest` runs one test at a time unless it is given `-j`/`--parallel` or
`CTEST_PARALLEL_LEVEL` is set (or a `--preset` test preset sets
`execution.jobs`), so a suite of independent test executables leaves every
core but one idle on a multi-core runner.

Static signals (every workflow / composite-action `run:` line, plus one
level into a referenced `ci/*.py` script's literal subprocess argv, plus --
from `local-gate lint` -- the `ci/*.py` scripts the local gate names):

- violation: a `ctest` invocation with no `-j`/`-jN`/`--parallel [N]`, no
  `CTEST_PARALLEL_LEVEL` in its environment (inline assignment, step, job
  or workflow `env:`, or the job exporting it through `$GITHUB_ENV`), and no
  test preset declaring `execution.jobs`; or one that pins the level to 1.
- needs_review: a `--preset` whose `CMakePresets.json` entry cannot be
  resolved, or a referenced script that builds a `ctest` command dynamically
  (no literal argv) and never mentions a parallelism flag or
  `CTEST_PARALLEL_LEVEL` -- ci-lint cannot see the final argv.

Not a finding: `ctest -N`/`--show-only` (lists tests, runs none), and a
script that mentions `CTEST_PARALLEL_LEVEL` (its environment is not
statically resolvable, so the mention is taken as intent). A same-line
`# ci-lint: allow CPP-001 <reason>` excuses one line (e.g. a suite whose
tests share a fixed port).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding, Status
from ci_lint.rules.test_invocations import (
    allowed,
    iter_run_lines,
    referenced_scripts,
    script_calls,
    script_string_constants,
    source_line,
    split_commands_with_env,
)
from ci_lint.workflow_scan import as_dict, as_list, jobs_of, load_composite_actions, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

RULE = "CPP-001"
ENV_VAR = "CTEST_PARALLEL_LEVEL"
_LIST_ONLY: frozenset[str] = frozenset({"-N", "--show-only"})
_PRESET_FILES: tuple[str, ...] = ("CMakePresets.json", "CMakeUserPresets.json")

_FIX = (
    "run the suite in parallel: 'ctest -j' (CMake >= 3.29: one job per core) or 'ctest --parallel <N>', "
    "set CTEST_PARALLEL_LEVEL in the job's env:, or give the test preset 'execution.jobs'. Tests that "
    "cannot share a host belong in a RESOURCE_LOCK/RUN_SERIAL property, not a serial suite"
)


@dataclass(frozen=True)
class Parallelism:
    """What an argv says about ctest parallelism: `level` is the explicit
    level (0 = unbounded `-j`), None when the argv sets none."""

    level: int | None
    preset: str | None
    list_only: bool


def _level(value: str) -> int:
    return int(value) if value.isdigit() else 0


def parse_ctest_args(args: tuple[str, ...]) -> Parallelism:
    level: int | None = None
    preset: str | None = None
    list_only = False
    i = 0
    while i < len(args):
        arg = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if arg in _LIST_ONLY:
            list_only = True
        elif arg in ("-j", "--parallel"):
            if nxt.isdigit():
                level = int(nxt)
                i += 1
            else:
                level = 0
        elif arg.startswith("--parallel="):
            level = _level(arg.split("=", 1)[1])
        elif arg.startswith("-j") and arg[2:].isdigit():
            level = int(arg[2:])
        elif arg == "--preset" and nxt:
            preset = nxt
            i += 1
        elif arg.startswith("--preset="):
            preset = arg.split("=", 1)[1]
        i += 1
    return Parallelism(level=level, preset=preset, list_only=list_only)


def _is_ctest(token: str) -> bool:
    return token.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe") == "ctest"


def find_ctest(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    """The argv after `ctest` when `tokens` runs it (any wrapper prefix)."""

    for i, tok in enumerate(tokens):
        if _is_ctest(tok):
            return tokens[i + 1 :]
    return None


# ── CMakePresets.json test presets ───────────────────────────────────────────


@dataclass(frozen=True)
class PresetJobs:
    found: bool
    jobs: int | None


@dataclass(frozen=True)
class TestPreset:
    name: str
    jobs: int | None
    inherits: tuple[str, ...]


def _test_presets(repo_root: Path) -> list[TestPreset]:
    out: list[TestPreset] = []
    for file_name in _PRESET_FILES:
        try:
            doc: JsonValue = json.loads((repo_root / file_name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        presets = doc.get("testPresets") if isinstance(doc, dict) else None
        for raw in presets if isinstance(presets, list) else []:
            if not isinstance(raw, dict):
                continue
            execution = raw.get("execution")
            jobs = execution.get("jobs") if isinstance(execution, dict) else None
            inherits = raw.get("inherits")
            parents = (inherits,) if isinstance(inherits, str) else tuple(
                str(x) for x in (inherits if isinstance(inherits, list) else [])
            )
            out.append(TestPreset(str(raw.get("name")), jobs if isinstance(jobs, int) else None, parents))
    return out


def preset_jobs(repo_root: Path, name: str) -> PresetJobs:
    """`execution.jobs` of test preset `name`, following `inherits`."""

    by_name = {p.name: p for p in _test_presets(repo_root)}
    seen: set[str] = set()
    queue = [name]
    found = False
    while queue:
        current = queue.pop(0)
        if current in seen or current not in by_name:
            continue
        seen.add(current)
        found = True
        preset = by_name[current]
        if preset.jobs is not None:
            return PresetJobs(True, preset.jobs)
        queue.extend(preset.inherits)
    return PresetJobs(found, None)


# ── environment scopes ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnvScope:
    """A workflow/job/step whose environment sets CTEST_PARALLEL_LEVEL;
    `prefix` is matched against a run line's location."""

    path: str
    prefix: str
    value: str


def _env_value(env: object) -> str | None:
    if isinstance(env, dict) and ENV_VAR in env:
        return str(env[ENV_VAR]).strip()
    return None


def _job_scopes(path: str, job_id: str, raw_job: YamlValue) -> list[EnvScope]:
    job = as_dict(raw_job)
    out: list[EnvScope] = []
    value = _env_value(job.get("env"))
    if value is not None:
        out.append(EnvScope(path, f"jobs.{job_id}.", value))
    for i, step in enumerate(steps_of(job)):
        run = step.get("run")
        if isinstance(run, str) and ENV_VAR in run and "GITHUB_ENV" in run:
            out.append(EnvScope(path, f"jobs.{job_id}.", "exported"))
        value = _env_value(step.get("env"))
        if value is not None:
            out.append(EnvScope(path, f"jobs.{job_id}.steps[{i}]", value))
    return out


def env_scopes(repo_root: Path) -> list[EnvScope]:
    out: list[EnvScope] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        top = _env_value(doc.get("env"))
        if top is not None:
            out.append(EnvScope(wf.path, "", top))
        for job_id, job in jobs_of(doc).items():
            out.extend(_job_scopes(wf.path, job_id, job))
    for act in load_composite_actions(repo_root):
        if act.status != LoadStatus.OK:
            continue
        runs = as_dict(act.document).get("runs")
        steps = as_list(runs.get("steps")) if isinstance(runs, dict) else []
        for i, step in enumerate(steps):
            value = _env_value(step.get("env")) if isinstance(step, dict) else None
            if value is not None:
                out.append(EnvScope(act.path, f"runs.steps[{i}]", value))
    return out


def _scoped_level(scopes: list[EnvScope], path: str, loc: str) -> str | None:
    values = [s.value for s in scopes if s.path == path and loc.startswith(s.prefix)]
    return values[-1] if values else None


# ── the check ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CtestSite:
    args: tuple[str, ...]
    env_level: str | None  # CTEST_PARALLEL_LEVEL as the command sees it, if set
    path: str
    line: int | None
    loc: str
    raw: str


def _run_line_sites(repo_root: Path) -> list[CtestSite]:
    scopes = env_scopes(repo_root)
    out: list[CtestSite] = []
    for run in iter_run_lines(repo_root):
        line, path, loc = run.line, run.path, run.loc
        for cmd in split_commands_with_env(line):
            env, tokens = cmd.env, cmd.tokens
            args = find_ctest(tuple(tokens))
            if args is None:
                continue
            level = env.get(ENV_VAR, _scoped_level(scopes, path, loc))
            out.append(CtestSite(args, level, path, None, loc, line))
    return out


def _verdict(site: CtestSite, repo_root: Path) -> Finding | None:
    parsed = parse_ctest_args(site.args)
    if parsed.list_only or allowed(site.raw, RULE):
        return None
    where = f"{site.loc}: 'ctest'"
    if parsed.level is not None:
        if parsed.level == 1:
            return Finding(rule=RULE, path=site.path, line=site.line,
                           message=f"{where} pins the parallel level to 1, so the suite runs serially", fix=_FIX)
        return None
    if site.env_level is not None:
        if site.env_level == "1":
            return Finding(rule=RULE, path=site.path, line=site.line,
                           message=f"{where} runs with {ENV_VAR}=1, so the suite runs serially", fix=_FIX)
        return None
    if parsed.preset is not None:
        jobs = preset_jobs(repo_root, parsed.preset)
        if not jobs.found:
            return Finding(rule=RULE, path=site.path, line=site.line, status=Status.NEEDS_REVIEW,
                           message=f"{where} uses test preset '{parsed.preset}', which is not in "
                           "CMakePresets.json/CMakeUserPresets.json; cannot confirm its execution.jobs",
                           fix=_FIX)
        if jobs.jobs is not None and jobs.jobs > 1:
            return None
    return Finding(
        rule=RULE,
        path=site.path,
        line=site.line,
        message=f"{where} has no -j/--parallel and no {ENV_VAR}, so the suite runs one test at a time",
        fix=_FIX,
    )


def _mentions_parallelism(constants: list[str]) -> bool:
    return any(
        c in ("-j", "--parallel") or c.startswith("--parallel=") or bool(re.fullmatch(r"-j\d+", c)) or ENV_VAR in c
        for c in constants
    )


def check_cpp_001(repo_root: Path, *, extra_scripts: tuple[str, ...] = ()) -> list[Finding]:
    findings: list[Finding] = []
    for site in _run_line_sites(repo_root):
        finding = _verdict(site, repo_root)
        if finding is not None:
            findings.append(finding)

    scripts = sorted({*referenced_scripts(repo_root), *(s for s in extra_scripts if (repo_root / s).is_file())})
    for rel in scripts:
        script = repo_root / rel
        constants = [c.value for c in script_string_constants(script)]
        try:
            env_mentioned = ENV_VAR in script.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            env_mentioned = False
        literal = False
        for call in script_calls(script):
            args = find_ctest(call.tokens)
            if args is None:
                continue
            literal = True
            site = CtestSite(args, "set" if env_mentioned else None, rel, call.line,
                             f"line {call.line}", source_line(script, call.line))
            finding = _verdict(site, repo_root)
            if finding is not None:
                findings.append(finding)
        if literal or env_mentioned or not any(_is_ctest(c) for c in constants) or _mentions_parallelism(constants):
            continue
        line = next(c.line for c in script_string_constants(script) if _is_ctest(c.value))
        if allowed(source_line(script, line), RULE):
            continue
        findings.append(
            Finding(
                rule=RULE,
                path=rel,
                line=line,
                status=Status.NEEDS_REVIEW,
                message=f"line {line}: this CI script builds a 'ctest' command dynamically and never "
                f"mentions -j/--parallel or {ENV_VAR}; cannot confirm the suite runs in parallel",
                fix=_FIX,
            )
        )
    return findings
