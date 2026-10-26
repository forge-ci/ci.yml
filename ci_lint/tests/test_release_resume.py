"""`ci-lint release resume-plan` / `verify-frozen` -- REL-004's reuse half
(ci_lint.release_resume, zackees/ci.yml#109).

fixtures/runtime/release-resume/template-run-36525765375.json is a live
GET-only recording (2026-10-25, trimmed to the fields read) of
zackees/template-python-rust-cmd run 36525765375 -- #109's own evidence run.
Its real artifact listing is EMPTY: `release-dist-verified` is uploaded with
`retention-days: 1`, so a resume more than a day later finds nothing to reuse
(the `artifact-missing` case). The happy path adds one artifact shaped like a
real listing entry from the same repository (artifact 12585513170). No
network: replay raises GitHubApiError for any URL not in the table.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ci_lint import cli
from ci_lint.cargo_messages import JsonValue
from ci_lint.fleet_cli import replay_fetch
from ci_lint.release import verify_resume
from ci_lint.release_resume import REASONS, ResumeRequest, decide, github_output_lines, verify_frozen_dist
from ci_lint.tests.helpers import FIXTURES

FIXTURE = FIXTURES / "runtime" / "release-resume" / "template-run-36525765375.json"
REPO = "zackees/template-python-rust-cmd"
RUN = "36525765375"
SHA = "cc3245f9a3b5c07a47c39a4eb64b5164ef95d2ff"
BASE = f"https://api.github.com/repos/{REPO}/actions/runs/{RUN}"
ARTS = f"{BASE}/artifacts?per_page=100&name=release-dist-verified"
JOBS = f"{BASE}/jobs?per_page=100&filter=latest&page=1"
REQUIRED = ("release-verify", "platform-run (windows-x64) (native, prebuilt) [d02e7637c456]")

ARTIFACT: dict[str, JsonValue] = {
    "id": 12585513170,
    "name": "release-dist-verified",
    "expired": False,
    "digest": "sha256:80c5e55de495e6e7854d6ca6b1b7f55a3ac163a417bcd069c4a9c8ef453a87d5",
    "workflow_run": {"id": int(RUN), "head_sha": SHA},
}


def _recorded() -> dict[str, JsonValue]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _happy() -> dict[str, JsonValue]:
    table = _recorded()
    table[ARTS] = [200, {"total_count": 1, "artifacts": [copy.deepcopy(ARTIFACT)]}]
    return table


def _write_dist(root: Path, sha: str = SHA) -> Path:
    dist = root / "frozen"
    dist.mkdir()
    entries = []
    for name, data in (("app-1.0-cp310-abi3-win_amd64.whl", b"wheel"), ("app-1.0.tar.gz", b"sdist")):
        (dist / name).write_bytes(data)
        entries.append({"path": name, "kind": "x", "platform": None, "version": "1.0", "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"schema_version": 1, "candidate_sha": sha, "ci_toml_digest": "d", "version": "1.0", "artifacts": entries}
    (dist / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dist


class ResumePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dist = _write_dist(self.root)

    def _decide(self, table: dict[str, JsonValue], **kw: str):  # type: ignore[no-untyped-def]
        req = ResumeRequest(
            repo=REPO, from_run=kw.get("from_run", RUN), sha=kw.get("sha", SHA), required_jobs=REQUIRED, dist=self.dist
        )
        return decide(req, fetch=replay_fetch(table), token="t")

    def test_happy_path_reuses(self) -> None:
        d = self._decide(_happy())
        self.assertTrue(d.resume, d.detail)
        self.assertEqual("verified", d.reason)
        self.assertEqual("12585513170", d.artifact_id)
        self.assertIn("resume=true", github_output_lines(d))
        self.assertIn("artifact_id=12585513170", github_output_lines(d))

    def test_recorded_run_has_no_artifact(self) -> None:
        d = self._decide(_recorded())
        self.assertFalse(d.resume)
        self.assertEqual("artifact-missing", d.reason)

    def test_not_a_resume(self) -> None:
        self.assertEqual("not-a-resume", self._decide(_happy(), from_run="").reason)

    def test_no_token(self) -> None:
        req = ResumeRequest(repo=REPO, from_run=RUN, sha=SHA, required_jobs=REQUIRED, dist=self.dist)
        self.assertEqual("no-token", decide(req, fetch=None, token=None).reason)

    def test_sha_mismatch(self) -> None:
        self.assertEqual("run-sha-mismatch", self._decide(_happy(), sha="a" * 40).reason)

    def test_unrecorded_url_is_api_error(self) -> None:
        table = _happy()
        del table[JOBS]
        self.assertEqual("api-error", self._decide(table).reason)

    def test_run_404(self) -> None:
        table = _happy()
        table[BASE] = [404, {"message": "Not Found"}]
        self.assertEqual("run-not-found", self._decide(table).reason)

    def test_forbidden_is_api_error(self) -> None:
        table = _happy()
        table[ARTS] = [403, {"message": "Resource not accessible"}]
        self.assertEqual("api-error", self._decide(table).reason)

    def test_fork_head(self) -> None:
        table = _happy()
        table[BASE][1]["head_repository"]["full_name"] = "evil/fork"  # type: ignore[index]
        self.assertEqual("run-repo-mismatch", self._decide(table).reason)

    def test_in_progress(self) -> None:
        table = _happy()
        table[BASE][1]["status"] = "in_progress"  # type: ignore[index]
        self.assertEqual("run-not-completed", self._decide(table).reason)

    def test_artifact_states(self) -> None:
        cases = {
            "artifact-expired": {"expired": True},
            "artifact-no-digest": {"digest": None},
            "artifact-sha-mismatch": {"workflow_run": {"head_sha": "b" * 40}},
        }
        for reason, patch in cases.items():
            table = _happy()
            table[ARTS][1]["artifacts"][0].update(patch)  # type: ignore[index]
            self.assertEqual(reason, self._decide(table).reason, reason)
        table = _happy()
        table[ARTS][1]["artifacts"].append(copy.deepcopy(ARTIFACT))  # type: ignore[index]
        self.assertEqual("artifact-ambiguous", self._decide(table).reason)

    def test_required_jobs(self) -> None:
        table = _happy()
        for job in table[JOBS][1]["jobs"]:  # type: ignore[index]
            if job["name"] == "release-verify":
                job["conclusion"] = "failure"
        self.assertEqual("required-job-not-success", self._decide(table).reason)
        table = _happy()
        table[JOBS][1]["jobs"] = [j for j in table[JOBS][1]["jobs"] if j["name"] != "release-verify"]  # type: ignore[index]
        table[JOBS][1]["total_count"] = len(table[JOBS][1]["jobs"])  # type: ignore[index]
        self.assertEqual("required-job-missing", self._decide(table).reason)

    def test_tampered_dist(self) -> None:
        (self.dist / "app-1.0.tar.gz").write_bytes(b"rebuilt")
        d = self._decide(_happy())
        self.assertEqual("dist-not-verified", d.reason)
        self.assertTrue(d.findings and all(f.rule == "REL-004" for f in d.findings))

    def test_every_reason_is_registered(self) -> None:
        self.assertIn("verified", REASONS)

    def test_cli_github_output(self) -> None:
        replay = self.root / "replay.json"
        replay.write_text(json.dumps(_happy()), encoding="utf-8")
        out = self.root / "gh_out"
        argv = ["release", "resume-plan", "--repo", REPO, "--from-run", RUN, "--sha", SHA, "--dist", str(self.dist),
                "--replay", str(replay), "--github-output", "--json"]
        for job in REQUIRED:
            argv += ["--required-job", job]
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}), redirect_stdout(io.StringIO()) as buf:
            code = cli.main(argv)
        self.assertEqual(0, code)
        self.assertTrue(json.loads(buf.getvalue())["resume"])
        self.assertIn("resume=true\nreason=verified\n", out.read_text(encoding="utf-8"))

    def test_cli_usage_error(self) -> None:
        with redirect_stdout(io.StringIO()):
            code = cli.main(["release", "resume-plan", "--repo", REPO, "--sha", "nope", "--dist", ".", "--required-job", "x"])
        self.assertEqual(2, code)


class VerifyFrozenTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dist = _write_dist(Path(tmp.name))

    def test_intact(self) -> None:
        self.assertEqual((), verify_frozen_dist(self.dist, SHA))

    def test_wrong_candidate(self) -> None:
        self.assertTrue(verify_frozen_dist(self.dist, "c" * 40))

    def test_extra_unverified_artifact(self) -> None:
        (self.dist / "app-1.0-cp310-abi3-win_arm64.whl").write_bytes(b"new")
        findings = verify_frozen_dist(self.dist, SHA)
        self.assertTrue(any("never verified" in f.message for f in findings))

    def test_missing_manifest(self) -> None:
        (self.dist / "release-manifest.json").unlink()
        self.assertTrue(verify_frozen_dist(self.dist, SHA))

    def test_path_traversal_entry_rejected(self) -> None:
        m = json.loads((self.dist / "release-manifest.json").read_text(encoding="utf-8"))
        m["artifacts"].append({"path": "../etc", "sha256": "0"})
        (self.dist / "release-manifest.json").write_text(json.dumps(m), encoding="utf-8")
        self.assertTrue(any("malformed" in f.message for f in verify_frozen_dist(self.dist, SHA)))

    def test_cli_exit_codes(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["release", "verify-frozen", "--dist", str(self.dist), "--sha", SHA]))
            (self.dist / "app-1.0.tar.gz").write_bytes(b"x")
            self.assertEqual(1, cli.main(["release", "verify-frozen", "--dist", str(self.dist), "--sha", SHA]))


class VerifyResumeExtensionTest(unittest.TestCase):
    """`release verify --resume` now also refuses a frozen manifest for another
    candidate and any staged artifact the frozen set never contained."""

    def test_candidate_and_extras(self) -> None:
        from ci_lint.release import ArtifactRecord

        with tempfile.TemporaryDirectory() as tmp:
            dist = _write_dist(Path(tmp))
            manifest = dist / "release-manifest.json"
            frozen = json.loads(manifest.read_text(encoding="utf-8"))["artifacts"]
            arts = tuple(ArtifactRecord(a["path"], "wheel", None, "1.0", a["sha256"]) for a in frozen)
            self.assertEqual([], verify_resume(dist, manifest, arts, candidate_sha=SHA))
            extra = arts + (ArtifactRecord("app-1.0-x.whl", "wheel", None, "1.0", "0" * 64),)
            msgs = [f.message for f in verify_resume(dist, manifest, extra, candidate_sha="d" * 40)]
            self.assertTrue(any("frozen manifest is for candidate" in m for m in msgs))
            self.assertTrue(any("absent from the frozen" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
