"""`ci-lint attest verify --required … --mode off|warn|enforce` (GATE-010).

The switch lets a fast CI step demand specific gates without changing what
`--mode off` does, so the requirement can ship as a warning and be tightened
later by flipping one value.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from ci_lint.tests.test_attestations import DEFINITION, GitCase, _git


class RequiredModeTest(GitCase):
    def setUp(self) -> None:
        super().setUp()
        # The definition is committed first: committing it later would change the
        # tree after an attestation is stamped, making every trailer stale.
        (self.repo / "ci-attestations.yml").write_text(DEFINITION, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "definition")

    def run_verify(self, *extra: str) -> tuple[int, str]:
        import argparse

        from ci_lint.attest_cli import register

        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["attest", "verify", "--repo", str(self.repo), "--commit", "HEAD", *extra])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = args.func(args)
        return code, out.getvalue()

    def test_missing_required_gate_warns_and_exits_zero(self):
        self.commit("only")
        code, out = _verify_with_required(self, "--required", "rust/all/fmt", "--mode", "warn")
        self.assertEqual(code, 0, out)
        self.assertIn("::warning title=Missing client attestation::", out)
        self.assertIn("rust/all/fmt not attested for tree", out)
        self.assertIn("Run ./push", out)

    def test_missing_required_gate_enforce_exits_one(self):
        self.commit("only")
        code, out = _verify_with_required(self, "--required", "rust/all/fmt", "--mode", "enforce")
        self.assertEqual(code, 1, out)
        self.assertIn("::error title=Missing client attestation::", out)

    def test_mode_off_is_silent_and_unchanged(self):
        self.commit("only")
        code, out = _verify_with_required(self, "--required", "rust/all/fmt")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Missing client attestation", out)

    def test_attested_required_gate_is_silent(self):
        self.commit("base")
        self.commit("feature")
        self.amend_with(self.attest("rust/all/fmt", "rust"))
        code, out = _verify_with_required(self, "--required", "rust/all/fmt", "--mode", "enforce")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Missing client attestation", out)

    def test_comma_separated_and_repeated_flags_accumulate(self):
        self.commit("only")
        code, out = _verify_with_required(
            self,
            "--required",
            "rust/all/fmt,rust/x86_64-unknown-linux-gnu/test",
            "--required",
            "rust/x86_64-unknown-linux-gnu/clippy",
            "--mode",
            "warn",
        )
        self.assertEqual(code, 0, out)
        for gate in (
            "rust/all/fmt",
            "rust/x86_64-unknown-linux-gnu/test",
            "rust/x86_64-unknown-linux-gnu/clippy",
        ):
            self.assertIn(gate, out)


def _verify_with_required(case: RequiredModeTest, *extra: str) -> tuple[int, str]:
    return case.run_verify(*extra)


if __name__ == "__main__":
    unittest.main()
