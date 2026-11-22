"""GATE-014 (`./push` delegates to the prooft runner) and GATE-017 (a runnable
lint lane comes before any build) -- ci_lint/gate_push_prooft.py.

The `./push` shapes are the fleet's on 2026-11-22: clud's `ci/push.py` ran
`ci.attest` then `git push`, soldr/zccache call `ci-lint local-gate run`, and
FastLED/cli and fbuild drive `prooft run` lanes themselves.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ci_lint.attestations import parse_definition
from ci_lint.finding import Status
from ci_lint.gate_push_prooft import check_gate_014, check_gate_017, check_push_prooft

DELEGATING_PUSH = (
    '#!/usr/bin/env python\n"""Publish through the prooft runner (GATE-014)."""\nimport os\nimport sys\n\n'
    'os.execvp("prooft", ["prooft", "--push", *sys.argv[1:]])\n'
)
RUST_POLICY = """\
version: 1
lanes:
  rust: {run: soldr}
  tests:
    run: [soldr, cargo, test, --workspace]
gates:
  rust/all/fmt: {lane: rust}
  rust/x86_64-unknown-linux-gnu/clippy: {lane: rust}
  rust/x86_64-unknown-linux-gnu/test: {lane: tests}
jobs:
  ci.yml:lint: [rust/all/fmt, rust/x86_64-unknown-linux-gnu/clippy]
"""


class _Repo(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="gate014-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def rules(self) -> list[tuple[str, Status]]:
        return [(f.rule, f.status) for f in check_push_prooft(self.root)]


class Gate014Test(_Repo):
    def test_a_local_gate_only_repo_without_push_needs_review(self) -> None:
        self.write("local-gate.toml", "[gate]\nrun = [\"./ci.py\"]\n")
        self.assertEqual(self.rules(), [("GATE-014", Status.NEEDS_REVIEW)])

    def test_a_delegating_push_with_a_lint_lane_is_clean(self) -> None:
        self.write("push", DELEGATING_PUSH)
        self.write("ci-attestations.yml", RUST_POLICY)
        self.assertEqual(self.rules(), [])

    def test_a_repo_without_push_or_attestation_is_out_of_scope(self) -> None:
        self.assertEqual(self.rules(), [])

    def test_an_attesting_repo_without_push_is_flagged(self) -> None:
        self.write("ci-attestations.yml", RUST_POLICY)
        findings = check_gate_014(self.root)
        self.assertEqual([f.rule for f in findings], ["GATE-014"])
        self.assertIn("no root `./push`", findings[0].message)

    def test_a_push_that_attests_and_pushes_itself_is_flagged_per_line(self) -> None:
        self.write("push", "#!/usr/bin/env python\nrun(['python', '-m', 'ci.attest'])\nrun(['git', 'push', 'origin'])\n"
                   "run(['gh', 'pr', 'create', '--fill'])\n")
        findings = check_gate_014(self.root)
        messages = [f.message for f in findings]
        self.assertIn("`./push` does not delegate to the prooft runner", messages)
        self.assertEqual(sorted(f.line for f in findings if f.line), [3, 4])

    def test_ci_lint_local_gate_and_prooft_lanes_are_the_runners_job(self) -> None:
        self.write("push", "#!/bin/sh\nci-lint local-gate run\nprooft run lint -- ./lint\nprooft --push\n")
        lines = sorted(f.line for f in check_gate_014(self.root) if f.line)
        self.assertEqual(lines, [2, 3])

    def test_no_lint_in_push_is_flagged(self) -> None:
        self.write("push", '#!/bin/sh\nexec prooft --no-lint --push "$@"\n')
        findings = check_gate_014(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("--no-lint", findings[0].message)

    def test_comments_do_not_count(self) -> None:
        self.write("push", '#!/bin/sh\n# used to run: git push origin HEAD\nexec prooft --push "$@"\n')
        self.assertEqual(check_gate_014(self.root), [])

    def test_docstring_prose_does_not_count(self) -> None:
        self.write("push", '#!/usr/bin/env python\n"""Publish.\n\nNo more `git push origin HEAD` by hand.\n"""\n'
                   'import os\nimport sys\n\nos.execvp("prooft", ["prooft", "--push", *sys.argv[1:]])\n')
        self.assertEqual(check_gate_014(self.root), [])

    def test_a_long_push_that_mentions_prooft_needs_review(self) -> None:
        body = "".join(f"echo step {i}\n" for i in range(30))
        self.write("push", f"#!/bin/sh\n{body}exec prooft --push\n")
        findings = check_gate_014(self.root)
        self.assertEqual([(f.rule, f.status) for f in findings], [("GATE-014", Status.NEEDS_REVIEW)])


class Gate017Test(_Repo):
    def test_push_without_a_definition_is_flagged(self) -> None:
        self.write("push", DELEGATING_PUSH)
        self.assertEqual([f.rule for f in check_gate_017(self.root)], ["GATE-017"])

    def test_lanes_without_run_are_not_runnable(self) -> None:
        self.write("push", DELEGATING_PUSH)
        self.write("ci-attestations.yml", "version: 1\nlanes:\n  lint: {max-age-hours: 24}\n"
                   "gates:\n  python/all/lint: {lane: lint}\n")
        findings = check_gate_017(self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("no runnable lint lane", findings[0].message)

    def test_only_a_test_lane_is_not_enough(self) -> None:
        self.write("push", DELEGATING_PUSH)
        self.write("ci-attestations.yml", "version: 1\nlanes:\n  tests:\n    run: [./test]\n"
                   "gates:\n  python/linux-x64/test: {lane: tests}\n")
        self.assertEqual([f.rule for f in check_gate_017(self.root)], ["GATE-017"])

    def test_a_declared_lint_stage_counts(self) -> None:
        self.write("push", DELEGATING_PUSH)
        self.write("ci-attestations.yml", "version: 1\nlanes:\n  gate:\n    run: [./lint]\n    stage: lint\n"
                   "gates:\n  general/linux-x64/static-checks: {lane: gate}\n")
        self.assertEqual(check_gate_017(self.root), [])


class DefinitionAcceptsProoftKeysTest(unittest.TestCase):
    def test_lanes_and_mode_are_not_unknown_top_level_keys(self) -> None:
        load = parse_definition("mode: shadow\n" + RUST_POLICY)
        self.assertEqual([f.message for f in load.findings], [])


if __name__ == "__main__":
    unittest.main()
