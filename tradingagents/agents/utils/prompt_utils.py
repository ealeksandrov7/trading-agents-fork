from __future__ import annotations

from typing import Iterable, Optional


def compact_text(text: Optional[str], *, max_chars: int, max_lines: Optional[int] = None) -> str:
    if not text:
        return ""

    normalized = str(text).strip()
    if not normalized:
        return ""

    lines = [line.rstrip() for line in normalized.splitlines()]
    if max_lines is not None and len(lines) > max_lines:
        head_count = max(1, max_lines - 1)
        lines = lines[:head_count] + ["...[truncated]"]
    shortened = "\n".join(lines)

    if len(shortened) <= max_chars:
        return shortened

    keep = max(64, max_chars - len("\n...[truncated]"))
    return shortened[:keep].rstrip() + "\n...[truncated]"


def compact_section(
    title: str,
    text: Optional[str],
    *,
    max_chars: int,
    max_lines: Optional[int] = None,
) -> str:
    body = compact_text(text, max_chars=max_chars, max_lines=max_lines)
    if not body:
        return ""
    return f"{title}:\n{body}"


def compact_sections(sections: Iterable[str]) -> str:
    return "\n\n".join(section for section in sections if section)


def build_compact_market_context(
    *,
    market_report: str,
    sentiment_report: str,
    news_report: str,
    fundamentals_report: str,
    report_max_chars: int = 1200,
) -> str:
    return compact_sections(
        [
            compact_section(
                "Market Research",
                market_report,
                max_chars=report_max_chars,
                max_lines=18,
            ),
            compact_section(
                "Sentiment",
                sentiment_report,
                max_chars=report_max_chars,
                max_lines=18,
            ),
            compact_section(
                "News",
                news_report,
                max_chars=report_max_chars,
                max_lines=18,
            ),
            compact_section(
                "Fundamentals",
                fundamentals_report,
                max_chars=report_max_chars,
                max_lines=18,
            ),
        ]
    )


def compact_history(
    history: Optional[str],
    *,
    max_chars: int = 1200,
    max_turns: int = 4,
) -> str:
    if not history:
        return ""
    turns = [turn.strip() for turn in str(history).split("\n") if turn.strip()]
    if len(turns) > max_turns:
        turns = turns[-max_turns:]
    return compact_text("\n".join(turns), max_chars=max_chars, max_lines=max_turns + 1)


def compact_memories(memories: Optional[str], *, max_chars: int = 500) -> str:
    compacted = compact_text(memories, max_chars=max_chars, max_lines=6)
    return compacted or "No closely relevant prior lessons."


def build_investment_debate_summary(
    *,
    bull_history: Optional[str],
    bear_history: Optional[str],
    latest_response: Optional[str],
) -> str:
    sections = [
        compact_section("Bull Case", bull_history, max_chars=450, max_lines=4),
        compact_section("Bear Case", bear_history, max_chars=450, max_lines=4),
        compact_section("Latest Exchange", latest_response, max_chars=350, max_lines=3),
    ]
    return compact_sections(sections)


def build_risk_debate_summary(
    *,
    aggressive_history: Optional[str],
    conservative_history: Optional[str],
    neutral_history: Optional[str],
    latest_speaker: Optional[str],
) -> str:
    sections = [
        compact_section("Aggressive View", aggressive_history, max_chars=350, max_lines=3),
        compact_section("Conservative View", conservative_history, max_chars=350, max_lines=3),
        compact_section("Neutral View", neutral_history, max_chars=350, max_lines=3),
    ]
    if latest_speaker:
        sections.append(f"Latest Speaker:\n{latest_speaker}")
    return compact_sections(sections)
