"""CACHE-037 (zackees/ci.yml#121, docs/policy-rust.md "Cargo registry
cache"): the static clauses.

* Payload: a repository-owned `actions/cache*` step caching cargo-home
  paths may save only `registry/index/`, `registry/cache/`, `.global-cache`
  and `git/db/`. Anything else under cargo home -- `registry/src/`,
  `git/checkouts/`, `bin/`, the whole `registry/` or `git/`, or the whole
  `~/.cargo` -- is a violation.
* Key: such a step's `key:` must be `<os>-<arch>-<Cargo.lock hash>`. A key
  with no unresolved expression that lacks `runner.os`, `runner.arch` or a
  `hashFiles(...Cargo.lock)` component, or that hashes files besides
  `Cargo.lock`, is a violation; a key carrying any other `${{ }}`
  expression cannot be resolved statically (it may supply a component, or
  add a toolchain digest / per-job suffix) and is `needs_review`.
* Interim: a `zackees/setup-soldr` step with `cargo-registry-cache: true`
  is `needs_review` while setup-soldr's released payload still includes
  `src/`/`checkouts/` and its key the toolchain digest (policy item 5).

Left candidate: the LF-normalised lockfile hash, the registry family's
`evict = "lru"` clause, and manifest-based payload checks.
"""

from __future__ import annotations

from ci_lint import repo_identity

import re
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.cache_static import _is_cache_action_slug, _loaded_files
from ci_lint.rules.tools import _iter_uses_with
from ci_lint.schema import CiToml

RULE = "CACHE-037"
ALLOWED_SUBPATHS = ("registry/index", "registry/cache", ".global-cache", "git/db")
_CARGO_HOME_RE = re.compile(
    r"^(?:~/\.cargo|\$HOME/\.cargo|\$\{\{\s*env\.HOME\s*\}\}/\.cargo|\.cargo"
    r"|\$CARGO_HOME|\$\{CARGO_HOME\}|\$\{\{\s*env\.CARGO_HOME\s*\}\})(?:/|$)"
)
_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_HASHFILES_RE = re.compile(r"hashFiles\((.*?)\)", re.S)
_FIX_PAYLOAD = (
    "cache only registry/index/, registry/cache/, .global-cache and git/db/ -- cargo re-extracts "
    "registry/src/ and git/checkouts/ locally (Cargo Book, 'Caching the Cargo home in CI'); "
    "prefer setup-soldr's cargo-registry-cache over a hand-rolled step (#121)"
)
_FIX_KEY = (
    "key the registry cache on <os>-<arch>-<Cargo.lock hash> only, e.g. "
    "cargo-registry-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('**/Cargo.lock') }} -- "
    "no toolchain digest or per-job suffix (#121)"
)


def cargo_home_subpath(path: str) -> str | None:
    """The cargo-home-relative part of a cache `path:` entry, or None when
    the entry is not under cargo home. `""` means the whole cargo home."""
    p = path.strip().replace("\\", "/")
    m = _CARGO_HOME_RE.match(p)
    if m is None:
        return None
    return p[m.end():].strip("/")


def _allowed(sub: str) -> bool:
    return any(sub == a or sub.startswith(a + "/") for a in ALLOWED_SUBPATHS)


def _paths(raw: object) -> list[str]:
    if not isinstance(raw, str):
        return []
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("!")]


def _key_findings(key: str, location: str) -> list[Finding]:
    exprs = [e.strip() for e in _EXPR_RE.findall(key)]
    has_os = any(e == "runner.os" for e in exprs)
    has_arch = any(e == "runner.arch" for e in exprs)
    hash_exprs = [e for e in exprs if e.startswith("hashFiles(")]
    lock_hash = False
    extra_files = False
    for e in hash_exprs:
        for args in _HASHFILES_RE.findall(e):
            parts = [a.strip().strip("'\"") for a in args.split(",") if a.strip()]
            for part in parts:
                if part.endswith("Cargo.lock"):
                    lock_hash = True
                else:
                    extra_files = True
    unresolved = [e for e in exprs if e not in ("runner.os", "runner.arch") and not e.startswith("hashFiles(")]
    missing = [n for n, ok in (("runner.os", has_os), ("runner.arch", has_arch), ("hashFiles('**/Cargo.lock')", lock_hash)) if not ok]
    if extra_files:
        return [Finding(rule=RULE, path=location, fix=_FIX_KEY,
                        message=f"cargo registry cache key '{key}' hashes files besides Cargo.lock; downloaded .crate files depend only on the lockfile")]
    if unresolved:
        what = f"lacks {', '.join(missing)} and " if missing else ""
        return [Finding(rule=RULE, path=location, fix=_FIX_KEY, status=Status.NEEDS_REVIEW,
                        message=f"cargo registry cache key '{key}' {what}carries expression(s) "
                        f"{', '.join(unresolved)} that cannot be resolved statically -- confirm the key is exactly <os>-<arch>-<lockhash>")]
    if missing:
        return [Finding(rule=RULE, path=location, fix=_FIX_KEY,
                        message=f"cargo registry cache key '{key}' lacks {', '.join(missing)}")]
    return []


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def check_cache_037(ci: CiToml, repo_root: Path) -> list[Finding]:
    del ci  # the static clauses read only workflow/composite-action steps
    findings: list[Finding] = []
    for lf in _loaded_files(repo_root):
        path = lf.path
        for step in _iter_uses_with(lf.doc, is_composite=lf.is_composite):
            slug = step.uses.split("@", 1)[0]
            location = f"{path}#{step.loc}"
            if repo_identity.is_setup_soldr(slug) and _truthy(step.with_.get("cargo-registry-cache")):
                findings.append(Finding(
                    rule=RULE, path=location, status=Status.NEEDS_REVIEW,
                    message="setup-soldr cargo-registry-cache: setup-soldr v0.9.86 still saves registry/src/ and "
                    "git/checkouts/ and keys on the toolchain digest (policy-rust.md #121, interim item 5)",
                    fix="keep cargo-registry-cache through setup-soldr; this clears once setup-soldr ships the "
                    "downloads-only payload and <os>-<arch>-<lockhash> key",
                ))
                continue
            if not _is_cache_action_slug(slug):
                continue
            subs = [(p, s) for p in _paths(step.with_.get("path")) if (s := cargo_home_subpath(p)) is not None]
            if not subs:
                continue
            for raw, sub in subs:
                if not _allowed(sub):
                    shown = sub or "(whole cargo home)"
                    findings.append(Finding(
                        rule=RULE, path=location, fix=_FIX_PAYLOAD,
                        message=f"cargo-home cache path '{raw}' ({shown}) is beyond registry/index, "
                        "registry/cache, .global-cache and git/db",
                    ))
            if any(s.startswith("registry") or s == "" for _, s in subs):
                key = step.with_.get("key")
                if isinstance(key, str):
                    findings.extend(_key_findings(key, location))
    return findings
