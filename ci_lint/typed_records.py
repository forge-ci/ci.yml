"""PY-002: records are typed dataclasses, not tuples or dictionaries.

Owner directive (2026-10-01): tuples and dictionaries used as records are
strongly discouraged in favour of typed dataclasses; `-> tuple[int,
list[str], int]` is a policy violation. A positional tuple hides what each
slot means and a `dict[str, X]` record hides which keys exist; a frozen
dataclass names both and lets the type checker hold them.

What this scan reports, per Python file, from the AST:

- a **heterogeneous tuple** annotation -- `tuple[A, B, ...]` with two or
  more element types -- on a function return or a dataclass field.
  `tuple[X, ...]` (an immutable homogeneous sequence) is fine.
- a **dict** (`dict[...]`, `Dict[...]`) annotation on a function return or
  a dataclass field, unless its value type is a declared wire-boundary
  value (`JsonValue`, `YamlValue`, `TomlValue`): JSON/YAML/TOML are allowed
  at serialization boundaries, typed concretely (AGENTS.md).

The same check covers **parameters and annotated variables** (owner
directive 2026-10-02: "dataclass for complex types, no dictionaries"):
`jobs: dict[str, dict[str, dict[str, YamlValue]]]` is a record and must be
a dataclass. The one exception there is a *flat string map* --
a dict whose values are scalars (`dict[str, str]`, `Mapping[str, int]`,
`dict[str, Path | None]`) -- so a function may still accept an environment,
a header map such as `os.environ`, or a counter. A dict whose values are
containers, records, `object`/`Any` or wire values (`dict[str, list[...]]`,
`dict[str, dict[...]]`, `dict[str, JsonValue]`) is a record and is reported. `Mapping`,
`MutableMapping`, `defaultdict` and `OrderedDict` count as dicts.

**Module-local type aliases** (#193) are resolved before judging: a
module-level `Foo = dict[str, Bar]`, `Foo: TypeAlias = ...` or
`type Foo = ...` is expanded (transitively, cycle-safe) wherever `Foo`
appears in an annotation, so an alias cannot hide a shape.

An **index of dataclasses** -- `dict[K, D]` whose value `D` is a class
decorated `@dataclass` in the same module, or imported from a `ci_lint`
module where it is so decorated -- is allowed at returns and fields too.
Anything unresolvable counts, as before.

The counts are a ratchet (`ci_lint/tests/typed_records_baseline.json`):
existing code may only go down, and new files start at zero.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path

WIRE_VALUE_TYPES: frozenset[str] = frozenset({"JsonValue", "YamlValue", "TomlValue"})
_TUPLE_NAMES = frozenset({"tuple", "Tuple"})
_DICT_NAMES = frozenset({"dict", "Dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict", "DefaultDict"})


@dataclass(frozen=True)
class RecordViolation:
    path: str
    line: int
    where: str
    annotation: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.where} is annotated `{self.annotation}`"


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


_UNTYPED = frozenset({"object", "Any"})


@dataclass(frozen=True)
class _Alias:
    name: str
    value: ast.expr


@dataclass(frozen=True)
class _Ctx:
    """Per-module resolution context: aliases and known dataclass names."""

    aliases: tuple[_Alias, ...]
    dataclasses: frozenset[str]


_EMPTY = _Ctx((), frozenset())


class _Expander(ast.NodeTransformer):
    def __init__(self, aliases: tuple[_Alias, ...]) -> None:
        self.aliases = {a.name: a.value for a in aliases}
        self.active: set[str] = set()

    def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802 - ast API
        target = self.aliases.get(node.id)
        if target is None or node.id in WIRE_VALUE_TYPES or node.id in self.active:
            return node
        self.active.add(node.id)
        try:
            return self.visit(copy.deepcopy(target))
        finally:
            self.active.discard(node.id)


def _expand(node: ast.expr | None, ctx: _Ctx) -> ast.expr | None:
    if node is None or not ctx.aliases:
        return node
    return _Expander(ctx.aliases).visit(copy.deepcopy(node))


def _type_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Subscript):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _type_like(node.left) or _type_like(node.right)
    return False


def _alias_stmt(stmt: ast.stmt) -> _Alias | None:
    """The `(name, value)` of a module-level alias statement, if it is one:
    `Foo = <type>`, `Foo = Bar`, `Foo: TypeAlias = ...`, `type Foo = ...`."""

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        if _type_like(stmt.value) or isinstance(stmt.value, ast.Name):
            return _Alias(stmt.targets[0].id, stmt.value)
    elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None
          and _name(stmt.annotation) == "TypeAlias"):
        return _Alias(stmt.target.id, stmt.value)
    elif type(stmt).__name__ == "TypeAlias":  # `type Foo = ...` (3.12+)
        name = getattr(stmt, "name", None)
        value = getattr(stmt, "value", None)
        if isinstance(name, ast.Name) and isinstance(value, ast.expr):
            return _Alias(name.id, value)
    return None


def _module_aliases(tree: ast.Module) -> tuple[_Alias, ...]:
    raw: dict[str, ast.expr] = {}
    names: dict[str, ast.expr] = {}
    for stmt in tree.body:
        alias = _alias_stmt(stmt)
        if alias is not None:
            (names if isinstance(alias.value, ast.Name) else raw)[alias.name] = alias.value
    # `Foo = Bar` only aliases a shape when Bar (transitively) is one.
    changed = True
    while changed:
        changed = False
        for key, value in list(names.items()):
            if isinstance(value, ast.Name) and value.id in raw:
                raw[key] = value
                del names[key]
                changed = True
    return tuple(_Alias(k, v) for k, v in raw.items())


def _dataclass_names(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and _is_dataclass(n)}


def _imported_dataclasses(tree: ast.Module, path: str, root: Path | None) -> set[str]:
    """Names imported from a ci_lint module that is a @dataclass there.
    Without a root (or an unreadable module) nothing is assumed."""

    if root is None:
        return set()
    out: set[str] = set()
    pkg_parts = Path(path).with_suffix("").parts[:-1]
    for stmt in tree.body:
        if not isinstance(stmt, ast.ImportFrom):
            continue
        if stmt.level:
            base = pkg_parts[: len(pkg_parts) - (stmt.level - 1)] if stmt.level - 1 <= len(pkg_parts) else ()
            parts = (*base, *(stmt.module.split(".") if stmt.module else ()))
        else:
            parts = tuple((stmt.module or "").split("."))
        if not parts or parts[0] != "ci_lint":
            continue
        known = _module_dataclasses(root, parts)
        out.update(a.asname or a.name for a in stmt.names if a.name in known)
    return out


_DC_CACHE: dict[str, frozenset[str]] = {}


def _module_dataclasses(root: Path, parts: tuple[str, ...]) -> frozenset[str]:
    file = root.joinpath(*parts).with_suffix(".py")
    if not file.is_file():
        file = root.joinpath(*parts, "__init__.py")
    key = str(file)
    if key not in _DC_CACHE:
        try:
            _DC_CACHE[key] = frozenset(_dataclass_names(ast.parse(file.read_text(encoding="utf-8"))))
        except (OSError, SyntaxError):
            _DC_CACHE[key] = frozenset()
    return _DC_CACHE[key]


def _complex_map(sub: ast.Subscript) -> bool:
    """For parameters and variables: a dict is a record when its values are
    themselves maps (`dict[str, dict[str, YamlValue]]`) or untyped
    (`object`, `Any`). A scalar-valued map (environment, counter), an index
    of dataclasses (`dict[str, Entry]`, `dict[str, list[Entry]]`) and a
    single-level parsed document at its boundary (`dict[str, TomlValue]`)
    are not."""

    items = sub.slice.elts if isinstance(sub.slice, ast.Tuple) else [sub.slice]
    if not items:
        return True
    for node in ast.walk(items[-1]):
        if isinstance(node, ast.Subscript) and _name(node.value) in _DICT_NAMES:
            return True
        if isinstance(node, (ast.Name, ast.Attribute)) and _name(node) in _UNTYPED:
            return True
    return False


def _offending(node: ast.expr | None, *, lenient: bool = False, ctx: _Ctx = _EMPTY) -> bool:
    """True when the annotation contains a record-shaped tuple or dict.
    `lenient` (parameters, variables) applies `_complex_map` to dicts
    instead of the return/field rule: only nested or untyped maps."""

    node = _expand(node, ctx)
    if node is None:
        return False
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Subscript):
            continue
        base = _name(sub.value)
        if base in _TUPLE_NAMES and _record_tuple(sub):
            return True
        if base in _DICT_NAMES and (_complex_map(sub) if lenient else _strict_dict(sub, ctx)):
            return True
    return False


def _record_tuple(sub: ast.Subscript) -> bool:
    items = sub.slice.elts if isinstance(sub.slice, ast.Tuple) else [sub.slice]
    homogeneous = len(items) == 2 and isinstance(items[1], ast.Constant) and items[1].value is Ellipsis
    return len(items) >= 2 and not homogeneous


def _strict_dict(sub: ast.Subscript, ctx: _Ctx) -> bool:
    """Return/field rule: every dict counts except a wire-boundary document
    or an index of known dataclasses (`dict[str, Entry]`)."""

    items = sub.slice.elts if isinstance(sub.slice, ast.Tuple) else [sub.slice]
    if items and isinstance(items[-1], (ast.Name, ast.Attribute)) and _name(items[-1]) in ctx.dataclasses:
        return False
    value = ast.unparse(items[-1]) if items else ""
    return not any(wire in value for wire in WIRE_VALUE_TYPES)


def _is_dataclass(cls: ast.ClassDef) -> bool:
    for dec in cls.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _name(target) == "dataclass":
            return True
    return False


def scan_source(path: str, source: str, root: Path | None = None) -> list[RecordViolation]:  # noqa: C901
    """`root` (the directory holding `ci_lint/`) lets imported ci_lint
    dataclasses count as index values; without it only same-module ones do."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ctx = _Ctx(_module_aliases(tree),
               frozenset(_dataclass_names(tree) | _imported_dataclasses(tree, path, root)))
    out: list[RecordViolation] = []
    fields: set[int] = set()  # dataclass fields, already judged by the stricter field rule
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _offending(node.returns, ctx=ctx):
                out.append(RecordViolation(path, node.lineno, f"return of {node.name}()", ast.unparse(node.returns)))
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, *(x for x in (a.vararg, a.kwarg) if x)):
                if _offending(arg.annotation, lenient=True, ctx=ctx):
                    assert arg.annotation is not None
                    out.append(RecordViolation(path, arg.lineno, f"parameter {node.name}({arg.arg})",
                                               ast.unparse(arg.annotation)))
        elif isinstance(node, ast.ClassDef) and _is_dataclass(node):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and _offending(item.annotation, ctx=ctx):
                    fields.add(id(item))
                    target = ast.unparse(item.target)
                    out.append(
                        RecordViolation(path, item.lineno, f"field {node.name}.{target}", ast.unparse(item.annotation))
                    )
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and id(node) not in fields
                and _offending(node.annotation, lenient=True, ctx=ctx)):
            out.append(RecordViolation(path, node.lineno, f"variable {ast.unparse(node.target)}",
                                       ast.unparse(node.annotation)))
    return sorted(out, key=lambda v: v.line)


def scan_tree(root: Path, package: str) -> list[RecordViolation]:
    """Every non-test module under `root/package`."""

    out: list[RecordViolation] = []
    for file in sorted((root / package).rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        if "/tests/" in rel:
            continue
        out.extend(scan_source(rel, file.read_text(encoding="utf-8"), root))
    return out
