"""A small Rust lexer that blanks comments and string-literal content, and
finds attribute/macro spans across line breaks.

Round-2A brief, defect 2: `ci_lint.rules.layout`'s LAYOUT-001 scan and
`ci_lint.cargo_scan.has_cfg_feature` (RUST-011) matched on raw source
lines, so a doc comment merely *mentioning* a selector -- for example
`//! supports #[cfg(target_os = "linux")] per platform` or
`/// see cfg(feature = "json")` -- was flagged exactly like real code.

`strip_comments_and_strings` is not a full Rust tokenizer: it only needs to
turn the *content* of `//`, `//!`, `///` and `/* */` (nestable) comments,
plus `"..."`, `b"..."`, raw (`r"..."`, `r#"...Byte"#`, ...) and raw-byte
string literals, into blanks -- preserving every newline so line numbers of
the surviving code are unchanged, and leaving single-quoted lifetimes
(`'a`, `'static`) alone rather than misreading them as unterminated char
literals.

Round-6B, defect (template-python-rust-cmd, round 2F): LAYOUT-001 and
RUST-011's `cfg(feature` check both scanned line by line too, so an
attribute or macro call split across lines -- `#[cfg(\n    windows\n)]` --
evaded both. `find_attribute_and_macro_spans` matches bracket nesting to
find whole `#[...]` / `#![...]` attributes, `cfg!(...)` macro calls, and
`cfg_select! { ... }` blocks regardless of internal line breaks, returning
each as one whitespace-normalized span anchored at the line it starts on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _is_blank(ch: str) -> str:
    return ch if ch == "\n" else " "


def _char_literal_len(source: str, i: int) -> int | None:
    """If `source[i] == \"'\"` starts a char literal (`'x'`, `'\\n'`,
    `'\\u{1F600}'`, ...), return its length including both quotes; else
    None (it is a lifetime like `'a` or `'static`, or a lone `'`)."""

    n = len(source)
    j = i + 1
    if j >= n:
        return None
    if source[j] == "\\":
        k = j + 1
        if k >= n:
            return None
        if source[k] == "u" and k + 1 < n and source[k + 1] == "{":
            end_brace = source.find("}", k)
            if end_brace == -1:
                return None
            k = end_brace + 1
        else:
            k += 1
        if k < n and source[k] == "'":
            return k + 1 - i
        return None
    if source[j] != "'" and j + 1 < n and source[j + 1] == "'":
        return j + 2 - i
    return None


@dataclass(frozen=True)
class _RawPrefix:
    prefix_len: int
    hashes: int


def _raw_string_prefix_len(source: str, i: int) -> _RawPrefix | None:
    """If a raw (byte) string literal starts at `i` (`r"`, `r#"`, `br##"`,
    ...), return `(prefix_len, hash_count)` where `prefix_len` covers
    everything up to and including the opening quote; else None."""

    n = len(source)
    j = i
    if j < n and source[j] == "b":
        j += 1
    if j >= n or source[j] != "r":
        return None
    j += 1
    hashes = 0
    while j < n and source[j] == "#":
        hashes += 1
        j += 1
    if j >= n or source[j] != '"':
        return None
    return _RawPrefix(j + 1 - i, hashes)


def strip_comments_and_strings(source: str) -> str:  # noqa: C901
    out: list[str] = []
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        # `//`, `//!`, `///` line comments: blank to end of line.
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue

        # `/* ... */` block comments, nestable.
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            depth = 1
            out.append("  ")
            i += 2
            while i < n and depth > 0:
                if source[i : i + 2] == "/*":
                    depth += 1
                    out.append("  ")
                    i += 2
                elif source[i : i + 2] == "*/":
                    depth -= 1
                    out.append("  ")
                    i += 2
                else:
                    out.append(_is_blank(source[i]))
                    i += 1
            continue

        # raw / raw-byte strings: (b)?r(#*)"..."(#*)
        raw = _raw_string_prefix_len(source, i)
        if raw is not None:
            prefix_len, hashes = raw.prefix_len, raw.hashes
            out.append(" " * prefix_len)
            body_start = i + prefix_len
            closer = '"' + "#" * hashes
            end = source.find(closer, body_start)
            if end == -1:
                out.append("".join(_is_blank(c) for c in source[body_start:n]))
                i = n
            else:
                out.append("".join(_is_blank(c) for c in source[body_start:end]))
                out.append(" " * len(closer))
                i = end + len(closer)
            continue

        # normal / byte strings: "..." or b"..." (with \-escapes).
        if ch == '"' or (ch == "b" and i + 1 < n and source[i + 1] == '"'):
            start = i
            i += 1 if ch == '"' else 2
            out.append(" " * (i - start))
            while i < n and source[i] != '"':
                if source[i] == "\\" and i + 1 < n:
                    out.append(_is_blank(source[i]))
                    out.append(_is_blank(source[i + 1]))
                    i += 2
                    continue
                out.append(_is_blank(source[i]))
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue

        # char literal vs. lifetime.
        if ch == "'":
            length = _char_literal_len(source, i)
            if length is not None:
                out.append(" " * length)
                i += length
                continue
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


@dataclass(frozen=True)
class AttrSpan:
    """One `#[...]` / `#![...]` attribute, `cfg!(...)` macro call, or
    `cfg_select! { ... }` block, found by matching bracket depth rather
    than scanning line by line -- so a form split across source lines,
    e.g. `#[cfg(\n    windows\n)]` or
    `#[cfg_attr(\n    unix,\n    derive(Debug)\n)]`, is returned as ONE
    span. `text` is the span's source with internal whitespace/newlines
    collapsed to single spaces; `line` is the 1-based source line the span
    STARTS on, for reporting."""

    line: int
    text: str
    start: int = 0
    end: int = 0


_ATTR_START_RE = re.compile(r"#!?\[")
_CFG_MACRO_START_RE = re.compile(r"\bcfg!\s*\(")
_CFG_SELECT_START_RE = re.compile(r"\bcfg_select!\s*\{")


def _line_at(code: str, index: int) -> int:
    return code.count("\n", 0, index) + 1


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _match_bracket(code: str, open_index: int, open_ch: str, close_ch: str) -> int | None:
    """`code[open_index] == open_ch`; return the index of the `close_ch`
    that closes it. Depth is tracked only for `open_ch`/`close_ch`
    themselves -- well-formed Rust code nests each bracket kind (`(`/`)`,
    `[`/`]`, `{`/`}`) independently of the others, so this is enough
    without a full tokenizer, and it is immune to a different bracket kind
    appearing inside (e.g. the `(...)` groups inside a `#[...]`
    attribute)."""

    depth = 0
    i = open_index
    n = len(code)
    while i < n:
        c = code[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _iter_bracket_spans(
    code: str, start_re: re.Pattern[str], open_ch: str, close_ch: str
) -> list[AttrSpan]:
    spans: list[AttrSpan] = []
    for m in start_re.finditer(code):
        open_index = m.end() - 1
        if open_index < 0 or code[open_index] != open_ch:
            continue
        close_index = _match_bracket(code, open_index, open_ch, close_ch)
        if close_index is None:
            continue
        raw = code[m.start() : close_index + 1]
        spans.append(
            AttrSpan(
                line=_line_at(code, m.start()),
                text=_normalize_whitespace(raw),
                start=m.start(),
                end=close_index + 1,
            )
        )
    return spans


def find_attribute_and_macro_spans(code: str) -> list[AttrSpan]:
    """Scan *code* -- already passed through `strip_comments_and_strings`
    -- for `#[...]` / `#![...]` attributes, `cfg!(...)` macro calls, and
    `cfg_select! { ... }` blocks, matching nested brackets so a form split
    across lines comes back as one normalized span anchored at its
    opening line. Callers (LAYOUT-001's host-selector scan,
    `cargo_scan.has_cfg_feature`'s RUST-011 `cfg(feature` scan) search
    each span's `.text` instead of scanning raw source lines, so a
    multi-line attribute or macro call is seen whole."""

    spans: list[AttrSpan] = []
    spans.extend(_iter_bracket_spans(code, _ATTR_START_RE, "[", "]"))
    spans.extend(_iter_bracket_spans(code, _CFG_MACRO_START_RE, "(", ")"))
    spans.extend(_iter_bracket_spans(code, _CFG_SELECT_START_RE, "{", "}"))
    spans.sort(key=lambda s: s.line)
    return spans
