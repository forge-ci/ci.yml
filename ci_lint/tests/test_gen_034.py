"""GEN-034: auto-merge is policed, and a ready PR can land unattended.

`ci_lint.settings_audit.check_gen_034` is the audit verdict;
`ci_lint.merge_policy` is the fleet rollout that fixes the safe half (enable
auto-merge where required checks already police the branch). Every test
injects fake fetch/write functions -- no network.
"""

from __future__ import annotations

import unittest

from ci_lint.cargo_messages import JsonValue
from ci_lint.merge_policy import evaluate, rollout, to_json_dict
from ci_lint.settings_audit import RepoFacts, SettingsReply, check_gen_034

REPO = "forge-ci/example"
API = "https://api.github.com"
BRANCH = "main"

_REQUIRED_RULESET = {
    "enforcement": "active",
    "target": "branch",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "CI OK"}]},
        }
    ],
}

_QUEUE_RULESET = {
    "enforcement": "active",
    "target": "branch",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "rules": [
        {"type": "merge_queue"},
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "CI OK"}]},
        },
    ],
}

_NO_PROTECTION = SettingsReply(404, {"message": "Branch not protected"})


def _facts(allow_auto_merge: bool | None) -> tuple[SettingsReply, RepoFacts | None]:
    body = {"owner": {"type": "Organization"}, "default_branch": BRANCH, "allow_auto_merge": allow_auto_merge}
    return SettingsReply(200, body), RepoFacts(owner_type="Organization", allow_auto_merge=allow_auto_merge)


def _rulesets_reply(rulesets: list[JsonValue] | None) -> SettingsReply:
    return SettingsReply(200, rulesets) if rulesets is not None else SettingsReply(404, None)


def _findings(allow: bool, rulesets: list[JsonValue] | None) -> list:
    reply, facts = _facts(allow)
    return check_gen_034(REPO, reply, facts, _NO_PROTECTION, _rulesets_reply(rulesets), BRANCH)


def _policy(allow: bool | None, rulesets: list[JsonValue] | None):
    _, facts = _facts(allow)
    return evaluate(REPO, BRANCH, facts, _NO_PROTECTION, _rulesets_reply(rulesets))


class Gen034AuditTest(unittest.TestCase):
    def test_auto_merge_without_required_checks_is_a_violation(self) -> None:
        findings = _findings(True, [])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "violation")
        self.assertIn("unpoliced", findings[0].message)

    def test_auto_merge_policed_by_a_required_check_is_clean(self) -> None:
        self.assertEqual(_findings(True, [_REQUIRED_RULESET]), [])

    def test_auto_merge_with_a_queue_but_no_required_check_is_still_unpoliced(self) -> None:
        queue_only = [
            {
                "enforcement": "active",
                "target": "branch",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [{"type": "merge_queue"}],
            }
        ]
        findings = _findings(True, queue_only)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "violation")

    def test_auto_merge_policed_by_a_queue_rule_is_clean(self) -> None:
        self.assertEqual(_findings(True, [_QUEUE_RULESET]), [])

    def test_required_checks_without_auto_merge_or_queue_is_a_gap(self) -> None:
        findings = _findings(False, [_REQUIRED_RULESET])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "needs_review")
        self.assertIn("human merge click", findings[0].message)

    def test_auto_merge_off_with_no_required_checks_is_not_our_finding(self) -> None:
        self.assertEqual(_findings(False, []), [])

    def test_unreadable_settings_are_needs_review_never_a_pass(self) -> None:
        findings = check_gen_034(REPO, SettingsReply(403, {}), None, _NO_PROTECTION, SettingsReply(200, []), BRANCH)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "needs_review")

    def test_missing_payload_field_is_needs_review(self) -> None:
        reply = SettingsReply(200, {"owner": {"type": "Organization"}})
        facts = RepoFacts(owner_type="Organization", allow_auto_merge=None)
        findings = check_gen_034(REPO, reply, facts, _NO_PROTECTION, SettingsReply(200, []), BRANCH)
        self.assertEqual(findings[0].status, "needs_review")


class RolloutVerdictTest(unittest.TestCase):
    def test_verdicts(self) -> None:
        self.assertEqual(_policy(True, [_REQUIRED_RULESET]).verdict, "policed")
        self.assertEqual(_policy(True, []).verdict, "unpoliced")
        self.assertEqual(_policy(False, [_REQUIRED_RULESET]).verdict, "enable")
        self.assertEqual(_policy(False, [_QUEUE_RULESET]).verdict, "policed-by-queue")
        self.assertEqual(_policy(False, []).verdict, "no-checks")


