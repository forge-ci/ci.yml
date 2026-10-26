"""`ci-lint audit`: live, read-only fleet settings audit -- issue #6 §7
(secrets & publishing) plus GEN-006/GEN-011 (branch-protection/merge-queue
and required-check-name gaps identified in docs/case-studies/fbuild-ci-
cost.md and the round-5 brief). Distinct from `ci_lint cache audit` (which
classifies GitHub Actions cache entries); this module never touches the
cache API.

Every check here is read-only (GET only, never PATCH/POST/DELETE) and every
call goes through the injectable `FetchStatusFn` (`ci_lint.github_api
.default_fetch_status`), so no test in this package ever touches the
network -- the round-5 brief's "no network in tests (recorded fixtures)".

Rules implemented:
  - SEC-005: any repository Actions secret or environment secret exists.
    GET .../actions/secrets and .../environments/{name}/secrets both need
    an admin token; a 403 is reported `needs_review` ("requires an admin
    token"), never treated as a pass.
  - SEC-006: the environment named in ci.toml's [publish].pypi.environment
    is missing, or its deployment branch policy doesn't restrict deploys to
    the default branch.
  - SEC-007: the repository's default Actions workflow permissions are not
    read-only.
  - GEN-006: `required_status_checks.strict` is true on the default branch
    with no merge queue configured (rulesets + classic branch protection;
    a 404 on either is treated as "none").
  - GEN-022 (live half, #432): no active merge-queue ruleset covers the
    default branch (violation), or a queue is active with no required
    status check (needs_review). Reuses the GEN-006 payloads.
  - GEN-011: the default branch's required status checks don't include the
    gate check name (default "CI OK"); reported `needs_review` (a policy
    decision, not a code defect) when branch protection itself is absent.
  - GATE-012 (live half, ci.yml#206): a required status check (branch
    protection or an active ruleset) names a GitHub App check or is bound
    to an app other than GitHub Actions -- act cannot run it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.cargo_messages import JsonValue
from ci_lint.github_api import FetchStatusFn, GitHubApiError
from ci_lint.remote_only import check_required_checks as check_gate_012_required
from ci_lint.rules.doc_claims import DocClaim, scan_repo_docs
from ci_lint.rules.setup_soldr_freshness import check_rust_014
from ci_lint.rules.status_badges import check_gen_026
from ci_lint.schema import CiToml
from ci_lint.rules.linter_pin import check_ct_007

API_ROOT = "https://api.github.com"
DEFAULT_GATE_CHECK_NAME = "CI OK"

NEEDS_ADMIN_FIX = (
    "re-run with a token that has repository admin scope (Settings -> Secrets/Environments read "
    "access) to actually complete this check -- a 403 here is never treated as a pass"
)


@dataclass(frozen=True)
class AuditReport:
    findings: tuple[Finding, ...]
    repo: str
    default_branch: str


@dataclass(frozen=True)
class SettingsReply:
    """One GET's status plus its parsed body (the API-JSON boundary,
    typed `JsonValue`). Replaces the `(status, body)` record tuples the
    module passed around (PY-002, zackees/ci.yml#193); the FetchStatusFn
    wire protocol itself is unchanged."""

    status: int
    body: JsonValue


def _get(fetch_status: FetchStatusFn, token: str, url: str) -> SettingsReply | None:
    """Wraps a live call: `None` means a transport failure (DNS, timeout,
    ...), distinct from any HTTP status the server actually returned."""

    try:
        status, body = fetch_status(url, token)
        return SettingsReply(status, body)
    except GitHubApiError:
        return None


def _network_finding(rule: str, path: str, url: str) -> Finding:
    return Finding(
        rule=rule,
        status=Status.NEEDS_REVIEW,
        path=path,
        message=f"could not reach the GitHub API for {url} (network/transport failure)",
        fix="re-run once the GitHub API is reachable; this is never treated as a pass on failure",
    )


def _forbidden_finding(rule: str, path: str, what: str) -> Finding:
    return Finding(
        rule=rule,
        status=Status.NEEDS_REVIEW,
        path=path,
        message=f"cannot check {what}: 403 Forbidden -- requires an admin token",
        fix=NEEDS_ADMIN_FIX,
    )


def _unexpected_status_finding(rule: str, path: str, what: str, status: int) -> Finding:
    return Finding(
        rule=rule,
        status=Status.NEEDS_REVIEW,
        path=path,
        message=f"unexpected HTTP {status} checking {what}",
        fix="re-run and inspect the response; this is never treated as a pass on an unexpected status",
    )


# ── SEC-005: any repository or environment Actions secret exists ──────────


def _check_secrets_endpoint(
    fetch_status: FetchStatusFn, token: str, repo: str, *, url: str, path: str, what: str
) -> list[Finding]:
    result = _get(fetch_status, token, url)
    if result is None:
        return [_network_finding("SEC-005", path, url)]
    status, body = result.status, result.body
    if status == 403:
        return [_forbidden_finding("SEC-005", path, what)]
    if status == 404:
        # No such environment (checked separately by SEC-006); not a secrets finding.
        return []
    if status != 200 or not isinstance(body, dict):
        return [_unexpected_status_finding("SEC-005", path, what, status)]
    total = body.get("total_count")
    if isinstance(total, int) and total > 0:
        names = [s.get("name") for s in body.get("secrets", []) if isinstance(s, dict)] if isinstance(
            body.get("secrets"), list
        ) else []
        return [
            Finding(
                rule="SEC-005",
                path=path,
                message=f"{total} {what} exist(s)"
                + (f" ({', '.join(str(n) for n in names)})" if names else ""),
                fix="this profile is OIDC-only (issue #6 §7): delete the stored secret(s) (repo "
                "Settings -> Secrets and variables -> Actions, or the environment's own Secrets "
                "page) -- the publish job must mint its token via id-token, never read a stored one",
            )
        ]
    return []


def check_sec_005(fetch_status: FetchStatusFn, token: str, repo: str, ci: CiToml) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(
        _check_secrets_endpoint(
            fetch_status,
            token,
            repo,
            url=f"{API_ROOT}/repos/{repo}/actions/secrets",
            path=f"repo:{repo}",
            what="repository Actions secret(s)",
        )
    )
    env_names: set[str] = set()
    if ci.publish.pypi is not None and ci.publish.pypi.environment:
        env_names.add(ci.publish.pypi.environment)
    for env in sorted(env_names):
        findings.extend(
            _check_secrets_endpoint(
                fetch_status,
                token,
                repo,
                url=f"{API_ROOT}/repos/{repo}/environments/{env}/secrets",
                path=f"environment:{env}",
                what=f"environment '{env}' secret(s)",
            )
        )
    return findings


# ── SEC-006: publish environment missing / not branch-restricted ──────────


def check_sec_006(fetch_status: FetchStatusFn, token: str, repo: str, ci: CiToml, default_branch: str) -> list[Finding]:  # noqa: C901
    if ci.publish.pypi is None or not ci.publish.pypi.environment:
        return [
            Finding(
                rule="SEC-006",
                status=Status.NEEDS_REVIEW,
                path="ci.toml",
                message="[publish].pypi.environment is not declared; cannot check its deployment "
                "branch policy",
                fix="declare [publish.pypi].environment in ci.toml (e.g. \"pypi\")",
            )
        ]
    env = ci.publish.pypi.environment
    url = f"{API_ROOT}/repos/{repo}/environments/{env}"
    result = _get(fetch_status, token, url)
    if result is None:
        return [_network_finding("SEC-006", f"environment:{env}", url)]
    status, body = result.status, result.body
    if status == 403:
        return [_forbidden_finding("SEC-006", f"environment:{env}", f"environment '{env}'")]
    if status == 404:
        return [
            Finding(
                rule="SEC-006",
                path=f"environment:{env}",
                message=f"environment '{env}' (ci.toml [publish.pypi].environment) does not exist "
                "on this repository",
                fix=f"create the '{env}' environment (repo Settings -> Environments) and restrict its "
                f"deployment branch policy to the default branch ({default_branch!r}) before this "
                "profile's publish job can mint an OIDC token bound to it",
            )
        ]
    if status != 200 or not isinstance(body, dict):
        return [_unexpected_status_finding("SEC-006", f"environment:{env}", f"environment '{env}'", status)]

    policy = body.get("deployment_branch_policy")
    if policy is None:
        return [
            Finding(
                rule="SEC-006",
                path=f"environment:{env}",
                message=f"environment '{env}' has no deployment branch policy -- any branch (including "
                "any PR head) can deploy to it",
                fix=f"restrict '{env}' to the default branch: enable 'Selected branches' with exactly "
                f"'{default_branch}', or 'Protected branches only' if {default_branch!r} is the "
                "repository's only protected branch",
            )
        ]
    if not isinstance(policy, dict):
        return [_unexpected_status_finding("SEC-006", f"environment:{env}", f"environment '{env}'", status)]

    protected_only = policy.get("protected_branches") is True
    custom = policy.get("custom_branch_policies") is True
    if protected_only:
        return []  # only branches with a branch-protection rule may deploy -- accepted as restricted
    if not custom:
        return [
            Finding(
                rule="SEC-006",
                path=f"environment:{env}",
                message=f"environment '{env}' has deployment_branch_policy set but neither "
                "'protected_branches' nor 'custom_branch_policies' is enabled",
                fix=f"restrict '{env}' to the default branch (custom branch policy naming exactly "
                f"'{default_branch}', or 'Protected branches only')",
            )
        ]

    policies_url = f"{API_ROOT}/repos/{repo}/environments/{env}/deployment-branch-policies"
    policies_result = _get(fetch_status, token, policies_url)
    if policies_result is None:
        return [_network_finding("SEC-006", f"environment:{env}", policies_url)]
    p_status, p_body = policies_result.status, policies_result.body
    if p_status == 403:
        return [_forbidden_finding("SEC-006", f"environment:{env}", f"'{env}' deployment branch policies")]
    if p_status != 200 or not isinstance(p_body, dict):
        return [_unexpected_status_finding("SEC-006", f"environment:{env}", f"'{env}' deployment branch policies", p_status)]
    raw_policies = p_body.get("branch_policies")
    names = (
        sorted({p.get("name") for p in raw_policies if isinstance(p, dict) and isinstance(p.get("name"), str)})
        if isinstance(raw_policies, list)
        else []
    )
    if names == [default_branch]:
        return []
    return [
        Finding(
            rule="SEC-006",
            path=f"environment:{env}",
            message=f"environment '{env}' custom deployment branch policy allows {names or '(none)'}, "
            f"not restricted to exactly the default branch ({default_branch!r})",
            fix=f"set '{env}''s custom branch policy to exactly '{default_branch}' (remove every other "
            "pattern, including wildcards)",
        )
    ]


# ── SEC-007: default workflow permissions not read-only ───────────────────


def check_sec_007(fetch_status: FetchStatusFn, token: str, repo: str) -> list[Finding]:
    url = f"{API_ROOT}/repos/{repo}/actions/permissions/workflow"
    result = _get(fetch_status, token, url)
    if result is None:
        return [_network_finding("SEC-007", f"repo:{repo}", url)]
    status, body = result.status, result.body
    if status == 403:
        return [_forbidden_finding("SEC-007", f"repo:{repo}", "default Actions workflow permissions")]
    if status != 200 or not isinstance(body, dict):
        return [_unexpected_status_finding("SEC-007", f"repo:{repo}", "default Actions workflow permissions", status)]
    default_perms = body.get("default_workflow_permissions")
    if default_perms != "read":
        return [
            Finding(
                rule="SEC-007",
                path=f"repo:{repo}",
                message=f"repository default workflow permissions = {default_perms!r}, expected 'read'",
                fix="repo Settings -> Actions -> General -> Workflow permissions -> 'Read repository "
                "contents permission' (never the default 'Read and write'); every job that needs more "
                "must request it explicitly via [allow].permissions",
            )
        ]
    return []


# ── GEN-006 / GEN-011: branch protection + rulesets ────────────────────────


def _fetch_branch_protection(
    fetch_status: FetchStatusFn, token: str, repo: str, branch: str
) -> SettingsReply | None:
    url = f"{API_ROOT}/repos/{repo}/branches/{branch}/protection"
    result = _get(fetch_status, token, url)
    if result is None:
        return None
    return SettingsReply(result.status, result.body if isinstance(result.body, dict) else None)


def _fetch_rulesets(fetch_status: FetchStatusFn, token: str, repo: str) -> SettingsReply | None:
    """Lists rulesets, then fetches each one's full detail (the list
    endpoint's summary objects don't carry `rules`/`conditions`). A 404 on
    the list itself is treated as "no rulesets" (status 404, empty list) --
    the round-5 brief's "treat 404 as none" -- a 403 propagates as status
    403 with an empty list so the caller can report needs_review."""

    url = f"{API_ROOT}/repos/{repo}/rulesets"
    result = _get(fetch_status, token, url)
    if result is None:
        return None
    status, body = result.status, result.body
    if status == 404:
        return SettingsReply(404, [])
    if status != 200 or not isinstance(body, list):
        return SettingsReply(status, [])
    detailed: list[dict[str, JsonValue]] = []
    for summary in body:
        if not isinstance(summary, dict):
            continue
        rid = summary.get("id")
        if rid is None:
            continue
        detail_result = _get(fetch_status, token, f"{API_ROOT}/repos/{repo}/rulesets/{rid}")
        if detail_result is None:
            continue
        if detail_result.status == 200 and isinstance(detail_result.body, dict):
            detailed.append(detail_result.body)
    return SettingsReply(status, detailed)


def _ruleset_covers_branch(ruleset: dict[str, JsonValue], default_branch: str) -> bool:
    """Best-effort: a ruleset's `conditions.ref_name.include` patterns are a
    small DSL (`~DEFAULT_BRANCH`, `~ALL`, `refs/heads/<name>`, fnmatch-style
    globs). This resolves exactly the patterns the fleet's own rulesets are
    expected to use; an unmatched pattern is conservatively NOT counted as
    covering the branch (so GEN-006 can still fire and get human review,
    rather than silently trusting a pattern this function doesn't parse)."""

    conditions = ruleset.get("conditions")
    if not isinstance(conditions, dict):
        return False
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict):
        return False
    include = ref_name.get("include")
    if not isinstance(include, list):
        return False
    target_ref = f"refs/heads/{default_branch}"
    for pattern in include:
        if not isinstance(pattern, str):
            continue
        if pattern in ("~ALL", "~DEFAULT_BRANCH", target_ref):
            return True
    return False


