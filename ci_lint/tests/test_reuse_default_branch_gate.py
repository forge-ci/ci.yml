"""GEN-021 Phase 2 (zackees/ci.yml#158): `[reuse.default-branch]` in
ci_lint.schema and `ci-lint gate --default-branch-reuse` in
ci_lint.runtime.gate.

Both halves are fail-closed by construction, and the tests below are
mostly about that: a document that is absent, wrong-schema, shadow-mode,
about a different sha, about a different event, or about a different lane
digest must never turn a skipped required job into a pass. The one
positive case is the only thing that may.

Inline documents rather than fixtures on purpose: every case differs from
the happy path by exactly one field, and a fixture directory would hide
that in a diff of JSON files instead of showing it in a diff of Python.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from ci_lint.github_api import FetchFn, GitHubApiError, JsonValue
from ci_lint.runtime.gate import compute_gate, parse_default_branch_reuse, render_text
from ci_lint.schema import load_ci_toml

PUSH_SHA = "deadbeef00000000000000000000000000000000"
HEAD_SHA = "1111111111111111111111111111111111111111"
RUN_ID = 555
PR = 1798


def _plan() -> dict[str, JsonValue]:
    return {
        "required_jobs": ["static", "fast", "dylint"],
        "lane_digests": {"fast": "abc123", "dylint": "def456"},
        "mergeable": True,
    }


def _needs() -> dict[str, JsonValue]:
    return {
        "static": {"result": "success"},
        "fast": {"result": "skipped"},
        "dylint": {"result": "skipped"},
    }


def _doc(**overrides: JsonValue) -> dict[str, JsonValue]:
    doc: dict[str, JsonValue] = {
        "schema": 1,
        "command": "reuse-check",
        "repo": "owner/repo",
        "sha": PUSH_SHA,
        "event_name": "push",
        "mode": "enforce",
        "reuse": True,
        "would_reuse": True,
        "reason": "verified",
        "pr": PR,
        "pr_head_sha": HEAD_SHA,
        "run_id": RUN_ID,
        "jobs": [
            {"name": "fast [abc123]", "job_id": 9001, "conclusion": "success"},
            {"name": "dylint [def456]", "job_id": 9002, "conclusion": "success"},
        ],
    }
    doc.update(overrides)
    return doc


def _matrix_fetch(url: str, token: str) -> JsonValue:
    assert token
    return {"conclusion": "success", "head_sha": HEAD_SHA,
            "name": "platform-build (linux-x64) (on ubuntu-24.04, soldr) [pb01]"}


def _good_fetch(url: str, token: str) -> JsonValue:
    assert token
    if "9001" in url:
        return {"conclusion": "success", "head_sha": HEAD_SHA, "name": "fast [abc123]"}
    if "9002" in url:
        return {"conclusion": "success", "head_sha": HEAD_SHA, "name": "dylint [def456]"}
    raise AssertionError(f"unexpected URL {url}")


def _gate(doc: JsonValue | None, fetch: FetchFn | None = _good_fetch) -> object:
    return compute_gate(
        _plan(),
        _needs(),
        fetch=fetch,
        token="t" if fetch else None,
        repo="owner/repo" if fetch else None,
        default_branch_reuse=doc,
        push_sha=PUSH_SHA,
    )


def _skipped_ok(report: object, job_id: str) -> bool:
    for status in report.statuses:  # type: ignore[attr-defined]
        if status.job_id == job_id:
            return status.ok
    raise AssertionError(f"no status for {job_id}")


class ParseDefaultBranchReuseTest(unittest.TestCase):
    def test_accepts_a_valid_enforce_document(self) -> None:
        parsed = parse_default_branch_reuse(_doc(), sha=PUSH_SHA)
        self.assertTrue(parsed.usable)
        self.assertEqual(parsed.reason, "verified")
        assert parsed.reuse is not None
        self.assertEqual(parsed.reuse.pr, PR)
        self.assertEqual(parsed.reuse.run_id, RUN_ID)
        self.assertEqual(
            [j.name for j in parsed.reuse.jobs], ["fast [abc123]", "dylint [def456]"]
        )

    def test_rejects_a_shadow_document(self) -> None:
        """A shadow document describes what *could* be skipped. It must never
        skip anything -- `would_reuse` alone is not a proof."""

        parsed = parse_default_branch_reuse(_doc(mode="shadow", reuse=False), sha=PUSH_SHA)
        self.assertFalse(parsed.usable)
        self.assertIn("never reuses", parsed.reason)

    def test_rejects_wrong_schema(self) -> None:
        parsed = parse_default_branch_reuse(_doc(schema=2), sha=PUSH_SHA)
        self.assertFalse(parsed.usable)

    def test_rejects_a_non_push_event(self) -> None:
        parsed = parse_default_branch_reuse(_doc(event_name="pull_request"), sha=PUSH_SHA)
        self.assertFalse(parsed.usable)

    def test_rejects_another_pushs_sha(self) -> None:
        parsed = parse_default_branch_reuse(_doc(), sha="f" * 40)
        self.assertFalse(parsed.usable)

    def test_skips_the_sha_check_when_the_push_sha_is_unknown(self) -> None:
        """Locally there is no GITHUB_SHA; the document is still parsed so
        the live re-verification below is what decides. An unknown sha is
        not itself a reason to discard otherwise-checkable evidence."""

        parsed = parse_default_branch_reuse(_doc(), sha=None)
        self.assertTrue(parsed.usable)
        self.assertEqual(parsed.reason, "verified")

    def test_rejects_a_document_with_no_proving_jobs(self) -> None:
        parsed = parse_default_branch_reuse(_doc(jobs=[]), sha=PUSH_SHA)
        self.assertFalse(parsed.usable)

    def test_rejects_a_non_object(self) -> None:
        parsed = parse_default_branch_reuse([], sha=PUSH_SHA)  # type: ignore[arg-type]
        self.assertFalse(parsed.usable)


class GateDefaultBranchReuseTest(unittest.TestCase):
    def test_a_proven_skip_passes_and_names_the_pr(self) -> None:
        report = _gate(_doc())
        self.assertTrue(report.ok)
        self.assertTrue(_skipped_ok(report, "fast"))
        self.assertTrue(_skipped_ok(report, "dylint"))
        fast = next(s for s in report.statuses if s.job_id == "fast")
        self.assertEqual(fast.reused_from_pr, PR)
        self.assertEqual(fast.reused_from_run, RUN_ID)
        self.assertIn(f"reused from PR #{PR} run {RUN_ID}", render_text(report))

    def test_matching_a_bare_job_id_also_works(self) -> None:
        """A workflow whose display name *is* its id needs no digest."""

        report = _gate(
            _doc(jobs=[{"name": "fast", "job_id": 9001}]),
            fetch=lambda url, token: {"conclusion": "success", "head_sha": HEAD_SHA, "name": "fast"},
        )
        self.assertTrue(_skipped_ok(report, "fast"))

    def test_matching_the_matrix_display_name_shape(self) -> None:
        """A matrix lane's display name carries the platform, its host, and
        its tool between the job id and the digest -- a middle this gate
        cannot reconstruct. It must still match, on the id plus the digest."""

        plan = {"required_jobs": ["platform-build"], "lane_digests": {"platform:linux-x64": "pb01"}, "mergeable": True}
        doc = _doc(
            sha=PUSH_SHA,
            jobs=[{"name": "platform-build (linux-x64) (on ubuntu-24.04, soldr) [pb01]", "job_id": 9100}],
        )
        report = compute_gate(
            plan, {"platform-build": {"result": "skipped"}}, fetch=_matrix_fetch, token="t",
            repo="owner/repo", default_branch_reuse=doc, push_sha=PUSH_SHA,
        )
        self.assertTrue(report.ok)
        self.assertTrue(_skipped_ok(report, "platform-build"))

    def test_a_matrix_name_for_another_lane_digest_is_rejected(self) -> None:
        """The shape matches but the tier does not -- which is exactly what
        the digest is there to catch."""

        plan = {"required_jobs": ["platform-build"], "lane_digests": {"platform:linux-x64": "pb01"}, "mergeable": True}
        doc = _doc(jobs=[{"name": "platform-build (linux-x64) (on ubuntu-24.04, soldr) [OLD]", "job_id": 9100}])
        report = compute_gate(
            plan, {"platform-build": {"result": "skipped"}}, fetch=_matrix_fetch, token="t",
            repo="owner/repo", default_branch_reuse=doc, push_sha=PUSH_SHA,
        )
        self.assertFalse(_skipped_ok(report, "platform-build"))
        self.assertFalse(report.ok)

    def test_a_name_merely_containing_the_id_is_not_a_match(self) -> None:
        """Guarding against a permissive prefix match letting an unrelated
        job's proof stand in for this one."""

        report = _gate(_doc(jobs=[{"name": "fast-extra [abc123]", "job_id": 9001}]))
        self.assertFalse(_skipped_ok(report, "fast"))

    def test_proving_id_of_another_successful_job_is_rejected(self) -> None:
        """#362: the transport's matching name cannot rename the live job."""
        report = _gate(_doc(jobs=[{"name": "fast [abc123]", "job_id": 9002}]))
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertFalse(report.ok)

    def test_live_job_without_name_cannot_prove_a_required_skip(self) -> None:
        report = _gate(
            _doc(), fetch=lambda url, token: {"conclusion": "success", "head_sha": HEAD_SHA}
        )
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertFalse(report.ok)

    def test_a_wrong_lane_digest_is_not_a_proof(self) -> None:
        """The digest is the "same tier" proof. A document naming a digest
        the current plan did not select proves a different lane set."""

        report = _gate(_doc(jobs=[{"name": "fast [SOMETHING-ELSE]", "job_id": 9001}]))
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertFalse(report.ok)

    def test_a_shadow_document_leaves_the_skip_a_failure(self) -> None:
        report = _gate(_doc(mode="shadow", reuse=False))
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertFalse(report.ok)

    def test_no_token_is_needs_review_not_a_silent_pass(self) -> None:
        report = _gate(_doc(), fetch=None)
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertFalse(report.ok)
        self.assertTrue(any(f.status.value == "needs_review" for f in report.findings))

    def test_an_unreachable_api_is_needs_review(self) -> None:
        def boom(url: str, token: str) -> JsonValue:
            raise GitHubApiError("boom")

        report = _gate(_doc(), fetch=boom)
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertTrue(any(f.status.value == "needs_review" for f in report.findings))

    def test_a_job_that_reevaluated_to_failure_is_rejected(self) -> None:
        """The document was written when the job was green; the live check
        is what makes it true now."""

        report = _gate(
            _doc(),
            fetch=lambda url, token: {"conclusion": "failure", "head_sha": HEAD_SHA},
        )
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertFalse(report.ok)

    def test_a_job_on_another_head_is_rejected(self) -> None:
        report = _gate(
            _doc(),
            fetch=lambda url, token: {"conclusion": "success", "head_sha": "2" * 40,
                                      "name": "fast [abc123]"},
        )
        self.assertFalse(_skipped_ok(report, "fast"))

    def _queue_fetch(self, url: str, token: str) -> JsonValue:
        # merge_group evidence: the proving jobs ran on the pushed commit.
        name = "fast [abc123]" if "9001" in url else "dylint [def456]"
        return {"conclusion": "success", "head_sha": PUSH_SHA, "name": name}

    def test_queue_evidence_is_verified_against_its_proving_sha(self) -> None:
        report = _gate(_doc(proving_sha=PUSH_SHA), fetch=self._queue_fetch)
        self.assertTrue(_skipped_ok(report, "fast"))
        self.assertTrue(report.ok)

    def test_queue_jobs_without_a_proving_sha_are_checked_against_the_pr_head(self) -> None:
        report = _gate(_doc(), fetch=self._queue_fetch)
        self.assertFalse(_skipped_ok(report, "fast"))

    def test_a_proving_sha_does_not_accept_jobs_on_a_third_commit(self) -> None:
        report = _gate(
            _doc(proving_sha=PUSH_SHA),
            fetch=lambda url, token: {"conclusion": "success", "head_sha": HEAD_SHA, "name": "fast [abc123]"},
        )
        self.assertFalse(_skipped_ok(report, "fast"))

    def test_a_document_without_a_job_id_is_needs_review(self) -> None:
        """A name we cannot re-fetch is not a proof, and is not a failure
        either -- it is unverifiable, which is its own state."""

        report = _gate(_doc(jobs=[{"name": "fast [abc123]"}]))
        self.assertFalse(_skipped_ok(report, "fast"))
        self.assertTrue(any(f.status.value == "needs_review" for f in report.findings))

    def test_omitting_the_flag_changes_nothing(self) -> None:
        """The pre-#158 behavior must be byte-identical when the new flag is
        absent, so every existing ci.toml workflow is unaffected."""

        report = compute_gate(_plan(), _needs())
        self.assertFalse(report.ok)
        self.assertFalse(_skipped_ok(report, "fast"))

    def test_a_job_absent_from_the_document_still_fails(self) -> None:
        """One verified job must not carry an unverified sibling."""

        report = _gate(_doc(jobs=[{"name": "fast [abc123]", "job_id": 9001}]))
        self.assertTrue(_skipped_ok(report, "fast"))
        self.assertFalse(_skipped_ok(report, "dylint"))
        self.assertFalse(report.ok)

    def test_json_output_carries_the_provenance(self) -> None:
        report = _gate(_doc())
        from ci_lint.runtime.gate import to_json_dict

        payload = to_json_dict(report)
        fast = next(s for s in payload["statuses"] if s["job_id"] == "fast")
        self.assertEqual(fast["reused_from_pr"], PR)
        self.assertEqual(fast["reused_from_run"], RUN_ID)


