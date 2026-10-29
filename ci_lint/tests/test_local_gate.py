"""GATE-001..004 -- ci_lint/local_gate.py, ci_lint/first_pass.py (zackees/ci.yml#166).

Every test that needs git builds a throwaway repository in a tempdir; the
static-rule tests also need YAML tooling and are skipped without it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.finding import Status
from ci_lint.first_pass import PrClassification, RunSample, classify, collect, render_text
from ci_lint.local_gate import (
    HOOK_MARKER,
    Attestation,
    check_commit,
    check_gate_static,
    check_push,
    hook_script,
    install_hook,
    load_gate_config,
    parse_attestation,
    run_gate,
    verify,
)
from ci_lint.proc import run_captured
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import requires_yaml_tooling

TREE = "a" * 40
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "rust-pypi-app" / "ci.toml"


def _git(repo: Path, *args: str) -> str:
    proc = run_captured(["git", "-C", str(repo), *args])
    if not proc.ok:
        raise subprocess.CalledProcessError(proc.returncode, ["git", *args], proc.stdout, proc.stderr)
    return proc.stdout.strip()


def _repo(tmp: Path, gate_body: str, mode: str | None = None) -> Path:
    """A git repo whose gate is `python3 gate.py`; `gate.py` runs `gate_body`."""

    mode_line = f'mode = "{mode}"\n' if mode else ""
    _git(tmp, "init", "-q", "-b", "main")
    _git(tmp, "config", "user.email", "t@example.com")
    _git(tmp, "config", "user.name", "t")
    (tmp / "local-gate.toml").write_text(
        f'[gate]\n{mode_line}run = ["{sys.executable}", "gate.py"]\nmirrors = ["ci.yml:lint"]\nverify = "ci.yml:verify"\n',
        encoding="utf-8",
    )
    (tmp / "gate.py").write_text(gate_body, encoding="utf-8")
    (tmp / "src.txt").write_text("one\n", encoding="utf-8")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-q", "-m", "init")
    return tmp


class TempRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not on PATH")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, repo: Path):  # type: ignore[no-untyped-def]
        loaded = load_gate_config(repo)
        config, findings = loaded.config, loaded.findings
        self.assertEqual(findings, [])
        assert config is not None
        return config


class AttestationTest(unittest.TestCase):
    def test_round_trip_and_last_trailer_wins(self) -> None:
        att = Attestation(tree=TREE, secs=12)
        msg = f"subject\n\nbody\n\nLocal-Gate: v1 tree={'b' * 40}\n{att.trailer()}\n"
        self.assertEqual(parse_attestation(msg), att)

    def test_rejects_unknown_version_and_bad_tree(self) -> None:
        self.assertIsNone(parse_attestation(f"s\n\nLocal-Gate: v9 tree={TREE}\n"))
        self.assertIsNone(parse_attestation("s\n\nLocal-Gate: v1 tree=abc\n"))
        self.assertIsNone(parse_attestation("no trailer"))


class ConfigTest(TempRepoCase):
    def test_mode_defaults_to_shadow(self) -> None:
        """A declaration with no `mode` must not fail a PR: the GATE-003 default
        is report-only, so an unattested head runs the full remote suite."""
        (self.tmp / "local-gate.toml").write_text(
            '[gate]\nrun = ["python3", "ci/local_gate.py"]\n', encoding="utf-8"
        )
        self.assertEqual(self.config(self.tmp).mode, "shadow")

    def test_absent_means_not_opted_in(self) -> None:
        loaded = load_gate_config(self.tmp)
        self.assertIsNone(loaded.config)
        self.assertEqual(loaded.findings, [])

    def test_unknown_key_and_bad_mirror(self) -> None:
        (self.tmp / "local-gate.toml").write_text(
            '[gate]\nrun = ["x"]\nmirrors = ["lint"]\nbogus = 1\n', encoding="utf-8"
        )
        findings = load_gate_config(self.tmp).findings
        self.assertEqual({f.rule for f in findings}, {"CT-001", "GATE-001"})

    def test_ci_toml_local_gate_parses_through_schema(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8")
        text += '\n[local.gate]\nrun = ["python3", "ci/gate.py"]\nmirrors = ["ci.yml:fast"]\nverify = "ci.yml:precheck"\n'
        (self.tmp / "ci.toml").write_text(text, encoding="utf-8")
        _loaded = load_ci_toml(self.tmp)
        ci = _loaded.ci
        findings = _loaded.findings
        self.assertFalse([f for f in findings if "local" in f.message or f.rule == "GATE-001"], findings)
        assert ci is not None and ci.local.gate is not None
        self.assertEqual(ci.local.gate.run, ("python3", "ci/gate.py"))
        config = load_gate_config(self.tmp).config
        assert config is not None
        self.assertEqual(str(config.verify), "ci.yml:precheck")

    def test_both_declarations_is_gate_001(self) -> None:
        text = EXAMPLE.read_text(encoding="utf-8") + '\n[local.gate]\nrun = ["a"]\n'
        (self.tmp / "ci.toml").write_text(text, encoding="utf-8")
        (self.tmp / "local-gate.toml").write_text('[gate]\nrun = ["b"]\n', encoding="utf-8")
        loaded = load_gate_config(self.tmp)
        config, findings = loaded.config, loaded.findings
        self.assertIn("GATE-001", {f.rule for f in findings})
        assert config is not None
        self.assertEqual(config.run, ("a",))


class RunGateTest(TempRepoCase):
    def test_pass_stamps_head_and_second_run_is_a_no_op(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        before_tree = _git(repo, "rev-parse", "HEAD^{tree}")
        outcome = run_gate(repo, self.config(repo))
        self.assertEqual(outcome.exit_code, 0, outcome.message)
        self.assertEqual(_git(repo, "rev-parse", "HEAD^{tree}"), before_tree)
        self.assertEqual(check_commit(repo, "HEAD").state, "attested")
        again = run_gate(repo, self.config(repo))
        self.assertIn("already attested", again.message)

    def test_failing_gate_does_not_stamp(self) -> None:
        repo = _repo(self.tmp, "raise SystemExit(3)\n")
        outcome = run_gate(repo, self.config(repo))
        self.assertEqual(outcome.exit_code, 3)
        self.assertEqual(check_commit(repo, "HEAD").state, "missing")

    def test_dirty_tree_is_refused(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        (repo / "src.txt").write_text("two\n", encoding="utf-8")
        self.assertEqual(run_gate(repo, self.config(repo)).exit_code, 2)

    def test_gate_that_rewrites_files_is_refused(self) -> None:
        repo = _repo(self.tmp, "open('src.txt','w').write('formatted\\n')\n")
        outcome = run_gate(repo, self.config(repo))
        self.assertEqual(outcome.exit_code, 1)
        self.assertIn("changed the repository", outcome.message)
        self.assertEqual(check_commit(repo, "HEAD").state, "missing")

    def test_content_amend_after_gate_is_stale(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        run_gate(repo, self.config(repo))
        (repo / "src.txt").write_text("sneaky\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-a", "--amend", "--no-edit")
        self.assertEqual(check_commit(repo, "HEAD").state, "stale")

    def test_merge_from_base_on_attested_parent(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        _git(repo, "checkout", "-q", "-b", "feature")
        (repo / "f.txt").write_text("f\n", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "feature")
        run_gate(repo, self.config(repo))
        _git(repo, "checkout", "-q", "main")
        (repo / "m.txt").write_text("m\n", encoding="utf-8")
        _git(repo, "add", "m.txt")
        _git(repo, "commit", "-q", "-m", "main moved")
        _git(repo, "checkout", "-q", "feature")
        _git(repo, "merge", "-q", "--no-edit", "main")
        self.assertEqual(check_commit(repo, "HEAD").state, "attested-parent")


class VerifyAndHookTest(TempRepoCase):
    def test_verify_outcomes(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        config = self.config(repo)
        head = _git(repo, "rev-parse", "HEAD")
        self.assertEqual(verify(repo, config, sha=head, event="push", author=None).state, "not-applicable")
        self.assertEqual(
            verify(repo, config, sha=head, event="pull_request", author="dependabot[bot]").state, "exempt"
        )
        # The default mode is `shadow`: an unattested head is reported, not failed,
        # so nothing is skipped and the PR runs the full remote suite instead of
        # going red for a forgotten local gate.
        default_outcome = verify(repo, config, sha=head, event="pull_request", author="dev")
        self.assertEqual(default_outcome.exit_code, 0)
        self.assertFalse(default_outcome.attested)
        self.assertEqual(default_outcome.state, "missing")
        self.assertIn("full remote suite runs", default_outcome.message)
        enforced = config.__class__(**{**config.__dict__, "mode": "enforce"})
        self.assertEqual(verify(repo, enforced, sha=head, event="pull_request", author="dev").exit_code, 1)
        run_gate(repo, config)
        head = _git(repo, "rev-parse", "HEAD")
        self.assertTrue(verify(repo, config, sha=head, event="pull_request", author="dev").attested)

    def test_check_push(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="enforce")
        head = _git(repo, "rev-parse", "HEAD")
        line = f"refs/heads/main {head} refs/heads/main {'0' * 40}\n"
        self.assertEqual(check_push(repo, line).exit_code, 1)
        tag = f"refs/tags/v1 {head} refs/tags/v1 {'0' * 40}\n"
        self.assertEqual(check_push(repo, tag).exit_code, 0)
        run_gate(repo, self.config(repo))
        head = _git(repo, "rev-parse", "HEAD")
        self.assertEqual(check_push(repo, f"refs/heads/main {head} refs/heads/main {'0' * 40}\n").exit_code, 0)

    def test_check_push_wip_reason_admits_only_unattested_heads(self) -> None:
        # ci.yml#406: a paused handoff branch cannot pass the gate yet; an
        # explicit reason lets it through instead of a blanket --no-verify.
        repo = _repo(self.tmp, "print('ok')\n", mode="enforce")
        head = _git(repo, "rev-parse", "HEAD")
        line = f"refs/heads/wip {head} refs/heads/wip {'0' * 40}\n"
        self.assertEqual(check_push(repo, line, wip_reason="  ").exit_code, 1)
        push = check_push(repo, line, wip_reason="paused draft for #1964")
        self.assertEqual(push.exit_code, 0)
        self.assertEqual(push.problems, [])
        self.assertEqual(len(push.wip), 1)
        self.assertIn("refs/heads/wip", push.wip[0])

    def test_check_push_shadow_mode_warns_but_passes(self) -> None:
        # talos-hq/clud#2265: shadow-mode repositories are not blocked locally.
        for mode in (None, "shadow"):
            with tempfile.TemporaryDirectory() as raw:
                repo = _repo(Path(raw), "print('ok')\n", mode=mode)
                head = _git(repo, "rev-parse", "HEAD")
                _git(repo, "update-ref", "refs/remotes/origin/main", head)
                push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n")
                self.assertEqual(push.exit_code, 0)
                self.assertEqual(push.problems, [])
                self.assertEqual(len(push.shadow), 1)

    def test_check_push_enforce_mode_refuses_unattested(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="enforce")
        head = _git(repo, "rev-parse", "HEAD")
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n")
        self.assertEqual(push.exit_code, 1)
        self.assertEqual(push.shadow, [])

    def test_install_hook_refreshes_managed_hook_idempotently(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        hook = repo / ".git" / "hooks" / "pre-push"
        old = hook_script("uvx old-pin ci-lint")
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(old, encoding="utf-8")
        first = install_hook(repo, "new-launcher")
        self.assertEqual(first.exit_code, 0)
        self.assertIn("refreshed", first.message)
        text = hook.read_text(encoding="utf-8")
        self.assertIn("exec new-launcher local-gate check-push", text)
        self.assertNotIn("old-pin", text)
        again = install_hook(repo, "new-launcher")
        self.assertEqual(again.exit_code, 0)
        self.assertIn("already current", again.message)
        self.assertEqual(hook.read_text(encoding="utf-8"), text)

    def test_install_hook_leaves_custom_hook_untouched_and_backs_up_on_force(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 1)
        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        self.assertEqual(install_hook(repo, "ci-lint", force=True).exit_code, 0)
        self.assertEqual((hook.parent / "pre-push.pre-ci-lint").read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        self.assertIn(HOOK_MARKER, hook.read_text(encoding="utf-8"))

    def test_check_push_flipping_enforce_to_shadow_is_still_refused(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="enforce")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "local-gate.toml").write_text(
            (repo / "local-gate.toml").read_text(encoding="utf-8").replace('mode = "enforce"', 'mode = "shadow"'),
            encoding="utf-8")
        _git(repo, "commit", "-qam", "flip to shadow")
        head = _git(repo, "rev-parse", "HEAD")
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {base}\n")
        self.assertEqual(push.exit_code, 1)
        # A new branch is judged against the merge-base with origin's default branch.
        _git(repo, "update-ref", "refs/remotes/origin/main", base)
        new_branch = check_push(repo, f"refs/heads/y {head} refs/heads/y {'0' * 40}\n")
        self.assertEqual(new_branch.exit_code, 1)

    def _shadow_with_origin(self) -> tuple[Path, str]:
        repo = _repo(self.tmp, "print('ok')\n", mode="shadow")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/main", base)
        (repo / "src.txt").write_text("two\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "two")
        return repo, base

    def test_check_push_without_a_remote_base_fails_closed(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="shadow")
        head = _git(repo, "rev-parse", "HEAD")
        # The remote has a tip we never fetched and nothing resolves a base.
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'c' * 40}\n")
        self.assertEqual(push.exit_code, 1)
        self.assertIn("no remote base commit", push.problems[0])
        self.assertIn("git fetch", push.problems[0])

    def test_check_push_in_a_shallow_clone_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            origin = _repo(Path(raw) / "o", "print('ok')\n", mode="shadow") if (Path(raw) / "o").mkdir() is None else None
            assert origin is not None
            (origin / "src.txt").write_text("two\n", encoding="utf-8")
            _git(origin, "commit", "-qam", "two")
            clone = Path(raw) / "c"
            proc = run_captured(["git", "clone", "-q", "--depth", "1", origin.as_uri(), str(clone)])
            self.assertTrue(proc.ok, proc.stderr)
            _git(clone, "config", "user.email", "t@example.com")
            _git(clone, "config", "user.name", "t")
            (clone / "src.txt").write_text("three\n", encoding="utf-8")
            _git(clone, "commit", "-qam", "three")
            _git(clone, "update-ref", "-d", "refs/remotes/origin/HEAD")
            head = _git(clone, "rev-parse", "HEAD")
            # Unrelated-history stand-in for a missing base: the default ref is gone.
            _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
            push = check_push(clone, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n", remote="origin")
            self.assertEqual(push.exit_code, 1)
            self.assertIn("cannot be trusted", push.problems[0])

    def test_check_push_judges_the_pushed_sha_not_head(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="shadow")
        base = _git(repo, "rev-parse", "HEAD")
        toml = repo / "local-gate.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("shadow", "enforce"), encoding="utf-8")
        _git(repo, "commit", "-qam", "enforce")
        _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
        toml.write_text(toml.read_text(encoding="utf-8").replace("enforce", "shadow"), encoding="utf-8")
        _git(repo, "commit", "-qam", "flip to shadow")
        pushed = _git(repo, "rev-parse", "HEAD")
        line = f"refs/heads/x {pushed} refs/heads/x {'0' * 40}\n"
        # A non-checked-out branch, and a detached HEAD at the old shadow commit:
        # the merge-base must come from the pushed sha, not from HEAD.
        _git(repo, "checkout", "-q", "--detach", base)
        self.assertEqual(check_push(repo, line).exit_code, 1)
        _git(repo, "checkout", "-q", "-b", "elsewhere", base)
        self.assertEqual(check_push(repo, line).exit_code, 1)

    def test_check_push_shadow_remote_tip_cannot_hide_enforcing_default_branch(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="enforce")
        toml = repo / "local-gate.toml"
        _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
        _git(repo, "checkout", "-q", "-b", "feature")
        toml.write_text(toml.read_text(encoding="utf-8").replace("enforce", "shadow"), encoding="utf-8")
        _git(repo, "commit", "-qam", "flip to shadow")
        tip = _git(repo, "rev-parse", "HEAD")  # already on the remote branch
        (repo / "src.txt").write_text("more\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "later")
        pushed = _git(repo, "rev-parse", "HEAD")
        push = check_push(repo, f"refs/heads/feature {pushed} refs/heads/feature {tip}\n")
        self.assertEqual(push.exit_code, 1)
        self.assertIn("mode = enforce", push.problems[0])

    def test_check_push_first_push_of_a_new_repo_honours_shadow(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        head = _git(repo, "rev-parse", "HEAD")
        push = check_push(repo, f"refs/heads/main {head} refs/heads/main {'0' * 40}\n", remote="https://example.invalid/r.git")
        self.assertEqual(push.exit_code, 0)
        self.assertEqual(len(push.shadow), 1)
        self.assertTrue(push.mode_defaulted)

    def test_check_push_unresolved_remote_refs_fail_closed(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="shadow")
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/feature", head)  # remote refs, no default branch
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n")
        self.assertEqual(push.exit_code, 1)

    def test_check_push_escalated_push_does_not_print_defaulted_notice(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/feature", head)
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n")
        self.assertEqual(push.exit_code, 1)
        self.assertFalse(push.mode_defaulted)

    def test_check_push_missing_remote_tip_falls_back_to_merge_base(self) -> None:
        repo, base = self._shadow_with_origin()
        head = _git(repo, "rev-parse", "HEAD")
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'b' * 40}\n")
        self.assertEqual(push.exit_code, 0)
        self.assertEqual(len(push.shadow), 1)
        # And an enforce policy at the merge-base is honored, not skipped.
        (repo / "local-gate.toml").write_text(
            (repo / "local-gate.toml").read_text(encoding="utf-8").replace("shadow", "enforce"), encoding="utf-8")
        _git(repo, "commit", "-qam", "enforce")
        tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/main", tip)
        (repo / "local-gate.toml").write_text(
            (repo / "local-gate.toml").read_text(encoding="utf-8").replace("enforce", "shadow"), encoding="utf-8")
        _git(repo, "commit", "-qam", "flip")
        flipped = _git(repo, "rev-parse", "HEAD")
        refused = check_push(repo, f"refs/heads/x {flipped} refs/heads/x {'b' * 40}\n")
        self.assertEqual(refused.exit_code, 1)
        self.assertIn("mode = enforce", refused.problems[0])

    def test_check_push_resolves_default_branch_from_the_pushed_remote(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="shadow")
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/upstream/master", head)
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n", remote="upstream")
        self.assertEqual(push.exit_code, 0)

    def test_check_push_defaulted_mode_is_flagged(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/main", head)
        self.assertTrue(check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n").mode_defaulted)
        with tempfile.TemporaryDirectory() as raw:
            explicit = _repo(Path(raw), "print('ok')\n", mode="shadow")
            sha = _git(explicit, "rev-parse", "HEAD")
            _git(explicit, "update-ref", "refs/remotes/origin/main", sha)
            self.assertFalse(check_push(explicit, f"refs/heads/x {sha} refs/heads/x {'0' * 40}\n").mode_defaulted)

    def test_check_push_shadow_still_refuses_policy_findings(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n", mode="shadow")
        _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
        (repo / "local-gate.toml").write_text('[gate]\nmode = "shadow"\nrun = []\n', encoding="utf-8")
        _git(repo, "commit", "-qam", "break policy")
        head = _git(repo, "rev-parse", "HEAD")
        push = check_push(repo, f"refs/heads/x {head} refs/heads/x {'0' * 40}\n")
        self.assertEqual(push.exit_code, 1)
        self.assertIn("outgoing gate policy", push.problems[0])

    def test_install_hook_preserves_edited_managed_hook(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        hook = repo / ".git" / "hooks" / "pre-push"
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 0)
        edited = hook.read_text(encoding="utf-8") + "echo extra step\n"
        hook.write_text(edited, encoding="utf-8")
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 1)
        self.assertEqual(hook.read_text(encoding="utf-8"), edited)
        self.assertEqual(install_hook(repo, "ci-lint", force=True).exit_code, 0)
        self.assertEqual((hook.parent / "pre-push.pre-ci-lint").read_text(encoding="utf-8"), edited)

    def test_install_hook_marker_in_a_comment_is_not_managed(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        custom = "#!/bin/sh\n# mine\n# note\n# a comment: installed by: ci-lint is how others do it\n"
        hook.write_text(custom, encoding="utf-8")
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 1)
        self.assertEqual(hook.read_text(encoding="utf-8"), custom)

    def test_install_hook_force_warns_when_replacing_a_backup(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho one\n", encoding="utf-8")
        self.assertNotIn("replaced an earlier backup", install_hook(repo, "ci-lint", force=True).message)
        hook.write_text("#!/bin/sh\necho two\n", encoding="utf-8")
        self.assertIn("replaced an earlier backup", install_hook(repo, "ci-lint", force=True).message)

    def test_install_hook_refuses_foreign_hook(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 0)
        hook = repo / ".git" / "hooks" / "pre-push"
        self.assertIn(HOOK_MARKER, hook.read_text(encoding="utf-8"))
        hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 1)
        self.assertEqual(install_hook(repo, "ci-lint", force=True).exit_code, 0)

    def test_hook_probes_its_launcher_before_gating(self) -> None:
        """A hook whose launcher has died must refuse loudly.

        `install-hook` records the interpreter that installed it, and under
        `uvx` that is a temporary path inside uv's cache, which uv
        garbage-collects. The hook then silently stops gating every later push
        -- the worst failure a gate can have.
        """

        repo = _repo(self.tmp, "print('ok')\n")
        self.assertEqual(install_hook(repo, "ci-lint").exit_code, 0)
        script = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
        # The probe must precede the real check-push exec, or a dead launcher
        # is discovered only by exec'ing a missing binary.
        self.assertIn("local-gate --help", script)
        self.assertLess(script.index("local-gate --help"), script.index("check-push"))
        # `--version` is not a valid ci_lint invocation (argparse exits 2), so
        # probing on it would report every healthy hook as broken.
        self.assertNotIn("--version", script)

    def test_dead_launcher_hook_refuses_loudly(self) -> None:
        repo = _repo(self.tmp, "print('ok')\n")
        dead = f"{self.tmp / 'no-such-python'} -m ci_lint"
        self.assertEqual(install_hook(repo, dead).exit_code, 0)
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.chmod(0o755)
        line = f"refs/heads/main {'a' * 40} refs/heads/main {'0' * 40}\n"
        proc = run_captured([str(hook)], input_text=line)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("can no longer run ci-lint", proc.stderr)


WORKFLOW_GREEN = """\
name: CI
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000
      - run: python3 -m ci_lint local-gate verify --repo .
  lint:
    needs: verify
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000
      - name: Enlarge swap
        run: sudo fallocate -l 4G /swap
      - run: |
          set -euo pipefail
          GATE_LANE=remote PYGATE --lane lint
  build:
    needs: lint
    runs-on: ubuntu-24.04
    steps:
      - run: cargo build