def _has_active_merge_queue(rulesets: list[dict[str, JsonValue]], default_branch: str) -> bool:
    for rs in rulesets:
        if rs.get("enforcement") != "active":
            continue
        if rs.get("target") not in ("branch", None):
            continue
        rules = rs.get("rules")
        if not isinstance(rules, list):
            continue
        has_merge_queue = any(isinstance(r, dict) and r.get("type") == "merge_queue" for r in rules)
        if has_merge_queue and _ruleset_covers_branch(rs, default_branch):
            return True
    return False


def check_gen_006(
    branch_protection: SettingsReply | None,
    rulesets: SettingsReply | None,
    default_branch: str,
) -> list[Finding]:
    if branch_protection is None:
        return [_network_finding("GEN-006", f"branch:{default_branch}", "branch protection")]
    bp_status, bp_body = branch_protection.status, branch_protection.body
    if bp_status == 403:
        return [_forbidden_finding("GEN-006", f"branch:{default_branch}", "branch protection")]
    if bp_status == 404:
        return []  # no branch protection at all -> nothing for GEN-006 to flag (GEN-011 covers this)
    if bp_status != 200 or bp_body is None:
        return [_unexpected_status_finding("GEN-006", f"branch:{default_branch}", "branch protection", bp_status)]

    required_status_checks = bp_body.get("required_status_checks")
    strict = isinstance(required_status_checks, dict) and required_status_checks.get("strict") is True
    if not strict:
        return []

    if rulesets is None:
        return [_network_finding("GEN-006", f"branch:{default_branch}", "rulesets")]
    rs_status, rs_list = rulesets.status, rulesets.body
    if rs_status == 403:
        return [_forbidden_finding("GEN-006", f"branch:{default_branch}", "rulesets")]
    if rs_status not in (200, 404):
        return [_unexpected_status_finding("GEN-006", f"branch:{default_branch}", "rulesets", rs_status)]

    if _has_active_merge_queue(rs_list, default_branch):
        return []

    return [
        Finding(
            rule="GEN-006",
            path=f"branch:{default_branch}",
            message=f"branch protection on '{default_branch}' sets required_status_checks.strict = "
            "true with no active merge-queue ruleset covering it -- every merge forces every other "
            "open PR's required checks to re-run",
            fix="either configure a merge queue (a ruleset with an active 'merge_queue' rule covering "
            f"'{default_branch}') or set required_status_checks.strict = false (docs/case-studies/"
            "fbuild-ci-cost.md's GEN-006 finding)",
        )
    ]


