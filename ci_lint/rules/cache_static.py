"""Group 9: static cache rules (CACHE-001, CACHE-002, CACHE-003, CACHE-004).

`actions/cache` (and its `/save`/`/restore` sub-actions) may only appear
inside the one sanctioned wrapper directory `[allow].cache-actions.only-in`
(default `.github/actions/cache`, still SHA-pinned per SEC-004) -- anywhere
else is CACHE-001. Inside that wrapper, a `key:` input must itself be an
expression that reads a prior step's output or a passed-through input
(keys come from `ci_lint cache key`), never a literal -- CACHE-002.
CACHE-002 also still flags a volatile component (`github.sha`, ...) in any
`key:`/`cache-key-suffix:` anywhere. CACHE-003's static half:
`astral-sh/setup-uv`'s `save-cache` input, when `[allow].setup-uv.require`
says it must be `"plan"`-derived, must not be missing or a literal `"true"`
(round-4B; both are exactly the "saves on every PR push" failure mode
documented under "astral-sh/setup-uv's own cache" in docs/ci-toml.md).
Finally, the total declared cache footprint (steady state + one
lockfile-change peak + the PR delta budget) must fit under `[cache].budget`,
which itself may not exceed GitHub's 10GB default repository cap
(CACHE-004). CACHE-014 (M2-17, ci.yml#42) sizes `[cache.pr].max-per-pr`
against two independent signals, each optional so a repository that hasn't
supplied one gets no finding from that half: `[cache.pr].expected-open-prs`
(if set) times `max-per-pr` must not exceed `[cache.pr].budget` (a fleet of
open PRs whose deltas each hit the cap would blow the PR budget even
though every individual PR stayed under `max-per-pr`), and
`[cache.pr].measured-largest-delta` (if set, e.g. copied from a
`ci-lint cache delta manifest` run against a real large PR) must not
exceed `max-per-pr` (a cap sized below an already-observed delta silently
truncates that PR's saved cache every time).
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.resolve import resolve_flow
from ci_lint.schema import CacheFamily, CiToml
from ci_lint.workflow_scan import as_dict, jobs_of, load_composite_actions, load_workflows, steps_of
from ci_lint.yaml_io import LoadStatus, YamlValue
from ci_lint.rules.tools import _iter_uses_with, _is_plan_expr

SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)$", re.IGNORECASE)
SIZE_UNITS: dict[str, int] = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
TEN_GB = 10 * 1024**3
# CACHE-029: a family this large, with nothing bounding how many entries may
# exist at once, is what fills a repository's 10GB cache budget.
UNBOUNDED_FAMILY_BYTES = 256 * 1024**2

VOLATILE_TOKENS: tuple[str, ...] = ("github.sha", "github.run_id", "github.run_number")
KEY_FIELD_NAMES: frozenset[str] = frozenset({"key", "cache-key-suffix"})
CACHE_ACTION_SLUGS_PREFIX = "actions/cache"

# CACHE-010 (issue #7): env vars known to silently disable a cache layer
# while setup-soldr/zccache still reports the layer as configured --
# zackees/clud's `_dylint.yml` set ZCCACHE_DISABLE=1 (arrived incidentally
# in clud#487, a PR about Codex fallback instructions) alongside a declared
# zccache-unit/dylint-output family; the setup step's own
# `cache-policy layers=...` log line never changed, so nothing in a green
# run revealed the contradiction. Kept as an explicit allowlist per the
# issue's own open question ("should the kill-switch list be owned by
# setup-soldr as a machine-readable manifest") -- not yet true, so this
# starts as ci-lint's own reserved list, extend it as more are found.
KILL_SWITCH_ENV_VARS: frozenset[str] = frozenset(
    {"ZCCACHE_DISABLE", "SOLDR_NO_CACHE", "SOLDR_CACHE_DISABLE", "SOLDR_DYLINT_NO_CACHE", "CCACHE_DISABLE"}
)
# `CCACHE_DISABLE` (zackees/ci.yml#393) only contradicts a declared `via =
# "ccache"` family; every other switch contradicts a setup-soldr/zccache one.
_CCACHE_SWITCHES: frozenset[str] = frozenset({"CCACHE_DISABLE"})
_TRUTHY_STRINGS: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def parse_size(text: str) -> int | None:
    m = SIZE_RE.match(text.strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).upper()
    return int(value * SIZE_UNITS[unit])


def _is_cache_action_slug(slug: str) -> bool:
    return slug == "actions/cache" or slug.startswith(f"{CACHE_ACTION_SLUGS_PREFIX}/")


def _loaded_files(repo_root: Path) -> list[tuple[str, bool, dict[str, YamlValue]]]:
    files = [(w.path, False, as_dict(w.document)) for w in load_workflows(repo_root) if w.status == LoadStatus.OK]
    files += [
        (a.path, True, as_dict(a.document)) for a in load_composite_actions(repo_root) if a.status == LoadStatus.OK
    ]
    return files


def check_cache_001(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    only_in = ci.allow.cache_actions.only_in.strip("/")
    for path, is_composite, doc in _loaded_files(repo_root):
        file_dir = Path(path).parent.as_posix()
        for use_step in _iter_uses_with(doc, is_composite=is_composite):
            uses = use_step.uses
            loc = use_step.loc
            _with = use_step.with_
            slug = uses.split("@", 1)[0]
            if not _is_cache_action_slug(slug):
                continue
            if file_dir == only_in:
                continue  # the one sanctioned wrapper (still SHA-pinned per SEC-004)
            findings.append(
                Finding(
                    rule="CACHE-001",
                    path=path,
                    message=f"{loc}: raw '{slug}' is used directly outside the sanctioned wrapper "
                    f"'{only_in}' (found in '{file_dir}')",
                    fix=f"move this call into the composite-action wrapper at {only_in}/action.yml "
                    f"(SHA-pinned per SEC-004) and call that wrapper from {path} instead; caching "
                    "otherwise goes through zackees/setup-soldr's declared families (or the ci-lint "
                    "'uv' family), never a direct actions/cache call",
                )
            )
    return findings


def _check_key_value(value: YamlValue, path: str, loc: str) -> list[Finding]:
    if not isinstance(value, str):
        return []
    out: list[Finding] = []
    for token in VOLATILE_TOKENS:
        if token in value:
            out.append(
                Finding(
                    rule="CACHE-002",
                    path=path,
                    message=f"{loc} contains volatile component '{token}': {value!r}",
                    fix=f"remove '{token}' from {loc}; key on hashFiles(<lockfile>) or a date-based "
                    "rotation instead of a per-run value",
                )
            )
    return out


def _check_wrapper_key_is_dynamic(value: YamlValue, path: str, loc: str) -> list[Finding]:
    """Inside the sanctioned `actions/cache*` wrapper, `key:` must be an
    expression that reads a prior step's output or a passed-through input
    (round-4B: keys are built by `ci_lint cache key`, never hand-written) --
    a literal string, even one with no volatile token, is CACHE-002 here."""

    if not isinstance(value, str):
        return []
    if _is_plan_expr(value) or re.search(r"steps\.[\w-]+\.outputs\.", value):
        return []
    return [
        Finding(
            rule="CACHE-002",
            path=path,
            message=f"{loc} is a literal cache key inside the sanctioned wrapper: {value!r}",
            fix=f"build {loc} from 'ci_lint cache key' (e.g. a prior step's "
            "'${{ steps.<id>.outputs.key }}') or pass it through as '${{ inputs.key }}' -- never "
            "hardcode a literal key inside the wrapper",
        )
    ]


def check_cache_002(ci: CiToml, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    only_in = ci.allow.cache_actions.only_in.strip("/")
    for path, is_composite, doc in _loaded_files(repo_root):
        file_dir = Path(path).parent.as_posix()
        for use_step in _iter_uses_with(doc, is_composite=is_composite):
            uses = use_step.uses
            loc = use_step.loc
            with_ = use_step.with_
            for field_name in KEY_FIELD_NAMES:
                findings.extend(_check_key_value(with_.get(field_name), path, f"{loc}.with.{field_name}"))
            slug = uses.split("@", 1)[0]
            if _is_cache_action_slug(slug) and file_dir == only_in and "key" in with_:
                findings.extend(
                    _check_wrapper_key_is_dynamic(with_.get("key"), path, f"{loc}.with.key")
                )
    for fam_id, fam in ci.cache.family.items():
        if fam.key is None:
            continue
        for component in fam.key:
            findings.extend(
                _check_key_value(component, "ci.toml", f"[cache.family.{fam_id}].key")
            )
    return findings


def _setup_uv_save_cache_violation(actual: YamlValue) -> str | None:
    """`None` if OK; else the reason `astral-sh/setup-uv`'s `save-cache`
    input fails the plan-driven requirement. Mirrors the two failure modes
    docs/ci-toml.md's "astral-sh/setup-uv's own cache" section documents as
    actually saving on every ordinary PR push: a missing input (falls back
    to the action's own `"auto"` default) and a literal `"true"` (forces a
    save regardless of ref). An explicit `"false"`, or any other value,
    is not this rule's concern."""

    if actual is None:
        return "is missing (falls back to the action's 'auto' default, which still saves on every PR push)"
    if isinstance(actual, str) and actual.strip().lower() == "true":
        return f"= {actual!r} is a literal 'true' (saves unconditionally, on every ref including PRs)"
    return None


