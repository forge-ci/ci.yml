"""Match findings against declared `[[exceptions]]` and flag expired ones.

Section B of the round-1A brief: "Declared [[exceptions]] matching a finding
turn it into status `approved_exception` (printed every run); an expired
exception is itself a violation (`CT-005`)."

A rule in UNWAIVABLE_RULES ignores `[[exceptions]]`: its findings stay
violations, and each entry naming it is reported as a violation of that
rule at ci.toml.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace

from ci_lint.finding import Finding, Status
from ci_lint.schema import CiToml, ExceptionEntry


# CACHE-025 (Swatinem/rust-cache ban): maintainer decision 2026-10-02, no
# exceptions of any kind.
UNWAIVABLE_RULES: frozenset[str] = frozenset({"CACHE-025"})


@dataclass(frozen=True)
class _ExceptionKey:
    rule: str
    path: str


@dataclass(frozen=True)
class ExceptionOutcome:
    findings: list[Finding]
    matched: list[ExceptionEntry]


def _is_expired(entry: ExceptionEntry, today: datetime.date) -> bool | None:
    """True/False, or None if `expires` could not be parsed (schema
    validation already reported that as CT-002; treat as not-yet-expired so
    we don't double-report)."""

    try:
        expires = datetime.date.fromisoformat(entry.expires)
    except ValueError:
        return None
    return today > expires


def apply_exceptions(
    ci: CiToml, findings: list[Finding], *, today: datetime.date | None = None
) -> ExceptionOutcome:
    today = today or datetime.date.today()
    matched: list[ExceptionEntry] = []
    out: list[Finding] = []

    for finding in findings:
        entry = next(
            (
                e
                for e in ci.exceptions
                if e.rule == finding.rule and e.path == finding.path and e.rule not in UNWAIVABLE_RULES
            ),
            None,
        )
        if entry is None:
            out.append(finding)
            continue
        matched.append(entry)
        expired = _is_expired(entry, today)
        if expired:
            # Still a violation -- the exception no longer covers it.
            out.append(finding)
        else:
            out.append(replace(finding, status=Status.APPROVED_EXCEPTION))

    for entry in ci.exceptions:
        if entry.rule in UNWAIVABLE_RULES:
            out.append(
                Finding(
                    rule=entry.rule,
                    path="ci.toml",
                    message=f"[[exceptions]] entry for {entry.rule} on '{entry.path}': {entry.rule} cannot be waived",
                    fix=f"delete the [[exceptions]] entry and fix the {entry.rule} finding itself "
                    "(maintainer decision 2026-10-02: no exceptions)",
                )
            )

    # CT-005: an expired exception is itself a violation, reported once per
    # exception entry (not once per finding it would have covered).
    seen_expired: set[_ExceptionKey] = set()
    for entry in ci.exceptions:
        expired = _is_expired(entry, today)
        if not expired:
            continue
        key = _ExceptionKey(entry.rule, entry.path)
        if key in seen_expired:
            continue
        seen_expired.add(key)
        out.append(
            Finding(
                rule="CT-005",
                path="ci.toml",
                message=(
                    f"exception for {entry.rule} on '{entry.path}' expired {entry.expires} "
                    f"(issue: {entry.issue})"
                ),
                fix=(
                    "either fix the underlying finding and delete the [[exceptions]] entry, or "
                    "renew it by updating 'expires' after re-review and recording why in 'reason'"
                ),
            )
        )

    return ExceptionOutcome(findings=out, matched=matched)
