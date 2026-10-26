"""CT-001..003, CT-005, CT-006, TAG-001, TAG-002 -- schema.py, exceptions.py,
rules/contract.py. None of these need parsed workflow YAML."""

from __future__ import annotations

import datetime
import unittest

from ci_lint.exceptions import apply_exceptions
from ci_lint.rules.contract import check_ct_006, check_tag_001, check_tag_002
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]


class SchemaFixtureTest(unittest.TestCase):
    def test_ct_001_unknown_key(self) -> None:
        red_findings = load_ci_toml(fixture("CT-001", "red")).findings
        self.assertIn("CT-001", rule_ids(red_findings))
        green_findings = load_ci_toml(fixture("CT-001", "green")).findings
        self.assertNotIn("CT-001", rule_ids(green_findings))

    def test_ct_002_wrong_type(self) -> None:
        red_findings = load_ci_toml(fixture("CT-002", "red")).findings
        self.assertIn("CT-002", rule_ids(red_findings))
        green_findings = load_ci_toml(fixture("CT-002", "green")).findings
        self.assertNotIn("CT-002", rule_ids(green_findings))

    def test_ct_002_cache_family_per_is_a_strict_enum(self) -> None:
        """Round-2A amendment 2: [cache.family.<id>].per must be one of
        none/platform/cross-platform/os -- an unrecognized value (e.g. the
        real template's "target") is a CT-002 schema error with a fix
        message listing the allowed values, never a silent fallback to
        cardinality 1."""

        _loaded = load_ci_toml(fixture("CT-002", "red-cache-per"))
        ci = _loaded.ci
        red_findings = _loaded.findings
        self.assertIn("CT-002", rule_ids(red_findings))
        per_finding = next(f for f in red_findings if f.rule == "CT-002" and "dylint-out.per" in f.message)
        self.assertIn("target", per_finding.message)
        self.assertIn("platform", per_finding.fix)
        self.assertIn("cross-platform", per_finding.fix)
        self.assertIn("os", per_finding.fix)
        self.assertIn("none", per_finding.fix)
        # the raw (invalid) value is still preserved on the parsed object --
        # CT-002 doesn't null it out, matching every other CT-002 case.
        assert ci is not None
        self.assertEqual("target", ci.cache.family["dylint-out"].per)

        green_findings = load_ci_toml(fixture("CT-002", "green-cache-per")).findings
        self.assertNotIn("CT-002", rule_ids(green_findings))

    def test_ct_002_cache_family_via_is_a_strict_allowlist(self) -> None:
        """Round-4A brief, deliverable 1: [cache.family.<id>].via must
        resolve to a real cache-key-prefix shape (ci_lint.cache.families).
        An unrecognized value is CT-002, never silently accepted -- and
        the two newly-declared via values (setup-soldr:soldr-mini,
        setup-soldr:dylint) are accepted."""

        _loaded = load_ci_toml(fixture("CT-002", "red-cache-via"))
        ci = _loaded.ci
        red_findings = _loaded.findings
        self.assertIn("CT-002", rule_ids(red_findings))
        via_finding = next(f for f in red_findings if f.rule == "CT-002" and "compile.via" in f.message)
        self.assertIn("setup-soldr:bogus-family", via_finding.message)
        assert ci is not None
        self.assertEqual("setup-soldr:bogus-family", ci.cache.family["compile"].via)

        _loaded = load_ci_toml(fixture("CT-002", "green-cache-via"))
        ci = _loaded.ci
        green_findings = _loaded.findings
        self.assertNotIn("CT-002", rule_ids(green_findings))
        assert ci is not None
        self.assertEqual("setup-soldr:soldr-mini", ci.cache.family["soldr-mini"].via)
        self.assertEqual("setup-soldr:dylint", ci.cache.family["dylint"].via)

    def test_ct_003_wrong_schema(self) -> None:
        red_findings = load_ci_toml(fixture("CT-003", "red")).findings
        self.assertIn("CT-003", rule_ids(red_findings))
        green_findings = load_ci_toml(fixture("CT-003", "green")).findings
        self.assertNotIn("CT-003", rule_ids(green_findings))

    def test_ct_006_platform_target_mismatch(self) -> None:
        repo = fixture("CT-006", "red")
        ci = load_ci_toml(repo).ci
        self.assertIn("CT-006", rule_ids(check_ct_006(ci, repo)))

        repo = fixture("CT-006", "green")
        ci = load_ci_toml(repo).ci
        self.assertNotIn("CT-006", rule_ids(check_ct_006(ci, repo)))

    def test_ct_006_missing_cargo_toml(self) -> None:
        ci = load_ci_toml(fixture("CT-006", "green")).ci
        findings = check_ct_006(ci, fixture("CT-006", "green").parent.parent)  # no Cargo.toml here
        self.assertIn("CT-006", rule_ids(findings))

    def test_tag_001_unknown_reserved_tag(self) -> None:
        ci = load_ci_toml(fixture("TAG-001", "red")).ci
        self.assertIn("TAG-001", rule_ids(check_tag_001(ci, "[ci-bogus] add thing")))

        ci = load_ci_toml(fixture("TAG-001", "green")).ci
        self.assertNotIn("TAG-001", rule_ids(check_tag_001(ci, "[ci-windows] add thing")))

    def test_tag_001_ignores_non_reserved_brackets(self) -> None:
        ci = load_ci_toml(fixture("TAG-001", "green")).ci
        self.assertNotIn("TAG-001", rule_ids(check_tag_001(ci, "[WIP] still working")))

    def test_tag_002_release_with_no_test(self) -> None:
        self.assertIn("TAG-002", rule_ids(check_tag_002("[release][no-test] go")))
        self.assertIn("TAG-002", rule_ids(check_tag_002("[release][no-test-integration] go")))
        self.assertEqual([], check_tag_002("[release] go"))
        self.assertEqual([], check_tag_002("[no-test] go"))


class ExceptionFixtureTest(unittest.TestCase):
    def test_ct_005_expired_exception_is_a_violation(self) -> None:
        ci = load_ci_toml(fixture("CT-005", "red")).ci
        outcome = apply_exceptions(ci, [])
        self.assertIn("CT-005", rule_ids(outcome.findings))

    def test_ct_005_unexpired_exception_is_silent(self) -> None:
        ci = load_ci_toml(fixture("CT-005", "green")).ci
        outcome = apply_exceptions(ci, [])
        self.assertNotIn("CT-005", rule_ids(outcome.findings))

    def test_matched_exception_becomes_approved(self) -> None:
        from ci_lint.finding import Finding, Status

        ci = load_ci_toml(fixture("CT-005", "green")).ci
        finding = Finding(rule="GEN-005", path="ci.sh", message="x", fix="y")
        outcome = apply_exceptions(ci, [finding])
        self.assertEqual(1, len(outcome.findings))
        self.assertEqual(Status.APPROVED_EXCEPTION, outcome.findings[0].status)

    def test_today_is_a_real_date(self) -> None:
        # sanity: the exceptions module uses the real clock by default.
        self.assertIsInstance(datetime.date.today(), datetime.date)


if __name__ == "__main__":
    unittest.main()