def check_cache_003_setup_uv(ci: CiToml, repo_root: Path) -> list[Finding]:
    """Round-4B: every `astral-sh/setup-uv` use (no single wrapper location
    -- unlike setup-soldr, this action is meant to be called directly), for
    each `[allow].setup-uv.require` entry whose expected value is the
    special string `"plan"`."""

    findings: list[Finding] = []
    require = ci.allow.setup_uv.require
    if not require:
        return findings
    for path, is_composite, doc in _loaded_files(repo_root):
        for use_step in _iter_uses_with(doc, is_composite=is_composite):
            uses = use_step.uses
            loc = use_step.loc
            with_ = use_step.with_
            if uses.split("@", 1)[0] != "astral-sh/setup-uv":
                continue
            for entry in require.entries:
                key, expected = entry.key, entry.value
                actual = with_.get(key)
                if expected == "plan":
                    reason = _setup_uv_save_cache_violation(actual)
                    if reason is not None:
                        findings.append(
                            Finding(
                                rule="CACHE-003",
                                path=path,
                                message=f"{loc}: astral-sh/setup-uv input '{key}' {reason}",
                                fix=f"set '{key}' to an expression derived from the precheck plan "
                                f"(e.g. '${{{{ needs.precheck.outputs.cache_save }}}}' in the caller, "
                                f"or '${{{{ inputs.{key} }}}}' inside a composite wrapper) at {loc}",
                            )
                        )
                elif actual is None or str(actual).lower() != expected.lower():
                    findings.append(
                        Finding(
                            rule="CACHE-003",
                            path=path,
                            message=f"{loc}: astral-sh/setup-uv input '{key}' = {actual!r}, expected "
                            f"'{expected}'",
                            fix=f'set \'{key}: "{expected}"\' at {loc}',
                        )
                    )
    return findings


def _cardinality(ci: CiToml, per: str | None) -> int:
    """`per`'s cardinality multiplier. `ci_lint.schema` already rejects any
    value outside `CACHE_FAMILY_PER_VALUES` as `CT-002` (round-2A amendment
    2), so `None`/`"none"` are the only ways to reach the `1` fallback
    here legitimately; an invalid value that somehow arrives anyway still
    falls back to `1` rather than crashing -- its CT-002 finding is what
    tells the user it's wrong, not this arithmetic."""

    if per == "platform":
        return len(ci.platforms)
    if per == "cross-platform":
        return max(len(ci.platforms) - 1, 0)
    if per == "os":
        return len({p.group for p in ci.platforms.values()})
    return 1


def cardinality(ci: CiToml, per: str | None, shapes: int = 0) -> int:
    """Public alias of `_cardinality`, for callers outside this module
    (round-4A: `ci_lint.cache.ops.preprune` reuses the exact same `per`
    arithmetic CACHE-004 uses, with live sizes substituted where known --
    one implementation of the cardinality rule, not two).

    A family that declares `shapes` has already said how many entries it
    holds, so its own shape count wins over the `per` axis -- which is the
    whole point of declaring them (zackees/ci.yml#1321)."""

    if shapes > 0:
        return shapes
    return _cardinality(ci, per)


