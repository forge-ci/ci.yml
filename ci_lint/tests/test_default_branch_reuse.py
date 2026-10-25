"""`ci-lint reuse-check` / `ci-lint reuse-report` -- GEN-021 verified reuse on
default-branch pushes (ci_lint.default_branch_reuse, zackees/ci.yml#156).

Fixtures under fixtures/runtime/default-branch-reuse/ are live `reuse-check
--record` recordings against zackees/clud (2026-09-30, GET-only), trimmed to
the fields ci_lint reads:

  - clud-pr1643.json: squash merge 1ab2f4e4 of PR #1643; tree == head
    8f8bc6c4's tree; PR run 36742138072 green in the current (sharded)
    minimal tier. The happy path.
  - clud-pr1575.json: squash merge 37288c01 of PR #1575, one of the seven
    red clud `main` runs caused by the `test_codex_installer_rm` flake; tree
    == head, PR run 36630585480 green (pre-shard job names). Reuse would
    have skipped the flaky re-run.
  - clud-pr1630.json: squash merge 8c20d604 of PR #1630; merged tree !=
    head tree (a real merge interaction broke `main`). Must never reuse.
  - clud-push-runs.json: the four `main` push runs `reuse-report` walks.

Every other case is a documented mutation of one of those recordings. No
network: replay raises GitHubApiError for any URL not in the table.
"""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ci_lint import cli
from ci_lint.default_branch_reuse import (
    DEFAULT_MIN_RUNS,
    REASONS,
    ReportRow,
    ReuseReport,
    ReuseRequest,
    decide,
    github_output_lines,
    normalize_workflow,
    render_step_summary,
    run_report,
    to_json_dict,
)
from ci_lint.fleet_cli import replay_fetch
from ci_lint.tests.helpers import FIXTURES

DIR = FIXTURES / "runtime" / "default-branch-reuse"
REPO = "zackees/clud"
API = "https://api.github.com/repos/zackees/clud"

SHA_1643 = "1ab2f4e49d7eee355dda5878e9dc1f354886f81c"
HEAD_1643 = "8f8bc6c40d3f277730ef843e7a843d3b10963023"
RUN_1643 = 36742138072
SHA_1575 = "37288c0185deaab0e1babc5ec1e67ac0e1fb692b"
SHA_1630 = "8c20d6044e963952c7b84a07481b16fe2bc36b18"

SHARDED = (
    "Static checks",
    "Dylint / Dylint",
    "Clippy linux-x64 / x86_64-unknown-linux-gnu",
    "Build linux-x64 / x86_64-unknown-linux-gnu",
    "Test linux-x64 (unit) (rust) / x86_64-unknown-linux-gnu unit",
    "Test linux-x64 (unit) (py1of2) / x86_64-unknown-linux-gnu unit",
    "Test linux-x64 (unit) (py2of2) / x86_64-unknown-linux-gnu unit",
)
PRE_SHARD = (
    "Static checks",
    "Dylint / Dylint",
    "Build linux-x64 / x86_64-unknown-linux-gnu",
    "Test linux-x64 (unit) / x86_64-unknown-linux-gnu unit",
)
# Present in both job-name eras (before and after clud#1615/#1619).
STABLE = ("Static checks", "Dylint / Dylint", "Build linux-x64 / x86_64-unknown-linux-gnu")

PULLS_1643 = f"{API}/commits/{SHA_1643}/pulls?per_page=100"
RUNS_1643 = f"{API}/actions/runs?head_sha={HEAD_1643}&event=pull_request&per_page=100&page=1"
JOBS_1643 = f"{API}/actions/runs/{RUN_1643}/jobs?filter=latest&per_page=100&page=1"

Table = dict[str, list[object]]


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def page_url(url: str, page: int) -> str:
    return f"{url.rsplit('&page=', 1)[0]}&page={page}"


def load(name: str) -> Table:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


