"""`_load_event` falls back to GITHUB_EVENT_PATH (template-python-rust-cmd#19:
title-edit reuse silently never fired because the plan step omitted --event)."""

import json
import os
import tempfile
import unittest
from unittest import mock

from ci_lint.cli import _head_sha_from_event, _load_event


class EventPathFallbackTest(unittest.TestCase):
    def test_uses_github_event_path_when_no_flag(self) -> None:
        payload = {"pull_request": {"title": "[ci-windows] x", "head": {"sha": "a" * 40}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "event.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": path}):
                event = _load_event(None)
        self.assertEqual(_head_sha_from_event(event), "a" * 40)

    def test_explicit_flag_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = os.path.join(tmp, "explicit.json")
            with open(explicit, "w", encoding="utf-8") as fh:
                json.dump({"pull_request": {"head": {"sha": "b" * 40}}}, fh)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": os.path.join(tmp, "missing.json")}):
                event = _load_event(explicit)
        self.assertEqual(_head_sha_from_event(event), "b" * 40)

    def test_no_flag_no_env_is_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_load_event(None).payload, {})


if __name__ == "__main__":
    unittest.main()
