"""Cost and context math: prices, de-duplication, windows, alerts."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from cockpit.core import Session  # noqa: E402
from cockpit.pricing import (  # noqa: E402
    context_level, context_window, fmt_usd, price_for, prompt_tokens, request_cost,
)
from cockpit.team import Accumulator, UsageBook  # noqa: E402
from cockpit.tui import Snapshot, UiState, state_glyph, visible  # noqa: E402

M = 1_000_000


def cost(model, **usage):
    return request_cost(model, usage)


class PriceTableTests(unittest.TestCase):
    def test_longest_prefix_wins(self):
        self.assertEqual(price_for("claude-opus-5-5").input, 4)
        self.assertEqual(price_for("claude-opus-5").input, 5)
        self.assertEqual(price_for("claude-haiku-4-5-20251001").output, 5)
        self.assertEqual(price_for("claude-opus-4-1-20250805").input, 15)
        self.assertIsNone(price_for("gpt-5"))
        self.assertIsNone(price_for(""))

    def test_each_token_category_on_opus_55(self):
        self.assertAlmostEqual(cost("claude-opus-5-5", input_tokens=M), 4.0)
        self.assertAlmostEqual(cost("claude-opus-5-5", output_tokens=M), 20.0)
        self.assertAlmostEqual(cost("claude-opus-5-5", cache_read_input_tokens=M), 0.20)
        self.assertAlmostEqual(cost("claude-opus-5-5", cache_creation_input_tokens=M), 5.0)  # 5m default
        self.assertAlmostEqual(cost("claude-opus-5-5", cache_creation_input_tokens=M,
                                    cache_creation={"ephemeral_1h_input_tokens": M}), 8.0)

    def test_fast_mode_and_us_only_multipliers(self):
        self.assertAlmostEqual(cost("claude-opus-5-5", output_tokens=M, speed="fast"), 40.0)
        self.assertAlmostEqual(cost("claude-sonnet-5-5", output_tokens=M, inference_geo="us"), 11.0)
        self.assertAlmostEqual(cost("claude-sonnet-5-5", output_tokens=M, inference_geo="global"), 10.0)

    def test_haiku_55_long_prompt_tier(self):
        self.assertAlmostEqual(cost("claude-haiku-5-5", input_tokens=100_000, output_tokens=M), 0.01 + 0.50)
        long = cost("claude-haiku-5-5", input_tokens=1000, cache_read_input_tokens=100_000, output_tokens=M)
        self.assertAlmostEqual(long, 1000 * 0.50 / M + 100_000 * 0.05 / M + 2.50)

    def test_placeholders_and_unknown_models(self):
        self.assertEqual(cost("<synthetic>", input_tokens=0, output_tokens=0), 0.0)
        self.assertIsNone(cost("mystery-model", output_tokens=10))
        self.assertEqual(request_cost("claude-opus-5-5", None), 0.0)
        self.assertEqual(prompt_tokens({"input_tokens": 1, "cache_read_input_tokens": 2,
                                        "cache_creation_input_tokens": 3, "output_tokens": 99}), 6)


class ContextTests(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(context_window("claude-opus-5-5"), 1_000_000)
        self.assertEqual(context_window("claude-sonnet-4-6"), 1_000_000)
        self.assertEqual(context_window("claude-sonnet-4-5-20250929"), 200_000)
        self.assertEqual(context_window("claude-sonnet-4-20250514"), 200_000)
        self.assertEqual(context_window("claude-sonnet-4-5", observed_tokens=300_000), 1_000_000)

    def test_levels_and_money_format(self):
        self.assertEqual([context_level(p) for p in (10, 70, 84.9, 85, 99)],
                         ["ok", "warn", "warn", "critical", "critical"])
        self.assertEqual([fmt_usd(v) for v in (None, 0, 0.004, 0.5, 13.834, 347.2)],
                         ["?", "$0", "<$0.01", "$0.50", "$13.83", "$347"])


def record(msg_id, block, usage=None, model="claude-opus-5-5"):
    return {"type": "assistant", "timestamp": "2026-11-07T10:00:00Z",
            "message": {"id": msg_id, "model": model, "content": [block],
                        "usage": usage or {"input_tokens": 0, "cache_read_input_tokens": M, "output_tokens": 0}}}


class DedupeTests(unittest.TestCase):
    def test_one_response_logged_as_several_records_counts_once(self):
        acc = Accumulator()
        acc.feed(record("msg_1", {"type": "thinking", "thinking": "…"}), "main")
        acc.feed(record("msg_1", {"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "/a"}}), "main")
        acc.feed(record("msg_1", {"type": "text", "text": "done"}), "main")
        self.assertAlmostEqual(acc.cost_usd, 0.20)           # one cache read of 1M, not three
        self.assertEqual((acc.tool_calls, acc.report), (1, "done"))  # but every block counts
        acc.feed(record("msg_2", {"type": "text", "text": "again"}), "main")
        self.assertAlmostEqual(acc.cost_usd, 0.40)
        self.assertAlmostEqual(acc.last_turn_usd, 0.20)

    def test_synthetic_messages_do_not_change_model_or_cost(self):
        acc = Accumulator()
        acc.feed(record("a", {"type": "text", "text": "x"}), "main")
        acc.feed(record("b", {"type": "text", "text": "interrupted"}, model="<synthetic>",
                        usage={"input_tokens": 0, "output_tokens": 0}), "main")
        self.assertEqual(acc.model, "claude-opus-5-5")
        self.assertTrue(acc.cost_known)


class UsageBookTests(unittest.TestCase):
    def test_session_usage_adds_agents_and_forgets_dead_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "-p" / "s1"
            base.parent.mkdir(parents=True)
            usage = {"input_tokens": 0, "cache_read_input_tokens": 500_000, "output_tokens": 0}
            base.with_suffix(".jsonl").write_text(json.dumps(record("m1", {"type": "text", "text": "hi"}, usage)) + "\n")
            (base / "subagents").mkdir(parents=True)
            (base / "subagents" / "agent-x.jsonl").write_text(
                json.dumps(record("m2", {"type": "text", "text": "ok"}, usage, model="claude-sonnet-5-5")) + "\n")
            (base / "subagents" / "agent-x.meta.json").write_text("{}")
            book = UsageBook(Path(tmp))
            found = book.usage("s1")
            self.assertEqual(found.context_tokens, 500_000)
            self.assertAlmostEqual(found.pct, 50.0)
            self.assertAlmostEqual(found.cost_usd, 0.10 + 0.05)
            self.assertAlmostEqual(found.agents_usd, 0.05)
            self.assertIsNone(book.usage("missing"))
            book.keep_only({"s1"})
            self.assertEqual(set(book.watchers), {"s1"})


def session(pid, status="idle", sdk=False):
    return Session(pid=pid, name=f"s{pid}", status=status, entrypoint="sdk-ts" if sdk else "cli", cwd="/x",
                   status_updated_ms=9_000_000, rss_kb=1, tree_rss_kb=1, session_id=f"id{pid}")


class DashboardAlertTests(unittest.TestCase):
    def test_context_alerts_fullest_first_and_interactive_only(self):
        from cockpit.team import SessionUsage

        def usage(tokens):
            return SessionUsage("claude-opus-5-5", tokens, 1_000_000, 1.0, 0.0, 0.1, True, False)

        snap = Snapshot(sessions=(session(1), session(2), session(3), session(4, sdk=True)), now=10_000_000,
                        usage={1: usage(720_000), 2: usage(900_000), 3: usage(100_000), 4: usage(990_000)})
        self.assertEqual([s.pid for s, _ in snap.context_alerts()], [2, 1])
        self.assertAlmostEqual(snap.spend_usd, 4.0)

    def test_same_conversation_in_two_terminals_is_counted_once(self):
        from dataclasses import replace as dc_replace
        from cockpit.core import duplicate_of
        from cockpit.team import SessionUsage
        a, b, c = session(5905), dc_replace(session(8807), session_id="id5905"), session(7)
        self.assertEqual(duplicate_of([b, a, c]), {8807: 5905})
        u = SessionUsage("claude-opus-5-5", 1, 1_000_000, 75.72, 0.0, 0.1, True, False)
        snap = Snapshot(sessions=(a, b, c), now=10_000_000, usage={5905: u, 8807: u, 7: u})
        self.assertAlmostEqual(snap.spend_usd, 75.72 * 2)

    def test_waiting_sessions_sort_first_and_blink(self):
        snap = Snapshot(sessions=(session(1, "busy"), session(2, "waiting"), session(3)), now=10_000_000)
        self.assertEqual([s.pid for s in visible(snap, UiState())], [2, 1, 3])
        self.assertEqual(state_glyph(session(2, "waiting"), 0, 0)[1], "warn")


if __name__ == "__main__":
    unittest.main()
