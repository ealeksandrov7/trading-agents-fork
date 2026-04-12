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

- `BotRunner.run_replay(start, end, data_source=..., mode=..., strategy_filter=...)`

CLI command:

- `python cli/main.py bot-replay ...`

Replay supports two historical data sources:

- `vendor`: current vendor-backed OHLCV path
- `hyperliquid`: Hyperliquid-native candle snapshots

Hyperliquid historical candle retrieval is implemented in [tradingagents/execution/hyperliquid.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/execution/hyperliquid.py).

Important replay detail:

- replay regime classification uses the replay bars directly
- it does not re-fetch a separate market stream for classification
- `candidate-only` replay evaluates deterministic candidates without invoking the graph
- `full-llm` replay follows the same routed single-strategy path as live
- `candidate-only` replay can compare all strategies allowed by the active regime unless `--strategy` is set

Replay metrics currently include:

- signal count
- simulated trades vs no-trade
- regime grouping
- strategy grouping
- passive forward behavior by regime, even for bars with no trade
- top skip reasons by regime
- top candidate and filter rejection reasons by strategy
- `R_1`, `R_2`, `R_4`, `R_8`
- `MAE` / `MFE`

Current replay summary sections:

- overall run summary
- `By Regime`
- `Regime Behavior`
- `By Strategy`
- `Top Skip Reasons`

Replay modes:

- `regime-only`: classify regimes only
- `candidate-only`: evaluate deterministic candidates without invoking the LLM graph
- `full-llm`: run the same routed strategy flow as live, including the graph

Replay diagnostics are meant to answer three different questions:

- `regime-only`: how often is the market being classified into each regime?
- `candidate-only`: how often does each deterministic strategy actually find a setup?
- `full-llm`: how does the full bot behave after deterministic routing and gating?

### How To Read Replay Output

The replay CLI currently prints several tables. They should be interpreted as follows:

- `Bot Replay Summary`
  - high-level run metadata and counts
  - `Simulated trades` means a setup existed and the replay fill model would have filled it
  - `No trade` means either no setup existed, the mode intentionally stayed flat, or a candidate never filled
- `By Regime`
  - how many evaluated bars fell into each regime
  - in deterministic modes, this helps separate classification frequency from actual trade frequency
- `Regime Behavior`
  - passive forward behavior of all bars in that regime, even if the bot stayed flat
  - use this to study whether `low_quality`, `range`, or trend labels behave differently after classification
  - `Avg Fwd4 %` / `Avg Fwd8 %` show directional drift
  - `Avg Abs4 %` and `Avg Range8 %` show movement magnitude regardless of direction
- `By Strategy`
  - how many opportunities were evaluated for each strategy family
  - in `candidate-only`, this is the main table for comparing deterministic signal frequency and simulated performance
- `Top Skip Reasons`
  - fastest way to diagnose why signals are missing
  - `regime:*` rows explain why bars landed in a regime bucket
  - `candidate:*` rows explain why a strategy did not produce a candidate
  - `filter:*` rows explain why a structured decision was flattened after candidate approval

Practical guidance:

- If `range_fade` counts are low, first inspect `By Regime` and `Top Skip Reasons` before changing range-fade rules.
- If `low_quality` is large, use `Regime Behavior` before loosening thresholds. Do not force more `range` labels without evidence.
- If `candidate-only` looks promising but `full-llm` degrades, the issue is likely prompt or post-parse behavior rather than regime classification.

## State and Diagnostics

Bot runtime state is stored via:

- [tradingagents/bot/models.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/models.py)
- [tradingagents/bot/state.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/state.py)

Relevant persisted fields:

- `regime_snapshot`
- `candidate_snapshot`
- `last_decision_diagnostics`
- exchange/order/position state

Runtime graph state now also carries:

- `allowed_setup_families`
- routed `setup_family`
- regime summary/context
- candidate summary/context

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

The journal schema is migration-safe for the current additions:

- `allowed_setup_families`
- `selected_setup_family`

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

Current practical interpretation of the regime taxonomy:

- `trend_up` / `trend_down`: tradable trend conditions
- `range`: explicitly bounded mean-reversion conditions
- `high_volatility_event`: hard no-trade due to event-style instability
- `low_quality`: ambiguous or transitional structure; intentionally not forced into range or trend

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

Replay only one strategy:

```bash
python cli/main.py bot-replay \
  --symbol BTC-USD \
  --timeframe 1h \
  --start "2026-03-01 00:00" \
  --end "2026-04-11 23:00" \
  --data-source hyperliquid \
  --mode candidate-only \
  --strategy range_fade \
  --testnet
```

Research-oriented replay for dense regime sampling:

```bash
python cli/main.py bot-replay \
  --symbol BTC-USD \
  --timeframe 1h \
  --start "2026-03-01 00:00" \
  --end "2026-04-11 23:00" \
  --data-source hyperliquid \
  --analysis-interval-minutes 60 \
  --mode candidate-only \
  --testnet
```

## Session Rehydration Hint

If a future Codex session needs context quickly, point it to:

- this document: [docs/bot_architecture.md](/Users/evlogialeksandrov/repos/TradingAgents/docs/bot_architecture.md)
- the live orchestration path: [tradingagents/bot/runner.py](/Users/evlogialeksandrov/repos/TradingAgents/tradingagents/bot/runner.py)

That should be enough to recover the current architecture without re-deriving it from scratch.