def family_footprint(ci: CiToml, fam: CacheFamily) -> int:
    """The family's steady-state bytes, the number both CACHE-004 and
    `ci-lint cache janitor`'s LRU trim have to agree on.

    With `shapes`, it is the SUM of the per-shape ceilings -- an honest
    bound for a family whose entries differ in size. Without, it is
    `max x cardinality`, which over-counts exactly those families."""

    if fam.shapes:
        return sum(parse_size(sh.max) or 0 for sh in fam.shapes)
    return (parse_size(fam.max) or 0) * cardinality(ci, fam.per)


def check_cache_004(ci: CiToml) -> tuple[list[Finding], str]:  # noqa: C901
    findings: list[Finding] = []
    budget = parse_size(ci.cache.budget)
    if budget is None:
        findings.append(
            Finding(
                rule="CACHE-004",
                path="ci.toml",
                message=f"[cache].budget {ci.cache.budget!r} is not a parseable size (e.g. '9GB')",
                fix="set [cache].budget to a number + unit, e.g. '9GB'",
            )
        )
        return findings, "CACHE-004: could not compute arithmetic -- [cache].budget is unparseable"

    if budget > TEN_GB:
        findings.append(
            Finding(
                rule="CACHE-004",
                path="ci.toml",
                message=f"[cache].budget = {ci.cache.budget} exceeds the 10GB repository cache cap",
                fix="lower [cache].budget to 10GB or less (GitHub's default per-repo cache cap)",
            )
        )

    lines: list[str] = []
    steady_total = 0
    lockfile_total = 0
    unparseable = False
    for fam_id in sorted(ci.cache.family):
        fam = ci.cache.family[fam_id]
        max_bytes = parse_size(fam.max)
        if max_bytes is None:
            findings.append(
                Finding(
                    rule="CACHE-004",
                    path="ci.toml",
                    message=f"[cache.family.{fam_id}].max {fam.max!r} is not a parseable size",
                    fix=f"set [cache.family.{fam_id}].max to a number + unit, e.g. '150MB'",
                )
            )
            unparseable = True
            continue
        if fam.shapes:
            card = len(fam.shapes)
            steady = sum(parse_size(sh.max) or 0 for sh in fam.shapes)
            detail = ", ".join(f"{sh.scope}={sh.max}" for sh in fam.shapes)
            label = f"{len(fam.shapes)} shapes [{detail}]"
        else:
            card = _cardinality(ci, fam.per)
            steady = max_bytes * card
            label = f"{fam.max} x {card} ({fam.per or 'none'})"
        steady_total += steady
        tag = " (lockfile peak counted again)" if fam.lockfile else ""
        lines.append(f"  {fam_id}: {label} = {steady} B{tag}")
        if fam.lockfile:
            lockfile_total += steady

    pr_budget = parse_size(ci.cache.pr.budget) if ci.cache.pr.budget else 0
    if ci.cache.pr.budget and pr_budget is None:
        findings.append(
            Finding(
                rule="CACHE-004",
                path="ci.toml",
                message=f"[cache.pr].budget {ci.cache.pr.budget!r} is not a parseable size",
                fix="set [cache.pr].budget to a number + unit, e.g. '1GB'",
            )
        )
        pr_budget = 0

    writer_flow_ids = [fid for fid, flow in ci.flows.items() if resolve_flow(ci, fid).cache == "write"]
    writer_flows_pre_pruned = [resolve_flow(ci, fid).pre_prune for fid in writer_flow_ids]
    all_pre_pruned = bool(writer_flow_ids) and all(writer_flows_pre_pruned)

    # `pre-prune = true` on every writer flow removes only the
    # lockfile-change-peak term (the moment old+new lockfile-keyed entries
    # briefly coexist is pruned before the write) -- it does NOT waive the
    # rest of the budget proof (round-2A brief, defect 4). Without full
    # pre-prune, the peak term still applies.
    if all_pre_pruned:
        worst = steady_total + pr_budget
        formula = "worst = steady + [cache.pr].budget (every writer flow pre-prunes: lockfile peak waived)"
    else:
        worst = steady_total + lockfile_total + pr_budget
        formula = "worst = steady + lockfile-change peak + [cache.pr].budget (not every writer flow pre-prunes)"

    arithmetic = (
        "CACHE-004 budget arithmetic:\n"
        + "\n".join(lines)
        + f"\n  steady total            = {steady_total} B"
        + f"\n  + lockfile-change peak  = {lockfile_total} B"
        + f"\n  + [cache.pr].budget     = {pr_budget} B"
        + f"\n  formula applied: {formula}"
        + f"\n  = worst case            = {worst} B"
        + f"\n  budget ([cache].budget) = {budget} B"
    )

    if unparseable:
        return findings, arithmetic

    if worst > budget:
        fix = (
            "lower family 'max' sizes or their cardinality ('per'), or raise [cache].budget (up to "
            "10GB)"
        )
        if not all_pre_pruned:
            fix += (
                "; setting 'pre-prune = true' on every writer flow (cache = \"write\") removes the "
                "lockfile-change-peak term, but the remaining steady total + [cache.pr].budget must "
                "still fit"
            )
        findings.append(
            Finding(
                rule="CACHE-004",
                path="ci.toml",
                message=f"cache budget exceeded: worst case {worst} B > budget {budget} B ({formula})",
                fix=fix,
            )
        )

    return findings, arithmetic


def _is_truthy(value: YamlValue) -> bool:
    if not isinstance(value, (str, int, bool)):
        return False
    return str(value).strip().lower() in _TRUTHY_STRINGS


def _iter_env_entries(doc: dict[str, YamlValue]) -> list[tuple[str, YamlValue, str]]:
    """`(name, value, loc)` for every env var set at workflow, job, or step
    level -- a kill-switch is just as effective set at any of the three."""

    out: list[tuple[str, YamlValue, str]] = []
    top_env = doc.get("env")
    if isinstance(top_env, dict):
        out.extend((k, v, "env") for k, v in top_env.items())
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            job_env = job.get("env")
            if isinstance(job_env, dict):
                out.extend((k, v, f"jobs.{job_id}.env") for k, v in job_env.items())
            steps = job.get("steps")
            if isinstance(steps, list):
                for i, step in enumerate(steps):
                    if not isinstance(step, dict):
                        continue
                    step_env = step.get("env")
                    if isinstance(step_env, dict):
                        out.extend((k, v, f"jobs.{job_id}.steps[{i}].env") for k, v in step_env.items())
    return out