def _contexts_of(checks: JsonValue) -> set[str]:
    """`context` names from a `[{"context": ...}]` checks array."""

    if not isinstance(checks, list):
        return set()
    return {str(c["context"]) for c in checks if isinstance(c, dict) and isinstance(c.get("context"), str)}


def _ruleset_contexts(rs: dict[str, JsonValue]) -> set[str]:
    rules = rs.get("rules")
    if not isinstance(rules, list):
        return set()
    contexts: set[str] = set()
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            params = rule.get("parameters")
            if isinstance(params, dict):
                contexts |= _contexts_of(params.get("required_status_checks"))
    return contexts


def _required_contexts(
    branch_protection: SettingsReply | None,
    rulesets: list[dict[str, JsonValue]],
    default_branch: str,
) -> set[str]:
    """Every required status-check context on the default branch, from
    classic branch protection and every active ruleset covering it."""

    contexts: set[str] = set()
    if branch_protection is not None and branch_protection.status == 200 and branch_protection.body is not None:
        rsc = branch_protection.body.get("required_status_checks")
        if isinstance(rsc, dict):
            raw = rsc.get("contexts")
            if isinstance(raw, list):
                contexts.update(c for c in raw if isinstance(c, str))
            contexts |= _contexts_of(rsc.get("checks"))
    for rs in rulesets:
        if rs.get("enforcement") == "active" and _ruleset_covers_branch(rs, default_branch):
            contexts |= _ruleset_contexts(rs)
    return contexts


