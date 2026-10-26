"""M2-22 (ci.yml#52, part of #44/#1 -- issue #5-owned candidates): GEN-007,
RUST-008, RUST-010. Static/precheck-time checks only; RUST-009 (job-log
evidence) lives in `ci_lint.runtime.toolchain_build` because it needs real
build-log text, not ci.toml/workflow YAML alone.

GEN-007 (docs/policy-general.md): "A CI trigger's paths: (or an equivalent
common-paths list) is scoped broader than the actual blast radius of the
changed code, so a large per-artifact/per-platform matrix fans out on a
shared-code edit that only a representative subset can meaningfully
validate." This is a STATIC HEURISTIC (docs/policy-general.md's Status
column allows "Candidate"/needs_review here; the brief explicitly says
"needs_review allowed") -- ci_lint has no fleet-wide blast-radius model, so
it can only flag the shape that makes the candidate plausible: a workflow
whose `pull_request`/`push` trigger has NO `paths:` filter (or one that is
itself a repo-wide wildcard, which is equivalent to none) while at least
one of its jobs fans out over a >=3-entry `strategy.matrix` list. Always
needs_review, never a hard violation -- confirming the blast radius truly
doesn't need that fan-out requires reading the changed code, which is
outside ci_lint's static scope.

RUST-008 (docs/policy-rust.md): "The quick gate does not run check-only
analysis for every declared supported target triple, and the repository
does not prove a boundary lint that confines platform `cfg` (soldr#2762
pattern)." Static: enumerate `ci.toml`'s declared `[platforms]` target
triples; a `dylints/**/*.rs` file whose name/path mentions "boundary"
counts as the proof of a boundary lint (issue #5's own soldr#2762
reference -- a Dylint rule that fails a `cfg(target_os = ...)` branch
compiled somewhere the boundary doesn't cover) and exempts the repo. Absent
that, the "fast"/"quick" job (by job id containing "fast" or "quick") must
run a check-only command (`cargo check`/`soldr cargo check`, never
`build`/`test`) naming every declared target via `--target`; a target not
named is RUST-008.

RUST-010 (docs/policy-rust.md): "`soldr cook` runs or saves outside Linux,
saves from a non-default-branch ref, or captures post-build `target/`
state." Static: for every job invoking `soldr cook` (any subcommand) --
runs-on not Linux is a violation; a `save`/`upload` subcommand with no
default-branch guard (`if:` referencing `github.ref` or
`github.event.pull_request.base.ref` and a literal default-branch name, OR
a `github.ref_name == '<default>'` comparison) is needs_review (cannot
statically prove the guard's condition is correct, only that a guard-shaped
`if:` is present or absent); and a `save`/`upload` subcommand whose step
appears AFTER a build step (`cargo build`/`soldr cargo build`/`cargo
test`/`soldr cargo test`, which write `target/`) in the same job is
needs_review ("post-build target/ capture" cannot be told apart from a
correct pre-build-only save without knowing what `soldr cook` actually
uploads).
"""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.tools import ENV_ASSIGN_RE, find_commands
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import as_dict, as_list, get_on_section, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

# ── GEN-007 ──────────────────────────────────────────────────────────────

_WILDCARD_PATHS: frozenset[str] = frozenset({"**", "**/*", "*"})
_MATRIX_FANOUT_THRESHOLD = 3


def _max_matrix_fanout(doc: dict[str, YamlValue]) -> int:
    largest = 0
    for job in jobs_of(doc).values():
        strategy = job.get("strategy")
        if not isinstance(strategy, dict):
            continue
        matrix = strategy.get("matrix")
        if not isinstance(matrix, dict):
            continue
        for value in matrix.values():
            if isinstance(value, list):
                largest = max(largest, len(value))
    return largest


def _paths_is_unscoped(paths: YamlValue) -> bool:
    if paths is None:
        return True
    if isinstance(paths, list):
        return any(isinstance(p, str) and p.strip() in _WILDCARD_PATHS for p in paths)
    return False


