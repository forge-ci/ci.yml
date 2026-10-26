"""Strict declarations for workflow execution proof; not execution evidence."""

from __future__ import annotations

import tomllib
import unittest

from ci_lint.finding import Finding
from ci_lint.workflow_replay_config import parse_replay

DECLARATION = '''
repository = "owner/repo"
workflow = "ci.yml"
mode = "minimal"
[[jobs]]
source-job = "ci.yml:tests"
key = "CI/Tests"
steps = ["Run tests"]
lanes = ["tests"]
'''


class ReplayDeclarationTest(unittest.TestCase):
    def test_provider_query_is_a_qualified_nonempty_argv(self):
        for value, qualified, valid in (('["bosn", "ci", "runners", "list", "--json"]', True, True),
                                        ('[]', True, False), ('[""]', True, False),
                                        ('["bosn"]', False, False), ('"bosn"', True, False)):
            document = DECLARATION.replace('mode = "minimal"',
                        f'mode = "minimal"\nqualified = {str(qualified).lower()}\nprovider-query = {value}')
            findings: list[Finding] = []
            config = parse_replay(tomllib.loads(document), source="local-gate.toml",
                                  path="gate.replay", findings=findings)
            self.assertEqual(config is not None, valid, findings)

    def test_repository_aliases_are_distinct_former_names(self) -> None:
        for value, valid in (('["old/repo"]', True), ('["old/repo", "older/repo"]', True),
                             ('["owner/repo"]', False), ('["OWNER/repo"]', False),
                             ('["old/repo", "Old/Repo"]', False), ('["not-a-repo"]', False), ('"old/repo"', False)):
            document = DECLARATION.replace('mode = "minimal"', f'mode = "minimal"\nrepository-aliases = {value}')
            findings: list[Finding] = []
            config = parse_replay(tomllib.loads(document), source="local-gate.toml",
                                  path="gate.replay", findings=findings)
            self.assertEqual(config is not None, valid, (value, findings))
        findings = []
        config = parse_replay(tomllib.loads(DECLARATION), source="local-gate.toml", path="gate.replay",
                              findings=findings)
        assert config is not None
        self.assertEqual(config.repository_aliases, ())

    def test_qualified_omitted_steps_derive_checks_from_source(self) -> None:
        document = DECLARATION.replace('mode = "minimal"', 'mode = "minimal"\nqualified = true')
        document = document.replace('steps = ["Run tests"]\n', '')
        findings: list[Finding] = []
        config = parse_replay(tomllib.loads(document), source="local-gate.toml", path="gate.replay", findings=findings)
        self.assertEqual(findings, [])
        assert config is not None
        self.assertTrue(config.jobs[0].derive_checks)
        for addition in ('steps = []\n', 'input-skip-steps = ["Run tests"]\n'):
            findings = []
            self.assertIsNone(parse_replay(tomllib.loads(document + addition), source="local-gate.toml",
                                          path="gate.replay", findings=findings))

    def test_qualified_declarations_do_not_repeat_display_names(self) -> None:
        document = DECLARATION.replace('mode = "minimal"', 'mode = "minimal"\nqualified = true')
        document = document.replace('key = "CI/Tests"\n', '')
        findings: list[Finding] = []
        config = parse_replay(tomllib.loads(document), source="local-gate.toml", path="gate.replay", findings=findings)
        self.assertEqual(findings, [])
        assert config is not None
        self.assertTrue(config.qualified)
        self.assertEqual(config.jobs[0].proof.key, "ci.yml:tests")

    def test_qualified_source_cannot_be_duplicated_under_another_label(self) -> None:
        document = DECLARATION.replace('mode = "minimal"', 'mode = "minimal"\nqualified = true')
        document += '\n[[jobs]]\nsource-job="ci.yml:tests"\nkey="Another label"\nsteps=["Other check"]\nlanes=["tests"]\n'
        findings: list[Finding] = []
        self.assertIsNone(parse_replay(tomllib.loads(document), source="local-gate.toml",
                                      path="gate.replay", findings=findings))
        self.assertTrue(findings)

    def test_report_source_is_explicit_and_strict(self) -> None:
        for value, valid in (("file", True), ("stdout", True), ("unknown", False), ("", False)):
            with self.subTest(value=value):
                findings: list[Finding] = []
                document = DECLARATION.replace('mode = "minimal"', f'mode = "minimal"\nreport-source = "{value}"')
                config = parse_replay(tomllib.loads(document), source="local-gate.toml",
                                      path="gate.replay", findings=findings)
                self.assertEqual(config is not None, valid)
                if config is not None:
                    self.assertEqual(config.report_source, value)

    def test_strict_declaration_keeps_coverage_and_execution_names(self) -> None:
        findings: list[Finding] = []
        config = parse_replay(tomllib.loads(DECLARATION), source="local-gate.toml",
                              path="gate.replay", findings=findings)
        self.assertEqual(findings, [])
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.workflow, ".github/workflows/ci.yml")
        self.assertEqual(config.jobs[0].source_job, "ci.yml:tests")
        self.assertEqual(config.jobs[0].proof.steps, ("Run tests",))

    def test_unknown_keys_and_empty_coverage_are_rejected(self) -> None:
        for document in (
            DECLARATION + 'bogus = true\n',
            DECLARATION.replace('steps = ["Run tests"]', 'steps = []'),
            DECLARATION.replace('source-job = "ci.yml:tests"', 'source-job = "../ci.yml:tests"'),
            DECLARATION.replace('repository = "owner/repo"', 'repository = ""'),
            DECLARATION.replace('mode = "minimal"', 'mode = "unknown"'),
            DECLARATION + '[[jobs]]\nsource-job="ci.yml:tests"\nkey="CI/Tests"\nsteps=["Run tests"]\nlanes=["tests"]\n',
        ):
            with self.subTest(document=document):
                findings: list[Finding] = []
                self.assertIsNone(parse_replay(tomllib.loads(document), source="local-gate.toml",
                                              path="gate.replay", findings=findings))
                self.assertTrue(findings)

    def test_lane_dispatch_selection_keeps_exact_tier(self) -> None:
        document = DECLARATION + '\n[[selections]]\nlane="tests"\njob="tests"\nevent="workflow_dispatch"\ninputs={tier="test"}\n'
        findings: list[Finding] = []
        config = parse_replay(tomllib.loads(document), source="local-gate.toml", path="gate.replay", findings=findings)
        self.assertEqual(findings, [])
        assert config is not None
        self.assertEqual(config.selections[0].inputs[0].value, "test")
        for invalid in (document.replace('lane="tests"', 'lane="unknown"'),
                        document.replace('event="workflow_dispatch"', 'event="push"'),
                        document + '\n[[selections]]\nlane="tests"\njob="tests"\nevent="workflow_dispatch"\n'):
            with self.subTest(document=invalid):
                rejected: list[Finding] = []
                self.assertIsNone(parse_replay(tomllib.loads(invalid), source="local-gate.toml", path="gate.replay", findings=rejected))


class MultiWorkflowSelectionTest(unittest.TestCase):
    def test_explicit_whole_workflow_lane_retains_its_entrypoint(self) -> None:
        document = DECLARATION + '\n[[selections]]\nlane="tests"\nworkflow="dylint.yml"\nall-jobs=true\nevent="pull_request"\n'
        findings: list[Finding] = []
        config = parse_replay(tomllib.loads(document), source="local-gate.toml",
                              path="gate.replay", findings=findings)
        self.assertEqual(findings, [])
        assert config is not None
        self.assertIsNone(config.selections[0].selected_job)
        self.assertEqual(config.selections[0].workflow, ".github/workflows/dylint.yml")

    def test_whole_workflow_and_job_selector_are_mutually_exclusive(self) -> None:
        document = DECLARATION + '\n[[selections]]\nlane="tests"\njob="tests"\nall-jobs=true\nevent="pull_request"\n'
        findings: list[Finding] = []
        self.assertIsNone(parse_replay(tomllib.loads(document), source="local-gate.toml",
                                      path="gate.replay", findings=findings))
        self.assertTrue(findings)