"""


@requires_yaml_tooling
class StaticRulesTest(TempRepoCase):
    def _write(self, workflow: str, *, setup_steps: str = '["Enlarge swap"]') -> Path:
        wf_dir = self.tmp / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(workflow.replace("PYGATE", "python3 ci/gate.py"), encoding="utf-8")
        (self.tmp / "local-gate.toml").write_text(
            '[gate]\nrun = ["python3", "ci/gate.py"]\nmirrors = ["ci.yml:lint"]\nverify = "ci.yml:verify"\n'
            f"setup-steps = {setup_steps}\n",
            encoding="utf-8",
        )
        return self.tmp

    def _rules(self, repo: Path) -> list[str]:
        return [f.rule for f in check_gate_static(self.config(repo), repo) if f.status == Status.VIOLATION]

    def test_repo_without_workflows_needs_no_mirror_or_verify(self) -> None:
        (self.tmp / "local-gate.toml").write_text('[gate]\nrun = ["python3", "ci/gate.py"]\n', encoding="utf-8")
        self.assertEqual(check_gate_static(self.config(self.tmp), self.tmp), [])

    def test_green(self) -> None:
        self.assertEqual(self._rules(self._write(WORKFLOW_GREEN)), [])

    def test_remote_only_command_in_mirror_is_gate_001(self) -> None:
        wf = WORKFLOW_GREEN.replace("--lane lint\n", "--lane lint\n          cargo clippy\n")
        self.assertEqual(self._rules(self._write(wf)), ["GATE-001"])

    def test_unlisted_setup_run_step_is_gate_001(self) -> None:
        self.assertEqual(self._rules(self._write(WORKFLOW_GREEN, setup_steps="[]")), ["GATE-001"])

    def test_remote_only_action_in_mirror_is_gate_001(self) -> None:
        wf = WORKFLOW_GREEN.replace(
            "      - name: Enlarge swap", "      - uses: ./.github/actions/extra-lint\n      - name: Enlarge swap"
        )
        self.assertEqual(self._rules(self._write(wf)), ["GATE-001"])

    def test_job_not_behind_verify_is_gate_002(self) -> None:
        wf = WORKFLOW_GREEN + "  docs:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo hi\n"
        self.assertEqual(self._rules(self._write(wf)), ["GATE-002"])

    def test_verify_job_without_verify_step_is_gate_002(self) -> None:
        wf = WORKFLOW_GREEN.replace("python3 -m ci_lint local-gate verify --repo .", "echo skipped")
        self.assertEqual(self._rules(self._write(wf)), ["GATE-002"])


    def _review(self, repo: Path) -> list[str]:
        return [f.message for f in check_gate_static(self.config(repo), repo)
                if f.rule == "GATE-002" and f.status == Status.NEEDS_REVIEW]

    def test_other_pr_workflow_without_verify_is_gate_002_review(self) -> None:
        repo = self._write(WORKFLOW_GREEN)
        other = "name: lint\non: [pull_request]\njobs:\n  ruff:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: ruff check\n"
        (repo / ".github" / "workflows" / "python-lint.yml").write_text(other, encoding="utf-8")
        (repo / ".github" / "workflows" / "nightly.yml").write_text(
            other.replace("[pull_request]", "[schedule]"), encoding="utf-8")
        messages = self._review(repo)
        self.assertEqual(len(messages), 1)
        self.assertIn("python-lint.yml", messages[0])
        self.assertNotIn("nightly.yml", messages[0])
        self.assertEqual(self._rules(repo), [])

    def test_other_pr_workflow_with_own_verify_is_clean(self) -> None:
        repo = self._write(WORKFLOW_GREEN)
        other = ("on: pull_request\njobs:\n  v:\n    runs-on: ubuntu-24.04\n    steps:\n"
                 "      - run: python3 -m ci_lint local-gate verify --repo .\n"
                 "  ruff:\n    needs: v\n    runs-on: ubuntu-24.04\n    steps:\n      - run: ruff check\n")
        (repo / ".github" / "workflows" / "python-lint.yml").write_text(other, encoding="utf-8")
        self.assertEqual(self._review(repo), [])


class FirstPassTest(unittest.TestCase):
    def test_classify(self) -> None:
        one = [RunSample("s1", "success", 1, "2026-10-01T00:00:00Z", True),
               RunSample("s1", "cancelled", 1, "2026-10-01T00:00:01Z", True)]
        self.assertEqual(classify(one), PrClassification(1, 0, 0, True, True))
        fixup = one + [RunSample("s2", "success", 1, "2026-10-01T01:00:00Z", False)]
        self.assertFalse(classify(fixup).first_pass)
        rerun = [RunSample("s1", "success", 2, "2026-10-01T00:00:00Z", False)]
        self.assertFalse(classify(rerun).first_pass)

    def test_rerun_after_an_attempt_one_success_is_not_first_pass(self) -> None:
        runs = [
            RunSample("s1", "success", 1, "2026-10-01T00:00:00Z", True),
            RunSample("s1", "success", 2, "2026-10-01T00:00:01Z", True),
        ]
        self.assertFalse(classify(runs).first_pass)
        self.assertEqual(classify(runs).reruns, 1)

    def test_collect_with_recorded_responses(self) -> None:
        pulls: JsonValue = [
            {"number": 7, "title": "t", "head": {"ref": "b7"}, "created_at": "2026-10-01T00:00:00Z",
             "merged_at": "2026-10-01T02:00:00Z", "updated_at": "2026-10-01T02:00:00Z"},
        ]
        runs: JsonValue = {
            "workflow_runs": [
                {"path": ".github/workflows/ci.yml", "head_sha": "s1", "conclusion": "success", "run_attempt": 1,
                 "created_at": "2026-10-01T00:01:00Z",
                 "head_commit": {"message": f"x\n\nLocal-Gate: v1 tree={TREE}"}},
                {"path": ".github/workflows/other.yml", "head_sha": "s0", "conclusion": "failure", "run_attempt": 1,
                 "created_at": "2026-10-01T00:01:00Z", "head_commit": {"message": "x"}},
            ]
        }

        def fetch(url: str, _token: str) -> JsonValue:
            return pulls if "/pulls?" in url else runs

        report = collect("o/r", "ci.yml", datetime(2026, 9, 30, tzinfo=timezone.utc), fetch, "tok", min_prs=1)
        self.assertEqual(report.rate, 1.0)
        self.assertTrue(report.prs[0].attested)
        self.assertIsNone(report.finding())
        self.assertEqual((report.merged_total, report.scanned, report.without_runs), (1, 1, 0))

    def test_collect_scopes_workflow_and_limit(self) -> None:
        pulls: JsonValue = [
            {"number": n, "title": "t", "head": {"ref": f"b{n}"}, "created_at": "2026-10-01T00:00:00Z",
             "merged_at": f"2026-10-01T0{n}:00:00Z", "updated_at": "2026-10-01T09:00:00Z"} for n in (1, 2, 3)
        ]
        seen: list[str] = []

        def fetch(url: str, _token: str) -> JsonValue:
            seen.append(url)
            if "/pulls?" in url:
                return pulls
            return {"workflow_runs": []}

        report = collect("o/r", "lint.yml", datetime(2026, 9, 30, tzinfo=timezone.utc), fetch, "tok", limit=2)
        self.assertEqual((report.merged_total, report.scanned, report.without_runs), (3, 2, 2))
        run_urls = [u for u in seen if "/runs?" in u]
        self.assertTrue(run_urls and all("/actions/workflows/lint.yml/runs?" in u for u in run_urls))
        self.assertTrue(any("branch=b3" in u for u in run_urls) and not any("branch=b1" in u for u in run_urls))
        self.assertIn("newest 2 of 3", render_text(report))


if __name__ == "__main__":
    unittest.main()


ISO_GATE = """[gate]
run = ["python3", "ci/gate.py"]

