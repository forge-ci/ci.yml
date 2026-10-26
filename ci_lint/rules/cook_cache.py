"""Third-party `soldr cook` cache, static clauses (zackees/ci.yml#120).

docs/policy-rust.md "Third-party `soldr cook` cache" -- the *sanctioned
interim* is statically checkable today; the target contract (CACHE-017's
third-party-slice key, CACHE-019's live generation count) depends on
setup-soldr features that have not shipped and stays candidate.

RUST-024: a job whose setup-soldr step cooks (`prebuild-deps` absent/empty,
  `soldr-cook` or `cargo-chef` -- setup-soldr's action.yml resolves empty to
  `soldr-cook`) and that sets fingerprint-relevant build env
  (`RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS`, `CARGO_TARGET_<T>_LINKER`,
  `CC[_<T>]`/`CXX[_<T>]`, `CARGO_TARGET_DIR`) in a LATER step -- a step
  `env:` block, an `export`, or a `>> $GITHUB_ENV` write. The cook ran under
  a different env than the build, so its output is wasted. `violation` when
  `prebuild-deps` is a literal cooking value, `needs_review` when it is an
  expression.

RUST-025: a hand-rolled cook cache -- an `actions/cache*` step whose `path:`
  or `key:` names cook output, a `soldr cook save|upload|push` step (RUST-010
  separately judges where such a step runs), or a `[cache.family]` whose
  literal `prefix` names cook output but whose `via` is not setup-soldr's.

CACHE-018 (interim): a cooking setup-soldr step in `ci-pre.yml` is a
  `violation`; a cooking step in a pull_request-triggered lane whose
  `cache-key-suffix` no push-triggered lane also cooks with is
  `needs_review` (job `if:` conditions and the push branch filter are not
  evaluated, so it cannot prove the push lane runs on the default branch,
  nor that the PR lane really restores).
"""

from __future__ import annotations

from ci_lint import repo_identity

import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.tools import find_commands
from ci_lint.schema import CiToml
from ci_lint.workflow_scan import as_dict, get_on_section, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue

_SETUP_SOLDR_RE = re.compile(rf"^{repo_identity.SETUP_SOLDR_RE}(/cook)?@", re.IGNORECASE)
_COOKING_VALUES: frozenset[str] = frozenset({"", "soldr-cook", "cargo-chef"})
_FINGERPRINT_ENV_RE = re.compile(
    r"^(RUSTFLAGS|CARGO_ENCODED_RUSTFLAGS|CARGO_TARGET_DIR|CARGO_TARGET_[A-Z0-9_]+_LINKER|"
    r"CC|CXX|CC_[A-Za-z0-9_]+|CXX_[A-Za-z0-9_]+)$"
)
_EXPORT_RE = re.compile(r"(?:^|[\s;&|])export\s+([A-Za-z_][A-Za-z0-9_]*)=")
_GITHUB_ENV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=[^\n]*>>\s*\"?\$\{?GITHUB_ENV")
_COOK_PATH_RE = re.compile(r"cook", re.IGNORECASE)
_SAVE_TOKENS: frozenset[str] = frozenset({"save", "upload", "push"})


@dataclass(frozen=True)
class CookStep:
    """A setup-soldr step and whether it cooks: `True`, `False`, or
    `None` when `prebuild-deps` is an unevaluable expression."""

    index: int
    cooks: bool | None
    suffix: str


def _cook_step(index: int, step: dict[str, YamlValue]) -> CookStep | None:
    uses = step.get("uses")
    if not isinstance(uses, str) or not _SETUP_SOLDR_RE.match(uses.strip()):
        return None
    with_ = as_dict(step.get("with"))
    raw = with_.get("prebuild-deps")
    value = "" if raw is None else str(raw).strip()
    suffix_raw = with_.get("cache-key-suffix")
    suffix = "" if suffix_raw is None else str(suffix_raw).strip()
    if "${{" in value:
        return CookStep(index=index, cooks=None, suffix=suffix)
    return CookStep(index=index, cooks=value.lower() in _COOKING_VALUES, suffix=suffix)


def _cook_steps(job: dict[str, YamlValue]) -> list[CookStep]:
    out: list[CookStep] = []
    for i, step in enumerate(steps_of(job)):
        found = _cook_step(i, step)
        if found is not None and found.cooks is not False:
            out.append(found)
    return out


def _fingerprint_vars_set(step: dict[str, YamlValue]) -> list[str]:
    names = [k for k in as_dict(step.get("env")) if _FINGERPRINT_ENV_RE.match(k)]
    run = step.get("run")
    if isinstance(run, str):
        for regex in (_EXPORT_RE, _GITHUB_ENV_RE):
            names.extend(m.group(1) for m in regex.finditer(run) if _FINGERPRINT_ENV_RE.match(m.group(1)))
    return sorted(set(names))


