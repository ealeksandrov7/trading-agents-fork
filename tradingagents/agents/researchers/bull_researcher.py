from tradingagents.agents.utils.prompt_utils import (
    build_investment_debate_summary,
    build_compact_market_context,
    compact_history,
    compact_memories,
    compact_text,
)
from tradingagents.dataflows.config import get_config


def create_bull_researcher(llm, memory):
    def bull_node(state) -> dict:
        config = get_config()
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        summary = investment_debate_state.get("summary", "")
        bull_history = investment_debate_state.get("bull_history", "")

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
            latest_bear_argument = compact_text(
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
            latest_bear_argument = current_response
            lessons = past_memory_str

        prompt = f"""You are a Bull Analyst advocating for investing in the stock. Your task is to build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators. Leverage the provided research and data to address concerns and counter bearish arguments effectively.

Key points to focus on:
- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.
- Bear Counterpoints: Critically analyze the bear argument with specific data and sound reasoning, addressing concerns thoroughly and showing why the bull perspective holds stronger merit.
- Engagement: Present your argument in a conversational style, engaging directly with the bear analyst's points and debating effectively rather than just listing data.
- Keep the response tight: 6 bullets or fewer, no long recap, and under 220 words.

Resources available:
{market_context}
Conversation history of the debate:
{debate_history}
Last bear argument:
{latest_bear_argument}
Reflections from similar situations and lessons learned:
{lessons}
Use this information to deliver a compelling bull argument, refute the bear's concerns, and engage in a dynamic debate that demonstrates the strengths of the bull position. You must also address reflections and learn from lessons and mistakes you made in the past.
"""

        response = llm.invoke(prompt)

        argument = f"Bull Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "summary": build_investment_debate_summary(
                bull_history=bull_history + "\n" + argument,
                bear_history=investment_debate_state.get("bear_history", ""),
                latest_response=argument,
            ),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
