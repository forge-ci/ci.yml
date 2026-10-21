"""Only exact-hit cache writes may have skipped replay sections."""

import copy
import unittest

from ci_lint.workflow_replay_cache_save import approved_cache_save
from ci_lint.workflow_replay import ReplayJob
from ci_lint.workflow_replay_config import DeclaredReplayJob
from ci_lint.workflow_replay_static import _check_job


class CacheSaveProofTest(unittest.TestCase):
    def setUp(self):
        self.job = {"steps": [
            {"id": "compile_cache", "name": "Restore cache", "uses": "actions/cache/restore@v5",
             "with": {"path": "cache/units", "key": "units-v1"}},
            {"name": "Save cache", "uses": "actions/cache/save@v5",
             "if": "steps.compile_cache.outputs.cache-hit != 'true'",
             "with": {"path": "cache/units", "key": "${{ steps.compile_cache.outputs.cache-primary-key }}"}},
        ]}

    def test_matching_exact_hit_save_is_recognized(self):
        self.assertTrue(approved_cache_save(self.job, "Save cache"))

    def test_default_branch_exact_hit_save_is_optional_during_dispatch(self):
        for branch in ("main", "master"):
            job = copy.deepcopy(self.job)
            job["steps"][1]["if"] = (
                "${{ github.ref == 'refs/heads/" + branch
                + "' && steps.compile_cache.outputs.cache-hit != 'true' }}"
            )
            with self.subTest(branch=branch):
                self.assertTrue(approved_cache_save(job, "Save cache"))

    def test_branch_guard_cannot_excuse_mismatched_cache_or_validation(self):
        guard = "github.ref == 'refs/heads/main' && steps.compile_cache.outputs.cache-hit != 'true'"
        for change in (
            {"run": "run-tests"},
            {"if": "github.ref == 'refs/heads/main' || steps.compile_cache.outputs.cache-hit != 'true'"},
            {"with": {"path": "other", "key": "${{ steps.compile_cache.outputs.cache-primary-key }}"}},
            {"with": {"path": "cache/units", "key": "other-key"}},
        ):
            job = copy.deepcopy(self.job)
            job["steps"][1]["if"] = guard
            job["steps"][1].update(change)
            with self.subTest(change=change):
                self.assertFalse(approved_cache_save(job, "Save cache"))

    def test_validation_steps_and_unmatched_saves_cannot_be_excused(self):
        changes = (
            {"run": "run-tests"}, {"uses": "owner/cache/save@v5"},
            {"if": "false"}, {"if": "steps.other.outputs.cache-hit != 'true'"},
            {"with": {"path": "another-cache", "key": "${{ steps.compile_cache.outputs.cache-primary-key }}"}},
            {"with": {"path": "cache/units", "key": "unrelated-key"}},
        )
        for change in changes:
            job = copy.deepcopy(self.job)
            job["steps"][1].update(change)
            with self.subTest(change=change):
                self.assertFalse(approved_cache_save(job, "Save cache"))
        self.assertFalse(approved_cache_save(self.job, "Restore cache"))
        job = copy.deepcopy(self.job)
        job["steps"].reverse()
        self.assertFalse(approved_cache_save(job, "Save cache"))
        job = copy.deepcopy(self.job)
        job["steps"].append(copy.deepcopy(job["steps"][0]))
        self.assertFalse(approved_cache_save(job, "Save cache"))

    def test_static_binding_rejects_optional_restore_or_validation(self):
        declared = DeclaredReplayJob("ci.yml:tests", ReplayJob(
            "CI/Tests", ("Restore cache", "Save cache"), ("Save cache",)), ())
        self.assertEqual(_check_job(declared, self.job, "ci.yml"), [])
        invalid = DeclaredReplayJob("ci.yml:tests", ReplayJob(
            "CI/Tests", ("Restore cache", "Save cache"), ("Restore cache",)), ())
        self.assertTrue(_check_job(invalid, self.job, "ci.yml"))