def check_gen_022_live(
    branch_protection: SettingsReply | None,
    rulesets: SettingsReply | None,
    default_branch: str,
) -> list[Finding]:
    """GEN-022 live half (#405/#432 step 3): the default branch's ruleset
    enables a merge queue, and the queue has required checks to wait on.

    Zero extra calls: reuses the rulesets/branch-protection payloads GEN-006
    already fetched. No active queue ruleset covering the default branch is a
    violation; a queue with no required status check anywhere (ruleset or
    classic protection) is `needs_review` -- the queue would merge without
    waiting on any `merge_group` run. Whether the required contexts are ones
    a `merge_group` run actually reports is not decidable from settings alone
    and stays the human step-4 evidence (a green queue run)."""

    path = f"branch:{default_branch}"
    if rulesets is None:
        return [_network_finding("GEN-022", path, "rulesets")]
    if rulesets.status == 403:
        return [_forbidden_finding("GEN-022", path, "rulesets")]
    if rulesets.status not in (200, 404):
        return [_unexpected_status_finding("GEN-022", path, "rulesets", rulesets.status)]
    rs_list = rulesets.body if rulesets.status == 200 else []
    if not _has_active_merge_queue(rs_list, default_branch):
        return [
            Finding(
                rule="GEN-022",
                path=path,
                message=f"no active ruleset with a 'merge_queue' rule covers '{default_branch}' -- "
                "merges bypass the merge queue GEN-022 requires",
                fix="follow docs/policy-general.md's GEN-022 transition steps: bump the planner pin, add "
                "the merge_group trigger, then enable a 'main merge queue' ruleset whose required checks "
                "are the contexts a merge_group run reports",
            )
        ]
    if branch_protection is not None and branch_protection.status == 403:
        return [_forbidden_finding("GEN-022", path, "branch protection")]
    if not _required_contexts(branch_protection, rs_list, default_branch):
        return [
            Finding(
                rule="GEN-022",
                status=Status.NEEDS_REVIEW,
                path=path,
                message=f"a merge queue is active on '{default_branch}' but no required status check is "
                "configured (ruleset or branch protection), so the queue merges without waiting on CI",
                fix="add the exact contexts a merge_group run reports (the CI OK aggregator or gate names) "
                "as required checks -- GEN-022 transition step 3",
            )
        ]
    return []


