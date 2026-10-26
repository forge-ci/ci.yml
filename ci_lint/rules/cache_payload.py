"""CACHE-007 (round M2-20, zackees/ci.yml#44 part A): a cache payload must
never contain a `[cache].never`-forbidden content class -- linked test
binaries, nextest archives, `incremental/`, or a whole `target/` directory
(zackees/zccache#1525, soldr#2931-#2938). Two evidence sources:

* Static: every `path:` input given to a sanctioned `actions/cache*` step
  (inside `[allow].cache-actions.only-in`, or a raw `actions/cache*` use --
  CACHE-001 already flags the latter separately, this rule still classifies
  its paths) is classified by `ci_lint.cache.payload_classify`.
* Runtime: `ci-lint cache payload-check --manifest <file>` classifies the
  actual file list a save wrote (a JSON array of path strings -- e.g. from
  `tar -tf` on the archive, or a wrapper-emitted manifest), for the case a
  glob path like `target/debug/` looks clean statically but resolves to a
  forbidden class once expanded.
"""

from __future__ import annotations

import json
from pathlib import Path

from ci_lint.cache.payload_classify import classify_payload_paths
from ci_lint.finding import Finding
from ci_lint.rules.tools import _iter_uses_with
from ci_lint.rules.cache_static import _is_cache_action_slug, _loaded_files
from ci_lint.schema import CiToml
from ci_lint.yaml_io import YamlValue


def _findings_for_paths(paths: list[str], *, location: str) -> list[Finding]:
    findings: list[Finding] = []
    for violation in classify_payload_paths(paths):
        findings.append(
            Finding(
                rule="CACHE-007",
                path=location,
                message=f"cached path '{violation.path}' {violation.render().split(': ', 1)[1]}",
                fix="remove the forbidden path from the cache payload (cache the ccache/zccache object store, "
                "not the build tree); linked test binaries, "
                "nextest archives, incremental/ output, a whole target/ directory and a CMake "
                "build tree (build/, CMakeFiles/, CMakeCache.txt) are per-run outputs -- publish them as a short-retention artifact instead, never a "
                "cross-run cache ([cache].never; zackees/zccache#1525, soldr#2931-#2938)",
            )
        )
    return findings


def _with_paths(with_: dict[str, YamlValue]) -> list[str]:
    raw = with_.get("path")
    if isinstance(raw, str):
        return [line for line in raw.splitlines() if line.strip()]
    return []


def check_cache_007_static(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for loaded in _loaded_files(repo_root):
        path, is_composite, doc = loaded.path, loaded.is_composite, loaded.doc
        for use_step in _iter_uses_with(doc, is_composite=is_composite):
            uses = use_step.uses
            loc = use_step.loc
            with_ = use_step.with_
            slug = uses.split("@", 1)[0]
            if not _is_cache_action_slug(slug):
                continue
            paths = _with_paths(with_)
            if not paths:
                continue
            findings.extend(_findings_for_paths(paths, location=f"{path}#{loc}"))
    return findings


class PayloadManifestError(ValueError):
    """A `--manifest` file that is not a JSON array of path strings."""


def load_manifest_paths(manifest_path: Path) -> list[str]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadManifestError(f"could not read/parse '{manifest_path}' as JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(p, str) for p in data):
        raise PayloadManifestError(f"'{manifest_path}' must be a JSON array of path strings")
    return data


def check_cache_007_manifest(paths: list[str], *, manifest_path: str) -> list[Finding]:
    return _findings_for_paths(paths, location=manifest_path)
