"""GEN-023 (static clause) -- ci_lint/rules/cache_stats_summary.py (zackees/ci.yml#411)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ci_lint.finding import Status
from ci_lint.rules.cache_stats_summary import check_gen_023
from ci_lint.tests.helpers import requires_yaml_tooling


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


SOLDR_NONE = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: zackees/setup-soldr@v0
        with:
          compile-cache-stats: none
"""

SOLDR_DEFAULT = SOLDR_NONE.replace("        with:\n          compile-cache-stats: none\n", "")

WRAPPER_NONE = """\
runs:
  using: composite
  steps:
    - uses: zackees/setup-soldr@0123456789abcdef0123456789abcdef01234567
      with:
        compile-cache-stats: None
"""

CCACHE_NO_SUMMARY = """\
on: push
jobs:
  cpp:
    runs-on: ubuntu-latest
    env:
      CMAKE_CXX_COMPILER_LAUNCHER: ccache
    steps:
      - run: cmake -B build && cmake --build build
"""

CCACHE_SUMMARY = CCACHE_NO_SUMMARY + """\
      - if: always()
        run: |
          echo '### linux ccache stats' >> "$GITHUB_STEP_SUMMARY"
          ccache --show-stats >> "$GITHUB_STEP_SUMMARY"
"""

ZCCACHE_RUN = """\
on: push
jobs:
  cpp:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/build
"""

BUILD_ACTION = """\
runs:
  using: composite
  steps:
    - shell: bash
      run: cmake -B build -DCMAKE_C_COMPILER_LAUNCHER=zccache
"""


@requires_yaml_tooling
class Gen023Tests(unittest.TestCase):
    def _check(self, files: dict[str, str]) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in files.items():
                _write(root, rel, text)
            return check_gen_023(root)

    def test_stats_none_is_violation(self) -> None:
        findings = self._check({".github/workflows/ci.yml": SOLDR_NONE})
        self.assertEqual(1, len(findings))
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertEqual("GEN-023", findings[0].rule)

    def test_default_stats_is_clean(self) -> None:
        self.assertEqual([], self._check({".github/workflows/ci.yml": SOLDR_DEFAULT}))

    def test_wrapper_stats_none_is_violation(self) -> None:
        findings = self._check({".github/actions/soldr/action.yml": WRAPPER_NONE})
        self.assertEqual(1, len(findings))
        self.assertEqual(".github/actions/soldr/action.yml", findings[0].path)

    def test_ccache_without_summary_needs_review(self) -> None:
        findings = self._check({".github/workflows/ci.yml": CCACHE_NO_SUMMARY})
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_ccache_with_summary_is_clean(self) -> None:
        self.assertEqual([], self._check({".github/workflows/ci.yml": CCACHE_SUMMARY}))

    def test_zccache_via_composite_needs_review(self) -> None:
        findings = self._check(
            {".github/workflows/ci.yml": ZCCACHE_RUN, ".github/actions/build/action.yml": BUILD_ACTION}
        )
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_setup_soldr_job_is_not_double_reported(self) -> None:
        text = CCACHE_NO_SUMMARY.replace("    steps:\n", "    steps:\n      - uses: zackees/setup-soldr@v0\n")
        self.assertEqual([], self._check({".github/workflows/ci.yml": text}))


if __name__ == "__main__":
    unittest.main()
