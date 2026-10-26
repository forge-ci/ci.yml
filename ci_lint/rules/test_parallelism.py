"""TEST-003: in-process test workers on one runner need serial-utilization evidence.

zackees/ci.yml#152 (evidence: zackees/clud#1614, run 36687465340): `pytest -n
auto` made clud's 1,573-test CPU-bound pytest phase slower, 172 s vs ~135 s
serial, because a hosted "4 vCPU" runner has two physical cores with SMT and
a serial run already uses both. docs/policy-general.md's TEST-003 clause 3
allows pytest-xdist / `--numprocesses` on one runner only with recorded
serial-utilization evidence: `sum(test time) / wall` and physical vs logical
`nproc` for the serial run, with the ratio well below the physical core count.

The policy names the evidence's content but no location, so ci-lint reads it
from one file, ``ci/serial-utilization.json`` (the minimal format, documented
in the policy section)::

    {"sum_test_seconds": 135.0, "wall_seconds": 270.0,
     "physical_cores": 2, "logical_cores": 4, "run_url": "https://..."}

Static signals (run: lines, one level into a referenced ci/*.py script):

- needs_review: a pytest invocation with `-n`/`-n<N>`/`--numprocesses` and
  no evidence file, or a malformed one, or one whose ratio is not below half
  the physical core count (~0.5 per policy's "well below").
- pass: `-n 0` / `-n0` (xdist disabled), or valid evidence under the bar.

Always needs_review, never a violation: the rule is decided policy, and an
I/O-bound suite may legitimately benefit. Sharding-proof clauses 1-2 are not
checked here. A same-line `# ci-lint: allow TEST-003 <reason>` excuses a line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ci_lint.finding import Finding, Status
from ci_lint.rules.test_invocations import allowed, iter_command_sites

RULE = "TEST-003"
EVIDENCE_PATH = "ci/serial-utilization.json"
_FIELDS = ("sum_test_seconds", "wall_seconds", "physical_cores", "logical_cores")
_FIX = (
    "split a CPU-bound suite across jobs instead (docs/policy-general.md, TEST-003), or record the "
    f"serial run's utilization in {EVIDENCE_PATH} (sum_test_seconds, wall_seconds, physical_cores, "
    "logical_cores, run_url) showing sum/wall well below the physical core count"
)


@dataclass(frozen=True)
class Evidence:
    ok: bool
    reason: str  # empty when ok


def _is_pytest(tokens: tuple[str, ...]) -> int | None:
    """Index after the pytest program token, or None."""

    for i, tok in enumerate(tokens):
        base = tok.rsplit("/", 1)[-1]
        if base in ("pytest", "py.test"):
            return i + 1
        if tok == "-m" and i + 1 < len(tokens) and tokens[i + 1] == "pytest":
            return i + 2
    return None


def workers_flag(tokens: tuple[str, ...]) -> str | None:
    """The in-process-workers flag a pytest command carries, unless it disables xdist."""

    start = _is_pytest(tokens)
    if start is None:
        return None
    args = tokens[start:]
    for i, tok in enumerate(args):
        if tok in ("-n", "--numprocesses"):
            value = args[i + 1] if i + 1 < len(args) else ""
        elif tok.startswith("--numprocesses="):
            value = tok.split("=", 1)[1]
        elif tok.startswith("-n") and len(tok) > 2 and not tok.startswith("--"):
            value = tok[2:]
        else:
            continue
        if value == "0":
            return None
        return f"{tok} {value}".strip() if tok in ("-n", "--numprocesses") else tok
    return None


def load_evidence(repo_root: Path) -> Evidence:
    path = repo_root / EVIDENCE_PATH
    if not path.is_file():
        return Evidence(False, f"no {EVIDENCE_PATH} records serial-utilization evidence")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Evidence(False, f"{EVIDENCE_PATH} is not valid JSON ({exc})")
    if not isinstance(doc, dict):
        return Evidence(False, f"{EVIDENCE_PATH} is not a JSON object")
    nums: dict[str, float] = {}
    for key in _FIELDS:
        value = doc.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return Evidence(False, f"{EVIDENCE_PATH}: '{key}' must be a positive number")
        nums[key] = float(value)
    if nums["physical_cores"] > nums["logical_cores"]:
        return Evidence(False, f"{EVIDENCE_PATH}: physical_cores exceeds logical_cores")
    ratio = nums["sum_test_seconds"] / nums["wall_seconds"]
    bar = nums["physical_cores"] / 2
    if ratio >= bar:
        return Evidence(
            False,
            f"{EVIDENCE_PATH}: serial utilization {ratio:.2f} is not well below the "
            f"{nums['physical_cores']:g} physical cores (bar: < {bar:g}) -- the suite is CPU-bound",
        )
    return Evidence(True, "")


def check_test_003(repo_root: Path) -> list[Finding]:
    sites = [
        (site, flag)
        for site in iter_command_sites(repo_root)
        if (flag := workers_flag(site.tokens)) is not None and not allowed(site.raw, RULE)
    ]
    if not sites:
        return []
    evidence = load_evidence(repo_root)
    if evidence.ok:
        return []
    return [
        Finding(
            rule=RULE,
            path=site.path,
            line=site.line,
            status=Status.NEEDS_REVIEW,
            message=f"{site.loc}: pytest runs in-process workers ({flag}) on one runner and "
            f"{evidence.reason}",
            fix=_FIX,
        )
        for site, flag in sites
    ]