class ReuseSchemaTest(unittest.TestCase):
    """`[reuse.default-branch]` -- ci_lint.schema._parse_reuse."""

    # The canonical example, so the only findings under test are the ones
    # this table causes. Its placeholder `linter` is the one field that is
    # not a real value, so it is pinned to a real-looking SHA here.
    _RAW = (
        pathlib.Path(__file__).resolve().parents[2] / "examples" / "rust-pypi-app" / "ci.toml"
    ).read_text(encoding="utf-8").replace(
        "zackees/ci.yml@<40-hex-sha>", "zackees/ci.yml@" + "a" * 40
    )
    # The example now carries its own `[reuse.default-branch]` (issue #160),
    # which every case here would then collide with by appending a second
    # table. Drop it so each case's table is the only one under test.
    _START = "# ── Default-branch verified reuse"
    _END = "# ── Allowlists"
    BASE = (
        _RAW[: _RAW.index(_START)] + _RAW[_RAW.index(_END) :]
        if _START in _RAW and _END in _RAW
        else _RAW
    )

    def _load(self, body: str) -> tuple[object, list[str]]:
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "ci.toml").write_text(self.BASE + "\n" + body, encoding="utf-8")
        _loaded = load_ci_toml(root)
        ci = _loaded.ci
        findings = _loaded.findings
        return (ci.reuse if ci else None), [f.message for f in findings if f.rule.startswith("CT-")]

    def test_absent_table_defaults_to_off(self) -> None:
        reuse, _ = self._load("")
        self.assertEqual(reuse.mode, "off")
        self.assertEqual(reuse.workflows, ("ci.yml",))
        self.assertEqual(reuse.required_jobs, None)  # "plan"
        self.assertEqual(reuse.max_age_hours, 24.0)
        self.assertTrue(reuse.nightly)

    def test_a_bare_reuse_table_is_an_opt_out_not_an_error(self) -> None:
        reuse, findings = self._load("[reuse]\n")
        self.assertEqual(reuse.mode, "off")
        self.assertEqual(findings, [])

    def test_reads_every_field(self) -> None:
        reuse, _ = self._load(
            '[reuse.default-branch]\nmode = "enforce"\nworkflows = ["ci.yml"]\n'
            'required-jobs = ["Build linux-x64", "Dylint"]\n'
            'exempt-jobs = ["static"]\nmax-age-hours = 12\nnightly = false\n'
        )
        self.assertEqual(reuse.mode, "enforce")
        self.assertEqual(reuse.required_jobs, ("Build linux-x64", "Dylint"))
        self.assertEqual(reuse.exempt_jobs, ("static",))
        self.assertEqual(reuse.max_age_hours, 12.0)
        self.assertFalse(reuse.nightly)

    def test_accepts_a_fractional_max_age(self) -> None:
        reuse, _ = self._load('[reuse.default-branch]\nmax-age-hours = 0.5\n')
        self.assertEqual(reuse.max_age_hours, 0.5)

    def test_rejects_an_unknown_mode(self) -> None:
        _, findings = self._load('[reuse.default-branch]\nmode = "sometimes"\n')
        self.assertTrue(any("mode" in m for m in findings), findings)

    def test_rejects_a_max_age_out_of_range(self) -> None:
        _, findings = self._load("[reuse.default-branch]\nmax-age-hours = 999\n")
        self.assertTrue(any("max-age-hours" in m for m in findings), findings)

    def test_rejects_a_zero_max_age(self) -> None:
        _, findings = self._load("[reuse.default-branch]\nmax-age-hours = 0\n")
        self.assertTrue(any("max-age-hours" in m for m in findings), findings)

    def test_rejects_a_bool_max_age(self) -> None:
        """bool is an int subclass in Python; `max-age-hours = true` is a
        user error, not a freshness window of 1 hour."""

        _, findings = self._load("[reuse.default-branch]\nmax-age-hours = true\n")
        self.assertTrue(any("max-age-hours" in m for m in findings), findings)

    def test_rejects_a_bare_string_other_than_plan(self) -> None:
        _, findings = self._load('[reuse.default-branch]\nrequired-jobs = "everything"\n')
        self.assertTrue(any("required-jobs" in m for m in findings), findings)

    def test_rejects_duplicate_required_jobs(self) -> None:
        _, findings = self._load('[reuse.default-branch]\nrequired-jobs = ["a", "a"]\n')
        self.assertTrue(any("duplicate" in m for m in findings), findings)

    def test_rejects_an_empty_required_jobs_array(self) -> None:
        _, findings = self._load("[reuse.default-branch]\nrequired-jobs = []\n")
        self.assertTrue(any("required-jobs" in m for m in findings), findings)

    def test_unknown_key_is_ct_001(self) -> None:
        _, findings = self._load("[reuse.default-branch]\nbogus = 1\n")
        self.assertTrue(any("bogus" in m for m in findings), findings)

    def test_a_workflow_not_declared_in_allow_is_rejected(self) -> None:
        """Reading a workflow the repository does not ship would make the
        decision silently prove nothing at all."""

        _, findings = self._load(
            '[reuse.default-branch]\nmode = "shadow"\nworkflows = ["nowhere.yml"]\n'
        )
        self.assertTrue(
            any("nowhere.yml" in m and "not declared" in m for m in findings), findings
        )

    def test_mode_off_skips_the_allow_cross_check(self) -> None:
        """A repository that has not opted in must not be asked to declare
        anything it never uses."""

        _, findings = self._load('[reuse.default-branch]\nmode = "off"\nworkflows = ["nowhere.yml"]\n')
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