def check_cache_010(ci: CiToml, repo_root: Path) -> list[Finding]:
    """Issue #7: a workflow or job sets a cache-layer kill-switch env var
    while ci.toml still declares a [cache.family] backed by a matching
    setup-soldr/zccache layer -- the setup step's own summary keeps
    claiming the layer is active, so a green run never surfaces the
    contradiction on its own (see KILL_SWITCH_ENV_VARS above for the
    zackees/clud evidence this codifies)."""

    vias = [fam.via for fam in ci.cache.family.values()]
    soldr_active = any(v.startswith("setup-soldr:") or v.startswith("zccache") for v in vias)
    ccache_active = "ccache" in vias
    if not (soldr_active or ccache_active):
        return []

    findings: list[Finding] = []
    for path, _is_composite, doc in _loaded_files(repo_root):
        for name, value, loc in _iter_env_entries(doc):
            applies = ccache_active if name in _CCACHE_SWITCHES else soldr_active
            if name in KILL_SWITCH_ENV_VARS and applies and _is_truthy(value):
                findings.append(
                    Finding(
                        rule="CACHE-010",
                        path=path,
                        message=f"{loc}.{name} = {value!r} disables a cache layer while ci.toml declares "
                        "a [cache.family] via a matching setup-soldr/zccache/ccache backend",
                        fix=f"remove '{name}' (or set it falsy) at {loc} in {path} so the declared layer "
                        "is actually written; if it is intentionally disabled here, retire the matching "
                        "[cache.family] entry instead of leaving ci.toml claim it active",
                    )
                )
    return findings


def check_cache_014(ci: CiToml) -> list[Finding]:
    """M2-17 (ci.yml#42): warn when `[cache.pr].max-per-pr` is sized wrong
    for the repository's scale, using whichever of the two optional inputs
    is supplied. Both are warnings (not hard failures): unlike CACHE-004's
    proven worst-case arithmetic, these depend on inputs a repository may
    not have measured yet (an open-PR count estimate, a delta measurement)."""

    findings: list[Finding] = []
    max_per_pr = parse_size(ci.cache.pr.max_per_pr) if ci.cache.pr.max_per_pr else None
    if max_per_pr is None:
        return findings

    expected_open_prs = ci.cache.pr.expected_open_prs
    if expected_open_prs is not None:
        pr_budget = parse_size(ci.cache.pr.budget) if ci.cache.pr.budget else None
        if pr_budget is not None:
            fleet_worst = max_per_pr * expected_open_prs
            if fleet_worst > pr_budget:
                findings.append(
                    Finding(
                        rule="CACHE-014",
                        path="ci.toml",
                        message=(
                            f"[cache.pr].max-per-pr ({ci.cache.pr.max_per_pr} = {max_per_pr} B) x "
                            f"[cache.pr].expected-open-prs ({expected_open_prs}) = {fleet_worst} B, "
                            f"which exceeds [cache.pr].budget ({ci.cache.pr.budget} = {pr_budget} B)"
                        ),
                        fix=(
                            "lower [cache.pr].max-per-pr, lower [cache.pr].expected-open-prs (if the "
                            "estimate was too high), or raise [cache.pr].budget -- see docs/ci-toml.md "
                            "'Sizing [cache.pr] for repository scale' for the formula"
                        ),
                    )
                )

    measured = ci.cache.pr.measured_largest_delta
    if measured:
        measured_bytes = parse_size(measured)
        if measured_bytes is not None and measured_bytes > max_per_pr:
            findings.append(
                Finding(
                    rule="CACHE-014",
                    path="ci.toml",
                    message=(
                        f"[cache.pr].max-per-pr ({ci.cache.pr.max_per_pr} = {max_per_pr} B) is below "
                        f"[cache.pr].measured-largest-delta ({measured} = {measured_bytes} B): the "
                        "largest observed PR delta would be truncated on save"
                    ),
                    fix=(
                        "raise [cache.pr].max-per-pr to at least [cache.pr].measured-largest-delta "
                        "(re-measure with 'ci-lint cache delta manifest' against the largest real PR "
                        "if this value is stale) -- see docs/ci-toml.md 'Sizing [cache.pr] for "
                        "repository scale'"
                    ),
                )
            )

    return findings


def check_cache_029(ci: CiToml) -> list[Finding]:
    """A large family with nothing bounding how many entries may exist at
    once will fill the repository's whole cache budget.

    Measured across the three largest Rust repositories, 2026-10-04, with
    `ci-lint cache audit` on the one that declares its families:

        kernal-api   49 entries, 9.39 GiB, 87% superseded -> 6.5 GiB reclaimed,
                     3.01 GiB left (it declares `[cache.family]`, so the
                     family boundary is known)
        clud         32 entries, 10.56 GiB -- ALREADY OVER GitHub's 10GB cap,
                     so new saves were being refused; 1.86 GiB recoverable
        running-process 100 entries, 9.16 GiB; 1.01 GiB recoverable

    clud and running-process declare no `[cache.family]`, so their family
    boundaries have to be inferred from the key shape
    (`...-v<n>-<runner>-<lockhash>-<shape>-<contenthash>`, grouping on
    everything but the hex components). That inference is deliberately
    conservative -- it keeps entries rather than guessing -- which is why
    their recoverable fraction is far below kernal-api's.

    The mechanism is the same in all three: these families' keys embed a
    per-run hash, so every run appends an entry nothing ever retires.

    `max` declares the size of ONE entry, not the family's footprint, so it
    cannot catch this. Only `evict = "lru"` bounds it: the janitor keeps the
    family at `max x cardinality`, evicting least-recently-used.

    `per` is deliberately NOT accepted as an exemption. It declares the
    *writer shape*, not the entry count, and kernal-api is the proof:
    every one of its families declares `per = "none"` while `dylint` still
    accumulated 13 live entries. docs/ci-toml.md records the same problem
    for `setup-uv`, whose key encodes arch, platform, OS version, Python
    version and a content hash -- so its live cardinality can exceed what
    `per = "os"` claims.

    Only families at or above 256 MB are flagged: below that, unbounded
    entry counts are noise rather than a budget threat."""

    findings: list[Finding] = []
    for fid, family in sorted(ci.cache.family.items()):
        if family.evict == "lru":
            continue
        declared = parse_size(family.max)
        if declared is None or declared < UNBOUNDED_FAMILY_BYTES:
            continue
        findings.append(
            Finding(
                rule="CACHE-029",
                path="ci.toml",
                message=(
                    f"cache family '{fid}' declares max = {family.max} but nothing bounds how many "
                    f"entries may exist at once, so the family can grow without limit until the "
                    "repository's whole cache budget is consumed"
                ),
                fix=(
                    f"add evict = \"lru\" to '[cache.family.{fid}]' so ci-lint cache janitor keeps "
                    f"the family at max x cardinality. Measure the current state with "
                    f"'ci-lint cache audit --repo .' -- CACHE-006 names the superseded entries that "
                    f"are consuming the budget."
                ),
            )
        )
    return findings


