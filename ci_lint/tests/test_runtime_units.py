"""`ci-lint units` -- ci_lint/runtime/units.py + ci_lint/cargo_messages.py.

Uses a small recorded `--message-format=json` fixture (round-2A brief,
part 2a), never a real cargo invocation.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from ci_lint.cargo_messages import parse_compiler_artifacts
from ci_lint.cargo_scan import discover_workspace
from ci_lint.runtime.units import compute_units, render_text
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES


class CargoMessagesParseTest(unittest.TestCase):
    def test_only_compiler_artifacts_survive_and_bad_lines_are_ignored(self) -> None:
        path = FIXTURES / "runtime" / "units" / "basic.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        artifacts = parse_compiler_artifacts(lines)
        # 6 compiler-artifact lines in the fixture; build-script-executed,
        # build-finished and the stray text line are all skipped.
        self.assertEqual(6, len(artifacts))
        self.assertTrue(all(a.package_name for a in artifacts))


class UnitsRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FIXTURES / "runtime" / "workspace"
        _loaded = load_ci_toml(self.repo)
        self.ci = _loaded.ci
        findings = _loaded.findings
        self.assertIsNotNone(self.ci, msg=str(findings))
        self.crates = discover_workspace(self.repo)
        path = FIXTURES / "runtime" / "units" / "basic.jsonl"
        self.artifacts = parse_compiler_artifacts(path.read_text(encoding="utf-8").splitlines())

    def test_ignores_non_workspace_packages(self) -> None:
        report = compute_units(self.ci, self.crates, self.artifacts)
        self.assertEqual(("some-external-dep",), report.unmatched_packages)

    def test_derives_target_triple_from_filenames_else_host(self) -> None:
        report = compute_units(self.ci, self.crates, self.artifacts)
        demo_core = next(c for c in report.crates if c.package == "demo-core")
        triples = {u.target_triple for u in demo_core.units}
        self.assertIn("host", triples)
        self.assertIn("x86_64-pc-windows-msvc", triples)

    def test_private_crate_with_two_feature_sets_in_one_profile_is_rust_011(self) -> None:
        report = compute_units(self.ci, self.crates, self.artifacts)
        messages = [f.message for f in report.findings if f.rule == "RUST-011"]
        self.assertTrue(
            any("demo-core" in m and "distinct feature sets" in m for m in messages), msg=messages
        )

    def test_public_crate_feature_set_outside_ship_is_rust_011(self) -> None:
        report = compute_units(self.ci, self.crates, self.artifacts)
        messages = [f.message for f in report.findings if f.rule == "RUST-011"]
        self.assertTrue(any("demo" in m and "extra" in m and "[rust].ship" in m for m in messages), msg=messages)

    def test_render_text_does_not_crash_and_mentions_the_crates(self) -> None:
        report = compute_units(self.ci, self.crates, self.artifacts)
        text = render_text(report)
        self.assertIn("demo-core", text)
        self.assertIn("demo", text)


class EmptyDefaultFeatureTest(unittest.TestCase):
    """zackees/ci.yml#89: `default = []` is the empty graph, so a public
    crate compiled with `['default']` matches `[rust].ship = [[]]` (GREEN);
    a genuinely extra feature, or a non-empty default expanding to one, is
    still RUST-011 (RED)."""

    def setUp(self) -> None:
        repo = FIXTURES / "runtime" / "workspace"
        _loaded = load_ci_toml(repo)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None and ci.rust is not None, findings
        self.ci = replace(ci, rust=replace(ci.rust, ship=((),)))
        self.crates = discover_workspace(repo)
        lines = (FIXTURES / "runtime" / "units" / "basic.jsonl").read_text(encoding="utf-8").splitlines()
        self.demo = next(a for a in parse_compiler_artifacts(lines) if a.package_name == "demo" and not a.features)

    def _public_findings(self, default: tuple[str, ...], compiled: tuple[str, ...]) -> list[str]:
        crates = [
            replace(c, features={**c.features, "default": default}) if c.name == "demo" else c for c in self.crates
        ]
        report = compute_units(self.ci, crates, [replace(self.demo, features=compiled)])
        return [f.message for f in report.findings if f.rule == "RUST-011" and "public crate" in f.message]

    def test_green_default_on_empty_default_is_the_empty_set(self) -> None:
        self.assertEqual([], self._public_findings((), ("default",)))
        self.assertEqual([], self._public_findings((), ()))

    def test_green_ship_default_spelling_still_accepted(self) -> None:
        assert self.ci.rust is not None
        self.ci = replace(self.ci, rust=replace(self.ci.rust, ship=(("default",),)))
        self.assertEqual([], self._public_findings((), ()))

    def test_red_extra_feature_still_rust_011(self) -> None:
        self.assertEqual(1, len(self._public_findings((), ("extra",))))

    def test_red_nonempty_default_expands_to_its_members(self) -> None:
        self.assertEqual(1, len(self._public_findings(("extra",), ("default", "extra"))))


if __name__ == "__main__":
    unittest.main()
