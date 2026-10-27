"""REL-008: a release-acceptance evidence probe is total (talos-hq/clud#2253).

Mirrors the real clud acceptance probe closely enough to be convincing: a
``tests/test_installer_public_ci.py`` that either skips (and so emits no
evidence row, which the aggregate then fails closed on) or records the gap
inside the row it emits.
"""

from __future__ import annotations

import unittest

from ci_lint.finding import Status
from ci_lint.rules.acceptance_evidence import check_rel_008
from ci_lint.tests.helpers import init_git_repo
from ci_lint.tests.test_complexity import _Repo

# The incident: a probe that skips when there is no prior catalog release, so
# it emits no row and the aggregate raises "missing public host evidence".
SKIPPING_PROBE = '''\
"""Released-mode acceptance: prove the promoted installer for one host."""

import json
import pytest


def assert_older_installer_discovery(host: str) -> None:
    prior = prior_catalog_release()
    if prior is None:
        pytest.skip("no prior public release")
    assert prior.version < current_version()


def emit_row(host: str) -> None:
    with open("evidence/%s.json" % host, "w") as fh:
        json.dump({"host": host, "ok": True}, fh)
'''

# The fix: the gap is recorded inside the row, so the row is always emitted.
TOTAL_PROBE = '''\
"""Released-mode acceptance: prove the promoted installer for one host."""

import json


def assert_older_installer_discovery(host: str) -> None:
    prior = prior_catalog_release()
    if prior is None:
        row = {"unavailable": "no catalog-aware prior release", "catalog_move_version": "2.8.35"}
    else:
        row = {"host": host, "prior": prior.version}
    with open("evidence/%s.json" % host, "w") as fh:
        json.dump(row, fh)
'''

PROBE_PATH = "tests/test_installer_public_ci.py"


def _run(files: dict[str, str]):
    # The rule scans `list_repo_files`, which uses `git ls-files` inside a work
    # tree (the real CI-checkout path); `git init` the throwaway tree so the
    # scan sees exactly these files and not the fixture's own environment.
    with _Repo(files) as root:
        init_git_repo(root)
        return check_rel_008(root)


class Rel008ClauseATest(unittest.TestCase):
    def test_skipping_probe_is_violation(self) -> None:
        findings = _run({PROBE_PATH: SKIPPING_PROBE})
        self.assertEqual(1, len(findings))
        self.assertEqual("REL-008", findings[0].rule)
        self.assertEqual(Status.VIOLATION, findings[0].status)
        self.assertEqual(PROBE_PATH, findings[0].path)
        self.assertEqual(10, findings[0].line)  # the `pytest.skip(...)` line
        self.assertIn("unavailable", findings[0].fix)

    def test_total_probe_recording_marker_is_silent(self) -> None:
        self.assertEqual([], _run({PROBE_PATH: TOTAL_PROBE}))

    def test_pytest_skip_exception_match_is_not_a_suppression(self) -> None:
        text = (
            "import pytest\n\n\n"
            "def test_absent_prior() -> None:\n"
            "    with pytest.raises(pytest.skip.Exception):\n"
            "        assert_older_installer_discovery('x')\n"
        )
        self.assertEqual([], _run({PROBE_PATH: text}))

    def test_importorskip_is_violation(self) -> None:
        text = "import pytest\n\n\ndef test_probe() -> None:\n    pytest.importorskip('clud')\n"
        findings = _run({PROBE_PATH: text})
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])

    def test_raise_skiptest_is_violation(self) -> None:
        text = "import unittest\n\n\ndef test_probe() -> None:\n    raise unittest.SkipTest('nope')\n"
        findings = _run({PROBE_PATH: text})
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])

    def test_sys_exit_zero_with_evidence_row_is_violation(self) -> None:
        text = (
            "import json\nimport sys\n\n\n"
            "def main() -> None:\n"
            "    if not available():\n"
            "        sys.exit(0)\n"
            "    with open('evidence/row.json', 'w') as fh:\n"
            "        json.dump({'evidence': []}, fh)\n"
        )
        findings = _run({PROBE_PATH: text})
        self.assertEqual([Status.VIOLATION], [f.status for f in findings])

    def test_sys_exit_zero_without_evidence_row_is_clean(self) -> None:
        text = "import sys\n\n\ndef main() -> None:\n    sys.exit(0)\n"
        self.assertEqual([], _run({PROBE_PATH: text}))

    def test_allow_comment_with_reason_excuses_the_line(self) -> None:
        text = (
            "import pytest\n\n\n"
            "def test_probe() -> None:\n"
            "    pytest.skip('manual-only host')  # ci-lint: allow REL-008 an arm64 board we cannot run in CI\n"
        )
        self.assertEqual([], _run({PROBE_PATH: text}))

    def test_bare_allow_comment_without_reason_is_not_an_excuse(self) -> None:
        text = (
            "import pytest\n\n\n"
            "def test_probe() -> None:\n"
            "    pytest.skip('manual-only host')  # ci-lint: allow REL-008\n"
        )
        self.assertEqual([Status.VIOLATION], [f.status for f in _run({PROBE_PATH: text})])

    def test_non_acceptance_path_is_not_scanned(self) -> None:
        self.assertEqual([], _run({"tests/test_widgets.py": SKIPPING_PROBE}))

    def test_fixtures_tree_is_excluded(self) -> None:
        self.assertEqual([], _run({"ci_lint/tests/fixtures/installer/probe.py": SKIPPING_PROBE}))


