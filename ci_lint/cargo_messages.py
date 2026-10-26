"""Parse `cargo ... --message-format=json` output into typed records.

Round-2A brief, part 2a/2b: `ci-lint units` and `ci-lint tests size` both
read the JSON Lines a `cargo build`/`cargo test --no-run` (etc.) run wrote
with `--message-format=json`. That stream interleaves several message
`reason`s (`compiler-message`, `build-script-executed`, `build-finished`,
...) -- this module keeps only `compiler-artifact` records, converts them
into a frozen dataclass (AGENTS.md: never a raw dict past the JSON
boundary), and ignores everything else, including lines that are not even
valid JSON (cargo/rustc occasionally interleave plain text on the same
stream in practice).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# The JSON wire boundary itself, typed concretely per AGENTS.md -- never `Any`.
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class CompilerArtifact:
    """One `{"reason": "compiler-artifact", ...}` cargo message, narrowed
    to the fields `units`/`tests size` need."""

    package_name: str
    package_version: str
    manifest_path: str
    features: tuple[str, ...]  # sorted
    opt_level: str
    debuginfo: str
    profile_test: bool
    target_name: str
    target_kinds: tuple[str, ...]
    filenames: tuple[str, ...]
    executable: str | None
    fresh: bool


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_str_tuple(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str))
    return ()


@dataclass(frozen=True)
class PackageId:
    name: str
    version: str


def _parse_package_id(package_id: JsonValue) -> PackageId:
    """`package_id` is either the legacy `"name version (source)"` string
    or (cargo >= 1.77 with `-Znext-lockfile-bump`/newer toolchains) a
    `"registry+source#name@version"`-shaped SPEC string. Handle both; an
    unrecognized shape yields `("", "")` rather than raising, since a
    malformed/missing package_id must never crash the whole scan."""

    if not isinstance(package_id, str):
        return PackageId("", "")
    if " " in package_id:
        parts = package_id.split(" ", 2)
        if len(parts) >= 2:
            return PackageId(parts[0], parts[1])
        return PackageId(parts[0], "")
    if "#" in package_id:
        head, _, tail = package_id.rpartition("#")
        if "@" in tail:
            name, version = tail.rsplit("@", 1)
            return PackageId(name, version)
        # Cargo's package-ID spec omits the name when it equals the final
        # path component of the source URL: `path+file:///ws/crates/foo#0.1.0`
        # means package `foo` version `0.1.0`. The fragment is then only a
        # version, and the name is the URL's last path segment.
        name = head.rstrip("/").rsplit("/", 1)[-1] if head else ""
        return PackageId(name, tail)
    return PackageId(package_id, "")


def _one_artifact(doc: dict[str, JsonValue]) -> CompilerArtifact:
    pkg = _parse_package_id(doc.get("package_id"))
    name, version = pkg.name, pkg.version
    manifest_path = doc.get("manifest_path")
    target = _as_dict(doc.get("target"))
    profile = _as_dict(doc.get("profile"))
    opt_level = profile.get("opt_level")
    debuginfo = profile.get("debuginfo")
    profile_test = profile.get("test")
    executable = doc.get("executable")
    fresh = doc.get("fresh")
    return CompilerArtifact(
        package_name=name,
        package_version=version,
        manifest_path=manifest_path if isinstance(manifest_path, str) else "",
        features=tuple(sorted(_as_str_tuple(doc.get("features")))),
        opt_level=str(opt_level) if opt_level is not None else "",
        debuginfo="none" if debuginfo is None else str(debuginfo),
        profile_test=profile_test is True,
        target_name=str(target.get("name") or ""),
        target_kinds=_as_str_tuple(target.get("kind")),
        filenames=_as_str_tuple(doc.get("filenames")),
        executable=executable if isinstance(executable, str) else None,
        fresh=fresh is True,
    )


def parse_compiler_artifacts(lines: list[str]) -> list[CompilerArtifact]:
    """Every `compiler-artifact` message in `lines`, in order. Non-JSON
    lines and JSON messages with a different `reason` are silently
    skipped -- that is the documented shape of `--message-format=json`
    output, not an error."""

    out: list[CompilerArtifact] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg: JsonValue = json.loads(line)
        except json.JSONDecodeError:
            continue
        doc = _as_dict(msg)
        if doc.get("reason") != "compiler-artifact":
            continue
        out.append(_one_artifact(doc))
    return out


def load_artifacts_file(path: Path) -> list[CompilerArtifact]:
    text = path.read_text(encoding="utf-8")
    return parse_compiler_artifacts(text.splitlines())
