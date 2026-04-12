# Hyperliquid Bot Architecture

This document is the current source of truth for the autonomous Hyperliquid bot in this fork. Use it in future Codex sessions to quickly recover the bot's intent, flow, and key integration points.

## Current Objective

The bot is currently optimized for a narrow, measurable first live strategy:

- Market: `BTC-USD`
- Timeframe: `1h`
- Strategy families: `trend_pullback`, `range_fade`
- Execution venue: Hyperliquid
- Design goal: deterministic regime routing first, LLM refinement second, deterministic execution validation last

The system is still intentionally explicit. It supports a small routed strategy set rather than broad discretionary multi-strategy trading.

## Core Live Flow

The live bot loop is implemented in [tradingagents/bot/runner.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/runner.py).

For each eligible analysis bar, the flow is:

1. Sync exchange state from Hyperliquid.
2. Run deterministic regime classification.
3. Route the active regime to its allowed strategy family.
4. Run deterministic setup candidate detection for the routed strategy.
5. If any deterministic gate fails, return synthetic `FLAT/NO_ACTION` without invoking the graph.
6. If the routed strategy produces a valid candidate, invoke the LLM graph.
7. Parse the structured trade decision.
8. Apply deterministic post-parse quality filters.
9. If the decision still passes, convert it into an order intent and execute.

The intended shape is:

`market data -> regime gate -> strategy router -> candidate gate -> LLM graph -> quality filter -> execution`

## Deterministic Gates

### 1. Regime Gate

Implemented in [tradingagents/bot/regime.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/regime.py).

Current v1 supported mode:

- only `1h`

Current labels:

- `trend_up`
- `trend_down`
- `range`
- `high_volatility_event`
- `low_quality`

Current routing behavior:

- `trend_up` -> `trend_pullback`
- `trend_down` -> `trend_pullback`
- `range` -> `range_fade`
- `high_volatility_event` / `low_quality` -> hard skip

The regime gate derives EMA structure, ATR-based volatility, slope, spread, and pullback-zone metadata.

### 2. Candidate Gate

Implemented in [tradingagents/bot/candidate.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/candidate.py).

Purpose:

- decide whether the current bar is worth an LLM call
- detect a plausible candidate for the routed strategy only
- not generate the final trade

Current supported candidate detectors:

- `trend_pullback`
  - regime must already route to trend continuation
  - recent bars must retrace into the allowed pullback zone
  - reclaim/continuation confirmation must exist
  - an invalidation level must be derivable
  - estimated reward-to-risk must exceed configured minimum
- `range_fade`
  - regime must already route to range mean reversion
  - recent structure must remain inside a bounded range
  - price must be near a range edge, not mid-range
  - edge rejection/reversal confirmation must exist
  - stop and target must fit a favorable reward-to-risk profile

If `candidate_setup_present=False`, the graph is skipped entirely.

## LLM Graph Role

The graph remains responsible for refining and structuring an already prequalified opportunity.

Important files:

- [tradingagents/graph/trading_graph.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/graph/trading_graph.py)
- [tradingagents/graph/setup.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/graph/setup.py)
- [tradingagents/agents/analysts/market_analyst.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/agents/analysts/market_analyst.py)
- [tradingagents/agents/trader/trader.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/agents/trader/trader.py)
- [tradingagents/agents/managers/portfolio_manager.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/agents/managers/portfolio_manager.py)

The graph is now prompt-constrained by:

- regime summary
- regime payload
- allowed setup families
- candidate summary
- candidate payload
- routed setup family

The graph should not invent alternate setups when deterministic gates say no trade.

## Post-Parse Quality Filter

Implemented in [tradingagents/bot/runner.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/runner.py).

After the portfolio manager emits a structured decision, the bot validates:

- symbol matches active market
- direction matches regime bias
- strategy is allowed for the active regime
- entry is inside the routed candidate zone
- entry orientation behaves like the routed strategy, not an invalid chase
- reward-to-risk exceeds configured minimum
- entry distance from mark price is within limit

If the decision fails these checks, it is flattened to `FLAT/NO_ACTION` and logged as `decision_rejected`.

## Replay Flow

Replay is also handled by [tradingagents/bot/runner.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/runner.py).

Use:

- `BotRunner.run_replay(start, end, data_source=...)`

CLI command:

- `python cli/main.py bot-replay ...`

Replay supports two historical data sources:

- `vendor`: current vendor-backed OHLCV path
- `hyperliquid`: Hyperliquid-native candle snapshots

Hyperliquid historical candle retrieval is implemented in [tradingagents/execution/hyperliquid.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/execution/hyperliquid.py).

Important replay detail:

- replay regime classification uses the replay bars directly
- it does not re-fetch a separate market stream for classification

Replay metrics currently include:

- signal count
- simulated trades vs no-trade
- regime grouping
- strategy grouping
- top skip reasons by regime
- top candidate and filter rejection reasons by strategy
- `R_1`, `R_2`, `R_4`, `R_8`
- `MAE` / `MFE`

Replay modes:

- `regime-only`: classify regimes only
- `candidate-only`: evaluate deterministic candidates without invoking the LLM graph
- `full-llm`: run the same routed strategy flow as live, including the graph

