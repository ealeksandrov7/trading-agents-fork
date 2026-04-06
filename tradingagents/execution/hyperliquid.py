from __future__ import annotations

import os
from typing import Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from .models import EntryMode, ExecutionMode, OrderIntent, OrderPreview, OrderStatus, Position, TradeAction


class HyperliquidExecutionError(RuntimeError):
    """Raised when Hyperliquid configuration or execution fails."""


class HyperliquidExecutor:
    def __init__(
        self,
        *,
        wallet_address: Optional[str] = None,
        private_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.base_url = base_url or os.getenv("HYPERLIQUID_BASE_URL")
        self.wallet_address = wallet_address or os.getenv("HYPERLIQUID_WALLET_ADDRESS")
        self.private_key = private_key or os.getenv("HYPERLIQUID_PRIVATE_KEY")
        self.info = Info(base_url=self.base_url, skip_ws=True)
        self.exchange: Optional[Exchange] = None

        if self.private_key:
            wallet = Account.from_key(self.private_key)
            self.exchange = Exchange(
                wallet=wallet,
                base_url=self.base_url,
                account_address=self.wallet_address or wallet.address,
            )
            self.wallet_address = self.wallet_address or wallet.address

    def get_mark_price(self, symbol: str) -> float:
        mids = self.info.all_mids()
        if symbol not in mids:
            raise HyperliquidExecutionError(f"no Hyperliquid mid price for {symbol}")
        return float(mids[symbol])

    def get_open_position(self) -> Optional[Position]:
        if not self.wallet_address:
            return None

        state = self.info.user_state(self.wallet_address)
        for asset_position in state.get("assetPositions", []):
            position = asset_position.get("position", {})
            size = float(position.get("szi", "0") or "0")
            if size == 0:
                continue

            side = TradeAction.LONG if size > 0 else TradeAction.SHORT
            return Position(
                symbol=position["coin"],
                side=side,
                size=abs(size),
                entry_price=float(position.get("entryPx") or 0.0),
                stop_loss=None,
                take_profit=None,
                opened_at="",
                mode=ExecutionMode.LIVE,
            )
        return None

    def execute(self, intent: OrderIntent) -> OrderPreview:
        if self.exchange is None:
            raise HyperliquidExecutionError(
                "live execution requires HYPERLIQUID_PRIVATE_KEY"
            )

        self.exchange.update_leverage(intent.leverage, intent.symbol, is_cross=True)

        if intent.action == TradeAction.FLAT:
            raw = self.exchange.market_close(intent.symbol, sz=intent.size)
            return OrderPreview(
                status=OrderStatus.FILLED,
                mode=ExecutionMode.LIVE,
                symbol=intent.symbol,
                action=intent.action,
                message="Submitted live market close.",
                reference_price=intent.reference_price,
                size=intent.size,
                leverage=intent.leverage,
                raw_response=raw,
            )

        if intent.entry_mode == EntryMode.MARKET:
            raw = self.exchange.market_open(
                intent.symbol,
                is_buy=intent.action == TradeAction.LONG,
                sz=intent.size,
            )
            message = "Submitted live market order."
            status = OrderStatus.FILLED
        else:
            limit_price = self._resolve_limit_price(intent)
            raw = self.exchange.order(
                intent.symbol,
                is_buy=intent.action == TradeAction.LONG,
                sz=intent.size,
                limit_px=limit_price,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=False,
            )
            message = "Submitted live resting limit order."
            status = OrderStatus.PREVIEW
        return OrderPreview(
            status=status,
            mode=ExecutionMode.LIVE,
            symbol=intent.symbol,
            action=intent.action,
            message=message,
            reference_price=self._resolve_limit_price(intent)
            if intent.entry_mode != EntryMode.MARKET
            else intent.reference_price,
            size=intent.size,
            leverage=intent.leverage,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            raw_response=raw,
        )

    def _resolve_limit_price(self, intent: OrderIntent) -> float:
        if intent.limit_price is not None:
            return intent.limit_price
        if intent.limit_zone_low is not None and intent.limit_zone_high is not None:
            return (intent.limit_zone_low + intent.limit_zone_high) / 2
        return intent.reference_price