class RolloutApplyTest(unittest.TestCase):
    def test_apply_enables_auto_merge_only_where_checks_exist_and_re_reads(self) -> None:
        state = {"allow_auto_merge": False}
        writes: list[dict[str, JsonValue]] = []

        def fetch(url: str, _token: str) -> tuple[int, JsonValue]:
            if url == f"{API}/orgs/forge-ci/repos?type=all&per_page=100&page=1":
                return 200, [{"full_name": REPO, "default_branch": BRANCH, "fork": False, "archived": False}]
            if url == f"{API}/repos/{REPO}":
                return 200, {
                    "owner": {"type": "Organization"},
                    "default_branch": BRANCH,
                    "allow_auto_merge": state["allow_auto_merge"],
                }
            if url == f"{API}/repos/{REPO}/branches/{BRANCH}/protection":
                return 404, {"message": "Branch not protected"}
            if url == f"{API}/repos/{REPO}/rulesets":
                return 200, [{"id": 1}]
            if url == f"{API}/repos/{REPO}/rulesets/1":
                return 200, _REQUIRED_RULESET
            raise AssertionError(f"unexpected URL {url}")

        def write(url: str, _token: str, method: str, payload: dict[str, JsonValue]) -> tuple[int, JsonValue]:
            self.assertEqual(method, "PATCH")
            writes.append({"url": url, "payload": payload})
            state["allow_auto_merge"] = bool(payload["allow_auto_merge"])
            return 200, {"ok": True}

        report = rollout(fetch, token="t", owners=("forge-ci",), repos=(), apply=True, write=write)
        self.assertEqual(report.applied, (f"{REPO}:enable",))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["url"], f"{API}/repos/{REPO}")
        self.assertEqual(writes[0]["payload"], {"allow_auto_merge": True})
        self.assertEqual(report.policies[0].verdict, "policed")
        self.assertFalse(report.dry_run)
        self.assertEqual(to_json_dict(report)["applied"], [f"{REPO}:enable"])

    def test_apply_disables_unpoliced_auto_merge(self) -> None:
        state = {"allow_auto_merge": True}
        payloads: list[JsonValue] = []

        def fetch(url: str, _token: str) -> tuple[int, JsonValue]:
            if url == f"{API}/repos/{REPO}":
                return 200, {
                    "owner": {"type": "Organization"},
                    "default_branch": BRANCH,
                    "allow_auto_merge": state["allow_auto_merge"],
                }
            if url == f"{API}/repos/{REPO}/branches/{BRANCH}/protection":
                return 404, None
            if url == f"{API}/repos/{REPO}/rulesets":
                return 200, []
            raise AssertionError(f"unexpected URL {url}")

        def write(url: str, _token: str, method: str, payload: dict[str, JsonValue]) -> tuple[int, JsonValue]:
            self.assertEqual(method, "PATCH")
            payloads.append(payload)
            state["allow_auto_merge"] = bool(payload["allow_auto_merge"])
            return 200, {"ok": True}

        report = rollout(fetch, token="t", owners=(), repos=(REPO,), apply=True, write=write)
        self.assertEqual(payloads, [{"allow_auto_merge": False}])
        self.assertEqual(report.applied, (f"{REPO}:disable",))
        self.assertEqual(report.policies[0].verdict, "no-checks")

    def test_dry_run_writes_nothing(self) -> None:
        def fetch(url: str, _token: str) -> tuple[int, JsonValue]:
            if url == f"{API}/repos/{REPO}":
                return 200, {
                    "owner": {"type": "Organization"},
                    "default_branch": BRANCH,
                    "allow_auto_merge": False,
                }
            if url == f"{API}/repos/{REPO}/branches/{BRANCH}/protection":
                return 404, None
            if url == f"{API}/repos/{REPO}/rulesets":
                return 200, [{"id": 1}]
            if url == f"{API}/repos/{REPO}/rulesets/1":
                return 200, _REQUIRED_RULESET
            raise AssertionError(f"unexpected URL {url}")

        def write(*_args, **_kwargs):  # pragma: no cover - must never be called
            raise AssertionError("dry run must not write")

        report = rollout(fetch, token="t", owners=(), repos=(REPO,), apply=False, write=write)
        self.assertEqual(report.applied, ())
        self.assertTrue(report.dry_run)
        self.assertEqual(report.policies[0].verdict, "enable")

    def test_a_repo_with_no_required_checks_is_never_enabled(self) -> None:
        def fetch(url: str, _token: str) -> tuple[int, JsonValue]:
            if url == f"{API}/repos/{REPO}":
                return 200, {"owner": {"type": "Organization"}, "default_branch": BRANCH, "allow_auto_merge": False}
            if url == f"{API}/repos/{REPO}/branches/{BRANCH}/protection":
                return 404, None
            if url == f"{API}/repos/{REPO}/rulesets":
                return 200, []
            raise AssertionError(f"unexpected URL {url}")

        def write(*_args, **_kwargs):  # pragma: no cover - must never be called
            raise AssertionError("auto-merge must not be enabled without required checks")

        report = rollout(fetch, token="t", owners=(), repos=(REPO,), apply=True, write=write)
        self.assertEqual(report.applied, ())
        self.assertEqual(report.policies[0].verdict, "no-checks")


if __name__ == "__main__":
    unittest.main()