def req(sha: str = SHA_1643, now: str = "2026-09-30T16:18:16Z", jobs: tuple[str, ...] = SHARDED, **kw: object) -> ReuseRequest:
    base = ReuseRequest(
        repo=REPO,
        sha=sha,
        workflows=(".github/workflows/ci.yml",),
        required_jobs=jobs,
        now=_t(now),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def run(table: Table, request: ReuseRequest | None = None):
    return decide(request or req(), replay_fetch(table), "t")


def body(table: Table, url: str):
    return table[url][1]


def jobs_of(table: Table) -> list[dict[str, object]]:
    return body(table, JOBS_1643)["jobs"]  # type: ignore[index,return-value]


def job(table: Table, name: str) -> dict[str, object]:
    return next(j for j in jobs_of(table) if j["name"] == name)


def ci_run(table: Table) -> dict[str, object]:
    return next(r for r in body(table, RUNS_1643)["workflow_runs"] if r["id"] == RUN_1643)  # type: ignore[index]


class VerifiedTest(unittest.TestCase):
    def test_evidence_output_escapes_lines_and_preserves_shadow_decision(self) -> None:
        decision = replace(run(load("clud-pr1643.json"), req(mode="shadow")),
                           detail="untrusted text\nreuse=true\n\"quoted\" λ")
        lines = github_output_lines(decision)
        evidence = next(line.split("=", 1)[1] for line in lines if line.startswith("evidence_json="))
        self.assertEqual(len(evidence.splitlines()), 1)
        self.assertEqual(json.loads(evidence), to_json_dict(decision))
        self.assertFalse(json.loads(evidence)["reuse"])

    def test_squash_merge_pr1643_is_verified_in_five_calls(self) -> None:
        d = run(load("clud-pr1643.json"))
        self.assertEqual((d.verdict, d.reason), (True, "verified"))
        self.assertTrue(d.reuse)
        self.assertEqual(d.pr, 1643)
        self.assertEqual(d.pr_head_sha, HEAD_1643)
        self.assertEqual(d.tree, "3410191f9f21351dea1c1504cc4fafc08b4edf55")
        self.assertEqual([r.run_id for r in d.runs], [RUN_1643])
        self.assertEqual([j.name for j in d.jobs], list(SHARDED))
        self.assertEqual(d.api_calls, 5)

    def test_flake_class_pr1575_would_have_been_skipped(self) -> None:
        d = run(load("clud-pr1575.json"), req(SHA_1575, "2026-09-29T21:11:10Z", PRE_SHARD))
        self.assertEqual((d.verdict, d.reason, d.pr), (True, "verified", 1575))
        self.assertEqual(d.runs[0].run_id, 36630585480)

    def test_rebase_merge_tip_with_new_sha_but_same_tree_is_verified(self) -> None:
        # A rebase merge rewrites every PR commit: the pushed tip X differs
        # from the PR head, GitHub records merge_commit_sha = X (the commit
        # the base branch was updated to), and only X's tree matters.
        table = load("clud-pr1643.json")
        tip = "c" * 40
        pulls = copy.deepcopy(body(table, PULLS_1643))
        pulls[0]["merge_commit_sha"] = tip  # type: ignore[index]
        commit = copy.deepcopy(body(table, f"{API}/git/commits/{SHA_1643}"))
        commit["sha"] = tip  # type: ignore[index]
        commit["parents"] = [{"sha": "d" * 40}]  # type: ignore[index]
        table[f"{API}/commits/{tip}/pulls?per_page=100"] = [200, pulls]
        table[f"{API}/git/commits/{tip}"] = [200, commit]
        d = run(table, req(sha=tip))
        self.assertEqual((d.verdict, d.reason), (True, "verified"))
        self.assertNotEqual(d.request.sha, d.pr_head_sha)

    def test_merge_commit_with_two_parents_and_head_tree_is_verified(self) -> None:
        table = load("clud-pr1643.json")
        commit = body(table, f"{API}/git/commits/{SHA_1643}")
        commit["parents"] = [{"sha": "e" * 40}, {"sha": HEAD_1643}]  # type: ignore[index]
        self.assertTrue(run(table).verdict)

    def test_newer_cancelled_run_is_passed_over(self) -> None:
        table = load("clud-pr1643.json")
        newer = copy.deepcopy(ci_run(table))
        newer.update(id=RUN_1643 + 5, conclusion="cancelled", created_at="2026-09-30T16:17:00Z")
        body(table, RUNS_1643)["workflow_runs"].append(newer)  # type: ignore[index]
        body(table, RUNS_1643)["total_count"] += 1  # type: ignore[index]
        d = run(table)
        self.assertEqual((d.verdict, d.runs[0].run_id), (True, RUN_1643))

    def test_run_created_after_the_clock_is_ignored(self) -> None:
        table = load("clud-pr1643.json")
        later = copy.deepcopy(ci_run(table))
        later.update(id=RUN_1643 + 9, conclusion="failure", created_at="2026-09-30T17:00:00Z")
        body(table, RUNS_1643)["workflow_runs"].append(later)  # type: ignore[index]
        self.assertTrue(run(table).verdict)

    def test_jobs_listing_is_paginated(self) -> None:
        table = load("clud-pr1643.json")
        real = jobs_of(table)
        filler = [dict(real[4], id=900000 + i, name=f"filler {i}") for i in range(100)]
        page1 = real + filler[: 100 - len(real)]
        page2 = filler[100 - len(real) :]
        table[JOBS_1643] = [200, {"total_count": len(page1) + len(page2), "jobs": page1}]
        table[page_url(JOBS_1643, 2)] = [200, {"total_count": len(page1) + len(page2), "jobs": page2}]
        d = run(table)
        self.assertEqual((d.verdict, d.api_calls), (True, 6))

    def test_shadow_mode_reports_would_reuse_but_never_reuses(self) -> None:
        d = run(load("clud-pr1643.json"), req(mode="shadow"))
        self.assertEqual((d.reuse, d.would_reuse, d.reason), (False, True, "verified"))
        doc = to_json_dict(d)
        self.assertEqual((doc["reuse"], doc["would_reuse"], doc["mode"]), (False, True, "shadow"))
        self.assertIn("reuse=false", github_output_lines(d))
        self.assertIn("would_reuse=true", github_output_lines(d))
        self.assertIn("(shadow)", render_step_summary(d))


QUEUE_RUNS_1630 = f"{API}/actions/runs?head_sha={SHA_1630}&event=merge_group&per_page=100&page=1"
QUEUE_RUN = RUN_1643 + 100
QUEUE_JOBS = f"{API}/actions/runs/{QUEUE_RUN}/jobs?filter=latest&per_page=100&page=1"
QUEUE_NOW = "2026-09-30T16:18:16Z"


def queue_table(conclusion: str = "success") -> Table:
    """PR #1630's tree mismatch, plus a merge_group run on the merged commit
    itself: PR #1643's recorded run and jobs, re-pointed at SHA_1630."""

    table = load("clud-pr1630.json")
    donor = load("clud-pr1643.json")
    queue_run = copy.deepcopy(ci_run(donor))
    queue_run.update(id=QUEUE_RUN, head_sha=SHA_1630, event="merge_group", conclusion=conclusion)
    table[QUEUE_RUNS_1630] = [200, {"total_count": 1, "workflow_runs": [queue_run]}]
    jobs = copy.deepcopy(body(donor, JOBS_1643))
    for item in jobs["jobs"]:  # type: ignore[index]
        item["run_id"] = QUEUE_RUN
    table[QUEUE_JOBS] = [200, jobs]
    return table


class QueueEvidenceTest(unittest.TestCase):
    def test_queue_run_on_the_pushed_commit_proves_a_mismatched_tree(self) -> None:
        d = run(queue_table(), req(SHA_1630, QUEUE_NOW, queue_evidence=True))
        self.assertEqual((d.verdict, d.reason, d.pr), (True, "verified", 1630), d.detail)
        self.assertEqual([r.run_id for r in d.runs], [QUEUE_RUN])
        self.assertIn("merge_group run on", d.detail)
        self.assertIn("tree-mismatch on the head", d.detail)

    def test_queue_evidence_is_opt_in(self) -> None:
        d = run(queue_table(), req(SHA_1630, QUEUE_NOW))
        self.assertEqual((d.verdict, d.reason), (False, "tree-mismatch"))

    def test_no_queue_run_keeps_the_pr_head_reason(self) -> None:
        table = queue_table()
        table[QUEUE_RUNS_1630] = [200, {"total_count": 0, "workflow_runs": []}]
        d = run(table, req(SHA_1630, QUEUE_NOW, queue_evidence=True))
        self.assertEqual((d.verdict, d.reason), (False, "tree-mismatch"))

    def test_a_failed_queue_run_is_never_reuse(self) -> None:
        d = run(queue_table("failure"), req(SHA_1630, QUEUE_NOW, queue_evidence=True))
        self.assertEqual((d.verdict, d.reason), (False, "newest-run-not-success"))

    def test_a_pull_request_run_on_the_pushed_commit_is_not_queue_evidence(self) -> None:
        table = queue_table()
        body(table, QUEUE_RUNS_1630)["workflow_runs"][0]["event"] = "pull_request"  # type: ignore[index]
        d = run(table, req(SHA_1630, QUEUE_NOW, queue_evidence=True))
        self.assertEqual((d.verdict, d.reason), (False, "tree-mismatch"))

    def test_a_queue_run_does_not_rescue_a_direct_push(self) -> None:
        table = queue_table()
        table[f"{API}/commits/{SHA_1630}/pulls?per_page=100"] = [200, []]
        d = run(table, req(SHA_1630, QUEUE_NOW, queue_evidence=True))
        self.assertEqual((d.verdict, d.reason), (False, "no-associated-pr"))


class FailClosedTest(unittest.TestCase):
    def assertReason(self, table: Table, reason: str, request: ReuseRequest | None = None) -> None:
        d = run(table, request)
        self.assertFalse(d.verdict, d.detail)
        self.assertFalse(d.reuse)
        self.assertEqual(d.reason, reason, d.detail)
        self.assertIn(d.reason, REASONS)

    def test_tree_mismatch_pr1630_stops_after_three_calls(self) -> None:
        d = run(load("clud-pr1630.json"), req(SHA_1630, "2026-09-30T10:44:10Z"))
        self.assertEqual((d.verdict, d.reason, d.pr), (False, "tree-mismatch", 1630))
        self.assertEqual((d.api_calls, d.runs), (3, ()))

    def test_zero_associated_prs_direct_push(self) -> None:
        table = load("clud-pr1643.json")
        table[PULLS_1643] = [200, []]
        self.assertReason(table, "no-associated-pr")

    def test_open_unmerged_pr_is_not_an_association(self) -> None:
        table = load("clud-pr1643.json")
        body(table, PULLS_1643)[0]["merged_at"] = None  # type: ignore[index]
        self.assertReason(table, "no-associated-pr")

    def test_pr_whose_merge_commit_is_another_sha(self) -> None:
        # The pushed SHA is associated with a merged PR, but that PR's merge
        # landed as a different commit (e.g. a later push on top).
        table = load("clud-pr1643.json")
        body(table, PULLS_1643)[0]["merge_commit_sha"] = "f" * 40  # type: ignore[index]
        self.assertReason(table, "no-associated-pr")

    def test_ambiguous_prs(self) -> None:
        table = load("clud-pr1643.json")
        pulls = body(table, PULLS_1643)
        twin = copy.deepcopy(pulls[0])  # type: ignore[index]
        twin["number"] = 1644
        pulls.append(twin)  # type: ignore[union-attr]
        self.assertReason(table, "ambiguous-pr")

    def test_fork_head_is_never_reused(self) -> None:
        table = load("clud-pr1643.json")
        body(table, PULLS_1643)[0]["head"]["repo"] = {"full_name": "someone/clud"}  # type: ignore[index]
        self.assertReason(table, "fork-head")

    def test_fork_run_is_never_reused(self) -> None:
        table = load("clud-pr1643.json")
        ci_run(table)["head_repository"] = {"full_name": "someone/clud"}
        self.assertReason(table, "fork-run")

    def test_pr_head_ci_failed(self) -> None:
        table = load("clud-pr1643.json")
        ci_run(table)["conclusion"] = "failure"
        self.assertReason(table, "newest-run-not-success")

    def test_no_run_of_the_named_workflow(self) -> None:
        self.assertReason(load("clud-pr1643.json"), "no-successful-run", req(workflows=(".github/workflows/other.yml",)))

    def test_newer_run_still_in_progress(self) -> None:
        table = load("clud-pr1643.json")
        newer = copy.deepcopy(ci_run(table))
        newer.update(id=RUN_1643 + 5, status="in_progress", conclusion=None, created_at="2026-09-30T16:17:00Z")
        body(table, RUNS_1643)["workflow_runs"].append(newer)  # type: ignore[index]
        self.assertReason(table, "run-in-progress")

    def test_newer_iteration_mode_run_disqualifies_older_green_minimal_run(self) -> None:
        # clud's `ci-windows` label: a newer run on the same head runs static
        # + Windows lanes only and skips every Linux job, yet concludes
        # success. Only the newest decisive run is consulted, so the older
        # green minimal-tier run cannot be cherry-picked.
        table = load("clud-pr1643.json")
        newer_id = RUN_1643 + 7
        newer = copy.deepcopy(ci_run(table))
        newer.update(id=newer_id, created_at="2026-09-30T16:17:30Z")
        body(table, RUNS_1643)["workflow_runs"].append(newer)  # type: ignore[index]
        windows_jobs = copy.deepcopy(jobs_of(table))
        for j in windows_jobs:
            j["run_id"] = newer_id
            if j["name"] in ("Build windows-x64", "Test windows-x64"):
                j["conclusion"] = "success"
            elif j["name"] not in ("Static checks", "Dylint / Dylint", "CI OK"):
                j["conclusion"] = "skipped"
        table[f"{API}/actions/runs/{newer_id}/jobs?filter=latest&per_page=100&page=1"] = [
            200,
            {"total_count": len(windows_jobs), "jobs": windows_jobs},
        ]
        d = run(table)
        self.assertEqual(d.reason, "required-job-not-success")
        self.assertIn("Clippy linux-x64", d.detail)
        self.assertEqual(d.runs[0].run_id, newer_id)

    def test_required_job_skipped(self) -> None:
        table = load("clud-pr1643.json")
        job(table, "Test linux-x64 (unit) (py2of2) / x86_64-unknown-linux-gnu unit")["conclusion"] = "skipped"
        self.assertReason(table, "required-job-not-success")

    def test_required_job_cancelled_or_neutral(self) -> None:
        for conclusion in ("cancelled", "neutral"):
            table = load("clud-pr1643.json")
            job(table, "Dylint / Dylint")["conclusion"] = conclusion
            self.assertReason(table, "required-job-not-success")

    def test_successful_run_with_a_required_job_missing(self) -> None:
        # The run concluded success, but the Clippy job is simply absent
        # (renamed, or a different tier) -- per-job proof, not run proof.
        table = load("clud-pr1643.json")
        body(table, JOBS_1643)["jobs"] = [j for j in jobs_of(table) if not str(j["name"]).startswith("Clippy")]  # type: ignore[index]
        self.assertReason(table, "required-job-missing")

    def test_renamed_required_job_fails_closed(self) -> None:
        # PR #1575 predates clud's unit sharding: the current job names are
        # not in its run, so a rename can never silently widen reuse.
        self.assertReason(load("clud-pr1575.json"), "required-job-missing", req(SHA_1575, "2026-09-29T21:11:10Z", SHARDED))

    def test_stale_run(self) -> None:
        self.assertReason(load("clud-pr1643.json"), "stale-run", req(now="2026-10-01T18:00:00Z"))
        self.assertTrue(run(load("clud-pr1643.json"), req(now="2026-10-01T18:00:00Z", max_age_hours=48)).verdict)

    def test_job_completed_after_the_clock(self) -> None:
        self.assertReason(load("clud-pr1643.json"), "run-after-decision", req(now="2026-09-30T16:12:00Z"))

    def test_api_error_fails_closed(self) -> None:
        table = load("clud-pr1643.json")
        table[JOBS_1643] = [500, {"message": "Server Error"}]
        self.assertReason(table, "api-error")
        table = load("clud-pr1643.json")
        del table[RUNS_1643]  # replay raises GitHubApiError: a transport failure
        self.assertReason(table, "api-error")

    def test_rate_limit_fails_closed(self) -> None:
        table = load("clud-pr1643.json")
        table[PULLS_1643] = [403, {"message": "API rate limit exceeded for installation ID 1."}]
        self.assertReason(table, "rate-limited")
        table[PULLS_1643] = [429, None]
        self.assertReason(table, "rate-limited")

    def test_malformed_payload_fails_closed(self) -> None:
        table = load("clud-pr1643.json")
        del body(table, f"{API}/git/commits/{HEAD_1643}")["tree"]  # type: ignore[union-attr]
        self.assertReason(table, "api-malformed")

    def test_too_many_runs_fails_closed(self) -> None:
        table = load("clud-pr1643.json")
        filler = ci_run(table)
        for page in (1, 2, 3):
            url = page_url(RUNS_1643, page)
            table[url] = [200, {"total_count": 350, "workflow_runs": [dict(filler, id=i) for i in range(100)]}]
        self.assertReason(table, "too-many-runs")

    def test_preconditions_make_no_api_call(self) -> None:
        def explode(url: str, token: str) -> tuple[int, object]:
            raise AssertionError(f"unexpected API call: {url}")

        cases = (
            (req(event_name="pull_request"), "event-not-push"),
            (req(event_name="workflow_dispatch"), "event-not-push"),
            (req(event_name="schedule"), "event-not-push"),
            (req(ref="refs/tags/v1.2.3"), "ref-not-default-branch"),
            (req(ref="refs/heads/feature"), "ref-not-default-branch"),
        )
        for request, reason in cases:
            d = decide(request, explode, "t")  # type: ignore[arg-type]
            self.assertEqual((d.verdict, d.reason, d.api_calls), (False, reason, 0))
        self.assertEqual(decide(req(), None, None).reason, "no-token")
        self.assertEqual(decide(req(), explode, "").reason, "no-token")  # type: ignore[arg-type]


class DocsAlignedTest(unittest.TestCase):
    def test_every_reason_code_is_documented(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for doc in ("docs/designs/default-branch-verified-reuse.md", "docs/ci-toml.md"):
            text = (root / doc).read_text(encoding="utf-8")
            missing = [r for r in REASONS if f"`{r}`" not in text]
            self.assertEqual(missing, [], f"{doc} does not document reason code(s) {missing}")

    def test_workflow_normalization(self) -> None:
        self.assertEqual(normalize_workflow("ci.yml"), ".github/workflows/ci.yml")
        self.assertEqual(normalize_workflow("./.github/workflows/ci.yml"), ".github/workflows/ci.yml")
        self.assertEqual(normalize_workflow(".github/workflows/ci.yml"), ".github/workflows/ci.yml")


class CliTest(unittest.TestCase):
    ARGS = [
        "reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--event-name", "push",
        "--ref", "refs/heads/main",
    ]

    def _main(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_replay_writes_github_output_and_out_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_out = Path(tmp) / "out"
            doc_path = Path(tmp) / "reuse.json"
            env = {"GITHUB_OUTPUT": str(gh_out), "GITHUB_STEP_SUMMARY": str(Path(tmp) / "summary")}
            argv = self.ARGS + ["--sha", SHA_1643, "--now", "2026-09-30T16:18:16Z", "--replay", str(DIR / "clud-pr1643.json"),
                                "--github-output", "--out", str(doc_path), "--json"]
            for name in SHARDED:
                argv += ["--required-job", name]
            with mock.patch.dict(os.environ, env):
                rc, out = self._main(argv)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["reason"], "verified")
            lines = gh_out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [line.split("=", 1)[0] for line in lines],
                ["reuse", "would_reuse", "reason", "pr", "run_id", "run_url", "tree", "evidence_json"],
            )
            self.assertIn("reuse=true", lines)
            self.assertIn(f"run_id={RUN_1643}", lines)
            self.assertEqual(json.loads(doc_path.read_text(encoding="utf-8"))["schema"], 1)
            evidence = next(line.split("=", 1)[1] for line in lines if line.startswith("evidence_json="))
            self.assertEqual(json.loads(evidence), json.loads(out))
            self.assertEqual(json.loads(evidence), json.loads(doc_path.read_text(encoding="utf-8")))
            self.assertIn("source PR: #1643", (Path(tmp) / "summary").read_text(encoding="utf-8"))

    def test_cannot_prove_still_exits_zero(self) -> None:
        rc, out = self._main(
            self.ARGS + ["--sha", SHA_1630, "--now", "2026-09-30T10:44:10Z", "--replay", str(DIR / "clud-pr1630.json"),
                         "--required-job", "Static checks", "--json"]
        )
        self.assertEqual((rc, json.loads(out)["reuse"], json.loads(out)["reason"]), (0, False, "tree-mismatch"))

    def test_no_token_live_run_fails_closed_with_exit_zero(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True):
            rc, out = self._main(self.ARGS + ["--sha", SHA_1643, "--required-job", "Static checks", "--json"])
        self.assertEqual((rc, json.loads(out)["reason"]), (0, "no-token"))

    def test_usage_errors_exit_two(self) -> None:
        good = ["--sha", SHA_1643, "--required-job", "Static checks"]
        bad = (
            ["reuse-check", "--repo", "not-a-slug", "--workflow", "ci.yml"] + good,
            ["reuse-check", "--repo", REPO] + good,
            ["reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--sha", "abc", "--required-job", "x"],
            ["reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--sha", SHA_1643],
            ["reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--max-age-hours", "0"] + good,
            ["reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--now", "yesterday"] + good,
            ["reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--record", "a", "--replay", "b"] + good,
            ["reuse-check", "--repo", REPO, "--workflow", "ci.yml", "--replay", str(DIR / "missing.json")] + good,
        )
        for argv in bad:
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(argv), 2, argv)


