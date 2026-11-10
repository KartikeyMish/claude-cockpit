"""Dashboard tests: pure key handling, drawing helpers, and a real pty smoke run."""

import os
import pty
import select
import signal
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from cockpit.core import Session  # noqa: E402
from cockpit.tui import (  # noqa: E402
    Snapshot, UiState, View, bar, fit, footer_hint, handle_key, name_cell, session_columns, state_glyph, visible,
)

NOW = 10_000_000_000
HOUR_MS = 3600 * 1000


def utf8_locale():
    """A UTF-8 locale this machine has: en_US.UTF-8 isn't generated on every Linux."""
    import locale
    saved = locale.setlocale(locale.LC_ALL)
    try:
        for name in ("en_US.UTF-8", "C.UTF-8", "C.utf8"):
            try:
                locale.setlocale(locale.LC_ALL, name)
                return name
            except locale.Error:
                continue
        return "C.UTF-8"
    finally:
        locale.setlocale(locale.LC_ALL, saved)


UTF8 = utf8_locale()


def session(pid, status="idle", idle_h=1.0, sdk=False, kb=1024 * 200):
    return Session(pid=pid, name=f"s{pid}", status=status, entrypoint="sdk-ts" if sdk else "cli",
                   cwd="/tmp", status_updated_ms=int(NOW - idle_h * HOUR_MS), rss_kb=kb, tree_rss_kb=kb)


def snap(*sessions, protected=()):
    return Snapshot(sessions=tuple(sessions), now=NOW, protected=frozenset(protected))


class VisibleOrderTests(unittest.TestCase):
    def test_busy_first_then_recent_then_stale_and_sdk_hidden(self):
        s = snap(session(1, idle_h=20), session(2, status="busy"), session(3, idle_h=0.1), session(4, sdk=True))
        self.assertEqual([x.pid for x in visible(s, UiState())], [2, 3, 1])
        self.assertEqual(len(visible(s, UiState(show_sdk=True))), 4)


class KeyHandlingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = snap(session(1, idle_h=10), session(2, idle_h=1), session(3, status="busy"),
                             session(9, idle_h=0.01), protected={9})
        self.rows = visible(self.snapshot, UiState())

    def press(self, state, *keys):
        action = None
        for key in keys:
            state, action = handle_key(state, key, self.rows, self.snapshot)
        return state, action

    def test_navigation_is_clamped(self):
        state, _ = self.press(UiState(), "KEY_UP", "KEY_UP")
        self.assertEqual(state.selected, 0)
        state, _ = self.press(UiState(), *["j"] * 10)
        self.assertEqual(state.selected, len(self.rows) - 1)

    def test_enter_launches_and_q_quits(self):
        self.assertEqual(self.press(UiState(), "\n")[1], ("launch",))
        self.assertEqual(self.press(UiState(), "q")[1], ("quit",))

    def test_close_requires_confirmation_and_targets_selected_row(self):
        index = next(i for i, s in enumerate(self.rows) if s.pid == 1)
        state, action = self.press(UiState(selected=index), "x")
        self.assertEqual((state.mode, action), ("confirm-close", None))
        self.assertIn("stays resumable", state.message)
        state, action = self.press(state, "y")
        self.assertEqual(action, ("close", 1))
        self.assertEqual(state.mode, "main")

    def test_any_other_key_cancels_confirmation(self):
        index = next(i for i, s in enumerate(self.rows) if s.pid == 1)
        state, _ = self.press(UiState(selected=index), "x")
        self.assertEqual(state.mode, "confirm-close")
        state, action = self.press(state, "n")
        self.assertIsNone(action)
        self.assertEqual(state.message, "Cancelled.")

    def test_refuses_to_close_busy_or_own_session(self):
        busy = next(i for i, s in enumerate(self.rows) if s.pid == 3)
        mine = next(i for i, s in enumerate(self.rows) if s.pid == 9)
        for index, phrase in ((busy, "working"), (mine, "this session")):
            state, action = self.press(UiState(selected=index), "x")
            self.assertEqual(state.mode, "main")
            self.assertIn(phrase, state.message)

    def test_gc_offers_only_when_something_is_stale(self):
        state, _ = self.press(UiState(), "g")
        self.assertEqual(state.mode, "confirm-gc")
        self.assertIn("Close 1 sessions", state.message)
        _, action = self.press(state, "y")
        self.assertEqual(action, ("gc",))
        fresh = snap(session(2, idle_h=1))
        state, _ = handle_key(UiState(), "g", visible(fresh, UiState()), fresh)
        self.assertEqual(state.mode, "main")
        self.assertIn("lean", state.message)

    def test_worktree_input_filters_characters(self):
        state, _ = self.press(UiState(), "w", "f", "i", "x", " ", "$", "/", "1", "KEY_BACKSPACE")
        self.assertEqual(state.buffer, "fix/")
        state, action = self.press(state, "\n")
        self.assertEqual(action, ("worktree", "fix"))

    def test_worktree_escape_and_empty_name(self):
        state, action = self.press(UiState(), "w", "\n")
        self.assertIsNone(action)
        self.assertEqual(state.mode, "input-wt")
        state, _ = self.press(state, "\x1b")
        self.assertEqual(state.mode, "main")

    def test_toggles(self):
        state, _ = self.press(UiState(), "a", "t")
        self.assertTrue(state.show_sdk)
        self.assertFalse(state.show_tiers)


