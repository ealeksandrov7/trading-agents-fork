from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor

TOOL_ERROR_PREFIX = "[TOOL_ERROR]"


def _format_tool_error(tool_name: str, symbol: str, detail: str) -> str:
    return f"{TOOL_ERROR_PREFIX} tool={tool_name} symbol={symbol} detail={detail}"


def _looks_like_data_failure(result: str) -> bool:
    lowered = result.lower()
    failure_markers = (
        "no data found for symbol",
        "possibly delisted",
        "failed download",
        "quote not found",
        "error getting",
        "runtimeerror:",
        "traceback",
    )
    return any(marker in lowered for marker in failure_markers)


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.

    Guidance:
        Use timeframe-appropriate windows rather than long raw CSV spans.
        Recommended raw OHLCV windows:
        - 1h: 5 to 7 calendar days
        - 4h: 10 to 14 calendar days
        - 1d: 30 to 120 calendar days
        Broader trend context for intraday runs should come from indicators and the higher-timeframe anchor, not months of hourly CSV.
    """
    try:
        result = route_to_vendor("get_stock_data", symbol, start_date, end_date)
    except Exception as exc:
        return _format_tool_error("get_stock_data", symbol, str(exc))
    if isinstance(result, str) and _looks_like_data_failure(result):
        return _format_tool_error("get_stock_data", symbol, result)
    return result
