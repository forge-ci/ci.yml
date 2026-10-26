"""Source-bound live cache maintenance never becomes local test evidence."""

from ci_lint import repo_identity
import re
import shlex
from pathlib import Path

from ci_lint.attestations import load_definition
from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Finding
from ci_lint.workflow_scan import ParsedYamlFile, as_dict, jobs_of, steps_of
from ci_lint.yaml_io import YamlValue

MARKER = "CI_REMOTE_ONLY"
STUB_NAME = "Remote-only: runs on GitHub, not under act (GATE-012)"
COMMANDS = frozenset({"budget", "trim", "janitor", "preprune"})


def _maintenance_command(run: YamlValue) -> bool:
    if not isinstance(run, str) or "\n" in run.strip() or "\r" in run.strip():
        return False
    try:
        tokens = shlex.split(run)
    except ValueError:
        return False
    return (len(tokens) >= 6 and tokens[0] in ("python", "python3")
            and tokens[1:5] == ["-P", "-m", "ci_lint", "cache"] and tokens[5] in COMMANDS
            and all(re.fullmatch(r"[A-Za-z0-9_./=-]+", token) for token in tokens[6:]))


def declared_remote(job: dict[str, YamlValue]) -> bool:
    return as_dict(job.get("env")).get(MARKER) is not None


def _safe_env(env: YamlValue, policy_path: str | None) -> bool:
    values = as_dict(env)
    return (not set(values) - {MARKER, "GITHUB_TOKEN", "PYTHONPATH"}
            and ("PYTHONPATH" not in values or values["PYTHONPATH"] == policy_path))


def _policy_checkout(step: dict[str, YamlValue]) -> str | None:
    inputs = as_dict(step.get("with"))
    path, ref = inputs.get("path"), inputs.get("ref")
    if (repo_identity.is_ci_yml(inputs.get("repository")) and isinstance(ref, str)
            and re.fullmatch(r"[0-9a-f]{40}", ref) is not None and isinstance(path, str)
            and re.fullmatch(r"[A-Za-z_.][A-Za-z0-9_.-]*", path) is not None and path not in (".", "..")
            and inputs.get("persist-credentials") is False
            and not set(inputs) - {"repository", "ref", "path", "persist-credentials"}):
        return path
    return None


def _safe_checkout(step: dict[str, YamlValue]) -> bool:
    uses = step.get("uses")
    return (isinstance(uses, str) and re.fullmatch(r"actions/checkout@[0-9a-f]{40}", uses) is not None
            and "run" not in step and "if" not in step)


def source_maintenance(job: dict[str, YamlValue], document: dict[str, YamlValue]) -> bool:
    """Require pinned module provenance and Python's safe import path.

    -P prevents the repository working directory from shadowing ci_lint.
    No custom interpreter environment or source-provided planner outputs.
    """
    if (not isinstance(as_dict(job.get("env")).get(MARKER), str)
            or any(job.get(key) for key in ("outputs", "container", "services", "defaults"))
            or document.get("defaults") or not _safe_env(document.get("env"), None)):
        return False
    path: str | None = None
    commands = False
    for step in steps_of(job):
        if step.get("shell") is not None or "working-directory" in step:
            return False
        if "uses" in step:
            if path is not None or not _safe_checkout(step) or step.get("env"):
                return False
            path = _policy_checkout(step)
            if path is None and set(as_dict(step.get("with"))) - {"ref", "fetch-depth", "persist-credentials"}:
                return False
        elif path is not None and _maintenance_command(step.get("run")):
            if (not _safe_env(job.get("env"), path) or not _safe_env(step.get("env"), path)
                    or {**as_dict(job.get("env")), **as_dict(step.get("env"))}.get("PYTHONPATH") != path):
                return False
            commands = True
        else:
            return False
    return commands


def _unexecuted_source_step(step: dict[str, JsonValue], source_steps: tuple[str, ...]) -> bool:
    return (isinstance(step.get("id"), str) and bool(step["id"]) and step["id"] != "0"
            and step.get("name") in source_steps[1:] and step.get("status") == "completed"
            and step.get("conclusion") == "skipped"
            and all(step.get(field) is None for field in
                    ("first_seq", "last_seq", "exit_code", "started_at", "completed_at", "duration_ms")))


def prove_maintenance(raw: dict[str, JsonValue], key: str,
                      source_steps: tuple[str, ...] = ()) -> None:
    sections = raw.get("sections")
    main = ([item for item in sections if isinstance(item, dict) and item.get("stage") == "Main"]
            if isinstance(sections, list) else [])
    stubs = [item for item in main if item.get("id") == "0" and item.get("name") == STUB_NAME]
    if raw.get("status") != "completed" or raw.get("conclusion") != "remote_only" or len(stubs) != 1:
        raise ValueError(f"remote maintenance lacks its completed diagnostic stub: {key}")
    step = stubs[0]
    first, last = step.get("first_seq"), step.get("last_seq")
    if (step.get("id") != "0" or step.get("name") != STUB_NAME or step.get("status") != "completed"
            or step.get("conclusion") != "success" or type(first) is not int or type(last) is not int
            or not 0 < first <= last or raw.get("output_evidence") is not None):
        raise ValueError(f"remote maintenance cannot provide executed checks or outputs: {key}")
    skipped = [item for item in main if item is not step]
    if (not all(_unexecuted_source_step(item, source_steps) for item in skipped)
            or len({item.get("id") for item in skipped}) != len(skipped)
            or len({item.get("name") for item in skipped}) != len(skipped)):
        raise ValueError(f"remote maintenance has invalid original-step skips: {key}")


def non_attestable_jobs(files: tuple[ParsedYamlFile, ...]) -> frozenset[str]:
    """Include reusable callers: skipping a caller would suppress hosted work."""
    remote: set[str] = set()
    for file in files:
        for job_id, job in jobs_of(as_dict(file.document)).items():
            if declared_remote(job):
                remote.add(f"{Path(file.path).name}:{job_id}")
    changed = True
    while changed:
        changed = False
        for file in files:
            for job_id, job in jobs_of(as_dict(file.document)).items():
                ref, uses = f"{Path(file.path).name}:{job_id}", job.get("uses")
                if (ref not in remote and isinstance(uses, str) and uses.startswith("./.github/workflows/")
                        and any(item.startswith(Path(uses).name + ":") for item in remote)):
                    remote.add(ref)
                    changed = True
    return frozenset(remote)


def check_maintenance_attestations(repo: Path, files: tuple[ParsedYamlFile, ...]) -> list[Finding]:
    loaded = load_definition(repo)
    if loaded is None or loaded.definition is None:
        return []
    remote = non_attestable_jobs(files)
    return [Finding(rule="GATE-010", path="ci-attestations.yml",
                    message=f"remote-only job or reusable caller cannot be attested: {item.job}",
                    fix="keep hosted maintenance required; attest only locally executed checks")
            for item in loaded.definition.jobs if item.job in remote]