def check_cache_030(ci: CiToml) -> list[Finding]:
    """`[cache.promote] mode = "ancestor"` declares that a default-branch
    push inherits the cache a pull request proved, instead of rewriting it.

    The MECHANISM now exists -- this rule's premise was wrong when it was
    written. zackees/setup-soldr shipped `src/lib/ancestor-cache.ts` in #566,
    with `autoKeyEnabled` gating it in `src/main.ts` and present in both built
    bundles. It is still an opt-in PILOT that no fleet repository has adopted:
    `auto-key` defaults to "false", `auto-key-trusted-writers` must name
    reviewed immutable writer jobs, and as of 2026-10-05 no repository has a
    single live `setup-soldr-ancestor-build-v1-*` entry. Issue #552 tracks the
    fleet-wide rollout, not the implementation.

    So this stays `needs_review`, but for a sharper reason than "nothing
    implements it": the mechanism is available and OFF, so the declaration is a
    standing invitation to opt in -- and the drift backstop must outlive the
    declaration until promotion is actually observed working on that
    repository."""

    if ci.cache.promote.mode != "ancestor":
        return []
    return [
        Finding(
            rule="CACHE-030",
            status=Status.NEEDS_REVIEW,
            path="ci.toml",
            message='\'cache.promote.mode = "ancestor"\' declares nearest-ancestor cache promotion, '
            "but setup-soldr's ancestor mechanism is an opt-in pilot that is off: `auto-key` defaults "
            'to "false" and `auto-key-trusted-writers` must name reviewed immutable writer jobs '
            "(no fleet repository has a live `setup-soldr-ancestor-build-v1-*` entry as of 2026-10-05)",
            fix="opt in at the workflow: pass `cache-key: auto` (or `auto-key: \"true\"`) plus an "
            "explicit auto-key-trusted-writers list, per zackees/setup-soldr#552. Until a live "
            "setup-soldr-ancestor-build-v1-* entry is observed on this repository, promotion does "
            "not happen -- keep the drift backstop (a scheduled full default-branch run, or enough "
            "naturally-must-run pushes) and do not remove it on the strength of this declaration",
        )
    ]


def check_group9(ci: CiToml, repo_root: Path) -> tuple[list[Finding], str]:
    findings: list[Finding] = []
    findings.extend(check_cache_001(ci, repo_root))
    findings.extend(check_cache_002(ci, repo_root))
    findings.extend(check_cache_003_setup_uv(ci, repo_root))
    findings.extend(check_cache_010(ci, repo_root))
    cache_004_findings, arithmetic = check_cache_004(ci)
    findings.extend(cache_004_findings)
    findings.extend(check_cache_014(ci))
    findings.extend(check_cache_029(ci))
    findings.extend(check_cache_030(ci))
    findings.extend(check_cache_031(ci))
    findings.extend(check_cache_032(ci))
    findings.extend(check_cache_034(ci, repo_root))
    findings.extend(check_cache_038(ci, repo_root))
    from ci_lint.rules.lint_cache import check_lint_cache  # pylint: disable=import-outside-toplevel

    findings.extend(check_lint_cache(ci, repo_root))
    return findings, arithmetic


def check_cache_031(ci: CiToml) -> list[Finding]:
    """A repository that declares ancestor promotion, but whose families
    cannot support it, is promising something its keys cannot deliver.

    Promotion means: given commit M, find the nearest ancestor's cache entry.
    That search is decidable from the keys alone only when a key carries the
    lineage label (`m<n>[-c<k>]-<sha10>[-pr-<N>]`,
    ci_lint.cache_lineage) -- `m<n>` is the first-parent ordinal, so
    `m<i>` precedes `m<j>` iff i < j. A key that ends in a bare content hash
    (setup-soldr's `...-<16 hex>`) cannot answer the question at all: a
    content hash is a function of the input tree, so nothing in it records
    ancestry. That is not a gap a janitor can bridge -- the evidence is gone
    at save time.

    Measured 2026-10-04 across the fleet's live cache listings: the label is
    adopted exactly where it carries no bytes. soldr has 70 lineage-labeled
    entries totalling 0.00 GiB (all `att1-*` attestation side-entries)
    against 5.97 GiB of bare-hash build caches; bosn 85 / 0.00 GiB against
    4.41 GiB; zccache, clud and kernal-api have none at all.

    This is `needs_review`, never a hard failure, for two reasons. The
    opt-in exists but is OFF (`auto-key` defaults to "false";
    auto-key-trusted-writers must name reviewed immutable writer jobs), so a
    finding may be a repository that has not opted in rather than one that got
    it wrong -- and CACHE-030 already asks the same question of the
    declaration. And a family only becomes promotable when its keys change
    shape -- an instantaneous fix would invalidate every warm entry at once,
    which on repositories already at the 10GB cap is a real cost, not a free
    correction.

    Note the shipped mechanism uses a DIFFERENT label than this module's.
    setup-soldr's ancestor keys are
    `setup-soldr-ancestor-build-v1-<identity>-source-<sha>-run-<n>-attempt-<n>[-pr-<n>]`
    and select by shortest parent-edge distance from a live `git` DAG walk
    (`dagDistances`), not by the `m<n>` first-parent ordinal, so
    `ci_lint.cache_lineage.parse_key` returns None for them. Both record
    ancestry and both survive a merge; they are two encodings, not two
    truths. Reconciling them is zackees/ci.yml#185's open question."""

    if ci.cache.promote.mode != "ancestor":
        return []
    findings: list[Finding] = []
    for fid, family in sorted(ci.cache.family.items()):
        if family.promote == "ancestor":
            continue
        declared = parse_size(family.max)
        if declared is None or declared < UNBOUNDED_FAMILY_BYTES:
            continue
        findings.append(
            Finding(
                rule="CACHE-031",
                status=Status.NEEDS_REVIEW,
                path="ci.toml",
                message=(
                    f"'cache.promote.mode = \"ancestor\"' is declared, but cache family '{fid}' "
                    f"(max = {family.max}) does not declare promote = \"ancestor\" on its own keys, "
                    "so its entries carry a content hash rather than a lineage label and no "
                    "nearest-ancestor promotion search can select one"
                ),
                fix=(
                    f"add promote = \"ancestor\" to '[cache.family.{fid}]' and have the workflow "
                    "suffix the key with the label from 'ci-lint attest lineage' "
                    "(m<n>-<sha10> on main, m<b>-c<k>-<sha10>-pr-<N> on a PR, the PR part already "
                    f"being ${{{{ env.PR_CACHE_TAG }}}} per CACHE-013). Blocked on "
                    "zackees/setup-soldr#552 for setup-soldr-owned families; changing a live key "
                    "shape also invalidates that family's warm entries, so schedule it"
                ),
            )
        )
    return findings


