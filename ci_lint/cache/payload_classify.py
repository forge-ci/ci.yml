"""CACHE-007 (round M2-20, zackees/ci.yml#44 part A): classify a cache
payload's declared/actual paths against `[cache].never`'s forbidden content
classes -- linked test binaries, nextest archives, `incremental/`, and a
whole `target/` directory (zackees/zccache#1525, soldr#2931-#2938), and a
CMake build tree (zackees/ci.yml#393).

`ci.toml` schema 3 already accepts `[cache].never` as a *declaration*
(policy-general.md's Cache policy); this module is the first place that
inspects real path strings -- either the paths a workflow declares it will
cache (static, via `ci_lint.rules.cache_payload`) or the paths a save
actually wrote (runtime, via a JSON manifest) -- against that declaration,
so a payload that smuggles a forbidden class in is caught by content, not
by trusting the family's own label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# One entry per `[cache].never` content class this module can recognize
# from a path string alone. Order matters only for the first-match message;
# a path can trip more than one class and every match is reported.
@dataclass(frozen=True)
class _NeverClass:
    content_class: str
    pattern: re.Pattern[str]


_NEVER_CLASS_PATTERNS: tuple[_NeverClass, ...] = (
    _NeverClass(
        "linked test binary",
        re.compile(r"(?:^|/)target/(?:[^/]+/)?[^/]+/deps/[^/]*-[0-9a-f]{16}(?:\.exe)?$"),
    ),
    _NeverClass(
        "nextest archive",
        re.compile(r"\.tar\.zst$|nextest-archive"),
    ),
    _NeverClass(
        "incremental/",
        re.compile(r"(?:^|/)incremental(?:/|$)"),
    ),
    _NeverClass(
        "whole target/ directory",
        re.compile(r"(?:^|/)target/?$"),
    ),
    # zackees/ci.yml#393 (docs/policy-cpp.md): a CMake build tree is per-run
    # output keyed on absolute paths and configure state; the compiler's
    # object cache (ccache/zccache) is the reusable part. A whole build
    # directory (`build/`, `build-<x>/`, `build_<x>/`, `cmake-build-<x>/`,
    # `out/build/<preset>/`), or any path inside CMake's own `CMakeFiles/` or
    # `CMakeCache.txt`, is the build tree.
    _NeverClass(
        "CMake build tree",
        re.compile(
            r"(?:^|/)(?:_?build|build[-_][A-Za-z0-9._-]+|cmake-build-[A-Za-z0-9._-]+|out/build(?:/[^/]+)?)/?$"
            r"|(?:^|/)CMakeFiles(?:/|$)|(?:^|/)CMakeCache\.txt$"
        ),
    ),
)


def is_cmake_build_tree(path: str) -> bool:
    """True when `path` is (or is inside the metadata of) a CMake build tree."""

    pattern = next(n.pattern for n in _NEVER_CLASS_PATTERNS if n.content_class == "CMake build tree")
    return pattern.search(path.strip().replace("\\", "/")) is not None


@dataclass(frozen=True)
class PayloadViolation:
    path: str
    content_class: str

    def render(self) -> str:
        return f"{self.path}: matches forbidden class '{self.content_class}' ([cache].never)"


def classify_payload_paths(paths: list[str]) -> list[PayloadViolation]:
    """Return one `PayloadViolation` per (path, matched class) pair. A path
    that matches more than one forbidden class produces more than one
    violation -- deliberately, so the fix message names every reason."""

    violations: list[PayloadViolation] = []
    for raw in paths:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        for never in _NEVER_CLASS_PATTERNS:
            if never.pattern.search(path):
                violations.append(PayloadViolation(path=raw, content_class=never.content_class))
    return violations
