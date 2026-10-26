"""Possible values of a setup-soldr key input, for CACHE-038 (#125).

A local composite wrapper often forwards `cache-key-suffix` through an
expression rather than a bare `${{ inputs.<n> }}` -- template-python-rust-cmd's
`.github/actions/soldr/action.yml` appends the planner's PR key component only
on a saving PR run:

    ${{ inputs.save-cache == 'true' && inputs.cache-key-pr != '' &&
        (inputs.cache-key-suffix != '' && format('{0}-{1}', inputs.cache-key-suffix,
        inputs.cache-key-pr) || inputs.cache-key-pr) || inputs.cache-key-suffix }}

This module evaluates the small GitHub expression subset such wrappers use
(`&&`, `||`, `!`, `==`, `!=`, parentheses, string literals, `format(...)`,
`inputs.*` resolved from the caller's `with:` or the input default) to the SET
of strings it can produce. Anything it cannot bound returns None, which
CACHE-038 keeps reporting as a dynamic suffix (needs_review).

An unknown operand is allowed only inside a comparison (it makes the
comparison "either"); as a VALUE it is unresolvable -- except the PR key
component CACHE-013 standardises: the planner's `cache_key_pr` output (`""` or
`pr-<N>`) and `github.event.pull_request.number` (`""` or `<N>`). One run has
one PR number, so a fixed sample stands in for `<N>` in every job alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ci_lint.yaml_io import YamlValue

_TRUE = "\x01T"
_FALSE = "\x01F"
_EXPR_RE = re.compile(r"^\$\{\{(.*)\}\}$", re.DOTALL)
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>&&|\|\||==|!=|[!(),])|(?P<word>[A-Za-z_][A-Za-z0-9_.\-]*|\d+))"
)
_PR_SAMPLE = "pr-1"


class Unresolvable(Exception):
    """The expression's value set cannot be bounded statically."""


@dataclass(frozen=True)
class Token:
    kind: str  # "str" | "op" | "word"
    text: str


@dataclass(frozen=True)
class InputDefault:
    """One composite-action input's declared default."""

    name: str
    value: str


@dataclass(frozen=True)
class Context:
    """How `inputs.*` resolve: the caller's `with:` plus the composite's defaults."""

    caller: dict[str, YamlValue]
    defaults: tuple[InputDefault, ...]
    composite: bool


@dataclass(frozen=True)
class Values:
    """A value set; `unknown` = some operand the evaluator cannot bound."""

    items: frozenset[str]
    unknown: bool = False


_UNKNOWN = Values(frozenset(), unknown=True)
_EITHER = Values(frozenset({_TRUE, _FALSE}))


def _tokenize(body: str) -> list[Token]:
    out: list[Token] = []
    pos = 0
    body = body.rstrip()
    while pos < len(body):
        m = _TOKEN_RE.match(body, pos)
        if m is None or m.end() == pos:
            raise Unresolvable(body)
        kind = m.lastgroup or ""
        out.append(Token(kind, m.group(kind)))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, tokens: list[Token], ctx: Context) -> None:
        self.tokens = tokens
        self.pos = 0
        self.ctx = ctx

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, text: str | None = None) -> Token:
        tok = self.peek()
        if tok is None or (text is not None and tok.text != text):
            raise Unresolvable(text or "end")
        self.pos += 1
        return tok

    def parse(self) -> Values:
        value = self.or_()
        if self.peek() is not None:
            raise Unresolvable("trailing tokens")
        return value

    def or_(self) -> Values:
        left = self.and_()
        while (tok := self.peek()) is not None and tok.text == "||":
            self.take()
            left = _or(left, self.and_())
        return left

    def and_(self) -> Values:
        left = self.cmp()
        while (tok := self.peek()) is not None and tok.text == "&&":
            self.take()
            left = _and(left, self.cmp())
        return left

    def cmp(self) -> Values:
        left = self.unary()
        tok = self.peek()
        if tok is not None and tok.text in ("==", "!="):
            self.take()
            return _compare(left, self.unary(), tok.text == "==")
        return left

    def unary(self) -> Values:
        tok = self.peek()
        if tok is not None and tok.text == "!":
            self.take()
            return _not(self.unary())
        return self.primary()

    def primary(self) -> Values:
        tok = self.take()
        if tok.kind == "str":
            return Values(frozenset({tok.text[1:-1].replace("''", "'")}))
        if tok.text == "(":
            value = self.or_()
            self.take(")")
            return value
        if tok.kind != "word":
            raise Unresolvable(tok.text)
        nxt = self.peek()
        if nxt is not None and nxt.text == "(":
            return self.call(tok.text)
        return self.ref(tok.text)

    def call(self, name: str) -> Values:
        self.take("(")
        args: list[Values] = []
        while (tok := self.peek()) is not None and tok.text != ")":
            args.append(self.or_())
            if (sep := self.peek()) is not None and sep.text == ",":
                self.take()
        self.take(")")
        if name != "format" or not args:
            return _UNKNOWN
        return _format(args[0], args[1:])

    def ref(self, name: str) -> Values:
        low = name.lower()
        if low in ("true", "false"):
            return Values(frozenset({_TRUE if low == "true" else _FALSE}))
        if low == "null":
            return Values(frozenset({""}))
        if name.isdigit():
            return Values(frozenset({name}))
        if not low.startswith("inputs.") and low.endswith("cache_key_pr"):
            return Values(frozenset({"", _PR_SAMPLE}))
        if low == "github.event.pull_request.number":
            return Values(frozenset({"", "1"}))
        if low.startswith("inputs.") and self.ctx.composite:
            return _input_values(name[len("inputs.") :], self.ctx)
        return _UNKNOWN


