"""
autonomous/execution/alpaca_bridge.py
======================================
Translates a TradingAgents PortfolioDecision (5-tier rating) into concrete
Alpaca brokerage orders.

Supported ratings → actions
---------------------------
  Buy         → Full-size market buy order
  Overweight  → Partial buy (OVERWEIGHT_FRACTION of max position)
  Hold        → No new order; maintain existing position
  Underweight → Reduce existing position to UNDERWEIGHT_FRACTION
  Sell        → Close entire position (market sell)

The bridge operates in paper trading mode by default. Set
``paper=False`` only after thorough backtesting.

Usage
-----
    from autonomous.execution.alpaca_bridge import AlpacaBridge

    bridge = AlpacaBridge(
        api_key="YOUR_ALPACA_KEY",
        secret_key="YOUR_ALPACA_SECRET",
        paper=True,
    )
    result = bridge.execute_decision(
        ticker="NVDA",
        rating="Buy",
        entry_price=900.0,
        stop_loss=860.0,
        position_sizing="5% of portfolio",
    )
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Position sizing constants
DEFAULT_POSITION_PCT = 0.05        # 5% of portfolio per position by default
OVERWEIGHT_FRACTION = 0.60         # 60% of full position for Overweight
UNDERWEIGHT_FRACTION = 0.40        # Reduce to 40% of current position for Underweight
MAX_POSITION_PCT = 0.10            # Hard cap: never exceed 10% in a single stock

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL  = "https://api.alpaca.markets"


class AlpacaBridge:
    """
    Executes TradingAgents decisions via the Alpaca brokerage API.

    Parameters
    ----------
    api_key : str
        Alpaca API key. Falls back to ``ALPACA_API_KEY`` env var.
    secret_key : str
        Alpaca secret key. Falls back to ``ALPACA_SECRET_KEY`` env var.
    paper : bool
        If True (default), use the paper trading endpoint.
    max_position_pct : float
        Maximum fraction of portfolio to allocate to any single position.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: bool = True,
        max_position_pct: float = MAX_POSITION_PCT,
    ):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.paper = paper
        self.max_position_pct = max_position_pct
        self.base_url = ALPACA_PAPER_URL if paper else ALPACA_LIVE_URL
        self._api = None

        if not self.api_key or not self.secret_key:
            logger.warning(
                "Alpaca credentials not set. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY env vars. Running in dry-run mode."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def execute_decision(
        self,
        ticker: str,
        rating: str,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        position_sizing: Optional[str] = None,
        investment_thesis: Optional[str] = None,
    ) -> Dict:
        """
        Map a TradingAgents rating to an Alpaca order and submit it.

        Parameters
        ----------
        ticker : str
            Stock ticker symbol.
        rating : str
            One of: Buy, Overweight, Hold, Underweight, Sell.
        entry_price : float, optional
            Suggested entry price from TradingAgents. Used for limit orders.
        stop_loss : float, optional
            Stop-loss price from TradingAgents.
        position_sizing : str, optional
            Human-readable sizing guidance from TradingAgents PM.
        investment_thesis : str, optional
            Full reasoning text — logged for audit trail.

        Returns
        -------
        dict
            Order result with keys: ticker, action, qty, order_id, status, message.
        """
        rating = rating.strip().title()
        logger.info(
            "[AlpacaBridge] %s → rating=%s | entry=%s | stop=%s | sizing=%s",
            ticker, rating, entry_price, stop_loss, position_sizing,
        )

        if rating in ("Buy", "Overweight"):
            return self._handle_buy(ticker, rating, entry_price, stop_loss, position_sizing)
        elif rating == "Hold":
            return self._handle_hold(ticker)
        elif rating == "Underweight":
            return self._handle_underweight(ticker)
        elif rating == "Sell":
            return self._handle_sell(ticker)
        else:
            logger.warning("Unknown rating '%s' for %s — no action taken.", rating, ticker)
            return {"ticker": ticker, "action": "none", "status": "unknown_rating", "message": f"Unrecognised rating: {rating}"}

    def get_portfolio_summary(self) -> Dict:
        """Return current account equity, cash, and open positions."""
        api = self._get_api()
        if api is None:
            return {"error": "Alpaca not configured"}
        try:
            account = api.get_account()
            positions = api.list_positions()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "positions": [
                    {
                        "ticker": p.symbol,
                        "qty": float(p.qty),
                        "market_value": float(p.market_value),
                        "unrealized_pl": float(p.unrealized_pl),
                        "unrealized_plpc": float(p.unrealized_plpc),
                        "current_price": float(p.current_price),
                    }
                    for p in positions
                ],
            }
        except Exception as e:
            logger.error("Failed to fetch portfolio summary: %s", e)
            return {"error": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Order handlers
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_buy(
        self,
        ticker: str,
        rating: str,
        entry_price: Optional[float],
        stop_loss: Optional[float],
        position_sizing: Optional[str],
    ) -> Dict:
        """Place a buy order for Buy or Overweight rating."""
        api = self._get_api()
        if api is None:
            return self._dry_run_result(ticker, "buy", rating)

        try:
            equity = float(api.get_account().equity)
            fraction = 1.0 if rating == "Buy" else OVERWEIGHT_FRACTION
            alloc_pct = min(DEFAULT_POSITION_PCT * fraction, self.max_position_pct)

            # Parse explicit sizing if provided (e.g. "5% of portfolio")
            if position_sizing:
                parsed = self._parse_position_pct(position_sizing)
                if parsed:
                    alloc_pct = min(parsed * fraction, self.max_position_pct)

            # Determine price for qty calculation
            price = entry_price or self._get_current_price(api, ticker)
            if not price or price <= 0:
                return {"ticker": ticker, "action": "buy", "status": "error", "message": "Could not determine price"}

            qty = int((equity * alloc_pct) / price)
            if qty <= 0:
                return {"ticker": ticker, "action": "buy", "status": "skipped", "message": "Calculated qty=0, insufficient funds"}

            order_kwargs = {
                "symbol": ticker,
                "qty": qty,
                "side": "buy",
                "type": "limit" if entry_price else "market",
                "time_in_force": "day",
            }
            if entry_price:
                order_kwargs["limit_price"] = round(entry_price, 2)

            order = api.submit_order(**order_kwargs)

            # Attach stop-loss as a separate bracket order if provided
            if stop_loss:
                self._place_stop_loss(api, ticker, qty, stop_loss)

            logger.info(
                "[AlpacaBridge] BUY %d x %s @ %s (order_id=%s)",
                qty, ticker, entry_price or "market", order.id,
            )
            return {
                "ticker": ticker,
                "action": "buy",
                "qty": qty,
                "order_id": order.id,
                "status": order.status,
                "message": f"{rating} — bought {qty} shares",
            }

        except Exception as e:
            logger.error("Buy order failed for %s: %s", ticker, e)
            return {"ticker": ticker, "action": "buy", "status": "error", "message": str(e)}

    def _handle_hold(self, ticker: str) -> Dict:
        """No action for Hold rating."""
        logger.info("[AlpacaBridge] HOLD %s — no action taken.", ticker)
        return {
            "ticker": ticker,
            "action": "hold",
            "status": "no_action",
            "message": "Hold — maintaining existing position",
        }

    def _handle_underweight(self, ticker: str) -> Dict:
        """Reduce existing position to UNDERWEIGHT_FRACTION."""
        api = self._get_api()
        if api is None:
            return self._dry_run_result(ticker, "reduce", "Underweight")

        try:
            position = api.get_position(ticker)
            current_qty = int(float(position.qty))
            target_qty = int(current_qty * UNDERWEIGHT_FRACTION)
            sell_qty = current_qty - target_qty

            if sell_qty <= 0:
                return {"ticker": ticker, "action": "reduce", "status": "skipped", "message": "Position already at target"}

            order = api.submit_order(
                symbol=ticker,
                qty=sell_qty,
                side="sell",
                type="market",
                time_in_force="day",
            )
            logger.info("[AlpacaBridge] REDUCE %d x %s (order_id=%s)", sell_qty, ticker, order.id)
            return {
                "ticker": ticker,
                "action": "reduce",
                "qty": sell_qty,
                "order_id": order.id,
                "status": order.status,
                "message": f"Underweight — sold {sell_qty} shares (reduced to {UNDERWEIGHT_FRACTION*100:.0f}%)",
            }
        except Exception as e:
            if "position does not exist" in str(e).lower():
                return {"ticker": ticker, "action": "reduce", "status": "no_position", "message": "No existing position to reduce"}
            logger.error("Reduce order failed for %s: %s", ticker, e)
            return {"ticker": ticker, "action": "reduce", "status": "error", "message": str(e)}

    def _handle_sell(self, ticker: str) -> Dict:
        """Close entire position for Sell rating."""
        api = self._get_api()
        if api is None:
            return self._dry_run_result(ticker, "sell", "Sell")

        try:
            order = api.close_position(ticker)
            logger.info("[AlpacaBridge] SELL (close) %s (order_id=%s)", ticker, order.id)
            return {
                "ticker": ticker,
                "action": "sell",
                "order_id": order.id,
                "status": order.status,
                "message": "Sell — closed entire position",
            }
        except Exception as e:
            if "position does not exist" in str(e).lower():
                return {"ticker": ticker, "action": "sell", "status": "no_position", "message": "No position to close"}
            logger.error("Sell order failed for %s: %s", ticker, e)
            return {"ticker": ticker, "action": "sell", "status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_api(self):
        """Lazily initialise the Alpaca REST client."""
        if self._api is not None:
            return self._api
        if not self.api_key or not self.secret_key:
            return None
        try:
            import alpaca_trade_api as tradeapi
            self._api = tradeapi.REST(
                self.api_key,
                self.secret_key,
                self.base_url,
                api_version="v2",
            )
            return self._api
        except ImportError:
            logger.error(
                "alpaca-trade-api not installed. Run: pip install alpaca-trade-api"
            )
            return None

    def _get_current_price(self, api, ticker: str) -> Optional[float]:
        """Fetch the latest trade price for a ticker."""
        try:
            trade = api.get_latest_trade(ticker)
            return float(trade.price)
        except Exception as e:
            logger.warning("Could not fetch price for %s: %s", ticker, e)
            return None

    def _place_stop_loss(self, api, ticker: str, qty: int, stop_price: float) -> None:
        """Place a stop-loss sell order for an existing position."""
        try:
            api.submit_order(
                symbol=ticker,
                qty=qty,
                side="sell",
                type="stop",
                stop_price=round(stop_price, 2),
                time_in_force="gtc",
            )
            logger.info("[AlpacaBridge] Stop-loss set for %s @ %.2f", ticker, stop_price)
        except Exception as e:
            logger.warning("Could not place stop-loss for %s: %s", ticker, e)

    def _parse_position_pct(self, sizing_str: str) -> Optional[float]:
        """
        Extract a percentage from a sizing string like '5% of portfolio'.
        Returns a float between 0 and 1, or None if unparseable.
        """
        import re
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", sizing_str)
        if match:
            return float(match.group(1)) / 100.0
        return None

    def _dry_run_result(self, ticker: str, action: str, rating: str) -> Dict:
        """Return a simulated result when Alpaca is not configured."""
        logger.info("[AlpacaBridge] DRY RUN: %s %s (rating=%s)", action.upper(), ticker, rating)
        return {
            "ticker": ticker,
            "action": action,
            "status": "dry_run",
            "message": f"Dry run — Alpaca not configured. Would {action} {ticker} ({rating})",
        }
