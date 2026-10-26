"""PY-002 -- ci_lint/typed_records.py, and the ratchet over ci_lint's own source."""

from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path

from ci_lint.typed_records import scan_source, scan_tree

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("typed_records_baseline.json")


class ScanTest(unittest.TestCase):
    def _where(self, src: str) -> list[str]:
        return [v.where for v in scan_source("m.py", src)]

    def test_heterogeneous_tuple_return_is_reported(self) -> None:
        self.assertEqual(self._where("def f() -> tuple[int, list[str], int]: ..."), ["return of f()"])
        self.assertEqual(self._where("def f() -> tuple[int, str] | None: ..."), ["return of f()"])

    def test_homogeneous_tuple_and_plain_types_are_fine(self) -> None:
        self.assertEqual(self._where("def f() -> tuple[str, ...]: ...\ndef g() -> list[int]: ..."), [])

    def test_dict_return_and_dataclass_field(self) -> None:
        src = (
            "from dataclasses import dataclass\n"
            "def f() -> dict[str, int]: ...\n"
            "@dataclass(frozen=True)\nclass R:\n    a: dict[str, str]\n    b: tuple[int, int]\n    c: list[int]\n"
        )
        self.assertEqual(self._where(src), ["return of f()", "field R.a", "field R.b"])

    def test_wire_boundary_dicts_and_parameters_are_allowed(self) -> None:
        src = (
            "def f(env: dict[str, str]) -> dict[str, JsonValue]: ...\n"
            "def g() -> dict[str, YamlValue] | None: ...\n"
        )
        self.assertEqual(self._where(src), [])

    def test_complex_parameters_and_variables_are_reported(self) -> None:
        # Owner directive 2026-10-02: dataclasses for complex types, no dictionaries.
        src = (
            "def f(jobs: dict[str, dict[str, dict[str, YamlValue]]]) -> None: ...\n"
            "def g(pkg: dict[str, object], pairs: list[tuple[str, str]]) -> None: ...\n"
            "def h() -> None:\n    triggers: dict[str, dict[str, int]] = {}\n    raw: Mapping[str, Any] = {}\n"
        )
        self.assertEqual(
            self._where(src),
            ["parameter f(jobs)", "parameter g(pkg)", "parameter g(pairs)", "variable triggers", "variable raw"],
        )

    def test_flat_maps_indexes_and_boundary_documents_are_allowed(self) -> None:
        src = (
            "def f(env: Mapping[str, str], counts: dict[str, int], by_id: dict[int, Entry],\n"
            "      groups: dict[str, list[Entry]], raw: dict[str, TomlValue], opt: dict[str, Path | None]) -> None:\n"
            "    seen: dict[str, Entry] = {}\n"
        )
        self.assertEqual(self._where(src), [])

    def test_mapping_aliases_count_as_dicts_at_returns(self) -> None:
        self.assertEqual(self._where("def f() -> Mapping[str, int]: ...\ndef g() -> defaultdict[str, int]: ..."),
                         ["return of f()", "return of g()"])

    def test_plain_class_fields_are_not_scanned(self) -> None:
        self.assertEqual(self._where("class C:\n    a: dict[str, int]\n"), [])


