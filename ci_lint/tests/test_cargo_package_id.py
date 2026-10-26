"""Regression tests for cargo package-ID spec parsing (template-python-rust-cmd#17).

Cargo omits the package name from a package-ID spec when it equals the final
path component of the source URL, so `path+file:///ws/crates/foo#0.1.0` is
package `foo` version `0.1.0`. Round 2A's parser returned `("0.1.0", "")`
for that shape, which misidentified every path dependency in `units` and
`tests size`.
"""

import unittest

from ci_lint.cargo_messages import PackageId, _parse_package_id


class ParsePackageIdTest(unittest.TestCase):
    def test_path_dependency_with_name_omitted(self) -> None:
        self.assertEqual(
            _parse_package_id("path+file:///ws/crates/private/template-core#0.1.0"),
            PackageId("template-core", "0.1.0"),
        )

    def test_path_dependency_with_explicit_name(self) -> None:
        self.assertEqual(
            _parse_package_id("path+file:///ws/crates/core#template-core@0.1.0"),
            PackageId("template-core", "0.1.0"),
        )

    def test_registry_dependency(self) -> None:
        self.assertEqual(
            _parse_package_id("registry+https://github.com/rust-lang/crates.io-index#serde@1.0.228"),
            PackageId("serde", "1.0.228"),
        )

    def test_legacy_space_separated_form(self) -> None:
        self.assertEqual(
            _parse_package_id("template-core 0.1.0 (path+file:///ws/crates/private/template-core)"),
            PackageId("template-core", "0.1.0"),
        )

    def test_trailing_slash_in_path(self) -> None:
        self.assertEqual(_parse_package_id("path+file:///ws/foo/#1.2.3"), PackageId("foo", "1.2.3"))

    def test_non_string_is_empty(self) -> None:
        self.assertEqual(_parse_package_id(None), PackageId("", ""))


if __name__ == "__main__":
    unittest.main()