def _input_values(name: str, ctx: Context) -> Values:
    if name in ctx.caller:
        raw = ctx.caller[name]
        text = _scalar(raw)
        if text is None:
            return Values(frozenset({""}))
        if "${{" in text:
            return _eval(text, Context(caller={}, defaults=(), composite=False), allow_unknown=True)
        return Values(frozenset({text}))
    return Values(frozenset({next((d.value for d in ctx.defaults if d.name == name), "")}))


def _scalar(value: YamlValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _truthy(v: str) -> bool:
    return v not in ("", _FALSE)


def _or(left: Values, right: Values) -> Values:
    if left.unknown:
        return _UNKNOWN
    kept = frozenset(v for v in left.items if _truthy(v))
    if len(kept) == len(left.items):
        return Values(kept)
    return Values(kept | right.items, right.unknown)


def _and(left: Values, right: Values) -> Values:
    if left.unknown:
        return _UNKNOWN
    falsy = frozenset(v for v in left.items if not _truthy(v))
    if len(falsy) == len(left.items):
        return Values(falsy)
    return Values(falsy | right.items, right.unknown)


def _not(value: Values) -> Values:
    if value.unknown:
        return _EITHER
    return Values(frozenset(_FALSE if _truthy(v) else _TRUE for v in value.items))


def _compare(left: Values, right: Values, equal: bool) -> Values:
    if left.unknown or right.unknown:
        return _EITHER
    results = {_TRUE if ((a == b) == equal) else _FALSE for a in left.items for b in right.items}
    return Values(frozenset(results))


def _format(fmt: Values, args: list[Values]) -> Values:
    if fmt.unknown or any(a.unknown for a in args):
        return _UNKNOWN
    outs: set[str] = set()
    for template in fmt.items:
        partial = {template}
        for i, arg in enumerate(args):
            partial = {p.replace("{" + str(i) + "}", a) for p in partial for a in arg.items}
        outs |= partial
    return Values(frozenset(outs))


def _eval(text: str, ctx: Context, allow_unknown: bool = False) -> Values:
    m = _EXPR_RE.match(text.strip())
    if m is None:
        if "${{" in text:
            raise Unresolvable(text)
        return Values(frozenset({text.strip()}))
    value = _Parser(_tokenize(m.group(1)), ctx).parse()
    if value.unknown and not allow_unknown:
        raise Unresolvable(text)
    return value


def possible_values(raw: YamlValue, ctx: Context) -> frozenset[str] | None:
    """Every string `raw` can evaluate to, or None when it cannot be bounded."""

    text = _scalar(raw)
    if text is None:
        return frozenset({""})
    try:
        value = _eval(text, ctx)
    except Unresolvable:
        return None
    if _TRUE in value.items:
        return None
    return frozenset("" if v == _FALSE else v.strip() for v in value.items)
