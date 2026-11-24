#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Route a job to the shared self-hosted pool when it has idle capacity.

Ported and generalized from obs-rust/obs-studio `ci/select_mq_runner.py`.
Capacity comes from one of two sources:

- `signal` (the default with no token): the tokenless read-only pool status
  `GET https://signal.forge-ci.com/v1/pool?org=<owner>` (schema
  `forge-ci.pool-status` version 1, forge-ci/signal.forge-ci.com
  docs/droplet.md), which lists each org's online runners as label sets
  and, in `dispatcher`, the Dell dispatchers' room to start runners
  (forge-ci/gh-runner#67);
- `github` (the default when a token is given): `GET /orgs/{owner}/actions/runners`
  with that token.

Capacity is idle matching runners plus, with the signal source and
`count-room` on (the default), the dispatchers' `spare` room for lanes whose
labels carry every wanted label: a job routed there queues, signal sees it,
and the host's dispatcher starts a runner for it (a lane at floor 0 has no
idle runner but is still selectable). A dispatcher entry older than 180 s or
malformed counts as no room.

Every unknown state selects the hosted fallback: a missing token, an
untrusted event, a fork pull request, an API or status error or timeout,
malformed or stale data, or too little idle capacity. A pool outage never
blocks CI.

Only fixed diagnostics and counts are printed, never API text, exception
messages or tokens. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"

MAX_PAGES = 5
PER_PAGE = 100
API_TIMEOUT = 3
DEFAULT_FALLBACK = "ubuntu-latest"
# Events whose code is the repository's own. A pull_request qualifies only
# when its head repository is the base repository (not a fork).
TRUSTED_EVENTS = frozenset({"push", "merge_group", "workflow_dispatch", "schedule"})
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
MODES = frozenset({"", "idle", "prefer"})
SOURCES = frozenset({"", "auto", "signal", "github"})
DEFAULT_STATUS_URL = "https://signal.forge-ci.com/v1/pool"
LEDGER_URL = "https://signal.forge-ci.com/v1/lease"
LEDGER_AUDIENCE = "signal.forge-ci.com"
LEASE_WAIT_SECONDS = 45
LEASE_ID = re.compile(r"[0-9a-f]{24}")
STATUS_SCHEMA = "forge-ci.pool-status"
STATUS_VERSION = 1
# Oldest status data the selector accepts, whatever the server's max_age_s says.
STATUS_MAX_AGE_S = 30
# Oldest dispatcher report whose room counts (signal drops older ones too).
DISPATCHER_MAX_AGE_S = 180

# fetch(url, token) -> parsed JSON page. Injected so tests never touch the network.
Fetch: TypeAlias = Callable[[str, str], "JsonValue"]


class ProbeError(Exception):
    """A fixed, credential-free reason for falling back to hosted runners."""


@dataclass(frozen=True)
class Settings:
    labels: str
    fallback: str
    token: str
    min_idle: str
    mode: str
    trusted_only: str
    event: str
    event_path: str
    repository: str
    owner: str
    api_url: str
    source: str = ""
    status_url: str = ""
    count_room: str = "true"
    lease_class: str = ""
    lease_job: str = ""
    run_id: str = ""
    run_attempt: str = ""
    oidc_url: str = ""
    oidc_token: str = ""


@dataclass(frozen=True)
class Decision:
    runs_on: str  # compact JSON value for fromJSON()
    pool: bool
    reason: str


@dataclass(frozen=True)
class RunnerState:
    runner_id: int
    busy: bool


@dataclass(frozen=True)
class RunnersPage:
    runners: list[JsonValue]
    total: int


@dataclass(frozen=True)
class StatusGroup:
    """One label set from the pool status: how many such runners are online and busy."""

    labels: frozenset[str]
    online: int
    busy: int


@dataclass(frozen=True)
class Capacity:
    online: int
    busy: int
    room: int = 0  # runners the dispatchers would start (signal `dispatcher` spare)

    @property
    def idle(self) -> int:
        return self.online - self.busy


def warning(message: str) -> None:
    print(f"::warning::pool-select: {message}")


def compact(value: JsonValue) -> str:
    return json.dumps(value, separators=(",", ":"))