def check_cache_032(ci: CiToml) -> list[Finding]:
    """A promotion opt-in the pinned action cannot honour.

    setup-soldr's ancestor pilot (#566) is MERGED and present in
    `dist/main.js`, but has never been released: `git tag --contains
    afdd8bf` is empty, and no v0.9.x tag's `action.yml` declares
    `auto-key`. Every setup-soldr SHA a repository can legitimately pin
    therefore predates the pilot -- soldr pins `a07bab94`, bosn `fe965fc7`,
    neither with `auto-key` in `action.yml`.

    SEC-004 requires a 40-hex SHA pin (`zackees/setup-soldr@v0` is the single
    sanctioned float), so a repository cannot reach the feature at all. The
    failure is silent: `cache-key: auto` or `auto-key: "true"` is accepted as
    an ordinary input, the pilot code never runs, and every restore falls back
    to the legacy key with no error.

    RUST-014 cannot cover this. It flags a pin that predates a release marked
    critical, and there is no release here to mark -- the feature is ahead of
    the tags.

    The `v0` float: measured 2026-10-05, `v0` was `dfbe962` (#532), behind
    the pilot. Since 2026-10-10 it resolves to `b05310a` (setup-soldr#574),
    which carries the pilot, so `@v0` reaches it and an older 40-hex pin
    does not.

    It stays `needs_review` rather than a violation because the gate is a
    release cadence, not a repository defect: nothing in this repository is
    wrong, and the finding must clear itself the moment a tag carries #566.
    Its claim is that the declared opt-in is unreachable -- weaker and
    different from CACHE-030/031's "promotion is not happening".
    """

    if ci.cache.promote.mode != "ancestor":
        return []
    return [
        Finding(
            rule="CACHE-032",
            status=Status.NEEDS_REVIEW,
            path="ci.toml",
            message='\'cache.promote.mode = "ancestor"\' is declared, but setup-soldr\'s ancestor '
            "pilot (#566) is merged and unreleased: no tag contains it, no released tag's action.yml "
            "declares `auto-key`, and the sanctioned `v0` float trails main by 33 commits -- so no "
            "SEC-004-legal reference to the action can reach it",
            fix="confirm the referenced setup-soldr revision declares `auto-key` in its action.yml "
            "before relying on this. Today none does -- not a pinned SHA (SEC-004), and not the "
            "sanctioned `zackees/setup-soldr@v0` float, which is 33 commits behind main. The opt-in "
            "is silently inert until the pilot ships in a release (zackees/setup-soldr#552)",
        )
    ]



# CACHE-034: `pre-prune = true` is a CLAIM about what the workflow does, and
# CACHE-004's worst-case arithmetic trusts it -- `all_pre_pruned` waives the
# lockfile-change peak outright. Nothing checked that any workflow actually
# performs the pre-prune, so the waiver can be had for free.
#
# Found in FastLED/fbuild#1652 (2026-10-05): `[flow.main] pre-prune = true`
# with `ci-lint cache preprune` appearing in ZERO workflow files. Removing the
# declaration moved the modelled worst case from 7.92 GB to 15.84 GB against a
# 10.20 GB budget -- the declaration had been suppressing 7.92 GB of modelled
# footprint that nothing was reclaiming.
#
# The check is deliberately text-level: a pre-prune reached through a
# composite action or a delegated `ci/*.py` script is still honoured, so the
# scan covers workflows, composite actions, and `ci/` scripts one level deep.
# It is `needs_review` rather than a violation because the call may legitimately
# live in a script this scan cannot resolve, and a false accusation here would
# push a repository toward a budget it can actually meet.
_PREPRUNE_CALL = re.compile(r"cache\s+preprune")
_PREPRUNE_SURFACES = ("ci",)
# #352's teaching, not a loosened pattern: a WORKFLOW may also honour the
# waiver by its own identity -- `name: Cache Pre-prune` (zccache's spelling)
# or a file stem of `cache-pre-prune.yml` -- because zccache implements the
# prune by hand (`planLockTransitionPrePrune`, guarded deletes) instead of
# the ci-lint command, and a literal-only scan reads it as unhonoured. Only
# this canonical spelling counts; every other surface still has to show the
# literal invocation. An idle workflow wearing the name is a claim of the
# same order as `pre-prune = true` itself -- the live audit (CACHE-006/
# 008/009 and the budget verdict) is what catches a named workflow that
# never deletes anything.
_PREPRUNE_WORKFLOW_IDENTITY = re.compile(r"^cache[\s_-]*pre[\s_-]*prune$", re.IGNORECASE)