def check_gen_007(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        on = get_on_section(doc)
        fanout = _max_matrix_fanout(doc)
        if fanout < _MATRIX_FANOUT_THRESHOLD:
            continue
        for trigger_name in ("pull_request", "push"):
            trigger = on.get(trigger_name)
            if not isinstance(trigger, dict):
                continue
            paths = trigger.get("paths")
            if not _paths_is_unscoped(paths):
                continue
            shape = "no 'paths:' filter" if paths is None else f"a repo-wide wildcard paths filter ({paths!r})"
            findings.append(
                Finding(
                    rule="GEN-007",
                    path=wf.path,
                    status=Status.NEEDS_REVIEW,
                    message=(
                        f"on.{trigger_name} has {shape} while a job in this workflow fans out over a "
                        f"{fanout}-entry strategy.matrix -- confirm every matrix leg's blast radius "
                        "actually needs to run on every change this trigger fires for, or scope "
                        "'paths:' to the code each leg validates"
                    ),
                    fix=(
                        f"add an on.{trigger_name}.paths: list scoped to the directories/files this "
                        "workflow's matrix legs actually validate (not a repo-wide '**'), so a change "
                        "outside that blast radius does not fan out the full matrix; if every leg "
                        "genuinely must run on any change, record that as a documented exception rather "
                        "than leaving the trigger unscoped by default"
                    ),
                )
            )
    return findings


# ── RUST-008 ─────────────────────────────────────────────────────────────

_FAST_QUICK_JOB_ID_HINTS: tuple[str, ...] = ("fast", "quick")
_CHECK_SUBCOMMANDS: frozenset[str] = frozenset({"check"})


def _is_cargo_check_command(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] == "cargo" and len(tokens) > 1 and tokens[1] in _CHECK_SUBCOMMANDS:
        return True
    if tokens[0] == "soldr" and len(tokens) > 2 and tokens[1] == "cargo" and tokens[2] in _CHECK_SUBCOMMANDS:
        return True
    return False


def _target_values(tokens: list[str]) -> list[str]:
    return [tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t == "--target"]


def _has_boundary_lint(repo_root: Path) -> bool:
    dylints_dir = repo_root / "dylints"
    if not dylints_dir.is_dir():
        return False
    for rs_path in dylints_dir.rglob("*.rs"):
        rel = rs_path.relative_to(dylints_dir).as_posix().lower()
        if "boundary" in rel:
            return True
    return False


def _job_commands(job: dict[str, YamlValue]) -> list[list[str]]:
    commands: list[list[str]] = []
    for step in steps_of(job):
        run = step.get("run")
        if isinstance(run, str):
            commands.extend(find_commands(run))
    return commands


@dataclass(frozen=True)
class _FastJob:
    path: str
    job_id: str
    job: dict[str, YamlValue]


def check_rust_008(ci: CiToml, repo_root: Path) -> list[Finding]:  # noqa: C901
    targets = sorted({p.target for p in ci.platforms.values()})
    if not targets:
        return []
    if _has_boundary_lint(repo_root):
        return []

    fast_jobs: list[_FastJob] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        for job_id, job in jobs_of(doc).items():
            if any(hint in job_id.lower() for hint in _FAST_QUICK_JOB_ID_HINTS):
                fast_jobs.append(_FastJob(wf.path, job_id, job))

    if not fast_jobs:
        return [
            Finding(
                rule="RUST-008",
                path=None,
                message=(
                    "no 'fast'/'quick' job runs a check-only (cargo check) analysis for the declared "
                    f"target triples ({', '.join(targets)}), and no dylints/**/*boundary*.rs lint "
                    "proves platform cfg is otherwise confined"
                ),
                fix=(
                    "add 'soldr cargo check --target <T>' for every declared [platforms].*.target to "
                    "the quick gate job (named 'fast'/'quick'), or add a Dylint boundary lint under "
                    "dylints/ (its path naming 'boundary') that confines platform-specific cfg to a "
                    "reviewed module so a missing check-only pass cannot hide a cross-target break"
                ),
            )
        ]

    covered: set[str] = set()
    uses_matrix_target = False
    for fast in fast_jobs:
        job = fast.job
        strategy = job.get("strategy")
        if isinstance(strategy, dict) and isinstance(strategy.get("matrix"), dict):
            uses_matrix_target = True
        for tokens in _job_commands(job):
            if _is_cargo_check_command(tokens):
                covered.update(_target_values(tokens))

    missing = [t for t in targets if t not in covered]
    if not missing:
        return []

    status = Status.NEEDS_REVIEW if uses_matrix_target else Status.VIOLATION
    locs = ", ".join(f"{f.path}:{f.job_id}" for f in fast_jobs)
    return [
        Finding(
            rule="RUST-008",
            path=fast_jobs[0].path,
            status=status,
            message=(
                f"the quick gate job(s) ({locs}) run check-only analysis for "
                f"{sorted(covered) if covered else '(no targets)'}, missing declared target(s) "
                f"{missing}, and no dylints/**/*boundary*.rs lint proves platform cfg is confined"
            ),
            fix=(
                "add 'soldr cargo check --target <T>' for each missing target to the quick gate job, "
                "or resolve the matrix-driven target expression so ci-lint can confirm every declared "
                "target is covered, or add a Dylint boundary lint under dylints/ that confines "
                "platform cfg so a missing check-only pass cannot hide a cross-target break"
            ),
        )
    ]


# ── RUST-010 ─────────────────────────────────────────────────────────────

_BUILD_LIKE_SUBCOMMANDS: frozenset[str] = frozenset({"build", "test", "nextest"})
_SAVE_LIKE_TOKENS: frozenset[str] = frozenset({"save", "upload", "push"})
_DEFAULT_BRANCH_GUARD_HINTS: tuple[str, ...] = ("github.ref", "github.ref_name", "base.ref", "default_branch")


def _is_cook_command(tokens: list[str]) -> bool:
    return len(tokens) > 1 and tokens[0] == "soldr" and tokens[1] == "cook"


def _cook_is_save(tokens: list[str]) -> bool:
    return _is_cook_command(tokens) and any(t in _SAVE_LIKE_TOKENS for t in tokens[2:])


def _is_build_like(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] == "cargo" and len(tokens) > 1 and tokens[1] in _BUILD_LIKE_SUBCOMMANDS:
        return True
    if tokens[0] == "soldr" and len(tokens) > 2 and tokens[1] == "cargo" and tokens[2] in _BUILD_LIKE_SUBCOMMANDS:
        return True
    return False


def _step_has_default_branch_guard(step: dict[str, YamlValue]) -> bool:
    condition = step.get("if")
    if not isinstance(condition, str):
        return False
    lowered = condition.lower()
    return any(hint in lowered for hint in _DEFAULT_BRANCH_GUARD_HINTS)


def _runs_on_is_linux(runs_on: YamlValue) -> bool | None:
    if isinstance(runs_on, str):
        return runs_on.strip().lower().startswith("ubuntu")
    if isinstance(runs_on, list) and runs_on and all(isinstance(x, str) for x in runs_on):
        return all(x.strip().lower().startswith("ubuntu") for x in runs_on)
    return None


def check_rust_010(repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        for job_id, job in jobs_of(doc).items():
            steps = steps_of(job)
            cook_step_indices: list[int] = []
            for i, step in enumerate(steps):
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                for tokens in find_commands(run):
                    j = 0
                    while j < len(tokens) and ENV_ASSIGN_RE.match(tokens[j]):
                        j += 1
                    remaining = tokens[j:]
                    if _is_cook_command(remaining):
                        cook_step_indices.append(i)
                        runs_on_verdict = _runs_on_is_linux(job.get("runs-on"))
                        if runs_on_verdict is False:
                            findings.append(
                                Finding(
                                    rule="RUST-010",
                                    path=wf.path,
                                    message=(
                                        f"jobs.{job_id} runs 'soldr cook' but runs-on = "
                                        f"{job.get('runs-on')!r}, not a Linux runner"
                                    ),
                                    fix="move soldr cook to a Linux ('ubuntu-*') job -- cook's cache "
                                    "layers are built and saved from Linux only",
                                )
                            )
                        if _cook_is_save(remaining) and not _step_has_default_branch_guard(step):
                            findings.append(
                                Finding(
                                    rule="RUST-010",
                                    path=wf.path,
                                    status=Status.NEEDS_REVIEW,
                                    message=(
                                        f"jobs.{job_id} runs 'soldr cook {' '.join(remaining[2:])}' with "
                                        "no visible default-branch guard (an 'if:' referencing "
                                        "github.ref/ref_name/base.ref) -- cannot confirm it only saves "
                                        "from the default branch"
                                    ),
                                    fix="guard the save step with an 'if:' comparing github.ref_name (or "
                                    "github.event.pull_request.base.ref for a PR-context save) against the "
                                    "repository's default branch, so a save never runs from an arbitrary ref",
                                )
                            )
                        if _cook_is_save(remaining) and any(
                            _is_build_like(t)
                            for prior in steps[:i]
                            for t in find_commands(prior.get("run") or "")
                        ):
                            findings.append(
                                Finding(
                                    rule="RUST-010",
                                    path=wf.path,
                                    status=Status.NEEDS_REVIEW,
                                    message=(
                                        f"jobs.{job_id} runs 'soldr cook {' '.join(remaining[2:])}' after "
                                        "a build/test step in the same job -- confirm this save captures "
                                        "pre-build layers only, not post-build target/ output"
                                    ),
                                    fix="run 'soldr cook save' before any 'cargo build'/'cargo test' step "
                                    "in this job (or confirm cook's own manifest excludes post-build "
                                    "target/ paths), so a compiled artifact never gets captured into the "
                                    "cooked base layer",
                                )
                            )
    return findings


def check_group_m2_22(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_gen_007(repo_root))
    findings.extend(check_rust_008(ci, repo_root))
    findings.extend(check_rust_010(repo_root))
    return findings
