"""Title-edit reuse: which lanes already have a green job for the identical
lane digest on the identical PR head SHA (round-3A brief, Part 2).

Security note (also in docs/ci-toml.md): reuse only ever *skips* work that
was already proven green for the *identical* digest on the *identical*
head SHA. It is looked up fresh on every `plan --reuse` run, scoped to the
GitHub API's own `head_sha=` filter, and (`ci_lint.runtime.gate`, Part 3)
re-verified live before a gate ever treats a skipped required job as a
pass. It never lets an older or different commit's result stand in for
this one, and never lets a lane with a different digest (different
inputs) stand in for this lane's.

Only ever consulted for the `pull_request` event -- never `push`,
`schedule`, or `workflow_dispatch` (round-3A brief, Part 2: "Never reuse
on push/schedule/dispatch"). Verified reuse on default-branch pushes
(GEN-021) is a separate mechanism with its own proof rules:
`ci_lint.default_branch_reuse` (`ci-lint reuse-check`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import FetchFn, GitHubApiError

# A previous run's job counts as covering this workflow only if its
# `path` (the workflow file GitHub attributes the run to) ends with this --
# matches `ci.yml`'s own `.github/workflows/ci.yml` regardless of which
# repository checkout this runs from.
WORKFLOW_PATH_SUFFIX = ".github/workflows/ci.yml"

_RUNS_PER_PAGE = 50
_JOBS_PER_PAGE = 100


@dataclass(frozen=True)
class ReuseEntry:
    run_id: int
    job_id: int
    html_url: str

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {"run_id": self.run_id, "job_id": self.job_id, "html_url": self.html_url}


@dataclass(frozen=True)
class LaneReuse:
    lane: str
    entry: ReuseEntry | None


@dataclass(frozen=True)
class ReuseMap(Mapping[str, "ReuseEntry | None"]):
    """Read-only lane -> `ReuseEntry | None`, backed by `LaneReuse` records."""

    entries: tuple[LaneReuse, ...] = ()

    @classmethod
    def of(cls, reuse: Mapping[str, ReuseEntry | None]) -> ReuseMap:
        return cls(tuple(LaneReuse(lane, entry) for lane, entry in reuse.items()))

    def __getitem__(self, lane: str) -> ReuseEntry | None:
        for item in self.entries:
            if item.lane == lane:
                return item.entry
        raise KeyError(lane)

    def __iter__(self) -> Iterator[str]:
        return (item.lane for item in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ReuseResult:
    """`reuse` maps every lane key ci_lint knows about ("fast", "dylint",
    "platform:<id>") to a `ReuseEntry` (a proven-green previous job for
    that lane's exact digest) or `None`. `warning` is set (and the whole
    result degrades to "reuse nothing") on any GitHub API error -- never
    raised, per this module's docstring."""

    reuse: ReuseMap
    warning: str | None

    def to_json_dict(self) -> dict[str, JsonValue]:
        return {lane: (entry.to_json_dict() if entry is not None else None) for lane, entry in self.reuse.items()}


def empty_result(lane_digests: dict[str, str], *, warning: str | None = None) -> ReuseResult:
    return ReuseResult(reuse=ReuseMap.of({lane: None for lane in lane_digests}), warning=warning)


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def compute_reuse(  # noqa: C901
    *,
    repo: str,
    head_sha: str,
    current_run_id: str | None,
    lane_digests: dict[str, str],
    fetch: FetchFn,
    token: str,
) -> ReuseResult:
    """Never raises: any API error yields `empty_result(..., warning=...)`
    (round-3A brief, Part 2: "On any API error: reuse nothing and print a
    warning; never fail the precheck because the API is down")."""

    if not lane_digests:
        return empty_result(lane_digests)

    try:
        runs_payload = fetch(
            f"https://api.github.com/repos/{repo}/actions/runs"
            f"?head_sha={head_sha}&event=pull_request&per_page={_RUNS_PER_PAGE}",
            token,
        )
    except GitHubApiError as exc:
        return empty_result(lane_digests, warning=f"reuse: {exc}")

    runs = _as_list(_as_dict(runs_payload).get("workflow_runs"))
    candidates: list[dict[str, JsonValue]] = []
    for raw in runs:
        run = _as_dict(raw)
        path = run.get("path")
        run_id = run.get("id")
        if not isinstance(path, str) or not path.endswith(WORKFLOW_PATH_SUFFIX):
            continue
        if not isinstance(run_id, int):
            continue
        if current_run_id is not None and str(run_id) == str(current_run_id):
            continue
        candidates.append(run)

    # Newest first. The API itself returns runs newest-first, but sort
    # defensively (by run id, which is monotonically increasing) so a test
    # fixture or a future paginated fetch doesn't have to guarantee order.
    candidates.sort(key=lambda r: r["id"] if isinstance(r.get("id"), int) else 0, reverse=True)  # type: ignore[index]

    reuse: dict[str, ReuseEntry | None] = {lane: None for lane in lane_digests}
    remaining = set(lane_digests)
    warning: str | None = None

    for run in candidates:
        if not remaining:
            break
        run_id_val = run.get("id")
        if not isinstance(run_id_val, int):
            continue
        try:
            jobs_payload = fetch(
                f"https://api.github.com/repos/{repo}/actions/runs/{run_id_val}/jobs?per_page={_JOBS_PER_PAGE}",
                token,
            )
        except GitHubApiError as exc:
            warning = f"reuse: {exc}"
            continue

        for raw_job in _as_list(_as_dict(jobs_payload).get("jobs")):
            if not remaining:
                break
            job = _as_dict(raw_job)
            name = job.get("name")
            conclusion = job.get("conclusion")
            job_id = job.get("id")
            html_url = job.get("html_url")
            if not isinstance(name, str) or conclusion != "success" or not isinstance(job_id, int):
                continue
            for lane in list(remaining):
                digest = lane_digests[lane]
                if f"[{digest}]" in name:
                    reuse[lane] = ReuseEntry(
                        run_id=run_id_val,
                        job_id=job_id,
                        html_url=html_url if isinstance(html_url, str) else "",
                    )
                    remaining.discard(lane)

    return ReuseResult(reuse=ReuseMap.of(reuse), warning=warning)
