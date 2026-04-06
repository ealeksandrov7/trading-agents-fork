from tradingagents.agents.utils.prompt_utils import (
    build_risk_debate_summary,
    build_compact_market_context,
    compact_history,
    compact_text,
)
from tradingagents.dataflows.config import get_config


def create_conservative_debator(llm):
    def conservative_node(state) -> dict:
        config = get_config()
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        summary = risk_debate_state.get("summary", "")
        conservative_history = risk_debate_state.get("conservative_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
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
            latest_aggressive = compact_text(
                current_aggressive_response,
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
            latest_aggressive = current_aggressive_response
            latest_neutral = current_neutral_response
            decision_context = trader_decision

        prompt = f"""As the Conservative Risk Analyst, your primary objective is to protect assets, minimize volatility, and ensure steady, reliable growth. You prioritize stability, security, and risk mitigation, carefully assessing potential losses, economic downturns, and market volatility. When evaluating the trader's decision or plan, critically examine high-risk elements, pointing out where the decision may expose the firm to undue risk and where more cautious alternatives could secure long-term gains. Here is the trader's decision:

{decision_context}

Your task is to actively counter the arguments of the Aggressive and Neutral Analysts, highlighting where their views may overlook potential threats or fail to prioritize sustainability. Respond directly to their points, drawing from the following data sources to build a convincing case for a low-risk approach adjustment to the trader's decision:

{market_context}
Here is the current conversation history:
{debate_history}
Here is the last response from the aggressive analyst:
{latest_aggressive}
Here is the last response from the neutral analyst:
{latest_neutral}
If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage by questioning their optimism and emphasizing the potential downsides they may have overlooked. Address each of their counterpoints to showcase why a conservative stance is ultimately the safest path for the firm's assets. Focus on debating and critiquing their arguments to demonstrate the strength of a low-risk strategy over their approaches. Keep the output under 180 words and prioritize only the highest-signal objections."""

        response = llm.invoke(prompt)

        argument = f"Conservative Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "summary": build_risk_debate_summary(
                aggressive_history=risk_debate_state.get("aggressive_history", ""),
                conservative_history=conservative_history + "\n" + argument,
                neutral_history=risk_debate_state.get("neutral_history", ""),
                latest_speaker="Conservative",
            ),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node