def _preprune_call_exists(repo_root: Path) -> bool | None:
    """Whether any scanned surface honours the pre-prune waiver.

    Honoured means the literal `ci-lint cache preprune` invocation on any
    scanned surface, or -- workflows only -- the canonical pre-prune
    workflow identity (`_PREPRUNE_WORKFLOW_IDENTITY`).

    Returns None when the answer is UNKNOWN: a workflow that exists but could
    not be parsed (no PyYAML and no `yq`) must not be read as "no call exists",
    or a missing parser would manufacture a finding. The caller reports
    nothing in that case -- an unreadable tree is not evidence of a
    contradiction.
    """

    def _has(text: str) -> bool:
        return _PREPRUNE_CALL.search(text) is not None

    unparsed = False
    workflows = _documents_contain(load_workflows(repo_root), recognise_identity=True)
    if workflows.matched:
        return True
    composites = _documents_contain(load_composite_actions(repo_root))
    if composites.matched:
        return True
    unparsed = workflows.unparsed or composites.unparsed
    for rel in _iter_ci_scripts(repo_root):
        try:
            if _has((repo_root / rel).read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return None if unparsed else False


@dataclass(frozen=True)
class _ScanOutcome:
    """Result of scanning one workflow-like surface for a pre-prune call."""

    matched: bool
    unparsed: bool


def _documents_contain(loaded: list[object], *, recognise_identity: bool = False) -> _ScanOutcome:
    matched = False
    unparsed = False
    for item in loaded:
        status = getattr(item, "status", None)
        if status != LoadStatus.OK:
            unparsed = True
            continue
        if _PREPRUNE_CALL.search(_document_text(getattr(item, "document", None))):
            matched = True
            continue
        if recognise_identity and _workflow_identity_honours(item):
            matched = True
    return _ScanOutcome(matched=matched, unparsed=unparsed)


def _workflow_identity_honours(item: object) -> bool:
    """A workflow claiming the role by canonical name or file stem (#352).

    Both spellings are accepted because zccache uses both: the file is
    `.github/workflows/cache-pre-prune.yml` and the document says
    `name: Cache Pre-prune`. The pattern is anchored, so a lookalike such
    as `Cache Pre-prune deployer` is NOT a match.
    """

    stem = Path(str(getattr(item, "path", ""))).stem
    if _PREPRUNE_WORKFLOW_IDENTITY.search(stem):
        return True
    name = as_dict(getattr(item, "document", None)).get("name")
    return isinstance(name, str) and _PREPRUNE_WORKFLOW_IDENTITY.search(name) is not None


def _iter_ci_scripts(repo_root: Path) -> list[str]:
    """`ci/` scripts one level deep, the same surface GHAPI-001 scans."""

    ci_dir = repo_root / "ci"
    if not ci_dir.is_dir():
        return []
    return sorted(str(p.relative_to(repo_root)) for p in ci_dir.glob("*.py"))


def _document_text(document: object) -> str:
    """Flatten a workflow/composite document to text for the call scan.

    `_document_text` is deliberately crude: it only has to find a command
    string, and a false negative here would wrongly clear a real finding, so
    it errs toward including everything rather than parsing precisely.
    """

    return "\n".join(_walk_strings(document))


def _walk_strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        out: list[str] = []
        for value in node.values():
            out.extend(_walk_strings(value))
        return out
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_walk_strings(item))
        return out
    return []


def check_cache_034(ci: CiToml, repo_root: Path) -> list[Finding]:
    """`pre-prune = true` on a writer flow with no pre-prune call anywhere."""

    declaring: list[str] = []
    for fid, flow in ci.flows.items():
        if resolve_flow(ci, fid).pre_prune:
            declaring.append(fid)
    if not declaring:
        return []
    exists = _preprune_call_exists(repo_root)
    if exists is not False:
        # True (a call exists), or None (the tree could not be read). Neither
        # is evidence of an unhonoured declaration.
        return []
    flows = ", ".join(f"'{f}'" for f in sorted(declaring))
    return [
        Finding(
            rule="CACHE-034",
            status=Status.NEEDS_REVIEW,
            path="ci.toml",
            message=(
                f"flow(s) {flows} declare `pre-prune = true`, which waives the lockfile-change "
                "peak from CACHE-004's worst case, but no workflow, composite action, or ci/ "
                "script invokes `ci-lint cache preprune` -- the waiver is unhonoured, so the "
                "modelled footprint is understated by the whole peak term"
            ),
            fix=(
                "either add the pre-prune step to the writer flow -- `ci-lint cache preprune "
                "--lockfile-changed` on a job holding `actions: write`, ordered ahead of the "
                "cache saves WITHIN THE SAME RUN (same job, or an earlier `needs:` link; "
                "GitHub Actions has no cross-workflow barrier, so a prune in a workflow the "
                "writers do not `needs:` orders nothing) -- or, where neither that permission "
                "nor that ordering can be had, remove `pre-prune = true` so CACHE-004 counts "
                "the peak. Removal is the honest fix but not a free one: the waived "
                "lockfile-change peak returns and the modelled footprint grows by it "
                "(zackees/ci.yml#354)"
            ),
        )
    ]


# CACHE-038 (#125): setup-soldr's build-cache key is
# `setup-soldr-buildcache-v2-<os>-<arch>-<toolchain>-<lock>[-<suffix>]`, so two
# saving jobs on one runner label with one (or no) `cache-key-suffix` compute
# one key. GitHub cache keys are immutable: the first job to finish wins, the
# other's save is silently dropped while its log still says `saved`
# (FastLED/fbuild#1587: a 166.5 MB facade cache beat the 478.7 MB workspace one,
# and PRs restored it for a 28% hit rate).
_SETUP_SOLDR_SLUG = "zackees/setup-soldr"
_NON_BUILD_SUBACTIONS: tuple[str, ...] = ("/cook", "/cleanup")
_NO_SAVE_VALUES: frozenset[str] = frozenset({"false", "none", "no", "off", "0"})
_INPUT_REF_RE = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")


@dataclass(frozen=True)
class _SoldrWriter:
    """One setup-soldr build-cache saving step, as one job sees it."""

    workflow: str
    job: str
    loc: str
    runs_on: str | None  # None = dynamic, not statically known
    suffix: str | None  # None = dynamic; "" = absent
    save: str


@dataclass(frozen=True)
class _InputDefault:
    """One composite-action input's declared default."""

    name: str
    value: str


@dataclass(frozen=True)
class _CompositeSoldr:
    """A local composite action's setup-soldr build step plus its input defaults."""

    action: str  # `./<action dir>`, as a caller's `uses:` names it
    with_: dict[str, YamlValue]
    defaults: tuple[_InputDefault, ...]


