"""GEN-005 -- ci_lint/rules/shell.py."""

from __future__ import annotations

import unittest

from ci_lint.rules.shell import check_group3, cpp_allowlisted_command
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


@requires_yaml_tooling
class ShellFixtureTest(unittest.TestCase):
    def test_gen_005_multiline_run_step(self) -> None:
        repo = fixture("GEN-005", "red")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_group3(ci, repo)]
        self.assertIn("GEN-005", rules)

        repo = fixture("GEN-005", "green")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_group3(ci, repo)]
        self.assertNotIn("GEN-005", rules)

    def test_gen_005_cpp_allowlist(self) -> None:
        # zackees/ci.yml#393: one cmake configure / cmake --build / ctest
        # command may wrap over backslash-continued lines; two commands,
        # control syntax, or a non-allowlisted cmake mode still may not.
        repo = fixture("GEN-005", "green-cmake")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], [f for f in check_group3(ci, repo) if f.rule == "GEN-005"])

        repo = fixture("GEN-005", "red-cmake")
        ci = load_ci_toml(repo).ci
        found = [f for f in check_group3(ci, repo) if f.rule == "GEN-005"]
        self.assertEqual(4, len(found), [f.message for f in found])
        by_step = {f.message.split(":", 1)[0]: f for f in found}
        self.assertIn("more than one line", by_step["jobs.build.steps[2]"].message)
        self.assertIn("$(", by_step["jobs.build.steps[3]"].message)
        self.assertIn("--parallel", by_step["jobs.build.steps[3]"].fix)
        self.assertIn("||", by_step["jobs.build.steps[4]"].message)
        self.assertIn("more than one line", by_step["jobs.build.steps[5]"].message)


class CppAllowlistTest(unittest.TestCase):
    def test_recognizes_the_three_commands(self) -> None:
        self.assertEqual("cmake configure", cpp_allowlisted_command("cmake -S . -B build \\\n  -DX=1\n"))
        self.assertEqual("cmake configure", cpp_allowlisted_command("cmake --preset=ci"))
        self.assertEqual("cmake --build", cpp_allowlisted_command("CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build b"))
        self.assertEqual("ctest", cpp_allowlisted_command("/usr/bin/ctest -j --test-dir b"))
        self.assertIsNone(cpp_allowlisted_command("cmake -E echo hi"))
        self.assertIsNone(cpp_allowlisted_command("cmake -S . -B b\nctest"))
        self.assertIsNone(cpp_allowlisted_command("make -j"))


if __name__ == "__main__":
    unittest.main()