def check_rust_024(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            cooks = _cook_steps(job)
            if not cooks:
                continue
            first = cooks[0]
            steps = steps_of(job)
            late = sorted({v for step in steps[first.index + 1 :] for v in _fingerprint_vars_set(step)})
            if not late:
                continue
            findings.append(
                Finding(
                    rule="RUST-024",
                    path=wf.path,
                    status=Status.VIOLATION if first.cooks else Status.NEEDS_REVIEW,
                    message=(
                        f"jobs.{job_id} cooks in its setup-soldr step (step {first.index + 1}) but sets "
                        f"{', '.join(late)} in a later step -- the cook key and fingerprints miss the real "
                        "build env, so the cooked output is rebuilt"
                        + ("" if first.cooks else " (prebuild-deps is an expression; cannot tell if it cooks)")
                    ),
                    fix="set these variables before the setup-soldr step (job-level env:), or set "
                    "'prebuild-deps: none' on this lane until a deferred-cook hook exists",
                )
            )
    return findings


def _cache_step_names_cook(step: dict[str, YamlValue]) -> bool:
    uses = step.get("uses")
    if not isinstance(uses, str) or not uses.strip().lower().startswith("actions/cache"):
        return False
    with_ = as_dict(step.get("with"))
    text = f"{with_.get('path') or ''}\n{with_.get('key') or ''}"
    return bool(_COOK_PATH_RE.search(text))


def _runs_cook_save(step: dict[str, YamlValue]) -> bool:
    run = step.get("run")
    if not isinstance(run, str):
        return False
    return any(
        len(t) > 2 and t[0] == "soldr" and t[1] == "cook" and any(x in _SAVE_TOKENS for x in t[2:])
        for t in find_commands(run)
    )


def _rust_025_family(ci: CiToml) -> list[Finding]:
    findings: list[Finding] = []
    for fam_id in sorted(ci.cache.family):
        fam = ci.cache.family[fam_id]
        if fam.via.startswith("setup-soldr:") or not fam.prefix or not _COOK_PATH_RE.search(fam.prefix):
            continue
        findings.append(
            Finding(
                rule="RUST-025",
                path="ci.toml",
                message=(
                    f"[cache.family.{fam_id}] caches cook output (prefix {fam.prefix!r}) outside "
                    "setup-soldr's prebuild-deps: soldr-cook"
                ),
                fix="drop the family and let setup-soldr own the cook cache "
                "('prebuild-deps: soldr-cook', 'save-cache: auto'; via = \"setup-soldr:cook\")",
            )
        )
    return findings


def check_rust_025(ci: CiToml | None, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = [] if ci is None else _rust_025_family(ci)
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(wf.document)).items():
            for i, step in enumerate(steps_of(job)):
                if _cache_step_names_cook(step):
                    what = "an actions/cache step whose path/key names cook output"
                elif _runs_cook_save(step):
                    what = "a 'soldr cook save/upload/push' step"
                else:
                    continue
                findings.append(
                    Finding(
                        rule="RUST-025",
                        path=wf.path,
                        message=f"jobs.{job_id} step {i + 1} is {what} -- a hand-rolled cook cache",
                        fix="remove it; cook caching goes only through setup-soldr "
                        "('prebuild-deps: soldr-cook', 'save-cache: auto')",
                    )
                )
    return findings


def _triggers(doc: dict[str, YamlValue]) -> frozenset[str]:
    return frozenset(get_on_section(doc))


@dataclass(frozen=True)
class CookLane:
    path: str
    job_id: str
    step: CookStep


def check_cache_018(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    pr_lanes: list[CookLane] = []
    push_suffixes: set[str] = set()
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK:
            continue
        doc = as_dict(wf.document)
        triggers = _triggers(doc)
        is_pre = Path(wf.path).name in {"ci-pre.yml", "ci-pre.yaml"}
        for job_id, job in jobs_of(doc).items():
            for cs in _cook_steps(job):
                if is_pre:
                    findings.append(
                        Finding(
                            rule="CACHE-018",
                            path=wf.path,
                            status=Status.VIOLATION if cs.cooks else Status.NEEDS_REVIEW,
                            message=f"jobs.{job_id} cooks in ci-pre.yml -- ci-pre.yml never cooks or saves",
                            fix="set 'prebuild-deps: none' on this setup-soldr step",
                        )
                    )
                    continue
                if "push" in triggers:
                    push_suffixes.add(cs.suffix)
                if "pull_request" in triggers or "pull_request_target" in triggers:
                    pr_lanes.append(CookLane(path=wf.path, job_id=job_id, step=cs))
    for lane in pr_lanes:
        if lane.step.suffix in push_suffixes:
            continue
        findings.append(
            Finding(
                rule="CACHE-018",
                path=lane.path,
                status=Status.NEEDS_REVIEW,
                message=(
                    f"jobs.{lane.job_id} cooks on pull_request with cache-key-suffix "
                    f"{lane.step.suffix!r}, but no push-triggered lane cooks with that suffix -- "
                    "no default-branch run saves this cook shape, so every PR re-cooks for nothing"
                ),
                fix="run the same lane (same cache-key-suffix) on the default-branch push, or set "
                "'prebuild-deps: none' on it",
            )
        )
    return findings


def check_group_cook_cache(ci: CiToml | None, repo_root: Path) -> list[Finding]:
    return [*check_rust_024(repo_root), *check_rust_025(ci, repo_root), *check_cache_018(repo_root)]
