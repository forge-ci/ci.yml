"""Repository identities survive the fleet's GitHub transfers
(zackees/{ci.yml,setup-soldr} -> forge-ci/*): both names are accepted."""

from __future__ import annotations

import unittest

from ci_lint import repo_identity
from ci_lint.rules import cook_cache, macos_signing, setup_soldr_freshness, workflows
from ci_lint.rules.cache_static import _is_build_soldr
from ci_lint.schema import LINTER_RE

SHA = "a" * 40


class RepoIdentityTest(unittest.TestCase):
    def test_ct_002_linter_pin_accepts_both_names(self) -> None:
        for owner in ("forge-ci", "zackees"):
            m = LINTER_RE.match(f"{owner}/ci.yml@{SHA}")
            self.assertIsNotNone(m, owner)
        self.assertIsNone(LINTER_RE.match(f"someone/ci.yml@{SHA}"))

    def test_sec_004_float_exemption_accepts_both_names(self) -> None:
        for slug in repo_identity.SETUP_SOLDR_REPOS:
            self.assertTrue(workflows._is_sanctioned_setup_soldr_float(slug, "v0"), slug)
        self.assertFalse(workflows._is_sanctioned_setup_soldr_float("someone/setup-soldr", "v0"))

    def test_setup_soldr_slug_sites(self) -> None:
        for slug in repo_identity.SETUP_SOLDR_REPOS:
            self.assertIsNotNone(setup_soldr_freshness._SHA_PIN_RE.match(f"{slug}/cook@{SHA}"))
            self.assertIsNotNone(cook_cache._SETUP_SOLDR_RE.match(f"{slug}/cook@v0"))
            self.assertTrue(_is_build_soldr(f"{slug}@v0"))
            self.assertFalse(_is_build_soldr(f"{slug}/cleanup@v0"))
        self.assertIsNone(repo_identity.setup_soldr_prefix("someone/setup-soldr@v0"))

    def test_rel_006_signing_action_accepts_both_names(self) -> None:
        for slug in repo_identity.CI_YML_REPOS:
            self.assertIsNotNone(macos_signing._ACTION.match(f"{slug}/actions/app-signing-macos@{SHA}"))


if __name__ == "__main__":
    unittest.main()
