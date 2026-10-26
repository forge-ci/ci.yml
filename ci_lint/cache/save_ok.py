"""`ci-lint cache save-ok <family>`: issue #6 §6's "when we do NOT save"
table, rule ID CACHE-008, evaluated in order 1-11 (11: a CMake build tree,
zackees/ci.yml#393) (round-4A brief,
deliverable 3). Prints the first rule that blocks the save, or "save:
yes"; a skipped save is always explainable by its rule number.
"""

from __future__ import annotations

from dataclasses import dataclass

from ci_lint.cargo_messages import JsonValue
from ci_lint.cache.payload_classify import is_cmake_build_tree
from ci_lint.rules.cache_static import parse_size
from ci_lint.schema import CiToml

# ci.toml (schema 3) has no declared "-save-min-compiles" threshold -- that
# is a setup-soldr runtime INPUT (issue #6 §6's "Already provided by
# setup-soldr" list: "*-save-min-compiles skips re-uploading a restored
# cache"), not a schema-3 field. Round-4A decision (documented in
# docs/ci-toml.md): `save-ok` applies a fixed default of 1 new compile
# unit -- "fewer than the minimum new compile units" means "zero".
MIN_NEW_UNITS_DEFAULT = 1


@dataclass(frozen=True)
class SaveOkRequest:
    family: str
    flow: str
    event: str
    pr: int | None = None
    fork: bool = False
    act: bool = False
    build_failed: bool = False
    exact_hit: bool = False
    new_units: int | None = None
    payload_bytes: int | None = None
    base_key: str | None = None
    current_base_key: str | None = None
    lockfile_changed: bool = False
    tags: tuple[str, ...] = ()
    rerun_saved: bool = False
    # The paths the save would archive (`--path`, repeatable). Only rule 11
    # reads them; empty means "not supplied", never "an empty payload".
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SaveOkResult:
    save: bool
    rule: int | None
    reason: str

    def render(self) -> str:
        if self.save:
            return "save: yes"
        return f"save: no (rule {self.rule}: {self.reason})"

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {"save": self.save, "rule": self.rule, "reason": self.reason}


class SaveOkInputError(ValueError):
    """Bad input (unknown family, unparseable size) -- the CLI layer turns
    this into exit 2 (round-4A brief: "exit 2 on bad input")."""


def evaluate_save_ok(ci: CiToml, req: SaveOkRequest) -> SaveOkResult:  # noqa: C901 -- one rule per branch, by design
    fam = ci.cache.family.get(req.family)
    if fam is None and req.family not in ci.cache.retired:
        raise SaveOkInputError(
            f"'{req.family}' is neither declared in [cache.family] nor listed in [cache].retired"
        )

    is_pr_scoped = req.pr is not None

    # Rule 1: the run is from a fork, or runs under act.
    if req.fork or req.act:
        return SaveOkResult(False, 1, "the run is from a fork, or under act (local results never upload)")

    # Rule 2: a base-layer save attempt on any ref but a declared writer flow.
    # A `--pr N` save is explicitly PR-scoped (a delta, or -- rule 9's
    # escalation -- a PR-scoped base); rules 7-9 govern it instead.
    if not is_pr_scoped and req.flow not in ci.cache.write_on:
        return SaveOkResult(
            False,
            2,
            f"'{req.family}' is a base layer; flow {req.flow!r} is not a declared writer "
            f"([cache].write-on = {sorted(ci.cache.write_on)})",
        )

    # Rule 3: the family is retired, or (bad-input case already raised above) its content is
    # in [cache].never.
    if req.family in ci.cache.retired:
        return SaveOkResult(False, 3, f"family {req.family!r} is retired ([cache].retired)")

    assert fam is not None  # guaranteed once "retired" is ruled out, by the bad-input check above

    # Rule 4: the build step failed or was cancelled. A test failure alone never blocks a save
    # (compile units are still valid; red-test iteration is the common agent loop).
    if req.build_failed:
        return SaveOkResult(False, 4, "the build step failed or was cancelled")

    # Rule 5: the exact key hit, or fewer than the minimum new compile units.
    if req.exact_hit:
        return SaveOkResult(False, 5, "the exact key already hit -- nothing new to store")
    if req.new_units is not None and req.new_units < MIN_NEW_UNITS_DEFAULT:
        return SaveOkResult(
            False,
            5,
            f"{req.new_units} new compile unit(s) is below the minimum ({MIN_NEW_UNITS_DEFAULT})",
        )

    # Rule 6: the payload is below the family's min, or above its max (poison guard + budget).
    if req.payload_bytes is not None:
        min_bytes = parse_size(fam.min) if fam.min else None
        max_bytes = parse_size(fam.max) if fam.max else None
        if min_bytes is not None and req.payload_bytes < min_bytes:
            return SaveOkResult(
                False,
                6,
                f"payload {req.payload_bytes}B is below [cache.family.{req.family}].min ({fam.min})",
            )
        if max_bytes is not None and req.payload_bytes > max_bytes:
            return SaveOkResult(
                False,
                6,
                f"payload {req.payload_bytes}B is above [cache.family.{req.family}].max ({fam.max})",
            )

    # Rule 7: a PR delta whose base isn't main's CURRENT base -- stale the moment it's saved.
    if (
        is_pr_scoped
        and req.base_key is not None
        and req.current_base_key is not None
        and req.base_key != req.current_base_key
    ):
        return SaveOkResult(False, 7, "the PR delta's restored base no longer matches the current base key")

    # Rule 8: the delta exceeds max-per-pr. ("...or the PR budget is still full after trimming
    # closed PRs and evicting by LRU" needs live cache-account state across every open PR, which
    # this pure per-call check does not have -- see ci_lint.cache.audit/ops, which enforce that
    # half live; documented as a round-4A decision in docs/ci-toml.md.)
    if is_pr_scoped and req.payload_bytes is not None and ci.cache.pr.max_per_pr:
        max_per_pr = parse_size(ci.cache.pr.max_per_pr)
        if max_per_pr is not None and req.payload_bytes > max_per_pr:
            return SaveOkResult(
                False,
                8,
                f"delta payload {req.payload_bytes}B exceeds [cache.pr].max-per-pr ({ci.cache.pr.max_per_pr})",
            )

    # Rule 9: the PR changed Cargo.lock or the toolchain, without [ci-cache-save].
    if is_pr_scoped and req.lockfile_changed and "ci-cache-save" not in req.tags:
        return SaveOkResult(
            False,
            9,
            "the PR changed Cargo.lock/the toolchain without [ci-cache-save]; a delta would be a full base",
        )

    # Rule 10: a re-run attempt that already saved this exact key (idempotence).
    if req.rerun_saved:
        return SaveOkResult(False, 10, "this exact key was already saved by a previous attempt this run")

    # Rule 11 (zackees/ci.yml#393, docs/policy-cpp.md): the payload is a CMake build tree.
    # The compiler's object cache (a ccache/zccache directory) is the reusable layer; the build
    # tree is per-run configure/link output bound to absolute paths -- never a cross-run cache.
    tree = [p for p in req.paths if is_cmake_build_tree(p)]
    if tree:
        return SaveOkResult(
            False,
            11,
            f"the payload includes a CMake build tree ({', '.join(tree)}); save the ccache/zccache "
            "object store instead",
        )

    return SaveOkResult(True, None, "no rule 1-11 applies")
