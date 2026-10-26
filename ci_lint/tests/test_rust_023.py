"""RUST-023 -- ci_lint/rules/rust_double_check.py (zackees/ci.yml#115)."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.rust_double_check import check_rust_023, covered, normalize
from ci_lint.tests.helpers import requires_yaml_tooling

_PIN = '[toolchain]\nchannel = "1.95.0"\n'


def _run(workflow: str, files: tuple[tuple[str, str], ...] = ()) -> list[Finding]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wf = root / ".github" / "workflows" / "ci.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text(textwrap.dedent(workflow), encoding="utf-8")
        for rel, text in files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(textwrap.dedent(text), encoding="utf-8")
        return check_rust_023(root)


def _lint(*lines: str) -> str:
    body = "".join(f"          {ln}\n" for ln in lines)
    return "on: pull_request\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n" + body


class SelectionTest(unittest.TestCase):
    def _cov(self, check: str, clippy: str) -> bool:
        a, b = normalize(tuple(check.split())), normalize(tuple(clippy.split()))
        assert a is not None and b is not None
        return covered(a, b)

    def test_subset_and_equal(self) -> None:
        self.assertTrue(self._cov("--workspace --all-targets", "--workspace --all-targets"))
        self.assertTrue(self._cov("-p a", "--workspace --all-targets"))
        self.assertTrue(self._cov("--workspace", "--workspace --all-targets"))
        self.assertTrue(self._cov("--tests", "--all-targets"))

    def test_not_covered(self) -> None:
        self.assertFalse(self._cov("--target x86_64-unknown-linux-musl", "--workspace"))
        self.assertFalse(self._cov("--all-targets", "--workspace"))
        self.assertFalse(self._cov("--features x", ""))
        self.assertFalse(self._cov("--release", ""))
        self.assertFalse(self._cov("--workspace", "-p a"))

    def test_unprovable(self) -> None:
        self.assertIsNone(normalize(("--features", "${{ matrix.f }}")))
        self.assertIsNone(normalize(("--weird-flag",)))


@requires_yaml_tooling
class Rust023ClauseATest(unittest.TestCase):
    def test_red_check_then_clippy(self) -> None:
        findings = _run(
            _lint(
                "soldr cargo check --workspace --all-targets",
                "soldr cargo clippy --workspace --all-targets -- -D warnings",
            )
        )
        self.assertEqual(1, len(findings))
        self.assertEqual((findings[0].rule, findings[0].status), ("RUST-023", Status.VIOLATION))

    def test_green_clippy_only_and_order_and_cross_target(self) -> None:
        self.assertEqual([], _run(_lint("cargo clippy --workspace -- -D warnings")))
        self.assertEqual([], _run(_lint("cargo clippy --workspace", "cargo check --workspace")))
        self.assertEqual(
            [], _run(_lint("cargo check --target aarch64-apple-darwin", "cargo clippy --workspace -- -D warnings"))
        )

    def test_green_allow_comment(self) -> None:
        self.assertEqual(
            [],
            _run(_lint("cargo check --workspace  # ci-lint: allow RUST-023 measured", "cargo clippy --workspace")),
        )

    def test_needs_review_expression(self) -> None:
        findings = _run(_lint("cargo check --features ${{ matrix.f }}", "cargo clippy --workspace"))
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_separate_jobs_not_same_path(self) -> None:
        wf = (
            "on: pull_request\njobs:\n"
            "  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cargo check --workspace\n"
            "  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cargo clippy --workspace\n"
        )
        self.assertEqual([], _run(wf))

    def test_red_one_level_into_ci_script(self) -> None:
        script = 'import subprocess\nsubprocess.run(["cargo", "check", "--workspace"], check=True)\n'
        findings = _run(_lint("python ci/lint.py", "cargo clippy --workspace"), (("ci/lint.py", script),))
        self.assertEqual(1, len(findings))
        self.assertEqual(("ci/lint.py", 2), (findings[0].path, findings[0].line))


def _msrv(toolchain_step: str = "", cmd: str = "cargo check --workspace") -> str:
    return (
        "on: pull_request\njobs:\n  msrv:\n    runs-on: ubuntu-latest\n    steps:\n"
        + toolchain_step
        + f"      - run: {cmd}\n"
    )


@requires_yaml_tooling
class Rust023ClauseBTest(unittest.TestCase):
    def _files(self, rust_version: str) -> tuple[tuple[str, str], ...]:
        return (
            ("rust-toolchain.toml", _PIN),
            ("Cargo.toml", f'[package]\nname = "x"\nrust-version = "{rust_version}"\n'),
        )

    def test_red_msrv_on_pin(self) -> None:
        for step in (
            "",
            "      - uses: dtolnay/rust-toolchain@1.95.0\n",
            "      - run: rustup toolchain install 1.95\n",
        ):
            findings = _run(_msrv(step), self._files("1.95"))
            self.assertEqual([Status.VIOLATION], [f.status for f in findings], step)

    def test_green_older_msrv(self) -> None:
        step = "      - uses: dtolnay/rust-toolchain@1.80.0\n"
        self.assertEqual([], _run(_msrv(step), self._files("1.80")))
        self.assertEqual([], _run(_msrv(cmd="cargo +1.80.0 check"), self._files("1.95.0")))

    def test_needs_review_named_channel(self) -> None:
        step = "      - uses: dtolnay/rust-toolchain@stable\n"
        findings = _run(_msrv(step), self._files("1.95.0"))
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])
