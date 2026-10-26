"""`ci-lint cache key` -- ci_lint.cache.keys (round-4A brief, deliverable 2)."""

from __future__ import annotations

import hashlib
import unittest

from ci_lint.cache.keys import CacheKeyError, build_delta_key, build_family_key
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import FIXTURES

REPO = FIXTURES / "runtime" / "cache" / "repo"


class BuildFamilyKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        _loaded = load_ci_toml(REPO)
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.ci = ci

    def test_uv_family_key_has_os_python_and_lockfile_hash_components(self) -> None:
        fk = build_family_key(self.ci, "uv", platform_id="linux-x64", repo_root=REPO)
        self.assertEqual("uv-v1-", fk.prefix)
        self.assertEqual(3, len(fk.components))
        self.assertEqual("linux-x64", fk.components[0])
        self.assertEqual("3.11", fk.components[1])  # the floor (pythons[0])
        expected_hash = hashlib.sha256((REPO / "uv.lock").read_bytes()).hexdigest()[:16]
        self.assertEqual(expected_hash, fk.components[2])
        self.assertEqual(f"uv-v1-linux-x64-3.11-{expected_hash}", fk.key)

    def test_ccache_family_key_is_family_owned_with_components(self) -> None:
        # zackees/ci.yml#393: ccache/zccache write no keys of their own, so
        # ci-lint builds `<family>-v1-<components>` exactly as for via=ci-lint.
        from ci_lint.cache.families import resolve_prefix
        from ci_lint.schema import load_ci_toml
        from ci_lint.tests.helpers import FIXTURES
        _loaded = load_ci_toml(FIXTURES / "CACHE-004" / "green-cpp")
        ci = _loaded.ci
        findings = _loaded.findings
        assert ci is not None, findings
        self.assertEqual("zccache-v1-", resolve_prefix("zccache", "zccache"))
        fk = build_family_key(ci, "ccache", platform_id="linux-x64", repo_root=FIXTURES / "CACHE-004" / "green-cpp")
        self.assertTrue(fk.key.startswith("ccache-v1-"), fk.key)
        self.assertEqual(1, len(fk.components))

    def test_key_is_deterministic(self) -> None:
        a = build_family_key(self.ci, "uv", platform_id="linux-x64", repo_root=REPO)
        b = build_family_key(self.ci, "uv", platform_id="linux-x64", repo_root=REPO)
        self.assertEqual(a.key, b.key)

    def test_os_component_without_platform_is_an_error(self) -> None:
        with self.assertRaises(CacheKeyError):
            build_family_key(self.ci, "uv", platform_id=None, repo_root=REPO)

    def test_unknown_platform_is_an_error(self) -> None:
        with self.assertRaises(CacheKeyError):
            build_family_key(self.ci, "uv", platform_id="does-not-exist", repo_root=REPO)

    def test_undeclared_family_is_an_error(self) -> None:
        with self.assertRaises(CacheKeyError):
            build_family_key(self.ci, "does-not-exist", platform_id="linux-x64", repo_root=REPO)

    def test_missing_lockfile_component_file_is_an_error(self) -> None:
        with self.assertRaises(CacheKeyError):
            build_family_key(self.ci, "uv", platform_id="linux-x64", repo_root=FIXTURES)  # no uv.lock here

    def test_setup_soldr_family_still_resolves_its_prefix(self) -> None:
        # `compile` has no declared `key` components (setup-soldr builds its
        # real key); build_family_key still resolves the correct prefix so
        # e.g. `cache heal` can be pointed at the right family shape.
        fk = build_family_key(self.ci, "compile", platform_id=None, repo_root=REPO)
        self.assertEqual("setup-soldr-buildcache-v2-", fk.prefix)
        self.assertEqual((), fk.components)
        # no declared `key` components: .key drops the dangling trailing hyphen.
        self.assertEqual("setup-soldr-buildcache-v2", fk.key)

    def test_setup_uv_family_ignores_a_declared_key_array(self) -> None:
        # round-4A amendment: astral-sh/setup-uv builds its OWN real key
        # (arch/platform/os-version/python/dependency-hash, none of which
        # is ci-lint's os/python/lockfile-hash convention or ordering) --
        # a [cache.family.<id>].key array on a non-"ci-lint" family must
        # never be appended onto its prefix, or this would fabricate a key
        # shape that action never actually produces.
        fk = build_family_key(self.ci, "setup-uv-cache", platform_id="linux-x64", repo_root=REPO)
        self.assertEqual("setup-uv-2-", fk.prefix)
        self.assertEqual((), fk.components)
        self.assertEqual("setup-uv-2", fk.key)


class BuildDeltaKeyTest(unittest.TestCase):
    def test_matches_the_design_shape(self) -> None:
        key = build_delta_key(family_id="compile", platform_id="linux-x64", pr=42, base_key="some-base-key")
        self.assertTrue(key.startswith("delta-v1-pr-42-compile-linux-x64-b"))
        base8 = key.rsplit("-b", 1)[1]
        self.assertEqual(8, len(base8))
        self.assertEqual(hashlib.sha256(b"some-base-key").hexdigest()[:8], base8)

    def test_self_heals_when_the_base_key_string_changes(self) -> None:
        k1 = build_delta_key(family_id="compile", platform_id="linux-x64", pr=1, base_key="base-A")
        k2 = build_delta_key(family_id="compile", platform_id="linux-x64", pr=1, base_key="base-B")
        self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
