from tradingagents.agents.utils.prompt_utils import (
    build_risk_debate_summary,
    build_compact_market_context,
    compact_history,
    compact_text,
)
from tradingagents.dataflows.config import get_config


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        config = get_config()
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        summary = risk_debate_state.get("summary", "")
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")

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
            latest_aggressive = compact_text(
                current_aggressive_response,
                max_chars=400,
                max_lines=5,
            )
            latest_conservative = compact_text(
                current_conservative_response,
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
            latest_aggressive = current_aggressive_response
            latest_conservative = current_conservative_response
            decision_context = trader_decision

        prompt = f"""As the Neutral Risk Analyst, your role is to provide a balanced perspective, weighing both the potential benefits and risks of the trader's decision or plan. You prioritize a well-rounded approach, evaluating the upsides and downsides while factoring in broader market trends, potential economic shifts, and diversification strategies.Here is the trader's decision:

{decision_context}

Your task is to challenge both the Aggressive and Conservative Analysts, pointing out where each perspective may be overly optimistic or overly cautious. Use insights from the following data sources to support a moderate, sustainable strategy to adjust the trader's decision:

{market_context}
Here is the current conversation history:
{debate_history}
Here is the last response from the aggressive analyst:
{latest_aggressive}
Here is the last response from the conservative analyst:
{latest_conservative}
If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by analyzing both sides critically, addressing weaknesses in the aggressive and conservative arguments to advocate for a more balanced approach. Challenge each of their points to illustrate why a moderate risk strategy might offer the best of both worlds, providing growth potential while safeguarding against extreme volatility. Focus on debating rather than simply presenting data, aiming to show that a balanced view can lead to the most reliable outcomes. Keep the output under 180 words and prioritize only the highest-signal points."""

        response = llm.invoke(prompt)

        argument = f"Neutral Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "summary": build_risk_debate_summary(
                aggressive_history=risk_debate_state.get("aggressive_history", ""),
                conservative_history=risk_debate_state.get("conservative_history", ""),
                neutral_history=neutral_history + "\n" + argument,
                latest_speaker="Neutral",
            ),
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node