def parse_labels(raw: str) -> list[str]:
    """An empty input (or `[]`) means hosted; anything else must be a string array."""
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ProbeError("labels input is not valid JSON") from error
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProbeError("labels input must be a JSON array of nonempty strings")
    return [item.strip() for item in value]


def fallback_json(raw: str) -> str:
    """The fallback as JSON: a nonempty string array, or a single label string."""
    text = raw.strip() or DEFAULT_FALLBACK
    if not text.startswith("["):
        return compact(text)
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return compact(value)
    warning(f"fallback input is not a nonempty JSON string array; using {DEFAULT_FALLBACK}")
    return compact(DEFAULT_FALLBACK)


def required_idle(min_idle: str, mode: str) -> int:
    """Idle runners required; prefer mode needs any one matching online runner."""
    if mode not in MODES:
        raise ProbeError("unknown mode (expected empty, 'idle' or 'prefer')")
    if mode == "prefer":
        return 1
    try:
        minimum = int(min_idle.strip() or "1")
    except ValueError as error:
        raise ProbeError("min-idle is not an integer") from error
    if minimum < 1:
        raise ProbeError("min-idle must be at least 1")
    return minimum


def is_trusted_only(raw: str) -> bool:
    """Only an explicit false disables the trust check; anything else keeps it."""
    return raw.strip().casefold() not in {"false", "0", "no", "off"}


