"""Group 8: packaging (PKG-003, PKG-004, PKG-005, PKG-007).

Soldr is the only PEP 517 backend, the wheel bundles the native CLI as the
`PATH` command, and a missing native extension must fail loudly. PKG-007
(zackees/ci.yml#19) is the release-Linux-wheel glibc-2.17 floor: enforced by
the build tool (soldr's catalogue toolchain), verified here statically
against the workflow that drives a release build.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from ci_lint.finding import Finding
from ci_lint.repo_files import list_repo_files
from ci_lint.rules.pkg_abi3 import check_pkg_008
from ci_lint.schema import CliBinary, CiToml
from ci_lint.workflow_scan import as_dict, jobs_of, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus

SOLDR_PIN_RE = re.compile(r"^soldr\s*(==|>=|~=|>)")

# PKG-007 detection: a job step's `run:` text that builds a release wheel
# for a `*-unknown-linux-gnu` target -- the case zackees/soldr#3432 /
# zackees/ci.yml#19 covers. A bare `--target` + `--release` pair is enough
# to identify "this job builds a Linux release wheel", regardless of
# whether it goes through soldr, a wrapper script (`ci/release.py build`),
# or (the violation this rule exists to catch) a bypass like maturin+zig.
_LINUX_GNU_TARGET_RE = re.compile(r"-unknown-linux-gnu\b")
_RELEASE_FLAG_RE = re.compile(r"--release\b")
_SOLDR_WHEEL_RE = re.compile(r"\bsoldr\s+wheel\b|\brelease\.py\s+build\b", re.IGNORECASE)
_BARE_MATURIN_ZIG_RE = re.compile(r"\bmaturin\b[^\n]*--zig\b|--zig\b[^\n]*\bmaturin\b", re.IGNORECASE)
_GLIBC_VERIFY_RE = re.compile(r"glibc[-_ ]?check|verify_wheel_glibc|--max-glibc", re.IGNORECASE)
_HOST_GLIBC_RE = re.compile(r"--host-glibc\b")
_ARM_RUNNER_RE = re.compile(r"arm", re.IGNORECASE)


def check_pkg_007(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK or wf.document is None:
            continue
        doc = as_dict(wf.document)
        for job_id, job in jobs_of(doc).items():
            run_texts = [
                s.get("run") for s in steps_of(job) if isinstance(s.get("run"), str)
            ]
            joined = "\n".join(t for t in run_texts if isinstance(t, str))
            if not (_LINUX_GNU_TARGET_RE.search(joined) and _RELEASE_FLAG_RE.search(joined)):
                continue  # not a release build of a *-unknown-linux-gnu wheel
            job_loc = f"{wf.path}#jobs.{job_id}"
            runs_on = job.get("runs-on")
            runs_on_str = runs_on if isinstance(runs_on, str) else str(runs_on)

            if _BARE_MATURIN_ZIG_RE.search(joined) and not _SOLDR_WHEEL_RE.search(joined):
                findings.append(
                    Finding(
                        rule="PKG-007",
                        path=job_loc,
                        message=f"jobs.{job_id} builds a release *-unknown-linux-gnu wheel with "
                        "maturin's --zig cross path instead of 'soldr wheel --release'",
                        fix="build the release Linux wheel with 'soldr wheel --release --target "
                        "<triple>' (or a wrapper that calls it); soldr's catalogue toolchain is "
                        "what enforces the glibc-2.17 floor -- maturin --zig is not the blessed path "
                        "(zackees/ci.yml#17/#19)",
                    )
                )

            if _ARM_RUNNER_RE.search(runs_on_str):
                findings.append(
                    Finding(
                        rule="PKG-007",
                        path=job_loc,
                        message=f"jobs.{job_id} builds a release *-unknown-linux-gnu wheel on "
                        f"runs-on: {runs_on_str!r} (an arm64 runner); no catalogue glibc-2.17 "
                        "bundle is hosted on arm64 today (soldr#2874)",
                        fix="build Linux release wheels on an x86_64 runner (e.g. ubuntu-24.04); "
                        "cross-build the aarch64 wheel from there instead of hosting the build on "
                        "an arm64 runner",
                    )
                )

            if not _GLIBC_VERIFY_RE.search(joined):
                findings.append(
                    Finding(
                        rule="PKG-007",
                        path=job_loc,
                        message=f"jobs.{job_id} builds a release *-unknown-linux-gnu wheel but no "
                        "step verifies its glibc symbol floor against the tag",
                        fix="add a glibc verification step right after the build (e.g. "
                        "'python3 ci/release.py glibc-check --wheel <wheel> --max-glibc 2.17', "
                        "wrapping soldr's verify_wheel_glibc.py) that fails the job on any mismatch "
                        "or any floor above policy",
                    )
                )

            if _HOST_GLIBC_RE.search(joined):
                findings.append(
                    Finding(
                        rule="PKG-007",
                        path=job_loc,
                        message=f"jobs.{job_id} passes --host-glibc, overriding the catalogue "
                        "glibc-2.17 toolchain for a release wheel",
                        fix="remove --host-glibc from a release build, or record a "
                        f'[[exceptions]] entry (rule = "PKG-007", path = "{job_loc}") in ci.toml '
                        "with an issue link, owner and review date",
                    )
                )
    return findings


def _load_toml(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def check_pkg_004(repo_root: Path) -> list[Finding]:  # noqa: C901
    findings: list[Finding] = []
    pyproject = _load_toml(repo_root / "pyproject.toml")
    if pyproject is None:
        return [
            Finding(
                rule="PKG-004",
                path="pyproject.toml",
                message="no pyproject.toml at repo root, or it is not valid TOML",
                fix='add pyproject.toml with [build-system] build-backend = "soldr" and requires = '
                '["soldr==<version>"]',
            )
        ]
    build_system = pyproject.get("build-system")
    build_system = build_system if isinstance(build_system, dict) else {}
    backend = build_system.get("build-backend")
    if backend != "soldr":
        findings.append(
            Finding(
                rule="PKG-004",
                path="pyproject.toml",
                message=f"[build-system].build-backend = {backend!r}, expected \"soldr\"",
                fix='set [build-system] build-backend = "soldr" in pyproject.toml',
            )
        )
    requires = build_system.get("requires")
    requires = requires if isinstance(requires, list) else []
    if not any(isinstance(r, str) and SOLDR_PIN_RE.match(r) for r in requires):
        findings.append(
            Finding(
                rule="PKG-004",
                path="pyproject.toml",
                message="[build-system].requires has no 'soldr' version requirement "
                "('soldr>=<floor>' by default, or an exact 'soldr==<version>' pin per "
                "zackees/ci.yml#18's RUST-013)",
                fix='add "soldr>=<floor>" (or, only with a recorded [[exceptions]] entry, '
                '"soldr==<version>") to [build-system].requires in pyproject.toml',
            )
        )
    if any(isinstance(r, str) and "maturin" in r.lower() for r in requires):
        findings.append(
            Finding(
                rule="PKG-004",
                path="pyproject.toml",
                message="[build-system].requires includes maturin directly",
                fix="remove maturin from [build-system].requires; soldr pulls its own pinned maturin",
            )
        )
    dep_groups = pyproject.get("dependency-groups")
    if isinstance(dep_groups, dict):
        for group_name, entries in dep_groups.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                name = entry if isinstance(entry, str) else (
                    entry.get("include-group") if isinstance(entry, dict) else None
                )
                if isinstance(name, str) and "maturin" in name.lower():
                    findings.append(
                        Finding(
                            rule="PKG-004",
                            path="pyproject.toml",
                            message=f"[dependency-groups].{group_name} includes maturin",
                            fix=f"remove maturin from [dependency-groups].{group_name}; soldr pulls "
                            "its own pinned maturin",
                        )
                    )
    uv_lock = _load_toml(repo_root / "uv.lock")
    if uv_lock is not None:
        packages = uv_lock.get("package")
        for pkg in packages if isinstance(packages, list) else []:
            if not isinstance(pkg, dict):
                continue
            pkg_name = pkg.get("name")
            if pkg_name == "soldr":
                continue
            deps = pkg.get("dependencies")
            dep_names = {
                d.get("name")
                for d in deps
                if isinstance(d, dict) and isinstance(d.get("name"), str)
            } if isinstance(deps, list) else set()
            if "maturin" in dep_names:
                findings.append(
                    Finding(
                        rule="PKG-004",
                        path="uv.lock",
                        message=f"uv.lock: package '{pkg_name}' depends on maturin directly",
                        fix=f"remove the direct maturin dependency from '{pkg_name}'; only soldr may "
                        "depend on maturin",
                    )
                )
    return findings


def _bundle_bin_matches(entry: object, cli: CliBinary) -> bool:
    """`[tool.soldr.pep517].bundle-bins` accepts two shapes (round-2A brief,
    defect 3): a plain string naming the crate (`"template-cli"`), or a
    table `{ bin = "<cli command name>", package = "<crate>" }` -- the
    shape soldr's own docs use and the template repo ships
    (`[{ bin = "template-cli", package = "template-cli" }]`). A table entry
    matches on `bin` == `[python].cli.name`; if it also carries `package`,
    that must equal `[python].cli.crate`."""

    if isinstance(entry, str):
        return entry == cli.crate
    if isinstance(entry, dict):
        if entry.get("bin") != cli.name:
            return False
        package = entry.get("package")
        return package is None or package == cli.crate
    return False


def check_pkg_003(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    if ci.python is None:
        return findings
    pyproject = _load_toml(repo_root / "pyproject.toml")
    if pyproject is None:
        return findings  # already reported by PKG-004
    project = pyproject.get("project")
    project = project if isinstance(project, dict) else {}
    for key in ("scripts", "gui-scripts"):
        scripts = project.get(key)
        if isinstance(scripts, dict) and ci.python.cli.name in scripts:
            findings.append(
                Finding(
                    rule="PKG-003",
                    path="pyproject.toml",
                    message=f"[project.{key}] defines '{ci.python.cli.name}', shadowing the native "
                    "CLI the wheel bundles",
                    fix=f"remove '{ci.python.cli.name}' from [project.{key}]; the command on PATH "
                    "must be the bundled Rust binary, not a Python shim",
                )
            )
    tool = pyproject.get("tool")
    soldr_tool = tool.get("soldr") if isinstance(tool, dict) else None
    pep517 = soldr_tool.get("pep517") if isinstance(soldr_tool, dict) else None
    bundle_bins = pep517.get("bundle-bins") if isinstance(pep517, dict) else None
    bundle_bins = bundle_bins if isinstance(bundle_bins, list) else []
    if not any(_bundle_bin_matches(entry, ci.python.cli) for entry in bundle_bins):
        findings.append(
            Finding(
                rule="PKG-003",
                path="pyproject.toml",
                message=f"[tool.soldr.pep517].bundle-bins {bundle_bins} does not include "
                f"'{ci.python.cli.crate}' (as a plain string, or as a table with "
                f"bin = \"{ci.python.cli.name}\")",
                fix=f'add "{ci.python.cli.crate}" (or {{ bin = "{ci.python.cli.name}", package = '
                f'"{ci.python.cli.crate}" }}) to [tool.soldr.pep517].bundle-bins in pyproject.toml',
            )
        )
    return findings


def _catches_import_error(node: ast.Try) -> bool:
    for handler in node.handlers:
        if handler.type is None:
            return True
        if isinstance(handler.type, ast.Name) and handler.type.id == "ImportError":
            return True
        if isinstance(handler.type, ast.Tuple) and any(
            isinstance(e, ast.Name) and e.id == "ImportError" for e in handler.type.elts
        ):
            return True
    return False


def _imports_native(node: ast.Try) -> bool:
    for stmt in node.body:
        if isinstance(stmt, ast.ImportFrom):
            if stmt.module and "_native" in stmt.module:
                return True
            # `from . import _native` / `from .pkg import _native as x`: the
            # module lives in `names`, not `module`, when it's a bare name.
            if any("_native" in alias.name for alias in stmt.names):
                return True
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if "_native" in alias.name:
                    return True
    return False


def check_pkg_005(repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    # git-tracked (or not-yet-ignored) files under src/ only (ci_lint.repo_files):
    # round-1A's `src_dir.rglob("*.py")` also walked gitignored build output
    # such as a stray `src/**/.venv/`, `src/**/__pycache__/` or vendored tree.
    py_files = [
        repo_root / rel for rel in list_repo_files(repo_root) if rel.startswith("src/") and rel.endswith(".py")
    ]
    for f in py_files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        rel = f.relative_to(repo_root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and _imports_native(node) and _catches_import_error(node):
                findings.append(
                    Finding(
                        rule="PKG-005",
                        path=rel,
                        line=node.lineno,
                        message="try/except ImportError around the '._native' import silently masks "
                        "a missing native extension",
                        fix="remove the try/except ImportError around the ._native import; import it "
                        "unconditionally so a missing native module fails loudly",
                    )
                )
    return findings


_WHEEL_SMOKE_RE = re.compile(r"wheel", re.IGNORECASE)
_NPM_PACK_RE = re.compile(r"\bnpm\s+pack\b")
_NPM_INSTALL_RE = re.compile(r"\bnpm\s+(ci|install)\b")


def check_pkg_001(ci: CiToml) -> list[Finding]:
    """A PyPI app's quick gate must build+install a Linux wheel and run a
    clean-venv smoke test, not defer that to release only (issue #1/#2).
    Static evidence: the flow that runs on every PR push ('pr', or the
    first flow if none is named 'pr') must select a `required = true`
    suite whose `run` command mentions wheel packaging (the canonical
    example's `suites.smoke = { run = "ci/wheel.py smoke", required =
    true }`) for at least one linux-group platform."""

    if ci.python is None:
        return []
    flow = ci.flows.get("pr") or next(iter(ci.flows.values()), None)
    if flow is None:
        return [
            Finding(
                rule="PKG-001",
                path="ci.toml#flow",
                message="no '[flow.pr]' (or any flow) declared to check for a wheel install smoke",
                fix='declare "[flow.pr]" with suites including one required suite whose run '
                'command builds+installs a wheel and smoke-tests it (e.g. "ci/wheel.py smoke")',
            )
        ]
    flow_suite_ids: set[str] = set()
    seen: set[str] = set()
    cur: object = flow
    while cur is not None and cur.id not in seen:  # walk `extends` chain
        seen.add(cur.id)
        suites = cur.suites
        if isinstance(suites, tuple):
            flow_suite_ids.update(suites)
        elif suites == "all":
            flow_suite_ids.update(ci.suites)
        cur = ci.flows.get(cur.extends) if cur.extends else None

    smoke_ok = any(
        sid in flow_suite_ids and ci.suites[sid].required and _WHEEL_SMOKE_RE.search(ci.suites[sid].run)
        for sid in ci.suites
    )
    if smoke_ok:
        return []
    return [
        Finding(
            rule="PKG-001",
            path=f"ci.toml#flow.{flow.id}",
            message=f"flow '{flow.id}' has no required suite whose run command performs a wheel "
            "install smoke test",
            fix='add a suite (e.g. suites.smoke = { run = "ci/wheel.py smoke", required = true }) '
            f"that builds the wheel, installs it into a clean venv, and smoke-tests it, and select "
            f"it in flow.{flow.id}.suites",
        )
    ]


def check_pkg_002(repo_root: Path) -> list[Finding]:
    """An npm app's quick gate must `npm pack` the tarball and install+
    smoke-test it (issue #1/#2). Applies only when package.json exists at
    the repo root -- ci_lint has no npm packaging profile yet, so this is a
    plain grep over every workflow's run: text for the repo root, not a
    ci.toml-schema-driven check like PKG-001."""

    if not (repo_root / "package.json").is_file():
        return []
    from ci_lint.workflow_scan import as_dict, jobs_of, load_workflows, steps_of  # local: avoid cycle
    from ci_lint.yaml_io import LoadStatus

    joined_parts: list[str] = []
    for wf in load_workflows(repo_root):
        if wf.status != LoadStatus.OK or wf.document is None:
            continue
        doc = as_dict(wf.document)
        for job in jobs_of(doc).values():
            for step in steps_of(job):
                run = step.get("run")
                if isinstance(run, str):
                    joined_parts.append(run)
    joined = "\n".join(joined_parts)
    if _NPM_PACK_RE.search(joined) and _NPM_INSTALL_RE.search(joined):
        return []
    return [
        Finding(
            rule="PKG-002",
            path="package.json",
            message="package.json exists, but no workflow step runs both 'npm pack' and "
            "'npm ci'/'npm install' (a clean tarball install smoke)",
            fix="add a quick-gate step that runs 'npm pack', installs the produced tarball into a "
            "clean directory ('npm ci'/'npm install <tarball>'), and smoke-tests the installed "
            "package",
        )
    ]


# zackees/ci.yml#90: PKG-004 (soldr is the only PEP 517 backend) is a
# requirement of the profiles that ship a native wheel -- today only
# `rust-pypi-app` (docs/ci-toml.md: the only profile with enforced
# requirements). A `rust-library` repo may carry an unrelated pure-Python
# package (kernal-api: a hatchling `python/kernal_api`) that builds no
# native wheel, so its pyproject backend is not this rule's business.
# zackees/ci.yml#106: PKG-003 (the native CLI must not be shadowed by a
# Python script, and must be listed in [tool.soldr.pep517].bundle-bins) is
# the same kind of wheel-shipping concern, so it is scoped identically --
# a `rust-library` repo's [python].cli (if any) is not a bundled wheel CLI
# and must not be checked against a wheel it never ships.
NATIVE_WHEEL_PROFILES: frozenset[str] = frozenset({"rust-pypi-app"})


def check_group8(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_pkg_001(ci))
    findings.extend(check_pkg_002(repo_root))
    if ci.profile in NATIVE_WHEEL_PROFILES:
        findings.extend(check_pkg_004(repo_root))
        findings.extend(check_pkg_003(ci, repo_root))
        findings.extend(check_pkg_008(repo_root))
    findings.extend(check_pkg_005(repo_root))
    findings.extend(check_pkg_007(repo_root))
    return findings
