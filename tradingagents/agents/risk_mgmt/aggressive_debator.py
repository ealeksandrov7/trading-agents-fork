from tradingagents.agents.utils.prompt_utils import (
    build_risk_debate_summary,
    build_compact_market_context,
    compact_history,
    compact_text,
)
from tradingagents.dataflows.config import get_config


def create_aggressive_debator(llm):
    def aggressive_node(state) -> dict:
        config = get_config()
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        summary = risk_debate_state.get("summary", "")
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]

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
            latest_conservative = compact_text(
                current_conservative_response,
                max_chars=400,
                max_lines=5,
            )
            latest_neutral = compact_text(
                current_neutral_response,
                max_chars=400,
                max_lines=5,
            )
            decision_context = compact_text(trader_decision, max_chars=1000, max_lines=14)
        else:
            market_context = (
                f"Market Research Report:\n{market_research_report}\n\n"
                f"Social Media Sentiment Report:\n{sentiment_report}\n\n"
                f"Latest World Affairs Report:\n{news_report}\n\n"
                f"Company Fundamentals Report:\n{fundamentals_report}"
            )
            debate_history = history
            latest_conservative = current_conservative_response
            latest_neutral = current_neutral_response
            decision_context = trader_decision

        prompt = f"""As the Aggressive Risk Analyst, your role is to actively champion high-reward, high-risk opportunities, emphasizing bold strategies and competitive advantages. When evaluating the trader's decision or plan, focus intently on the potential upside, growth potential, and innovative benefits—even when these come with elevated risk. Use the provided market data and sentiment analysis to strengthen your arguments and challenge the opposing views. Specifically, respond directly to each point made by the conservative and neutral analysts, countering with data-driven rebuttals and persuasive reasoning. Highlight where their caution might miss critical opportunities or where their assumptions may be overly conservative. Here is the trader's decision:

{decision_context}

Your task is to create a compelling case for the trader's decision by questioning and critiquing the conservative and neutral stances to demonstrate why your high-reward perspective offers the best path forward. Incorporate insights from the following sources into your arguments:

{market_context}
Here is the current conversation history:
{debate_history}
Here are the last arguments from the conservative analyst:
{latest_conservative}
Here are the last arguments from the neutral analyst:
{latest_neutral}
If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by addressing any specific concerns raised, refuting the weaknesses in their logic, and asserting the benefits of risk-taking to outpace market norms. Maintain a focus on debating and persuading, not just presenting data. Challenge each counterpoint to underscore why a high-risk approach is optimal. Keep the output under 180 words and prioritize only the highest-signal rebuttals."""

        response = llm.invoke(prompt)

        argument = f"Aggressive Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "summary": build_risk_debate_summary(
                aggressive_history=aggressive_history + "\n" + argument,
                conservative_history=risk_debate_state.get("conservative_history", ""),
                neutral_history=risk_debate_state.get("neutral_history", ""),
                latest_speaker="Aggressive",
            ),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node