class Rel008SkipifInputTest(unittest.TestCase):
    def test_skipif_on_workflow_input_is_needs_review(self) -> None:
        text = (
            "import os\nimport pytest\n\n\n"
            "@pytest.mark.skipif(\n"
            "    os.environ.get('CLUD_PUBLIC_RELEASE_TAG') is None,\n"
            "    reason='CLUD_PUBLIC_RELEASE_TAG not set by the workflow',\n"
            ")\n"
            "def test_public_probe() -> None:\n"
            "    pass\n"
        )
        findings = _run({PROBE_PATH: text})
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])
        self.assertIn("CLUD_PUBLIC_RELEASE_TAG", findings[0].message)

    def test_skipif_without_workflow_input_is_not_flagged(self) -> None:
        text = (
            "import sys\nimport pytest\n\n\n"
            "@pytest.mark.skipif(sys.platform != 'linux', reason='linux only')\n"
            "def test_public_probe() -> None:\n"
            "    pass\n"
        )
        self.assertEqual([], _run({PROBE_PATH: text}))


class Rel008ClauseBTest(unittest.TestCase):
    GLOB_NO_DENOMINATOR = '''\
"""Aggregate the per-host evidence rows written by the acceptance probes."""

import json
from pathlib import Path


def require_evidence(dir: str) -> None:
    rows = [json.loads(p.read_text()) for p in Path(dir).glob("*.json")]
    assert rows
'''

    GLOB_WITH_DENOMINATOR = '''\
"""Aggregate the per-host evidence rows written by the acceptance probes."""

import json
from pathlib import Path

REQUIRED_TARGETS = ["linux-x64", "linux-arm64", "macos-arm64", "windows-x64"]


def require_evidence(dir: str) -> None:
    rows = [json.loads(p.read_text()) for p in Path(dir).glob("*.json")]
    if len(rows) != len(REQUIRED_TARGETS):
        raise ValueError("missing public host evidence")
'''

    def test_globbed_denominator_is_needs_review(self) -> None:
        findings = _run({"ci/installer_public_evidence.py": self.GLOB_NO_DENOMINATOR})
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])
        self.assertEqual("REL-008", findings[0].rule)

    def test_declared_denominator_is_silent(self) -> None:
        self.assertEqual([], _run({"ci/installer_public_evidence.py": self.GLOB_WITH_DENOMINATOR}))

    def test_listdir_without_denominator_is_needs_review(self) -> None:
        text = (
            "import json\nimport os\n\n\n"
            "def require_evidence(dir: str) -> None:\n"
            "    rows = [json.load(open(os.path.join(dir, n))) for n in os.listdir(dir)]\n"
            "    print(len(rows))\n"
        )
        findings = _run({"ci/public_release_evidence.py": text})
        self.assertEqual([Status.NEEDS_REVIEW], [f.status for f in findings])

    def test_glob_without_loading_rows_is_not_flagged(self) -> None:
        text = (
            "from pathlib import Path\n\n\n"
            "def count(dir: str) -> int:\n"
            "    return len(list(Path(dir).glob('*.json')))\n"
        )
        self.assertEqual([], _run({"ci/acceptance_files.py": text}))


if __name__ == "__main__":
    unittest.main()
