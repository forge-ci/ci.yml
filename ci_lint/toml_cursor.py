"""Strict TOML table consumption.

`ci.toml` loading is STRICT (round-1A schema brief, section A): any key never
consumed by the schema is `CT-001` (unknown key, reported with its full key
path); any key whose value has the wrong TOML type is `CT-002`. `Cursor`
implements that contract once so every table in `ci_lint/schema.py` gets it
for free: pop known keys with a typed accessor, then call `finish()` to turn
whatever is left over into `CT-001` findings.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TypeVar

from ci_lint.finding import Finding, Status

TomlValue = None | bool | int | float | str | list["TomlValue"] | dict[str, "TomlValue"]

T = TypeVar("T")


def _type_name(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "table"
    if value is None:
        return "none"
    return type(value).__name__


@dataclass(frozen=True)
class StrEntry:
    """One `key = "value"` pair of a string -> string table."""

    key: str
    value: str


@dataclass(frozen=True)
class StrMap:
    """A string -> string table (`[allow].setup-*.require`), kept as typed
    entries in declaration order rather than a dict field (PY-002, #193)."""

    entries: tuple[StrEntry, ...] = ()

    @classmethod
    def of(cls, table: Mapping[str, str]) -> StrMap:
        return cls(tuple(StrEntry(k, v) for k, v in table.items()))

    def get(self, key: str) -> str | None:
        return next((e.value for e in self.entries if e.key == key), None)

    def __contains__(self, key: object) -> bool:
        return any(e.key == key for e in self.entries)

    def __iter__(self) -> Iterator[str]:
        return (e.key for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class StrTupleEntry:
    """One `key = ["a", "b"]` pair of a string -> string-array table."""

    key: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class StrTupleMap:
    """A string -> string-array table (`[allow].workflows`,
    `[allow].permissions`), as typed entries (PY-002, #193)."""

    entries: tuple[StrTupleEntry, ...] = ()

    @classmethod
    def of(cls, table: Mapping[str, tuple[str, ...]]) -> StrTupleMap:
        return cls(tuple(StrTupleEntry(k, tuple(v)) for k, v in table.items()))

    def get(self, key: str) -> tuple[str, ...] | None:
        return next((e.values for e in self.entries if e.key == key), None)

    def __contains__(self, key: object) -> bool:
        return any(e.key == key for e in self.entries)

    def __iter__(self) -> Iterator[str]:
        return (e.key for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class NamedTable:
    """One `[<key>.<id>]` subtable of a table of tables, in declaration order."""

    key: str
    table: dict[str, TomlValue]


@dataclass
class Cursor:
    """A live view over one TOML table, tracked at `path` (dotted key path)."""

    table: dict[str, TomlValue]
    path: str
    findings: list[Finding]
    source: str = "ci.toml"

    def __post_init__(self) -> None:
        # Work on a shallow copy so popping keys never mutates the caller's tree.
        self._remaining: dict[str, TomlValue] = dict(self.table)

    def key_path(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def _ct002(self, key: str, expected: str, got: TomlValue) -> None:
        self.findings.append(
            Finding(
                rule="CT-002",
                path=self.source,
                message=f"'{self.key_path(key)}' must be {expected}, got {_type_name(got)} ({got!r})",
                fix=f"set '{self.key_path(key)}' to a {expected} in {self.source}",
            )
        )

    def _missing(self, key: str, expected: str) -> None:
        self.findings.append(
            Finding(
                rule="CT-002",
                path=self.source,
                message=f"missing required key '{self.key_path(key)}' (expected {expected})",
                fix=f"add '{self.key_path(key)} = <{expected}>' to {self.source}",
            )
        )

    def str_(self, key: str, *, required: bool = True, default: str | None = None) -> str | None:
        if key not in self._remaining:
            if required:
                self._missing(key, "string")
            return default
        value = self._remaining.pop(key)
        if not isinstance(value, str):
            self._ct002(key, "a string", value)
            return default
        return value

    def bool_(self, key: str, *, required: bool = True, default: bool | None = None) -> bool | None:
        if key not in self._remaining:
            if required:
                self._missing(key, "bool")
            return default
        value = self._remaining.pop(key)
        if not isinstance(value, bool):
            self._ct002(key, "a bool", value)
            return default
        return value

    def int_(self, key: str, *, required: bool = True, default: int | None = None) -> int | None:
        if key not in self._remaining:
            if required:
                self._missing(key, "int")
            return default
        value = self._remaining.pop(key)
        if isinstance(value, bool) or not isinstance(value, int):
            self._ct002(key, "an int", value)
            return default
        return value

    def number_(self, key: str, *, required: bool = True, default: float | None = None) -> float | None:
        """A field that is an int or a float (TOML has one number type, so
        `24` and `24.0` both arrive as int/float). bool is rejected: it is
        an int subclass in Python but never a number a user means here."""

        if key not in self._remaining:
            if required:
                self._missing(key, "a number")
            return default
        value = self._remaining.pop(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._ct002(key, "a number", value)
            return default
        return float(value)

    def list_str(
        self, key: str, *, required: bool = True, default: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        if key not in self._remaining:
            if required:
                self._missing(key, "an array of strings")
            return default
        value = self._remaining.pop(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self._ct002(key, "an array of strings", value)
            return default
        return tuple(value)

    def str_or_list(
        self, key: str, *, required: bool = True, default: tuple[str, ...] | str | None = None
    ) -> tuple[str, ...] | str | None:
        """A field that is either a bare string (e.g. "all") or a string array."""

        if key not in self._remaining:
            if required:
                self._missing(key, "a string or an array of strings")
            return default
        value = self._remaining.pop(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return tuple(value)
        self._ct002(key, "a string or an array of strings", value)
        return default

    def list_str_list(
        self, key: str, *, required: bool = True
    ) -> tuple[tuple[str, ...], ...]:
        """A field like `ship = [[], ["json"]]`: an array of string arrays."""

        if key not in self._remaining:
            if required:
                self._missing(key, "an array of string arrays")
            return ()
        value = self._remaining.pop(key)
        if not isinstance(value, list):
            self._ct002(key, "an array of string arrays", value)
            return ()
        out: list[tuple[str, ...]] = []
        ok = True
        for item in value:
            if not isinstance(item, list) or not all(isinstance(v, str) for v in item):
                ok = False
                break
            out.append(tuple(item))
        if not ok:
            self._ct002(key, "an array of string arrays", value)
            return ()
        return tuple(out)

    def dict_str_str(self, key: str, *, required: bool = True) -> StrMap:
        if key not in self._remaining:
            if required:
                self._missing(key, "a table of string -> string")
            return StrMap()
        value = self._remaining.pop(key)
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            self._ct002(key, "a table of string -> string", value)
            return StrMap()
        return StrMap(tuple(StrEntry(k, str(v)) for k, v in value.items()))

    def dict_str_list_str(
        self, key: str, *, required: bool = True
    ) -> StrTupleMap:
        """A table whose values are each an array of strings, e.g.
        `[allow].permissions`: `{ "actions: write" = ["precheck"] }` (round-4B)."""

        if key not in self._remaining:
            if required:
                self._missing(key, "a table of string -> array of strings")
            return StrTupleMap()
        value = self._remaining.pop(key)
        if not isinstance(value, dict):
            self._ct002(key, "a table of string -> array of strings", value)
            return StrTupleMap()
        out: list[StrTupleEntry] = []
        for sub_key, sub_val in value.items():
            if not isinstance(sub_val, list) or not all(isinstance(v, str) for v in sub_val):
                self.findings.append(
                    Finding(
                        rule="CT-002",
                        path=self.source,
                        message=f"'{self.key_path(key)}.{sub_key}' must be an array of strings, got "
                        f"{_type_name(sub_val)}",
                        fix=f"set '{self.key_path(key)}.{sub_key}' to an array of strings in {self.source}",
                    )
                )
                continue
            out.append(StrTupleEntry(sub_key, tuple(sub_val)))
        return StrTupleMap(tuple(out))

    def table_(self, key: str, *, required: bool = True) -> dict[str, TomlValue] | None:
        """Pop a subtable and return its raw dict for a nested Cursor."""

        if key not in self._remaining:
            if required:
                self._missing(key, "a table")
            return None
        value = self._remaining.pop(key)
        if not isinstance(value, dict):
            self._ct002(key, "a table", value)
            return None
        return value

    def raw_table_of_tables(
        self, key: str, *, required: bool = True
    ) -> tuple[NamedTable, ...]:
        """Pop a table whose values are all themselves tables (e.g. [platforms])."""

        raw = self.table_(key, required=required)
        if raw is None:
            return ()
        out: list[NamedTable] = []
        for sub_key, sub_val in raw.items():
            if not isinstance(sub_val, dict):
                self.findings.append(
                    Finding(
                        rule="CT-002",
                        path=self.source,
                        message=f"'{self.key_path(key)}.{sub_key}' must be a table, got {_type_name(sub_val)}",
                        fix=f"make '{self.key_path(key)}.{sub_key}' a table in {self.source}",
                    )
                )
                continue
            out.append(NamedTable(sub_key, sub_val))
        return tuple(out)

    def array_of_tables(self, key: str) -> list[dict[str, TomlValue]]:
        """Pop `[[key]]`, an array-of-tables. Missing -> empty list, not an error."""

        if key not in self._remaining:
            return []
        value = self._remaining.pop(key)
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            self.findings.append(
                Finding(
                    rule="CT-002",
                    path=self.source,
                    message=f"'{self.key_path(key)}' must be an array of tables, got {_type_name(value)}",
                    fix=f"make '[[{self.key_path(key)}]]' entries tables in {self.source}",
                )
            )
            return []
        return value  # type: ignore[return-value]

    def finish(self) -> None:
        """Whatever is left over is an unknown key: CT-001."""

        for key in self._remaining:
            self.findings.append(
                Finding(
                    rule="CT-001",
                    path=self.source,
                    message=f"unknown key '{self.key_path(key)}'",
                    fix=f"remove '{self.key_path(key)}' from {self.source}, or use the documented field "
                    "(see docs/ci-toml.md) if you meant something else",
                )
            )
