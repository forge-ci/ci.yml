"""`ci-lint example drift`: issue #6's round rule made machine-checked
(round-6C brief, item 3) -- "the ci.toml example is identical in both
repos apart from repo-specific values."

Compares `examples/rust-pypi-app/ci.toml` (or any `--example` path)
against a target repository's `ci.toml`, key by key, over the RAW parsed
TOML structure -- never through `ci_lint.schema`'s typed loader, which
normalizes away exactly the key-presence and per-field distinctions this
check exists to catch (a dropped `[suites.<id>].required = true`, an
extra top-level table, a `[cache.family]` member whose cardinality
(`per`) silently changed).

Every difference is classified as either:

- **repo-specific** (allowed; the round rule's own list): the top-level
  `linter` pin; each `[platforms.<id>]`'s `runs-on`/`wheel`;
  `[rust].public`/`private`/`ship` and `[rust.tests].binaries`;
  `[allow].platform-selector`/`platform-code`; each `[suites.<id>]`'s
  `run` command (its own repo-specific test-invocation path -- this is
  what "test paths" in the brief means: the *command*, never `required`/
  `gating`/`cache`/`kind`, which are policy); each
  `[cache.family.<name>]`'s `max`/`min`; the whole `[[exceptions]]`
  array (inherently repo-specific by construction); `[python].cli`'s
  `name`/`crate`.
- **schema/policy drift** (everything else: a missing/extra table or
  key, a different enum/value, a different flow/tag/rule) -- exit 1.

AGENTS.md's typed-boundary rule applies: `TomlValue` types the wire
boundary this module reads (stdlib `tomllib`), and every comparison
downstream of `_load_toml` works over frozen `DriftEntry` records, never
a raw dict pulled out of the diff.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ci_lint.cargo_messages import JsonValue

TomlScalar = None | bool | int | float | str
TomlValue = TomlScalar | list["TomlValue"] | dict[str, "TomlValue"]


class ExampleDriftError(Exception):
    """The `--example` or `--repo` ci.toml could not be read or parsed --
    exit 2, never silently treated as "no differences"."""


@dataclass(frozen=True)
class DriftEntry:
    path: str
    kind: str  # "missing_in_repo" | "extra_in_repo" | "value_differs"
    repo_specific: bool
    example: object
    repo: object


def _load_toml(path: Path, label: str) -> dict[str, TomlValue]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ExampleDriftError(f"cannot read {label} ci.toml {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ExampleDriftError(f"cannot parse {label} ci.toml {path}: {exc}") from exc


# A repository's WORKFLOW GRAPH is its own: the example is ONE repository
# (template-python-rust-cmd, 2 workflows), while soldr ships 29 and
# running-process 22. Which workflow files exist, and which events each may
# select on, is a per-repo fact -- the same class of difference as a platform's
# `runs-on`. Reporting it as policy drift says "you should have the template's
# workflows", which is meaningless, and it blocks exactly the repositories that
# most need to adopt the profile. What stays policy is the EVENT vocabulary,
# checked separately by GEN-008 (an undeclared workflow file) and by precheck
# group 12.
_EXACT_REPO_SPECIFIC: frozenset[tuple[str, ...]] = frozenset({
    ("linter",),
    ("rust", "public"),
    ("rust", "private"),
    ("rust", "ship"),
    ("rust", "tests", "binaries"),
    ("allow", "platform-selector"),
    ("allow", "platform-code"),
    ("python", "cli", "name"),
    ("python", "cli", "crate"),
})

@dataclass(frozen=True)
class _TailRule:
    """A path PREFIX under which one trailing key (or the whole subtree) is a
    per-repo fact. A dataclass, not a nested tuple: PY-002 forbids
    record-shaped tuples, and this is a record."""

    prefix: tuple[str, ...]
    # Empty means EVERY path under `prefix` (beyond the prefix itself) is a
    # per-repo fact -- used for `allow.workflows.<file>`, where the FILE SET
    # differs per repository. The bare table path is never exempt, so this
    # cannot mask a whole-table diff.
    tails: frozenset[str] = frozenset()

    def matches(self, path: tuple[str, ...]) -> bool:
        if path[: len(self.prefix)] != self.prefix:
            return False
        rest = path[len(self.prefix) :]
        if not rest:
            return False  # the bare table itself: still policy
        return not self.tails or rest[-1] in self.tails


_TAIL_REPO_SPECIFIC: tuple[_TailRule, ...] = (
    _TailRule(("platforms",), frozenset({"runs-on", "wheel"})),
    _TailRule(("suites",), frozenset({"run"})),
    _TailRule(("cache", "family"), frozenset({"max", "min"})),
    _TailRule(("allow", "workflows"), frozenset()),
)


def _is_repo_specific(path: tuple[str, ...]) -> bool:
    if path in _EXACT_REPO_SPECIFIC or path[:1] == ("exceptions",):
        return True
    return any(rule.matches(path) for rule in _TAIL_REPO_SPECIFIC)


def _walk(example: TomlValue, repo: TomlValue, path: tuple[str, ...], out: list[DriftEntry]) -> None:
    if isinstance(example, dict) and isinstance(repo, dict):
        example_keys = set(example)
        repo_keys = set(repo)
        for k in sorted(example_keys - repo_keys):
            p = path + (k,)
            out.append(
                DriftEntry(".".join(p), "missing_in_repo", _is_repo_specific(p), example[k], None)
            )
        for k in sorted(repo_keys - example_keys):
            p = path + (k,)
            out.append(DriftEntry(".".join(p), "extra_in_repo", _is_repo_specific(p), None, repo[k]))
        for k in sorted(example_keys & repo_keys):
            _walk(example[k], repo[k], path + (k,), out)
        return
    if example == repo:
        return
    out.append(DriftEntry(".".join(path), "value_differs", _is_repo_specific(path), example, repo))


def compute_example_drift(example_path: Path, repo_ci_toml: Path) -> tuple[DriftEntry, ...]:
    example = _load_toml(example_path, "--example")
    repo = _load_toml(repo_ci_toml, "--repo")
    out: list[DriftEntry] = []
    _walk(example, repo, (), out)
    return tuple(out)


def to_json_dict(entries: tuple[DriftEntry, ...]) -> dict[str, JsonValue]:
    return {
        "entries": [
            {
                "path": e.path,
                "kind": e.kind,
                "repo_specific": e.repo_specific,
                "example": e.example,
                "repo": e.repo,
            }
            for e in entries
        ],
        "policy_drift_count": sum(1 for e in entries if not e.repo_specific),
        "repo_specific_count": sum(1 for e in entries if e.repo_specific),
    }


def render_text(entries: tuple[DriftEntry, ...], *, example_path: Path, repo_ci_toml: Path) -> str:
    lines = [f"ci-lint example drift: {example_path} vs {repo_ci_toml}"]
    if not entries:
        lines.append("no differences")
        return "\n".join(lines)

    repo_specific = [e for e in entries if e.repo_specific]
    drift = [e for e in entries if not e.repo_specific]

    if repo_specific:
        lines.append("")
        lines.append(f"repo-specific ({len(repo_specific)}, allowed):")
        for e in repo_specific:
            lines.append(f"  {e.kind:<16} {e.path}: example={e.example!r} repo={e.repo!r}")
    if drift:
        lines.append("")
        lines.append(f"SCHEMA/POLICY DRIFT ({len(drift)}, must match the example):")
        for e in drift:
            lines.append(f"  {e.kind:<16} {e.path}: example={e.example!r} repo={e.repo!r}")

    lines.append("")
    lines.append(
        f"ci-lint example drift: {len(drift)} policy violation(s), "
        f"{len(repo_specific)} repo-specific difference(s)"
    )
    return "\n".join(lines)
