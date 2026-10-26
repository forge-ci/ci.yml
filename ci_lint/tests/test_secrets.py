"""SEC-001, SEC-002 -- ci_lint/rules/secrets_rules.py.

SEC-001 is a raw-text scan (works without PyYAML/yq); SEC-002 needs the
parsed job/permissions structure. Round-4B refinement: top-level
`permissions:` may be at most `contents: read` + `actions: read`; a job may
hold those two for free, but any wider per-job grant (e.g. `actions:
write`) must list that job id in ci.toml's `[allow].permissions."<grant>"`
array, and `id-token: write` still additionally requires
`environment: pypi` on the `publish` job specifically.
"""

from __future__ import annotations

import unittest

from ci_lint.rules.secrets_rules import check_sec_001, check_sec_002
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class SecretsFixtureTest(unittest.TestCase):
    def test_sec_001_forbidden_secret_reference(self) -> None:
        rules = [f.rule for f in check_sec_001(fixture("SEC-001", "red"))]
        self.assertIn("SEC-001", rules)
        rules = [f.rule for f in check_sec_001(fixture("SEC-001", "green"))]
        self.assertNotIn("SEC-001", rules)

    def test_sec_001_github_token_is_allowed(self) -> None:
        rules = [f.rule for f in check_sec_001(fixture("SEC-001", "green"))]
        self.assertEqual([], rules)

    def test_sec_001_honours_allow_secrets(self) -> None:
        """ci.yml#135: a secret named in [allow].secrets is not SEC-001; the
        same reference with an empty [allow].secrets (the red fixture) is."""

        repo = fixture("SEC-001", "green-allowed")
        ci = load_ci_toml(repo).ci
        self.assertEqual(("MY_TOKEN",), ci.allow.secrets)
        rules = [f.rule for f in check_sec_001(repo, ci.allow.secrets)]
        self.assertNotIn("SEC-001", rules)

        repo = fixture("SEC-001", "red")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_sec_001(repo, ci.allow.secrets)]
        self.assertIn("SEC-001", rules)

    @requires_yaml_tooling
    def test_sec_002_wide_top_level_permissions(self) -> None:
        repo = fixture("SEC-002", "red")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_sec_002(ci, repo)]
        self.assertIn("SEC-002", rules)

        repo = fixture("SEC-002", "green")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_sec_002(ci, repo)]
        self.assertNotIn("SEC-002", rules)

    @requires_yaml_tooling
    def test_sec_002_job_grant_needs_allow_permissions_entry(self) -> None:
        """Round-4B: `jobs.cache-maint.permissions = { actions: write }` is
        SEC-002 unless ci.toml's [allow].permissions."actions: write"
        lists the 'cache-maint' job id."""

        repo = fixture("SEC-002", "red-job-grant")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_sec_002(ci, repo)]
        self.assertIn("SEC-002", rules)

        repo = fixture("SEC-002", "green-job-grant")
        ci = load_ci_toml(repo).ci
        rules = [f.rule for f in check_sec_002(ci, repo)]
        self.assertNotIn("SEC-002", rules)

    @requires_yaml_tooling
    def test_sec_002_id_token_follows_declared_publishers(self) -> None:
        """ci.yml#134: 'id-token: write' is allowed on the 'publish' job in
        [publish].pypi.environment (not a hard-coded 'pypi'), and on an
        [allow].permissions-listed job that exchanges it through
        rust-lang/crates-io-auth-action (crates.io trusted publishing)."""

        repo = fixture("SEC-002", "green-oidc-publishers")
        ci = load_ci_toml(repo).ci
        findings = check_sec_002(ci, repo)
        self.assertEqual([], findings, msg=[f.render() for f in findings])

        repo = fixture("SEC-002", "red-oidc-publishers")
        ci = load_ci_toml(repo).ci
        messages = [f.message for f in check_sec_002(ci, repo) if f.rule == "SEC-002"]
        self.assertEqual(2, len(messages), msg=messages)
        self.assertTrue(any("jobs.publish." in m for m in messages), msg=messages)
        self.assertTrue(any("jobs.publish-crates." in m for m in messages), msg=messages)

    @requires_yaml_tooling
    def test_sec_002_actions_read_is_free_everywhere(self) -> None:
        """Evidence of need: zackees/template-python-rust-cmd#19 had to add
        two [[exceptions]] just for 'actions: read'; round-4B makes it free
        at both the top level and on any job, with no [allow].permissions
        entry required."""

        repo = fixture("SEC-002", "green-job-grant")
        ci = load_ci_toml(repo).ci
        findings = check_sec_002(ci, repo)
        self.assertFalse(
            [f for f in findings if "actions" in f.message and "read" in f.message],
            msg=[f.render() for f in findings],
        )


if __name__ == "__main__":
    unittest.main()
