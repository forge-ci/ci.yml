"""Run a gate with a fresh replay report and reject unproved successful exits."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from ci_lint.cargo_messages import JsonValue
from ci_lint.execution_pins import ExecutionPins, parse_execution_pins
from ci_lint.workflow_replay import ReplayExpectation, prove_replay
from ci_lint.workflow_replay_outputs import ReplayOutput, unique_json_object, require_output_producer
from ci_lint.workflow_replay_expansion import ExpandedJob, OutputProver
from ci_lint.workflow_scan import load_workflows
from ci_lint.workflow_replay_config import ReplayConfig
from ci_lint.workflow_replay_capture import request_arguments, require_captured_outputs
from ci_lint.workflow_replay_plan import build_plan, declared_check
from ci_lint.full_run_receipt import FullRunEvidence, ReceiptPass

REPORT_ENV = "CI_LINT_GATE_REPLAY_REPORT"
MAX_REPORT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class CheckedCommand:
    returncode: int
    error: str | None = None
    full_run: FullRunEvidence | None = None


def _read_report(path: Path, *, max_bytes: int = MAX_REPORT_BYTES) -> JsonValue:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise ValueError(f"replay report must be a regular file no larger than {max_bytes} bytes")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)


def query_execution_pins(repo: Path, config: ReplayConfig | None) -> ExecutionPins | None:
    if config is None or not config.provider_query:
        return None
    with tempfile.TemporaryDirectory(prefix="ci-provider-") as scratch:
        path = Path(scratch) / "status.json"
        with path.open("wb") as output, (Path(scratch) / "stderr.log").open("wb") as errors:
            process = subprocess.run(list(config.provider_query), cwd=repo, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=errors, timeout=60, check=False)
        if process.returncode != 0:
            raise ValueError(f"execution provider query failed (exit {process.returncode})")
        raw = _read_report(path, max_bytes=64 * 1024)
    runners = raw.get("runners") if isinstance(raw, dict) else None
    if not isinstance(runners, dict):
        raise ValueError("execution provider status has no runners record")
    return parse_execution_pins(runners.get("execution_pins"))


def bind_execution_pins(repo: Path, config: ReplayConfig) -> ReplayConfig:
    pins = query_execution_pins(repo, config)
    if config.execution_pins is not None and config.execution_pins != pins:
        raise ValueError("execution provider changed during the gate; rerun with current pins")
    return replace(config, execution_pins=pins)


def _execute(repo: Path, argv: tuple[str, ...], report: Path, *, report_source: str,
             env: dict[str, str], stdout: BinaryIO | None) -> int:
    if report_source == "file":
        return subprocess.run(list(argv), cwd=repo, env=env, stdin=subprocess.DEVNULL,
                              stdout=stdout, stderr=subprocess.STDOUT if stdout is not None else None,
                              check=False).returncode
    if report_source != "stdout":
        raise ValueError("unknown replay report source")
    with report.open("wb") as receipt:
        process = subprocess.run(list(argv), cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                 stdout=receipt, stderr=stdout, check=False)
    if stdout is not None:
        with report.open("rb") as receipt:
            shutil.copyfileobj(receipt, stdout)
    return process.returncode



def _receipt_prover(raw: JsonValue, config: ReplayConfig, expectation: ReplayExpectation) -> OutputProver:
    def prove(expanded: ExpandedJob) -> tuple[ReplayOutput, ...]:
        declarations = tuple(item for item in config.jobs if item.source_job == expanded.source_job)
        if len(declarations) != 1:
            raise ValueError("dynamic replay producer lacks an unambiguous source declaration")
        check = declared_check(declarations[0], expanded, mode=config.mode, event=expectation.event)
        if check.excluded or check.remote_maintenance:
            return ()  # The final complete proof validates skips and maintenance stubs.
        return prove_replay(raw, replace(expectation, required_jobs=(check,))).outputs
    return prove


def run_checked_command(repo: Path, argv: tuple[str, ...], config: ReplayConfig, *,
                        head: str, tree: str, lane: str | None = None,
                        env: dict[str, str] | None = None, stdout: BinaryIO | None = None) -> CheckedCommand:
    try:
        config = bind_execution_pins(repo, config)
        files = tuple(load_workflows(repo)) if config.qualified else ()
        plan = build_plan(repo, config, lane=lane, defer_outputs=True, files=files)
        argv = request_arguments(argv, plan.capture_requests)
        if (plan.deferred_outputs or plan.capture_requests) and config.execution_pins is not None:
            require_output_producer(config.execution_pins.act_version)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return CheckedCommand(1, f"workflow replay proof rejected before execution: {exc}")
    expectation = ReplayExpectation(config.repository, repo, head, tree, plan.workflow,
                                    plan.selected, config.mode, plan.required,
                                    "pr" if plan.event == "pull_request" else plan.event, plan.event,
                                    plan.inputs, config.execution_pins,
                                    repository_aliases=config.repository_aliases)
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ci-replay-") as scratch:
        report = Path(scratch) / "report.json"
        child_env = dict(env) if env is not None else os.environ.copy()
        child_env[REPORT_ENV] = str(report)
        try:
            returncode = _execute(repo, argv, report, report_source=config.report_source,
                                  env=child_env, stdout=stdout)
            if returncode != 0:
                return CheckedCommand(returncode)
            raw = _read_report(report)
            if plan.deferred_outputs:
                plan = build_plan(repo, config, lane=lane, files=files,
                                  prove_outputs=_receipt_prover(raw, config, expectation))
                expectation = replace(expectation, required_jobs=plan.required)
            proof = prove_replay(raw, expectation)
            require_captured_outputs(plan.required, proof.outputs, plan.capture_requests)
            bind_execution_pins(repo, config)
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
            return CheckedCommand(1, f"workflow replay proof rejected: {exc}")
    evidence = (FullRunEvidence(tuple(ReceiptPass(lane, int(round(time.monotonic() - start))) for lane in plan.lanes))
                if plan.lanes else None)
    return CheckedCommand(0, full_run=evidence)
