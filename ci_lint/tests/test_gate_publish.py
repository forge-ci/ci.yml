"""Publish immutable qualified heads, with real Git transport and race controls."""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from ci_lint.cli import main
from ci_lint.gate_publish import publish
from ci_lint.local_gate import check_push
from ci_lint.tests import test_attestation_push as push_fixtures
from ci_lint.tests.test_local_gate import TempRepoCase, _git


class GatePublishTest(TempRepoCase):
    def prepare(self, *, attest: bool = True) -> str:
        head = push_fixtures.AttestationPushTest.prepare(self, attest=attest)
        remote = self.tmp / "remote.git"
        remote.mkdir()
        _git(remote, "init", "--bare", "-q")
        _git(self.tmp, "remote", "add", "origin", str(remote))
        _git(self.tmp, "push", "-q", "origin", "HEAD^:refs/heads/main")
        return head

    def remote_head(self) -> str:
        return _git(self.tmp / "remote.git", "rev-parse", "refs/heads/main")

    def test_pushes_exact_stamped_sha_and_preserves_message(self) -> None:
        head = self.prepare()
        message = _git(self.tmp, "log", "-1", "--format=%B")
        result = publish(self.tmp, sha=head)
        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(result.head_sha, head)
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(_git(self.tmp, "log", "-1", "--format=%B"), message)
        self.assertEqual(publish(self.tmp, sha=head).exit_code, 0)

    def test_missing_gate_proof_never_updates_remote(self) -> None:
        head = self.prepare(attest=False)
        before = self.remote_head()
        result = publish(self.tmp, sha=head)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("missing", result.message)
        self.assertEqual(self.remote_head(), before)

    def test_cli_publishes_without_running_or_amending_the_gate(self) -> None:
        head = self.prepare()
        with redirect_stdout(io.StringIO()) as output:
            result = main(["local-gate", "push", "--repo", str(self.tmp), "--sha", head])
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(_git(self.tmp, "rev-parse", "HEAD"), head)
        self.assertEqual(self.remote_head(), head)

    def test_wrong_expected_head_and_dirty_source_refuse(self) -> None:
        head = self.prepare()
        before = self.remote_head()
        self.assertNotEqual(publish(self.tmp, sha=before).exit_code, 0)
        (self.tmp / "src.txt").write_text("concurrent edit\n")
        self.assertNotEqual(publish(self.tmp, sha=head).exit_code, 0)
        self.assertEqual(self.remote_head(), before)

    def test_detached_and_invalid_transport_arguments_refuse(self) -> None:
        head = self.prepare()
        before = self.remote_head()
        for remote in ("--all", "origin\nother", "https://unexpected.invalid/repo"):
            self.assertNotEqual(publish(self.tmp, sha=head, remote=remote).exit_code, 0)
        self.assertNotEqual(publish(self.tmp, sha="HEAD").exit_code, 0)
        _git(self.tmp, "checkout", "-q", "--detach")
        self.assertNotEqual(publish(self.tmp, sha=head).exit_code, 0)
        self.assertEqual(self.remote_head(), before)

    def test_head_move_during_validation_refuses_before_transport(self) -> None:
        head = self.prepare()
        before = self.remote_head()

        def move(repo, lines):
            result = check_push(repo, lines)
            _git(repo, "reset", "--soft", "HEAD^")
            return result

        with patch("ci_lint.gate_publish.check_push", side_effect=move):
            result = publish(self.tmp, sha=head)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.remote_head(), before)

    def test_edit_during_validation_refuses_before_transport(self) -> None:
        head = self.prepare()
        before = self.remote_head()

        def edit(repo, lines):
            result = check_push(repo, lines)
            (repo / "src.txt").write_text("concurrent edit\n")
            return result

        with patch("ci_lint.gate_publish.check_push", side_effect=edit):
            self.assertNotEqual(publish(self.tmp, sha=head).exit_code, 0)
        self.assertEqual(self.remote_head(), before)

    def test_remote_lease_preserves_concurrent_remote_update(self) -> None:
        head = self.prepare()
        message = _git(self.tmp, "log", "-1", "--format=%B")

        def advance(repo, lines):
            result = check_push(repo, lines)
            _git(repo, "commit", "-q", "--allow-empty", "-m", "other publisher")
            competing = _git(repo, "rev-parse", "HEAD")
            _git(repo, "push", "-q", "origin", competing + ":refs/heads/main")
            _git(repo, "reset", "--soft", head)
            return result

        with patch("ci_lint.gate_publish.check_push", side_effect=advance):
            result = publish(self.tmp, sha=head)
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotEqual(self.remote_head(), head)
        self.assertEqual(_git(self.tmp, "log", "-1", "--format=%B"), message)

    def test_edit_after_transport_reports_the_published_immutable_commit(self) -> None:
        from ci_lint.gate_publish import _remote_head

        head = self.prepare()
        calls = 0

        def observe(repo, remote, branch):
            nonlocal calls
            calls += 1
            result = _remote_head(repo, remote, branch)
            if calls == 2:
                (repo / "src.txt").write_text("edit during network operation\n")
            return result

        with patch("ci_lint.gate_publish._remote_head", side_effect=observe):
            result = publish(self.tmp, sha=head)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("transport succeeded for " + head, result.message)
        self.assertEqual(result.head_sha, head)
        self.assertEqual(self.remote_head(), head)

    def test_other_gate_owner_prevents_publication(self) -> None:
        from ci_lint.gate_run_lock import hold
        from ci_lint.lane_cache import run_lock_path

        head = self.prepare()
        before = self.remote_head()
        with hold(run_lock_path(self.tmp)):
            result = publish(self.tmp, sha=head)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.remote_head(), before)

    def push_destination(self):
        other = self.tmp / "push.git"
        other.mkdir()
        _git(other, "init", "--bare", "-q")
        _git(self.tmp, "push", "-q", str(other), "HEAD^:refs/heads/main")
        return other

    def test_distinct_push_url_is_the_observed_and_published_destination(self) -> None:
        head = self.prepare()
        fetch_before = self.remote_head()
        other = self.push_destination()
        _git(self.tmp, "remote", "set-url", "--push", "origin", str(other))
        result = publish(self.tmp, sha=head)
        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(_git(other, "rev-parse", "refs/heads/main"), head)
        self.assertEqual(self.remote_head(), fetch_before)

    def test_multiple_push_urls_refuse_before_mutating_any_destination(self) -> None:
        head = self.prepare()
        before = self.remote_head()
        other = self.push_destination()
        _git(self.tmp, "remote", "set-url", "--add", "--push", "origin", str(self.tmp / "remote.git"))
        _git(self.tmp, "remote", "set-url", "--add", "--push", "origin", str(other))
        result = publish(self.tmp, sha=head)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.remote_head(), before)
        self.assertEqual(_git(other, "rev-parse", "refs/heads/main"), before)

    def test_failed_post_transport_confirmation_preserves_published_sha(self) -> None:
        from ci_lint.gate_publish import _remote_head
        from ci_lint.local_gate import GitError

        head = self.prepare()
        calls = 0

        def observe(repo, remote, branch):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise GitError("connection lost during confirmation")
            return _remote_head(repo, remote, branch)

        with patch("ci_lint.gate_publish._remote_head", side_effect=observe):
            result = publish(self.tmp, sha=head)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.head_sha, head)
        self.assertIn(head, result.message)
        self.assertEqual(self.remote_head(), head)

    def test_transport_timeout_reports_publication_uncertainty(self) -> None:
        import subprocess
        from ci_lint.proc import run_captured

        head = self.prepare()

        def transport(argv, **kwargs):
            if argv[:2] == ["git", "push"]:
                raise subprocess.TimeoutExpired(argv, 120)
            return run_captured(argv, **kwargs)

        with patch("ci_lint.gate_publish.run_captured", side_effect=transport):
            result = publish(self.tmp, sha=head)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.head_sha, head)
        self.assertIn("unconfirmed", result.message)
