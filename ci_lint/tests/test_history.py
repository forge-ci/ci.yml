"""`ci-lint history` (zackees/ci.yml#99): diff recorded fleet scans.

Fixtures under fixtures/HISTORY are three hand-written `fleet scan --json`
documents: the first dated only by its filename, the others by
`scanned_at`. Between scan 2 and 3: a's RUN-001 resolves, a's GEN-008 is
new, a's FLEET-001 persists since scan 1, b's GEN-006 returns (new again,
first seen restarts), and c is not in scan 3 at all (unscanned, not
resolved).
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ci_lint import cli
from ci_lint.history import build_history, load_snapshot, scan_date
from ci_lint.tests.helpers import FIXTURES

D = FIXTURES / "HISTORY"
FILES = [D / "third.json", D / "scan-2026-10-01.json", D / "second.json"]  # deliberately unordered


def _states(report):
    return {(e.repo, e.rule): (e.state, e.first_seen, e.last_seen) for e in report.entries}


class HistoryTest(unittest.TestCase):
    def test_states_and_first_seen(self) -> None:
        got = _states(build_history([load_snapshot(p) for p in FILES]))
        self.assertEqual(
            got,
            {
                ("zackees/a", "FLEET-001"): ("persisting", "2026-10-01", "2026-10-03"),
                ("zackees/a", "GEN-008"): ("new", "2026-10-03", "2026-10-03"),
                ("zackees/a", "RUN-001"): ("resolved", "2026-10-01", "2026-10-02"),
                ("zackees/b", "GEN-006"): ("new", "2026-10-03", "2026-10-03"),
                ("zackees/c", "SEC-007"): ("unscanned", "2026-10-01", "2026-10-02"),
            },
        )

    def test_two_scans_only(self) -> None:
        got = _states(build_history([load_snapshot(D / "scan-2026-10-01.json"), load_snapshot(D / "second.json")]))
        self.assertEqual(got[("zackees/b", "GEN-006")], ("resolved", "2026-10-01", "2026-10-01"))

    def test_needs_two(self) -> None:
        with self.assertRaises(ValueError):
            build_history([load_snapshot(FILES[0])])

    def test_undated(self) -> None:
        with self.assertRaises(ValueError):
            scan_date(Path("scan.json"), {"repos": []})

    def test_cli_json_deterministic(self) -> None:
        outs = []
        for order in (FILES, list(reversed(FILES))):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["history", "--json", *map(str, order)])
            self.assertEqual(rc, 1)  # b's GEN-006 is a new violation
            outs.append(buf.getvalue())
        self.assertEqual(outs[0], outs[1])
        doc = json.loads(outs[0])
        self.assertEqual(doc["counts"], {"new": 2, "persisting": 1, "resolved": 1, "unscanned": 1})

    def test_cli_text(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["history", *map(str, FILES)])
        self.assertIn("first seen 2026-10-01", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
