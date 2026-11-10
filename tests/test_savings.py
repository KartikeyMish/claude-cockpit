"""Delegation savings: hand-computed sessions read through the real transcript tailers.

Prices used (USD per million tokens):
  claude-opus-5-5   input 4, write 5m 5, write 1h 8, read 0.20, output 20
  claude-sonnet-5-5 input 2, write 5m 2.5, write 1h 4, read 0.10, output 10
Expected values below are written in micro-dollars (1e-6 USD) so the arithmetic is visible.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from cockpit.pricing import rate  # noqa: E402
from cockpit.savings import chars_to_tokens, inline_usage, session_savings  # noqa: E402
from cockpit.team import Accumulator, TeamWatcher, TranscriptTail, UsageBook  # noqa: E402

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
T0 = datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc)
U = 1e-6  # one micro-dollar
BRIEF_400 = {"prompt": "x" * 386}  # json.dumps(...) is exactly 400 chars = 160 tokens (chars / 2.5)


def iso(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def usage(inp=0, w5=0, w1h=0, read=0, out=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": w5 + w1h,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h},
            "cache_read_input_tokens": read, "output_tokens": out}


def call(ts, msg_id, model=OPUS, stop="tool_use", blocks=None, **tokens):
    return {"type": "assistant", "timestamp": iso(ts),
            "message": {"id": msg_id, "model": model, "stop_reason": stop,
                        "content": blocks or [{"type": "text", "text": "."}], "usage": usage(**tokens)}}


def spawn(tool_use_id, brief=BRIEF_400):
    return {"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": brief}


def tool_result(ts, tool_use_id, agent_id, chars, status="completed"):
    return {"type": "user", "timestamp": iso(ts),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                                     "content": [{"type": "text", "text": "r" * chars}]}]},
            "toolUseResult": {"status": status, "agentId": agent_id}}


def notification_text(agent_id, tool_use_id, chars):
    head = (f"<task-notification>\n<task-id>{agent_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
            "<status>completed</status>\n<result>")
    tail = "</result>\n</task-notification>"
    return head + "n" * (chars - len(head) - len(tail)) + tail


def notification(ts, agent_id, tool_use_id, chars):
    return {"type": "user", "timestamp": iso(ts), "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": notification_text(agent_id, tool_use_id, chars)}}


def queued_notification(ts, agent_id, tool_use_id, chars):
    return {"type": "attachment", "timestamp": iso(ts),
            "attachment": {"type": "queued_command", "commandMode": "task-notification",
                           "prompt": notification_text(agent_id, tool_use_id, chars)}}


def peer(ts, agent_id, chars, handback=True):
    origin = {"kind": "peer", "from": agent_id, "senderTaskId": agent_id}
    if handback:
        origin["handback"] = True
    return {"type": "user", "timestamp": iso(ts), "origin": origin, "message": {"role": "user", "content": "p" * chars}}


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def write_session(projects, main, agents, sid="sess"):
    """agents: {agent_id: (meta, records)}"""
    base = projects / "-proj" / sid
    write_jsonl(base.with_suffix(".jsonl"), main)
    for agent_id, (meta, records) in agents.items():
        write_jsonl(base / "subagents" / f"agent-{agent_id}.jsonl", records)
        (base / "subagents" / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
    return base


def savings_of(main, agents):
    with tempfile.TemporaryDirectory() as tmp:
        write_session(Path(tmp), main, agents)
        return session_savings("sess", TeamWatcher("sess", Path(tmp)).collect())


# ---------------------------------------------------------------- fixtures

def sonnet_scout_session():
    """Opus main (C = 100k) runs a Sonnet scout synchronously; 2 main calls follow the result."""
    main = [
        call(0, "m1", read=100_000, out=1000, blocks=[spawn("sp1")]),
        tool_result(60, "sp1", "s1", 400),                          # H = 160
        call(61, "m2", read=100_000, w1h=1000, out=100),            # main writes 1h cache
        call(62, "m3", read=101_000, out=100, stop="end_turn"),
    ]
    scout = [
        call(1, "s-k1", model=SONNET, inp=10, w5=9_990, out=500),                 # S = 10,000
        call(30, "s-k2", model=SONNET, inp=10, w5=5_490, read=10_000, out=1000),  # P = 15,500
    ]
    return main, {"s1": ({"agentType": "orchestrator:scout", "toolUseId": "sp1", "spawnDepth": 1}, scout)}


def opus_background_session(compact_at=None, main_model=OPUS):
    """Opus agent in the background with W = 162k, then 10 main calls; main acks the launch
    (end_turn, no tool) and is woken by the notification."""
    main = [
        call(0, "m1", model=main_model, read=50_000, out=10, blocks=[spawn("sp2")]),
        tool_result(0.5, "sp2", "o1", 200, status="async_launched"),            # L = 80
        call(1, "m2", model=main_model, read=50_000, out=40, stop="end_turn"),  # ack: 10,000 + 800
        notification(100, "o1", "sp2", 800),                                   # H = 320, main idle: wake
    ]
    for i in range(10):
        main.append(call(101 + i, f"m{3 + i}", model=main_model, read=60_000, out=100,
                         stop="end_turn" if i == 9 else "tool_use"))           # wake call m3: 14,000
    if compact_at is not None:
        main.append({"type": "system", "subtype": "compact_boundary", "timestamp": iso(compact_at)})
    agent = [
        call(2, "o-k1", w5=20_000, out=1000),                    # S = 20,000
        call(3, "o-k2", w5=100_000, read=20_000, out=1000),
        call(4, "o-k3", w5=60_000, read=120_000, out=2000),      # P_n = 180,000; W = 162,000
    ]
    return main, {"o1": ({"agentType": "orchestrator:senior-engineer", "toolUseId": "sp2", "spawnDepth": 1},
                         agent)}


# ---------------------------------------------------------------- tests

class HandComputedTests(unittest.TestCase):
    def test_sonnet_scout_under_opus_main_saves_through_model_mix(self):
        result = savings_of(*sonnet_scout_session())
        run = result.runs[0]
        self.assertEqual((run.baseline_tokens, run.work_tokens, run.handback_tokens, run.brief_tokens),
                         (10_000, 6_500, 160, 160))
        self.assertEqual((run.main_context, run.later_main_calls, run.handback), (100_000, 2, "result"))
        # actual (Sonnet): k1 20 + 24,975 + 5,000; k2 20 + 13,725 + 1,000 + 10,000
        self.assertAlmostEqual(run.actual_usd, 54_740 * U)
        # same tokens at Opus rates: k1 40 + 49,950 + 10,000; k2 40 + 27,450 + 2,000 + 20,000
        self.assertAlmostEqual(run.repriced_usd, 109_480 * U)
        # inline: k1 reads C (20,000) + 10,000 out; k2 40 + 27,450 + 100k read (20,000) + 20,000
        self.assertAlmostEqual(run.inline_work_usd, 97_490 * U)
        self.assertAlmostEqual(run.carry_usd, 2 * 6_340 * 0.20 * U)                     # 2,536
        # brief out 3,200 + (160 + 0 + 160) x 1h write 8 = 2,560 + 2 later calls x 160 x 0.20 = 64
        self.assertAlmostEqual(run.overhead_usd, 5_824 * U)
        self.assertAlmostEqual(run.model_mix_usd, 54_740 * U)
        self.assertAlmostEqual(run.context_usd, (97_490 - 109_480 + 2_536) * U)
        self.assertAlmostEqual(result.saved_usd, 39_462 * U)
        self.assertGreater(result.model_mix_usd, 0)
        self.assertAlmostEqual(result.pct, 100 * 39_462 / (97_490 + 2_536))
        self.assertEqual(result.kept_out_tokens, 6_340)
        self.assertFalse(result.partial)

    def test_opus_agent_with_big_work_and_many_later_calls_saves_through_context(self):
        result = savings_of(*opus_background_session())
        run = result.runs[0]
        self.assertEqual((run.work_tokens, run.handback_tokens, run.launch_tokens, run.later_main_calls),
                         (162_000, 320, 80, 10))
        self.assertEqual(run.handback, "notification")
        self.assertAlmostEqual(run.actual_usd, 1_008_000 * U)
        self.assertAlmostEqual(run.model_mix_usd, 0.0)
        self.assertAlmostEqual(run.inline_work_usd, 930_000 * U)    # 30,000 + 530,000 + 370,000
        self.assertAlmostEqual(run.carry_usd, 10 * 161_680 * 0.20 * U)
        # brief 3,200 + 560 x 5m write 5 = 2,800 + 11 calls x 240 x 0.20 = 528 + ack 10,800 + wake 14,000
        self.assertAlmostEqual(run.overhead_usd, 31_328 * U)
        self.assertAlmostEqual(run.context_usd, 245_360 * U)
        self.assertAlmostEqual(result.saved_usd, 214_032 * U)
        self.assertEqual(result.main_peak_tokens, 60_000)
        self.assertEqual(result.inline_peak_tokens, 60_000 + 161_680)
        self.assertFalse(result.would_compact)

    def test_tiny_agent_with_big_brief_and_few_later_calls_loses_money(self):
        brief = {"prompt": "y" * 3986}  # 4,000 chars = 1,600 tokens
        main = [call(0, "m1", read=20_000, out=1000, blocks=[spawn("sp3", brief)]),
                tool_result(10, "sp3", "t1", 40),                     # H = 16
                call(11, "m2", read=21_000, out=50, stop="end_turn")]
        agent = [call(1, "t-k1", w5=15_000, out=200)]                 # S = 15,000, W = 200
        result = savings_of(main, {"t1": ({"agentType": "routine-worker", "toolUseId": "sp3"}, agent)})
        run = result.runs[0]
        self.assertAlmostEqual(run.actual_usd, 79_000 * U)
        self.assertAlmostEqual(run.inline_work_usd, 8_000 * U)
        self.assertAlmostEqual(run.carry_usd, 184 * 0.20 * U)
        # brief out 32,000 + 1,616 x 5 = 8,080 + 1 call x 1,600 x 0.20 = 320
        self.assertAlmostEqual(run.overhead_usd, 40_400 * U)
        self.assertAlmostEqual(result.saved_usd, (8_000 + 36.8 - 79_000 - 40_400) * U)
        self.assertLess(result.saved_usd, 0)
        self.assertLess(result.pct, 0)


class HandbackFormatTests(unittest.TestCase):
    def test_queued_attachment_and_user_record_of_one_notification_count_once(self):
        main, agents = opus_background_session()
        main.insert(4, queued_notification(100, "o1", "sp2", 800))  # same text, logged twice
        main.insert(5, {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(100),
                        "content": notification_text("o1", "sp2", 800)})
        run = savings_of(main, agents).runs[0]
        self.assertEqual(run.handback_tokens, 320)

    def test_attachment_notification_alone_is_a_hand_back(self):
        main, agents = opus_background_session()
        main[3] = queued_notification(100, "o1", "sp2", 800)
        run = savings_of(main, agents).runs[0]
        self.assertEqual((run.handback, run.handback_tokens, run.later_main_calls), ("notification", 320, 10))

    def test_peer_report_and_interim_messages_all_count_toward_h(self):
        main, agents = opus_background_session()
        main[3:4] = [peer(50, "o1", 400, handback=False),        # interim: counts in H, not as the end
                     peer(100, "o1", 1200),                      # SubagentHandback report
                     notification(100.5, "o1", "sp2", 800)]      # pointer notification
        run = savings_of(main, agents).runs[0]
        self.assertEqual(run.handback_tokens, 160 + 480 + 320)
        self.assertEqual(run.handback, "notification")
        self.assertEqual(run.later_main_calls, 10)

    def test_peer_messages_from_other_sessions_are_ignored(self):
        main, agents = opus_background_session()
        main.insert(4, peer(100, "some-other-session", 4000))
        self.assertEqual(savings_of(main, agents).runs[0].handback_tokens, 320)

    def test_missing_hand_back_means_no_carry(self):
        main, agents = opus_background_session()
        del main[3]
        run = savings_of(main, agents).runs[0]
        self.assertEqual((run.later_main_calls, run.carry_usd, run.handback, run.handback_tokens), (0, 0.0, "", 0))

    def test_compaction_after_the_hand_back_stops_the_carry(self):
        run = savings_of(*opus_background_session(compact_at=105.5)).runs[0]
        self.assertEqual(run.later_main_calls, 5)

    def test_inline_peak_beyond_the_window_is_flagged(self):
        result = savings_of(*opus_background_session(main_model="claude-sonnet-4-5"))
        self.assertEqual(result.window, 200_000)
        self.assertEqual(result.inline_peak_tokens, 221_680)
        self.assertTrue(result.would_compact)


class NestingAndEdgeTests(unittest.TestCase):
    def test_nested_agent_is_folded_into_its_parent_run(self):
        main = [call(0, "m1", read=50_000, out=10, blocks=[spawn("spA")]),
                tool_result(50, "spA", "A", 400),
                call(51, "m2", read=50_000, out=10, stop="end_turn")]
        parent = [call(1, "a1", w5=20_000, out=100, blocks=[spawn("spB", {"prompt": "z"})]),
                  tool_result(20, "spB", "B", 800),                       # H_B = 320
                  call(21, "a2", w5=1_000, read=20_000, out=100)]         # W_A = 1,000 + 100
        child = [call(2, "b1", model=SONNET, w5=10_000, out=100),
                 call(3, "b2", model=SONNET, w5=5_000, read=10_000, out=900)]  # W_B = 5,900
        result = savings_of(main, {
            "A": ({"agentType": "senior-engineer", "toolUseId": "spA", "spawnDepth": 1}, parent),
            "B": ({"agentType": "scout", "toolUseId": "spB", "spawnDepth": 2}, child)})
        self.assertEqual(len(result.runs), 1)
        run = result.runs[0]
        self.assertEqual((run.agent_id, run.nested, run.calls), ("A", 1, 4))
        self.assertEqual(run.work_tokens, 1_100 + (5_900 - 320))
        child_actual = (10_000 * 2.5 + 100 * 10 + 5_000 * 2.5 + 10_000 * 0.10 + 900 * 10) * U
        parent_actual = (20_000 * 5 + 100 * 20 + 1_000 * 5 + 20_000 * 0.20 + 100 * 20) * U
        self.assertAlmostEqual(run.actual_usd, parent_actual + child_actual)
        self.assertGreater(run.model_mix_usd, 0)  # the Sonnet child repriced at Opus rates

    def test_unknown_model_marks_the_session_partial_and_leaves_the_run_out(self):
        main, agents = sonnet_scout_session()
        agents["s1"] = (agents["s1"][0], [call(1, "x1", model="claude-mystery-9", w5=10_000, out=10)])
        result = savings_of(main, agents)
        self.assertTrue(result.partial)
        self.assertFalse(result.runs[0].known)
        self.assertEqual((result.saved_usd, result.inline_usd), (0.0, 0.0))
        self.assertIsNone(result.pct)

    def test_session_without_agents_saves_nothing(self):
        result = savings_of([call(0, "m1", read=1000, out=10, stop="end_turn")], {})
        self.assertEqual((result.runs, result.saved_usd, result.kept_out_tokens), ((), 0.0, 0))
        self.assertIsNone(result.pct)
        self.assertAlmostEqual(result.session_usd, (1000 * 0.20 + 10 * 20) * U)

    def test_missing_session_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(UsageBook(Path(tmp)).savings("nope"))

    def test_usage_book_exposes_savings_and_stays_incremental(self):
        main, agents = opus_background_session()
        with tempfile.TemporaryDirectory() as tmp:
            base = write_session(Path(tmp), main[:3], agents)  # still running: no hand-back yet
            book = UsageBook(Path(tmp))
            early = book.savings("sess")
            self.assertEqual((early.runs[0].later_main_calls, early.runs[0].handback), (0, ""))
            with base.with_suffix(".jsonl").open("a") as handle:
                handle.write("".join(json.dumps(r) + "\n" for r in main[3:]))
            late = book.savings("sess")
            self.assertEqual(late.runs[0].later_main_calls, 10)
            self.assertAlmostEqual(late.saved_usd, 214_032 * U)


class PrimitiveTests(unittest.TestCase):
    def test_inline_usage_shrinks_uncached_tokens_when_main_is_smaller_than_the_baseline(self):
        u = inline_usage(usage(inp=100, w5=500, read=1_000, out=7), main_context=200, baseline=1_500,
                         first=False, flags={})
        # prompt must be C + P - S = 200 + 1,600 - 1,500 = 300: reads go to 0, writes shrink by 300
        self.assertEqual((u["input_tokens"], u["cache_creation"]["ephemeral_5m_input_tokens"],
                          u["cache_read_input_tokens"], u["output_tokens"]), (100, 200, 0, 7))

    def test_rates_and_rounding(self):
        self.assertAlmostEqual(rate(OPUS, "read"), 0.20 * U)
        self.assertAlmostEqual(rate(OPUS, "output", speed="fast", geo="us"), 20 * 2 * 1.1 * U)
        self.assertAlmostEqual(rate("claude-haiku-5-5", "read", prompt=200_000), 0.05 * U)
        self.assertIsNone(rate("gpt-5", "read"))
        with self.assertRaises(ValueError):
            rate(OPUS, "bogus")
        self.assertEqual([chars_to_tokens(n) for n in (0, 1, 4, 5, -3)], [0, 1, 2, 2, 0])

    def test_tail_parses_hand_backs_but_not_unrelated_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.jsonl"
            write_jsonl(path, [call(0, "m1", read=10, blocks=[spawn("sp1")]),
                               tool_result(1, "other-tool", "zz", 50),
                               tool_result(2, "sp1", "a1", 40)])
            acc = TranscriptTail(path, "main").refresh()
            self.assertEqual([(h.kind, h.agent_id, h.chars) for h in acc.handbacks], [("result", "a1", 40)])
            self.assertEqual(acc.spawns["sp1"].brief_chars, 400)

    def test_failed_spawn_error_string_is_not_a_hand_back(self):
        acc = Accumulator()
        acc.feed(call(0, "m1", blocks=[spawn("sp1")], read=10), "main")
        acc.feed({"type": "user", "timestamp": iso(1), "toolUseResult": "Error: denied",
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "sp1", "content": "Error"}]}},
                 "main")
        self.assertEqual(acc.handbacks, [])


if __name__ == "__main__":
    unittest.main()