class DrawingHelperTests(unittest.TestCase):
    def test_bar(self):
        self.assertEqual(bar(0, 4), "····")
        self.assertEqual(bar(1, 4), "████")
        self.assertEqual(bar(0.5, 4), "██··")
        self.assertEqual(len(bar(0.33, 10)), 10)
        self.assertEqual(bar(5, 3), "███")

    def test_fit(self):
        self.assertEqual(fit("abcdef", 4), "abc…")
        self.assertEqual(fit("ab", 4), "ab")
        self.assertEqual(fit("ab", 0), "")

    def test_state_glyph(self):
        self.assertEqual(state_glyph(session(1, status="busy"), NOW, 0)[1], "busy")
        self.assertEqual(state_glyph(session(1, idle_h=10), NOW, 0), ("◌", "stale"))
        self.assertEqual(state_glyph(session(1, idle_h=1), NOW, 0), ("○", "idle"))


class FakeScreen:
    """Just enough of a curses window to render rows into a character grid."""

    def __init__(self, height, width):
        self.height, self.width = height, width
        self.grid = [[" "] * width for _ in range(height)]

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, _attr=0):
        for offset, char in enumerate(text):
            if x + offset < self.width:
                self.grid[y][x + offset] = char

    def line(self, y):
        return "".join(self.grid[y]).rstrip()


def render_row(s, width, hooks_stale=()):
    view = View.__new__(View)  # skip curses colour setup: attr() falls back to plain text
    view.screen, view.colors, view.tiers = FakeScreen(3, width), {}, []
    snapshot = Snapshot(sessions=(s,), now=NOW, hooks_stale=frozenset(hooks_stale))
    view._session_row(0, s, snapshot, False, 0, *session_columns(width))
    return view.screen.line(0)


class StaleHooksMarkerTests(unittest.TestCase):
    def test_marker_keeps_every_column_at_80_and_120(self):
        for width in (80, 120):
            with self.subTest(width=width):
                s = session(7, idle_h=2)
                plain, marked = render_row(s, width), render_row(s, width, {7})
                self.assertIn("s7", plain)
                self.assertNotIn("⟳", plain)
                self.assertIn("s7 ⟳", marked)
                self.assertEqual(marked.count("⟳"), 1)
                self.assertLessEqual(len(marked), width)
                self.assertEqual(len(plain), len(marked))           # same right edge: SAVED intact
                self.assertEqual(plain.index("idle"), marked.index("idle"))

    def test_long_names_are_cut_to_make_room_for_the_marker(self):
        name_w, _ = session_columns(80)
        long = Session(**dict(session(7).__dict__, name="a-very-long-session-name-indeed"))
        cell = name_cell(long, name_w, True)
        self.assertEqual(len(cell), name_w - 1)
        self.assertTrue(cell.endswith("… ⟳"))
        self.assertEqual(len(name_cell(long, name_w, False)), name_w - 1)
        self.assertIn("⟳", render_row(long, 80, {7}))

    def test_footer_hint_only_while_a_listed_session_is_stale(self):
        rows = [session(1), session(2)]
        self.assertEqual(footer_hint(Snapshot(sessions=tuple(rows), now=NOW), rows), "")
        hint = footer_hint(Snapshot(sessions=tuple(rows), now=NOW, hooks_stale=frozenset({2})), rows)
        self.assertIn("restart to load current hooks: exit, then claude --resume", hint)
        self.assertEqual(footer_hint(Snapshot(now=NOW, hooks_stale=frozenset({9})), rows), "")


@unittest.skipUnless(sys.platform != "win32", "needs a POSIX pty")
class DashboardSmokeTests(unittest.TestCase):
    """Starts the real dashboard in a pseudo-terminal, presses keys, quits."""

    def run_dashboard(self, keys, rows=40, cols=140, timeout=8.0):
        import fcntl
        import termios

        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", LANG=UTF8, LC_ALL=UTF8)
            os.execv(sys.executable, [sys.executable, str(ROOT / "bin" / "ccx")])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(pid, signal.SIGWINCH)
        output, deadline, pending = b"", time.monotonic() + timeout, list(keys)
        next_key_at = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            # The UI animates, so it is never quiet: type on a timer once it has drawn.
            if pending and b"\x1b[?1049h" in output:
                now = time.monotonic()
                if next_key_at is None:
                    next_key_at = now + 0.6
                elif now >= next_key_at:
                    os.write(fd, pending.pop(0).encode())
                    next_key_at = now + 0.25
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                os.close(fd)
                return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")
        # The pty closes (EIO) when the child exits; collect its status before judging.
        for _ in range(20):
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                os.close(fd)
                return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        self.fail(f"dashboard did not exit; last output:\n{output[-2000:].decode('utf-8', 'replace')}")

    def test_opens_navigates_toggles_and_quits_cleanly(self):
        code, screen = self.run_dashboard(["j", "t", "t", "a", "w", "\x1b", "x", "n", "q"])
        self.assertEqual(code, 0)
        for text in ("claude-cockpit", "SESSIONS", "AGENT TIERS", "architect", "worktree"):
            self.assertIn(text, screen)

    def test_tiny_window_shows_hint_instead_of_crashing(self):
        code, screen = self.run_dashboard(["q"], rows=10, cols=40)
        self.assertEqual(code, 0)
        self.assertIn("bigger", screen)


class NonInteractiveFallbackTests(unittest.TestCase):
    def test_piped_ccx_prints_list_instead_of_ui(self):
        out = subprocess.run([sys.executable, str(ROOT / "bin" / "ccx")], capture_output=True,
                             text=True, timeout=30, stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("\x1b[?1049h", out.stdout)  # never enters the alternate screen


if __name__ == "__main__":
    unittest.main()
