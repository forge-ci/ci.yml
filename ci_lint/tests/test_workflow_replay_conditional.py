"""Conditional steps: runtime-dynamic output guards prove either way (#415).

The clud repro: `_dylint.yml`'s run steps are guarded by
`steps.pass.outputs.cache-hit != 'true'` (the input-keyed pass marker,
zackees/clud#2077/#2078). On a marker hit the step skips *by design*, so a
plan demanding unconditional success rejected a fully green replay
(zackees/ci.yml#415: 31/31 jobs success, act exit 0, no stamp). The
source-derived contract now classifies these as `conditional_steps`: the
execution proof accepts exactly one Main section that either succeeded or
explicitly skipped -- never absent, never failed, never ambiguous.
"""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from ci_lint.workflow_replay import ReplayJob, prove_replay
from ci_lint.workflow_replay_checks import conditional_step_names
from ci_lint.tests import test_workflow_replay as fixtures


class ConditionalStepSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = {"steps": [
            {"name": "Lookup marker", "id": "pass", "run": "lookup"},
            {"name": "Run dylint", "if": "steps.pass.outputs.cache-hit != 'true'", "run": "dylint"},
            {"name": "Save marker", "if": "success() && steps.pass.outputs.cache-hit != 'true'",
             "uses": "actions/cache/save@v4"},
            {"name": "Build", "run": "build"},
            {"name": "Dependent", "if": "steps.pass.outcome == 'success'", "run": "x"},
        ]}

    def test_dynamic_output_guards_of_a_running_producer_are_conditional(self) -> None:
        self.assertEqual(("Run dylint", "Save marker"), conditional_step_names(self.job, set()))

    def test_classed_steps_never_double_classify(self) -> None:
        # The derive path passes cache-save/minimal/input classes as
        # skip_names; those keep their own (stricter) section contracts.
        self.assertEqual(("Run dylint",), conditional_step_names(self.job, {"Save marker"}))
        self.assertEqual((), conditional_step_names(self.job, {"Run dylint", "Save marker"}))

    def test_an_excluded_producer_keeps_the_conditional_off_its_dependents(self) -> None:
        # `pass` itself skipped means its outputs are the finite-exclusion
        # class's business (condition_excludes / producers), not this one.
        job = {"steps": [
            {"name": "Fetch", "id": "fetch", "if": "inputs.x != ''", "run": "f"},
            {"name": "Use", "if": "steps.fetch.outputs.data != ''", "run": "u"},
        ]}
        self.assertEqual((), conditional_step_names(job, {"Fetch"}))
        # ...but with a RUNNING producer the dependent is conditional.
        self.assertEqual(("Use",), conditional_step_names(job, set()))

    def test_outcome_guards_and_unbound_names_stay_required(self) -> None:
        # `.outcome` of a running producer is statically success -- not a
        # dynamic OUTPUT read; and an unbound ${{ }} name cannot be matched.
        self.assertEqual((), conditional_step_names({"steps": [
            {"name": "Dependent", "if": "steps.pass.outcome == 'success'", "run": "x"},
            {"name": "${{ inputs.why }}", "if": "steps.pass.outputs.x != ''", "run": "y"},
        ]}, set()))


class ConditionalStepRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = fixtures.WorkflowReplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.raw = fixture.raw
        self.expected = replace(fixture.expected, required_jobs=(ReplayJob(
            "CI/Tests", ("Run tests", "Run dylint"), conditional_steps=("Run dylint",)),))
        self.sections = self.raw["tree"]["groups"][0]["jobs"][0]["sections"]

    def _dylint(self, **change) -> dict:
        section = {"name": "Run dylint", "stage": "Main", "status": "completed",
                   "conclusion": "skipped", "first_seq": None, "last_seq": None}
        section.update(change)
        return section

    def test_explicit_skip_and_success_both_prove_but_nothing_else(self) -> None:
        # Marker hit: skipped by design -> accepted.
        self.sections.append(self._dylint())
        self.assertEqual(prove_replay(self.raw, self.expected).jobs, ("CI/Tests",))
        # Marker miss: executed successfully -> accepted.
        self.sections[-1] = self._dylint(conclusion="success", first_seq=10, last_seq=11)
        self.assertEqual(prove_replay(self.raw, self.expected).jobs, ("CI/Tests",))
        # Failure / cancellation / absence / duplication still reject.
        for change in ({"conclusion": "failure"}, {"stage": "Post"},
                       {"status": "queued"}, {"conclusion": "cancelled"}):
            raw = copy.deepcopy(self.raw)
            raw["tree"]["groups"][0]["jobs"][0]["sections"][-1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                prove_replay(raw, self.expected)
        self.sections.pop()
        with self.assertRaises(ValueError):
            prove_replay(self.raw, self.expected)
        self.sections.append(self._dylint())
        self.sections.append(self._dylint())
        with self.assertRaises(ValueError):
            prove_replay(self.raw, self.expected)

    def test_conditional_never_leaks_into_a_finite_exclusion_class(self) -> None:
        job = self.expected.required_jobs[0]
        for changed in (replace(job, conditional_steps=("Run dylint", "Run dylint")),
                        replace(job, conditional_steps=("Unknown",)),
                        replace(job, conditional_steps=("Run tests",),
                                input_skip_steps=("Run tests",)),
                        replace(job, conditional_steps=("Run tests",),
                                cache_save_steps=("Run tests",))):
            with self.assertRaises(ValueError):
                prove_replay(self.raw, replace(self.expected, required_jobs=(changed,)))

    def test_a_plain_required_step_still_demands_success(self) -> None:
        # Guard rails: the fixture's own "Run tests" is NOT conditional.
        sections = self.sections
        for index, section in enumerate(sections):
            if section.get("name") == "Run tests":
                sections[index] = {**section, "conclusion": "skipped"}
        with self.assertRaises(ValueError):
            prove_replay(self.raw, self.expected)