[gate.isolation]
marker = "TOOL_TEST_ISOLATED"
runner = ["bosn", "run", "--task", "test"]
guard = "scripts/test_wrapper.sh"
"""
ISO_GUARD = '#!/bin/sh\nif [ "${CI:-}" != true ] && [ "${TOOL_TEST_ISOLATED:-}" != 1 ]; then exit 97; fi\nexec "$@"\n'
ISO_BOSN = '[stack.dev]\ndockerfile = "docker/Dockerfile"\n\n[task.test]\nstack = "dev"\ncmd = "cargo nextest run"\n'
ISO_SCRIPT = 'TESTS = ("bosn", "run", "--task", "test")\n'


class TreeProofTest(TempRepoCase):
    """GATE-009 -- the isolated runner proves it ran this worktree (zackees/ci.yml#196, zackees/bosn#314)."""

    def _repo(self, *, proves: bool, gate_script: str, entry: str) -> Path:
        gate = ISO_GATE + ("proves-tree = true\n" if proves else "")
        files = {
            "local-gate.toml": gate,
            "scripts/test_wrapper.sh": ISO_GUARD,
            "bosn.toml": ISO_BOSN.replace('cmd = "cargo nextest run"', 'cmd = "python3 /repo/ci/entry.py"'),
            "docker/Dockerfile": "FROM rust@sha256:" + "0" * 64 + "\nENV TOOL_TEST_ISOLATED=1\n",
            "ci/gate.py": ISO_SCRIPT + gate_script,
            "ci/entry.py": entry,
        }
        for rel, text in files.items():
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / rel).write_text(text, encoding="utf-8")
        return self.tmp

    def _gate_009(self, repo: Path) -> list[str]:
        return [f"{f.status.value}:{f.path}" for f in check_gate_static(self.config(repo), repo) if f.rule == "GATE-009"]

    ECHO = 'print("gate-nonce: " + open("/repo/.gate-nonce").read())\n'

    def test_proven(self) -> None:
        repo = self._repo(proves=True, gate_script='NONCE = ".gate-nonce"\n', entry='p = ".gate-nonce"\n' + self.ECHO)
        self.assertEqual(self._gate_009(repo), [])

    def test_not_declared_is_needs_review(self) -> None:
        repo = self._repo(proves=False, gate_script="", entry="")
        self.assertEqual(self._gate_009(repo), [f"{Status.NEEDS_REVIEW.value}:local-gate.toml"])

    def test_declared_but_not_wired(self) -> None:
        repo = self._repo(proves=True, gate_script="", entry="print('hi')\n")
        self.assertEqual(sorted(self._gate_009(repo)),
                         sorted([f"{Status.VIOLATION.value}:local-gate.toml", f"{Status.VIOLATION.value}:bosn.toml"]))


