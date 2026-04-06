from tradingagents.agents.utils.prompt_utils import (
    build_investment_debate_summary,
    build_compact_market_context,
    compact_history,
    compact_memories,
    compact_text,
)
from tradingagents.dataflows.config import get_config


def create_bear_researcher(llm, memory):
    def bear_node(state) -> dict:
        config = get_config()
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        summary = investment_debate_state.get("summary", "")
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for rec in past_memories:
            past_memory_str += rec["recommendation"] + "\n\n"

        if config.get("compact_reasoning", True):
            market_context = build_compact_market_context(
                market_report=market_research_report,
                sentiment_report=sentiment_report,
                news_report=news_report,
                fundamentals_report=fundamentals_report,
                report_max_chars=config.get("compact_report_max_chars", 1200),
            )
            debate_history = compact_history(
                summary or history,
                max_chars=config.get("compact_history_max_chars", 1200),
            )
            latest_bull_argument = compact_text(
                current_response,
                max_chars=450,
                max_lines=6,
            )
            lessons = compact_memories(
                past_memory_str,
                max_chars=config.get("compact_memory_max_chars", 500),
            )
        else:
            market_context = (
                f"Market research report:\n{market_research_report}\n\n"
                f"Social media sentiment report:\n{sentiment_report}\n\n"
                f"Latest world affairs news:\n{news_report}\n\n"
                f"Company fundamentals report:\n{fundamentals_report}"
            )
            debate_history = history
            latest_bull_argument = current_response
            lessons = past_memory_str

        prompt = f"""You are a Bear Analyst making the case against investing in the stock. Your goal is to present a well-reasoned argument emphasizing risks, challenges, and negative indicators. Leverage the provided research and data to highlight potential downsides and counter bullish arguments effectively.

Key points to focus on:

- Risks and Challenges: Highlight factors like market saturation, financial instability, or macroeconomic threats that could hinder the stock's performance.
- Competitive Weaknesses: Emphasize vulnerabilities such as weaker market positioning, declining innovation, or threats from competitors.
- Negative Indicators: Use evidence from financial data, market trends, or recent adverse news to support your position.
- Bull Counterpoints: Critically analyze the bull argument with specific data and sound reasoning, exposing weaknesses or over-optimistic assumptions.
- Engagement: Present your argument in a conversational style, directly engaging with the bull analyst's points and debating effectively rather than simply listing facts.
- Keep the response tight: 6 bullets or fewer, no long recap, and under 220 words.

Resources available:

{market_context}
Conversation history of the debate:
{debate_history}
Last bull argument:
{latest_bull_argument}
Reflections from similar situations and lessons learned:
{lessons}
Use this information to deliver a compelling bear argument, refute the bull's claims, and engage in a dynamic debate that demonstrates the risks and weaknesses of investing in the stock. You must also address reflections and learn from lessons and mistakes you made in the past.
"""

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "summary": build_investment_debate_summary(
                bull_history=investment_debate_state.get("bull_history", ""),
                bear_history=bear_history + "\n" + argument,
                latest_response=argument,
            ),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
