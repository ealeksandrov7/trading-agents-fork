import time
import logging

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from stockstats import wrap
from typing import Annotated
import os
from .config import get_config

logger = logging.getLogger(__name__)


def get_analysis_timeframe() -> str:
    return get_config().get("analysis_timeframe", "1d").lower()


def get_timeframe_interval() -> str:
    timeframe = get_analysis_timeframe()
    return "1h" if timeframe in ("1h", "4h") else "1d"


def get_timeframe_label() -> str:
    timeframe = get_analysis_timeframe()
    if timeframe in ("1h", "4h"):
        return timeframe
    return "1d"


def get_cutoff_timestamp(curr_date: str) -> pd.Timestamp:
    curr_ts = pd.to_datetime(curr_date)
    if len(str(curr_date).strip()) > 10:
        return curr_ts
    timeframe = get_analysis_timeframe()
    if timeframe in ("1h", "4h"):
        return curr_ts + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    return curr_ts


def resample_ohlcv(data: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe == "1h":
        return data
    if timeframe != "4h":
        return data

    if data.empty:
        return data

    frame = data.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"]).sort_values("Date")
    frame = frame.set_index("Date")

    agg_map = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    if "Adj Close" in frame.columns:
        agg_map["Adj Close"] = "last"

    resampled = (
        frame.resample("4h", label="right", closed="right")
        .agg(agg_map)
        .dropna(subset=["Open", "High", "Low", "Close"])
        .reset_index()
    )
    return resampled


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    if "Date" not in data.columns:
        if "Datetime" in data.columns:
            data = data.rename(columns={"Datetime": "Date"})
        elif "index" in data.columns:
            data = data.rename(columns={"index": "Date"})

    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 15 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.
    """
    config = get_config()
    curr_date_dt = get_cutoff_timestamp(curr_date)
    timeframe = get_analysis_timeframe()
    interval = get_timeframe_interval()

    # Cache uses a fixed window (15y to today) so one file per symbol
    today_date = pd.Timestamp.today()
    if timeframe in ("1h", "4h"):
        start_date = today_date - pd.DateOffset(days=729)
    else:
        start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = today_date.strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{symbol}-YFin-data-{interval}-{start_str}-{end_str}.csv",
    )

    if os.path.exists(data_file):
        data = pd.read_csv(data_file, on_bad_lines="skip")
    else:
        data = yf_retry(lambda: yf.download(
            symbol,
            start=start_str,
            end=end_str,
            interval=interval,
            multi_level_index=False,
            progress=False,
            auto_adjust=True,
        ))
        data = data.reset_index()
        data.to_csv(data_file, index=False)

    data = _clean_dataframe(data)
    data = resample_ohlcv(data, timeframe)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        timeframe = get_analysis_timeframe()
        cutoff = get_cutoff_timestamp(curr_date)

        df[indicator]  # trigger stockstats to calculate the indicator
        if timeframe in ("1h", "4h"):
            matching_rows = df[df["Date"] <= cutoff].tail(1)
        else:
            curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")
            df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
            matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