class BosnCiRunnerTest(TempRepoCase):
    """GATE-005/009 with bosn's act engine as the isolated runner (zackees/clud)."""

    def _repo(self, script: str) -> Path:
        files = {
            "local-gate.toml": ISO_GATE.replace('runner = ["bosn", "run", "--task", "test"]',
                                                'runner = ["bosn", "ci", "run"]') + "proves-tree = true\n",
            "scripts/test_wrapper.sh": ISO_GUARD,
            "ci/gate.py": 'RUNNER = ("bosn", "ci", "run")\n' + script,
        }
        for rel, text in files.items():
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / rel).write_text(text, encoding="utf-8")
        return self.tmp

    def _rules(self, repo: Path) -> list[str]:
        return sorted(f.rule for f in check_gate_static(self.config(repo), repo) if f.rule in ("GATE-005", "GATE-009"))

    def test_run_record_proof_satisfies_both(self) -> None:
        script = 'ok = r["workspace"] == ROOT and r["sha"] == HEAD and r["dirty"] is None\n'
        self.assertEqual(self._rules(self._repo(script)), [])

    def test_missing_run_record_check(self) -> None:
        self.assertEqual(self._rules(self._repo("print('trust the exit code')\n")), ["GATE-009"])

    def _direct(self, *, enrolled: bool = True) -> Path:
        repo = self._repo("")
        path = repo / "local-gate.toml"
        text = path.read_text().replace('run = ["python3", "ci/gate.py"]',
                                        'run = ["bosn", "ci", "run", "--wait", "--json"]')
        if enrolled:
            text += '''
[gate.replay]
repository = "zackees/tool"
workflow = "ci.yml"
mode = "minimal"
qualified = true
report-source = "stdout"
provider-query = ["bosn", "ci", "runners", "list", "--json"]
[[gate.replay.jobs]]
source-job = "ci.yml:tests"
'''
        path.write_text(text)
        return repo

    def test_direct_shared_replay_satisfies_isolation(self) -> None:
        self.assertEqual(self._rules(self._direct()), [])

    def test_direct_exit_code_without_shared_proof_is_rejected(self) -> None:
        self.assertEqual(self._rules(self._direct(enrolled=False)), ["GATE-005", "GATE-009"])

    def test_direct_shared_replay_still_requires_host_guard(self) -> None:
        repo = self._direct()
        (repo / "scripts/test_wrapper.sh").write_text('exec "$@"\n')
        self.assertEqual(self._rules(repo), ["GATE-005"])

    def test_direct_incomplete_shared_enrollment_is_rejected(self) -> None:
        for before, after in (
            ('report-source = "stdout"', 'report-source = "file"'),
            ('provider-query = ["bosn", "ci", "runners", "list", "--json"]', ''),
            ('run = ["bosn", "ci", "run", "--wait", "--json"]',
             'run = ["other-runner", "--wait", "--json"]'),
        ):
            with self.subTest(before=before):
                repo = self._direct()
                path = repo / "local-gate.toml"
                path.write_text(path.read_text().replace(before, after))
                self.assertEqual(self._rules(repo), ["GATE-005", "GATE-009"])

    def test_direct_shared_replay_requires_tree_proof_declaration(self) -> None:
        repo = self._direct()
        path = repo / "local-gate.toml"
        path.write_text(path.read_text().replace('proves-tree = true', 'proves-tree = false'))
        self.assertEqual(self._rules(repo), ["GATE-009"])

    def test_unmapped_host_lane_cannot_use_shared_isolation_exemption(self) -> None:
        repo = self._direct()
        path = repo / "local-gate.toml"
        path.write_text(path.read_text() + '''
lanes = ["covered"]
[gate.lanes.covered]
run = ["bosn", "ci", "run", "--wait", "--json"]
tools = ["bosn"]
[gate.lanes.host-tests]
run = ["python3", "-m", "pytest"]
tools = ["python3"]
''')
        self.assertEqual(self._rules(repo), ["GATE-005", "GATE-009"])

        # Mapping a host command to replay still must not grant permission
        # to launch the suite outside the declared isolated runner.
        path.write_text(path.read_text().replace('lanes = ["covered"]',
                                                'lanes = ["covered", "host-tests"]'))
        self.assertEqual(self._rules(repo), ["GATE-005", "GATE-009"])

        path.write_text(path.read_text().replace('run = ["python3", "-m", "pytest"]',
                                                'run = ["bosn", "ci", "run", "--wait", "--json"]'))
        self.assertEqual(self._rules(repo), [])


