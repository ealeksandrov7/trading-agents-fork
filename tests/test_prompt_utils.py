import unittest

from tradingagents.agents.utils.prompt_utils import (
    build_investment_debate_summary,
    build_risk_debate_summary,
    build_compact_market_context,
    compact_history,
    compact_memories,
    compact_text,
)


class PromptUtilsTests(unittest.TestCase):
    def test_compact_text_truncates_and_marks_output(self):
        text = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        compacted = compact_text(text, max_chars=24, max_lines=3)
        self.assertIn("[truncated]", compacted)

    def test_compact_history_keeps_latest_turns(self):
        history = "\n".join(f"Turn {idx}" for idx in range(1, 7))
        compacted = compact_history(history, max_chars=200, max_turns=3)
        self.assertNotIn("Turn 1", compacted)
        self.assertIn("Turn 6", compacted)

    def test_compact_market_context_preserves_all_sections(self):
        context = build_compact_market_context(
            market_report="market",
            sentiment_report="sentiment",
            news_report="news",
            fundamentals_report="fundamentals",
            report_max_chars=40,
        )
        self.assertIn("Market Research", context)
        self.assertIn("Sentiment", context)
        self.assertIn("News", context)
        self.assertIn("Fundamentals", context)

    def test_compact_memories_has_fallback_message(self):
        self.assertEqual(
            compact_memories("", max_chars=50),
            "No closely relevant prior lessons.",
        )

    def test_build_investment_debate_summary_includes_both_sides(self):
        summary = build_investment_debate_summary(
            bull_history="Bull Analyst: upside via breakout and strong momentum",
            bear_history="Bear Analyst: resistance overhead and weak breadth",
            latest_response="Bull Analyst: breakout risk/reward still dominates",
        )
        self.assertIn("Bull Case", summary)
        self.assertIn("Bear Case", summary)
        self.assertIn("Latest Exchange", summary)

    def test_build_risk_debate_summary_tracks_latest_speaker(self):
        summary = build_risk_debate_summary(
            aggressive_history="Aggressive Analyst: press the edge",
            conservative_history="Conservative Analyst: protect capital",
            neutral_history="Neutral Analyst: wait for cleaner confirmation",
            latest_speaker="Neutral",
        )
        self.assertIn("Aggressive View", summary)
        self.assertIn("Conservative View", summary)
        self.assertIn("Neutral View", summary)
        self.assertIn("Latest Speaker", summary)


if __name__ == "__main__":
    unittest.main()
