"""The planner: compute one run's flow/tag selection (round-1A brief, section C).

Selection = (flow base ∪ tag adds) − tag removes. Tags are only read from
the PR title, and only for `pull_request` events -- never for `push`/
`schedule`/`workflow_dispatch`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.resolve import platform_groups, resolve_flow
from ci_lint.rules.contract import extract_bracket_tokens
from ci_lint.schema import CiToml

# The blob-hash lockfiles read (when present) into every lane digest, so a
# dependency or toolchain change invalidates title-edit reuse for every
# lane, not just the ones whose own inputs literally changed (round-3A
# brief, Part 1: "the repo's Cargo.lock + uv.lock + rust-toolchain.toml
# blob hashes when present").
_ENV_FINGERPRINT_FILES: tuple[str, ...] = ("Cargo.lock", "uv.lock", "rust-toolchain.toml")

EVENT_TO_FLOW: dict[str, str] = {
    "pull_request": "pr",
    "push": "main",
    "schedule": "nightly",
    "workflow_dispatch": "release",
    # A merge-queue run validates the same change a PR run did (GEN-022), so
    # it takes the PR flow -- or a declared `queue` flow when the repository
    # wants the queue to run something different. No title tokens apply.
    "merge_group": "pr",
}

QUEUE_FLOW = "queue"

DISPATCH_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class PlanError(ValueError):
    """The requested selection is invalid (e.g. [release] with [no-test])."""


@dataclass(frozen=True)
class PlanPlatform:
    id: str
    target: str
    runs_on: str
    group: str


@dataclass(frozen=True)
class PlatformLane:
    """One non-default-platform matrix leg (round-3A brief, Part 1): every
    selected platform EXCEPT `[flow.pr]`'s own default fast-lane
    platform(s) -- see `_default_fast_lane_platforms`. `suites` is the
    run's resolved suite selection (`Plan.suites`): every platform lane
    runs the same suites the fast lane does -- "unit and smoke always;
    plus integration/init/perf when selected by flow or tags, minus
    removals" is exactly how `Plan.suites` itself is already computed, so
    there is no separate per-platform suite composition to derive."""

    id: str
    target: str
    runs_on: str
    group: str
    wheel: str | None
    suites: tuple[str, ...]

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "target": self.target,
            "runs_on": self.runs_on,
            "group": self.group,
            "wheel": self.wheel,
            "suites": list(self.suites),
        }


@dataclass(frozen=True)
class LaneDigest:
    lane: str  # "fast", "dylint" or "platform:<id>"
    digest: str  # 12 hex characters


@dataclass(frozen=True)
class LaneDigests(Mapping[str, str]):
    """Read-only lane -> digest mapping backed by typed `LaneDigest` records."""

    entries: tuple[LaneDigest, ...] = ()

    def __getitem__(self, lane: str) -> str:
        for entry in self.entries:
            if entry.lane == lane:
                return entry.digest
        raise KeyError(lane)

    def __iter__(self) -> Iterator[str]:
        return (entry.lane for entry in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class Plan:
    flow: str
    event_name: str
    tags: tuple[str, ...]
    platforms: tuple[PlanPlatform, ...]
    suites: tuple[str, ...]
    dylint_targets: tuple[str, ...]
    cache_mode: str
    publish: str
    mergeable: bool
    digest: str
    reasons: tuple[str, ...]
    required_jobs: tuple[str, ...]
    needs_platform_lanes: bool
    dispatch_sha: str | None = None
    # Round-3A brief, Part 1 additions. Defaulted so every existing
    # `Plan(...)` call site (there are none outside `compute_plan`, but
    # keeping these last and defaulted matches the dataclass field-order
    # rule and keeps this a purely additive change) keeps working.
    platform_lanes: tuple[PlatformLane, ...] = ()
    fast_suites: tuple[str, ...] = ()
    lane_digests: LaneDigests = field(default_factory=LaneDigests)

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {
            "flow": self.flow,
            "event_name": self.event_name,
            "tags": list(self.tags),
            "platforms": [
                {"id": p.id, "target": p.target, "runs_on": p.runs_on, "group": p.group}
                for p in self.platforms
            ],
            "suites": list(self.suites),
            "dylint_targets": list(self.dylint_targets),
            "cache_mode": self.cache_mode,
            "publish": self.publish,
            "mergeable": self.mergeable,
            "digest": self.digest,
            "reasons": list(self.reasons),
            "required_jobs": list(self.required_jobs),
            "needs_platform_lanes": self.needs_platform_lanes,
            "dispatch_sha": self.dispatch_sha,
            "platform_lanes": [pl.to_json_dict() for pl in self.platform_lanes],
            "fast_suites": list(self.fast_suites),
            "lane_digests": dict(self.lane_digests),
        }


def dylint_target_platform_ids(ci: CiToml) -> tuple[str, ...]:
    """Every `[platforms]` id Dylint must check from the one Linux job
    (issue #6 §2/§3) when `ci.toml` alone -- not a particular run's flow/
    tag resolution -- decides it: `[lint.dylint].targets = "all-platforms"`
    means every declared platform. This is the ci.toml-only half of
    `compute_plan`'s own `dylint_targets` computation below (which also
    considers a flow's `resolved.dylint` override); `ci_lint.runtime.dylint`
    (RUST-003's `ci-lint dylint coverage`) has no run/flow/tag context of
    its own -- it is a standalone repo-level check -- so it calls this
    directly rather than a full `compute_plan`."""

    if ci.lint_dylint is not None and ci.lint_dylint.targets == "all-platforms":
        return tuple(sorted(ci.platforms))
    return ()


def _required_job_ids(
    needs_platform_lanes: bool,
    dylint_targets: tuple[str, ...],
    suites: tuple[str, ...] = (),
    publish: str = "none",
) -> tuple[str, ...]:
    """The job `id:`s (`needs:` keys) `ci-lint gate` requires to have
    `result == "success"` (round-2A brief, part 2d; amended by the
    orchestrator's round-2A amendment 1). This is the one place that
    convention is defined; the template's `ci.yml` must name its jobs to
    match:

      - "precheck" always (the precheck job itself).
      - "fast" always -- the default linux-x64 build+unit+smoke lane.
      - "dylint" when any dylint target is selected.
      - "platform-build" and "platform-run" only when `needs_platform_lanes`
        is true -- these are matrix jobs (one leg per non-default
        platform); GitHub aggregates every matrix leg into a single
        `needs.<id>.result`, so one id each covers the whole matrix.

      - "init" when the `init` suite is selected -- the from-zero job
        (no cache restore, template instantiation), which must run in its
        own job because it may not share any restored cache.
      - "perf" when the `perf` suite is selected (its job must succeed even
        when the suite is non-gating; gating only decides whether a
        regression fails it).
      - "release-verify" when publish is "rehearsal" or "mock" -- the
        staged-artifact completeness check (`ci_lint release verify`).
      - "publish" when publish is "mock" -- the top-level OIDC mock
        publisher (PyPI cannot trust a reusable workflow, warehouse#11096).

    "ci-ok" (the gate job itself) is never in this list -- a job cannot
    require its own result.
    """

    jobs = ["precheck", "fast"]
    if dylint_targets:
        jobs.append("dylint")
    if needs_platform_lanes:
        jobs.append("platform-build")
        jobs.append("platform-run")
    if "init" in suites:
        jobs.append("init")
    if "perf" in suites:
        jobs.append("perf")
    if publish in ("rehearsal", "mock"):
        jobs.append("release-verify")
    if publish == "mock":
        jobs.append("publish")
    return tuple(jobs)


def _default_fast_lane_platforms(ci: CiToml) -> frozenset[str]:
    """The fixed platform set the always-required "fast" job covers --
    `[flow.pr]`'s own declared `platforms` (normally just `linux-x64`),
    regardless of which flow the current run actually resolves. Round-2A
    amendment 3: `needs_platform_lanes` is true exactly when the final
    selection includes anything beyond this fixed baseline, not merely
    "more than one platform" -- release/nightly's `platforms = "all"`
    base is itself already beyond this baseline, so they still need the
    platform-build/platform-run matrix even though nothing was "added" by
    a tag."""

    pr_flow = resolve_flow(ci, "pr")
    return frozenset(_expand_platforms(ci, pr_flow.platforms))


def _expand_platforms(ci: CiToml, value: tuple[str, ...] | str | None) -> set[str]:
    if value is None:
        return set()
    if value == "all":
        return set(ci.platforms)
    return {v for v in value if v in ci.platforms}


def _expand_suites(ci: CiToml, value: tuple[str, ...] | str | None) -> set[str]:
    if value is None:
        return set()
    if value == "all":
        return set(ci.suites)
    if value == "tests":
        return set(ci.suites)  # only meaningful as a `remove` sentinel; see below
    return {v for v in value if v in ci.suites}


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env_fingerprint(ci: CiToml, repo_root: Path | None) -> dict[str, JsonValue]:
    """The environment component mixed into every lane digest: the
    `linter` pin (always available -- no repo_root needed) plus the
    lockfile/toolchain blob hashes "when present" (round-3A brief, Part
    1). `repo_root=None` (e.g. a unit test that only has a `CiToml`, no
    checkout) simply omits the file-hash components -- the digest is then
    only as strong as `linter`, which keeps `compute_plan` callable
    without a repo_root, matching every pre-round-3A call site."""

    fingerprint: dict[str, JsonValue] = {"linter": ci.linter}
    if repo_root is not None:
        for name in _ENV_FINGERPRINT_FILES:
            digest = _sha256_file(repo_root / name)
            if digest is not None:
                fingerprint[name] = digest
    return fingerprint


def _lane_digest(inputs: dict[str, JsonValue]) -> str:
    """sha256 over the canonical (sorted-key, compact) JSON of `inputs`,
    truncated to 12 hex characters (round-3A brief, Part 1: `<12 hex>`,
    matching the template's planned `fast [a1b2c3d4e5f6]` job-name
    convention)."""

    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _compute_lane_digests(
    ci: CiToml,
    *,
    fast_platform_ids: list[str],
    fast_suites: tuple[str, ...],
    dylint_targets: tuple[str, ...],
    platform_lanes: tuple[PlatformLane, ...],
    env: dict[str, JsonValue],
) -> LaneDigests:
    digests: dict[str, str] = {}

    fast_platforms_payload: list[JsonValue] = [
        {"id": pid, "target": ci.platforms[pid].target} for pid in sorted(fast_platform_ids)
    ]
    digests["fast"] = _lane_digest(
        {"platforms": fast_platforms_payload, "suites": sorted(fast_suites), "env": env}
    )

    digests["dylint"] = _lane_digest({"targets": sorted(dylint_targets), "env": env})

    for lane in platform_lanes:
        digests[f"platform:{lane.id}"] = _lane_digest(
            {
                "id": lane.id,
                "target": lane.target,
                "runs_on": lane.runs_on,
                "suites": sorted(lane.suites),
                "wheel": lane.wheel,
                "env": env,
            }
        )

    return LaneDigests(tuple(LaneDigest(lane, digest) for lane, digest in digests.items()))


def _resolve_publish_mode(ci: CiToml, flow_publish: str | None) -> str:
    if flow_publish is None:
        return "none"
    if flow_publish == "rehearsal":
        return "rehearsal"
    if flow_publish == "pypi":
        return ci.publish.pypi.mode if ci.publish.pypi is not None else "none"
    return flow_publish


def compute_plan(  # noqa: C901
    ci: CiToml,
    *,
    event_name: str,
    title: str = "",
    ref: str | None = None,
    dispatch_sha: str | None = None,
    repo_root: Path | None = None,
) -> Plan:
    if event_name not in EVENT_TO_FLOW:
        raise PlanError(f"unsupported event '{event_name}'; expected one of {sorted(EVENT_TO_FLOW)}")

    flow_id = EVENT_TO_FLOW[event_name]
    if event_name == "merge_group" and QUEUE_FLOW in ci.flows:
        flow_id = QUEUE_FLOW
    reasons: list[str] = [f"event '{event_name}' selects base flow '{flow_id}'"]

    tokens: tuple[str, ...] = ()
    if event_name == "pull_request":
        tokens = tuple(extract_bracket_tokens(title))

    matched_declared: list[str] = [t for t in tokens if t in ci.tags]

    # A [release] tag switches the flow itself, before base resolution.
    for tid in matched_declared:
        rule = ci.tags[tid]
        if rule.flow is not None:
            if rule.flow not in ci.flows:
                raise PlanError(f"tag '[{tid}]' points at unknown flow '{rule.flow}'")
            flow_id = rule.flow
            reasons.append(f"tag '[{tid}]' switches flow to '{flow_id}'")

    has_release = "release" in tokens
    has_no_test = any(t == "no-test" or t.startswith("no-test-") for t in tokens)
    if has_release and has_no_test:
        raise PlanError("'[release]' combined with a '[no-test*]' tag is not a valid selection (TAG-002)")

    resolved = resolve_flow(ci, flow_id)
    platforms = _expand_platforms(ci, resolved.platforms)
    suites = _expand_suites(ci, resolved.suites)
    reasons.append(
        f"flow '{flow_id}' base: platforms={sorted(platforms)}, suites={sorted(suites)}"
    )

    cache_mode = "write" if resolved.cache == "write" else "read"
    flow_publish = resolved.publish
    mergeable = True

    platform_ids = set(ci.platforms)
    suite_ids = set(ci.suites)
    groups = set(platform_groups(ci))

    for token in tokens:
        if token in ci.tags:
            rule = ci.tags[token]
            if rule.add is not None:
                if rule.add.platforms is not None:
                    added = _expand_platforms(ci, rule.add.platforms)
                    platforms |= added
                    reasons.append(f"tag '[{token}]' adds platforms {sorted(added)}")
                if rule.add.suites is not None:
                    added_s = _expand_suites(ci, rule.add.suites)
                    suites |= added_s
                    reasons.append(f"tag '[{token}]' adds suites {sorted(added_s)}")
            if rule.remove is not None:
                if rule.remove.suites == "tests":
                    # "tests" removes suites of test kind (default kind, e.g.
                    # unit/smoke/integration/init) but never a non-test suite
                    # such as a `kind = "bench"` perf suite -- so
                    # `[ci-perf][no-test]` still runs perf regardless of tag
                    # order in the title (ci.yml#46: the perf loop must stay
                    # build+perf only, not empty).
                    test_suite_ids = {
                        sid for sid, s in ci.suites.items() if s.kind != "bench"
                    }
                    removed_tests = suites & test_suite_ids
                    reasons.append(
                        f"tag '[{token}]' removes test suites {sorted(removed_tests)}"
                    )
                    suites -= test_suite_ids
                elif rule.remove.suites is not None:
                    removed = _expand_suites(ci, rule.remove.suites)
                    suites -= removed
                    reasons.append(f"tag '[{token}]' removes suites {sorted(removed)}")
                mergeable = False
            if rule.cache is not None:
                reasons.append(f"tag '[{token}]' sets cache mode hint '{rule.cache}'")
            if rule.publish is not None:
                flow_publish = rule.publish
                reasons.append(f"tag '[{token}]' sets publish '{rule.publish}'")
            continue

        # Derived tags not declared in [tags]: platform id, platform group,
        # ci-test-<suite>, no-test-<suite>, ci-perf-<group>.
        if token.startswith("ci-"):
            rest = token[len("ci-"):]
            if rest in platform_ids:
                platforms.add(rest)
                reasons.append(f"tag '[{token}]' adds platform '{rest}'")
                continue
            if rest in groups:
                added = {p for p, pl in ci.platforms.items() if pl.group == rest}
                platforms |= added
                reasons.append(f"tag '[{token}]' adds platform group '{rest}': {sorted(added)}")
                continue
            if rest.startswith("test-"):
                sid = rest[len("test-"):]
                if sid in suite_ids:
                    suites.add(sid)
                    reasons.append(f"tag '[{token}]' adds suite '{sid}'")
                    continue
            if rest.startswith("perf-"):
                if "perf" in suite_ids:
                    suites.add("perf")
                    reasons.append(f"tag '[{token}]' adds suite 'perf' (derived {token})")
                    continue
        if token.startswith("no-test-"):
            sid = token[len("no-test-"):]
            if sid in suite_ids:
                suites.discard(sid)
                mergeable = False
                reasons.append(f"tag '[{token}]' removes suite '{sid}'")
                continue
        # Unknown reserved tags are reported by precheck (TAG-001); the
        # planner ignores them rather than guessing.

    publish = _resolve_publish_mode(ci, flow_publish)

    # Dylint checks every DECLARED platform from one Linux job regardless of
    # which platforms this run actually builds (issue #6 §2/§3: "Dylint for
    # every declared platform from one Linux job") -- it is a cheap
    # check-only pass, not a full cross-build, so it is never narrowed to
    # the tag-selected `platforms` subset the way real build lanes are
    # (round-2A brief, part 2f).
    dylint_targets: tuple[str, ...] = ()
    if resolved.dylint == "all-platforms" or (ci.lint_dylint is not None and ci.lint_dylint.targets == "all-platforms"):
        dylint_targets = tuple(sorted(ci.platforms))

    if event_name == "workflow_dispatch":
        if dispatch_sha is None or not DISPATCH_SHA_RE.match(dispatch_sha):
            raise PlanError("workflow_dispatch requires a 40-hex-character 'sha' input")
        reasons.append(f"workflow_dispatch pinned to sha={dispatch_sha}")

    plan_platforms = tuple(
        PlanPlatform(id=pid, target=ci.platforms[pid].target, runs_on=ci.platforms[pid].runs_on, group=ci.platforms[pid].group)
        for pid in sorted(platforms)
    )

    selection_repr = json.dumps(
        {
            "flow": flow_id,
            "tags": sorted(tokens),
            "platforms": sorted(platforms),
            "suites": sorted(suites),
            "cache_mode": cache_mode,
            "publish": publish,
            "mergeable": mergeable,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(selection_repr.encode("utf-8")).hexdigest()

    default_fast_platforms = _default_fast_lane_platforms(ci)
    needs_platform_lanes = bool(platforms - default_fast_platforms)
    final_suites = tuple(sorted(suites))

    # Round-3A brief, Part 1: every selected platform EXCEPT the flow's
    # default fast-lane platform(s) -- empty exactly when
    # needs_platform_lanes is false, since both are derived from the same
    # `platforms - default_fast_platforms` set.
    platform_lanes = tuple(
        PlatformLane(
            id=p.id,
            target=p.target,
            runs_on=p.runs_on,
            group=p.group,
            wheel=ci.platforms[p.id].wheel,
            suites=final_suites,
        )
        for p in plan_platforms
        if p.id not in default_fast_platforms
    )

    env_fingerprint = _env_fingerprint(ci, repo_root)
    lane_digests = _compute_lane_digests(
        ci,
        fast_platform_ids=sorted(default_fast_platforms & platforms),
        fast_suites=final_suites,
        dylint_targets=dylint_targets,
        platform_lanes=platform_lanes,
        env=env_fingerprint,
    )

    return Plan(
        flow=flow_id,
        event_name=event_name,
        tags=tokens,
        platforms=plan_platforms,
        suites=final_suites,
        dylint_targets=dylint_targets,
        cache_mode=cache_mode,
        publish=publish,
        mergeable=mergeable,
        digest=digest,
        reasons=tuple(reasons),
        required_jobs=_required_job_ids(needs_platform_lanes, dylint_targets, final_suites, publish),
        needs_platform_lanes=needs_platform_lanes,
        dispatch_sha=dispatch_sha,
        platform_lanes=platform_lanes,
        fast_suites=final_suites,
        lane_digests=lane_digests,
    )


def build_act_event(title: str) -> dict[str, JsonValue]:
    """A minimal `pull_request` act event JSON carrying the title (bosn -> act, §11)."""

    return {
        "pull_request": {
            "title": title,
            "number": 1,
            "base": {"ref": "main"},
            "head": {"ref": "act-local"},
        },
        "action": "synchronize",
    }