class IsolationMountTest(TempRepoCase):
    """GATE-005/009 regressions from the zccache rollout: a non-/repo mount
    destination and a multi-line Dockerfile ENV."""

    def _repo(self, *, dockerfile_env: str, cmd: str, mount: str) -> Path:
        bosn = ('[stack.dev]\ndockerfile = "docker/Dockerfile"\n\n[stack.dev.mounts]\n'
                f'repo = {{ source = ".", destination = "{mount}" }}\n\n[task.test]\nstack = "dev"\ncmd = "{cmd}"\n')
        files = {
            "local-gate.toml": ISO_GATE + "proves-tree = true\n",
            "scripts/test_wrapper.sh": ISO_GUARD,
            "bosn.toml": bosn,
            "docker/Dockerfile": "FROM rust@sha256:" + "0" * 64 + "\n" + dockerfile_env,
            "ci/gate.py": ISO_SCRIPT + 'NONCE = ".gate-nonce"\n',
            "ci/entry.py": 'p = ".gate-nonce"\nprint("gate-nonce: " + open("/work/.gate-nonce").read())\n',
        }
        for rel, text in files.items():
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / rel).write_text(text, encoding="utf-8")
        return self.tmp

    def _rules(self, repo: Path) -> list[str]:
        return sorted(f.rule for f in check_gate_static(self.config(repo), repo) if f.rule in ("GATE-005", "GATE-009"))

    def test_work_mount_resolves_entry_script(self) -> None:
        repo = self._repo(dockerfile_env="ENV TOOL_TEST_ISOLATED=1\n", cmd="python3 /work/ci/entry.py", mount="/work")
        self.assertEqual(self._rules(repo), [])

    def test_repo_prefix_does_not_resolve_under_a_work_mount(self) -> None:
        repo = self._repo(dockerfile_env="ENV TOOL_TEST_ISOLATED=1\n", cmd="python3 /repo/ci/entry.py", mount="/work")
        self.assertEqual(self._rules(repo), ["GATE-009"])

    def test_multiline_env_sets_the_marker(self) -> None:
        env = "ENV A=1 \\\n    TOOL_TEST_ISOLATED=1 \\\n    B=2\n"
        repo = self._repo(dockerfile_env=env, cmd="python3 /work/ci/entry.py", mount="/work")
        self.assertEqual(self._rules(repo), [])