def check_gen_011(
    branch_protection: SettingsReply | None,
    default_branch: str,
    gate_check_name: str,
) -> list[Finding]:
    if branch_protection is None:
        return [_network_finding("GEN-011", f"branch:{default_branch}", "branch protection")]
    bp_status, bp_body = branch_protection.status, branch_protection.body
    if bp_status == 403:
        return [_forbidden_finding("GEN-011", f"branch:{default_branch}", "branch protection")]
    if bp_status == 404:
        return [
            Finding(
                rule="GEN-011",
                status=Status.NEEDS_REVIEW,
                path=f"branch:{default_branch}",
                message=f"branch '{default_branch}' has no branch protection at all, so its required "
                f"status checks cannot include {gate_check_name!r}",
                fix="this is a policy decision, not a code defect -- configure branch protection with "
                f"{gate_check_name!r} required if the fleet intends this repository to be unmergeable "
                "without a green gate",
            )
        ]
    if bp_status != 200 or bp_body is None:
        return [_unexpected_status_finding("GEN-011", f"branch:{default_branch}", "branch protection", bp_status)]

    required_status_checks = bp_body.get("required_status_checks")
    contexts: set[str] = set()
    if isinstance(required_status_checks, dict):
        raw_contexts = required_status_checks.get("contexts")
        if isinstance(raw_contexts, list):
            contexts.update(c for c in raw_contexts if isinstance(c, str))
        raw_checks = required_status_checks.get("checks")
        if isinstance(raw_checks, list):
            contexts.update(
                c.get("context") for c in raw_checks if isinstance(c, dict) and isinstance(c.get("context"), str)
            )
    if gate_check_name in contexts:
        return []
    return [
        Finding(
            rule="GEN-011",
            path=f"branch:{default_branch}",
            message=f"required status checks on '{default_branch}' are {sorted(contexts) or '(none)'}, "
            f"missing the gate check {gate_check_name!r}",
            fix=f"add {gate_check_name!r} to the branch's required status checks (repo Settings -> "
            "Branches -> branch protection rule), or pass --gate-check-name if this repository's gate "
            "job is intentionally named differently",
        )
    ]