class ReportTest(unittest.TestCase):
    def _table(self) -> Table:
        table: Table = {}
        for name in ("clud-push-runs.json", "clud-pr1643.json", "clud-pr1575.json", "clud-pr1630.json"):
            table.update(load(name))
        return table

    def _report(self, jobs: tuple[str, ...] = STABLE, **kw: object):
        template = req(jobs=jobs)
        return run_report(replay_fetch(self._table()), "t", template, "2026-09-29", **kw)  # type: ignore[arg-type]

    def test_retroactive_shadow_classifies_each_push_run(self) -> None:
        report = self._report(min_runs=3)
        outcomes = {r.run_id: (r.outcome, r.decision.reason) for r in report.rows}
        self.assertEqual(
            outcomes,
            {
                36743212427: ("safe-skip", "verified"),  # PR #1643, main green
                36704217465: ("must-run", "tree-mismatch"),  # PR #1630, the real catch
                36631561921: ("false-reuse-candidate", "verified"),  # PR #1575, the flake
                36537294163: ("no-signal", outcomes[36537294163][1]),  # cancelled push run
            },
        )
        self.assertEqual(report.decisive, 3)
        self.assertEqual(report.verdict, "blocked")

    def test_accepting_the_analysed_flake_promotes(self) -> None:
        report = self._report(min_runs=3, accept_flaky=frozenset({36631561921}))
        self.assertEqual(report.verdict, "promotable")
        self.assertEqual(report.count("accepted-flaky"), 1)

    def test_default_bar_promotes_on_the_first_verified_safe_skip(self) -> None:
        """GEN-021 rollout decision (2026-10-04): a single green, attested PR
        merge is sufficient evidence. The per-run decision is sound on its
        own terms, so the old multi-run quota bought confidence in the tool
        rather than soundness in the decision -- and permanently locked
        low-traffic repositories out of the mechanism. One verified safe
        skip with no false-reuse candidate now promotes."""

        report = self._report(accept_flaky=frozenset({36631561921}))
        self.assertEqual(report.verdict, "promotable")
        self.assertEqual(report.min_runs, 1)
        self.assertEqual(DEFAULT_MIN_RUNS, 1)

    def test_an_explicitly_raised_bar_still_stages_a_rollout(self) -> None:
        """`--min-runs N` remains available for a deliberate staged rollout;
        only the default moved."""

        report = self._report(min_runs=99, accept_flaky=frozenset({36631561921}))
        self.assertEqual(report.verdict, "insufficient-sample")

    def test_runs_that_never_reuse_are_a_no_op_not_a_promotion(self) -> None:
        """Every push must-run (squash batches, drifted trees) means reuse
        never fired -- no evidence the mechanism works, so nothing to
        promote, even though decisive runs exist and no candidate fired."""

        report = self._report()
        self.assertTrue(report.decisive > 0)
        self.assertEqual(report.count("safe-skip"), 1)
        only_must_run = ReuseReport(
            report.repo, report.workflow, report.since, report.until, report.min_runs,
            tuple(
                ReportRow(r.run_id, r.run_url, r.sha, r.created_at, r.conclusion,
                          r.run_attempt, "must-run", r.decision)
                for r in report.rows
            ),
            report.api_calls,
        )
        self.assertEqual(only_must_run.verdict, "no-op")

    def test_current_job_names_make_the_pre_shard_run_must_run(self) -> None:
        rows = {r.run_id: r for r in self._report(jobs=SHARDED).rows}
        self.assertEqual(rows[36631561921].outcome, "must-run")
        self.assertEqual(rows[36631561921].decision.reason, "required-job-missing")

    def test_a_failure_in_a_job_reuse_would_still_run_is_not_a_candidate(self) -> None:
        """The candidate metric must blame the decision, not the run.

        A red push run is evidence against reuse only if reuse would have
        skipped one of the jobs that actually failed. Measured on
        kernal-api: 5 of 19 runs were red purely because `Retire
        superseded and disabled cache generations` (cache housekeeping,
        which runs identically under reuse) flaked, while `verify`, `linux`
        and `Dylint workspace` all succeeded. Counting those made the
        repository look permanently unsafe on a 29% "false reuse" rate that
        was not false reuse at all."""

        from ci_lint.default_branch_reuse import _outcome

        proved = frozenset({"linux", "Dylint workspace"})
        self.assertEqual(
            _outcome(True, "failure", 1, frozenset(), proved,
                     frozenset({"Retire superseded and disabled cache generations"})),
            "must-run",
        )

    def test_a_failure_in_a_skippable_job_is_still_a_candidate(self) -> None:
        from ci_lint.default_branch_reuse import _outcome

        self.assertEqual(
            _outcome(True, "failure", 1, frozenset(), frozenset({"linux"}), frozenset({"linux"})),
            "false-reuse-candidate",
        )

    def test_an_unattributable_failure_stays_a_candidate(self) -> None:
        """Unknown must not read as innocent: with no job list to place the
        failure against, the run stays a candidate for someone to analyse."""

        from ci_lint.default_branch_reuse import _outcome

        self.assertEqual(
            _outcome(True, "failure", 1, frozenset(), frozenset({"linux"}), frozenset()),
            "false-reuse-candidate",
        )

    def test_an_ambiguous_required_job_name_is_named_as_such(self) -> None:
        """soldr's `ci.yml` has two jobs both named `Lint` -- `lint-docs`,
        a deliberate no-op for docs-only and attested heads (soldr#3318),
        and the real `lint`. The no-op skips by design, so the generic
        `required-job-not-success` pointed at the test suite instead of at
        the duplicate display name, and read as "reuse is unsafe here" when
        the truth was "this name proves nothing".

        Measured cost of the confusion: soldr measured 0% reuse across 15
        decisive runs. With `Lint` out of the required list the same runs
        measure 75% reusable, 9 safe skips, 0 candidates, `promotable`.

        Still fail-closed -- the same refusal, named."""

        self.assertIn("ambiguous-required-job", REASONS)

    def test_listing_error_is_an_error_verdict(self) -> None:
        report = run_report(replay_fetch({}), "t", req(jobs=STABLE), "2026-09-29")
        self.assertEqual((report.verdict, report.rows), ("error", ()))

    def test_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            path.write_text(json.dumps(self._table()), encoding="utf-8")
            argv = ["reuse-report", "--repo", REPO, "--workflow", "ci.yml", "--since", "2026-09-29",
                    "--replay", str(path), "--min-runs", "3", "--json"]
            for name in STABLE:
                argv += ["--required-job", name]
            for extra, code, verdict in (([], 1, "blocked"), (["--accept-flaky", "36631561921"], 0, "promotable")):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli.main(argv + extra)
                self.assertEqual((rc, json.loads(buf.getvalue())["verdict"]), (code, verdict))
            bad_date = ["reuse-report", "--repo", REPO, "--workflow", "ci.yml", "--since", "29-09-2026",
                        "--replay", str(path), "--required-job", "x"]
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(bad_date), 2)


if __name__ == "__main__":
    unittest.main()