## State and Diagnostics

Bot runtime state is stored via:

- [tradingagents/bot/models.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/models.py)
- [tradingagents/bot/state.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/state.py)

Relevant persisted fields:

- `regime_snapshot`
- `candidate_snapshot`
- `last_decision_diagnostics`
- exchange/order/position state

Current event log types now include deterministic gate outputs such as:

- `regime`
- `candidate`
- `decision_rejected`

## SQLite Journal

The bot now maintains a dedicated SQLite journal for live decision cycles.

Default path:

- `./results/bot_journal.sqlite`

Configured by:

- `bot_journal_path` in [tradingagents/default_config.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/default_config.py)

Implementation:

- [tradingagents/bot/journal.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/journal.py)

The journal is separate from `bot_state.json`. It is the long-term append-only store for the bot's own operational history.

Each row currently stores:

- symbol and timeframe
- decision timestamp
- analysis timestamp
- regime label and regime payload
- allowed strategy families and selected strategy family
- candidate direction and candidate payload
- raw action payload
- final action payload
- quality-filter reasons
- tool errors
- plan action
- final outcome and outcome message
- exchange snapshot
- order intent and order preview when applicable

The journal is currently written for live bot cycles. Replay still returns in-memory evaluation results and does not yet persist rows into the SQLite journal.

## Config Defaults That Matter

Defined in [tradingagents/default_config.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/default_config.py).

Key bot-specific defaults:

- `bot_enabled_strategy_families = ["trend_pullback", "range_fade"]`
- `bot_regime_strategy_map = {"trend_up": ["trend_pullback"], "trend_down": ["trend_pullback"], "range": ["range_fade"]}`
- `bot_min_reward_risk = 1.8`
- `bot_journal_path = "./results/bot_journal.sqlite"`
- regime thresholds for spread/slope/volatility
- `bot_pullback_atr_tolerance`
- range-fade thresholds for edge proximity, width sanity, stop buffer, and target buffer
- per-timeframe max entry distance

If changing strategy behavior, check config defaults before changing prompt text.

## Known Current Constraints

- v1 logic is intentionally optimized for `BTC 1h`
- replay can still hit LLM-provider auth issues if bars pass deterministic gates and the configured provider credentials are invalid
- the graph still contains debate/research stages, but deterministic gates now prevent most unnecessary LLM calls

## Recommended Future Extensions

If continuing the bot work, the most natural next steps are:

1. Add `breakout_retest` behind the same deterministic regime router.
2. Persist replay outputs and decision diagnostics into dedicated strategy-evaluation artifacts.
3. Reduce graph breadth for live bot mode if the remaining debate stages are still too expensive.
4. Add strategy-specific calibration from the SQLite journal and replay outputs.

## Strategy Glossary

### trend_pullback

This is the current live strategy family.

Definition:

- trade in the direction of an already confirmed trend
- wait for price to retrace into a favorable zone
- require evidence of continuation/reclaim before treating the setup as valid

In practical terms:

- `LONG`: buy pullbacks in `trend_up`
- `SHORT`: short rallies in `trend_down`

Why this strategy was chosen first:

- it is easier to define deterministically than broader discretionary strategies
- it naturally supports selective no-trade behavior
- it usually provides cleaner invalidation and reward-to-risk structure than breakout chasing
- it is a strong baseline strategy for measuring whether the bot can behave with discipline

Known weaknesses:

- undertrades when markets trend without retracing
- performs poorly in chop if regime classification is weak
- can become too sparse if pullback-zone or reclaim rules are too strict

### breakout_retest

Not implemented yet.

Intended meaning:

- wait for a valid breakout through a meaningful level
- do not chase immediately
- look for a retest/hold of the breakout level before entry

Likely future use:

- complementary strategy to `trend_pullback` during momentum expansion conditions

### range_fade

Intended meaning:

- trade reversions at the edges of a clearly defined range
- buy near support and sell near resistance
- should only be used when the market is classified as range-bound, not trending

Current bot usage:

- `range` regime routes to `range_fade`
- candidate detection requires an edge touch plus rejection confirmation
- entries should stay near the range boundary and invalidation sits just outside the range

## Quick Commands

Live bot:

```bash
python cli/main.py bot --symbol BTC-USD --timeframe 1h --testnet
```

Replay with vendor data:

```bash
python cli/main.py bot-replay \
  --symbol BTC-USD \
  --timeframe 1h \
  --start "2026-03-01 00:00" \
  --end "2026-04-11 23:00" \
  --data-source vendor \
  --mode candidate-only
```

Replay with Hyperliquid candles:

```bash
python cli/main.py bot-replay \
  --symbol BTC-USD \
  --timeframe 1h \
  --start "2026-03-01 00:00" \
  --end "2026-04-11 23:00" \
  --data-source hyperliquid \
  --mode full-llm \
  --testnet
```

## Session Rehydration Hint

If a future Codex session needs context quickly, point it to:

- this document: [docs/bot_architecture.md](/Users/evlogialeksandrov/repos/TradingAgents/docs/bot_architecture.md)
- the live orchestration path: [tradingagents/bot/runner.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/runner.py)

That should be enough to recover the current architecture without re-deriving it from scratch.
