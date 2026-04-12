from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.stockstats_utils import (
    get_analysis_timeframe,
    get_indicator_analysis_window_days,
)
from tradingagents.agents.utils.core_stock_tools import TOOL_ERROR_PREFIX

@tool
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve a single technical indicator for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): A single technical indicator name, e.g. 'rsi', 'macd'. Call this tool once per indicator.
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back. If omitted, defaults are timeframe-aware:
            1h -> 5 days, 4h -> 10 days, 1d -> 30 days
    Returns:
        str: A formatted dataframe containing the technical indicators for the specified ticker symbol and indicator.
    """
    timeframe = get_analysis_timeframe()
    effective_lookback = look_back_days
    if look_back_days == 30:
        effective_lookback = get_indicator_analysis_window_days(timeframe)

    # LLMs sometimes pass multiple indicators as a comma-separated string;
    # split and process each individually.
    indicators = [i.strip() for i in indicator.split(",") if i.strip()]
    results = []
    for ind in indicators:
        try:
            result = route_to_vendor("get_indicators", symbol, ind, curr_date, effective_lookback)
            if isinstance(result, str):
                lowered = result.lower()
                if any(
                    marker in lowered
                    for marker in (
                        "error getting stockstats",
                        "error getting bulk stockstats",
                        "possibly delisted",
                        "failed download",
                        "quote not found",
                        "no data found for symbol",
                    )
                ):
                    result = (
                        f"{TOOL_ERROR_PREFIX} tool=get_indicators symbol={symbol} "
                        f"indicator={ind} detail={result}"
                    )
            results.append(result)
        except ValueError as e:
            results.append(str(e))
        except Exception as e:
            results.append(
                f"{TOOL_ERROR_PREFIX} tool=get_indicators symbol={symbol} indicator={ind} detail={e}"
            )
    return "\n\n".join(results)
