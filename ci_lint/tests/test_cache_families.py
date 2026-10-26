"""`ci_lint.cache.families` -- the `via` -> key-prefix resolution table
(round-4A brief, deliverable 1)."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from ci_lint.cache.audit import classify
from ci_lint.cache.github_cache import CacheEntry
from ci_lint.schema import CiToml, load_ci_toml
from ci_lint.cache.families import (
    ALLOWED_VIA_VALUES,
    resolve_prefix,
    resolve_prefixes,
    resolve_retired_prefix,
)


class FamilyResolutionTest(unittest.TestCase):
    def test_every_shape_from_the_round_4a_brief(self) -> None:
        # DESIGN SOURCE table, verbatim (round-4A brief, deliverable 1),
        # plus "setup-uv" (same-round amendment).
        expected = {
            "setup-soldr:build-cache": "setup-soldr-buildcache-v2-",
            "setup-soldr:cargo-registry": "setup-soldr-cargoregistry-v1-",
            "setup-soldr:cook": "cook-base-v2-",
            "setup-soldr:cross-targets": "setup-soldr-prepare-v3-",
            "setup-soldr:dylint": "setup-soldr-dylint-v2-",
            "setup-soldr:dylint-output": "setup-soldr-dylint-output-v2-",
            "setup-soldr:soldr-mini": "soldr-mini-v2-",
            "setup-soldr:solo-toolchain": "solo-toolchain-v3-",
            "setup-soldr:cook-delta": "cook-delta-v2-",
            "setup-uv": "setup-uv-2-",
        }
        for via, prefix in expected.items():
            with self.subTest(via=via):
                self.assertEqual(prefix, resolve_prefix(via, "irrelevant-family-id"))

    def test_ci_lint_via_uses_the_familys_own_id(self) -> None:
        self.assertEqual("uv-v1-", resolve_prefix("ci-lint", "uv"))
        self.assertEqual("compile-v1-", resolve_prefix("ci-lint", "compile"))

    def test_setup_uv_accepts_the_v6_legacy_generation(self) -> None:
        # zackees/ci.yml#87: building keys uses the current generation only.
        self.assertEqual(("setup-uv-2-", "setup-uv-1-"), resolve_prefixes("setup-uv", "uv"))
        self.assertEqual(("uv-v1-",), resolve_prefixes("ci-lint", "uv"))
        self.assertEqual((), resolve_prefixes("bogus:family", "x"))

    def test_unknown_via_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_prefix("bogus:family", "x"))

    def test_allowed_via_values_matches_every_shape_plus_ci_lint(self) -> None:
        self.assertIn("ci-lint", ALLOWED_VIA_VALUES)
        self.assertIn("setup-soldr:soldr-mini", ALLOWED_VIA_VALUES)
        self.assertIn("setup-soldr:dylint", ALLOWED_VIA_VALUES)
        self.assertIn("setup-uv", ALLOWED_VIA_VALUES)
        self.assertNotIn("bogus:family", ALLOWED_VIA_VALUES)

    def test_retired_prefix_matches_a_live_key_with_the_same_start(self) -> None:
        self.assertTrue("solo-toolchain-v3-linux-x64-glibc-rustc1.95.0".startswith(resolve_retired_prefix("solo-toolchain")))
        self.assertTrue("cook-delta-v2-linux-x64-whatever".startswith(resolve_retired_prefix("cook-delta-v2")))
        self.assertFalse("cook-delta-v3-linux-x64-whatever".startswith(resolve_retired_prefix("cook-delta-v2")))


if __name__ == "__main__":
    unittest.main()


class LiteralPrefixFamilyTest(unittest.TestCase):
    """`[cache.family.<id>].prefix` -- zackees/ci.yml#347.

    `via` maps one value to exactly one literal prefix and is a strict
    allowlist, so a cache no producer in the allowlist owns could not be
    declared at all. Measured 2026-10-05: 203 live
    `sccache/<a>/<b>/<c>/<hash>` entries on zackees/zccache plus its own
    `zccache-Linux-X64-bench-*` -- all permanently `CACHE-001 undeclared`,
    and the janitor treats undeclared as deletable.
    """

    def _toml(self, extra: str) -> CiToml:
        text = (
            pathlib.Path(__file__).resolve().parents[2]
            / "examples" / "rust-pypi-app" / "ci.toml"
        ).read_text(encoding="utf-8").replace(
            "zackees/ci.yml@<40-hex-sha>", "zackees/ci.yml@" + "a" * 40
        ) + extra
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "ci.toml").write_text(text, encoding="utf-8")
        _loaded = load_ci_toml(d)
        ci = _loaded.ci
        findings = _loaded.findings
        self.assertEqual([f.message for f in findings], [], extra)
        return ci

    def test_a_sharded_path_cache_is_classifiable(self) -> None:
        ci = self._toml('\n[cache.family.sccache]\nprefix = "sccache/"\nmax = "25MB"\n')
        entries = [
            CacheEntry(
                id=1, ref="refs/heads/main",
                key="sccache/7/6/9/7691df0f6f0a1b347e8bfff30b5d5b49",
                version="v", size_in_bytes=1024,
                created_at="2026-10-01T00:00:00Z", last_accessed_at="2026-10-01T00:00:00Z",
            )
        ]
        self.assertEqual(classify(ci, entries)[0].family_id, "sccache")

    def test_a_repository_owned_prefix_is_classifiable(self) -> None:
        ci = self._toml(
            '\n[cache.family.own]\nprefix = "zccache-Linux-X64-bench-"\nmax = "300MB"\n'
        )
        entries = [
            CacheEntry(
                id=1, ref="refs/heads/main",
                key="zccache-Linux-X64-bench-abc123", version="v", size_in_bytes=1024,
                created_at="2026-10-01T00:00:00Z", last_accessed_at="2026-10-01T00:00:00Z",
            )
        ]
        self.assertEqual(classify(ci, entries)[0].family_id, "own")

    def test_prefix_alone_is_enough(self) -> None:
        """`via` is optional when a literal prefix is given -- that is the
        whole point."""

        ci = self._toml('\n[cache.family.only]\nprefix = "only-"\nmax = "5MB"\n')
        self.assertEqual(ci.cache.family["only"].via, "")

    def test_a_family_needing_both_via_and_prefix_is_rejected(self) -> None:
        text = (
            pathlib.Path(__file__).resolve().parents[2]
            / "examples" / "rust-pypi-app" / "ci.toml"
        ).read_text(encoding="utf-8").replace(
            "zackees/ci.yml@<40-hex-sha>", "zackees/ci.yml@" + "a" * 40
        ) + '\n[cache.family.both]\nvia = "setup-soldr:cook"\nprefix = "x-"\nmax = "5MB"\n'
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "ci.toml").write_text(text, encoding="utf-8")
        findings = load_ci_toml(d).findings
        self.assertTrue(any("both 'via' and 'prefix'" in f.message for f in findings))

    def test_a_family_claiming_neither_is_rejected(self) -> None:
        text = (
            pathlib.Path(__file__).resolve().parents[2]
            / "examples" / "rust-pypi-app" / "ci.toml"
        ).read_text(encoding="utf-8").replace(
            "zackees/ci.yml@<40-hex-sha>", "zackees/ci.yml@" + "a" * 40
        ) + '\n[cache.family.orphan]\nmax = "5MB"\n'
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "ci.toml").write_text(text, encoding="utf-8")
        findings = load_ci_toml(d).findings
        self.assertTrue(any("neither 'via' nor 'prefix'" in f.message for f in findings))
