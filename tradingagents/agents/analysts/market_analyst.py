from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_indicators,
    get_language_instruction,
    get_stock_data,
)
from tradingagents.dataflows.config import get_config


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])
        analysis_timeframe = get_config().get("analysis_timeframe", "1d")
        regime_summary = state.get("regime_summary", "")
        setup_family = state.get("setup_family", "trend_pullback")
        allowed_setup_families = state.get("allowed_setup_families", []) or []
        regime_context = state.get("regime_context", {}) or {}
        higher_timeframe_summary = state.get("higher_timeframe_summary", "")
        higher_timeframe_context = state.get("higher_timeframe_context", {}) or {}
        candidate_summary = state.get("candidate_summary", "")
        candidate_context = state.get("candidate_context", {}) or {}
        intraday_instruction = ""
        if analysis_timeframe in {"4h", "1h"}:
            intraday_instruction = (
                "This is a short-horizon trading run. Prioritize recent structure, momentum, volatility, "
                "reclaim/rejection levels, and actionable entry zones over long-term macro commentary. "
                "Be specific about where a pullback entry or breakout trigger would make sense. "
                "Use shorter indicator lookbacks appropriate for the active timeframe, but keep the broader trend in view via the higher-timeframe anchor included in intraday indicator output. "
                "When calling get_stock_data, do not request months of raw hourly history. Use only a short raw OHLCV window: 1h runs should usually request 5-7 calendar days, and 4h runs should usually request 10-14 calendar days."
            )

        tools = [
            get_stock_data,
            get_indicators,
        ]

        trade_allowed = bool(regime_context.get("trade_allowed"))
        preferred_action = regime_context.get("preferred_action", "FLAT")
        system_message = (
            f"""You are a trading assistant tasked with analyzing financial markets for an autonomous Hyperliquid bot. Your role is to validate one approved setup family instead of brainstorming multiple trade ideas.

Approved setup family: **{setup_family}**
Allowed strategy families for this regime: **{', '.join(allowed_setup_families) or 'none'}**
Deterministic regime gate:
- {regime_summary or 'No regime summary provided.'}
- The regime payload says trade_allowed={str(trade_allowed).lower()} and preferred_action={preferred_action}.
- Higher timeframe trend filter:
- {higher_timeframe_summary or 'No higher-timeframe summary provided.'}
- The higher-timeframe payload says label={higher_timeframe_context.get("label", "neutral")} and preferred_action={higher_timeframe_context.get("preferred_action", "FLAT")}.
- Deterministic setup candidate:
- {candidate_summary or 'No candidate summary provided.'}
- The candidate payload says candidate_setup_present={str(bool(candidate_context.get("candidate_setup_present"))).lower()} and direction={candidate_context.get("direction", preferred_action)}.
- If trade_allowed is false, do not manufacture a trade. State clearly that this bar is a no-trade regime.
- If candidate_setup_present is false, do not manufacture a trade. Explain why the routed {setup_family} candidate failed and preserve a no-trade stance.
- If trade_allowed is true, evaluate only whether the approved setup is present in the routed direction. For trend_pullback that is the regime preferred direction; for range_fade that is the candidate direction at the active range edge. Do not propose alternate setups.
- Your report must explicitly answer:
  1. Is a valid {setup_family} setup present? yes/no
  2. What is the directional bias?
  3. What exact entry level or zone is justified?
  4. What invalidation proves the idea wrong?
  5. If no trade, why should the bot stand aside?

Indicator toolbox reference. Select the **most relevant indicators** for validating the active setup and market condition from the following list. Choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. The configured analysis timeframe for this run is **{analysis_timeframe}** and you should interpret the retrieved OHLCV and indicators on that timeframe. {intraday_instruction} When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names. Do not request unnecessarily long raw OHLCV ranges for intraday runs. Write a detailed report focused on whether the approved setup is valid right now. Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )
        if setup_family == "range_fade":
            system_message += (
                "\nFor range_fade specifically: validate bounded range behavior, identify the active range low/high, "
                "confirm whether price is rejecting an edge rather than drifting in the middle, place invalidation just outside the range, "
                "and do not convert a developing breakout into a fade trade."
            )
        else:
            system_message += (
                "\nFor trend_pullback specifically: validate continuation with a pullback into the allowed zone, "
                "prefer reclaim/continuation evidence, do not chase extension bars far from the pullback zone, "
                "and require alignment with the higher-timeframe trend filter."
            )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)
        prompt = prompt.partial(analysis_timeframe=analysis_timeframe)
        prompt = prompt.partial(intraday_instruction=intraday_instruction)
        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