class IsolationTest(TempRepoCase):
    """GATE-005 -- ci_lint/gate_isolation.py (zackees/ci.yml#168)."""

    def _repo(self, *, guard: str = ISO_GUARD, docker_env: str = "ENV TOOL_TEST_ISOLATED=1\n",
              script: str = ISO_SCRIPT, gate: str = ISO_GATE) -> Path:
        files = {
            "local-gate.toml": gate,
            "scripts/test_wrapper.sh": guard,
            "bosn.toml": ISO_BOSN,
            "docker/Dockerfile": "FROM rust@sha256:" + "0" * 64 + "\n" + docker_env,
            "ci/gate.py": script,
        }
        for rel, text in files.items():
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / rel).write_text(text, encoding="utf-8")
        return self.tmp

    def _gate_005(self, repo: Path) -> list[Status]:
        return [f.status for f in check_gate_static(self.config(repo), repo) if f.rule == "GATE-005"]

    def test_green(self) -> None:
        self.assertEqual(self._gate_005(self._repo()), [])

    def test_guard_that_ignores_the_marker(self) -> None:
        self.assertEqual(self._gate_005(self._repo(guard="#!/bin/sh\nexec \"$@\"\n")), [Status.VIOLATION])

    def test_isolated_image_that_never_sets_the_marker(self) -> None:
        self.assertEqual(self._gate_005(self._repo(docker_env="")), [Status.VIOLATION])

    def test_gate_that_never_runs_the_isolated_suite(self) -> None:
        self.assertEqual(self._gate_005(self._repo(script="print('lint only')\n")), [Status.VIOLATION])

    def test_bad_marker_name(self) -> None:
        (self.tmp / "local-gate.toml").write_text(ISO_GATE.replace("TOOL_TEST_ISOLATED", "not a var"), encoding="utf-8")
        findings = load_gate_config(self.tmp).findings
        self.assertEqual([f.rule for f in findings], ["GATE-005"])

    def test_self_hosted_tool_without_isolation_needs_review(self) -> None:
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "remote", "add", "origin", "https://github.com/zackees/soldr.git")
        (self.tmp / "local-gate.toml").write_text('[gate]\nrun = ["python3", "ci/gate.py"]\n', encoding="utf-8")
        self.assertEqual(self._gate_005(self.tmp), [Status.NEEDS_REVIEW])
        _git(self.tmp, "remote", "set-url", "origin", "git@github.com:zackees/other.git")
        self.assertEqual(self._gate_005(self.tmp), [])
