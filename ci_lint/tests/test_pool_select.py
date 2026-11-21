"""actions/pool-select/select_pool.py: route to the self-hosted pool only when
it is trusted and has idle capacity; every other state falls back to hosted."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError

SCRIPT = Path(__file__).resolve().parents[2] / "actions" / "pool-select" / "select_pool.py"
_SPEC = importlib.util.spec_from_file_location("select_pool", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
sp = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sp
_SPEC.loader.exec_module(sp)

POOL = '["self-hosted","forge-pool"]'
TOKEN = "ghs_secret_value"


def runner(runner_id: int, *, busy: bool = False, status: str = "online", labels: tuple[str, ...] = ()) -> dict:
    names = labels or ("self-hosted", "forge-pool", "dell-jw22zc3", "obs-ubuntu-2604")
    return {"id": runner_id, "status": status, "busy": busy, "labels": [{"name": n, "type": "custom"} for n in names]}


class FakeApi:
    """Serves pages in order; records each requested URL and token."""

    def __init__(self, *pages: object, error: Exception | None = None) -> None:
        self.pages = list(pages)
        self.error = error
        self.urls: list[str] = []
        self.tokens: list[str] = []

    def __call__(self, url: str, token: str) -> object:
        self.urls.append(url)
        self.tokens.append(token)
        if self.error is not None:
            raise self.error
        return self.pages.pop(0)


def page(*runners: dict, total: int | None = None) -> dict:
    return {"total_count": len(runners) if total is None else total, "runners": list(runners)}


def settings(**overrides: str) -> sp.Settings:
    base = sp.Settings(
        labels=POOL,
        fallback="ubuntu-latest",
        token=TOKEN,
        min_idle="1",
        mode="",
        trusted_only="true",
        event="push",
        event_path="",
        repository="FastLED/FastLED",
        owner="FastLED",
        api_url="https://api.github.com",
    )
    return replace(base, **overrides)


def run(cfg: sp.Settings, api: FakeApi) -> tuple[sp.Decision, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        decision = sp.select(cfg, api)
    return decision, out.getvalue()


class EventTrust(unittest.TestCase):
    def _pr_event(self, head: str | None, event: str = "pull_request", **extra: str) -> sp.Decision:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            repo = None if head is None else {"full_name": head}
            path.write_text(json.dumps({"pull_request": {"head": {"repo": repo}}}), encoding="utf-8")
            api = FakeApi(page(runner(1)))
            decision, _ = run(settings(event=event, event_path=str(path), **extra), api)
            self.api_calls = len(api.urls)
            return decision

    def test_same_repo_pull_request_uses_pool(self) -> None:
        decision = self._pr_event("fastled/fastled")
        self.assertTrue(decision.pool)
        self.assertEqual(decision.runs_on, POOL)

    def test_fork_pull_request_is_hosted_without_api(self) -> None:
        decision = self._pr_event("someone/FastLED")
        self.assertFalse(decision.pool)
        self.assertEqual(decision.runs_on, '"ubuntu-latest"')
        self.assertEqual(self.api_calls, 0)
        self.assertIn("fork", decision.reason)

    def test_deleted_fork_head_is_hosted(self) -> None:
        self.assertFalse(self._pr_event(None).pool)

    def test_unreadable_event_payload_is_hosted(self) -> None:
        decision, _ = run(settings(event="pull_request", event_path="/nonexistent/event.json"), FakeApi(page(runner(1))))
        self.assertFalse(decision.pool)

    def test_pull_request_target_is_never_pooled(self) -> None:
        self.assertFalse(self._pr_event("FastLED/FastLED", event="pull_request_target").pool)
        self.assertEqual(self.api_calls, 0)

    def test_untrusted_events_are_hosted(self) -> None:
        for event in ("workflow_run", "issue_comment", "release", ""):
            with self.subTest(event=event):
                api = FakeApi(page(runner(1)))
                decision, _ = run(settings(event=event), api)
                self.assertFalse(decision.pool)
                self.assertEqual(api.urls, [])

    def test_trusted_events_use_pool(self) -> None:
        for event in ("push", "merge_group", "workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                self.assertTrue(run(settings(event=event), FakeApi(page(runner(1))))[0].pool)

    def test_trusted_only_false_allows_any_event(self) -> None:
        self.assertTrue(self._pr_event("someone/FastLED", trusted_only="false").pool)
        self.assertTrue(run(settings(event="workflow_run", trusted_only="False"), FakeApi(page(runner(1))))[0].pool)

    def test_unrecognized_trusted_only_keeps_the_check(self) -> None:
        self.assertFalse(self._pr_event("someone/FastLED", trusted_only="maybe").pool)


class TokenAndErrors(unittest.TestCase):
    def test_no_token_is_hosted_with_warning_and_no_api(self) -> None:
        api = FakeApi(page(runner(1)))
        decision, out = run(settings(token="", source="github"), api)
        self.assertFalse(decision.pool)
        self.assertEqual(api.urls, [])
        self.assertIn("::warning::", out)

    def test_api_errors_fall_back_with_fixed_diagnostics(self) -> None:
        errors = (
            HTTPError("https://api.github.com", 403, f"denied {TOKEN}", None, None),
            URLError(f"boom {TOKEN}"),
            TimeoutError(f"slow {TOKEN}"),
            ValueError(f"bad json {TOKEN}"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                decision, out = run(settings(), FakeApi(error=error))
                self.assertFalse(decision.pool)
                self.assertEqual(decision.runs_on, '"ubuntu-latest"')
                self.assertIn("::warning::", out)
                self.assertNotIn(TOKEN, out + decision.reason)
                self.assertNotIn("denied", out)
        self.assertIn("403", run(settings(), FakeApi(error=errors[0]))[0].reason)

    def test_malformed_responses_fall_back(self) -> None:
        bad_pages = (
            [],
            {"runners": "x", "total_count": 1},
            {"runners": [], "total_count": "1"},
            page({"id": "1", "status": "online", "busy": False, "labels": []}),
            page({"id": 1, "status": "online", "busy": "no", "labels": []}),
            page({"id": 1, "status": "online", "busy": False, "labels": "self-hosted"}),
            page({"id": 1, "status": "online", "busy": False, "labels": [{"id": 3}]}),
            page("runner"),
        )
        for bad in bad_pages:
            with self.subTest(bad=bad):
                decision, out = run(settings(), FakeApi(bad))
                self.assertFalse(decision.pool)
                self.assertIn("::warning::", out)

    def test_invalid_config_falls_back_without_api(self) -> None:
        for override in ({"labels": "self-hosted"}, {"labels": '["a", ""]'}, {"labels": '{"a": 1}'},
                         {"min_idle": "x"}, {"min_idle": "0"}, {"mode": "always"}):
            with self.subTest(override=override):
                api = FakeApi(page(runner(1)))
                decision, out = run(settings(**override), api)
                self.assertFalse(decision.pool)
                self.assertEqual(api.urls, [])
                self.assertIn("::warning::", out)

    def test_empty_labels_mean_hosted_quietly(self) -> None:
        for labels in ("", "  ", "[]"):
            with self.subTest(labels=labels):
                api = FakeApi(page(runner(1)))
                decision, out = run(settings(labels=labels), api)
                self.assertFalse(decision.pool)
                self.assertEqual((api.urls, out), ([], ""))

    def test_queries_the_owner_org_with_the_token(self) -> None:
        api = FakeApi(page(runner(1)))
        run(settings(repository="talos-hq/clud", owner="talos-hq", api_url="https://ghe.example/api/v3/"), api)
        self.assertEqual(api.urls, ["https://ghe.example/api/v3/orgs/talos-hq/actions/runners?per_page=100&page=1"])
        self.assertEqual(api.tokens, [TOKEN])


class Capacity(unittest.TestCase):
    def test_idle_runner_selects_pool(self) -> None:
        decision, _ = run(settings(), FakeApi(page(runner(1, busy=True), runner(2))))
        self.assertTrue(decision.pool)
        self.assertIn("online=2, busy=1, idle=1, required=1", decision.reason)

    def test_all_busy_is_hosted(self) -> None:
        decision, _ = run(settings(), FakeApi(page(runner(1, busy=True), runner(2, busy=True))))
        self.assertFalse(decision.pool)
        self.assertIn("idle=0", decision.reason)

    def test_offline_and_nonmatching_runners_do_not_count(self) -> None:
        api = FakeApi(page(runner(1, status="offline"), runner(2, labels=("self-hosted", "other-pool"))))
        decision, _ = run(settings(), api)
        self.assertFalse(decision.pool)
        self.assertIn("online=0", decision.reason)

    def test_min_idle_threshold(self) -> None:
        pool = page(runner(1), runner(2), runner(3, busy=True))
        self.assertTrue(run(settings(min_idle="2"), FakeApi(pool))[0].pool)
        pool = page(runner(1), runner(2), runner(3, busy=True))
        self.assertFalse(run(settings(min_idle="3"), FakeApi(pool))[0].pool)

    def test_prefer_mode_accepts_busy_online_runner(self) -> None:
        self.assertTrue(run(settings(mode="prefer"), FakeApi(page(runner(1, busy=True))))[0].pool)
        self.assertFalse(run(settings(mode="prefer"), FakeApi(page(runner(1, status="offline"))))[0].pool)

    def test_label_matching_is_case_insensitive_subset(self) -> None:
        cfg = settings(labels='["Self-Hosted","FORGE-POOL","ubuntu-2404"]')
        api = FakeApi(page(runner(1, labels=("self-hosted", "forge-pool", "dell-fsx3mn2", "ubuntu-2404"))))
        decision, _ = run(cfg, api)
        self.assertTrue(decision.pool)
        self.assertEqual(decision.runs_on, '["Self-Hosted","FORGE-POOL","ubuntu-2404"]')

    def test_labels_must_all_be_on_one_runner(self) -> None:
        cfg = settings(labels='["self-hosted","forge-pool","ubuntu-2404"]')
        api = FakeApi(page(runner(1), runner(2, labels=("self-hosted", "ubuntu-2404"))))
        self.assertFalse(run(cfg, api)[0].pool)

    def test_pagination_counts_later_pages_and_dedupes(self) -> None:
        first = page(*(runner(i, busy=True) for i in range(100)), total=150)
        second = page(runner(99), runner(100), total=150)
        api = FakeApi(first, second)
        decision, _ = run(settings(), api)
        self.assertEqual(len(api.urls), 2)
        self.assertTrue(decision.pool)
        # Runner 99 was busy on page 1 and idle on page 2: counted once, as busy.
        self.assertIn("online=101, busy=100, idle=1", decision.reason)

    def test_pagination_is_bounded(self) -> None:
        full = [page(*(runner(p * 100 + i, busy=True) for i in range(100)), total=10_000) for p in range(10)]
        api = FakeApi(*full)
        decision, _ = run(settings(), api)
        self.assertEqual(len(api.urls), sp.MAX_PAGES)
        self.assertFalse(decision.pool)


class Fallback(unittest.TestCase):
    def test_fallback_forms(self) -> None:
        self.assertEqual(sp.fallback_json(""), '"ubuntu-latest"')
        self.assertEqual(sp.fallback_json("ubuntu-24.04"), '"ubuntu-24.04"')
        self.assertEqual(sp.fallback_json('["ubuntu-latest", "x64"]'), '["ubuntu-latest","x64"]')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sp.fallback_json("[broken"), '"ubuntu-latest"')
            self.assertEqual(sp.fallback_json("[]"), '"ubuntu-latest"')

    def test_array_fallback_is_used_when_hosted(self) -> None:
        decision, _ = run(settings(fallback='["windows-latest"]', token=""), FakeApi())
        self.assertEqual(decision.runs_on, '["windows-latest"]')


class Publish(unittest.TestCase):
    def test_writes_outputs_and_summary_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp) / "out", Path(tmp) / "summary"
            env = {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary), "POOL_TOKEN": TOKEN}
            decision = sp.Decision(POOL, True, "idle pool capacity (online=1, busy=0, idle=1, required=1)")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(sp.publish(decision, env), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), f"runs-on={POOL}\npool=true\n")
            text = summary.read_text(encoding="utf-8")
            self.assertIn("Selected **self-hosted pool**", text)
            self.assertNotIn(TOKEN, text + out.getvalue())

    def test_settings_from_env_derives_owner(self) -> None:
        cfg = sp.settings_from_env({"GITHUB_REPOSITORY": "forge-ci/soldr", "POOL_MODE": " Prefer "})
        self.assertEqual((cfg.owner, cfg.mode, cfg.trusted_only), ("forge-ci", "prefer", "true"))
        self.assertEqual(cfg.api_url, "https://api.github.com")


STATUS_URL = "https://signal.forge-ci.com/v1/pool"
DELL = ["Linux", "X64", "dell-jw22zc3", "forge-pool", "nixos-host", "self-hosted", "ubuntu-2404"]
DELL2 = ["Linux", "X64", "dell-fsx3mn2", "forge-pool", "nixos-host", "self-hosted", "ubuntu-2404"]
QUICK = ["Linux", "forge-quick", "self-hosted"]


def status(*groups: dict, org: str = "FastLED", state: str = "ok", age: object = 1.5, **top: object) -> dict:
    entry = {"state": state, "error": None, "fetched_at": "2026-11-21T08:29:57Z", "age_s": age,
             "runners": list(groups), "dispatcher": None}
    doc = {"schema": "forge-ci.pool-status", "version": 1, "generated_at": "2026-11-21T08:29:58Z",
           "ttl_s": 10, "max_age_s": 30, "orgs": {org: entry}}
    doc.update(top)
    return doc


def group(labels: list[str], online: int, busy: int) -> dict:
    return {"labels": labels, "online": online, "busy": busy}


def signal(**overrides: str) -> sp.Settings:
    return settings(token="", **overrides)


class SignalSource(unittest.TestCase):
    """source 'signal': the tokenless pool status at signal.forge-ci.com."""

    def test_no_token_defaults_to_signal_and_sends_no_token(self) -> None:
        api = FakeApi(status(group(DELL, 2, 1)))
        decision, out = run(signal(), api)
        self.assertTrue(decision.pool)
        self.assertEqual(decision.runs_on, POOL)
        self.assertEqual(api.urls, [f"{STATUS_URL}?org=FastLED"])
        self.assertEqual(api.tokens, [""])
        self.assertIn("source=signal", decision.reason)
        self.assertNotIn("::warning::", out)

    def test_token_keeps_the_github_probe_by_default(self) -> None:
        api = FakeApi(page(runner(1)))
        decision, _ = run(settings(), api)
        self.assertTrue(decision.pool)
        self.assertTrue(api.urls[0].startswith("https://api.github.com/orgs/FastLED/actions/runners"))
        self.assertIn("source=github", decision.reason)

    def test_explicit_signal_ignores_the_token(self) -> None:
        api = FakeApi(status(group(DELL, 1, 0)))
        decision, _ = run(settings(source="signal"), api)
        self.assertTrue(decision.pool)
        self.assertEqual(api.tokens, [""])

    def test_status_url_override_and_owner_quoting(self) -> None:
        api = FakeApi(status(group(DELL, 1, 0), org="talos-hq"))
        run(signal(owner="talos-hq", status_url="https://example.test/v1/pool?x=1"), api)
        self.assertEqual(api.urls, ["https://example.test/v1/pool?x=1&org=talos-hq"])

    def test_non_https_status_url_is_hosted_without_a_request(self) -> None:
        api = FakeApi(status(group(DELL, 1, 0)))
        decision, out = run(signal(status_url="http://signal.forge-ci.com/v1/pool"), api)
        self.assertFalse(decision.pool)
        self.assertEqual(api.urls, [])
        self.assertIn("::warning::", out)

    def test_sums_every_matching_label_set(self) -> None:
        doc = status(group(DELL, 4, 4), group(DELL2, 4, 3), group(QUICK, 4, 0))
        decision, _ = run(signal(), FakeApi(doc))
        self.assertTrue(decision.pool)
        self.assertIn("online=8, busy=7, idle=1", decision.reason)

    def test_all_busy_is_hosted_and_prefer_accepts_busy(self) -> None:
        doc = status(group(DELL, 2, 2), group(QUICK, 4, 0))
        decision, out = run(signal(), FakeApi(doc))
        self.assertFalse(decision.pool)
        self.assertIn("not enough pool capacity", decision.reason)
        self.assertNotIn("::warning::", out)
        self.assertTrue(run(signal(mode="prefer"), FakeApi(doc))[0].pool)

    def test_min_idle_and_case_insensitive_subset(self) -> None:
        doc = status(group(DELL, 3, 1))
        self.assertTrue(run(signal(min_idle="2"), FakeApi(doc))[0].pool)
        self.assertFalse(run(signal(min_idle="3"), FakeApi(doc))[0].pool)
        self.assertTrue(run(signal(labels='["SELF-HOSTED","Forge-Pool","UBUNTU-2404"]'), FakeApi(doc))[0].pool)

    def test_unrecognized_label_is_hosted(self) -> None:
        doc = status(group(DELL, 4, 0), group(QUICK, 4, 0))
        decision, out = run(signal(labels='["self-hosted","forge-pool","no-such-lane"]'), FakeApi(doc))
        self.assertFalse(decision.pool)
        self.assertIn("online=0", decision.reason)

    def test_org_key_matches_owner_case_insensitively(self) -> None:
        self.assertTrue(run(signal(owner="fastled"), FakeApi(status(group(DELL, 1, 0))))[0].pool)

    def test_fork_pull_request_never_asks_the_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": "x/FastLED"}}}}))
            api = FakeApi(status(group(DELL, 1, 0)))
            decision, _ = run(signal(event="pull_request", event_path=str(path)), api)
        self.assertFalse(decision.pool)
        self.assertEqual(api.urls, [])

    def test_unknown_source_is_hosted(self) -> None:
        api = FakeApi(status(group(DELL, 1, 0)))
        decision, out = run(signal(source="ledger"), api)
        self.assertFalse(decision.pool)
        self.assertEqual(api.urls, [])
        self.assertIn("::warning::", out)

    def test_fail_closed_on_bad_or_stale_status(self) -> None:
        good = group(DELL, 4, 0)
        cases = {
            "wrong schema": status(good, schema="something-else"),
            "future version": status(good, version=2),
            "string version": status(good, version="1"),
            "org missing": status(good, org="obs-rust"),
            "org in error": status(good, state="error"),
            "stale": status(good, age=31),
            "stale by server max": status(good, age=12, max_age_s=10),
            "no age": status(good, age=None),
            "negative age": status(good, age=-1),
            "bool age": status(good, age=True),
            "runners not a list": {**status(), "orgs": {"FastLED": {"state": "ok", "age_s": 1, "runners": {}}}},
            "busy > online": status(group(DELL, 1, 2)),
            "negative count": status(group(DELL, -1, 0)),
            "labels not strings": status(group([1, 2], 1, 0)),  # type: ignore[list-item]
            "orgs not a map": status(good, orgs=[]),
            "not an object": [],
        }
        for name, doc in cases.items():
            with self.subTest(case=name):
                decision, out = run(signal(), FakeApi(doc))
                self.assertFalse(decision.pool)
                self.assertEqual(decision.runs_on, '"ubuntu-latest"')
                self.assertIn("::warning::", out)

    def test_http_errors_and_timeouts_are_hosted(self) -> None:
        for error in (HTTPError(STATUS_URL, 503, "unavailable", None, None), HTTPError(STATUS_URL, 502, "x", None, None),
                      URLError("refused"), TimeoutError("slow")):
            with self.subTest(error=type(error).__name__):
                decision, out = run(signal(), FakeApi(error=error))
                self.assertFalse(decision.pool)
                self.assertIn("::warning::", out)
                self.assertNotIn("unavailable", out)

    def test_dispatcher_and_unknown_fields_are_ignored(self) -> None:
        doc = status(group(DELL, 1, 0), extra="new")
        doc["orgs"]["FastLED"]["dispatcher"] = {"hosts": [{"host": "dell-jw22zc3", "free_slots": 3}]}
        doc["orgs"]["FastLED"]["runners"][0]["new_field"] = 1
        self.assertTrue(run(signal(), FakeApi(doc))[0].pool)

    def test_settings_from_env_reads_source_and_url(self) -> None:
        cfg = sp.settings_from_env({"GITHUB_REPOSITORY": "forge-ci/soldr", "POOL_SOURCE": "signal",
                                    "POOL_STATUS_URL": "https://x.test/v1/pool"})
        self.assertEqual((cfg.source, cfg.status_url), ("signal", "https://x.test/v1/pool"))

    def test_github_fetch_sends_no_authorization_without_token(self) -> None:
        seen: list[dict[str, str]] = []

        class Resp(io.BytesIO):
            def __enter__(self) -> "Resp":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_urlopen(request: object, timeout: float) -> Resp:
            seen.append(dict(request.header_items()))  # type: ignore[attr-defined]
            return Resp(b"{}")

        original = sp.urlopen
        sp.urlopen = fake_urlopen  # type: ignore[assignment]
        try:
            sp.github_fetch(STATUS_URL, "")
            sp.github_fetch("https://api.github.com/x", TOKEN)
        finally:
            sp.urlopen = original
        self.assertNotIn("Authorization", seen[0])
        self.assertEqual(seen[1]["Authorization"], f"Bearer {TOKEN}")

    def test_action_wires_source_and_status_url(self) -> None:
        action = (Path(__file__).resolve().parents[2] / "actions" / "pool-select" / "action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('default: "https://signal.forge-ci.com/v1/pool"', action)
        self.assertIn("POOL_SOURCE: ${{ inputs.source }}", action)
        self.assertIn("POOL_STATUS_URL: ${{ inputs.status-url }}", action)


if __name__ == "__main__":
    unittest.main()


# The uv release the forge-quick image seeds into its tool cache (forge-ci/soldrix
# image/quick/Dockerfile). An unpinned setup-uv resolves "latest", downloads it into the
# job's memory-backed tmpfs and peaked at 253 MiB on a real forge-quick job, where
# memguard kills it at 85% of 256 MiB; the selector then silently ran hosted every time.
FORGE_QUICK_SEEDED_UV = "0.13.7"


class ActionPinsSeededUvTest(unittest.TestCase):
    def test_setup_uv_step_pins_the_seeded_uv_version(self) -> None:
        action = (Path(__file__).resolve().parents[2] / "actions" / "pool-select" / "action.yml").read_text(
            encoding="utf-8"
        )
        step = action.split("uses: astral-sh/setup-uv@", 1)[1].split("\n    - ", 1)[0]
        self.assertIn(f'version: "{FORGE_QUICK_SEEDED_UV}"', step)
