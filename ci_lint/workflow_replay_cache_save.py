"""Recognize only exact-hit guarded saves of the same restored cache family."""

import re

from ci_lint.workflow_scan import as_dict, steps_of
from ci_lint.yaml_io import YamlValue


def _action(step: dict[str, YamlValue], verb: str) -> bool:
    value = step.get("uses")
    return isinstance(value, str) and re.fullmatch(
        rf"actions/cache/{verb}@(?:v[0-9]+|[0-9a-f]{{40}})", value) is not None


def _guard(value: YamlValue) -> str | None:
    if not isinstance(value, str):
        return None
    expression = value.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    match = re.fullmatch(
        r"(?:github\.ref\s*==\s*'refs/heads/(?:main|master)'\s*&&\s*)?"
        r"steps\.([A-Za-z0-9_-]+)\.outputs\.cache-hit\s*!=\s*'true'", expression)
    return match[1] if match is not None else None


def approved_cache_save(job: dict[str, YamlValue], name: str) -> bool:
    steps = steps_of(job)
    saves = [step for step in steps if step.get("name") == name]
    if len(saves) != 1 or not _action(saves[0], "save") or "run" in saves[0]:
        return False
    save = saves[0]
    identity = _guard(save.get("if"))
    restores = [step for step in steps if step.get("id") == identity]
    if identity is None or len(restores) != 1 or not _action(restores[0], "restore"):
        return False
    restore = restores[0]
    save_inputs = as_dict(save.get("with"))
    restore_inputs = as_dict(restore.get("with"))
    key = save_inputs.get("key")
    path = save_inputs.get("path")
    return (steps.index(restore) < steps.index(save) and "run" not in restore
            and isinstance(path, str) and bool(path.strip()) and path == restore_inputs.get("path")
            and isinstance(key, str) and key.strip() == "${{ steps." + identity + ".outputs.cache-primary-key }}")


def approved_pr_cache_save(job: dict[str, YamlValue], name: str) -> bool:
    """Prove an official save is excluded on PRs, without waiving validation."""
    saves = [step for step in steps_of(job) if step.get("name") == name]
    if len(saves) != 1 or not _action(saves[0], "save") or "run" in saves[0]:
        return False
    save = saves[0]
    expression = save.get("if")
    if not isinstance(expression, str):
        return False
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    guard = re.fullmatch(
        r"(?:github\.event_name != 'pull_request'|github\.ref == 'refs/heads/main')"
        r"(?: && steps\.[A-Za-z0-9_-]+\.outputs\.cache-hit != 'true')?", expression)
    inputs = as_dict(save.get("with"))
    return guard is not None and all(
        isinstance(inputs.get(key), str) and bool(str(inputs[key]).strip()) for key in ("key", "path"))
