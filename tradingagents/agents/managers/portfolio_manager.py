from tradingagents.agents.utils.agent_utils import build_instrument_context, get_language_instruction
from tradingagents.execution import DecisionParseError, DecisionParser
from tradingagents.dataflows.config import get_config


def create_portfolio_manager(llm, memory):
    def portfolio_manager_node(state) -> dict:

        instrument_context = build_instrument_context(state["company_of_interest"])
        analysis_timeframe = get_config().get("analysis_timeframe", "1d")

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        market_research_report = state["market_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        sentiment_report = state["sentiment_report"]
        trader_plan = state["investment_plan"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        entry_rules = """- Use `MARKET` only if the trade should be entered immediately at current price.
- Use `LIMIT` if the trade should wait for one specific price level.
- Use `LIMIT_ZONE` if the trade should wait for a bounce/retracement/retest zone.
- For `MARKET`, set `entry_price`, `entry_zone_low`, and `entry_zone_high` to null.
- For `LIMIT`, set `entry_price` and leave zone fields null.
- For `LIMIT_ZONE`, set `entry_zone_low` and `entry_zone_high` and leave `entry_price` null."""

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

Your highest priority is format compliance. You must output the `STRUCTURED_DECISION` JSON block first. If you are uncertain, still emit the JSON with your best estimate. Do not skip the JSON block.

{instrument_context}

---

**Action Scale** (use exactly one):
- **LONG**: Enter or add long exposure
- **SHORT**: Enter or add short exposure
- **FLAT**: Stay out or close the existing position

**Context:**
- Trader's proposed plan: **{trader_plan}**
- Lessons from past decisions: **{past_memory_str}**
- Trade date: **{state["trade_date"]}**
        - Required time horizon for the structured output: **{analysis_timeframe}**

**Required Output Structure:**
1. `STRUCTURED_DECISION`
```json
{{
  "symbol": "{state["company_of_interest"]}",
  "timestamp": "{state["trade_date"]}",
  "action": "LONG | SHORT | FLAT",
  "entry_mode": "MARKET | LIMIT | LIMIT_ZONE",
  "entry_price": null,
  "entry_zone_low": null,
  "entry_zone_high": null,
  "confidence": 0.0,
  "thesis_summary": "One sentence.",
  "time_horizon": "{analysis_timeframe}",
  "stop_loss": 0.0,
  "take_profit": 0.0,
  "invalidation": "What would prove the trade wrong.",
  "size_hint": "small | medium | large"
}}
```
2. `EXECUTIVE_SUMMARY`
   A concise action plan covering direction, risk levels, and time horizon.
3. `INVESTMENT_THESIS`
   Detailed reasoning anchored in the analysts' debate and past reflections.

Rules for the JSON:
- Output valid JSON only inside the fenced block.
- Output the JSON block before any prose.
- Confidence must be between 0 and 1.
- For `FLAT`, set both `stop_loss` and `take_profit` to null.
- For `LONG` and `SHORT`, both `stop_loss` and `take_profit` are mandatory numeric prices.
- {entry_rules}
- If the correct stance is defensive or capital-preservation only, use `FLAT` instead of prose like "stay in cash" or "maintain liquidity."

---

**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts. If the horizon is 4h or 1h, prefer tactical setups that can trigger and resolve quickly.{get_language_instruction()}"""

        response = llm.invoke(prompt)
        action = {}
        action_error = ""
        try:
            action = DecisionParser.parse(
                response.content,
                fallback_symbol=state["company_of_interest"],
                fallback_timestamp=state["trade_date"],
                fallback_time_horizon=analysis_timeframe,
            ).model_dump()
        except DecisionParseError as exc:
            action_error = str(exc)

        new_risk_debate_state = {
            "judge_decision": response.content,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": response.content,
            "final_trade_action": action,
            "final_trade_action_error": action_error,
        }

    return portfolio_manager_node