def _is_build_soldr(uses: str) -> bool:
    if not uses.startswith(_SETUP_SOLDR_SLUG):
        return False
    rest = uses[len(_SETUP_SOLDR_SLUG) :].split("@", 1)[0]
    return rest == "" or rest not in _NON_BUILD_SUBACTIONS


def _composite_soldr_steps(repo_root: Path) -> list[_CompositeSoldr]:
    """Every local composite action's setup-soldr build steps (one level deep)."""

    out: list[_CompositeSoldr] = []
    for item in load_composite_actions(repo_root):
        if item.status != LoadStatus.OK:
            continue
        doc = as_dict(item.document)
        inputs = doc.get("inputs")
        defaults = tuple(
            _InputDefault(name=str(name), value=str(spec.get("default")))
            for name, spec in (inputs.items() if isinstance(inputs, dict) else ())
            if isinstance(spec, dict) and spec.get("default") is not None
        )
        action = "./" + str(Path(item.path).parent).strip("/")
        out.extend(
            _CompositeSoldr(action=action, with_=s.with_, defaults=defaults)
            for s in _iter_uses_with(doc, is_composite=True)
            if _is_build_soldr(s.uses)
        )
    return out


def _resolve_input(
    value: YamlValue, caller: dict[str, YamlValue], defaults: tuple[_InputDefault, ...]
) -> YamlValue:
    """Substitute a bare `${{ inputs.<n> }}` with the caller's value or default."""

    if isinstance(value, str):
        m = _INPUT_REF_RE.match(value.strip())
        if m:
            name = m.group(1)
            if name in caller:
                return caller[name]
            return next((d.value for d in defaults if d.name == name), None)
    return value


def _as_text(value: YamlValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _runs_on_key(value: YamlValue) -> str | None:
    if isinstance(value, str):
        return None if "${{" in value else value.strip()
    if isinstance(value, list) and value and all(isinstance(v, str) and "${{" not in v for v in value):
        return ",".join(sorted(str(v).strip() for v in value))
    return None


def _writer_from_with(
    wf: str, job: str, loc: str, runs_on: str | None, with_: dict[str, YamlValue]
) -> _SoldrWriter | None:
    save = _as_text(with_.get("save-cache"))
    save_text = "auto" if save is None else save.strip()
    if save_text.lower() in _NO_SAVE_VALUES:
        return None
    raw_suffix = _as_text(with_.get("cache-key-suffix"))
    if raw_suffix is None:
        suffix: str | None = ""
    elif "${{" in raw_suffix:
        suffix = None
    else:
        suffix = raw_suffix.strip()
    return _SoldrWriter(workflow=wf, job=job, loc=loc, runs_on=runs_on, suffix=suffix, save=save_text)


def _collect_soldr_writers(repo_root: Path) -> list[_SoldrWriter]:
    composites = _composite_soldr_steps(repo_root)
    writers: list[_SoldrWriter] = []
    for item in load_workflows(repo_root):
        if item.status != LoadStatus.OK:
            continue
        for job_id, job in jobs_of(as_dict(item.document)).items():
            runs_on = _runs_on_key(job.get("runs-on"))
            for i, step in enumerate(steps_of(job)):
                uses = step.get("uses")
                if not isinstance(uses, str):
                    continue
                raw_with = step.get("with")
                with_ = raw_with if isinstance(raw_with, dict) else {}
                loc = f"jobs.{job_id}.steps[{i}]"
                candidates: list[dict[str, YamlValue]] = []
                if _is_build_soldr(uses):
                    candidates.append(with_)
                else:
                    for comp in (c for c in composites if c.action == uses.rstrip("/")):
                        candidates.append(
                            {k: _resolve_input(v, with_, comp.defaults) for k, v in comp.with_.items()}
                        )
                for cand in candidates:
                    writer = _writer_from_with(item.path, job_id, loc, runs_on, cand)
                    if writer is not None:
                        writers.append(writer)
    return writers


def _describe(w: _SoldrWriter) -> str:
    return f"{w.workflow} {w.loc}"


def check_cache_038(ci: CiToml, repo_root: Path) -> list[Finding]:
    """Two or more saving setup-soldr jobs racing for one immutable cache key."""

    _ = ci  # uniform group-9 signature; the rule reads only the workflows
    writers = _collect_soldr_writers(repo_root)
    findings: list[Finding] = []
    dynamic = [w for w in writers if w.runs_on is None or w.suffix is None]
    static = sorted(
        (w for w in writers if w.runs_on is not None and w.suffix is not None),
        key=lambda w: (w.runs_on or "", w.suffix or ""),
    )
    fix = (
        "give every saving setup-soldr job on the same runner a distinct `cache-key-suffix` "
        "(e.g. the job id), or set `save-cache: \"false\"` on all but the one job whose build "
        "the others should restore (zackees/ci.yml#125, FastLED/fbuild#1587)"
    )
    for (runs_on, suffix), grouped in itertools.groupby(static, key=lambda w: (w.runs_on, w.suffix)):
        members = list(grouped)
        jobs = {(m.workflow, m.job) for m in members}
        if len(jobs) < 2:
            continue
        where = "; ".join(_describe(m) for m in members)
        shown = f"`cache-key-suffix: {suffix}`" if suffix else "no `cache-key-suffix`"
        findings.append(
            Finding(
                rule="CACHE-038",
                status=Status.VIOLATION,
                path=members[0].workflow,
                message=(
                    f"{len(jobs)} jobs save a setup-soldr build cache on `runs-on: {runs_on}` with "
                    f"{shown} ({where}); they compute one immutable key, so the first to finish "
                    "wins and every other save is silently dropped while its log still says saved"
                ),
                fix=fix,
            )
        )
    if dynamic and len(writers) >= 2:
        for w in dynamic:
            part = "runs-on" if w.runs_on is None else "cache-key-suffix"
            findings.append(
                Finding(
                    rule="CACHE-038",
                    status=Status.NEEDS_REVIEW,
                    path=w.workflow,
                    message=(
                        f"{_describe(w)} saves a setup-soldr build cache with a dynamic {part}; "
                        f"with {len(writers)} saving setup-soldr steps in the repository, confirm "
                        "no two of them resolve to the same runner and suffix"
                    ),
                    fix=fix,
                )
            )
    return findings