class AliasTest(unittest.TestCase):
    """#193: a module-local type alias is judged as its aliased shape."""

    def _where(self, src: str) -> list[str]:
        return [v.where for v in scan_source("m.py", src)]

    def test_forbidden_shape_behind_alias_counts(self) -> None:
        src = (
            "Nested = dict[str, dict[str, int]]\n"
            "Pair = tuple[int, str]\n"
            "def f(x: Nested) -> Pair: ...\n"
            "def g() -> None:\n    y: Nested = {}\n"
        )
        self.assertEqual(self._where(src), ["return of f()", "parameter f(x)", "variable y"])

    def test_typealias_and_type_statement_forms(self) -> None:
        src = (
            "from typing import TypeAlias\n"
            "A: TypeAlias = dict[str, object]\n"
            "type B = tuple[int, int]\n"
            "def f(a: A) -> B: ...\n"
        )
        self.assertEqual(self._where(src), ["return of f()", "parameter f(a)"])

    def test_transitive_and_cycle_safe(self) -> None:
        src = (
            "Inner = dict[str, int]\nOuter = dict[str, Inner]\nAgain = Outer\n"
            "X = list[Y]\nY = list[X]\n"
            "def f(o: Again, c: X) -> None: ...\n"
        )
        self.assertEqual(self._where(src), ["parameter f(o)"])

    def test_allowed_shapes_behind_alias_stay_allowed(self) -> None:
        src = (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\nclass Entry:\n    a: int\n"
            "Flat = dict[str, str]\nIndex = dict[str, Entry]\nSeq = tuple[str, ...]\n"
            "def f(env: Flat, idx: Index) -> Seq: ...\n"
            "def g() -> Index: ...\n"
            "@dataclass(frozen=True)\nclass R:\n    by_id: Index\n    names: Seq\n"
        )
        self.assertEqual(self._where(src), [])

    def test_flat_map_alias_at_a_field_counts_like_the_literal(self) -> None:
        src = (
            "from dataclasses import dataclass\nStrMap = dict[str, str]\n"
            "@dataclass(frozen=True)\nclass R:\n    m: StrMap\n"
        )
        self.assertEqual(self._where(src), ["field R.m"])

    def test_wire_value_aliases_are_not_expanded(self) -> None:
        src = (
            'JsonValue = None | int | str | list["JsonValue"] | dict[str, "JsonValue"]\n'
            "Doc = dict[str, JsonValue]\n"
            "def f(d: Doc) -> Doc: ...\n"
        )
        self.assertEqual(self._where(src), [])


class DataclassIndexTest(unittest.TestCase):
    def test_unknown_value_type_still_counts_at_returns(self) -> None:
        self.assertEqual([v.where for v in scan_source("m.py", "def f() -> dict[str, Entry]: ...")],
                         ["return of f()"])

    def test_dataclass_imported_from_ci_lint_module_is_resolved(self) -> None:
        src = "from ci_lint.schema import Platform, CiToml\nfrom ci_lint.finding import Finding\n" \
              "def f() -> dict[str, Platform]: ...\ndef g() -> dict[str, Finding]: ...\n"
        self.assertEqual([v.where for v in scan_source("ci_lint/m.py", src, ROOT)], [])
        # without a root nothing outside the module is assumed
        self.assertEqual(len(scan_source("ci_lint/m.py", src)), 2)

    def test_non_dataclass_import_still_counts(self) -> None:
        src = "from ci_lint.schema import load_ci_toml\ndef f() -> dict[str, load_ci_toml]: ...\n"
        self.assertEqual(len(scan_source("ci_lint/m.py", src, ROOT)), 1)


class RatchetTest(unittest.TestCase):
    def test_no_module_exceeds_its_baseline(self) -> None:
        baseline: dict[str, int] = json.loads(BASELINE.read_text(encoding="utf-8"))["counts"]
        current = Counter(v.path for v in scan_tree(ROOT, "ci_lint"))
        grown = {
            path: (count, baseline.get(path, 0)) for path, count in current.items() if count > baseline.get(path, 0)
        }
        self.assertEqual(
            grown, {},
            "PY-002: use a typed dataclass instead of a record-shaped tuple/dict "
            f"(module: (now, allowed)) -- {grown}\n"
            + "\n".join(v.render() for v in scan_tree(ROOT, "ci_lint") if v.path in grown),
        )

    def test_baseline_is_tight(self) -> None:
        baseline: dict[str, int] = json.loads(BASELINE.read_text(encoding="utf-8"))["counts"]
        current = Counter(v.path for v in scan_tree(ROOT, "ci_lint"))
        loose = {path: (current.get(path, 0), allowed) for path, allowed in baseline.items() if current.get(path, 0) < allowed}
        self.assertEqual(loose, {}, f"PY-002 ratchet: lower these baseline entries to the current count -- {loose}")


if __name__ == "__main__":
    unittest.main()
