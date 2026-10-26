"""RUST-017: `--no-capture` / a single test thread serializes a CI test run.

zackees/ci.yml#79 (evidence: zackees/running-process#1231). `cargo nextest
run --no-capture` implies `--test-threads=1`: a repo that adds it "so
println!s show up" silently turns a parallel suite serial. running-process's
2197 per-test durations summed to 318.5 s against a 318.6 s wall; dropping
the flag took nextest 318.6 s -> 181.8 s. nextest's own
`failure-output = "immediate-final"` already prints a failing or timed-out
test's output, so the flag buys nothing CI needs.

Static signals (run: lines, one level into a referenced ci/*.py script):

- violation: a `cargo nextest run` / `cargo test` invocation carrying
  `--no-capture`/`--nocapture` (before or after `--`), or `--test-threads 1`.
- needs_review: a referenced ci/*.py script that builds a test command and
  holds a `--no-capture`/`--nocapture` string it appends dynamically (the
  running-process shape: an env-var opt-in) -- ci-lint cannot see whether
  the opt-in is on in CI.

Not a finding (ci.yml#136): `cargo test <name> -- --exact|--ignored
--test-threads=1`, a single named test, where one thread costs nothing.

A same-line `# ci-lint: allow RUST-017 <reason>` (e.g. a Windows-only
serialization with a documented cause) excuses that line.
"""

from __future__ import annotations

from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.test_invocations import (
    allowed,
    find_test_invocation,
    iter_test_invocations,
    referenced_scripts,
    script_calls,
    script_string_constants,
    source_line,
)

RULE = "RUST-017"
NOCAPTURE_FLAGS: frozenset[str] = frozenset({"--no-capture", "--nocapture"})

_FIX = (
    "drop the flag from CI; configure nextest's profile instead ('[profile.ci] failure-output = "
    '"immediate-final"\' in .config/nextest.toml, optionally success-output) so failing and '
    "timed-out tests still print their output while the suite stays parallel. Keep --no-capture "
    "as a local debugging opt-in only"
)


def _serializing_flag(args: tuple[str, ...]) -> str | None:
    for i, arg in enumerate(args):
        if arg in NOCAPTURE_FLAGS:
            return arg
        if arg == "--test-threads=1":
            return arg
        if arg == "--test-threads" and i + 1 < len(args) and args[i + 1] == "1":
            return "--test-threads 1"
    return None


# cargo flags that consume the next token, so that token is not a test-name filter.
_CARGO_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-p", "--package", "-F", "--features", "--test", "--bin", "--example", "--bench",
        "--target", "--target-dir", "--manifest-path", "--profile", "-j", "--jobs", "--color",
        "--message-format", "--exclude", "--config", "-Z", "--lockfile-path",
    }
)  # fmt: skip
# libtest flags after `--` that consume the next token.
_LIBTEST_VALUE_FLAGS: frozenset[str] = frozenset(
    {"--test-threads", "--skip", "--format", "--color", "--logfile", "-Z", "--shuffle-seed"}
)


def _positionals(tokens: tuple[str, ...], value_flags: frozenset[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            skip = tok in value_flags
            continue
        out.append(tok)
    return out


def _selects_single_test(args: tuple[str, ...]) -> bool:
    """ci.yml#136: a test-name filter together with `--exact` or `--ignored`
    after `--` runs one named test, so `--test-threads=1` serializes
    nothing. A bare substring filter, or `--ignored` with no filter, can
    still select many tests and stays a violation."""

    if "--" in args:
        cut = args.index("--")
        before, after = args[:cut], args[cut + 1 :]
    else:
        before, after = args, ()
    has_filter = bool(_positionals(before, _CARGO_VALUE_FLAGS)) or bool(
        _positionals(after, _LIBTEST_VALUE_FLAGS)
    )
    return has_filter and ("--exact" in after or "--ignored" in after)


def check_rust_017(repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    for inv in iter_test_invocations(repo_root):
        flag = _serializing_flag(inv.args)
        if flag is None or allowed(inv.site.raw, RULE):
            continue
        if flag not in NOCAPTURE_FLAGS and inv.kind == "cargo-test" and _selects_single_test(inv.args):
            continue
        what = "cargo nextest run" if inv.kind == "nextest" else "cargo test"
        findings.append(
            Finding(
                rule=RULE,
                path=inv.site.path,
                line=inv.site.line,
                message=f"{inv.site.loc}: '{what}' passes {flag}, which forces the test run serial "
                "(one test at a time on a multi-core runner)",
                fix=_FIX,
            )
        )

    flagged_paths = {f.path for f in findings}
    for rel in referenced_scripts(repo_root):
        script = repo_root / rel
        builds_tests = any(find_test_invocation(c.tokens) is not None for c in script_calls(script))
        constants = script_string_constants(script)
        if not builds_tests:
            builds_tests = any(v in ("nextest", "test") for v in (c.value for c in constants)) and any(
                v == "cargo" or v.endswith("/cargo") for v in (c.value for c in constants)
            )
        if not builds_tests:
            continue
        if rel in flagged_paths:
            continue  # the literal argv is already a violation above
        for const in constants:
            value, lineno = const.value, const.line
            if value not in NOCAPTURE_FLAGS:
                continue
            if allowed(source_line(script, lineno), RULE):
                continue
            findings.append(
                Finding(
                    rule=RULE,
                    path=rel,
                    line=lineno,
                    status=Status.NEEDS_REVIEW,
                    message=f"line {lineno}: this CI script can append '{value}' to its test command; "
                    "cannot statically confirm the opt-in is off in CI",
                    fix="make sure no workflow turns this opt-in on (it serializes the suite); if it is "
                    "a local-debugging switch, say so with a same-line '# ci-lint: allow RUST-017 "
                    "<reason>' comment. " + _FIX,
                )
            )
    return findings
