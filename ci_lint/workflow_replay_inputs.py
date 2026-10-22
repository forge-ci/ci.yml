"""Bind typed literal workflow-call inputs without evaluating expressions."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from ci_lint.workflow_scan import as_dict, get_on_section
from ci_lint.yaml_io import YamlValue


@dataclass(frozen=True)
class BoundInput:
    name: str
    value: str | bool


def input_value(value: YamlValue, inputs: tuple[BoundInput, ...],
                matrix: YamlValue = None) -> str | bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}\s*", value)
    if match:
        return next((item.value for item in inputs if item.name == match[1]), None)
    match = re.fullmatch(r"\s*\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}\s*", value)
    if match:
        found = as_dict(matrix).get(match[1])
        return found if isinstance(found, (str, bool)) else None
    return value if "${{" not in value else None


def bind_call_inputs(document: dict[str, YamlValue], caller: dict[str, YamlValue],
                     parent: tuple[BoundInput, ...], matrix: YamlValue = None, *,
                     boolean_value: Callable[[YamlValue], bool | None] | None = None) -> tuple[BoundInput, ...]:
    declarations = as_dict(as_dict(get_on_section(document).get("workflow_call")).get("inputs"))
    supplied = as_dict(caller.get("with"))
    if set(supplied) - set(declarations):
        raise ValueError("reusable call supplies undeclared inputs")
    result: list[BoundInput] = []
    for name, raw in declarations.items():
        descriptor = as_dict(raw)
        if descriptor.get("required") is True and name not in supplied:
            raise ValueError(f"reusable call lacks required input {name}")
        kind = descriptor.get("type")
        if kind not in ("string", "boolean"):
            continue
        raw_value = supplied[name] if name in supplied else descriptor.get("default", False if kind == "boolean" else "")
        value = input_value(raw_value, parent if name in supplied else (), matrix if name in supplied else None)
        if value is None and kind == "boolean" and name in supplied and boolean_value is not None:
            value = boolean_value(raw_value)
        if value is None and _context_expression(raw_value):
            continue
        if not isinstance(value, str if kind == "string" else bool):
            raise ValueError(f"reusable input {name} is not a statically known {kind}")
        result.append(BoundInput(name, value))
    return tuple(result)


def _context_expression(value: YamlValue) -> bool:
    """Unresolved runner contexts never establish a condition or identity."""
    return (isinstance(value, str) and "${{" in value and
            re.fullmatch(r"\s*\$\{\{\s*(?:inputs|matrix)\.[A-Za-z0-9_-]+\s*\}\}\s*", value) is None)


def bound_name(value: YamlValue, inputs: tuple[BoundInput, ...], matrix: YamlValue = None) -> str:
    if not isinstance(value, str):
        raise ValueError("execution identity has a nonliteral workflow or job name")
    def substitute(match: re.Match[str]) -> str:
        found = (next((item.value for item in inputs if item.name == match[2]), None)
                 if match[1] == "inputs" else as_dict(matrix).get(match[2]))
        if not isinstance(found, (str, bool, int)):
            raise ValueError(f"execution identity depends on unknown {match[1]} {match[2]}")
        return str(found).lower() if isinstance(found, bool) else str(found)
    resolved = re.sub(r"\$\{\{\s*(inputs|matrix)\.([A-Za-z0-9_-]+)\s*\}\}", substitute, value)
    if not resolved.strip() or "${{" in resolved:
        raise ValueError("execution identity has a nonliteral workflow or job name")
    return resolved
