"""CACHE-007 (round M2-20, zackees/ci.yml#44 part A): a cache payload must
never contain a linked test binary, nextest archive, incremental/ output,
or a whole target/ directory. Static half scans declared `path:` inputs on
a sanctioned `actions/cache*` step; runtime half classifies a JSON path
manifest."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ci_lint.cache.payload_classify import classify_payload_paths
from ci_lint.rules.cache_payload import (
    PayloadManifestError,
    check_cache_007_manifest,
    check_cache_007_static,
    load_manifest_paths,
)
from ci_lint.schema import load_ci_toml
from ci_lint.tests.helpers import fixture, requires_yaml_tooling


class CachePayloadClassifyTest(unittest.TestCase):
    def test_classifies_forbidden_classes(self) -> None:
        violations = classify_payload_paths(
            ["target/", "target/x86_64-unknown-linux-gnu/incremental/foo", "~/.cargo/registry"]
        )
        classes = {v.content_class for v in violations}
        self.assertIn("whole target/ directory", classes)
        self.assertIn("incremental/", classes)
        self.assertEqual(2, len(violations))

    def test_linked_products_include_cross_target_profile_paths(self) -> None:
        paths = [
            "target/debug/deps/suite-0123456789abcdef",
            "target/aarch64-unknown-linux-gnu/debug/deps/suite-0123456789abcdef",
            "target/x86_64-pc-windows-msvc/release/deps/suite-0123456789abcdef.exe",
            r"C:\repo\target\aarch64-pc-windows-msvc\debug\deps\suite-0123456789abcdef.exe",
        ]
        violations = classify_payload_paths(paths)
        self.assertEqual(paths, [v.path for v in violations])
        self.assertTrue(all(v.content_class == "linked test binary" for v in violations))

    def test_cross_target_compiler_inputs_remain_allowed(self) -> None:
        paths = [
            "target/aarch64-unknown-linux-gnu/debug/deps/libsuite-0123456789abcdef.rmeta",
            "target/x86_64-pc-windows-msvc/release/deps/libsuite-0123456789abcdef.rlib",
            "target/aarch64-unknown-linux-gnu/debug/deps/suite-0123456789abcdef.d",
        ]
        self.assertEqual([], classify_payload_paths(paths))


class CachePayload007StaticTest(unittest.TestCase):
    @requires_yaml_tooling
    def test_red_flags_target_and_incremental_paths(self) -> None:
        repo = fixture("CACHE-007", "red")
        ci = load_ci_toml(repo).ci
        findings = check_cache_007_static(ci, repo)
        rules = [f.rule for f in findings]
        self.assertEqual(["CACHE-007"] * 2, rules)

    @requires_yaml_tooling
    def test_green_registry_path_is_clean(self) -> None:
        repo = fixture("CACHE-007", "green")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], check_cache_007_static(ci, repo))


class CachePayload007CmakeTest(unittest.TestCase):
    """zackees/ci.yml#393: a CMake build tree is never a cache payload; the
    ccache/zccache object store is."""

    def test_classifies_cmake_build_trees(self) -> None:
        tree = ["build", "build/", "build-linux/", "cmake-build-debug", "out/build/linux-ci",
                "build/CMakeFiles/obs.dir/a.c.o", "build/CMakeCache.txt"]
        violations = classify_payload_paths(tree)
        self.assertEqual(tree, [v.path for v in violations])
        self.assertTrue(all(v.content_class == "CMake build tree" for v in violations))

    def test_object_caches_and_lookalikes_are_allowed(self) -> None:
        ok = ["~/.ccache", "build/.ccache", ".zccache", "~/.cache/zccache", "src/build.rs", "deps/buildspec.json"]
        self.assertEqual([], classify_payload_paths(ok))

    @requires_yaml_tooling
    def test_static_red_and_green(self) -> None:
        repo = fixture("CACHE-007", "red-cmake")
        ci = load_ci_toml(repo).ci
        findings = check_cache_007_static(ci, repo)
        self.assertEqual(["CACHE-007"] * 2, [f.rule for f in findings])
        self.assertTrue(all("CMake build tree" in f.message for f in findings))
        repo = fixture("CACHE-007", "green-cmake")
        ci = load_ci_toml(repo).ci
        self.assertEqual([], check_cache_007_static(ci, repo))


class CachePayload007ManifestTest(unittest.TestCase):
    def test_manifest_flags_nextest_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "manifest.json"
            manifest.write_text(json.dumps(["target/nextest-archive.tar.zst", "src/lib.rs"]), encoding="utf-8")
            paths = load_manifest_paths(manifest)
            findings = check_cache_007_manifest(paths, manifest_path=str(manifest))
            self.assertEqual(["CACHE-007"], [f.rule for f in findings])

    def test_manifest_clean_is_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "manifest.json"
            manifest.write_text(json.dumps(["Cargo.lock", "uv.lock"]), encoding="utf-8")
            paths = load_manifest_paths(manifest)
            self.assertEqual([], check_cache_007_manifest(paths, manifest_path=str(manifest)))

    def test_bad_manifest_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "manifest.json"
            manifest.write_text("not json", encoding="utf-8")
            with self.assertRaises(PayloadManifestError):
                load_manifest_paths(manifest)


if __name__ == "__main__":
    unittest.main()