# ── GEN-010 (live half): merge-queue / branch-protection doc claims ───────
#
# The static half (ci_lint.rules.doc_claims.check_gen_010_static, run by
# every `precheck`) already resolves the native-Dylint claim offline and
# reports merge-queue/branch-protection claims as NEEDS_REVIEW ("cannot
# confirm without a live read"). This reuses the SAME branch-protection and
# rulesets payloads GEN-006/GEN-011 already fetch (no extra live call) to
# turn each of those advisory claims into a real verdict.


def check_gen_010(  # noqa: C901
    claims: list[DocClaim],
    branch_protection: SettingsReply | None,
    rulesets: SettingsReply | None,
    default_branch: str,
) -> list[Finding]:
    relevant = [c for c in claims if c.kind in ("merge_queue", "branch_protection")]
    if not relevant:
        return []

    bp_exists: bool | None = None  # None = unknown (network/403/unexpected)
    if branch_protection is not None:
        bp_status, _bp_body = branch_protection.status, branch_protection.body
        if bp_status == 200:
            bp_exists = True
        elif bp_status == 404:
            bp_exists = False

    has_merge_queue: bool | None = None
    if rulesets is not None:
        rs_status, rs_list = rulesets.status, rulesets.body
        if rs_status in (200, 404):
            has_merge_queue = _has_active_merge_queue(rs_list, default_branch)

    findings: list[Finding] = []
    for c in relevant:
        if c.kind == "branch_protection":
            if bp_exists is None:
                findings.append(
                    Finding(
                        rule="GEN-010",
                        status=Status.NEEDS_REVIEW,
                        path=c.path,
                        line=c.line,
                        message=f"doc claims branch protection ({c.text!r}); could not confirm live "
                        f"(branch protection lookup on {default_branch!r} was inconclusive)",
                        fix=NEEDS_ADMIN_FIX,
                    )
                )
            elif not bp_exists:
                findings.append(
                    Finding(
                        rule="GEN-010",
                        path=c.path,
                        line=c.line,
                        message=f"doc claims branch protection ({c.text!r}), but branch "
                        f"{default_branch!r} has no branch protection configured at all",
                        fix="either configure branch protection on the default branch (repo Settings "
                        "-> Branches), or correct the doc to describe actual enforcement",
                    )
                )
        elif c.kind == "merge_queue":
            if has_merge_queue is None:
                findings.append(
                    Finding(
                        rule="GEN-010",
                        status=Status.NEEDS_REVIEW,
                        path=c.path,
                        line=c.line,
                        message=f"doc claims a merge queue ({c.text!r}); could not confirm live "
                        "(rulesets lookup was inconclusive)",
                        fix=NEEDS_ADMIN_FIX,
                    )
                )
            elif not has_merge_queue:
                findings.append(
                    Finding(
                        rule="GEN-010",
                        path=c.path,
                        line=c.line,
                        message=f"doc claims a merge queue ({c.text!r}), but no active merge-queue "
                        f"ruleset covers {default_branch!r}",
                        fix="either configure a merge queue (an active ruleset with a 'merge_queue' "
                        "rule covering the default branch), or correct the doc to describe actual "
                        "enforcement (docs/case-studies/clud-ci-cost.md's original GEN-010 finding)",
                    )
                )
    return findings


