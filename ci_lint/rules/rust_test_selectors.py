"""RUST-015: a filtered feature pass must select its test targets.

zackees/ci.yml#81 (evidence: zackees/running-process#1234). A nextest `-E`
filter-expression -- or a positional name filter to either `cargo nextest
run` or `cargo test` -- chooses which tests *run*, never what *compiles*. A
"run these 15 tests with feature X" pass without a target selector rebuilds
every test target of the package with that feature set: running-process's
daemon-feature pass compiled ~30 test binaries (32.4 s in CI) for 15 tests
living in two of them; adding `--lib --test daemon_integration` cut the
compile to 18.6 s for the same 15 tests.

Static signal (run: lines, one level into a referenced ci/*.py script): a
test invocation that combines a non-default feature set (`--features`/`-F`,
`--all-features`, `--no-default-features`) with a test filter (`-E`/
`--filter-expr`/`--filterset`, or a positional name filter before `--`) and
no target selector (`--lib`, `--bin`, `--test`, `--example`, `--bench`,
`--doc`, `--bins`/`--examples`/`--benches`). `--tests`/`--all-targets` are
NOT selectors here -- they select every test target, which is the problem.

A same-line `# ci-lint: allow RUST-015 <reason>` excuses one line.
"""

from __future__ import annotations

from dataclasses import dataclass

import re
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.rules.test_invocations import allowed, iter_test_invocations

RULE = "RUST-015"

FEATURE_FLAGS: frozenset[str] = frozenset({"--features", "-F", "--all-features", "--no-default-features"})
FILTER_FLAGS: frozenset[str] = frozenset({"-E", "--filter-expr", "--filterset"})
SELECTOR_FLAGS: frozenset[str] = frozenset(
    {"--lib", "--bin", "--bins", "--test", "--example", "--examples", "--bench", "--benches", "--doc"}
)

# Options (cargo test + cargo nextest run) whose value is the next token.
VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "-p", "--package", "--exclude", "--features", "-F", "--target", "--target-dir",
        "--manifest-path", "-j", "--jobs", "--profile", "-P", "--cargo-profile", "--color",
        "--message-format", "--config", "-Z", "--bin", "--test", "--example", "--bench",
        "-E", "--filter-expr", "--filterset", "--partition", "--retries", "--test-threads",
        "--run-ignored", "--archive-file", "--archive-format", "--workspace-remap",
        "--tool-config-file", "--cargo-metadata", "--binaries-metadata", "--target-dir-remap",
        "--build-jobs", "--max-fail", "--failure-output", "--success-output", "--status-level",
        "--final-status-level", "--hide-progress-bar", "--timings", "--lockfile-path",
    }
)


# A shell redirection ends the argv (`2>&1`, `> log`, `2>/dev/null`).
_REDIRECT_RE = re.compile(r"^\d*[<>]")


@dataclass(frozen=True)
class _ArgClass:
    has_features: bool
    filt: str | None
    has_selector: bool


def _classify(args: tuple[str, ...]) -> _ArgClass:
    """Whether a feature set, a test filter and a target selector appear."""

    has_features = False
    filt: str | None = None
    has_selector = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or _REDIRECT_RE.match(arg):
            break
        name = arg.split("=", 1)[0] if arg.startswith("--") else arg
        if name in FEATURE_FLAGS or (arg.startswith("-F") and len(arg) > 2):
            has_features = True
        if name in SELECTOR_FLAGS:
            has_selector = True
        if name in FILTER_FLAGS and filt is None:
            value = arg.split("=", 1)[1] if "=" in arg else (args[i + 1] if i + 1 < len(args) else "")
            filt = f"{name} '{value}'"
        if arg in VALUE_OPTIONS and "=" not in arg:
            i += 2
            continue
        if not arg.startswith("-") and "$" not in arg and filt is None:
            filt = f"name filter '{arg}'"
        i += 1
    return _ArgClass(has_features, filt, has_selector)


def check_rust_015(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for inv in iter_test_invocations(repo_root):
        cls = _classify(inv.args)
        has_features, filt, has_selector = cls.has_features, cls.filt, cls.has_selector
        if not has_features or filt is None or has_selector or allowed(inv.site.raw, RULE):
            continue
        what = "cargo nextest run" if inv.kind == "nextest" else "cargo test"
        findings.append(
            Finding(
                rule=RULE,
                path=inv.site.path,
                line=inv.site.line,
                message=f"{inv.site.loc}: '{what}' runs a non-default feature set with a {filt} but no "
                "target selector -- the filter limits which tests run, not what compiles, so every "
                "test target of the package is rebuilt with those features",
                fix="list the targets that hold the filtered tests ('soldr cargo nextest list' with the same "
                "filter names their binaries) and add them as selectors, e.g. '--lib --test <name>' "
                "(running-process#1234: 32.4 s -> 18.6 s compile for the same 15 tests); '--tests' and "
                "'--all-targets' select every test target and do not count",
            )
        )
    return findings