def head_repository(event_path: str) -> str:
    """The pull request's head repository, or "" when unknown (a deleted fork)."""
    try:
        with Path(event_path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        name = payload["pull_request"]["head"]["repo"]["full_name"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return name if isinstance(name, str) else ""


def untrusted_reason(settings: Settings) -> str:
    """Why this event may not use the pool, or "" when it may."""
    if not is_trusted_only(settings.trusted_only) or settings.event in TRUSTED_EVENTS:
        return ""
    if settings.event == "pull_request":
        head = head_repository(settings.event_path)
        if head and head.casefold() == settings.repository.casefold():
            return ""
        return "pull request from a fork (or an unknown head repository)"
    if settings.event in PR_EVENTS:
        return f"{settings.event} is never routed to the pool"
    return "event is not a trusted pool event"


def runner_labels(runner: dict[str, JsonValue]) -> set[str]:
    labels = runner.get("labels")
    if not isinstance(labels, list):
        raise ProbeError("malformed runner data")
    names = set()
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else None
        if not isinstance(name, str):
            raise ProbeError("malformed runner data")
        names.add(name.casefold())
    return names


def matching_runner(runner: JsonValue, wanted: set[str]) -> RunnerState | None:
    """The state of an online runner carrying every wanted label, else None."""
    if not isinstance(runner, dict):
        raise ProbeError("malformed runner data")
    runner_id, busy, status = runner.get("id"), runner.get("busy"), runner.get("status")
    if type(runner_id) is not int or type(busy) is not bool or not isinstance(status, str):
        raise ProbeError("malformed runner data")
    if status != "online" or not wanted.issubset(runner_labels(runner)):
        return None
    return RunnerState(runner_id, busy)


def page_runners(page: JsonValue) -> RunnersPage:
    if not isinstance(page, dict):
        raise ProbeError("malformed runners response")
    runners, total = page.get("runners"), page.get("total_count")
    if not isinstance(runners, list) or type(total) is not int:
        raise ProbeError("malformed runners response")
    return RunnersPage(runners, total)


def count_capacity(fetch: Fetch, settings: Settings, wanted: set[str]) -> Capacity:
    """Count matching online runners and how many are busy, over bounded pages."""
    base = f"{settings.api_url.rstrip('/')}/orgs/{settings.owner}/actions/runners?per_page={PER_PAGE}"
    matched: dict[int, bool] = {}
    for number in range(1, MAX_PAGES + 1):
        page = page_runners(fetch(f"{base}&page={number}", settings.token))
        for runner in page.runners:
            found = matching_runner(runner, wanted)
            if found is not None:
                # A runner seen twice (pages shifting) is busy if either view says so.
                matched[found.runner_id] = matched.get(found.runner_id, False) or found.busy
        if len(page.runners) < PER_PAGE or number * PER_PAGE >= page.total:
            break
    return Capacity(online=len(matched), busy=sum(matched.values()))


def resolve_source(settings: Settings) -> str:
    """`signal` or `github`. Empty/`auto` means github with a token, signal without."""
    source = settings.source.strip().casefold()
    if source not in SOURCES:
        raise ProbeError("unknown source (expected empty, 'auto', 'signal' or 'github')")
    if source in {"", "auto"}:
        return "github" if settings.token.strip() else "signal"
    return source


def status_url(settings: Settings) -> str:
    base = settings.status_url.strip() or DEFAULT_STATUS_URL
    if not base.startswith("https://"):
        raise ProbeError("status-url must be an https URL")
    return f"{base}{'&' if '?' in base else '?'}org={quote(settings.owner, safe='')}"


def _count(value: JsonValue) -> int:
    if type(value) is not int or value < 0:
        raise ProbeError("malformed pool status")
    return value


def status_group(value: JsonValue) -> StatusGroup:
    if not isinstance(value, dict):
        raise ProbeError("malformed pool status")
    labels, online, busy = value.get("labels"), _count(value.get("online")), _count(value.get("busy"))
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels) or busy > online:
        raise ProbeError("malformed pool status")
    return StatusGroup(frozenset(str(label).casefold() for label in labels), online, busy)


def org_entry(document: JsonValue, owner: str) -> dict[str, JsonValue]:
    """The owner's fresh `ok` entry of a v1 pool status, or a fixed reason to run hosted."""
    if not isinstance(document, dict) or document.get("schema") != STATUS_SCHEMA:
        raise ProbeError("pool status has an unknown schema")
    if document.get("version") != STATUS_VERSION:
        raise ProbeError("pool status has an unsupported version")
    orgs = document.get("orgs")
    if not isinstance(orgs, dict):
        raise ProbeError("malformed pool status")
    entry = next((v for k, v in orgs.items() if k.casefold() == owner.casefold()), None)
    if not isinstance(entry, dict):
        raise ProbeError("pool status has no entry for this organization")
    if entry.get("state") != "ok":
        raise ProbeError("pool status reports an error for this organization")
    age, server_max = entry.get("age_s"), document.get("max_age_s")
    limit = min(STATUS_MAX_AGE_S, server_max) if type(server_max) is int else STATUS_MAX_AGE_S
    if not isinstance(age, (int, float)) or isinstance(age, bool) or not 0 <= age <= limit:
        raise ProbeError("pool status is stale")
    return entry


def org_status(document: JsonValue, owner: str) -> list[StatusGroup]:
    """The owner's runner groups from a v1 pool status, or a fixed reason to run hosted."""
    groups = org_entry(document, owner).get("runners")
    if not isinstance(groups, list):
        raise ProbeError("malformed pool status")
    return [status_group(group) for group in groups]


def dispatcher_room(entry: dict[str, JsonValue], wanted: set[str]) -> int:
    """`spare` of every fresh dispatcher lane carrying all wanted labels.

    Lenient: a missing, null or malformed `dispatcher` (or host entry) adds no
    room, it never fails the selection; idle runners still count.
    """
    disp = entry.get("dispatcher")
    hosts = disp.get("hosts") if isinstance(disp, dict) else None
    if not isinstance(hosts, list):
        return 0
    room = 0
    for host in hosts:
        if not isinstance(host, dict):
            continue
        labels, spare, age = host.get("labels"), host.get("spare"), host.get("age_s")
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            continue
        if type(spare) is not int or spare < 0:
            continue
        if not isinstance(age, (int, float)) or isinstance(age, bool) or not 0 <= age <= DISPATCHER_MAX_AGE_S:
            continue
        if wanted.issubset({str(label).casefold() for label in labels}):
            room += spare
    return room


def counts_room(raw: str) -> bool:
    """Only an explicit false turns the dispatcher room off."""
    return raw.strip().casefold() not in {"false", "0", "no", "off"}


def signal_capacity(fetch: Fetch, settings: Settings, wanted: set[str]) -> Capacity:
    """Sum the online and busy counts of every label set carrying all wanted
    labels, plus the dispatchers' spare room for them. No token is sent."""
    entry = org_entry(fetch(status_url(settings), ""), settings.owner)
    groups = entry.get("runners")
    if not isinstance(groups, list):
        raise ProbeError("malformed pool status")
    matched = [g for g in (status_group(group) for group in groups) if wanted.issubset(g.labels)]
    room = dispatcher_room(entry, wanted) if counts_room(settings.count_room) else 0
    return Capacity(online=sum(g.online for g in matched), busy=sum(g.busy for g in matched), room=room)


def describe_error(error: Exception) -> str:
    """Error class and HTTP status only: messages may carry server text or secrets."""
    if isinstance(error, ProbeError):
        return str(error)
    status = f" {error.code}" if isinstance(error, HTTPError) else ""
    return f"runner availability probe failed ({type(error).__name__}{status})"


def probe(fetch: Fetch, settings: Settings, labels: list[str], fallback: str) -> Decision:
    required = required_idle(settings.min_idle, settings.mode)
    source = resolve_source(settings)
    if source == "github" and not settings.token.strip():
        warning("no token; using hosted runners")
        return Decision(fallback, False, "no runner probe token")
    if not settings.owner:
        raise ProbeError("repository owner is unknown")
    wanted = {label.casefold() for label in labels}
    if source == "signal":
        capacity = signal_capacity(fetch, settings, wanted)
    else:
        capacity = count_capacity(fetch, settings, wanted)
    available = (capacity.online if settings.mode == "prefer" else capacity.idle) + capacity.room
    counts = (
        f"source={source}, online={capacity.online}, busy={capacity.busy}, idle={capacity.idle}, "
        f"required={required}, room={capacity.room}"
    )
    if available >= required:
        if settings.mode == "prefer":
            what = "matching online runner (prefer mode)"
        elif capacity.idle >= required:
            what = "idle pool capacity"
        else:
            what = "pool capacity (a dispatcher starts the runner)"
        return Decision(compact(labels), True, f"{what} ({counts})")
    return Decision(fallback, False, f"not enough pool capacity ({counts})")


def select(settings: Settings, fetch: Fetch) -> Decision:
    """Return the runs-on decision; never raises."""
    fallback = fallback_json(settings.fallback)
    try:
        labels = parse_labels(settings.labels)
        if not labels:
            return Decision(fallback, False, "no pool labels configured")
        reason = untrusted_reason(settings)
        if reason:
            return Decision(fallback, False, reason)
        if settings.source.strip().casefold() == "lease":
            return reserve_runner(settings, fetch, fallback)
        return probe(fetch, settings, labels, fallback)
    except Exception as error:  # noqa: BLE001 -- every failure falls back to hosted
        reason = describe_error(error)
        warning(f"{reason}; using hosted runners")
        return Decision(fallback, False, reason)


def github_fetch(url: str, token: str) -> JsonValue:
    """GET JSON. The pool status is read with no token: no Authorization header at all."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "forge-ci-pool-select",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310 -- https only (status_url checks)
        return json.load(response)


def lease_post(url: str, token: str, body: dict[str, JsonValue], timeout: float) -> JsonValue:
    request = Request(url, data=compact(body).encode(), headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "forge-ci-pool-select",
    }, method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- constant ledger origin
        return json.loads(response.read(65_536))


def lease_identity(settings: Settings, fetch: Fetch) -> str:
    oidc = urlsplit(settings.oidc_url)
    if (oidc.scheme != "https" or not (oidc.hostname or "").endswith(".actions.githubusercontent.com")
            or oidc.username is not None or oidc.password is not None):
        raise ProbeError("invalid GitHub OIDC endpoint")
    identity = fetch(settings.oidc_url + ("&" if oidc.query else "?") + f"audience={LEDGER_AUDIENCE}",
                     settings.oidc_token)
    token = identity.get("value") if isinstance(identity, dict) else None
    if not isinstance(token, str) or not token:
        raise ProbeError("GitHub OIDC response did not contain an identity")
    return token


def reserve_runner(
    settings: Settings,
    fetch: Fetch,
    fallback: str,
    post: Callable[[str, str, dict[str, JsonValue], float], JsonValue] = lease_post,
    clock: Callable[[], float] = time.monotonic,
    delay: Callable[[float], None] = time.sleep,
) -> Decision:
    if not settings.lease_class or not settings.lease_job or not settings.oidc_token:
        return Decision(fallback, False, "lease inputs or GitHub OIDC permission missing")
    body: dict[str, JsonValue] = {
        "class": settings.lease_class,
        "job": settings.lease_job,
        "run_id": int(settings.run_id),
        "run_attempt": int(settings.run_attempt),
        "labels": [label.casefold() for label in parse_labels(settings.labels)
                   if label.casefold() not in {"self-hosted", "forge-pool"}],
    }
    token = lease_identity(settings, fetch)
    expires = clock() + LEASE_WAIT_SECONDS
    pending = None
    try:
        while (remaining := expires - clock()) > 0:
            response = post(LEDGER_URL, token, body, min(API_TIMEOUT, remaining))
            if not isinstance(response, dict):
                raise ProbeError("invalid lease response")
            lease_id = response.get("lease_id")
            if isinstance(lease_id, str) and LEASE_ID.fullmatch(lease_id):
                pending = lease_id
            if response.get("grant") is True:
                labels = response.get("runs_on")
                if not pending or labels != ["self-hosted", f"lease-{pending}"]:
                    raise ProbeError("invalid reserved runner labels")
                pending = None
                return Decision(compact(labels), True, "reserved runner is online")
            if response.get("reason") != "runner_starting":
                return Decision(fallback, False, "ledger declined the reservation")
            delay(min(3, max(0, expires - clock())))
        return Decision(fallback, False, "reserved runner did not come online in time")
    finally:
        if pending is not None:
            try:
                post(f"{LEDGER_URL}/{pending}/release", token, {}, API_TIMEOUT)
            except (OSError, ValueError):
                warning("could not release pending reservation")


def settings_from_env(env: dict[str, str]) -> Settings:
    repository = env.get("GITHUB_REPOSITORY", "")
    return Settings(
        labels=env.get("POOL_LABELS", ""),
        fallback=env.get("POOL_FALLBACK", ""),
        token=env.get("POOL_TOKEN", ""),
        min_idle=env.get("POOL_MIN_IDLE", ""),
        mode=env.get("POOL_MODE", "").strip().casefold(),
        trusted_only=env.get("POOL_TRUSTED_ONLY", "true"),
        event=env.get("GITHUB_EVENT_NAME", ""),
        event_path=env.get("GITHUB_EVENT_PATH", ""),
        repository=repository,
        owner=env.get("GITHUB_REPOSITORY_OWNER", "") or repository.partition("/")[0],
        api_url=env.get("GITHUB_API_URL", "") or "https://api.github.com",
        source=env.get("POOL_SOURCE", ""),
        status_url=env.get("POOL_STATUS_URL", ""),
        count_room=env.get("POOL_COUNT_ROOM", "true"),
        lease_class=env.get("POOL_LEASE_CLASS", "").strip(),
        lease_job=env.get("POOL_LEASE_JOB", "").strip() or env.get("GITHUB_JOB", ""),
        run_id=env.get("GITHUB_RUN_ID", ""),
        run_attempt=env.get("GITHUB_RUN_ATTEMPT", ""),
        oidc_url=env.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
        oidc_token=env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
    )


def append(path: str, text: str) -> bool:
    if not path:
        return False
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        return False
    return True


def publish(decision: Decision, env: dict[str, str]) -> int:
    selected = "self-hosted pool" if decision.pool else "hosted"
    print(f"pool-select: {selected}: {decision.reason}")
    output = f"runs-on={decision.runs_on}\npool={'true' if decision.pool else 'false'}\n"
    if not append(env.get("GITHUB_OUTPUT", ""), output):
        warning("could not write step outputs")
        return 1
    summary = f"### Runner pool selection\n\nSelected **{selected}** (`{decision.runs_on}`): {decision.reason}.\n"
    if not append(env.get("GITHUB_STEP_SUMMARY", ""), summary):
        warning("could not write the step summary")
    return 0


def main() -> int:
    env = dict(os.environ)
    return publish(select(settings_from_env(env), github_fetch), env)


if __name__ == "__main__":
    raise SystemExit(main())