# ── Orchestration ──────────────────────────────────────────────────────────


def run_audit(
    ci: CiToml,
    *,
    fetch_status: FetchStatusFn,
    token: str,
    repo: str,
    default_branch: str = "main",
    gate_check_name: str = DEFAULT_GATE_CHECK_NAME,
    repo_root: Path | None = None,
) -> AuditReport:
    findings: list[Finding] = []
    findings.extend(check_sec_005(fetch_status, token, repo, ci))
    findings.extend(check_sec_006(fetch_status, token, repo, ci, default_branch))
    findings.extend(check_sec_007(fetch_status, token, repo))
    # CT-007: one extra GET, only when a pin is declared. A pin naming a
    # commit that does not exist makes `local-gate run` unrunnable, so this
    # costs a call to catch a defect that silently disables a repository's
    # whole local verification.
    findings.extend(check_ct_007(ci, fetch_status=fetch_status, token=token, repo=repo))

    branch_protection = _fetch_branch_protection(fetch_status, token, repo, default_branch)
    rulesets = _fetch_rulesets(fetch_status, token, repo)
    findings.extend(check_gen_006(branch_protection, rulesets, default_branch))
    findings.extend(check_gen_011(branch_protection, default_branch, gate_check_name))
    findings.extend(check_gen_022_live(branch_protection, rulesets, default_branch))
    protection_body = branch_protection.body if branch_protection is not None and branch_protection.status == 200 else None
    ruleset_list = rulesets.body if rulesets is not None and rulesets.status == 200 else None
    findings.extend(check_gate_012_required(protection_body, ruleset_list, default_branch))

    if repo_root is not None:
        claims = scan_repo_docs(repo_root)
        findings.extend(check_gen_010(claims, branch_protection, rulesets, default_branch))
        findings.extend(check_rust_014(fetch_status, token, repo_root))
        # GEN-026 with the live default branch: resolves the static needs_review.
        findings.extend(check_gen_026(repo_root, default_branch=default_branch))

    return AuditReport(findings=tuple(findings), repo=repo, default_branch=default_branch)


def to_json_dict(report: AuditReport) -> dict[str, JsonValue]:
    return {
        "repo": report.repo,
        "default_branch": report.default_branch,
        "findings": [
            {"rule": f.rule, "status": f.status.value, "path": f.path, "message": f.message, "fix": f.fix}
            for f in report.findings
        ],
    }


def render_text(report: AuditReport) -> str:
    lines = [f"ci-lint audit: {report.repo} (default branch: {report.default_branch})"]
    rules = ("SEC-005", "SEC-006", "SEC-007", "GEN-006", "GEN-010", "GEN-011", "GEN-022", "GATE-012", "RUST-014")
    by_rule: dict[str, list[Finding]] = {r: [] for r in rules}
    for f in report.findings:
        by_rule.setdefault(f.rule, []).append(f)
    lines.append(f"{'rule':<10} {'status':<14} count")
    for rule in rules:
        items = by_rule.get(rule, [])
        if not items:
            lines.append(f"{rule:<10} {'clean':<14} 0")
            continue
        violations = sum(1 for f in items if f.status == Status.VIOLATION)
        needs_review = sum(1 for f in items if f.status == Status.NEEDS_REVIEW)
        label = "violation" if violations else "needs_review" if needs_review else "clean"
        lines.append(f"{rule:<10} {label:<14} {len(items)}")
    lines.append("")
    if not report.findings:
        lines.append("ci-lint audit: no findings.")
    for f in report.findings:
        lines.append(f.render())
    return "\n".join(lines)
