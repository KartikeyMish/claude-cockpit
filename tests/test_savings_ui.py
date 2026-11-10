"""Savings in the dashboard row, the team view header and `ccx savings` (report + text)."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from cockpit.core import Session  # noqa: E402
from cockpit.pricing import fmt_money, fmt_signed_usd  # noqa: E402
from cockpit.savings import SessionSavings, session_savings  # noqa: E402
from cockpit.savings_report import build_report, project_label, render_text, report_json  # noqa: E402
from cockpit.team import SessionUsage, TeamWatcher, UsageBook  # noqa: E402
from cockpit.team_view import TeamScreen, TeamState, savings_lines  # noqa: E402
from cockpit.tui import Snapshot, UiState, View, saved_cell, session_columns, sessions_header  # noqa: E402
from test_savings import (  # noqa: E402
    call, opus_background_session, sonnet_scout_session, spawn, tool_result, write_session,
)

NOW_MS = 10_000_000


class FakeScreen:
    def __init__(self, height, width):
        self.height, self.width = height, width
        self.grid = [[" "] * width for _ in range(height)]

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, attr=0):
        for i, ch in enumerate(text):
            if x + i < self.width:
                self.grid[y][x + i] = ch

    def erase(self):
        self.grid = [[" "] * self.width for _ in range(self.height)]

    def refresh(self):
        pass

    def line(self, y):
        return "".join(self.grid[y]).rstrip()


def fake_view(height, width):
    view = View.__new__(View)  # skip curses colour setup: attr() falls back to 0
    view.screen, view.colors, view.tiers = FakeScreen(height, width), {}, []
    return view


def savings_for(main, agents):
    with tempfile.TemporaryDirectory() as tmp:
        write_session(Path(tmp), main, agents)
        return session_savings("sess", TeamWatcher("sess", Path(tmp)).collect())


def session(pid=1, name="api@main"):
    return Session(pid=pid, name=name, status="idle", entrypoint="cli", cwd="/Users/me/api",
                   status_updated_ms=NOW_MS - 60_000, rss_kb=400_000, tree_rss_kb=400_000, session_id=f"id{pid}")


def render_row(width, savings, usage=True):
    view = fake_view(20, width)
    s = session()
    snap = Snapshot(sessions=(s,), now=NOW_MS,
                    usage={1: SessionUsage("claude-opus-5-5", 412_000, 1_000_000, 13.83, 1.2, 0.3, True, False)}
                    if usage else {},
                    savings={1: savings} if savings else {})
    name_w, dir_w = session_columns(width)
    view._session_row(0, s, snap, False, 0, name_w, dir_w)
    return view.screen.line(0), sessions_header(name_w, dir_w)


class MoneyFormatTests(unittest.TestCase):
    def test_signed_and_minus_formats(self):
        self.assertEqual([fmt_signed_usd(v) for v in (0, 0.004, 1.2, -0.41, 218.33)],
                         ["$0", "+<$0.01", "+$1.20", "−$0.41", "+$218"])
        self.assertEqual([fmt_money(v) for v in (3.5, -3.5)], ["$3.50", "−$3.50"])


class DashboardRowTests(unittest.TestCase):
    def test_saved_cell_signs_and_markers(self):
        positive = savings_for(*sonnet_scout_session())
        flagged = savings_for(*opus_background_session(main_model="claude-sonnet-4-5"))
        self.assertEqual(saved_cell(positive), ("+$0.04", "ok"))
        self.assertTrue(saved_cell(flagged)[0].endswith("!"))
        self.assertEqual(saved_cell(None), ("-", "dim"))
        self.assertEqual(saved_cell(savings_for([call(0, "m1", read=10, out=1)], {})), ("-", "dim"))

    def test_negative_and_partial_session(self):
        brief = {"prompt": "y" * 3986}
        main = [call(0, "m1", read=20_000, out=1000, blocks=[spawn("sp3", brief)]),
                tool_result(10, "sp3", "t1", 40), call(11, "m2", read=21_000, out=50, stop="end_turn")]
        agents = {"t1": ({"toolUseId": "sp3"}, [call(1, "t-k1", w5=15_000, out=200)]),
                  "u1": ({"toolUseId": "nope"}, [call(1, "u-k1", model="claude-mystery", w5=10, out=1)])}
        text, tone = saved_cell(savings_for(main, agents))
        self.assertEqual((text, tone), ("−$0.11~", "warn"))

    def test_row_fits_80_columns_by_dropping_dir(self):
        self.assertEqual(session_columns(80), (16, 0))
        self.assertEqual(session_columns(86), (16, 0))
        self.assertEqual(session_columns(87), (16, 8))
        row, header = render_row(80, savings_for(*opus_background_session(main_model="claude-sonnet-4-5")))
        self.assertLessEqual(len(row), 79)
        self.assertLessEqual(len(header) + 2, 80)
        self.assertNotIn("DIR", header)
        self.assertTrue(row.rstrip().endswith("!"))
        self.assertEqual(header.rstrip()[-5:], "SAVED")
        self.assertEqual(row.index("$13.83") + len("$13.83"), 2 + header.index("COST") + len("COST"))
        self.assertEqual(len(row), 2 + len(header))  # SAVED cell ends under the header's edge

    def test_wide_row_keeps_dir_and_aligns_saved_under_header(self):
        row, header = render_row(140, savings_for(*sonnet_scout_session()))
        self.assertIn("DIR", header)
        self.assertIn("/api", row)
        # header is drawn from column 2; row cells are right-aligned to the same edges
        self.assertEqual(row.index("+$0.04") + len("+$0.04"), 2 + header.index("SAVED") + len("SAVED"))
        self.assertEqual(row.index("$13.83") + len("$13.83"), 2 + header.index("COST") + len("COST"))

    def test_session_without_usage_still_aligns(self):
        row, header = render_row(100, None, usage=False)
        self.assertEqual(len(row), 2 + len(header.rstrip()))


class TeamHeaderTests(unittest.TestCase):
    def test_header_line_text(self):
        lines = savings_lines(savings_for(*opus_background_session()))
        self.assertEqual(lines, [("saved ≈$0.21 (17%) · mix $0 · ctx $0.25 · overhead −$0.03 · "
                                  "162k kept out of main", "ok")])
        self.assertEqual(savings_lines(None), [])

    def test_compaction_note(self):
        lines = savings_lines(savings_for(*opus_background_session(main_model="claude-sonnet-4-5")))
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1][0], "inline, main would have reached ≈222k of 200k and compacted: "
                                      "treat ctx as an upper bound")

    def test_team_screen_draws_the_savings_line(self):
        main, agents = opus_background_session()
        with tempfile.TemporaryDirectory() as tmp:
            write_session(Path(tmp), main, agents)
            watcher = TeamWatcher("sess", Path(tmp))
            team = watcher.snapshot()
            saved = session_savings("sess", watcher.collect())
        view = fake_view(30, 120)
        TeamScreen(view, "api@main").draw(team, TeamState(), 0, saved)
        screen = [view.screen.line(y) for y in range(30)]
        self.assertTrue(any(line.strip().startswith("saved ≈$0.21 (17%)") for line in screen))
        self.assertTrue(any("AGENTS" in line for line in screen))


class UsageBookCacheTests(unittest.TestCase):
    def test_savings_recomputed_only_when_a_transcript_grows(self):
        main, agents = opus_background_session()
        with tempfile.TemporaryDirectory() as tmp:
            base = write_session(Path(tmp), main[:3], agents)
            book = UsageBook(Path(tmp))
            first = book.savings("sess")
            self.assertIs(book.savings("sess"), first)  # nothing new: cached object
            with base.with_suffix(".jsonl").open("a") as handle:
                handle.write("".join(json.dumps(r) + "\n" for r in main[3:]))
            grown = book.savings("sess")
            self.assertIsNot(grown, first)
            self.assertEqual(grown.runs[0].later_main_calls, 10)
            book.keep_only(set())
            self.assertEqual((book.watchers, book.saved), ({}, {}))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_carries_savings(self):
        snap = Snapshot(sessions=(session(),), now=NOW_MS, savings={1: savings_for(*sonnet_scout_session())})
        self.assertIsInstance(snap.savings[1], SessionSavings)
        self.assertEqual(UiState().selected, 0)


def make_projects(tmp: Path) -> Path:
    projects = tmp / "projects"
    write_session(projects, *sonnet_scout_session(), sid="s-scout")
    write_session(projects, *opus_background_session(main_model="claude-sonnet-4-5"), sid="s-flagged")
    write_session(projects, *opus_background_session(), sid="s-opus")
    write_session(projects, [call(0, "m1", read=100, out=1, stop="end_turn")], {}, sid="s-plain")
    old = write_session(projects, *opus_background_session(), sid="s-old")
    stamp = time.time() - 30 * 86400
    os.utime(old.with_suffix(".jsonl"), (stamp, stamp))
    return projects


class ReportTests(unittest.TestCase):
    def test_rows_totals_and_compact_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_report(make_projects(Path(tmp)), days=7)
        self.assertEqual(report.scanned, 4)  # s-old is outside the window
        self.assertEqual([r.savings.session_id for r in report.rows], ["s-opus", "s-flagged", "s-scout"])
        self.assertEqual((report.total.sessions, report.total.runs, report.total.would_compact), (3, 3, 1))
        self.assertEqual(report.without_compact.sessions, 2)
        self.assertAlmostEqual(report.total.saved_usd, sum(r.savings.saved_usd for r in report.rows))
        self.assertAlmostEqual(report.without_compact.saved_usd, 0.214032 + 0.039462)
        self.assertAlmostEqual(report.total.context_usd,
                               report.total.work_in_context_usd + report.total.carry_usd)

    def test_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_report(make_projects(Path(tmp)), days=7)
        text = render_text(report)
        for needle in ("last 7 days", "3 of 4 sessions ran agents", "all sessions", "without would-compact",
                       "1 session would have overflowed", "Top 3 sessions", "s-opus", "compact",
                       'README "Measuring savings"'):
            self.assertIn(needle, text)
        data = json.loads(json.dumps(report_json(report)))
        self.assertEqual(data["totals"]["without_would_compact"]["sessions"], 2)
        self.assertEqual(len(data["sessions"][0]["runs"]), 1)
        self.assertIn("saved_usd", data["sessions"][0]["runs"][0])

    def test_empty_window_and_bad_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_report(Path(tmp), days=1)
            self.assertIn("nothing to compare", render_text(report))
            self.assertIsNone(report.total.pct)
            with self.assertRaises(ValueError):
                build_report(Path(tmp), days=0)

    def test_project_label(self):
        home = Path("/Users/me")
        self.assertEqual(project_label("-Users-me-api", home), "api")
        self.assertEqual(project_label("-Users-me", home), "~")
        self.assertEqual(project_label("-private-tmp", home), "-private-tmp")


class CliTests(unittest.TestCase):
    def test_parse_savings_args(self):
        from cockpit import cli
        from cockpit.core import CockpitError
        self.assertEqual(cli.parse_savings_args([]), (7.0, False))
        self.assertEqual(cli.parse_savings_args(["--days", "30", "--json"]), (30.0, True))
        self.assertEqual(cli.parse_savings_args(["--days=0.5"]), (0.5, False))
        for bad in (["--bogus"], ["--days"], ["--days", "0"], ["--days", "x"], ["--days", "-3"], ["7"]):
            with self.assertRaises(CockpitError, msg=bad):
                cli.parse_savings_args(bad)

    def test_unknown_flag_exits_2(self):
        from cockpit import cli
        self.assertEqual(cli.main(["savings", "--bogus"]), 2)
        self.assertEqual(cli.main(["savings", "--days", "0"]), 2)

    def test_savings_command_text_and_json(self):
        import contextlib
        import io
        from unittest import mock
        from cockpit import cli
        with tempfile.TemporaryDirectory() as tmp:
            projects = make_projects(Path(tmp))
            with mock.patch("cockpit.savings_report.PROJECTS_DIR", projects):
                text, data = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(text):
                    self.assertEqual(cli.main(["savings", "--days", "7"]), 0)
                with contextlib.redirect_stdout(data):
                    self.assertEqual(cli.main(["savings", "--json"]), 0)
        self.assertIn("3 of 4 sessions ran agents", text.getvalue())
        self.assertEqual(json.loads(data.getvalue())["totals"]["all"]["sessions"], 3)

    def test_help_lists_savings(self):
        from cockpit import cli
        self.assertIn("ccx savings [--days N] [--json]", cli.__doc__)

    def test_ls_row_saved_cell_aligns_with_header(self):
        from cockpit import cli
        s = session()
        usage = SessionUsage("claude-opus-5-5", 412_000, 1_000_000, 13.83, 1.2, 0.3, True, False)
        flagged = savings_for(*opus_background_session(main_model="claude-sonnet-4-5"))
        row = cli.session_row(s, NOW_MS, usage, flagged)  # not a tty in tests: no colour codes
        header = cli.SESSIONS_HEADER
        self.assertEqual(row.index("+$0.17!") + len("+$0.17!"), header.index("SAVED") + len("SAVED"))
        self.assertEqual(row.index("$13.83") + len("$13.83"), header.index("COST") + len("COST"))
        bare = cli.session_row(s, NOW_MS)
        self.assertEqual(bare.index("api@main"), header.index("NAME"))

    def test_ls_json_fields(self):
        from cockpit import cli
        self.assertEqual(cli.savings_json(None), {"saved_usd": None, "saved_partial": False, "would_compact": False})
        flagged = cli.savings_json(savings_for(*opus_background_session(main_model="claude-sonnet-4-5")))
        self.assertEqual((flagged["saved_partial"], flagged["would_compact"]), (False, True))
        self.assertGreater(flagged["saved_usd"], 0)
        self.assertEqual(cli.saved_text(savings_for([call(0, "m1", read=10, out=1)], {})), "-")


if __name__ == "__main__":
    unittest.main()
