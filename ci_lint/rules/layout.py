"""Group 6: platform-code layout (LAYOUT-001).

Host selection belongs in exactly one facade per language: the globs in
`[allow].platform-code`, or the single file named by `[allow].platform-
selector`. Everything else -- including tests -- is in scope. `dylints/**`
(lint-crate fixtures legitimately exercise host cfg) and `ci_lint/**` (this
package's own RED fixtures do too) are excluded.
"""

from __future__ import annotations

import re
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.globs import matches_any
from ci_lint.py_lexer import strip_comments_and_strings as py_strip
from ci_lint.repo_files import list_repo_files
from ci_lint.rust_lexer import find_attribute_and_macro_spans
from ci_lint.rust_lexer import strip_comments_and_strings as rust_strip
from ci_lint.schema import CiToml

RUST_CFG_RE = re.compile(r"#\[cfg(_attr)?\s*\(|cfg!\s*\(|cfg_select!")
RUST_SELECTORS: tuple[str, ...] = (
    "windows",
    "unix",
    "target_os",
    "target_family",
    "target_arch",
    "target_env",
    "target_abi",
    "target_vendor",
    "target_endian",
    "target_pointer_width",
)
# Guest-target predicates (#95): selecting the wasm guest build is not host
# OS selection. A cfg span whose every selector predicate is one of these,
# with no bare `windows`/`unix`, is not a LAYOUT-001 subject -- the same set
# kernal-api's own platform-boundary Dylint treats as guest-only.
GUEST_PREDICATES: frozenset[str] = frozenset(
    {
        'target_family="wasm"',
        'target_arch="wasm32"',
        'target_arch="wasm64"',
        'target_os="wasi"',
        'target_os="unknown"',
    }
)
_SELECTOR_PAIR_RE = re.compile(r'\b(target_[a-z_]+)\s*=\s*"([^"]*)"')
_BARE_HOST_RE = re.compile(r"\b(windows|unix)\b")
RUST_HOST_PATHS: tuple[str, ...] = ("std::os::", "libc::", "windows_sys::")
PY_SELECTORS: tuple[str, ...] = ("sys.platform", "platform.system(", "os.name")
EXCLUDED_PREFIXES: tuple[str, ...] = ("dylints/", "ci_lint/")
PY_SCAN_PREFIXES: tuple[str, ...] = ("src/", "tests/", "ci/")


def _is_guest_only(stripped: str, raw: str) -> bool:
    """True when every selector predicate in the span is a guest-target one
    (`GUEST_PREDICATES`). Values come from *raw* (strings intact); bare
    `windows`/`unix` come from *stripped* so a string cannot hide one."""

    if _BARE_HOST_RE.search(stripped):
        return False
    pairs = [f'{k}="{v}"' for k, v in _SELECTOR_PAIR_RE.findall(raw)]
    if not pairs:
        return False
    if len(pairs) != sum(stripped.count(sel) for sel in RUST_SELECTORS if sel.startswith("target_")):
        return False
    return all(p in GUEST_PREDICATES for p in pairs)


def _check_rust_file(repo_root: Path, rel: str) -> list[Finding]:
    """Round-6B: host selectors inside `#[cfg(...)]` / `#[cfg_attr(...)]` /
    `cfg!(...)` / `cfg_select! {...}` are matched over the whole
    (bracket-matched, whitespace-normalized) attribute/macro span -- not
    line by line -- so a form split across lines, e.g.
    `#[cfg(\n    windows\n)]`, is still caught. `std::os::`/`libc::`/
    `windows_sys::` path references stay a per-line substring scan; they
    are not attribute/macro syntax and are not expected to split across
    lines."""

    findings: list[Finding] = []
    try:
        text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    code = rust_strip(text)

    for span in find_attribute_and_macro_spans(code):
        if RUST_CFG_RE.search(span.text) and any(sel in span.text for sel in RUST_SELECTORS):
            if _is_guest_only(span.text, text[span.start : span.end]):
                continue
            findings.append(
                Finding(
                    rule="LAYOUT-001",
                    path=rel,
                    line=span.line,
                    message=f"host selector outside the platform facade: {span.text}",
                    fix="move this platform-specific code into an [allow].platform-code path, or "
                    "into the one file named by [allow].platform-selector",
                )
            )

    for i, line in enumerate(code.splitlines(), start=1):
        for hp in RUST_HOST_PATHS:
            if hp in line:
                findings.append(
                    Finding(
                        rule="LAYOUT-001",
                        path=rel,
                        line=i,
                        message=f"references '{hp}' outside the platform facade: {line.strip()}",
                        fix="move this code behind the platform facade (e.g. crate::platform::*), "
                        "or into the one file named by [allow].platform-selector",
                    )
                )
    return findings


def _check_python_file(repo_root: Path, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = py_strip(text).splitlines()
    for i, line in enumerate(lines, start=1):
        for sel in PY_SELECTORS:
            if sel in line:
                findings.append(
                    Finding(
                        rule="LAYOUT-001",
                        path=rel,
                        line=i,
                        message=f"references '{sel}' outside the platform facade: {line.strip()}",
                        fix="move this platform-specific check into an [allow].platform-code path",
                    )
                )
    return findings


def check_group6(ci: CiToml, repo_root: Path) -> list[Finding]:
    tracked = list_repo_files(repo_root)

    platform_code = ci.allow.platform_code
    selector = ci.allow.platform_selector

    findings: list[Finding] = []
    for rel in tracked:
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        if rel == selector:
            continue
        if matches_any(rel, platform_code):
            continue
        if rel.endswith(".rs"):
            findings.extend(_check_rust_file(repo_root, rel))
        elif rel.endswith(".py") and rel.startswith(PY_SCAN_PREFIXES):
            findings.extend(_check_python_file(repo_root, rel))
    return findings
