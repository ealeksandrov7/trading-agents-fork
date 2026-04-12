import os

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", "./results"),
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4",
    "backend_url": "https://api.openai.com/v1",
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": "medium",    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    "analysis_timeframe": "1d",
    "compact_reasoning": True,
    "compact_report_max_chars": 1200,
    "compact_history_max_chars": 1200,
    "compact_memory_max_chars": 500,
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # Execution settings
    "execution_mode": os.getenv("TRADINGAGENTS_EXECUTION_MODE", "analysis"),
    "allowed_symbols": ["BTC", "ETH"],
    "decision_timeframe": "4h",
    "max_risk_per_trade_pct": 0.01,
    "max_leverage": 2,
    "min_notional_usd": 10.0,
    "single_position_mode": True,
    "require_manual_live_confirm": True,
    "paper_ledger_path": os.getenv(
        "TRADINGAGENTS_PAPER_LEDGER_PATH", "./results/paper_ledger.json"
    ),
    "hyperliquid_base_url": os.getenv("HYPERLIQUID_BASE_URL"),
    "hyperliquid_wallet_address": os.getenv("HYPERLIQUID_WALLET_ADDRESS"),
    "hyperliquid_testnet": os.getenv("HYPERLIQUID_TESTNET", "false").lower() == "true",
    # Autonomous bot settings
    "bot_symbol": os.getenv("TRADINGAGENTS_BOT_SYMBOL", "BTC-USD"),
    "bot_analysis_interval_minutes": 240,
    "bot_reconcile_interval_seconds": 60,
    "bot_setup_expiry_bars_default": 3,
    "bot_default_intraday_analysts": ["market"],
    "bot_default_swing_analysts": ["market", "social", "news", "fundamentals"],
    "bot_state_path": os.getenv("TRADINGAGENTS_BOT_STATE_PATH", "./results/bot_state.json"),
    "bot_journal_path": os.getenv("TRADINGAGENTS_BOT_JOURNAL_PATH", "./results/bot_journal.sqlite"),
    "bot_fail_closed": True,
    "bot_strategy_setup_family": "trend_pullback",
    "bot_enabled_strategy_families": ["trend_pullback", "range_fade"],
    "bot_regime_strategy_map": {
        "trend_up": ["trend_pullback"],
        "trend_down": ["trend_pullback"],
        "range": ["range_fade"],
        "high_volatility_event": [],
        "low_quality": [],
    },
    "bot_min_reward_risk": 1.8,
    "bot_regime_trend_spread_min_pct": 0.003,
    "bot_regime_slope_min_pct": 0.0015,
    "bot_regime_range_spread_max_pct": 0.0015,
    "bot_regime_range_slope_max_pct": 0.0007,
    "bot_regime_volatility_event_atr_pct": 0.035,
    "bot_regime_bar_shock_atr_multiple": 1.8,
    "bot_pullback_atr_tolerance": 0.75,
    "bot_range_fade_edge_atr_tolerance": 0.55,
    "bot_range_fade_min_width_atr": 1.5,
    "bot_range_fade_max_width_atr": 5.5,
    "bot_range_fade_stop_buffer_atr": 0.2,
    "bot_range_fade_target_buffer_atr": 0.35,
    "bot_deterministic_trend_pullback_target_r_multiple": 2.0,
    "bot_deterministic_trend_pullback_expiry_bars": 5,
    "bot_deterministic_range_fade_target_mode": "reference",
    "bot_deterministic_range_fade_target_r_multiple": 2.0,
    "bot_deterministic_range_fade_expiry_bars": 3,
    "bot_replay_initial_equity": 1000.0,
    "max_entry_distance_pct_by_timeframe": {
        "1h": 0.05,
        "4h": 0.08,
        "1d": 0.15,
    },
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
}
